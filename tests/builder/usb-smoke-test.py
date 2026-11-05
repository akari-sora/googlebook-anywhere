#!/usr/bin/env python3
"""Offline checks for the opt-in isolated KANO backlight trial."""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import devprofile  # noqa: E402
import usb_smoke  # noqa: E402

PROFILE = {"model": {"id": "kano"}, "kmods": ["ac", "gbos_wakefilter", "gbos_kbd_backlight"]}
ENV = {"GBOS_USB_SMOKE": "1", "GBOS_LAPIS_BUILD": "16658739", "GBOS_EC": "drain",
       "GBOS_ADB_PUBKEY": "adb.pub"}
OLD = str(uuid.uuid4())
NEW = str(uuid.uuid4())


def initrd(lines):
    raw = "".join(lines).encode()
    return b"070701archive" + raw + struct.pack("<II", len(raw), sum(raw)) + usb_smoke.MAGIC


class SmokeTests(unittest.TestCase):
    def test_preflight_accepts_one_configuration_only(self):
        usb_smoke.validate_options(PROFILE, ENV)
        for changes in ({"GBOS_USB_SMOKE": "yes"}, {"GBOS_LAPIS_BUILD": "16452207"},
                        {"GBOS_ADB_PUBKEY": ""}, {"GBOS_EC": ""}, {"GBOS_EC": "sensors"},
                        {"GBOS_DEFAULT_ENTRY": "lapis-debug.conf"}, {"GBOS_LOADER_EDITOR": "yes"},
                        {"GBOS_SKIP_SELFTEST": "1"}, {"GBOS_DEV_KERNEL_DIR": "/tmp/kernel"},
                        {"GBOS_TEST_AUTOOFF": "60"}, {"GBOS_ALLOW_FIRMWARE_DRIFT": "1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                usb_smoke.validate_options(PROFILE, ENV | changes)
        for bad_profile in ({"model": {"id": "other"}, "kmods": PROFILE["kmods"]},
                            {"model": {"id": "kano"}, "kmods": ["ac"]}):
            with self.assertRaises(ValueError):
                usb_smoke.validate_options(bad_profile, ENV)
        usb_smoke.validate_options({"model": {"id": "other"}, "kmods": []}, {})

    def test_rebind_checksum_and_exact_boot_uuid(self):
        source = initrd(["androidboot.foo=bar\n", f"androidboot.boot_part_uuid={OLD}\n"])
        output = usb_smoke.rebind_bootconfig(source, OLD, NEW)
        self.assertTrue(output.startswith(b"070701archive"))
        size, checksum = struct.unpack_from("<II", output, len(output) - 20)
        raw = output[-20-size:-20]
        self.assertEqual(checksum, sum(raw))
        self.assertIn(f"androidboot.boot_part_uuid={NEW}\n".encode(), raw)
        for bad in (source[:-1], source[:-21] + b"x" + source[-20:],
                    initrd([f"androidboot.boot_part_uuid={OLD}\n"] * 2),
                    initrd([f"androidboot.boot_part_uuid={NEW}\n"])):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                usb_smoke.rebind_bootconfig(bad, OLD, NEW)
        with self.assertRaises(ValueError):
            usb_smoke.rebind_bootconfig(source, OLD, OLD)

    def test_gpt_new_unique_guids_same_layout(self):
        names = ("EFI-SYSTEM", "boot_a", "userdata")
        before = {n: {"guid": str(uuid.uuid4()), "type": str(uuid.uuid4()), "start": i * 4096,
                      "end": (i + 1) * 4096} for i, n in enumerate(names)}
        after = {n: row | {"guid": str(uuid.uuid4())} for n, row in before.items()}
        self.assertEqual(usb_smoke.validate_gpt_rewrite(before, after), after)
        mutants = [{**after, "userdata": after["userdata"] | {"guid": before["boot_a"]["guid"]}},
                   {**after, "userdata": after["userdata"] | {"guid": after["boot_a"]["guid"]}},
                   {**after, "boot_a": after["boot_a"] | {"start": 3}},
                   {name: value for name, value in after.items() if name != "boot_a"}]
        for bad in mutants:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                usb_smoke.validate_gpt_rewrite(before, bad)

    def test_both_entries_require_drain_nvme_and_stock_led_blacklist(self):
        extra = ["gpio_cros_ec", "rtc_cros_ec"]
        never = ["tpm", "spi_intel"]
        modules = extra + never + ["cros_ec_dev", "cros_ec_keyb", "cros_kbd_led_backlight"]
        common = ["module_blacklist=" + ",".join(modules), "initcall_blacklist=nvme_init,mei_me_driver_init", "efi=noruntime"]
        debug = common + ["earlycon=uart", "keep_bootcon", "printk.devkmsg=on"]
        quiet = common + ["loglevel=4"]
        def validate(d, q):
            usb_smoke.validate_entries(d, q, extra, never, devprofile.cmdline_within_limit, devprofile.boot_cmdline_len)
        validate(debug, quiet)
        variants = [(debug + ["initcall_blacklist=nvme_init"], quiet),
                    (debug, [x.replace("nvme_init,", "") for x in quiet]),
                    (debug, [x.replace("cros_ec_dev,", "") for x in quiet]),
                    (debug, [x.replace("cros_kbd_led_backlight", "cros_ec") for x in quiet]),
                    (debug, quiet + ["earlycon=uart"]),
                    (debug + ["padding=" + "x" * 2000], quiet)]
        for d, q in variants:
            with self.subTest(d=d, q=q), self.assertRaises(ValueError):
                validate(d, q)

    def test_entry_preflight_build_script(self):
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td) / "kano"
            shutil.copytree(ROOT / "profiles/kano", profile)
            text = (profile / "profile.toml").read_text()
            changes = {
                'kmods = ["ac", "gbos_wakefilter"]': 'kmods = ["ac", "gbos_wakefilter", "gbos_kbd_backlight"]',
                'modules = ["cros_ec_debugfs"': 'modules = ["cros_kbd_led_backlight", "cros_ec_debugfs"',
                'allow = ["cros_ec_proto", "cros_kbd_led_backlight"]': 'allow = ["cros_ec_proto"]',
            }
            for old, new in changes.items():
                self.assertEqual(text.count(old), 1)
                text = text.replace(old, new)
            (profile / "profile.toml").write_text(text)
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            fake_sgdisk = bin_dir / "sgdisk"
            fake_sgdisk.write_text("#!/bin/sh\nexit 0\n")
            fake_sgdisk.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
            for key in list(env):
                if key.startswith("GBOS_"):
                    env.pop(key)
            env.update(ENV | {"GBOS_PROFILE": str(profile), "GBOS_BASE": "x", "GBOS_INITRD": "x",
                              "GBOS_OUT": td + "/out", "GBOS_OTATOOLS": "x", "GBOS_AVB_KEY": "x",
                              "GBOS_INPUTS": td})
            for changes in ({"GBOS_EC": ""}, {"GBOS_SKIP_SELFTEST": "1"}, {"GBOS_DEFAULT_ENTRY": "lapis-debug.conf"}):
                result = subprocess.run(["bash", str(ROOT / "builder/build.sh")], env=env | changes,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("USB smoke requires", result.stdout + result.stderr)
            # A valid smoke configuration passes its new gate and reaches the existing KBD module input check.
            result = subprocess.run(["bash", str(ROOT / "builder/build.sh")], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("USB smoke requires", result.stdout + result.stderr)
            self.assertIn("GBOS_KMOD_KBD_BACKLIGHT", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
