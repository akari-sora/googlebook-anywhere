#!/usr/bin/env python3
"""Focused F1/F9 tests for hardlink refusal and the post-patch IFMA scanner."""

import hashlib
import gzip
import importlib.util
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
IFMA_DIR = ROOT / "builder/ifma"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PATCHER = load("patch_ifma_scan_tests", IFMA_DIR / "patch-ifma.py")
SCANNER = load("ifma_scanner_tests", IFMA_DIR / "scan-ifma.py")


def lz4_fixture_frame(legacy=False):
    payload = b"fixture"
    if legacy:
        return b"\x02\x21\x4c\x18" + struct.pack("<I", len(payload)) + payload
    return (b"\x04\x22\x4d\x18\x60\x40\0"
            + struct.pack("<I", 0x80000000 | len(payload)) + payload + b"\0" * 4)


def elf_with_segments(executable=b"", non_executable=b""):
    size = 0x1500
    data = bytearray(size)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into(
        "<HHIQQQIHHHHHH", data, 16,
        2, 62, 1, 0, 64, 0, 0, 64, 56, 2, 0, 0, 0,
    )
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0x100, 0x400100, 0, 0x100, 0x100, 0x1000)
    struct.pack_into("<IIQQQQQQ", data, 120, 1, 4, 0x1300, 0x401300, 0, 0x100, 0x100, 0x1000)
    data[0x110:0x110 + len(executable)] = executable
    data[0x1310:0x1310 + len(non_executable)] = non_executable
    return bytes(data)


def large_executable_elf(executable_size, hit_offset):
    data = bytearray(0x100 + executable_size)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into(
        "<HHIQQQIHHHHHH", data, 16,
        2, 62, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0,
    )
    struct.pack_into(
        "<IIQQQQQQ", data, 64,
        1, 5, 0x100, 0x400100, 0, executable_size, executable_size, 0x1000,
    )
    data[0x100 + hit_offset:0x100 + hit_offset + 5] = EncodingTests.LUQ
    return bytes(data)


def semantic_gnu_hash_elf(*, mutation=None, code_hit=False):
    data = bytearray(0x900)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    section_count = 5 if mutation == "duplicate" else 4
    struct.pack_into(
        "<HHIQQQIHHHHHH", data, 16,
        2, 62, 1, 0, 64, 0x700, 0, 64, 56, 2, 64, section_count, 0,
    )
    struct.pack_into(
        "<IIQQQQQQ", data, 64,
        1, 5, 0, 0x400000, 0, 0x600, 0x600, 0x1000,
    )
    struct.pack_into(
        "<IIQQQQQQ", data, 120,
        SCANNER.PT_DYNAMIC, SCANNER.PF_R, 0x180, 0x400180, 0, 0x60, 0x60, 8,
    )

    dynsym_offset = 0x280
    strtab_offset = 0x300
    gnu_offset = 0x380
    dynsym_address = 0x400000 + dynsym_offset
    strtab_address = 0x400000 + strtab_offset
    gnu_address = 0x400000 + gnu_offset
    strings = b"\0Gfq1oa\0end\0"
    data[strtab_offset:strtab_offset + len(strings)] = strings
    struct.pack_into("<I", data, dynsym_offset + 24, 1)
    struct.pack_into("<I", data, dynsym_offset + 48, 8)

    hashes = [SCANNER._gnu_hash(b"Gfq1oa"), SCANNER._gnu_hash(b"end")]
    assert hashes[0] == 0xB4F1E2C4
    bloom_shift = 5
    bloom = 0
    for value in hashes:
        bloom |= (1 << (value % 64)) | (1 << ((value >> bloom_shift) % 64))
    gnu_table = struct.pack(
        "<IIIIQIII", 1, 1, 1, bloom_shift, bloom,
        1, hashes[0] & ~1, (hashes[1] & ~1) | 1,
    )
    data[gnu_offset:gnu_offset + len(gnu_table)] = gnu_table

    dynamic = [
        (SCANNER.DT_GNU_HASH, gnu_address),
        (SCANNER.DT_SYMTAB, dynsym_address),
        (SCANNER.DT_STRTAB, strtab_address),
        (SCANNER.DT_STRSZ, len(strings)),
        (SCANNER.DT_SYMENT, 24),
        (SCANNER.DT_NULL, 0),
    ]
    if mutation == "wrong-dynamic-pointer":
        dynamic[0] = (SCANNER.DT_GNU_HASH, gnu_address + 8)
    elif mutation == "missing-dt-null":
        dynamic[-1] = (1, 0)
    elif mutation == "mismapped-section":
        gnu_address += 1
        dynamic[0] = (SCANNER.DT_GNU_HASH, gnu_address)
    for index, item in enumerate(dynamic):
        struct.pack_into("<QQ", data, 0x180 + index * 16, *item)

    def section(index, section_type, flags, address, offset, size, link, align, entsize):
        struct.pack_into(
            "<IIQQQQIIQQ", data, 0x700 + index * 64,
            0, section_type, flags, address, offset, size, link, 0, align, entsize,
        )

    dynsym_flags = SCANNER.SHF_ALLOC | (SCANNER.SHF_EXECINSTR if mutation == "dynsym-exec" else 0)
    strtab_flags = SCANNER.SHF_ALLOC | (1 if mutation == "strtab-write" else 0)
    section(1, SCANNER.SHT_DYNSYM, dynsym_flags, dynsym_address, dynsym_offset, 72, 2, 8, 24)
    section(2, SCANNER.SHT_STRTAB, strtab_flags, strtab_address, strtab_offset, len(strings), 0, 1, 0)
    gnu_flags = SCANNER.SHF_ALLOC | (SCANNER.SHF_EXECINSTR if mutation == "executable-flag" else 0)
    section(3, SCANNER.SHT_GNU_HASH, gnu_flags, gnu_address, gnu_offset, len(gnu_table), 1, 8, 0)
    if mutation == "duplicate":
        section(4, SCANNER.SHT_GNU_HASH, SCANNER.SHF_ALLOC, gnu_address, gnu_offset, len(gnu_table), 1, 8, 0)
    elif mutation == "chain-mismatch":
        data[gnu_offset + len(gnu_table) - 1] ^= 0x40
    if code_hit:
        data[0x500:0x505] = EncodingTests.HUQ
    return bytes(data), gnu_offset + 28


