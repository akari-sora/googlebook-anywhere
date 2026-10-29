#!/usr/bin/env python3
"""Focused regression tests for persistent APEX keys and APK signer identity."""

import glob
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
IFMA_PATH = Path(os.environ.get("IFMA_TEST_MODULE", ROOT / "builder/lib/ifma.py"))
SPEC = importlib.util.spec_from_file_location("ifma_key_signing_under_test", IFMA_PATH)
ifma = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ifma)


def _copy_keyset(source, destination):
    shutil.copytree(source, destination)
    destination.chmod(0o700)
    (destination / "apex_payload.pem").chmod(0o600)
    (destination / "apex_container.pem").chmod(0o600)


class ApexKeysetTests(unittest.TestCase):
    @classmethod
    def _ensure_fixture(cls):
        if hasattr(cls, "_class_tmp"):
            return
        cls._class_tmp = tempfile.TemporaryDirectory(prefix="ifma-key-tests-")
        cls.base = Path(cls._class_tmp.name)
        cls.seed = cls.base / "seed"
        with mock.patch.dict(os.environ, {}, clear=True):
            ifma.ensure_apex_keys(cls.seed)

    @classmethod
    def setUpClass(cls):
        cls._ensure_fixture()

    def setUp(self):
        type(self)._ensure_fixture()
        self.case = Path(tempfile.mkdtemp(prefix="case-", dir=self.base))

    def tearDown(self):
        shutil.rmtree(self.case)

    def test_default_is_next_to_avb_key_and_survives_fresh_outputs(self):
        avb_key = self.case / "persistent/keys/vbmeta.pem"
        avb_key.parent.mkdir(parents=True)
        avb_key.write_text("path anchor only\n", encoding="utf-8")
        out1 = self.case / "out-one"
        out2 = self.case / "out-two"
        env = {"GBOS_AVB_KEY": str(avb_key), "GBOS_OUT": str(out1)}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(ifma._default_keys_dir(), avb_key.parent / "apex-keys")
            keys1 = ifma.ensure_apex_keys()
            fingerprints_before_second_call = dict(ifma.apex_key_fingerprints(keys1))
        with mock.patch.dict(
            os.environ, {"GBOS_AVB_KEY": str(avb_key), "GBOS_OUT": str(out2)}, clear=True
        ):
            keys2, fingerprints2 = ifma.ensure_apex_keys_and_fingerprints()
        self.assertEqual(fingerprints_before_second_call, fingerprints2)
        self.assertEqual(Path(keys1["payload_key"]), Path(keys2["payload_key"]))

    def test_explicit_directory_wins(self):
        explicit = self.case / "explicit"
        env = {
            "GBOS_APEX_KEYS_DIR": str(explicit),
            "GBOS_AVB_KEY": str(self.case / "avb/vbmeta.pem"),
            "GBOS_OUT": str(self.case / "out"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(ifma._default_keys_dir(), explicit)

    def test_partial_keyset_stops_without_replacing_existing_key(self):
        keys_dir = self.case / "keys"
        _copy_keyset(self.seed, keys_dir)
        payload_before = hashlib.sha256((keys_dir / "apex_payload.pem").read_bytes()).hexdigest()
        container_before = hashlib.sha256((keys_dir / "apex_container.pem").read_bytes()).hexdigest()
        (keys_dir / "apex_container.der").unlink()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "partial APEX keyset"):
                ifma.ensure_apex_keys(keys_dir)
        self.assertEqual(payload_before, hashlib.sha256((keys_dir / "apex_payload.pem").read_bytes()).hexdigest())
        self.assertEqual(container_before, hashlib.sha256((keys_dir / "apex_container.pem").read_bytes()).hexdigest())
        self.assertFalse((keys_dir / "apex_container.der").exists())

    def test_0644_private_key_stops(self):
        keys_dir = self.case / "keys"
        _copy_keyset(self.seed, keys_dir)
        (keys_dir / "apex_payload.pem").chmod(0o644)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "mode 0600"):
                ifma.ensure_apex_keys(keys_dir)

    def test_wrong_directory_mode_stops(self):
        keys_dir = self.case / "keys"
        _copy_keyset(self.seed, keys_dir)
        keys_dir.chmod(0o755)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "mode 0700"):
                ifma.ensure_apex_keys(keys_dir)

    def test_keys_directory_inside_output_stops(self):
        out = self.case / "out"
        with mock.patch.dict(os.environ, {"GBOS_OUT": str(out)}, clear=True):
            with self.assertRaisesRegex(SystemExit, "outside GBOS_OUT"):
                ifma.ensure_apex_keys(out / "keys")
        self.assertFalse((out / "keys").exists())

    def test_symlink_key_stops(self):
        keys_dir = self.case / "keys"
        _copy_keyset(self.seed, keys_dir)
        payload = keys_dir / "apex_payload.pem"
        real_payload = keys_dir / "payload.real"
        payload.rename(real_payload)
        payload.symlink_to(real_payload.name)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "non-symlink"):
                ifma.ensure_apex_keys(keys_dir)

    def test_wrong_size_key_stops(self):
        keys_dir = self.case / "keys"
        _copy_keyset(self.seed, keys_dir)
        subprocess.run(
            ["openssl", "genrsa", "-out", str(keys_dir / "apex_payload.pem"), "2048"],
            check=True,
            capture_output=True,
        )
        (keys_dir / "apex_payload.pem").chmod(0o600)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "RSA-4096"):
                ifma.ensure_apex_keys(keys_dir)

    def test_wrong_key_type_stops(self):
        keys_dir = self.case / "keys"
        _copy_keyset(self.seed, keys_dir)
        subprocess.run(
            ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(keys_dir / "apex_payload.pem")],
            check=True,
            capture_output=True,
        )
        (keys_dir / "apex_payload.pem").chmod(0o600)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "invalid APEX payload key"):
                ifma.ensure_apex_keys(keys_dir)

    def test_mismatched_container_certificate_stops(self):
        keys_dir = self.case / "keys"
        other = self.case / "other"
        _copy_keyset(self.seed, keys_dir)
        with mock.patch.dict(os.environ, {}, clear=True):
            other_keys = ifma.ensure_apex_keys(other)
        shutil.copyfile(other_keys["container_cert_der"], keys_dir / "apex_container.der")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "do not match"):
                ifma.ensure_apex_keys(keys_dir)


