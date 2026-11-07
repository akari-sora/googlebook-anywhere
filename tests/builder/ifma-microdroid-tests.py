#!/usr/bin/env python3
"""Explicit guest scope; public synthetic inputs, never a private guest ELF."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
IFMA = ROOT / "builder/ifma"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P = load("manager_scope_patcher", IFMA / "patch-ifma.py")
FIXTURE = load("manager_scope_fixture", Path(__file__).with_name("test-patch-ifma.py"))
SCOPE = ("microdroid", "system")
MANAGER_PIN = "4dc035949d55c4985c9f36c3d90012317261dce01cd07cb2d961434ac310d326"


class GuestScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ifma-guest-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "tree"
        self.target = self.root / "system/bin/microdroid_manager"
        self.target.parent.mkdir(parents=True)
        self.original, self.patched, self.row = FIXTURE.synthetic_elf()
        self.row["path"] = "system/bin/microdroid_manager"
        for key, data in (("function_sha256", self.original),
                          ("patched_function_sha256", self.patched)):
            off = P.Elf(data).v2o(self.row["function_va"], self.row["function_size"], True)
            self.row[key] = hashlib.sha256(data[off:off + self.row["function_size"]]).hexdigest()
        self.target.write_bytes(self.original)
        self.target.chmod(0o750)
        self.manifest = self.base / "manager.json"
        self.report = self.base / "report.json"

    def run_guest(self, apply=True):
        data = (json.dumps(self.row, indent=2) + "\n").encode()
        self.manifest.write_bytes(data)
        # Only fixture inputs bypass the real shipped row pin, never product code.
        with mock.patch.object(P, "MANAGER_MANIFEST_SHA256", hashlib.sha256(data).hexdigest()):
            return P.run(self.root, self.manifest, self.report, apply, scope=SCOPE)

    def test_shipped_guest_row_is_exact_and_does_not_change_host_manifest(self):
        self.assertEqual(P.MANAGER_SCOPE, SCOPE)
        self.assertEqual(P.MANAGER_MANIFEST_SHA256, MANAGER_PIN)
        path = IFMA / "microdroid-manager.json"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), MANAGER_PIN)
        row = P._load_manager_manifest(path)
        self.assertEqual(row["path"], "system/bin/microdroid_manager")
        self.assertEqual(len(P._load_manifest(IFMA / "manifest.json")), 17)
        self.assertEqual(P.TARGET_COUNT, 17)
        self.assertNotIn(row["path"], P.EXPECTED_PATHS)
        self.assertEqual(sum(P.SCOPED_COUNTS.values()), 17)

    def test_verify_apply_and_reapply_use_existing_pipeline(self):
        self.assertEqual(self.run_guest(False)["changed"], [])
        self.assertEqual(self.target.read_bytes(), self.original)
        first = self.run_guest()
        self.assertEqual(first["scope"], "microdroid:system")
        self.assertEqual(first["changed"], [self.row["path"]])
        self.assertEqual(len(first["targets"]), 1)
        self.assertEqual(self.target.read_bytes(), self.patched)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o750)
        self.assertEqual(self.run_guest()["changed"], [])

    def test_changed_manifest_bytes_refuse_before_mutation(self):
        self.manifest.write_text(json.dumps(self.row))
        with self.assertRaisesRegex(P.PatchError, "manager manifest.*SHA256"):
            P.run(self.root, self.manifest, self.report, True, scope=SCOPE)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertFalse(self.report.exists())

    def test_other_scopes_cannot_select_guest_row(self):
        for scope in (None, ("partition", "system_a"), ("microdroid", "other")):
            with self.subTest(scope=scope):
                self.manifest.write_text(json.dumps(self.row))
                with self.assertRaises(P.PatchError):
                    P.run(self.root, self.manifest, self.report, True, scope=scope)
                self.assertEqual(self.target.read_bytes(), self.original)
        with self.assertRaises(P.PatchError):
            P.run(self.root, IFMA / "manifest.json", self.report, True, scope=SCOPE)

    def test_wrong_fixed_path_refuses_even_with_fixture_pin(self):
        self.row["path"] = "system/bin/other"
        with self.assertRaisesRegex(P.PatchError, "manager.*path"):
            self.run_guest()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_both_function_hashes_are_required_and_verified(self):
        for key in ("function_sha256", "patched_function_sha256"):
            with self.subTest(key=key):
                saved = self.row[key]
                self.row[key] = "0" * 64
                with self.assertRaisesRegex(P.PatchError, "function.*SHA256"):
                    self.run_guest()
                self.assertEqual(self.target.read_bytes(), self.original)
                self.assertFalse(self.report.exists())
                del self.row[key]
                with self.assertRaises(P.PatchError):
                    self.run_guest()
                self.row[key] = saved

    def test_whole_function_must_be_executable_file_backed(self):
        self.row["function_size"] = len(self.original) * 2
        with self.assertRaises(P.PatchError):
            self.run_guest()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_patched_input_still_requires_complete_function_hash_contract(self):
        self.target.write_bytes(self.patched)
        del self.row["function_sha256"]
        with self.assertRaisesRegex(P.PatchError, "function.*SHA256"):
            self.run_guest()
        self.assertEqual(self.target.read_bytes(), self.patched)

    def test_nonexecutable_mapping_refuses_before_replacement(self):
        import struct
        data = bytearray(self.original)
        struct.pack_into("<I", data, 68, 4)  # PT_LOAD flags: read, not execute.
        self.target.write_bytes(data)
        with self.assertRaisesRegex(P.PatchError, "file-backed load segment"):
            self.run_guest()
        self.assertEqual(self.target.read_bytes(), data)

    def test_unknown_input_bytes_refuse(self):
        self.target.write_bytes(self.original[:-1] + b"\xff")
        with self.assertRaisesRegex(P.PatchError, "unknown full-file SHA256"):
            self.run_guest()
        self.assertFalse(self.report.exists())

    def test_context_and_table_checks_are_not_bypassed(self):
        for field in ("context_hex", "table_hex"):
            with self.subTest(field=field):
                value = self.row[field]
                self.row[field] = "ff" + value[2:]
                with self.assertRaises(P.PatchError):
                    self.run_guest()
                self.assertEqual(self.target.read_bytes(), self.original)
                self.row[field] = value

    def test_report_target_and_manifest_aliases_refuse(self):
        for report in (self.target, self.manifest):
            with self.subTest(report=report):
                self.report = report
                with self.assertRaisesRegex(P.PatchError, "report path aliases"):
                    self.run_guest()
                self.assertEqual(self.target.read_bytes(), self.original)

    def test_symlink_and_hardlink_targets_refuse(self):
        real = self.target.with_name("original")
        self.target.rename(real)
        self.target.symlink_to(real.name)
        with self.assertRaisesRegex(P.PatchError, "symlink"):
            self.run_guest()
        self.target.unlink()
        os.link(real, self.target)
        with self.assertRaisesRegex(P.PatchError, "hardlink"):
            self.run_guest()
        self.assertEqual(real.read_bytes(), self.original)

    def test_cli_selects_guest_manifest_only_for_explicit_scope(self):
        report = {"targets": [], "changed": []}
        for scope, expected in (("microdroid:system", "microdroid-manager.json"),
                                ("partition:system_a", "manifest.json")):
            with self.subTest(scope=scope), mock.patch.object(P, "run", return_value=report) as run:
                self.assertEqual(P.main(["--root", str(self.root), "--report", str(self.report),
                                         "--scope", scope]), 0)
                self.assertEqual(Path(run.call_args.args[1]).name, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
