#!/usr/bin/env python3
"""Device profile loader (builder/lib/devprofile.py): the KANO profile loads and gives the entries the builder made
before profiles existed (the former builder/image.py logic is kept below as the reference, differing in quiet only by
xe.force_probe carrying the KANO SKU iGPU IDs, 46a6 first, and in debug also by omitting the 3 console= tokens so the
debug command line fits the lapis x86 budget with headroom); broken profiles are refused (missing / unknown keys,
wrong types, weakened safety rules); the KANO hooks and firmware list match the former builder/vendor.sh expectations
plus the two RPL SOF firmware files. No image, no network, no root. The optional extract-ikconfig
differential runs a vendored, sha256-pinned copy of the kernel script (fixtures/extract-ikconfig, no network) and
skips unless GNU grep/tr are available and zcat and gunzip can decompress a gzip
member from stdin while keeping the trailing-garbage exit status 2.
usage: tests/builder/profile-test.py"""
import ast, contextlib, copy, hashlib, importlib.util, io, os, re, shlex, shutil, subprocess, sys, tempfile, tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder" / "lib"))
sys.dont_write_bytecode = True
import devprofile                                    # noqa: E402

passed = failed = 0


def check(name, ok):
    global passed, failed
    print(("PASS " if ok else "FAIL ") + name)
    passed += bool(ok); failed += not ok


# Pinned verbatim copy of the kernel's adl_p_desc PCI ID macros from lapis kernel commit
# 3938c8d97142d5cf31543aa96f864a2cf832dda0 (pinned as KSRC_COMMIT in builder/kmod/build-ac.sh:20):
# - include/drm/intel/pciids.h:668-722 (comment on 668; #define INTEL_ADLP_IDS at 669, INTEL_RPLU_IDS at 709,
#   INTEL_RPLP_IDS at 717). ADL-N and RPL-S are in the excerpt so a drift into those groups is visible.
# - drivers/gpu/drm/xe/xe_pci.c:366-369 (which macro groups bind to &adl_p_desc)
KERNEL_COMMIT = "3938c8d97142d5cf31543aa96f864a2cf832dda0"
KERNEL_PCIIDS_H = """\
/* ADL-P */
#define INTEL_ADLP_IDS(MACRO__, ...) \\
\tMACRO__(0x46A0, ## __VA_ARGS__), \\
\tMACRO__(0x46A1, ## __VA_ARGS__), \\
\tMACRO__(0x46A2, ## __VA_ARGS__), \\
\tMACRO__(0x46A3, ## __VA_ARGS__), \\
\tMACRO__(0x46A6, ## __VA_ARGS__), \\
\tMACRO__(0x46A8, ## __VA_ARGS__), \\
\tMACRO__(0x46AA, ## __VA_ARGS__), \\
\tMACRO__(0x462A, ## __VA_ARGS__), \\
\tMACRO__(0x4626, ## __VA_ARGS__), \\
\tMACRO__(0x4628, ## __VA_ARGS__), \\
\tMACRO__(0x46B0, ## __VA_ARGS__), \\
\tMACRO__(0x46B1, ## __VA_ARGS__), \\
\tMACRO__(0x46B2, ## __VA_ARGS__), \\
\tMACRO__(0x46B3, ## __VA_ARGS__), \\
\tMACRO__(0x46C0, ## __VA_ARGS__), \\
\tMACRO__(0x46C1, ## __VA_ARGS__), \\
\tMACRO__(0x46C2, ## __VA_ARGS__), \\
\tMACRO__(0x46C3, ## __VA_ARGS__)

/* ADL-N */
#define INTEL_ADLN_IDS(MACRO__, ...) \\
\tMACRO__(0x46D0, ## __VA_ARGS__), \\
\tMACRO__(0x46D1, ## __VA_ARGS__), \\
\tMACRO__(0x46D2, ## __VA_ARGS__), \\
\tMACRO__(0x46D3, ## __VA_ARGS__), \\
\tMACRO__(0x46D4, ## __VA_ARGS__)

/* RPL-S */
#define INTEL_RPLS_IDS(MACRO__, ...) \\
\tMACRO__(0xA780, ## __VA_ARGS__), \\
\tMACRO__(0xA781, ## __VA_ARGS__), \\
\tMACRO__(0xA782, ## __VA_ARGS__), \\
\tMACRO__(0xA783, ## __VA_ARGS__), \\
\tMACRO__(0xA788, ## __VA_ARGS__), \\
\tMACRO__(0xA789, ## __VA_ARGS__), \\
\tMACRO__(0xA78A, ## __VA_ARGS__), \\
\tMACRO__(0xA78B, ## __VA_ARGS__)

/* RPL-U */
#define INTEL_RPLU_IDS(MACRO__, ...) \\
\tMACRO__(0xA721, ## __VA_ARGS__), \\
\tMACRO__(0xA7A1, ## __VA_ARGS__), \\
\tMACRO__(0xA7A9, ## __VA_ARGS__), \\
\tMACRO__(0xA7AC, ## __VA_ARGS__), \\
\tMACRO__(0xA7AD, ## __VA_ARGS__)

/* RPL-P */
#define INTEL_RPLP_IDS(MACRO__, ...) \\
\tMACRO__(0xA720, ## __VA_ARGS__), \\
\tMACRO__(0xA7A0, ## __VA_ARGS__), \\
\tMACRO__(0xA7A8, ## __VA_ARGS__), \\
\tMACRO__(0xA7AA, ## __VA_ARGS__), \\
\tMACRO__(0xA7AB, ## __VA_ARGS__)
"""
KERNEL_XE_PCI_C = """\
\tINTEL_ADLP_IDS(INTEL_VGA_DEVICE, &adl_p_desc),
\tINTEL_ADLN_IDS(INTEL_VGA_DEVICE, &adl_n_desc),
\tINTEL_RPLU_IDS(INTEL_VGA_DEVICE, &adl_p_desc),
\tINTEL_RPLP_IDS(INTEL_VGA_DEVICE, &adl_p_desc),
"""


def derive_adlp_desc_ids(pciids_h=KERNEL_PCIIDS_H, xe_pci_c=KERNEL_XE_PCI_C):
    """Re-derive the xe.force_probe ID list for adl_p_desc from the kernel's pciids.h and xe_pci.c macro lines, keeping
    46a6 first and preserving macro order for the rest."""
    macros = {}
    for m in re.finditer(r"^#define\s+(INTEL_\w+_IDS)\(MACRO__,\s*\.\.\.\)\s*\\\n((?:\tMACRO__\(0x[0-9A-Fa-f]{4},[^\n]*\n?)+)",
                         pciids_h, re.M):
        macros[m.group(1)] = [x.lower() for x in re.findall(r"MACRO__\(0x([0-9A-Fa-f]{4}),", m.group(2))]
    groups = re.findall(r"^\s*(INTEL_\w+_IDS)\(INTEL_VGA_DEVICE,\s*&adl_p_desc\),", xe_pci_c, re.M)
    assert groups == ["INTEL_ADLP_IDS", "INTEL_RPLU_IDS", "INTEL_RPLP_IDS"], groups
    raw = [x for g in groups for x in macros[g]]
    assert "46a6" in raw and len(raw) == len(set(raw)), raw
    return ["46a6"] + [x for x in raw if x != "46a6"]


ADLP_DESC_IDS = derive_adlp_desc_ids()
# 7 ids map to retail KANO SKUs; 5 are platform candidates (ADL-P/RPL-U macro members),
# KANO mapping unconfirmed — see docs/COMPATIBILITY.md. The retail mapping is the one
# written in docs/STATUS-KANO.md and docs/COMPATIBILITY.md (Acer retail model numbers and
# coreboot's KANO list). Not the whole adl_p_desc list. Order here is not significant; the
# profile must follow macro order with 46a6 first (see kano_probe_ids).
KANO_SKU_IDS = (
    "46a6",  # retail KANO SKU: verified i7-1260P (CBI SKU 131073); also coreboot-listed i5-1240P / i7-1280P
    "46a3",  # platform candidate (ADL-P/RPL-U macro member); KANO mapping unconfirmed
    "46a8",  # retail KANO SKU: i5-1235U; coreboot-listed i7-1255U
    "4626",  # retail KANO SKU: 1-channel memory variant of 46a6
    "4628",  # retail KANO SKU: 1-channel memory variant of 46a8
    "46b2",  # platform candidate (ADL-P/RPL-U macro member); KANO mapping unconfirmed
    "46b3",  # retail KANO SKU: i3-1215U
    "a721",  # platform candidate (ADL-P/RPL-U macro member); KANO mapping unconfirmed
    "a7a1",  # retail KANO SKU: i5-1335U / i7-1355U
    "a7a9",  # retail KANO SKU: i3-1315U
    "a7ac",  # platform candidate (ADL-P/RPL-U macro member); KANO mapping unconfirmed
    "a7ad",  # platform candidate (ADL-P/RPL-U macro member); KANO mapping unconfirmed
)


def kano_probe_ids(adlp_ids, sku_ids):
    """46a6 first, then the other SKU IDs in adl_p_desc order. None if any SKU ID is outside adl_p_desc."""
    sku = list(sku_ids)
    if not sku or sku.count("46a6") != 1 or len(sku) != len(set(sku)):
        return None
    if any(x not in adlp_ids for x in sku):
        return None
    return ["46a6"] + [x for x in adlp_ids if x in set(sku) and x != "46a6"]


