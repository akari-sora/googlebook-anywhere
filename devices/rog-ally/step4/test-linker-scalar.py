#!/usr/bin/env python3
"""Synthetic patch/verdict contracts; real original ELF stays on Linux."""
import importlib.util
from pathlib import Path
import os
import stat
import struct
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


patch = module('patch_linker', 'patch-linker-scalar.py')
runner = module('run_linker', 'run-linker-scalar.py')


def fixture():
    data = bytearray(patch.SOURCE_SIZE)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<H', data, 18, 62)
    struct.pack_into('<Q', data, 32, 64)
    struct.pack_into('<HH', data, 54, 56, len(patch.LOADS))
    for i, (off, va, size, mem, flags) in enumerate(patch.LOADS):
        struct.pack_into('<IIQQQQQQ', data, 64 + i * 56, 1, flags, off, va, 0, size, mem, 4096)
    for offset, old in patch.GATES:
        data[offset:offset + 2] = old
    return bytes(data)


def authenticated_fixture(data):
    # In-process tests only: the shipping CLI cannot override authentication.
    updated = bytearray(data)
    for offset, _ in patch.GATES:
        updated[offset:offset + 2] = patch.NEW
    functions = [(a, b, patch.digest(data[a:b]), patch.digest(updated[a:b]))
                 for a, b, _, _ in patch.FUNCTIONS]
    return mock.patch.multiple(patch, SOURCE_SHA256=patch.digest(data), FUNCTIONS=functions)


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'linker64'
        self.output = self.root / 'linker64.scalar'
        self.data = fixture()
        self.source.write_bytes(self.data)
        self.source.chmod(0o751)
        self.pins = authenticated_fixture(self.data)
        self.pins.start()
        self.xattr_mock = None
        if not hasattr(os, 'listxattr'):
            self.xattr_mock = mock.patch.object(os, 'listxattr', return_value=[], create=True)
            self.xattr_mock.start()

    def tearDown(self):
        self.pins.stop()
        if self.xattr_mock:
            self.xattr_mock.stop()
        self.temp.cleanup()

    def test_exact_six_bytes_mode_readback_original_preserved(self):
        result = patch.patch(self.source, self.output)
        out = self.output.read_bytes()
        expected = [o + i for o, _ in patch.GATES for i in (0, 1)]
        self.assertEqual([i for i, (a, b) in enumerate(zip(self.data, out)) if a != b], expected)
        self.assertEqual(result['changed_offsets'], expected)
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o751)
        self.assertEqual(result['output_sha256'], patch.digest(out))

    def test_unknown_and_already_patched_sha_refused(self):
        for data in (self.data[:-1] + b'!', patch.transform(self.data)):
            self.source.write_bytes(data)
            with self.assertRaisesRegex(patch.Refused, 'SHA256'):
                patch.patch(self.source, self.output)
            self.assertFalse(self.output.exists())

    def test_each_old_gate_is_independently_required(self):
        for offset, _ in patch.GATES:
            data = bytearray(self.data)
            data[offset] ^= 1
            with authenticated_fixture(bytes(data)):
                with self.assertRaisesRegex(patch.Refused, 'old gate'):
                    patch.transform(bytes(data))

    def test_both_whole_function_pins(self):
        for index in (0, 1):
            functions = list(patch.FUNCTIONS)
            a, b, _, new = functions[index]
            functions[index] = (a, b, '0' * 64, new)
            with mock.patch.object(patch, 'FUNCTIONS', functions):
                with self.assertRaisesRegex(patch.Refused, 'whole-function context'):
                    patch.transform(self.data)

    def test_patched_function_pin(self):
        functions = [(a, b, old, '0' * 64) for a, b, old, _ in patch.FUNCTIONS]
        with mock.patch.object(patch, 'FUNCTIONS', functions):
            with self.assertRaisesRegex(patch.Refused, 'patched whole-function'):
                patch.transform(self.data)

    def test_load_table_pinned(self):
        data = bytearray(self.data)
        struct.pack_into('<I', data, 64 + 56 + 4, 4)
        with authenticated_fixture(bytes(data)):
            with self.assertRaisesRegex(patch.Refused, 'PT_LOAD table'):
                patch.transform(bytes(data))

    def test_existing_output_alias_and_symlinks_refused(self):
        self.output.write_bytes(b'keep')
        with self.assertRaises(patch.Refused):
            patch.patch(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b'keep')
        with self.assertRaises(patch.Refused):
            patch.patch(self.source, self.source)
        self.output.unlink()
        self.output.symlink_to(self.root / 'missing')
        with self.assertRaises(patch.Refused):
            patch.patch(self.source, self.output)
        self.output.unlink()
        link = self.root / 'link'
        link.symlink_to(self.source)
        with self.assertRaisesRegex(patch.Refused, 'symlink'):
            patch.patch(link, self.output)
        parent = self.root / 'parent'
        parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(patch.Refused, 'symlink'):
            patch.patch(self.source, parent / 'new')

    def test_hardlink_refused(self):
        os.link(self.source, self.root / 'alias')
        with self.assertRaisesRegex(patch.Refused, 'hardlinked'):
            patch.patch(self.source, self.output)

    def test_fsync_failure_cleanup(self):
        with mock.patch.object(patch.os, 'fsync', side_effect=OSError('test fsync failure')):
            with self.assertRaises(OSError):
                patch.patch(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), self.data)

    def rows(self, gfni=False):
        rows = []
        for kind in range(4):
            for state in ('original', 'patched'):
                for mode in ('native', 'tcg'):
                    native = mode == 'native'
                    header = (f'PROBE_KIND={hex(kind)}\nCPUID.7.1.EAX={hex(16 if native else 0)}\n'
                              f'CPUID.7.0.ECX={hex(256 if native or gfni else 0x0041021c)}\n'
                              'CPUID.1.ECX=0x18000000\nCPUID.80000001.ECX=0x20\n')
                    if state == 'original' and not native:
                        site = runner.FAULTS[kind][1 if gfni else 0] + 0x40000000
                        code = 0xc4c27152f5 if gfni else 0xc4c3f1cec9
                        text = (f'FAULT_RIP={hex(site)}\nFAULT_BYTES5={hex(code)}\n'
                                'COMPLETED_CASES=0x5a\nBEGIN_FIRST_COUNT=0x8\nBEGIN_SECOND_COUNT=0x8\n'
                                'SIGNAL=0x4\nSI_CODE=0x2\nFAULT_CAPTURED\n')
                        status = 137
                    else:
                        text = f'CASES={hex(260 if kind == 3 else 250)}\nRESULT_DIGEST={hex(1234 + kind)}\nALL_PASS\n'
                        status = 0 if native else 1
                    rows.append(dict(kind=kind, state=state, mode=mode, output=header + text, exit=status))
        return rows

    def test_gfni_fault_is_classified_not_vnni_isolation(self):
        result = runner.verify(self.rows())
        self.assertEqual(len(result), 4)
        self.assertTrue(all(x['classification'] == 'GFNI_FIRST_UNSUPPORTED_NOT_VNNI_ISOLATED' for x in result))

    def test_exact_vnni_isolation_requires_gfni(self):
        result = runner.verify(self.rows(gfni=True))
        self.assertTrue(all(x['classification'] == 'EXACT_VNNI_SIGILL_ISOLATED' for x in result))
        rows = self.rows()
        rows[1]['output'] = rows[1]['output'].replace('0x4018e310', '0x4018e3e3')
        with self.assertRaisesRegex(runner.patch.Refused, 'first unsupported site'):
            runner.verify(rows)

    def test_wrong_signal_site_bytes_tiny_cases_refused(self):
        for old, new in [('SIGNAL=0x4', 'SIGNAL=0xb'), ('SI_CODE=0x2', 'SI_CODE=0x1'),
                         ('0x4018e310', '0x4018e326'), ('0xc4c3f1cec9', '0xc4c27152f5'),
                         ('COMPLETED_CASES=0x5a', 'COMPLETED_CASES=0x59')]:
            with self.subTest(old=old):
                rows = self.rows()
                rows[1]['output'] = rows[1]['output'].replace(old, new)
                with self.assertRaises(runner.patch.Refused):
                    runner.verify(rows)

    def test_duplicate_partial_digest_mismatch_refused(self):
        for change in ('duplicate', 'partial', 'digest'):
            rows = self.rows()
            if change == 'duplicate':
                rows[-1] = dict(rows[-2])
            elif change == 'partial':
                rows[-1]['output'] = rows[-1]['output'].replace('CASES=0x104', 'CASES=0xfa')
            else:
                rows[-1]['output'] = rows[-1]['output'].replace('DIGEST=0x4d5', 'DIGEST=0x4d6')
            with self.assertRaises(runner.patch.Refused):
                runner.verify(rows)

    def test_privacy_guards_before_mapping_and_calls(self):
        main = (ROOT / 'probe-linker-scalar.c').read_text().split('static void probe_main(void) {', 1)[1]
        dumpable = main.index('sys(157, 4, 0, 0, 0, 0, 0) != 0')
        privs = main.index('sys(157, 38, 1, 0, 0, 0, 0) != 0')
        fail = main.index('put("PRIVACY_GUARD_FAIL\\n"); done(2);')
        self.assertLess(dumpable, privs)
        self.assertLess(privs, fail)
        self.assertLess(fail, main.index('load(TARGET_BASE)'))
        self.assertLess(fail, main.index('run_case(consumer'))
        self.assertNotIn('#ifdef', main[dumpable:fail])


if __name__ == '__main__':
    unittest.main(verbosity=2)
