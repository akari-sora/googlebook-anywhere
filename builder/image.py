#!/usr/bin/env python3
"""Stage 2 of the Googlebook build for one device profile (GBOS_PROFILE, builder/lib/devprofile.py): ESP initrd with the
profile's first-stage changes (KANO: pci-stub loaded first), two systemd-boot entries (quiet, serial-debug) made
from the stock vendor_boot options, the profile and the safety rules (profiles/_safety.toml), and two QEMU self-tests
of the finished USB image:
  1. functional: kernel + initrd + the serial-debug options handed to QEMU directly, Android checked on the serial log;
  2. boot chain: firmware -> the image's own ESP -> systemd-boot -> the default entry, checked by disk writes.
The image is built as <name>.partial and renamed to <name> only when every applicable check passes.
Without a QEMU GPU, framework completion/stability are NOT_RUN; all other checks remain mandatory."""
import hashlib, json, os, re, shutil, socket, struct, subprocess, sys, tempfile, time, uuid
from pathlib import Path
if not __debug__:
    sys.exit("builder/image.py checks the safety options with assert: run it without python -O / PYTHONOPTIMIZE")
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import devprofile
import build_record
import usb_smoke
from qemu_write_trace import nvme_trace_safe, userdata_write_bytes
GBOS_OUT = Path(os.environ["GBOS_OUT"])
SRC = GBOS_OUT / "vendor/stage-vendor.img"
PROFILE_DIR = Path(os.environ.get("GBOS_PROFILE") or Path(__file__).resolve().parent.parent / "profiles/kano")
PROFILE_BYTES = (PROFILE_DIR / "profile.toml").read_bytes()
PROF = devprofile.load(PROFILE_DIR)
if (PROFILE_DIR / "profile.toml").read_bytes() != PROFILE_BYTES:
    sys.exit("profile changed during selection")
DROP = devprofile.first_stage_drop(PROF)  # stage-1 modules the final initrd drops (QEMU self-test below)
if PROF["model"]["status"] != "verified":
    print(f"WARNING: profile {PROF['model']['id']} is {PROF['model']['status']!r}, not verified on its hardware "
          "(profiles/<model>/profile.toml model.status)", flush=True)
NAME = os.environ.get("GBOS_IMAGE_NAME") or PROF["model"]["image_name"]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.img", NAME):
    sys.exit(f"GBOS_IMAGE_NAME must be a plain file name ending in .img, not {NAME!r}")
OUT = GBOS_OUT / "image"; OUT.mkdir(parents=True, exist_ok=True)
FINAL = OUT / NAME
IMG = OUT / (NAME + ".partial")
PCI_STUB = Path(os.environ["GBOS_PCI_STUB_KO"])
QEMU = os.environ.get("GBOS_QEMU", "/usr/bin/qemu-system-x86_64")
OVMF_CODE = os.environ.get("GBOS_OVMF_CODE", "/usr/share/edk2/x64/OVMF_CODE.4m.fd")
OVMF_VARS = os.environ.get("GBOS_OVMF_VARS", "/usr/share/edk2/x64/OVMF_VARS.4m.fd")
QEMU_LIB = os.environ.get("GBOS_QEMU_LIB", "")
QEMU_SECONDS = int(os.environ.get("GBOS_QEMU_SECONDS", "200"))
DEFAULT_ENTRY = os.environ.get("GBOS_DEFAULT_ENTRY", "lapis-quiet.conf")
EDITOR = os.environ.get("GBOS_LOADER_EDITOR", "no")
ADB = bool(os.environ.get("GBOS_ADB_PUBKEY"))
SKIP_SELFTEST = os.environ.get("GBOS_SKIP_SELFTEST") == "1"
USB_SMOKE = os.environ.get("GBOS_USB_SMOKE", "") == "1"
try:
    usb_smoke.validate_options(PROF, os.environ)
except (ValueError, OSError) as exc:
    sys.exit(str(exc))
GENERIC = PROF["display"]["stack"] == "gop-swiftshader"   # GOP framebuffer + SwiftShader (profiles/generic-x86)
AMD_HX370 = PROF["model"]["id"] == "amd-hx370"
# Both generic-x86 and amd-hx370's safe default entry use the GOP framebuffer + SwiftShader path in QEMU self-tests.
GOP = GENERIC or AMD_HX370
def parse_gbos_ec(raw, model_id):
    """GBOS_EC for one profile.

    None (the variable is unset) on KANO is typec, the default image. None on any other profile is no
    features. "" and "none" are the explicit opt-out (no features) on every profile. drain is exclusive.
    A sensors/typec/buttons list is KANO only. Duplicates, empty items and unknown items are refused.
    build.sh and vendor.sh apply the same rule before this stage.
    """
    if raw is None:
        raw = "typec" if model_id == "kano" else "none"
    if not isinstance(raw, str):
        raise SystemExit("GBOS_EC must be empty, none, drain, or a comma list of sensors, typec, buttons")
    if raw in ("", "none"):
        features = []
    elif raw == "drain":
        features = ["drain"]
    elif re.fullmatch(r"(sensors|typec|buttons)(,(sensors|typec|buttons))*", raw):
        features = raw.split(",")
        if len(features) != len(set(features)):
            raise SystemExit(
                "GBOS_EC must be empty, none, drain, or a comma list of sensors, typec, buttons, not " + raw)
    else:
        raise SystemExit(
            "GBOS_EC must be empty, none, drain, or a comma list of sensors, typec, buttons, not " + raw)
    if features and model_id != "kano":
        raise SystemExit("GBOS_EC is audited for the KANO only")
    return features


# KANO default is typec (USB-C USB3/DisplayPort). GBOS_EC=none or an explicit empty value opts out.
# Other profiles stay with no EC features unless they set a value, and any feature value is still refused.
# Each feature releases an audited set of ChromeOS EC kernel drivers (docs/SAFETY.md, "EC 功能變體").
# drain is not a feature: it releases only cros_ec and cros_ec_lpcs. The EC userspace channels and every
# EC writer stay blocked in every variant, including thunderbolt, cros_typec_switch, cros_ec_ucsi and
# cros_usbpd_charger. KANO only.
EC_FEATURES = parse_gbos_ec(os.environ.get("GBOS_EC"), PROF["model"]["id"])
if EC_FEATURES == ["drain"]:
    _EC_FOR_KBD = "drain"
elif not EC_FEATURES:
    _EC_FOR_KBD = "none"
else:
    _EC_FOR_KBD = ",".join(EC_FEATURES)
try:
    devprofile.check_kbd_backlight_inputs(PROF, _EC_FOR_KBD, os.environ, Path(__file__).resolve().parent)
except (ValueError, OSError) as exc:
    sys.exit(str(exc))
KMOD_KBD_BACKLIGHT = "gbos_kbd_backlight" in PROF["kmods"]
KMOD_AC = "ac" in PROF["kmods"]   # vendor carries ac.ko (builder/kmod/build-ac.sh; builder/vendor.sh checks the pin)
KMOD_WAKEFILTER = "gbos_wakefilter" in PROF["kmods"]   # vendor carries gbos_wakefilter.ko (build-gbos_wakefilter.sh)
# Developer-only. Empty = no auto-off service (install.sh refuses a non-empty value). When set, QEMU has no
# /data/kano-autooff.nonce, so the serial log must show the rc parsed, the nonce gate staying on, and the
# oneshot exited 0. When empty, the serial log must not mention the service.
TEST_AUTOOFF = os.environ.get("GBOS_TEST_AUTOOFF", "")
if any(os.path.lexists(str(FINAL) + suffix) for suffix in ("", ".sha256", ".build-record.json", ".not-self-tested")) :
    sys.exit(f"refusing: {FINAL} already exists")
ENTRIES = ("lapis-debug.conf", "lapis-quiet.conf")
assert DEFAULT_ENTRY in ENTRIES, DEFAULT_ENTRY
assert EDITOR in ("yes", "no"), EDITOR

def run(*a):
    print("+", " ".join(map(str, a)), flush=True); subprocess.run(list(map(str, a)), check=True)
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(16 << 20), b""): h.update(b)
    return h.hexdigest()

def ally_bootstrap_initrd(prof, data, *, verify=False):
    """Explicit Ally-only repair, preserving the ESP's complete bootconfig footer."""
    if not prof.get("cpu_compat", {}).get("patch_ally_bootstrap", False):
        return data, None
    import ally_bootstrap
    if not ally_bootstrap.enabled(prof):
        raise ValueError("Ally bootstrap opt-in not enabled")
    if len(data) < 20 or not data.endswith(b"#BOOTCONFIG\n"):
        raise ValueError("Ally initrd bootconfig footer missing")
    size, checksum = struct.unpack("<II", data[-20:-12])
    if not 0 < size <= len(data) - 20:
        raise ValueError("Ally initrd bootconfig size invalid")
    start = len(data) - 20 - size
    config = data[start:-20]
    if sum(config) & 0xFFFFFFFF != checksum:
        raise ValueError("Ally initrd bootconfig checksum mismatch")
    body = data[:start]
    if verify:
        receipt = ally_bootstrap.verify_archive(body)
        updated = data
    else:
        body, receipt = ally_bootstrap.patch_archive(body)
        updated = body + data[start:]
    receipt = dict(receipt, source_identity=ally_bootstrap.source_identity(),
                   bootconfig_sha256=hashlib.sha256(data[start:]).hexdigest(),
                   input_initrd_sha256=hashlib.sha256(data).hexdigest(),
                   output_initrd_sha256=hashlib.sha256(updated).hexdigest())
    return updated, receipt

def check_ally_bootstrap_continuity(preparation, readback):
    """Module-list edits may move offsets, never target metadata or bootconfig."""
    for field in ("members", "bootconfig_sha256", "source_identity"):
        if preparation[field] != readback[field]:
            raise ValueError("Ally bootstrap final ESP continuity mismatch: " + field)

