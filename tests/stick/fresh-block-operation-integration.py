#!/usr/bin/env python3
"""Opt-in fresh installs on freshly allocated, inode-verified disposable loops.

No device argument is accepted. Requires explicit root Linux loop opt-in, an
existing private root scratch directory, and the reviewed helper's updater hash
pin. This is synthetic loop evidence, not USB/NVMe or pristine-source acceptance.
"""
from contextlib import contextmanager
import hashlib
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
ACTIVE = (sys.platform.startswith('linux') and os.environ.get('GBOS_TEST_BLOCK_LOOP') == '1'
          and os.geteuid() == 0)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


regular = load('fresh_block_regular_fixture', ROOT / 'tests/stick/fresh-operation-integration.py')
resources = load('fresh_block_owned_resource', ROOT / 'tests/update/fat-block-loop-test.py')
fixtures, gpt, m, MIB = regular.fixtures, regular.gpt, regular.m, regular.MIB
GAP, TAIL = regular.GAP, regular.TAIL


@unittest.skipUnless(ACTIVE and all(shutil.which(tool) for tool in
                     ('losetup', 'mformat', 'mcopy', 'mmd', 'mdir', 'mdel', 'minfo', 'lz4')),
                     'requires explicit root Linux disposable-loop/mtools/lz4 test')
