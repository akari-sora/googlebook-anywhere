#!/usr/bin/env python3
"""Actual catalog/DMI/update consumer on private miniature images; no devices."""
import ast
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('shared_operation_test', ROOT / 'install/stick/shared-source.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


@unittest.skipUnless(hasattr(os, 'O_PATH') and hasattr(os, 'memfd_create') and all(shutil.which(x) for x in
                     ('mformat', 'mmd', 'mcopy', 'mdir', 'mdel', 'minfo', 'lz4')),
                     'requires Linux memfd, O_PATH, mtools and lz4')
class Operation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='gbos-operation-fixtures-')
        cls.root = Path(cls.tmp.name).resolve()
        tree = ast.parse((ROOT / 'tests/update/selftest.py').read_text())
        names = {'rnd', 'sh', 'gpt_bytes', 'lp_super', 'newc', 'bootconfig', 'avb', 'make_release'}
        constants = {'LAYOUT', 'ESP_T', 'LIN_T', 'VBM_T', 'DYN', 'LIFT', 'OPTS'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names or
                 isinstance(n, ast.Assign) and len(n.targets) == 1 and
                 isinstance(n.targets[0], ast.Name) and n.targets[0].id in constants]
        ns = {'Path': Path, 'W': cls.root, 'MIB': 1 << 20, 'os': os, 'random': random,
              'struct': struct, 'uuid': uuid, 'zlib': zlib, 'hashlib': hashlib,
              'subprocess': subprocess, 'ENV': dict(os.environ, MTOOLS_SKIP_CHECK='1')}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<fixture-definitions>', 'exec'), ns)
        items = []
        for profile, model, identity in (
            ('kano', 'KANO', {'sys_vendor': 'Google', 'product_name': 'Kano'}),
            ('amd-hx370', 'AMD', {'sys_vendor': 'ONE-NETBOOK', 'product_name': 'ONEXPLAYER X1Pro',
             'board_vendor': 'ONE-NETBOOK', 'board_name': 'ONEXPLAYER X1Pro'})):
            source, template = (cls.root / (profile + suffix) for suffix in ('.img', '-template.img'))
            ns['make_release'](source, model=model, plain=False, vendor='v2')
            ns['make_release'](template, model=model, plain=False, vendor='v1')
            items.append({'profile': profile, 'build_id': '16452207',
                          'image': {'path': source.name, 'size': source.stat().st_size, 'sha256': digest(source)},
                          'boot_entries': ['lapis-debug.conf', 'lapis-quiet.conf'], 'hardware_validation': 'unvalidated'})
            dmi = cls.root / (profile + '-dmi')
            dmi.mkdir()
            for key, value in identity.items():
                (dmi / key).write_text(value + '\n')
        raw = json.dumps({'schema': 1, 'payloads': items}).encode()
        (cls.root / 'catalog.json').write_bytes(raw)
        cls.expected = hashlib.sha256(raw).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='gbos-operation-case-')
        self.addCleanup(self.work.cleanup)
        self.case = Path(self.work.name).resolve()
        self.target = self.case / 'target.img'
        shutil.copyfile(self.root / 'kano-template.img', self.target)
        self.scratch = self.case / 'scratch'
        self.scratch.mkdir()
        self.env = patch.dict(os.environ, TMPDIR=str(self.scratch))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(setattr, m.update, 'FAIL_AT', '')

    def apply(self, profile='kano', target=None, expected=None):
        return m.apply_observed_image(str(self.root), 'catalog.json', expected or self.expected,
                                      str(target or self.target), self.root / (profile + '-dmi'))

    def assert_clean(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        for flag in (m.update.WRITING, m.update.WINDOW, m.update.STICK):
            self.assertEqual(flag, [])

    def installed(self, target=None):
        disk = m.update.Disk(str(target or self.target))
        try:
            inst = m.update.Installed(disk, str(self.case))
            ranges = [(inst.gpt[p + '_a']['start'], inst.gpt[p + '_a']['len']) for p in m.update.SLOT_PHYS]
            data = inst.gpt['userdata']
            ranges.append((data['start'], data['len']))
            preserved = [disk.sha(off, size) for off, size in ranges]
            return inst.current, inst.gpt, preserved, inst.shared_digest, inst.slot_of.copy()
        finally:
            os.close(disk.fd)

    def test_real_update_selects_each_profile_and_preserves_data_old_slot_gpt(self):
        for profile in ('kano', 'amd-hx370'):
            with self.subTest(profile=profile):
                shutil.copyfile(self.root / (profile + '-template.img'), self.target)
                before = self.installed()
                result = self.apply(profile)
                after = self.installed()
                self.assertEqual(result['result'], 'APPLIED_SYNTHETIC_IMAGE')
                self.assertEqual(result['profile'], profile)
                self.assertFalse(result['physical_write_allowed'])
                self.assertEqual(after[0], 'b')
                self.assertEqual(after[1:3], before[1:3])
                self.assertEqual(after[4]['prev-lapis-quiet.conf'], 'a')
                self.assertIsNone(after[3])
                self.assert_clean()

    def rollback(self, profile='kano'):
        return m.rollback_observed_image(str(self.root), 'catalog.json', self.expected,
                                         str(self.target), self.root / (profile + '-dmi'))

    def test_fresh_update_and_rollback_twice_for_both_profiles(self):
        for profile in ('kano', 'amd-hx370'):
            with self.subTest(profile=profile):
                source = self.root / (profile + '.img')
                source_before = digest(source)
                with self.target.open('wb') as out:
                    out.truncate(source.stat().st_size + 24 * m.update.MIB)
                m.install_observed_image(str(self.root), 'catalog.json', self.expected,
                                         str(self.target), self.root / (profile + '-dmi'))
                before = self.installed()
                self.assertEqual(before[0], 'a')
                self.apply(profile)
                updated = self.installed()
                self.assertEqual(updated[0], 'b')
                self.assertEqual(updated[1:3], before[1:3])
                self.assertEqual(self.rollback(profile)['result'], 'ROLLED_BACK_SYNTHETIC_IMAGE')
                reverted = self.installed()
                self.assertEqual(reverted[0], 'a')
                self.assertEqual(reverted[4]['prev-lapis-quiet.conf'], 'b')
                self.assertEqual(reverted[1:3], before[1:3])
                self.rollback(profile)
                final = self.installed()
                self.assertEqual(final[0], 'b')
                self.assertEqual(final[4]['prev-lapis-quiet.conf'], 'a')
                self.assertEqual(final[1:3], before[1:3])
                self.assertEqual(digest(source), source_before)
                self.assert_clean()

    def test_rollback_missing_previous_interrupted_and_cross_build_refused(self):
        before = digest(self.target)
        with self.assertRaises(m.update.Refuse):
            self.rollback()
        self.assertEqual(digest(self.target), before)
        m.update.FAIL_AT = 'sharedstate'
        with self.assertRaises(m.update.Failed):
            self.apply()
        m.update.FAIL_AT = ''
        before = digest(self.target)
        with self.assertRaises(m.update.Refuse):
            self.rollback()
        self.assertEqual(digest(self.target), before)
        self.apply()
        before = digest(self.target)
        with patch.object(m.update, 'slot_build', return_value=-1):
            with self.assertRaisesRegex(m.update.Refuse, 'previous version has Google build'):
                self.rollback()
        self.assertEqual(digest(self.target), before)
        self.assert_clean()

    def test_rollback_target_replaced_immediately_before_write(self):
        self.apply()
        old, replacement = self.case / 'original.img', self.case / 'replacement.img'
        shutil.copyfile(self.target, replacement)
        before = digest(self.target)
        slot_build = m.update.slot_build
        def replace(*args):
            result = slot_build(*args)
            self.target.rename(old)
            replacement.rename(self.target)
            return result
        with patch.object(m.update, 'slot_build', side_effect=replace):
            with self.assertRaisesRegex(ValueError, 'target changed'):
                self.rollback()
        self.assertEqual(digest(self.target), before)
        self.assertEqual(digest(old), before)
        self.assert_clean()

    def test_rollback_source_replaced_immediately_before_write(self):
        self.apply()
        source = self.root / 'kano.img'
        old = self.root / 'kano-before-swap.img'
        replacement = self.root / 'kano-replacement.img'
        shutil.copyfile(source, replacement)
        before = digest(self.target)
        slot_build = m.update.slot_build
        def replace(*args):
            result = slot_build(*args)
            source.rename(old)
            replacement.rename(source)
            return result
        try:
            with patch.object(m.update, 'slot_build', side_effect=replace):
                with self.assertRaises((ValueError, m.update.Refuse)):
                    self.rollback()
            self.assertEqual(digest(self.target), before)
        finally:
            if old.exists():
                source.unlink()
                old.rename(source)
            replacement.unlink(missing_ok=True)
        self.assert_clean()

    def test_rollback_target_replaced_after_write_is_classified(self):
        self.apply()
        old, replacement = self.case / 'opened.img', self.case / 'replacement.img'
        shutil.copyfile(self.target, replacement)
        before = digest(replacement)
        write = m.update.Fat.write
        replaced = []
        def replace_after_write(fat, *args):
            result = write(fat, *args)
            if not replaced:
                self.target.rename(old)
                replacement.rename(self.target)
                replaced.append(True)
            return result
        with patch.object(m.update.Fat, 'write', replace_after_write):
            with self.assertRaisesRegex(m.update.Failed, 'target path changed'):
                self.rollback()
        self.assertEqual(digest(self.target), before)
        self.assertNotEqual(digest(old), before)
        self.assertEqual(self.installed(old)[0], 'a')
        self.assert_clean()

    def test_rollback_write_failure_is_classified_and_cleans_up(self):
        self.apply()
        failure = RuntimeError('write failed')
        with patch.object(m.update.Fat, 'write', side_effect=failure):
            with self.assertRaisesRegex(m.update.Failed, 'after entering write phase') as caught:
                self.rollback()
        self.assertIs(caught.exception.__cause__, failure)
        self.assert_clean()

    def test_bad_catalog_or_unknown_machine_refuses_before_opening_writer(self):
        before = digest(self.target)
        with patch.object(m.update, 'Disk', wraps=m.update.Disk) as opened:
            with self.assertRaises(ValueError):
                self.apply(expected='0' * 64)
            unknown = self.case / 'unknown-dmi'
            unknown.mkdir()
            with self.assertRaises(ValueError):
                m.apply_observed_image(str(self.root), 'catalog.json', self.expected, str(self.target), unknown)
            opened.assert_not_called()
        self.assertEqual(digest(self.target), before)
        self.assert_clean()

    def test_oversized_nonregular_and_source_alias_refuse_before_writer(self):
        large = self.case / 'large.img'
        with large.open('wb') as f:
            f.truncate(128 * m.update.MIB + 1)
        fifo = self.case / 'fifo'
        os.mkfifo(fifo)
        with patch.object(m.update, 'Disk', wraps=m.update.Disk) as opened:
            for target in (large, fifo, self.root / 'kano.img'):
                with self.subTest(target=target.name), self.assertRaises((ValueError, m.update.Refuse)):
                    self.apply(target=target)
            opened.assert_not_called()
        self.assert_clean()

    def test_target_replaced_after_observation_refuses_without_update(self):
        old = self.case / 'observed.img'
        replacement = self.case / 'replacement.img'
        shutil.copyfile(self.root / 'kano-template.img', replacement)
        before = digest(self.target)
        actual_disk = m.update.Disk
        def swap(*args, **kwargs):
            self.target.rename(old)
            replacement.rename(self.target)
            return actual_disk(*args, **kwargs)
        with patch.object(m.update, 'Disk', side_effect=swap), patch.object(m.update, 'apply') as apply:
            with self.assertRaisesRegex(ValueError, 'target changed'):
                self.apply()
            apply.assert_not_called()
        self.assertEqual(digest(self.target), before)
        self.assertEqual(digest(old), before)
        self.assert_clean()

    def test_target_replaced_during_boot_preflight_refuses_before_first_write(self):
        old, replacement = self.case / 'observed.img', self.case / 'replacement.img'
        shutil.copyfile(self.target, replacement)
        before = digest(self.target)
        boot_files = m.update.boot_files
        def replace_after_boot(*args, **kwargs):
            result = boot_files(*args, **kwargs)
            self.target.rename(old)
            replacement.rename(self.target)
            return result
        with patch.object(m.update, 'boot_files', side_effect=replace_after_boot):
            with self.assertRaisesRegex(ValueError, 'target changed before update'):
                self.apply()
        self.assertEqual(digest(self.target), before)
        self.assertEqual(digest(old), before)
        self.assert_clean()

    def test_runtime_error_after_state_reports_failed_with_cause_and_cleans_up(self):
        before = self.installed()
        failure = RuntimeError('mcopy failed after durable state')
        def fail_after_state(name):
            if name.startswith('sharedstate:'):
                raise failure
        with patch.object(m.update, 'step', side_effect=fail_after_state):
            with self.assertRaisesRegex(m.update.Failed, 'after entering write phase') as caught:
                self.apply()
        self.assertIs(caught.exception.__cause__, failure)
        after = self.installed()
        self.assertEqual(after[:3], before[:3])
        self.assertIsNotNone(after[3])
        self.assert_clean()

    def test_cancel_after_state_closes_all_descriptors_and_safe_retry_succeeds(self):
        saved = []
        actual_release, actual_disk = m.shared_release, m.update.Disk
        @contextmanager
        def record_release(*args, **kwargs):
            with actual_release(*args, **kwargs) as pair:
                saved.append(pair)
                yield pair
        disks = []
        def record_disk(*args, **kwargs):
            disk = actual_disk(*args, **kwargs)
            disks.append(disk.fd)
            return disk
        def cancel(name):
            if name.startswith('sharedstate:'):
                raise KeyboardInterrupt('cancel after durable state')
        before = self.installed()
        with patch.object(m, 'shared_release', record_release), patch.object(m.update, 'Disk', record_disk), \
                patch.object(m.update, 'step', side_effect=cancel):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        for fd in disks:
            with self.assertRaises(OSError):
                os.fstat(fd)
        for release, binding in saved:
            with self.assertRaises((OSError, m.update.Refuse)):
                binding.verify(release)
        interrupted = self.installed()
        self.assertEqual(interrupted[:3], before[:3])
        self.assertIsNotNone(interrupted[3])
        self.assert_clean()
        self.assertEqual(self.apply()['result'], 'APPLIED_SYNTHETIC_IMAGE')
        self.assertEqual(self.installed()[0], 'b')
        self.assert_clean()

    def test_late_menu_interruption_remains_refused_and_byte_identical_on_retry(self):
        m.update.FAIL_AT = 'sharedmenu-lapis-quiet.conf'
        with self.assertRaises(m.update.Failed):
            self.apply()
        m.update.FAIL_AT = ''
        before = digest(self.target)
        with self.assertRaisesRegex(m.update.Refuse, 'late menu'):
            self.apply()
        self.assertEqual(digest(self.target), before)
        self.assert_clean()

    def test_target_path_replaced_after_state_never_redirects_writes(self):
        old, replacement = self.case / 'opened.img', self.case / 'replacement.img'
        shutil.copyfile(self.target, replacement)
        before = digest(replacement)
        original_step = m.update.step
        def replace_after_state(name):
            if name.startswith('sharedstate:'):
                self.target.rename(old)
                replacement.rename(self.target)
            original_step(name)
        with patch.object(m.update, 'step', side_effect=replace_after_state):
            with self.assertRaisesRegex(m.update.Failed, 'target path changed'):
                self.apply()
        self.assertEqual(digest(self.target), before)
        self.assertNotEqual(digest(old), before)
        self.assertEqual(self.installed(old)[0], 'b')
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
