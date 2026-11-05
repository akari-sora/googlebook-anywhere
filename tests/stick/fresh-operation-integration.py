#!/usr/bin/env python3
"""Fresh catalog/DMI operations on private miniature files; never devices."""
import hashlib
import importlib.util
import json
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
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fixtures = load('fresh_fixtures', ROOT / 'tests/stick/shared-operation-integration.py')
gpt = load('fresh_independent_gpt', ROOT / 'tests/stick/gptlib.py')
m, MIB = fixtures.m, 1 << 20
GAP, TAIL = b'OLD-TARGET-GAP-IS-NOT-SECURELY-ERASED', b'OLD-TARGET-USERDATA-TAIL'


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'memfd_create')
                     and all(shutil.which(t) for t in ('mformat', 'mcopy', 'mmd', 'mdir', 'mdel', 'minfo', 'lz4')),
                     'requires Linux memfd/O_PATH/mtools/lz4')
class FreshOperation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.Operation.setUpClass.__func__(cls)
        cls.addClassCleanup(cls.tmp.cleanup)
        catalog = json.loads((cls.root / 'catalog.json').read_bytes())
        cls.sources = {}
        for item in catalog['payloads']:
            source = cls.root / item['image']['path']
            _, parts = gpt.read_gpt(source)
            data = next(p for p in parts.values() if p['name'] == 'userdata')
            with source.open('r+b') as out:
                out.seek(data['first'] * 512)
                out.write(b'SOURCE-USERDATA-MUST-NOT-BE-COPIED')
                out.seek(MIB - 128)
                out.write(b'SOURCE-GAP-MUST-NOT-BE-COPIED')
                out.flush()
                os.fsync(out.fileno())
            item['image']['sha256'] = fixtures.digest(source)
            cls.sources[item['profile']] = item['image']['sha256']
        raw = json.dumps(catalog).encode()
        (cls.root / 'catalog.json').write_bytes(raw)
        cls.expected = hashlib.sha256(raw).hexdigest()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='gbos-fresh-case-')
        self.addCleanup(self.work.cleanup)
        self.case = Path(self.work.name)
        self.scratch = self.case / 'scratch'
        self.scratch.mkdir()
        self.target = self.case / 'target.img'
        self.size = (self.root / 'kano.img').stat().st_size + 24 * MIB
        self.make_target(self.target)
        env = patch.dict(os.environ, TMPDIR=str(self.scratch))
        env.start()
        self.addCleanup(env.stop)

    def make_target(self, path):
        with path.open('wb') as out:
            out.truncate(self.size)
            out.seek(MIB - 128)
            out.write(GAP)
            out.seek((self.size // 512 - 33) * 512 - len(TAIL))
            out.write(TAIL)

    def apply(self, profile='kano', target=None, expected=None):
        return m.install_observed_image(str(self.root), 'catalog.json', expected or self.expected,
                                         str(target or self.target), self.root / (profile + '-dmi'))

    def assert_clean(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        for flag in (m.update.WRITING, m.update.WINDOW, m.update.STICK):
            self.assertEqual(flag, [])

    def test_both_profiles_new_guids_data_bootconfig_and_unchanged_source(self):
        for profile in ('kano', 'amd-hx370'):
            with self.subTest(profile=profile):
                self.make_target(self.target)
                source = self.root / (profile + '.img')
                old_header, old = gpt.read_gpt(source)
                result = self.apply(profile)
                self.assertEqual(result['result'], 'INSTALLED_SYNTHETIC_IMAGE')
                self.assertEqual(result['profile'], profile)
                self.assertFalse(result['physical_write_allowed'])
                header, parts = gpt.read_gpt(self.target, backup=True)
                new_guids = {header['disk'], *(p['guid'] for p in parts.values())}
                self.assertEqual(len(new_guids), len(parts) + 1)
                self.assertFalse(new_guids & {old_header['disk'], *(p['guid'] for p in old.values())})
                self.assertEqual(set(parts), set(old))
                for number, part in parts.items():
                    for key in ('name', 'first', 'type', 'attrs'):
                        self.assertEqual(part[key], old[number][key])
                    if part['name'] == 'userdata':
                        self.assertEqual(part['last'], header['last'])
                        self.assertGreater(part['last'], old[number]['last'])
                        self.assertEqual(gpt.sha(self.target, part['first'] * 512, 16 * MIB),
                                         hashlib.sha256(bytes(16 * MIB)).hexdigest())
                    else:
                        self.assertEqual(part['last'], old[number]['last'])
                        if part['name'] != 'EFI-SYSTEM':
                            start, length = part['first'] * 512, (part['last'] - part['first'] + 1) * 512
                            self.assertEqual(gpt.sha(self.target, start, length), gpt.sha(source, start, length))
                with self.target.open('rb') as target:
                    target.seek(MIB - 128)
                    self.assertEqual(target.read(len(GAP)), GAP)
                    target.seek((header['last'] + 1) * 512 - len(TAIL))
                    self.assertEqual(target.read(len(TAIL)), TAIL)
                disk = m.update.Disk(str(self.target))
                try:
                    inst = m.update.Installed(disk, str(self.case))
                    self.assertEqual(inst.current, 'a')
                    boot = next(p for p in parts.values() if p['name'] == 'boot_a')
                    bc = gpt.bootconfig(inst.fat.read('/initrd.img'))
                    self.assertIn(b'androidboot.boot_part_uuid=' + boot['guid'].encode(), bc.splitlines())
                    self.assertEqual(set(inst.entries), {'lapis-quiet.conf', 'lapis-debug.conf'})
                    for entry in inst.entries.values():
                        self.assertNotIn('nvme_init', entry['options'])
                        self.assertIn('efi=noruntime', entry['options'])
                finally:
                    os.close(disk.fd)
                self.assertEqual(fixtures.digest(source), self.sources[profile])
                self.assert_clean()

    def test_bad_catalog_alias_small_target_and_test_environment_refuse_unchanged(self):
        before = fixtures.digest(self.target)
        with self.assertRaises(ValueError):
            self.apply(expected='0' * 64)
        with patch.dict(os.environ, GBOS_UPDATE_TEST_FAIL_AT=''):
            with self.assertRaisesRegex(ValueError, 'test environment'):
                self.apply()
        with self.assertRaises(m.update.Refuse):
            self.apply(target=self.root / 'kano.img')
        small = self.case / 'small.img'
        small.write_bytes(bytes(MIB))
        small_before = fixtures.digest(small)
        with self.assertRaises((ValueError, m.update.Refuse)):
            self.apply(target=small)
        self.assertEqual(fixtures.digest(small), small_before)
        self.assertEqual(fixtures.digest(self.target), before)
        self.assert_clean()

    def test_rw_handover_swap_refuses_without_writing_either_image(self):
        old, replacement = self.case / 'original.img', self.case / 'replacement.img'
        self.make_target(replacement)
        before, original_disk = fixtures.digest(self.target), m.update.Disk
        def swap(*args, **kwargs):
            self.target.rename(old)
            replacement.rename(self.target)
            return original_disk(*args, **kwargs)
        with patch.object(m.update, 'Disk', side_effect=swap):
            with self.assertRaisesRegex(ValueError, 'target changed'):
                self.apply()
        self.assertEqual(fixtures.digest(old), before)
        self.assertEqual(fixtures.digest(self.target), before)
        self.assert_clean()

    def test_write_exception_and_cancel_release_resources(self):
        with patch.object(m.update.Disk, 'pwrite', side_effect=RuntimeError('injected write failure')):
            with self.assertRaises(m.update.Failed) as failed:
                self.apply()
        self.assertIsInstance(failed.exception.__cause__, RuntimeError)
        self.assertIn('target may be modified', str(failed.exception))
        self.assert_clean()
        with patch.object(m.update.Disk, 'pwrite', side_effect=KeyboardInterrupt('cancel fresh write')):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        self.assert_clean()

    def test_postwrite_swap_never_redirects_and_reports_partial_failure(self):
        old, replacement = self.case / 'opened.img', self.case / 'replacement.img'
        self.make_target(replacement)
        before = fixtures.digest(replacement)
        pwrite, changed = m.update.Disk.pwrite, []
        def swap(disk, offset, data):
            result = pwrite(disk, offset, data)
            if not changed and offset >= MIB and any(data):
                self.target.rename(old)
                replacement.rename(self.target)
                changed.append(True)
            return result
        with patch.object(m.update.Disk, 'pwrite', swap):
            with self.assertRaises(m.update.Failed) as failed:
                self.apply()
        self.assertTrue(changed)
        self.assertIn('may be modified', str(failed.exception))
        self.assertEqual(fixtures.digest(self.target), before)
        self.assertNotEqual(fixtures.digest(old), before)
        self.assert_clean()


    def test_parent_flags_preserved_and_own_partial_write_classified(self):
        flags = (m.update.WRITING, m.update.WINDOW, m.update.STICK)
        original_flags = [list(flag) for flag in flags]
        markers = [object() for _ in flags]
        for flag, marker in zip(flags, markers):
            flag.append(marker)
        before = fixtures.digest(self.target)
        original_write, changed = m.update.Disk.pwrite, []

        def fail_after_payload_write(disk, offset, data):
            result = original_write(disk, offset, data)
            if offset >= MIB and any(data) and not changed:
                disk.sync_drop()
                changed.append(True)
                raise ValueError('injected current-operation partial write')
            return result

        try:
            with self.assertRaises(ValueError):
                self.apply(expected='0' * 64)
            self.assertEqual(fixtures.digest(self.target), before)
            with patch.object(m.update.Disk, 'pwrite', fail_after_payload_write):
                with self.assertRaises(m.update.Failed) as failed:
                    self.apply()
            self.assertTrue(changed)
            self.assertIsInstance(failed.exception.__cause__, ValueError)
            self.assertIn('target may be modified', str(failed.exception))
            self.assertNotEqual(fixtures.digest(self.target), before)
            for flag, original, marker in zip(flags, original_flags, markers):
                self.assertEqual(flag, original + [marker])
        finally:
            for flag, original in zip(flags, original_flags):
                flag[:] = original
        self.assert_clean()


if __name__ == '__main__':
    unittest.main(verbosity=2)
