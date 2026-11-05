#!/usr/bin/env python3
"""Synthetic checks for the opt-in backlight build gates; no image or hardware."""
import ast
import hashlib
import importlib.util
import os
import shutil
import subprocess
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'builder/lib'))
import devprofile

class BacklightBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / 'kmod').mkdir()
        self.script = self.base / 'kmod/build-gbos_kbd_backlight.sh'
        self.ko = self.base / 'module.ko'
        self.ko.write_bytes(b'synthetic module')
        self.pin = hashlib.sha256(self.ko.read_bytes()).hexdigest()
        self.script.write_text('KBD_BACKLIGHT_KO_SHA=' + self.pin + '\n')
        self.prof = {'kmods': ['gbos_kbd_backlight']}
        self.env = {'GBOS_KMOD_KBD_BACKLIGHT': str(self.ko)}

    def check(self, ec='drain'):
        devprofile.check_kbd_backlight_inputs(self.prof, ec, self.env, self.base)

    def test_valid_and_default(self):
        self.check()
        self.prof = {'kmods': []}
        with self.assertRaisesRegex(ValueError, 'profile does not list'):
            self.check()
        self.env.clear()
        self.check('')

    def test_ec_and_kernel(self):
        for value in ('', 'sensors', 'drain,sensors'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'requires GBOS_EC=drain'):
                self.check(value)
        self.env['GBOS_DEV_KERNEL_DIR'] = '/tmp/kernel'
        with self.assertRaisesRegex(ValueError, 'stock kernel'):
            self.check()

    def test_pin_and_file_guards(self):
        for pin in ('', '0'*63, 'A'*64, '0'*64 + '\nKBD_BACKLIGHT_KO_SHA=' + self.pin):
            self.script.write_text('KBD_BACKLIGHT_KO_SHA=' + pin + '\n')
            with self.subTest(pin=pin), self.assertRaisesRegex(ValueError, 'valid pinned'):
                self.check()
        self.script.unlink()
        with self.assertRaisesRegex(ValueError, 'valid pinned'):
            self.check()
        self.script.write_text('KBD_BACKLIGHT_KO_SHA=' + self.pin + '\n')
        for path in ('', str(self.base), str(self.base / 'missing')):
            self.env['GBOS_KMOD_KBD_BACKLIGHT'] = path
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'pinned module'):
                self.check()
        self.ko.write_bytes(b'wrong')
        self.env['GBOS_KMOD_KBD_BACKLIGHT'] = str(self.ko)
        with self.assertRaisesRegex(ValueError, 'pinned module'):
            self.check()

    def test_each_real_stage_rejects_wrong_ec_and_unlisted_module(self):
        spec = importlib.util.spec_from_file_location("profile_fixture", ROOT / "tests/builder/kano-backlight-profile-test.py")
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        profile = self.base / "kano"
        shutil.copytree(ROOT / "profiles/kano", profile)
        (profile / "profile.toml").write_text(fixture.candidate())
        env = os.environ.copy()
        env.update({"GBOS_BASE": "unused", "GBOS_INITRD": "unused", "GBOS_OUT": str(self.base / "out"),
                    "GBOS_OTATOOLS": "unused", "GBOS_AVB_KEY": "unused", "GBOS_INPUTS": "unused",
                    "GBOS_FIRMWARE": "unused", "GBOS_AUDIO_SRC": "unused", "GBOS_PROFILE": str(profile),
                    "GBOS_PCI_STUB_KO": "unused", "GBOS_EC": "", "GBOS_KMOD_KBD_BACKLIGHT": str(self.ko)})
        env.pop("GBOS_DEV_KERNEL_DIR", None)
        for name in ("build.sh", "vendor.sh", "image.py"):
            command = ([sys.executable] if name.endswith(".py") else ["bash"]) + [str(ROOT / "builder" / name)]
            with self.subTest(stage=name):
                result = subprocess.run(command, env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires GBOS_EC=drain", result.stdout + result.stderr)
                env["GBOS_PROFILE"] = str(ROOT / "profiles/kano")
                result = subprocess.run(command, env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("profile does not list", result.stdout + result.stderr)
                env["GBOS_PROFILE"] = str(profile)

    def test_stage_wiring_and_order(self):
        for name in ('build.sh', 'vendor.sh', 'image.py'):
            text = (ROOT / 'builder' / name).read_text()
            self.assertIn('devprofile.check_kbd_backlight_inputs(', text)
        vendor = (ROOT / 'builder/vendor.sh').read_text()
        first = 'exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/modules cros-ec-proto'
        second = 'exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/kano gbos_kbd_backlight'
        self.assertLess(vendor.index(first), vendor.index(second))
        self.assertLess(vendor.index(second), vendor.index('/system/bin/sh /vendor/etc/kano-kbd-modules.sh'))
        self.assertIn('exec_background u:r:kano_kmsg_logcat:s0 -- /system/bin/sh /vendor/etc/kano-kbd-modules.sh', vendor)
        self.assertIn('for k in KANO_KMODS if k != "gbos_kbd_backlight"', vendor)
        self.assertEqual(vendor.count('"ac", "gbos_wakefilter", "gbos_kbd_backlight"'), 2)
        self.assertIn('$TMP/firmware-vendor_dlkm/lib/modules/cros-ec-proto.ko', vendor)
        self.assertIn('5ddce2fcfe24e9f1451783bb42d7bbdb23b99955fae211c8bc1ae8f32098ad48', vendor)
        image = (ROOT / 'builder/image.py').read_text()
        self.assertIn('7816c5d5b3500093e597b7362ecab103d5a32141b100d3eb7a8b33221fce7f41', image)
        self.assertIn('"kbd: module loaded"', image)
        self.assertIn('"kbd: stock module not loaded"', image)

    def test_ssdt_opt_in(self):
        tree = ast.parse((ROOT / 'builder/image.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'ec_ssdt')
        ns = {'struct': struct}
        exec(compile(ast.Module([fn], []), 'image.py', 'exec'), ns)
        old, new = ns['ec_ssdt'](), ns['ec_ssdt'](True)
        self.assertNotIn(b'GOOG0002', old)
        self.assertIn(b'GOOG0002', new)
        self.assertEqual(sum(old) & 255, 0)
        self.assertEqual(sum(new) & 255, 0)

    def test_vendor_inventory_tracks_optional_snapshot(self):
        vendor = (ROOT / 'builder/vendor.sh').read_text()
        start = vendor.index('expect_new = sorted(set(')
        end = vendor.index('if prof["display"]["stack"]', start)
        code = compile(vendor[start:end], 'vendor-inventory', 'exec')
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                kmods = ['ac', 'gbos_wakefilter']
                if enabled:
                    kmods.append('gbos_kbd_backlight')
                ns = {'os': os, 'p_new': [], 'fw_new': [], 'prof': {'kmods': kmods}}
                exec(code, ns)
                self.assertEqual('etc/kano-kbd-modules.sh' in ns['expect_new'], enabled)
                self.assertEqual('lib/kano/gbos_kbd_backlight.ko' in ns['expect_new'], enabled)
                self.assertNotIn('etc/unrelated.sh', ns['expect_new'])

    def test_actual_module_snapshot_and_qemu_gate(self):
        tree = ast.parse((ROOT / 'builder/image.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'kbd_module_log_checks')
        ns = {'re': re}
        exec(compile(ast.Module([fn], []), 'image.py', 'exec'), ns)
        check = ns['kbd_module_log_checks']
        script = (ROOT / 'builder/kmod/kano-kbd-modules.sh').read_text()
        modules, kmsg = self.base / 'modules', self.base / 'kmsg'
        script = script.replace('/proc/modules', str(modules)).replace('/dev/kmsg', str(kmsg))
        runner = self.base / 'snapshot.sh'
        runner.write_text(script)
        new = 'gbos_kbd_backlight 123 0 - Live 0x0000\n'
        stock = 'cros_kbd_led_backlight 42 0 - Live 0x0000\n'
        for contents, expected in ((new, (True, True)), ('other 1 0 - Live 0\n', (False, True)),
                                   (new + stock, (True, False)), (stock, (False, False))):
            with self.subTest(contents=contents):
                modules.write_text(contents)
                result = subprocess.run(['sh', str(runner)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                log = '<14>[  30.1][ T1] ' + kmsg.read_text().removeprefix('<6>').strip() + '\r\n'
                checks = check(log)
                self.assertEqual((checks['kbd: module loaded'], checks['kbd: stock module not loaded']), expected)
        self.assertEqual(tuple(check('no snapshot').values()), (False, False))
        self.assertEqual(tuple(check('gbos-kbd-modules: new=1 stock=0\n'
                                     'ueventd: Loaded kernel module /vendor/lib/modules/cros-kbd-led-backlight.ko\n').values()),
                         (True, False))
        modules.unlink()
        kmsg.unlink()
        self.assertNotEqual(subprocess.run(['sh', str(runner)], capture_output=True).returncode, 0)
        self.assertFalse(kmsg.exists())

if __name__ == '__main__':
    unittest.main(verbosity=2)
