#!/usr/bin/env python3
"""Bounded extraction of an AVB-wrapped EROFS partition from a larger disk."""

import hashlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import ifma


class SystemExtentTests(unittest.TestCase):
    category = "unit"

    def test_stream_stays_inside_erofs_extent_with_bounded_reads(self):
        payload = b"EROFS fixture\0" * 180_000
        vbmeta = b"verified-metadata" * 100
        footer = struct.pack(">4sIIQQQ28x", b"AVBf", 1, 0, len(payload), len(payload), len(vbmeta))
        extent = payload + vbmeta + footer
        prefix = b"previous partition" * 100
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "disk.img"
            output = Path(directory) / "system.erofs"
            image.write_bytes(prefix + extent + b"following partition" * 100)
            real_copy = ifma.shutil.copyfileobj
            reads = []

            def observed_copy(source, target, length):
                class ObservedReader:
                    def read(self, count):
                        self_test.assertGreater(count, 0)
                        self_test.assertLessEqual(count, 1024 * 1024)
                        chunk = source.read(count)
                        reads.append(len(chunk))
                        return chunk
                real_copy(ObservedReader(), target, length)

            self_test = self
            with mock.patch.object(ifma.shutil, "copyfileobj", side_effect=observed_copy):
                ifma._copy_erofs_extent(image, len(prefix), len(extent), output)
            self.assertEqual(output.stat().st_size, len(payload))
            self.assertEqual(hashlib.sha256(output.read_bytes()).digest(), hashlib.sha256(payload).digest())
            self.assertGreater(len(reads), 2)

    def test_out_of_bounds_extent_refused_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "disk.img"
            output = Path(directory) / "system.erofs"
            image.write_bytes(b"x" * 128)
            for offset, size in [(-1, 64), (0, 63), (80, 64), (0, 129)]:
                with self.subTest(offset=offset, size=size):
                    with self.assertRaisesRegex(SystemExit, "extent is truncated or outside"):
                        ifma._copy_erofs_extent(image, offset, size, output)
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