def relocatable_elf(executable=b"", non_executable=b""):
    data = bytearray(0x500)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into(
        "<HHIQQQIHHHHHH", data, 16,
        1, 62, 1, 0, 0, 0x100, 0, 64, 0, 0, 64, 3, 0,
    )
    struct.pack_into("<IIQQQQIIQQ", data, 0x140, 0, 1, 0x4, 0, 0x200, 0x100, 0, 0, 1, 0)
    struct.pack_into("<IIQQQQIIQQ", data, 0x180, 0, 1, 0x2, 0, 0x300, 0x100, 0, 0, 1, 0)
    data[0x210:0x210 + len(executable)] = executable
    data[0x310:0x310 + len(non_executable)] = non_executable
    return bytes(data)


def newc_archive(name, payload):
    def member(member_name, member_payload, mode):
        name_bytes = member_name.encode("utf-8") + b"\0"
        fields = (
            1, mode, 0, 0, 1, 0, len(member_payload),
            0, 0, 0, 0, len(name_bytes), 0,
        )
        header = b"070701" + b"".join(f"{value:08x}".encode("ascii") for value in fields)
        name_pad = b"\0" * ((-(len(header) + len(name_bytes))) % 4)
        data_pad = b"\0" * ((-len(member_payload)) % 4)
        return header + name_bytes + name_pad + member_payload + data_pad

    return member(name, payload, 0o100644) + member("TRAILER!!!", b"", 0)


def with_bootconfig(payload, bootconfig=b"androidboot.test=1\n"):
    footer = struct.pack(
        "<II", len(bootconfig), sum(bootconfig) & 0xFFFFFFFF,
    ) + b"#BOOTCONFIG\n"
    return payload + bootconfig + footer


RESIDUAL_OFFSET = 0x17B96D
RESIDUAL_BINARY_SHA256 = "32efab45a5653acc33c440fc0761744e70a95caf45b807209764185da747bb15"
RESIDUAL_IMAGE_SHA256 = "df0733894a15bc0cb60480c38958f4d272c196d56b4230f253f2d921faa1e66f"


def erofs_marker(marker):
    image = bytearray(2048)
    image[1024:1028] = b"\xe2\xe1\xf5\xe0"
    image[-len(marker):] = marker
    return bytes(image)


def residual_policy(binary, microdroid_image, **changes):
    row = {
        "partition": "system_a",
        "container_chain": [
            {"kind": "tree_file", "path": "system/apex/com.google.android.virt.apex"},
            {"kind": "zip_member", "path": "apex_payload.img"},
            {
                "kind": "erofs_file", "path": "etc/fs/microdroid.img",
                "sha256": hashlib.sha256(microdroid_image).hexdigest(),
            },
            {
                "kind": "erofs_file", "path": "system/bin/microdroid_manager",
                "sha256": hashlib.sha256(binary).hexdigest(),
            },
        ],
        "file_offset": f"{RESIDUAL_OFFSET:#x}",
        "instruction": "vpmadd52luq",
        "reason": "protected microdroid guest only",
    }
    row.update(changes)
    return row