def sku_table_igpu_ids(text):
    """Backtick-wrapped 4-digit hex IDs from the iGPU column of docs/STATUS-KANO.md's SKU table."""
    ids = []
    in_table = False
    for line in text.splitlines():
        if line.startswith("| 機型"):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break
        if line.startswith("|---"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 3:
            return None
        ids.extend(re.findall(r"`([0-9a-f]{4})`", cols[2]))
    return ids


def force_probe_ids(prof):
    opts = [o for k in ("add", "tail") for o in prof["cmdline"][k] if o.startswith("xe.force_probe=")]
    if len(opts) != 1 or "*" in opts[0]:
        return None
    ids = opts[0].split("=", 1)[1].split(",")
    if not ids or any(not re.fullmatch(r"[0-9a-f]{4}", x) for x in ids) or len(ids) != len(set(ids)):
        return None
    return ids


KANO_IDS = kano_probe_ids(ADLP_DESC_IDS, KANO_SKU_IDS)
XE_FORCE_PROBE_OPT = "xe.force_probe=" + ",".join(KANO_IDS or [])

# Stock vendor_boot_a command line of lapis build 16452207 (checked by SHA-256 in builder/image.py:131), plus a synthetic
# variant that also carries an initcall_blacklist= token to verify stock_tokens() strips it.
REAL_VCMD = ("snd_intel_sdw_acpi.sdw_link_mask=0x5 kvm-intel.pkvm=1 intel_iommu=igfx_off xe.mem_monitor_enabled=1 "
             "console=null 8250.nr_uarts=4 console_msg_format=syslog log_buf_len=1M cgroup_disable=pressure "
             "root=/dev/ram0 firmware_class.path=/vendor/firmware reserve_mem=20M:2M:trace "
             "trace_instance=boot_mapped^traceoff@trace drm.trace=0x106 sched_proxy_exec=1 "
             "module_blacklist=cros_ec_debugfs proc_mem.force_override=ptrace binder.impl=rust no_console_suspend "
             "softlockup_panic=1 nmi_watchdog=panic pm.dpm_watchdog_enabled=1 cpufreq.default_governor=performance "
             "fred=on spi-intel.writeable=1 spi-intel.ignore_protection_status=1 ia32_emulation=false vsyscall=none "
             "trace_instance=backup=boot_mapped xe.enable_psr=1 snd_intel_dspcfg.dsp_driver=3 "
             "snd_sof_intel_hda_common.sof_use_tplg_nhlt=1 snd_sof_pci.ipc_type=1 snd_intel_sdw_acpi.sdw_link_mask=0xF "
             "xe.enable_panel_replay=0 bootconfig")
VCMD = REAL_VCMD.replace("bootconfig", "initcall_blacklist=foo bootconfig")
REDUNDANT_CONSOLES = ("console=ttyS0,115200n8", "console=ttyS4,115200n8", "console=uart8250,mmio32,0xfe03e000,115200n8")


def former_entries(vcmd):
    """builder/image.py before profiles (2026-10-12), fed with the base ESP options it read then: the stock options without
    the first 'console=null ' plus the base image's milestone-M serial options (base/gbos_base.py loader-confs)."""
    esp = vcmd.replace("console=null ", "", 1) + (" loglevel=8 ignore_loglevel console=ttyS0,115200n8 console=ttyS4,115200n8"
                                                  " console=uart8250,mmio32,0xfe03e000,115200n8"
                                                  " earlycon=uart8250,mmio32,0xfe03e000,115200n8 xe.force_probe=46a6")
    opts = esp.split()
    drop = {"kvm-intel.pkvm=1", "fred=on", "spi-intel.writeable=1", "spi-intel.ignore_protection_status=1",
            "softlockup_panic=1", "nmi_watchdog=panic", "pm.dpm_watchdog_enabled=1", "cpufreq.default_governor=performance",
            "snd_intel_sdw_acpi.sdw_link_mask=0x5", "xe.enable_psr=1", "snd_sof_pci.ipc_type=1"}
    opts = [o for o in opts if o not in drop and not o.startswith(("module_blacklist=", "initcall_blacklist="))]
    BL = ["cros_ec_debugfs", "tpm_tis_i2c_cr50", "cros_ec_lpcs", "cros_ec_usb", "spi_intel_pci", "spi_intel", "i2c_dev",
          "thunderbolt", "tpm", "tpm_tis", "tpm_tis_core", "tpm_tis_spi", "cros_ec", "cros_ec_proto",
          "cros_ec_dev", "cros_ec_chardev", "gsmi", "chromeos_acpi", "nvram", "mtd", "spi_nor", "spidev",
          "intel_quicki2c", "intel_quickspi", "at24", "cros_hps_i2c", "i2c_cros_ec_tunnel", "cros_peripheral_charger",
          "cros_ec_spi", "cros_ec_i2c", "cros_ec_ishtp", "cros_ec_sysfs", "cros_ec_vbc", "cros_ec_typec", "cros_typec_switch",
          "cros_usbpd_notify", "cros_usbpd_charger", "cros_usbpd_logger", "cros_charge_control", "cros_ec_ucsi", "cros_ec_keyb",
          "cros_ec_sensorhub", "cros_ec_sensors_core", "cros_ec_sensors", "cros_ec_accel_legacy", "cros_ec_baro",
          "cros_ec_lid_angle", "cros_ec_light_prox", "cros_ec_lightbar", "cros_ec_pwrmon", "cros_ec_cec", "leds_cros_ec",
          "hid_google_hammer", "wilco_ec", "wilco_charger", "wilco_ec_debugfs", "wilco_ec_telem", "rtc_wilco_ec"]
    BL.remove("cros_ec_proto")
    tail = ["initcall_blacklist=nvme_init,mei_me_driver_init", "efi=noruntime", "efi_pstore.pstore_disable=1", "panic=0",
            "softlockup_panic=0", "nmi_watchdog=0", "pm.dpm_watchdog_enabled=0", "cpufreq.default_governor=schedutil",
            "xe.enable_dpt=0", "xe.enable_psr=0", "xe.enable_fbc=0", "snd_sof_pci.ipc_type=0"]
    normal = opts + ["module_blacklist=" + ",".join(BL)] + tail
    debug = [o for o in opts if o != "8250.nr_uarts=4"] + ["module_blacklist=" + ",".join(BL + ["8250_dw", "intel_lpss_acpi"])] + \
        tail + ["keep_bootcon", "printk.devkmsg=on", "pci-stub.ids=8086:51a8,8086:51a9,8086:51aa,8086:51ab"]
    quiet = [o for o in debug if not o.startswith(("console=", "earlycon=")) and o not in
             ("keep_bootcon", "printk.devkmsg=on", "ignore_loglevel", "loglevel=8")] + ["loglevel=4"]
    return {"normal": normal, "debug": debug, "quiet": quiet}


kano = devprofile.load(ROOT / "profiles" / "kano")
check("profiles/kano loads and validates", kano["model"]["id"] == "kano" and kano["model"]["image_name"] == "googlebook-kano.img")
check("pinned KERNEL_COMMIT matches KSRC_COMMIT in builder/kmod/build-ac.sh",
      f"KSRC_COMMIT={KERNEL_COMMIT}\n" in (ROOT / "builder/kmod/build-ac.sh").read_text())
check("REAL_VCMD matches lapis build 16452207 vendor_boot_a SHA-256",
      hashlib.sha256(REAL_VCMD.encode()).hexdigest() == "311e95df564037a863af096fb6265f15e8340e4b60e2736676e43d81d876aee7")
_pub_spec = importlib.util.spec_from_file_location(
    "_public_export_test", ROOT / "tests/stick/public-export-test.py"
)
_pub_mod = importlib.util.module_from_spec(_pub_spec)
_pub_spec.loader.exec_module(_pub_mod)


def is_public_export_tree(root=ROOT):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return bool(_pub_mod.is_public_export_tree(root))
    except SystemExit:
        return False


def is_release_archive_tree(root=ROOT):
    return (
        not (root / ".git").exists()
        and not (root / "tools/release/PUBLIC-EXPORT").exists()
        and not (root / "research").exists()
        and not (root / "tools/release/private-patterns.txt").exists()
        and not (root / "tools/release/make-release.sh").exists()
    )


cfg_path = ROOT / "research/amd/lapis-kernel.config"
if cfg_path.is_file():
    cfg = cfg_path.read_text()
    cfg_cmdline = re.search(r'^CONFIG_CMDLINE="([^"]*)"', cfg, re.M)
    check("pinned CONFIG_CMDLINE is lapis 6.18.21 research/amd/lapis-kernel.config:522 and override is unset",
          cfg_cmdline is not None and cfg_cmdline.group(1) == devprofile.KERNEL_CONFIG_CMDLINE
          and "# CONFIG_CMDLINE_OVERRIDE is not set" in cfg
          and "Linux/x86_64 6.18.21 Kernel Configuration" in cfg)
elif is_public_export_tree(ROOT) or is_release_archive_tree(ROOT):
    # The release archive and the public export (tools/release/PUBLIC-EXPORT) do not ship research/.
    print("SKIP research/amd/lapis-kernel.config comparison (release archive / public export do not ship research/)")
else:
    check("research/amd/lapis-kernel.config is present in this checkout", False)
if os.environ.get("GBOS_PROFILE_SKIP_SUBTEST") == "1":
    print(f"profile sub-run: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
check("bootloader budget is COMMAND_LINE_SIZE 2048 - 1 - len(CONFIG_CMDLINE) - 1 separator space",
      devprofile.COMMAND_LINE_SIZE == 2048
      and devprofile.X86_BOOT_CMDLINE_MAX == 2048 - 1 - len(devprofile.KERNEL_CONFIG_CMDLINE) - 1
      and devprofile.X86_BOOT_CMDLINE_MAX == 1973
      and devprofile.CMDLINE_HEADROOM >= 64)
check("adl_p_desc re-derives to 28 IDs (ADLP + RPL-U + RPL-P), 46a6 first; ADL-N and RPL-S stay out",
      len(ADLP_DESC_IDS) == 28 and ADLP_DESC_IDS[0] == "46a6"
      and set(KANO_SKU_IDS) <= set(ADLP_DESC_IDS)
      and not ({"46d0", "a780"} & set(ADLP_DESC_IDS)))
table_ids = sku_table_igpu_ids((ROOT / "docs/STATUS-KANO.md").read_text())
probe_ids = force_probe_ids(kano)
check("xe.force_probe is the KANO SKU iGPU IDs (46a6 first, no '*'), each one in re-derived adl_p_desc",
      KANO_IDS is not None and probe_ids == KANO_IDS and set(probe_ids) == set(KANO_SKU_IDS) == set(table_ids)
      and all(x in ADLP_DESC_IDS for x in probe_ids))
dropped = derive_adlp_desc_ids(KERNEL_PCIIDS_H.replace("0x46A8", "0x46AF"))
check("drift guard: a listed SKU ID missing from re-derived adl_p_desc fails",
      "46a8" not in dropped and kano_probe_ids(dropped, KANO_SKU_IDS) is None)
check("drift guard: a SKU-table ID missing from xe.force_probe fails",
      table_ids is not None and "46a8" in table_ids and set(table_ids) - {"46a8"} != set(probe_ids))
for bad_Label, bad_opt in (("single 46a6", "xe.force_probe=46a6"),
                           ("wildcard *", "xe.force_probe=*"),
                           ("46a6 not first", "xe.force_probe=" + ",".join(KANO_IDS[1:] + ["46a6"])),
                           ("missing last SKU ID", "xe.force_probe=" + ",".join(KANO_IDS[:-1])),
                           ("extra ADL-N ID 46d0", "xe.force_probe=" + ",".join(KANO_IDS + ["46d0"])),
                           ("extra RPL-P ID a7a0", "xe.force_probe=" + ",".join(KANO_IDS + ["a7a0"])),
                           ("duplicate ID", "xe.force_probe=" + ",".join(KANO_IDS + ["46a8"])),
                           ("uppercase hex ID", "xe.force_probe=" + ",".join(KANO_IDS[:-1] + ["A7AD"]))):
    p_bad = copy.deepcopy(kano)
    p_bad["cmdline"]["add"] = [bad_opt if o.startswith("xe.force_probe=") else o for o in p_bad["cmdline"]["add"]]
    bad_ids = force_probe_ids(p_bad)
    check(f"xe.force_probe drift guard catches {bad_Label}",
          bad_ids != KANO_IDS or bad_ids is None or any(x not in ADLP_DESC_IDS for x in (bad_ids or [])))

new = devprofile.make_entries(kano, VCMD)
old = former_entries(VCMD)
check("KANO quiet entry options = former builder/image.py ones with only xe.force_probe set to the KANO SKU IDs",
      new["quiet"] == [XE_FORCE_PROBE_OPT if o == "xe.force_probe=46a6" else o for o in old["quiet"]]
      and old["quiet"].count("xe.force_probe=46a6") == 1)
for k in ("normal", "debug"):
    expected_k = [XE_FORCE_PROBE_OPT if o == "xe.force_probe=46a6" else o for o in old[k] if o not in REDUNDANT_CONSOLES]
    check(f"KANO {k} entry options = former builder/image.py ones with xe.force_probe set to the KANO SKU IDs and console= tokens dropped",
          new[k] == expected_k and "earlycon=uart8250,mmio32,0xfe03e000,115200n8" in new[k]
          and "keep_bootcon" in new["debug"]
          and old[k].count("xe.force_probe=46a6") == 1)
real_entries = devprofile.make_entries(kano, REAL_VCMD)
limit = devprofile.X86_BOOT_CMDLINE_MAX - devprofile.CMDLINE_HEADROOM
for k in ("debug", "quiet"):
    n = devprofile.boot_cmdline_len(real_entries[k])
    check(f"KANO {k} boot_command_line length ({n} B) fits kernel max {devprofile.X86_BOOT_CMDLINE_MAX} with headroom {devprofile.CMDLINE_HEADROOM} (limit {limit})",
          devprofile.cmdline_within_limit(n) and devprofile.X86_BOOT_CMDLINE_MAX - n >= devprofile.CMDLINE_HEADROOM)
image_py = (ROOT / "builder/image.py").read_text()
check("builder/image.py uses cmdline_within_limit and no longer accepts n < 2000",
      "cmdline_within_limit" in image_py and "n < 2000" not in image_py)
check("cmdline lengths 1974-1999 fail the builder limit (the old n < 2000 check would pass)",
      all(not devprofile.cmdline_within_limit(n) and n < 2000 for n in range(1974, 2000)))
check("a length inside the kernel max but inside the old 2000 gate fails once headroom is reserved",
      devprofile.cmdline_within_limit(limit) and not devprofile.cmdline_within_limit(limit + 1)
      and limit + 1 <= devprofile.X86_BOOT_CMDLINE_MAX and limit + 1 < 2000)
with_consoles = [XE_FORCE_PROBE_OPT if o == "xe.force_probe=46a6" else o for o in former_entries(REAL_VCMD)["debug"]]
untrimmed = devprofile.boot_cmdline_len(with_consoles)
check(f"restoring the 3 console= tokens ({untrimmed} B) does not fit with headroom {devprofile.CMDLINE_HEADROOM}, so they stay off",
      untrimmed > limit and "console=ttyS0,115200n8" not in real_entries["debug"]
      and "earlycon=uart8250,mmio32,0xfe03e000,115200n8" in real_entries["debug"]
      and "keep_bootcon" in real_entries["debug"])
for k in ("normal", "debug", "quiet"):
    check(f"KANO {k} entry does not carry erst_disable", "erst_disable" not in new[k])
mods, ics, plain = devprofile.guards(kano)
check("guards: 57 modules, initcalls nvme_init + mei_me_driver_init, efi=noruntime",
      len(mods) == 57 and ics == ["nvme_init", "mei_me_driver_init"] and plain == ["efi=noruntime"])
check("KANO entry titles unchanged (the plain entry was dropped in v0.2)", (kano["entries"]["quiet"], kano["entries"]["debug"]) ==
      ("Googlebook KANO quiet", "Googlebook KANO safe serial-debug") and set(kano["entries"]) == {"quiet", "debug"})

# firmware list = the former 9 pinned files + 2 RPL SOF files (sof-rpl.ri and sof-rpl-max98373-nau8825.tplg)
FORMER_FW = {"i915/adlp_dmc.bin": "3516de2e134ddcf3b319c75d2e437779fecbd58cbb77234bd6f297c544e92ccb",
             "i915/adlp_guc_70.bin": "b7a800cbdcdf687e7b1c81f3316d040ab75fa3e5c698ebc356f077af17462ef8",
             "i915/tgl_huc.bin": "dbb1316bac13a76427ffa7827e52b5b9affe8a4cadd23dc22e2a54e35c53bdad",
             "iwlwifi-so-a0-gf-a0-89.ucode": "7b53254bb28115f91b6eaa33b1f9a3526e32966105eaac587e259dd17790f384",
             "iwlwifi-so-a0-gf-a0.pnvm": "2aee7592ddbd6c88f1aa51be4ada188fce44765405e52d7f3056b46e62cf3f91",
             "intel/ibt-0040-0041.sfi": "8dcfb3a7c1592c11c3e80505d21ee78dbde3de0c830d8e3e856371c37717b62c",
             "intel/ibt-0040-0041.ddc": "fe272982577efdc289cfe3e8bedafca607c0b7d6c9df1aab1880e22ef930d077",
             "intel/sof/community/sof-adl.ri": "e3489dcb277e87eee8bc091ab7d51163a63c85541c22522775122b54263769f3",
             "intel/sof-tplg/sof-adl-max98373-nau8825.tplg": "4beb29400d9beed0c80bcc11b3f4e82aad70a54d6e851638c81eb087a10d4de5"}
RPL_FW = {"intel/sof/community/sof-rpl.ri": ("sof-bin", "sof-bin-2025.12.2/sof/community/sof-tgl.ri",
                                             "e3489dcb277e87eee8bc091ab7d51163a63c85541c22522775122b54263769f3"),
          "intel/sof-tplg/sof-rpl-max98373-nau8825.tplg": ("sof-bin", "sof-bin-2025.12.2/sof-tplg/sof-adl-max98373-nau8825.tplg",
                                                           "4beb29400d9beed0c80bcc11b3f4e82aad70a54d6e851638c81eb087a10d4de5")}
check("KANO firmware = the 9 former pinned files + 2 RPL SOF files (path, source and SHA-256)",
      {f["path"]: f["sha256"] for f in kano["firmware"][:9]} == FORMER_FW
      and {f["path"]: (f["from"], f["src"], f["sha256"]) for f in kano["firmware"][9:]} == RPL_FW
      and len(kano["firmware"]) == 11)
r = subprocess.run([sys.executable, ROOT / "builder/lib/devprofile.py", "firmware", ROOT / "profiles/kano"], capture_output=True, text=True)
check("devprofile.py firmware prints one '<path> <from> <src> <sha256>' line per file (11 files)",
      r.returncode == 0 and len(r.stdout.splitlines()) == 11 and all(len(l.split()) == 4 for l in r.stdout.splitlines()))
r = subprocess.run([sys.executable, ROOT / "builder/lib/devprofile.py", "get", ROOT / "profiles/kano", "model.image_name"],
                   capture_output=True, text=True)
check("devprofile.py get model.image_name", r.returncode == 0 and r.stdout == "googlebook-kano.img\n")

# the hooks report the vendor paths the former vendor.sh listed for the KANO, and vendor/ESP trees differ from
# the v0.2.0 profile only by the cmdline entries and the two added RPL SOF files
spec = importlib.util.spec_from_file_location("kano_hooks", ROOT / "profiles/kano/vendor.py")
hooks = importlib.util.module_from_spec(spec); spec.loader.exec_module(hooks)
hn, hc = hooks.expected(kano)
check("KANO hooks: new paths = the audio dir + 3 files and ueventd.kano.rc",
      sorted(hn) == sorted(["etc/audio/sof-nau8825_kano", "etc/audio/sof-nau8825_kano/audio_config.xml",
                            "etc/audio/sof-nau8825_kano/mixer_paths.xml", "etc/audio/sof-nau8825_kano/volume_curves.xml",
                            "etc/ueventd/ueventd.kano.rc"]))
check("KANO hooks: changed paths = audio_default.xml, the keyboard idc, excluded-input-devices.xml",
      sorted(hc) == sorted(["etc/data/hal_configs/audio_default.xml", "usr/idc/AT_Translated_Set_2_keyboard.idc",
                            "etc/excluded-input-devices.xml"]))
check("KANO kano-hw.rc head (wifi_rc) is the former text",
      hooks.wifi_rc(type("C", (), {"prof": kano})) ==
      "# KANO: the AX211 needs the iwlmvm op-mode (the image only loads iwlmld, for the lapis BE200/BE211).\non early-boot\n"
      "    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/modules iwlmvm\n\n")


def vendor_fw_paths(fw_entries):
    out = set()
    for fwe in fw_entries:
        parts = fwe["path"].split("/")
        out.update("firmware/" + "/".join(parts[:i]) for i in range(1, len(parts)))
        out.add("firmware/" + fwe["path"])
    return out


def esp_entry_files(prof, vcmd):
    e = devprofile.make_entries(prof, vcmd)
    fmt = lambda title, o: f"title {title}\nlinux /kernel.efi\ninitrd /initrd.img\noptions {' '.join(o)}\n"
    return {
        "loader/loader.conf": "default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n",
        "loader/entries/lapis-quiet.conf": fmt(prof["entries"]["quiet"], e["quiet"]),
        "loader/entries/lapis-debug.conf": fmt(prof["entries"]["debug"], e["debug"]),
    }


kano_v020 = copy.deepcopy(kano)
kano_v020["model"]["name"] = "Acer Chromebook Spin 714 (CP714-1WN)"
kano_v020["cmdline"]["add"] = ["loglevel=8", "ignore_loglevel", "console=ttyS0,115200n8", "console=ttyS4,115200n8",
                               "console=uart8250,mmio32,0xfe03e000,115200n8", "earlycon=uart8250,mmio32,0xfe03e000,115200n8",
                               "xe.force_probe=46a6"]
kano_v020["firmware"] = kano["firmware"][:9]
esp_old = esp_entry_files(kano_v020, REAL_VCMD)
esp_new = esp_entry_files(kano, REAL_VCMD)
check("ESP loader/ entries vs v0.2.0 differ only in lapis-quiet.conf and lapis-debug.conf options (loader.conf identical)",
      esp_old["loader/loader.conf"] == esp_new["loader/loader.conf"]
      and esp_new["loader/entries/lapis-quiet.conf"] == esp_old["loader/entries/lapis-quiet.conf"].replace("xe.force_probe=46a6", XE_FORCE_PROBE_OPT)
      and esp_old["loader/entries/lapis-debug.conf"] != esp_new["loader/entries/lapis-debug.conf"])
check("vendor tree + hooks vs v0.2.0 differ only by the two added RPL SOF firmware files",
      vendor_fw_paths(kano["firmware"]) - vendor_fw_paths(kano_v020["firmware"]) ==
      {"firmware/intel/sof/community/sof-rpl.ri", "firmware/intel/sof-tplg/sof-rpl-max98373-nau8825.tplg"}
      and not (vendor_fw_paths(kano_v020["firmware"]) - vendor_fw_paths(kano["firmware"]))
      and hooks.expected(kano) == hooks.expected(kano_v020)
      and hooks.wifi_rc(type("C", (), {"prof": kano})) == hooks.wifi_rc(type("C", (), {"prof": kano_v020}))
      and {k: kano[k] for k in ("kmods", "blacklist", "audio", "wifi", "display", "input", "camera", "hooks")} ==
          {k: kano_v020[k] for k in ("kmods", "blacklist", "audio", "wifi", "display", "input", "camera", "hooks")})

# ---- broken profiles are refused
SRC = (ROOT / "profiles/kano/profile.toml").read_text()
tmp = Path(tempfile.mkdtemp(prefix="gbos-profile-test-"))


def attempt(label, edit, expect, name="kano", extra_files=("audio-src.list", "vendor.py"), src=None, src_root=None):
    d = tmp / label.replace(" ", "_") / name
    d.mkdir(parents=True)
    origin = src_root or (ROOT / "profiles/kano")
    for f in extra_files:
        shutil.copy(origin / f, d / f)
    text = edit(SRC if src is None else src)
    (d / "profile.toml").write_text(text)
    try:
        devprofile.load(d)
        check(f"refuses {label}", False)
    except (devprofile.ProfileError, tomllib.TOMLDecodeError) as e:
        check(f"refuses {label} ({e})", expect in str(e))


def sub(old, new):
    def f(t):
        assert t.count(old) >= 1, old
        return t.replace(old, new, 1)
    return f


attempt("a missing key", sub('image_name = "googlebook-kano.img"\n', ""), "missing key image_name")
attempt("an unknown key", sub("[wifi]\n", "[wifi]\nmodprob = []\n"), "unknown key(s) modprob")
attempt("an unknown table", sub("[wifi]\n", "[wiifi]\nx = 1\n[wifi]\n"), "unknown key(s) wiifi")
attempt("a wrong type (string for a list)", sub('modprobe = ["iwlmvm"]', 'modprobe = "iwlmvm"'), "wifi.modprobe: expected a list")
attempt("a wrong type (integer in a string list)", sub('debug_drop = ["8250.nr_uarts=4"]', "debug_drop = [4]"), "expected str")
attempt("a wrong schema", sub("schema = 1", "schema = 2"), "schema 2")
attempt("model.id different from the directory", sub('id = "kano"', 'id = "other"'), "equal to the directory name")
attempt("an unknown status", sub('status = "verified"', 'status = "great"'), "model.status")
attempt("an image name with a path", sub('image_name = "googlebook-kano.img"', 'image_name = "../x.img"'), "image_name")
attempt("a required module removed from the blacklist", sub('"cros_ec_chardev",', ""), "lacks required module(s): cros_ec_chardev")
attempt("spi_nor removed from the blacklist", sub('"spi_nor", ', ""), "lacks required module(s): spi_nor")
attempt("mtd removed from the blacklist", sub('"mtd", ', ""), "lacks required module(s): mtd")
attempt("tpm_tis_i2c_cr50 removed from the blacklist", sub('"tpm_tis_i2c_cr50", ', ""), "lacks required module(s): tpm_tis_i2c_cr50")
attempt("an allowed module that is also blocked", sub('allow = ["cros_ec_proto", "cros_kbd_led_backlight"]', 'allow = ["tpm"]'),
        "blacklist.allow overlaps")
for opt in ("efi=runtime", "spi-intel.writeable=1", "module_blacklist=x", "initcall_blacklist=x", "efi_pstore.pstore_disable=0", "lockdown=none"):
    attempt(f"the profile option {opt}", sub('tail = ["xe.enable_dpt=0"', f'tail = ["{opt}", "xe.enable_dpt=0"'), "not allowed in a profile")
attempt("dropping a safety option", sub('stock_drop = ["snd_intel_sdw_acpi', 'stock_drop = ["efi=noruntime", "snd_intel_sdw_acpi'),
        "must not remove a safety option")
attempt("an option with a space", sub('debug_drop = ["8250.nr_uarts=4"]', 'debug_drop = ["a b"]'), "not one kernel command line option")
attempt("a firmware path with ..", sub('path = "i915/adlp_dmc.bin"', 'path = "../etc/passwd"'), "not a plain relative path")
attempt("a short firmware SHA-256", sub('sha256 = "3516de2e', 'sha256 = "3516'), "64 lower-case hex")
attempt("a duplicate firmware path", sub('path = "i915/adlp_guc_70.bin"', 'path = "i915/adlp_dmc.bin"'), "listed twice")
attempt("an unknown firmware source", sub('from = "sof-bin"', 'from = "random"'), "linux-firmware or sof-bin")
attempt("an empty firmware requested_by",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nrequested_by = ""\n'),
        "requested_by: expected 'runtime: <non-empty single-line reason>'")
attempt("a firmware requested_by with a wrong prefix",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nrequested_by = "static: xe"\n'),
        "requested_by: expected 'runtime: <non-empty single-line reason>'")
attempt("a firmware requested_by with an empty runtime reason",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nrequested_by = "runtime:   "\n'),
        "requested_by: expected 'runtime: <non-empty single-line reason>'")
attempt("a multiline firmware requested_by",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nrequested_by = "runtime: a\\nb"\n'),
        "requested_by: expected 'runtime: <non-empty single-line reason>'")
attempt("a firmware requested_by with a U+2028 line separator",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nrequested_by = "runtime: a\\u2028b"\n'),
        "requested_by: expected 'runtime: <non-empty single-line reason>'")
attempt("a firmware requested_by of only zero-width text",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nrequested_by = "runtime: \\u200b"\n'),
        "requested_by: expected 'runtime: <non-empty single-line reason>'")
attempt("an empty firmware loaded_by",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nloaded_by = ""\n'),
        "loaded_by: expected '<vendor|vendor_dlkm|system_dlkm|initrd>:<relative .ko path>'")
attempt("a firmware loaded_by that is only a module name",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nloaded_by = "xe"\n'),
        "loaded_by: expected '<vendor|vendor_dlkm|system_dlkm|initrd>:<relative .ko path>'")
attempt("a firmware loaded_by with ..",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nloaded_by = "vendor:lib/../x.ko"\n'),
        "loaded_by: expected '<vendor|vendor_dlkm|system_dlkm|initrd>:<relative .ko path>'")
attempt("a firmware pin with both requested_by and loaded_by",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nrequested_by = "runtime: x"\n'
            'loaded_by = "vendor_dlkm:lib/modules/xe.ko"\n'),
        "requested_by and loaded_by are mutually exclusive")
attempt("a non-string firmware requested_by",
        sub('path = "i915/adlp_dmc.bin"\n', 'path = "i915/adlp_dmc.bin"\nrequested_by = 1\n'),
        "expected str")
attempt("an adb property in build_prop_set", sub('[["vendor.hwc.drm.enable_color_pipeline", "true", "false"],',
                                                 '[["ro.adb.secure", "1", "0"],'), "not an adb/debuggable property")
attempt("a malformed pci-stub id", sub('"8086:51a8"', '"8086:51A8"'), "vvvv:dddd")
attempt("hook-driven settings without a hook", sub('vendor = "vendor.py"', 'vendor = ""'), "need a hooks.vendor file")
attempt("a hooks.vendor file that is not there", sub('vendor = "vendor.py"', 'vendor = "nothere.py"'), "is not a Python file")
attempt("an audio source list that is not there", sub('sources = "audio-src.list"', 'sources = "nothere.list"'), "is not a file")
attempt("TOML that does not parse", sub("[model]", "[model"), "")
attempt("unblock snd_hda_intel on kano",
        sub("[wifi]\n", "[modules]\nunblock = [\"snd_hda_intel\"]\nload = []\n[wifi]\n"),
        "not allowlisted for kano")
attempt("load bluetooth on kano",
        sub("[wifi]\n", "[modules]\nunblock = []\nload = [\"bluetooth\"]\n[wifi]\n"),
        "not allowlisted for kano")

# the required blacklist is the whole v0.1 hard blacklist, and holds builder/image.py's EC_NEVER
req = devprofile.load_safety()["blacklist"]["required"]
check("_safety.toml blacklist.required = the 57 modules every v0.1 entry blocked", sorted(req) == sorted(mods) and len(set(req)) == 57)
img = (ROOT / "builder/image.py").read_text()
never = ast.literal_eval(img.split("EC_NEVER = ", 1)[1].split(")", 1)[0] + ")")
check("builder/image.py EC_NEVER is part of blacklist.required (spi_nor included)", set(never) <= set(req) and "spi_nor" in never)

# the probe's built-in copy of the safety blacklist matches profiles/_safety.toml
probe = (ROOT / "install/gbos-probe.sh").read_text()
src = probe.split("SAFETY_REQUIRED = ", 1)[1].split("]", 1)[0] + "]"
check("install/gbos-probe.sh SAFETY_REQUIRED = profiles/_safety.toml blacklist.required",
      ast.literal_eval(src) == devprofile.load_safety()["blacklist"]["required"])

amd = devprofile.load(ROOT / "profiles" / "amd-hx370")
gx = devprofile.load(ROOT / "profiles" / "generic-x86")
check("amd-hx370 is experimental, native display, kmods = [] in profile.toml (safe entry stages gbos_efifb.ko via vendor.sh)",
      amd["model"]["id"] == "amd-hx370" and amd["model"]["status"] == "experimental"
      and amd["model"]["image_name"] == "googlebook-amd-hx370.img"
      and amd["display"]["stack"] == "native" and amd["display"]["hwc_backend"] == "client"
      and amd["kmods"] == [])
check("amd-hx370 gralloc.sha256 pins the minigbm4 libminigbm_gralloc.so",
      amd.get("gralloc") == {"sha256": "297457940c9090754e71d4027e157167825edab4139eb7d8828d1660b98e0d42"})
check("kano and generic-x86 have no [gralloc] table",
      "gralloc" not in kano and "gralloc" not in gx)
amods, aics, aplain = devprofile.guards(amd)
amd_extra = {"tpm_crb", "ccp", "amdtee", "spi_amd", "spi_amd_pci", "amd_sfh", "sp5100_tco"}
check("amd-hx370 blacklist is generic-x86 plus AMD fTPM, PSP and SPI modules",
      set(gx["blacklist"]["modules"]) <= set(amd["blacklist"]["modules"])
      and amd_extra <= set(amd["blacklist"]["modules"]) and amd["blacklist"]["allow"] == [])
check("amd-hx370 keeps nvme_init, mei_me_driver_init and efi=noruntime",
      aics == ["nvme_init", "mei_me_driver_init"] and aplain == ["efi=noruntime"] and set(req) <= amods)
fw = {f["path"]: f["sha256"] for f in amd["firmware"]}
amdgpu_fw = [f for f in amd["firmware"] if f["path"].startswith("amdgpu/")]
check("amd-hx370 pins 13 Strix Point blobs from linux-firmware 20260810",
      amd["firmware_sources"]["linux_firmware_tag"] == "20260810" and len(amdgpu_fw) == 13
      and all(f["from"] == "linux-firmware" and f["src"] == f["path"] for f in amdgpu_fw)
      and fw["amdgpu/gc_11_5_0_pfp.bin"] == "82ccf0265d841351183b011a79422799431f0c11f6d11165d64d7dfe404bda31"
      and fw["amdgpu/dcn_3_5_dmcub.bin"] == "d59800dbd65cb94fcfc0de0c47f463323b3abc260b3dbb7d9003b756024aaf13"
      and fw["amdgpu/psp_14_0_0_toc.bin"] == "da82e510ba90b3939656ecd2092870e5eef1c1b71ad2a4b776bc9e57bae1f05f"
      and fw["amdgpu/vpe_6_1_0.bin"] == "10a900e43e8633bc1ebe03d2d88f9b3e2f96a43c1961b190023fa1d26423a41a"
      and "amdgpu/smu_14_0_2.bin" not in fw and "amdgpu/isp_4_1_1.bin" not in fw
      and "amdgpu/umsch_mm_4_0_0.bin" not in fw)
# AX210: PCI 8086:2725 -> iwlwifi-ty-a0-gf-a0-89.ucode + .pnvm; USB 8087:0032 -> ibt-0041-0041.sfi/.ddc.
# path is what the driver requests under /vendor/firmware; src is the 20260810 WHENCE File path.
AX210_FW = {
    "iwlwifi-ty-a0-gf-a0-89.ucode": ("linux-firmware", "intel/iwlwifi/iwlwifi-ty-a0-gf-a0-89.ucode",
                                     "9c63b42020d69598af746b7e265427f459ab773f1e31969d861e3e7b5fa30de4"),
    "iwlwifi-ty-a0-gf-a0.pnvm": ("linux-firmware", "intel/iwlwifi/iwlwifi-ty-a0-gf-a0.pnvm",
                                 "1059f93a225e5c644d7c9c49302277b102f532b34672ef046c458befc1675fbb"),
    "intel/ibt-0041-0041.sfi": ("linux-firmware", "intel/ibt-0041-0041.sfi",
                                "2d891022ded1d2d8b208cc5b7632f3098c761ae099024de971e4158dd5681a9c"),
    "intel/ibt-0041-0041.ddc": ("linux-firmware", "intel/ibt-0041-0041.ddc",
                                "fe272982577efdc289cfe3e8bedafca607c0b7d6c9df1aab1880e22ef930d077"),
}


def ax210_firmware_pinned(entries):
    got = {f["path"]: (f["from"], f["src"], f["sha256"]) for f in entries if not f["path"].startswith("amdgpu/")}
    return (got == AX210_FW
            and all(re.fullmatch(r"[0-9a-f]{64}", sha) and src and path
                    for path, (_src_from, src, sha) in got.items()))


check("amd-hx370 AX210 Wi-Fi and Bluetooth firmware (path, src, 64-hex sha256)",
      ax210_firmware_pinned(amd["firmware"]) and len(amd["firmware"]) == 17)
check("kano firmware pins have requested_by only on the 6 runtime-named Bluetooth and SOF files",
      [f["path"] for f in kano["firmware"] if not f["requested_by"]] == [
          "i915/adlp_dmc.bin", "i915/adlp_guc_70.bin", "i915/tgl_huc.bin",
          "iwlwifi-so-a0-gf-a0-89.ucode", "iwlwifi-so-a0-gf-a0.pnvm",
      ] and [f["path"] for f in kano["firmware"] if f["requested_by"].startswith("runtime: ")] == [
          "intel/ibt-0040-0041.sfi", "intel/ibt-0040-0041.ddc",
          "intel/sof/community/sof-adl.ri", "intel/sof-tplg/sof-adl-max98373-nau8825.tplg",
          "intel/sof/community/sof-rpl.ri", "intel/sof-tplg/sof-rpl-max98373-nau8825.tplg",
      ])
check("amd-hx370 firmware pins: loaded_by vendor:lib/dev-modules/amdgpu.ko on the 13 conditionally loaded amdgpu blobs, "
      "requested_by only on the 2 AX210 Bluetooth files",
      [f["path"] for f in amd["firmware"] if not f["requested_by"] and not f["loaded_by"]] == [
          "iwlwifi-ty-a0-gf-a0-89.ucode", "iwlwifi-ty-a0-gf-a0.pnvm",
      ] and [f["path"] for f in amd["firmware"] if f["requested_by"].startswith("runtime: ")] == [
          "intel/ibt-0041-0041.sfi", "intel/ibt-0041-0041.ddc",
      ] and [f["path"] for f in amd["firmware"] if f["loaded_by"] == "vendor:lib/dev-modules/amdgpu.ko"] == [
          f["path"] for f in amd["firmware"] if f["path"].startswith("amdgpu/")
      ] and len([f for f in amd["firmware"] if f["loaded_by"]]) == 13)
check("kano firmware pins have no loaded_by", not any(f["loaded_by"] for f in kano["firmware"]))
for drop_path in AX210_FW:
    dropped = [f for f in amd["firmware"] if f["path"] != drop_path]
    check(f"dropping {drop_path} fails the AX210 firmware pin",
          len(dropped) == 16 and not ax210_firmware_pinned(dropped))
sha_mut = copy.deepcopy(amd["firmware"])
next(f for f in sha_mut if f["path"] == "iwlwifi-ty-a0-gf-a0-89.ucode")["sha256"] = "0" * 64
check("changing the AX210 ucode sha256 fails the pin", not ax210_firmware_pinned(sha_mut))
ent = devprofile.make_entries(amd, VCMD)
bl_debug = next(o for o in ent["debug"] if o.startswith("module_blacklist=")).split("=", 1)[1].split(",")
bl_quiet = next(o for o in ent["quiet"] if o.startswith("module_blacklist=")).split("=", 1)[1].split(",")
check("amd-hx370 safe default entry (quiet) blacklists amdgpu and drops amdgpu.* options",
      "amdgpu" in bl_quiet and not any(o.startswith("amdgpu.") for o in ent["quiet"])
      and "xe.enable_psr=1" not in ent["quiet"] and "snd_sof_pci.ipc_type=1" not in ent["quiet"]
      and "initcall_blacklist=nvme_init,mei_me_driver_init" in ent["quiet"]
      and "efi=noruntime" in ent["quiet"]
      and "erst_disable" in ent["quiet"]
      and "tpm_crb" in bl_quiet and "ccp" in bl_quiet
      and "sdhci_pci" in bl_quiet and "sdhci_acpi" in bl_quiet)
check("amd-hx370 experimental native entry (debug) allows amdgpu and keeps amdgpu.* options",
      "amdgpu" not in bl_debug
      and "amdgpu.modeset=1" in ent["debug"] and "amdgpu.dc=1" in ent["debug"]
      and "amdgpu.dcdebugmask=0x10" in ent["debug"]
      and "initcall_blacklist=nvme_init,mei_me_driver_init" in ent["debug"]
      and "efi=noruntime" in ent["debug"]
      and "erst_disable" in ent["debug"]
      and "tpm_crb" in bl_debug and "ccp" in bl_debug
      and "sdhci_pci" in bl_debug and "sdhci_acpi" in bl_debug)
check("amd-hx370 menu titles distinguish safe default from experimental native amdgpu",
      (amd["entries"]["quiet"], amd["entries"]["debug"]) ==
      ("Googlebook AMD HX370 safe", "Googlebook AMD HX370 native amdgpu (experimental)"))
check("amd-hx370 modules unblock snd_hda_intel and load bluetooth, btusb, codec, iwlmvm",
      amd["modules"] == {"unblock": ["snd_hda_intel"],
                         "load": ["bluetooth", "btusb", "snd_hda_codec_alc269", "iwlmvm"]}
      and amd["_module_dirs"] == {"snd_hda_intel": "vendor", "bluetooth": "dev", "btusb": "vendor",
                                  "snd_hda_codec_alc269": "vendor", "iwlmvm": "vendor"})
AMD_BOOT = (
    "on early-boot\n"
    "    exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/dev-modules bluetooth\n"
    "    exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/modules btusb\n"
    "    exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/modules snd_hda_codec_alc269\n"
    "    exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/modules iwlmvm\n"
    "    exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/modules snd_hda_intel\n"
    "\n")
check("amd-hx370 early-boot modprobe is bluetooth, btusb, alc269, iwlmvm, then snd_hda_intel, without -b",
      devprofile.append_module_boot("", amd) == AMD_BOOT
      and " -b" not in AMD_BOOT and "--use-blocklist" not in AMD_BOOT
      and "exec_background" not in AMD_BOOT
      and AMD_BOOT.index("snd_hda_codec_alc269") < AMD_BOOT.index("snd_hda_intel"))
lapis_tsv = (ROOT / "install/probe/lapis-modules.tsv").read_text()
check("shipped lapis set includes snd_hda_codec_alc269 and iwlmvm, and has no snd_hda_codec_alc245",
      "\nsnd_hda_codec_alc269\t" in lapis_tsv and "\niwlmvm\t" in lapis_tsv
      and "\nsnd_hda_codec_alc245\t" not in lapis_tsv)
storage_names = devprofile._storage_firmware_module_names()
storage_expected = frozenset({
    "ahci", "ahci_platform", "ccp", "ccp_crypto", "libahci", "libata", "mei_me", "mtd", "nvme",
    "sdhci_acpi", "sdhci_pci", "spi_amd", "spi_amd_pci", "spi_intel", "spi_intel_pci",
    "spi_intel_platform", "spi_nor", "spidev", "thunderbolt", "tpm", "tpm_crb", "tpm_tis",
    "tpm_tis_core", "tpm_tis_i2c_cr50", "tpm_tis_spi",
})
check("storage/firmware module names equal the kconfig gate list",
      storage_names == storage_expected
      and {"usb_storage", "uas", "mmc_block"}.isdisjoint(storage_names | devprofile._NEVER_SHIP_KO))
_mod_pids = sorted({pid for rows in devprofile.load_safety()["_modules"].values() for pid in rows})
check("every _safety modules profile id has profiles/<id>/profile.toml",
      bool(_mod_pids) and all((ROOT / "profiles" / pid / "profile.toml").is_file() for pid in _mod_pids))
check("stock modules.blocklist is snd_hda_intel in one place",
      devprofile.STOCK_MODULES_BLOCKLIST == frozenset({"snd_hda_intel"}))
kano_head = hooks.wifi_rc(type("C", (), {"prof": kano}))
generic_head = ""
check("kano and generic-x86 add no module boot lines, so the early-boot text stays byte-identical",
      devprofile.module_boot_lines(kano) == "" and devprofile.append_module_boot(kano_head, kano) is kano_head
      and devprofile.module_boot_lines(gx) == "" and devprofile.append_module_boot(generic_head, gx) is generic_head
      and devprofile.missing_dev_modules(kano, "") == [] and devprofile.missing_dev_modules(gx, "") == [])
ko_root = tmp / "devko"
(ko_root / "modules").mkdir(parents=True)
check("amd-hx370 dev-modules check wants bluetooth.ko",
      devprofile.missing_dev_modules(amd, str(ko_root)) == ["bluetooth"])
(ko_root / "modules" / "bluetooth.ko").write_bytes(b"ko")
check("amd-hx370 dev-modules check accepts a top-level bluetooth.ko",
      devprofile.missing_dev_modules(amd, str(ko_root)) == [])
def vendor_module_boot_source(text):
    """Live vendor.sh statements from the missing-ko check through the kano-hw.rc write.

    Full-line comments are not a start marker. The returned text is exec'd; a commented-out
    call or a disabled abort stays in the slice and changes the result.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("missing_ko = devprofile.missing_dev_modules("):
            start = i
            break
    if start is None:
        return None
    end = None
    for j in range(start, len(lines)):
        if lines[j].startswith('(tree / "etc/init/kano-hw.rc").write_text(hw_head +'):
            for k in range(j, min(j + 40, len(lines))):
                if lines[k].startswith('""" + dev_kmod_boot)'):
                    end = k
                    break
            break
    if end is None:
        return None
    return "".join(lines[start:end + 1])


def exec_vendor_module_boot(src, prof, hw_head, tree, kdir):
    ns = {"devprofile": devprofile, "os": os, "Path": Path, "prof": prof, "hw_head": hw_head, "tree": tree}
    old = os.environ.get("GBOS_DEV_KERNEL_DIR")
    os.environ["GBOS_DEV_KERNEL_DIR"] = kdir
    try:
        exec(src, ns)
    finally:
        if old is None:
            os.environ.pop("GBOS_DEV_KERNEL_DIR", None)
        else:
            os.environ["GBOS_DEV_KERNEL_DIR"] = old
    return ns["hw_head"]


vsh = (ROOT / "builder/vendor.sh").read_text()
boot_src = vendor_module_boot_source(vsh)
boot_tree = tmp / "module-boot-tree"
(boot_tree / "etc/init").mkdir(parents=True, exist_ok=True)
abort_msg = ""
aborted = False
if boot_src is not None:
    try:
        exec_vendor_module_boot(boot_src, amd, "", boot_tree, str(tmp / "no-bluetooth-ko"))
    except SystemExit as e:
        aborted = True
        abort_msg = str(e)
check("vendor.sh module-boot aborts when the dev-modules bluetooth.ko is missing",
      boot_src is not None and aborted and "bluetooth" in abort_msg
      and not (boot_tree / "etc/init/kano-hw.rc").exists())
ko_dir = tmp / "module-boot-ko"
(ko_dir / "modules").mkdir(parents=True)
(ko_dir / "modules" / "bluetooth.ko").write_bytes(b"ko")
amd_rc = boot_tree / "etc/init/kano-hw.rc"
amd_head = ""
if boot_src is not None and aborted:
    amd_head = exec_vendor_module_boot(boot_src, amd, "", boot_tree, str(ko_dir))
amd_rc_text = amd_rc.read_text() if amd_rc.is_file() else ""
check("vendor.sh module-boot writes the amd-hx370 rc lines in order",
      amd_head == AMD_BOOT and amd_rc_text.startswith(AMD_BOOT)
      and " -b " not in amd_rc_text.split("# KANO:", 1)[0])
kano_rc_tree = tmp / "module-boot-kano"
(kano_rc_tree / "etc/init").mkdir(parents=True)
kano_after = exec_vendor_module_boot(boot_src, kano, kano_head, kano_rc_tree, "") if boot_src else None
kano_rc_text = (kano_rc_tree / "etc/init/kano-hw.rc").read_text() if boot_src else ""
check("vendor.sh module-boot leaves the kano early-boot head byte-identical",
      boot_src is not None and kano_after is kano_head and kano_rc_text.startswith(kano_head)
      and "snd_hda_codec_alc269" not in kano_rc_text and "bluetooth" not in kano_rc_text
      and "btusb" not in kano_rc_text and "snd_hda_intel" not in kano_rc_text)
gx_rc_tree = tmp / "module-boot-gx"
(gx_rc_tree / "etc/init").mkdir(parents=True)
gx_before = ""
gx_after = exec_vendor_module_boot(boot_src, gx, gx_before, gx_rc_tree, "") if boot_src else None
gx_rc_text = (gx_rc_tree / "etc/init/kano-hw.rc").read_text() if boot_src else ""
check("vendor.sh module-boot leaves the generic-x86 early-boot head byte-identical",
      boot_src is not None and gx_after is gx_before and gx_rc_text.startswith("# KANO: join")
      and "bluetooth" not in gx_rc_text and "snd_hda_intel" not in gx_rc_text
      and "snd_hda_codec_alc269" not in gx_rc_text and "iwlmvm" not in gx_rc_text)
AMD_SRC = (ROOT / "profiles/amd-hx370/profile.toml").read_text()
AMD_ROOT = ROOT / "profiles/amd-hx370"
attempt("btusb before bluetooth on amd-hx370",
        sub('load = ["bluetooth", "btusb", "snd_hda_codec_alc269", "iwlmvm"]',
            'load = ["btusb", "bluetooth", "snd_hda_codec_alc269", "iwlmvm"]'),
        "requires bluetooth", name="amd-hx370", extra_files=("vendor.py",), src=AMD_SRC, src_root=AMD_ROOT)
attempt("btusb without bluetooth on amd-hx370",
        sub('load = ["bluetooth", "btusb", "snd_hda_codec_alc269", "iwlmvm"]',
            'load = ["btusb", "snd_hda_codec_alc269", "iwlmvm"]'),
        "requires bluetooth", name="amd-hx370", extra_files=("vendor.py",), src=AMD_SRC, src_root=AMD_ROOT)
attempt("unblock xe on amd-hx370",
        sub('unblock = ["snd_hda_intel"]', 'unblock = ["xe"]'),
        "not allowlisted for amd-hx370", name="amd-hx370", extra_files=("vendor.py",), src=AMD_SRC, src_root=AMD_ROOT)
attempt("duplicate snd_hda_intel unblock",
        sub('unblock = ["snd_hda_intel"]', 'unblock = ["snd_hda_intel", "snd_hda_intel"]'),
        "duplicate items", name="amd-hx370", extra_files=("vendor.py",), src=AMD_SRC, src_root=AMD_ROOT)
GRALLOC_SHA = "297457940c9090754e71d4027e157167825edab4139eb7d8828d1660b98e0d42"
attempt("a short gralloc sha256",
        sub(f'sha256 = "{GRALLOC_SHA}"', 'sha256 = "17a08daa"'),
        "64 lower-case hex", name="amd-hx370", extra_files=("vendor.py",), src=AMD_SRC, src_root=AMD_ROOT)
attempt("an uppercase gralloc sha256",
        sub(f'sha256 = "{GRALLOC_SHA}"', f'sha256 = "{GRALLOC_SHA.upper()}"'),
        "64 lower-case hex", name="amd-hx370", extra_files=("vendor.py",), src=AMD_SRC, src_root=AMD_ROOT)
attempt("an unknown gralloc key",
        sub("[gralloc]\n", "[gralloc]\nextra = \"nope\"\n"),
        "unknown key(s) extra", name="amd-hx370", extra_files=("vendor.py",), src=AMD_SRC, src_root=AMD_ROOT)
attempt("gralloc sha256 missing",
        sub(f'[gralloc]\nsha256 = "{GRALLOC_SHA}"\n', "[gralloc]\n"),
        "missing key sha256", name="amd-hx370", extra_files=("vendor.py",), src=AMD_SRC, src_root=AMD_ROOT)
def _gralloc_string(t):
    # A bare key after [hooks] would land in that table. Put the string at the top level.
    needle = f'[gralloc]\nsha256 = "{GRALLOC_SHA}"\n'
    assert t.count(needle) == 1 and t.count("schema = 1\n") >= 1
    return t.replace(needle, "").replace("schema = 1\n", 'schema = 1\ngralloc = "nope"\n', 1)


attempt("gralloc as a string", _gralloc_string,
        "expected a table", name="amd-hx370", extra_files=("vendor.py",), src=AMD_SRC, src_root=AMD_ROOT)
attempt("an integer gralloc sha256",
        sub(f'sha256 = "{GRALLOC_SHA}"', "sha256 = 1"),
        "expected str", name="amd-hx370", extra_files=("vendor.py",), src=AMD_SRC, src_root=AMD_ROOT)
safety_txt = (ROOT / "profiles/_safety.toml").read_text()
SAFETY_LOAD = ('load = ["amd-hx370 bluetooth dev", "amd-hx370 btusb vendor", '
               '"amd-hx370 snd_hda_codec_alc269 vendor", "amd-hx370 iwlmvm vendor"]')
AMD_LOAD = 'load = ["bluetooth", "btusb", "snd_hda_codec_alc269", "iwlmvm"]'


def refuse_safety(label, old, new, expect):
    assert safety_txt.count(old) == 1, old
    path = tmp / (label.replace(" ", "_") + ".toml")
    path.write_text(safety_txt.replace(old, new, 1))
    try:
        devprofile.load_safety(path)
        check(label, False)
    except devprofile.ProfileError as e:
        check(f"{label} ({e})", expect in str(e))


