#!/usr/bin/env python3
"""Resource bounds on the opt-in LZ4 path used by preview boot planning."""
import importlib.util
import hashlib
import struct
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('update_preview_bounds', ROOT / 'install/gbos-update.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class Lz4Bounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / 'lz4'
        self.env = patch.dict(os.environ, {'PATH': self.tmp.name + os.pathsep + os.environ.get('PATH', '')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def stub(self, body):
        self.bin.write_text('#!/bin/sh\n' + body + '\n')
        self.bin.chmod(0o755)

    def test_compressor_success_and_input_limit(self):
        self.stub('cat')
        with patch.object(u, 'PREVIEW_RAMDISK_MAX', 1024):
            self.assertEqual(u.preview_ramdisk_compress(b'abc'), b'abc')
            with self.assertRaisesRegex(u.Refuse, 'input limit'):
                u.preview_ramdisk_compress(b'x' * 1025)

    def test_output_limit_refuses_and_reaps_child(self):
        self.stub('exec head -c 8192 /dev/zero')
        with self.assertRaisesRegex(u.Refuse, 'output limit'):
            u.preview_lz4(b'input', [], 1024, 'encoder')

    def test_timeout_refuses_and_reaps_child(self):
        self.stub('exec sleep 30')
        processes = []
        real = subprocess.Popen
        def launch(*args, **kwargs):
            p = real(*args, **kwargs)
            processes.append(p)
            return p
        with patch.object(u, 'PREVIEW_LZ4_TIMEOUT', 0.1), patch.object(u.subprocess, 'Popen', side_effect=launch):
            with self.assertRaisesRegex(u.Refuse, 'encoder timed out'):
                u.preview_ramdisk_compress(b'abc')
        self.assertIsNotNone(processes[0].poll())

    def test_boot_files_preview_uses_only_bounded_codec(self):
        class Inst:
            target = 'b'
            current = 'a'
            gpt = {'boot_a': {'guid': 'guid'}}
            @property
            def entries(self):
                raise LookupError('after codec')
        class Release:
            bc = b'bootconfig'
            ramdisk = b'input'
            kernel = b'kernel'
        with patch.object(u, 'bc_set', return_value=b'bootconfig'), \
             patch.object(u, 'preview_ramdisk_plain', return_value=b'plain') as decode, \
             patch.object(u, 'preview_ramdisk_compress', return_value=b'compressed') as encode, \
             patch.object(u, 'ramdisk_plain', side_effect=AssertionError('unbounded decoder')), \
             patch.object(u, 'run', side_effect=AssertionError('unbounded compressor')):
            with self.assertRaisesRegex(LookupError, 'after codec'):
                u.boot_files(Inst(), Release(), 'nvme', self.tmp.name, preview=True)
        decode.assert_called_once_with(b'input')
        encode.assert_called_once_with(b'plain')

    def test_preview_lz4_uses_explicit_scratch_directory(self):
        self.stub('cat')
        original = u.tempfile.TemporaryFile
        with patch.object(u.tempfile, 'TemporaryFile', wraps=original) as temp:
            self.assertEqual(u.preview_lz4(b'abc', [], 10, 'test', self.tmp.name), b'abc')
        temp.assert_called_once_with(dir=self.tmp.name)

    def test_failed_encoder_refuses(self):
        self.stub('exit 3')
        with self.assertRaisesRegex(u.Refuse, 'encoder failed'):
            u.preview_ramdisk_compress(b'abc')


class PreviewFatTests(unittest.TestCase):
    def test_missing_directory_is_classified_from_parent_inventory(self):
        fat = u.PreviewFat('/private/snapshot', '/private')
        with patch.object(fat, '_run', return_value=b'::/loader\n') as run:
            self.assertEqual(fat.ls('/missing'), [])
        self.assertEqual(run.call_count, 1)

    def test_existing_directory_error_is_refused(self):
        fat = u.PreviewFat('/private/snapshot', '/private')
        with patch.object(fat, '_run', side_effect=[b'::/loader\n', u.Refuse('mtools failed')]):
            with self.assertRaisesRegex(u.Refuse, 'mtools failed'):
                fat.ls('/loader')

    def test_output_and_deadline_limits(self):
        fat = u.PreviewFat('/private/snapshot', '/private')
        with self.assertRaisesRegex(u.Refuse, 'output limit'):
            fat._run(['head', '-c', '8192', '/dev/zero'], 1024)
        fat.deadline = u.time.monotonic() + 0.1
        with self.assertRaisesRegex(u.Refuse, 'timed out'):
            fat._run(['sleep', '30'], 1024)

    def test_rejects_mtools_patterns(self):
        fat = u.PreviewFat('/private/snapshot', '/private')
        for path in ('/foo/*', '/foo/../bar', 'relative', '/foo:bar'):
            with self.subTest(path=path), self.assertRaises(u.Refuse):
                fat.read(path)


class InstalledPreviewFailures(unittest.TestCase):
    def test_nondefault_entry_io_and_initrd_failures_propagate(self):
        for failing_path in ('/loader/entries/lapis-debug.conf', '/debug-initrd.img'):
            class Fat:
                def ls(self, path):
                    return ['lapis-debug.conf']
                def read(self, path):
                    if path == failing_path:
                        raise u.PreviewFatError('FAT observation failed')
                    return b'entry'
            snapshot = type('Snapshot', (), {'gpt': {'super': {}, u.ESP: {}},
                                                'lp': object(), 'fat': Fat()})()
            with self.subTest(failing_path=failing_path), \
                 patch.object(u, 'entry_parse', return_value={'initrd': ['/debug-initrd.img']}):
                with self.assertRaisesRegex(u.PreviewFatError, 'FAT observation failed'):
                    u.Installed(object(), '/tmp', preview=True, snapshot=snapshot)

    def test_malformed_entry_still_treated_as_damaged(self):
        class Fat:
            def ls(self, path):
                return ['lapis-debug.conf']
            def read(self, path):
                if path.endswith('lapis-debug.conf'):
                    return b'malformed'
                raise RuntimeError('no loader')
            def exists(self, path):
                return False
        snapshot = type('Snapshot', (), {'gpt': {'super': {}, u.ESP: {}},
                                            'lp': object(), 'fat': Fat()})()
        with patch.object(u, 'entry_parse', side_effect=u.Refuse('damaged entry')):
            with self.assertRaisesRegex(u.Refuse, 'cannot tell which slot'):
                u.Installed(object(), '/tmp', preview=True, snapshot=snapshot)


class MetadataSnapshot(unittest.TestCase):
    def setUp(self):
        self.data = bytearray(256 * 1024)
        struct.pack_into('<QII', self.data, 512 + 72, 2, 1, 128)
        geo = bytearray(u.GEO_SIZE)
        struct.pack_into('<II32sIII', geo, 0, u.GEO_MAGIC, 52, bytes(32), 4096, 1, 4096)
        geo[8:40] = hashlib.sha256(geo[:52]).digest()
        self.super_start = 32768
        base = self.super_start + u.RESERVED
        self.data[base:base + u.GEO_SIZE] = geo
        self.data[base + u.GEO_SIZE:base + 2 * u.GEO_SIZE] = geo
        self.meta = base + 2 * u.GEO_SIZE
        self.data[self.meta:self.meta + 4] = b'orig'
        data = self.data
        class Disk:
            path = '<test>'
            size = len(data)
            def pread(self, off, n):
                return bytes(data[off:off + n])
        self.disk = Disk()
        self.layout = {'super': {'start': self.super_start, 'len': 128 * 1024}}

    def test_metadata_is_captured_and_not_reread(self):
        def read_lp(rd, size):
            return rd(u.RESERVED + 2 * u.GEO_SIZE, 4)
        with patch.object(u, 'gpt_read', return_value=self.layout), \
             patch.object(u, 'validate_preview_layout'), \
             patch.object(u, 'preview_resource_limits'), \
             patch.object(u.LP, 'read', side_effect=read_lp):
            snapshot = u.PreviewMetadataSnapshot(self.disk)
        self.data[self.meta:self.meta + 4] = b'edit'
        self.assertEqual(snapshot.lp, b'orig')
        self.assertEqual(snapshot.pread(self.meta, 4), b'orig')
        with self.assertRaisesRegex(u.Refuse, 'outside captured'):
            snapshot.pread(self.meta - 1, 2)

    def test_unbounded_gpt_table_rejected_before_capture(self):
        struct.pack_into('<QII', self.data, 512 + 72, 2, (u.MIB // 128) + 1, 128)
        with self.assertRaisesRegex(u.Refuse, 'GPT table exceeds'):
            u.PreviewMetadataSnapshot(self.disk)

    def test_invalid_geometry_rejected(self):
        self.data[self.super_start + u.RESERVED] = 0
        with patch.object(u, 'gpt_read', return_value=self.layout), \
             patch.object(u, 'validate_preview_layout'):
            with self.assertRaises(u.Refuse):
                u.PreviewMetadataSnapshot(self.disk)


if __name__ == '__main__':
    unittest.main()
