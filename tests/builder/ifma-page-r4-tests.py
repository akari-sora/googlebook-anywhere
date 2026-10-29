#!/usr/bin/env python3
"""Executable file-page coverage at PT_LOAD edges (Claude B0-B4)."""
import importlib.util
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'ifma_page_scanner', os.environ.get('IFMA_SCANNER_UNDER_TEST', ROOT / 'builder/ifma/scan-ifma.py'))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def page_elf(segments, at, code, size=0x3000):
    data = bytearray(size)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHIQQQIHHHHHH', data, 16, 2, 62, 1, 0x400000 + at,
                     64, 0, 0, 64, 56, len(segments), 0, 0, 0)
    for index, (offset, vaddr, filesz, memsz, flags) in enumerate(segments):
        struct.pack_into('<IIQQQQQQ', data, 64 + index*56, 1, flags, offset,
                         vaddr, vaddr, filesz, memsz, 4096)
    data[at:at+len(code)] = code
    return bytes(data)


class PageTests(unittest.TestCase):
    category = 'synthetic'

    def check_case(self, segments, at):
        for opcode in (bytes.fromhex('c4e2f1b4c3'), bytes.fromhex('c4e2f1b5c3')):
            with self.subTest(opcode=opcode.hex()):
                elf = page_elf(segments, at, opcode + b'\xc3')
                hits = S.scan_elf_bytes(elf)['hits']
                self.assertIn((at, 0x400000+at), [(h['file_offset'], h['virtual_address']) for h in hits])
                with tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp)/'tool').write_bytes(elf)
                    with self.assertRaisesRegex(S.ScanError, 'forbidden VEX IFMA'):
                        S.scan_tree(tmp)

    def test_b0_inside_filesz(self):
        self.check_case([(0, 0x400000, 0x1000, 0x1000, 5)], 0xff0)

    def test_b1_after_filesz_in_same_page(self):
        self.check_case([(0, 0x400000, 0xff0, 0xff0, 5)], 0xff4)

    def test_b2_instruction_crosses_touching_rx_pages(self):
        self.check_case([(0, 0x400000, 0xffe, 0xffe, 5), (0x1000, 0x401000, 0x1000, 0x1000, 5)], 0xffc)

    def test_b3_before_unaligned_offset(self):
        self.check_case([(0x100, 0x400100, 0xf00, 0xf00, 5)], 0x80)

    def test_b4_file_page_tail_with_larger_memsz(self):
        self.check_case([(0, 0x400000, 0xff0, 0x2000, 5)], 0xff4)

    def test_page_end_is_clamped_to_eof(self):
        elf = page_elf([(0, 0x400000, 0xff0, 0xff0, 5)], 0xff4, bytes.fromhex('c4e2f1b4c3'), size=0xff9)
        self.assertEqual([h['file_offset'] for h in S.scan_elf_bytes(elf)['hits']], [0xff4])

    def test_touching_virtual_pages_with_noncontiguous_file_bytes(self):
        opcode = bytes.fromhex('c4e2f1b4c3')
        elf = bytearray(page_elf([(0, 0x400000, 0xffe, 0xffe, 5),
                                 (0x2000, 0x401000, 0x1000, 0x1000, 5)], 0xffc, b''))
        elf[0xffc:0x1000] = opcode[:4]
        elf[0x2000] = opcode[4]
        self.assertEqual([(h['file_offset'], h['virtual_address'], h['encoding_hex'])
                          for h in S.scan_elf_bytes(elf)['hits']], [(0xffc, 0x400ffc, opcode.hex())])

    def test_next_unmapped_page_is_not_scanned(self):
        elf = page_elf([(0, 0x400000, 0xff0, 0xff0, 5)], 0x1004, bytes.fromhex('c4e2f1b4c3'))
        self.assertEqual(S.scan_elf_bytes(elf)['hits'], [])

    def test_page_bytes_and_zero_fill_candidates_are_both_retained(self):
        elf = page_elf([(0, 0x400000, 0xfff, 0x2000, 5)], 0xffb, bytes.fromhex('c4e2f1b4c3'))
        self.assertEqual({h['encoding_hex'] for h in S.scan_elf_bytes(elf)['hits']},
                         {'c4e2f1b4c3', 'c4e2f1b400'})

    def test_eof_clamped_partial_page_zero_fill_detects_boundary_ifma(self):
        for opcode_byte in (0xb4, 0xb5):
            full_zero_modrm = bytes([0xc4, 0xe2, 0xf1, opcode_byte, 0x00])
            with self.subTest(opcode=hex(opcode_byte), bytes_before_eof=4):
                size = 0x210
                at = size - 4
                elf = page_elf([(0, 0x400000, size, size, 5)], at, full_zero_modrm[:4], size=size)
                hits = S.scan_elf_bytes(elf)['hits']
                self.assertEqual(
                    [(h['file_offset'], h['virtual_address'], h['encoding_hex']) for h in hits],
                    [(at, 0x400000 + at, full_zero_modrm.hex())],
                )
                with tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp) / 'exec').write_bytes(elf)
                    with self.assertRaisesRegex(S.ScanError, 'forbidden VEX IFMA'):
                        S.scan_tree(tmp)

            # When EOF is on a page boundary, no zeros are synthesized into the next unmapped page.
            with self.subTest(opcode=hex(opcode_byte), page_aligned_eof=True):
                elf = page_elf([(0, 0x400000, 0x1000, 0x1000, 5)], 0xffc, full_zero_modrm[:4], size=0x1000)
                self.assertEqual(S.scan_elf_bytes(elf)['hits'], [])

            # When 1 zero byte remains before the page end (size=0xfff), a 4-byte prefix+opcode
            # at 0xffb..0xffe is completed by the synthesized zero ModRM byte at 0xfff.
            with self.subTest(opcode=hex(opcode_byte), one_zero_before_page_end=True):
                elf = page_elf([(0, 0x400000, 0xfff, 0xfff, 5)], 0xffb, full_zero_modrm[:4], size=0xfff)
                hits = S.scan_elf_bytes(elf)['hits']
                self.assertEqual(
                    [(h['file_offset'], h['virtual_address'], h['encoding_hex']) for h in hits],
                    [(0xffb, 0x400ffb, full_zero_modrm.hex())],
                )

    def test_overlapping_rounded_executable_pages_with_different_deltas_are_refused(self):
        # Unrounded VAs [0x400000, 0x400800) and [0x400800, 0x401000) do not overlap,
        # but both map virtual page [0x400000, 0x401000) with different file deltas.
        elf = page_elf(
            [(0x1000, 0x400000, 0x800, 0x800, 5), (0x2800, 0x400800, 0x800, 0x800, 5)],
            0x1000, b'\xc3', size=0x3000,
        )
        with self.assertRaisesRegex(S.ScanError, 'overlapping executable PT_LOAD'):
            S.scan_elf_bytes(elf)

        # Same virtual page with identical file delta coalesces cleanly.
        same_delta = page_elf(
            [(0x1000, 0x400000, 0x800, 0x800, 5), (0x1800, 0x400800, 0x800, 0x800, 5)],
            0x1ff0, bytes.fromhex('c4e2f1b4c3'), size=0x3000,
        )
        result = S.scan_elf_bytes(same_delta)
        self.assertEqual(result['bytes'], 0x1000)
        self.assertEqual([h['file_offset'] for h in result['hits']], [0x1ff0])

    def test_executable_pt_load_with_zero_filesz_and_unaligned_offset_is_refused(self):
        unaligned = page_elf([(0x123, 0x400123, 0, 0x1000, 5)], 0, b'', size=0x2000)
        with self.assertRaisesRegex(S.ScanError, 'p_filesz == 0.*p_offset'):
            S.scan_elf_bytes(unaligned)

        aligned = page_elf([(0x1000, 0x401000, 0, 0x1000, 5)], 0, b'', size=0x2000)
        self.assertEqual(S.scan_elf_bytes(aligned)['hits'], [])

        rw_unaligned = page_elf([(0x123, 0x400123, 0, 0x1000, 6)], 0, b'', size=0x2000)
        self.assertEqual(S.scan_elf_bytes(rw_unaligned)['hits'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