def load_amd(label, profile_text, safety_text):
    d = tmp / label.replace(" ", "_") / "amd-hx370"
    d.mkdir(parents=True)
    shutil.copy(AMD_ROOT / "vendor.py", d / "vendor.py")
    (d / "profile.toml").write_text(profile_text)
    sp = d.parent / "_safety.toml"
    sp.write_text(safety_text)
    return devprofile.load(d, sp)


never_ship_expected = frozenset({
    "acpi_configfs", "acpi_dbg", "capsule_loader", "ec_sys", "efivarfs", "msr",
})
check("never-ship set is acpi_configfs, acpi_dbg, capsule_loader, ec_sys, efivarfs, msr",
      devprofile._NEVER_SHIP_KO == never_ship_expected)
for _never in sorted(never_ship_expected):
    refuse_safety(f"allowlist refuses {_never} from the never-ship set",
                  SAFETY_LOAD, SAFETY_LOAD[:-1] + f', "amd-hx370 {_never} vendor"]',
                  f"{_never} is in the never-ship set")
refuse_safety("unblock must use the vendor dir",
              'unblock = ["amd-hx370 snd_hda_intel vendor"]',
              'unblock = ["amd-hx370 snd_hda_intel dev"]',
              "must use the vendor dir")
refuse_safety("safety allowlist refuses a repeated row",
              'unblock = ["amd-hx370 snd_hda_intel vendor"]',
              'unblock = ["amd-hx370 snd_hda_intel vendor", "amd-hx370 snd_hda_intel vendor"]',
              "listed twice")
refuse_safety("allowlist profile id must name an existing profile",
              SAFETY_LOAD, SAFETY_LOAD[:-1] + ', "amd-hx37o k10temp dev"]',
              "amd-hx37o has no profiles/amd-hx37o/profile.toml")
refuse_safety("allowlist refuses nvme as a storage or firmware-update module",
              SAFETY_LOAD, SAFETY_LOAD[:-1] + ', "amd-hx370 nvme dev"]',
              "nvme is a storage or firmware-update module")
refuse_safety("allowlist refuses sdhci_pci as a storage or firmware-update module",
              SAFETY_LOAD, SAFETY_LOAD[:-1] + ', "amd-hx370 sdhci_pci vendor"]',
              "sdhci_pci is a storage or firmware-update module")
refuse_safety("allowlist refuses ccp as a storage or firmware-update module",
              SAFETY_LOAD, SAFETY_LOAD[:-1] + ', "amd-hx370 ccp vendor"]',
              "ccp is a storage or firmware-update module")
refuse_safety("allowlist refuses ahci as a storage or firmware-update module",
              SAFETY_LOAD, SAFETY_LOAD[:-1] + ', "amd-hx370 ahci vendor"]',
              "ahci is a storage or firmware-update module")
moved_hda = safety_txt.replace(
    'unblock = ["amd-hx370 snd_hda_intel vendor"]', 'unblock = []', 1).replace(
    SAFETY_LOAD, SAFETY_LOAD[:-1] + ', "amd-hx370 snd_hda_intel vendor"]', 1)
hda_path = tmp / "snd_hda_intel_on_load.toml"
hda_path.write_text(moved_hda)
try:
    devprofile.load_safety(hda_path)
    check("load must not name a stock modules.blocklist module", False)
except devprofile.ProfileError as e:
    check(f"load must not name a stock modules.blocklist module ({e})",
          "snd_hda_intel is on the stock modules.blocklist; use unblock" in str(e))
refuse_safety("unblock must only name a stock modules.blocklist module",
              'unblock = ["amd-hx370 snd_hda_intel vendor"]',
              'unblock = ["amd-hx370 snd_hda_intel vendor", "amd-hx370 k10temp vendor"]',
              "k10temp is not on the stock modules.blocklist")
tpm_safety = safety_txt.replace(
    'unblock = ["amd-hx370 snd_hda_intel vendor"]',
    'unblock = ["amd-hx370 snd_hda_intel vendor", "amd-hx370 tpm vendor"]')
(tmp / "tpm_allow.toml").write_text(tpm_safety)
try:
    devprofile.load_safety(tmp / "tpm_allow.toml")
    check("safety allowlist refuses a required blacklist module", False)
except devprofile.ProfileError as e:
    check(f"safety allowlist refuses a required blacklist module ({e})",
          "tpm is a required blacklist module" in str(e))
try:
    ag = load_amd(
        "allowlisted amdgpu",
        AMD_SRC.replace(AMD_LOAD, AMD_LOAD[:-1] + ', "amdgpu"]'),
        safety_txt.replace(SAFETY_LOAD, SAFETY_LOAD[:-1] + ', "amd-hx370 amdgpu dev"]', 1))
    check("allowlisted amdgpu is still refused because it is on the module blacklist", ag is None)
except devprofile.ProfileError as e:
    check(f"allowlisted amdgpu is still refused because it is on the module blacklist ({e})",
          "amdgpu is on the module blacklist" in str(e))
try:
    tco = load_amd(
        "allowlisted sp5100_tco",
        AMD_SRC.replace(AMD_LOAD, AMD_LOAD[:-1] + ', "sp5100_tco"]'),
        safety_txt.replace(SAFETY_LOAD, SAFETY_LOAD[:-1] + ', "amd-hx370 sp5100_tco vendor"]', 1))
    check("allowlisted sp5100_tco is still refused because it is on the module blacklist", tco is None)
except devprofile.ProfileError as e:
    check(f"allowlisted sp5100_tco is still refused because it is on the module blacklist ({e})",
          "sp5100_tco is on the module blacklist" in str(e))
try:
    dbg = load_amd(
        "allowlisted debug_add name",
        AMD_SRC.replace('debug_add = []', 'debug_add = ["k10temp"]', 1).replace(
            AMD_LOAD, AMD_LOAD[:-1] + ', "k10temp"]'),
        safety_txt.replace(SAFETY_LOAD, SAFETY_LOAD[:-1] + ', "amd-hx370 k10temp dev"]', 1))
    check("a profile debug_add name is refused", dbg is None)
except devprofile.ProfileError as e:
    check(f"a profile debug_add name is refused ({e})",
          "k10temp is on the module blacklist" in str(e))
vendor_bt = safety_txt.replace(
    '"amd-hx370 bluetooth dev"', '"amd-hx370 bluetooth vendor"', 1)
try:
    ordered = load_amd("bluetooth vendor row in order", AMD_SRC, vendor_bt)
    check("a vendor bluetooth row still accepts btusb after it",
          ordered["_module_dirs"]["bluetooth"] == "vendor"
          and ordered["modules"]["load"][:2] == ["bluetooth", "btusb"])
except devprofile.ProfileError as e:
    check(f"a vendor bluetooth row still accepts btusb after it ({e})", False)
try:
    load_amd("btusb without vendor bluetooth",
             AMD_SRC.replace(AMD_LOAD, 'load = ["btusb"]'),
             vendor_bt)
    check("btusb without bluetooth is refused when the bluetooth row is vendor", False)
except devprofile.ProfileError as e:
    check(f"btusb without bluetooth is refused when the bluetooth row is vendor ({e})",
          "requires bluetooth" in str(e))

# ---- own-kernel .config / System.map / IKCFG safety gate (devprofile.check_kconfig)
import bz2, copy, gzip, hashlib, lzma, os, struct
check("KCONFIG_BUILTIN_MODULE covers every symbol in KCONFIG_NEVER_BUILTIN",
      set(devprofile.KCONFIG_BUILTIN_MODULE) == {k for k, _ in devprofile.KCONFIG_NEVER_BUILTIN})
# 55 in-tree names from profiles/_safety.toml blacklist.required are in KCONFIG_BUILTIN_MODULE or the SATA
# names. cros_ec_usb and cros_ec_pwrmon are out-of-tree lapis modules. intel_thc_hid is not in _safety.toml.
# libahci is co-built with ahci; existing profiles refuse CONFIG_SATA_AHCI=m because ahci is not listed.
covered_mods = set(devprofile.KCONFIG_BUILTIN_MODULE.values()) | {"spi_amd", "spi_amd_pci", "libata", "ahci", "ahci_platform", "libahci", "cros_ec_usb", "cros_ec_pwrmon"}
check("every profiles/_safety.toml blacklist.required module is accounted for in check_kconfig",
      set(req) <= covered_mods)

GOOD_KCFG = "\n".join([
    "CONFIG_KALLSYMS=y",
    "CONFIG_MODULES=y",
    "CONFIG_MODULE_UNLOAD=y",
    "CONFIG_MODVERSIONS=y",
    "CONFIG_IKCONFIG=y",
    "CONFIG_IKCONFIG_PROC=y",
    "CONFIG_CMDLINE_BOOL=y",
    'CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
    "# CONFIG_CMDLINE_OVERRIDE is not set",
    "# CONFIG_BOOT_CONFIG_EMBED is not set",
    "# CONFIG_BOOT_CONFIG_FORCE is not set",
    "CONFIG_BLK_DEV_NVME=y",
    "CONFIG_INTEL_MEI_ME=y",
    "# CONFIG_ATA is not set",
    "# CONFIG_EFIVAR_FS is not set",
    "# CONFIG_EFI_CAPSULE_LOADER is not set",
    "# CONFIG_DEVMEM is not set",
    "# CONFIG_DEVPORT is not set",
    "# CONFIG_PROC_KCORE is not set",
    "# CONFIG_X86_MSR is not set",
    "# CONFIG_ACPI_CUSTOM_METHOD is not set",
    "# CONFIG_STRICT_DEVMEM is not set",
    "CONFIG_HAS_IOPORT=y",
    "CONFIG_X86_IOPL_IOPERM=y",
    "# CONFIG_TCG_TPM is not set",
    "# CONFIG_SPI_AMD is not set",
    "# CONFIG_MTD is not set",
    "# CONFIG_CROS_EC is not set",
    "# CONFIG_GOOGLE_SMI is not set",
    "CONFIG_USB4=m",
    "# CONFIG_CRYPTO_DEV_CCP is not set",
    "# CONFIG_AMDTEE is not set",
    "# CONFIG_TEE is not set",
    "# CONFIG_AMD_SFH_HID is not set",
    "# CONFIG_SP5100_TCO is not set",
    # step-1 kernel: CONFIG_AMD_PMC=m (s2idle). Not a blacklist target; the gate must accept it.
    "CONFIG_AMD_PMC=m",
    "CONFIG_DRM_AMDGPU=m",
    "# CONFIG_DRM_AMD_ISP is not set",
]) + "\n"
GOOD_SYSMAP = "ffffffff81000000 t nvme_init\nffffffff81000100 t mei_me_driver_init\n"


def _lz4_literal_block(raw_bytes: bytes) -> bytes:
    """Encode raw_bytes as a valid uncompressed-literals LZ4 block for deterministic pure-Python LZ4 tests."""
    n = len(raw_bytes)
    if n < 15:
        hdr = bytes([n << 4])
    else:
        rem = n - 15
        hdr = bytearray([0xF0])
        while rem >= 255:
            hdr.append(255)
            rem -= 255
        hdr.append(rem)
        hdr = bytes(hdr)
    return struct.pack("<I", len(hdr) + n) + hdr + raw_bytes


def _compress_payload(vmlinux: bytes, fmt: str) -> bytes:
    if fmt == "uncompressed":
        return vmlinux
    if fmt == "gzip":
        return gzip.compress(vmlinux)
    if fmt == "lz4":
        mid = len(vmlinux) // 2
        return (
            b"\x02!L\x18"
            + _lz4_literal_block(vmlinux[:mid])
            + _lz4_literal_block(vmlinux[mid:])
            + struct.pack("<I", len(vmlinux))
        )
    if fmt == "xz":
        return lzma.compress(vmlinux, format=lzma.FORMAT_XZ)
    if fmt == "lzma":
        # scripts/extract-ikconfig looks for \x5d\x00\x00\x00. Python's default FORMAT_ALONE
        # dictionary does not start that way, so compress with dict_size 16MiB (bytes 00 00 00 01).
        filters = [{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 24, "lc": 3, "lp": 0, "pb": 2}]
        comp = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=filters)
        body = comp.compress(vmlinux) + comp.flush()
        return bytes([0x5D, 0x00, 0x00, 0x00, 0x01]) + struct.pack("<Q", len(vmlinux)) + body
    if fmt == "bzip2":
        return bz2.compress(vmlinux)
    if fmt == "zstd":
        r = subprocess.run(["zstd", "-q", "-c"], input=vmlinux, capture_output=True, check=True)
        return r.stdout
    raise ValueError(f"unknown format {fmt}")


def make_kdir(name, cfg_text, sysmap_text=GOOD_SYSMAP, ikcfg_text=None, fmt="gzip", elf_symbols=None, setup_extra=b""):
    kd = tmp / f"kdir-{name}"
    kd.mkdir(parents=True, exist_ok=True)
    if cfg_text is not None:
        (kd / ".config").write_text(cfg_text)
    if sysmap_text is not None:
        (kd / "System.map").write_text(sysmap_text)
    ik = cfg_text if ikcfg_text is None else ikcfg_text
    if ik is not None:
        vmlinux = devprofile.build_test_vmlinux_elf(ik, symbols=elf_symbols)
        comp = _compress_payload(vmlinux, fmt)
        (kd / "bzImage").write_bytes(devprofile.wrap_hdrs_bzimage(comp, setup_extra=setup_extra))
    return kd


kd_ok = make_kdir("ok", GOOD_KCFG)
devprofile.check_kconfig(amd, kd_ok)
r = subprocess.run([sys.executable, ROOT / "builder/lib/devprofile.py", "check-kconfig", ROOT / "profiles/amd-hx370", kd_ok],
                   capture_output=True, text=True)
check("check-kconfig accepts a compliant amd-hx370 kernel dir", r.returncode == 0 and "ok for amd-hx370" in r.stdout)

# Supported HdrS payload formats are decoded from raw[start:start+plen]. LZO is not one of them.
# A second IKCFG outside that slice is a candidate and is refused (it is not ignored).
DECOY_SETUP = b"IKCFG_ST" + gzip.compress(GOOD_KCFG.encode()) + b"IKCFG_ED"
for fmt in ("gzip", "lz4", "xz", "lzma", "zstd", "bzip2", "uncompressed"):
    kd_fmt_ok = make_kdir(f"fmt-ok-{fmt}", GOOD_KCFG, fmt=fmt)
    check(f"check-kconfig accepts HdrS bzImage with {fmt} payload",
          devprofile.check_kconfig(amd, kd_fmt_ok)["CONFIG_DRM_AMDGPU"] == "m")
    kd_fmt_bad = make_kdir(
        f"fmt-bad-{fmt}",
        GOOD_KCFG,
        ikcfg_text=GOOD_KCFG + "CONFIG_TCG_TPM=y\n",
        fmt=fmt,
    )
    try:
        devprofile.check_kconfig(amd, kd_fmt_bad)
        check(f"check-kconfig refuses {fmt} payload IKCFG mismatch", False)
    except devprofile.ProfileError as e:
        check(f"check-kconfig refuses {fmt} payload IKCFG mismatch ({e})",
              "embedded IKCFG does not match" in str(e))

kd_setup_decoy = make_kdir("setup-decoy", GOOD_KCFG, fmt="gzip", setup_extra=DECOY_SETUP)
try:
    devprofile.check_kconfig(amd, kd_setup_decoy)
    check("check-kconfig refuses a setup-area IKCFG candidate outside the HdrS slice", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses a setup-area IKCFG candidate outside the HdrS slice ({e})",
          "more than one plausible payload or IKCFG candidate" in str(e))

# A trailing IKCFG outside the HdrS slice is a second candidate and is refused.
kd_trailer_decoy = tmp / "kdir-trailer-decoy"
kd_trailer_decoy.mkdir(parents=True, exist_ok=True)
(kd_trailer_decoy / ".config").write_text(GOOD_KCFG)
(kd_trailer_decoy / "System.map").write_text(GOOD_SYSMAP)
bad_vmlinux = devprofile.build_test_vmlinux_elf(GOOD_KCFG + "CONFIG_ATA=y\n")
(kd_trailer_decoy / "bzImage").write_bytes(
    devprofile.wrap_hdrs_bzimage(gzip.compress(bad_vmlinux)) + DECOY_SETUP
)
try:
    devprofile.check_kconfig(amd, kd_trailer_decoy)
    check("check-kconfig refuses trailing IKCFG outside the HdrS slice", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses trailing IKCFG outside the HdrS slice ({e})",
          "more than one plausible payload or IKCFG candidate" in str(e)
          and "scripts/extract-ikconfig" in str(e))

# Undecodable / unknown payload format inside HdrS must be refused even if setup or payload has a plaintext IKCFG decoy
kd_undecodable = tmp / "kdir-undecodable"
kd_undecodable.mkdir(parents=True, exist_ok=True)
(kd_undecodable / ".config").write_text(GOOD_KCFG)
(kd_undecodable / "System.map").write_text(GOOD_SYSMAP)
(kd_undecodable / "bzImage").write_bytes(
    devprofile.wrap_hdrs_bzimage(b"\xde\xad\xbe\xef" + DECOY_SETUP, setup_extra=DECOY_SETUP)
)
try:
    devprofile.check_kconfig(amd, kd_undecodable)
    check("check-kconfig refuses undecodable HdrS payload format even when plaintext IKCFG decoy is present", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses undecodable HdrS payload format ({e})",
          "unsupported or undecodable bzImage payload format" in str(e))

# Missing HdrS header with raw plaintext IKCFG_ST in an arbitrary blob (the old r6 fixture) must be refused
kd_r6_fixture = tmp / "kdir-r6-fixture"
kd_r6_fixture.mkdir(parents=True, exist_ok=True)
(kd_r6_fixture / ".config").write_text(GOOD_KCFG)
(kd_r6_fixture / "System.map").write_text(GOOD_SYSMAP)
(kd_r6_fixture / "bzImage").write_bytes(b"MZ" + b"\0" * 64 + DECOY_SETUP)
try:
    devprofile.check_kconfig(amd, kd_r6_fixture)
    check("check-kconfig refuses r6 non-HdrS non-ELF bzImage fixture", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses r6 non-HdrS non-ELF bzImage fixture ({e})",
          "unsupported or undecodable bzImage payload format" in str(e))

# When neither CONFIG_BLK_DEV_NVME=y nor CONFIG_INTEL_MEI_ME=y is built-in, System.map is not required
kd_no_builtin_ic = make_kdir(
    "no_builtin_ic",
    GOOD_KCFG.replace("CONFIG_BLK_DEV_NVME=y", "# CONFIG_BLK_DEV_NVME is not set")
             .replace("CONFIG_INTEL_MEI_ME=y", "# CONFIG_INTEL_MEI_ME is not set"),
    sysmap_text=None,
)
check("check-kconfig accepts missing System.map when neither NVMe nor MEI is built-in",
      devprofile.check_kconfig(amd, kd_no_builtin_ic)["CONFIG_DRM_AMDGPU"] == "m")


def check_kcfg_refuses(label, cfg_mod, expect, sysmap_text=GOOD_SYSMAP, ikcfg_text=None, prof=amd, elf_symbols=None):
    kd = make_kdir(
        label.replace(" ", "_").replace("/", "_").replace("=", "_").replace("'", ""),
        cfg_mod,
        sysmap_text=sysmap_text,
        ikcfg_text=ikcfg_text,
        elf_symbols=elf_symbols,
    )
    try:
        devprofile.check_kconfig(prof, kd)
        check(f"check-kconfig refuses {label}", False)
    except devprofile.ProfileError as e:
        check(f"check-kconfig refuses {label} ({e})", expect in str(e))


check_kcfg_refuses("missing .config", None, "missing .config")
# Grok M2: tiny .config (<5 keys) and tiny bzImage (<4096 B) must not bypass validation
check_kcfg_refuses("1-line .config (Grok M2)", "CONFIG_FOO=y\n", "CONFIG_KALLSYMS=y is required")
kd_tiny_bz = make_kdir("tiny_bzimage", GOOD_KCFG, ikcfg_text=None)
(kd_tiny_bz / "bzImage").write_bytes(b"MZ" + b"\0" * 100)
try:
    devprofile.check_kconfig(amd, kd_tiny_bz)
    check("check-kconfig refuses <4KB bzImage missing HdrS/IKCFG (Grok M2)", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses <4KB bzImage missing HdrS/IKCFG ({e})",
          "unsupported or undecodable bzImage payload format" in str(e))

check_kcfg_refuses("CONFIG_KALLSYMS disabled", GOOD_KCFG.replace("CONFIG_KALLSYMS=y", "# CONFIG_KALLSYMS is not set"), "CONFIG_KALLSYMS=y is required")
check_kcfg_refuses("CONFIG_MODULES disabled", GOOD_KCFG.replace("CONFIG_MODULES=y", "# CONFIG_MODULES is not set"), "CONFIG_MODULES=y is required")
check_kcfg_refuses("CONFIG_IKCONFIG disabled", GOOD_KCFG.replace("CONFIG_IKCONFIG=y", "# CONFIG_IKCONFIG is not set"), "CONFIG_IKCONFIG=y is required")
check_kcfg_refuses("CONFIG_STRICT_DEVMEM=n", GOOD_KCFG + "CONFIG_STRICT_DEVMEM=n\n", "CONFIG_STRICT_DEVMEM=n is forbidden")
check_kcfg_refuses("CONFIG_IO_STRICT_DEVMEM=n", GOOD_KCFG + "CONFIG_IO_STRICT_DEVMEM=n\n", "CONFIG_IO_STRICT_DEVMEM=n is forbidden")
check_kcfg_refuses("CONFIG_CMDLINE_OVERRIDE=y", GOOD_KCFG + "CONFIG_CMDLINE_OVERRIDE=y\n", "CONFIG_CMDLINE_OVERRIDE=y is forbidden")
check_kcfg_refuses("CONFIG_CMDLINE_FORCE=y", GOOD_KCFG + "CONFIG_CMDLINE_FORCE=y\n", "CONFIG_CMDLINE_FORCE=y is forbidden")
# Argon M-R3-1 / Grok M4: embedded / forced bootconfig
check_kcfg_refuses("CONFIG_BOOT_CONFIG_EMBED=y", GOOD_KCFG + "CONFIG_BOOT_CONFIG_EMBED=y\n", "CONFIG_BOOT_CONFIG_EMBED=y is forbidden")
check_kcfg_refuses("CONFIG_BOOT_CONFIG_FORCE=y", GOOD_KCFG + "CONFIG_BOOT_CONFIG_FORCE=y\n", "CONFIG_BOOT_CONFIG_FORCE=y is forbidden")
check_kcfg_refuses("CONFIG_CMDLINE with '--' separator",
                   GOOD_KCFG.replace('CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
                                     'CONFIG_CMDLINE="console=ttynull -- init=/bin/sh"'),
                   "'--' stops parse_args()")
check_kcfg_refuses("CONFIG_CMDLINE with efi=runtime",
                   GOOD_KCFG.replace('CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
                                     'CONFIG_CMDLINE="console=ttynull efi=runtime"'),
                   "starts with a reserved safety/GPU prefix")
check_kcfg_refuses("CONFIG_CMDLINE with module_blacklist=",
                   GOOD_KCFG.replace('CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
                                     'CONFIG_CMDLINE="console=ttynull module_blacklist=only_one"'),
                   "starts with a reserved safety/GPU prefix")
check_kcfg_refuses("CONFIG_CMDLINE with initcall_blacklist=",
                   GOOD_KCFG.replace('CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
                                     'CONFIG_CMDLINE="console=ttynull initcall_blacklist=foo"'),
                   "starts with a reserved safety/GPU prefix")
check_kcfg_refuses("CONFIG_CMDLINE with amdgpu.modeset=1",
                   GOOD_KCFG.replace('CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
                                     'CONFIG_CMDLINE="console=ttynull amdgpu.modeset=1"'),
                   "starts with a reserved safety/GPU prefix")
# Argon M-R3-2 / Grok M3: '-' and '_' normalization and pci-stub.ids / pci_stub.ids in CONFIG_CMDLINE
check_kcfg_refuses("CONFIG_CMDLINE with underscore spi_intel.writeable=1",
                   GOOD_KCFG.replace('CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
                                     'CONFIG_CMDLINE="console=ttynull spi_intel.writeable=1"'),
                   "starts with a reserved safety/GPU prefix")
check_kcfg_refuses("CONFIG_CMDLINE with pci-stub.ids=",
                   GOOD_KCFG.replace('CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
                                     'CONFIG_CMDLINE="console=ttynull pci-stub.ids=1002:150e"'),
                   "starts with a reserved safety/GPU prefix")
check_kcfg_refuses("CONFIG_CMDLINE with underscore pci_stub.ids=",
                   GOOD_KCFG.replace('CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
                                     'CONFIG_CMDLINE="console=ttynull pci_stub.ids=1002:150e"'),
                   "starts with a reserved safety/GPU prefix")
check_kcfg_refuses("CONFIG_CMDLINE with dropped stock option fred=on",
                   GOOD_KCFG.replace('CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
                                     'CONFIG_CMDLINE="console=ttynull fred=on"'),
                   "listed in cmdline stock_drop/debug_drop/quiet_drop")
check_kcfg_refuses("CONFIG_CMDLINE conflicting with safety tail panic=0",
                   GOOD_KCFG.replace('CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"',
                                     'CONFIG_CMDLINE="console=ttynull panic=5"'),
                   "conflicts with safety/profile tail option")
check_kcfg_refuses("CONFIG_BLK_DEV_NVME=m without nvme blacklisted", GOOD_KCFG.replace("CONFIG_BLK_DEV_NVME=y", "CONFIG_BLK_DEV_NVME=m"), "CONFIG_BLK_DEV_NVME=m is not blocked")
check_kcfg_refuses("missing System.map when CONFIG_BLK_DEV_NVME=y", GOOD_KCFG, "missing System.map (required when CONFIG_BLK_DEV_NVME=y", sysmap_text=None)
check_kcfg_refuses("System.map missing nvme_init when CONFIG_BLK_DEV_NVME=y", GOOD_KCFG, "text symbol (t/T) nvme_init is missing from System.map", sysmap_text="ffffffff81000100 t mei_me_driver_init\n")
check_kcfg_refuses("System.map with non-text (d) nvme_init when CONFIG_BLK_DEV_NVME=y", GOOD_KCFG, "text symbol (t/T) nvme_init is missing from System.map",
                   sysmap_text="ffffffff81000000 d nvme_init\nffffffff81000100 t mei_me_driver_init\n")
check_kcfg_refuses("forged System.map whose nvme_init address differs from vmlinux ELF",
                   GOOD_KCFG,
                   "System.map does not match",
                   sysmap_text="ffffffff81000000 t nvme_init\nffffffff81000100 t mei_me_driver_init\n",
                   elf_symbols=[(0xFFFFFFFF81000999, "t", "nvme_init"), (0xFFFFFFFF81000100, "t", "mei_me_driver_init")])
check_kcfg_refuses("CONFIG_INTEL_MEI_ME=m without mei_me blacklisted", GOOD_KCFG.replace("CONFIG_INTEL_MEI_ME=y", "CONFIG_INTEL_MEI_ME=m"), "CONFIG_INTEL_MEI_ME=m without 'mei_me' in blacklist.modules")
check_kcfg_refuses("missing System.map when CONFIG_INTEL_MEI_ME=y",
                   GOOD_KCFG.replace("CONFIG_BLK_DEV_NVME=y", "# CONFIG_BLK_DEV_NVME is not set"),
                   "missing System.map (required when CONFIG_INTEL_MEI_ME=y", sysmap_text=None)
check_kcfg_refuses("System.map missing mei_me_driver_init when CONFIG_INTEL_MEI_ME=y", GOOD_KCFG, "text symbol (t/T) mei_me_driver_init is missing from System.map", sysmap_text="ffffffff81000000 t nvme_init\n")
check_kcfg_refuses("CONFIG_ATA=y", GOOD_KCFG + "CONFIG_ATA=y\n", "CONFIG_ATA=y")
check_kcfg_refuses("CONFIG_SATA_AHCI=m", GOOD_KCFG + "CONFIG_SATA_AHCI=m\n", "CONFIG_SATA_AHCI=m")
check_kcfg_refuses("CONFIG_SATA_AHCI_PLATFORM=y", GOOD_KCFG + "CONFIG_SATA_AHCI_PLATFORM=y\n", "CONFIG_SATA_AHCI_PLATFORM=y")
check_kcfg_refuses("CONFIG_SATA_AHCI_PLATFORM=m", GOOD_KCFG + "CONFIG_SATA_AHCI_PLATFORM=m\n", "CONFIG_SATA_AHCI_PLATFORM=m")

# Stripped vmlinux ELF with PT_LOAD + kallsyms table: accepted when System.map matches, refused when forged
def _build_kallsyms_vmlinux_and_sysmap(
    cfg_text: str,
    nvme_offset: int = 0x100,
    tamper_sysmap: bool = False,
    nvme_ks_name: str = "nvme_init",
    nvme_ks_type: str = "t",
    banner: bytes | None = None,
    names_gap: int = 8,
    omit_from_kallsyms: tuple[str, ...] = (),
    map_only: tuple[tuple[int, str, str], ...] = (),
):
    """Synthetic stripped vmlinux. Count is a .long plus 4 bytes of .balign 8 padding (names_gap=8).
    Offsets are unsigned, matching kernel/kallsyms.c (rel_base + u32 offset)."""
    base_va = 0xFFFFFFFF81000000
    body = bytearray(0x2000)
    # 0x000: linux_banner
    if banner is None:
        banner = b"Linux version 6.18.21-android17-5-g3938c8d97142 (gbos@gbos)\x00"
    body[0:len(banner)] = banner
    # token_table at 0x200: 256 single-byte ASCII tokens (token i = bytes([i]) + b"\x00")
    tok_tab_off = 0x200
    tok_idx = []
    cur = tok_tab_off
    for b_val in range(256):
        tok_idx.append(cur - tok_tab_off)
        body[cur] = b_val
        body[cur + 1] = 0
        cur += 2
    # token_index at 0x500 (512 bytes), followed immediately by kallsyms_offsets at 0x700
    tok_idx_off = 0x500
    struct.pack_into("<256H", body, tok_idx_off, *tok_idx)
    offsets_off = tok_idx_off + 512
    # _sinittext/_einittext stay in kallsyms without --all-symbols. __init_begin and
    # __init_end do not; omit_from_kallsyms drops them from the runtime table while
    # System.map (built from full_syms) still lists them.
    full_syms = [
        (0x000, "R", "linux_banner"),
        (0x080, "T", "_stext"),
        (0x0A0, "T", "_sinittext"),
        (nvme_offset, nvme_ks_type, nvme_ks_name),
        (0x140, "t", "mei_me_driver_init"),
        (0x160, "T", "_einittext"),
        (0x180, "T", "_etext"),
        (0x190, "T", "__init_begin"),
        (0x1C0, "T", "__init_end"),
    ]
    omitted = set(omit_from_kallsyms)
    syms = [s for s in full_syms if s[2] not in omitted]
    num_syms = len(syms)
    for idx, (off_val, _, _) in enumerate(syms):
        struct.pack_into("<I", body, offsets_off + idx * 4, off_val & 0xFFFFFFFF)
    rel_base_off = (offsets_off + num_syms * 4 + 7) & ~7
    struct.pack_into("<Q", body, rel_base_off, base_va)
    num_syms_off = 0x900
    names_off = num_syms_off + names_gap
    struct.pack_into("<I", body, num_syms_off, num_syms)
    ncur = names_off
    for _, tp, sname in syms:
        entry = tp.encode("ascii") + sname.encode("ascii")
        body[ncur] = len(entry)
        body[ncur + 1:ncur + 1 + len(entry)] = entry
        ncur += 1 + len(entry)
    ikblob = b"IKCFG_ST" + gzip.compress(cfg_text.encode("utf-8")) + b"IKCFG_ED"
    body.extend(ikblob)
    # 64-bit ELF header + 1 PT_LOAD program header at offset 64 (size 56), no section headers (stripped vmlinux)
    file_hdr_sz = 128
    ehdr = bytearray(64)
    ehdr[0:4] = b"\x7fELF"
    ehdr[4] = 2
    ehdr[5] = 1
    ehdr[6] = 1
    struct.pack_into("<HHIQQQIHHHHHH", ehdr, 16, 2, 62, 1, base_va, 64, 0, 0, 64, 56, 1, 64, 0, 0)
    phdr = struct.pack("<IIQQQQQQ", 1, 5, file_hdr_sz, base_va, base_va, len(body), len(body), 0x1000)
    vmlinux = bytes(ehdr) + phdr + b"\x00" * (file_hdr_sz - 120) + bytes(body)
    sysmap_lines = []
    for off_val, tp, sname in full_syms:
        map_name = "nvme_init" if sname == nvme_ks_name else sname
        map_tp = "t" if sname == nvme_ks_name else tp
        va = base_va + (0x110 if (tamper_sysmap and map_name == "nvme_init") else off_val)
        sysmap_lines.append(f"{va:016x} {map_tp} {map_name}")
    for ksym, koff in (
        ("kallsyms_token_table", tok_tab_off),
        ("kallsyms_token_index", tok_idx_off),
        ("kallsyms_offsets", offsets_off),
        ("kallsyms_relative_base", rel_base_off),
        ("kallsyms_num_syms", num_syms_off),
        ("kallsyms_names", names_off),
    ):
        sysmap_lines.append(f"{base_va + koff:016x} R {ksym}")
    # map_only symbols are in System.map and not in the runtime table. kallsyms.c
    # drops an alias that shares an endpoint address and keeps the endpoint itself.
    for off_val, tp, sname in map_only:
        sysmap_lines.append(f"{base_va + off_val:016x} {tp} {sname}")
    return vmlinux, "\n".join(sysmap_lines) + "\n"