def gpt(path):
    with open(path, "rb") as f:
        f.seek(512); h = f.read(92)
        if h[:8] != b"EFI PART": sys.exit(f"{path}: no GPT")
        lba, n, esz = struct.unpack_from("<QII", h, 72)
        f.seek(lba * 512); t = f.read(n * esz)
    parts = {}
    for i in range(n):
        e = t[i * esz:(i + 1) * esz]
        if e[:16] == bytes(16): continue
        first, last = struct.unpack_from("<QQ", e, 32)
        parts[e[56:128].decode("utf-16le").rstrip("\0")] = dict(
            type=str(uuid.UUID(bytes_le=e[:16])), guid=str(uuid.UUID(bytes_le=e[16:32])), start=first * 512, end=(last + 1) * 512)
    return parts
PARTS = gpt(SRC)
if PARTS.get("EFI-SYSTEM", {}).get("type") != "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": sys.exit("no EFI-SYSTEM partition in the base image")
ESP = f"@@{PARTS['EFI-SYSTEM']['start']}"
USERDATA = PARTS["userdata"]
def mtype(img, path):
    return subprocess.run(["mtype", "-i", f"{img}{ESP}", path], check=True, capture_output=True, text=True).stdout.replace("\r", "")

def ally_storage_module(prof, checked, kernel_dir):
    """Load only the known Ally first-stage USB BOT module with checked kernel ABI."""
    if prof["model"]["id"] != "rog-ally":
        return None
    required = {"CONFIG_USB_STORAGE": "m", "CONFIG_SCSI": "y", "CONFIG_BLK_DEV_SD": "y"}
    if checked is None or any(checked.get(k) != v for k, v in required.items()):
        raise ValueError("Ally USB storage kernel config mismatch")
    root = Path(kernel_dir)
    ko, dep, symvers = root / "modules/usb-storage.ko", root / "modules/modules.dep", root / "Module.symvers"
    for path in (ko, dep, symvers):
        if path.is_symlink() or not path.is_file(): raise ValueError("missing Ally storage input: " + str(path))
    content = ko.read_bytes()
    if hashlib.sha256(content).hexdigest() != "f429104c8803c8156f8278d37b08147d11eb3f287b58320a10fda9bf286fffd4":
        raise ValueError("Ally storage module pin mismatch")
    lines = [x for x in dep.read_text().splitlines() if x.split(":", 1)[0] == "usb-storage.ko"]
    if lines != ["usb-storage.ko:"]:
        raise ValueError("Ally storage dependencies changed")
    # This pinned in-tree module intentionally exports usb_stor symbols. The
    # generic checker rejects exports, but all its other checks remain mandatory.
    checked_module = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "kmod/check-ko.py"),
                                     str(ko), str(symvers), "SMP preempt mod_unload modversions "],
                                    capture_output=True, text=True)
    failures = [line for line in checked_module.stdout.splitlines() if line.startswith("FAIL ")]
    if checked_module.returncode != 1 or failures != ["FAIL the module exports symbols"] or checked_module.stderr:
        raise ValueError("Ally USB storage ABI verification failed")
    if ko.read_bytes() != content: raise ValueError("Ally storage module changed during ABI check")
    return content

# initrd: drop the LPSS UART drivers from first-stage modules.load, keep the bootconfig as built
IMG.unlink(missing_ok=True)
run("cp", "--reflink=auto", SRC, IMG)
if USB_SMOKE:
    # Work on the private staged copy, never on the caller's base image.
    # A separate pre-write verifier must still compare the result to live NVMe.
    old_parts = PARTS
    run("sgdisk", "--randomize-guids", IMG)
    run("sgdisk", "--verify", IMG)
    PARTS = usb_smoke.validate_gpt_rewrite(old_parts, gpt(IMG))
    USERDATA = PARTS["userdata"]
run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/initrd.img", OUT / "src-initrd.img")
src = (OUT / "src-initrd.img").read_bytes()
if USB_SMOKE:
    src = usb_smoke.rebind_bootconfig(src, old_parts["boot_a"]["guid"], PARTS["boot_a"]["guid"])
src, ally_bootstrap_preparation = ally_bootstrap_initrd(PROF, src)
assert src.endswith(b"#BOOTCONFIG\n")
size, _ = struct.unpack("<II", src[-20:-12]); bc = src[-20 - size:]; body = src[:-20 - size]
digest = (GBOS_OUT / "vendor/logs/vbmeta.digest.txt").read_text().split()[0]
assert re.fullmatch(r"[0-9a-f]{64}", digest) and f"androidboot.vbmeta.digest={digest}\n".encode() in bc, \
    "ESP bootconfig must carry the re-signed vbmeta digest"
STUB = bool(PROF["cmdline"]["pci_stub_ids"])
# Bind first-stage treatment to the embedded config of the exact shipped kernel.
DEV_KERNEL_DIR = os.environ.get("GBOS_DEV_KERNEL_DIR", "")
initrd_checked_kernel = devprofile.check_kconfig(PROF, DEV_KERNEL_DIR) if DEV_KERNEL_DIR else None
storage_ko = ally_storage_module(PROF, initrd_checked_kernel, DEV_KERNEL_DIR)
if storage_ko is not None and STUB: raise ValueError("Ally storage with pci stub unsupported")
storage_load = storage_dep = storage_added = 0
builtin_mfd = bool(initrd_checked_kernel and initrd_checked_kernel.get("CONFIG_MFD_CORE") == "y")
mfd_depchanged = 0
out = bytearray(); off = 0; changed = 0; added = 0; depchanged = 0
while off < len(body):
    if body[off:off+6] != b"070701":
        nxt = body.find(b"070701", off)
        if nxt < 0: out += body[off:]; break
        out += body[off:nxt]; off = nxt
    h = bytearray(body[off:off+110]); ns = int(h[94:102], 16); fs = int(h[54:62], 16)
    nend = (off + 110 + ns + 3) & ~3; name = body[off+110:off+110+ns-1].decode(); data = body[nend:nend+fs]
    nxt = (nend + fs + 3) & ~3
    if storage_ko is not None and name == "lib/modules/usb-storage.ko":
        raise ValueError("duplicate USB storage module")
    if name == "lib/modules/modules.load":
        load_text = devprofile.stage2_modules_load(data.decode(), PROF)
        if builtin_mfd:
            load_text = devprofile.drop_builtin_mfd_load(load_text)
        if storage_ko is not None:
            if "usb-storage.ko" in load_text.splitlines(): raise ValueError("duplicate USB storage load")
            load_text += "usb-storage.ko\n"
            storage_load += 1
        data = load_text.encode()
        if USB_SMOKE and any("nvme" in x.lower() for x in data.decode().splitlines()):
            sys.exit("USB smoke first-stage modules.load contains NVMe")
        h[54:62] = b"%08X" % len(data); changed += 1
        out += h + body[off+110:nend] + data + b"\0" * ((-len(data)) % 4)
        if storage_ko is not None:
            nm = b"lib/modules/usb-storage.ko\0"
            fields = [0x7FFF0002, 0o100644, 0, 0, 1, int(h[46:54], 16), len(storage_ko), 0, 0, 0, 0, len(nm), 0]
            hdr = b"070701" + b"".join(b"%08X" % v for v in fields)
            out += hdr + nm + b"\0" * ((-(110 + len(nm))) % 4) + storage_ko + b"\0" * ((-len(storage_ko)) % 4)
            storage_added += 1
        if not STUB:
            off = nxt; continue
        # new entry: lib/modules/pci-stub.ko (first stage loads it before ueventd can load intel-lpss-pci)
        ko = PCI_STUB.read_bytes()
        nm = b"lib/modules/pci-stub.ko\0"
        fields = [0x7FFF0001, 0o100644, 0, 0, 1, int(h[46:54], 16), len(ko), 0, 0, 0, 0, len(nm), 0]
        hdr = b"070701" + b"".join(b"%08X" % v for v in fields)
        out += hdr + nm + b"\0" * ((-(110 + len(nm))) % 4) + ko + b"\0" * ((-len(ko)) % 4)
        added += 1
    elif name == "lib/modules/modules.dep" and (STUB or builtin_mfd or storage_ko is not None):
        if builtin_mfd:
            data = devprofile.drop_builtin_mfd_dependencies(data.decode()).encode()
            mfd_depchanged += 1
        if storage_ko is not None:
            if b"usb-storage.ko" in data: raise ValueError("duplicate USB storage dependency")
            data += b"/lib/modules/usb-storage.ko:\n"
            storage_dep += 1
        if STUB:
            data = data + b"/lib/modules/pci-stub.ko:\n"
        h[54:62] = b"%08X" % len(data); depchanged += int(STUB)
        out += h + body[off+110:nend] + data + b"\0" * ((-len(data)) % 4)
    else:
        if builtin_mfd and name in ("lib/modules/modules.alias", "lib/modules/modules.softdep"):
            assert b"mfd-core" not in data and b"mfd_core" not in data, "unexpected mfd alias/softdep"
        out += body[off:nxt]
    off = nxt
assert mfd_depchanged == int(builtin_mfd)
assert storage_load == storage_dep == storage_added == int(storage_ko is not None)
assert changed == 1 and added == depchanged == (1 if STUB else 0), (changed, added, depchanged)
(OUT / "initrd.img").write_bytes(bytes(out) + bc)

# entries: the reviewed vendor_boot options shared by lapis builds 16452207/16658739, the profile's
# additions and the project-wide safety options (builder/lib/devprofile.py make_entries)
VBOOT = PARTS["vendor_boot_a"]
with open(SRC, "rb") as f:
    f.seek(VBOOT["start"]); vh = f.read(4096)
assert vh[:8] == b"VNDRBOOT" and struct.unpack_from("<I", vh, 8)[0] == 4, "vendor_boot_a: not a v4 vendor boot image"
vcmd = vh[28:28 + 2048].split(b"\0")[0]
assert hashlib.sha256(vcmd).hexdigest() == "311e95df564037a863af096fb6265f15e8340e4b60e2736676e43d81d876aee7", \
    "vendor_boot_a command line is not the reviewed command line of supported lapis builds"
