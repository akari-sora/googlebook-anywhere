#!/usr/bin/env python3
"""Mixed initramfs and magic-based container boundary regressions."""
import gzip
import hashlib
import importlib.util
import io
import lzma
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('boundary_fixtures', Path(__file__).with_name('ifma-scan-tests.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
S = f.SCANNER

class BoundaryTests(unittest.TestCase):
    category = 'synthetic'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'tree'
        self.root.mkdir()
        self.clean = f.newc_archive('clean', b'data')

    def scan(self, **kwargs):
        return S.scan_tree(self.root, provenance={'partition':'system_a'}, **kwargs)

    def frames(self, payload):
        frames = [('xz', lzma.compress(payload)), ('gzip', gzip.compress(payload))]
        for name, args in [('lz4', ['-z', '-c']), ('lz4', ['-l', '-z', '-c']), ('zstd', ['-q', '-c'])]:
            tool = shutil.which(name)
            with self.subTest(optional_frame=name, legacy='-l' in args):
                if not tool:
                    self.skipTest(name + ' CLI unavailable for this frame case')
                frames.append((name + ('-legacy' if '-l' in args else ''), subprocess.run([tool] + args, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout))
        return frames

    def test_compressed_first_then_plain_ifma_is_refused(self):
        for kind, frame in self.frames(self.clean):
            for opcode in (f.EncodingTests.LUQ, f.EncodingTests.HUQ):
                with self.subTest(kind=kind, opcode=opcode.hex()):
                    (self.root/'ramdisk').write_bytes(frame + b'\0'*13 + f.newc_archive('evil', f.elf_with_segments(opcode)))
                    with self.assertRaisesRegex(S.ScanError, 'forbidden VEX IFMA'):
                        self.scan()

    def test_compressed_first_junk_is_refused_even_with_outer_pin(self):
        for kind, frame in self.frames(self.clean):
            with self.subTest(kind=kind):
                data = frame + b'\0'*13 + b'junk'
                (self.root/'ramdisk').write_bytes(data)
                with self.assertRaises(S.ScanError):
                    self.scan()
                pin = {'partition':'system_a','container_chain':[{'kind':'tree_file','path':'ramdisk'}], 'sha256':hashlib.sha256(data).hexdigest(), 'reason':'test outer pin cannot hide trailing bytes'}
                with self.assertRaises(S.ScanError):
                    self.scan(unsupported_allowlist=[pin])

    def test_legal_compressed_first_mixes_are_fully_opened(self):
        frames = self.frames(self.clean)
        for kind, first in frames:
            with self.subTest(kind=kind):
                data = first + b'\0'*9 + self.clean + b'\0'*5 + b''.join(frame for _, frame in frames) + self.clean + b'\0'*31
                (self.root/'ramdisk').write_bytes(data)
                report = self.scan()
                self.assertEqual(report['hits'], [])
                # One newc payload per frame plus the two plain archives.
                self.assertGreaterEqual(sum(row['kind']=='cpio-newc' for row in report['nested_opened']), len(frames)+2)

    def test_every_compressed_frame_transition_is_legal(self):
        frames = self.frames(self.clean)
        for left_kind, left in frames:
            for right_kind, right in frames:
                with self.subTest(left=left_kind, right=right_kind):
                    (self.root/'ramdisk').write_bytes(left + right)
                    report = self.scan()
                    self.assertEqual(report['hits'], [])
                    self.assertEqual(sum(row['kind']=='cpio-newc' for row in report['nested_opened']), 2)

    def test_bootconfig_after_mixed_initramfs_is_validated(self):
        config = b'androidboot.test = "boundary"\n'
        footer = struct.pack('<II', len(config), sum(config)) + b'#BOOTCONFIG\n'
        for kind, frame in [('newc', self.clean), ('mixed-newc', self.clean+gzip.compress(self.clean))] + self.frames(self.clean):
            with self.subTest(kind=kind):
                (self.root/'ramdisk').write_bytes(frame + self.clean + config + footer)
                report = self.scan()
                self.assertEqual(report['hits'], [])
                self.assertEqual(sum(row['kind']=='android-bootconfig' for row in report['nested_opened']), 1)

    def test_non_lz4_bootconfig_invalid_size_or_checksum_is_refused(self):
        config = b'androidboot.test = "boundary"\n'
        for prefix in (self.clean, gzip.compress(self.clean), lzma.compress(self.clean)):
            for size, checksum, message in ((len(config), sum(config)+1, 'checksum'),
                                            (len(prefix)+len(config)+1, sum(config), 'size')):
                with self.subTest(magic=prefix[:6].hex(), message=message):
                    footer = struct.pack('<II', size, checksum) + b'#BOOTCONFIG\n'
                    (self.root/'ramdisk').write_bytes(prefix + config + footer)
                    with self.assertRaisesRegex(S.ScanError, 'invalid Android bootconfig '+message):
                        self.scan()

    def test_bootconfig_does_not_hide_an_archive_or_unexplained_tail(self):
        config = b'androidboot.test = "boundary"\n'
        footer = struct.pack('<II', len(config), sum(config)) + b'#BOOTCONFIG\n'
        for tail, message in ((f.newc_archive('evil', f.elf_with_segments(f.EncodingTests.LUQ)), 'forbidden VEX IFMA'),
                              (b'junk', 'non-zero initramfs tail')):
            with self.subTest(message=message):
                (self.root/'ramdisk').write_bytes(gzip.compress(self.clean) + tail + config + footer)
                with self.assertRaisesRegex(S.ScanError, message):
                    self.scan()

    def test_declared_bootconfig_cannot_hide_binary_archive(self):
        evil = f.newc_archive('evil', f.elf_with_segments(f.EncodingTests.LUQ))
        footer = struct.pack('<II', len(evil), sum(evil)) + b'#BOOTCONFIG\n'
        for kind, prefix in [('newc', self.clean)] + self.frames(self.clean):
            with self.subTest(kind=kind):
                (self.root/'ramdisk').write_bytes(prefix + evil + footer)
                with self.assertRaisesRegex(S.ScanError, 'invalid Android bootconfig text'):
                    self.scan()

    def test_bootconfig_rejects_binary_or_internal_nul(self):
        for config in (b'key=ok\0hidden=bad\n', b'key=\xff\n', b'key=\x1b\n'):
            with self.subTest(config=config):
                footer = struct.pack('<II', len(config), sum(config)) + b'#BOOTCONFIG\n'
                (self.root/'ramdisk').write_bytes(self.clean + config + footer)
                with self.assertRaisesRegex(S.ScanError, 'invalid Android bootconfig text'):
                    self.scan()

    def test_bootconfig_allows_text_and_terminal_nul_padding(self):
        for padding in range(5):
            with self.subTest(padding=padding):
                config = b'key = "value"\n\t\r' + b'\0'*padding
                footer = struct.pack('<II', len(config), sum(config)) + b'#BOOTCONFIG\n'
                (self.root/'ramdisk').write_bytes(self.clean + config + footer)
                self.assertEqual(self.scan()['hits'], [])

    def test_malformed_frame_pin_cannot_hide_readable_plain_tail(self):
        evil = f.newc_archive('evil', f.elf_with_segments(f.EncodingTests.LUQ))
        for malformed in (b'\xfd7zXZ\0bad', b'\x1f\x8bbad', b'\x28\xb5\x2f\xfdbad', b'\x04\x22\x4d\x18bad', b'\x02\x21\x4c\x18bad'):
            with self.subTest(magic=malformed.hex()):
                data=malformed+evil
                (self.root/'ramdisk').write_bytes(data)
                pin={'partition':'system_a','container_chain':[{'kind':'tree_file','path':'ramdisk'}], 'sha256':hashlib.sha256(data).hexdigest(), 'reason':'invalid framing must be fatal'}
                with self.assertRaises(S.ScanError):
                    self.scan(unsupported_allowlist=[pin])

    def test_frame_decoder_failure_cannot_pin_the_entire_mixed_stream(self):
        data=gzip.compress(self.clean)+f.newc_archive('evil', f.elf_with_segments(f.EncodingTests.LUQ))
        (self.root/'ramdisk').write_bytes(data)
        pin={'partition':'system_a','container_chain':[{'kind':'tree_file','path':'ramdisk'}], 'sha256':hashlib.sha256(data).hexdigest(), 'reason':'only the bounded frame may match an unsupported pin'}
        with mock.patch.object(S, '_scan_decoded_payload', side_effect=S.ImageOpenError('decoder validation failure')) as decoder:
            with self.assertRaises(S.ScanError):
                self.scan(unsupported_allowlist=[pin])
        decoder.assert_called_once()

    def test_adjacent_plain_archives_have_distinct_container_identities(self):
        (self.root/'ramdisk').write_bytes(self.clean+self.clean)
        events=[]
        report=self.scan(coverage_callback=events.append)
        self.assertEqual(sum(row['kind']=='cpio-newc' for row in report['nested_opened']), 2)
        data=[row for row in events if row['classification']=='data']
        self.assertEqual(len(data), 2)
        self.assertNotEqual(data[0]['container_chain'], data[1]['container_chain'])

    def test_legacy_lz4_accepts_short_zero_padding(self):
        for kind, frame in self.frames(self.clean):
            if kind != 'lz4-legacy':
                continue
            for count in (1,2,3):
                with self.subTest(count=count):
                    (self.root/'ramdisk').write_bytes(frame + b'\0'*count)
                    self.assertEqual(self.scan()['hits'], [])

    def test_renamed_readable_containers_are_opened(self):
        evil = f.elf_with_segments(f.EncodingTests.LUQ)
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('payload', evil)
        cases = [('newc', f.newc_archive('evil', evil)), ('zip', out.getvalue())] + self.frames(f.newc_archive('evil', evil))
        for kind, data in cases:
            with self.subTest(kind=kind):
                (self.root/'ordinary.data').write_bytes(data)
                with self.assertRaisesRegex(S.ScanError, 'forbidden VEX IFMA'):
                    self.scan()

    def test_renamed_unopenable_magic_is_refused(self):
        cases = [b'AVB0vbmeta', b'PK\x03\x04bad', b'\xfd7zXZ\0bad', b'\x1f\x8bbad', b'\x28\xb5\x2f\xfdbad', b'\x04\x22\x4d\x18bad', b'\x02\x21\x4c\x18bad', b'070701bad', b'\x3a\xff\x26\xedbad']
        for offset, magic in [(1024,b'\xe2\xe1\xf5\xe0'), (1080,b'\x53\xef'), (0x202,b'HdrS')]:
            data=bytearray(2048); data[offset:offset+len(magic)]=magic; cases.append(bytes(data))
        for data in cases:
            with self.subTest(magic=data[:8].hex(), size=len(data)):
                (self.root/'ordinary').write_bytes(data)
                with self.assertRaises(S.ScanError):
                    self.scan()

    def test_unknown_magic_requires_opening_for_image_suffix(self):
        for name in ('ordinary', 'opaque.img', 'opaque.xz', 'opaque.ko', 'opaque.zip'):
            with self.subTest(name=name):
                path=self.root/name; path.write_bytes(b'not a recognized container')
                if path.suffix in S.IMAGE_SUFFIXES:
                    with self.assertRaises(S.ScanError):
                        self.scan()
                else:
                    self.assertEqual(self.scan()['unsupported_images'], [])
                path.unlink()

    def test_coverage_callback_accounts_for_data_and_containers(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as archive:
            archive.writestr('note.txt',b'zip data')
            archive.writestr('nested',gzip.compress(f.newc_archive('cpio.txt',b'cpio data')))
        (self.root/'outer').write_bytes(out.getvalue())
        (self.root/'root.txt').write_bytes(b'root data')
        events=[]
        self.scan(coverage_callback=events.append)
        data=[row for row in events if row['classification']=='data']
        self.assertEqual(sorted(row['container_chain'][-1]['path'] for row in data), ['cpio.txt','note.txt','root.txt'])
        self.assertEqual(len(events),len({(row['source'],str(row['container_chain'])) for row in events}))
        self.assertTrue(all(len(row['sha256'])==64 and 'size' in row and 'prefix_hex' in row for row in events))

if __name__=='__main__':
    unittest.main(verbosity=2)