ks_vmlinux, ks_sysmap_ok = _build_kallsyms_vmlinux_and_sysmap(GOOD_KCFG, tamper_sysmap=False)
kd_ks_ok = tmp / "kdir-kallsyms-ok"
kd_ks_ok.mkdir(parents=True, exist_ok=True)
(kd_ks_ok / ".config").write_text(GOOD_KCFG)
(kd_ks_ok / "System.map").write_text(ks_sysmap_ok)
(kd_ks_ok / "bzImage").write_bytes(devprofile.wrap_hdrs_bzimage(_compress_payload(ks_vmlinux, "lz4")))
check("check-kconfig accepts stripped vmlinux ELF when System.map matches kallsyms",
      devprofile.check_kconfig(amd, kd_ks_ok)["CONFIG_DRM_AMDGPU"] == "m")

_, ks_sysmap_bad = _build_kallsyms_vmlinux_and_sysmap(GOOD_KCFG, tamper_sysmap=True)
kd_ks_bad = tmp / "kdir-kallsyms-bad"
kd_ks_bad.mkdir(parents=True, exist_ok=True)
(kd_ks_bad / ".config").write_text(GOOD_KCFG)
(kd_ks_bad / "System.map").write_text(ks_sysmap_bad)
(kd_ks_bad / "bzImage").write_bytes(devprofile.wrap_hdrs_bzimage(_compress_payload(ks_vmlinux, "lz4")))
try:
    devprofile.check_kconfig(amd, kd_ks_bad)
    check("check-kconfig refuses stripped vmlinux ELF when System.map disagrees with kallsyms", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses stripped vmlinux ELF when System.map disagrees with kallsyms ({e})",
          "System.map entry for nvme_init" in str(e) and "kallsyms" in str(e))

# Every symbol in KCONFIG_NEVER_YM is refused for both =y and =m
for sym, _ in devprofile.KCONFIG_NEVER_YM:
    for val in ("y", "m"):
        check_kcfg_refuses(f"{sym}={val}", GOOD_KCFG + f"{sym}={val}\n", f"{sym}={val} is forbidden")
# Named, not generated from KCONFIG_NEVER_YM: deleting the PFRUT row must still fail these.
check_kcfg_refuses(
    "named CONFIG_ACPI_PFRUT=y",
    GOOD_KCFG + "CONFIG_ACPI_PFRUT=y\n",
    "Platform Firmware Runtime Update",
)
check_kcfg_refuses(
    "named CONFIG_ACPI_PFRUT=m",
    GOOD_KCFG + "CONFIG_ACPI_PFRUT=m\n",
    "Platform Firmware Runtime Update",
)
kd_pfrut_off = make_kdir("pfrut-unset", GOOD_KCFG + "# CONFIG_ACPI_PFRUT is not set\n")
try:
    _pfrut_off = devprofile.check_kconfig(amd, kd_pfrut_off)
    check(
        "check-kconfig accepts a kernel that leaves CONFIG_ACPI_PFRUT unset",
        _pfrut_off.get("CONFIG_ACPI_PFRUT") is None and _pfrut_off["CONFIG_DRM_AMDGPU"] == "m",
    )
except Exception as e:
    check(f"check-kconfig accepts a kernel that leaves CONFIG_ACPI_PFRUT unset ({e})", False)

# Every symbol in KCONFIG_NEVER_BUILTIN is refused when =y
for sym, _ in devprofile.KCONFIG_NEVER_BUILTIN:
    check_kcfg_refuses(f"built-in {sym}=y", GOOD_KCFG + f"{sym}=y\n", f"{sym}=y is forbidden")

# Codex M1: CONFIG_CRYPTO_DEV_CCP=y and CONFIG_CRYPTO_DEV_SP_PSP=y are allowed when CONFIG_CRYPTO_DEV_CCP_DD=m and 'ccp' is blacklisted,
# and refused when CONFIG_CRYPTO_DEV_CCP_DD=y or unset or 'ccp' is not blacklisted.
kd_ccp_ok = make_kdir("ccp_dd_m_ok", GOOD_KCFG + "CONFIG_CRYPTO_DEV_CCP=y\nCONFIG_CRYPTO_DEV_SP_PSP=y\nCONFIG_CRYPTO_DEV_CCP_DD=m\n")
check("check-kconfig accepts CONFIG_CRYPTO_DEV_CCP=y + CONFIG_CRYPTO_DEV_SP_PSP=y when CONFIG_CRYPTO_DEV_CCP_DD=m and 'ccp' is blacklisted",
      devprofile.check_kconfig(amd, kd_ccp_ok)["CONFIG_CRYPTO_DEV_CCP_DD"] == "m")
check_kcfg_refuses("CONFIG_CRYPTO_DEV_CCP=y without CONFIG_CRYPTO_DEV_CCP_DD=m",
                   GOOD_KCFG + "CONFIG_CRYPTO_DEV_CCP=y\n",
                   "CONFIG_CRYPTO_DEV_CCP=y is forbidden unless the CCP linkage unit CONFIG_CRYPTO_DEV_CCP_DD=m")
check_kcfg_refuses("CONFIG_CRYPTO_DEV_SP_PSP=y without CONFIG_CRYPTO_DEV_CCP_DD=m",
                   GOOD_KCFG + "CONFIG_CRYPTO_DEV_SP_PSP=y\n",
                   "CONFIG_CRYPTO_DEV_SP_PSP=y is forbidden unless the CCP linkage unit CONFIG_CRYPTO_DEV_CCP_DD=m")
check_kcfg_refuses("CONFIG_CRYPTO_DEV_CCP=y + CONFIG_CRYPTO_DEV_CCP_DD=m on generic-x86 without 'ccp' blacklisted",
                   GOOD_KCFG + "CONFIG_CRYPTO_DEV_CCP=y\nCONFIG_CRYPTO_DEV_CCP_DD=m\n",
                   "without 'ccp' in blacklist.modules",
                   prof=gx)

# Modular (=m) KCONFIG_NEVER_BUILTIN symbols whose module is not in blacklist.modules must be refused
check_kcfg_refuses("CONFIG_SPI_INTEL_PLATFORM=m without spi_intel_platform blacklisted",
                   GOOD_KCFG + "CONFIG_SPI_INTEL_PLATFORM=m\n",
                   "CONFIG_SPI_INTEL_PLATFORM=m without 'spi_intel_platform' in blacklist.modules is forbidden")
check_kcfg_refuses("CONFIG_EEPROM_EE1004=m without ee1004 blacklisted",
                   GOOD_KCFG + "CONFIG_EEPROM_EE1004=m\n",
                   "CONFIG_EEPROM_EE1004=m without 'ee1004' in blacklist.modules is forbidden")
# Grok M1: CONFIG_TEE=m produces tee.ko (not amdtee.ko) and must require 'tee' in blacklist.modules
check_kcfg_refuses("CONFIG_TEE=m on amd-hx370 without 'tee' blacklisted (Grok M1)",
                   GOOD_KCFG + "CONFIG_TEE=m\n",
                   "CONFIG_TEE=m without 'tee' in blacklist.modules is forbidden")
# CONFIG_SPI_AMD=m builds both spi-amd.ko and spi-amd-pci.ko and requires both in blacklist.modules
amd_no_spi_pci = dict(amd, blacklist=dict(amd["blacklist"], modules=[m for m in amd["blacklist"]["modules"] if m != "spi_amd_pci"]))
check_kcfg_refuses("CONFIG_SPI_AMD=m without spi_amd_pci blacklisted",
                   GOOD_KCFG + "CONFIG_SPI_AMD=m\n",
                   "CONFIG_SPI_AMD=m without 'spi_amd_pci' in blacklist.modules is forbidden",
                   prof=amd_no_spi_pci)
for sym, mod in (("CONFIG_TCG_CRB", "tpm_crb"), ("CONFIG_CRYPTO_DEV_CCP_DD", "ccp"), ("CONFIG_CRYPTO_DEV_CCP_CRYPTO", "ccp_crypto"),
                 ("CONFIG_AMDTEE", "amdtee"), ("CONFIG_SPI_AMD", "spi_amd"), ("CONFIG_MMC_SDHCI_PCI", "sdhci_pci"),
                 ("CONFIG_SP5100_TCO", "sp5100_tco"), ("CONFIG_UCSI_ACPI", "ucsi_acpi"),
                 ("CONFIG_INTEL_THC_HID", "intel_thc")):
    check_kcfg_refuses(f"{sym}=m on generic-x86 without {mod} blacklisted",
                       GOOD_KCFG + f"{sym}=m\n",
                       f"{sym}=m without {mod!r} in blacklist.modules is forbidden",
                       prof=gx)
check_kcfg_refuses("built-in CONFIG_DRM_AMDGPU=y on amd-hx370", GOOD_KCFG.replace("CONFIG_DRM_AMDGPU=m", "CONFIG_DRM_AMDGPU=y"), "CONFIG_DRM_AMDGPU=y is forbidden")
check_kcfg_refuses("missing CONFIG_DRM_AMDGPU on amd-hx370", GOOD_KCFG.replace("CONFIG_DRM_AMDGPU=m", "# CONFIG_DRM_AMDGPU is not set"), "requires CONFIG_DRM_AMDGPU=m")
check_kcfg_refuses("CONFIG_DRM_AMD_ISP=m on amd-hx370", GOOD_KCFG + "CONFIG_DRM_AMD_ISP=m\n", "CONFIG_DRM_AMD_ISP=m")
check_kcfg_refuses("bzImage embedded IKCFG mismatch with .config", GOOD_KCFG, "embedded IKCFG does not match",
                   ikcfg_text=GOOD_KCFG + "CONFIG_TCG_TPM=y\n")

