#!/usr/bin/env python3
"""Block update engine contracts without opening any real device."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('shared_block_engine_test', ROOT / 'install/gbos-update.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class BlockAssociation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'not-a-device'
        path.write_bytes(bytes(4096))
        self.fd = os.open(path, os.O_RDWR)
        self.addCleanup(os.close, self.fd)
        self.disk = object.__new__(u.Disk)
        self.disk.fd, self.disk.size, self.disk.blk, self.disk.write = self.fd, 4096, True, True
        self.rel = object.__new__(u.SharedRelease)
        self.binding = object.__new__(u.SharedBinding)
        self.binding._closed, self.binding.release = False, self.rel
        self.snapshots = []

    def operation(self, callback=lambda: None):
        snapshots = self.snapshots
        class Snapshot:
            def __init__(self, disk, tmp, sealed_esp):
                self.disk, self.sealed, self.closed = disk, sealed_esp, False
                self.gpt = {u.ESP: {'start': 512}}
                snapshots.append(self)
            def verify_unchanged(self, disk):
                if disk is not self.disk:
                    raise AssertionError('different disk')
            def close(self):
                self.closed = True
        inst = object()
        with patch.object(u.stat, 'S_ISBLK', return_value=True), \
             patch.object(u, 'PreviewMetadataSnapshot', Snapshot), \
             patch.object(u, 'Installed', return_value=inst):
            op = u.SharedBlockOperation(self.disk, self.rel, self.binding, self.tmp.name, 'a' * 64, callback)
        return op, inst

    def test_association_rejects_wrong_objects_and_closed_binding(self):
        op, _ = self.operation()
        with patch.object(u.stat, 'S_ISBLK', return_value=True):
            op.verify(self.disk, self.rel, self.binding)
            for disk, release, binding in ((object(), self.rel, self.binding),
                                           (self.disk, object(), self.binding),
                                           (self.disk, self.rel, object())):
                with self.assertRaises(u.Refuse):
                    op.verify(disk, release, binding)
            self.binding._closed = True
            with self.assertRaises(u.Refuse):
                op.verify(self.disk, self.rel, self.binding)
        op.close()
        op.close()
        self.assertTrue(self.snapshots[0].closed)
        with self.assertRaises(u.Refuse):
            op.verify(self.disk, self.rel, self.binding)

    def test_prewrite_compares_snapshot_before_last_target_observation(self):
        called = []
        op, _ = self.operation(lambda: called.append('target'))
        with patch.object(u.stat, 'S_ISBLK', return_value=True), \
             patch.object(op.snapshot, 'verify_unchanged', side_effect=lambda disk: called.append('snapshot')):
            op.before_write()
        self.assertEqual(called, ['target', 'snapshot', 'target'])
        with patch.object(u.stat, 'S_ISBLK', return_value=True), \
             patch.object(op.snapshot, 'verify_unchanged', side_effect=u.Refuse('changed')):
            with self.assertRaises(u.Refuse):
                op.before_write()
        self.assertEqual(called, ['target', 'snapshot', 'target', 'target'])
        op.close()

    def test_bad_identity_or_unbound_release_refuses(self):
        with patch.object(u.stat, 'S_ISBLK', return_value=True):
            for identity in ('a' * 63, 'A' * 64, '', None):
                with self.subTest(identity=identity), self.assertRaises(u.Refuse):
                    u.SharedBlockOperation(self.disk, self.rel, self.binding, self.tmp.name, identity, lambda: None)
            self.binding.release = object()
            with self.assertRaises(u.Refuse):
                u.SharedBlockOperation(self.disk, self.rel, self.binding, self.tmp.name, 'a' * 64, lambda: None)

    def test_post_update_installed_is_bounded_and_checked_before_and_after(self):
        calls = []
        op, _ = self.operation(lambda: calls.append('target'))
        snapshots = self.snapshots
        class Snapshot:
            def __init__(self, disk, tmp, sealed_esp):
                self.closed = False
                self.sealed = sealed_esp
                calls.append('snapshot')
                snapshots.append(self)
            def close(self):
                self.closed = True
        def parse(*args, **kwargs):
            self.assertTrue(kwargs['preview'])
            self.assertIs(kwargs['snapshot'], snapshots[-1])
            calls.append('installed')
            return 'installed'
        with patch.object(u.stat, 'S_ISBLK', return_value=True), \
             patch.object(u, 'PreviewMetadataSnapshot', Snapshot), patch.object(u, 'Installed', side_effect=parse):
            self.assertEqual(op.installed_after(), 'installed')
        self.assertEqual(calls, ['target', 'snapshot', 'installed', 'target'])
        self.assertTrue(snapshots[-1].sealed)
        op.close()
        self.assertTrue(all(s.closed for s in snapshots))

    def test_initial_preflight_exception_closes_snapshot(self):
        class Snapshot:
            closed = False
            def __init__(self, *args, **kwargs):
                outer.append(self)
            def close(self):
                self.closed = True
        outer = []
        with patch.object(u.stat, 'S_ISBLK', return_value=True), \
             patch.object(u, 'PreviewMetadataSnapshot', Snapshot), \
             patch.object(u, 'Installed', side_effect=RuntimeError('bad installed metadata')):
            with self.assertRaisesRegex(RuntimeError, 'bad installed metadata'):
                u.SharedBlockOperation(self.disk, self.rel, self.binding, self.tmp.name, 'a' * 64, lambda: None)
        self.assertEqual(len(outer), 1)
        self.assertTrue(outer[0].closed)

    def test_block_apply_refuses_without_matching_operation_before_writes(self):
        with patch.object(u.stat, 'S_ISBLK', return_value=True), \
             patch.object(u.SharedBinding, 'verify', side_effect=AssertionError('source should not be consumed')):
            for operation in (None, object()):
                with self.subTest(operation=operation), self.assertRaisesRegex(u.Refuse, 'block operation'):
                    u.apply(self.disk, self.rel, 'nvme', False, None, self.tmp.name,
                            shared_binding=self.binding, block_operation=operation)
        self.assertEqual(os.pread(self.fd, 4096, 0), bytes(4096))

    def test_device_identity_changes_resume_digest_without_changing_regular_digest(self):
        class Disk:
            size = 1024 * 1024
            def pread(self, off, size):
                data = bytearray(512)
                data[56:72] = uuid.UUID(int=1).bytes_le
                return bytes(data)
        class Installed:
            current, target = 'a', 'b'
            gpt = {'userdata': dict(n=1, start=4096, len=8192, type='type', guid='guid', attrs=0)}
        source = {'profile': 'kano'}
        disk, inst = Disk(), Installed()
        no_identity = u.shared_identity(disk, inst, source, 'nvme', False)
        baseline = {'v': 1, 'operation': 'keep-data-update', 'source': source, 'mode': 'nvme',
                    'without_fallback': False, 'current': 'a', 'target': 'b', 'disk_size': disk.size,
                    'disk_guid': str(uuid.UUID(int=1)),
                    'partitions': [dict(inst.gpt['userdata'], name='userdata')]}
        self.assertEqual(no_identity, hashlib.sha256(json.dumps(baseline, sort_keys=True,
                                                               separators=(',', ':')).encode()).hexdigest())
        first = u.shared_identity(disk, inst, source, 'nvme', False, 'a' * 64)
        second = u.shared_identity(disk, inst, source, 'nvme', False, 'b' * 64)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, no_identity)
        with self.assertRaises(u.Refuse):
            u.shared_identity(disk, inst, source, 'nvme', False, 'invalid')


class BoundedTargetFat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'not-a-device'
        path.write_bytes(b'')
        self.fd = os.open(path, os.O_RDWR)
        self.addCleanup(os.close, self.fd)
        self.fat = u.BoundedMutableFat('/irrelevant', self.tmp.name, self.fd)

    def test_mutation_errors_and_timeout_fail(self):
        with self.assertRaisesRegex(u.Failed, 'operation failed'):
            self.fat._mutate([sys.executable, '-c', 'raise SystemExit(1)'])
        self.fat.remaining_time = 0.05
        with self.assertRaisesRegex(u.Failed, 'timed out'):
            self.fat._mutate([sys.executable, '-c', 'import time; time.sleep(5)'])
        self.assertLessEqual(self.fat.remaining_time, 0)

    def test_input_and_call_limits_refuse(self):
        self.fat.input_remaining = 1
        with self.assertRaisesRegex(u.Failed, 'input limit'):
            self.fat._mutate([sys.executable, '-c', 'pass'], b'ab')
        self.fat.calls = 4096
        with self.assertRaisesRegex(u.Failed, 'budget'):
            self.fat._mutate([sys.executable, '-c', 'pass'])

    def test_directory_failure_is_not_mistaken_for_empty(self):
        binary = Path(self.tmp.name) / 'mdir'
        binary.write_text('#!/bin/sh\nexit 1\n')
        binary.chmod(0o755)
        with patch.dict(os.environ, {'PATH': self.tmp.name}):
            with self.assertRaisesRegex(u.Refuse, 'mtools failed'):
                self.fat.ls('/')

    def test_mutable_write_rejects_mismatched_readback(self):
        with patch.object(self.fat, '_mutate') as mutate, \
             patch.object(self.fat, 'read', return_value=b'different'):
            with self.assertRaisesRegex(u.Failed, 'reads back different'):
                self.fat.write('/file', b'expected')
        mutate.assert_called_once()


@unittest.skipUnless(hasattr(os, 'memfd_create') and sys.platform.startswith('linux'), 'requires Linux memfd seals')
class SealedTargetSnapshot(unittest.TestCase):
    def test_snapshot_seals_and_detects_both_metadata_and_esp_mutation(self):
        import fcntl
        class Disk:
            def __init__(self):
                self.data = bytearray(16384)
                self.size = len(self.data)
                self.data[4096 + 11:4096 + 13] = struct.pack('<H', 512)
                self.data[4096 + 19:4096 + 21] = struct.pack('<H', 8)
                self.data[4096 + 510:4096 + 512] = b'\x55\xaa'
            def pread(self, off, n):
                return bytes(self.data[off:off+n])
        disk = Disk()
        snapshot = object.__new__(u.PreviewMetadataSnapshot)
        snapshot.size = disk.size
        snapshot.ranges = [(0, disk.pread(0, 1024))]
        snapshot._esp_fd, snapshot._esp_range = None, None
        snapshot._capture_sealed_esp(disk, {'start': 4096, 'len': 4096}, '/tmp')
        fd = snapshot._esp_fd
        try:
            expected = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
            self.assertEqual(fcntl.fcntl(fd, fcntl.F_GET_SEALS) & expected, expected)
            with self.assertRaises(OSError):
                os.pwrite(fd, b'x', 0)
            snapshot.verify_unchanged(disk)
            disk.data[0] = 1
            with self.assertRaisesRegex(u.Refuse, 'metadata changed'):
                snapshot.verify_unchanged(disk)
            disk.data[0] = 0
            disk.data[4096 + 700] = 1
            with self.assertRaisesRegex(u.Refuse, 'ESP changed'):
                snapshot.verify_unchanged(disk)
        finally:
            snapshot.close()
            snapshot.close()
        with self.assertRaises(OSError):
            os.fstat(fd)


if __name__ == '__main__':
    unittest.main()
