#!/usr/bin/env python3
"""Public synthetic exact-addition policy tests, not private ISA acceptance."""
import ast
import copy
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
SPEC = importlib.util.spec_from_file_location("ally_vendor_policy_ifma", ROOT / "builder/lib/ifma.py")
ifma = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ifma)
FIXTURE_SPEC = importlib.util.spec_from_file_location("ally_vendor_public_elf", Path(__file__).with_name("ifma-scan-tests.py"))
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)

ALLY = {"model": {"id": "rog-ally"}, "display": {"stack": "gop-swiftshader"}, "cpu_compat": {"patch_ifma": True, "patch_ally_bootstrap": True}}
VENDOR = {"partition": "vendor_a", "container_chain": []}
PATHS = ("lib/dev-modules/uas.ko", "lib64/hw/vulkan.pastel.so")


class AdditionPolicyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.tree = self.root / "vendor"
        self.tree.mkdir()
        self.specs = []
        for path, encoding in zip(PATHS, (fixtures.EncodingTests.LUQ, fixtures.EncodingTests.HUQ)):
            # movabs immediate contains the public raw pattern; no private bytes.
            data = fixtures.elf_with_segments(b"\x48\xb8" + encoding + b"\x90" * 3 + b"\xc3")
            target = self.tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.specs.append({"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                               "file_offset": 0x112, "instruction": "vpmadd52luq" if path == PATHS[0] else "vpmadd52huq",
                               "reason": "public synthetic movabs immediate"})

    def configured(self, proof=True, specs=None):
        patches = mock.patch.multiple(ifma,
            _ALLY_VENDOR_ADDITIONS_EVIDENCE_CONFIRMED=proof,
            _ALLY_VENDOR_ADDITIONS=tuple(self.specs if specs is None else specs))
        return patches

    def scan(self, profile=ALLY, **kwargs):
        with mock.patch.object(ifma, "_manifest_scan_policy", return_value=([], [], [])), \
                mock.patch.object(ifma.SCAN_IFMA, "_find_disassembler", return_value=()):
            return ifma.scan_rebuilt_tree(self.tree, "arbitrary display label", provenance=kwargs.pop("provenance", VENDOR),
                                          rebuilt_vendor_profile=profile, **kwargs)

    def test_pending_evidence_gate_refuses_before_target_open(self):
        with mock.patch.object(ifma, "_ALLY_VENDOR_ADDITIONS_EVIDENCE_CONFIRMED", False), \
                mock.patch.object(ifma.os, "open") as opened:
            with self.assertRaisesRegex(SystemExit, "evidence.*pending"):
                self.scan()
            opened.assert_not_called()

    def test_flag_alone_cannot_accept_pending_reasons(self):
        specs = copy.deepcopy(self.specs)
        specs[0]["reason"] = "PENDING_ACTUAL_ANNOTATION"
        with self.configured(specs=specs), mock.patch.object(ifma.os, "open") as opened:
            with self.assertRaisesRegex(SystemExit, "evidence.*pending"):
                self.scan()
            opened.assert_not_called()

    def test_exact_two_public_additions_are_counted(self):
        with self.configured():
            report = self.scan()
        self.assertEqual(report["hits"], [])
        self.assertEqual(len(report["accepted_non_instructions"]), 2)
        self.assertEqual({row["container_chain"][0]["path"] for row in report["accepted_non_instructions"]}, set(PATHS))

    def test_legacy_profile_does_not_enable_overlay(self):
        for profile in ({}, {"model": {"id": "kano"}}, {"cpu_compat": {"patch_ally_bootstrap": False}}):
            with self.subTest(profile=profile), self.configured():
                with self.assertRaisesRegex(SystemExit, "forbidden VEX"):
                    self.scan(profile)

    def test_nonboolean_flags_and_wrong_model_refuse(self):
        for flag in ("true", 1, None, [], {}):
            profile = copy.deepcopy(ALLY)
            profile["cpu_compat"]["patch_ally_bootstrap"] = flag
            with self.subTest(flag=flag), self.configured(), self.assertRaises(SystemExit):
                self.scan(profile)
        for value in (False, 1, "true", None):
            profile = copy.deepcopy(ALLY)
            profile["cpu_compat"]["patch_ifma"] = value
            with self.subTest(ifma=value), self.configured(), self.assertRaises(SystemExit):
                self.scan(profile)
        profile = copy.deepcopy(ALLY)
        profile["model"]["id"] = "kano"
        with self.configured(), self.assertRaisesRegex(SystemExit, "rog-ally"):
            self.scan(profile)

    def test_wrong_provenance_or_kind_refuses_without_using_label(self):
        for provenance, kind in (({"partition": "system_a", "container_chain": []}, "tree_file"),
                                 ({"partition": "vendor_a", "container_chain": [{"kind": "zip_member", "path": "x"}]}, "tree_file"),
                                 (VENDOR, "erofs_file")):
            with self.subTest(provenance=provenance, kind=kind), self.configured(), self.assertRaisesRegex(SystemExit, "scope"):
                self.scan(provenance=provenance, root_kind=kind)

    def test_missing_target_refuses(self):
        (self.tree / PATHS[1]).unlink()
        with self.configured(), self.assertRaises(SystemExit):
            self.scan()

    def test_same_size_content_substitution_refuses(self):
        target = self.tree / PATHS[0]
        data = bytearray(target.read_bytes())
        data[-1] ^= 1
        target.write_bytes(data)
        with self.configured(), self.assertRaisesRegex(SystemExit, "SHA256"):
            self.scan()

    def test_wrong_offset_refuses(self):
        specs = copy.deepcopy(self.specs)
        specs[0]["file_offset"] += 1
        with self.configured(specs=specs), self.assertRaisesRegex(SystemExit, "candidate"):
            self.scan()

    def test_parent_symlink_refuses(self):
        parent = self.tree / "lib/dev-modules"
        moved = self.root / "elsewhere"
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        with self.configured(), self.assertRaises(SystemExit):
            self.scan()

    def test_root_ancestor_symlink_refuses(self):
        alias = self.root / "alias"
        alias.symlink_to(self.tree, target_is_directory=True)
        self.tree = alias
        with self.configured(), self.assertRaises(SystemExit):
            self.scan()

    def test_leaf_symlink_and_hardlink_refuse(self):
        target = self.tree / PATHS[0]
        actual = self.root / "actual"
        target.rename(actual)
        target.symlink_to(actual)
        with self.configured(), self.assertRaises(SystemExit):
            self.scan()
        target.unlink()
        target.hardlink_to(actual)
        with self.configured(), self.assertRaisesRegex(SystemExit, "identity"):
            self.scan()

    def test_duplicate_source_pin_refuses(self):
        with self.configured(specs=self.specs + [self.specs[0]]), self.assertRaisesRegex(SystemExit, "duplicate"):
            self.scan()

    def test_existing_manifest_duplicate_remains_refused(self):
        with self.configured(), ifma._ally_vendor_additions(self.tree, ALLY, provenance=VENDOR, root_kind="tree_file") as rows:
            duplicates = copy.deepcopy(rows)
        with self.configured(), mock.patch.object(ifma, "_manifest_scan_policy", return_value=([], duplicates, [])), \
                self.assertRaisesRegex(SystemExit, "duplicate pin"):
            ifma.scan_rebuilt_tree(self.tree, "x", provenance=VENDOR, rebuilt_vendor_profile=ALLY)

    def test_vendor_has_pastel_delegates_ally_bootstrap_to_additions(self):
        self.assertFalse(ifma._vendor_has_pastel(ALLY))
        self.assertFalse(ifma._vendor_has_pastel({"cpu_compat": None, "display": None, "model": None}))
        unbootstrapped = copy.deepcopy(ALLY)
        unbootstrapped["cpu_compat"]["patch_ally_bootstrap"] = False
        self.assertTrue(ifma._vendor_has_pastel(unbootstrapped))
        with self.configured(), ifma._ally_vendor_additions(self.tree, ALLY, provenance=VENDOR, root_kind="tree_file") as rows:
            pastel_row = [copy.deepcopy(r) for r in rows if ifma._is_pastel_vendor_pin(r)]
        self.assertEqual(len(pastel_row), 1)
        def fake_policy(_path, *, include_pastel=False):
            return ([], list(pastel_row) if include_pastel else [], [])
        with self.configured(), mock.patch.object(ifma, "_manifest_scan_policy", side_effect=fake_policy), \
                mock.patch.object(ifma.SCAN_IFMA, "_find_disassembler", return_value=()):
            report = ifma.scan_rebuilt_tree(
                self.tree, "vendor_a rebuilt", provenance=VENDOR,
                include_pastel=ifma._vendor_has_pastel(ALLY), rebuilt_vendor_profile=ALLY,
            )
            self.assertEqual(len(report["accepted_non_instructions"]), 2)
            with self.assertRaisesRegex(SystemExit, "duplicate pin"):
                ifma.scan_rebuilt_tree(
                    self.tree, "vendor_a rebuilt", provenance=VENDOR,
                    include_pastel=True, rebuilt_vendor_profile=ALLY,
                )

    def test_existing_manifest_pin_still_must_be_observed(self):
        absent = {"partition": "vendor_a", "container_chain": [{"kind": "tree_file", "path": "missing"}],
                  "sha256": "0" * 64, "file_offset": "0x1", "encoding_hex": "c4e2f1b4c3",
                  "reason": "public absent existing policy"}
        with self.configured(), mock.patch.object(ifma, "_manifest_scan_policy", return_value=([], [absent], [])), \
                mock.patch.object(ifma.SCAN_IFMA, "_find_disassembler", return_value=()), \
                self.assertRaisesRegex(SystemExit, "known_non_instructions pin missing"):
            ifma.scan_rebuilt_tree(self.tree, "x", provenance=VENDOR, rebuilt_vendor_profile=ALLY)

    def test_unlisted_real_instruction_is_not_accepted(self):
        (self.tree / "extra-true-ifma").write_bytes(fixtures.elf_with_segments(fixtures.EncodingTests.LUQ))
        with self.configured(), self.assertRaisesRegex(SystemExit, "forbidden VEX"):
            self.scan()

    def test_extra_hit_in_pinned_file_is_not_accepted(self):
        target = self.tree / PATHS[0]
        data = bytearray(target.read_bytes())
        data[0x130:0x135] = fixtures.EncodingTests.LUQ
        target.write_bytes(data)
        specs = copy.deepcopy(self.specs)
        specs[0]["sha256"] = hashlib.sha256(data).hexdigest()
        with self.configured(specs=specs), self.assertRaisesRegex(SystemExit, "forbidden VEX"):
            self.scan()

    def test_target_write_during_scan_refuses(self):
        target = self.tree / PATHS[0]
        def mutate(*args, **kwargs):
            with target.open("r+b") as stream:
                stream.seek(-1, 2)
                stream.write(b"z")
            return {"accepted_residuals": []}
        with self.configured(), mock.patch.object(ifma.SCAN_IFMA, "scan_tree", side_effect=mutate), \
                self.assertRaisesRegex(SystemExit, "changed"):
            self.scan()

    def test_path_swap_during_scan_refuses(self):
        target = self.tree / PATHS[0]
        def swap(*args, **kwargs):
            target.rename(target.with_suffix(".old"))
            target.write_bytes(target.with_suffix(".old").read_bytes())
            return {"accepted_residuals": []}
        with self.configured(), mock.patch.object(ifma.SCAN_IFMA, "scan_tree", side_effect=swap), \
                self.assertRaisesRegex(SystemExit, "changed"):
            self.scan()

    def test_parent_swap_during_scan_refuses(self):
        parent = self.tree / "lib/dev-modules"
        def swap(*args, **kwargs):
            parent.rename(parent.with_name("previous"))
            parent.mkdir()
            (parent / "uas.ko").write_bytes((parent.with_name("previous") / "uas.ko").read_bytes())
            return {"accepted_residuals": []}
        with self.configured(), mock.patch.object(ifma.SCAN_IFMA, "scan_tree", side_effect=swap), \
                self.assertRaisesRegex(SystemExit, "changed"):
            self.scan()

    def test_ordinary_scan_does_not_add_policy(self):
        with self.configured(), mock.patch.object(ifma.SCAN_IFMA, "scan_tree", return_value={"accepted_residuals": []}) as scan:
            ifma.scan_rebuilt_tree(self.tree, "vendor_a rebuilt", provenance=VENDOR)
        self.assertEqual(scan.call_args.kwargs["known_non_instructions"], ifma._manifest_scan_policy(ifma.MANIFEST_PATH)[1])

    def test_only_final_vendor_verification_supplies_profile_trigger(self):
        tree = ast.parse(Path(ifma.__file__).read_text())
        callers = []
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef):
                continue
            for call in ast.walk(function):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "scan_rebuilt_tree"):
                    keywords = {item.arg: item.value for item in call.keywords}
                    if "rebuilt_vendor_profile" in keywords:
                        value = keywords["rebuilt_vendor_profile"]
                        self.assertIsInstance(value, ast.Name)
                        self.assertEqual(value.id, "prof")
                        callers.append(function.name)
        self.assertEqual(callers, ["verify_rebuilt_vendor_tree"])


if __name__ == "__main__":
    unittest.main()
