#!/usr/bin/env python3
"""Private regular-file regression tests for source bytes consumed by the updater."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('shared_update_test', ROOT / 'install/gbos-update.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class ChunkReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'source.img'
        self.data = bytes(range(256)) * (8192 + 3) + b'tail'
        self.path.write_bytes(self.data)
        self.fd = os.open(self.path, os.O_RDONLY)
        self.addCleanup(os.close, self.fd)
        self.item = {'profile': 'kano', 'image': {'size': len(self.data),
                     'sha256': hashlib.sha256(self.data).hexdigest()}}
        with patch.object(u, 'image_release_from_reader', return_value=object()):
            self.binding = u.SharedBinding(self.fd, self.item, 'a' * 64, [self.fd], self.tmp.name)

    def mutate(self, offset):
        fd = os.open(self.path, os.O_WRONLY)
        try:
            os.pwrite(fd, bytes([self.data[offset] ^ 0xff]), offset)
        finally:
            os.close(fd)

    def test_unaligned_cross_chunk_tail_and_empty(self):
        for off, size in [(0, 1), (1048576 - 5, 12), (1048576 + 3, 17),
                          (len(self.data) - 4, 4), (len(self.data), 0)]:
            with self.subTest(off=off, size=size):
                self.assertEqual(self.binding.pread(off, size), self.data[off:off + size])

    def test_detects_mutation_in_other_part_of_requested_chunk(self):
        self.mutate(100)
        with self.assertRaises(u.Refuse):
            self.binding.pread(0, 1)

    def test_returned_bytes_come_from_verified_buffer(self):
        original = os.pread
        changed = []
        def read_then_change(fd, n, off):
            result = original(fd, n, off)
            if not changed:
                self.mutate(0)
                changed.append(True)
            return result
        with patch.object(u.os, 'pread', side_effect=read_then_change):
            self.assertEqual(self.binding.pread(0, 1), self.data[:1])
        with self.assertRaises(u.Refuse):
            self.binding.pread(0, 1)

    def test_detects_mutation_in_tail_chunk(self):
        self.mutate(len(self.data) - 1)
        with self.assertRaises(u.Refuse):
            self.binding.pread(2 * 1048576, 1)

    def test_partial_reads_accumulate_but_short_read_is_refused(self):
        original = os.pread
        def partial(fd, n, off):
            return original(fd, max(1, n // 2), off)
        with patch.object(u.os, 'pread', side_effect=partial):
            self.assertEqual(self.binding.pread(1048576 - 2, 8), self.data[1048576 - 2:1048576 + 6])
        with patch.object(u.os, 'pread', return_value=b''):
            with self.assertRaises(u.Refuse):
                self.binding.pread(0, 1)

    def test_bad_bounds_are_refused(self):
        for off, size in [(-1, 1), (0, -1), (len(self.data), 1), (True, 1), (0, False)]:
            with self.subTest(off=off, size=size), self.assertRaises(u.Refuse):
                self.binding.pread(off, size)

    def test_oversized_direct_construction_is_refused_at_identity_check(self):
        item = {'profile': 'kano', 'image': {'size': 128 * 1024 ** 3 + 1,
                 'sha256': hashlib.sha256(self.data).hexdigest()}}
        with self.assertRaisesRegex(u.Refuse, 'invalid shared source identity'):
            u.SharedBinding(self.fd, item, 'a' * 64, [self.fd], self.tmp.name)

    def test_apply_stops_on_mutation_after_target_write(self):
        # Exercise apply's real write_stream and Disk on a private regular file.
        # Only the GPT/ESP parser and boot planning are test doubles.
        target = Path(self.tmp.name) / 'target.img'
        target.write_bytes(b'T' * (6 * 1048576))
        disk = u.Disk(str(target), write=True)
        self.addCleanup(os.close, disk.fd)
        self.addCleanup(u.WRITING.clear)
        self.addCleanup(u.WINDOW.clear)
        self.addCleanup(u.STICK.clear)
        old_slot = disk.pread(4 * 1048576, 512)
        userdata = disk.pread(5 * 1048576, 512)
        old_target = disk.pread(2 * 1048576, 512)

        class FakeFat:
            def __init__(self):
                self.files = {'/loader/entries/lapis-quiet.conf': b'old boot'}
            def write(self, path, data):
                self.files[path] = data
            def read(self, path):
                return self.files[path]
            def ls(self, path):
                return ['lapis-quiet.conf']
            def delete(self, path):
                self.files.pop(path, None)

        class FakeLP:
            def __init__(self):
                self.parts = [{'name': 'system_b', 'extents': []},
                              {'name': 'vendor_b', 'extents': []}]
            def part(self, name):
                return next((p for p in self.parts if p['name'] == name), None)

        fat = FakeFat()
        inst = type('Inst', (), {})()
        inst.current, inst.target = 'a', 'b'
        inst.shared_digest, inst.resume = None, False
        inst.fat, inst.lp, inst.super = fat, FakeLP(), {'start': 0}
        inst.slot_of = {'lapis-quiet.conf': 'a'}
        inst.state_text = lambda ident: 'updating b\nfrom a\n'
        release = type('Rel', (), {})()
        release.ident = 'private fixture'
        release.dyn = {'system': {'extents': [(0, 1)], 'attrs': 0},
                       'vendor': {'extents': [(2048, 1)], 'attrs': 0}}
        release.super_rd = self.binding.pread
        self.binding.release = release
        check = {'extents': {'system_b': [(4096, 1)], 'vendor_b': [(6144, 1)]},
                 'sizes': {'system_b': 512, 'vendor_b': 512}, 'group': 0}
        boot = {'fits': True, 'prev': {'lapis-quiet.conf': 'old'}, 'drop': []}
        original_write_stream = u.write_stream
        writes = []
        def mutate_after_first_write(*args):
            result = original_write_stream(*args)
            writes.append(True)
            if len(writes) == 1:
                self.mutate(1048576 + 5)
            return result
        with patch.object(u, 'gpt_read', return_value={}), \
             patch.object(u, 'validate_preview_layout'), \
             patch.object(u, 'preview_resource_limits'), \
             patch.object(u, 'Installed', return_value=inst), \
             patch.object(u, 'shared_identity', return_value='a' * 64), \
             patch.object(u, 'check', return_value=check), \
             patch.object(u, 'boot_files', return_value=boot), \
             patch.object(u, 'plan_text', return_value=[]), \
             patch.object(u, 'guard_ranges', return_value=[]), \
             patch.object(u, 'write_stream', side_effect=mutate_after_first_write):
            with self.assertRaisesRegex(u.Failed, 'shared source chunk') as failure:
                u.apply(disk, release, 'nvme', False, None, self.tmp.name, self.binding)
        self.assertIsInstance(failure.exception.__cause__, u.Refuse)
        self.assertNotEqual(disk.pread(2 * 1048576, 512), old_target)
        self.assertEqual(disk.pread(4 * 1048576, 512), old_slot)
        self.assertEqual(disk.pread(5 * 1048576, 512), userdata)
        self.assertEqual(fat.read('/loader/entries/lapis-quiet.conf'), b'old boot')
        self.assertIn(b'shared-version 1', fat.read(u.STATE))
        self.assertTrue(u.WRITING)

    def test_source_generator_refusal_translates_to_failed_with_cause(self):
        refusal = u.Refuse('source unavailable')
        def source():
            raise refusal
            yield b''
        disk = type('NoWrites', (), {'pwrite': lambda *_: self.fail('unexpected write')})()
        with self.assertRaisesRegex(u.Failed, 'source unavailable') as failure:
            u.write_stream(disk, [(0, 1)], source())
        self.assertIs(failure.exception.__cause__, refusal)

    def test_bad_initial_digest_never_parses_release(self):
        item = {'profile': 'kano', 'image': {'size': len(self.data), 'sha256': 'b' * 64}}
        with patch.object(u, 'image_release_from_reader') as parser:
            with self.assertRaises(u.Refuse):
                u.SharedBinding(self.fd, item, 'a' * 64, [self.fd], self.tmp.name)
            parser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
