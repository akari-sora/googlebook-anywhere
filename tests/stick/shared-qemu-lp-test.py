#!/usr/bin/env python3
"""Bounded synthetic LP readback and corruption regressions."""
import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import struct
import tempfile
import unittest

path = Path(__file__).with_name('shared-qemu-lp.py')
spec = importlib.util.spec_from_file_location('shared_qemu_lp', path)
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)
SIZE = 8 * 1024 * 1024


def make_fixture(path, *, version, updated=False, wrong_old=False, wrong_new=False):
    """Construct an LP fixture independently of the updater and checker."""
    geometry = bytearray(52)
    struct.pack_into('<II32sIII', geometry, 0, 0x616C4467, 52, bytes(32), 65536, 3, 4096)
    geometry[8:40] = hashlib.sha256(geometry).digest()
    extents, data, partitions = [], [], bytearray()
    for slot in 'ab':
        for index, name in enumerate(lp.LOGICAL):
            present = slot == 'a' or updated
            count, first = int(present), len(extents)
            if present:
                sector = 2048 + len(extents)
                extents.append(struct.pack('<QIQI', 1, 0, sector, 0))
                v = 1 if slot == 'a' and updated else version
                if name != 'vendor':
                    v = 0
                if name == 'vendor' and ((slot == 'a' and wrong_old) or (slot == 'b' and wrong_new)):
                    v = 9
                data.append((sector, bytes([index + 20 * v]) * 512))
            partitions += (f'{name}_{slot}'.encode().ljust(36, b'\0')
                           + struct.pack('<IIII', 1, first, count, 1 if slot == 'a' else 2))
    groups = b''.join(name.encode().ljust(36, b'\0') + struct.pack('<IQ', 0, SIZE - 1048576)
                      for name in ('default', 'google_dynamic_partitions_a', 'google_dynamic_partitions_b'))
    block = struct.pack('<QIIQ', 2048, 1048576, 0, SIZE) + b'super'.ljust(36, b'\0') + struct.pack('<I', 0)
    tables = bytes(partitions) + b''.join(extents) + groups + block
    header = bytearray(256)
    struct.pack_into('<IHHI', header, 0, 0x414C5030, 10, 2, 256)
    struct.pack_into('<I', header, 44, len(tables))
    header[48:80] = hashlib.sha256(tables).digest()
    offset = 0
    for at, count, entry_size in ((80, 12, 52), (92, len(extents), 24), (104, 3, 48), (116, 1, 64)):
        struct.pack_into('<III', header, at, offset, count, entry_size)
        offset += count * entry_size
    header[12:44] = hashlib.sha256(header).digest()
    with path.open('w+b') as output:
        output.truncate(SIZE)
        for offset in (4096, 8192):
            output.seek(offset)
            output.write(geometry)
        for i in range(6):
            output.seek(12288 + i * 65536)
            output.write(header + tables)
        for sector, payload in data:
            output.seek(sector * 512)
            output.write(payload)


class LpReadback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.part = {'name': 'super', 'first': 0, 'last': SIZE // 512 - 1}

    def snap(self, name, **kwargs):
        path = self.root / name
        make_fixture(path, **kwargs)
        fd = os.open(path, os.O_RDONLY)
        try:
            return lp.snapshot(fd, self.part)
        finally:
            os.close(fd)

    def test_valid_update_checks_both_slots_and_all_copies(self):
        before = self.snap('before', version=1)
        release = self.snap('release', version=2)
        after = self.snap('after', version=2, updated=True)
        self.assertEqual(after['metadata_copies'], 6)
        lp.assert_fixture_change(before, release)
        self.assertIsNone(lp.verify_update(before, after, release))

    def test_one_corrupt_metadata_copy_is_rejected(self):
        path = self.root / 'corrupt'
        make_fixture(path, version=1)
        with path.open('r+b') as f:
            f.seek(12288 + 3 * 65536 + 14)
            byte = f.read(1)
            f.seek(-1, 1)
            f.write(bytes([byte[0] ^ 1]))
        fd = os.open(path, os.O_RDONLY)
        try:
            with self.assertRaisesRegex(ValueError, 'checksum'):
                lp.snapshot(fd, self.part)
        finally:
            os.close(fd)

    def test_old_slot_corruption_fails_update_verification(self):
        before = self.snap('before', version=1)
        release = self.snap('release', version=2)
        after = self.snap('after', version=2, updated=True, wrong_old=True)
        with self.assertRaisesRegex(ValueError, 'old logical slot A changed: vendor'):
            lp.verify_update(before, after, release)

    def test_new_slot_wrong_content_fails_update_verification(self):
        before = self.snap('before', version=1)
        release = self.snap('release', version=2)
        after = self.snap('after', version=2, updated=True, wrong_new=True)
        with self.assertRaisesRegex(ValueError, 'updated logical slot B differs from source: vendor'):
            lp.verify_update(before, after, release)

    def test_identical_vendor_fixture_does_not_count_as_upgrade(self):
        before = self.snap('before', version=1)
        release = self.snap('release', version=1)
        with self.assertRaisesRegex(ValueError, 'vendor did not change'):
            lp.assert_fixture_change(before, release)

    def test_existing_selftest_fixture_is_independently_readable(self):
        source = path.parents[2] / 'tests/update/selftest.py'
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'lp_super')
        namespace = {'hashlib': hashlib, 'struct': struct, 'MIB': 1048576}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<existing-fixture>', 'exec'), namespace)
        payloads = {name: bytes([i + 1]) * 512 for i, name in enumerate(lp.LOGICAL)}
        raw = namespace['lp_super'](SIZE, payloads, b'x' * 512)
        target = self.root / 'existing'
        target.write_bytes(raw)
        fd = os.open(target, os.O_RDONLY)
        try:
            result = lp.snapshot(fd, self.part)
        finally:
            os.close(fd)
        self.assertEqual(result['metadata_copies'], 6)
        for name, data in payloads.items():
            self.assertEqual(result['partitions'][name + '_a']['sha256'], hashlib.sha256(data).hexdigest())


if __name__ == '__main__':
    unittest.main()
