#!/usr/bin/env python3
import hashlib
import os
from pathlib import Path
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'builder/lib'))
import readonly_update_engine as mod
import devprofile

P = {'model': {'id': 'amd-hx370'}, 'cpu_compat': {'patch_ifma': True}, 'services': {'readonly_update_engine': True}}
def sha(b): return hashlib.sha256(b).hexdigest()

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / mod.RC).parent.mkdir(parents=True)
        (self.root / mod.BINARY).parent.mkdir(parents=True)
        self.rc = (mod.ORIGINAL + '\n    class late_start\n').encode()
        self.bin = b'stock binary'; self.shim = b'reviewed shim'
        (self.root / mod.RC).write_bytes(self.rc)
        (self.root / mod.BINARY).write_bytes(self.bin)
        self.shimpath = self.root / 'shim'; self.shimpath.write_bytes(self.shim)
        self.pins = patch.multiple(mod, STOCK_RC_SHA256=sha(self.rc), STOCK_BINARY_SHA256=sha(self.bin), SHIM_SHA256=sha(self.shim))
        self.pins.start(); self.addCleanup(self.pins.stop)
        self.env = patch.dict(os.environ, {'GBOS_READONLY_UPDATE_ENGINE': str(self.shimpath)})
        self.env.start(); self.addCleanup(self.env.stop)
    def test_apply_and_readback(self):
        report = mod.apply(P, self.root)
        self.assertEqual(set(report), {mod.RC, mod.BINARY})
        self.assertEqual((self.root / mod.BINARY).read_bytes(), self.shim)
        self.assertNotIn(mod.ORIGINAL, (self.root / mod.RC).read_text())
        mod.verify(self.root, report)
        (self.root / mod.BINARY).write_bytes(b'wrong')
        with self.assertRaises(ValueError): mod.verify(self.root, report)
    def test_bad_stock_or_binary_fails_before_writes(self):
        for path in (self.root / mod.RC, self.root / mod.BINARY, self.shimpath):
            with self.subTest(path=path):
                old = path.read_bytes(); path.write_bytes(b'wrong')
                with self.assertRaises(ValueError): mod.apply(P, self.root)
                path.write_bytes(old)
                self.assertEqual((self.root / mod.RC).read_bytes(), self.rc)
                self.assertEqual((self.root / mod.BINARY).read_bytes(), self.bin)
    def test_unknown_extra_updater_fails(self):
        (self.root / 'system/etc/init/other.rc').write_text('service extra /system/bin/update_engine_nostats\n')
        with self.assertRaises(ValueError): mod.apply(P, self.root)
    def test_not_opted_in_is_noop_and_wrong_profile_refused(self):
        self.assertEqual(mod.apply({}, self.root), {})
        for p in ({**P, 'model': {'id':'kano'}}, {**P, 'cpu_compat': {'patch_ifma':False}}):
            with self.assertRaises(ValueError): mod.apply(p, self.root)
    def test_profile_loader_rejects_wrong_profile_and_missing_ifma(self):
        for name, mutation in (('kano', '\n[services]\nreadonly_update_engine = true\n'),
                               ('amd-hx370', None)):
            with self.subTest(name=name):
                target = self.root / name
                shutil.copytree(ROOT / 'profiles' / name, target)
                profile = target / 'profile.toml'
                data = profile.read_text()
                if mutation is None:
                    data = data.replace('patch_ifma = true', 'patch_ifma = false')
                else:
                    data += mutation
                profile.write_text(data)
                with self.assertRaisesRegex(devprofile.ProfileError, 'requires amd-hx370 and cpu_compat.patch_ifma'):
                    devprofile.load(target)
    def test_real_profiles(self):
        self.assertTrue(devprofile.load(ROOT / 'profiles/amd-hx370')['services']['readonly_update_engine'])
        self.assertFalse(devprofile.load(ROOT / 'profiles/kano')['services']['readonly_update_engine'])

if __name__ == '__main__': unittest.main()