class ApexContainerSignerTests(unittest.TestCase):
    @classmethod
    def _ensure_fixture(cls):
        if hasattr(cls, "_class_tmp"):
            return
        cls._class_tmp = tempfile.TemporaryDirectory(prefix="ifma-signing-tests-")
        cls.base = Path(cls._class_tmp.name)
        with mock.patch.dict(os.environ, {}, clear=True):
            cls.keys = ifma.ensure_apex_keys(cls.base / "keys")
        raw_zip = cls.base / "raw.apex"
        with zipfile.ZipFile(raw_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("apex_manifest.pb", b"synthetic-manifest")
            zf.writestr("AndroidManifest.xml", b"synthetic-binary-manifest")
            zf.writestr("apex_pubkey", b"synthetic-payload-public-key")
            zf.writestr("apex_payload.img", b"P" * (2 * 1024 * 1024 + 17))
        unsigned = cls.base / "unsigned.apex"
        ifma.repack_apex_zip(
            raw_zip,
            unsigned,
            b"P" * (2 * 1024 * 1024 + 17),
            b"synthetic-payload-public-key",
        )
        cls.signed = cls.base / "signed.apex"
        ifma.sign_apex_container(
            unsigned,
            cls.keys["container_key"],
            cls.keys["container_cert_der"],
            cls.signed,
            min_sdk=33,
            max_sdk=37,
        )
        cls.pub_der = subprocess.run(
            [
                "openssl", "pkey", "-in", str(cls.keys["container_key"]),
                "-pubout", "-outform", "DER",
            ],
            check=True,
            capture_output=True,
        ).stdout

    @classmethod
    def setUpClass(cls):
        cls._ensure_fixture()

    def setUp(self):
        type(self)._ensure_fixture()

    def _mutate_signer_spki(self, signer_number):
        raw = bytearray(self.signed.read_bytes())
        hits = []
        pos = 0
        while True:
            pos = raw.find(self.pub_der, pos)
            if pos < 0:
                break
            hits.append(pos)
            pos += len(self.pub_der)
        self.assertEqual(len(hits), 4, "expected cert and signer SPKI in each of v2 and v3")
        signer_hits = (hits[1], hits[3])
        raw[signer_hits[signer_number] + len(self.pub_der) - 16] ^= 1
        out = self.base / f"mutated-signer-{signer_number}.apex"
        out.write_bytes(raw)
        return out

    def test_valid_v2_v3_container(self):
        result = ifma.verify_apex_container(
            self.signed, expected_cert_der=Path(self.keys["container_cert_der"]).read_bytes()
        )
        self.assertTrue(result["v2_verified"] and result["v3_verified"])

    def test_v2_signer_spki_byte_change_is_refused(self):
        with self.assertRaisesRegex(ValueError, "v2 signer public key does not match"):
            ifma.verify_apex_container(self._mutate_signer_spki(0))

    def test_v3_signer_spki_byte_change_is_refused(self):
        with self.assertRaisesRegex(ValueError, "v3 signer public key does not match"):
            ifma.verify_apex_container(self._mutate_signer_spki(1))

class ApexContainerOracleTests(unittest.TestCase):
    category = "oracle"

    def test_official_apksigner_oracle(self):
        ApexContainerSignerTests._ensure_fixture()
        signed = ApexContainerSignerTests.signed
        java = shutil.which("java")
        android_home = os.environ.get("ANDROID_HOME")
        jars = sorted(glob.glob(str(Path(android_home) / "build-tools/*/lib/apksigner.jar"))) if android_home else []
        if not java or not jars:
            reason = "java missing" if not java else "ANDROID_HOME build-tools apksigner.jar missing"
            print(f"SKIP official apksigner oracle: {reason}")
            self.skipTest(reason)
        jar = jars[-1]
        for min_sdk, max_sdk in ((24, 27), (33, 36)):
            res = subprocess.run(
                [
                    java, "-jar", jar, "verify", "--verbose",
                    "--min-sdk-version", str(min_sdk), "--max-sdk-version", str(max_sdk),
                    str(signed),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        mutated = ApexContainerSignerTests()._mutate_signer_spki(0)
        rejected = subprocess.run(
            [
                java, "-jar", jar, "verify", "--verbose",
                "--min-sdk-version", "24", "--max-sdk-version", "27",
                str(mutated),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rejected.returncode, 0, "apksigner accepted a mutated signer SPKI")

        mutated_v3 = ApexContainerSignerTests()._mutate_signer_spki(1)
        rejected_v3 = subprocess.run(
            [
                java, "-jar", jar, "verify", "--verbose",
                "--min-sdk-version", "33", "--max-sdk-version", "36",
                str(mutated_v3),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rejected_v3.returncode, 0, "apksigner accepted a mutated v3 signer SPKI")


if __name__ == "__main__":
    unittest.main(verbosity=2)
