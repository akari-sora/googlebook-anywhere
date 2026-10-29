#!/usr/bin/env python3
"""Focused F2-F6 regressions for APEX AVB/container handling."""

import glob
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import ifma


def _footer(original=4096, vbmeta_offset=8192, vbmeta_size=1024, major=1, minor=0):
    return struct.pack(
        ">4sIIQQQ28s",
        b"AVBf",
        major,
        minor,
        original,
        vbmeta_offset,
        vbmeta_size,
        b"",
    )


def _compressed_bytes(path, name):
    raw = Path(path).read_bytes()
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(name)
        return ifma._raw_compressed_zip_entry(raw, info, path), info


def _with_duplicate_signer(apex_path, scheme_id, out_path):
    raw = Path(apex_path).read_bytes()
    cd_offset, cd_size, eocd_offset = ifma._locate_zip_sections(raw)
    size_footer = struct.unpack_from("<Q", raw, cd_offset - 24)[0]
    block_start = cd_offset - 8 - size_footer
    pairs = raw[block_start + 8 : cd_offset - 24]
    rebuilt = bytearray()
    pos = 0
    while pos < len(pairs):
        pair_len = struct.unpack_from("<Q", pairs, pos)[0]
        block_id = struct.unpack_from("<I", pairs, pos + 8)[0]
        value = pairs[pos + 12 : pos + 8 + pair_len]
        if block_id == scheme_id:
            signers, end = ifma._read_len_prefixed(value)
            if end != len(value):
                raise AssertionError("unexpected scheme block layout")
            value = ifma._len_prefixed(signers + signers)
        rebuilt += struct.pack("<QI", 4 + len(value), block_id) + value
        pos += 8 + pair_len
    block_size = len(rebuilt) + 8 + 16
    signing_block = struct.pack("<Q", block_size) + rebuilt + struct.pack("<Q", block_size) + ifma.APK_SIG_BLOCK_MAGIC
    cd = raw[cd_offset : cd_offset + cd_size]
    eocd = bytearray(raw[eocd_offset:])
    struct.pack_into("<I", eocd, 16, block_start + len(signing_block))
    Path(out_path).write_bytes(raw[:block_start] + signing_block + cd + bytes(eocd))


def _with_added_signing_block_pair(apex_path, block_id, value, out_path):
    raw = Path(apex_path).read_bytes()
    cd_offset, cd_size, eocd_offset = ifma._locate_zip_sections(raw)
    size_footer = struct.unpack_from("<Q", raw, cd_offset - 24)[0]
    block_start = cd_offset - 8 - size_footer
    pairs = raw[block_start + 8 : cd_offset - 24]
    pairs += struct.pack("<QI", 4 + len(value), block_id) + value
    block_size = len(pairs) + 8 + 16
    signing_block = struct.pack("<Q", block_size) + pairs + struct.pack("<Q", block_size) + ifma.APK_SIG_BLOCK_MAGIC
    cd = raw[cd_offset : cd_offset + cd_size]
    eocd = bytearray(raw[eocd_offset:])
    struct.pack_into("<I", eocd, 16, block_start + len(signing_block))
    Path(out_path).write_bytes(raw[:block_start] + signing_block + cd + bytes(eocd))