class FreshBlockOperation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = os.environ.get('GBOS_TEST_LOOP_ROOT', '')
        if not os.path.isabs(base):
            raise RuntimeError('private root-owned scratch directory required')
        cls.basefd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        cls.addClassCleanup(os.close, cls.basefd)
        st = os.fstat(cls.basefd)
        if st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o077:
            raise RuntimeError('scratch must be root-owned and private')
        old_signal = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            raise KeyboardInterrupt('fresh block test terminated; reconciling owned loop')

        signal.signal(signal.SIGTERM, terminate)
        cls.addClassCleanup(signal.signal, signal.SIGTERM, old_signal)
        regular.FreshOperation.setUpClass.__func__(cls)
        cls.catalog_before = (cls.root / 'catalog.json').read_bytes()
        # A generic maintenance menu fixture represents the separate source-media
        # default. It is not evidence of a real USB or authenticated boot chain.
        loader = cls.root / 'loader'
        loader.mkdir()
        cls.media_default = loader / 'loader.conf'
        cls.media_default.write_bytes(b'default gbos-universal.conf\ntimeout 0\neditor no\n'
                                      b'auto-entries no\nauto-firmware no\n')
        cls.media_default_before = cls.media_default.read_bytes()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='gbos-fresh-block-case-')
        self.addCleanup(self.work.cleanup)
        self.case = Path(self.work.name).resolve()
        self.scratch = self.case / 'scratch'
        self.scratch.mkdir()
        self.size = (self.root / 'kano.img').stat().st_size + 24 * MIB
        env = patch.dict(os.environ, TMPDIR=str(self.scratch))
        env.start()
        self.addCleanup(env.stop)

    @contextmanager
    def loop_target(self):
        with resources.managed_backing(f'/proc/{os.getpid()}/fd/{self.basefd}', size=self.size) as (_, backing):
            # The target has no GPT/filesystem, only deliberately retained old
            # gap/tail sentinels. Populate before attachment to avoid cache aliasing.
            with backing.open('r+b') as out:
                out.seek(MIB - 128)
                out.write(GAP)
                out.seek((self.size // 512 - 33) * 512 - len(TAIL))
                out.write(TAIL)
                out.flush()
                os.fsync(out.fileno())
            loop = resources.command('losetup', '--find', '--show', str(backing)).strip()
            resources.owned_loop(loop, backing, size=self.size)
            print(json.dumps({'event': 'FRESH_BLOCK_LOOP_ALLOCATED', 'loop': loop,
                              'backing_inode': backing.stat().st_ino, 'case': self._testMethodName}), flush=True)
            yield loop, backing, m.observe_block_target(loop)
        self.assertFalse(backing.parent.exists())

    def apply(self, loop, facts, profile='kano', expected=None):
        return m.install_observed_block(str(self.root), 'catalog.json', expected or self.expected, loop,
                                         expected_target=facts, dmi_root=self.root / (profile + '-dmi'))

    def assert_released(self, loop, facts):
        reopened = m.update.Disk(loop, write=True, rdev=facts['rdev'],
                                 syspath=facts['syspath'], nbytes=facts['nbytes'])
        os.close(reopened.fd)
        self.assertEqual(list(self.scratch.iterdir()), [])
        for flag in (m.update.WRITING, m.update.WINDOW, m.update.STICK):
            self.assertEqual(flag, [])

    def assert_source_unchanged(self):
        self.assertEqual((self.root / 'catalog.json').read_bytes(), self.catalog_before)
        self.assertEqual(self.media_default.read_bytes(), self.media_default_before)
        for profile, digest in self.sources.items():
            self.assertEqual(fixtures.digest(self.root / (profile + '.img')), digest)

    def test_actual_fresh_both_profiles_new_guids_zero_prefix_retained_gap_tail_and_source(self):
        for profile in ('kano', 'amd-hx370'):
            with self.subTest(profile=profile), self.loop_target() as (loop, backing, facts):
                source = self.root / (profile + '.img')
                old_header, old = gpt.read_gpt(source)
                result = self.apply(loop, facts, profile)
                self.assertEqual(result['result'], 'INSTALLED_OBSERVED_BLOCK')
                self.assertEqual(result['profile'], profile)
                self.assertEqual(result['hardware'], 'NOT_VERIFIED')
                self.assertTrue({'NO_DURABLE_FRESH_RECOVERY', 'NOT_SECURE_ERASE',
                                 'CALLER_PRISTINE_TRUST_REQUIRED'} <= set(result['limitations']))
                header, parts = gpt.read_gpt(backing, backup=True)
                new_guids = {header['disk'], *(p['guid'] for p in parts.values())}
                self.assertEqual(len(new_guids), len(parts) + 1)
                self.assertFalse(new_guids & {old_header['disk'], *(p['guid'] for p in old.values())})
                self.assertEqual(set(parts), set(old))
                for number, part in parts.items():
                    for key in ('name', 'first', 'type', 'attrs'):
                        self.assertEqual(part[key], old[number][key])
                    if part['name'] == 'userdata':
                        self.assertEqual(part['last'], self.size // 512 - 34)
                        self.assertEqual(part['last'], header['last'])
                        self.assertGreater(part['last'], old[number]['last'])
                        self.assertEqual(gpt.sha(backing, part['first'] * 512, 16 * MIB),
                                         hashlib.sha256(bytes(16 * MIB)).hexdigest())
                    else:
                        self.assertEqual(part['last'], old[number]['last'])
                        if part['name'] != 'EFI-SYSTEM':
                            offset, size = part['first'] * 512, (part['last'] - part['first'] + 1) * 512
                            self.assertEqual(gpt.sha(backing, offset, size), gpt.sha(source, offset, size))
                with backing.open('rb') as out:
                    out.seek(MIB - 128)
                    self.assertEqual(out.read(len(GAP)), GAP)
                    out.seek((header['last'] + 1) * 512 - len(TAIL))
                    self.assertEqual(out.read(len(TAIL)), TAIL)
                # Read through an actual exclusive loop fd; no partition paths.
                disk = m.update.Disk(loop, write=True, rdev=facts['rdev'],
                                     syspath=facts['syspath'], nbytes=facts['nbytes'])
                try:
                    inst = m.update.Installed(disk, str(self.case))
                    self.assertEqual(inst.current, 'a')
                    self.assertEqual(set(inst.entries), {'lapis-quiet.conf', 'lapis-debug.conf'})
                    boot_guid = next(p['guid'] for p in parts.values() if p['name'] == 'boot_a')
                    self.assertIn(b'androidboot.boot_part_uuid=' + boot_guid.encode(),
                                  gpt.bootconfig(inst.fat.read('/initrd.img')).splitlines())
                    self.assertEqual(inst.fat.read('/loader/loader.conf'),
                                     b'default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n')
                    for entry in inst.entries.values():
                        self.assertNotIn('nvme_init', entry['options'])
                        self.assertIn('efi=noruntime', entry['options'])
                    with self.assertRaisesRegex(m.update.Refuse, 'exclusively'):
                        other = m.update.Disk(loop, write=True, rdev=facts['rdev'])
                        os.close(other.fd)
                finally:
                    os.close(disk.fd)
                self.assert_released(loop, facts)
                self.assert_source_unchanged()
                print(json.dumps({'event': 'ACTUAL_FRESH_BLOCK_OK', 'profile': profile,
                                  'scope': 'DISPOSABLE_LOOP_ONLY'}), flush=True)

    def test_wrong_facts_catalog_and_testenv_are_write_free(self):
        with self.loop_target() as (loop, backing, facts):
            before, actual_disk = fixtures.digest(backing), m.update.Disk
            with patch.object(m.update, 'Disk', wraps=actual_disk) as opened:
                for changed in ({}, dict(facts, device_identity='0' * 64),
                                dict(facts, nbytes=facts['nbytes'] + 512),
                                dict(facts, logical_sector_size=4096)):
                    with self.subTest(facts=changed), self.assertRaises(ValueError):
                        self.apply(loop, changed)
                with self.assertRaises(ValueError):
                    self.apply(loop, facts, expected='0' * 64)
                with patch.dict(os.environ, GBOS_UPDATE_TEST_FAIL_AT=''):
                    with self.assertRaisesRegex(ValueError, 'test environment'):
                        self.apply(loop, facts)
                self.assertFalse(any(call.kwargs.get('write') for call in opened.call_args_list))
            self.assertEqual(fixtures.digest(backing), before)
            self.assert_released(loop, facts)
            self.assert_source_unchanged()

    def test_final_prewrite_topology_drift_refuses_unchanged(self):
        with self.loop_target() as (loop, backing, facts):
            before = fixtures.digest(backing)
            original_observation, original_boot = m._target_observation, m.fresh.prepare_boot
            changed = []

            def observation(*args, **kwargs):
                result = original_observation(*args, **kwargs)
                return result + ('injected-final-topology-drift',) if changed else result

            def boot(*args, **kwargs):
                result = original_boot(*args, **kwargs)
                changed.append(True)
                return result

            with patch.object(m, '_target_observation', side_effect=observation), \
                    patch.object(m.fresh, 'prepare_boot', side_effect=boot), \
                    patch.object(m.update.Disk, 'pwrite', side_effect=AssertionError('unexpected target write')) as writes:
                with self.assertRaisesRegex(ValueError, 'topology'):
                    self.apply(loop, facts)
                writes.assert_not_called()
            self.assertTrue(changed)
            self.assertEqual(fixtures.digest(backing), before)
            self.assert_released(loop, facts)
            self.assert_source_unchanged()

    def test_final_prewrite_held_catalog_drift_refuses_target_unchanged(self):
        with self.loop_target() as (loop, backing, facts):
            before = fixtures.digest(backing)
            fd = os.open(self.root / 'catalog.json', os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            original_boot, changed = m.fresh.prepare_boot, []

            def boot(*args, **kwargs):
                result = original_boot(*args, **kwargs)
                self.assertEqual(os.pwrite(fd, b'[', 0), 1)
                os.fsync(fd)
                self.assertEqual(os.pread(fd, 1, 0), b'[')
                changed.append(True)
                return result

            try:
                with patch.object(m.fresh, 'prepare_boot', side_effect=boot), \
                        patch.object(m.update.Disk, 'pwrite', side_effect=AssertionError('unexpected target write')) as writes:
                    with self.assertRaises(ValueError):
                        self.apply(loop, facts)
                    writes.assert_not_called()
                self.assertTrue(changed)
                self.assertEqual(fixtures.digest(backing), before)
                self.assert_released(loop, facts)
            finally:
                try:
                    if os.pwrite(fd, self.catalog_before, 0) != len(self.catalog_before):
                        raise RuntimeError('fixture catalog restoration was short')
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self.assert_source_unchanged()

    def test_payload_write_fault_reports_partial_failure_and_releases_claim(self):
        with self.loop_target() as (loop, backing, facts):
            before, original_write, changed = fixtures.digest(backing), m.update.Disk.pwrite, []

            def fail_after_payload_write(disk, offset, data):
                self.assertEqual(m._devno(os.fstat(disk.fd).st_rdev), facts['rdev'])
                result = original_write(disk, offset, data)
                if offset >= MIB and any(data) and not changed:
                    disk.sync_drop()
                    changed.append(True)
                    raise RuntimeError('injected after an actual payload write')
                return result

            with patch.object(m.update.Disk, 'pwrite', fail_after_payload_write):
                with self.assertRaises(m.update.Failed) as failed:
                    self.apply(loop, facts)
            self.assertTrue(changed)
            self.assertIsInstance(failed.exception.__cause__, RuntimeError)
            self.assertIn('target may be modified', str(failed.exception))
            self.assertNotEqual(fixtures.digest(backing), before)
            disk = m.update.Disk(loop)
            try:
                self.assertEqual(disk.pread(512, 512), bytes(512))
                self.assertEqual(disk.pread(self.size - 512, 512), bytes(512))
                self.assertEqual(disk.pread(MIB - 128, len(GAP)), GAP)
            finally:
                os.close(disk.fd)
            self.assert_released(loop, facts)
            self.assert_source_unchanged()
            print(json.dumps({'event': 'FRESH_PARTIAL_FAILURE_CLEANUP_OK',
                              'recovery': 'NO_DURABLE_FRESH_RECOVERY',
                              'scope': 'DISPOSABLE_LOOP_ONLY'}), flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
