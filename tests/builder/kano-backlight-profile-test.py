#!/usr/bin/env python3
"""Opt-in KANO backlight profile guards and drain cmdline budget; no hardware/image build."""
import ast
import contextlib
import importlib.util
import io
import hashlib
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "builder" / "lib"))
import devprofile

ORIGINAL = ROOT / "profiles" / "kano"
PROFILE = (ORIGINAL / "profile.toml").read_text()
LED_PATH = "/sys/devices/platform/GOOG0004:00/GOOG0002:00/leds"
NEW = "gbos_kbd_backlight"
STOCK = "cros_kbd_led_backlight"
DUPLICATE = "cgroup_disable=pressure"


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise AssertionError(f"expected one occurrence of {old!r}, got {text.count(old)}")
    return text.replace(old, new, 1)


def candidate():
    text = replace_once(PROFILE, 'kmods = ["ac", "gbos_wakefilter"]',
                        'kmods = ["ac", "gbos_wakefilter", "gbos_kbd_backlight"]')
    text = replace_once(text, 'modules = ["cros_ec_debugfs",',
                        'modules = ["cros_kbd_led_backlight", "cros_ec_debugfs",')
    text = replace_once(text, 'allow = ["cros_ec_proto", "cros_kbd_led_backlight"]',
                        'allow = ["cros_ec_proto"]')
    return replace_once(text, 'stock_drop = ["snd_intel_sdw_acpi',
                        'stock_drop = ["cgroup_disable=pressure", "snd_intel_sdw_acpi')


def image_ec_helpers():
    tree = ast.parse((ROOT / "builder" / "image.py").read_text())
    names = {"ec_released", "ec_blacklist", "ec_variant_entries"}
    values = {"EC_CORE", "EC_RELEASE", "EC_EXTRA_BLOCK", "EC_NEVER", "EC_DRAIN", "DRAIN_DEBUG_DROP"}
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in names) or
             (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in values for t in n.targets))]
    namespace = {}
    exec(compile(ast.Module(nodes, []), "image.py", "exec"), namespace)
    return namespace