class AvbFooterTests(unittest.TestCase):
    def test_terminal_v1_footer_and_bounds_are_required(self):
        image = b"E" * 4096 + b"\x00" * 4096 + b"V" * 1024 + b"\x00" * 1024 + _footer()
        stripped, size = ifma._strip_avb_footer(image, "payload")
        self.assertEqual(stripped, b"E" * 4096)
        self.assertEqual(size, len(image))

        bad_cases = (
            (image[:-64] + _footer(major=2), "major version"),
            (image[:-64] + _footer(vbmeta_offset=2048), "vbmeta bounds"),
            (image[:-64] + _footer(vbmeta_offset=len(image), vbmeta_size=1), "vbmeta bounds"),
            (image + b"tail", "terminal 64-byte"),
        )
        for bad, message in bad_cases:
            with self.subTest(message=message), self.assertRaisesRegex(SystemExit, message):
                ifma._strip_avb_footer(bad, "payload")

    def test_calc_and_real_footer_share_exact_avb_arguments(self):
        calls = []

        def fake_run(*args):
            calls.append(args)
            return "12345" if "--calc_max_image_size" in args else ""

        with mock.patch.object(ifma, "_run_ot", side_effect=fake_run):
            self.assertEqual(ifma._apex_max_image_size("ot", 65536, "key.pem", "ab" * 32, "key-name"), 12345)
            ifma._add_apex_hashtree_footer(
                "ot", "payload.img", 65536, "key.pem", "ab" * 32, "key-name"
            )
        self.assertEqual(calls[0][3:-1], calls[1][5:])
        self.assertEqual(calls[0][3:-1], ifma._apex_hashtree_args(65536, "key.pem", "ab" * 32, "key-name"))

    def test_payload_partition_grows_until_rebuilt_erofs_fits(self):
        with mock.patch.object(ifma, "_apex_max_image_size", side_effect=(50000, 55000, 70000)) as calc:
            size = ifma._apex_partition_size(
                "ot", 65536, 60000, "key.pem", "ab" * 32, "key-name"
            )
        self.assertEqual(size, 192512)
        self.assertEqual(
            [call.args[1] for call in calc.call_args_list],
            [65536, 126976, 192512],
        )


class StockIdentityTests(unittest.TestCase):
    def test_stock_avb_identity_is_parsed_exactly(self):
        salt = "12" * 32
        parsed = ifma._parse_apex_avb_info(
            f"Footer version: 1.0\nSalt: {salt}\nProp: apex.key -> 'com.example.key'\n",
            "test.apex",
        )
        self.assertEqual(parsed, {"salt": salt, "apex_key": "com.example.key"})
        with self.assertRaisesRegex(SystemExit, "exactly one"):
            ifma._parse_apex_avb_info("Salt: 00\n", "test.apex")

    def test_stock_identity_drift_is_refused(self):
        spec = {"salt": "34" * 32, "apex_key": "expected"}
        with mock.patch.object(
            ifma,
            "_run_ot",
            return_value="Salt: " + "34" * 32 + "\nProp: apex.key -> 'different'\n",
        ):
            with self.assertRaisesRegex(SystemExit, "identity drifted"):
                ifma._stock_apex_avb_identity("ot", "payload.img", spec, "test.apex")


class ApexContainerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="ifma-container-tests-")
        cls.base = Path(cls.temp.name)
        with mock.patch.dict(os.environ, {}, clear=True):
            cls.keys = ifma.ensure_apex_keys(cls.base / "keys")

        cls.stock = cls.base / "stock.apex"
        with zipfile.ZipFile(cls.stock, "w") as zf:
            zf.writestr("apex_manifest.pb", b"manifest" * 127, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("AndroidManifest.xml", b"android-manifest", compress_type=zipfile.ZIP_STORED)
            # Preserve stock ZIP metadata even when the old SourceStamp block
            # is omitted by our signer. The official v2/v3 oracle covers this.
            zf.writestr("stamp-cert-sha256", b"stock-stamp-certificate-digest")
            zf.writestr("apex_pubkey", b"old-pubkey", compress_type=zipfile.ZIP_STORED)
            zf.writestr("apex_payload.img", b"old-payload", compress_type=zipfile.ZIP_STORED)
        cls.unsigned = cls.base / "unsigned.apex"
        ifma.repack_apex_zip(cls.stock, cls.unsigned, b"new-payload" * 1000, b"new-pubkey")
        cls.signed = cls.base / "signed.apex"
        ifma.sign_apex_container(
            cls.unsigned,
            cls.keys["container_key"],
            cls.keys["container_cert_der"],
            cls.signed,
            min_sdk=33,
            max_sdk=37,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_repack_copies_unchanged_compressed_bytes_and_crc(self):
        for name in ("apex_manifest.pb", "AndroidManifest.xml"):
            stock_bytes, stock_info = _compressed_bytes(self.stock, name)
            new_bytes, new_info = _compressed_bytes(self.unsigned, name)
            self.assertEqual(new_bytes, stock_bytes)
            self.assertEqual((new_info.CRC, new_info.file_size), (stock_info.CRC, stock_info.file_size))

    def test_alignment_extra_records_real_alignment(self):
        raw = self.unsigned.read_bytes()
        with zipfile.ZipFile(self.unsigned) as zf:
            info = zf.getinfo("apex_payload.img")
            fn_len, ex_len = struct.unpack_from("<HH", raw, info.header_offset + 26)
            extra_start = info.header_offset + 30 + fn_len
            extra = raw[extra_start : extra_start + ex_len]
            self.assertEqual(struct.unpack_from("<H", extra, 0)[0], ifma.ZIPALIGN_EXTRA_HEADER_ID)
            self.assertEqual(struct.unpack_from("<H", extra, 4)[0], 16384)
            self.assertEqual(extra_start + ex_len, ((extra_start + ex_len + 16383) // 16384) * 16384)

    def test_alignment_extra_covers_every_four_byte_residue(self):
        for offset in range(16):
            with self.subTest(offset=offset):
                extra = ifma._alignment_extra(offset, len("resources.arsc"), 4)
                self.assertEqual((offset + 30 + len("resources.arsc") + len(extra)) % 4, 0)
                if extra:
                    self.assertEqual(struct.unpack_from("<H", extra, 2)[0] + 4, len(extra))

    def test_repack_aligns_every_stored_entry_before_and_after_signing(self):
        for path in (self.unsigned, self.signed):
            raw = path.read_bytes()
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.compress_type == zipfile.ZIP_STORED:
                        fn_len, ex_len = struct.unpack_from("<HH", raw, info.header_offset + 26)
                        self.assertEqual((info.header_offset + 30 + fn_len + ex_len) % 4, 0, info.filename)

    def test_verifier_preserves_stock_sdk_and_unchanged_entry_contract(self):
        result = ifma.verify_apex_container(
            self.signed,
            expected_cert_der=Path(self.keys["container_cert_der"]).read_bytes(),
            stock_apex_path=self.stock,
        )
        self.assertEqual((result["v3_min_sdk"], result["v3_max_sdk"]), (33, 37))

        variants = {
            "crc-and-size": (("apex_manifest.pb", b"changed"), ("AndroidManifest.xml", b"android-manifest")),
            "name": (("renamed.pb", b"manifest" * 127), ("AndroidManifest.xml", b"android-manifest")),
            "order": (("AndroidManifest.xml", b"android-manifest"), ("apex_manifest.pb", b"manifest" * 127)),
        }
        for label, unchanged in variants.items():
            changed_stock = self.base / f"changed-stock-{label}.apex"
            with zipfile.ZipFile(changed_stock, "w") as zf:
                for name, data in unchanged:
                    zf.writestr(name, data)
                zf.writestr("apex_pubkey", b"old-pubkey")
                zf.writestr("apex_payload.img", b"old-payload")
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "unchanged ZIP entry"):
                ifma.verify_apex_container(self.signed, stock_apex_path=changed_stock)

    def test_valid_signature_cannot_hide_mutable_entry_reordering(self):
        stock = self.base / "interleaved-stock.apex"
        with zipfile.ZipFile(stock, "w") as zf:
            zf.writestr("a", b"a")
            zf.writestr("apex_payload.img", b"old-payload")
            zf.writestr("b", b"b")
            zf.writestr("apex_pubkey", b"old-pubkey")

        reordered_source = self.base / "reordered-source.apex"
        with zipfile.ZipFile(reordered_source, "w") as zf:
            zf.writestr("a", b"a")
            zf.writestr("b", b"b")
            zf.writestr("apex_pubkey", b"old-pubkey")
            zf.writestr("apex_payload.img", b"old-payload")
        unsigned = self.base / "reordered-unsigned.apex"
        ifma.repack_apex_zip(reordered_source, unsigned, b"new-payload", b"new-pubkey")
        signed = self.base / "reordered-signed.apex"
        ifma.sign_apex_container(
            unsigned,
            self.keys["container_key"],
            self.keys["container_cert_der"],
            signed,
            min_sdk=33,
            max_sdk=37,
        )
        ifma.verify_apex_container(signed)
        with self.assertRaisesRegex(ValueError, "unchanged ZIP entry"):
            ifma.verify_apex_container(signed, stock_apex_path=stock)

    def test_zip_contract_rejects_missing_and_duplicate_names(self):
        cases = (
            ("missing-payload", ("apex_pubkey", "other"), "exactly one apex_payload.img"),
            ("missing-pubkey", ("apex_payload.img", "other"), "exactly one apex_pubkey"),
            ("duplicate-payload", ("apex_payload.img", "apex_payload.img", "apex_pubkey"), "exactly one apex_payload.img"),
            ("duplicate-pubkey", ("apex_payload.img", "apex_pubkey", "apex_pubkey"), "exactly one apex_pubkey"),
            ("duplicate-other", ("apex_payload.img", "apex_pubkey", "other", "other"), "duplicate ZIP entry names"),
        )
        for label, names, message in cases:
            path = self.base / f"{label}.apex"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(path, "w") as zf:
                    for name in names:
                        zf.writestr(name, name.encode())
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                ifma._zip_unchanged_entry_contract(path)

    def test_v2_and_v3_require_exactly_one_signer(self):
        for scheme_id, label in (
            (ifma.APK_SIG_SCHEME_V2_ID, "v2"),
            (ifma.APK_SIG_SCHEME_V3_ID, "v3"),
        ):
            duplicate = self.base / f"duplicate-{label}.apex"
            _with_duplicate_signer(self.signed, scheme_id, duplicate)
            with self.subTest(scheme=label), self.assertRaisesRegex(ValueError, "expected exactly one"):
                ifma.verify_apex_container(duplicate)

    def test_unknown_signing_block_pair_is_refused(self):
        unknown = self.base / "unknown-pair.apex"
        _with_added_signing_block_pair(self.signed, 0x12345678, b"unknown", unknown)
        with self.assertRaisesRegex(ValueError, "unexpected APK Signing Block ID set"):
            ifma.verify_apex_container(unknown)

    def test_source_stamp_signing_block_pair_is_refused(self):
        stamped = self.base / "source-stamp.apex"
        _with_added_signing_block_pair(
            self.signed, ifma.SOURCE_STAMP_V2_BLOCK_ID, b"stale-source-stamp", stamped
        )
        with self.assertRaisesRegex(ValueError, "unexpected APK Signing Block ID set"):
            ifma.verify_apex_container(stamped)

    def test_stock_source_stamp_block_is_dropped_and_zip_entries_preserved(self):
        stamped = self.base / "stamped-stock.apex"
        _with_added_signing_block_pair(
            self.signed, ifma.SOURCE_STAMP_V2_BLOCK_ID, b"stock-source-stamp", stamped
        )
        result = ifma.verify_apex_container(stamped, allow_stock_source_stamp=True)
        self.assertTrue(result['v2_verified'] and result['v3_verified'])
        self.assertTrue(result['source_stamp_present'])
        unsigned = self.base / "stamped-rebuilt-unsigned.apex"
        signed = self.base / "stamped-rebuilt.apex"
        ifma.repack_apex_zip(stamped, unsigned, b'rebuilt-payload', b'rebuilt-pubkey')
        ifma.sign_apex_container(unsigned, self.keys['container_key'],
                                 self.keys['container_cert_der'], signed, min_sdk=33, max_sdk=37)
        rebuilt = ifma.verify_apex_container(signed, stock_apex_path=stamped)
        self.assertFalse(rebuilt['source_stamp_present'])
        with zipfile.ZipFile(stamped) as stock_zip, zipfile.ZipFile(signed) as rebuilt_zip:
            for old in stock_zip.infolist():
                if old.filename in ('apex_payload.img', 'apex_pubkey'):
                    continue
                new = rebuilt_zip.getinfo(old.filename)
                self.assertEqual((old.CRC, old.file_size), (new.CRC, new.file_size))
                self.assertEqual(ifma._raw_compressed_zip_entry(stamped.read_bytes(), old, stamped),
                                 ifma._raw_compressed_zip_entry(signed.read_bytes(), new, signed))

    def test_source_stamp_allowance_cannot_be_used_for_rebuilt_verification(self):
        for output_args in ({'expected_cert_der': b'certificate'}, {'stock_apex_path': self.stock}):
            with self.subTest(output_args=output_args), self.assertRaisesRegex(ValueError, 'only for stock input'):
                ifma.verify_apex_container(self.signed, allow_stock_source_stamp=True, **output_args)

    def test_stock_source_stamp_does_not_allow_unknown_pairs_or_content_drift(self):
        stamped = self.base / "stamped-negative.apex"
        _with_added_signing_block_pair(
            self.signed, ifma.SOURCE_STAMP_V2_BLOCK_ID, b"stock-source-stamp", stamped
        )
        unknown = self.base / "stamped-unknown.apex"
        _with_added_signing_block_pair(stamped, 0x12345678, b'unknown', unknown)
        with self.assertRaisesRegex(ValueError, 'unexpected APK Signing Block ID set'):
            ifma.verify_apex_container(unknown, allow_stock_source_stamp=True)
        raw = bytearray(stamped.read_bytes())
        with zipfile.ZipFile(stamped) as zf:
            info = zf.getinfo('apex_payload.img')
            name_size, extra_size = struct.unpack_from('<HH', raw, info.header_offset+26)
            raw[info.header_offset+30+name_size+extra_size] ^= 1
        tampered = self.base / 'stamped-tampered.apex'
        tampered.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'content digest mismatch'):
            ifma.verify_apex_container(tampered, allow_stock_source_stamp=True)

    def test_patch_apex_verifies_stamped_stock_before_payload_rebuild(self):
        stamped = self.base / "stamped-patch-input.apex"
        _with_added_signing_block_pair(
            self.signed, ifma.SOURCE_STAMP_V2_BLOCK_ID, b"stock-source-stamp", stamped
        )
        with mock.patch.object(ifma, '_stock_apex_avb_identity', side_effect=RuntimeError('stock verified')):
            with self.assertRaisesRegex(RuntimeError, 'stock verified'):
                ifma.patch_apex_file(stamped, 'apex/com.android.hardware.audio.desktop.apex',
                                     self.base/'stamped-patch-work', self.base/'otatools', self.keys)

class ApexContainerOracleTests(unittest.TestCase):
    category = "oracle"

    @classmethod
    def setUpClass(cls):
        ApexContainerTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        ApexContainerTests.tearDownClass()

    def test_official_apksigner_oracle(self):
        java = shutil.which("java")
        android_home = os.environ.get("ANDROID_HOME")
        jars = sorted(glob.glob(str(Path(android_home) / "build-tools/*/lib/apksigner.jar"))) if android_home else []
        if not java or not jars:
            self.skipTest("java or ANDROID_HOME build-tools apksigner.jar unavailable")
        for min_sdk, max_sdk in ((24, 27), (33, 37)):
            result = subprocess.run(
                [
                    java,
                    "-jar",
                    jars[-1],
                    "verify",
                    "--verbose",
                    "--min-sdk-version",
                    str(min_sdk),
                    "--max-sdk-version",
                    str(max_sdk),
                    str(ApexContainerTests.signed),
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