E = devprofile.make_entries(PROF, vcmd.decode())
normal, debug, quiet = E["normal"], E["debug"], E["quiet"]
# GBOS_EC: the EC transport/core plus each feature's drivers leave the blacklist; nothing they send writes EC flash,
# CBI, I2C, firmware or reboots the EC (audit: docs/SAFETY.md). cros_ec_dev still adds the cros-ec-chardev, -sysfs,
# -debugfs, -hwmon, -lightbar, -vbc, -charge-control, -usbpd-charger/-logger, -pchg, -led and -gpio/-rtc/-wdt cells; their
# drivers stay blacklisted, so /dev/cros_ec and every EC write interface stay absent. Two drivers outside the released
# set then send EC commands too: cros_kbd_led_backlight (allowed in every entry for the keyboard backlight) binds the
# cros-keyboard-leds cell when the EC reports EC_FEATURE_PWM_KEYB and sends PWM_GET/SET_KEYBOARD_BACKLIGHT (EC RAM only;
# the cell cannot be blocked on its own), and cros_ec_typec's VDM code sends TYPEC_VDM_RESPONSE (a read).
EC_CORE = ["cros_ec", "cros_ec_lpcs", "cros_ec_dev"]
EC_RELEASE = {"sensors": ["cros_ec_sensorhub", "cros_ec_sensors_core", "cros_ec_sensors", "cros_ec_lid_angle"],
              "typec": ["cros_ec_typec", "cros_usbpd_notify"],
              "buttons": ["cros_ec_keyb"]}
# not in this lapis build's vendor_dlkm; blocked in the EC variants so a future vendor_dlkm cannot add an EC writer
EC_EXTRA_BLOCK = ["gpio_cros_ec", "cros_ec_hwmon", "cros_ec_wdt", "rtc_cros_ec", "cros_ec_uart", "cros_ec_rpmsg"]
# must stay blocked in every variant: EC userspace channels, EC writers, other MCUs, TPM, SPI flash, firmware NVRAM
EC_NEVER = ("cros_ec_chardev", "cros_ec_debugfs", "cros_ec_sysfs", "cros_ec_vbc", "cros_ec_lightbar", "i2c_cros_ec_tunnel",
            "cros_charge_control", "leds_cros_ec", "cros_ec_spi", "cros_ec_i2c", "cros_ec_ishtp", "cros_ec_usb",
            "cros_peripheral_charger", "cros_usbpd_charger", "cros_usbpd_logger", "cros_typec_switch", "cros_ec_ucsi",
            "tpm", "tpm_tis", "tpm_tis_core", "tpm_tis_i2c_cr50", "spi_intel_pci", "spi_intel", "thunderbolt", "i2c_dev",
            "gsmi", "nvram", "mtd", "spi_nor", "spidev", "chromeos_acpi")
# drain is not a feature and must not use EC_CORE: that set includes cros_ec_dev.
EC_DRAIN = {"cros_ec", "cros_ec_lpcs"}
DRAIN_ONLY = EC_FEATURES == ["drain"]

def ec_released(features):
    """Modules that leave module_blacklist. drain releases the transport/core pair only."""
    if features == ["drain"]:
        return set(EC_DRAIN)
    if not features:
        return set()
    return set(EC_CORE + [m for f in features for m in EC_RELEASE[f]])

def ec_blacklist(options, features):
    """Drop released modules from each module_blacklist= and append EC_EXTRA_BLOCK. Empty features: unchanged."""
    released_now = ec_released(features)
    if not features:
        return list(options)
    out = []
    for x in options:
        if x.startswith("module_blacklist="):
            ms = x.split("=", 1)[1].split(",")
            x = "module_blacklist=" + ",".join(
                [m for m in ms if m not in released_now] + [m for m in EC_EXTRA_BLOCK if m not in ms])
        out.append(x)
    return out

# Before the EC rewrite, KANO serial-debug is 1879 bytes and quiet is 1787 (docs/STATUS-KANO.md).
# typec (the KANO default) drops five blacklist names and appends EC_EXTRA_BLOCK, a net +13 bytes:
# debug 1892, quiet 1800, both inside the 1909 limit (17 bytes of builder slack on debug).
# drain removes only cros_ec and cros_ec_lpcs and appends the same six names, a net +57 bytes (1936),
# which fails cmdline_within_limit. The quiet entry already omits these two verbosity tokens.
# Dropping them on the drain serial-debug entry lands on 1909, so the 64-byte kano-boot.sh
# headroom stays free. earlycon (the only serial console), keep_bootcon and printk.devkmsg=on stay.
DRAIN_DEBUG_DROP = ("ignore_loglevel", "loglevel=8")

def ec_variant_entries(normal, debug, quiet, features):
    """Blacklist rewrite for one GBOS_EC value. drain also drops two debug verbosity tokens so the line fits."""
    normal, debug, quiet = (ec_blacklist(normal, features), ec_blacklist(debug, features),
                            ec_blacklist(quiet, features))
    if features == ["drain"]:
        debug = [o for o in debug if o not in DRAIN_DEBUG_DROP]
    return normal, debug, quiet

released = ec_released(EC_FEATURES)
assert released <= set(PROF["blacklist"]["modules"]) and not released & set(EC_NEVER), released
base_debug, base_quiet = list(debug), list(quiet)
normal, debug, quiet = ec_variant_entries(normal, debug, quiet, EC_FEATURES)
def entry(title, o): return f"title {title}\nlinux /kernel.efi\ninitrd /initrd.img\noptions {' '.join(o)}\n"
SERIAL = devprofile.SERIAL
STUB_OPT = "pci-stub.ids=" + ",".join(PROF["cmdline"]["pci_stub_ids"])
assert (not STUB or STUB_OPT in quiet) and not any(o.startswith(SERIAL) for o in quiet)
# quiet differs from the serial-debug entry only by the serial console/log-level parameters and the profile's
# optional quiet_drop / quiet_add (used by amd-hx370 so the default entry is the safe GOP + SwiftShader path).
debug_nobl = [o for o in debug if not o.startswith("module_blacklist=")]
quiet_nobl = [o for o in quiet if not o.startswith("module_blacklist=")]
assert sorted(set(debug_nobl) ^ set(quiet_nobl)) == sorted(
    set(o for o in debug_nobl if o.startswith(SERIAL))
    | {"keep_bootcon", "printk.devkmsg=on", "loglevel=4"}
    | (set(debug_nobl) & {"ignore_loglevel", "loglevel=8"})
    | set(PROF["cmdline"]["quiet_drop"])
), set(debug_nobl) ^ set(quiet_nobl)
debug_bls = [x for x in debug if x.startswith("module_blacklist=")][0].split("=", 1)[1].split(",")
quiet_bls = [x for x in quiet if x.startswith("module_blacklist=")][0].split("=", 1)[1].split(",")
assert quiet_bls == debug_bls + [m for m in PROF["blacklist"]["quiet_add"] if m not in debug_bls and m not in released], (debug_bls, quiet_bls)
if AMD_HX370:
    assert "amdgpu" in quiet_bls and "amdgpu" not in debug_bls, (quiet_bls, debug_bls)
    assert not any(o.startswith("amdgpu.") for o in quiet), quiet
    assert {"amdgpu.modeset=1", "amdgpu.dc=1", "amdgpu.dcdebugmask=0x10"} <= set(debug), debug
SAFE = PROF["_safety"]
# EC_NEVER is part of the project-wide required blacklist, so every entry of every profile and variant blocks it
assert set(EC_NEVER) <= set(SAFE["blacklist"]["required"]), set(EC_NEVER) - set(SAFE["blacklist"]["required"])
# a profile for other machines must not carry a machine-specific MMIO console (it would write that address)
assert not GOP or not any(o.split("=", 1)[0] in ("console", "earlycon") and "mmio" in o for o in normal + debug + quiet)
for name, o in (("debug", debug), ("quiet", quiet)):
    n = devprofile.boot_cmdline_len(o)
    limit = devprofile.X86_BOOT_CMDLINE_MAX - devprofile.CMDLINE_HEADROOM
    slack = devprofile.X86_BOOT_CMDLINE_MAX - n
    print(f"{name} command line length {n}; kernel max {devprofile.X86_BOOT_CMDLINE_MAX}; "
          f"headroom {devprofile.CMDLINE_HEADROOM}; limit {limit}; slack {slack}")
    assert devprofile.cmdline_within_limit(n), name
    bl = [x for x in o if x.startswith("module_blacklist=")]; assert len(bl) == 1
    bls = bl[0].split("=", 1)[1].split(",")
    for m in SAFE["blacklist"]["required"]:
        assert m in bls or m in released, (name, m)
    assert set(EC_NEVER) <= set(bls), (name, set(EC_NEVER) - set(bls))
    assert not EC_FEATURES or (not released & set(bls) and set(EC_EXTRA_BLOCK) <= set(bls)), name
    if DRAIN_ONLY:
        base_opts = {"debug": base_debug, "quiet": base_quiet}[name]
        base_bl = next(x for x in base_opts if x.startswith("module_blacklist=")).split("=", 1)[1].split(",")
        # Exactly the two transport modules leave. cros_ec_dev is not in EC_NEVER (feature variants need it).
        assert set(bls) == (set(base_bl) - {"cros_ec", "cros_ec_lpcs"}) | set(EC_EXTRA_BLOCK), (name, bls)
        assert {"cros_ec_dev", "cros_ec_keyb"} <= set(bls), (name, bls)
        assert set(EC_NEVER) <= set(bls), (name, set(EC_NEVER) - set(bls))
        assert not ({"cros_ec", "cros_ec_lpcs"} & set(bls)), (name, bls)
    for m in PROF["blacklist"]["allow"]:
        assert m not in bls, (name, m)
    if name == "quiet":
        assert set(PROF["blacklist"]["quiet_add"]) <= set(bls), (name, PROF["blacklist"]["quiet_add"])
    assert "efi=noruntime" in o and not any(x.startswith("spi-intel.") for x in o), name
    assert [x for x in o if x.startswith("efi=")] == ["efi=noruntime"], name