def real_vcmd():
    tree = ast.parse((ROOT / "tests" / "builder" / "profile-test.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "REAL_VCMD" for t in n.targets))
    value = ast.literal_eval(node.value)
    # image.py pins the exact vendor_boot cmdline. Verify the fixture, too.
    if hashlib.sha256(value.encode()).hexdigest() != "311e95df564037a863af096fb6265f15e8340e4b60e2736676e43d81d876aee7":
        raise AssertionError("vendor_boot cmdline fixture differs from image.py pin")
    return value


class BacklightProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gbos-kano-backlight-profile-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.original_bytes = (ORIGINAL / "profile.toml").read_bytes()

    def load(self, text, name="kano"):
        root = self.base / name
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(ORIGINAL, root)
        (root / "profile.toml").write_text(text)
        return devprofile.load(root)

    def refuses(self, text, message, name="kano"):
        with self.assertRaisesRegex(devprofile.ProfileError, re.escape(message)):
            self.load(text, name)

    def test_default_profile_unchanged_and_candidate_loads(self):
        default = devprofile.load(ORIGINAL)
        profile = self.load(candidate())
        self.assertNotIn(NEW, default["kmods"])
        self.assertIn(STOCK, default["blacklist"]["allow"])
        self.assertIn(NEW, profile["kmods"])
        self.assertIn(STOCK, profile["blacklist"]["modules"])
        self.assertNotIn(STOCK, profile["blacklist"]["allow"])
        self.assertIn("cros_ec_proto", profile["blacklist"]["allow"])
        self.assertEqual(profile["input"]["kbd_backlight_leds"], LED_PATH)
        self.assertEqual((ORIGINAL / "profile.toml").read_bytes(), self.original_bytes)

    def test_reject_other_model(self):
        self.refuses(replace_once(candidate(), 'id = "kano"', 'id = "other"'),
                     "restricted to kano", name="other")

    def test_reject_without_stock_blacklist(self):
        self.refuses(replace_once(candidate(), '"cros_kbd_led_backlight", "cros_ec_debugfs"',
                                  '"cros_ec_debugfs"'), "stock keyboard backlight driver blacklisted")

    def test_reject_stock_also_allowed(self):
        self.refuses(replace_once(candidate(), 'allow = ["cros_ec_proto"]',
                                  'allow = ["cros_ec_proto", "cros_kbd_led_backlight"]'),
                     "blacklist.allow overlaps")

    def test_reject_provider_not_allowed(self):
        self.refuses(replace_once(candidate(), 'allow = ["cros_ec_proto"]', 'allow = []'),
                     "requires cros_ec_proto allowed")

    def test_reject_own_driver_blacklisted(self):
        self.refuses(replace_once(candidate(), 'debug_add = ["8250_dw",',
                                  'debug_add = ["gbos_kbd_backlight", "8250_dw",'),
                     "cannot be blacklisted")

    def test_reject_missing_or_wrong_led_path(self):
        for path in ("", "/sys/devices/platform/other/leds"):
            with self.subTest(path=path):
                self.refuses(replace_once(candidate(), f'kbd_backlight_leds = "{LED_PATH}"',
                                          f'kbd_backlight_leds = "{path}"'), "requires the KANO keyboard LED path")

    def test_reject_duplicate_new_module(self):
        self.refuses(replace_once(candidate(), '"gbos_kbd_backlight"]',
                                  '"gbos_kbd_backlight", "gbos_kbd_backlight"]'),
                     "expected distinct names")

    def test_drain_budget_and_blacklist_invariants(self):
        profile = self.load(candidate())
        default = devprofile.load(ORIGINAL)
        vcmd = real_vcmd()
        # Fixed GKI always prepends this reviewed config string before the loader
        # command line: dropping only the duplicate token changes no behavior.
        self.assertIn(DUPLICATE, devprofile.KERNEL_CONFIG_CMDLINE.split())
        self.assertEqual(vcmd.split().count(DUPLICATE), 1)
        helpers = image_ec_helpers()
        entries = devprofile.make_entries(profile, vcmd)
        default_entries = devprofile.make_entries(default, vcmd)
        current = helpers["ec_variant_entries"](entries["normal"], entries["debug"], entries["quiet"], ["drain"])
        baseline = helpers["ec_variant_entries"](default_entries["normal"], default_entries["debug"],
                                                  default_entries["quiet"], ["drain"])
        for index, name in enumerate(("normal", "debug", "quiet")):
            with self.subTest(name=name):
                opts, before = current[index], baseline[index]
                self.assertNotIn(DUPLICATE, opts)
                self.assertIn(DUPLICATE, before)
                before_bl = next(x for x in before if x.startswith("module_blacklist="))
                after_bl = next(x for x in opts if x.startswith("module_blacklist="))
                before_set = set(before_bl.split("=", 1)[1].split(","))
                after_set = set(after_bl.split("=", 1)[1].split(","))
                self.assertEqual(after_set, before_set | {STOCK})
                self.assertTrue(set(helpers["EC_NEVER"]) <= after_set)
                self.assertTrue({"cros_ec_dev", "cros_ec_keyb"} <= after_set)
                self.assertTrue({"cros_ec", "cros_ec_lpcs"}.isdisjoint(after_set))
                if name != "normal":
                    length = devprofile.boot_cmdline_len(opts)
                    self.assertTrue(devprofile.cmdline_within_limit(length), (name, length))
                    self.assertGreaterEqual(devprofile.X86_BOOT_CMDLINE_MAX - length,
                                            devprofile.CMDLINE_HEADROOM)
                    self.assertEqual(length, devprofile.boot_cmdline_len(before) - 1)


class KeyboardIdcTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gbos-kano-kbd-idc-")
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        self.idc = self.tree / "usr/idc/AT_Translated_Set_2_keyboard.idc"
        self.idc.parent.mkdir(parents=True)
        self.ueventd = self.tree / "etc/ueventd/ueventd.kano.rc"
        self.ueventd.parent.mkdir(parents=True)
        self.cil = self.tree / "etc/selinux/vendor_sepolicy.cil"
        self.cil.parent.mkdir(parents=True)
        self.gline = '(genfscon sysfs "/devices/platform/GOOG0002:00/leds" (u object_r sysfs_keyboard_backlight ((s0) (s0))))'
        self.cil.write_text(self.gline + "\n")
        spec = importlib.util.spec_from_file_location("kano_vendor", ROOT / "profiles/kano/vendor.py")
        self.hooks = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hooks)
        self.old = "device.additionalSysfsLedsNode = " + self.hooks.KB_OLD + "/chromeos::kbd_backlight\n"
        self.vmap = {"etc/ueventd/ueventd.fatcat.rc": {"uid": 0, "gid": 0}}
        self.ctx = SimpleNamespace(tree=self.tree, vmap=self.vmap,
                                   prof={"input": {"kbd_backlight_leds": LED_PATH}, "model": {"id": "kano"}})

    def run_hook(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.hooks.kbd_backlight(self.ctx)

    def test_inherited_levels_removed_other_lines_preserved(self):
        tail = ("# keyboard.backlight.brightnessLevels = commented\n"
                "keyboard.backlight.brightnessLevels.extra = leave\n"
                "keyboard.backlight.other = leave\n")
        self.idc.write_text(self.old + "keyboard.backlight.brightnessLevels = 0,6,12,18,24,30,36,42,48,54\n" + tail)
        self.run_hook()
        self.assertEqual(self.idc.read_text(), self.old.replace(self.hooks.KB_OLD, LED_PATH) + tail)
        self.assertIn(LED_PATH + "/* brightness 0660 system system", self.ueventd.read_text())
        self.assertIn(LED_PATH[4:], self.cil.read_text())
        self.assertEqual(self.vmap["etc/ueventd/ueventd.kano.rc"], self.vmap["etc/ueventd/ueventd.fatcat.rc"])

    def test_absent_levels_is_valid(self):
        self.idc.write_text(self.old + "keyboard.other = yes\n")
        self.run_hook()
        self.assertEqual(self.idc.read_text(), self.old.replace(self.hooks.KB_OLD, LED_PATH) + "keyboard.other = yes\n")

    def test_whitespace_variant_and_final_line_removed(self):
        self.idc.write_text(self.old + "  \tkeyboard.backlight.brightnessLevels\t=0,54")
        self.run_hook()
        self.assertEqual(self.idc.read_text(), self.old.replace(self.hooks.KB_OLD, LED_PATH))

    def test_duplicate_active_assignments_refused_before_output(self):
        original = self.old + ("keyboard.backlight.brightnessLevels = 0,54\n"
                               "  keyboard.backlight.brightnessLevels=0,100\n")
        self.idc.write_text(original)
        cil_before = self.cil.read_bytes()
        with self.assertRaisesRegex(SystemExit, "brightness levels not unique"):
            self.run_hook()
        self.assertEqual(self.idc.read_text(), original)
        self.assertEqual(self.cil.read_bytes(), cil_before)
        self.assertFalse(self.ueventd.exists())
        self.assertNotIn("etc/ueventd/ueventd.kano.rc", self.vmap)

    def test_empty_led_profile_is_noop(self):
        self.idc.write_text(self.old + "keyboard.backlight.brightnessLevels = 0,54\n")
        before = self.idc.read_bytes(), self.cil.read_bytes(), dict(self.vmap)
        self.ctx.prof["input"]["kbd_backlight_leds"] = ""
        self.run_hook()
        self.assertEqual((self.idc.read_bytes(), self.cil.read_bytes(), self.vmap), before)
        self.assertFalse(self.ueventd.exists())

    def test_invalid_led_path_and_genfscon_still_refused(self):
        self.idc.write_text("unrelated = 1\n")
        with self.assertRaisesRegex(SystemExit, "keyboard idc not as expected"):
            self.run_hook()
        self.assertFalse(self.ueventd.exists())
        self.idc.write_text(self.old)
        self.cil.write_text("invalid policy\n")
        with self.assertRaisesRegex(SystemExit, "keyboard genfscon not as expected"):
            self.run_hook()



if __name__ == "__main__":
    unittest.main(verbosity=2)
