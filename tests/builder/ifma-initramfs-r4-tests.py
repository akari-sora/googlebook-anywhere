#!/usr/bin/env python3
"""Round-4 initramfs padding, bounded CPIO, and legacy-LZ4 regressions."""
import bz2
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile
import zlib

import sys
sys.dont_write_bytecode = True

spec = importlib.util.spec_from_file_location(
    "ifma_r4_fixtures", Path(__file__).with_name("ifma-scan-tests.py")
)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
SCANNER = fixtures.SCANNER
if os.environ.get("IFMA_SCANNER_UNDER_TEST"):
    spec = importlib.util.spec_from_file_location(
        "r4_initramfs_scanner", os.environ["IFMA_SCANNER_UNDER_TEST"]
    )
    SCANNER = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(SCANNER)


class InitramfsRound4Tests(unittest.TestCase):
    category = "synthetic"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "tree"
        self.root.mkdir()
        self.clean = fixtures.newc_archive("clean", b"clean")

    def scan(self, **kwargs):
        return SCANNER.scan_tree(
            self.root, provenance={"partition": "system_a"}, **kwargs
        )

    def write(self, data, name="ramdisk"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def fake_tool(self, name, payload):
        tools = self.base / "tools"
        bindir = tools / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        output = bindir / f"{name}-output"
        output.write_bytes(payload)
        tool = bindir / name
        tool.write_text(f'#!/bin/sh\ncat "$(dirname "$0")/{name}-output"\n')
        tool.chmod(0o755)
        return tools

    def test_top_level_nul_padding_before_plain_or_gzip_ifma_is_refused(self):
        for padding in (4, 4096):
            for opcode in (fixtures.EncodingTests.LUQ, fixtures.EncodingTests.HUQ):
                evil = fixtures.newc_archive(
                    "evil", fixtures.elf_with_segments(opcode)
                )
                for kind, member in (("plain", evil), ("gzip", gzip.compress(evil))):
                    with self.subTest(padding=padding, opcode=opcode.hex(), kind=kind):
                        self.write(b"\0" * padding + member)
                        with self.assertRaisesRegex(
                            SCANNER.ScanError, "forbidden VEX IFMA"
                        ):
                            self.scan()

    def test_nul_padded_decompressed_members_after_an_archive_are_refused(self):
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        cases = (
            ("A1", self.clean + gzip.compress(b"\0" * 4 + evil)),
            ("A2", gzip.compress(self.clean) + gzip.compress(b"\0" * 4 + evil)),
            ("A3", self.clean + lzma.compress(b"\0" * 4 + evil)),
            ("A4", self.clean + gzip.compress(b"\0" * 512 + evil)),
        )
        for name, data in cases:
            with self.subTest(name=name):
                self.write(data)
                with self.assertRaisesRegex(
                    SCANNER.ScanError, "forbidden VEX IFMA"
                ):
                    self.scan()

    def test_clean_padded_archives_pass_and_gzip_assets_remain_data(self):
        for data in (
            b"\0" * 4 + self.clean,
            b"\0" * 4096 + gzip.compress(self.clean),
            self.clean + gzip.compress(b"\0" * 4 + self.clean),
        ):
            with self.subTest(size=len(data)):
                self.write(data)
                self.assertEqual(self.scan()["hits"], [])
        (self.root / "ramdisk").unlink()
        for index, payload in enumerate(
            (b"*PPD-Adobe: clean\n", b"<html>clean</html>", b"<xml>clean</xml>")
        ):
            path = self.write(gzip.compress(payload), f"asset-{index}.gz")
            events = []
            self.assertEqual(self.scan(coverage_callback=events.append)["hits"], [])
            decoded = [
                row for row in events
                if row["container_chain"][-1]["kind"] == "gzip_payload"
            ]
            self.assertEqual(len(decoded), 1)
            self.assertEqual(decoded[0]["classification"], "data")
            path.unlink()

    def test_padded_member_keeps_real_offset_sha_and_typed_chain(self):
        padding = b"\0" * 12
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        frame = gzip.compress(padding + evil)
        data = self.clean + frame
        self.write(data)
        events = []
        with self.assertRaisesRegex(
            SCANNER.ScanError,
            r"ramdisk!/<tail:0x[0-9a-f]+>!/<gzip>!/<tail:0xc>!/evil\+0x110",
        ):
            self.scan(coverage_callback=events.append)
        tail = [
            row for row in events
            if row.get("kind") == "gzip"
            and row["container_chain"][-1]["kind"] == "initramfs_tail"
        ]
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["size"], len(frame))
        self.assertEqual(tail[0]["sha256"], hashlib.sha256(frame).hexdigest())
        self.assertEqual(
            tail[0]["container_chain"][-1]["path"],
            f"offset:{len(self.clean):#x}",
        )

    def test_suffixless_zip_members_with_long_nul_prefix_are_scanned_or_data(self):
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        outer = self.root / "outer.zip"
        with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("padded-evil", b"\0" * 4096 + evil)
        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
            self.scan()

        with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("padded-clean", b"\0" * 4096 + self.clean)
            archive.writestr("padded-data", b"\0" * 4096 + b"ordinary asset")
            archive.writestr("all-nul", b"\0" * 8192)
        events = []
        self.assertEqual(self.scan(coverage_callback=events.append)["hits"], [])
        data_paths = {
            row["container_chain"][-1]["path"]
            for row in events if row["classification"] == "data"
        }
        self.assertIn("padded-data", data_paths)
        self.assertIn("all-nul", data_paths)
        clean_members = [
            row for row in events
            if row.get("kind") == "cpio-newc"
            and row["source"].endswith("padded-clean!/<tail:0x1000>")
        ]
        self.assertEqual(len(clean_members), 1)
        self.assertEqual(clean_members[0]["size"], len(self.clean))
        self.assertEqual(
            clean_members[0]["sha256"], hashlib.sha256(self.clean).hexdigest()
        )

    def test_lz4_zstd_and_bzimage_decoded_padding_reaches_ifma(self):
        padding = b"\0" * 4096
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.HUQ)
        )
        decoded = padding + evil
        lz4_payload = b"x" * 300
        lz4_block = (0x80000000 | len(lz4_payload)).to_bytes(4, "little") + lz4_payload
        lz4_frame = b"\x04\x22\x4d\x18\x60\x40\0" + lz4_block + b"\0\0\0\0"
        zstd_frame = (
            b"\x28\xb5\x2f\xfd\x60"
            + (len(lz4_payload) - 256).to_bytes(2, "little")
            + ((len(lz4_payload) << 3) | 1).to_bytes(3, "little")
            + lz4_payload
        )
        cases = (
            ("lz4", lz4_frame, self.fake_tool("lz4", decoded)),
            ("zstd", zstd_frame, self.fake_tool("zstd", decoded)),
        )
        for name, data, tools in cases:
            with self.subTest(kind=name):
                self.write(data)
                with self.assertRaisesRegex(
                    SCANNER.ScanError, rf"{name}>!/<tail:0x1000>!/evil\+0x110"
                ):
                    self.scan(otatools=tools)

        bzimage = bytearray(0x206)
        bzimage[0x202:0x206] = b"HdrS"
        self.write(bytes(bzimage))
        fake_module = SimpleNamespace(
            ProfileError=RuntimeError,
            _extract_bzimage_payload=lambda path: decoded,
        )
        fake_spec = SimpleNamespace(
            loader=SimpleNamespace(exec_module=lambda module: None)
        )
        with mock.patch.object(
            SCANNER.importlib.util, "spec_from_file_location", return_value=fake_spec
        ), mock.patch.object(
            SCANNER.importlib.util, "module_from_spec", return_value=fake_module
        ):
            with self.assertRaisesRegex(
                SCANNER.ScanError,
                r"vmlinux!/<tail:0x1000>!/evil\+0x110",
            ):
                self.scan()

    def test_gzip_and_xz_frame_decode_is_reused_for_padded_payload_scan(self):
        decoded = b"\0" * 4096 + fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        cases = (
            ("gzip", gzip.compress(decoded), SCANNER.gzip, "open"),
            ("xz", lzma.compress(decoded), SCANNER.lzma, "open"),
        )
        for name, frame, module, attribute in cases:
            with self.subTest(kind=name):
                self.write(frame)
                with mock.patch.object(
                    module, attribute,
                    side_effect=AssertionError(f"{name} payload decoded twice"),
                ):
                    with self.assertRaisesRegex(
                        SCANNER.ScanError,
                        rf"{name}>!/<tail:0x1000>!/evil\+0x110",
                    ):
                        self.scan()

    def test_failed_frame_validation_removes_partial_decoded_scratch(self):
        scratch = self.base / "scratch"
        scratch.mkdir()
        self.write(gzip.compress(self.clean)[:-10])
        with self.assertRaisesRegex(SCANNER.ScanError, "invalid gzip stream frame"):
            self.scan(scratch_dir=scratch)
        self.assertEqual(list(scratch.iterdir()), [])

    def test_plain_cpio_materialization_is_linear_and_observes_exact_members(self):
        archives = [fixtures.newc_archive(f"file-{index}", bytes([index]))
                    for index in range(12)]
        data = b"".join(archives)
        self.write(data)
        copied = []
        original = SCANNER._copy_stream_member

        def recording_copy(stream, destination, length):
            copied.append(length)
            return original(stream, destination, length)

        events = []
        with mock.patch.object(
            SCANNER, "_copy_stream_member", side_effect=recording_copy
        ):
            self.assertEqual(self.scan(coverage_callback=events.append)["hits"], [])
        self.assertEqual(copied, [len(archive) for archive in archives])
        members = [
            row for row in events
            if row.get("kind") == "cpio-newc"
        ]
        self.assertEqual(len(members), len(archives))
        for row, archive in zip(members, archives):
            self.assertEqual(row["size"], len(archive))
            self.assertEqual(row["sha256"], hashlib.sha256(archive).hexdigest())

    def test_legacy_lz4_block_size_with_gzip_magic_bytes_is_not_a_boundary(self):
        block_size = 0x00018B1F
        block = b"x" * block_size
        first = b"ordinary first block"
        legacy = (
            b"\x02\x21\x4c\x18"
            + len(first).to_bytes(4, "little") + first
            + block_size.to_bytes(4, "little") + block
        )
        self.write(legacy)
        tools = self.fake_tool("lz4", self.clean)
        self.assertEqual(self.scan(otatools=tools)["hits"], [])

    def test_complete_legacy_lz4_block_wins_even_if_size_starts_valid_gzip(self):
        first = b"ordinary first block"
        accidental_gzip = gzip.compress(self.clean, mtime=0)
        self.assertEqual(accidental_gzip[:4], b"\x1f\x8b\x08\x00")
        block_size = int.from_bytes(accidental_gzip[:4], "little")
        second = accidental_gzip[4:] + b"\0" * (
            block_size - len(accidental_gzip) + 4
        )
        legacy = (
            b"\x02\x21\x4c\x18"
            + len(first).to_bytes(4, "little") + first
            + accidental_gzip[:4] + second
        )
        self.write(legacy)
        events = []
        report = self.scan(
            otatools=self.fake_tool("lz4", self.clean),
            coverage_callback=events.append,
        )
        self.assertEqual(report["hits"], [])
        self.assertEqual(
            [row["kind"] for row in report["nested_opened"]
             if row["kind"] in ("lz4", "gzip")],
            ["lz4"],
        )
        frames = [row for row in events if row.get("kind") == "lz4"]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["size"], len(legacy))
        self.assertEqual(frames[0]["sha256"], hashlib.sha256(legacy).hexdigest())

    def test_any_leading_nul_count_before_supported_compressed_ifma_is_refused(self):
        for opcode in (fixtures.EncodingTests.LUQ, fixtures.EncodingTests.HUQ):
            evil = fixtures.newc_archive(
                "evil", fixtures.elf_with_segments(opcode)
            )
            gzip_frame = gzip.compress(evil)
            xz_frame = lzma.compress(evil)
            for padding in (1, 2, 3, 4, 5, 4097):
                for kind, frame in (("gzip", gzip_frame), ("xz", xz_frame)):
                    with self.subTest(
                        opcode=opcode.hex(), kind=kind, padding=padding
                    ):
                        path = self.write(b"\0" * padding + frame)
                        try:
                            with self.assertRaisesRegex(
                                SCANNER.ScanError,
                                rf"forbidden VEX IFMA.*ramdisk!/<tail:{padding:#x}>!/<{kind}>!/evil\+0x110",
                            ):
                                self.scan()
                        finally:
                            path.unlink(missing_ok=True)

                with self.subTest(opcode=opcode.hex(), kind="img-gzip", padding=padding):
                    img = self.write(b"\0" * padding + gzip_frame, "initrd.img")
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.ScanError,
                            rf"forbidden VEX IFMA.*initrd\.img!/<tail:{padding:#x}>!/<gzip>!/evil\+0x110",
                        ):
                            self.scan()
                    finally:
                        img.unlink(missing_ok=True)

                with self.subTest(opcode=opcode.hex(), kind="decoded-gzip", padding=padding):
                    outer_gz = self.write(
                        gzip.compress(b"\0" * padding + gzip_frame), "nested.gz"
                    )
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.ScanError,
                            rf"forbidden VEX IFMA.*nested\.gz!/<gzip>!/<tail:{padding:#x}>!/<gzip>!/evil\+0x110",
                        ):
                            self.scan()
                    finally:
                        outer_gz.unlink(missing_ok=True)

                with self.subTest(opcode=opcode.hex(), kind="zip-gzip", padding=padding):
                    outer_zip = self.root / "outer.zip"
                    with zipfile.ZipFile(
                        outer_zip, "w", compression=zipfile.ZIP_STORED
                    ) as archive:
                        archive.writestr("member", b"\0" * padding + gzip_frame)
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.ScanError,
                            rf"forbidden VEX IFMA.*outer\.zip!/member!/<tail:{padding:#x}>!/<gzip>!/evil\+0x110",
                        ):
                            self.scan()
                    finally:
                        outer_zip.unlink(missing_ok=True)

        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        lz4_payload = b"x" * 300
        lz4_block = (0x80000000 | len(lz4_payload)).to_bytes(4, "little") + lz4_payload
        lz4_frame = b"\x04\x22\x4d\x18\x60\x40\0" + lz4_block + b"\0\0\0\0"
        lz4_legacy = b"\x02\x21\x4c\x18" + len(lz4_payload).to_bytes(4, "little") + lz4_payload
        zstd_frame = (
            b"\x28\xb5\x2f\xfd\x60"
            + (len(lz4_payload) - 256).to_bytes(2, "little")
            + ((len(lz4_payload) << 3) | 1).to_bytes(3, "little")
            + lz4_payload
        )
        for padding in (1, 2, 3, 4, 4097):
            for kind, frame, tool_name in (
                ("lz4-frame", lz4_frame, "lz4"),
                ("lz4-legacy", lz4_legacy, "lz4"),
                ("zstd", zstd_frame, "zstd"),
            ):
                with self.subTest(kind=kind, padding=padding):
                    path = self.write(b"\0" * padding + frame)
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.ScanError,
                            rf"forbidden VEX IFMA.*ramdisk!/<tail:{padding:#x}>!/<{tool_name}>!/evil\+0x110",
                        ):
                            self.scan(otatools=self.fake_tool(tool_name, evil))
                    finally:
                        path.unlink(missing_ok=True)

        for unaligned_padding in (1, 2, 3):
            with self.subTest(unaligned_cpio_padding=unaligned_padding):
                path = self.write(b"\0" * unaligned_padding + evil)
                try:
                    events = []
                    self.assertEqual(self.scan(coverage_callback=events.append)["hits"], [])
                    self.assertEqual(events[-1]["classification"], "data")
                finally:
                    path.unlink(missing_ok=True)

    def test_leading_nuls_before_unsupported_magics_require_exact_pin_across_entries(self):
        magics = (
            (b"BZh", "bzip2"),
            (b"\x5d\x00\x00", "lzma-alone"),
            (b"\x89LZO", "lzo"),
            (b"ANDROID!", "android-boot"),
            (b"VNDRBOOT", "android-vendor-boot"),
        )
        for padding in (1, 2, 3, 4, 4097):
            for magic, expected_kind in magics:
                raw = b"\0" * padding + magic + b"opaque payload"
                raw_sha = hashlib.sha256(raw).hexdigest()
                expected_reason = f"unsupported {expected_kind} at {padding:#x}"

                with self.subTest(entry="tree", kind=expected_kind, padding=padding):
                    path = self.write(raw, "ordinary")
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.ScanError, re_escape := expected_reason
                        ):
                            self.scan()
                        events = []
                        pin = {
                            "partition": "system_a",
                            "container_chain": [{"kind": "tree_file", "path": "ordinary"}],
                            "sha256": raw_sha,
                            "reason": "pinned padded opaque container",
                        }
                        report = self.scan(
                            unsupported_allowlist=[pin],
                            coverage_callback=events.append,
                        )
                        self.assertEqual(len(report["unsupported_images"]), 1)
                        self.assertEqual(report["unsupported_images"][0]["sha256"], raw_sha)
                        self.assertEqual(report["unsupported_images"][0]["reason"], expected_reason)
                        self.assertEqual(events[-1]["classification"], "container")
                        self.assertEqual(events[-1]["kind"], expected_kind)
                        self.assertEqual(events[-1]["sha256"], raw_sha)
                    finally:
                        path.unlink(missing_ok=True)

                with self.subTest(entry="zip", kind=expected_kind, padding=padding):
                    outer_zip = self.root / "outer.zip"
                    with zipfile.ZipFile(
                        outer_zip, "w", compression=zipfile.ZIP_STORED
                    ) as archive:
                        archive.writestr("member", raw)
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.ScanError, expected_reason
                        ):
                            self.scan()
                        pin = {
                            "partition": "system_a",
                            "container_chain": [
                                {"kind": "tree_file", "path": "outer.zip"},
                                {"kind": "zip_member", "path": "member"},
                            ],
                            "sha256": raw_sha,
                            "reason": "pinned padded zip opaque container",
                        }
                        report = self.scan(unsupported_allowlist=[pin])
                        self.assertEqual(len(report["unsupported_images"]), 1)
                        self.assertEqual(report["unsupported_images"][0]["sha256"], raw_sha)
                        self.assertEqual(report["unsupported_images"][0]["reason"], expected_reason)
                    finally:
                        outer_zip.unlink(missing_ok=True)

                with self.subTest(entry="decoded", kind=expected_kind, padding=padding):
                    outer_gz = self.write(gzip.compress(raw), "outer.gz")
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.ScanError, expected_reason
                        ):
                            self.scan()
                        pin = {
                            "partition": "system_a",
                            "container_chain": [
                                {"kind": "tree_file", "path": "outer.gz"},
                                {"kind": "gzip_payload", "path": "<gzip>"},
                            ],
                            "sha256": raw_sha,
                            "reason": "pinned padded decoded opaque container",
                        }
                        report = self.scan(unsupported_allowlist=[pin])
                        self.assertEqual(len(report["unsupported_images"]), 1)
                        self.assertEqual(report["unsupported_images"][0]["sha256"], raw_sha)
                        self.assertEqual(report["unsupported_images"][0]["reason"], expected_reason)
                    finally:
                        outer_gz.unlink(missing_ok=True)

    def test_undecodable_compressed_magic_after_leading_nuls_pinnable_only_for_gzip_xz_legacy_lz4(self):
        """gzip/xz/legacy-LZ4 magics that decode to 0 bytes stay pinnable;
        modern zstd/lz4 magics that fail to decode are never pinnable (V1)."""
        magics = (
            (b"\x1f\x8b" + b"\0" * 16, "gzip"),
            (b"\xfd7zXZ\0" + b"\0" * 16, "xz"),
            (b"\x28\xb5\x2f\xfd" + b"\0" * 4, "zstd"),
            (b"\x04\x22\x4d\x18" + b"\0" * 16, "lz4"),
            (b"\x02\x21\x4c\x18\x04\x00\x00\x00" + b"\0" * 2, "lz4"),
        )
        for padding in (1, 4, 4097):
            for body, expected_kind in magics:
                raw = b"\0" * padding + body
                raw_sha = hashlib.sha256(raw).hexdigest()
                bad_sha = "0" * 64
                expected_reason = rf"unsupported nested image: .*invalid {expected_kind} stream frame at {padding:#x}"
                if expected_kind in ("zstd", "lz4") and not body.startswith(b"\x02\x21\x4c\x18"):
                    # V1 spec change: a modern zstd/lz4 frame that fails to
                    # decode is never pinnable (was: exact pin accepted it).
                    self._modern_failed_magic_refused(raw, expected_kind, padding)
                    continue

                with self.subTest(entry="tree", kind=expected_kind, padding=padding):
                    path = self.write(raw, "ordinary")
                    try:
                        with self.assertRaisesRegex(SCANNER.ScanError, expected_reason):
                            self.scan()
                        events = []
                        pin = {
                            "partition": "system_a",
                            "container_chain": [{"kind": "tree_file", "path": "ordinary"}],
                            "sha256": raw_sha,
                            "reason": "pinned false-positive padded compressed magic",
                        }
                        report = self.scan(
                            unsupported_allowlist=[pin],
                            coverage_callback=events.append,
                        )
                        self.assertEqual(len(report["unsupported_images"]), 1)
                        self.assertEqual(report["unsupported_images"][0]["sha256"], raw_sha)
                        self.assertRegex(
                            report["unsupported_images"][0]["reason"],
                            rf"invalid {expected_kind} stream frame at {padding:#x}",
                        )
                        self.assertEqual(events[-1]["classification"], "container")
                        self.assertEqual(events[-1]["kind"], expected_kind)
                        self.assertEqual(events[-1]["sha256"], raw_sha)
                        with self.assertRaises(SCANNER.ScanError):
                            self.scan(unsupported_allowlist=[dict(pin, sha256=bad_sha)])
                    finally:
                        path.unlink(missing_ok=True)

                with self.subTest(entry="zip", kind=expected_kind, padding=padding):
                    outer_zip = self.root / "outer.zip"
                    with zipfile.ZipFile(
                        outer_zip, "w", compression=zipfile.ZIP_STORED
                    ) as archive:
                        archive.writestr("member", raw)
                    try:
                        with self.assertRaisesRegex(SCANNER.ScanError, expected_reason):
                            self.scan()
                        pin = {
                            "partition": "system_a",
                            "container_chain": [
                                {"kind": "tree_file", "path": "outer.zip"},
                                {"kind": "zip_member", "path": "member"},
                            ],
                            "sha256": raw_sha,
                            "reason": "pinned zip false-positive padded compressed magic",
                        }
                        report = self.scan(unsupported_allowlist=[pin])
                        self.assertEqual(len(report["unsupported_images"]), 1)
                        self.assertEqual(report["unsupported_images"][0]["sha256"], raw_sha)
                        with self.assertRaises(SCANNER.ScanError):
                            self.scan(unsupported_allowlist=[dict(pin, sha256=bad_sha)])
                    finally:
                        outer_zip.unlink(missing_ok=True)

                with self.subTest(entry="decoded", kind=expected_kind, padding=padding):
                    outer_gz = self.write(gzip.compress(raw), "outer.gz")
                    try:
                        with self.assertRaisesRegex(SCANNER.ScanError, expected_reason):
                            self.scan()
                        pin = {
                            "partition": "system_a",
                            "container_chain": [
                                {"kind": "tree_file", "path": "outer.gz"},
                                {"kind": "gzip_payload", "path": "<gzip>"},
                            ],
                            "sha256": raw_sha,
                            "reason": "pinned decoded false-positive padded compressed magic",
                        }
                        report = self.scan(unsupported_allowlist=[pin])
                        self.assertEqual(len(report["unsupported_images"]), 1)
                        self.assertEqual(report["unsupported_images"][0]["sha256"], raw_sha)
                        with self.assertRaises(SCANNER.ScanError):
                            self.scan(unsupported_allowlist=[dict(pin, sha256=bad_sha)])
                    finally:
                        outer_gz.unlink(missing_ok=True)

    def _modern_failed_magic_refused(self, raw, kind, padding):
        raw_sha = hashlib.sha256(raw).hexdigest()
        reason = rf"invalid {kind} stream frame at {padding:#x}"
        entries = (
            ("tree", "ordinary", raw, [{"kind": "tree_file", "path": "ordinary"}]),
            ("zip", "outer.zip", None, [
                {"kind": "tree_file", "path": "outer.zip"},
                {"kind": "zip_member", "path": "member"},
            ]),
            ("decoded", "outer.gz", gzip.compress(raw), [
                {"kind": "tree_file", "path": "outer.gz"},
                {"kind": "gzip_payload", "path": "<gzip>"},
            ]),
        )
        for entry, name, data, chain in entries:
            with self.subTest(entry=entry, kind=kind, padding=padding, rule="never-pinnable"):
                if data is None:
                    with zipfile.ZipFile(self.root / name, "w", compression=zipfile.ZIP_STORED) as archive:
                        archive.writestr("member", raw)
                    path = self.root / name
                else:
                    path = self.write(data, name)
                pin = {
                    "partition": "system_a", "container_chain": chain, "sha256": raw_sha,
                    "reason": "attempt to pin a failed modern frame",
                }
                try:
                    for allow in ([], [pin]):
                        with self.assertRaisesRegex(SCANNER.ScanError, reason) as caught:
                            self.scan(unsupported_allowlist=allow)
                        self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
                        self.assertNotRegex(str(caught.exception), "unsupported nested image")
                finally:
                    path.unlink(missing_ok=True)

    def test_kernel_two_byte_unsupported_magics_and_small_dict_lzma_are_refused(self):
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        small_lzma = lzma.compress(
            evil,
            format=lzma.FORMAT_ALONE,
            filters=[{"id": lzma.FILTER_LZMA1, "dict_size": 32768}],
        )
        self.assertEqual(small_lzma[:5], b"\x5d\x00\x80\x00\x00")
        cases = (
            ("lzma-small-dict", small_lzma, "lzma-alone"),
            ("bzip2-2byte", b"\x42\x5a\x39opaque-bzip2", "bzip2"),
            ("lzo-2byte", b"\x89\x4c\x00opaque-lzo", "lzo"),
            ("gzip-old-2byte", b"\x1f\x9eopaque-gzip-old", "gzip-old"),
        )
        for label, payload, expected_kind in cases:
            for padding in (0, 1, 4097):
                raw = b"\0" * padding + payload
                raw_sha = hashlib.sha256(raw).hexdigest()
                expected_reason = f"unsupported {expected_kind} at {padding:#x}"
                with self.subTest(case=label, padding=padding):
                    path = self.write(raw, "ramdisk")
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.ScanError, expected_reason
                        ):
                            self.scan()
                        events = []
                        pin = {
                            "partition": "system_a",
                            "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                            "sha256": raw_sha,
                            "reason": "pinned kernel 2-byte unsupported container",
                        }
                        report = self.scan(
                            unsupported_allowlist=[pin],
                            coverage_callback=events.append,
                        )
                        self.assertEqual(len(report["unsupported_images"]), 1)
                        self.assertEqual(report["unsupported_images"][0]["sha256"], raw_sha)
                        self.assertEqual(report["unsupported_images"][0]["reason"], expected_reason)
                        self.assertEqual(events[-1]["classification"], "container")
                        self.assertEqual(events[-1]["kind"], expected_kind)
                        self.assertEqual(events[-1]["sha256"], raw_sha)
                        with self.assertRaises(SCANNER.ScanError):
                            self.scan(unsupported_allowlist=[dict(pin, sha256="0" * 64)])
                    finally:
                        path.unlink(missing_ok=True)

        for ordinary in (
            b"BAordinary",
            b"\x5d\x01ordinary",
            b"\x89Mordinary",
            b"\x1f\x00ordinary",
            b"\0\x5d\x01ordinary",
            b"\0\x89Mordinary",
        ):
            with self.subTest(ordinary=ordinary.hex()):
                path = self.write(ordinary, "ramdisk")
                try:
                    events = []
                    report = self.scan(coverage_callback=events.append)
                    self.assertEqual(report["hits"], [])
                    self.assertEqual(report["unsupported_images"], [])
                    self.assertEqual(events[-1]["classification"], "data")
                finally:
                    path.unlink(missing_ok=True)

    def test_kernel_equivalent_gzip_and_partial_frame_salvage_refuse_ifma_even_with_pin(self):
        evil_elf = fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        evil = fixtures.newc_archive("evil", evil_elf)
        gz_evil = gzip.compress(evil)
        gz_bad_crc = bytearray(gz_evil)
        gz_bad_crc[-8] ^= 0xFF
        gz_bad_isize = bytearray(gz_evil)
        gz_bad_isize[-1] ^= 0xFF

        comp = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
        raw_deflate = comp.compress(evil) + comp.flush()
        flagged_header = (
            b"\x1f\x8b\x08\x1e\x00\x00\x00\x00\x00\x03"
            + struct.pack("<H", 3) + b"ext"
            + b"ramdisk.cpio\x00"
            + b"comment\x00"
            + b"\x12\x34"
        )
        gz_flagged_no_trailer = flagged_header + raw_deflate

        sync_comp = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
        sync_deflate = sync_comp.compress(evil) + sync_comp.flush(zlib.Z_SYNC_FLUSH)
        gz_partial_corrupt = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + sync_deflate + b"\xff\xff\xff\xff"

        xz_evil = lzma.compress(evil)
        for label, payload in (
            ("gzip-missing-trailer", b"\0" + gz_evil[:-8]),
            ("gzip-wrong-crc32", b"\0" + bytes(gz_bad_crc)),
            ("gzip-wrong-isize", b"\0" + bytes(gz_bad_isize)),
            ("gzip-flagged-header-no-trailer", b"\0" + gz_flagged_no_trailer),
            ("gzip-partial-corrupt-after-sync", b"\0" + gz_partial_corrupt),
            ("xz-truncated-footer", b"\0" + xz_evil[:-12]),
            ("xz-truncated-trailer-member", b"\0" + xz_evil[:-48]),
            ("xz-truncated-mid-elf-member", b"\0" + xz_evil[:-64]),
        ):
            with self.subTest(case=label):
                path = self.write(payload, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "reason": "attempt to pin kernel-decodable or partial frame",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan()
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        # Partial clean frame (> 0 decoded bytes before failure) must also fail
        # closed as ScanError (never pinnable ImageOpenError).
        xz_clean_partial = b"\0" + lzma.compress(self.clean)[:-12]
        path = self.write(xz_clean_partial, "ramdisk")
        try:
            pin = {
                "partition": "system_a",
                "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                "sha256": hashlib.sha256(xz_clean_partial).hexdigest(),
                "reason": "attempt to pin partial clean xz",
            }
            with self.assertRaisesRegex(SCANNER.ScanError, "invalid xz stream frame at 0x1"):
                self.scan(unsupported_allowlist=[pin])
        finally:
            path.unlink(missing_ok=True)

        # 00 + 1f 8b + all-NUL tail (or truncated magic at EOF) that decodes to
        # 0 bytes remains pinnable unsupported.
        for label, zero_byte_gzip in (
            ("bad-method-all-nul", b"\0\x1f\x8b" + b"\0" * 16),
            ("truncated-magic-only", b"\0\x1f\x8b"),
        ):
            with self.subTest(zero_decoded=label):
                path = self.write(zero_byte_gzip, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(zero_byte_gzip).hexdigest(),
                    "reason": "pinned 0-byte-decoded gzip magic",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "unsupported nested image"):
                        self.scan()
                    report = self.scan(unsupported_allowlist=[pin])
                    self.assertEqual(len(report["unsupported_images"]), 1)
                finally:
                    path.unlink(missing_ok=True)

        # zstd/lz4 frames that emit > 0 bytes before failing must scan that
        # partial output and refuse as unpinnable ScanError on both truncated
        # and structurally complete frames.
        partial_tools = self.base / "partial-tools"
        partial_bin = partial_tools / "bin"
        partial_bin.mkdir(parents=True, exist_ok=True)
        evil_payload_file = partial_tools / "evil.cpio"
        evil_payload_file.write_bytes(evil)
        for tool_name in ("zstd", "lz4"):
            script = partial_bin / tool_name
            script.write_text(f"#!/bin/sh\ncat {str(evil_payload_file)!r}\nexit 1\n")
            script.chmod(0o755)
        opaque = b"corrupt-frame-body"
        struct_zstd = (
            b"\x28\xb5\x2f\xfd\x20"
            + bytes([len(opaque)])
            + ((len(opaque) << 3) | (2 << 1) | 1).to_bytes(3, "little")
            + opaque
        )
        struct_lz4 = (
            b"\x04\x22\x4d\x18\x60\x40\x82"
            + len(opaque).to_bytes(4, "little")
            + opaque
            + b"\x00\x00\x00\x00"
        )
        for label, raw in (
            ("zstd-truncated-partial-evil", b"\0\x28\xb5\x2f\xfd\x20\x05"),
            ("lz4-truncated-partial-evil", b"\0\x04\x22\x4d\x18\x60\x40\x82\x05\x00\x00\x00"),
            ("zstd-struct-partial-evil", b"\0" + struct_zstd),
            ("lz4-struct-partial-evil", b"\0" + struct_lz4),
        ):
            with self.subTest(partial_tool_case=label):
                path = self.write(raw, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin partial zstd/lz4 decode",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan(otatools=partial_tools, unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        # Valid gzip with > 64 KiB decompressed text and wrong CRC32 or missing
        # trailer is accepted as data.
        clean_text_gz = bytearray(gzip.compress(b"clean text payload line\n" * 4096))
        clean_text_gz[-8] ^= 0xFF
        for label, candidate_bytes in (
            ("wrong-crc32", bytes(clean_text_gz)),
            ("missing-trailer", bytes(clean_text_gz[:-8])),
        ):
            with self.subTest(clean_gzip=label):
                path = self.write(candidate_bytes, "notice.txt.gz")
                try:
                    events = []
                    report = self.scan(coverage_callback=events.append)
                    self.assertEqual(report["hits"], [])
                    self.assertEqual(report["unsupported_images"], [])
                    self.assertEqual(events[-1]["classification"], "data")
                finally:
                    path.unlink(missing_ok=True)

    def test_initramfs_undecodable_frame_boundaries_one_through_four(self):
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        opaque = b"corrupt-zstd-block-body"
        bad_zstd_frame = (
            b"\x28\xb5\x2f\xfd\x20"
            + bytes([len(opaque)])
            + ((len(opaque) << 3) | (2 << 1) | 1).to_bytes(3, "little")
            + opaque
        )
        failing_tools = self.base / "failing-tools"
        failing_bin = failing_tools / "bin"
        failing_bin.mkdir(parents=True, exist_ok=True)
        zstd_script = failing_bin / "zstd"
        zstd_script.write_text("#!/bin/sh\nexit 1\n")
        zstd_script.chmod(0o755)

        # 1. Real member at 0 + second frame whose payload fails to decode ->
        # refused (ScanError, not pinnable), both when _compressed_member_length
        # is None (kills N2) and when struct length is valid but decode fails (kills N3).
        for label, second_frame, tools in (
            ("length-none-after-member", b"\0" * 4 + b"\x1f\x8bnot-a-valid-gzip", None),
            ("struct-ok-decode-fail-after-member", b"\0" * 4 + bad_zstd_frame, failing_tools),
        ):
            with self.subTest(boundary=1, case=label):
                raw = self.clean + second_frame
                path = self.write(raw, "ramdisk")
                outer_pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt outer pin on corrupt second member",
                }
                tail_offset = len(self.clean) + 4
                frame_pin = {
                    "partition": "system_a",
                    "container_chain": [
                        {"kind": "tree_file", "path": "ramdisk"},
                        {"kind": "initramfs_tail", "path": f"offset:{tail_offset:#x}"},
                    ],
                    "sha256": hashlib.sha256(second_frame[4:]).hexdigest(),
                    "reason": "attempt frame pin on corrupt second member",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "invalid (gzip|zstd) stream frame"):
                        self.scan(otatools=tools, unsupported_allowlist=[outer_pin])
                    with self.assertRaisesRegex(SCANNER.ScanError, "invalid (gzip|zstd) stream frame"):
                        self.scan(otatools=tools, unsupported_allowlist=[frame_pin])
                finally:
                    path.unlink(missing_ok=True)

        # 2. Padded undecodable zstd frame at >0 followed by a valid second
        # member -> V1: refused under the outer AND the frame-level pin.
        with self.subTest(boundary=2):
            raw_clean_tail = b"\0" * 4 + bad_zstd_frame + self.clean
            path = self.write(raw_clean_tail, "ramdisk")
            outer_pin = {
                "partition": "system_a",
                "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                "sha256": hashlib.sha256(raw_clean_tail).hexdigest(),
                "reason": "outer pin must not cover frame followed by member",
            }
            frame_pin = {
                "partition": "system_a",
                "container_chain": [
                    {"kind": "tree_file", "path": "ramdisk"},
                    {"kind": "initramfs_tail", "path": "offset:0x4"},
                ],
                "sha256": hashlib.sha256(bad_zstd_frame).hexdigest(),
                "reason": "exact frame pin for padded first frame",
            }
            try:
                for pin in (outer_pin, frame_pin):
                    with self.assertRaisesRegex(SCANNER.ScanError, r"invalid zstd stream frame at 0x4") as caught:
                        self.scan(otatools=failing_tools, unsupported_allowlist=[pin])
                    self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
            finally:
                path.unlink(missing_ok=True)

            raw_evil_tail = b"\0" * 4 + bad_zstd_frame + evil
            path = self.write(raw_evil_tail, "ramdisk")
            try:
                with self.assertRaisesRegex(SCANNER.ScanError, r"invalid zstd stream frame at 0x4"):
                    self.scan(otatools=failing_tools, unsupported_allowlist=[frame_pin])
            finally:
                path.unlink(missing_ok=True)

        # 3. Padded undecodable zstd frame at >0 followed by 0 or more trailing
        # NULs -> V1: refused even with the outer pin (was: pinnable).
        for trailing_nuls in (b"", b"\0" * 8):
            with self.subTest(boundary=3, trailing_nuls=len(trailing_nuls)):
                raw = b"\0" * 4 + bad_zstd_frame + trailing_nuls
                path = self.write(raw, "ramdisk")
                outer_pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "outer pin for padded undecodable frame with trailing NULs",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, r"invalid zstd stream frame at 0x4") as caught:
                        self.scan(otatools=failing_tools, unsupported_allowlist=[outer_pin])
                    self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
                finally:
                    path.unlink(missing_ok=True)

        # 4. Padded undecodable zstd frame at >0 followed by a non-zero tail ->
        # V1: the failed frame itself refuses, with or without any pin.
        with self.subTest(boundary=4):
            raw = b"\0" * 4 + bad_zstd_frame + b"NONZERO_GARBAGE_TAIL"
            path = self.write(raw, "ramdisk")
            outer_pin = {
                "partition": "system_a",
                "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                "sha256": hashlib.sha256(raw).hexdigest(),
                "reason": "outer pin must not pass non-zero tail",
            }
            frame_pin = {
                "partition": "system_a",
                "container_chain": [
                    {"kind": "tree_file", "path": "ramdisk"},
                    {"kind": "initramfs_tail", "path": "offset:0x4"},
                ],
                "sha256": hashlib.sha256(bad_zstd_frame).hexdigest(),
                "reason": "frame pin for first frame before non-zero tail",
            }
            try:
                for allow in ([], [outer_pin], [frame_pin]):
                    with self.assertRaisesRegex(SCANNER.ScanError, r"invalid zstd stream frame at 0x4"):
                        self.scan(otatools=failing_tools, unsupported_allowlist=allow)
            finally:
                path.unlink(missing_ok=True)

    def test_legacy_lz4_per_chunk_decode_and_kernel_end_rules(self):
        lz4 = shutil.which("lz4")
        if not lz4:
            self.skipTest("lz4 CLI unavailable")
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        evil_legacy = subprocess.run(
            [lz4, "-l", "-c"], input=evil,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        clean_legacy = subprocess.run(
            [lz4, "-l", "-c"], input=self.clean,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        text_legacy = subprocess.run(
            [lz4, "-l", "-c"], input=b"clean legacy text line\n" * 64,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        truncated_second = (500).to_bytes(4, "little") + b"A" * 20
        corrupt_second = (16).to_bytes(4, "little") + b"\xff" * 16

        for label, tail in (
            ("truncated-chunk2", truncated_second),
            ("corrupt-chunk2", corrupt_second),
        ):
            for pad in (b"", b"\0"):
                raw = pad + evil_legacy + tail
                with self.subTest(case=label, pad=len(pad)):
                    path = self.write(raw, "ramdisk")
                    pin = {
                        "partition": "system_a",
                        "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "reason": "attempt to pin multi-chunk legacy lz4 with IFMA chunk 1",
                    }
                    try:
                        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                            self.scan()
                        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                            self.scan(unsupported_allowlist=[pin])
                    finally:
                        path.unlink(missing_ok=True)

        multi_clean = clean_legacy + clean_legacy[4:]
        for label, tail in (
            ("clean-truncated-last", truncated_second),
            ("clean-corrupt-last", corrupt_second),
        ):
            for pad in (b"", b"\0"):
                raw = pad + multi_clean + tail
                with self.subTest(case=label, pad=len(pad)):
                    path = self.write(raw, "ramdisk")
                    pin = {
                        "partition": "system_a",
                        "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "reason": "attempt to pin clean multi-chunk legacy lz4 with bad tail chunk",
                    }
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.ScanError,
                            r"(invalid lz4 stream frame|lz4 decompression failed)",
                        ):
                            self.scan(unsupported_allowlist=[pin])
                    finally:
                        path.unlink(missing_ok=True)

        for label, ending in (
            ("zero-size-chunk", b"\x00\x00\x00\x00"),
            ("three-byte-nul-fragment", b"\x00\x00\x00"),
        ):
            with self.subTest(end_rule=label, payload="cpio"):
                path = self.write(clean_legacy + ending, "ramdisk.lz4")
                try:
                    report = self.scan()
                    self.assertEqual(report["hits"], [])
                    self.assertIn(
                        "cpio-newc",
                        [row["kind"] for row in report["nested_opened"]],
                    )
                    state = {
                        "otatools": None,
                        "scratch": self.base,
                        "nested": True,
                        "max_depth": 8,
                        "partition": "system_a",
                        "applicable_residuals": [],
                        "applicable_non_instructions": [],
                        "applicable_unsupported": [],
                        "object_policies": [],
                        "coverage_callback": None,
                        "report": dict(report, nested_opened=[]),
                    }
                    SCANNER._scan_single_image(
                        path, "ramdisk.lz4", (), state, 0, ".lz4", "lz4",
                    )
                    self.assertIn(
                        "cpio-newc",
                        [row["kind"] for row in state["report"]["nested_opened"]],
                    )
                finally:
                    path.unlink(missing_ok=True)

            with self.subTest(end_rule=label, payload="data"):
                path = self.write(text_legacy + ending, "notice.txt.lz4")
                try:
                    events = []
                    report = self.scan(coverage_callback=events.append)
                    self.assertEqual(report["hits"], [])
                    self.assertEqual(events[-1]["classification"], "data")
                finally:
                    path.unlink(missing_ok=True)

            with self.subTest(end_rule=label, payload="evil"):
                raw = evil_legacy + ending
                path = self.write(raw, "ramdisk.lz4")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk.lz4"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin legacy lz4 with end rule",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        for label, ending in (
            ("three-byte-fragment", b"\x01\x02\x03"),
            ("two-byte-fragment", b"XY"),
        ):
            with self.subTest(end_rule=label, payload="cpio"):
                raw = clean_legacy + ending
                path = self.write(raw, "ramdisk.lz4")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk.lz4"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin legacy lz4 with non-zero <4B fragment",
                }
                try:
                    state = {
                        "otatools": None,
                        "scratch": self.base,
                        "nested": True,
                        "max_depth": 8,
                        "partition": "system_a",
                        "applicable_residuals": [],
                        "applicable_non_instructions": [],
                        "applicable_unsupported": [],
                        "object_policies": [],
                        "coverage_callback": None,
                        "report": dict(report, nested_opened=[]),
                    }
                    SCANNER._scan_single_image(
                        path, "ramdisk.lz4", (), state, 0, ".lz4", "lz4",
                    )
                    self.assertIn(
                        "cpio-newc",
                        [row["kind"] for row in state["report"]["nested_opened"]],
                    )
                    with self.assertRaisesRegex(SCANNER.ScanError, "non-zero initramfs tail"):
                        self.scan()
                    with self.assertRaisesRegex(SCANNER.ScanError, "non-zero initramfs tail"):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

            with self.subTest(end_rule=label, payload="data"):
                raw = text_legacy + ending
                path = self.write(raw, "notice.txt.lz4")
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "non-zero initramfs tail"):
                        self.scan()
                finally:
                    path.unlink(missing_ok=True)

            with self.subTest(end_rule=label, payload="evil"):
                raw = evil_legacy + ending
                path = self.write(raw, "ramdisk.lz4")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk.lz4"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin legacy lz4 with non-zero fragment",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        zero_out_then_corrupt = b"\x02\x21\x4c\x18\x01\x00\x00\x00\x00" + corrupt_second
        over_128k_legacy_corrupt = (
            b"\x02\x21\x4c\x18"
            + (130 * 1024).to_bytes(4, "little")
            + b"\xff" * (130 * 1024)
        )
        for label, raw in (
            ("zero-out-chunk1-corrupt-chunk2", zero_out_then_corrupt),
            ("over-128k-legacy-corrupt", over_128k_legacy_corrupt),
        ):
            with self.subTest(unpinnable_legacy=label):
                path = self.write(raw, "ramdisk.lz4")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk.lz4"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin multi-chunk or >128 KiB 0-output legacy lz4",
                }
                try:
                    with self.assertRaisesRegex(
                        SCANNER.ScanError,
                        r"(invalid lz4 stream frame|lz4 decompression failed)",
                    ):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

    def test_streamed_partial_zstd_and_lz4_frames_and_zero_output_boundaries(self):
        zstd = shutil.which("zstd")
        lz4 = shutil.which("lz4")
        if not zstd or not lz4:
            self.skipTest("zstd or lz4 CLI unavailable")
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )

        def parse_zstd_blocks(frame):
            pos = 4
            desc = frame[pos]
            pos += 1
            single = bool(desc & 0x20)
            if not single:
                pos += 1
            pos += (0, 1, 2, 4)[desc & 3] + (1 if single else 0, 2, 4, 8)[desc >> 6]
            hdr_end = pos
            blocks = []
            while True:
                b_start = pos
                val = int.from_bytes(frame[pos:pos + 3], "little")
                pos += 3
                last, btype, bsize = val & 1, (val >> 1) & 3, val >> 3
                stored = 1 if btype == 1 else bsize
                blocks.append((b_start, pos + stored, last, btype, bsize))
                pos += stored
                if last:
                    break
            return hdr_end, blocks

        # >128 KiB two-block and three-block zstd frames with corrupt/truncated later block.
        large_evil = evil + b"\0" * (140 * 1024 - len(evil))
        z_large = subprocess.run(
            [zstd, "-q", "--no-check", "-c"], input=large_evil,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        hdr_end, large_blocks = parse_zstd_blocks(z_large)
        self.assertGreaterEqual(len(large_blocks), 2)
        b1_end = large_blocks[0][1]
        b2_start, b2_end, _, b2_type, b2_size = large_blocks[1]
        b2_nonlast = ((b2_size << 3) | (b2_type << 1) | 0).to_bytes(3, "little") + z_large[b2_start + 3:b2_end]
        corrupt_zstd_last = ((16 << 3) | (2 << 1) | 1).to_bytes(3, "little") + b"\xff" * 16
        truncated_zstd_next = ((64 << 3) | (2 << 1) | 1).to_bytes(3, "little") + b"\xff" * 8
        two_block_gt128k = b"\x28\xb5\x2f\xfd\x00\x58" + z_large[hdr_end:b1_end] + b2_nonlast + corrupt_zstd_last
        two_block_128k = b"\x28\xb5\x2f\xfd\x00\x58" + z_large[hdr_end:b1_end] + corrupt_zstd_last
        two_block_trunc = b"\x28\xb5\x2f\xfd\x00\x58" + z_large[hdr_end:b1_end] + truncated_zstd_next

        # Sub-128 KiB two-block zstd frame (Block 1 < 128 KiB + corrupt/truncated Block 2).
        z_small = subprocess.run(
            [zstd, "-q", "--no-check", "-c"], input=evil,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        s_hdr_end, small_blocks = parse_zstd_blocks(z_small)
        sb_start, sb_end, _, sb_type, sb_size = small_blocks[0]
        sb_nonlast = ((sb_size << 3) | (sb_type << 1) | 0).to_bytes(3, "little") + z_small[sb_start + 3:sb_end]
        sub128k_zstd_corrupt = b"\x28\xb5\x2f\xfd\x00\x58" + sb_nonlast + corrupt_zstd_last
        sub128k_zstd_trunc = b"\x28\xb5\x2f\xfd\x00\x58" + sb_nonlast + truncated_zstd_next

        # Modern LZ4 two-block frame (Block 1 valid + Block 2 corrupt or truncated).
        lz4_small = subprocess.run(
            [lz4, "-q", "--no-frame-crc", "-c"], input=evil,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        lz4_flags = lz4_small[4]
        lz4_hdr_len = 6 + (8 if lz4_flags & 0x08 else 0) + (4 if lz4_flags & 0x01 else 0) + 1
        b1_raw_size = struct.unpack_from("<I", lz4_small, lz4_hdr_len)[0]
        b1_step = 4 + (b1_raw_size & 0x7FFFFFFF) + (4 if lz4_flags & 0x10 else 0)
        lz4_prefix = lz4_small[:lz4_hdr_len + b1_step]
        lz4_two_block_corrupt = lz4_prefix + (16).to_bytes(4, "little") + b"\xff" * 16 + b"\x00\x00\x00\x00"
        lz4_two_block_trunc = lz4_prefix + (64).to_bytes(4, "little") + b"\xff" * 8

        for label, frame_bytes in (
            ("zstd-gt128k-corrupt-last", two_block_gt128k),
            ("zstd-128k-corrupt-block2", two_block_128k),
            ("zstd-128k-truncated-block2", two_block_trunc),
            ("zstd-sub128k-corrupt-block2", sub128k_zstd_corrupt),
            ("zstd-sub128k-truncated-block2", sub128k_zstd_trunc),
            ("lz4-frame-corrupt-block2", lz4_two_block_corrupt),
            ("lz4-frame-truncated-block2", lz4_two_block_trunc),
        ):
            for pad in (b"", b"\0"):
                raw = pad + frame_bytes
                with self.subTest(case=label, pad=len(pad)):
                    path = self.write(raw, "ramdisk")
                    pin = {
                        "partition": "system_a",
                        "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "reason": "attempt to pin multi-block zstd/lz4 with IFMA prefix",
                    }
                    try:
                        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                            self.scan()
                        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                            self.scan(unsupported_allowlist=[pin])
                    finally:
                        path.unlink(missing_ok=True)

        # Clean multi-block corrupt frame and >128 KiB 0-output frame must fail
        # closed as unpinnable ScanError (both direct and NUL-prefixed).
        z_clean = subprocess.run(
            [zstd, "-q", "--no-check", "-c"], input=self.clean,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        _, clean_blocks = parse_zstd_blocks(z_clean)
        cb_start, cb_end, _, cb_type, cb_size = clean_blocks[0]
        cb_nonlast = ((cb_size << 3) | (cb_type << 1) | 0).to_bytes(3, "little") + z_clean[cb_start + 3:cb_end]
        clean_two_block_zstd = b"\x28\xb5\x2f\xfd\x00\x58" + cb_nonlast + corrupt_zstd_last
        over_128k_zero_zstd = (
            b"\x28\xb5\x2f\xfd\x00\x58"
            + (((130 * 1024) << 3) | (2 << 1) | 1).to_bytes(3, "little")
            + b"\xff" * (130 * 1024)
        )
        over_128k_zero_zstd_trunc = b"\0" * 4 + over_128k_zero_zstd[:-10]
        over_128k_zero_lz4 = (
            b"\x04\x22\x4d\x18\x60\x70\x73"
            + (130 * 1024).to_bytes(4, "little")
            + b"\xff" * (130 * 1024)
            + b"\x00\x00\x00\x00"
        )
        for label, raw in (
            ("clean-2block-zstd", clean_two_block_zstd),
            ("over-128k-zero-zstd", over_128k_zero_zstd),
            ("over-128k-zero-zstd-padded-trunc", over_128k_zero_zstd_trunc),
            ("over-128k-zero-lz4", over_128k_zero_lz4),
        ):
            with self.subTest(unpinnable_case=label):
                path = self.write(raw, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin multi-block or >128 KiB failed zstd/lz4",
                }
                try:
                    with self.assertRaisesRegex(
                        SCANNER.ScanError,
                        r"(invalid (zstd|lz4) stream frame|(zstd|lz4) decompression failed)",
                    ):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        # V1: a failed 1-block zstd/lz4 frame is refused even with an exact
        # pin, whether the decoder writes its bytes first or writes nothing.
        for tool_name, tool_bin in (("zstd", zstd), ("lz4", lz4)):
            good_one_block = subprocess.run(
                [tool_bin, "-q", "-c"], input=self.clean,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            ).stdout
            bad_one_block = good_one_block[:-1] + bytes([good_one_block[-1] ^ 0xFF])
            # Information only: lz4 1.9.4 writes the decoded block and then
            # exits 66; lz4 1.10.0 and zstd write nothing. The verdicts below
            # are checked under BOTH behaviours via wrappers, on any host.
            probe = subprocess.run(
                [tool_bin, "-dc"], input=bad_one_block,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertNotEqual(probe.returncode, 0)
            for behaviour in ("emit-then-fail", "emit-nothing"):
                self._one_block_verdicts(
                    tool_name, tool_bin, bad_one_block, evil, behaviour,
                )

    def _failing_decoder_wrapper(self, tool_name, tool_bin, behaviour, emit):
        """Put a *tool_name* wrapper first on PATH: success passes through;
        on failure it emits *emit* then exits 66, or emits nothing and exits 34."""
        wrap = Path(self.temp.name) / f"wrap-{tool_name}-{behaviour}"
        wrap.mkdir(exist_ok=True)
        emit_file = wrap / "emit.bin"
        emit_file.write_bytes(emit)
        script = wrap / tool_name
        script.write_text(
            "#!" + sys.executable + "\n"
            "import subprocess, sys\n"
            f"real, mode, emit = {tool_bin!r}, {behaviour!r}, {str(emit_file)!r}\n"
            "p = subprocess.run([real] + sys.argv[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
            "if p.returncode == 0:\n"
            "    sys.stdout.buffer.write(p.stdout); sys.exit(0)\n"
            "if mode == 'emit-then-fail':\n"
            "    sys.stdout.buffer.write(open(emit, 'rb').read()); sys.stdout.flush()\n"
            "    sys.stderr.write('Error 66 : Decompression error : ERROR_contentChecksum_invalid\\n'); sys.exit(66)\n"
            "sys.stderr.write('Error 34 : checksum invalid\\n'); sys.exit(34)\n"
        )
        script.chmod(0o755)
        return mock.patch.dict(
            os.environ, {"PATH": f"{wrap}{os.pathsep}{os.environ.get('PATH', '')}"}
        )

    def _one_block_verdicts(self, tool_name, tool_bin, bad_one_block, evil, behaviour):
        with self._failing_decoder_wrapper(tool_name, tool_bin, behaviour, self.clean):
            self.assertEqual(SCANNER.shutil.which(tool_name),
                             str(Path(self.temp.name) / f"wrap-{tool_name}-{behaviour}" / tool_name))
            wrapped = subprocess.run(
                [tool_name, "-dc"], input=bad_one_block,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertNotEqual(wrapped.returncode, 0)
            if behaviour == "emit-then-fail":
                self.assertNotEqual(wrapped.stdout, b"")
            else:
                self.assertEqual(wrapped.stdout, b"")
            self._one_block_cases(tool_name, bad_one_block, evil, behaviour)
        if behaviour == "emit-then-fail":
            # Bytes from a failed decode are never trusted: IFMA in them is refused.
            with self._failing_decoder_wrapper(tool_name, tool_bin, behaviour, evil):
                with self.subTest(tool=tool_name, behaviour=behaviour, case="failed-decode-ifma-refused"):
                    path = self.write(bad_one_block, "ramdisk")
                    pin = {
                        "partition": "system_a",
                        "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                        "sha256": hashlib.sha256(bad_one_block).hexdigest(),
                        "reason": "exact 1-block frame at offset 0 (decoder emits IFMA, then fails)",
                    }
                    try:
                        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                            self.scan(unsupported_allowlist=[pin])
                    finally:
                        path.unlink(missing_ok=True)

    def _one_block_cases(self, tool_name, bad_one_block, evil, behaviour):
        # V1 spec change: both sub-cases used to expect the exact pin to apply.
        with self.subTest(tool=tool_name, behaviour=behaviour, case="offset0-refused-with-pin"):
            path = self.write(bad_one_block, "ramdisk")
            pin = {
                "partition": "system_a",
                "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                "sha256": hashlib.sha256(bad_one_block).hexdigest(),
                "reason": f"exact 1-block frame at offset 0 ({behaviour})",
            }
            try:
                with self.assertRaisesRegex(
                    SCANNER.ScanError, rf"{tool_name} decompression failed",
                ) as caught:
                    self.scan(unsupported_allowlist=[pin])
                self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
            finally:
                path.unlink(missing_ok=True)

        with self.subTest(tool=tool_name, behaviour=behaviour, case="padded-then-valid-member-refused-with-pin"):
            frame_pin = {
                "partition": "system_a",
                "container_chain": [
                    {"kind": "tree_file", "path": "ramdisk"},
                    {"kind": "initramfs_tail", "path": "offset:0x4"},
                ],
                "sha256": hashlib.sha256(bad_one_block).hexdigest(),
                "reason": f"exact frame pin before a valid member ({behaviour})",
            }
            for tail in (self.clean, evil):
                path = self.write(b"\0" * 4 + bad_one_block + tail, "ramdisk")
                try:
                    with self.assertRaisesRegex(
                        SCANNER.ScanError, rf"invalid {tool_name} stream frame at 0x4",
                    ) as caught:
                        self.scan(unsupported_allowlist=[frame_pin])
                    self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
                finally:
                    path.unlink(missing_ok=True)

    def test_v1_failed_modern_frame_repros_refused_with_exact_pins(self):
        """Review-1 repros: a failed modern zstd/lz4 frame never takes a pin."""
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        trailer = fixtures.newc_archive("a", b"")[-124:]
        self.assertEqual(trailer[110:120], b"TRAILER!!!")
        blocked = fixtures.newc_archive("blocked.img", b"not an image")
        self.assertTrue(blocked.endswith(trailer))
        blocked_then_evil = blocked[:-len(trailer)] + evil
        for tool_name in ("zstd", "lz4"):
            tool_bin = shutil.which(tool_name)
            if not tool_bin:
                self.skipTest(f"{tool_name} CLI unavailable")

            def bad_frame(payload):
                good = subprocess.run(
                    [tool_bin, "-q", "-c"], input=payload,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                ).stdout
                return good[:-1] + bytes([good[-1] ^ 0xFF])

            def refused(frame, regex, chain=None, data=None, name="ramdisk"):
                path = self.write(frame if data is None else data, name)
                pin = {
                    "partition": "system_a",
                    "container_chain": chain or [{"kind": "tree_file", "path": name}],
                    "sha256": hashlib.sha256(frame).hexdigest(),
                    "reason": "exact pin on a failed modern frame",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, regex) as caught:
                        self.scan(unsupported_allowlist=[pin])
                    self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
                    return str(caught.exception)
                finally:
                    path.unlink(missing_ok=True)

            # cursor: IFMA inside a checksum-failed single block; the decoder
            # writes nothing -> the block is never scanned, still refused.
            evil_frame = bad_frame(evil)
            with self._failing_decoder_wrapper(tool_name, tool_bin, "emit-nothing", b""):
                with self.subTest(tool=tool_name, repro="cursor-zero-output-ifma"):
                    refused(evil_frame, rf"{tool_name} decompression failed")
                # Same frame without IFMA: still refused (there is no pin path).
                with self.subTest(tool=tool_name, repro="cursor-zero-output-clean"):
                    refused(bad_frame(self.clean), rf"{tool_name} decompression failed")
            # Codex: the decoder writes a newc whose first member cannot be
            # opened (blocked.img) and whose IFMA ELF comes after it; the
            # diagnostic scan stops early, which must not become acceptance.
            codex_frame = bad_frame(blocked_then_evil)
            with self._failing_decoder_wrapper(tool_name, tool_bin, "emit-then-fail", blocked_then_evil):
                with self.subTest(tool=tool_name, repro="codex-top-level"):
                    refused(codex_frame, rf"{tool_name} decompression failed")
                with self.subTest(tool=tool_name, repro="codex-nested-zip-member"):
                    outer = io.BytesIO()
                    with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_STORED) as archive:
                        archive.writestr("member", codex_frame)
                    refused(
                        codex_frame, rf"{tool_name} decompression failed",
                        chain=[
                            {"kind": "tree_file", "path": "outer.zip"},
                            {"kind": "zip_member", "path": "member"},
                        ],
                        data=outer.getvalue(), name="outer.zip",
                    )
                with self.subTest(tool=tool_name, repro="codex-initramfs-tail"):
                    data = b"\0" * 4 + codex_frame
                    refused(
                        codex_frame, rf"invalid {tool_name} stream frame at 0x4",
                        chain=[
                            {"kind": "tree_file", "path": "ramdisk"},
                            {"kind": "initramfs_tail", "path": "offset:0x4"},
                        ],
                        data=data,
                    )
                with self.subTest(tool=tool_name, repro="codex-truncated-frame"):
                    good = subprocess.run(
                        [tool_bin, "-q", "-c"], input=blocked_then_evil,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                    ).stdout
                    truncated = good[:-6]
                    refused(truncated, rf"invalid {tool_name} stream frame|{tool_name} decompression failed")

    def _damaged_lzma_zip(self, size=None, evil=True):
        """ZIP [a.bin: ZIP_LZMA with corrupt data, evil: IFMA ELF (stored)]
        (+ stored filler up to *size*): reading a.bin raises lzma.LZMAError."""
        rnd = random.Random(7)
        abin = bytes(rnd.getrandbits(8) for _ in range(4000)) + b"x" * 4000
        evil_elf = (fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
                    if evil else b"plain data")

        def build(fill):
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as archive:
                archive.writestr(zipfile.ZipInfo("a.bin"), abin, compress_type=zipfile.ZIP_LZMA)
                archive.writestr(zipfile.ZipInfo("evil"), evil_elf, compress_type=zipfile.ZIP_STORED)
                if fill is not None:
                    archive.writestr(zipfile.ZipInfo("filler"), b"\x5a" * fill,
                                     compress_type=zipfile.ZIP_STORED)
            return bytearray(out.getvalue())

        raw = build(None) if size is None else build(size - len(build(0)))
        if size is not None:
            self.assertEqual(len(raw), size)
        offset = 30 + len("a.bin") + 9 + 3  # local header + name + LZMA props
        for index in range(offset, offset + 40):
            raw[index] ^= 0xA5
        raw = bytes(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            with self.assertRaises(lzma.LZMAError):
                archive.read("a.bin")
        return raw

    def _w1_refused(self, data, name, pin_chain, pinned):
        path = self.write(data, name)
        pin = {
            "partition": "system_a",
            "container_chain": pin_chain,
            "sha256": hashlib.sha256(pinned).hexdigest(),
            "reason": "exact pin on a failed modern frame",
        }
        try:
            with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA") as caught:
                self.scan(unsupported_allowlist=[pin])
            self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
        finally:
            path.unlink(missing_ok=True)

    def test_w1_damaged_zip_member_never_hides_a_later_member(self):
        """A corrupt first ZIP member is 'cannot open' but the IFMA ELF in the
        next member is still scanned, so even a pin on the ZIP is refused."""
        raw = self._damaged_lzma_zip()
        self._w1_refused(raw, "blob.zip", [{"kind": "tree_file", "path": "blob.zip"}], raw)
        # Without IFMA after it, the damaged member is still a member that
        # could not be read: refused with or without an exact pin on the ZIP
        # (gb-ifma023e E2 spec change: was "accepted with an exact pin").
        harmless = self._damaged_lzma_zip(evil=False)
        path = self.write(harmless, "blob.zip")
        try:
            with self.assertRaisesRegex(SCANNER.ScanError, r"invalid ZIP/APEX member data: a\.bin: LZMAError"):
                self.scan()
            with self.assertRaisesRegex(SCANNER.ScanError, r"invalid ZIP/APEX member data: a\.bin: LZMAError") as caught:
                self.scan(unsupported_allowlist=[{
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "blob.zip"}],
                    "sha256": hashlib.sha256(harmless).hexdigest(),
                    "reason": "damaged member, nothing after it",
                }])
            self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
        finally:
            path.unlink(missing_ok=True)
        for exc_type in (lzma.LZMAError, zlib.error, EOFError):
            with self.subTest(member_error=exc_type.__name__):
                self.assertTrue(issubclass(exc_type, SCANNER.ZIP_MEMBER_DATA_ERRORS))

    def test_w1_claude_native_zstd_lzma_zip_frame_refused_with_exact_pins(self):
        """Claude review-2 I-1, native decoder (no wrapper): 2-block zstd frame
        with a bad content checksum; block 1 = ZIP [corrupt LZMA member, IFMA
        ELF]. The decoder writes block 1 then fails; no pin may accept it."""
        zstd = shutil.which("zstd")
        if not zstd:
            self.skipTest("zstd CLI unavailable")
        raw = self._damaged_lzma_zip(131072)
        rnd = random.Random(8)
        content = raw + bytes(rnd.getrandbits(8) for _ in range(70000))
        good = subprocess.run(
            [zstd, "-q", "-c", "--check"], input=content,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        bad = good[:-1] + bytes([good[-1] ^ 0xFF])
        probe = subprocess.run([zstd, "-dc"], input=bad,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(probe.returncode, 0)
        self.assertEqual(probe.stdout[:131072], raw)
        for name in ("blob.zst", "ramdisk"):
            with self.subTest(place=name):
                self._w1_refused(bad, name, [{"kind": "tree_file", "path": name}], bad)
        with self.subTest(place="zip-member"):
            outer = io.BytesIO()
            with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("member", bad)
            self._w1_refused(
                outer.getvalue(), "outer.zip",
                [{"kind": "tree_file", "path": "outer.zip"},
                 {"kind": "zip_member", "path": "member"}],
                bad,
            )
        with self.subTest(place="nul-prefix-outer-pin"):
            data = b"\0" * 4 + bad
            self._w1_refused(data, "ramdisk", [{"kind": "tree_file", "path": "ramdisk"}], data)

    def test_w1_emit_then_fail_lzma_zip_refused_for_zstd_and_lz4(self):
        """The same ZIP written by a failing single-block frame (emit-then-fail
        wrapper): a clean ScanError for zstd and lz4, never an uncaught error."""
        raw = self._damaged_lzma_zip()
        for tool_name in ("zstd", "lz4"):
            tool_bin = shutil.which(tool_name)
            if not tool_bin:
                self.skipTest(f"{tool_name} CLI unavailable")
            good = subprocess.run(
                [tool_bin, "-q", "-c"], input=self.clean,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            ).stdout
            bad = good[:-1] + bytes([good[-1] ^ 0xFF])
            with self._failing_decoder_wrapper(tool_name, tool_bin, "emit-then-fail", raw):
                with self.subTest(tool=tool_name, place="top-level"):
                    self._w1_refused(bad, "ramdisk", [{"kind": "tree_file", "path": "ramdisk"}], bad)
                with self.subTest(tool=tool_name, place="nul-prefix-outer-pin"):
                    data = b"\0" * 4 + bad
                    self._w1_refused(data, "ramdisk", [{"kind": "tree_file", "path": "ramdisk"}], data)

    def test_w1_any_diagnostic_scan_exception_becomes_failed_frame_error(self):
        """Whatever the diagnostic scan of a failed frame raises (here an
        unmapped lzma.LZMAError / zlib.error), the result is FailedFrameError
        naming it, never a pinnable ImageOpenError."""
        for tool_name in ("zstd", "lz4"):
            tool_bin = shutil.which(tool_name)
            if not tool_bin:
                self.skipTest(f"{tool_name} CLI unavailable")
            good = subprocess.run(
                [tool_bin, "-q", "-c"], input=self.clean,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            ).stdout
            bad = good[:-1] + bytes([good[-1] ^ 0xFF])
            for exc in (lzma.LZMAError("Corrupt input data"), zlib.error("bad block")):
                with self._failing_decoder_wrapper(tool_name, tool_bin, "emit-then-fail", self.clean), \
                        mock.patch.object(SCANNER, "_scan_decoded_payload", side_effect=exc), \
                        self.subTest(tool=tool_name, error=type(exc).__name__):
                    path = self.write(bad, "ramdisk")
                    pin = {
                        "partition": "system_a",
                        "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                        "sha256": hashlib.sha256(bad).hexdigest(),
                        "reason": "exact pin on a failed modern frame",
                    }
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.FailedFrameError,
                            rf"{tool_name} decompression failed.*{type(exc).__name__}: ",
                        ):
                            self.scan(unsupported_allowlist=[pin])
                    finally:
                        path.unlink(missing_ok=True)

    def _pin(self, chain, data, reason="exact pin"):
        return {"partition": "system_a", "container_chain": chain,
                "sha256": hashlib.sha256(data).hexdigest(), "reason": reason}

    def _bad_frame(self, tool_bin, payload):
        good = subprocess.run([tool_bin, "-q", "-c"], input=payload,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        return good[:-1] + bytes([good[-1] ^ 0xFF])

    def test_w1plus_diagnostic_oserror_after_failed_frame_is_never_pinnable(self):
        """Codex review-2 I1: the bytes a failed frame wrote are a newc whose
        first member is an EROFS image whose extractor cannot start
        (FileNotFoundError from a bad shebang), then an IFMA ELF."""
        otatools = self.base / "broken-ota"
        (otatools / "bin").mkdir(parents=True)
        fsck = otatools / "bin" / "fsck.erofs"
        fsck.write_text("#!/nonexistent/interpreter\n")
        fsck.chmod(0o755)
        erofs = bytearray(4096)
        erofs[1024:1028] = b"\xe2\xe1\xf5\xe0"
        evil = fixtures.newc_archive("evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ))
        trailer = fixtures.newc_archive("a", b"")[-124:]
        emitted = fixtures.newc_archive("blocked.img", bytes(erofs))[:-len(trailer)] + evil
        for tool_name in ("zstd", "lz4"):
            tool_bin = shutil.which(tool_name)
            if not tool_bin:
                self.skipTest(f"{tool_name} CLI unavailable")
            frame = self._bad_frame(tool_bin, self.clean)
            outer = io.BytesIO()
            with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("member", frame)
            outer = outer.getvalue()
            cases = [("zip-member-frame-pin", "outer.zip", outer,
                      self._pin([{"kind": "tree_file", "path": "outer.zip"},
                                 {"kind": "zip_member", "path": "member"}], frame)),
                     ("zip-whole-pin", "outer.zip", outer,
                      self._pin([{"kind": "tree_file", "path": "outer.zip"}], outer))]
            if tool_name == "zstd":
                cases.insert(0, ("top-level-frame-pin", "blob.zst", frame,
                                 self._pin([{"kind": "tree_file", "path": "blob.zst"}], frame)))
            with self._failing_decoder_wrapper(tool_name, tool_bin, "emit-then-fail", emitted):
                for case, name, data, pin in cases:
                    with self.subTest(tool=tool_name, case=case):
                        path = self.write(data, name)
                        try:
                            with self.assertRaises(SCANNER.ScanError) as caught:
                                self.scan(unsupported_allowlist=[pin], otatools=str(otatools))
                            self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
                            self.assertRegex(str(caught.exception),
                                             rf"{tool_name} decompression failed.*(OSError|FileNotFoundError|PermissionError)")
                        finally:
                            path.unlink(missing_ok=True)

    def test_w2_missing_or_unstartable_modern_decoder_is_a_hard_refusal(self):
        """Codex review-2 I2 (decided): no zstd/lz4 decoder (PATH emptied in
        this process only, no otatool copy) or one that cannot start is an
        unpinnable ScanError for a modern frame, at top level, after NUL
        padding, inside a ZIP and inside an EROFS image."""
        for tool_name in ("zstd", "lz4"):
            tool_bin = shutil.which(tool_name)
            if not tool_bin:
                self.skipTest(f"{tool_name} CLI unavailable")
            frame = self._bad_frame(tool_bin, self.clean)
            zipped = io.BytesIO()
            with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("member", frame)
            zipped = zipped.getvalue()
            padded = b"\0" * 4 + frame
            # Codex I2: a damaged frame whose bytes after the header are all
            # NUL, after NUL padding (the boundary path of the partial helper).
            header_nuls = b"\0" * 4 + frame[:6] + b"\0" * 64
            ota = self.base / f"erofs-ota-{tool_name}"
            (ota / "bin").mkdir(parents=True)
            (ota / "bin" / "frame").write_bytes(frame)
            fsck = ota / "bin" / "fsck.erofs"
            fsck.write_text('#!/bin/sh\nout=${1#--extract=}\n/bin/mkdir -p "$out"\n'
                            '/bin/cp "${0%/*}/frame" "$out/blob.' + ("zst" if tool_name == "zstd" else "lz4") + '"\n')
            fsck.chmod(0o755)
            erofs = bytearray(4096)
            erofs[1024:1028] = b"\xe2\xe1\xf5\xe0"
            erofs = bytes(erofs)
            suffix = "zst" if tool_name == "zstd" else "lz4"
            cases = [
                ("top-level", f"blob.{suffix}", frame, [{"kind": "tree_file", "path": f"blob.{suffix}"}], frame, None),
                ("nul-padding", "ramdisk", padded, [{"kind": "tree_file", "path": "ramdisk"}], padded, None),
                ("nul-padding-header-then-nuls", "ramdisk", header_nuls,
                 [{"kind": "tree_file", "path": "ramdisk"}], header_nuls, None),
                ("zip", "outer.zip", zipped, [{"kind": "tree_file", "path": "outer.zip"},
                                              {"kind": "zip_member", "path": "member"}], frame, None),
                ("zip-whole-pin", "outer.zip", zipped, [{"kind": "tree_file", "path": "outer.zip"}], zipped, None),
                ("erofs", "blob.img", erofs, [{"kind": "tree_file", "path": "blob.img"}], erofs, str(ota)),
            ]
            with mock.patch.dict(os.environ, {"PATH": ""}):
                self.assertIsNone(SCANNER.shutil.which(tool_name))
                for case, name, data, chain, pinned, otatools in cases:
                    with self.subTest(tool=tool_name, missing=case):
                        path = self.write(data, name)
                        try:
                            with self.assertRaisesRegex(SCANNER.ScanError, rf"{tool_name} unavailable") as caught:
                                self.scan(unsupported_allowlist=[self._pin(chain, pinned)], otatools=otatools)
                            self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
                        finally:
                            path.unlink(missing_ok=True)
            broken = self.base / f"broken-{tool_name}"
            (broken / "bin").mkdir(parents=True)
            (broken / "bin" / tool_name).write_text("#!/nonexistent/interpreter\n")
            (broken / "bin" / tool_name).chmod(0o755)
            for case, name, data, chain, pinned, _ in cases[:5]:
                with self.subTest(tool=tool_name, cannot_start=case):
                    path = self.write(data, name)
                    try:
                        with self.assertRaisesRegex(SCANNER.ScanError, rf"{tool_name} decoder could not start") as caught:
                            self.scan(unsupported_allowlist=[self._pin(chain, pinned)], otatools=str(broken))
                        self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
                    finally:
                        path.unlink(missing_ok=True)

    def test_w5_failed_legacy_lz4_call_that_wrote_bytes_is_unpinnable(self):
        """Codex review-2 I3: a legacy LZ4 chunk call that writes the real
        decoded bytes (here an IFMA newc) and then fails (exit 66, or SIGTERM
        after flushing) is refused even with an exact pin; a failed call that
        wrote nothing keeps the per-chunk pin rule."""
        lz4 = shutil.which("lz4")
        if not lz4:
            self.skipTest("lz4 CLI unavailable")
        evil = fixtures.newc_archive("evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ))
        legacy = subprocess.run([lz4, "-l", "-q", "-c"], input=evil,
                                stdout=subprocess.PIPE, check=True).stdout
        self.assertEqual(legacy[:4], b"\x02\x21\x4c\x18")
        modes = {
            "forward-then-66": "sys.stdout.buffer.write(p.stdout); sys.stdout.flush(); sys.exit(66)\n",
            "forward-then-sigterm": "sys.stdout.buffer.write(p.stdout); sys.stdout.flush(); os.kill(os.getpid(), signal.SIGTERM)\n",
            "zero-output": "sys.exit(66)\n",
        }
        for mode, tail in modes.items():
            wrap = self.base / f"legacy-{mode}"
            wrap.mkdir()
            script = wrap / "lz4"
            script.write_text(
                "#!" + sys.executable + "\nimport os, signal, subprocess, sys\n"
                f"p = subprocess.run([{lz4!r}] + sys.argv[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
                + tail
            )
            script.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{wrap}{os.pathsep}{os.environ.get('PATH', '')}"}), \
                    self.subTest(mode=mode):
                path = self.write(legacy, "blob.lz4")
                pin = self._pin([{"kind": "tree_file", "path": "blob.lz4"}], legacy)
                try:
                    if mode == "zero-output":
                        report = self.scan(unsupported_allowlist=[pin])
                        self.assertEqual(report["hits"], [])
                        self.assertEqual(len(report["accepted_unsupported"]), 1)
                    else:
                        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA") as caught:
                            self.scan(unsupported_allowlist=[pin])
                        self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
                finally:
                    path.unlink(missing_ok=True)

    def test_gzip_over_64kib_without_trailer_accepts_clean_data_and_refuses_ifma(self):
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        for length in (65535, 65536, 65537, 70000):
            with self.subTest(length=length, kind="clean"):
                raw_clean = gzip.compress(b"A" * length)[:-8]
                path = self.write(raw_clean, "notice.txt.gz")
                try:
                    events = []
                    report = self.scan(coverage_callback=events.append)
                    self.assertEqual(report["hits"], [])
                    self.assertEqual(report["unsupported_images"], [])
                    self.assertEqual(events[-1]["classification"], "data")
                finally:
                    path.unlink(missing_ok=True)

            with self.subTest(length=length, kind="ifma"):
                padded_evil = evil + b"\0" * (length - len(evil))
                raw_evil = gzip.compress(padded_evil)[:-8]
                path = self.write(raw_evil, "ramdisk.gz")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk.gz"}],
                    "sha256": hashlib.sha256(raw_evil).hexdigest(),
                    "reason": "attempt to pin >64 KiB gzip without trailer",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan()
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        # Verify flushed tail bytes at EOF (>64 KiB boundary) are written to
        # decoded_output so a 65537-byte ELF payload is not truncated.
        clean_65537_elf = bytearray(fixtures.large_executable_elf(65537 - 0x100, 0))
        clean_65537_elf[0x100:0x105] = b"\x90" * 5
        raw_elf_gz = gzip.compress(bytes(clean_65537_elf))[:-8]
        path = self.write(raw_elf_gz, "mod.ko.gz")
        try:
            report = self.scan()
            self.assertEqual(report["hits"], [])
            self.assertEqual(report["unsupported_images"], [])
        finally:
            path.unlink(missing_ok=True)

    def test_r10_p1_legacy_lz4_continuation_empty_and_compressbound(self):
        lz4 = shutil.which("lz4")
        if not lz4:
            self.skipTest("lz4 CLI unavailable")
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )

        def lz4_legacy(payload):
            return subprocess.run(
                [lz4, "-q", "-l", "-c"],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout

        empty_legacy = lz4_legacy(b"")
        self.assertEqual(empty_legacy, b"\x02\x21\x4c\x18")
        evil_legacy = lz4_legacy(evil)
        clean_legacy = lz4_legacy(self.clean)

        # Codex split cpio (k = 1..5) and same-stream split, with/without NUL,
        # named ramdisk and ramdisk.lz4 -> refused with IFMA hit.
        for split in range(1, 6):
            part_a = lz4_legacy(evil[:split])
            part_b = lz4_legacy(evil[split:])
            variants = (
                (("concat", part_a + part_b), ("same-stream", part_a + part_b[4:]))
                if split in (1, 5)
                else (("concat", part_a + part_b),)
            )
            for variant, combined in variants:
                for pad in (b"", b"\0"):
                    fnames = ("ramdisk", "ramdisk.lz4") if split in (1, 5) else ("ramdisk",)
                    for fname in fnames:
                        raw = pad + combined
                        with self.subTest(split=split, variant=variant, pad=len(pad), fname=fname):
                            path = self.write(raw, fname)
                            try:
                                with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                                    self.scan()
                            finally:
                                path.unlink(missing_ok=True)

        # Clean split cpio and clean lz4l(b"") + lz4l(clean) -> accepted.
        for label, clean_stream in (
            ("empty-plus-clean", empty_legacy + clean_legacy),
            ("split-clean-1", lz4_legacy(self.clean[:1]) + lz4_legacy(self.clean[1:])),
            ("split-clean-5", lz4_legacy(self.clean[:5]) + lz4_legacy(self.clean[5:])),
        ):
            for pad in (b"", b"\0"):
                for fname in ("ramdisk", "ramdisk.lz4", "notice.bin.lz4"):
                    if pad and fname.endswith(".lz4"):
                        continue
                    with self.subTest(clean_case=label, pad=len(pad), fname=fname):
                        path = self.write(pad + clean_stream, fname)
                        try:
                            events = []
                            report = self.scan(coverage_callback=events.append)
                            self.assertEqual(report["hits"], [])
                            self.assertEqual(report["unsupported_images"], [])
                            if fname == "notice.bin.lz4":
                                self.assertEqual(events[-1]["classification"], "data")
                        finally:
                            path.unlink(missing_ok=True)

        # Empty legacy stream (4-byte magic, or magic + 0, or magic + <4B NULs) -> data;
        # magic + <4B non-NUL -> member ends at 4 and non-NUL tail is refused.
        for label, empty_raw in (
            ("magic-only", empty_legacy),
            ("magic-zero-word", empty_legacy + b"\x00\x00\x00\x00"),
            ("magic-short-nul-frag", empty_legacy + b"\x00\x00\x00"),
        ):
            with self.subTest(empty_case=label):
                path = self.write(empty_raw, "ramdisk.lz4")
                try:
                    events = []
                    report = self.scan(coverage_callback=events.append)
                    self.assertEqual(report["hits"], [])
                    self.assertEqual(report["unsupported_images"], [])
                    self.assertEqual(events[-1]["classification"], "data")
                finally:
                    path.unlink(missing_ok=True)

        # Claude I1 cases with exact outer pin -> refused with IFMA hit.
        for label, raw in (
            ("nul-empty-plus-evil", b"\0" + empty_legacy + evil_legacy),
            ("offset0-empty-plus-evil", empty_legacy + evil_legacy),
            ("nul-magic-zero-gzip-evil", b"\0" + empty_legacy + b"\0" * 4 + gzip.compress(evil, mtime=0)),
            ("offset0-magic-zero-gzip-evil", empty_legacy + b"\0" * 4 + gzip.compress(evil, mtime=0)),
            ("nul-magic-zero-pad-newc-evil", b"\0" + empty_legacy + b"\0" * 8 + evil),
        ):
            with self.subTest(i1_case=label):
                path = self.write(raw, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin I1 legacy LZ4 sequence",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan()
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        # 9 MiB incompressible legacy stream (first chunk ~8,421,4xx bytes <= compressBound)
        # -> data when clean, and refused with IFMA hit when EVIL is behind it.
        rnd = random.Random(7)
        rand9 = rnd.randbytes(9 * 1024 * 1024)
        clean_9m_cpio = fixtures.newc_archive("blob", rand9)
        big_clean = lz4_legacy(clean_9m_cpio)
        first_chunk_size = struct.unpack_from("<I", big_clean, 4)[0]
        self.assertGreater(first_chunk_size, 8 * 1024 * 1024)
        self.assertLessEqual(first_chunk_size, 8421520)

        path = self.write(big_clean, "notice.bin.lz4")
        try:
            events = []
            report = self.scan(coverage_callback=events.append)
            self.assertEqual(report["hits"], [])
            self.assertEqual(report["unsupported_images"], [])
            self.assertEqual(events[-1]["classification"], "data")
        finally:
            path.unlink(missing_ok=True)

        big_evil = lz4_legacy(clean_9m_cpio + evil)
        for fname, raw in (("ramdisk.lz4", big_evil), ("ramdisk", b"\0" + big_evil)):
            with self.subTest(big_evil_fname=fname):
                path = self.write(raw, fname)
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": fname}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin >8MiB compressBound stream with IFMA",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        # Chunk size > 8,421,520 and not the magic -> scan output so far, then unpinnable ScanError.
        gt_bound = empty_legacy + (8421521).to_bytes(4, "little") + b"\0" * 64
        for pad in (b"", b"\0"):
            raw = pad + gt_bound
            with self.subTest(gt_bound_pad=len(pad)):
                path = self.write(raw, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin >compressBound legacy lz4 chunk",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "invalid lz4 stream frame"):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        evil_then_gt_bound = evil_legacy + (8421521).to_bytes(4, "little") + b"\0" * 64
        path = self.write(evil_then_gt_bound, "ramdisk")
        try:
            with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                self.scan()
        finally:
            path.unlink(missing_ok=True)

        # Defense in depth: a legacy LZ4 that fails with 0 output and is followed by any
        # non-NUL byte -> unpinnable even with exact outer or frame-level pin.
        bad_legacy_one_chunk = empty_legacy + (4).to_bytes(4, "little") + b"\xff\xff\xff\xff"
        for label, raw, chain in (
            (
                "offset0-corrupt-chunk-then-clean",
                bad_legacy_one_chunk + self.clean,
                [{"kind": "tree_file", "path": "ramdisk"}],
            ),
            (
                "padded-corrupt-chunk-then-clean-frame-pin",
                b"\0" * 4 + bad_legacy_one_chunk + self.clean,
                [
                    {"kind": "tree_file", "path": "ramdisk"},
                    {"kind": "initramfs_tail", "path": "offset:0x4"},
                ],
            ),
            (
                "padded-truncated-chunk-then-non-nul-outer-pin",
                b"\0" * 4 + empty_legacy + (8).to_bytes(4, "little") + b"XY",
                [{"kind": "tree_file", "path": "ramdisk"}],
            ),
        ):
            with self.subTest(defense_in_depth=label):
                path = self.write(raw, "ramdisk")
                pinned_bytes = bad_legacy_one_chunk if "frame-pin" in label else raw
                pin = {
                    "partition": "system_a",
                    "container_chain": chain,
                    "sha256": hashlib.sha256(pinned_bytes).hexdigest(),
                    "reason": "attempt to pin 0-output legacy lz4 followed by non-NUL",
                }
                try:
                    with self.assertRaisesRegex(
                        SCANNER.ScanError,
                        r"(invalid lz4 stream frame|lz4 decompression failed)",
                    ):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

    def test_r10_p2_zstd_and_lz4_longest_prefix_salvage(self):
        zstd = shutil.which("zstd")
        if not zstd:
            self.skipTest("zstd CLI unavailable")
        evil_elf = fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        evil_cpio = fixtures.newc_archive("evil", evil_elf)

        # Argon's 4-block zstd frame: [128 KiB raw block, valid compressed cpio block,
        # corrupt non-final block, corrupt final block] -> decoded size == 131072 + len(cpio).
        b1_hdr = (131072 << 3).to_bytes(3, "little")
        b1_body = b"\0" * 131072
        single = subprocess.run(
            [zstd, "-1", "--no-check", "-c"],
            input=evil_cpio,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
        desc = single[4]
        single_seg = bool(desc & 0x20)
        fcs = (1 if single_seg else 0, 2, 4, 8)[desc >> 6]
        did = (0, 1, 2, 4)[desc & 3]
        hdr_end = 4 + 1 + (0 if single_seg else 1) + did + fcs
        bh_val = int.from_bytes(single[hdr_end : hdr_end + 3], "little")
        bsize = bh_val >> 3
        b2_hdr = (bh_val & ~1).to_bytes(3, "little")
        b2_body = single[hdr_end + 3 : hdr_end + 3 + bsize]
        b3_hdr = ((64 << 3) | (2 << 1) | 0).to_bytes(3, "little")
        b3_body = b"\xff" * 64
        b4_hdr = ((64 << 3) | (2 << 1) | 1).to_bytes(3, "little")
        b4_body = b"\xff" * 64
        argon_frame = (
            b"\x28\xb5\x2f\xfd\x00\x38"
            + b1_hdr + b1_body
            + b2_hdr + b2_body
            + b3_hdr + b3_body
            + b4_hdr + b4_body
        )

        src = self.base / "argon4.zst"
        dst = self.base / "argon4.out"
        src.write_bytes(argon_frame)
        dst.write_bytes(b"")
        rc, _, unpinnable = SCANNER._decode_compressed_cli_stream(
            src, dst, "zstd", zstd, None, {"scratch": self.base, "otatools": None}
        )
        self.assertNotEqual(rc, 0)
        self.assertTrue(unpinnable)
        self.assertEqual(dst.stat().st_size, 131072 + len(evil_cpio))

        for pad in (b"", b"\0"):
            raw = pad + argon_frame
            path = self.write(raw, "ramdisk")
            pin = {
                "partition": "system_a",
                "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                "sha256": hashlib.sha256(raw).hexdigest(),
                "reason": "attempt to pin Argon 4-block zstd frame",
            }
            try:
                with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                    self.scan(unsupported_allowlist=[pin])
            finally:
                path.unlink(missing_ok=True)

        # Claude's probe_zstd_gap construction (zstd -3 --target-compressed-block-size=3000,
        # newc a.txt 493,568 B + IFMA ELF + b.txt 900 KiB, block 63 corrupted).
        rnd = random.Random(3)
        words = [
            bytes(rnd.choice(b"abcdefghijklmnop") for _ in range(rnd.randint(3, 9)))
            for _ in range(4000)
        ]

        def filler(n):
            out = bytearray()
            while len(out) < n:
                out += rnd.choice(words) + b" "
            return bytes(out[:n])

        def newc_member(name, payload, mode=0o100644):
            nb = name.encode() + b"\0"
            fields = (1, mode, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(nb), 0)
            hdr = b"070701" + b"".join(f"{v:08x}".encode() for v in fields)
            return (
                hdr
                + nb
                + b"\0" * ((-(len(hdr) + len(nb))) % 4)
                + payload
                + b"\0" * ((-len(payload)) % 4)
            )

        # Advance RNG through trial 0 to reach trial 1 (x == 493,568).
        x0 = rnd.randint(200, 1400) * 1024
        _ = filler(x0)
        _ = filler(900 * 1024)
        x1 = rnd.randint(200, 1400) * 1024
        self.assertEqual(x1, 493568)
        gap_archive = (
            newc_member("a.txt", filler(x1))
            + newc_member("evil", evil_elf)
            + newc_member("b.txt", filler(900 * 1024))
            + newc_member("TRAILER!!!", b"", 0)
        )
        gap_frame = subprocess.run(
            [zstd, "-q", "-3", "--no-check", "--target-compressed-block-size=3000", "-c"],
            input=gap_archive,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
        pos = 4
        desc = gap_frame[pos]
        pos += 1
        single_seg = bool(desc & 0x20)
        if not single_seg:
            pos += 1
        pos += (0, 1, 2, 4)[desc & 3] + (1 if single_seg else 0, 2, 4, 8)[desc >> 6]
        blocks = []
        while True:
            st = pos
            v = int.from_bytes(gap_frame[pos : pos + 3], "little")
            pos += 3
            last, bt, bs = v & 1, (v >> 1) & 3, v >> 3
            stored = 1 if bt == 1 else bs
            blocks.append((st, pos + stored, last, bt, bs))
            pos += stored
            if last:
                break
        st63, en63, _, _, _ = blocks[63]
        bad_gap = bytearray(gap_frame)
        for idx in range(st63 + 3, min(en63, st63 + 3 + 48)):
            bad_gap[idx] ^= 0x5A
        bad_gap = bytes(bad_gap)

        for pad in (b"", b"\0"):
            raw = pad + bad_gap
            with self.subTest(gap_pad=len(pad)):
                path = self.write(raw, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin probe_zstd_gap corrupt block 63",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan()
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

    def test_r10_p3_length_none_at_nonzero_offset_requires_all_nul_tail(self):
        evil_elf = fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        evil = fixtures.newc_archive("evil", evil_elf)
        comp = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
        raw_deflate = comp.compress(evil) + comp.flush()

        cases = (
            ("doc-gzip-junk", b"\0\x1f\x8bnot-a-valid-gzipJUNKJUNK"),
            ("gzip-not-valid", b"\0\x1f\x8bnot-a-valid-gzip"),
            ("xz-not-valid", b"\0\xfd7zXZ\0not-a-valid-xz"),
            ("zstd-not-valid", b"\0\x28\xb5\x2f\xfdnot-a-valid-zstd"),
            ("lz4f-not-valid", b"\0\x04\x22\x4d\x18not-a-valid-lz4"),
            ("lz4l-not-valid", b"\0\x02\x21\x4c\x18not-a-valid-lz4"),
            ("bad-method-garbage", b"\0\x1f\x8bgarbage"),
            ("encrypted-flag", b"\0\x1f\x8b\x08\x20\x00\x00\x00\x00\x00\x03" + raw_deflate),
            ("valid-header-corrupt-deflate", b"\0\x1f\x8b\x08\x00\x00\x00\x00\x00\x03\xff\xff\xff\xff"),
        )
        for label, raw in cases:
            with self.subTest(case=label):
                path = self.write(raw, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin length-None frame with non-NUL tail",
                }
                try:
                    with self.assertRaisesRegex(
                        SCANNER.ScanError,
                        r"invalid (gzip|xz|zstd|lz4) stream frame at 0x1",
                    ):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

    def test_r10_p4_stream_decompressor_stderr_drain_timeout_and_batch_lz4(self):
        # 1. Codex probe-pipes case: 256 KiB stderr before 2 stdout bytes finishes with stdout bytes.
        out_path = self.base / "pipe-stdout.bin"
        cmd_stderr_flood = [
            sys.executable,
            "-c",
            'import os; os.write(2, b"x" * 262144); os.write(1, b"ok")',
        ]
        rc, stderr_tail = SCANNER._run_stream_decompressor(
            cmd_stderr_flood, out_path, timeout=2.0
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out_path.read_bytes(), b"ok")
        self.assertEqual(len(stderr_tail), SCANNER.STREAM_STDERR_TAIL_LIMIT)
        self.assertEqual(stderr_tail, b"x" * SCANNER.STREAM_STDERR_TAIL_LIMIT)

        # 2. Hanging child hits the timeout, is killed and reaped, and raises ScanError.
        hang_out = self.base / "pipe-hang.bin"
        pid_file = self.base / "hang.pid"
        cmd_hang = [
            "/bin/sh",
            "-c",
            f"echo $$ > {str(pid_file)!r}; exec sleep 2",
        ]
        t0 = time.monotonic()
        with self.assertRaisesRegex(SCANNER.ScanError, "stream decompressor timed out"):
            SCANNER._run_stream_decompressor(cmd_hang, hang_out, timeout=0.2)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.5)
        if pid_file.exists():
            child_pid = int(pid_file.read_text().strip())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

        # 3. Claude probe_chunks: 1,000 one-literal chunks decoded in one batched lz4 -dc call.
        lz4 = shutil.which("lz4")
        if lz4:
            many_chunks = b"\x02\x21\x4c\x18" + (b"\x02\x00\x00\x00\x10A" * 1000)
            path = self.write(many_chunks, "chunks.txt.lz4")
            try:
                t_start = time.monotonic()
                events = []
                report = self.scan(coverage_callback=events.append)
                dt = time.monotonic() - t_start
                self.assertEqual(report["hits"], [])
                self.assertEqual(events[-1]["classification"], "data")
                self.assertLess(dt, 3.0)
            finally:
                path.unlink(missing_ok=True)

    def test_r10_p7_initramfs_non_reset_decoded_member_is_unpinnable(self):
        lz4 = shutil.which("lz4")
        if not lz4:
            self.skipTest("lz4 CLI unavailable")
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )

        def lz4_legacy(payload):
            return subprocess.run(
                [lz4, "-q", "-l", "-c"],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout

        # Vendor ramdisk table whose entry 1 = lz4l(payload[:1]) and entry 2 = lz4l(payload[1:])
        # where payload = newc with an IFMA ELF -> refused as unpinnable ScanError.
        for label, payload in (("evil-split", evil), ("clean-split", self.clean)):
            entry1 = lz4_legacy(payload[:1])
            entry2 = lz4_legacy(payload[1:])
            for idx, entry_bytes in ((0, entry1), (1, entry2)):
                table_dir = self.base / f"{label}-table-{idx}"
                table_dir.mkdir(parents=True, exist_ok=True)
                entry_name = f"{idx}:ramdisk_{idx}"
                (table_dir / entry_name).write_bytes(entry_bytes)
                pin = {
                    "partition": "vendor_boot_a",
                    "container_chain": [
                        {"kind": "vendor_ramdisk_entry", "path": entry_name},
                    ],
                    "sha256": hashlib.sha256(entry_bytes).hexdigest(),
                    "reason": "attempt to pin split vendor_ramdisk_entry",
                }
                with self.subTest(table_case=label, entry=idx):
                    with self.assertRaisesRegex(
                        SCANNER.ScanError,
                        "unsupported non-newc initramfs member",
                    ):
                        SCANNER.scan_tree(
                            table_dir,
                            provenance={"partition": "vendor_boot_a"},
                            root_kind="vendor_ramdisk_entry",
                            unsupported_allowlist=[pin],
                        )

        # In initramfs tree file context (ramdisk), a compressed member whose decoded
        # output is raw data or a truncated cpio without TRAILER!!! is refused unpinnable,
        # while reporting any IFMA hits in the sequence.
        non_cpio_gz = gzip.compress(b"not a newc archive at all\n" * 16)
        truncated_cpio_gz = gzip.compress(self.clean[:-20])
        for label, raw in (
            ("non-cpio-gz", non_cpio_gz),
            ("truncated-cpio-gz", truncated_cpio_gz),
            ("clean-then-non-cpio-gz", self.clean + non_cpio_gz),
        ):
            with self.subTest(initramfs_non_reset=label):
                path = self.write(raw, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin non-Reset initramfs member",
                }
                try:
                    with self.assertRaisesRegex(
                        SCANNER.ScanError,
                        "unsupported non-newc initramfs member",
                    ):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        # If a non-Reset member is followed by an IFMA member (or contains an IFMA ELF),
        # the IFMA hit is reported!
        non_cpio_plus_evil = non_cpio_gz + gzip.compress(evil)
        path = self.write(non_cpio_plus_evil, "ramdisk")
        try:
            with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                self.scan()
        finally:
            path.unlink(missing_ok=True)

    def test_r11_q1_q8_initramfs_member_scope_and_nested_files(self):
        lz4 = shutil.which("lz4")
        zstd = shutil.which("zstd")
        if not lz4 or not zstd:
            self.skipTest("lz4 or zstd CLI unavailable")
        clean_elf = fixtures.elf_with_segments(b"\x90" * 5)
        evil_elf = fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        fw_bytes = bytes((idx * 73) & 0xFF for idx in range(1024))
        zstd_fw = subprocess.run(
            [zstd, "-q", "-c"], input=fw_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        zstd_evil = subprocess.run(
            [zstd, "-q", "-c"], input=evil_elf,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        lz4l_fw = subprocess.run(
            [lz4, "-q", "-l", "-c"], input=fw_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        lz4l_evil = subprocess.run(
            [lz4, "-q", "-l", "-c"], input=evil_elf,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout

        def lz4l_compress(data):
            return subprocess.run(
                [lz4, "-q", "-l", "-c"], input=data,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            ).stdout

        inner_cases = (
            ("ko-xz", "lib/modules/foo.ko.xz", lzma.compress(clean_elf), lzma.compress(evil_elf)),
            ("ko-gz", "lib/modules/foo.ko.gz", gzip.compress(clean_elf, mtime=0), gzip.compress(evil_elf, mtime=0)),
            ("fw-zst", "lib/firmware/a.bin.zst", zstd_fw, zstd_evil),
            ("txt-gz", "etc/notice.txt.gz", gzip.compress(b"ordinary config\n" * 16, mtime=0), gzip.compress(evil_elf, mtime=0)),
            ("config-txt-gz", "etc/config.txt.gz", gzip.compress(b"hello=1\n" * 64, mtime=0), gzip.compress(evil_elf, mtime=0)),
            ("fw-lz4l", "lib/firmware/b.bin.lz4", lz4l_fw, lz4l_evil),
        )
        outer_builders = (
            ("raw-tree", "ramdisk.img", lambda b: b, "system_a", "tree_file"),
            ("gz-tree", "ramdisk", lambda b: gzip.compress(b, mtime=0), "system_a", "tree_file"),
            ("raw-vendor", "0:ramdisk_0", lambda b: b, "vendor_boot_a", "vendor_ramdisk_entry"),
            ("gz-boot", "0:ramdisk_0", lambda b: gzip.compress(b, mtime=0), "standalone_initrd_a", "boot_ramdisk"),
            ("lz4l-boot", "0:ramdisk_0", lz4l_compress, "standalone_initrd_a", "boot_ramdisk"),
        )
        for label, member_name, clean_member, evil_member in inner_cases:
            clean_cpio = fixtures.newc_archive(member_name, clean_member)
            evil_cpio = fixtures.newc_archive(member_name, evil_member)
            for outer_tag, outer_name, wrap_fn, part, r_kind in outer_builders:
                clean_outer = wrap_fn(clean_cpio)
                evil_outer = wrap_fn(evil_cpio)
                with self.subTest(inner=label, outer=outer_tag, part=part, r_kind=r_kind):
                    tree_dir = self.base / f"q1-{label}-{outer_tag}-{part}-{r_kind}"
                    tree_dir.mkdir(parents=True, exist_ok=True)
                    target = tree_dir / outer_name
                    try:
                        target.write_bytes(clean_outer)
                        report = SCANNER.scan_tree(
                            tree_dir,
                            provenance={"partition": part},
                            root_kind=r_kind,
                        )
                        self.assertEqual(report["hits"], [])
                        self.assertEqual(report["unsupported_images"], [])

                        # IFMA control for every clean row: refused WITH the hit, with and without a pin.
                        target.write_bytes(evil_outer)
                        pin = {
                            "partition": part,
                            "container_chain": [{"kind": r_kind, "path": outer_name}],
                            "sha256": hashlib.sha256(evil_outer).hexdigest(),
                            "reason": "attempt to pin Q1 nested IFMA member",
                        }
                        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                            SCANNER.scan_tree(
                                tree_dir,
                                provenance={"partition": part},
                                root_kind=r_kind,
                            )
                        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                            SCANNER.scan_tree(
                                tree_dir,
                                provenance={"partition": part},
                                root_kind=r_kind,
                                unsupported_allowlist=[pin],
                            )
                    finally:
                        shutil.rmtree(tree_dir, ignore_errors=True)

        # Compressed kernel images on boot_a, init_boot_a, vendor_boot_a, standalone_initrd_a, system_a
        # are not initramfs archives and must pass when clean and fail on IFMA (with and without a pin).
        gz_clean_kernel = gzip.compress(clean_elf, mtime=0)
        gz_evil_kernel = gzip.compress(evil_elf, mtime=0)
        lz4l_clean_kernel = lz4l_compress(clean_elf)
        lz4l_evil_kernel = lz4l_compress(evil_elf)
        kernel_rows = (
            ("kernel-gz", "kernel", gz_clean_kernel, gz_evil_kernel),
            ("image-gz", "Image.gz", gz_clean_kernel, gz_evil_kernel),
            ("kernel-lz4l", "kernel", lz4l_clean_kernel, lz4l_evil_kernel),
        )
        for part in ("boot_a", "init_boot_a", "vendor_boot_a", "standalone_initrd_a", "system_a"):
            for ktag, kname, kclean, kevil in kernel_rows:
                with self.subTest(kernel_part=part, kernel_case=ktag):
                    kdir = self.base / f"q1-kernel-{part}-{ktag}"
                    kdir.mkdir(parents=True, exist_ok=True)
                    try:
                        (kdir / kname).write_bytes(kclean)
                        rep = SCANNER.scan_tree(kdir, provenance={"partition": part})
                        self.assertEqual(rep["hits"], [])
                        self.assertEqual(rep["unsupported_images"], [])

                        (kdir / kname).write_bytes(kevil)
                        pin = {
                            "partition": part,
                            "container_chain": [{"kind": "tree_file", "path": kname}],
                            "sha256": hashlib.sha256(kevil).hexdigest(),
                            "reason": "attempt to pin Q1 IFMA kernel",
                        }
                        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                            SCANNER.scan_tree(kdir, provenance={"partition": part})
                        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                            SCANNER.scan_tree(
                                kdir,
                                provenance={"partition": part},
                                unsupported_allowlist=[pin],
                            )
                    finally:
                        shutil.rmtree(kdir, ignore_errors=True)

        # Direct initramfs-named file with non-Reset gzip text is still refused.
        path = self.write(gzip.compress(b"hello\n", mtime=0), "initrd_notes.txt.gz")
        try:
            with self.assertRaisesRegex(
                SCANNER.ScanError, "unsupported non-newc initramfs member"
            ):
                self.scan()
        finally:
            path.unlink(missing_ok=True)

    def test_r11_q2_q8_kernel_reset_states_and_inner_bootconfig(self):
        lz4 = shutil.which("lz4")
        zstd = shutil.which("zstd")
        if not lz4 or not zstd:
            self.skipTest("lz4 or zstd CLI unavailable")
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        no_trailer_clean = self.clean[: self.clean.index(b"070701", 10)]
        no_trailer_evil = evil[: evil.index(b"070701", 10)]
        empty_zstd = subprocess.run(
            [zstd, "-q", "-c"], input=b"",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        empty_lz4f = subprocess.run(
            [lz4, "-q", "-c"], input=b"",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        empty_lz4l = subprocess.run(
            [lz4, "-q", "-l", "-c"], input=b"",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout

        # Valid kernel Reset states: NUL-only member, 0-byte member, trailerless complete cpio,
        # and 4-byte-aligned concatenated newc archives inside one decoded stream.
        valid_reset_cases = (
            ("nul-only-then-cpio", gzip.compress(b"\0" * 512, mtime=0) + gzip.compress(self.clean, mtime=0)),
            ("empty-gz-then-cpio", gzip.compress(b"", mtime=0) + gzip.compress(self.clean, mtime=0)),
            ("empty-zstd-then-cpio", empty_zstd + gzip.compress(self.clean, mtime=0)),
            ("empty-lz4f-alone", empty_lz4f),
            ("empty-lz4f-then-cpio", empty_lz4f + gzip.compress(self.clean, mtime=0)),
            ("empty-lz4l-alone", empty_lz4l),
            ("empty-lz4l-zero-chunk-then-cpio", empty_lz4l + b"\x00\x00\x00\x00" + gzip.compress(self.clean, mtime=0)),
            ("padded-empty-gz-then-cpio", b"\0" * 4 + gzip.compress(b"", mtime=0) + gzip.compress(self.clean, mtime=0)),
            ("cpio-then-gz-no-trailer", self.clean + gzip.compress(no_trailer_clean, mtime=0)),
            ("plain-no-trailer-then-gz", no_trailer_clean + gzip.compress(self.clean, mtime=0)),
            ("plain-no-trailer-then-nul-then-gz", no_trailer_clean + b"\0" * 4 + gzip.compress(self.clean, mtime=0)),
            ("aligned-nul-gap-inside-gz", gzip.compress(self.clean + b"\0" * 4 + self.clean, mtime=0)),
        )
        for label, raw in valid_reset_cases:
            with self.subTest(valid_reset=label):
                path = self.write(raw, "ramdisk.img")
                try:
                    report = self.scan()
                    self.assertEqual(report["hits"], [])
                    self.assertEqual(report["unsupported_images"], [])
                finally:
                    path.unlink(missing_ok=True)

        # Trailerless cpio containing an IFMA ELF is scanned and refused with forbidden VEX IFMA.
        for label, raw in (
            ("plain-no-trailer-evil", no_trailer_evil),
            ("gz-no-trailer-evil", gzip.compress(no_trailer_evil, mtime=0)),
        ):
            with self.subTest(evil_no_trailer=label):
                path = self.write(raw, "ramdisk")
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan()
                finally:
                    path.unlink(missing_ok=True)

        # Non-Reset states that must remain unpinnable:
        # 1. Misaligned NUL gap (2 NUL bytes -> position % 4 == 2) between archives inside a member.
        # 2. Entry without 4-byte trailing padding before EOF.
        # 3. Valid #BOOTCONFIG\n footer inside a decompressed payload (must not be stripped).
        # 4. TRAILER!!! entry with non-zero filesize/body (scanner is stricter than kernel SkipIt).
        # 5. Trailerless cpio followed by non-NUL non-magic junk.
        unpadded_entry = no_trailer_clean[:-3]
        nonempty_trailer_fields = [0, 0, 0, 0, 1, 0, 4, 0, 0, 0, 0, 11, 0]
        nonempty_trailer_header = b"070701" + b"".join(
            f"{val:08x}".encode("ascii") for val in nonempty_trailer_fields
        )
        # namesize 11 is TRAILER!!! plus one NUL. (110 + 11) % 4 == 1, so three
        # padding bytes precede the 4-byte body. A short name field truncates
        # the member before the trailer-body check runs.
        nonempty_trailer_cpio = (
            no_trailer_clean
            + nonempty_trailer_header
            + b"TRAILER!!!\x00"
            + b"\x00" * 3
            + b"ABCD"
        )
        bc_body = b"androidboot.mode=normal\n"
        bc_footer = (
            bc_body
            + struct.pack("<II", len(bc_body), sum(bc_body) & 0xFFFFFFFF)
            + SCANNER.BOOTCONFIG_MAGIC
        )
        invalid_reset_cases = (
            ("misaligned-2nul-gap", gzip.compress(self.clean + b"\0\0" + self.clean, mtime=0)),
            ("unpadded-entry-eof", gzip.compress(unpadded_entry, mtime=0)),
            ("inner-bootconfig-on-junk", gzip.compress(b"not-a-cpio" + bc_footer, mtime=0)),
            ("inner-bootconfig-on-cpio", gzip.compress(self.clean + bc_footer, mtime=0)),
            ("nonempty-trailer-body", nonempty_trailer_cpio),
            ("plain-no-trailer-then-junk", no_trailer_clean + b"JUNK"),
        )
        for label, raw in invalid_reset_cases:
            with self.subTest(invalid_reset=label):
                path = self.write(raw, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin non-Reset member",
                }
                if label == "nonempty-trailer-body":
                    # Exactly two plain-newc refusal points share
                    # "nonempty newc trailer": _cpio_archive_length
                    # (scan-ifma.py:1534) and _scan_cpio (scan-ifma.py:2471).
                    # Plain newc end-to-end calls _cpio_archive_length first.
                    # On ImageOpenError, _scan_initramfs_sequence also calls
                    # _scan_cpio, swallows that exception, and re-raises the
                    # archive-length error, so that end-to-end string cannot
                    # tell the two raises apart. The direct
                    # _cpio_archive_length call reaches only that function.
                    # The direct _scan_cpio call uses a trailer-only archive
                    # whose first member is TRAILER!!!, so it reaches only the
                    # _scan_cpio raise and never calls _cpio_archive_length.
                    # The same bytes inside gzip hit _decoded_is_cpio_reset_sequence
                    # first, which returns False; the scan then reports the
                    # Reset error instead of "nonempty newc trailer".
                    trailer_file = self.base / "nonempty-trailer.bin"
                    trailer_file.write_bytes(raw)
                    self.assertFalse(
                        SCANNER._decoded_is_cpio_reset_sequence(
                            trailer_file, "nonempty-trailer",
                        )
                    )
                    with trailer_file.open("rb") as stream:
                        with self.assertRaisesRegex(
                            SCANNER.ImageOpenError, "nonempty newc trailer",
                        ):
                            SCANNER._cpio_archive_length(
                                stream, 0, len(raw), "nonempty-trailer",
                            )
                    trailer_only = (
                        nonempty_trailer_header
                        + b"TRAILER!!!\x00"
                        + b"\x00" * 3
                        + b"ABCD"
                    )
                    trailer_only_path = self.base / "trailer-only-scan-cpio.bin"
                    trailer_only_path.write_bytes(trailer_only)
                    with self.assertRaisesRegex(
                        SCANNER.ImageOpenError, "nonempty newc trailer",
                    ):
                        SCANNER._scan_cpio(
                            trailer_only_path,
                            "trailer-only",
                            (),
                            {"report": {"nested_opened": []}},
                            0,
                        )
                    gz = gzip.compress(raw, mtime=0)
                    path.unlink()
                    gz_path = self.write(gz, "ramdisk.gz")
                    gz_pin = {
                        "partition": "system_a",
                        "container_chain": [{"kind": "tree_file", "path": "ramdisk.gz"}],
                        "sha256": hashlib.sha256(gz).hexdigest(),
                        "reason": "attempt to pin gzip nonempty trailer",
                    }
                    try:
                        with self.assertRaisesRegex(
                            SCANNER.ScanError,
                            "unsupported non-newc initramfs member",
                        ):
                            self.scan(unsupported_allowlist=[gz_pin])
                    finally:
                        gz_path.unlink(missing_ok=True)
                        path.write_bytes(raw)
                try:
                    with self.assertRaisesRegex(
                        SCANNER.ScanError,
                        "nonempty newc trailer"
                        if label == "nonempty-trailer-body"
                        else r"(unsupported non-newc initramfs member|non-zero cpio tail|truncated newc (member|header)|nonempty newc trailer|invalid newc header)",
                    ):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        # Even outside initramfs context, a decoded compressed payload must
        # never strip a trailing #BOOTCONFIG\n footer as outer bootconfig.
        non_initramfs_bc = gzip.compress(self.clean + bc_footer, mtime=0)
        path = self.write(non_initramfs_bc, "archive.cpio.gz")
        try:
            with self.assertRaisesRegex(SCANNER.ScanError, "non-zero initramfs tail"):
                self.scan()
        finally:
            path.unlink(missing_ok=True)

    def test_r11_q3_stream_decompressor_close_fds_and_deadline_race(self):
        # 1. Child closes fds 1 and 2 immediately, then sleeps past the timeout.
        out_path = self.base / "q3-close-fds.bin"
        pid_file = self.base / "q3-close-fds.pid"
        cmd_close_sleep = [
            "/bin/sh",
            "-c",
            f"echo $$ > {str(pid_file)!r}; exec >&- 2>&-; exec sleep 2",
        ]
        t0 = time.monotonic()
        with self.assertRaisesRegex(SCANNER.ScanError, "stream decompressor timed out"):
            SCANNER._run_stream_decompressor(cmd_close_sleep, out_path, timeout=0.2)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.5)
        self.assertTrue(pid_file.exists())
        child_pid = int(pid_file.read_text().strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

        # 2. Child that staggers stdout close (0.09s), stderr close (0.09s), and exit (0.09s)
        # exceeds a single 0.18s monotonic deadline even though each phase is < 0.18s.
        stagger_out = self.base / "q3-stagger.bin"
        cmd_stagger = [
            sys.executable,
            "-c",
            "import os, time; time.sleep(0.09); os.close(1); time.sleep(0.09); os.close(2); time.sleep(0.09)",
        ]
        with self.assertRaisesRegex(SCANNER.ScanError, "stream decompressor timed out"):
            SCANNER._run_stream_decompressor(cmd_stagger, stagger_out, timeout=0.18)

        # 3. Child that exits normally before deadline is not falsely flagged as timed out.
        ok_out = self.base / "q3-ok.bin"
        cmd_fast = [
            sys.executable,
            "-c",
            "import os; os.write(1, b'done'); os.close(1); os.close(2)",
        ]
        rc, stderr_bytes = SCANNER._run_stream_decompressor(
            cmd_fast, ok_out, timeout=2.0,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(stderr_bytes, b"")
        self.assertEqual(ok_out.read_bytes(), b"done")

        # 4. Child exits 0 before the 0.35s timer fires while a background subshell holds
        # stdout (with stderr closed) past the timer firing, exercising `if proc.poll() is not None: return` in kill_on_timeout.
        race_out = self.base / "q3-poll-guard.bin"
        cmd_poll_guard = [
            "/bin/sh",
            "-c",
            "(sleep 0.7) 2>/dev/null & printf done; exit 0",
        ]
        rc, stderr_bytes = SCANNER._run_stream_decompressor(
            cmd_poll_guard, race_out, timeout=0.35,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(stderr_bytes, b"")
        self.assertEqual(race_out.read_bytes(), b"done")

    def test_r11_q4_salvage_clean_prefix_vs_bruteforce_and_custom_stderr(self):
        lz4 = shutil.which("lz4")
        zstd = shutil.which("zstd")
        if not lz4 or not zstd:
            self.skipTest("lz4 or zstd CLI unavailable")

        rnd = random.Random(11)
        words = [
            bytes(rnd.choice(b"abcdefghijklmnop") for _ in range(rnd.randint(3, 8)))
            for _ in range(500)
        ]

        def make_chunk(size):
            buf = bytearray()
            while len(buf) < size:
                buf += rnd.choice(words) + b" "
            return bytes(buf[:size])

        # 1. 10-block zstd stream (>8 prefixes) with an empty raw block and distinct blocks.
        z_blocks = []
        for idx in range(9):
            chunk = make_chunk(2048)
            fr = subprocess.run(
                [zstd, "-q", "-1", "--no-check", "-c"],
                input=chunk, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            ).stdout
            pos = 4
            desc = fr[pos]
            pos += 1
            ss = bool(desc & 0x20)
            if not ss:
                pos += 1
            pos += (0, 1, 2, 4)[desc & 3] + (1 if ss else 0, 2, 4, 8)[desc >> 6]
            bh = int.from_bytes(fr[pos : pos + 3], "little")
            bt, bs = (bh >> 1) & 3, bh >> 3
            stored = 1 if bt == 1 else bs
            is_last = 1 if idx == 8 else 0
            new_bh = ((bs << 3) | (bt << 1) | is_last).to_bytes(3, "little")
            z_blocks.append(new_bh + fr[pos + 3 : pos + 3 + stored])
            if idx == 3:
                # Insert an empty raw block (last=0, type=0, size=0)
                z_blocks.append(b"\x00\x00\x00")

        z_header = b"\x28\xb5\x2f\xfd\x00\x58"
        z_stream = z_header + b"".join(z_blocks)

        # 2. Single-segment zstd frame with 4-byte FCS + content checksum (descriptor 0xa4) and >8 blocks.
        z_fcs_src = self.base / "q4-zstd-fcs-src.bin"
        z_fcs_src.write_bytes(make_chunk(96 * 1024))
        z_fcs_stream = subprocess.run(
            [zstd, "-q", "-3", "--target-compressed-block-size=2048", "-c", str(z_fcs_src)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        self.assertEqual(z_fcs_stream[4], 0xA4)

        # 3. 10-block modern LZ4 frame (>8 prefixes, independent blocks, no checksum).
        lz4_desc = b"\x60\x40"
        lz4_hc = b"\x82"
        lz4_blocks = []
        for idx in range(10):
            raw_blk = make_chunk(2048)
            comp_fr = subprocess.run(
                [lz4, "-q", "-B4", "--no-frame-crc", "-c"],
                input=raw_blk, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            ).stdout
            b_sz = struct.unpack_from("<I", comp_fr, 7)[0]
            b_len = b_sz & 0x7FFFFFFF
            lz4_blocks.append(comp_fr[7 : 7 + 4 + b_len])
        lz4_stream = b"\x04\x22\x4d\x18" + lz4_desc + lz4_hc + b"".join(lz4_blocks) + b"\x00\x00\x00\x00"

        # 4. Linked LZ4 frame with block checksum (-BX), content size, and content checksum (FLG 0x5c).
        lz4_linked_src = self.base / "q4-lz4-linked-src.bin"
        lz4_linked_src.write_bytes(make_chunk(700 * 1024))
        lz4_linked_stream = subprocess.run(
            [lz4, "-q", "-B4", "-BD", "--content-size", "-BX", "-c", str(lz4_linked_src)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        self.assertEqual(lz4_linked_stream[4], 0x5C)

        def xxh32(data, seed=0):
            """XXH32 from the xxHash specification, not the scanner helper."""
            prime1 = 0x9E3779B1
            prime2 = 0x85EBCA77
            prime3 = 0xC2B2AE3D
            prime4 = 0x27D4EB2F
            prime5 = 0x165667B1

            def rotl32(value, bits):
                value &= 0xFFFFFFFF
                return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF

            def read32(offset):
                return int.from_bytes(data[offset:offset + 4], "little")

            length = len(data)
            offset = 0
            if length >= 16:
                acc1 = (seed + prime1 + prime2) & 0xFFFFFFFF
                acc2 = (seed + prime2) & 0xFFFFFFFF
                acc3 = seed & 0xFFFFFFFF
                acc4 = (seed - prime1) & 0xFFFFFFFF

                def stripe(acc, lane):
                    acc = (acc + (lane * prime2 & 0xFFFFFFFF)) & 0xFFFFFFFF
                    acc = rotl32(acc, 13)
                    return (acc * prime1) & 0xFFFFFFFF

                while offset + 16 <= length:
                    acc1 = stripe(acc1, read32(offset))
                    acc2 = stripe(acc2, read32(offset + 4))
                    acc3 = stripe(acc3, read32(offset + 8))
                    acc4 = stripe(acc4, read32(offset + 12))
                    offset += 16
                digest = (
                    rotl32(acc1, 1) + rotl32(acc2, 7)
                    + rotl32(acc3, 12) + rotl32(acc4, 18)
                ) & 0xFFFFFFFF
            else:
                digest = (seed + prime5) & 0xFFFFFFFF
            digest = (digest + length) & 0xFFFFFFFF
            while offset + 4 <= length:
                digest = (digest + (read32(offset) * prime3 & 0xFFFFFFFF)) & 0xFFFFFFFF
                digest = (rotl32(digest, 17) * prime4) & 0xFFFFFFFF
                offset += 4
            while offset < length:
                digest = (digest + (data[offset] * prime5 & 0xFFFFFFFF)) & 0xFFFFFFFF
                digest = (rotl32(digest, 11) * prime1) & 0xFFFFFFFF
                offset += 1
            digest ^= digest >> 15
            digest = (digest * prime2) & 0xFFFFFFFF
            digest ^= digest >> 13
            digest = (digest * prime3) & 0xFFFFFFFF
            digest ^= digest >> 16
            return digest

        def lz4_header_checksum(desc):
            """LZ4 frame HC byte: (XXH32(descriptor, seed 0) >> 8) & 0xFF."""
            return (xxh32(desc, 0) >> 8) & 0xFF

        def frame_blocks(kind, frame):
            """Non-final block ends plus the final block span, from the frame bytes."""
            if kind == "zstd":
                if frame[:4] != b"\x28\xb5\x2f\xfd":
                    raise AssertionError("not a zstd frame")
                descriptor = frame[4]
                if descriptor & 0x18:
                    raise AssertionError("reserved zstd descriptor bits")
                pos = 5
                single = bool(descriptor & 0x20)
                if not single:
                    pos += 1
                pos += (0, 1, 2, 4)[descriptor & 3]
                pos += (1 if single else 0, 2, 4, 8)[descriptor >> 6]
                first = pos
                ends = []
                while pos + 3 <= len(frame):
                    value = int.from_bytes(frame[pos:pos + 3], "little")
                    last, block_type, block_size = value & 1, (value >> 1) & 3, value >> 3
                    if block_type == 3:
                        break
                    stored = 1 if block_type == 1 else block_size
                    block_end = pos + 3 + stored
                    if block_end > len(frame):
                        break
                    if last:
                        return first, ends, (pos, block_end)
                    ends.append(block_end)
                    pos = block_end
                raise AssertionError("zstd frame has no last block")
            if frame[:4] != b"\x04\x22\x4d\x18":
                raise AssertionError("not an lz4 frame")
            flags = frame[4]
            pos = 6
            if flags & 0x08:
                pos += 8
            if flags & 0x01:
                pos += 4
            pos += 1
            first = pos
            ends = []
            while pos + 4 <= len(frame):
                block_size = struct.unpack_from("<I", frame, pos)[0]
                if block_size == 0:
                    break
                payload = block_size & 0x7FFFFFFF
                step = payload + (4 if flags & 0x10 else 0)
                block_end = pos + 4 + step
                if block_end > len(frame):
                    break
                nxt = frame[block_end:block_end + 4]
                if len(nxt) == 4 and struct.unpack("<I", nxt)[0] == 0:
                    return first, ends, (pos, block_end)
                ends.append(block_end)
                pos = block_end
            raise AssertionError("lz4 frame has no last block")

        def independent_terminated(kind, frame_bytes, first_blk_off, p_end):
            if kind == "zstd":
                d = frame_bytes[4]
                single = bool(d & 0x20)
                dict_len = (0, 1, 2, 4)[d & 3]
                dict_off = 5 + (0 if single else 1)
                win_byte = b"\x88" if single else frame_bytes[5:6]
                return (
                    b"\x28\xb5\x2f\xfd"
                    + bytes([d & 3])
                    + win_byte
                    + frame_bytes[dict_off : dict_off + dict_len]
                    + frame_bytes[first_blk_off:p_end]
                    + b"\x01\x00\x00"
                )
            flg = frame_bytes[4] & 0x71
            dict_id = (
                frame_bytes[6 + (8 if frame_bytes[4] & 8 else 0) :][:4]
                if (frame_bytes[4] & 1)
                else b""
            )
            desc = bytes([flg]) + frame_bytes[5:6] + dict_id
            return (
                b"\x04\x22\x4d\x18"
                + desc
                + bytes([lz4_header_checksum(desc)])
                + frame_bytes[first_blk_off:p_end]
                + b"\x00\x00\x00\x00"
            )

        # Wrap zstd and lz4 in scripts that rewrite stderr per invocation so stderr comparison cannot work.
        wrap_dir = self.base / "q4-wrappers"
        wrap_bin = wrap_dir / "bin"
        wrap_bin.mkdir(parents=True, exist_ok=True)
        for name, real_bin in (("zstd", zstd), ("lz4", lz4)):
            script = wrap_bin / name
            script.write_text(
                f'#!/bin/sh\n"{real_bin}" "$@" 2>/dev/null\nrc=$?\n'
                f'if [ $rc -ne 0 ]; then echo "stderr-$$" >&2; fi\nexit $rc\n'
            )
            script.chmod(0o755)

        def cli_decode(argv, destination, timeout=15):
            """Decode with the real CLI. Not SCANNER._run_stream_decompressor."""
            try:
                completed = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail(f"cli_decode timed out after {timeout}s: {argv}")
            Path(destination).write_bytes(completed.stdout or b"")
            return completed.returncode, completed.stderr or b""

        samples = (
            ("zstd-multi", "zstd", zstd, z_stream, 6, False),
            ("zstd-ss-fcs-crc", "zstd", zstd, z_fcs_stream, 9, False),
            ("lz4-indep", "lz4", lz4, lz4_stream, 7, False),
            ("lz4-linked-bx-fcs", "lz4", lz4, lz4_linked_stream, 15, True),
        )
        comparisons = 0
        for sample_label, kind, real_bin, full_bytes, first_block_off, has_block_crc in samples:
            src_tmp = self.base / f"q4-{sample_label}-full.bin"
            src_tmp.write_bytes(full_bytes)
            parsed_first, ends, (last_start, last_end) = frame_blocks(kind, full_bytes)
            self.assertEqual(parsed_first, first_block_off)
            scanner_ends = SCANNER._nonfinal_block_prefix_ends(
                io.BytesIO(full_bytes), 0, len(full_bytes), kind,
            )
            self.assertEqual(
                ends,
                scanner_ends,
                f"{sample_label}: frame_blocks != _nonfinal_block_prefix_ends",
            )
            self.assertGreater(len(ends), 8)
            hdr_len = 3 if kind == "zstd" else 4
            crc_len = 4 if (kind == "lz4" and has_block_crc) else 0

            def span_body(start_off, end_off, hdr_len=hdr_len, crc_len=crc_len):
                return end_off - start_off - hdr_len - crc_len

            usable = []
            for index, end_off in enumerate(ends):
                start_off = ends[index - 1] if index else first_block_off
                if span_body(start_off, end_off) > 0:
                    usable.append(index)
            self.assertGreaterEqual(len(usable), 5)
            picked = []
            for index in (
                usable[0], usable[len(usable) // 4], usable[len(usable) // 2],
                usable[(3 * len(usable)) // 4], usable[-1],
            ):
                if index not in picked:
                    picked.append(index)
            for index in usable:
                if len(picked) >= 5:
                    break
                if index not in picked:
                    picked.append(index)
            self.assertEqual(len(picked), 5)
            self.assertGreater(span_body(last_start, last_end), 0)
            probes = [(index, False) for index in picked] + [(len(ends), True)]
            # Positions in this probe list, not literal block indexes. Slot 0
            # stays on the real CLI; every later probe, including the
            # final-block probe, goes through the stderr-rewriting wrapper.
            # The same slots apply to every sample, including lz4-indep.
            wrapper_slots = set(range(1, len(probes)))
            sample_count = 0
            wrapper_count = 0
            for probe_i, (fail_idx, corrupt_last) in enumerate(probes):
                corrupt = bytearray(full_bytes)
                if corrupt_last:
                    start_off, end_off = last_start, last_end
                else:
                    start_off = ends[fail_idx - 1] if fail_idx else first_block_off
                    end_off = ends[fail_idx]
                body_len = span_body(start_off, end_off)
                self.assertGreater(body_len, 0)
                if kind == "zstd":
                    last_bit = 1 if corrupt_last else 0
                    corrupt[start_off : start_off + 3] = (
                        (body_len << 3) | (2 << 1) | last_bit
                    ).to_bytes(3, "little")
                    corrupt[start_off + 3 : end_off] = b"\xff" * body_len
                else:
                    corrupt[start_off : start_off + 4] = struct.pack("<I", body_len)
                    corrupt[start_off + 4 : end_off] = b"\xff" * (body_len + crc_len)
                corrupt_bytes = bytes(corrupt)
                in_file = self.base / f"q4-{sample_label}-corrupt-{fail_idx}.bin"
                out_file = self.base / f"q4-{sample_label}-out-{fail_idx}.bin"
                in_file.write_bytes(corrupt_bytes)
                out_file.write_bytes(b"")

                # Independent brute force across:
                # (a) whole-frame decode,
                # (b) raw truncated prefix at every block boundary (no synthetic end mark), and
                # (c) independently re-framed end-marked prefix at every block boundary, plus monotonicity verification.
                p_in = self.base / "q4-brute-in.bin"
                p_out = self.base / "q4-brute-out.bin"
                p_out.write_bytes(b"")
                cli_decode([real_bin, "-dc", str(in_file)], p_out)
                brute_max = p_out.stat().st_size if p_out.exists() else 0

                end_marked_rcs = []
                for p_end in ends:
                    # (b) Raw truncated prefix without any header rewrite or synthetic trailer
                    p_in.write_bytes(corrupt_bytes[:p_end])
                    p_out.write_bytes(b"")
                    cli_decode([real_bin, "-dc", str(p_in)], p_out)
                    if p_out.exists():
                        brute_max = max(brute_max, p_out.stat().st_size)

                    # (c) Independently terminated block prefix
                    p_in.write_bytes(
                        independent_terminated(kind, corrupt_bytes, first_block_off, p_end)
                    )
                    p_out.write_bytes(b"")
                    b_rc, _ = cli_decode([real_bin, "-dc", str(p_in)], p_out)
                    end_marked_rcs.append(1 if b_rc == 0 else 0)
                    if b_rc == 0 and p_out.exists():
                        brute_max = max(brute_max, p_out.stat().st_size)

                # Verify `returncode == 0` is monotone across prefixes (1* followed by 0*).
                self.assertEqual(
                    end_marked_rcs,
                    sorted(end_marked_rcs, reverse=True),
                    f"{sample_label} fail_idx={fail_idx}: non-monotone prefix rcs {end_marked_rcs}",
                )
                # (c) is part of the verdict. All-zero end_marked_rcs is still
                # monotone, and (c) never raises brute_max above (a) and (b),
                # so a junk independent_terminated would otherwise stay 24/24.
                # Successes are the block ends strictly before the corruption.
                self.assertEqual(
                    sum(end_marked_rcs),
                    len(ends) if corrupt_last else fail_idx,
                    f"{sample_label} fail_idx={fail_idx}: (c) successes "
                    f"{sum(end_marked_rcs)} != {len(ends) if corrupt_last else fail_idx}",
                )

                use_wrapper = probe_i in wrapper_slots
                if use_wrapper:
                    wrapper_count += 1
                tool_to_use = str(wrap_bin / kind) if use_wrapper else real_bin
                otatools_arg = wrap_dir if use_wrapper else None
                rc, _, _ = SCANNER._decode_compressed_cli_stream(
                    in_file, out_file, kind, tool_to_use, otatools_arg,
                    {"scratch": self.base, "otatools": otatools_arg},
                )
                self.assertNotEqual(rc, 0)
                self.assertEqual(
                    out_file.stat().st_size, brute_max,
                    f"{sample_label} fail_idx={fail_idx}: scanner={out_file.stat().st_size} != brute={brute_max}",
                )
                comparisons += 1
                sample_count += 1
            self.assertEqual(
                wrapper_count, len(wrapper_slots),
                f"{sample_label} stderr-wrapper probes {wrapper_count}/{len(wrapper_slots)}",
            )
            print(f"Q4 {sample_label} {sample_count}/6", flush=True)
            print(
                f"Q4 {sample_label} wrapper {wrapper_count}/{len(wrapper_slots)}",
                flush=True,
            )
        print(f"Q4 comparisons {comparisons}/24", flush=True)
        self.assertEqual(comparisons, 24)
        # XXH32 seed 0. Spec, 16-byte stripes when len >= 16, otherwise
        # seed + PRIME32_5: Cyan4973/xxHash v0.8.3 doc/xxhash_spec.md
        # (XXH32 algorithm description). Reference values from
        # cli/xsum_sanity_check.c XSUM_XXH32_testdata over the buffer from
        # XSUM_fillTestBuffer (PRIME32=2654435761, PRIME64=11400714785074694797):
        # len 0 -> 0x02CC5D05, len 1 -> 0xCF65B03E, len 14 -> 0x1208E7E2
        # (all < 16 bytes), len 222 -> 0x5BD11DBD (>= 16-byte stripe branch).
        # b"abcd" -> 0xA3643705 is the 4-byte value on that same < 16-byte path.
        # Stripe-boundary prefixes of that same XSUM_fillTestBuffer, len 16
        # -> 0x93BA3759 and len 32 -> 0xD89829EC, come from the spec (not
        # from this xxh32). They were produced by two other transcriptions
        # of doc/xxhash_spec.md: a pointer loop with limit = end - 15 (the
        # xxhash.h `p < bEnd - 15` stripe condition) and a `length // 16`
        # stripe count. Those two agreed with each other, both reproduced
        # the published lengths above, and a third unrolled one-stripe hash
        # (no loop) reproduced the 16-byte value. len 16 is one full stripe
        # (`>= 16` and `offset + 16 <= length` each true once). len 32 is
        # two stripes (`offset + 16 <= length` true twice; `offset + 16 <
        # length` only once).
        sanity = bytearray(222)
        byte_gen = 2654435761
        for index in range(222):
            sanity[index] = (byte_gen >> 56) & 0xFF
            byte_gen = (byte_gen * 11400714785074694797) & 0xFFFFFFFFFFFFFFFF
        sanity = bytes(sanity)
        self.assertEqual(xxh32(b"", 0), 0x02CC5D05)
        self.assertEqual(xxh32(sanity[:1], 0), 0xCF65B03E)
        self.assertEqual(xxh32(sanity[:14], 0), 0x1208E7E2)
        self.assertEqual(xxh32(sanity[:16], 0), 0x93BA3759)
        self.assertEqual(xxh32(sanity[:32], 0), 0xD89829EC)
        self.assertEqual(xxh32(b"abcd", 0), 0xA3643705)
        self.assertEqual(xxh32(sanity, 0), 0x5BD11DBD)
        self.assertEqual(lz4_header_checksum(lz4_desc), 0x82)

    def test_r11_q5_legacy_lz4_zero_gap_gzip_and_trailing_fragments(self):
        lz4 = shutil.which("lz4")
        zstd = shutil.which("zstd")
        if not lz4 or not zstd:
            self.skipTest("lz4 or zstd CLI unavailable")
        evil = fixtures.newc_archive(
            "evil", fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        )
        lead_clean = subprocess.run(
            [lz4, "-q", "-l", "-c"], input=self.clean,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout

        # 1. Trailing fragment < 4 bytes: lz4l(clean) + "XY" and magic + "XY" both refuse as non-zero tail.
        for label, prefix in (("after-chunk", lead_clean), ("after-magic", SCANNER.LEGACY_LZ4_MAGIC)):
            raw_xy = prefix + b"XY"
            path = self.write(raw_xy, "ramdisk")
            pin = {
                "partition": "system_a",
                "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                "sha256": hashlib.sha256(raw_xy).hexdigest(),
                "reason": "attempt to pin <4B non-zero fragment",
            }
            try:
                with self.assertRaisesRegex(SCANNER.ScanError, "non-zero initramfs tail"):
                    self.scan()
                with self.assertRaisesRegex(SCANNER.ScanError, "non-zero initramfs tail"):
                    self.scan(unsupported_allowlist=[pin])
            finally:
                path.unlink(missing_ok=True)

            raw_nul = prefix + b"\0\0"
            path = self.write(raw_nul, "ramdisk")
            try:
                report = self.scan()
                self.assertEqual(report["hits"], [])
            finally:
                path.unlink(missing_ok=True)

        # 2. Zero-gap next member after decoded legacy LZ4 chunk:
        gz_evil_flg0 = gzip.compress(evil, mtime=0)
        gz_evil_fname = gz_evil_flg0[:3] + b"\x08" + gz_evil_flg0[4:10] + b"evil\0" + gz_evil_flg0[10:]
        zstd_evil = subprocess.run(
            [zstd, "-q", "-c"], input=evil,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        lz4f_evil = subprocess.run(
            [lz4, "-q", "-c"], input=evil,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        for label, nxt in (
            ("cpio-evil", evil),
            ("xz-evil", lzma.compress(evil)),
            ("zstd-evil", zstd_evil),
            ("lz4f-evil", lz4f_evil),
            ("gzip-fname-evil", gz_evil_fname),
            ("gzip-flg0-small-evil", gz_evil_flg0),
        ):
            raw = lead_clean + nxt
            with self.subTest(zerogap=label):
                path = self.write(raw, "ramdisk")
                pin = {
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "reason": "attempt to pin zero-gap legacy lz4 + evil member",
                }
                try:
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan()
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                        self.scan(unsupported_allowlist=[pin])
                finally:
                    path.unlink(missing_ok=True)

        # Zero-gap gzip with FLG=0 and size >= 559,903 B is treated as an LZ4 chunk (559,903 <= compressBound)
        # and fails decompression as an unpinnable ScanError.
        big_incompressible = random.Random(5).randbytes(600_000)
        gz_flg0_large = gzip.compress(fixtures.newc_archive("r", big_incompressible) + evil, mtime=0)
        self.assertGreaterEqual(len(gz_flg0_large), 559_903 + 4)
        raw_large = lead_clean + gz_flg0_large
        path = self.write(raw_large, "ramdisk")
        pin = {
            "partition": "system_a",
            "container_chain": [{"kind": "tree_file", "path": "ramdisk"}],
            "sha256": hashlib.sha256(raw_large).hexdigest(),
            "reason": "attempt to pin zero-gap FLG=0 >=559903B gzip after legacy lz4",
        }
        try:
            with self.assertRaisesRegex(
                SCANNER.ScanError,
                r"(invalid lz4 stream frame|lz4 decompression failed)",
            ):
                self.scan()
            with self.assertRaisesRegex(
                SCANNER.ScanError,
                r"(invalid lz4 stream frame|lz4 decompression failed)",
            ):
                self.scan(unsupported_allowlist=[pin])
        finally:
            path.unlink(missing_ok=True)

    def test_r11_q6_legacy_lz4_bisection_cost_single_pass_and_scaled_timeout(self):
        lz4 = shutil.which("lz4")
        if not lz4:
            self.skipTest("lz4 CLI unavailable")

        # 1. Single-pass decode for a valid >8 MiB multi-chunk legacy LZ4 stream
        # whose compressed chunks exceed 32 KiB (proving no double decode).
        count_file = self.base / "lz4-call-count.txt"
        count_file.write_text("0\n")
        wrap_dir = self.base / "q6-wrap"
        wrap_bin = wrap_dir / "bin"
        wrap_bin.mkdir(parents=True, exist_ok=True)
        lz4_wrap = wrap_bin / "lz4"
        lz4_wrap.write_text(
            f'#!/bin/sh\n'
            f'c=$(cat "{count_file}")\n'
            f'echo $((c + 1)) > "{count_file}"\n'
            f'exec "{lz4}" "$@"\n'
        )
        lz4_wrap.chmod(0o755)

        # Build a ~9 MiB payload with >32 KiB compressed chunk size so 74d572c would double-decode it.
        incompressible = random.Random(99).randbytes(64 * 1024)
        big_payload = incompressible + (b"B" * (9 * 1024 * 1024))
        big_legacy = subprocess.run(
            [lz4, "-q", "-l", "-c"], input=big_payload,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        path = self.write(big_legacy, "big.bin.lz4")
        try:
            report = self.scan(otatools=wrap_dir)
            self.assertEqual(report["hits"], [])
            calls = int(count_file.read_text().strip())
            self.assertEqual(calls, 1)
        finally:
            path.unlink(missing_ok=True)

        # 2. Bisection on 1,000 and 2,000 chunks with corrupt last chunk finishes quickly (<3.0s).
        for count in (1000, 2000):
            bad_last = (
                SCANNER.LEGACY_LZ4_MAGIC
                + (b"\x02\x00\x00\x00\x10A" * (count - 1))
                + b"\x04\x00\x00\x00\xff\xff\xff\xff"
            )
            path = self.write(bad_last, "chunks.txt.lz4")
            try:
                t0 = time.monotonic()
                with self.assertRaisesRegex(
                    SCANNER.ScanError,
                    r"(invalid lz4 stream frame|lz4 decompression failed)",
                ):
                    self.scan()
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 3.0)
            finally:
                path.unlink(missing_ok=True)

        # 3. Timeout scaling formula: 30s base + 1s per 4 MiB.
        self.assertAlmostEqual(SCANNER._stream_decompressor_timeout(0), 30.0)
        self.assertAlmostEqual(
            SCANNER._stream_decompressor_timeout(4 * 1024 * 1024), 31.0
        )
        self.assertAlmostEqual(
            SCANNER._stream_decompressor_timeout(16 * 1024 * 1024), 34.0
        )

        # 4. S4 real-binary proof: lz4 -dc itself enforces the 8 MiB per-chunk output cap
        # in programs/lz4io.c:LZ4IO_decodeLegacyStream, so a chunk that would decode to 8 MiB + 1 fails
        # both in the multi-chunk batch AND per chunk while chunk 0 is salvaged and scanned.
        def rle_lz4_chunk(total_decoded_len):
            match_len = total_decoded_len - 1 - 5
            extra = match_len - 4 - 15
            q, r = divmod(extra, 255)
            body = (
                bytes([0x1F])
                + b"A"
                + b"\x01\x00"
                + (b"\xff" * q)
                + bytes([r])
                + bytes([0x50])
                + b"AAAAA"
            )
            return struct.pack("<I", len(body)) + body

        chunk_8m = rle_lz4_chunk(SCANNER.LEGACY_LZ4_BLOCK_MAX_OUTPUT)
        chunk_8m_plus_1 = rle_lz4_chunk(SCANNER.LEGACY_LZ4_BLOCK_MAX_OUTPUT + 1)

        # Real lz4 -dc accepts 8 MiB chunks (single and 2-chunk batch) and rejects 8 MiB + 1
        # both per-chunk and in a 2-chunk batch [8 MiB, 8 MiB + 1].
        for ok_stream, expected_len in (
            (SCANNER.LEGACY_LZ4_MAGIC + chunk_8m, SCANNER.LEGACY_LZ4_BLOCK_MAX_OUTPUT),
            (SCANNER.LEGACY_LZ4_MAGIC + chunk_8m + chunk_8m, 2 * SCANNER.LEGACY_LZ4_BLOCK_MAX_OUTPUT),
        ):
            proc = subprocess.run(
                [lz4, "-dc"], input=ok_stream,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(len(proc.stdout), expected_len)

        for bad_stream, expected_salvaged, expect_unpinnable in (
            (SCANNER.LEGACY_LZ4_MAGIC + chunk_8m_plus_1, 0, False),
            (
                SCANNER.LEGACY_LZ4_MAGIC + chunk_8m + chunk_8m_plus_1,
                SCANNER.LEGACY_LZ4_BLOCK_MAX_OUTPUT,
                True,
            ),
        ):
            proc = subprocess.run(
                [lz4, "-dc"], input=bad_stream,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertNotEqual(proc.returncode, 0)
            src_lz4 = self.base / "q6-over8m-in.lz4"
            dst_lz4 = self.base / "q6-over8m-out.bin"
            src_lz4.write_bytes(bad_stream)
            dst_lz4.write_bytes(b"")
            with src_lz4.open("rb") as f:
                ok, _, unp = SCANNER._decode_legacy_lz4_stream(
                    f, 0, len(bad_stream), lz4, None,
                    {"scratch": self.base, "otatools": None}, dst_lz4,
                )
            self.assertFalse(ok)
            self.assertEqual(unp, expect_unpinnable)
            self.assertEqual(dst_lz4.stat().st_size, expected_salvaged)
            path = self.write(bad_stream, "over8m.bin.lz4")
            pin = {
                "partition": "system_a",
                "container_chain": [{"kind": "tree_file", "path": "over8m.bin.lz4"}],
                "sha256": hashlib.sha256(bad_stream).hexdigest(),
                "reason": "attempt to pin >8MiB legacy lz4 chunk",
            }
            try:
                with self.assertRaisesRegex(
                    SCANNER.ScanError,
                    r"(unsupported nested image|invalid lz4 stream frame|lz4 decompression failed)",
                ):
                    self.scan()
                if expect_unpinnable:
                    with self.assertRaisesRegex(
                        SCANNER.ScanError,
                        r"(invalid lz4 stream frame|lz4 decompression failed)",
                    ):
                        self.scan(unsupported_allowlist=[pin])
            finally:
                path.unlink(missing_ok=True)
                src_lz4.unlink(missing_ok=True)
                dst_lz4.unlink(missing_ok=True)

        # If chunk 0 contains an IFMA ELF and chunk 1 is an 8 MiB + 1 bomb, bisection salvages
        # chunk 0 and refuses with forbidden VEX IFMA even with a pin.
        evil_elf = fixtures.elf_with_segments(fixtures.EncodingTests.LUQ)
        evil_chunk0 = subprocess.run(
            [lz4, "-q", "-l", "-c"], input=evil_elf,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout[4:]
        evil_then_over8m = SCANNER.LEGACY_LZ4_MAGIC + evil_chunk0 + chunk_8m_plus_1
        path = self.write(evil_then_over8m, "evil-over8m.bin.lz4")
        pin = {
            "partition": "system_a",
            "container_chain": [{"kind": "tree_file", "path": "evil-over8m.bin.lz4"}],
            "sha256": hashlib.sha256(evil_then_over8m).hexdigest(),
            "reason": "attempt to pin IFMA chunk0 followed by >8MiB chunk1",
        }
        try:
            with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA"):
                self.scan(unsupported_allowlist=[pin])
        finally:
            path.unlink(missing_ok=True)

        # 5. Defense-in-depth batch_size > len(chunks) * LEGACY_LZ4_BLOCK_MAX_OUTPUT check:
        # if a custom/wrapped lz4 binary emits >8 MiB on a 1-chunk stream with rc=0,
        # the scanner refuses it with "legacy lz4 block output exceeds 8 MiB".
        cap_dir = self.base / "q6-cap-wrap"
        cap_bin = cap_dir / "bin"
        cap_bin.mkdir(parents=True, exist_ok=True)
        oversize_file = cap_dir / "oversize.bin"
        with oversize_file.open("wb") as handle:
            handle.truncate(SCANNER.LEGACY_LZ4_BLOCK_MAX_OUTPUT + 1)
        cap_lz4 = cap_bin / "lz4"
        cap_lz4.write_text(
            f'#!/bin/sh\ncat "{oversize_file}"\nexit 0\n'
        )
        cap_lz4.chmod(0o755)
        one_chunk = SCANNER.LEGACY_LZ4_MAGIC + b"\x02\x00\x00\x00\x10A"
        path = self.write(one_chunk, "cap.bin.lz4")
        pin = {
            "partition": "system_a",
            "container_chain": [{"kind": "tree_file", "path": "cap.bin.lz4"}],
            "sha256": hashlib.sha256(one_chunk).hexdigest(),
            "reason": "attempt to pin oversize batch output",
        }
        try:
            with self.assertRaisesRegex(
                SCANNER.ScanError,
                "legacy lz4 block output exceeds 8 MiB",
            ):
                self.scan(otatools=cap_dir, unsupported_allowlist=[pin])
        finally:
            path.unlink(missing_ok=True)
            oversize_file.unlink(missing_ok=True)


def _e2_audit_elf():
    """The audit's IFMA ELF: vpmadd52luq at file offset 0x110, file 0x1500
    bytes, ELF span (last PT_LOAD end) 0x1400."""
    data = bytearray(0x1500)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into(
        "<HHIQQQIHHHHHH", data, 16,
        2, 62, 1, 0, 64, 0, 0, 64, 56, 2, 0, 0, 0,
    )
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0x100, 0x400100, 0, 0x100, 0x100, 0x1000)
    struct.pack_into("<IIQQQQQQ", data, 120, 1, 4, 0x1300, 0x401300, 0, 0x100, 0x100, 0x1000)
    data[0x110:0x115] = bytes.fromhex("c4e2f1b4c3")
    return bytes(data)


_E2_DEV_SPEC = importlib.util.spec_from_file_location(
    "ifma_e2_devprofile", Path(__file__).resolve().parents[2] / "builder/lib/devprofile.py"
)
E2_DEV = importlib.util.module_from_spec(_E2_DEV_SPEC)
_E2_DEV_SPEC.loader.exec_module(E2_DEV)


class PinAllowlistE2Tests(unittest.TestCase):
    """gb-ifma023e E2: decoded (or partly decoded) content is never pinnable,
    and no library exception escapes scan_tree."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "tree"
        self.root.mkdir()
        self.evil = _e2_audit_elf()

    def scan_pinned(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        try:
            return SCANNER.scan_tree(
                self.root,
                provenance={"partition": "system_a", "container_chain": []},
                unsupported_allowlist=[{
                    "partition": "system_a",
                    "container_chain": [{"kind": "tree_file", "path": name}],
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "reason": "E2 exact pin",
                }],
            )
        finally:
            path.unlink(missing_ok=True)

    def assert_refused_with_hit(self, name, data, where):
        with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA") as caught:
            self.scan_pinned(name, data)
        self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
        self.assertIn("vpmadd52luq", str(caught.exception))
        self.assertIn(where, str(caught.exception))

    def assert_clean_refusal(self, name, data, pattern):
        try:
            self.scan_pinned(name, data)
        except SCANNER.ImageOpenError as exc:  # pragma: no cover - failure path
            self.fail(f"pinnable ImageOpenError: {exc}")
        except SCANNER.ScanError as exc:
            self.assertRegex(str(exc), pattern)
            return
        except Exception as exc:  # pragma: no cover - failure path
            self.fail(f"uncaught {type(exc).__name__}: {exc}")
        self.fail("accepted with an exact pin")

    def test_a1_bzimage_decoded_then_trailing_bytes_refused_with_exact_pin(self):
        cases = (
            ("gzip+5", gzip.compress(self.evil, mtime=0) + b"XXXXX"),
            ("xz+5", lzma.compress(self.evil, format=lzma.FORMAT_XZ) + b"YYYYY"),
            ("elf+1", self.evil + b"Z"),
        )
        for label, payload in cases:
            with self.subTest(case=label):
                self.assert_refused_with_hit(
                    "kernel.img", E2_DEV.wrap_hdrs_bzimage(payload), "kernel.img!/vmlinux+0x110",
                )

    def test_a1_decoded_bzimage_without_ifma_is_still_unpinnable(self):
        clean = bytearray(self.evil)
        clean[0x110:0x115] = b"\x90" * 5
        bz = E2_DEV.wrap_hdrs_bzimage(gzip.compress(bytes(clean), mtime=0) + b"XXXXX")
        self.assert_clean_refusal("kernel.img", bz, "trailing-garbage gzip payload")

    def test_a2_non_elf_compressed_module_payload_is_scanned_then_refused(self):
        payload = fixtures.newc_archive("bin/evil.elf", self.evil)
        cases = [
            ("mod.ko.gz", gzip.compress(payload, mtime=0)),
            ("mod.ko.xz", lzma.compress(payload, format=lzma.FORMAT_XZ)),
        ]
        zstd = shutil.which("zstd")
        if zstd:
            cases.append(("mod.ko.zst", subprocess.run(
                [zstd, "-q", "-c"], input=payload,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            ).stdout))
        for name, data in cases:
            with self.subTest(name=name):
                self.assert_refused_with_hit(name, data, "bin/evil.elf+0x110")
        if not zstd:
            self.skipTest("zstd CLI unavailable for the .ko.zst case")

    def _zip_two(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("bad.bin", b"hello compressed payload" * 8, compress_type=zipfile.ZIP_STORED)
            archive.writestr("evil.elf", self.evil, compress_type=zipfile.ZIP_STORED)
        return bytearray(buffer.getvalue())

    def test_a3_zip_utf8_flag_with_0xff_name_is_a_clean_scan_error(self):
        raw = self._zip_two()
        raw[6:8] = struct.pack("<H", struct.unpack_from("<H", raw, 6)[0] | (1 << 11))
        raw[30] = 0xFF
        central = raw.find(b"PK\x01\x02")
        raw[central + 8:central + 10] = struct.pack(
            "<H", struct.unpack_from("<H", raw, central + 8)[0] | (1 << 11))
        raw[central + 46] = 0xFF
        with self.assertRaises(UnicodeDecodeError):
            zipfile.ZipFile(io.BytesIO(bytes(raw)))
        self.assert_clean_refusal("payload.zip", bytes(raw), r"invalid ZIP/APEX: UnicodeDecodeError")

    def test_a3_zip_extract_version_7_is_a_clean_scan_error(self):
        raw = self._zip_two()
        central = raw.find(b"PK\x01\x02")
        raw[central + 6:central + 8] = struct.pack("<H", 70)
        with self.assertRaises(NotImplementedError):
            zipfile.ZipFile(io.BytesIO(bytes(raw)))
        self.assert_clean_refusal("payload.zip", bytes(raw), r"invalid ZIP/APEX: NotImplementedError")

    def test_a3_zip_damaged_zstd_member_is_refused_and_later_member_scanned(self):
        """ZIP_ZSTANDARD (method 93) member: Python 3.14 decodes it and raises
        compression.zstd.ZstdError for damaged data; older Pythons raise
        NotImplementedError at open. A member error of a type in no tuple
        (simulated ZstdError) is refused too, and the stored IFMA ELF after it
        is still scanned."""
        raw = self._zip_two()
        raw[8:10] = struct.pack("<H", 93)
        central = raw.find(b"PK\x01\x02")
        raw[central + 10:central + 12] = struct.pack("<H", 93)
        name_len, extra_len = struct.unpack_from("<HH", raw, 26)
        raw[30 + name_len + extra_len] ^= 0xFF
        self.assert_refused_with_hit("payload.zip", bytes(raw), "payload.zip!/evil.elf+0x110")

        class ZstdError(Exception):
            """Stand-in for compression.zstd.ZstdError (not an OSError)."""

        original_member = SCANNER._scan_zip_member

        def failing_first(archive, info, *args, **kwargs):
            if info.filename == "bad.bin":
                raise ZstdError("Unable to decompress Zstandard data: Unknown frame descriptor")
            return original_member(archive, info, *args, **kwargs)

        clean_zip = bytes(self._zip_two())
        with mock.patch.object(SCANNER, "_scan_zip_member", side_effect=failing_first):
            self.assert_refused_with_hit("payload.zip", clean_zip, "payload.zip!/evil.elf+0x110")

    def test_a3_unexpected_exception_anywhere_is_a_clean_scan_error(self):
        """Catch-all: a library exception of any type (here one raised while
        reading ZIP members) becomes a ScanError naming the type."""
        class Weird(Exception):
            pass

        harmless = io.BytesIO()
        with zipfile.ZipFile(harmless, "w") as archive:
            archive.writestr("a.bin", b"plain")
        with mock.patch.object(zipfile.ZipFile, "infolist", side_effect=Weird("boom")):
            self.assert_clean_refusal("payload.zip", harmless.getvalue(), r"Weird: boom")

    def test_e2_damaged_zip_member_without_ifma_is_unpinnable(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("bad.bin", b"hello compressed payload" * 8, compress_type=zipfile.ZIP_BZIP2)
            archive.writestr("ok.txt", b"nothing here", compress_type=zipfile.ZIP_STORED)
        raw = bytearray(buffer.getvalue())
        name_len, extra_len = struct.unpack_from("<HH", raw, 26)
        raw[30 + name_len + extra_len + 4] ^= 0xFF
        self.assert_clean_refusal("payload.zip", bytes(raw), r"invalid ZIP/APEX member data: bad\.bin")

    @unittest.skipUnless(sys.version_info >= (3, 14), "compression.zstd needs Python 3.14")
    def test_a3_real_zstd_error_member_py314(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("bad.bin", b"hello compressed payload" * 8, compress_type=93)
            archive.writestr("evil.elf", self.evil, compress_type=zipfile.ZIP_STORED)
        raw = bytearray(buffer.getvalue())
        name_len, extra_len = struct.unpack_from("<HH", raw, 26)
        raw[30 + name_len + extra_len + 2] ^= 0xFF
        self.assert_refused_with_hit("payload.zip", bytes(raw), "payload.zip!/evil.elf+0x110")


    def test_e3_shipped_avb0_vbmeta_pin_still_applies(self):
        """The shipped manifest's only unsupported pin is the AVB0 Microdroid
        vbmeta blob: an unknown image kind by magic, never decoded, so it stays
        on the pinnable allowlist. Same shape (65536-byte AVB0 .img inside an
        APEX ZIP member chain) with an exact pin is still accepted."""
        manifest = json.loads(
            (Path(__file__).resolve().parents[2] / "builder/ifma/manifest.json").read_text()
        )
        pins = manifest["unsupported_allowlist"]
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["container_chain"][-1]["path"], "etc/fs/microdroid_vbmeta.img")
        vbmeta = b"AVB0" + struct.pack(">II", 1, 0) + bytes(65536 - 12)
        apex = io.BytesIO()
        with zipfile.ZipFile(apex, "w") as archive:
            archive.writestr("etc/fs/microdroid_vbmeta.img", vbmeta)
        path = self.root / "com.google.android.virt.apex"
        path.write_bytes(apex.getvalue())
        chain = [
            {"kind": "tree_file", "path": "com.google.android.virt.apex"},
            {"kind": "zip_member", "path": "etc/fs/microdroid_vbmeta.img"},
        ]
        provenance = {"partition": "system_a", "container_chain": []}
        with self.assertRaisesRegex(SCANNER.ScanError, "unsupported nested image"):
            SCANNER.scan_tree(self.root, provenance=provenance)
        report = SCANNER.scan_tree(
            self.root, provenance=provenance,
            unsupported_allowlist=[{
                "partition": "system_a", "container_chain": chain,
                "sha256": hashlib.sha256(vbmeta).hexdigest(), "reason": "AVB0 vbmeta",
            }],
        )
        self.assertEqual(len(report["accepted_unsupported"]), 1)
        self.assertEqual(report["hits"], [])

    def test_e1_failing_tool_with_partial_output_is_scanned_then_refused(self):
        """fsck.erofs / simg2img / debugfs that fail AFTER writing output: the
        output is scanned and the image refused even with an exact pin; the
        same failure with no output stays pinnable (allowlist 1)."""
        tools = Path(self.temp.name) / "tools"
        (tools / "bin").mkdir(parents=True)
        evil_path = Path(self.temp.name) / "evil.elf"
        evil_path.write_bytes(self.evil)
        scripts = {
            "fsck.erofs": 'for a in "$@"; do case "$a" in --extract=*) d="${a#--extract=}";; esac; done\n'
                          '[ -n "$WRITE" ] && cp "$EVIL" "$d/evil.elf"; echo "corrupt" >&2; exit 1\n',
            "simg2img": '[ -n "$WRITE" ] && cp "$EVIL" "$2"; echo "bad chunk" >&2; exit 1\n',
            "debugfs": 'd="${2#rdump / }"; [ -n "$WRITE" ] && cp "$EVIL" "$d/evil.elf"; echo "error: short read"; exit 0\n',
        }
        for name, body in scripts.items():
            tool = tools / "bin" / name
            tool.write_text("#!/bin/sh\n" + body)
            tool.chmod(0o755)
        erofs = bytearray(4096); erofs[1024:1028] = b"\xe2\xe1\xf5\xe0"
        sparse = b"\x3a\xff\x26\xed" + bytes(60)
        ext4 = bytearray(4096); ext4[1080:1082] = b"\x53\xef"
        for label, data in (("erofs", bytes(erofs)), ("sparse", sparse), ("ext4", bytes(ext4))):
            name = f"{label}.img"
            pin = {
                "partition": "system_a",
                "container_chain": [{"kind": "tree_file", "path": name}],
                "sha256": hashlib.sha256(data).hexdigest(), "reason": "E1 tool pin",
            }
            path = self.root / name
            path.write_bytes(data)
            try:
                with self.subTest(tool=label, output="partial"), mock.patch.dict(
                    os.environ, {"WRITE": "1", "EVIL": str(evil_path)},
                ):
                    with self.assertRaisesRegex(SCANNER.ScanError, "forbidden VEX IFMA") as caught:
                        SCANNER.scan_tree(
                            self.root, otatools=tools,
                            provenance={"partition": "system_a", "container_chain": []},
                            unsupported_allowlist=[pin],
                        )
                    self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
                    self.assertIn("vpmadd52luq", str(caught.exception))
                with self.subTest(tool=label, output="none"), mock.patch.dict(
                    os.environ, {"WRITE": "", "EVIL": str(evil_path)},
                ):
                    report = SCANNER.scan_tree(
                        self.root, otatools=tools,
                        provenance={"partition": "system_a", "container_chain": []},
                        unsupported_allowlist=[pin],
                    )
                    self.assertEqual(len(report["accepted_unsupported"]), 1)
            finally:
                path.unlink(missing_ok=True)



def _block_zstd_module():
    """Make `from compression import zstd` fail for modules loaded inside the
    context (the scanner re-imports devprofile for every bzImage)."""
    return mock.patch.dict(sys.modules, {"compression": None, "compression.zstd": None})


class BzimagePayloadB3Tests(unittest.TestCase):
    """gb-ifma023f B1-B3: a bzImage payload is pinnable only when it was never
    decoded (devprofile.UndecodablePayloadError). Anything decoded - also
    before a checksum/CRC failure - is scanned and refused; a gzip CRC32/ISIZE
    mismatch follows Linux __gunzip and is not a failure."""

    setUp = PinAllowlistE2Tests.setUp
    scan_pinned = PinAllowlistE2Tests.scan_pinned
    assert_refused_with_hit = PinAllowlistE2Tests.assert_refused_with_hit
    assert_clean_refusal = PinAllowlistE2Tests.assert_clean_refusal
    HIT = "kernel.img!/vmlinux+0x110"

    def refuse(self, payload):
        self.assert_refused_with_hit("kernel.img", E2_DEV.wrap_hdrs_bzimage(payload), self.HIT)

    def clean_elf(self):
        clean = bytearray(self.evil)
        clean[0x110:0x115] = b"\x90" * 5
        return bytes(clean)

    def zstd_cli(self, data):
        zstd = shutil.which("zstd")
        if not zstd:
            self.skipTest("zstd CLI unavailable")
        return subprocess.run(
            [zstd, "-q", "-c", "--check"], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout

    def test_b3_codex_lz4_multiblock_bad_checksum_refused_with_exact_pin(self):
        lz4 = shutil.which("lz4")
        if not lz4:
            self.skipTest("lz4 CLI unavailable")
        payload = self.evil + random.Random(91).randbytes(5 * 1024 * 1024)
        good = subprocess.run(
            [lz4, "-q", "-c", "-B4", "--content-size"], input=payload,
            capture_output=True, check=True, timeout=60,
        ).stdout
        self.refuse(good[:-1] + bytes([good[-1] ^ 0xFF]))

    def _zstd_trailing5(self):
        return self.zstd_cli(self.evil) + b"XXXXX"

    def test_b3_zstd_trailing_junk_without_compression_zstd_refused(self):
        payload = self._zstd_trailing5()
        with _block_zstd_module():
            spec = importlib.util.spec_from_file_location("ifma_b3_dev_nozstd", E2_DEV.__file__)
            dev = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(dev)
            self.assertIsNone(dev._zstd)
            self.refuse(payload)

    @unittest.skipUnless(sys.version_info >= (3, 14), "compression.zstd needs Python 3.14")
    def test_b3_zstd_trailing_junk_with_compression_zstd_refused(self):
        self.assertIsNotNone(E2_DEV._zstd)
        self.refuse(self._zstd_trailing5())

    def _zstd_bad_xxh64(self):
        frame = bytearray(self.zstd_cli(self.evil))
        frame[-1] ^= 0xFF
        return bytes(frame)

    def test_b3_zstd_xxh64_flip_without_compression_zstd_refused(self):
        payload = self._zstd_bad_xxh64()
        with _block_zstd_module():
            self.refuse(payload)

    @unittest.skipUnless(sys.version_info >= (3, 14), "compression.zstd needs Python 3.14")
    def test_b3_zstd_xxh64_flip_with_compression_zstd_refused(self):
        self.refuse(self._zstd_bad_xxh64())

    def _gzip_flips(self, data):
        gz = gzip.compress(data, mtime=0)
        crc = bytearray(gz); crc[-8] ^= 0xFF
        isize = bytearray(gz); isize[-4] ^= 0x01
        return (("crc32", bytes(crc)), ("isize", bytes(isize)))

    def test_b3_gzip_crc32_or_isize_flip_is_decoded_and_scanned(self):
        for label, payload in self._gzip_flips(self.evil):
            with self.subTest(trailer=label):
                self.refuse(payload)

    def test_b3_gzip_crc32_or_isize_flip_with_clean_payload_accepted_without_pin(self):
        """Linux __gunzip ignores CRC32/ISIZE, so the kernel boots: same here."""
        for label, payload in self._gzip_flips(self.clean_elf()):
            with self.subTest(trailer=label):
                path = self.root / "kernel.img"
                path.write_bytes(E2_DEV.wrap_hdrs_bzimage(payload))
                try:
                    report = SCANNER.scan_tree(
                        self.root, provenance={"partition": "system_a", "container_chain": []},
                    )
                finally:
                    path.unlink(missing_ok=True)
                self.assertEqual(report["hits"], [])
                self.assertEqual(report["unsupported_images"], [])

    def test_b3_xz_check_flip_refused(self):
        xz = bytearray(lzma.compress(self.evil, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32))
        i = xz.find(zlib.crc32(self.evil).to_bytes(4, "little"))
        self.assertGreater(i, 0)
        xz[i] ^= 0xFF
        self.refuse(bytes(xz))

    def test_b3_bzip2_stream_crc_flip_refused(self):
        data = bytearray(bz2.compress(self.evil))
        data[-2] ^= 0x10
        with self.assertRaises(OSError):
            bz2.decompress(bytes(data))
        self.refuse(bytes(data))

    def test_b3_lzma_alone_flipped_byte_refused(self):
        data = bytearray(lzma.compress(self.evil, format=lzma.FORMAT_ALONE))
        data[-3] ^= 0x55
        with self.assertRaises(lzma.LZMAError):
            lzma.decompress(bytes(data), format=lzma.FORMAT_ALONE)
        self.refuse(bytes(data))

    def test_b3_legacy_lz4_cli_writes_then_fails_refused(self):
        """Legacy LZ4 magic, a valid chunk structure the pure-Python decoder
        rejects, and an lz4 CLI that writes the ELF then exits 1."""
        tools = Path(self.temp.name) / "fakebin"
        tools.mkdir()
        evil_path = Path(self.temp.name) / "evil.elf"
        evil_path.write_bytes(self.evil)
        lz4 = tools / "lz4"
        lz4.write_text(f'#!/bin/sh\ncat >/dev/null\ncat "{evil_path}"\nexit 1\n')
        lz4.chmod(0o755)
        block = b"\xff" * 64
        payload = b"\x02!L\x18" + struct.pack("<I", len(block)) + block
        with self.assertRaises(ValueError):
            E2_DEV._lz4_decompress_legacy(payload)
        with mock.patch.dict(os.environ, {"PATH": f"{tools}:{os.environ.get('PATH', '')}"}):
            self.refuse(payload)

    def test_b3_uncompressed_elf_pt_load_past_eof_refused(self):
        elf = bytearray(self.evil)
        struct.pack_into("<Q", elf, 120 + 32, 0x1000)  # second PT_LOAD p_filesz
        self.assertIsNone(E2_DEV._elf64_le_span(bytes(elf)))
        self.refuse(bytes(elf))

    @staticmethod
    def _lz4_literal_block(data):
        """An LZ4 block made of one literal-only sequence (valid LZ4)."""
        n = len(data) - 15
        ext = b"\xff" * (n // 255) + bytes([n % 255])
        return b"\xf0" + ext + data

    def test_b3_legacy_lz4_good_first_chunk_bad_second_refused(self):
        """Codex I1: legacy LZ4 whose first chunk decodes and second is
        damaged; both the native lz4 CLI and the pure-Python decoder (lz4 not
        on PATH) keep the first chunk, which is scanned."""
        good = self._lz4_literal_block(self.evil)
        bad = b"\xff" * 64
        payload = (b"\x02!L\x18" + struct.pack("<I", len(good)) + good
                   + struct.pack("<I", len(bad)) + bad)
        self.assertEqual(E2_DEV._lz4_decompress_block(good), self.evil)
        if shutil.which("lz4"):
            with self.subTest(decoder="lz4 CLI"):
                self.refuse(payload)
        empty = Path(self.temp.name) / "nolz4"
        empty.mkdir()
        with self.subTest(decoder="pure Python"), mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.assertIsNone(shutil.which("lz4"))
            self.refuse(payload)

    def test_b3_uncompressed_elf_section_table_past_eof_refused(self):
        """Codex I1: the section table points outside the file while the
        executable PT_LOAD is intact."""
        elf = bytearray(self.evil)
        struct.pack_into("<Q", elf, 40, 0x10000)          # e_shoff
        struct.pack_into("<HH", elf, 58, 64, 1)           # e_shentsize, e_shnum
        self.assertIsNone(E2_DEV._elf64_le_span(bytes(elf)))
        self.refuse(bytes(elf))

    def test_m4_zip_member_failing_mid_copy_is_scanned_before_delete(self):
        """Codex M4: a ~2 MB stored ELF member with a bad central CRC fails
        after copyfileobj wrote it; the partial bytes are scanned (IFMA named
        first) and the ZIP is still refused unpinnably."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("evil.elf", self.evil + b"A" * (2 * 1024 * 1024),
                             compress_type=zipfile.ZIP_STORED)
        raw = bytearray(buffer.getvalue())
        central = raw.find(b"PK\x01\x02")
        raw[central + 16] ^= 0x01
        self.assert_refused_with_hit("payload.zip", bytes(raw), "payload.zip!/evil.elf+0x110")

    def test_m5_scan_tree_initialisation_errors_are_scan_errors(self):
        missing = Path(self.temp.name) / "missing-root"
        with self.assertRaises(SCANNER.ScanError) as caught:
            SCANNER.scan_tree(missing)
        self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)
        self.assertIn("FileNotFoundError", str(caught.exception))
        scratch = Path(self.temp.name) / "scratch-file"
        scratch.write_text("x")
        with self.assertRaises(SCANNER.ScanError) as caught:
            SCANNER.scan_tree(self.root, scratch_dir=scratch / "sub")
        self.assertNotIsInstance(caught.exception, SCANNER.ImageOpenError)

    def test_b1_failed_decode_without_output_is_unpinnable(self):
        """A decoder failure that kept no byte is not 'never decoded': it is a
        ScanError, not the pinnable ImageOpenError."""
        payload = b"\x1f\x8b\x08\x00" + bytes(6) + b"\xff" * 64  # gzip header, invalid deflate
        self.assert_clean_refusal(
            "kernel.img", E2_DEV.wrap_hdrs_bzimage(payload), r"bzImage extraction failed: .*corrupt gzip payload",
        )

    def test_b1_never_decoded_payload_stays_pinnable(self):
        """UndecodablePayloadError (unsupported payload magic, HdrS slice) is
        the only pinnable bzImage failure."""
        # spec change (gb-ifma023g C2): the "lzo" case moved to
        # LzoKernelC2Tests (an LZO kernel is decoded, never pinnable).
        for label, data in (
            ("unknown", E2_DEV.wrap_hdrs_bzimage(b"NOPE" + bytes(64))),
        ):
            with self.subTest(payload=label):
                report = self.scan_pinned("kernel.img", data)
                self.assertEqual(len(report["accepted_unsupported"]), 1)
                self.assertEqual(report["hits"], [])

    def test_b4_m3_shipped_avb0_pin_through_the_erofs_layer(self):
        """m-3: the shipped pin's full chain, virt APEX -> apex_payload.img
        (EROFS) -> erofs_file etc/fs/microdroid_vbmeta.img, with real
        mkfs.erofs/fsck.erofs."""
        mkfs = shutil.which("mkfs.erofs")
        if not mkfs or not shutil.which("fsck.erofs"):
            self.skipTest("mkfs.erofs/fsck.erofs unavailable")
        manifest = json.loads(
            (Path(__file__).resolve().parents[2] / "builder/ifma/manifest.json").read_text()
        )
        (shipped,) = manifest["unsupported_allowlist"]
        chain = shipped["container_chain"]
        self.assertEqual([n["kind"] for n in chain], ["tree_file", "zip_member", "erofs_file"])
        vbmeta = b"AVB0" + struct.pack(">II", 1, 0) + bytes(65536 - 12)
        src = Path(self.temp.name) / "payload-src"
        (src / "etc/fs").mkdir(parents=True)
        (src / chain[2]["path"]).write_bytes(vbmeta)
        image = Path(self.temp.name) / "apex_payload.img"
        subprocess.run([mkfs, str(image), str(src)], check=True, capture_output=True, timeout=60)
        apex = self.root / chain[0]["path"]
        apex.parent.mkdir(parents=True)
        with zipfile.ZipFile(apex, "w") as archive:
            archive.write(image, chain[1]["path"], compress_type=zipfile.ZIP_STORED)
        provenance = {"partition": "system_a", "container_chain": []}
        with self.assertRaisesRegex(SCANNER.ScanError, "unsupported nested image") as caught:
            SCANNER.scan_tree(self.root, provenance=provenance)
        self.assertIn("microdroid_vbmeta.img", str(caught.exception))
        pin = dict(shipped, sha256=hashlib.sha256(vbmeta).hexdigest())
        report = SCANNER.scan_tree(self.root, provenance=provenance, unsupported_allowlist=[pin])
        self.assertEqual(len(report["accepted_unsupported"]), 1)
        self.assertEqual(report["hits"], [])


class LegacyLz4UnlZ4C1Tests(unittest.TestCase):
    """gb-ifma023g C1: a legacy-LZ4 bzImage payload follows Linux unlz4()
    (magic -> continue, size 0 -> end, < 4 bytes left -> end) and is never
    pinnable. Each case runs with the lz4 CLI and with the pure-Python
    decoder (lz4 hidden from PATH)."""

    setUp = PinAllowlistE2Tests.setUp
    scan_pinned = PinAllowlistE2Tests.scan_pinned
    assert_refused_with_hit = PinAllowlistE2Tests.assert_refused_with_hit
    assert_clean_refusal = PinAllowlistE2Tests.assert_clean_refusal
    clean_elf = BzimagePayloadB3Tests.clean_elf
    HIT = "kernel.img!/vmlinux+0x110"
    MAGIC = b"\x02!L\x18"

    def chunk(self, data):
        block = BzimagePayloadB3Tests._lz4_literal_block(data)
        return struct.pack("<I", len(block)) + block

    def shapes(self, data):
        c = self.chunk(data)
        return (
            ("good+00000000+pad", self.MAGIC + c + bytes(4) + b"\xaa" * 16),
            ("good+magic+good", self.MAGIC + c + self.MAGIC + c),
            ("good+2-byte tail", self.MAGIC + c + b"\x01\x02"),
        )

    def decoders(self):
        empty = Path(self.temp.name) / "nolz4"
        empty.mkdir(exist_ok=True)
        if shutil.which("lz4"):
            yield "lz4 CLI", {}
        yield "pure Python", {"PATH": str(empty)}

    def scan_unpinned(self, data):
        path = self.root / "kernel.img"
        path.write_bytes(data)
        try:
            return SCANNER.scan_tree(
                self.root, provenance={"partition": "system_a", "container_chain": []},
            )
        finally:
            path.unlink(missing_ok=True)

    def test_c1_unlz4_boundaries_with_ifma_refused_with_exact_pin(self):
        for decoder, env in self.decoders():
            for label, payload in self.shapes(self.evil):
                with self.subTest(decoder=decoder, shape=label), mock.patch.dict(os.environ, env):
                    self.assert_refused_with_hit(
                        "kernel.img", E2_DEV.wrap_hdrs_bzimage(payload), self.HIT,
                    )

    def test_c1_unlz4_boundaries_clean_payload_accepted_without_pin(self):
        for decoder, env in self.decoders():
            for label, payload in self.shapes(self.clean_elf()):
                with self.subTest(decoder=decoder, shape=label), mock.patch.dict(os.environ, env):
                    report = self.scan_unpinned(E2_DEV.wrap_hdrs_bzimage(payload))
                    self.assertEqual(report["hits"], [])
                    self.assertEqual(report["unsupported_images"], [])

    def test_c1_first_chunk_size_past_end_refused_with_exact_pin(self):
        block = BzimagePayloadB3Tests._lz4_literal_block(self.evil)
        payload = self.MAGIC + struct.pack("<I", len(block) + 4096) + block
        for decoder, env in self.decoders():
            with self.subTest(decoder=decoder), mock.patch.dict(os.environ, env):
                self.assert_refused_with_hit("kernel.img", E2_DEV.wrap_hdrs_bzimage(payload), self.HIT)

    def test_c1_no_decodable_chunk_with_bad_size_is_unpinnable(self):
        cases = (
            ("size over bound", self.MAGIC + struct.pack("<I", 0x7FFFFFF0) + b"\xff" * 64),
            ("size past end", self.MAGIC + struct.pack("<I", 4096) + b"\xff" * 64),
            ("only size 0", self.MAGIC + bytes(4) + b"\xaa" * 16),
        )
        for decoder, env in self.decoders():
            for label, payload in cases:
                with self.subTest(decoder=decoder, case=label), mock.patch.dict(os.environ, env):
                    self.assert_clean_refusal(
                        "kernel.img", E2_DEV.wrap_hdrs_bzimage(payload), r"corrupt LZ4 legacy payload",
                    )

    def test_c1_shared_model_matches_unlz4(self):
        c = self.chunk(self.evil)
        self.assertEqual(E2_DEV._lz4_decompress_legacy(self.MAGIC + c + bytes(4) + b"\xaa" * 16), self.evil)
        self.assertEqual(E2_DEV._lz4_decompress_legacy(self.MAGIC + c + self.MAGIC + c), self.evil * 2)
        self.assertEqual(E2_DEV._lz4_decompress_legacy(self.MAGIC + c + b"\x01\x02"), self.evil)
        self.assertEqual(E2_DEV._lz4_decompress_legacy(self.MAGIC + c + struct.pack("<I", len(self.evil))), self.evil)
        bad = self.MAGIC + c + struct.pack("<I", 64) + b"\xff" * 64
        with self.assertRaises(ValueError):
            E2_DEV._lz4_decompress_legacy(bad)
        self.assertEqual(E2_DEV._lz4_legacy_good_prefix(bad), self.evil)

    def test_c1_addendum_structural_cases_refused_with_exact_pin(self):
        """cursor-win round 4: trailing 1/3 bytes, 0-size + 1 byte, and a
        second chunk declaring 64 bytes with 8 present."""
        c = self.chunk(self.evil)
        cases = (
            ("good+1 byte", self.MAGIC + c + b"\x01"),
            ("good+3 bytes", self.MAGIC + c + b"\x01\x02\x03"),
            ("good+0-size+1 byte", self.MAGIC + c + bytes(4) + b"\x01"),
            ("second chunk short", self.MAGIC + c + struct.pack("<I", 64) + b"\xab" * 8),
        )
        for decoder, env in self.decoders():
            for label, payload in cases:
                with self.subTest(decoder=decoder, case=label), mock.patch.dict(os.environ, env):
                    self.assert_refused_with_hit("kernel.img", E2_DEV.wrap_hdrs_bzimage(payload), self.HIT)

    def test_c1_exactly_8_mib_single_block_decodes_and_is_scanned(self):
        """A block decoding to exactly 8 MiB has bsz > 8 MiB but <= LZ4
        compressBound(8 MiB); it decodes and is scanned normally."""
        data = self.evil + bytes(8 * 1024 * 1024 - len(self.evil))
        block = BzimagePayloadB3Tests._lz4_literal_block(data)
        self.assertGreater(len(block), 8 * 1024 * 1024)
        self.assertLessEqual(len(block), E2_DEV.LZ4_LEGACY_COMPRESS_BOUND)
        payload = self.MAGIC + struct.pack("<I", len(block)) + block
        self.assertEqual(E2_DEV._lz4_decompress_legacy(payload), data)
        empty = Path(self.temp.name) / "nolz4"
        empty.mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.assert_refused_with_hit("kernel.img", E2_DEV.wrap_hdrs_bzimage(payload), self.HIT)


class LzoKernelC2Tests(unittest.TestCase):
    """gb-ifma023g C2: a CONFIG_KERNEL_LZO bzImage payload is decoded with
    `lzop -dc` and scanned; without a usable lzop it is an unpinnable
    ScanError, not ImageOpenError."""

    setUp = PinAllowlistE2Tests.setUp
    scan_pinned = PinAllowlistE2Tests.scan_pinned
    assert_refused_with_hit = PinAllowlistE2Tests.assert_refused_with_hit
    assert_clean_refusal = PinAllowlistE2Tests.assert_clean_refusal
    clean_elf = BzimagePayloadB3Tests.clean_elf
    scan_unpinned = LegacyLz4UnlZ4C1Tests.scan_unpinned
    HIT = "kernel.img!/vmlinux+0x110"
    FAKE_LZO = b"\x89LZO\x00\x0d\x0a\x1a\x0a" + bytes(64)

    def fake_lzop(self, exit_code):
        tools = Path(self.temp.name) / f"fakelzop{exit_code}"
        tools.mkdir()
        evil_path = Path(self.temp.name) / "evil.elf"
        evil_path.write_bytes(self.evil)
        lzop = tools / "lzop"
        lzop.write_text(f'#!/bin/sh\ncat >/dev/null\ncat "{evil_path}"\nexit {exit_code}\n')
        lzop.chmod(0o755)
        return {"PATH": f"{tools}:{os.environ.get('PATH', '')}"}

    def real_lzo(self, data):
        lzop = shutil.which("lzop")
        out = subprocess.run([lzop, "-9", "-c"], input=data, capture_output=True, check=True).stdout
        return out + struct.pack("<I", len(data))

    def test_c2_lzop_output_is_scanned_with_exact_pin(self):
        for code in (0, 1):
            with self.subTest(fake_lzop_exit=code), mock.patch.dict(os.environ, self.fake_lzop(code)):
                self.assert_refused_with_hit("kernel.img", E2_DEV.wrap_hdrs_bzimage(self.FAKE_LZO), self.HIT)

    def test_c2_lzop_missing_is_unpinnable_scan_error(self):
        empty = Path(self.temp.name) / "nolzop"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.assertIsNone(shutil.which("lzop"))
            self.assert_clean_refusal(
                "kernel.img", E2_DEV.wrap_hdrs_bzimage(self.FAKE_LZO), r"LZO payload: lzop unavailable",
            )

    @unittest.skipUnless(shutil.which("lzop"), "lzop unavailable (not installed in this environment)")
    def test_c2_real_lzo_kernel_with_ifma_refused_with_exact_pin(self):
        self.assert_refused_with_hit("kernel.img", E2_DEV.wrap_hdrs_bzimage(self.real_lzo(self.evil)), self.HIT)

    @unittest.skipUnless(shutil.which("lzop"), "lzop unavailable (not installed in this environment)")
    def test_c2_real_clean_lzo_kernel_accepted_without_pin(self):
        report = self.scan_unpinned(E2_DEV.wrap_hdrs_bzimage(self.real_lzo(self.clean_elf())))
        self.assertEqual(report["hits"], [])
        self.assertEqual(report["unsupported_images"], [])


class DecoderBoundsC3Tests(unittest.TestCase):
    """gb-ifma023g C3: devprofile's bzImage decoders have the scanner's
    timeout (30 s + 1 s per 4 MiB) and an output cap. Hitting either raises
    DecoderLimitError, a DecodedPayloadError carrying what was produced (the
    scanner scans it and refuses unpinnably), never UndecodablePayloadError."""

    setUp = PinAllowlistE2Tests.setUp

    def decode(self, payload):
        path = Path(self.temp.name) / "bzImage"
        path.write_bytes(E2_DEV.wrap_hdrs_bzimage(payload))
        return E2_DEV._decompress_declared_payload(path)

    def test_c3_output_cap_keeps_the_produced_prefix(self):
        big = self.evil + bytes(256 * 1024)
        block = BzimagePayloadB3Tests._lz4_literal_block(big)
        cases = (
            ("gzip", gzip.compress(big, mtime=0)),
            ("xz", lzma.compress(big, format=lzma.FORMAT_XZ)),
            ("bzip2", bz2.compress(big)),
            ("legacy lz4", b"\x02!L\x18" + struct.pack("<I", len(block)) + block),
        )
        self.assertGreater(E2_DEV.DECODER_OUTPUT_CAP, 64 * 1024 * 1024)
        empty = Path(self.temp.name) / "nolz4"
        empty.mkdir()
        for label, payload in cases:
            with self.subTest(fmt=label), mock.patch.object(E2_DEV, "DECODER_OUTPUT_CAP", 4096), \
                    mock.patch.dict(os.environ, {"PATH": str(empty)} if label == "legacy lz4" else {}):
                with self.assertRaises(E2_DEV.DecoderLimitError) as caught:
                    self.decode(payload)
                self.assertIsInstance(caught.exception, E2_DEV.DecodedPayloadError)
                self.assertNotIsInstance(caught.exception, E2_DEV.UndecodablePayloadError)
                self.assertLessEqual(len(caught.exception.decoded), 4096)
                self.assertEqual(caught.exception.decoded[:0x115], self.evil[:0x115])

    def test_c3_cli_output_cap(self):
        tools = Path(self.temp.name) / "fakebin"
        tools.mkdir()
        lz4 = tools / "lz4"
        lz4.write_text("#!/bin/sh\ncat >/dev/null\nhead -c 100000 /dev/zero\n")
        lz4.chmod(0o755)
        with mock.patch.object(E2_DEV, "DECODER_OUTPUT_CAP", 4096), \
                mock.patch.dict(os.environ, {"PATH": f"{tools}:{os.environ.get('PATH', '')}"}):
            with self.assertRaises(E2_DEV.DecoderLimitError) as caught:
                self.decode(b'\x04"M\x18' + bytes(64))
        self.assertEqual(len(caught.exception.decoded), 4096)

    def test_c3_slow_decoder_times_out_and_keeps_its_output(self):
        tools = Path(self.temp.name) / "slowbin"
        tools.mkdir()
        evil_path = Path(self.temp.name) / "evil.elf"
        evil_path.write_bytes(self.evil)
        lz4 = tools / "lz4"
        # Output first: a loaded host can take most of a second to start the
        # shell, and the kept output is what this test checks.
        lz4.write_text(f'#!/bin/sh\ncat "{evil_path}"\ncat >/dev/null\nsleep 60\n')
        lz4.chmod(0o755)
        self.assertEqual(E2_DEV._decoder_timeout(8 * 1024 * 1024), 32)
        started = time.monotonic()
        with mock.patch.object(E2_DEV, "_decoder_timeout", lambda n: 3), \
                mock.patch.dict(os.environ, {"PATH": f"{tools}:{os.environ.get('PATH', '')}"}):
            with self.assertRaisesRegex(E2_DEV.DecoderLimitError, "timed out") as caught:
                self.decode(b'\x04"M\x18' + bytes(64))
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(caught.exception.decoded, self.evil)

    def test_d2_child_closing_stdout_then_sleeping_hits_the_deadline(self):
        tools = Path(self.temp.name) / "sleepbin"
        tools.mkdir()
        lz4 = tools / "lz4"
        lz4.write_text("#!/bin/sh\ncat >/dev/null\nexec >&-\nsleep 25\n")
        lz4.chmod(0o755)
        started = time.monotonic()
        with mock.patch.object(E2_DEV, "_decoder_timeout", lambda n: 1), \
                mock.patch.dict(os.environ, {"PATH": f"{tools}:{os.environ.get('PATH', '')}"}):
            with self.assertRaisesRegex(E2_DEV.DecoderLimitError, "timed out") as caught:
                self.decode(b'\x04"M\x18' + bytes(64))
        self.assertLess(time.monotonic() - started, 15)
        self.assertNotIsInstance(caught.exception, E2_DEV.UndecodablePayloadError)

    @unittest.skipUnless(sys.version_info >= (3, 14), "compression.zstd needs Python 3.14")
    def test_c3_zstd_module_checksum_flip_keeps_the_tail(self):
        from compression import zstd as czstd
        data = self.evil + random.Random(7).randbytes(64 * 1024)
        frame = bytearray(czstd.compress(data, options={czstd.CompressionParameter.checksum_flag: 1}))
        frame[-1] ^= 0xFF
        with self.assertRaises(E2_DEV.DecodedPayloadError) as caught:
            self.decode(bytes(frame))
        self.assertGreaterEqual(len(caught.exception.decoded), len(data) - 1)

    def test_c3_gzip_lenient_has_the_output_cap(self):
        big = gzip.compress(self.evil + bytes(256 * 1024), mtime=0)
        with mock.patch.object(E2_DEV, "DECODER_OUTPUT_CAP", 4096):
            with self.assertRaises(E2_DEV.DecoderLimitError) as caught:
                E2_DEV._gzip_lenient(big)
        self.assertEqual(len(caught.exception.decoded), 4096)


class HdrsPastEofC6Tests(unittest.TestCase):
    """gb-ifma023g C6: an HdrS payload_length running past EOF while the
    start is inside the file: raw[start:] is decoded, scanned and refused
    unpinnably; only nothing decodable stays pinnable."""

    setUp = PinAllowlistE2Tests.setUp
    scan_pinned = PinAllowlistE2Tests.scan_pinned
    assert_refused_with_hit = PinAllowlistE2Tests.assert_refused_with_hit
    clean_elf = BzimagePayloadB3Tests.clean_elf

    def past_eof(self, payload, extra=1):
        bz = bytearray(E2_DEV.wrap_hdrs_bzimage(payload))
        plen = int.from_bytes(bz[0x24c:0x250], "little")
        bz[0x24c:0x250] = (plen + extra).to_bytes(4, "little")
        return bytes(bz)

    def test_c6_gzip_plen_plus_one_refused_with_exact_pin(self):
        self.assert_refused_with_hit(
            "kernel.img", self.past_eof(gzip.compress(self.evil, mtime=0)), "kernel.img!/vmlinux+0x110",
        )

    def test_c6_clean_gzip_plen_past_eof_is_unpinnable(self):
        bz = self.past_eof(gzip.compress(self.clean_elf(), mtime=0))
        PinAllowlistE2Tests.assert_clean_refusal(self, "kernel.img", bz, "past end of file")

    def test_c6_nothing_decodable_past_eof_stays_pinnable(self):
        report = self.scan_pinned("kernel.img", self.past_eof(b"NOPE" + bytes(64)))
        self.assertEqual(len(report["accepted_unsupported"]), 1)
        self.assertEqual(report["hits"], [])

    def test_d1_lzo_without_lzop_past_eof_is_unpinnable(self):
        empty = Path(self.temp.name) / "nolzop"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.assertIsNone(shutil.which("lzop"))
            PinAllowlistE2Tests.assert_clean_refusal(
                self, "kernel.img", self.past_eof(LzoKernelC2Tests.FAKE_LZO), r"lzop unavailable",
            )

    def test_d1_legacy_lz4_nothing_decoded_past_eof_is_unpinnable(self):
        payload = b"\x02!L\x18" + bytes(4) + bytes(16)
        empty = Path(self.temp.name) / "nolz4"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            PinAllowlistE2Tests.assert_clean_refusal(
                self, "kernel.img", self.past_eof(payload), r"past end of file",
            )


if __name__ == "__main__":
    unittest.main()
