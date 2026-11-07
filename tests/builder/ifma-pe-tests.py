#!/usr/bin/env python3
"""Public synthetic PE executable coverage; no firmware or image inputs."""

import gzip
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = load("ifma_pe_scanner", ROOT / "builder/ifma/scan-ifma.py")
F = load("ifma_pe_fixtures", Path(__file__).with_name("ifma-scan-tests.py"))
LUQ = b"\xc4\xe2\xf1\xb4\xc0"
HUQ = b"\xc4\xe2\xf1\xb5\xc0"
EXEC = 0x20000000
PE, OPT = 0x40, 0x40 + 24


def pe_image(sections=None, *, directories=6, headers=0x400, entry=0x1000,
             image_base=0, align=32, subsystem=10):
    """Sections are (raw_offset, RVA, raw_bytes, virtual_size, flags)."""
    if sections is None:
        sections = [(headers, 0x1000, b"\x90" * 32, 32, EXEC)]
    optional = 112 + directories * 8
    table = OPT + optional
    size = max([headers, table + 40 * len(sections)]
               + [offset + len(raw) for offset, _, raw, _, _ in sections])
    image_size = max([headers]
                     + [rva + max(len(raw), virtual) for _, rva, raw, virtual, _ in sections])
    data = bytearray(size)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3c, PE)
    data[PE:PE + 4] = b"PE\0\0"
    # Linux's NumberOfSymbols=1 / PointerToSymbolTable=0 is intentional.
    struct.pack_into("<HHIIIHH", data, PE + 4,
                     0x8664, len(sections), 0, 0, 1, optional, 0x202)
    struct.pack_into("<H", data, OPT, 0x20b)
    struct.pack_into("<I", data, OPT + 16, entry)
    struct.pack_into("<Q", data, OPT + 24, image_base)
    struct.pack_into("<II", data, OPT + 32, align, align)
    struct.pack_into("<II", data, OPT + 56, image_size, headers)
    struct.pack_into("<H", data, OPT + 68, subsystem)
    struct.pack_into("<I", data, OPT + 108, directories)
    for index, (offset, rva, raw, virtual, flags) in enumerate(sections):
        # Unnamed executable sections must be scanned too.
        struct.pack_into("<8sIIIIIIHHI", data, table + index * 40,
                         b"", virtual, rva, len(raw), offset, 0, 0, 0, 0, flags)
        data[offset:offset + len(raw)] = raw
    return bytes(data), table


def hybrid(inner, outer=b"\x90" * 32):
    payload = gzip.compress(inner, mtime=0)
    text = outer.ljust(0x400, b"\x90") + payload
    data, _ = pe_image([(0x400, 0x1000, text, len(text) + 0x100, EXEC)])
    data = bytearray(data)
    data[0x1f1] = 3
    data[0x202:0x206] = b"HdrS"
    struct.pack_into("<II", data, 0x248, 0, len(payload))
    return bytes(data)