if USB_SMOKE:
    usb_smoke.validate_entries(debug, quiet, EC_EXTRA_BLOCK, EC_NEVER,
                               devprofile.cmdline_within_limit, devprofile.boot_cmdline_len)
(OUT / "lapis-quiet.conf").write_text(entry(usb_smoke.QUIET_TITLE if USB_SMOKE else PROF["entries"]["quiet"], quiet))
(OUT / "lapis-debug.conf").write_text(entry(usb_smoke.DEBUG_TITLE if USB_SMOKE else PROF["entries"]["debug"], debug))
# editor no (default): the menu cannot edit an entry's options, so the blacklist cannot be removed by a stray key;
# auto-firmware no: no "Reboot Into Firmware" menu item (it sets the OsIndications EFI variable)
(OUT / "loader.conf").write_text(f"default {DEFAULT_ENTRY}\ntimeout 5\neditor {EDITOR}\nauto-firmware no\n")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "initrd.img", "::/initrd.img")
# the base ESP's own lapis.conf (Google's options, without the safety blacklist) is deleted: only the two entries built
# here can be started. The former "plain" entry (no pci-stub) left the KANO panel dark while Android ran (its serial log
# stops when the LPSS UART is taken over; docs/JOURNAL.md, research/kano-compat/PLAN.md 2.5-E), so it is not built any more
run("mdel", "-i", f"{IMG}{ESP}", "::/loader/entries/lapis.conf")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "lapis-debug.conf", "::/loader/entries/lapis-debug.conf")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "lapis-quiet.conf", "::/loader/entries/lapis-quiet.conf")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "loader.conf", "::/loader/loader.conf")
for f in ENTRIES:
    assert mtype(IMG, f"::/loader/entries/{f}") == (OUT / f).read_text(), f
listing = subprocess.run(["mdir", "-b", "-i", f"{IMG}{ESP}", "::/loader/entries"], check=True, capture_output=True,
                         text=True).stdout.split("\n")
present = sorted(l.strip().replace("\r", "").rsplit("/", 1)[-1] for l in listing if l.strip() and not l.strip().endswith("/"))
assert present == sorted(ENTRIES), f"ESP loader/entries holds {present}, expected exactly {sorted(ENTRIES)}"
assert mtype(IMG, "::/loader/loader.conf") == (OUT / "loader.conf").read_text()
run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/initrd.img", OUT / "readback-initrd.img")
assert (OUT / "readback-initrd.img").read_bytes() == (OUT / "initrd.img").read_bytes()
_, ally_bootstrap_readback = ally_bootstrap_initrd(PROF, (OUT / "readback-initrd.img").read_bytes(), verify=True)
if ally_bootstrap_readback is not None:
    check_ally_bootstrap_continuity(ally_bootstrap_preparation, ally_bootstrap_readback)
    proof = {"preparation": ally_bootstrap_preparation, "esp_readback": ally_bootstrap_readback,
             "scope": "four bootstrap members only; not whole-image CPU or hardware acceptance"}
    (OUT / "ally-bootstrap.json").write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
    with (GBOS_OUT / "build-info.txt").open("a") as stream:
        stream.write("ally-bootstrap " + json.dumps(proof, sort_keys=True) + "\n")
def install_checked_kernel(prof, kernel_dir, debug_opts, quiet_opts, read_bzimage, copy_kernel, read_shipped):
    """GBOS_DEV_KERNEL_DIR checks in this file, then the ESP copy of kernel.efi.

    The build calls this. Tests call it with stub readers: dropping the call, skipping
    either SHA-256 check, or turning the length check into a constant fails those tests.
    read_bzimage() is the bzImage after check_kconfig. copy_kernel(path) writes the ESP.
    read_shipped() is the kernel.efi readback. debug_opts is the serial entry.
    """
    checked = devprofile.check_kconfig(prof, kernel_dir)
    dev_bz = Path(kernel_dir) / "bzImage"
    kb = read_bzimage()
    try:
        devprofile.require_same_bzimage(
            kb, checked.bzimage_sha256, f"{dev_bz} changed after check-kconfig read it"
        )
    except devprofile.ProfileError as e:
        sys.exit(str(e))
    assert kb[:2] == b"MZ" and kb[0x202:0x206] == b"HdrS", f"{dev_bz} is not an EFI-stub bzImage"
    # arch/x86/kernel/setup.c strlcat's the builtin CONFIG_CMDLINE in front of this
    # line. The 2000-character check above does not see that prefix.
    builtin_cmd = devprofile.builtin_cmdline_text(checked)
    for name, o in (("serial", debug_opts), ("quiet", quiet_opts)):
        n = len(" ".join(o)) + len("initrd=\\initrd.img ")
        assert devprofile.x86_cmdline_fits(n, builtin_cmd), (
            f"{name} command line ({n}) plus builtin CONFIG_CMDLINE ({len(builtin_cmd)}) "
            f"does not fit in x86 COMMAND_LINE_SIZE ({devprofile.X86_COMMAND_LINE_SIZE})"
        )
    copy_kernel(dev_bz)
    shipped = read_shipped()
    try:
        devprofile.require_same_bzimage(
            shipped, checked.bzimage_sha256, "kernel.efi is not the bzImage check-kconfig verified"
        )
    except devprofile.ProfileError as e:
        sys.exit(str(e))
    return checked


