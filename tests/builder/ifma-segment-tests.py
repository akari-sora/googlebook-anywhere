#!/usr/bin/env python3
"""Regression tests for executable PT_LOAD mapping boundaries."""

import importlib.util
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = ROOT / "builder/ifma/scan-ifma.py"


def load_scanner():
    spec = importlib.util.spec_from_file_location("ifma_segment_scanner", SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCANNER = load_scanner()


def split_load_elf(
    encoding,
    *,
    bytes_before_edge=4,
    second_file_offset=0x1000,
    second_vaddr=0x401000,
    prefix=b"",
    first_memsz=0x1000,
):
    first_size = 0x1000
    second_size = 0x1000
    size = max(first_size, second_file_offset + second_size) + 0x100
    data = bytearray(size)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    encoding_start = first_size - bytes_before_edge
    entry = 0x400000 + encoding_start - len(prefix)
    struct.pack_into(
        "<HHIQQQIHHHHHH", data, 16,
        2, SCANNER.EM_X86_64, 1, entry, 64, 0, 0, 64, 56, 2, 0, 0, 0,
    )
    struct.pack_into(
        "<IIQQQQQQ", data, 64,
        SCANNER.PT_LOAD, SCANNER.PF_R | SCANNER.PF_X,
        0, 0x400000, 0x400000, first_size, first_memsz, 0x1000,
    )
    struct.pack_into(
        "<IIQQQQQQ", data, 120,
        SCANNER.PT_LOAD, SCANNER.PF_R | SCANNER.PF_X,
        second_file_offset, second_vaddr, second_vaddr,
        second_size, second_size, 0x1000,
    )
    instruction = prefix + encoding
    prefix_start = encoding_start - len(prefix)
    split = first_size - prefix_start
    data[prefix_start:first_size] = instruction[:split]
    data[second_file_offset:second_file_offset + len(instruction) - split] = instruction[split:]
    return bytes(data), encoding_start, entry


def tiny_segment_elf(encoding, *, file_stride=1):
    segment_count = len(encoding)
    first_offset = 0x1000
    size = first_offset + file_stride * (segment_count - 1) + 0x100
    data = bytearray(size)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into(
        "<HHIQQQIHHHHHH", data, 16,
        2, SCANNER.EM_X86_64, 1, 0x400000, 64, 0, 0,
        64, 56, segment_count, 0, 0, 0,
    )
    for index, byte in enumerate(encoding):
        offset = first_offset + index * file_stride
        vaddr = 0x400000 + index
        struct.pack_into(
            "<IIQQQQQQ", data, 64 + index * 56,
            SCANNER.PT_LOAD, SCANNER.PF_R | SCANNER.PF_X,
            offset, vaddr, vaddr, 1, 1, 1,
        )
        data[offset] = byte
    return bytes(data), first_offset


class ExecutableSegmentBoundaryTests(unittest.TestCase):
    LUQ = bytes.fromhex("c4e2f1b4c3")
    HUQ = bytes.fromhex("c4e2f1b5c3")

    def test_contiguous_virtual_and_file_segments_find_every_split(self):
        for encoding, mnemonic in ((self.LUQ, "vpmadd52luq"), (self.HUQ, "vpmadd52huq")):
            for bytes_before_edge in range(1, 5):
                with self.subTest(mnemonic=mnemonic, bytes_before_edge=bytes_before_edge):
                    elf, start, entry = split_load_elf(
                        encoding, bytes_before_edge=bytes_before_edge,
                    )
                    self.assertEqual(entry, 0x400000 + start)
                    result = SCANNER.scan_elf_bytes(elf, "split")
                    self.assertEqual(result["segments"], 2)
                    self.assertEqual(result["bytes"], 0x2000)
                    self.assertEqual(
                        [(hit["file_offset"], hit["virtual_address"], hit["instruction"])
                         for hit in result["hits"]],
                        [(start, 0x400000 + start, mnemonic)],
                    )

    def test_legacy_prefix_and_entry_before_boundary_still_find_raw_encoding(self):
        elf, start, entry = split_load_elf(
            self.LUQ, bytes_before_edge=4, prefix=b"\x67",
        )
        self.assertEqual(entry, 0x400000 + start - 1)
        result = SCANNER.scan_elf_bytes(elf, "prefixed-split")
        self.assertEqual([hit["file_offset"] for hit in result["hits"]], [start])

    def test_unpinned_boundary_candidate_refuses_tree(self):
        elf, start, _ = split_load_elf(self.LUQ)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tree"
            root.mkdir()
            (root / "tool").write_bytes(elf)
            with self.assertRaisesRegex(
                SCANNER.ScanError,
                rf"forbidden VEX IFMA .*tool\+{start:#x}",
            ):
                SCANNER.scan_tree(root)

    def test_virtually_contiguous_noncontiguous_file_mappings_are_scanned(self):
        elf, start, _ = split_load_elf(self.HUQ, second_file_offset=0x2000)
        result = SCANNER.scan_elf_bytes(elf, "mapped-split")
        self.assertEqual(
            [(hit["file_offset"], hit["virtual_address"], hit["instruction"])
             for hit in result["hits"]],
            [(start, 0x400000 + start, "vpmadd52huq")],
        )

    def test_encoding_split_across_five_tiny_segments_is_found(self):
        elf, start = tiny_segment_elf(self.LUQ, file_stride=1)
        result = SCANNER.scan_elf_bytes(elf, "tiny-segments")
        self.assertEqual(result["segments"], 5)
        self.assertEqual(result["bytes"], 0x104)
        self.assertEqual(
            [(hit["file_offset"], hit["virtual_address"], hit["encoding_hex"])
             for hit in result["hits"]],
            [(start, 0x400000, self.LUQ.hex())],
        )
        # Distinct file deltas sharing a rounded executable VA page are refused.
        disjoint_delta_elf, _ = tiny_segment_elf(self.LUQ, file_stride=0x100)
        with self.assertRaisesRegex(
            SCANNER.ScanError, "overlapping executable PT_LOAD"
        ):
            SCANNER.scan_elf_bytes(disjoint_delta_elf, "tiny-segments-disjoint-delta")

    def test_unmapped_virtual_gap_is_not_joined(self):
        elf, _, _ = split_load_elf(self.LUQ, second_vaddr=0x402000)
        self.assertEqual(SCANNER.scan_elf_bytes(elf, "gap")["hits"], [])

    def test_zero_fill_tail_completes_a_raw_ifma_candidate(self):
        elf, _, _ = split_load_elf(
            self.LUQ,
            second_file_offset=0x1000,
            second_vaddr=0x402000,
            first_memsz=0x2000,
        )
        result = SCANNER.scan_elf_bytes(elf, "zero-fill")
        self.assertEqual(
            [(hit["file_offset"], hit["virtual_address"], hit["encoding_hex"])
             for hit in result["hits"]],
            [(0xffc, 0x400ffc, "c4e2f1b400")],
        )

    def test_overlapping_executable_virtual_mappings_are_refused(self):
        elf, _, _ = split_load_elf(self.LUQ, second_vaddr=0x400000)
        with self.assertRaisesRegex(SCANNER.ScanError, "overlapping executable PT_LOAD"):
            SCANNER.scan_elf_bytes(elf, "overlap")

    def test_nested_nonadjacent_program_header_overlap_is_refused(self):
        data = bytearray(0x2400)
        data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
        struct.pack_into(
            "<HHIQQQIHHHHHH", data, 16,
            2, SCANNER.EM_X86_64, 1, 0x401000, 64, 0, 0,
            64, 56, 3, 0, 0, 0,
        )
        loads = (
            (SCANNER.PF_R, 0x2000, 0x400000, 1, 0x3000),
            (SCANNER.PF_R, 0x2100, 0x400800, 1, 0x100),
            (SCANNER.PF_R | SCANNER.PF_X, 0x2200, 0x401000, 1, 0x100),
        )
        for index, (flags, offset, vaddr, filesz, memsz) in enumerate(loads):
            struct.pack_into(
                "<IIQQQQQQ", data, 64 + index * 56,
                SCANNER.PT_LOAD, flags, offset, vaddr, vaddr,
                filesz, memsz, 1,
            )
        with self.assertRaisesRegex(SCANNER.ScanError, "overlapping executable PT_LOAD"):
            SCANNER.scan_elf_bytes(bytes(data), "nested-overlap")

    def test_x86_machine_with_malformed_ident_is_refused(self):
        elf, _, _ = split_load_elf(self.LUQ)
        for ident_index, invalid_value in ((4, 0), (5, 2), (6, 0)):
            damaged = bytearray(elf)
            damaged[ident_index] = invalid_value
            with self.subTest(ident_index=ident_index):
                with self.assertRaisesRegex(SCANNER.ScanError, "malformed x86 ELF ident"):
                    SCANNER._elf_segments(io.BytesIO(damaged), len(damaged), "ident")


if __name__ == "__main__":
    unittest.main()