def residual_tree(base, binary):
    root = base / "tree"
    apex = root / "system/apex/com.google.android.virt.apex"
    apex.parent.mkdir(parents=True)
    outer = erofs_marker(b"outer-erofs")
    inner = erofs_marker(b"inner-erofs")
    with zipfile.ZipFile(apex, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("apex_payload.img", outer)

    tools = base / "otatools/bin"
    tools.mkdir(parents=True)
    (tools / "outer-erofs").write_bytes(outer)
    (tools / "inner-erofs").write_bytes(inner)
    (tools / "microdroid-manager").write_bytes(binary)
    fsck = tools / "fsck.erofs"
    fsck.write_text(
        "#!/bin/sh\n"
        "out=${1#--extract=}\n"
        "for arg do input=$arg; done\n"
        "if cmp -s \"$input\" \"$(dirname \"$0\")/outer-erofs\"; then\n"
        "  mkdir -p \"$out/etc/fs\"\n"
        "  cp \"$(dirname \"$0\")/inner-erofs\" \"$out/etc/fs/microdroid.img\"\n"
        "elif cmp -s \"$input\" \"$(dirname \"$0\")/inner-erofs\"; then\n"
        "  mkdir -p \"$out/system/bin\"\n"
        "  cp \"$(dirname \"$0\")/microdroid-manager\" \"$out/system/bin/microdroid_manager\"\n"
        "else\n"
        "  exit 9\n"
        "fi\n",
        encoding="utf-8",
    )
    fsck.chmod(0o755)
    return root, tools.parent, inner


def patch_fixture():
    data = bytearray(0x500)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into(
        "<HHIQQQIHHHHHH", data, 16,
        2, 62, 1, 0, 64, 0x100, 0, 64, 56, 1, 64, 2, 1,
    )
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0x400000, 0, len(data), len(data), 0x1000)
    names = b"\0.shstrtab\0"
    data[0x300:0x300 + len(names)] = names
    struct.pack_into("<IIQQQQIIQQ", data, 0x140, 1, 3, 0, 0, 0x300, len(names), 0, 0, 1, 0)
    context = bytes(range(PATCHER.CONTEXT_LENGTH))
    context = (
        context[:PATCHER.CONTEXT_PATCH_DELTA]
        + PATCHER.ORIGINAL
        + context[PATCHER.CONTEXT_PATCH_DELTA + len(PATCHER.ORIGINAL):]
    )
    data[0x200:0x200 + len(context)] = context
    table = bytes((index * 17) & 0xFF for index in range(PATCHER.TABLE_LENGTH))
    data[0x380:0x400] = table
    original = bytes(data)
    patched = bytearray(original)
    patched[0x21A:0x21A + len(PATCHER.REPLACEMENT)] = PATCHER.REPLACEMENT
    patched = bytes(patched)
    row = {
        "sha256": hashlib.sha256(original).hexdigest(),
        "patched_sha256": hashlib.sha256(patched).hexdigest(),
        "site_va": 0x400221,
        "file_offset": 0x221,
        "function_va": 0x400200,
        "function_size": PATCHER.CONTEXT_LENGTH,
        "size": len(original),
        "table_va": 0x400380,
        "table_file_offset": 0x380,
        "table_hex": table.hex(),
        "context_va": 0x400200,
        "context_offset": 0x200,
        "context_hex": context.hex(),
    }
    return original, patched, row


