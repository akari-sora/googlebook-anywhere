#!/usr/bin/env python3
"""Round-four suffix, unsupported magic, and optional-tool accounting checks."""
import gzip
import hashlib
import importlib.util
import io
import lzma
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('r4_boundary', HERE / 'ifma-boundary-tests.py')
boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
S = boundary.S
if os.environ.get('IFMA_SCANNER_UNDER_TEST'):
    spec = importlib.util.spec_from_file_location('r4_policy_scanner', os.environ['IFMA_SCANNER_UNDER_TEST'])
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)


class PolicyTests(unittest.TestCase):
    category = 'synthetic'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_unknown_image_suffix_is_refused_in_tree_and_zip(self):
        for suffix in sorted(S.IMAGE_SUFFIXES):
            for zipped in (False, True):
                with self.subTest(suffix=suffix, zipped=zipped):
                    if zipped:
                        out = io.BytesIO()
                        with zipfile.ZipFile(out, 'w') as archive:
                            archive.writestr('opaque' + suffix, b'opaque image')
                        path = self.root / 'outer'
                        path.write_bytes(out.getvalue())
                    else:
                        path = self.root / ('opaque' + suffix)
                        path.write_bytes(b'opaque image')
                    with self.assertRaisesRegex(S.ScanError, 'unsupported|unrecognized'):
                        S.scan_tree(self.root)
                    path.unlink()

    def test_unsupported_container_magics_require_exact_pin(self):
        for magic, expected_kind in (
            (b'BZh', 'bzip2'), (b'BZ9', 'bzip2'),
            (b'\x5d\x00\x00', 'lzma-alone'), (b'\x5d\x00\x80\x00\x00', 'lzma-alone'),
            (b'\x89LZO', 'lzo'), (b'\x89L\x00', 'lzo'),
            (b'\x1f\x9e', 'gzip-old'),
            (b'ANDROID!', 'android-boot'), (b'VNDRBOOT', 'android-vendor-boot'),
        ):
            with self.subTest(magic=magic):
                data = magic + b'opaque body'
                path = self.root / 'ordinary'
                path.write_bytes(data)
                expected_reason = f'unsupported {expected_kind} at 0x0'
                with self.assertRaisesRegex(S.ScanError, expected_reason):
                    S.scan_tree(self.root, provenance={'partition': 'system_a'})
                pin = {'partition': 'system_a', 'container_chain': [{'kind': 'tree_file', 'path': 'ordinary'}],
                       'sha256': hashlib.sha256(data).hexdigest(), 'reason': 'test-only opaque format pin'}
                report = S.scan_tree(self.root, provenance={'partition': 'system_a'}, unsupported_allowlist=[pin])
                self.assertEqual(len(report['unsupported_images']), 1)
                self.assertEqual(report['unsupported_images'][0]['reason'], expected_reason)
                path.unlink()

        padded_bzh = b'\0' * 4097 + b'BZh91AY&SYopaque'
        path = self.root / 'padded_bzh.bin'
        path.write_bytes(padded_bzh)
        try:
            with self.assertRaisesRegex(S.ScanError, r'unsupported bzip2 at 0x1001'):
                S.scan_tree(self.root, provenance={'partition': 'system_a'})
            pin = {
                'partition': 'system_a',
                'container_chain': [{'kind': 'tree_file', 'path': 'padded_bzh.bin'}],
                'sha256': hashlib.sha256(padded_bzh).hexdigest(),
                'reason': 'test-only padded bzip2 pin',
            }
            report = S.scan_tree(
                self.root,
                provenance={'partition': 'system_a'},
                unsupported_allowlist=[pin],
            )
            self.assertEqual(len(report['unsupported_images']), 1)
            self.assertEqual(
                report['unsupported_images'][0]['reason'],
                'unsupported bzip2 at 0x1001',
            )
        finally:
            path.unlink(missing_ok=True)

        for ordinary in (b'BAnot-bzip2', b'\x5d\x01not-lzma', b'\x89Mnot-lzo', b'\x1f\x00not-gzip'):
            with self.subTest(ordinary=ordinary):
                path = self.root / 'ordinary'
                path.write_bytes(ordinary)
                events = []
                report = S.scan_tree(self.root, coverage_callback=events.append)
                self.assertEqual(report['unsupported_images'], [])
                self.assertEqual(events[-1]['classification'], 'data')
                path.unlink()

    def test_claude_c1_to_c4_are_refused(self):
        evil = boundary.f.newc_archive('evil', boundary.f.elf_with_segments(boundary.f.EncodingTests.LUQ))
        cases = [('initrd.img', b'\0'*4 + evil), ('initrd.img', b'\0'*4 + gzip.compress(evil)),
                 ('boot.img', b'ANDROID!' + b'\0'*(4096-8) + gzip.compress(evil)),
                 ('payload.lz4', b'\0junk-not-lz4' + evil)]
        for index, (name, data) in enumerate(cases, 1):
            with self.subTest(case='C' + str(index)):
                path = self.root / name
                path.write_bytes(data)
                with self.assertRaises(S.ScanError):
                    S.scan_tree(self.root)
                path.unlink()

    def test_optional_frame_tools_generate_counted_skips(self):
        class MissingTools(boundary.BoundaryTests):
            def runTest(inner):
                self.assertEqual(len(inner.frames(b'data')), 2)
        result = unittest.TestResult()
        with mock.patch.object(boundary.shutil, 'which', return_value=None):
            MissingTools().run(result)
        self.assertEqual(len(result.skipped), 3)
        self.assertEqual(result.errors + result.failures, [])

    def test_compressed_image_inner_suffix_is_enforced_while_compressed_assets_stay_data(self):
        opaque = b'not a recognized inner container or elf'
        opaque_sha = hashlib.sha256(opaque).hexdigest()
        for inner_suffix in sorted(S.IMAGE_SUFFIXES - {'.ko'}):
            name = f'corrupt{inner_suffix}.gz'
            with self.subTest(entry='tree-gzip', name=name):
                path = self.root / name
                path.write_bytes(gzip.compress(opaque))
                try:
                    with self.assertRaisesRegex(S.ScanError, 'unsupported|unrecognized'):
                        S.scan_tree(self.root, provenance={'partition': 'system_a'})
                    pin = {
                        'partition': 'system_a',
                        'container_chain': [
                            {'kind': 'tree_file', 'path': name},
                            {'kind': 'gzip_payload', 'path': '<gzip>'},
                        ],
                        'sha256': opaque_sha,
                        'reason': 'test-only inner image suffix pin',
                    }
                    report = S.scan_tree(
                        self.root,
                        provenance={'partition': 'system_a'},
                        unsupported_allowlist=[pin],
                    )
                    self.assertEqual(len(report['unsupported_images']), 1)
                    self.assertTrue(
                        report['unsupported_images'][0]['source'].endswith(f'/{name}!/<gzip>'),
                    )
                finally:
                    path.unlink(missing_ok=True)

            with self.subTest(entry='zip-gzip', name=name):
                out = io.BytesIO()
                with zipfile.ZipFile(out, 'w') as archive:
                    archive.writestr(f'nested/{name}', gzip.compress(opaque))
                path = self.root / 'bundle.zip'
                path.write_bytes(out.getvalue())
                try:
                    with self.assertRaisesRegex(S.ScanError, 'unsupported|unrecognized'):
                        S.scan_tree(self.root, provenance={'partition': 'system_a'})
                finally:
                    path.unlink(missing_ok=True)

        for asset_name, body in (
            ('notice.txt.gz', b'plain notice text\n'),
            ('printer.ppd.gz', b'*PPD-Adobe: "4.3"\n'),
            ('index.html.gz', b'<html><body>ok</body></html>\n'),
        ):
            with self.subTest(asset=asset_name):
                path = self.root / asset_name
                path.write_bytes(gzip.compress(body))
                try:
                    events = []
                    report = S.scan_tree(self.root, coverage_callback=events.append)
                    self.assertEqual(report['hits'], [])
                    decoded = [
                        row for row in events
                        if row['container_chain'][-1]['kind'] == 'gzip_payload'
                    ]
                    self.assertEqual(len(decoded), 1)
                    self.assertEqual(decoded[0]['classification'], 'data')
                finally:
                    path.unlink(missing_ok=True)

    def test_multilayer_and_alternate_compression_suffixes_are_tracked_per_layer(self):
        text = b'plain notice text\n'
        clean_assets = (
            ('notice.txt.gz.gz', gzip.compress(gzip.compress(text))),
            ('notice.txt.xz.gz', gzip.compress(lzma.compress(text))),
            ('notice.txt.gz.xz', lzma.compress(gzip.compress(text))),
            ('notice.txt.gz', lzma.compress(text)),
            ('notice.txt.xz', gzip.compress(text)),
            ('notice.txt.zst', gzip.compress(text)),
        )
        for name, payload in clean_assets:
            for zipped in (False, True):
                with self.subTest(clean=name, zipped=zipped):
                    if zipped:
                        out = io.BytesIO()
                        with zipfile.ZipFile(out, 'w') as archive:
                            archive.writestr(name, payload)
                        path = self.root / 'assets.zip'
                        path.write_bytes(out.getvalue())
                    else:
                        path = self.root / name
                        path.write_bytes(payload)
                    try:
                        events = []
                        report = S.scan_tree(self.root, coverage_callback=events.append)
                        self.assertEqual(report['hits'], [])
                        self.assertEqual(report['unsupported_images'], [])
                        self.assertEqual(events[-1]['classification'], 'data')
                    finally:
                        path.unlink(missing_ok=True)

        opaque = b'not a recognized inner container or elf'
        zstd_opaque = (
            b'\x28\xb5\x2f\xfd\x20'
            + bytes([len(opaque)])
            + ((len(opaque) << 3) | 1).to_bytes(3, 'little')
            + opaque
        )
        refused_cases = (
            ('corrupt.img.gz.xz', lzma.compress(gzip.compress(opaque))),
            ('corrupt.img.zstd', zstd_opaque),
            ('corrupt.img.gzip', gzip.compress(opaque)),
            ('corrupt.img.gz', lzma.compress(opaque)),
        )
        for name, payload in refused_cases:
            for zipped in (False, True):
                with self.subTest(refused=name, zipped=zipped):
                    if zipped:
                        out = io.BytesIO()
                        with zipfile.ZipFile(out, 'w') as archive:
                            archive.writestr(name, payload)
                        path = self.root / 'bundle.zip'
                        path.write_bytes(out.getvalue())
                    else:
                        path = self.root / name
                        path.write_bytes(payload)
                    try:
                        with self.assertRaisesRegex(S.ScanError, 'unsupported|unrecognized'):
                            S.scan_tree(self.root, provenance={'partition': 'system_a'})
                    finally:
                        path.unlink(missing_ok=True)

        clean_cpio = boundary.f.newc_archive('clean', b'clean')
        tail_initrd = clean_cpio + gzip.compress(opaque)
        with self.subTest(refused='initrd.img-tail-gzip'):
            path = self.root / 'initrd.img'
            path.write_bytes(tail_initrd)
            try:
                with self.assertRaisesRegex(S.ScanError, 'unsupported|unrecognized'):
                    S.scan_tree(self.root, provenance={'partition': 'system_a'})
            finally:
                path.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
