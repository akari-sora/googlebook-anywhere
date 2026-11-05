#!/usr/bin/env python3
"""Actual shared block update, only on loops backed by this test's own files."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
ACTIVE = os.environ.get('GBOS_TEST_BLOCK_LOOP') == '1' and os.geteuid() == 0


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fixtures = load('block_operation_fixture', ROOT / 'tests/stick/shared-operation-integration.py')
resources = load('block_owned_loop_resource', ROOT / 'tests/update/fat-block-loop-test.py')
m = fixtures.m


@unittest.skipUnless(ACTIVE and all(shutil.which(x) for x in
                     ('losetup', 'mformat', 'mcopy', 'mmd', 'mdir', 'mdel', 'minfo', 'lz4', 'mount', 'umount')),
                     'requires explicit root Linux loop/mtools/lz4 test')
class BlockOperation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = os.environ.get('GBOS_TEST_LOOP_ROOT', '')
        if not os.path.isabs(base):
            raise RuntimeError('private root scratch required')
        cls.basefd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        cls.addClassCleanup(os.close, cls.basefd)
        st = os.fstat(cls.basefd)
        if st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o077:
            raise RuntimeError('scratch must be root-owned and private')
        cls.old_signal = signal.getsignal(signal.SIGTERM)
        def terminate(signum, frame):
            raise KeyboardInterrupt('block test terminated; reconciling owned loop')
        signal.signal(signal.SIGTERM, terminate)
        cls.addClassCleanup(signal.signal, signal.SIGTERM, cls.old_signal)
        fixtures.Operation.setUpClass.__func__(cls)
        cls.addClassCleanup(cls.tmp.cleanup)

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='gbos-block-case-')
        self.addCleanup(self.work.cleanup)
        self.case = Path(self.work.name).resolve()
        self.scratch = self.case / 'scratch'
        self.scratch.mkdir()
        self.env = patch.dict(os.environ, TMPDIR=str(self.scratch))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(setattr, m.update, 'FAIL_AT', '')

    @contextmanager
    def loop_target(self, profile='kano', clone=None, size=None):
        template = clone or self.root / (profile + '-template.img')
        nbytes = size or template.stat().st_size
        with resources.managed_backing(f'/proc/{os.getpid()}/fd/{self.basefd}', size=nbytes) as (tmp, backing):
            if size is None:
                shutil.copyfile(template, backing)
            loop = resources.command('losetup', '--find', '--show', str(backing)).strip()
            resources.owned_loop(loop, backing, size=nbytes)
            print(json.dumps({'event': 'BLOCK_OPERATION_LOOP_ALLOCATED', 'loop': loop,
                              'backing_inode': backing.stat().st_ino, 'case': self._testMethodName}), flush=True)
            yield loop, backing, m.observe_block_target(loop)

    def apply(self, loop, facts, profile='kano', expected=None):
        return m.apply_observed_block(str(self.root), 'catalog.json', expected or self.expected, loop,
                                      expected_target=facts, dmi_root=self.root / (profile + '-dmi'))

    def installed(self, backing):
        self.target = backing
        return fixtures.Operation.installed(self)

    def assert_clean(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        for flag in (m.update.WRITING, m.update.WINDOW, m.update.STICK):
            self.assertEqual(flag, [])

    def test_actual_update_both_profiles_preserves_data_old_slot_gpt_and_releases_claim(self):
        for profile in ('kano', 'amd-hx370'):
            with self.subTest(profile=profile), self.loop_target(profile) as (loop, backing, facts):
                before = self.installed(backing)
                result = self.apply(loop, facts, profile)
                after = self.installed(backing)
                self.assertEqual(result['result'], 'APPLIED_OBSERVED_BLOCK')
                self.assertEqual(result['profile'], profile)
                self.assertEqual(after[0], 'b')
                self.assertEqual(after[1:3], before[1:3])
                self.assertEqual(after[4]['prev-lapis-quiet.conf'], 'a')
                self.assertIsNone(after[3])
                reopened = m.update.Disk(loop, write=True, rdev=facts['rdev'], syspath=facts['syspath'], nbytes=facts['nbytes'])
                os.close(reopened.fd)
                self.assert_clean()
                print(json.dumps({'event': 'ACTUAL_BLOCK_UPDATE_OK', 'profile': profile,
                                  'scope': 'DISPOSABLE_LOOP_ONLY'}), flush=True)

    def test_wrong_target_catalog_test_environment_and_sector_refuse_before_writer(self):
        with self.loop_target() as (loop, backing, facts):
            before = fixtures.digest(backing)
            actual_disk = m.update.Disk
            cases = [dict(facts, device_identity='0' * 64), dict(facts, nbytes=facts['nbytes'] + 512),
                     dict(facts, logical_sector_size=4096), {}]
            with patch.object(m.update, 'Disk', wraps=actual_disk) as opened:
                for changed in cases:
                    with self.assertRaises((ValueError, m.update.Refuse)):
                        self.apply(loop, changed)
                with self.assertRaises(ValueError):
                    self.apply(loop, facts, expected='0' * 64)
                with patch.dict(os.environ, GBOS_UPDATE_TEST_FAIL_AT=''):
                    with self.assertRaisesRegex(ValueError, 'test environment'):
                        self.apply(loop, facts)
                self.assertFalse(any(call.kwargs.get('write') for call in opened.call_args_list))
            import fcntl
            ioctl = fcntl.ioctl
            def sector_4k(fd, request, arg=0, mutate=True):
                if request == 0x1268:
                    arg[:] = (4096).to_bytes(4, sys.byteorder)
                    return 0
                return ioctl(fd, request, arg, mutate)
            with patch.object(fcntl, 'ioctl', side_effect=sector_4k), patch.object(m.update, 'Disk', wraps=actual_disk) as opened:
                with self.assertRaisesRegex(ValueError, '512-byte'):
                    self.apply(loop, facts)
                self.assertFalse(any(call.kwargs.get('write') for call in opened.call_args_list))
            self.assertEqual(fixtures.digest(backing), before)
            self.assert_clean()

    def test_live_esp_change_during_preflight_refuses_without_updater_writes(self):
        with self.loop_target() as (loop, backing, facts):
            disk = m.update.Disk(str(backing))
            try:
                esp = m.update.gpt_read(disk)[m.update.ESP]
            finally:
                os.close(disk.fd)
            original = m.update.boot_files
            changed = []
            def mutate_after_preflight(*args, **kwargs):
                result = original(*args, **kwargs)
                # The block-device cache is separate from its backing file's
                # cache. Inject through the already-held, owned loop fd so the
                # updater observes the same device that this test changed.
                disk = args[0].disk
                self.assertEqual(m._devno(os.fstat(disk.fd).st_rdev), facts['rdev'])
                offset = esp['start'] + esp['len'] - 1
                byte = os.pread(disk.fd, 1, offset)
                injected = bytes([byte[0] ^ 1])
                self.assertEqual(os.pwrite(disk.fd, injected, offset), 1)
                disk.sync_drop()
                self.assertEqual(os.pread(disk.fd, 1, offset), injected)
                changed.append(fixtures.digest(backing))
                return result
            with patch.object(m.update, 'boot_files', side_effect=mutate_after_preflight):
                with self.assertRaisesRegex(m.update.Refuse, 'changed'):
                    self.apply(loop, facts)
            self.assertEqual(fixtures.digest(backing), changed[0])
            self.assert_clean()

    def test_topology_change_at_final_prewrite_refuses_unchanged(self):
        with self.loop_target() as (loop, backing, facts):
            before = fixtures.digest(backing)
            observed, boot_files, changed = m._target_observation, m.update.boot_files, []
            def observation(*args, **kwargs):
                result = observed(*args, **kwargs)
                return result + ('changed',) if changed else result
            def change(*args, **kwargs):
                result = boot_files(*args, **kwargs)
                changed.append(True)
                return result
            with patch.object(m, '_target_observation', side_effect=observation), \
                    patch.object(m.update, 'boot_files', side_effect=change):
                with self.assertRaisesRegex(ValueError, 'topology'):
                    self.apply(loop, facts)
            self.assertEqual(fixtures.digest(backing), before)
            self.assert_clean()

    def test_cancel_state_clone_refusal_and_same_device_resume(self):
        with self.loop_target() as (loop, backing, facts):
            before = self.installed(backing)
            def cancel(name):
                if name.startswith('sharedstate:'):
                    raise KeyboardInterrupt('cancel after durable state')
            with patch.object(m.update, 'step', side_effect=cancel):
                with self.assertRaises(KeyboardInterrupt):
                    self.apply(loop, facts)
            after = self.installed(backing)
            self.assertEqual(after[:3], before[:3])
            self.assertIsNotNone(after[3])
            self.assert_clean()
            with self.loop_target(clone=backing) as (other, clone, other_facts):
                self.assertNotEqual(other_facts['device_identity'], facts['device_identity'])
                unchanged = fixtures.digest(clone)
                with self.assertRaisesRegex(m.update.Refuse, 'identity differs'):
                    self.apply(other, other_facts)
                self.assertEqual(fixtures.digest(clone), unchanged)
            self.assertEqual(self.apply(loop, facts)['result'], 'APPLIED_OBSERVED_BLOCK')
            self.assertEqual(self.installed(backing)[0], 'b')
            self.assert_clean()

    def test_late_menu_failure_remains_fail_closed_on_retry(self):
        with self.loop_target() as (loop, backing, facts):
            m.update.FAIL_AT = 'sharedmenu-lapis-quiet.conf'
            with self.assertRaises(m.update.Failed):
                self.apply(loop, facts)
            m.update.FAIL_AT = ''
            unchanged = fixtures.digest(backing)
            with self.assertRaisesRegex(m.update.Refuse, 'late menu'):
                self.apply(loop, facts)
            self.assertEqual(fixtures.digest(backing), unchanged)
            self.assert_clean()

    def test_actual_readonly_mount_refuses_before_writer(self):
        with self.loop_target(size=8 * 1024 * 1024) as (loop, backing, facts):
            resources.command('mformat', '-i', str(backing), '-T', '16384', '-h', '2', '-s', '32', '::')
            mountpoint = self.case / 'mounted'
            mountpoint.mkdir()
            resources.command('mount', '-t', 'vfat', '-o', 'ro,nosuid,nodev,noexec', loop, str(mountpoint))
            try:
                actual_disk = m.update.Disk
                with patch.object(m.update, 'Disk', wraps=actual_disk) as opened:
                    with self.assertRaisesRegex(ValueError, 'mounted'):
                        self.apply(loop, facts)
                    self.assertFalse(any(call.kwargs.get('write') for call in opened.call_args_list))
            finally:
                resources.command('umount', str(mountpoint))
            self.assert_clean()


if __name__ == '__main__':
    unittest.main(verbosity=2)