class EncodingTests(unittest.TestCase):
    LUQ = bytes.fromhex("c4e2f1b4c3")
    HUQ = bytes.fromhex("c46285b5ff")

    def test_exact_vex_luq_and_huq_encodings_are_found(self):
        result = SCANNER.scan_elf_bytes(elf_with_segments(self.LUQ + b"\x90" + self.HUQ), "fixture")
        self.assertEqual([hit["instruction"] for hit in result["hits"]], ["vpmadd52luq", "vpmadd52huq"])
        self.assertEqual([hit["file_offset"] for hit in result["hits"]], [0x110, 0x116])

    def test_near_misses_and_truncated_encoding_are_not_hits(self):
        near = b"".join((
            bytes.fromhex("c4e1f1b4c3"),  # wrong opcode map
            bytes.fromhex("c4e271b4c3"),  # W=0
            bytes.fromhex("c4e2f0b4c3"),  # pp is not 66
            bytes.fromhex("c4e2f1b6c3"),  # wrong opcode
        ))
        self.assertEqual(SCANNER.scan_elf_bytes(elf_with_segments(near), "near")["hits"], [])

    def test_encoding_truncated_at_executable_page_boundary_is_not_a_hit(self):
        fixture = bytearray(elf_with_segments())
        struct.pack_into("<QQ", fixture, 96, 0xF00, 0xF00)
        fixture[0xFFC:0x1000] = bytes.fromhex("c4e2f1b4")
        self.assertEqual(SCANNER.scan_elf_bytes(bytes(fixture), "boundary")["hits"], [])

    def test_hit_split_across_streaming_chunk_boundary_is_found(self):
        fixture = large_executable_elf(SCANNER.SCAN_CHUNK + 32, SCANNER.SCAN_CHUNK - 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large-elf"
            path.write_bytes(fixture)
            result = SCANNER.scan_file(path)
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(result["hits"][0]["file_offset"], 0x100 + SCANNER.SCAN_CHUNK - 2)

    def test_relocatable_kernel_module_scans_only_executable_sections(self):
        hit = SCANNER.scan_elf_bytes(relocatable_elf(self.HUQ, self.LUQ), "module.ko")
        self.assertEqual([row["instruction"] for row in hit["hits"]], ["vpmadd52huq"])
        self.assertEqual(hit["hits"][0]["file_offset"], 0x210)

    def test_loose_encoding_in_non_executable_data_is_ignored(self):
        result = SCANNER.scan_elf_bytes(
            elf_with_segments(b"ordinary executable bytes", self.LUQ), "data-segment",
        )
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["segments"], 1)
        self.assertEqual(result["bytes"], 0x1000)

    def _scan_semantic_gnu_hash(self, mutation=None, code_hit=False):
        fixture, metadata_offset = semantic_gnu_hash_elf(
            mutation=mutation, code_hit=code_hit,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gnu-hash.elf"
            path.write_bytes(fixture)
            with mock.patch.object(SCANNER, "_find_disassembler", return_value=()):
                result = SCANNER.scan_file(path)
        return result, metadata_offset

    def test_semantically_valid_gnu_hash_candidate_is_only_annotated(self):
        result, metadata_offset = self._scan_semantic_gnu_hash()
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(result["raw_candidates"], 1)
        self.assertEqual(result["annotated_candidates"], [{
            "source": mock.ANY,
            "file_offset": metadata_offset,
            "virtual_address": 0x400000 + metadata_offset,
            "instruction": "vpmadd52luq",
            "metadata_file_offset_start": 0x380,
            "metadata_file_offset_end": 0x380 + 36,
            "reason": "validated-gnu-hash",
        }])

    def test_gnu_hash_metadata_ambiguity_and_spoofing_remain_fatal(self):
        mutations = (
            "executable-flag",
            "wrong-dynamic-pointer",
            "missing-dt-null",
            "mismapped-section",
            "duplicate",
            "chain-mismatch",
            "dynsym-exec",
            "strtab-write",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                result, metadata_offset = self._scan_semantic_gnu_hash(mutation=mutation)
                self.assertEqual(len(result["hits"]), 1)
                self.assertEqual(result["hits"][0]["file_offset"], metadata_offset)
                self.assertEqual(result["annotated_candidates"], [])

    def test_genuine_huq_in_executable_code_remains_fatal_with_valid_gnu_hash(self):
        result, metadata_offset = self._scan_semantic_gnu_hash(code_hit=True)
        self.assertEqual(len(result["annotated_candidates"]), 1)
        self.assertEqual(result["annotated_candidates"][0]["file_offset"], metadata_offset)
        self.assertEqual(len(result["hits"]), 2)
        self.assertEqual(result["hits"][1]["file_offset"], 0x500)
        self.assertEqual(result["hits"][1]["instruction"], "vpmadd52huq")

    def test_malformed_supported_x86_elf_is_refused(self):
        damaged = bytearray(elf_with_segments())
        struct.pack_into("<Q", damaged, 32, len(damaged) - 1)
        with self.assertRaisesRegex(SCANNER.ScanError, "program-header table"):
            SCANNER.scan_elf_bytes(bytes(damaged), "damaged")

    def _scan_file_with_disassembly(self, executable, output):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.elf"
            path.write_bytes(elf_with_segments(executable))
            with (
                mock.patch.object(SCANNER, "_find_disassembler", return_value="/tool/objdump"),
                mock.patch.object(SCANNER, "_objdump_output", return_value=output),
            ):
                return SCANNER.scan_file(path)

    def test_candidate_inside_decoded_instruction_is_only_annotated(self):
        instructions = bytes.fromhex(
            "62 71 3d 48 fe c4 "
            "62 b1 b5 48 73 d3 20"
        )
        output = (
            "  400110: 62 71 3d 48 fe c4"
            "                               vpaddd %zmm4,%zmm8,%zmm8\n"
            "  400116: 62 b1 b5 48 73 d3 20"
            "                            vpsrlq $0x20,%zmm19,%zmm9\n"
        )
        result = self._scan_file_with_disassembly(instructions, output)
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(result["raw_candidates"], 1)
        self.assertEqual(len(result["annotated_candidates"]), 1)
        self.assertEqual(result["annotated_candidates"][0]["file_offset"], 0x115)
        self.assertEqual(
            result["annotated_candidates"][0]["containing_instruction_start"],
            0x400110,
        )

    def test_candidate_at_verified_instruction_start_is_retained(self):
        output = "  400110: c4 e2 f1 b4 c3    vpmadd52luq %xmm3,%xmm1,%xmm0\n"
        result = self._scan_file_with_disassembly(self.LUQ, output)
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(result["hits"][0]["file_offset"], 0x110)

    def test_unknown_or_uncovered_disassembly_retains_candidate(self):
        for output in (
            "  400110: c4 e2 f1 b4 c3    (bad)\n",
            "  400100: 90                nop\n",
            "  400110: c4 e2 f1 b4 c2    vpmadd52luq %xmm2,%xmm1,%xmm0\n",
        ):
            with self.subTest(output=output):
                result = self._scan_file_with_disassembly(self.LUQ, output)
                self.assertEqual(len(result["hits"]), 1)
                self.assertEqual(result["raw_candidates"], 1)
                self.assertEqual(result["annotated_candidates"], [])

    def test_failed_first_disassembler_falls_back_to_second(self):
        instructions = bytes.fromhex(
            "62 71 3d 48 fe c4 "
            "62 b1 b5 48 73 d3 20"
        )
        output = (
            "  400110: 62 71 3d 48 fe c4"
            "                               vpaddd %zmm4,%zmm8,%zmm8\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.elf"
            path.write_bytes(elf_with_segments(instructions))
            with (
                mock.patch.object(
                    SCANNER, "_find_disassembler",
                    return_value=("/tool/llvm-objdump", "/tool/objdump"),
                ),
                mock.patch.object(
                    SCANNER, "_objdump_output", side_effect=(None, output),
                ) as disassemble,
            ):
                result = SCANNER.scan_file(path)
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(len(result["annotated_candidates"]), 1)
        self.assertEqual(result["annotated_candidates"][0]["disassembler"], "objdump")
        self.assertEqual(disassemble.call_count, 2)

    def test_clean_file_does_not_probe_for_disassembler(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clean.elf"
            path.write_bytes(elf_with_segments())
            with mock.patch.object(SCANNER, "_find_disassembler") as find:
                result = SCANNER.scan_file(path)
            self.assertEqual(result["hits"], [])
            find.assert_not_called()


class TreeAndNestedTests(unittest.TestCase):
    category = "synthetic"

    def test_tree_hit_aborts_and_clean_tree_reports_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()
            clean = root / "clean"
            clean.write_bytes(elf_with_segments())
            (root / "blob").write_bytes(EncodingTests.LUQ)
            report = SCANNER.scan_tree(root, nested=False)
            self.assertEqual(report["regular_files"], 2)
            self.assertEqual(report["x86_elf_files"], 1)
            self.assertEqual(report["hits"], [])

            clean.write_bytes(elf_with_segments(EncodingTests.LUQ))
            with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                SCANNER.scan_tree(root, nested=False)

    def test_zip_member_elf_is_scanned_without_scanning_raw_zip_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()
            archive = root / "nested.apex"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr("bin/unexpected", elf_with_segments(EncodingTests.HUQ))
                output.writestr("assets/loose-data", EncodingTests.LUQ)
            with self.assertRaisesRegex(SCANNER.ScanError, "nested.apex!/bin/unexpected"):
                SCANNER.scan_tree(root)

    def test_suffixless_zip_member_newc_is_detected_and_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()
            archive = root / "nested.zip"
            payload = newc_archive("bin/unexpected", elf_with_segments(EncodingTests.LUQ))
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr("payload", payload)
            with self.assertRaisesRegex(
                SCANNER.ScanError,
                "nested.zip!/payload!/bin/unexpected",
            ):
                SCANNER.scan_tree(root)

    def test_suffixless_zip_member_erofs_is_detected_and_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "tree"
            root.mkdir()
            image = bytearray(2048)
            image[1024:1028] = b"\xe2\xe1\xf5\xe0"
            with zipfile.ZipFile(root / "nested.apex", "w", compression=zipfile.ZIP_STORED) as output:
                output.writestr("payload", image)

            otatools = base / "otatools"
            bindir = otatools / "bin"
            bindir.mkdir(parents=True)
            (bindir / "payload").write_bytes(elf_with_segments(EncodingTests.HUQ))
            fsck = bindir / "fsck.erofs"
            fsck.write_text(
                "#!/bin/sh\n"
                "out=${1#--extract=}\n"
                "mkdir -p \"$out/bin\"\n"
                "cp \"$(dirname \"$0\")/payload\" \"$out/bin/unexpected\"\n",
                encoding="utf-8",
            )
            fsck.chmod(0o755)
            with self.assertRaisesRegex(
                SCANNER.ScanError,
                "nested.apex!/payload!/<erofs>/bin/unexpected",
            ):
                SCANNER.scan_tree(root, otatools=otatools)

    def test_erofs_is_opened_with_available_fsck_and_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "tree"
            root.mkdir()
            image = bytearray(2048)
            image[1024:1028] = b"\xe2\xe1\xf5\xe0"
            (root / "nested.img").write_bytes(image)
            otatools = base / "otatools"
            bindir = otatools / "bin"
            bindir.mkdir(parents=True)
            (bindir / "payload").write_bytes(elf_with_segments(EncodingTests.LUQ))
            fsck = bindir / "fsck.erofs"
            fsck.write_text(
                "#!/bin/sh\n"
                "out=${1#--extract=}\n"
                "mkdir -p \"$out/bin\"\n"
                "cp \"$(dirname \"$0\")/payload\" \"$out/bin/unexpected\"\n",
                encoding="utf-8",
            )
            fsck.chmod(0o755)
            with self.assertRaisesRegex(SCANNER.ScanError, "nested.img!/<erofs>/bin/unexpected"):
                SCANNER.scan_tree(root, otatools=otatools)

    def test_gzip_wrapped_newc_member_elf_is_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()
            archive = newc_archive("bin/unexpected", elf_with_segments(EncodingTests.LUQ))
            with gzip.open(root / "ramdisk.cpio.gz", "wb") as output:
                output.write(archive)
            with self.assertRaisesRegex(SCANNER.ScanError, "ramdisk.cpio.gz!/<gzip>!/bin/unexpected"):
                SCANNER.scan_tree(root)

    def test_non_elf_gzip_output_is_recognized_as_newc_at_next_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()
            archive = newc_archive("bin/clean", elf_with_segments())
            with gzip.open(root / "ramdisk.cpio.gz", "wb") as output:
                output.write(archive)
            report = SCANNER.scan_tree(root)
            self.assertEqual(
                [item["kind"] for item in report["nested_opened"]],
                ["gzip", "cpio-newc"],
            )
            self.assertEqual(report["x86_elf_files"], 1)

    def test_lz4_output_recurses_into_newc(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "tree"
            root.mkdir()
            (root / "ramdisk.cpio.lz4").write_bytes(lz4_fixture_frame())
            otatools = base / "otatools"
            bindir = otatools / "bin"
            bindir.mkdir(parents=True)
            (bindir / "payload-lz4").write_bytes(
                newc_archive("bin/unexpected", elf_with_segments(EncodingTests.HUQ))
            )
            lz4 = bindir / "lz4"
            lz4.write_text(
                "#!/bin/sh\ncat \"$(dirname \"$0\")/payload-lz4\"\n",
                encoding="utf-8",
            )
            lz4.chmod(0o755)
            with self.assertRaisesRegex(
                SCANNER.ScanError,
                "ramdisk.cpio.lz4!/<lz4>!/bin/unexpected",
            ):
                SCANNER.scan_tree(root, otatools=otatools)

    def test_lz4_android_bootconfig_is_validated_and_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "tree"
            root.mkdir()
            compressed = lz4_fixture_frame(legacy=True)
            image = root / "microdroid_initrd_debuggable.img"
            image.write_bytes(with_bootconfig(compressed))
            otatools = base / "otatools"
            bindir = otatools / "bin"
            bindir.mkdir(parents=True)
            (bindir / "payload-lz4").write_bytes(
                newc_archive("bin/clean", elf_with_segments())
            )
            lz4 = bindir / "lz4"
            lz4.write_text(
                "#!/bin/sh\n"
                f"[ \"$(wc -c < \"$2\")\" -eq {len(compressed)} ] || exit 9\n"
                "cat \"$(dirname \"$0\")/payload-lz4\"\n",
                encoding="utf-8",
            )
            lz4.chmod(0o755)
            report = SCANNER.scan_tree(root, otatools=otatools)
            self.assertEqual(report["hits"], [])
            self.assertIn(
                {"source": str(image), "kind": "android-bootconfig"},
                report["nested_opened"],
            )
            self.assertIn(
                {"source": str(image), "kind": "lz4"},
                report["nested_opened"],
            )

    def test_lz4_android_bootconfig_bad_size_and_checksum_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "tree"
            root.mkdir()
            image = root / "ramdisk.img"
            compressed = lz4_fixture_frame(legacy=True)
            otatools = base / "otatools"
            bindir = otatools / "bin"
            bindir.mkdir(parents=True)
            marker = base / "lz4-invoked"
            lz4 = bindir / "lz4"
            lz4.write_text(
                f"#!/bin/sh\ntouch {marker}\nexit 0\n",
                encoding="utf-8",
            )
            lz4.chmod(0o755)

            image.write_bytes(
                compressed + struct.pack("<II", 0xFFFFFFFF, 0) + b"#BOOTCONFIG\n"
            )
            with self.assertRaisesRegex(SCANNER.ScanError, "bootconfig size"):
                SCANNER.scan_tree(root, otatools=otatools)
            self.assertFalse(marker.exists())

            damaged = bytearray(with_bootconfig(compressed))
            damaged[-13] ^= 1
            image.write_bytes(damaged)
            with self.assertRaisesRegex(SCANNER.ScanError, "bootconfig checksum"):
                SCANNER.scan_tree(root, otatools=otatools)
            self.assertFalse(marker.exists())

    def test_lz4_output_is_rejected_when_decompressor_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "tree"
            root.mkdir()
            (root / "ramdisk.img").write_bytes(lz4_fixture_frame(legacy=True))
            otatools = base / "otatools"
            bindir = otatools / "bin"
            bindir.mkdir(parents=True)
            (bindir / "payload-lz4").write_bytes(
                newc_archive("bin/clean", elf_with_segments())
            )
            lz4 = bindir / "lz4"
            lz4.write_text(
                "#!/bin/sh\n"
                "cat \"$(dirname \"$0\")/payload-lz4\"\n"
                "exit 1\n",
                encoding="utf-8",
            )
            lz4.chmod(0o755)
            with self.assertRaisesRegex(SCANNER.ScanError, "lz4 decompression failed"):
                SCANNER.scan_tree(root, otatools=otatools)

    def test_ext4_is_opened_read_only_with_debugfs_and_empty_extract_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "tree"
            root.mkdir()
            image = bytearray(2048)
            image[1080:1082] = b"\x53\xef"
            (root / "nested.img").write_bytes(image)
            otatools = base / "otatools"
            bindir = otatools / "bin"
            bindir.mkdir(parents=True)
            (bindir / "payload-ext4").write_bytes(elf_with_segments(EncodingTests.LUQ))
            debugfs = bindir / "debugfs"
            debugfs.write_text(
                "#!/bin/sh\n"
                "out=${2#rdump / }\n"
                "mkdir -p \"$out/bin\"\n"
                "cp \"$(dirname \"$0\")/payload-ext4\" \"$out/bin/unexpected\"\n",
                encoding="utf-8",
            )
            debugfs.chmod(0o755)
            with self.assertRaisesRegex(SCANNER.ScanError, "nested.img!/<ext4>/bin/unexpected"):
                SCANNER.scan_tree(root, otatools=otatools)

            debugfs.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            debugfs.chmod(0o755)
            with self.assertRaisesRegex(SCANNER.ScanError, "no files extracted"):
                SCANNER.scan_tree(root, otatools=otatools)

    def test_corrupt_gzip_and_zip_member_crc_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()

            gz_path = root / "bad.cpio.gz"
            with gzip.open(gz_path, "wb") as output:
                output.write(newc_archive("etc/plain", b"plain",))
            damaged_gzip = bytearray(gz_path.read_bytes()[:-10])
            gz_path.write_bytes(damaged_gzip)
            with self.assertRaisesRegex(SCANNER.ScanError, "invalid gzip stream"):
                SCANNER.scan_tree(root)

            gz_path.unlink()
            zip_path = root / "bad.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as output:
                output.writestr("assets/plain", b"ordinary member bytes")
            damaged_zip = bytearray(zip_path.read_bytes())
            local_name_length, local_extra_length = struct.unpack_from("<HH", damaged_zip, 26)
            data_offset = 30 + local_name_length + local_extra_length
            damaged_zip[data_offset] ^= 1
            zip_path.write_bytes(damaged_zip)
            with self.assertRaisesRegex(SCANNER.ScanError, "invalid ZIP/APEX member data"):
                SCANNER.scan_tree(root)

    def test_known_nested_image_without_tool_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()
            image = bytearray(2048)
            image[1024:1028] = b"\xe2\xe1\xf5\xe0"
            (root / "nested.img").write_bytes(image)
            with mock.patch.object(SCANNER, "_tool", return_value=None):
                with self.assertRaisesRegex(SCANNER.ScanError, "fsck.erofs unavailable"):
                    SCANNER.scan_tree(root)


class ResidualPolicyTests(unittest.TestCase):
    def _binary(self, encoding=EncodingTests.LUQ, second=False):
        hit_offset = RESIDUAL_OFFSET - 0x100
        binary = bytearray(large_executable_elf(hit_offset + 32, hit_offset))
        binary[RESIDUAL_OFFSET:RESIDUAL_OFFSET + len(encoding)] = encoding
        if second:
            binary[RESIDUAL_OFFSET + 8:RESIDUAL_OFFSET + 13] = EncodingTests.LUQ
        return bytes(binary)

    def _scan(self, root, otatools, policy, **kwargs):
        with mock.patch.object(SCANNER, "_find_disassembler", return_value=()):
            return SCANNER.scan_tree(
                root,
                otatools=otatools,
                residuals=[policy],
                provenance={"partition": "system_a", "container_chain": []},
                **kwargs,
            )

    def test_shipped_policy_pins_the_decided_stock_objects_and_site(self):
        manifest = json.loads((IFMA_DIR / "manifest.json").read_text(encoding="utf-8"))
        policies = SCANNER._normalize_residuals(manifest["residuals"])
        self.assertEqual(len(policies), 9)
        policy = policies[0]
        self.assertEqual(policy["partition"], "system_a")
        self.assertEqual(policy["file_offset"], RESIDUAL_OFFSET)
        self.assertEqual(policy["instruction"], "vpmadd52luq")
        self.assertEqual(policy["container_chain"][-2]["sha256"], RESIDUAL_IMAGE_SHA256)
        self.assertEqual(policy["container_chain"][-1]["sha256"], RESIDUAL_BINARY_SHA256)

    def test_exact_nested_residual_is_accepted_with_structured_identity(self):
        binary = self._binary()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, otatools, inner = residual_tree(base, binary)
            policy = residual_policy(binary, inner)
            report = self._scan(root, otatools, policy)
        self.assertEqual(report["hits"], [])
        self.assertEqual(report["accepted_residuals"], [{
            **policy,
            "file_offset": f"{RESIDUAL_OFFSET:#x}",
        }])

    def test_direct_extracted_apex_payload_uses_same_canonical_chain(self):
        binary = self._binary()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            _, otatools, inner = residual_tree(base, binary)
            payload_root = base / "payload-tree"
            image = payload_root / "etc/fs/microdroid.img"
            image.parent.mkdir(parents=True)
            image.write_bytes(inner)
            policy = residual_policy(binary, inner)
            with mock.patch.object(SCANNER, "_find_disassembler", return_value=()):
                report = SCANNER.scan_tree(
                    payload_root,
                    otatools=otatools,
                    residuals=[policy],
                    provenance={
                        "partition": "system_a",
                        "container_chain": policy["container_chain"][:2],
                    },
                    root_kind="erofs_file",
                )
        self.assertEqual(report["accepted_residuals"][0]["container_chain"], policy["container_chain"])

    def test_provenance_cannot_skip_a_pinned_nested_image(self):
        binary = self._binary()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "guest-tree"
            target = root / "system/bin/microdroid_manager"
            target.parent.mkdir(parents=True)
            target.write_bytes(binary)
            inner = erofs_marker(b"inner-erofs")
            policy = residual_policy(binary, inner)
            with self.assertRaisesRegex(SCANNER.ScanError, "sha256-pinned ancestor"):
                SCANNER.scan_tree(
                    root,
                    residuals=[policy],
                    provenance={
                        "partition": "system_a",
                        "container_chain": [
                            {"kind": node["kind"], "path": node["path"]}
                            for node in policy["container_chain"][:3]
                        ],
                    },
                    root_kind="erofs_file",
                )

    def test_policy_requires_both_nested_image_and_binary_hashes(self):
        binary = self._binary()
        inner = erofs_marker(b"inner-erofs")
        for index in (-2, -1):
            with self.subTest(chain_index=index):
                policy = residual_policy(binary, inner)
                del policy["container_chain"][index]["sha256"]
                with self.assertRaisesRegex(SCANNER.ScanError, "must both pin sha256"):
                    SCANNER._normalize_residuals([policy])

    def test_missing_pinned_image_or_binary_is_refused(self):
        binary = self._binary()
        inner = erofs_marker(b"inner-erofs")
        policy = residual_policy(binary, inner)
        base_context = {
            "partition": "system_a",
            "container_chain": policy["container_chain"][:2],
        }
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            empty_payload = base / "empty-payload"
            empty_payload.mkdir()
            with self.assertRaisesRegex(SCANNER.ScanError, "pinned residual missing"):
                SCANNER.scan_tree(
                    empty_payload, residuals=[policy], provenance=base_context,
                    root_kind="erofs_file",
                )

            _, otatools, _ = residual_tree(base / "fixture", binary)
            payload = base / "payload"
            image = payload / "etc/fs/microdroid.img"
            image.parent.mkdir(parents=True)
            image.write_bytes(inner)
            fsck = otatools / "bin/fsck.erofs"
            fsck.write_text(
                "#!/bin/sh\n"
                "out=${1#--extract=}\n"
                "mkdir -p \"$out/system/bin\"\n",
                encoding="utf-8",
            )
            fsck.chmod(0o755)
            with self.assertRaisesRegex(SCANNER.ScanError, "pinned residual missing"):
                SCANNER.scan_tree(
                    payload, otatools=otatools, residuals=[policy],
                    provenance=base_context, root_kind="erofs_file",
                )

    def test_residual_file_hash_mismatch_is_refused(self):
        binary = self._binary()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, otatools, inner = residual_tree(base, binary)
            policy = residual_policy(binary, inner)
            policy["container_chain"][-1]["sha256"] = "0" * 64
            with self.assertRaisesRegex(SCANNER.ScanError, "pinned residual object changed"):
                self._scan(root, otatools, policy)

    def test_residual_image_hash_mismatch_is_refused(self):
        binary = self._binary()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, otatools, inner = residual_tree(base, binary)
            policy = residual_policy(binary, inner)
            policy["container_chain"][-2]["sha256"] = "f" * 64
            with self.assertRaisesRegex(SCANNER.ScanError, "pinned residual object changed"):
                self._scan(root, otatools, policy)

    def test_residual_offset_mismatch_is_refused(self):
        binary = self._binary()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, otatools, inner = residual_tree(base, binary)
            policy = residual_policy(binary, inner, file_offset=f"{RESIDUAL_OFFSET + 1:#x}")
            with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                self._scan(root, otatools, policy)

    def test_second_residual_hit_is_refused(self):
        binary = self._binary(second=True)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, otatools, inner = residual_tree(base, binary)
            policy = residual_policy(binary, inner)
            with self.assertRaisesRegex(SCANNER.ScanError, "found 1 forbidden"):
                self._scan(root, otatools, policy)

    def test_wrong_residual_file_chain_is_refused(self):
        binary = self._binary()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, otatools, inner = residual_tree(base, binary)
            policy = residual_policy(binary, inner)
            policy["container_chain"][-1]["path"] = "system/bin/not_microdroid_manager"
            with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                self._scan(root, otatools, policy)

    def test_huq_at_pinned_site_is_refused(self):
        binary = self._binary(EncodingTests.HUQ)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, otatools, inner = residual_tree(base, binary)
            policy = residual_policy(binary, inner)
            with self.assertRaisesRegex(SCANNER.ScanError, "vpmadd52huq"):
                self._scan(root, otatools, policy)

    def test_changed_no_hit_binary_is_refused_by_hash(self):
        expected_binary = self._binary()
        clean_binary = bytearray(expected_binary)
        clean_binary[RESIDUAL_OFFSET:RESIDUAL_OFFSET + 5] = b"\x90" * 5
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, otatools, inner = residual_tree(base, bytes(clean_binary))
            policy = residual_policy(expected_binary, inner)
            with self.assertRaisesRegex(SCANNER.ScanError, "pinned residual object changed"):
                self._scan(root, otatools, policy)


class HardlinkRefusalTests(unittest.TestCase):
    def test_hardlinked_target_is_refused_during_preflight_before_any_write(self):
        original, _, base_row = patch_fixture()
        shipped = json.loads((IFMA_DIR / "manifest.json").read_text(encoding="utf-8"))["targets"]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            rows = []
            for shipped_row in shipped:
                row = dict(base_row)
                row["path"] = shipped_row["path"]
                for key in ("partition", "image_path", "apex_path", "payload_path", "note"):
                    if key in shipped_row:
                        row[key] = shipped_row[key]
                target = root / row["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original)
                rows.append(row)
            last = root / rows[-1]["path"]
            alias = root / "unlisted-hardlink-alias"
            os.link(last, alias)
            manifest = base / "manifest.json"
            manifest.write_text(json.dumps({"schema": 1, "targets": rows}), encoding="utf-8")
            report = base / "report.json"
            first = root / rows[0]["path"]

            with self.assertRaisesRegex(PATCHER.PatchError, "st_nlink=2"):
                PATCHER.run(root, manifest, report, True)
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual(last.read_bytes(), original)
            self.assertEqual(alias.read_bytes(), original)
            self.assertFalse(report.exists())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(
        f"ifma scanner tests: {result.testsRun} run, "
        f"{len(result.skipped)} skipped, "
        f"{len(result.failures)} failures, {len(result.errors)} errors"
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
