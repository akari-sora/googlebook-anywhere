#!/usr/bin/env python3
"""Device profile loader (builder/lib/devprofile.py): the KANO profile loads and gives exactly the entries the builder
made before profiles existed (the former builder/image.py logic is kept below as the reference); broken profiles are
refused (missing / unknown keys, wrong types, weakened safety rules); the KANO hooks report the vendor changes the
former builder/vendor.sh expected. No image, no network, no root.
usage: tests/builder/profile-test.py"""
import ast, importlib.util, shutil, subprocess, sys, tempfile, tomllib
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


# A synthetic stock vendor_boot command line with every option the drop lists name, in a lapis-like order (the real
# one is in the official image and is checked by builder/image.py through its SHA-256).
VCMD = ("snd_intel_sdw_acpi.sdw_link_mask=0x5 kvm-intel.pkvm=1 intel_iommu=igfx_off console=null 8250.nr_uarts=4 "
        "log_buf_len=1M root=/dev/ram0 module_blacklist=cros_ec_debugfs no_console_suspend softlockup_panic=1 "
        "nmi_watchdog=panic pm.dpm_watchdog_enabled=1 cpufreq.default_governor=performance fred=on spi-intel.writeable=1 "
        "spi-intel.ignore_protection_status=1 xe.enable_psr=1 snd_intel_dspcfg.dsp_driver=3 snd_sof_pci.ipc_type=1 "
        "snd_intel_sdw_acpi.sdw_link_mask=0xF initcall_blacklist=foo bootconfig")


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
new = devprofile.make_entries(kano, VCMD)
old = former_entries(VCMD)
for k in ("normal", "debug", "quiet"):
    check(f"KANO {k} entry options = the former builder/image.py ones, token for token", new[k] == old[k])
mods, ics, plain = devprofile.guards(kano)
check("guards: 57 modules, initcalls nvme_init + mei_me_driver_init, efi=noruntime",
      len(mods) == 57 and ics == ["nvme_init", "mei_me_driver_init"] and plain == ["efi=noruntime"])
check("KANO entry titles unchanged (the plain entry was dropped in v0.2)", (kano["entries"]["quiet"], kano["entries"]["debug"]) ==
      ("Googlebook KANO quiet", "Googlebook KANO safe serial-debug") and set(kano["entries"]) == {"quiet", "debug"})

# firmware list = the former fetch-inputs.sh / vendor.sh lists
FORMER_FW = {"i915/adlp_dmc.bin": "3516de2e134ddcf3b319c75d2e437779fecbd58cbb77234bd6f297c544e92ccb",
             "i915/adlp_guc_70.bin": "b7a800cbdcdf687e7b1c81f3316d040ab75fa3e5c698ebc356f077af17462ef8",
             "i915/tgl_huc.bin": "dbb1316bac13a76427ffa7827e52b5b9affe8a4cadd23dc22e2a54e35c53bdad",
             "iwlwifi-so-a0-gf-a0-89.ucode": "7b53254bb28115f91b6eaa33b1f9a3526e32966105eaac587e259dd17790f384",
             "iwlwifi-so-a0-gf-a0.pnvm": "2aee7592ddbd6c88f1aa51be4ada188fce44765405e52d7f3056b46e62cf3f91",
             "intel/ibt-0040-0041.sfi": "8dcfb3a7c1592c11c3e80505d21ee78dbde3de0c830d8e3e856371c37717b62c",
             "intel/ibt-0040-0041.ddc": "fe272982577efdc289cfe3e8bedafca607c0b7d6c9df1aab1880e22ef930d077",
             "intel/sof/community/sof-adl.ri": "e3489dcb277e87eee8bc091ab7d51163a63c85541c22522775122b54263769f3",
             "intel/sof-tplg/sof-adl-max98373-nau8825.tplg": "4beb29400d9beed0c80bcc11b3f4e82aad70a54d6e851638c81eb087a10d4de5"}
check("KANO firmware = the former pinned list (path and SHA-256)", {f["path"]: f["sha256"] for f in kano["firmware"]} == FORMER_FW)
r = subprocess.run([sys.executable, ROOT / "builder/lib/devprofile.py", "firmware", ROOT / "profiles/kano"], capture_output=True, text=True)
check("devprofile.py firmware prints one '<path> <from> <src> <sha256>' line per file",
      r.returncode == 0 and len(r.stdout.splitlines()) == 9 and all(len(l.split()) == 4 for l in r.stdout.splitlines()))
r = subprocess.run([sys.executable, ROOT / "builder/lib/devprofile.py", "get", ROOT / "profiles/kano", "model.image_name"],
                   capture_output=True, text=True)
check("devprofile.py get model.image_name", r.returncode == 0 and r.stdout == "googlebook-kano.img\n")

# the hooks report the vendor paths the former vendor.sh listed for the KANO
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

# ---- broken profiles are refused
SRC = (ROOT / "profiles/kano/profile.toml").read_text()
tmp = Path(tempfile.mkdtemp(prefix="gbos-profile-test-"))


def attempt(label, edit, expect, name="kano", extra_files=("audio-src.list", "vendor.py")):
    d = tmp / label.replace(" ", "_") / name
    d.mkdir(parents=True)
    for f in extra_files:
        shutil.copy(ROOT / "profiles/kano" / f, d / f)
    text = edit(SRC)
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
attempt("an adb property in build_prop_set", sub('[["vendor.hwc.drm.enable_color_pipeline", "true", "false"],',
                                                 '[["ro.adb.secure", "1", "0"],'), "not an adb/debuggable property")
attempt("a malformed pci-stub id", sub('"8086:51a8"', '"8086:51A8"'), "vvvv:dddd")
attempt("hook-driven settings without a hook", sub('vendor = "vendor.py"', 'vendor = ""'), "need a hooks.vendor file")
attempt("a hooks.vendor file that is not there", sub('vendor = "vendor.py"', 'vendor = "nothere.py"'), "is not a Python file")
attempt("an audio source list that is not there", sub('sources = "audio-src.list"', 'sources = "nothere.list"'), "is not a file")
attempt("TOML that does not parse", sub("[model]", "[model"), "")

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

shutil.rmtree(tmp, ignore_errors=True)
print(f"profile tests: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