# Corrupt embedded IKCFG payload in bzImage must fail closed (not silently pass)
kd_corrupt = make_kdir("corrupt_ikcfg", GOOD_KCFG, ikcfg_text=None)
bad_elf = devprofile.build_test_vmlinux_elf(GOOD_KCFG).split(b"IKCFG_ST", 1)[0] + b"IKCFG_ST\x1f\x8b\x08\x00corrupt-gzip-streamIKCFG_ED"
(kd_corrupt / "bzImage").write_bytes(devprofile.wrap_hdrs_bzimage(gzip.compress(bad_elf)))
try:
    devprofile.check_kconfig(amd, kd_corrupt)
    check("check-kconfig refuses corrupted bzImage IKCFG gzip stream", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses corrupted bzImage IKCFG gzip stream ({e})", "corrupted embedded IKCFG" in str(e))

# Real-sized bzImage (>4KB) with valid ELF payload missing IKCFG must fail closed
kd_no_ikcfg = make_kdir("missing_ikcfg", GOOD_KCFG, ikcfg_text=None)
no_ik_elf = devprofile.build_test_vmlinux_elf(GOOD_KCFG).split(b"IKCFG_ST", 1)[0] + b"\0" * 5000
(kd_no_ikcfg / "bzImage").write_bytes(devprofile.wrap_hdrs_bzimage(gzip.compress(no_ik_elf)))
try:
    devprofile.check_kconfig(amd, kd_no_ikcfg)
    check("check-kconfig refuses >4KB bzImage missing embedded IKCFG", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses >4KB bzImage missing embedded IKCFG ({e})", "missing embedded IKCFG" in str(e))

# Pure-Python multi-block LZ4-legacy bzImage with x86 HdrS header (no external lz4 binary required)
kd_py_lz4 = make_kdir("py_lz4_hdrs_mismatch", GOOD_KCFG, ikcfg_text=GOOD_KCFG + "CONFIG_ATA=y\n", fmt="lz4")
extracted_py = devprofile._extract_bzimage_ikcfg(kd_py_lz4 / "bzImage", force_pure_python_lz4=True)
check("pure-Python LZ4-legacy HdrS extractor decodes multi-block vmlinux IKCFG",
      extracted_py is not None and "CONFIG_ATA=y" in extracted_py)
try:
    devprofile.check_kconfig(amd, kd_py_lz4)
    check("check-kconfig refuses multi-block LZ4 HdrS bzImage IKCFG mismatch", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses multi-block LZ4 HdrS bzImage IKCFG mismatch ({e})", "embedded IKCFG does not match" in str(e))

# Fake lz4 CLI emitting truncated output (IKCFG_ST without IKCFG_ED) with exit 63 and exit 0 must fall back to pure-Python decoder
fake_bin = tmp / "fake-lz4-bin"
fake_bin.mkdir(parents=True, exist_ok=True)
orig_path = os.environ.get("PATH", "")
for exit_code in (63, 0):
    fake_lz4 = fake_bin / "lz4"
    fake_lz4.write_text(
        "#!/bin/sh\n"
        "printf 'ELF\\000\\000\\000\\000IKCFG_ST\\037\\213\\010\\000truncated-without-end'\n"
        f"exit {exit_code}\n"
    )
    fake_lz4.chmod(0o755)
    os.environ["PATH"] = f"{fake_bin}:{orig_path}"
    try:
        dec = devprofile._extract_bzimage_ikcfg(kd_py_lz4 / "bzImage", force_pure_python_lz4=False)
        check(f"truncated lz4 CLI (exit {exit_code}, missing IKCFG_ED) falls back cleanly to pure-Python LZ4 decoder",
              dec is not None and "CONFIG_ATA=y" in dec)
        try:
            devprofile.check_kconfig(amd, kd_py_lz4)
            check(f"check-kconfig with truncated lz4 CLI (exit {exit_code}) still catches IKCFG mismatch", False)
        except devprofile.ProfileError as e:
            check(f"check-kconfig with truncated lz4 CLI (exit {exit_code}) still catches IKCFG mismatch ({e})",
                  "embedded IKCFG does not match" in str(e))
    finally:
        os.environ["PATH"] = orig_path

# --- round 4: ambiguous payload, .symtab vs kallsyms, and the other review fixtures ---

def _point_hdrs_at_append(image: bytes, extra: bytes) -> bytes:
    setup_sects = image[0x1F1] or 4
    setup_end = (setup_sects + 1) * 512
    out = bytearray(image + extra)
    struct.pack_into("<II", out, 0x248, len(image) - setup_end, len(extra))
    return bytes(out)


def _kdir_bz(name, cfg_text, sysmap_text, blob: bytes):
    kd = tmp / name
    kd.mkdir(parents=True, exist_ok=True)
    (kd / ".config").write_text(cfg_text)
    (kd / "System.map").write_text(sysmap_text)
    (kd / "bzImage").write_bytes(blob)
    return kd


mal_elf = devprofile.build_test_vmlinux_elf(GOOD_KCFG + "CONFIG_CMDLINE_OVERRIDE=y\n")
benign_elf = devprofile.build_test_vmlinux_elf(GOOD_KCFG)
crafted = _point_hdrs_at_append(
    devprofile.wrap_hdrs_bzimage(gzip.compress(mal_elf)),
    gzip.compress(benign_elf),
)
kd_hdrs_redirect = _kdir_bz("hdrs-redirect", GOOD_KCFG, GOOD_SYSMAP, crafted)
try:
    devprofile.check_kconfig(amd, kd_hdrs_redirect)
    check("check-kconfig refuses HdrS redirected onto an appended benign payload (synthetic, not a bootable kernel)", False)
except devprofile.ProfileError as e:
    check(
        "check-kconfig refuses HdrS redirected onto an appended benign payload "
        f"(synthetic, not a bootable kernel) ({e})",
        "more than one plausible payload or IKCFG candidate" in str(e)
        and "scripts/extract-ikconfig" in str(e),
    )

two_ik = devprofile.build_test_vmlinux_elf(GOOD_KCFG) + b"IKCFG_ST" + gzip.compress(
    (GOOD_KCFG + "CONFIG_DEVMEM=y\n").encode()
) + b"IKCFG_ED"
kd_two = _kdir_bz("two-ikcfg", GOOD_KCFG, GOOD_SYSMAP, devprofile.wrap_hdrs_bzimage(gzip.compress(two_ik)))
try:
    devprofile.check_kconfig(amd, kd_two)
    check("check-kconfig refuses two IKCFG pairs inside the decompressed payload", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses two IKCFG pairs inside the decompressed payload ({e})",
          "more than one plausible payload or IKCFG candidate" in str(e))

kd_lzo = _kdir_bz("lzo", GOOD_KCFG, GOOD_SYSMAP, devprofile.wrap_hdrs_bzimage(b"\x89LZO\x00\x0d\x0a\x1a\x0a" + b"\x00" * 16))
try:
    devprofile.check_kconfig(amd, kd_lzo)
    check("check-kconfig refuses LZO payload magic", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses LZO payload magic ({e})", "unsupported or undecodable" in str(e))

zstd_elf = devprofile.build_test_vmlinux_elf(GOOD_KCFG)
zstd_frame = _compress_payload(zstd_elf, "zstd")
kd_zstd_pad = _kdir_bz("zstd-trailer-ok", GOOD_KCFG, GOOD_SYSMAP, devprofile.wrap_hdrs_bzimage(zstd_frame + b"\x00\x01\x00\x00"))
check("check-kconfig accepts a zstd frame plus a 4-byte size trailer",
      devprofile.check_kconfig(amd, kd_zstd_pad)["CONFIG_DRM_AMDGPU"] == "m")
kd_zstd_junk = _kdir_bz("zstd-trailer-bad", GOOD_KCFG, GOOD_SYSMAP, devprofile.wrap_hdrs_bzimage(zstd_frame + b"\x00" * 8))
try:
    devprofile.check_kconfig(amd, kd_zstd_junk)
    check("check-kconfig refuses zstd trailing garbage past the 4-byte trailer", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses zstd trailing garbage past the 4-byte trailer ({e})",
          "corrupt or trailing-garbage zstd payload" in str(e))


def _append_nonloaded_symtab(vmlinux: bytes, sysmap_text: str) -> bytes:
    """Append a non-loaded .symtab that matches sysmap_text. PT_LOAD bytes are not modified."""
    entries = []
    for line in sysmap_text.splitlines():
        line = line.strip()
        if not line:
            continue
        addr_s, tp, name = line.split()
        entries.append((int(addr_s, 16), tp, name))
    strtab = bytearray(b"\x00")
    symtab = bytearray(24)
    for addr, tp, name in entries:
        name_off = len(strtab)
        strtab.extend(name.encode("ascii") + b"\x00")
        st_info = ((1 if tp.isupper() else 0) << 4) | (2 if tp in ("t", "T") else 1)
        symtab.extend(struct.pack("<IBBHQQ", name_off, st_info, ord(tp) & 0xFF, 1, addr, 16))
    shstr = bytearray(b"\x00.symtab\x00.strtab\x00.shstrtab\x00")
    blob = bytearray(vmlinux)
    sym_off = len(blob)
    blob += symtab
    str_off = len(blob)
    blob += strtab
    shstr_off = len(blob)
    blob += shstr
    shdr_off = len(blob)
    sh_null = b"\x00" * 64
    sh_sym = struct.pack("<IIQQQQIIQQ", 1, 2, 0, 0, sym_off, len(symtab), 2, 1, 8, 24)
    sh_str = struct.pack("<IIQQQQIIQQ", 9, 3, 0, 0, str_off, len(strtab), 0, 0, 1, 0)
    sh_shstr = struct.pack("<IIQQQQIIQQ", 17, 3, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0)
    blob += sh_null + sh_sym + sh_str + sh_shstr
    struct.pack_into("<Q", blob, 40, shdr_off)
    struct.pack_into("<H", blob, 60, 4)
    struct.pack_into("<H", blob, 62, 3)
    return bytes(blob)


ks_live, ks_live_map = _build_kallsyms_vmlinux_and_sysmap(GOOD_KCFG, nvme_ks_name="nvme_live")
kd_live = _kdir_bz("kallsyms-renamed", GOOD_KCFG, ks_live_map, devprofile.wrap_hdrs_bzimage(gzip.compress(ks_live)))
try:
    devprofile.check_kconfig(amd, kd_live)
    check("check-kconfig refuses kallsyms that renamed nvme_init when no .symtab is present", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses kallsyms that renamed nvme_init when no .symtab is present ({e})",
          "nvme_init" in str(e) and "kallsyms" in str(e))

ks_masked = _append_nonloaded_symtab(ks_live, ks_live_map)
kd_masked = _kdir_bz(
    "symtab-masks-kallsyms", GOOD_KCFG, ks_live_map, devprofile.wrap_hdrs_bzimage(gzip.compress(ks_masked))
)
try:
    devprofile.check_kconfig(amd, kd_masked)
    check("check-kconfig refuses a matching .symtab that hides a renamed runtime kallsyms (synthetic)", False)
except devprofile.ProfileError as e:
    check(
        "check-kconfig refuses a matching .symtab that hides a renamed runtime kallsyms "
        f"(synthetic) ({e})",
        "runtime kallsyms disagrees with .symtab" in str(e) and "nvme_init" in str(e),
    )

ks_both, ks_both_map = _build_kallsyms_vmlinux_and_sysmap(GOOD_KCFG)
ks_both_sym = _append_nonloaded_symtab(ks_both, ks_both_map)
kd_both = _kdir_bz("symtab-and-kallsyms-agree", GOOD_KCFG, ks_both_map, devprofile.wrap_hdrs_bzimage(gzip.compress(ks_both_sym)))
check(
    "check-kconfig accepts a synthetic ELF whose st_other stores the nm letter and whose .symtab agrees with kallsyms",
    devprofile.check_kconfig(amd, kd_both)["CONFIG_DRM_AMDGPU"] == "m",
)

kd_two_line = _kdir_bz("kallsyms-two-line-map", GOOD_KCFG, GOOD_SYSMAP, devprofile.wrap_hdrs_bzimage(gzip.compress(ks_vmlinux)))
try:
    devprofile.check_kconfig(amd, kd_two_line)
    check("check-kconfig refuses a two-line System.map against a kallsyms payload", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses a two-line System.map against a kallsyms payload ({e})",
          "missing linux_banner" in str(e))

ks_typed, ks_typed_map = _build_kallsyms_vmlinux_and_sysmap(GOOD_KCFG, nvme_ks_type="d")
kd_typed = _kdir_bz("kallsyms-type-d", GOOD_KCFG, ks_typed_map, devprofile.wrap_hdrs_bzimage(gzip.compress(ks_typed)))
try:
    devprofile.check_kconfig(amd, kd_typed)
    check("check-kconfig refuses kallsyms type d when System.map says t", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses kallsyms type d when System.map says t ({e})", "nvme_init" in str(e))

ks_llvm, ks_llvm_map = _build_kallsyms_vmlinux_and_sysmap(GOOD_KCFG, nvme_ks_name="nvme_init.llvm.123")
kd_llvm = _kdir_bz("kallsyms-llvm-rename", GOOD_KCFG, ks_llvm_map, devprofile.wrap_hdrs_bzimage(gzip.compress(ks_llvm)))
try:
    devprofile.check_kconfig(amd, kd_llvm)
    check("check-kconfig refuses kallsyms name nvme_init.llvm.123", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses kallsyms name nvme_init.llvm.123 ({e})", "nvme_init" in str(e))

ks_banner, ks_banner_map = _build_kallsyms_vmlinux_and_sysmap(GOOD_KCFG, banner=b"Not a Linux banner\x00")
kd_banner = _kdir_bz("kallsyms-banner", GOOD_KCFG, ks_banner_map, devprofile.wrap_hdrs_bzimage(gzip.compress(ks_banner)))
try:
    devprofile.check_kconfig(amd, kd_banner)
    check("check-kconfig refuses a linux_banner that does not start with 'Linux version '", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses a linux_banner that does not start with 'Linux version ' ({e})",
          "linux_banner" in str(e))

ks_gap, ks_gap_map = _build_kallsyms_vmlinux_and_sysmap(GOOD_KCFG, names_gap=4)
kd_gap = _kdir_bz("kallsyms-layout", GOOD_KCFG, ks_gap_map, devprofile.wrap_hdrs_bzimage(gzip.compress(ks_gap)))
try:
    devprofile.check_kconfig(amd, kd_gap)
    check("check-kconfig refuses kallsyms names that are not 8 bytes after the count", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses kallsyms names that are not 8 bytes after the count ({e})",
          "kallsyms table layout" in str(e))

kd_nobz = make_kdir("missing-bzimage", GOOD_KCFG)
(kd_nobz / "bzImage").unlink()
try:
    devprofile.check_kconfig(amd, kd_nobz)
    check("check-kconfig refuses a kernel dir with no bzImage", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses a kernel dir with no bzImage ({e})", "missing bzImage" in str(e))

check_kcfg_refuses(
    "CONFIG_LTO_CLANG_THIN=y while NVMe is built-in",
    GOOD_KCFG + "CONFIG_LTO_CLANG_THIN=y\n",
    "CONFIG_LTO_CLANG_THIN=y is forbidden",
)
check_kcfg_refuses(
    "CONFIG_LTO_CLANG_FULL=y while MEI is built-in",
    GOOD_KCFG + "CONFIG_LTO_CLANG_FULL=y\n",
    "CONFIG_LTO_CLANG_FULL=y is forbidden",
)
check_kcfg_refuses(
    "CONFIG_CROS_EC_UART=m without cros_ec_uart blacklisted",
    GOOD_KCFG + "CONFIG_CROS_EC=m\nCONFIG_CROS_EC_UART=m\n",
    "CONFIG_CROS_EC_UART=m without 'cros_ec_uart' in blacklist.modules",
)
check_kcfg_refuses(
    "CONFIG_CROS_EC_RPMSG=y",
    GOOD_KCFG + "CONFIG_CROS_EC_RPMSG=y\n",
    "CONFIG_CROS_EC_RPMSG=y is forbidden",
)
check_kcfg_refuses(
    "CONFIG_TCG_TIS_SPI_CR50=y without CONFIG_TCG_TIS_SPI=m",
    GOOD_KCFG + "CONFIG_TCG_TIS_SPI_CR50=y\n",
    "CONFIG_TCG_TIS_SPI_CR50=y is forbidden unless CONFIG_TCG_TIS_SPI=m",
)
kd_cr50 = make_kdir("cr50-bool", GOOD_KCFG + "CONFIG_TCG_TIS_SPI=m\nCONFIG_TCG_TIS_SPI_CR50=y\n")
check("check-kconfig accepts CR50=y when CONFIG_TCG_TIS_SPI=m and tpm_tis_spi is blacklisted",
      devprofile.check_kconfig(amd, kd_cr50)["CONFIG_TCG_TIS_SPI_CR50"] == "y")
amd_no_tis = dict(amd, blacklist=dict(amd["blacklist"], modules=[m for m in amd["blacklist"]["modules"] if m != "tpm_tis_spi"]))
check_kcfg_refuses(
    "CONFIG_TCG_TIS_SPI_CR50=y when tpm_tis_spi is not blacklisted",
    GOOD_KCFG + "CONFIG_TCG_TIS_SPI=m\nCONFIG_TCG_TIS_SPI_CR50=y\n",
    "without 'tpm_tis_spi' in blacklist.modules",
    prof=amd_no_tis,
)

kd_msr = make_kdir("msr-ko", GOOD_KCFG)
(kd_msr / "modules").mkdir()
(kd_msr / "modules" / "msr.ko").write_bytes(b"\x7fELFmsr")
try:
    devprofile.check_kconfig(amd, kd_msr)
    check("check-kconfig refuses a shipped msr.ko", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses a shipped msr.ko ({e})", "never-load" in str(e))

kd_tb = make_kdir("thunderbolt-ko", GOOD_KCFG)
(kd_tb / "modules").mkdir()
(kd_tb / "modules" / "thunderbolt.ko").write_bytes(b"\x7fELFtb")
(kd_tb / "modules" / "amdgpu.ko").write_bytes(b"\x7fELFgpu")
check("check-kconfig accepts thunderbolt.ko when CONFIG_USB4=m and it is blacklisted",
      devprofile.check_kconfig(amd, kd_tb)["CONFIG_USB4"] == "m")

kd_spi_ko = make_kdir("spi-intel-ko", GOOD_KCFG)
(kd_spi_ko / "modules").mkdir()
(kd_spi_ko / "modules" / "spi-intel.ko").write_bytes(b"\x7fELFspi")
try:
    devprofile.check_kconfig(amd, kd_spi_ko)
    check("check-kconfig refuses spi-intel.ko when CONFIG_SPI_INTEL is not =m", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses spi-intel.ko when CONFIG_SPI_INTEL is not =m ({e})",
          "does not correspond to a =m" in str(e))

# --- round 5: firmware writers, kallsyms without KALLSYMS_ALL, and the minor fixtures ---

check_kcfg_refuses(
    "CONFIG_RESET_ATTACK_MITIGATION=y names the NV variable",
    GOOD_KCFG + "CONFIG_RESET_ATTACK_MITIGATION=y\n",
    "MemoryOverwriteRequestControl",
)
kd_apei = make_kdir("apei-amd", GOOD_KCFG + "CONFIG_ACPI_APEI=y\n")
try:
    _apei_ok = devprofile.check_kconfig(amd, kd_apei)
    check("check-kconfig accepts CONFIG_ACPI_APEI=y on amd-hx370 because quiet and serial entries carry erst_disable",
          _apei_ok["CONFIG_ACPI_APEI"] == "y")
except Exception as e:
    check(f"check-kconfig accepts CONFIG_ACPI_APEI=y on amd-hx370 ({e})", False)
check_kcfg_refuses(
    "CONFIG_ACPI_APEI=y on generic-x86 without erst_disable",
    GOOD_KCFG + "CONFIG_ACPI_APEI=y\n",
    "erst_disable",
    prof=gx,
)
amd_quiet_drop_erst = copy.deepcopy(amd)
amd_quiet_drop_erst["cmdline"]["quiet_drop"] = list(amd_quiet_drop_erst["cmdline"]["quiet_drop"]) + ["erst_disable"]
try:
    devprofile.check_kconfig(amd_quiet_drop_erst, kd_apei)
    check("check-kconfig refuses CONFIG_ACPI_APEI=y when quiet_drop removes erst_disable from the quiet entry", False)
except devprofile.ProfileError as e:
    check(
        f"check-kconfig refuses CONFIG_ACPI_APEI=y when quiet_drop removes erst_disable from the quiet entry ({e})",
        "erst_disable" in str(e) and "quiet entry" in str(e) and "serial entry" not in str(e),
    )
amd_no_erst = copy.deepcopy(amd)
amd_no_erst["cmdline"]["tail"] = [t for t in amd_no_erst["cmdline"]["tail"] if t != "erst_disable"]
try:
    devprofile.check_kconfig(amd_no_erst, kd_apei)
    check("check-kconfig refuses CONFIG_ACPI_APEI=y when the quiet and serial entries both lack erst_disable", False)
except devprofile.ProfileError as e:
    check(
        f"check-kconfig refuses CONFIG_ACPI_APEI=y when the quiet and serial entries both lack erst_disable ({e})",
        "erst_disable" in str(e) and "quiet entry" in str(e) and "serial entry" in str(e),
    )
kd_pstore = make_kdir("efivars-pstore", GOOD_KCFG + "CONFIG_EFI_VARS_PSTORE=y\n")
check("check-kconfig accepts CONFIG_EFI_VARS_PSTORE=y when efi_pstore.pstore_disable=1 is on the tail",
      devprofile.check_kconfig(amd, kd_pstore)["CONFIG_EFI_VARS_PSTORE"] == "y")
amd_no_pstore = copy.deepcopy(amd)
amd_no_pstore["_safety"]["cmdline"]["tail"] = [
    t for t in amd_no_pstore["_safety"]["cmdline"]["tail"] if not str(t).startswith("efi_pstore.")
]
check_kcfg_refuses(
    "CONFIG_EFI_VARS_PSTORE=y without efi_pstore.pstore_disable=1",
    GOOD_KCFG + "CONFIG_EFI_VARS_PSTORE=y\n",
    "efi_pstore.pstore_disable=1",
    prof=amd_no_pstore,
)

ahci_mods = list(gx["blacklist"]["modules"]) + ["ahci"]
gx_ahci = copy.deepcopy(gx)
gx_ahci["blacklist"]["modules"] = ahci_mods
check_kcfg_refuses(
    "CONFIG_SATA_AHCI=m with ahci blacklisted but not libahci",
    GOOD_KCFG + "CONFIG_SATA_AHCI=m\n",
    "libahci",
    prof=gx_ahci,
)
gx_both = copy.deepcopy(gx)
gx_both["blacklist"]["modules"] = ahci_mods + ["libahci"]
kd_libahci = make_kdir("libahci-both", GOOD_KCFG + "CONFIG_SATA_AHCI=m\n")
check("check-kconfig accepts CONFIG_SATA_AHCI=m when ahci and libahci are both blacklisted",
      devprofile.check_kconfig(gx_both, kd_libahci).get("CONFIG_SATA_AHCI") == "m")

ks_all_n, ks_all_n_map = _build_kallsyms_vmlinux_and_sysmap(
    GOOD_KCFG, omit_from_kallsyms=("__init_begin", "__init_end"),
)
kd_all_n = _kdir_bz(
    "kallsyms-all-n", GOOD_KCFG, ks_all_n_map,
    devprofile.wrap_hdrs_bzimage(gzip.compress(ks_all_n)),
)
try:
    _all_n_ok = devprofile.check_kconfig(amd, kd_all_n)
    check("check-kconfig accepts KALLSYMS_ALL=n when the initcall and inittext range symbols match",
          _all_n_ok["CONFIG_BLK_DEV_NVME"] == "y")
except Exception as e:
    check(f"check-kconfig accepts KALLSYMS_ALL=n when the initcall and inittext range symbols match ({e})", False)
ks_alias, ks_alias_map = _build_kallsyms_vmlinux_and_sysmap(
    GOOD_KCFG,
    omit_from_kallsyms=("__init_begin", "__init_end"),
    map_only=((0x160, "T", "einittext_alias"),),
)
kd_alias = _kdir_bz(
    "kallsyms-endpoint-alias", GOOD_KCFG, ks_alias_map,
    devprofile.wrap_hdrs_bzimage(gzip.compress(ks_alias)),
)
try:
    _alias_ok = devprofile.check_kconfig(amd, kd_alias)
    _alias_addr = next((ln.split()[0] for ln in ks_alias_map.splitlines() if ln.endswith(" einittext_alias")), None)
    _einit_addr = next((ln.split()[0] for ln in ks_alias_map.splitlines() if ln.endswith(" _einittext")), None)
    check("check-kconfig accepts KALLSYMS_ALL=n when an _einittext alias is only in System.map",
          _alias_ok["CONFIG_BLK_DEV_NVME"] == "y" and _alias_addr is not None
          and _alias_addr == _einit_addr and " T einittext_alias" in ks_alias_map
          and "einittext_alias" not in ks_alias.decode("latin1"))
except Exception as e:
    check(f"check-kconfig accepts KALLSYMS_ALL=n when an _einittext alias is only in System.map ({e})", False)
_sinit_lines = []
for _line in ks_all_n_map.splitlines():
    _addr, _tp, _sym = _line.split()
    if _sym == "_sinittext":
        _addr = f"{int(_addr, 16) + 8:016x}"
    _sinit_lines.append(f"{_addr} {_tp} {_sym}")
kd_sinit = _kdir_bz(
    "kallsyms-sinittext-shift", GOOD_KCFG, "\n".join(_sinit_lines) + "\n",
    devprofile.wrap_hdrs_bzimage(gzip.compress(ks_all_n)),
)
try:
    devprofile.check_kconfig(amd, kd_sinit)
    check("check-kconfig refuses a System.map whose _sinittext is 8 bytes off the runtime table", False)
except devprofile.ProfileError as e:
    check(
        f"check-kconfig refuses a System.map whose _sinittext is 8 bytes off the runtime table ({e})",
        "_sinittext" in str(e) and "kallsyms" in str(e),
    )
ks_all_n_bad, ks_all_n_bad_map = _build_kallsyms_vmlinux_and_sysmap(
    GOOD_KCFG, nvme_ks_name="nvme_live", omit_from_kallsyms=("__init_begin", "__init_end"),
)
kd_all_n_bad = _kdir_bz(
    "kallsyms-all-n-mismatch", GOOD_KCFG, ks_all_n_bad_map,
    devprofile.wrap_hdrs_bzimage(gzip.compress(ks_all_n_bad)),
)
try:
    devprofile.check_kconfig(amd, kd_all_n_bad)
    check("check-kconfig refuses a KALLSYMS_ALL=n image whose initcall name does not match", False)
except devprofile.ProfileError as e:
    check(
        f"check-kconfig refuses a KALLSYMS_ALL=n image whose initcall name does not match ({e})",
        "nvme_init" in str(e) and "kallsyms" in str(e) and "__init_end" not in str(e),
    )
all_y_cfg = GOOD_KCFG + "CONFIG_KALLSYMS_ALL=y\n"
ks_all_y, ks_all_y_map = _build_kallsyms_vmlinux_and_sysmap(
    all_y_cfg, omit_from_kallsyms=("__init_end",),
)
kd_all_y = _kdir_bz(
    "kallsyms-all-y-missing-init-end", all_y_cfg, ks_all_y_map,
    devprofile.wrap_hdrs_bzimage(gzip.compress(ks_all_y)),
)
try:
    devprofile.check_kconfig(amd, kd_all_y)
    check("check-kconfig refuses KALLSYMS_ALL=y when kallsyms has no __init_end", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses KALLSYMS_ALL=y when kallsyms has no __init_end ({e})",
          "__init_end" in str(e) and "kallsyms" in str(e))

ks_oldban, ks_oldban_map = _build_kallsyms_vmlinux_and_sysmap(GOOD_KCFG, banner=b"Linux version 6.12.0\x00")
kd_oldban = _kdir_bz(
    "kallsyms-banner-612", GOOD_KCFG, ks_oldban_map,
    devprofile.wrap_hdrs_bzimage(gzip.compress(ks_oldban)),
)
try:
    devprofile.check_kconfig(amd, kd_oldban)
    check("check-kconfig refuses linux_banner Linux version 6.12.0", False)
except devprofile.ProfileError as e:
    check(f"check-kconfig refuses linux_banner Linux version 6.12.0 ({e})",
          "6.18" in str(e) and "android17" in str(e) and "release family" in str(e))
_off_ic = (
    GOOD_KCFG.replace("CONFIG_BLK_DEV_NVME=y", "# CONFIG_BLK_DEV_NVME is not set")
    .replace("CONFIG_INTEL_MEI_ME=y", "# CONFIG_INTEL_MEI_ME is not set")
)
ks_off_old, ks_off_old_map = _build_kallsyms_vmlinux_and_sysmap(_off_ic, banner=b"Linux version 6.12.0\x00")
kd_off_old = _kdir_bz(
    "kallsyms-banner-no-builtin", _off_ic, ks_off_old_map,
    devprofile.wrap_hdrs_bzimage(gzip.compress(ks_off_old)),
)
try:
    devprofile.check_kconfig(amd, kd_off_old)
    check("check-kconfig refuses Linux version 6.12.0 when NVMe and MEI are both off", False)
except devprofile.ProfileError as e:
    check(
        f"check-kconfig refuses Linux version 6.12.0 when NVMe and MEI are both off ({e})",
        "6.18" in str(e) and "android17" in str(e) and "release family" in str(e),
    )
ks_off_ok, ks_off_ok_map = _build_kallsyms_vmlinux_and_sysmap(_off_ic)
kd_off_ok = _kdir_bz(
    "kallsyms-banner-no-builtin-ok", _off_ic, ks_off_ok_map,
    devprofile.wrap_hdrs_bzimage(gzip.compress(ks_off_ok)),
)
try:
    _off_ok = devprofile.check_kconfig(amd, kd_off_ok)
    check("check-kconfig accepts the pinned release family when NVMe and MEI are both off",
          _off_ok["CONFIG_DRM_AMDGPU"] == "m")
except Exception as e:
    check(f"check-kconfig accepts the pinned release family when NVMe and MEI are both off ({e})", False)


def _break_gzip_crc(blob: bytes) -> bytes:
    out = bytearray(blob)
    out[-8] ^= 0xFF
    return bytes(out)


mal_gz = _break_gzip_crc(gzip.compress(
    devprofile.build_test_vmlinux_elf(GOOD_KCFG + "CONFIG_CMDLINE_OVERRIDE=y\n")
))
benign_gz = gzip.compress(devprofile.build_test_vmlinux_elf(GOOD_KCFG))
d2_image = _point_hdrs_at_append(devprofile.wrap_hdrs_bzimage(mal_gz), benign_gz)
kd_d2 = _kdir_bz("d2-bad-crc", GOOD_KCFG, GOOD_SYSMAP, d2_image)
try:
    devprofile.check_kconfig(amd, kd_d2)
    check("check-kconfig refuses a bad-CRC gzip left in place with HdrS pointed at an appended benign gzip (synthetic, not a bootable kernel)", False)
except devprofile.ProfileError as e:
    check(
        "check-kconfig refuses a bad-CRC gzip left in place with HdrS pointed at an appended benign gzip "
        f"(synthetic, not a bootable kernel) ({e})",
        "more than one plausible payload or IKCFG candidate" in str(e)
        and "scripts/extract-ikconfig" in str(e),
    )


def _xor_tail(blob: bytes, n: int) -> bytes:
    out = bytearray(blob)
    for i in range(1, n + 1):
        out[-i] ^= 0xFF
    return bytes(out)


def _same_format_d2(fmt: str) -> bytes:
    """Bad trailing bytes on a malicious member, benign member of the same format appended, HdrS aimed at the append."""
    mal = devprofile.build_test_vmlinux_elf(GOOD_KCFG + "CONFIG_CMDLINE_OVERRIDE=y\n")
    ben = devprofile.build_test_vmlinux_elf(GOOD_KCFG)
    if fmt == "xz":
        mal_c = _xor_tail(lzma.compress(mal, format=lzma.FORMAT_XZ), 1)
        ben_c = lzma.compress(ben, format=lzma.FORMAT_XZ)
    elif fmt == "bzip2":
        # bunzip2 1.0.8 keeps no stdout when a short member fails its stream
        # CRC before the first 100 KiB output buffer is flushed. Padding the
        # plaintext past that flush makes the tool emit the IKCFG and then
        # fail, which is what extract-ikconfig still reads.
        mal_c = _xor_tail(bz2.compress(mal + (b"\x00" * (100 * 1024))), 1)
        ben_c = bz2.compress(ben)
    elif fmt == "lzma":
        mal_c = _xor_tail(_compress_payload(mal, "lzma"), 3)
        ben_c = _compress_payload(ben, "lzma")
    else:
        raise ValueError(fmt)
    return _point_hdrs_at_append(devprofile.wrap_hdrs_bzimage(mal_c), ben_c)


d2_same = {}
for _fmt in ("xz", "bzip2", "lzma"):
    d2_same[_fmt] = _same_format_d2(_fmt)
    _kd_same = _kdir_bz(f"d2-same-{_fmt}", GOOD_KCFG, GOOD_SYSMAP, d2_same[_fmt])
    try:
        devprofile.check_kconfig(amd, _kd_same)
        check(
            f"check-kconfig refuses a trailing-error {_fmt} member left in place with HdrS pointed at an appended benign {_fmt} (synthetic, not a bootable kernel)",
            False,
        )
    except devprofile.ProfileError as e:
        check(
            f"check-kconfig refuses a trailing-error {_fmt} member left in place with HdrS pointed at an appended benign {_fmt} "
            f"(synthetic, not a bootable kernel) ({e})",
            "more than one plausible payload or IKCFG candidate" in str(e)
            and "scripts/extract-ikconfig" in str(e),
        )


def _ikcfg_at_tail(pad: bytes) -> bytes:
    """Plaintext whose only IKCFG sits at the end, after `pad` (empty pad: no earlier output)."""
    return pad + b"IKCFG_ST" + gzip.compress((GOOD_KCFG + "CONFIG_CMDLINE_OVERRIDE=y\n").encode()) + b"IKCFG_ED"


def _tail_ikcfg_d2(fmt: str, plain: bytes) -> bytes:
    """Same-format D2: corrupt the member that ends in an IKCFG, HdrS aims at a benign append."""
    ben = devprofile.build_test_vmlinux_elf(GOOD_KCFG)
    if fmt == "xz":
        mal_c = _xor_tail(lzma.compress(plain, format=lzma.FORMAT_XZ), 1)
        ben_c = lzma.compress(ben, format=lzma.FORMAT_XZ)
    elif fmt == "lzma":
        mal_c = _xor_tail(_compress_payload(plain, "lzma"), 3)
        ben_c = _compress_payload(ben, "lzma")
    else:
        raise ValueError(fmt)
    return _point_hdrs_at_append(devprofile.wrap_hdrs_bzimage(mal_c), ben_c)


# IKCFG at the member tail. "earlier" is 8 KiB of output before it (the 4096-byte
# drain used to keep that prefix and drop the marker). "none" is a short member:
# the first drain produces nothing and the 64-byte retry drops the marker.
d2_tail = {}
for _fmt in ("xz", "lzma"):
    for _shape, _pad in (("earlier", b"\x00" * 8192), ("none", b"")):
        d2_tail[(_fmt, _shape)] = _tail_ikcfg_d2(_fmt, _ikcfg_at_tail(_pad))
        _kd_tail = _kdir_bz(f"d2-tail-{_fmt}-{_shape}", GOOD_KCFG, GOOD_SYSMAP, d2_tail[(_fmt, _shape)])
        try:
            devprofile.check_kconfig(amd, _kd_tail)
            check(
                f"check-kconfig refuses an {_fmt} member with IKCFG at the tail ({_shape} output before it) and HdrS pointed at an appended benign {_fmt} (synthetic, not a bootable kernel)",
                False,
            )
        except devprofile.ProfileError as e:
            check(
                f"check-kconfig refuses an {_fmt} member with IKCFG at the tail ({_shape} output before it) and HdrS pointed at an appended benign {_fmt} "
                f"(synthetic, not a bootable kernel) ({e})",
                "more than one plausible payload or IKCFG candidate" in str(e)
                and "scripts/extract-ikconfig" in str(e),
            )

elf_span = devprofile.build_test_vmlinux_elf(GOOD_KCFG)
elf_extra = gzip.compress(devprofile.build_test_vmlinux_elf(GOOD_KCFG + "CONFIG_ATA=y\n"))
kd_elf_gz = _kdir_bz(
    "elf-plus-gzip", GOOD_KCFG, GOOD_SYSMAP,
    devprofile.wrap_hdrs_bzimage(elf_span + elf_extra),
)
try:
    devprofile.check_kconfig(amd, kd_elf_gz)
    check("check-kconfig refuses an uncompressed ELF payload with a gzip member inside the same payload_length", False)
except devprofile.ProfileError as e:
    check(
        f"check-kconfig refuses an uncompressed ELF payload with a gzip member inside the same payload_length ({e})",
        "trailing bytes" in str(e),
    )

honest_gz = devprofile.wrap_hdrs_bzimage(gzip.compress(devprofile.build_test_vmlinux_elf(GOOD_KCFG)))
raw_bad_crc = honest_gz + b"IKCFG_ST" + _break_gzip_crc(gzip.compress(b"CONFIG_CMDLINE_OVERRIDE=y\n")) + b"IKCFG_ED"
kd_raw_bad = _kdir_bz("raw-badcrc-trailer", GOOD_KCFG, GOOD_SYSMAP, raw_bad_crc)
try:
    _raw_ok = devprofile.check_kconfig(amd, kd_raw_bad)
    check(
        "check-kconfig accepts an honest gzip when a later raw IKCFG gzip has a bad CRC (dump_config is strict; synthetic, not a bootable kernel)",
        _raw_ok["CONFIG_DRM_AMDGPU"] == "m",
    )
except devprofile.ProfileError as e:
    check(
        "check-kconfig accepts an honest gzip when a later raw IKCFG gzip has a bad CRC "
        f"(dump_config is strict; synthetic, not a bootable kernel) ({e})",
        False,
    )
devprofile._hidden_decompressors.add("lzo")
try:
    kd_lzo_out = _kdir_bz("lzo-magic-outside", GOOD_KCFG, GOOD_SYSMAP, honest_gz + devprofile._LZO_FILE_MAGIC)
    try:
        devprofile.check_kconfig(amd, kd_lzo_out)
        check("check-kconfig refuses an LZO file header outside the HdrS slice when lzop is unavailable", False)
    except devprofile.ProfileError as e:
        check(
            f"check-kconfig refuses an LZO file header outside the HdrS slice when lzop is unavailable ({e})",
            "decompressor unavailable" in str(e),
        )
    kd_lzo_short = _kdir_bz("lzo-three-byte", GOOD_KCFG, GOOD_SYSMAP, honest_gz + b"\x89LZ")
    check("check-kconfig does not treat a 3-byte LZO magic as a second payload when lzop is unavailable",
          devprofile.check_kconfig(amd, kd_lzo_short)["CONFIG_DRM_AMDGPU"] == "m")
finally:
    devprofile._hidden_decompressors.discard("lzo")

devprofile._hidden_decompressors.add("zstd")
try:
    kd_zstd_magic = _kdir_bz("zstd-magic-outside", GOOD_KCFG, GOOD_SYSMAP, honest_gz + b"\x28\xb5\x2f\xfd")
    try:
        devprofile.check_kconfig(amd, kd_zstd_magic)
        check("check-kconfig refuses a zstd magic outside the HdrS slice when the decompressor is hidden", False)
    except devprofile.ProfileError as e:
        check(
            f"check-kconfig refuses a zstd magic outside the HdrS slice when the decompressor is hidden ({e})",
            "decompressor unavailable" in str(e),
        )
finally:
    devprofile._hidden_decompressors.discard("zstd")

harmless_zstd = subprocess.run(["zstd", "-q", "-c"], input=b"harmless-second-frame", capture_output=True, check=True).stdout
two_frames = zstd_frame + harmless_zstd
kd_two_zstd = _kdir_bz("zstd-two-frames", GOOD_KCFG, GOOD_SYSMAP, devprofile.wrap_hdrs_bzimage(two_frames))
saved_zstd = devprofile._zstd
devprofile._zstd = None
try:
    try:
        devprofile.check_kconfig(amd, kd_two_zstd)
        check("check-kconfig refuses two zstd frames in one HdrS slice on the CLI path", False)
    except devprofile.ProfileError as e:
        check(f"check-kconfig refuses two zstd frames in one HdrS slice on the CLI path ({e})",
              "corrupt or trailing-garbage zstd payload" in str(e))
    check("check-kconfig still accepts a zstd frame plus a 4-byte trailer with the CLI forced",
          devprofile.check_kconfig(amd, kd_zstd_pad)["CONFIG_DRM_AMDGPU"] == "m")
    try:
        devprofile.check_kconfig(amd, kd_zstd_junk)
        check("check-kconfig still refuses 8 trailing zstd bytes with the CLI forced", False)
    except devprofile.ProfileError as e:
        check(f"check-kconfig still refuses 8 trailing zstd bytes with the CLI forced ({e})",
              "corrupt or trailing-garbage zstd payload" in str(e))
finally:
    devprofile._zstd = saved_zstd


class _FakeZstd:
    class ZstdDecompressor:
        def decompress(self, data, max_length=None):
            end = devprofile._zstd_first_frame_size(data)
            if end is None:
                raise RuntimeError("not a zstd frame")
            self.eof = True
            self.unused_data = data[end:]
            proc = subprocess.run(["zstd", "-dc"], input=data[:end], capture_output=True, check=True)
            return proc.stdout


devprofile._zstd = _FakeZstd
try:
    try:
        devprofile.check_kconfig(amd, kd_two_zstd)
        check("check-kconfig refuses two zstd frames in one HdrS slice on the Python unused_data path", False)
    except devprofile.ProfileError as e:
        check(
            f"check-kconfig refuses two zstd frames in one HdrS slice on the Python unused_data path ({e})",
            "corrupt or trailing-garbage zstd payload" in str(e),
        )
    check("check-kconfig accepts a zstd frame plus a 4-byte trailer on the Python unused_data path",
          devprofile.check_kconfig(amd, kd_zstd_pad)["CONFIG_DRM_AMDGPU"] == "m")
finally:
    devprofile._zstd = saved_zstd

check("x86_cmdline_fits allows 2047 visible bytes with an empty builtin",
      devprofile.x86_cmdline_fits(2047, "") is True)
check("x86_cmdline_fits rejects 2048 visible bytes with an empty builtin",
      devprofile.x86_cmdline_fits(2048, "") is False)
check("x86_cmdline_fits rejects 2000 bootloader bytes plus a 48-character builtin",
      devprofile.x86_cmdline_fits(2000, "c" * 48) is False)
check("x86_cmdline_fits allows a builtin whose combined length is 2047",
      devprofile.x86_cmdline_fits(1998, "c" * 48) is True)
check("x86_cmdline_fits rejects a builtin whose combined length is 2048",
      devprofile.x86_cmdline_fits(1999, "c" * 48) is False)
check("builtin_cmdline_text is empty unless CONFIG_CMDLINE_BOOL=y",
      devprofile.builtin_cmdline_text({}) == ""
      and devprofile.builtin_cmdline_text({
          "CONFIG_CMDLINE_BOOL": "y",
          "CONFIG_CMDLINE": '"console=ttynull"',
      }) == "console=ttynull")
same_raw = b"bzImage-bytes-for-sha"
same_digest = hashlib.sha256(same_raw).hexdigest()
try:
    devprofile.require_same_bzimage(same_raw, same_digest, "kernel.efi is not the bzImage check-kconfig verified")
    check("require_same_bzimage accepts a matching SHA-256", True)
except devprofile.ProfileError as e:
    check(f"require_same_bzimage accepts a matching SHA-256 ({e})", False)
try:
    devprofile.require_same_bzimage(same_raw + b"x", same_digest, "kernel.efi is not the bzImage check-kconfig verified")
    check("require_same_bzimage refuses a SHA-256 mismatch for kernel.efi", False)
except devprofile.ProfileError as e:
    check(f"require_same_bzimage refuses a SHA-256 mismatch for kernel.efi ({e})",
          "does not match" in str(e) and "kernel.efi" in str(e))
try:
    devprofile.require_same_bzimage(b"other", same_digest, "/tmp/bzImage changed after check-kconfig read it")
    check("require_same_bzimage refuses a SHA-256 mismatch after check-kconfig", False)
except devprofile.ProfileError as e:
    check(f"require_same_bzimage refuses a SHA-256 mismatch after check-kconfig ({e})",
          "does not match" in str(e) and "changed after check-kconfig read it" in str(e))


def _image_call_names(call):
    return [a.id if isinstance(a, ast.Name) else None for a in call.args[:4]]


def _image_install_checked():
    """The install_checked_kernel function defined in builder/image.py, without running the build.

    The real call is the one directly in `if DEV_KERNEL_DIR:`, and DEV_KERNEL_DIR is
    os.environ.get("GBOS_DEV_KERNEL_DIR"). The third and fourth arguments are the
    serial and quiet lists that were passed, not a copy of one of them.
    """
    tree = ast.parse((ROOT / "builder" / "image.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "install_checked_kernel")
    from_env = False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "DEV_KERNEL_DIR" for t in node.targets):
            continue
        value = node.value
        from_env = (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "get"
            and isinstance(value.func.value, ast.Attribute)
            and value.func.value.attr == "environ"
            and bool(value.args)
            and isinstance(value.args[0], ast.Constant)
            and value.args[0].value == "GBOS_DEV_KERNEL_DIR"
        )
    direct = []
    elsewhere = []

    def _note(stmt, into):
        if isinstance(stmt, ast.FunctionDef):
            return
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == "install_checked_kernel"
        ):
            into.append(stmt.value)
            return
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "install_checked_kernel":
                elsewhere.append(sub)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            continue
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "DEV_KERNEL_DIR":
            for stmt in node.body:
                _note(stmt, direct)
            for stmt in node.orelse:
                _note(stmt, elsewhere)
            continue
        _note(node, elsewhere)
    guard_ok = (
        from_env
        and len(direct) == 1
        and not elsewhere
        and _image_call_names(direct[0]) == ["PROF", "DEV_KERNEL_DIR", "debug", "quiet"]
    )
    ns = {"devprofile": devprofile, "Path": Path, "sys": sys}
    exec(compile(ast.Module([fn], []), "image.py", "exec"), ns)
    return ns["install_checked_kernel"], guard_ok


_install_checked, _install_guard_ok = _image_install_checked()
check(
    "image.py calls install_checked_kernel under GBOS_DEV_KERNEL_DIR with debug and quiet",
    _install_guard_ok,
)
_bz_ok = (kd_ok / "bzImage").read_bytes()
_short = ["efi=noruntime"]
_copied = []
try:
    _installed = _install_checked(
        amd, kd_ok, _short, _short, lambda: _bz_ok, lambda p: _copied.append(p), lambda: _bz_ok,
    )
    check("image.py install_checked_kernel accepts a matching bzImage and a short quiet and serial line",
          _installed["CONFIG_DRM_AMDGPU"] == "m" and len(_copied) == 1)
except (SystemExit, AssertionError, devprofile.ProfileError) as e:
    check(f"image.py install_checked_kernel accepts a matching bzImage and a short quiet and serial line ({e})", False)
_copied_changed = []
try:
    _install_checked(
        amd, kd_ok, _short, _short, lambda: _bz_ok + b"x", lambda p: _copied_changed.append(p), lambda: _bz_ok,
    )
    check("image.py stops when the bzImage changes after check-kconfig", False)
except SystemExit as e:
    check("image.py stops when the bzImage changes after check-kconfig",
          "changed after check-kconfig" in str(e) and _copied_changed == [])
_copied_esp = []
try:
    _install_checked(
        amd, kd_ok, _short, _short, lambda: _bz_ok, lambda p: _copied_esp.append(p), lambda: _bz_ok + b"y",
    )
    check("image.py stops when the ESP kernel.efi readback does not match", False)
except SystemExit as e:
    check("image.py stops when the ESP kernel.efi readback does not match",
          "kernel.efi" in str(e) and len(_copied_esp) == 1)
_builtin = devprofile.builtin_cmdline_text(devprofile.parse_kconfig(GOOD_KCFG))
_prefix = len("initrd=\\initrd.img ")
# n + len(builtin) + 1 >= 2048, with n = len(token) + prefix. One entry is long, the other stays short.
_need = 2048 - _prefix - len(_builtin) - 1
_long = ["a" * _need]
try:
    _install_checked(
        amd, kd_ok, _short, _long, lambda: _bz_ok, lambda p: None, lambda: _bz_ok,
    )
    check("image.py stops when the quiet entry plus builtin CONFIG_CMDLINE does not fit", False)
except AssertionError as e:
    check("image.py stops when the quiet entry plus builtin CONFIG_CMDLINE does not fit",
          "quiet" in str(e) and "COMMAND_LINE_SIZE" in str(e))
try:
    _install_checked(
        amd, kd_ok, _long, _short, lambda: _bz_ok, lambda p: None, lambda: _bz_ok,
    )
    check("image.py stops when the serial entry plus builtin CONFIG_CMDLINE does not fit", False)
except AssertionError as e:
    check("image.py stops when the serial entry plus builtin CONFIG_CMDLINE does not fit",
          "serial" in str(e) and "COMMAND_LINE_SIZE" in str(e))


def _skip(name, why):
    print(f"SKIP {name} ({why})")


# The kernel's scripts/extract-ikconfig is vendored as a test fixture (no network). Its pinned sha256 is of the
# original upstream bytes, kept byte-identical; attribution lives in fixtures/extract-ikconfig.SOURCE.
_IKCFG_FIXTURE = ROOT / "tests" / "builder" / "fixtures" / "extract-ikconfig"
_IKCFG_SHA = "070020a9c5a0b9e6f7e24f5728073e8e62155be49133a9f3f37e8fc19230a9b1"
_GPL20_FIXTURE = ROOT / "tests" / "builder" / "fixtures" / "kernel-LICENSES-preferred-GPL-2.0"
_COPYING_FIXTURE = ROOT / "tests" / "builder" / "fixtures" / "kernel-COPYING"
_GPL20_SHA = "8780e78a1a737e127f25a65f6d95269bffd36158dc261114de7859b490bfc5aa"
_COPYING_SHA = "fb5a425bd3b3cd6071a3a9aff9909a859e7c1158d54d32e07658398cd67eb6a0"


def _gnu_banner(tool):
    """Return the tool's --version banner when it reports GNU, else None."""
    try:
        proc = subprocess.run([tool, "--version"], capture_output=True)
    except OSError:
        return None
    banner = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    # macOS BSD grep prints "grep (BSD grep, GNU compatible) …": matching a bare "GNU" would wrongly accept it, and
    # so would "GNU compatible". Real GNU tools print "(GNU grep)" / "(GNU coreutils)", so require "(GNU ".
    return banner if "(GNU " in banner else None


def _gzip_pipe_keeps_trailing(argv) -> bool:
    """True when argv matches the extract-ikconfig gzip exit-status contract.

    A clean member is exit 0. Several trailing bytes that are not a new gzip
    header are exit 2 and the member is still written (the script keeps status
    other than 1). A bad CRC is exit 1. One stray byte is not the test: GNU
    gzip 1.12 reports that as unexpected EOF, which is a different status.
    """
    clean = gzip.compress(b"probe-bytes")
    bad = bytearray(clean)
    bad[-8] ^= 0xFF
    try:
        ok = subprocess.run(argv, input=clean, capture_output=True)
        trail = subprocess.run(argv, input=clean + b"TRAIL", capture_output=True)
        crc = subprocess.run(argv, input=bytes(bad), capture_output=True)
    except OSError:
        return False
    return (
        ok.returncode == 0 and ok.stdout == b"probe-bytes"
        and trail.returncode == 2 and trail.stdout == b"probe-bytes"
        and crc.returncode == 1
    )


def _ikcfg_missing():
    missing = []
    for tool in ("grep", "tr"):
        if _gnu_banner(tool) is None:
            missing.append(f"{tool} is not GNU")
    if not _gzip_pipe_keeps_trailing(["zcat"]):
        missing.append("zcat cannot gzip-decompress stdin and keep trailing bytes (exit 2)")
    if not _gzip_pipe_keeps_trailing(["gunzip"]):
        missing.append("gunzip cannot gzip-decompress stdin and keep trailing bytes (exit 2)")
    return missing


# The fixture itself is checked on every host; only running it needs GNU tools.
script_bytes = _IKCFG_FIXTURE.read_bytes() if _IKCFG_FIXTURE.is_file() else b""
fixture_ok = hashlib.sha256(script_bytes).hexdigest() == _IKCFG_SHA
check("vendored extract-ikconfig fixture exists and matches the pinned upstream sha256", fixture_ok)
_gpl20_bytes = _GPL20_FIXTURE.read_bytes() if _GPL20_FIXTURE.is_file() else b""
_copying_bytes = _COPYING_FIXTURE.read_bytes() if _COPYING_FIXTURE.is_file() else b""
check(
    "vendored kernel LICENSES/preferred/GPL-2.0 matches the pinned sha256",
    hashlib.sha256(_gpl20_bytes).hexdigest() == _GPL20_SHA,
)
check(
    "vendored kernel COPYING matches the pinned sha256",
    hashlib.sha256(_copying_bytes).hexdigest() == _COPYING_SHA,
)
_missing = _ikcfg_missing()
if _missing:
    _skip("extract-ikconfig differential", "needs GNU grep/…: " + "; ".join(_missing))
elif fixture_ok:
    script_path = tmp / "extract-ikconfig"
    script_path.write_bytes(script_bytes)
    script_path.chmod(0o755)
    honest = devprofile.wrap_hdrs_bzimage(gzip.compress(devprofile.build_test_vmlinux_elf(GOOD_KCFG)))
    honest_path = tmp / "honest-gzip.bzImage"
    honest_path.write_bytes(honest)
    proc = subprocess.run(["bash", str(script_path), str(honest_path)], capture_output=True)
    printed = proc.stdout.decode("utf-8", "replace")
    gate_ok = devprofile.check_kconfig(amd, _kdir_bz("ikcfg-honest", GOOD_KCFG, GOOD_SYSMAP, honest))
    check("extract-ikconfig prints the honest gzip config and the gate accepts it",
          proc.returncode == 0 and "CONFIG_DRM_AMDGPU=m" in printed and gate_ok["CONFIG_DRM_AMDGPU"] == "m")
    d2_path = tmp / "d2.bzImage"
    d2_path.write_bytes(d2_image)
    proc_d2 = subprocess.run(["bash", str(script_path), str(d2_path)], capture_output=True)
    printed_d2 = proc_d2.stdout.decode("utf-8", "replace")
    refused = False
    try:
        devprofile.check_kconfig(amd, kd_d2)
    except devprofile.ProfileError:
        refused = True
    check(
        "extract-ikconfig prints the bad-CRC payload and the gate refuses that image (synthetic, not a bootable kernel)",
        "CONFIG_CMDLINE_OVERRIDE=y" in printed_d2 and refused,
    )
    raw_path = tmp / "raw-badcrc.bzImage"
    raw_path.write_bytes(raw_bad_crc)
    proc_raw = subprocess.run(["bash", str(script_path), str(raw_path)], capture_output=True)
    printed_raw = proc_raw.stdout.decode("utf-8", "replace")
    try:
        raw_gate = devprofile.check_kconfig(amd, kd_raw_bad)
        raw_accepted = raw_gate.get("CONFIG_DRM_AMDGPU") == "m"
    except devprofile.ProfileError:
        raw_accepted = False
    check(
        "extract-ikconfig prints the honest gzip when a later raw IKCFG has a bad CRC, and the gate accepts it",
        proc_raw.returncode == 0 and "CONFIG_DRM_AMDGPU=m" in printed_raw and "CONFIG_CMDLINE_OVERRIDE=y" not in printed_raw
        and raw_accepted,
    )
    for _fmt, _tool in (("xz", "unxz"), ("bzip2", "bunzip2"), ("lzma", "unlzma")):
        if shutil.which(_tool) is None:
            _skip(f"extract-ikconfig {_fmt} D2", f"{_tool} is missing")
            continue
        _path = tmp / f"d2-same-{_fmt}.bzImage"
        _path.write_bytes(d2_same[_fmt])
        _proc = subprocess.run(["bash", str(script_path), str(_path)], capture_output=True)
        _printed = _proc.stdout.decode("utf-8", "replace")
        _refused = False
        try:
            devprofile.check_kconfig(amd, _kdir_bz(f"ikcfg-d2-{_fmt}", GOOD_KCFG, GOOD_SYSMAP, d2_same[_fmt]))
        except devprofile.ProfileError:
            _refused = True
        check(
            f"extract-ikconfig prints the trailing-error {_fmt} payload and the gate refuses that image (synthetic, not a bootable kernel)",
            "CONFIG_CMDLINE_OVERRIDE=y" in _printed and _refused,
        )
    for (_fmt, _shape), _blob in d2_tail.items():
        _tool = {"xz": "unxz", "lzma": "unlzma"}[_fmt]
        if shutil.which(_tool) is None:
            _skip(f"extract-ikconfig {_fmt} tail {_shape}", f"{_tool} is missing")
            continue
        _path = tmp / f"d2-tail-{_fmt}-{_shape}.bzImage"
        _path.write_bytes(_blob)
        _proc = subprocess.run(["bash", str(script_path), str(_path)], capture_output=True)
        _printed = _proc.stdout.decode("utf-8", "replace")
        _refused = False
        try:
            devprofile.check_kconfig(amd, _kdir_bz(f"ikcfg-tail-{_fmt}-{_shape}", GOOD_KCFG, GOOD_SYSMAP, _blob))
        except devprofile.ProfileError:
            _refused = True
        check(
            f"extract-ikconfig prints the {_fmt} IKCFG-at-tail ({_shape}) payload and the gate refuses that image (synthetic, not a bootable kernel)",
            "CONFIG_CMDLINE_OVERRIDE=y" in _printed and _refused,
        )

if (
    (ROOT / "tools/release/private-patterns.txt").is_file()
    and not (ROOT / "tools/release/PUBLIC-EXPORT").exists()
    and os.environ.get("GBOS_PROFILE_SKIP_SUBTEST") != "1"
):
    _skip_dir = tmp / "skip-cases"
    _skip_dir.mkdir(exist_ok=True)
    _sub_env = {**os.environ, "GBOS_PROFILE_SKIP_SUBTEST": "1"}
    _priv = _skip_dir / "priv"
    if (ROOT / ".git").exists():
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(_priv)], check=True)
        shutil.copyfile(ROOT / "tests/builder/profile-test.py", _priv / "tests/builder/profile-test.py")
    else:
        shutil.copytree(ROOT, _priv, symlinks=True)
        subprocess.run(["git", "-C", str(_priv), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(_priv), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(_priv), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(_priv), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(_priv), "commit", "-q", "--allow-empty", "-m", "sync"], check=True)
    _pub = _skip_dir / "pub"
    _exp = subprocess.run(
        ["bash", str(_priv / "tools/release/make-public.sh"), "HEAD", str(_pub)],
        capture_output=True,
        text=True,
        cwd=str(_priv),
    )
    (_priv / "research/amd/lapis-kernel.config").unlink()
    (_priv / "tools/release/PUBLIC-EXPORT").write_bytes(b"")
    _p_empty = subprocess.run(
        [sys.executable, str(_priv / "tests/builder/profile-test.py")],
        capture_output=True,
        text=True,
        env=_sub_env,
    )
    check(
        "private clone without research/amd/lapis-kernel.config + empty PUBLIC-EXPORT marker fails",
        not is_public_export_tree(_priv)
        and _p_empty.returncode == 1
        and "FAIL research/amd/lapis-kernel.config is present in this checkout" in _p_empty.stdout
        and "profile tests:" not in _p_empty.stdout,
    )
    (_priv / "tools/release/PUBLIC-EXPORT").write_text("a" * 40 + "\n", encoding="utf-8")
    _p_tracked = subprocess.run(
        [sys.executable, str(_priv / "tests/builder/profile-test.py")],
        capture_output=True,
        text=True,
        env=_sub_env,
    )
    check(
        "private clone without research/amd/lapis-kernel.config + well-formed PUBLIC-EXPORT marker but research still tracked fails",
        not is_public_export_tree(_priv)
        and _p_tracked.returncode == 1
        and "FAIL research/amd/lapis-kernel.config is present in this checkout" in _p_tracked.stdout
        and "profile tests:" not in _p_tracked.stdout,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(_priv),
            "rm",
            "-rfq",
            "--ignore-unmatch",
            "--",
            "research",
            "tools/release/private-patterns.txt",
            "tools/harness/kano-env.local.sh",
        ],
        check=True,
    )
    shutil.rmtree(_priv / "research", ignore_errors=True)
    (_priv / "tools/release/PUBLIC-EXPORT").write_text("a" * 40 + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(_priv), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(_priv), "commit", "-q", "-m", "drop excludes"], check=True)
    _p_untracked_excl = subprocess.run(
        [sys.executable, str(_priv / "tests/builder/profile-test.py")],
        capture_output=True,
        text=True,
        env=_sub_env,
    )
    check(
        "private clone with excluded paths removed from index/worktree + well-formed PUBLIC-EXPORT marker still fails (private history present)",
        not is_public_export_tree(_priv)
        and _p_untracked_excl.returncode == 1
        and "FAIL research/amd/lapis-kernel.config is present in this checkout" in _p_untracked_excl.stdout
        and "profile sub-run: 3 passed, 1 failed" in _p_untracked_excl.stdout
        and "profile tests:" not in _p_untracked_excl.stdout,
    )
    subprocess.run(["git", "-C", str(_pub), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(_pub), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(_pub), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(_pub), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(_pub), "commit", "-q", "-m", "export"], check=True)
    _p_pub = subprocess.run(
        [sys.executable, str(_pub / "tests/builder/profile-test.py")],
        capture_output=True,
        text=True,
        env=_sub_env,
    )
    check(
        "real make-public.sh export + git init + commit skips research/amd/lapis-kernel.config comparison",
        _exp.returncode == 0
        and is_public_export_tree(_pub)
        and _p_pub.returncode == 0
        and "SKIP research/amd/lapis-kernel.config comparison" in _p_pub.stdout
        and "profile sub-run: 3 passed, 0 failed" in _p_pub.stdout
        and "profile tests:" not in _p_pub.stdout,
    )
import gralloc


def _place_like_vendor(tree, vmap, calls):
    def place(rel, src, like, mode):
        calls.append((rel, like, mode))
        if (tree / rel).exists() or like not in vmap:
            raise SystemExit("amd-hx370: unexpected vendor layout at " + rel)
        shutil.copyfile(src, tree / rel)
        (tree / rel).chmod(mode)
        meta = dict(vmap[like])
        meta["mode"] = mode
        vmap[rel] = meta
    return place


def _stock_tree(label, body=b"stock-gralloc"):
    tree = tmp / "gralloc" / label
    if tree.exists():
        shutil.rmtree(tree)
    (tree / "lib64").mkdir(parents=True)
    (tree / "lib64" / "libminigbm_gralloc.so").write_bytes(body)
    vmap = {"lib64/libminigbm_gralloc.so": {
        "mode": 0o644, "uid": 0, "gid": 0, "caps": None,
        "selinux": "u:object_r:same_process_hal_file:s0",
    }}
    return tree, vmap


def gralloc_errors(mod):
    """Live checks for one gralloc module. Empty means every check held."""
    errors = []

    def need(label, cond):
        if not cond:
            errors.append(label)

    rel = "lib64/libminigbm_gralloc.so"
    sel = "u:object_r:same_process_hal_file:s0"
    try:
        blob = b"drop-in-gralloc-bytes"
        digest = hashlib.sha256(blob).hexdigest()
        src = tmp / "gralloc" / "good.so"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(blob)
        tree, vmap = _stock_tree("happy", b"stock-gralloc")
        before = dict(vmap[rel])
        calls = []
        prof = {"gralloc": {"sha256": digest}}
        installed = mod.install_gralloc(prof, tree, vmap, _place_like_vendor(tree, vmap, calls),
                                        {"GBOS_MINIGBM": str(src)})
        got = (tree / rel).read_bytes()
        meta = vmap[rel]
        need("install returns true", installed is True)
        need("path is lib64/libminigbm_gralloc.so", calls == [(rel, rel, 0o644)])
        need("bytes replaced", got == blob)
        need("mode 0644", meta["mode"] == 0o644)
        need("file_contexts selinux kept", meta["selinux"] == sel == before["selinux"])
        need("uid gid caps kept", (meta["uid"], meta["gid"], meta["caps"]) == (0, 0, None))

        tree_m, vmap_m = _stock_tree("missing", b"stock-gralloc")
        missing_msg = ""
        try:
            mod.install_gralloc(prof, tree_m, vmap_m, _place_like_vendor(tree_m, vmap_m, []), {})
        except SystemExit as e:
            missing_msg = str(e)
        need("missing GBOS_MINIGBM refused", "needs libminigbm_gralloc.so" in missing_msg)
        need("missing input left the stock bytes", (tree_m / rel).read_bytes() == b"stock-gralloc")

        bad = tmp / "gralloc" / "bad.so"
        bad.write_bytes(b"not-the-pinned-library")
        tree_w, vmap_w = _stock_tree("wrong", b"stock-gralloc")
        wrong_msg = ""
        try:
            mod.install_gralloc(amd, tree_w, vmap_w, _place_like_vendor(tree_w, vmap_w, []),
                                {"GBOS_MINIGBM": str(bad)})
        except SystemExit as e:
            wrong_msg = str(e)
        need("wrong sha refused against the profile pin",
             "differs from profile gralloc.sha256" in wrong_msg and GRALLOC_SHA in wrong_msg)
        need("wrong sha left the stock bytes", (tree_w / rel).read_bytes() == b"stock-gralloc")

        tree_s, vmap_s = _stock_tree("absent-stock", b"stock-gralloc")
        (tree_s / rel).unlink()
        absent_msg = ""
        try:
            mod.install_gralloc(prof, tree_s, vmap_s, _place_like_vendor(tree_s, vmap_s, []),
                                {"GBOS_MINIGBM": str(src)})
        except SystemExit as e:
            absent_msg = str(e)
        need("missing stock file refused", "not in the vendor tree" in absent_msg)

        link_tree, link_vmap = _stock_tree("symlink", b"stock-gralloc")
        (link_tree / "lib64" / "real.so").write_bytes(b"real")
        (link_tree / rel).unlink()
        (link_tree / rel).symlink_to("real.so")
        link_msg = ""
        try:
            mod.install_gralloc(prof, link_tree, link_vmap, _place_like_vendor(link_tree, link_vmap, []),
                                {"GBOS_MINIGBM": str(src)})
        except SystemExit as e:
            link_msg = str(e)
        need("symlink stock refused", "not in the vendor tree" in link_msg)
        need("symlink target untouched", (link_tree / "lib64" / "real.so").read_bytes() == b"real")

        for label, other in (("kano", kano), ("generic-x86", gx)):
            tree_o, vmap_o = _stock_tree(label, b"stock-gralloc-" + label.encode())
            snap = dict(vmap_o[rel])
            stock = (tree_o / rel).read_bytes()
            returned = mod.install_gralloc(other, tree_o, vmap_o, _place_like_vendor(tree_o, vmap_o, []),
                                           {"GBOS_MINIGBM": str(src)})
            need(f"{label} install is a no-op", returned is False)
            need(f"{label} bytes stay byte-identical", (tree_o / rel).read_bytes() == stock)
            need(f"{label} vmap stays byte-identical", vmap_o[rel] == snap)
            base = ["build.prop", "etc/kano-logcat.sh"]
            need(f"{label} vendor diff omits gralloc", mod.changed_paths(other, base) == base)

        need("amd-hx370 vendor diff includes the replaced gralloc",
             mod.changed_paths(amd, ["build.prop"]) == ["build.prop", rel])

        link_src = tmp / "gralloc" / "input-link.so"
        if link_src.is_symlink() or link_src.exists():
            link_src.unlink()
        link_src.symlink_to(src)
        tree_link, vmap_link = _stock_tree("source-symlink", b"stock-gralloc")
        link_msg = ""
        try:
            mod.install_gralloc(prof, tree_link, vmap_link, _place_like_vendor(tree_link, vmap_link, []),
                                {"GBOS_MINIGBM": str(link_src)})
        except SystemExit as e:
            link_msg = str(e)
        need("symlink source refused", "symlink" in link_msg)
        need("symlink source left the stock bytes", (tree_link / rel).read_bytes() == b"stock-gralloc")

        tree_mode, vmap_mode = _stock_tree("mode755", b"stock-gralloc")
        vmap_mode[rel]["mode"] = 0o755
        mode_msg = ""
        try:
            mod.install_gralloc(prof, tree_mode, vmap_mode, _place_like_vendor(tree_mode, vmap_mode, []),
                                {"GBOS_MINIGBM": str(src)})
        except SystemExit as e:
            mode_msg = str(e)
        need("stock mode 0755 refused", "not mode 0644" in mode_msg)
        need("stock mode 0755 left the stock bytes", (tree_mode / rel).read_bytes() == b"stock-gralloc")

        def place_drift(rel_d, src_d, like, mode):
            if (tree_d / rel_d).exists() or like not in vmap_d:
                raise SystemExit("amd-hx370: unexpected vendor layout at " + rel_d)
            shutil.copyfile(src_d, tree_d / rel_d)
            (tree_d / rel_d).chmod(mode)
            meta_d = dict(vmap_d[like])
            meta_d["mode"] = mode
            meta_d["selinux"] = "u:object_r:vendor_file:s0"
            vmap_d[rel_d] = meta_d

        tree_d, vmap_d = _stock_tree("drift", b"stock-gralloc")
        drift_msg = ""
        try:
            mod.install_gralloc(prof, tree_d, vmap_d, place_drift, {"GBOS_MINIGBM": str(src)})
        except SystemExit as e:
            drift_msg = str(e)
        need("place() label drift refused", "file_contexts drifted" in drift_msg)

        def place_other(rel_o, src_o, like, mode):
            if like not in vmap_o:
                raise SystemExit("amd-hx370: unexpected vendor layout at " + rel_o)
            (tree_o / rel_o).write_bytes(b"placed-other-bytes")
            (tree_o / rel_o).chmod(mode)
            meta_o = dict(vmap_o[like])
            meta_o["mode"] = mode
            vmap_o[rel_o] = meta_o

        tree_o, vmap_o = _stock_tree("place-other", b"stock-gralloc")
        other_msg = ""
        try:
            mod.install_gralloc(prof, tree_o, vmap_o, place_other, {"GBOS_MINIGBM": str(src)})
        except SystemExit as e:
            other_msg = str(e)
        need("place() cannot replace the hashed bytes",
             other_msg == "" and (tree_o / rel).read_bytes() == blob)

        original_copy = shutil.copyfile

        def rewriting_copy(source_path, dest_path, **kwargs):
            src.write_bytes(b"changed-after-hash")
            return original_copy(source_path, dest_path, **kwargs)

        src.write_bytes(blob)
        tree_r, vmap_r = _stock_tree("rewrite", b"stock-gralloc")
        rewrite_msg = ""
        try:
            shutil.copyfile = rewriting_copy
            returned = mod.install_gralloc(prof, tree_r, vmap_r, _place_like_vendor(tree_r, vmap_r, []),
                                           {"GBOS_MINIGBM": str(src)})
        except SystemExit as e:
            returned = False
            rewrite_msg = str(e)
        finally:
            shutil.copyfile = original_copy
            src.write_bytes(blob)
        need("source rewrite between check and copy keeps the hashed bytes",
             returned is True and rewrite_msg == "" and (tree_r / rel).read_bytes() == blob)

        bad_target = tmp / "gralloc" / "bad-target.so"
        bad_target.write_bytes(b"changed-after-hash")
        race_file = tmp / "gralloc" / "race-file.so"
        race_file.write_bytes(blob)

        def retarget_copy(source_path, dest_path, **kwargs):
            race_file.unlink()
            race_file.symlink_to(bad_target)
            return original_copy(source_path, dest_path, **kwargs)

        tree_t, vmap_t = _stock_tree("retarget", b"stock-gralloc")
        retarget_msg = ""
        try:
            shutil.copyfile = retarget_copy
            returned_t = mod.install_gralloc(
                prof, tree_t, vmap_t, _place_like_vendor(tree_t, vmap_t, []),
                {"GBOS_MINIGBM": str(race_file)})
        except SystemExit as e:
            returned_t = False
            retarget_msg = str(e)
        finally:
            shutil.copyfile = original_copy
            if race_file.is_symlink() or race_file.exists():
                race_file.unlink()
        need("symlink re-point between check and copy keeps the hashed bytes",
             returned_t is True and retarget_msg == "" and (tree_t / rel).read_bytes() == blob)
    except (Exception, SystemExit) as e:
        errors.append(f"exception {type(e).__name__}: {e}")
    return errors


live_gralloc_errors = gralloc_errors(gralloc)
check("gralloc install path, mode 0644, file_contexts, sha refusal, kano/generic untouched",
      not live_gralloc_errors)
if live_gralloc_errors:
    print("gralloc errors:", live_gralloc_errors)

_vsh = (ROOT / "builder/vendor.sh").read_text()
_bsh = (ROOT / "builder/build.sh").read_text()
check("vendor.sh installs with place() and counts the file in the vendor diff",
      "gralloc.install_gralloc(prof, tree, vmap, place)" in _vsh
      and "gralloc.changed_paths(prof, expect_changed)" in _vsh)
def _build_sh_gralloc_preflight(text):
    amd = text.find('if [ "$MODEL" = amd-hx370 ]; then')
    link = text.find('[ ! -L "$GBOS_MINIGBM" ] || die "GBOS_MINIGBM is a symlink: $GBOS_MINIGBM"')
    need = text.find("needs libminigbm_gralloc.so")
    differs = text.find("differs from profile gralloc.sha256")
    after = text.find("for c in python3 mcopy")
    return 0 <= amd < link < need < differs < after


check("build.sh refuses a missing, symlink, or wrong-sha GBOS_MINIGBM inside the amd-hx370 preflight",
      _build_sh_gralloc_preflight(_bsh))
_bsh_nosym = _bsh.replace(
    '[ ! -L "$GBOS_MINIGBM" ] || die "GBOS_MINIGBM is a symlink: $GBOS_MINIGBM"\n', "", 1)
if _build_sh_gralloc_preflight(_bsh_nosym):
    check("mutation still green: build.sh symlink guard removed", False)
else:
    print("RED build.sh symlink guard removed")
    check("mutation shown red: build.sh symlink guard removed", True)


def _load_mutant(name, old, new):
    src = (ROOT / "builder/lib/gralloc.py").read_text()
    if src.count(old) != 1:
        check(f"mutation anchor once: {name}", False)
        return None
    path = tmp / "gralloc-mutants" / (name.replace(" ", "_") + ".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src.replace(old, new, 1))
    spec = importlib.util.spec_from_file_location("gralloc_mut_" + name.replace(" ", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MUTATIONS = (
    ("sha compare removed",
     "if got != want:\n        raise SystemExit(\n"
     "            f\"libminigbm_gralloc.so sha256 {got} differs from profile gralloc.sha256 {want}\")",
     "if False and got != want:\n        raise SystemExit(\n"
     "            f\"libminigbm_gralloc.so sha256 {got} differs from profile gralloc.sha256 {want}\")"),
    ("profile gate removed",
     "if not g:\n        return False",
     "if False and not g:\n        return False"),
    ("install path changed",
     'REL = "lib64/libminigbm_gralloc.so"',
     'REL = "lib64/hw/libminigbm_gralloc.so"'),
    ("mode changed to 0755",
     "MODE = 0o644",
     "MODE = 0o755"),
    ("unlink skipped",
     "        (tree / rel).unlink()\n",
     ""),
    ("file_contexts label overwritten",
     "        place(rel, Path(tmp_name), rel, MODE)\n",
     "        place(rel, Path(tmp_name), rel, MODE)\n"
     "        vmap[rel][\"selinux\"] = \"u:object_r:vendor_file:s0\"\n"),
    ("copies the live source",
     "        place(rel, Path(tmp_name), rel, MODE)\n"
     "        os.replace(tmp_name, tree / rel)\n"
     "        tmp_name = None\n",
     "        place(rel, Path(src), rel, MODE)\n"),
    ("os.replace removed",
     "        os.replace(tmp_name, tree / rel)\n"
     "        tmp_name = None\n",
     ""),
    ("symlink source accepted",
     "    if not src or src_path.is_symlink() or not src_path.is_file():\n"
     "        if src and src_path.is_symlink():\n"
     "            raise SystemExit(\"GBOS_MINIGBM is a symlink: \" + src)\n"
     "        raise SystemExit(\n",
     "    if not src or not src_path.is_file():\n"
     "        raise SystemExit(\n"),
    ("stock mode check removed",
     "    if vmap[rel][\"mode\"] != 0o644:\n"
     "        raise SystemExit(\"stock libminigbm_gralloc.so is not mode 0644\")\n",
     ""),
    ("file_contexts drift check removed",
     "    if meta[\"mode\"] != MODE or any(meta[k] != kept[k] for k in kept):\n"
     "        raise SystemExit(\"libminigbm_gralloc.so file_contexts drifted\")\n",
     ""),
    ("changed_paths ignores the profile gate",
     "if not prof.get(\"gralloc\"):\n        return expect_changed",
     "if False and not prof.get(\"gralloc\"):\n        return expect_changed"),
    ("changed_paths drops the gralloc path",
     "return sorted(set(list(expect_changed) + [REL]))",
     "return list(expect_changed)"),
)
for _name, _old, _new in _MUTATIONS:
    _mod = _load_mutant(_name, _old, _new)
    if _mod is None:
        continue
    _errs = gralloc_errors(_mod)
    if _errs:
        print(f"RED {_name}")
        check(f"mutation shown red: {_name}", True)
    else:
        check(f"mutation still green: {_name}", False)

import ast
import textwrap

def _readback_live(text):
    live = False
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if isinstance(test, ast.Constant) and test.value is False:
            continue
        if isinstance(test, ast.BoolOp) and any(
                isinstance(v, ast.Constant) and v.value is False for v in test.values):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                func = stmt.value.func
                if isinstance(func, ast.Name) and func.id == "readback_gralloc":
                    live = True
    return live


_vsh_text = (ROOT / "builder/vendor.sh").read_text(encoding="utf-8")
_install_at = _vsh_text.find("    import gralloc\n    if gralloc.install_gralloc(prof, tree, vmap, place):")
_install_end = _vsh_text.find('print("amd-hx370: vulkan.radeon.so"', _install_at)
_install_slice = textwrap.dedent(_vsh_text[_install_at:_install_end])
_pinned = b"drop-in-gralloc-bytes"
_pinned_sha = hashlib.sha256(_pinned).hexdigest()
_pinned_path = tmp / "gralloc" / "slice-src.so"
_pinned_path.parent.mkdir(parents=True, exist_ok=True)
_pinned_path.write_bytes(_pinned)
_tree_i, _vmap_i = _stock_tree("vendor-slice", b"stock-gralloc")
(_tree_i / "etc/init").mkdir(parents=True)
(_tree_i / "etc/init/fwupd.rc").write_bytes(b"fwupd\n")
_vmap_i["etc/init/fwupd.rc"] = {
    "mode": 0o644, "uid": 0, "gid": 0, "caps": None,
    "selinux": "u:object_r:vendor_configs_file:s0",
}
_calls_i = []
_ns = {
    "gralloc": gralloc,
    "prof": {"gralloc": {"sha256": _pinned_sha}},
    "tree": _tree_i,
    "vmap": _vmap_i,
    "place": _place_like_vendor(_tree_i, _vmap_i, _calls_i),
    "root": tmp / "notices-root",
    "hashlib": hashlib,
    "print": lambda *a, **k: None,
}
(_ns["root"] / "tools/minigbm").mkdir(parents=True)
(_ns["root"] / "tools/minigbm/NOTICES.txt").write_text("minigbm notices\n", encoding="utf-8")
_saved_minigbm = os.environ.get("GBOS_MINIGBM")
os.environ["GBOS_MINIGBM"] = str(_pinned_path)
_slice_msg = ""
try:
    exec(_install_slice, _ns)
except SystemExit as e:
    _slice_msg = str(e)
finally:
    if _saved_minigbm is None:
        os.environ.pop("GBOS_MINIGBM", None)
    else:
        os.environ["GBOS_MINIGBM"] = _saved_minigbm
check("vendor.sh install slice places the pinned bytes and the notices file",
      _slice_msg == ""
      and (_tree_i / "lib64/libminigbm_gralloc.so").read_bytes() == _pinned
      and (_tree_i / "etc/NOTICES-amd-hx370-minigbm.txt").read_text(encoding="utf-8") == "minigbm notices\n"
      and any(c[0] == "etc/NOTICES-amd-hx370-minigbm.txt" for c in _calls_i))
_false_slice = _install_slice.replace(
    "if gralloc.install_gralloc(prof, tree, vmap, place):",
    "if False and gralloc.install_gralloc(prof, tree, vmap, place):",
    1)
_tree_f, _vmap_f = _stock_tree("vendor-slice-false", b"stock-gralloc")
(_tree_f / "etc").mkdir(parents=True)
_vmap_f["etc/init/fwupd.rc"] = _vmap_i["etc/init/fwupd.rc"]
_ns_f = dict(_ns)
_ns_f["tree"] = _tree_f
_ns_f["vmap"] = _vmap_f
_ns_f["place"] = _place_like_vendor(_tree_f, _vmap_f, [])
os.environ["GBOS_MINIGBM"] = str(_pinned_path)
try:
    exec(_false_slice, _ns_f)
finally:
    if _saved_minigbm is None:
        os.environ.pop("GBOS_MINIGBM", None)
    else:
        os.environ["GBOS_MINIGBM"] = _saved_minigbm
check("mutation shown red: vendor install call skipped",
      (_tree_f / "lib64/libminigbm_gralloc.so").read_bytes() == b"stock-gralloc")

_post_at = _vsh_text.find("if prof.get(\"gralloc\"):\n    expect_new = sorted(set(expect_new + [\"etc/NOTICES-amd-hx370-minigbm.txt\"]))")
_post_end = _vsh_text.find("if only_old != expect_old", _post_at)
_post_ns = {
    "prof": {"gralloc": {"sha256": GRALLOC_SHA}},
    "expect_new": ["bin/radv-probe"],
    "expect_changed": [gralloc.REL],
    "gralloc": gralloc,
    "new": {"lib64/libminigbm_gralloc.so": ("file", GRALLOC_SHA)},
    "old": {"lib64/libminigbm_gralloc.so": ("file", "stock")},
}
exec(_vsh_text[_post_at:_post_end], _post_ns)
check("vendor readback slice accepts the pinned gralloc",
      "etc/NOTICES-amd-hx370-minigbm.txt" in _post_ns["expect_new"]
      and gralloc.REL in _post_ns["expect_changed"])
_post_same = {
    "prof": {"gralloc": {"sha256": GRALLOC_SHA}},
    "expect_new": [],
    "expect_changed": [gralloc.REL, "build.prop"],
    "gralloc": gralloc,
    "new": {"lib64/libminigbm_gralloc.so": ("file", GRALLOC_SHA)},
    "old": {"lib64/libminigbm_gralloc.so": ("file", GRALLOC_SHA)},
}
exec(_vsh_text[_post_at:_post_end], _post_same)
check("vendor diff drops gralloc when the pin is already the base image",
      _post_same["expect_changed"] == ["build.prop"])
_post_bad = dict(_post_ns)
_post_bad["new"] = {"lib64/libminigbm_gralloc.so": ("file", "0" * 64)}
_post_bad_msg = ""
try:
    exec(_vsh_text[_post_at:_post_end], _post_bad)
except SystemExit as e:
    _post_bad_msg = str(e)
check("vendor readback slice refuses a different gralloc sha",
      "rebuilt vendor lib64/libminigbm_gralloc.so" in _post_bad_msg)
_post_skip = _vsh_text[_post_at:_post_end].replace(
    'if prof.get("gralloc"):', 'if False and prof.get("gralloc"):', 1)
_post_skip_msg = ""
try:
    exec(_post_skip, {
        "prof": {"gralloc": {"sha256": GRALLOC_SHA}},
        "expect_new": [],
        "gralloc": gralloc,
        "new": {"lib64/libminigbm_gralloc.so": ("file", "0" * 64)},
    })
except SystemExit as e:
    _post_skip_msg = str(e)
check("mutation shown red: vendor readback gate skipped", _post_skip_msg == "")

_image = (ROOT / "builder/image.py").read_text(encoding="utf-8")
check("image.py calls readback_gralloc from a live branch", _readback_live(_image))
check("mutation shown red: image readback hidden behind False",
      not _readback_live(_image.replace(
          'if PROF.get("gralloc"):\n    readback_gralloc(',
          'if False and PROF.get("gralloc"):\n    readback_gralloc(',
          1)))
check("image readback_mismatch rejects a different blob",
      gralloc.readback_mismatch(b"other", _pinned_sha) is not None)
check("image readback_mismatch accepts the pinned blob",
      gralloc.readback_mismatch(_pinned, _pinned_sha) is None)


def _load_readback(source):
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "readback_gralloc")
    ns = {"Path": Path, "shutil": shutil, "sys": sys, "os": os}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "image-readback", "exec"), ns)
    return ns["readback_gralloc"]


def _readback_cases(fn):
    """Empty means accept, refuse, and cleanup all held."""
    errors = []
    work = tmp / "readback-live"

    def boom(img, w):
        Path(w).mkdir(parents=True)
        (Path(w) / "marker").write_bytes(b"z")
        raise RuntimeError("extract failed")

    try:
        fn("img", {"gralloc": {"sha256": _pinned_sha}}, work, boom)
        errors.append("extract exception not raised")
    except RuntimeError:
        if work.exists():
            errors.append("work dir left after extract failure")
    except SystemExit as e:
        errors.append("extract failure became SystemExit: " + str(e))

    def bad(img, w):
        Path(w).mkdir(parents=True)
        (Path(w) / "marker").write_bytes(b"z")
        return b"other-bytes"

    try:
        fn("img", {"gralloc": {"sha256": _pinned_sha}}, work, bad)
        errors.append("bad bytes accepted")
    except SystemExit as e:
        if "differs" not in str(e):
            errors.append("bad bytes: " + str(e))
        if work.exists():
            errors.append("work dir left after refusal")

    def good(img, w):
        Path(w).mkdir(parents=True)
        (Path(w) / "marker").write_bytes(b"z")
        return _pinned

    try:
        got = fn("img", {"gralloc": {"sha256": _pinned_sha}}, work, good)
    except SystemExit as e:
        errors.append("pinned bytes refused: " + str(e))
        got = None
    if got != _pinned_sha:
        errors.append("return " + repr(got))
    if work.exists():
        errors.append("work dir left after accept")
    called = []

    def no(img, w):
        called.append(w)
        return b"x"

    got = fn("img", {}, work, no)
    if got is not None or called:
        errors.append("missing gralloc table still extracted")
    return errors


_rb = _load_readback(_image)
_rb_errs = _readback_cases(_rb)
if _rb_errs:
    print("readback errors:", _rb_errs)
check("readback_gralloc refuses a different blob, accepts the pin, and removes the work dir", not _rb_errs)
for _rname, _rold, _rnew in (
    ("image readback refusal skipped",
     "    if err:\n        sys.exit(err)\n",
     "    if False and err:\n        sys.exit(err)\n"),
    ("image readback mismatch dropped",
     '        err = gralloc.readback_mismatch(blob, g["sha256"])\n',
     "        err = None\n"),
):
    if _image.count(_rold) != 1:
        check(f"mutation anchor once: {_rname}", False)
        continue
    _rfn = _load_readback(_image.replace(_rold, _rnew, 1))
    if _readback_cases(_rfn):
        print(f"RED {_rname}")
        check(f"mutation shown red: {_rname}", True)
    else:
        check(f"mutation still green: {_rname}", False)


_notices = (ROOT / "tools/minigbm/NOTICES.txt").read_text(encoding="utf-8")
check("minigbm NOTICES quotes BSD, libdrm MIT, and the patch commits",
      "Copyright (c) 2017 The Chromium OS Authors" in _notices
      and "nor the names of its" in _notices
      and "BSD-3-Clause" in _notices
      and "Precision Insight" in _notices
      and "Copyright 2011 Intel Corporation" in _notices
      and "Copyright 2014 Advanced Micro Devices, Inc." in _notices
      and "c662136" in _notices
      and "9d21b5c" in _notices
      and "0c4f081" in _notices
      and "PRECISION INSIGHT AND/OR ITS SUPPLIERS" in _notices
      and "VA LINUX SYSTEMS AND/OR ITS SUPPLIERS" in _notices
      and "THE COPYRIGHT HOLDER(S) OR AUTHOR(S)" in _notices
      and "Each header then disclaims" not in _notices)


def _bash_array(text, name):
    match = re.search(rf"(?m)^{name}=\((.*?)\n\)", text, re.S)
    if not match:
        return ""
    kept = [line for line in match.group(1).splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(kept)


def _hex_assign(text, name):
    found = re.findall(rf"(?m)^{re.escape(name)}=([0-9a-f]{{64}})$", text)
    return found[0] if len(found) == 1 else ""


def _yv12_helper_body(c_text):
    match = re.search(r"/\* yv12-helper-begin \*/\n(.*)\n/\* yv12-helper-end \*/", c_text, re.S)
    return match.group(1) if match else ""


_YV12_HOST = "unchecked"
_yv12_seq = 0


# Exact helper statement. A comment that merely mentions stride_align * 2 does not match.
_YV12_FORMULA_LINE = "\t\tstride = (stride + (stride_align * 2) - 1) & ~((stride_align * 2) - 1);\n"
# Return codes of the host harness. Each one is the assertion that failed.
_YV12_ASSERTION = {
    1: "luma % 16 == 0",
    2: "ALIGN((luma+1)/2, align) == ALIGN(luma/2, 16)",
    3: "already multiple 512 stays 512 at align 256",
    4: "already multiple 1024 stays 1024 at align 256",
    5: "already multiple 2048 stays 2048 at align 256",
    6: "align 16 boundary leaves 640 unchanged",
    7: "768 at align 256 becomes 1024",
    8: "1280 at align 256 becomes 1536",
    9: "256 at align 256 becomes 512",
    10: "640-wide after ALIGN32 and ALIGN256 is luma 1024 chroma 512",
}
_yv12_cache = {}


def _compile_yv12(body):
    """Compile the helper copied out of amdgpu_linear.c. None means no host cc."""
    global _YV12_HOST, _yv12_seq
    cached = _yv12_cache.get(body)
    if cached is not None:
        _YV12_HOST = "ran"
        return cached
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        _YV12_HOST = "no-cc"
        return None
    _YV12_HOST = "ran"
    _yv12_seq += 1
    work = tmp / f"yv12-{_yv12_seq}"
    work.mkdir()
    src = work / "yv12_harness.c"
    exe = work / "yv12_harness"
    # Inputs include strides already a multiple of 2*align (512, 1024, 2048 at
    # align 256) and the align>16 boundary (align 16 leaves 640). The scan
    # starts from even widths: ALIGN32, then ALIGN(align) when align is 256,
    # then the helper. 640 is not passed to the helper; ALIGN32+ALIGN256 turns
    # it into 768 first.
    src.write_text(
        "#include <stdint.h>\n" + body + "\n"
        "static uint32_t align_up(uint32_t x, uint32_t a)\n"
        "{\n"
        "  if (a <= 1u)\n"
        "    return x;\n"
        "  return (x + a - 1u) & ~(a - 1u);\n"
        "}\n"
        "int main(void) {\n"
        "  uint32_t luma, w, align;\n"
        "  int i;\n"
        "  static const uint32_t aligns[2] = {1u, 256u};\n"
        "  if (amdgpu_linear_yv12_luma(512, 256) != 512) return 3;\n"
        "  if (amdgpu_linear_yv12_luma(1024, 256) != 1024) return 4;\n"
        "  if (amdgpu_linear_yv12_luma(2048, 256) != 2048) return 5;\n"
        "  if (amdgpu_linear_yv12_luma(640, 16) != 640) return 6;\n"
        "  if (amdgpu_linear_yv12_luma(768, 256) != 1024) return 7;\n"
        "  if (amdgpu_linear_yv12_luma(1280, 256) != 1536) return 8;\n"
        "  if (amdgpu_linear_yv12_luma(256, 256) != 512) return 9;\n"
        "  luma = align_up(align_up(640, 32), 256);\n"
        "  luma = amdgpu_linear_yv12_luma(luma, 256);\n"
        "  if (luma != 1024 || luma / 2u != 512) return 10;\n"
        "  for (w = 2; w <= 8192; w += 2) {\n"
        "    for (i = 0; i < 2; i++) {\n"
        "      align = aligns[i];\n"
        "      luma = align_up(w, 32);\n"
        "      if (align > 1u)\n"
        "        luma = align_up(luma, align);\n"
        "      luma = amdgpu_linear_yv12_luma(luma, align);\n"
        "      if ((luma % 16u) != 0u) return 1;\n"
        "      if (align_up((luma + 1u) / 2u, align) != align_up(luma / 2u, 16u)) return 2;\n"
        "    }\n"
        "  }\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    proc = subprocess.run([cc, "-O2", "-Wall", "-o", str(exe), str(src)], capture_output=True, text=True)
    if proc.returncode != 0:
        result = "compile " + (proc.stderr or proc.stdout)[-300:]
    else:
        run = subprocess.run([str(exe)], capture_output=True)
        if run.returncode != 0:
            why = _YV12_ASSERTION.get(run.returncode, "unknown assertion")
            result = "exit %s: %s" % (run.returncode, why)
        else:
            result = ""
    _yv12_cache[body] = result
    return result


def _uniq(items):
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _repin_linear(script, c_text):
    """Point LINEAR_SHA and LIB_FROM_LINEAR at the mutated C text."""
    digest = hashlib.sha256(c_text.encode()).hexdigest()
    script, nlin = re.subn(r"(?m)^LINEAR_SHA=[0-9a-f]{64}$", "LINEAR_SHA=" + digest, script, count=1)
    script, nfrom = re.subn(r"(?m)^LIB_FROM_LINEAR=[0-9a-f]{64}$", "LIB_FROM_LINEAR=" + digest, script, count=1)
    if nlin != 1 or nfrom != 1:
        return ""
    return script


def _emit_mutation(name, detail, ok):
    if ok:
        print(f"RED {name}: {detail}")
        check(f"mutation shown red: {name}", True)
    else:
        print(f"mutation {name} was not red for {detail}")
        check(f"mutation shown red: {name}", False)


def _run_bash(body):
    try:
        return subprocess.run(["bash", "-c", body], capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(["bash"], 127, "", str(exc))


def _heredoc(text, marker, end):
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    stop = text.find(end, start)
    if stop < 0:
        return ""
    return text[start:stop]


def _fake_nm(path, table):
    lines = ["#!/usr/bin/env python3", "import os, sys", "name = os.path.basename(sys.argv[-1])", "table = {"]
    for name, syms in table.items():
        lines.append("    %r: %r," % (name, syms))
    lines.append("}")
    lines.append("for sym in table.get(name, []):")
    lines.append("    print('                 U ' + sym)")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


_HARDEN_OK = {
    "cros_gralloc_helpers.o": ["__stack_chk_fail"],
    "cros_gralloc_buffer.o": ["__stack_chk_fail"],
    "cros_gralloc_driver.o": ["__stack_chk_fail"],
    "virtgpu_virgl.o": ["__open_2"],
    "amdgpu_linear.o": ["__stack_chk_fail"],
}


def _harden_py(text):
    return _heredoc(text, "python3 - \"$NM\" \"$OUT/obj\" << 'PY'\n", "\nPY\n")


def _run_hardening(py_text, table):
    global _yv12_seq
    _yv12_seq += 1
    work = tmp / f"harden-{_yv12_seq}"
    work.mkdir()
    obj = work / "obj"
    obj.mkdir()
    for name in table:
        (obj / name).write_bytes(b"")
    nm = work / "nm"
    _fake_nm(nm, table)
    script = work / "check.py"
    script.write_text(py_text, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), str(nm), str(obj)], capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _drop_sym(table, name, sym):
    out = {key: list(val) for key, val in table.items()}
    out[name] = [item for item in out[name] if item != sym]
    return out


def _pin_parts(text):
    start = text.find('if [[ "$sha" != "$LIB_SHA" ]]; then\n')
    write = text.find('{\n  echo "PASS"\n', start)
    mv = 'mv "$OUT/BUILD-STATUS.txt.tmp" "$OUT/BUILD-STATUS.txt"\n'
    end = text.find(mv, write) if write >= 0 else -1
    if start < 0 or write < 0 or end < 0:
        return "", ""
    end += len(mv)
    return text[start:write], text[write:end]


def _run_pin(if_block, write_block, sha, lib_sha, swap=False):
    global _yv12_seq
    _yv12_seq += 1
    out = tmp / f"pin-{_yv12_seq}"
    out.mkdir()
    region = (write_block + if_block) if swap else (if_block + write_block)
    script = (
        "OUT=%s\nsha=%s\nLIB_SHA=%s\n"
        "size=1\nCOMMIT=c\nTREE=t\nTAR_SHA=tar\navg60=0\nSTOCK_SO_SHA=stock\n"
        "vendor_a=0\nsystem_a=0\nsystem_ext_a=0\nOFF=(1)\n"
        "log() { printf '%%s\\n' \"$*\"; }\n"
        "rm() { printf 'RM %%s\\n' \"$*\" >> \"$OUT/rm.log\"; }\n"
        "%s"
    ) % (shlex.quote(str(out)), shlex.quote(sha), shlex.quote(lib_sha), region)
    proc = _run_bash(script)
    status = out / "BUILD-STATUS.txt"
    published = status.is_file() and status.read_text(encoding="utf-8").startswith("PASS\n")
    rmlog = (out / "rm.log").read_text(encoding="utf-8") if (out / "rm.log").is_file() else ""
    return proc.returncode, published, rmlog, proc.stdout + proc.stderr


_BUILT_FROM_IF = (
    'if [[ "$LINEAR_SHA" != "$LIB_FROM_LINEAR" || "$PATCH_SHA" != "$LIB_FROM_PATCH" '
    '|| "$TAR_SHA" != "$LIB_FROM_TAR" ]]; then\n'
    '  log "pinned source sha does not match the library built-from record"\n'
    "  exit 1\n"
    "fi\n"
)


def _built_from_region(text):
    start = text.find(_BUILT_FROM_IF)
    line = 'log "library built-from pins match the source pins"\n'
    if start < 0:
        return ""
    end = text.find(line, start)
    if end < 0:
        return ""
    return text[start:end + len(line)]


def _run_built_from(region, match):
    linear = "aa" if match else "zz"
    script = (
        "LINEAR_SHA=aa\nLIB_FROM_LINEAR=%s\nPATCH_SHA=bb\nLIB_FROM_PATCH=bb\n"
        "TAR_SHA=cc\nLIB_FROM_TAR=cc\n"
        "log() { printf '%%s\\n' \"$*\"; }\n"
        "%s"
    ) % (linear, region)
    proc = _run_bash(script)
    return proc.returncode, proc.stdout + proc.stderr


def _region_between(text, start_s, end_s):
    start = text.find(start_s)
    if start < 0:
        return ""
    end = text.find(end_s, start)
    if end < 0:
        return ""
    return text[start:end + len(end_s)]


def _run_wrap(region, cc_body, cc_exists=True, cc_dir=False):
    global _yv12_seq
    _yv12_seq += 1
    work = tmp / f"wrap-{_yv12_seq}"
    work.mkdir()
    cc = work / "cc"
    cxx = work / "cxx"
    if cc_dir:
        cc.mkdir()
    elif cc_exists:
        cc.write_text(cc_body, encoding="utf-8")
    cxx.write_text("#!/bin/sh\n", encoding="utf-8")
    script = (
        "CC=%s\nCXX=%s\nlog() { printf '%%s\\n' \"$*\"; }\n%s"
    ) % (shlex.quote(str(cc)), shlex.quote(str(cxx)), region)
    proc = _run_bash(script)
    return proc.returncode, proc.stdout + proc.stderr


def _run_clang_bin(region, shebang):
    global _yv12_seq
    _yv12_seq += 1
    work = tmp / f"clangbin-{_yv12_seq}"
    work.mkdir()
    binary = work / "clang"
    if shebang:
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
    else:
        binary.write_bytes(b"\x7fELF")
    script = (
        "CLANG_BIN=%s\nlog() { printf '%%s\\n' \"$*\"; }\n%s"
    ) % (shlex.quote(str(binary)), region)
    proc = _run_bash(script)
    return proc.returncode, proc.stdout + proc.stderr


def _sources_writer_region(text):
    start_s = "home_literal() {\n"
    end_s = '} > "$OUT/SOURCES.txt"\n'
    start = text.find(start_s)
    if start < 0 or text.find(start_s, start + 1) >= 0:
        return ""
    end = text.find(end_s, start)
    if end < 0:
        return ""
    return text[start:end + len(end_s)]


def _sources_problems(region, img_under_home=True, checkout_pins=False, captured=None):
    """Run the SOURCES.txt writer. Empty means the output block and literal $HOME held.

    checkout_pins hashes the real minigbm sources and fills the library pin, so the
    text can be copied over the tracked SOURCES.txt. captured receives that text.
    """
    if not region:
        return ["missing writer"]
    global _yv12_seq
    _yv12_seq += 1
    work = tmp / f"sources-{_yv12_seq}"
    work.mkdir()
    home = work / "gbos-home-token"
    bindir = work / "bin"
    out = work / "out"
    bindir.mkdir()
    out.mkdir()
    (home / "gbos-phase/amd-t2/kernel-amd/ksrc/.git").mkdir(parents=True)
    if checkout_pins:
        work_src = work / "work"
        (work_src / "cxxsite").mkdir(parents=True)
        (work_src / "cxxsite" / "__config_site").write_text("site\n", encoding="utf-8")
        recipe = (ROOT / "tools/minigbm/build-libminigbm-gralloc.sh").read_text(encoding="utf-8")
        (bindir / "sha256sum").write_text(
            "#!/bin/sh\n"
            "exec python3 - \"$@\" << 'PY'\n"
            "import hashlib, sys\n"
            "for path in sys.argv[1:]:\n"
            "    print('%s  %s' % (hashlib.sha256(open(path, 'rb').read()).hexdigest(), path))\n"
            "PY\n",
            encoding="utf-8",
        )
        pin_lines = (
            "sha=%s\nsize=112384\n"
            "LIB_FROM_LINEAR=%s\nLIB_FROM_PATCH=%s\nLIB_FROM_TAR=%s\n"
            "PATCH=%s\nLINEAR_SRC=%s\nWORK=%s\n"
        ) % (
            shlex.quote(_hex_assign(recipe, "LIB_SHA")),
            shlex.quote(_hex_assign(recipe, "LIB_FROM_LINEAR")),
            shlex.quote(_hex_assign(recipe, "LIB_FROM_PATCH")),
            shlex.quote(_hex_assign(recipe, "LIB_FROM_TAR")),
            shlex.quote(str(ROOT / "tools/minigbm/upstream-delta.patch")),
            shlex.quote(str(ROOT / "tools/minigbm/amdgpu_linear.c")),
            shlex.quote(str(work_src)),
        )
    else:
        (bindir / "sha256sum").write_text(
            "#!/bin/sh\nprintf '%s\\n' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  -'\n",
            encoding="utf-8",
        )
        pin_lines = (
            "sha=1111111111111111111111111111111111111111111111111111111111111111\n"
            "size=112384\n"
            "LIB_FROM_LINEAR=2222222222222222222222222222222222222222222222222222222222222222\n"
            "LIB_FROM_PATCH=3333333333333333333333333333333333333333333333333333333333333333\n"
            "LIB_FROM_TAR=4444444444444444444444444444444444444444444444444444444444444444\n"
            "PATCH=/no/such/patch\nLINEAR_SRC=/no/such/linear\nWORK=/no/such/work\n"
        )
    (bindir / "git").write_text(
        "#!/bin/sh\n"
        "while [ \"$1\" = \"-C\" ]; do shift 2; done\n"
        "case \"$1\" in\n"
        "  rev-parse) printf '%s\\n' 0123456789abcdef0123456789abcdef01234567 ;;\n"
        "  ls-files) printf '%s\\n' '100644 blob abc 0 file' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    os.chmod(bindir / "sha256sum", 0o755)
    os.chmod(bindir / "git", 0o755)
    img = (
        '"$HOME/gbos-phase/amd-r11/out-amd-hx370/image/googlebook-amd-hx370.img"'
        if img_under_home else "/opt/stock/googlebook-amd-hx370.img"
    )
    script = (
        "HOME=%s\nOUT=%s\nexport PATH=%s:\"$PATH\"\n"
        "AIDL=\"$HOME/gbos-phase/fp/tools/bt/android-37.0/aidl\"\n"
        "NDK=\"$HOME/gbos-phase/fp/tools/android-ndk-r30\"\n"
        "IMG=%s\n"
        "%s"
        "GIT_URL=https://example.invalid/minigbm\nCOMMIT=ce79bbee\nTREE=95c2eb68\n"
        "TAR_SHA=54b1d07b\nLIBDRM_URL=https://example.invalid/libdrm\nLIBDRM_SHA=a2b98567\n"
        "AIDL_SHA=a4ad150a\nNDK_REV=30.0.16248370\nCLANG_SHA=ad535f0b\nSTOCK_SO_SHA=d4853695\n"
        "AOSP=/no/such/aosp\n"
        "HWC_URL=https://example.invalid/hwc\nHWC_DIR=/no/such/hwc\nOFF=()\nLEN=()\n"
        "%s"
    ) % (shlex.quote(str(home)), shlex.quote(str(out)), shlex.quote(str(bindir)), img, pin_lines, region)
    proc = _run_bash(script)
    path = out / "SOURCES.txt"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    problems = []
    if proc.returncode != 0:
        problems.append("writer exit %s %s" % (proc.returncode, (proc.stdout + proc.stderr)[-300:]))
    if str(home) in text:
        problems.append("expanded home")
    if captured is not None:
        captured.append(text)
    if checkout_pins:
        return problems
    required = [
        "\noutput\n",
        "libminigbm_gralloc.so-sha256: 1111111111111111111111111111111111111111111111111111111111111111\n",
        "size: 112384\n",
        "built-from-amdgpu_linear.c-sha256: 2222222222222222222222222222222222222222222222222222222222222222\n",
        "built-from-patch-sha256: 3333333333333333333333333333333333333333333333333333333333333333\n",
        "built-from-tar-sha256: 4444444444444444444444444444444444444444444444444444444444444444\n",
        "note: profile [gralloc] pin. ",
        "No second-host rebuild is recorded.",
        "path: $HOME/gbos-phase/fp/tools/bt/android-37.0/aidl\n",
        "path: $HOME/gbos-phase/fp/tools/android-ndk-r30\n",
        "path: $HOME/gbos-phase/amd-t2/kernel-amd/ksrc\n",
    ]
    if img_under_home:
        required.append("path: $HOME/gbos-phase/amd-r11/out-amd-hx370/image/googlebook-amd-hx370.img\n")
    else:
        required.append("path: /opt/stock/googlebook-amd-hx370.img\n")
    for line in required:
        if line not in text:
            problems.append("missing " + line.strip())
    return problems


def _sources_checkout_problems(sources, script, c_bytes, patch_bytes, readme, profile):
    """The SOURCES.txt checks a maintainer hits by copying the generated file back."""
    problems = []
    linear_sha = hashlib.sha256(c_bytes).hexdigest()
    lib_sha = _hex_assign(script, "LIB_SHA")
    if not (
        len(lib_sha) == 64
        and f'sha256 = "{lib_sha}"' in profile
        and f"`{lib_sha}`" in readme
        and f"libminigbm_gralloc.so-sha256: {lib_sha}" in sources
    ):
        problems.append("library pin")
    if not (
        _hex_assign(script, "LIB_FROM_LINEAR") == linear_sha
        and _hex_assign(script, "LIB_FROM_PATCH") == hashlib.sha256(patch_bytes).hexdigest()
        and _hex_assign(script, "LIB_FROM_TAR") == "54b1d07b4645ad73294eab40dbbc4578af43cdee192d4ba5e0b1c2747f5d8834"
        and f"amdgpu_linear.c-sha256: {linear_sha}" in sources
        and f"built-from-amdgpu_linear.c-sha256: {linear_sha}" in sources
        and "built-from-patch-sha256: 0d2e6678b9c71a47a412245a85dfbf99702bda0f56da522fe65b996bb0bee022" in sources
        and "built-from-tar-sha256: 54b1d07b4645ad73294eab40dbbc4578af43cdee192d4ba5e0b1c2747f5d8834" in sources
    ):
        problems.append("built-from")
    if not (
        "`LIB_SHA`, `LIB_FROM_LINEAR`, `LIB_FROM_PATCH`, and `LIB_FROM_TAR` update together." in readme
        and "Editing a source and `LIB_FROM_LINEAR`, `LIB_FROM_PATCH`, or `LIB_FROM_TAR` without that rebuild leaves the profile tests green and makes the build refuse." in readme
        and "LIB_SHA, LIB_FROM_LINEAR, LIB_FROM_PATCH, and LIB_FROM_TAR update together" in sources
    ):
        problems.append("update together")
    if not (
        "A second clean machine has not been compared." in readme
        and "No second-host rebuild is recorded." in sources
    ):
        problems.append("second-host")
    return problems


_COMPILE_FLAGS = (
    "-fstack-protector-strong",
    "-D_FORTIFY_SOURCE=2",
    "-ftrivial-auto-var-init=zero",
    "-funwind-tables",
    "-DANDROID_API_LEVEL=37",
)


def _compile_rows(text):
    region = _region_between(text, "CFLAGS=(\n", 'log "per-object hardening imports"\n')
    # The end marker is included by _region_between; drop that log line.
    if region.endswith('log "per-object hardening imports"\n'):
        region = region[: -len('log "per-object hardening imports"\n')]
    if not region.startswith("CFLAGS=("):
        return []
    global _yv12_seq
    _yv12_seq += 1
    work = tmp / f"ccargv-{_yv12_seq}"
    work.mkdir()
    rec = work / "record.py"
    record = work / "argv.txt"
    rec.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.environ['RECORD'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write('\\t'.join(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "if '-o' in args:\n"
        "    open(args[args.index('-o') + 1], 'w').close()\n",
        encoding="utf-8",
    )
    rec.chmod(0o755)
    script = (
        "OUT=%s\nSRC=%s\nexport RECORD=%s\nCC=%s\nCXX=$CC\n"
        "log() { printf '%%s\\n' \"$*\"; }\n"
        "mkdir -p \"$SRC\" \"$OUT/obj\"\n"
        "%s"
    ) % (shlex.quote(str(work)), shlex.quote(str(work / "src")), shlex.quote(str(record)), shlex.quote(str(rec)), region)
    proc = _run_bash(script)
    if proc.returncode != 0 or not record.is_file():
        print("compiler argv runner:", proc.returncode, (proc.stdout + proc.stderr)[-400:])
        return []
    return [line.split("\t") for line in record.read_text(encoding="utf-8").splitlines() if line]


def _split_rows(rows):
    c_rows, cxx_rows = [], []
    for row in rows:
        if "-c" not in row:
            continue
        src = row[row.index("-c") + 1]
        if src.endswith(".cc"):
            cxx_rows.append(row)
        elif src.endswith(".c"):
            c_rows.append(row)
    return c_rows, cxx_rows


def _rows_have(rows, flags):
    return bool(rows) and all(all(flag in row for flag in flags) for row in rows)


def _minigbm_script_errors(text, c_text):
    errs = []
    cflags = _bash_array(text, "CFLAGS")
    cxxflags = _bash_array(text, "CXXFLAGS")
    if not cflags or not cxxflags:
        errs.append("flag arrays")
    for flag in (
        "-fstack-protector-strong",
        "-D_FORTIFY_SOURCE=2",
        "-ftrivial-auto-var-init=zero",
        "-funwind-tables",
        "-DANDROID_API_LEVEL=37",
    ):
        if flag not in cflags or flag not in cxxflags:
            errs.append("both arrays " + flag)
    if "-U_FORTIFY_SOURCE" in cflags or "-U_FORTIFY_SOURCE" in cxxflags:
        errs.append("fortify undefined")
    if "-fvisibility-inlines-hidden" not in cxxflags:
        errs.append("visibility")
    if "-Wl,-z,relro" not in text or "-Wl,-z,now" not in text:
        errs.append("relro")
    awk_line = (
        'ndk_rev=$(awk -F= \'/Pkg.Revision/ {gsub(/ /,"",$2); print $2}\' '
        '"$NDK/source.properties")'
    )
    if "30.0.16248370" not in text or awk_line not in text or '"$ndk_rev" != "$NDK_REV"' not in text:
        errs.append("ndk revision")
    if re.search(r"(?m)^ndk_rev=\$NDK_REV\b", text):
        errs.append("ndk revision")
    if "95c2eb6837a39a6ef62ae454c5d9767cf2066c53" not in text:
        errs.append("tree")
    if "54b1d07b4645ad73294eab40dbbc4578af43cdee192d4ba5e0b1c2747f5d8834" not in text:
        errs.append("tar")
    if "sha256sum -c" in text and ".tar.gz" in text:
        errs.append("gzip pin")
    if 'readlink -f "$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/clang"' not in text:
        errs.append("clang binary")
    if "ad535f0b6fc303edba958b51636169ff0291c6e36463236c99ff8abcaa34247c" not in text:
        errs.append("clang binary sha")
    if 'echo "$CLANG_SHA  $CLANG_BIN"' not in text or 'echo "$CLANG_SHA  $CC"' in text:
        errs.append("clang wrapper hashed as compiler")
    if "missing clang wrapper" not in text or "clang wrapper is not a regular file" not in text:
        errs.append("clang wrapper type")
    if "clang wrapper is not a script" not in text:
        errs.append("clang wrapper type")
    if "resolved clang is a wrapper script, not the compiler binary" not in text:
        errs.append("clang binary script")
    for line in text.splitlines():
        if "sha256sum -c" in line and "|| true" in line:
            errs.append("checksum ignored")
            break
    if '"$CC" "${CFLAGS[@]}" -c "$SRC/$src"' not in text:
        errs.append("cflags not applied")
    if '"$CXX" "${CXXFLAGS[@]}" -c "$SRC/cros_gralloc/$src"' not in text:
        errs.append("cxxflags not applied")
    if "-fno-stack-protector" in text or "-U_FORTIFY_SOURCE" in text:
        errs.append("hardening disabled")
    if "object_hardening_ok" not in text or "missing __stack_chk_fail" not in text:
        errs.append("object hardening")
    if "missing __open_2" not in text or "virtgpu_virgl.o" not in text:
        errs.append("object hardening")
    if "cros_gralloc_buffer.o" not in text or "no C object imports __stack_chk_fail" not in text:
        errs.append("object hardening")
    if 'if [[ "$sha" != "$LIB_SHA" ]]; then' not in text:
        errs.append("library pin")
    if "refusing PASS: a pinned source changed or the library pin was not updated" not in text:
        errs.append("library pin")
    if '"$LINEAR_SHA" != "$LIB_FROM_LINEAR"' not in text or '"$PATCH_SHA" != "$LIB_FROM_PATCH"' not in text:
        errs.append("built-from")
    if '"$TAR_SHA" != "$LIB_FROM_TAR"' not in text:
        errs.append("built-from")
    if "pinned source sha does not match the library built-from record" not in text:
        errs.append("built-from")
    if _hex_assign(text, "LINEAR_SHA") != _hex_assign(text, "LIB_FROM_LINEAR"):
        errs.append("linear built-from")
    if _hex_assign(text, "PATCH_SHA") != _hex_assign(text, "LIB_FROM_PATCH"):
        errs.append("patch built-from")
    if _hex_assign(text, "TAR_SHA") != _hex_assign(text, "LIB_FROM_TAR"):
        errs.append("tar built-from")
    if hashlib.sha256(c_text.encode()).hexdigest() != _hex_assign(text, "LINEAR_SHA"):
        errs.append("linear file")
    if '"__stack_chk_fail" not in new_undef' not in text or "stock-undefsyms.txt" not in text:
        errs.append("stack chk")
    if '"__open_2" not in new_undef' not in text:
        errs.append("fortify open")
    if 'sys.exit("missing stock __*_chk imports:' in text:
        errs.append("vacuous chk")
    strings_at = text.find('print("STRINGS_OK")')
    pass_at = text.find('echo "PASS"')
    if strings_at < 0 or pass_at < strings_at:
        errs.append("pass before strings")
    if 'rm -f "$OUT/BUILD-STATUS.txt"' not in text:
        errs.append("stale status")
    if "status --porcelain" not in text:
        errs.append("dirty tree")
    if '\t.name = "amdgpu",\n' not in c_text:
        errs.append("backend name")
    if (
        "\tif (modifier != DRM_FORMAT_MOD_LINEAR && modifier != DRM_FORMAT_MOD_INVALID)\n"
        "\t\treturn -EINVAL;\n"
    ) not in c_text:
        errs.append("linear import")
    yv12_call = (
        "\tif (format == DRM_FORMAT_YVU420_ANDROID)\n"
        "\t\tstride = amdgpu_linear_yv12_luma(stride, stride_align);\n"
    )
    if yv12_call not in c_text:
        errs.append("yv12 call")
    helper = _yv12_helper_body(c_text)
    if not helper:
        errs.append("yv12 formula")
    else:
        host = _compile_yv12(helper)
        if host is None:
            if _YV12_FORMULA_LINE not in helper:
                errs.append("yv12 formula")
        elif host:
            errs.append("yv12 host " + host)
    return errs


_SOURCE_WHY = "release tree does not ship git-only minigbm build sources"
_script_path = ROOT / "tools/minigbm/build-libminigbm-gralloc.sh"
_c_path = ROOT / "tools/minigbm/amdgpu_linear.c"
_patch_path = ROOT / "tools/minigbm/upstream-delta.patch"
_git_checkout = (ROOT / ".git").exists()
_full_sources = _script_path.is_file() and _c_path.is_file() and _patch_path.is_file()
if _full_sources:
    _script = _script_path.read_text(encoding="utf-8")
    _c_text = _c_path.read_text(encoding="utf-8")
    _script_errs = _minigbm_script_errors(_script, _c_text)
    if _script_errs:
        print("minigbm script:", _script_errs)
    check("minigbm build script enforces the pins, hardening, and status order", not _script_errs)
    if os.environ.get("CI"):
        check("CI runs the YV12 host compile", _YV12_HOST == "ran")
    if _YV12_HOST == "no-cc":
        _skip("host compile of amdgpu_linear_yv12_luma", "no C compiler on PATH")
        check("text check: yv12 helper line rounds up with the stride_align * 2 mask",
              _YV12_FORMULA_LINE in _yv12_helper_body(_c_text))
    else:
        check("host compile of amdgpu_linear_yv12_luma: 512, 1024, 2048 stay at align 256; "
              "align 16 leaves 640; 768 becomes 1024 and 1280 becomes 1536; "
              "640-wide after ALIGN32 and ALIGN256 is luma 1024 chroma 512; "
              "even widths 2..8192 at align 1 and 256 keep chroma",
              not any(err.startswith("yv12 host") or err == "yv12 formula" for err in _script_errs))
    check("text check: non-linear import guard returns -EINVAL",
          "\tif (modifier != DRM_FORMAT_MOD_LINEAR && modifier != DRM_FORMAT_MOD_INVALID)\n"
          "\t\treturn -EINVAL;\n" in _c_text)
    check("text check: create() calls amdgpu_linear_yv12_luma for YVU420_ANDROID",
          "\tif (format == DRM_FORMAT_YVU420_ANDROID)\n"
          "\t\tstride = amdgpu_linear_yv12_luma(stride, stride_align);\n" in _c_text)
    _linear_sha = hashlib.sha256(_c_path.read_bytes()).hexdigest()
    _sources = (ROOT / "tools/minigbm/SOURCES.txt").read_text(encoding="utf-8")
    _readme = (ROOT / "tools/minigbm/README.md").read_text(encoding="utf-8")
    _profile = (ROOT / "profiles/amd-hx370/profile.toml").read_text(encoding="utf-8")
    _lib_sha = _hex_assign(_script, "LIB_SHA")
    check("amdgpu_linear.c matches LINEAR_SHA",
          f"LINEAR_SHA={_linear_sha}" in _script)
    _co = _sources_checkout_problems(
        _sources, _script, _c_path.read_bytes(), _patch_path.read_bytes(), _readme, _profile)
    check("library pin matches profile, README, and SOURCES", "library pin" not in _co)
    check("built-from source pins match the files and SOURCES", "built-from" not in _co)
    check("amdgpu_linear.c points at tools/minigbm/NOTICES.txt",
          "found in tools/minigbm/NOTICES.txt" in _c_text and "found in the LICENSE file" not in _c_text)
    _patch_sha = hashlib.sha256(_patch_path.read_bytes()).hexdigest()
    check("upstream-delta.patch matches the build script pin",
          _patch_sha == "0d2e6678b9c71a47a412245a85dfbf99702bda0f56da522fe65b996bb0bee022"
          and f"PATCH_SHA={_patch_sha}" in _script)
    check("README names LIB_SHA, LIB_FROM_LINEAR, LIB_FROM_PATCH, and LIB_FROM_TAR together",
          "update together" not in _co)
    check("README calls the clang wrapper test a presence and type check",
          "presence and type check" in _readme
          and "wrapper integrity check" not in _readme
          and "A change to the wrapper text is stopped by `LIB_SHA`" in _readme)
    check("README and SOURCES keep the single-host reproduction limit",
          "second-host" not in _co)
    _script_mutations = (
        ("api level removed from both compile lines", "  -DANDROID_API_LEVEL=37\n", "", 2,
         ["both arrays -DANDROID_API_LEVEL=37"]),
        ("cxx hardening flags removed",
         "  -fstack-protector-strong -D_FORTIFY_SOURCE=2 -ftrivial-auto-var-init=zero -funwind-tables\n"
         "  -fvisibility-inlines-hidden\n",
         "  -fvisibility-inlines-hidden\n", 1,
         ["both arrays -fstack-protector-strong", "both arrays -D_FORTIFY_SOURCE=2",
          "both arrays -ftrivial-auto-var-init=zero", "both arrays -funwind-tables"]),
        ("fortify open check removed",
         'if "__open_2" not in new_undef:\n'
         '    sys.exit("new lib does not import __open_2 (_FORTIFY_SOURCE)")\n',
         "", 1, ["fortify open"]),
        ("ndk pin bypassed",
         'ndk_rev=$(awk -F= \'/Pkg.Revision/ {gsub(/ /,"",$2); print $2}\' "$NDK/source.properties")\n',
         'ndk_rev=$NDK_REV; : $(awk -F= \'/Pkg.Revision/ {gsub(/ /,"",$2); print $2}\' "$NDK/source.properties")\n',
         1, ["ndk revision"]),
        ("ndk pin removed", '"$ndk_rev" != "$NDK_REV"', '"$ndk_rev" != "$ndk_rev"', 1, ["ndk revision"]),
        ("pass moved before strings", 'print("STRINGS_OK")\n', "", 1, ["pass before strings"]),
        ("PASS echo moved before STRINGS_OK",
         'print("STRINGS_OK")\n', 'echo "PASS"\nprint("STRINGS_OK")\n', 1, ["pass before strings"]),
        ("stack check removed", '"__stack_chk_fail" not in new_undef', "False", 1, ["stack chk"]),
        ("clang wrapper hashed as the compiler",
         'echo "$CLANG_SHA  $CLANG_BIN" | sha256sum -c -',
         'echo "$CLANG_SHA  $CC" | sha256sum -c -', 1, ["clang wrapper hashed as compiler"]),
        ("clang pin ignored",
         'echo "$CLANG_SHA  $CLANG_BIN" | sha256sum -c -',
         'echo "$CLANG_SHA  $CLANG_BIN" | sha256sum -c - || true', 1, ["checksum ignored"]),
        ("clang wrapper type check removed",
         '  if ! head -n 1 "$wrap" | grep -q \'^#!\'; then\n'
         '    log "clang wrapper is not a script: $wrap"\n'
         '    exit 1\n'
         '  fi\n',
         "", 1, ["clang wrapper type"]),
        ("resolved clang script guard removed",
         'if head -n 1 "$CLANG_BIN" | grep -q \'^#!\'; then\n'
         '  log "resolved clang is a wrapper script, not the compiler binary: $CLANG_BIN"\n'
         '  exit 1\n'
         'fi\n',
         "", 1, ["clang binary script"]),
        ("cxx flags not passed to cros_gralloc",
         '"$CXX" "${CXXFLAGS[@]}" -c "$SRC/cros_gralloc/$src"',
         '"$CXX" -c "$SRC/cros_gralloc/$src"', 1, ["cxxflags not applied"]),
        ("object hardening marker removed", 'print("object_hardening_ok")\n', "", 1, ["object hardening"]),
        ("library pin check removed",
         'if [[ "$sha" != "$LIB_SHA" ]]; then\n'
         '  log "built $sha != pinned library $LIB_SHA"\n'
         '  log "refusing PASS: a pinned source changed or the library pin was not updated"\n'
         '  log "removing /tmp scratch"\n'
         '  rm -rf /tmp/minigbm4\n'
         '  exit 1\n'
         'fi\n',
         "", 1, ["library pin"]),
    )
    for _mname, _mold, _mnew, _mcount, _mexpect in _script_mutations:
        if _script.count(_mold) != _mcount:
            check(f"minigbm mutation anchor once: {_mname}", False)
            continue
        _mut = _script.replace(_mold, _mnew)
        _errs = _uniq(_minigbm_script_errors(_mut, _c_text))
        _emit_mutation(_mname, _errs, _errs == _mexpect)
    _from_linear = _hex_assign(_script, "LIB_FROM_LINEAR")
    _detach = _script.replace(f"LIB_FROM_LINEAR={_from_linear}", "LIB_FROM_LINEAR=" + ("0" * 64), 1)
    _detach_errs = _uniq(_minigbm_script_errors(_detach, _c_text)) if _from_linear else []
    _emit_mutation("built-from linear pin detached", _detach_errs, _detach_errs == ["linear built-from"])
    _c_mutations = (
        ("backend renamed", '\t.name = "amdgpu",\n', '\t.name = "disabled",\n', ["backend name"], ""),
        ("linear import guard removed",
         "\tif (modifier != DRM_FORMAT_MOD_LINEAR && modifier != DRM_FORMAT_MOD_INVALID)\n"
         "\t\treturn -EINVAL;\n",
         "", ["linear import"], ""),
        ("yv12 stride doubling removed",
         "\tif (format == DRM_FORMAT_YVU420_ANDROID)\n"
         "\t\tstride = amdgpu_linear_yv12_luma(stride, stride_align);\n",
         "", ["yv12 call"], ""),
        ("yv12 helper doubling removed",
         "\t\tstride = (stride + (stride_align * 2) - 1) & ~((stride_align * 2) - 1);\n",
         "\t\tstride = stride;\n", [], "768 at align 256 becomes 1024"),
        ("yv12 mask uses stride_align instead of stride_align * 2",
         "\t\tstride = (stride + (stride_align * 2) - 1) & ~((stride_align * 2) - 1);\n",
         "\t\tstride = (stride + (stride_align * 2) - 1) & ~((stride_align) - 1); /* stride_align * 2 */\n",
         [], "already multiple 512 stays 512 at align 256"),
    )
    for _mname, _mold, _mnew, _mrest, _mhost in _c_mutations:
        if _c_text.count(_mold) != 1:
            check(f"minigbm mutation anchor once: {_mname}", False)
            continue
        _mut_c = _c_text.replace(_mold, _mnew, 1)
        _mut_script = _repin_linear(_script, _mut_c)
        _errs = _uniq(_minigbm_script_errors(_mut_script, _mut_c)) if _mut_script else ["repin failed"]
        _host = [err for err in _errs if err.startswith("yv12 host ")]
        _rest = [err for err in _errs if not err.startswith("yv12 host ")]
        if _YV12_HOST == "no-cc" and _mhost:
            _ok = _rest == ["yv12 formula"] and not _host
            _detail = _errs
        else:
            _ok = _rest == _mrest and (not _mhost) == (not _host) and (not _mhost or (len(_host) == 1 and _mhost in _host[0]))
            _detail = _errs
        if "linear file" in _errs:
            _ok = False
        _emit_mutation(_mname, _detail, _ok)
    _harden = _harden_py(_script)
    _harden_missing_stack = _drop_sym(_HARDEN_OK, "cros_gralloc_buffer.o", "__stack_chk_fail")
    _harden_missing_open = _drop_sym(_HARDEN_OK, "virtgpu_virgl.o", "__open_2")
    _harden_no_c = _drop_sym(_HARDEN_OK, "amdgpu_linear.o", "__stack_chk_fail")
    _h_ok = _run_hardening(_harden, _HARDEN_OK) if _harden else (1, "missing hardening python")
    _h_stack = _run_hardening(_harden, _harden_missing_stack) if _harden else (0, "")
    _h_open = _run_hardening(_harden, _harden_missing_open) if _harden else (0, "")
    _h_c = _run_hardening(_harden, _harden_no_c) if _harden else (0, "")
    check("per-object hardening accepts the required imports",
          _h_ok[0] == 0 and "object_hardening_ok" in _h_ok[1])
    check("per-object hardening refuses a cros_gralloc object without __stack_chk_fail",
          _h_stack[0] != 0 and "cros_gralloc_buffer.o missing __stack_chk_fail" in _h_stack[1])
    check("per-object hardening refuses virtgpu_virgl.o without __open_2",
          _h_open[0] != 0 and "virtgpu_virgl.o missing __open_2" in _h_open[1])
    check("per-object hardening refuses when no C object imports __stack_chk_fail",
          _h_c[0] != 0 and "no C object imports __stack_chk_fail" in _h_c[1])
    _harden_off = _harden
    for _old_exit, _new_exit in (
        ('        sys.exit(path.name + " missing __stack_chk_fail")\n',
         '        print(path.name + " missing __stack_chk_fail")\n'),
        ('        sys.exit(path.name + " missing __open_2")\n',
         '        print(path.name + " missing __open_2")\n'),
        ('    sys.exit("no C object imports __stack_chk_fail")\n',
         '    print("no C object imports __stack_chk_fail")\n'),
    ):
        if _harden_off.count(_old_exit) != 1:
            _harden_off = ""
            break
        _harden_off = _harden_off.replace(_old_exit, _new_exit, 1)
    if _harden_off:
        _h_mut = _run_hardening(_harden_off, _harden_missing_stack)
        _h_mut_open = _run_hardening(_harden_off, _harden_missing_open)
        _h_mut_c = _run_hardening(_harden_off, _harden_no_c)
        _emit_mutation(
            "object hardening rejection disabled",
            "missing imports exit 0",
            _h_mut[0] == 0 and _h_mut_open[0] == 0 and _h_mut_c[0] == 0,
        )
    else:
        check("minigbm mutation anchor once: object hardening rejection disabled", False)
    _pin_if, _pin_write = _pin_parts(_script)
    _pin_bad = _run_pin(_pin_if, _pin_write, "deadbeef", _lib_sha) if _pin_if and _lib_sha else (0, True, "", "")
    _pin_good = _run_pin(_pin_if, _pin_write, _lib_sha, _lib_sha) if _pin_if and _lib_sha else (1, False, "", "")
    check("library pin mismatch exits and does not publish PASS",
          _pin_bad[0] != 0 and not _pin_bad[1] and "/tmp/minigbm4" in _pin_bad[2])
    check("library pin match publishes PASS", _pin_good[0] == 0 and _pin_good[1])
    _pin_late = _run_pin(_pin_if, _pin_write, "deadbeef", _lib_sha, swap=True) if _pin_if else (1, False, "", "")
    _emit_mutation(
        "library pin check moved after PASS",
        "mismatch published PASS",
        _pin_late[1] and _pin_late[0] != 0,
    )
    _built_region = _built_from_region(_script)
    _built_bad = _run_built_from(_built_region, False) if _built_region else (0, "library built-from pins match")
    _built_good = _run_built_from(_built_region, True) if _built_region else (1, "")
    check("built-from mismatch exits before the match log",
          _built_bad[0] != 0 and "library built-from pins match the source pins" not in _built_bad[1])
    check("built-from match reaches the match log",
          _built_good[0] == 0 and "library built-from pins match the source pins" in _built_good[1])
    _built_mut_region = _built_region.replace("  exit 1\n", "  :\n", 1)
    _built_mut_bad = _run_built_from(_built_mut_region, False) if _built_mut_region and _built_mut_region != _built_region else (1, "")
    _emit_mutation(
        "built-from mismatch does not exit",
        "match log reached",
        "library built-from pins match the source pins" in _built_mut_bad[1],
    )
    _wrap = _region_between(
        _script, 'for wrap in "$CC" "$CXX"; do\n', 'log "clang wrappers exist and are scripts"\n')
    _wrap_ok = _run_wrap(_wrap, "#!/bin/sh\n") if _wrap else (1, "")
    _wrap_text = _run_wrap(_wrap, "not a script\n") if _wrap else (0, "")
    _wrap_miss = _run_wrap(_wrap, "", cc_exists=False) if _wrap else (0, "")
    _wrap_dir = _run_wrap(_wrap, "", cc_dir=True) if _wrap else (0, "")
    check("clang wrappers that are scripts are accepted", _wrap_ok[0] == 0)
    check("clang wrapper rejection runs: a non-script wrapper is refused",
          _wrap_text[0] != 0 and "clang wrapper is not a script" in _wrap_text[1])
    check("clang wrapper rejection runs: a missing wrapper is refused",
          _wrap_miss[0] != 0 and "missing clang wrapper" in _wrap_miss[1])
    check("clang wrapper rejection runs: a directory wrapper is refused",
          _wrap_dir[0] != 0 and "clang wrapper is not a regular file" in _wrap_dir[1])
    _wrap_mut_script = _script.replace(
        '  if ! head -n 1 "$wrap" | grep -q \'^#!\'; then\n'
        '    log "clang wrapper is not a script: $wrap"\n'
        '    exit 1\n'
        '  fi\n',
        "",
        1,
    )
    _wrap_mut = _region_between(
        _wrap_mut_script, 'for wrap in "$CC" "$CXX"; do\n', 'log "clang wrappers exist and are scripts"\n')
    _wrap_mut_run = _run_wrap(_wrap_mut, "not a script\n") if _wrap_mut else (1, "")
    _emit_mutation(
        "non-script clang wrapper accepted after the type check is removed",
        "exit 0",
        _wrap_mut_run[0] == 0,
    )
    _clang_if = _region_between(
        _script,
        'if head -n 1 "$CLANG_BIN" | grep -q \'^#!\'; then\n',
        "fi\n",
    )
    _clang_script = _run_clang_bin(_clang_if, True) if _clang_if else (0, "")
    _clang_elf = _run_clang_bin(_clang_if, False) if _clang_if else (1, "")
    check("resolved clang that is a script is refused",
          _clang_script[0] != 0 and "resolved clang is a wrapper script" in _clang_script[1])
    check("resolved clang that is not a script is accepted", _clang_elf[0] == 0)
    _rows = _compile_rows(_script)
    _c_rows, _cxx_rows = _split_rows(_rows)
    check("C compile argv receives the hardening flags", _rows_have(_c_rows, _COMPILE_FLAGS))
    check("C++ compile argv receives the hardening flags and -fvisibility-inlines-hidden",
          _rows_have(_cxx_rows, _COMPILE_FLAGS + ("-fvisibility-inlines-hidden",)))
    _cxx_mut = _script.replace(
        '"$CXX" "${CXXFLAGS[@]}" -c "$SRC/cros_gralloc/$src"',
        '"$CXX" -c "$SRC/cros_gralloc/$src"',
        1,
    )
    _mut_rows = _compile_rows(_cxx_mut)
    _mut_c_rows, _mut_cxx_rows = _split_rows(_mut_rows)
    _emit_mutation(
        "cxx flags absent from the compiler argv",
        "C++ argv dropped the hardening flags",
        _rows_have(_mut_c_rows, _COMPILE_FLAGS) and not _rows_have(_mut_cxx_rows, _COMPILE_FLAGS),
    )
    _src_region = _sources_writer_region(_script)
    _src_probs = _sources_problems(_src_region)
    if _src_probs:
        print("SOURCES writer:", _src_probs)
    check("OUT/SOURCES.txt carries the output block and literal $HOME", not _src_probs)
    _src_outside = _sources_problems(_src_region, img_under_home=False)
    if _src_outside:
        print("SOURCES writer outside home:", _src_outside)
    check("OUT/SOURCES.txt leaves a path outside $HOME unchanged", not _src_outside)
    _aidl_line = 'echo "path: $(home_literal "$AIDL")"\n'
    if _src_region.count(_aidl_line) != 1:
        check("minigbm mutation anchor once: SOURCES path expands $HOME", False)
    else:
        _src_expand = _sources_problems(_src_region.replace(_aidl_line, 'echo "path: $AIDL"\n', 1))
        _emit_mutation(
            "SOURCES path expands $HOME",
            "expanded home path",
            "expanded home" in _src_expand and any(item.startswith("missing path: $HOME/gbos-phase/fp/tools/bt/") for item in _src_expand),
        )
    _output_line = '  echo "output"\n'
    if _src_region.count(_output_line) != 1:
        check("minigbm mutation anchor once: SOURCES output block removed", False)
    else:
        _src_drop = _sources_problems(_src_region.replace(_output_line, "", 1))
        _emit_mutation(
            "SOURCES output block removed",
            "missing output",
            "missing output" in _src_drop,
        )
    _host_sentence = "No second-host rebuild is recorded."
    if _src_region.count(_host_sentence) != 1:
        check("minigbm mutation anchor once: SOURCES second-host wording", False)
    else:
        _legacy_probs = _sources_problems(_src_region.replace(
            _host_sentence, "This file does not record a second-host rebuild.", 1))
        _emit_mutation(
            "SOURCES second-host wording drifted",
            "missing No second-host rebuild is recorded.",
            any(item == "missing No second-host rebuild is recorded." for item in _legacy_probs),
        )
    _captured = []
    _copy_render = _sources_problems(_src_region, checkout_pins=True, captured=_captured)
    _tree = tmp / "sources-copyback"
    _dest = _tree / "tools" / "minigbm" / "SOURCES.txt"
    _dest.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "tools/minigbm/SOURCES.txt", _dest)
    if _copy_render or len(_captured) != 1 or not _captured[0]:
        print("SOURCES copy-back render:", _copy_render)
        check("generated SOURCES.txt copied over the tracked file passes the checkout checks", False)
    else:
        _dest.write_text(_captured[0], encoding="utf-8")
        _copied = _dest.read_text(encoding="utf-8")
        _copy_problems = _sources_checkout_problems(
            _copied, _script, _c_path.read_bytes(), _patch_path.read_bytes(), _readme, _profile)
        if _copy_problems:
            print("SOURCES copy-back:", _copy_problems)
        check("generated SOURCES.txt copied over the tracked file passes the checkout checks",
              not _copy_problems and _copied == _captured[0])
elif _git_checkout:
    check("minigbm build script is present in this git checkout", False)
    check("amdgpu_linear.c is present in this git checkout", False)
    check("upstream-delta.patch is present in this git checkout", False)
else:
    _skip("minigbm build script enforces the pins, hardening, and status order", _SOURCE_WHY)
    _skip("amdgpu_linear.c backend, linear import, and YV12 stride", _SOURCE_WHY)
    _skip("upstream-delta.patch pin", _SOURCE_WHY)


def _release_archive_paths(text):
    start = text.find('git -C "$ROOT" archive')
    pipe = text.find("| gzip", start)
    if start < 0 or pipe < 0 or " -- " not in text[start:pipe]:
        raise SystemExit("make-release.sh archive path list not found")
    body = text[start:pipe].split(" -- ", 1)[1].replace("\\\n", " ")
    return shlex.split(body)


_rel_maker = ROOT / "tools/release/make-release.sh"
if not _rel_maker.is_file():
    _skip("release-tree profile-test and vendor notice slice",
          "extracted release tree has no make-release.sh")
else:
    _rel_paths = _release_archive_paths(_rel_maker.read_text(encoding="utf-8"))
    _rel = tmp / "release-tree"
    if _rel.exists():
        shutil.rmtree(_rel)
    for _rel_path in _rel_paths:
        _src = ROOT / _rel_path
        _target = _rel / _rel_path
        if not _src.exists():
            check(f"release path exists: {_rel_path}", False)
            continue
        if _src.is_dir():
            shutil.copytree(_src, _target, symlinks=False)
        else:
            _target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_src, _target)
    check("release path list ships minigbm NOTICES and not the build sources",
          (_rel / "tools/minigbm/NOTICES.txt").is_file()
          and not (_rel / "tools/minigbm/build-libminigbm-gralloc.sh").exists()
          and not (_rel / "tools/minigbm/amdgpu_linear.c").exists()
          and not (_rel / "tools/minigbm/upstream-delta.patch").exists())
    _child = subprocess.run(
        [sys.executable, "-B", str(_rel / "tests/builder/profile-test.py")],
        cwd=_rel, capture_output=True, text=True)
    _child_out = _child.stdout + _child.stderr
    for _line in _child_out.splitlines():
        if _line.startswith("SKIP ") or _line.startswith("profile tests:"):
            print("release-tree " + _line)
    check("release-tree profile-test passes",
          _child.returncode == 0 and "profile tests:" in _child_out and ", 0 failed" in _child_out.split("profile tests:")[-1])
    if _child.returncode != 0:
        print("release-tree profile-test tail:", "\n".join(_child_out.splitlines()[-30:]))
    for _need_skip in (
        "SKIP minigbm build script enforces the pins, hardening, and status order (release tree does not ship git-only minigbm build sources)",
        "SKIP amdgpu_linear.c backend, linear import, and YV12 stride (release tree does not ship git-only minigbm build sources)",
        "SKIP upstream-delta.patch pin (release tree does not ship git-only minigbm build sources)",
    ):
        check("release-tree " + _need_skip, _need_skip in _child_out)
    _vtext = (_rel / "builder/vendor.sh").read_text(encoding="utf-8")
    _vstart = _vtext.index("    import gralloc\n    if gralloc.install_gralloc(prof, tree, vmap, place):")
    _vend = _vtext.index('print("amd-hx370: vulkan.radeon.so"', _vstart)
    _vslice = textwrap.dedent(_vtext[_vstart:_vend])
    _rtree, _rvmap = _stock_tree("release-vendor", b"stock-gralloc")
    (_rtree / "etc/init").mkdir(parents=True)
    (_rtree / "etc/init/fwupd.rc").write_bytes(b"fwupd\n")
    _rvmap["etc/init/fwupd.rc"] = {
        "mode": 0o644, "uid": 0, "gid": 0, "caps": None,
        "selinux": "u:object_r:vendor_configs_file:s0",
    }
    _rcalls = []
    _rns = {
        "gralloc": gralloc,
        "prof": {"gralloc": {"sha256": _pinned_sha}},
        "tree": _rtree,
        "vmap": _rvmap,
        "place": _place_like_vendor(_rtree, _rvmap, _rcalls),
        "root": _rel,
        "hashlib": hashlib,
        "print": lambda *a, **k: None,
    }
    os.environ["GBOS_MINIGBM"] = str(_pinned_path)
    _rmsg = ""
    try:
        exec(_vslice, _rns)
    except SystemExit as e:
        _rmsg = str(e)
    finally:
        if _saved_minigbm is None:
            os.environ.pop("GBOS_MINIGBM", None)
        else:
            os.environ["GBOS_MINIGBM"] = _saved_minigbm
    _notice_bytes = (_rel / "tools/minigbm/NOTICES.txt").read_bytes()
    check("release-tree vendor slice installs the shipped NOTICES",
          _rmsg == ""
          and (_rtree / "lib64/libminigbm_gralloc.so").read_bytes() == _pinned
          and (_rtree / "etc/NOTICES-amd-hx370-minigbm.txt").read_bytes() == _notice_bytes
          and any(c[0] == "etc/NOTICES-amd-hx370-minigbm.txt" for c in _rcalls))

shutil.rmtree(tmp, ignore_errors=True)
print(f"profile tests: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