def _extract_vendor_gralloc(img, work):
    """Bytes of lib64/libminigbm_gralloc.so inside the image's vendor_a."""
    work = Path(work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    ota = Path(os.environ["GBOS_OTATOOLS"])
    run(sys.executable, str(Path(__file__).resolve().parent / "lib/lp-dump.py"), img, work, "vendor_a")
    raw = (work / "vendor_a.img").read_bytes()
    idx = raw.rfind(b"AVBf")
    if idx < 0:
        sys.exit("vendor_a has no AVB footer")
    orig = struct.unpack_from(">Q", raw, idx + 12)[0]
    erofs = work / "vendor.erofs"
    erofs.write_bytes(raw[:orig])
    tree = work / "tree"
    tree.mkdir()
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(ota / "lib64")
    subprocess.run(
        [str(ota / "bin/fsck.erofs"), f"--extract={tree}", "--overwrite", str(erofs)],
        check=True, env=env)
    lib = tree / "lib64/libminigbm_gralloc.so"
    if not lib.is_file() or lib.is_symlink():
        sys.exit("image vendor libminigbm_gralloc.so missing")
    return lib.read_bytes()

def readback_gralloc(img, prof, work, extract):
    """Refuse when the vendor gralloc inside the image is not the profile pin."""
    import gralloc
    g = prof.get("gralloc")
    if not g:
        return None
    try:
        blob = extract(img, work)
        err = gralloc.readback_mismatch(blob, g["sha256"])
    finally:
        if Path(work).exists():
            shutil.rmtree(work, ignore_errors=True)
    if err:
        sys.exit(err)
    print("gralloc readback", g["sha256"])
    return g["sha256"]

DEV_KERNEL_DIR = os.environ.get("GBOS_DEV_KERNEL_DIR", "")
if DEV_KERNEL_DIR:
    def _read_checked_bz():
        return (Path(DEV_KERNEL_DIR) / "bzImage").read_bytes()

    def _copy_kernel(dev_bz):
        run("mcopy", "-o", "-i", f"{IMG}{ESP}", dev_bz, "::/kernel.efi")

    def _read_shipped_kernel():
        run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/kernel.efi", OUT / "kernel.efi")
        return (OUT / "kernel.efi").read_bytes()

    installed = install_checked_kernel(
        PROF, DEV_KERNEL_DIR, debug, quiet, _read_checked_bz, _copy_kernel, _read_shipped_kernel
    )
    if installed.bzimage_sha256 != initrd_checked_kernel.bzimage_sha256:
        sys.exit("kernel changed after first-stage module selection")
else:
    run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/kernel.efi", OUT / "kernel.efi")
if KMOD_KBD_BACKLIGHT and hashlib.sha256((OUT / "kernel.efi").read_bytes()).hexdigest() != "7816c5d5b3500093e597b7362ecab103d5a32141b100d3eb7a8b33221fce7f41":
    sys.exit("stock kernel.efi readback does not match pinned GKI")
if PROF.get("gralloc"):
    readback_gralloc(IMG, PROF, OUT / "gralloc-readback", _extract_vendor_gralloc)
print("ESP free:", subprocess.run(["mdir", "-i", f"{IMG}{ESP}", "::/"], capture_output=True, text=True).stdout.strip().splitlines()[-1])

# QEMU runs: image on USB (writes go to a throw-away snapshot), blank NVMe attached, until QEMU_SECONDS have passed.
# QEMU still running at the end means the guest neither crashed nor powered off; disk statistics come over QMP.
blank = OUT / "blank-nvme.img"
with open(blank, "wb") as fh: fh.truncate(1 << 30)
def qmp_query(path, *cmds, timeout=30):
    s = socket.socket(socket.AF_UNIX)
    f = None
    try:
        s.settimeout(timeout); s.connect(str(path)); f = s.makefile("rwb")
        def call(cmd):
            cmd, args = cmd if isinstance(cmd, tuple) else (cmd, None)
            f.write(json.dumps({"execute": cmd, **({"arguments": args} if args else {})}).encode() + b"\n"); f.flush()
            while True:
                r = json.loads(f.readline())
                if "return" in r or "error" in r: return r
        json.loads(f.readline()); call("qmp_capabilities")
        return [call(c) for c in cmds]   # QMP takes one client at a time
    finally:
        try:
            if f is not None: f.close()
        finally:
            s.close()
def functional_wake_check(path, query=qmp_query):
    """Wake only a proven S3 functional-test guest; any ambiguous QMP is failure."""
    try:
        response = query(path, "query-status", timeout=2)[0]
        state = response.get("return")
        if not isinstance(state, dict) or type(state.get("running")) is not bool:
            return False, False
        if state.get("status") == "running" and state["running"] is True:
            return True, False
        if state.get("status") != "suspended" or state["running"] is not False:
            return False, False
        machine = query(path, "query-current-machine", timeout=2)[0].get("return")
        if not isinstance(machine, dict) or machine.get("wakeup-suspend-support") is not True:
            return False, False
        response = query(path, "system_wakeup", timeout=2)[0]
        return (True, True) if response == {"return": {}} else (False, False)
    except (OSError, EOFError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False, False

FUNCTIONAL_QMP_OK = None
QMP_DIR = Path(tempfile.mkdtemp(prefix="gbos-qmp-"))   # unix socket paths are limited to about 108 bytes
def ac_ssdt(path):
    """An SSDT with one ACPI AC adapter, \\_SB.AC (ACPI0003, _PSR = 1: on line), for the functional test of ac.ko;
    QEMU's own ACPI tables have no AC device."""
    def pkg(body):   # PkgLength of a body shorter than 4096 bytes, counting the length bytes themselves
        n = len(body) + 1
        if n < 64: return bytes([n]) + body
        n += 1; assert n < 4096
        return bytes([0x40 | (n & 0xF), n >> 4]) + body
    hid = b"\x08_HID\x0dACPI0003\x00"                       # Name (_HID, "ACPI0003")
    psr = b"\x14" + pkg(b"_PSR\x00\xa4\x01")                  # Method (_PSR) { Return (One) }
    sta = b"\x14" + pkg(b"_STA\x00\xa4\x0a\x0f")              # Method (_STA) { Return (0x0F) }
    dev = b"\x5b\x82" + pkg(b"AC__" + hid + psr + sta)       # Device (AC)
    body = b"\x10" + pkg(b"\\_SB_" + dev)                    # Scope (\_SB)
    hdr = bytearray(b"SSDT" + struct.pack("<IBB", 36 + len(body), 2, 0) + b"GBOS  " + b"KANOAC  " + struct.pack("<I", 1)
                    + b"GBOS" + struct.pack("<I", 1))
    t = bytearray(hdr + body); t[9] = (-sum(t)) & 0xFF
    Path(path).write_bytes(bytes(t))
def qemu(tag, extra, probe_token=None):
    global FUNCTIONAL_QMP_OK
    serial, sock, vars_fd = OUT / f"{tag}-serial.log", QMP_DIR / f"{tag}.qmp", OUT / f"{tag}-vars.fd"
    for p in (serial, sock): p.unlink(missing_ok=True)
    trace, events = OUT / f"{tag}-write.trace", QMP_DIR / f"{tag}-trace-events"
    if tag == "chain" or USB_SMOKE:
        trace.unlink(missing_ok=True)
        events.write_text("scsi_disk_dma_command_WRITE\nblk_co_pwritev\n" +
                          ("pci_nvme_io_cmd\npci_nvme_admin_cmd\n" if USB_SMOKE else ""))
    shutil.copy(OVMF_VARS, vars_fd)
    cmd = [QEMU, "-machine", "q35,accel=kvm", "-cpu", "host", "-m", "8192",
           "-smp", "4", "-no-reboot", "-vga", "none", "-display", "none",
           # GOP framebuffer (OVMF drives bochs-display; romfile= because no VGA BIOS is needed)
           *(["-device", "bochs-display,id=gfx,romfile=,bus=pcie.0,addr=0x5"] if GOP else []),
           "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
           "-drive", f"if=pflash,format=raw,file={vars_fd}",
           "-device", "qemu-xhci,id=xhci,bus=pcie.0,addr=0x3",
           "-drive", f"file={IMG},if=none,id=usbdisk,format=raw,snapshot=on",
           "-device", "usb-storage,bus=xhci.0,drive=usbdisk,bootindex=1",
           "-drive", f"file={blank},if=none,id=nv,format=raw", "-device", "nvme,drive=nv,serial=BLANKNVME,bus=pcie.0,addr=0x2",
           "-serial", f"file:{serial}", "-monitor", "none", "-qmp", f"unix:{sock},server=on,wait=off",
           *(["-trace", f"events={events},file={trace}"] if tag == "chain" or USB_SMOKE else [])] + extra
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    if QEMU_LIB: env["LD_LIBRARY_PATH"] = QEMU_LIB
    print("+", " ".join(map(str, cmd)), flush=True)
    p = subprocess.Popen(list(map(str, cmd)), env=env)
    end = time.monotonic() + QEMU_SECONDS
    if tag == "functional": FUNCTIONAL_QMP_OK = None
    last_probe = None
    serial_offset = 0
    pending = ""
    while time.monotonic() < end and p.poll() is None:
        time.sleep(2)
        if tag == "functional" and time.monotonic() >= end - min(60, QEMU_SECONDS):
            healthy, woke = functional_wake_check(sock)
            FUNCTIONAL_QMP_OK = healthy if FUNCTIONAL_QMP_OK is None else FUNCTIONAL_QMP_OK and healthy
            if woke: print("functional QMP requested guest wake from S3", flush=True)
        if probe_token and serial.exists():
            with serial.open("rb") as stream:
                stream.seek(serial_offset)
                pending += stream.read().decode(errors="replace")
                serial_offset = stream.tell()
            lines = pending.split("\n")
            pending = lines.pop()
            if any("gbos-qemu-probe: " + probe_token + " " in line for line in lines):
                last_probe = time.monotonic()
    probe_age = None if last_probe is None else time.monotonic() - last_probe
    if probe_token:
        print("functional device probe age at end:", probe_age, flush=True)
    alive, stats, chain_blocks, chain_count, chain_trace = p.poll() is None, None, None, None, None
    if alive:
        try:
            # GOP framebuffer: capture the screen first, on the same QMP connection
            shot = [("screendump", {"filename": str(OUT / f"{tag}-screen.png"), "format": "png", "device": "gfx"})] if GOP else []
            if tag == "chain":
                # Pausing vCPUs does not complete outstanding I/O. Reconcile the
                # full request trace with completed QMP counters, then reconcile
                # again after QEMU exits so later completions cannot grant a pass.
                res = qmp_query(sock, *shot, "stop")
                if GOP: print(tag, "screendump", res[0])
                if "return" not in res[-1]: raise ValueError("QMP stop failed")
                deadline = time.monotonic() + 5
                while True:
                    r = qmp_query(sock, "query-blockstats")[-1]["return"]
                    try:
                        chain_count = userdata_write_bytes(trace, r, USERDATA["start"], USERDATA["end"], IMG.stat().st_size)
                        chain_blocks = r
                        chain_trace = trace.read_bytes()
                        break
                    except ValueError as exc:
                        if time.monotonic() >= deadline:
                            print(tag, "write trace did not reconcile:", exc)
                            break
                        time.sleep(0.2)
            else:
                if USB_SMOKE:
                    res = qmp_query(sock, *shot, "stop")
                    if GOP: print(tag, "screendump", res[0])
                    if "return" not in res[-1]: raise ValueError("QMP stop failed")
                    res = qmp_query(sock, "query-blockstats")
                    chain_trace = trace.read_bytes()
                else:
                    res = qmp_query(sock, *shot, "query-blockstats")
                    if GOP: print(tag, "screendump", res[0])
                r = res[-1]["return"]
            usb_stats = [d["stats"] for d in r if d.get("device") == "usbdisk" or
                         (tag != "chain" and d.get("qdev", "").endswith("usbdisk"))]
            if len(usb_stats) != 1: raise ValueError("USB block stats missing or ambiguous")
            stats = dict(usb_stats[0])
            if USB_SMOKE:
                nvme_stats = [d["stats"] for d in r if d.get("device") == "nv"]
                nv = nvme_stats[0] if len(nvme_stats) == 1 else {}
                # Never publish the zero-write claim until after QEMU exits and
                # the complete trace has been checked, for either boot path.
                stats["usb_smoke_nvme_wr_bytes"] = None
        except Exception as e:
            print(tag, "QMP query failed:", e)
        p.terminate()
        try: p.wait(30)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
        if tag == "chain" and stats is not None and chain_count is not None:
            try:
                final_count = userdata_write_bytes(trace, chain_blocks, USERDATA["start"], USERDATA["end"], IMG.stat().st_size)
                if final_count != chain_count or trace.read_bytes() != chain_trace:
                    raise ValueError("trace changed after QMP snapshot")
                stats["chain_userdata_write_bytes"] = final_count
            except ValueError as exc:
                print(tag, "final write trace refused:", exc)
        if USB_SMOKE and stats is not None and chain_trace is not None:
            try:
                nvme_trace_safe(trace)
                if trace.read_bytes() != chain_trace: raise ValueError("trace changed after QMP snapshot")
                if all(type(nv.get(k)) is int and nv[k] == 0 for k in
                       ("wr_bytes", "wr_operations", "failed_wr_operations", "invalid_wr_operations")):
                    stats["usb_smoke_nvme_wr_bytes"] = 0
            except (ValueError, OSError) as exc:
                print(tag, "final NVMe trace refused:", exc)
    print(tag, "QEMU", "ran the full", QEMU_SECONDS, "s" if alive else f"exited early with {p.returncode}", "usbdisk", stats)
    log = serial.read_text(errors="replace") if serial.exists() else ""
    return alive, stats, log, probe_age

def serial_device_probe(log, token, probe_age):
    """Parse the last fresh device probe, including an unset property or an absent process during boot."""
    reports = re.findall(r"gbos-qemu-probe: " + re.escape(token) + r" ([^\r\n]*)(\r?\n|$)", log)
    if probe_age is None or not 0 <= probe_age <= 15 or not reports or not reports[-1][1]:
        return None
    return re.fullmatch(r"boot_completed=([01]?) system_server_pid=([1-9]\d*)?", reports[-1][0].strip())

def serial_boot_completed(log, token, probe_age):
    """Use the last device-generated probe, not the kernel command line or an init action."""
    probe = serial_device_probe(log, token, probe_age)
    return bool(probe and probe.group(1) == "1" and probe.group(2))

def framework_log_checks(log, token):
    """Reject fatal Java system-process exceptions and repeated system_server deaths/restarts."""
    callers = set(re.findall(r"Caller\(pid=(\d+),uid=1000,sid=u:r:system_server:s0\)", log))
    deaths = set(re.findall(r"Untracked process \(pid: (\d+) name: \(system_server\).*?\) received SIG", log))
    probe_pids = set(re.findall(r"gbos-qemu-probe: " + re.escape(token) +
                                r" boot_completed=\S* system_server_pid=([1-9]\d*)\s*(?:\r?\n|$)", log))
    return {
        "no FATAL EXCEPTION IN SYSTEM PROCESS": "FATAL EXCEPTION IN SYSTEM PROCESS" not in log,
        "system_server did not restart repeatedly": len(callers | probe_pids) <= 1 and len(deaths) < 2,
    }

def qemu_framework_checks(log, token, probe_age, gop):
    """Only the QEMU configuration with a display device can accept a full framework boot."""
    checks = framework_log_checks(log, token)
    checks["fresh well-formed device probe"] = serial_device_probe(log, token, probe_age) is not None
    checks["device getprop sys.boot_completed is 1"] = serial_boot_completed(log, token, probe_age)
    not_run = {}
    if not gop:  # qemu() uses -vga none and adds bochs-display only for GOP configurations.
        reason = "QEMU has no GPU (-vga none, no GOP display); full Android framework boot not accepted"
        for name in ("device getprop sys.boot_completed is 1", "system_server did not restart repeatedly"):
            del checks[name]
            not_run[name] = reason
    return checks, not_run

def record_not_run(not_run):
    """Keep non-acceptance visible in both image.log and the build's persistent summary."""
    if not_run:
        with (GBOS_OUT / "build-info.txt").open("a") as info:
            for name, reason in not_run.items():
                line = f"NOT_RUN {name} ({reason})"
                print(line, flush=True)
                info.write(line + "\n")

before = sha256(IMG)
def vendor_rc_rejections(log):
    """Lines where init names one of the project's own vendor rc files and reports a refusal.

    Covers the shapes system/core 545d248 init logs: "<file>: <line>: <error>" from the parser
    (e.g. "ParseTriggers() failed: unexported property trigger found: ...", "service 'x'
    overrides another service across the treble boundary."), and "Command '...' action=...
    (<file>:<line>) took Nms and failed: ..." from Action::ExecuteCommand. Plain "processing
    action ... from (<file>:N)" and "Parsing file <file>..." lines do not match.
    """
    rc = r"\b(?:kano-services|kano-audiotest|kano-hw|kano-diag|kano-adb|kano-autooff|kano-bootfix|kano-logcat|amd-hx370-graphics|gbos-efifb|gbos-no-ota)\.rc\b"
    bad = r"(?i)(error|invalid|fail|not allowed|unexported|overrides another service|treble boundary|unable to)"
    return [ln for ln in log.splitlines() if "init: " in ln and re.search(rc, ln) and re.search(bad, ln)]
def publish_record(digest, status, not_run):
    record = {"schema": 1, "profile": PROF["model"]["id"],
              "profile_sha256": hashlib.sha256(PROFILE_BYTES).hexdigest(),
              "declared_build_id": os.environ.get("GBOS_LAPIS_BUILD", "16452207"),
              "build_id_basis": "selected_pins", "image": {"basename": FINAL.name,
              "size": IMG.stat().st_size, "sha256": digest}, "selftest": status,
              "not_run": sorted(not_run), "options": {"entry": DEFAULT_ENTRY, "editor": EDITOR,
              "ec": EC_FEATURES, "adb": ADB, "usb_smoke": USB_SMOKE,
              "model_override": os.environ.get("GBOS_ANDROID_MODEL") == "profile"},
              "pristine": "NOT_PROVEN", "input_coverage": "INCOMPLETE",
              "authority": "UNSIGNED_BUILDER_SELF_REPORT"}
    if os.environ.get("GBOS_SETUP_PIN_SKIP") == "1":
        record["options"]["setup_pin_skip"] = True
    build_record.publish(IMG, FINAL, digest, record, True)

checks = {}
if SKIP_SELFTEST:
    print("SKIP QEMU self-tests (GBOS_SKIP_SELFTEST=1): this image is NOT self-tested")
    after = before
    publish_record(after, "skipped", ["functional QEMU", "boot chain QEMU"])
    shutil.rmtree(QMP_DIR, ignore_errors=True)
    print(FINAL.name, "sha256", after)
    print("IMAGE OK (NOT SELF-TESTED)", FINAL)
    sys.exit(0)
# 1. functional: for amd-hx370, boot the SAFE default entry (quiet: amdgpu blacklisted, no amdgpu.*, gbos_efifb +
# SwiftShader) with COM1 serial logging enabled; for other profiles, boot the debug entry's options with COM1 console.
# QEMU has no KANO MMIO UART (0xfe03e000), so console= and earlycon= are removed and replaced with COM1
# (console=ttyS0,115200n8 and earlycon=uart8250,io,0x3f8). Without GOP, Android framework
# completion/stability are NOT_RUN; the remaining Android checks still gate the image. It does not
# show that the KANO AP UART still prints. On hardware, serial-debug keeps earlycon plus
# keep_bootcon (printk on the bootconsole, not an interactive tty).
if AMD_HX370:
    q = ([("loglevel=8" if o == "loglevel=4" else o) for o in quiet]
         + ["ignore_loglevel", "keep_bootcon", "printk.devkmsg=on",
            "console=ttyS0,115200n8", "earlycon=uart8250,io,0x3f8,115200n8"])
else:
    q = [o for o in debug if not o.startswith(("console=", "earlycon="))] + ["console=ttyS0,115200n8", "earlycon=uart8250,io,0x3f8,115200n8"]
probe_token = uuid.uuid4().hex
q.append("androidboot.gbos_qemu_nonce=" + probe_token)
def ec_ssdt(kbd_backlight=False):
    """GBOS_EC self-test only: an SSDT with the KANO's EC ACPI devices (\\_SB.CREC = GOOG0004 with GOOG0014, GOOG0007 and
    GOOG0003 below it) and no EC behind them, so ueventd loads the released drivers under this kernel and they must
    give up cleanly. Plain AML, assembled here (no iasl)."""
    def pkg(c):
        if len(c) + 1 <= 0x3F: return bytes([len(c) + 1]) + c
        n = len(c) + 2; assert n < 1 << 12
        return bytes([0x40 | (n & 0xF), n >> 4]) + c
    def hid(h): return b"\x08_HID\x0d" + h.encode() + b"\x00"
    def dev(seg, body): return b"\x5b\x82" + pkg(seg + body)
    crec = dev(b"CREC", hid("GOOG0004") + b"\x08_UID\x01" + dev(b"TYPC", hid("GOOG0014")) + dev(b"CKSC", hid("GOOG0007"))
               + dev(b"USBC", hid("GOOG0003")) + (dev(b"KBLT", hid("GOOG0002")) if kbd_backlight else b""))
    aml = b"\x10" + pkg(b"\\_SB_" + crec)
    t = bytearray(struct.pack("<4sIBB6s8sI4sI", b"SSDT", 36 + len(aml), 2, 0, b"GBOS  ", b"ECVARNT ", 1, b"GBOS", 1) + aml)
    t[9] = -sum(t) & 0xFF
    return bytes(t)
extra_acpi = []
if KMOD_AC:
    ac_ssdt(OUT / "ac-ssdt.aml"); extra_acpi = ["-acpitable", f"file={OUT / 'ac-ssdt.aml'}"]
if EC_FEATURES:
    (OUT / "ec-ssdt.aml").write_bytes(ec_ssdt()); extra_acpi += ["-acpitable", f"file={OUT / 'ec-ssdt.aml'}"]
if KMOD_KBD_BACKLIGHT:
    (OUT / "ec-ssdt.aml").write_bytes(ec_ssdt(True))
alive, _stats, log, probe_age = qemu("functional", ["-kernel", OUT / "kernel.efi", "-initrd", OUT / "initrd.img", "-append", " ".join(q)] + extra_acpi, probe_token)
if USB_SMOKE:
    checks["USB smoke functional: QEMU NVMe received no writes"] = (_stats or {}).get("usb_smoke_nvme_wr_bytes") == 0
checks["functional QMP wake control healthy"] = FUNCTIONAL_QMP_OK is True
boot_completed = serial_boot_completed(log, probe_token, probe_age)
print("functional device getprop sys.boot_completed:", "1" if boot_completed else "not confirmed", flush=True)
has = lambda p: re.search(p, log) is not None

def ec_selftest_checks(log, features):
    """QEMU serial checks for one GBOS_EC value.

    An empty list is the default image. QEMU has no EC, so a drain boot is expected to stop at
    "EC ID not detected". That line is not a drain pass and not a HOST_SLEEP pass.

    Whether a module loaded is decided only from "Loaded kernel module .../*.ko". The cros_ec
    core creates an unbound platform device named cros-ec-dev even when cros_ec_dev.ko stays
    blacklisted, so a bare cros-ec-dev line is not an MFD driver.

    Drain checks every such line against the blocked EC set: feature consumers, other
    transports, and EC_EXTRA_BLOCK. A file name may use '-' where the module name uses '_'.

    No early-boot modprobe was added. This checkout does not ship the vendor_dlkm modules.dep.
    Consumers depend on the core, not the other way around, and the GOOG0004 SSDT above (any
    non-empty EC_FEATURES, including drain) is the uevent path that loads cros_ec_lpcs. The
    KANO profile's early-boot module list stays empty.
    """
    def found(pattern):
        return re.search(pattern, log) is not None

    if not features:
        return {
            "no EC transport/core/chardev module loaded": not found(
                r"Loaded kernel module \S*/(cros_ec|cros_ec_lpcs|cros_ec_dev|cros_ec_chardev|cros_ec_sysfs|cros_ec_vbc|cros-ec-typec|cros_typec_switch)\.ko"),
        }
    out = {
        # The SSDT declares the EC's ACPI devices with no EC behind them. ueventd loads the released
        # drivers and they must give up cleanly. The EC channels and writers never load.
        "EC variant: no EC channel/writer module loaded": not found(
            r"Loaded kernel module \S*/(cros_ec_chardev|cros_ec_debugfs|cros_ec_sysfs|cros_ec_vbc|cros_ec_lightbar|i2c-cros-ec-tunnel|cros_charge-control|leds-cros_ec|cros_ec_spi|cros_ec_i2c|cros_ec_ishtp|cros-ec-usb|cros_peripheral_charger|cros_usbpd-charger|cros_usbpd_logger|cros_typec_switch|cros_ec_ucsi)\.ko"),
        "EC variant: cros_ec_lpcs loaded, probed GOOG0004, gave up (no EC in QEMU)": found(
            r"cros_ec_lpcs GOOG0004:00: EC ID not detected"),
        "EC variant: cros-ec-typec loaded, waits for the EC": found(
            r"cros-ec-typec GOOG0014:00: couldn't find parent EC device") if "typec" in features else True,
        "EC variant: no /dev/cros_ec": not found(r"/dev/cros_(ec|fp|ish)\b"),
    }
    if features == ["drain"]:
        # Every Loaded kernel module line. '-' and '_' name one module.
        # Consumers, other transports and EC_EXTRA_BLOCK stay blocked. Not a HOST_SLEEP pass.
        def mod_key(name):
            return name.replace("-", "_")

        def _drain_blocked_module_keys():
            blocked = set()
            for name in EC_EXTRA_BLOCK:
                blocked.add(mod_key(name))
            for name in EC_CORE:
                if name not in EC_DRAIN:
                    blocked.add(mod_key(name))
            for group in EC_RELEASE.values():
                for name in group:
                    blocked.add(mod_key(name))
            for name in EC_NEVER:
                key = mod_key(name)
                if "cros" in key:
                    blocked.add(key)
            for name in EC_DRAIN:
                blocked.discard(mod_key(name))
            blocked.discard("cros_ec_proto")
            return blocked  # drain-blocked EC module keys

        blocked = _drain_blocked_module_keys()
        loaded_now = [mod_key(m.group(1)) for m in re.finditer(
            r"Loaded kernel module \S*/([A-Za-z0-9_.-]+)\.ko\b", log)]
        out["drain: consumers, other transports and extra EC modules stayed unloaded (not a HOST_SLEEP pass)"] = (
            not any(key in blocked for key in loaded_now))
    return out

checks.update({
    "QEMU still running at the end (functional)": alive,
    "init second stage": has(r"init second stage started"),
    "no vbmeta mismatch": not has(r"(?i)vbmeta digest (error|mismatch)"),
    "serial log has no nvme controller pattern": not has(r"nvme nvme\d"),
    "system_server running": has(r"u:r:system_server:s0"),
    "keymint HAL present": has(r"IKeyMintDevice/default"),
    "fwupd never started": not has(r"starting service 'fwupd"),
    "no rollforward": not has(r"kernel_max_rollforward"),
    "no gsmi": not has(r"gsmi:"),
    "no kernel panic": not has(r"Kernel panic"),
    "first stage did not load the profile's dropped modules (KANO: LPSS UART)": not DROP or not has(r"Loading module /lib/modules/(" + "|".join(re.escape(x[:-3]) for x in sorted(DROP)) + r")\.ko"),
    "split policy compiled (no failure)": has(r"No precompiled sepolicy at /vendor") and not has(r"(?i)(failed to compile|could not load selinux policy|secilc: (?!.*linker: )[^\n]*(error|failed))"),
    "kano-logcat on serial": has(r"kano-logcat: "),
    "data mounted as f2fs": has(r"target=/data,type=f2fs\)=0"),
    "IWeaver not declared": not has(r"Found android.hardware.weaver.IWeaver/default in device VINTF manifest"),
    "kano_bootfix ran": has(r"Service .kano_bootfix. .*exited with status 0"),
    "kano_diag started": has(r"starting service .kano_diag."),
    "Wi-Fi modules of the profile modprobed (KANO: iwlmvm)": all(has(r"modprobe --dirname=/vendor/lib/modules " + re.escape(m)) for m in PROF["wifi"]["modprobe"]),
    "first stage loads pci-stub (if the profile uses it)": not STUB or has(r"Loading module /lib/modules/pci-stub.ko"),
    "kano audiotest rc parsed": has(r"init: Parsing file /vendor/etc/init/kano-audiotest.rc"),
    "kano-services rc parsed": has(r"init: Parsing file /vendor/etc/init/kano-services.rc"),
    "no vendor rc rejections": not vendor_rc_rejections(log),
})
framework_checks, not_run = qemu_framework_checks(log, probe_token, probe_age, GOP)
checks.update(framework_checks)
record_not_run(not_run)
checks.update(ec_selftest_checks(log, EC_FEATURES))
if GOP:
    checks.update({
        "generic: GOP framebuffer handed to gbos_efifb": has(r"gbos-efifb: GOP \d+x\d+ depth 32"),
        "generic: gbos_efifb registered as vkms": has(r"gbos_efifb: \d+x\d+ stride \d+ at 0x[0-9a-f]+ \((?:XRGB|XBGR)\), registered as vkms \(scanout\)"),
        "generic: no execmem denial": not has(r"avc: +denied +\{ execmem \}"),
        "generic: graphics allocator stays up": not has(r"Service 'vendor\.graphics\.allocator' \(pid \d+\) exited"),
        "generic: surfaceflinger not restarted": not has(r"Sending SIGKILL to service 'surfaceflinger'|Service 'surfaceflinger' \(pid \d+\) received SIG"),
        "generic: no DRM master failure": not has(r"Failed to set drm master|DRM/KMS master access required"),
    })
    # a uniform frame (black, or the firmware logo on black) compresses to a few KB; the setup wizard is over 40 KB
    shot = OUT / "functional-screen.png"
    checks["generic: non-blank GOP screendump (PNG over 20 KB)"] = shot.exists() and shot.stat().st_size > 20000
if AMD_HX370:
    checks.update({
        "amd-hx370 safe: graphics helper selected safe entry": has(r"amd-hx370: safe entry: ro\.hardware\.vulkan=pastel hwui=false renderengine=false"),
        "amd-hx370 safe: amdgpu not modprobed": not has(r"modprobe [^\n]*\bamdgpu\b"),
        "amd-hx370 safe: amdgpu not in /proc/modules": not has(r"gbos-dev-modules: [^\n]*\bamdgpu\b"),
        "amd-hx370 radv-probe: vkprop redirected ro.hardware.vulkan": has(r"vkprop: ro\.hardware\.vulkan -> vendor\.gbos\.radv_probe"),
        "amd-hx370 radv-probe: vulkan.radeon.so mapped in process": has(r"radv-probe: mapped [^\n]*/vendor/lib64/hw/vulkan\.radeon\.so"),
        "amd-hx370 radv-probe: vkCreateInstance succeeded": has(r"radv-probe: vkCreateInstance -> 0\b"),
        "amd-hx370 radv-probe: vkEnumeratePhysicalDevices succeeded (0 AMD GPUs in QEMU)": has(r"radv-probe: vkEnumeratePhysicalDevices -> 0 count=0\b"),
    })
if DEV_KERNEL_DIR:
    checks["dev-kernel: self-built Linux version"] = has(r"Linux version 6\.18\.21-android17-5\S* \(gbos@gbos\)")
    checks["dev-kernel: rust_binder loaded"] = has(r"rust_binder: Loaded Rust Binder\.")
    checks["dev-kernel: no module symbol/CRC/protection errors"] = not has(r"Cannot use protected symbol|disagrees about version of symbol|Unknown symbol in module")
    if AMD_HX370:
        checks["dev-kernel: gbos_efifb live in /proc/modules on safe entry"] = has(r"gbos-dev-modules: .*\bgbos_efifb\b")
        checks["dev-kernel: simpledrm not modprobed or live in /proc/modules on amd-hx370"] = (
            not has(r"modprobe [^\n]*\bsimpledrm\b") and not has(r"gbos-dev-modules: [^\n]*\bsimpledrm\b")
        )
    elif PROF["model"]["id"] == "rog-ally":
        checks["dev-kernel: Ally safe entry: gbos_efifb live"] = has(r"gbos-dev-modules: .*\bgbos_efifb\b")
        checks["dev-kernel: Ally safe entry: no native graphics or ACP70 load"] = (
            not has(r"modprobe [^\n]*\b(?:amdgpu|simpledrm|snd[-_]sof[-_]amd[-_]acp70)\b")
            and not has(r"gbos-dev-modules: [^\n]*\b(?:amdgpu|simpledrm|snd[-_]sof[-_]amd[-_]acp70)\b")
        )
    elif (Path(DEV_KERNEL_DIR) / "modules/amdgpu.ko").is_file():
        checks["dev-kernel: amdgpu.ko modprobe ran"] = has(r"Service 'exec \d+ \(/vendor/bin/modprobe --dirname=/vendor/lib/dev-modules amdgpu\)' \(pid \d+\) exited with status 0")
        checks["dev-kernel: amdgpu + audio + simpledrm live in /proc/modules"] = (
            has(r"gbos-dev-modules: .*\bamdgpu\b")
            and has(r"gbos-dev-modules: .*\bsnd_sof_amd_acp70\b")
            and has(r"gbos-dev-modules: .*\bsimpledrm\b")
        )
def kbd_module_log_checks(log):
    """An explicit /proc/modules snapshot proves presence and fail-closed absence.

    modprobe succeeds without ueventd's 'Loaded kernel module' message. QEMU has
    no EC, so module presence here does not establish a bound, working driver.
    """
    snapshots = re.findall(r"gbos-kbd-modules: new=([01]) stock=([01])\r?$", log, re.M)
    stock_loaded = re.search(r"Loaded kernel module [^\n]*/cros[-_]kbd[-_]led[-_]backlight\.ko", log)
    return {
        "kbd: module loaded": bool(snapshots) and any(new == "1" for new, _ in snapshots),
        "kbd: stock module not loaded": bool(snapshots) and all(stock == "0" for _, stock in snapshots) and not stock_loaded,
    }

if KMOD_KBD_BACKLIGHT:
    checks.update({
        "kbd: EC protocol modprobe exited 0": has(r"Service 'exec \d+ \(/vendor/bin/modprobe --dirname=/vendor/lib/modules cros-ec-proto\)' \(pid \d+\) exited with status 0"),
        "kbd: backlight modprobe exited 0": has(r"Service 'exec \d+ \(/vendor/bin/modprobe --dirname=/vendor/lib/kano gbos_kbd_backlight\)' \(pid \d+\) exited with status 0"),
    })
    checks.update(kbd_module_log_checks(log))
if KMOD_AC:
    checks.update({
        "ac.ko modprobe ran (profile kmods: ac)": has(r"Service 'exec \d+ \(/vendor/bin/modprobe --dirname=/vendor/lib/kano ac\)' \(pid \d+\) exited with status 0"),
        "AC adapter bound, on line": has(r"AC Adapter \[\w+\] \(on-line\)"),
        "healthd sees the charger (chg=a)": has(r"healthd: battery .*chg=a"),
        "no SELinux denial on the AC power supply": not has(r'avc:  denied .*(power_supply|ACPI0003)'),
    })
else:
    checks["no ac.ko (profile has no kmods: ac)"] = not has(r"modprobe --dirname=/vendor/lib/kano ac\)")
if KMOD_WAKEFILTER:
    checks.update({
        "gbos_wakefilter modprobe ran (profile kmods: gbos_wakefilter)": has(r"Service 'exec \d+ \(/vendor/bin/modprobe --dirname=/vendor/lib/kano gbos_wakefilter\)' \(pid \d+\) exited with status 0"),
        "gbos_wakefilter watches the ACPI Power Button": has(r"gbos_wakefilter: watching Power Button \(LNXPWRBN/button/input0\)"),
    })
else:
    checks["no gbos_wakefilter (profile has no kmods: gbos_wakefilter)"] = not has(r"gbos_wakefilter")
if ADB:
    checks["kano-adb rc parsed (GBOS_ADB_PUBKEY set)"] = has(r"init: Parsing file /vendor/etc/init/kano-adb.rc")
else:
    checks["no kano-adb (GBOS_ADB_PUBKEY not set)"] = not has(r"kano-adb|kano_adb")
def autooff_log_checks(log, enabled):
    """QEMU serial checks for GBOS_TEST_AUTOOFF. enabled is the env value; empty means the service is absent."""
    found = lambda p: re.search(p, log) is not None
    if enabled:
        return {
            "kano-autooff rc parsed (GBOS_TEST_AUTOOFF)": found(r"init: Parsing file /vendor/etc/init/kano-autooff\.rc"),
            "kano-autooff refuses QEMU (no /data nonce, staying on)": found(r"KANO-AUTOOFF: nonce gate closed, staying on"),
            "kano-autooff did not power off (no /data nonce)": not found(r"KANO-AUTOOFF: powering off"),
            "kano_autooff exited 0": found(r"Service .kano_autooff. .*exited with status 0"),
        }
    return {"no kano-autooff (GBOS_TEST_AUTOOFF not set)": not found(r"kano-autooff|kano_autooff|KANO-AUTOOFF")}
def no_ota_log_checks(log):
    """Serial-log checks for /vendor/etc/init/gbos-no-ota.rc (they need a QEMU boot log).

    init logs the rc parse, the action, and the stop itself: Service::KillProcessGroup logs
    "Sending <signal> to service 'update_engine' (pid N) process group" (older init: "Sending
    signal N"); a "received signal" line alone is not stop evidence (a crash logs it too). A failed
    command is logged by Action::ExecuteCommand as "Command 'stop update_engine' action=... took
    Nms and failed: ..." (system/core 545d248 init/action.cpp, init/service.cpp). Passing: the
    vendor action ran, the stop command did not fail, and update_engine either never started (the
    stop ran before init started it, seen in QEMU build 9) or init sent it a signal after its last
    "starting service" line (a restart is red). A log without the action line is red.
    """
    found = lambda p: re.search(p, log) is not None
    # Pair each "FATAL EXCEPTION" with the first AndroidRuntime "Process:" line after it, before
    # the next FATAL EXCEPTION, however many kernel/init lines are interleaved on the serial port.
    # With a logcat PID column ("E AndroidRuntime" lines carry it), the Process line must come from
    # the same PID. Java fatals only: not ANRs, native crashes or retry loops.
    gms_fatal = False
    lines = log.splitlines()
    pid_of = lambda ln: (m.group(1) if (m := re.search(r"\b(\d+)\s+\d+\s+[EF]\s+AndroidRuntime\b", ln)) else None)
    for i, line in enumerate(lines):
        if "FATAL EXCEPTION" not in line:
            continue
        pid = pid_of(line)
        for nxt in lines[i + 1:]:
            if "FATAL EXCEPTION" in nxt:
                break
            if not re.search(r"Process:\s*\S", nxt) or (pid and pid_of(nxt) not in (pid, None)):
                continue
            if re.search(r"Process:\s*com\.google\.android\.gms(?:[.\s,:]|$)", nxt):
                gms_fatal = True
            break
        if gms_fatal:
            break
    last_start = last_stop = -1
    for i, line in enumerate(lines):
        if re.search(r"init: starting service 'update_engine'", line):
            last_start = i
        if re.search(r"init: Sending (?:signal \d+|SIG[A-Z0-9]+) to service 'update_engine'", line):
            last_stop = i
    stop_failed = found(r"init: Command 'stop update_engine'[^\n]* failed") or found(r"init: [^\n]*Unable to[^\n]*update_engine")
    action_ran = found(r"init: processing action \(ro\.boot\.slot_suffix=\*\) from \(/vendor/etc/init/gbos-no-ota\.rc")
    return {
        "gbos-no-ota: rc parsed without errors": found(r"init: Parsing file /vendor/etc/init/gbos-no-ota\.rc")
            and not found(r"(?i)init: /vendor/etc/init/gbos-no-ota\.rc: .*(fail|error|unexported|not allowed|invalid)"),
        "gbos-no-ota: action ran": action_ran,
        "gbos-no-ota: update_engine not running (never started, or stopped after its last start)":
            action_ran and not stop_failed and (last_start == -1 or last_stop > last_start),
        "gbos-no-ota: no FATAL EXCEPTION in com.google.android.gms": not gms_fatal,
    }
checks.update(autooff_log_checks(log, TEST_AUTOOFF))
# gbos-no-ota keys need the QEMU serial log (the setup wizard check above covers the wizard).
checks.update(no_ota_log_checks(log))

# 2. boot chain: no -kernel, so OVMF boots the image's own ESP and systemd-boot starts the default
# entry with the options stored on that ESP (nothing is rewritten to COM1). Ran only when the
# default is quiet: quiet has no earlycon, and disk writes show Android reached /data. The debug
# entry's earlycon is the KANO MMIO UART, which QEMU does not have, so this test is not a hardware
# UART log of the debug entry.
if DEFAULT_ENTRY == "lapis-quiet.conf":
    alive, stats, clog, _probe_age = qemu("chain", [])
    wrote = (stats or {}).get("chain_userdata_write_bytes")
    checks["QEMU still running at the end (chain)"] = alive
    if USB_SMOKE:
        checks["USB smoke chain: QEMU NVMe received no writes"] = (stats or {}).get("usb_smoke_nvme_wr_bytes") == 0
    checks["chain: default entry keeps the serial console silent"] = "Linux version" not in clog and "Boot in" in clog
    if GOP:
        cshot = OUT / "chain-screen.png"
        checks["chain (generic): non-blank GOP screendump (PNG over 20 KB)"] = cshot.exists() and cshot.stat().st_size > 20000
    checks["chain: Android wrote /data (>= 16 MiB, into userdata)"] = wrote is not None and wrote >= (16 << 20)
    print("chain: confirmed usbdisk userdata write bytes", wrote, "userdata", USERDATA["start"], "-", USERDATA["end"])
    template = Path(OVMF_VARS).read_bytes(); after_vars = (OUT / "chain-vars.fd").read_bytes()
    print("chain: OVMF variable store bytes changed in QEMU (firmware boot entries; info only):",
          sum(a != b for a, b in zip(template, after_vars)))
else:
    print(f"SKIP boot chain test: default entry {DEFAULT_ENTRY} sends earlycon to the KANO's LPSS UART, which QEMU lacks")

after = sha256(IMG)
checks["image unchanged by the QEMU runs"] = before == after
for k, v in checks.items(): print("PASS" if v else "FAIL", k)
if not all(checks.values()):
    print(f"image left at {IMG} (not renamed: a check failed)")
    shutil.rmtree(QMP_DIR, ignore_errors=True)
    sys.exit(1)
publish_record(after, "passed", [name + ": " + reason for name, reason in not_run.items()] + ([] if DEFAULT_ENTRY == "lapis-quiet.conf" else ["boot chain QEMU"]))
shutil.rmtree(QMP_DIR, ignore_errors=True)
print(FINAL.name, "sha256", after)
print("IMAGE OK", FINAL)
