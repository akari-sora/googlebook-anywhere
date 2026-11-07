#!/usr/bin/env python3
"""Synthetic local contract tests, NOT private ELF/native/TCG acceptance."""
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

patch = module('patch', 'patch-init-scalar.py')
runner = module('runner', 'run-init-scalar.py')


def fixture():
    data = bytearray(patch.SOURCE_SIZE)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<H', data, 18, 62)
    struct.pack_into('<Q', data, 32, 64)
    struct.pack_into('<HH', data, 54, 56, 1)
    struct.pack_into('<IIQQQQQQ', data, 64, 1, 5, 0, 0x200000, 0,
                     len(data), len(data), 4096)
    data[patch.GATE_OFFSET:patch.GATE_OFFSET + 2] = patch.OLD
    return bytes(data)


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'init'
        self.output = self.root / 'init.scalar'
        self.data = fixture()
        self.source.write_bytes(self.data)
        self.source.chmod(0o751)
        # Test-only authenticated fixture pins: shipping CLI has no override.
        self.pins = mock.patch.multiple(patch, SOURCE_SHA256=patch.digest(self.data),
            FUNCTION_SHA256=patch.digest(self.data[0x2b8440:0x2b8878]))
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

    def test_only_two_bytes_mode_readback_and_input_unchanged(self):
        result = patch.patch(self.source, self.output)
        out = self.output.read_bytes()
        self.assertEqual([i for i, (a, b) in enumerate(zip(self.data, out)) if a != b],
                         [patch.GATE_OFFSET, patch.GATE_OFFSET + 1])
        self.assertEqual(out, self.data[:patch.GATE_OFFSET] + b'\x90\x90' + self.data[patch.GATE_OFFSET + 2:])
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o751)
        self.assertEqual(result['output_sha256'], patch.digest(out))

    def test_wrong_hash_no_output(self):
        self.source.write_bytes(self.data[:-1] + b'!')
        with self.assertRaisesRegex(patch.Refused, 'SHA256'):
            patch.patch(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_wrong_gate_even_with_authenticated_fixture_hash(self):
        data = bytearray(self.data)
        data[patch.GATE_OFFSET:patch.GATE_OFFSET + 2] = b'\x72\x13'
        with mock.patch.multiple(patch, SOURCE_SHA256=patch.digest(data),
                                FUNCTION_SHA256=patch.digest(data[0x2b8440:0x2b8878])):
            with self.assertRaisesRegex(patch.Refused, 'old gate'):
                patch.transform(bytes(data))

    def test_function_context_pin(self):
        with mock.patch.object(patch, 'FUNCTION_SHA256', '0' * 64):
            with self.assertRaisesRegex(patch.Refused, 'function context'):
                patch.transform(self.data)

    def test_mapping_refused(self):
        data = bytearray(self.data)
        struct.pack_into('<I', data, 68, 4)  # non-executable load
        with mock.patch.object(patch, 'SOURCE_SHA256', patch.digest(data)):
            with self.assertRaisesRegex(patch.Refused, 'mapping'):
                patch.transform(bytes(data))

    def test_existing_output_never_overwritten(self):
        self.output.write_bytes(b'keep')
        with self.assertRaisesRegex(patch.Refused, 'already exists'):
            patch.patch(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b'keep')

    def test_input_output_alias(self):
        with self.assertRaises(patch.Refused):
            patch.patch(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_symlink_input_and_parent_and_output(self):
        link = self.root / 'link'
        link.symlink_to(self.source)
        with self.assertRaisesRegex(patch.Refused, 'symlink'):
            patch.patch(link, self.output)
        parent = self.root / 'parent'
        parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(patch.Refused, 'symlink'):
            patch.patch(self.source, parent / 'new')
        self.output.symlink_to(self.root / 'missing')
        with self.assertRaises(patch.Refused):
            patch.patch(self.source, self.output)

    def test_hardlink_input(self):
        os.link(self.source, self.root / 'hardlink')
        with self.assertRaisesRegex(patch.Refused, 'hardlinked'):
            patch.patch(self.source, self.output)

    def test_fsync_failure_removes_partial_output(self):
        with mock.patch.object(patch.os, 'fsync', side_effect=OSError('fixture fsync failure')):
            with self.assertRaises(OSError):
                patch.patch(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_already_patched_refused(self):
        self.source.write_bytes(patch.transform(self.data))
        with self.assertRaisesRegex(patch.Refused, 'SHA256'):
            patch.patch(self.source, self.output)

    def test_cpio_contains_guest_as_init(self):
        exe = self.root / 'guest'
        exe.write_bytes(b'guest-fixture')
        data = runner.cpio(exe).read_bytes()
        self.assertEqual(data[:6], b'070701')
        self.assertIn(b'init\0', data)
        self.assertIn(b'guest-fixture', data)
        self.assertIn(b'TRAILER!!!\0', data)

    def rows(self):
        rows = []
        for state in ('original', 'patched'):
            for mode in ('native', 'tcg'):
                header = ('CPUID.7.1.EAX=' + ('0x10' if mode == 'native' else '0x0') + '\n'
                          'CPUID.7.0.ECX=0x100\nCPUID.1.ECX=0x18000000\nCPUID.80000001.ECX=0x20\n')
                if state == 'original' and mode == 'tcg':
                    text = ('COMPLETED_CASES=0x5a\nBEGIN_COUNT=0x8\nSIGNAL=0x4\nSI_CODE=0x2\n'
                            'FAULT_RIP=0x404b86e4\nFAULT_BYTES5=0xc4c27152f5\nFAULT_CAPTURED\n')
                    code = 137
                else:
                    text = 'CASES=0xfa\nRESULT_DIGEST=0x1234\nALL_PASS\n'
                    code = 0 if mode == 'native' else 1
                rows.append(dict(state=state, mode=mode, output=header + text, exit=code))
        return rows

    def test_privacy_guards_precede_target_mapping_and_calls(self):
        # Source-order contract only; actual Linux prctl behavior needs a real build/run.
        source = (ROOT / 'probe-init-scalar.c').read_text()
        main = source.split('static void probe_main(void) {', 1)[1]
        guard = main.index('if (sys(157, 4, 0, 0, 0, 0, 0) != 0 ||')
        no_new_privs = main.index('sys(157, 38, 1, 0, 0, 0, 0) != 0) {')
        failure = main.index('put("PRIVACY_GUARD_FAIL\\n"); done(2);')
        self.assertLess(guard, no_new_privs)
        self.assertLess(no_new_privs, failure)
        self.assertLess(failure, main.index('load(TARGET_BASE);'))
        self.assertLess(failure, main.index('consumer(object)'))
        self.assertNotIn('#ifdef', main[guard:failure])

    def test_four_cell_verifier_contract(self):
        runner.verify(self.rows())

    def test_missing_gfni_actual_mask_refused_in_every_cell(self):
        # Actual first failed baseline TCG run: 0x0041021c has no GFNI bit8.
        # Reject this mask even if a forged log otherwise claims full success.
        for index in range(4):
            with self.subTest(cell=index):
                rows = self.rows()
                rows[index]['output'] = rows[index]['output'].replace(
                    'CPUID.7.0.ECX=0x100', 'CPUID.7.0.ECX=0x0041021c')
                with self.assertRaisesRegex(runner.patch.Refused, 'GFNI absent'):
                    runner.verify(rows)

    def test_test_model_deliberately_adds_gfni(self):
        self.assertEqual(runner.TCG_CPU, 'EPYC-Genoa-v3,+gfni')

    def test_duplicate_cell_refused(self):
        rows = self.rows()
        rows[3] = dict(rows[2])
        with self.assertRaisesRegex(runner.patch.Refused, 'distinct cells'):
            runner.verify(rows)

    def test_wrong_tcg_feature_and_digest_refused(self):
        rows = self.rows()
        rows[3]['output'] = rows[3]['output'].replace('CPUID.7.1.EAX=0x0', 'CPUID.7.1.EAX=0x10')
        with self.assertRaisesRegex(runner.patch.Refused, 'VNNI capability'):
            runner.verify(rows)
        rows = self.rows()
        rows[3]['output'] = rows[3]['output'].replace('DIGEST=0x1234', 'DIGEST=0x1235')
        with self.assertRaisesRegex(runner.patch.Refused, 'digests differ'):
            runner.verify(rows)

    def test_arbitrary_sigill_and_partial_matrix_refused(self):
        rows = self.rows()
        rows[1]['output'] = rows[1]['output'].replace('0x404b86e4', '0x404b8611')
        with self.assertRaisesRegex(runner.patch.Refused, 'unexpected first fault'):
            runner.verify(rows)
        rows = self.rows()
        rows[3]['output'] = rows[3]['output'].replace('CASES=0xfa', 'CASES=0xf9')
        with self.assertRaisesRegex(runner.patch.Refused, 'incomplete matrix'):
            runner.verify(rows)


if __name__ == '__main__':
    unittest.main(verbosity=2)
