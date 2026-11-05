#!/usr/bin/env python3
"""Controller plus actual regular-image backend only; no block-device safety proof."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


controller = load('controller_image_integration', ROOT / 'install/stick/universal-controller.py')
fixtures = load('controller_image_fixtures', ROOT / 'tests/stick/shared-operation-integration.py')
gpt = load('controller_image_gpt', ROOT / 'tests/stick/gptlib.py')
m = fixtures.m
MIB = 1 << 20
LABEL = '/dev/nvme0n1'  # Label only; adapter never opens this path.


class ImageAdapter:
    """Map controller contracts onto exactly one owned regular test image."""
    def __init__(self, target):
        self.target = target
        self.calls = []
        st = target.lstat()
        if not target.is_file() or target.is_symlink() or not 0 < st.st_size <= 128 * MIB:
            raise ValueError('fixture target must be a small regular file')
        self.facts = {'rdev': '240:1', 'syspath': '/sys/devices/fixture/nvme0n1',
                      'nbytes': st.st_size, 'logical_sector_size': 512,
                      'identity_kind': 'nvme-namespace', 'device_identity': 'a' * 64}

    def observe_block_target(self, label):
        if label != LABEL:
            raise ValueError('unexpected synthetic label')
        return dict(self.facts)

    def plan_observed_block(self, root, catalog, digest, label, *, dmi_root):
        self.observe_block_target(label)
        return m.plan_observed_image(root, catalog, digest, str(self.target), dmi_root)

    def _write(self, operation, root, catalog, digest, label, expected_target, dmi_root):
        if self.observe_block_target(label) != expected_target:
            raise ValueError('synthetic facts changed')
        self.calls.append(operation)
        function = m.install_observed_image if operation == 'fresh' else m.apply_observed_image
        result = function(root, catalog, digest, str(self.target), dmi_root)
        wanted = 'INSTALLED_SYNTHETIC_IMAGE' if operation == 'fresh' else 'APPLIED_SYNTHETIC_IMAGE'
        if result.get('result') != wanted or result.get('physical_write_allowed') is not False:
            raise ValueError('not a synthetic result')
        return dict(result, result='INSTALLED_OBSERVED_BLOCK' if operation == 'fresh' else 'APPLIED_OBSERVED_BLOCK')

    def install_observed_block(self, root, catalog, digest, label, *, expected_target, dmi_root):
        return self._write('fresh', root, catalog, digest, label, expected_target, dmi_root)

    def apply_observed_block(self, root, catalog, digest, label, *, expected_target, dmi_root):
        return self._write('update', root, catalog, digest, label, expected_target, dmi_root)


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'O_PATH') and hasattr(os, 'memfd_create')
                     and all(shutil.which(t) for t in ('mformat', 'mcopy', 'mmd', 'mdir', 'mdel', 'minfo', 'lz4')),
                     'requires Linux memfd/O_PATH/mtools/lz4')
class ControllerImages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.Operation.setUpClass.__func__(cls)
        cls.addClassCleanup(cls.tmp.cleanup)

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='gbos-controller-image-')
        self.addCleanup(self.work.cleanup)
        self.case = Path(self.work.name).resolve()
        self.target = self.case / 'target.img'
        self.scratch = self.case / 'scratch'
        self.scratch.mkdir()
        self.env = patch.dict(os.environ, TMPDIR=str(self.scratch))
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_controller(self, operation, profile, adapter, confirmation):
        replies = iter(('1\n', confirmation))
        return controller.run(operation, snapshot=lambda: (profile, '16452207', self.expected),
                              shared=adapter, enumerate_targets=lambda: [LABEL],
                              read_line=lambda limit: next(replies), write=lambda text: None,
                              bundle=str(self.root), dmi=self.root / (profile + '-dmi'))

    def installed(self):
        return fixtures.Operation.installed(self)

    def test_fresh_and_update_each_profile(self):
        for profile in ('kano', 'amd-hx370'):
            with self.subTest(profile=profile):
                source = self.root / (profile + '.img')
                source_hash = fixtures.digest(source)
                original_header, original = gpt.read_gpt(source)
                original_user = next(p for p in original.values() if p['name'] == 'userdata')
                with self.target.open('wb') as f:
                    f.truncate(source.stat().st_size + 24 * MIB)
                    f.seek(original_user['first'] * 512)
                    f.write(b'PREEXISTING-USER-DATA')
                adapter = ImageAdapter(self.target)
                result = self.run_controller('fresh', profile, adapter, f'INSTALL {LABEL}\n')
                self.assertEqual(result, {'status': 'COMPLETED', 'hardware': 'NOT_VERIFIED'})
                header, parts = gpt.read_gpt(self.target, backup=True)
                new_ids = {header['disk'], *(p['guid'] for p in parts.values())}
                old_ids = {original_header['disk'], *(p['guid'] for p in original.values())}
                self.assertEqual(len(new_ids), len(parts) + 1)
                self.assertFalse(new_ids & old_ids)
                userdata = next(p for p in parts.values() if p['name'] == 'userdata')
                self.assertEqual(gpt.sha(self.target, userdata['first'] * 512, 16 * MIB),
                                 hashlib.sha256(bytes(16 * MIB)).hexdigest())
                with self.target.open('r+b') as f:
                    f.seek(userdata['first'] * 512)
                    f.write(b'USER-DATA-MUST-SURVIVE-UPDATE')
                before = self.installed()
                self.assertEqual(before[0], 'a')
                result = self.run_controller('update', profile, adapter, f'UPDATE {LABEL}\n')
                self.assertEqual(result, {'status': 'COMPLETED', 'hardware': 'NOT_VERIFIED'})
                after = self.installed()
                self.assertEqual(after[0], 'b')
                self.assertEqual(after[1:3], before[1:3])
                self.assertEqual(after[4]['prev-lapis-quiet.conf'], 'a')
                self.assertEqual(adapter.calls, ['fresh', 'update'])
                self.assertEqual(fixtures.digest(source), source_hash)
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_cancel_never_calls_write_backend_or_changes_target(self):
        for profile in ('kano', 'amd-hx370'):
            for operation in ('fresh', 'update'):
                with self.subTest(profile=profile, operation=operation):
                    shutil.copyfile(self.root / (profile + '-template.img'), self.target)
                    before = fixtures.digest(self.target)
                    adapter = ImageAdapter(self.target)
                    result = self.run_controller(operation, profile, adapter, '\n')
                    self.assertEqual(result, {'status': 'CANCELLED', 'hardware': 'NOT_VERIFIED'})
                    self.assertEqual(adapter.calls, [])
                    self.assertEqual(fixtures.digest(self.target), before)


if __name__ == '__main__':
    unittest.main()