class PeLayoutTests(unittest.TestCase):
    category = "synthetic"

    def scan(self, data):
        return S.scan_elf_bytes(data, "public-pe")

    def test_exec_hit_has_file_and_virtual_identity(self):
        data, _ = pe_image([(0x400, 0x1000, b"\x90" + LUQ, 6, EXEC)])
        result = self.scan(data)
        self.assertFalse(result["elf"])
        self.assertTrue(result["pe"])
        self.assertTrue(result["x86"])
        self.assertEqual([(h["file_offset"], h["virtual_address"]) for h in result["hits"]],
                         [(0x401, 0x1001)])
        self.assertEqual(result["raw_candidates"], 1)
        self.assertEqual(result["annotated_candidates"], [])

    def test_execute_flag_not_section_name_or_code_bit(self):
        data, _ = pe_image([(0x400, 0x1000, b"\x90", 1, EXEC),
                            (0x420, 0x2000, LUQ, 5, 0x20),
                            (0x440, 0x3000, HUQ, 5, EXEC)])
        result = self.scan(data)
        self.assertEqual([h["file_offset"] for h in result["hits"]], [0x440])
        self.assertEqual(result["bytes"], 6)

    def test_full_raw_padding_is_scanned(self):
        data, _ = pe_image([(0x400, 0x1000, b"\x90" * 12 + LUQ, 1, EXEC)])
        self.assertEqual(self.scan(data)["hits"][0]["file_offset"], 0x40c)

    def test_no_elf_page_rounding(self):
        data, _ = pe_image([(0x400, 0x1000, b"\x90", 1, EXEC),
                            (0x401, 0x2000, LUQ, 5, 0)])
        self.assertEqual(self.scan(data)["hits"], [])
        self.assertEqual(self.scan(data)["bytes"], 1)

    def test_chunk_boundary(self):
        data, _ = pe_image([(0x400, 0x1000, b"\x90" * 6 + LUQ, 11, EXEC)])
        with mock.patch.object(S, "SCAN_CHUNK", 8):
            self.assertEqual(self.scan(data)["hits"][0]["file_offset"], 0x406)

    def test_virtual_boundary_with_noncontiguous_raw_offsets(self):
        data, _ = pe_image([(0x400, 0x1000, LUQ[:2], 2, EXEC),
                            (0x500, 0x1002, LUQ[2:4], 2, EXEC),
                            (0x600, 0x1004, LUQ[4:], 1, EXEC)])
        hit = self.scan(data)["hits"][0]
        self.assertEqual((hit["file_offset"], hit["virtual_address"]), (0x400, 0x1000))
        gap = bytearray(data)
        # A virtual gap cannot be spliced together.
        table = OPT + 160
        struct.pack_into("<I", gap, table + 2 * 40 + 12, 0x1010)
        struct.pack_into("<I", gap, OPT + 56, 0x1011)
        self.assertEqual(self.scan(gap)["hits"], [])

    def test_bounded_zero_fill_edge(self):
        data, _ = pe_image([(0x400, 0x1000, LUQ[:4], 0xffff0000, EXEC)])
        result = self.scan(data)
        self.assertEqual(result["bytes"], 4)
        self.assertEqual(result["hits"][0]["encoding_hex"], (LUQ[:4] + b"\0").hex())
        self.assertTrue(result["hits"][0]["crosses_zero_fill"])

    def test_linux_66_32byte_alignment_raw16_and_virtual_slack(self):
        data, _ = pe_image([(0x400, 0x1000, LUQ + b"\x90" * 11, 0x200000, EXEC)], align=32)
        self.assertEqual(len(self.scan(data)["hits"]), 1)

    def test_linux_618_variable_optional_header_and_zero_base(self):
        for directories in (0, 6, 16):
            with self.subTest(directories=directories):
                data, _ = pe_image(directories=directories, headers=4096, image_base=0)
                self.assertTrue(self.scan(data)["pe"])

    def test_all_header_truncations_are_errors_not_data(self):
        data, table = pe_image()
        for end in (2, 0x3f, PE + 3, PE + 23, OPT + 111, table + 39, len(data) - 1):
            with self.subTest(end=end), self.assertRaises(S.ScanError):
                self.scan(data[:end])

    def test_bad_signatures_machine_optional_and_subsystem_are_errors(self):
        data, _ = pe_image()
        changes = ((0x3c, "<I", 0), (0x3c, "<I", 0xfffffff0),
                   (PE, "<I", 0), (PE + 4, "<H", 0x14c),
                   (PE + 6, "<H", 0), (PE + 22, "<H", 0),
                   (OPT, "<H", 0x10b), (OPT + 68, "<H", 3))
        for offset, fmt, value in changes:
            damaged = bytearray(data)
            struct.pack_into(fmt, damaged, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(S.ScanError):
                self.scan(damaged)

    def test_optional_directory_count_and_header_bounds(self):
        data, table = pe_image()
        for offset, value in ((OPT + 108, 7), (OPT + 60, table + 39),
                              (OPT + 60, len(data) + 1), (OPT + 56, 0x1000)):
            damaged = bytearray(data)
            struct.pack_into("<I", damaged, offset, value)
            with self.subTest(offset=offset), self.assertRaises(S.ScanError):
                self.scan(damaged)

    def test_raw_virtual_overlap_and_header_intrusion(self):
        data, table = pe_image([(0x400, 0x1000, b"A" * 16, 16, EXEC),
                                (0x500, 0x2000, b"B" * 16, 16, 0)])
        for offset, value in ((table + 40 + 20, 0x408), (table + 40 + 12, 0x1008),
                              (table + 20, 0x300), (table + 12, 0x300),
                              (table + 16, 0xffffffff), (table + 8, 0xffffffff)):
            damaged = bytearray(data)
            struct.pack_into("<I", damaged, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(S.ScanError):
                self.scan(damaged)

    def test_entry_must_be_unique_filebacked_executable(self):
        data, _ = pe_image([(0x400, 0x1000, b"\x90" * 16, 32, EXEC),
                            (0x500, 0x2000, b"D" * 16, 16, 0)])
        for value in (0, 0x100, 0x1010, 0x2000, 0xffffffff):
            damaged = bytearray(data)
            struct.pack_into("<I", damaged, OPT + 16, value)
            with self.subTest(entry=value), self.assertRaises(S.ScanError):
                self.scan(damaged)


class PeContainerTests(unittest.TestCase):
    category = "synthetic"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "tree"
        self.root.mkdir()

    def scan(self, **kw):
        return S.scan_tree(self.root, provenance=None, **kw)

    def test_suffixless_tree_and_separate_truthful_counters(self):
        data, _ = pe_image()
        (self.root / "suffixless").write_bytes(data)
        observed = []
        report = self.scan(coverage_callback=observed.append)
        self.assertEqual((report["pe_files"], report["x86_pe_files"]), (1, 1))
        self.assertEqual((report["elf_files"], report["x86_elf_files"]), (0, 0))
        self.assertEqual([r["classification"] for r in observed], ["x86_pe"])

    def test_elf_magic_keeps_priority_even_with_efi_suffix(self):
        path = self.root / "misnamed.efi"
        path.write_bytes(F.elf_with_segments(b"\x90"))
        report = self.scan()
        self.assertEqual((report["x86_elf_files"], report["pe_files"]), (1, 0))
        path.write_bytes(F.elf_with_segments(LUQ))
        with self.assertRaisesRegex(S.ScanError, "forbidden VEX IFMA"):
            self.scan()

    def test_malformed_efi_hint_even_without_nested_mode(self):
        (self.root / "BOOTX64.EFI").write_bytes(b"not a PE")
        for nested in (False, True):
            with self.subTest(nested=nested), self.assertRaises(S.ScanError):
                self.scan(nested=nested)

    def test_malformed_decoded_efi_hint_refuses(self):
        for name, payload in (
            ("BOOTX64.EFI.gz", gzip.compress(b"not a PE", mtime=0)),
            ("BOOTX64.EFI.gz.gz", gzip.compress(gzip.compress(b"not a PE", mtime=0), mtime=0)),
        ):
            path = self.root / name
            path.write_bytes(payload)
            with self.subTest(name=name), self.assertRaisesRegex(S.ScanError, "missing its MZ header"):
                self.scan()
            path.unlink()

    def test_decoded_efi_legacy_zero_offset_refuses_but_suffixless_retains_decode(self):
        payload = gzip.compress(F.elf_with_segments(b"\x90"), mtime=0)
        header = bytearray(0x800)
        header[:2] = b"MZ"
        header[0x1f1] = 3
        header[0x202:0x206] = b"HdrS"
        struct.pack_into("<II", header, 0x248, 0, len(payload))
        wrapped = gzip.compress(header + payload, mtime=0)
        path = self.root / "legacy.gz"
        path.write_bytes(wrapped)
        report = self.scan()
        self.assertEqual((report["pe_files"], report["x86_elf_files"]), (0, 1))
        path.rename(self.root / "BOOTX64.EFI.gz")
        with self.assertRaisesRegex(S.ScanError, "invalid PE header offset"):
            self.scan()

    def test_nested_compressed_efi_hint_refuses(self):
        compressed = gzip.compress(b"not a PE", mtime=0)
        path = self.root / "container"
        for kind in ("zip", "cpio"):
            if kind == "zip":
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("BOOTX64.EFI.gz", compressed)
            else:
                path.write_bytes(F.newc_archive("BOOTX64.EFI.gz", compressed))
            with self.subTest(kind=kind), self.assertRaisesRegex(S.ScanError, "missing its MZ header"):
                self.scan()
            path.unlink()

    def test_decoded_elf_magic_keeps_priority_over_efi_hint(self):
        path = self.root / "misnamed.efi.gz"
        path.write_bytes(gzip.compress(F.elf_with_segments(b"\x90"), mtime=0))
        report = self.scan()
        self.assertEqual((report["pe_files"], report["x86_elf_files"]), (0, 1))
        path.write_bytes(gzip.compress(F.elf_with_segments(LUQ), mtime=0))
        with self.assertRaisesRegex(S.ScanError, "forbidden VEX IFMA"):
            self.scan()

    def test_pe_in_zip_cpio_and_compressed_member(self):
        payload, _ = pe_image([(0x400, 0x1000, LUQ, 5, EXEC)])
        encodings = {
            "zip": None,
            "cpio": F.newc_archive("suffixless", payload),
            "gzip": gzip.compress(payload, mtime=0),
        }
        for name, data in encodings.items():
            path = self.root / "container"
            if name == "zip":
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("suffixless", payload)
            else:
                path.write_bytes(data)
            with self.subTest(kind=name), self.assertRaisesRegex(S.ScanError, "forbidden VEX IFMA"):
                self.scan()
            path.unlink()

    def test_malformed_nested_efi_does_not_fall_through_data(self):
        with zipfile.ZipFile(self.root / "outer", "w") as archive:
            archive.writestr("BOOTX64.EFI", b"bad")
        with self.assertRaises(S.ScanError):
            self.scan()

    def test_hybrid_outer_and_decoded_kernel_both_scan(self):
        for outer, inner, expected in ((LUQ, b"\x90", "hybrid+0x400"),
                                       (b"\x90", LUQ, "vmlinux+0x110")):
            data = hybrid(F.elf_with_segments(inner), outer)
            (self.root / "hybrid").write_bytes(data)
            with self.subTest(expected=expected), self.assertRaisesRegex(
                    S.ScanError, expected.replace("+", r"\+")):
                self.scan()

    def test_hybrid_clean_records_both_routes(self):
        (self.root / "hybrid").write_bytes(hybrid(F.elf_with_segments(b"\x90")))
        report = self.scan()
        self.assertEqual(report["x86_pe_files"], 1)
        self.assertEqual(report["x86_elf_files"], 1)
        self.assertIn("bzimage", [r["kind"] for r in report["nested_opened"]])

    def test_hybrid_outer_hit_not_hidden_by_decode_failure(self):
        data = bytearray(hybrid(F.elf_with_segments(b"\x90"), LUQ))
        struct.pack_into("<I", data, 0x24c, 0xffffffff)
        (self.root / "hybrid").write_bytes(data)
        with self.assertRaisesRegex(S.ScanError, "forbidden VEX IFMA"):
            self.scan()

    def test_legacy_non_pe_hdrs_retains_only_existing_decode_coverage(self):
        payload = gzip.compress(F.elf_with_segments(b"\x90"), mtime=0)
        header = bytearray(0x800)
        header[:2] = b"MZ"
        header[0x1f1] = 3
        header[0x202:0x206] = b"HdrS"
        struct.pack_into("<II", header, 0x248, 0, len(payload))
        path = self.root / "legacy"
        path.write_bytes(header + payload)
        report = self.scan()
        self.assertEqual(report["pe_files"], 0)
        self.assertEqual(report["x86_elf_files"], 1)
        for offset, name in ((64, "legacy"), (0, "legacy.efi")):
            damaged = bytearray(header)
            struct.pack_into("<I", damaged, 0x3c, offset)
            path.unlink(missing_ok=True)
            path = self.root / name
            path.write_bytes(damaged + payload)
            with self.subTest(offset=offset, name=name), self.assertRaises(S.ScanError):
                self.scan()

    def test_clean_nested_pe_and_hybrid_keep_separate_coverage(self):
        for kind in ("zip", "cpio"):
            for hybrid_payload in (False, True):
                payload = hybrid(F.elf_with_segments(b"\x90")) if hybrid_payload else pe_image()[0]
                path = self.root / "container"
                if kind == "zip":
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr("suffixless", payload)
                else:
                    path.write_bytes(F.newc_archive("suffixless", payload))
                with self.subTest(kind=kind, hybrid=hybrid_payload):
                    observed = []
                    report = self.scan(coverage_callback=observed.append)
                    self.assertEqual(report["x86_pe_files"], 1)
                    self.assertEqual(report["x86_elf_files"], int(hybrid_payload))
                    self.assertEqual([r["classification"] for r in observed].count("x86_pe"), 1)
                path.unlink()

    def test_pe_is_not_sent_to_elf_disassembly_annotation(self):
        data, _ = pe_image([(0x400, 0x1000, LUQ, 5, EXEC)])
        path = self.root / "app.efi"
        path.write_bytes(data)
        with mock.patch.object(S, "_find_disassembler", side_effect=AssertionError("ELF only")):
            self.assertEqual(S.scan_file(path)["raw_candidates"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
