#!/usr/bin/env python3
"""Builder call-path and bootconfig tests; no private image or hardware."""
import ast
import copy
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
sys.dont_write_bytecode = True
import devprofile  # noqa: E402


def load_wrapper():
    tree = ast.parse((ROOT / "builder/image.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "ally_bootstrap_initrd")
    ns = {"struct": struct, "hashlib": hashlib}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "image-wrapper", "exec"), ns)
    return ns["ally_bootstrap_initrd"]


def initrd(body=b"archive", config=b"androidboot.test=1\n\0"):
    return body + config + struct.pack("<II", len(config), sum(config)) + b"#BOOTCONFIG\n"


class ImageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fn = load_wrapper()
        self.prof = {"model": {"id": "rog-ally"},
                     "cpu_compat": {"patch_ally_bootstrap": True}}
        self.module = types.SimpleNamespace(
            enabled=lambda prof: True,
            patch_archive=mock.Mock(return_value=(b"patched", {"kind": "patch"})),
            verify_archive=mock.Mock(return_value={"kind": "verify"}),
            source_identity=mock.Mock(return_value={"sources": {"pin": "checked"}}))
        self.patch = mock.patch.dict(sys.modules, {"ally_bootstrap": self.module})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_disabled_is_byte_identical_and_does_not_load_dependency(self):
        with mock.patch.dict(sys.modules, {"ally_bootstrap": None}):
            for prof in ({}, {"cpu_compat": {}},
                         {"cpu_compat": {"patch_ally_bootstrap": False}}):
                self.assertEqual(self.fn(prof, b"arbitrary not initrd"),
                                 (b"arbitrary not initrd", None))

    def test_patch_preserves_exact_bootconfig_and_records_identity(self):
        original = initrd()
        result, receipt = self.fn(self.prof, original)
        self.assertEqual(result, b"patched" + original[len(b"archive"):])
        self.module.patch_archive.assert_called_once_with(b"archive")
        self.assertEqual(receipt["source_identity"], {"sources": {"pin": "checked"}})
        self.assertEqual(receipt["input_initrd_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(receipt["output_initrd_sha256"], hashlib.sha256(result).hexdigest())

    def test_verify_dispatches_readback_without_repatch(self):
        source = initrd()
        result, receipt = self.fn(self.prof, source, verify=True)
        self.assertEqual(result, source)
        self.module.patch_archive.assert_not_called()
        self.module.verify_archive.assert_called_once_with(b"archive")
        self.assertEqual(receipt["kind"], "verify")

    def test_bad_bootconfig_refuses_before_transform(self):
        source = initrd()
        bad_checksum = bytearray(source)
        bad_checksum[-16] ^= 1
        for bad in (b"", b"#BOOTCONFIG\n", source[:-1], bytes(bad_checksum),
                    source[:-20] + struct.pack("<II", 2**32 - 1, 0) + source[-12:],
                    b"archive" + struct.pack("<II", 0, 0) + b"#BOOTCONFIG\n"):
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                self.fn(self.prof, bad)
        self.module.patch_archive.assert_not_called()
        self.module.verify_archive.assert_not_called()

    def test_actual_image_calls_patch_then_final_esp_verification_before_publish(self):
        source = (ROOT / "builder/image.py").read_text()
        tree = ast.parse(source)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "ally_bootstrap_initrd"]
        self.assertEqual(len(calls), 2)
        patch, verify = sorted(calls, key=lambda n: n.lineno)
        self.assertFalse(patch.keywords)
        self.assertTrue(any(k.arg == "verify" and isinstance(k.value, ast.Constant)
                            and k.value.value is True for k in verify.keywords))
        self.assertLess(source.index('"::/initrd.img", OUT / "src-initrd.img"'),
                        source.index("src, ally_bootstrap_preparation ="))
        self.assertLess(patch.lineno, next(n.lineno for n in tree.body
                                         if isinstance(n, ast.While)))
        self.assertLess(source.index('"::/initrd.img", OUT / "readback-initrd.img"'),
                        source.index("_, ally_bootstrap_readback ="))
        self.assertLess(verify.lineno, next(n.lineno for n in tree.body
                                          if isinstance(n, ast.If)
                                          and ast.unparse(n.test) == "SKIP_SELFTEST"))
        self.assertIn('OUT / "ally-bootstrap.json"', source)
        self.assertIn('GBOS_OUT / "build-info.txt"', source)

    def test_profile_default_optin_and_wrong_model(self):
        kano = devprofile.load(ROOT / "profiles/kano")
        self.assertIs(kano["cpu_compat"]["patch_ally_bootstrap"], False)
        ally = devprofile.load(ROOT / "devices/rog-ally/profile-draft/rog-ally")
        self.assertIs(ally["cpu_compat"]["patch_ally_bootstrap"], True)
        self.assertNotEqual(ally["model"]["status"], "verified")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "kano"
            base.mkdir()
            text = (ROOT / "profiles/kano/profile.toml").read_text()
            (base / "profile.toml").write_text(text.replace(
                "[cpu_compat]\n", "[cpu_compat]\npatch_ally_bootstrap = true\n"))
            with self.assertRaises(devprofile.ProfileError):
                devprofile.load(base)

    def test_installer_cache_checks_external_inputs_only_when_enabled(self):
        source = (ROOT / "install/install.sh").read_text()
        check = source.index('cpu_compat.patch_ally_bootstrap)" = true')
        identity = source.index('builder/lib/ally_bootstrap.py" identity', check)
        compare = source.index('IMG="$WORK/build/image/$IMAGE_NAME"')
        self.assertLess(check, identity)
        self.assertLess(identity, compare)
        self.assertIn('ally-bootstrap $ALLY_BOOTSTRAP_ID', source[check:compare])

    def test_final_continuity_is_executable_and_enforced_before_receipt(self):
        source = (ROOT / "builder/image.py").read_text()
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "check_ally_bootstrap_continuity")
        ns = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "continuity", "exec"), ns)
        check = ns[node.name]
        proof = {"members": [{"name": "init", "mode": 0o100750, "archive_index": 2}],
                 "bootconfig_sha256": "config", "source_identity": {"sources": "pins"}}
        check(proof, dict(proof, archive_sha256="module edits legitimately change archive"))
        for field in proof:
            changed = copy.deepcopy(proof)
            changed[field] = "different"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                check(proof, changed)
        changed = copy.deepcopy(proof)
        changed["members"][0]["mode"] = 0o100755
        with self.assertRaisesRegex(ValueError, "members"):
            check(proof, changed)
        guard = source.index("check_ally_bootstrap_continuity(ally_bootstrap_preparation,")
        self.assertLess(guard, source.index('OUT / "ally-bootstrap.json"'))


class PartitionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "builder/lib/ifma.py").read_text()
        self.tree = ast.parse(self.source)
        names = {"ally_adler_tree", "ally_adler_changed_paths", "changed_vendor_paths"}
        nodes = [n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.ns = {"enabled": lambda prof: prof.get("cpu_compat", {}).get("patch_ifma", False),
                   "VENDOR_SURVIVING_CHANGED": ("existing-ifma",)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "partition-wrappers", "exec"), self.ns)
        self.module = types.SimpleNamespace(
            patch_tree=mock.Mock(return_value={"patched": "receipt"}),
            verify_tree=mock.Mock(return_value={"verified": "receipt"}),
            expected_changed_paths=mock.Mock(return_value=("new-adler",)))
        self.prof = {"cpu_compat": {"patch_ifma": True, "patch_ally_bootstrap": True}}

    def test_disabled_does_not_import_or_change_sets(self):
        with mock.patch.dict(sys.modules, {"ally_adler": None}):
            for prof in ({}, {"cpu_compat": {"patch_ifma": True}}):
                self.assertIsNone(self.ns["ally_adler_tree"](prof, "system", "unused"))
                self.assertEqual(self.ns["ally_adler_changed_paths"](prof, "vendor"), ())
            original = ["existing"]
            self.assertIs(self.ns["changed_vendor_paths"]({}, original), original)
            self.assertEqual(self.ns["changed_vendor_paths"]({"cpu_compat": {"patch_ifma": True}}, original),
                             ["existing", "existing-ifma"])

    def test_partition_dependency_refuses_partial_pipeline(self):
        prof = {"cpu_compat": {"patch_ally_bootstrap": True}}
        with self.assertRaisesRegex(SystemExit, "requires patch_ifma"):
            self.ns["ally_adler_tree"](prof, "system", "unused")
        with self.assertRaisesRegex(SystemExit, "requires patch_ifma"):
            self.ns["ally_adler_changed_paths"](prof, "vendor")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "rog-ally"
            base.mkdir()
            text = (ROOT / "devices/rog-ally/profile-draft/rog-ally/profile.toml").read_text()
            (base / "profile.toml").write_text(text.replace("patch_ifma = true", "patch_ifma = false"))
            with self.assertRaisesRegex(devprofile.ProfileError, "requires patch_ifma"):
                devprofile.load(base)

    def test_optin_patch_readback_and_exact_changed_set_dispatch(self):
        with mock.patch.dict(sys.modules, {"ally_adler": self.module}):
            self.assertEqual(self.ns["ally_adler_tree"](self.prof, "system", "stage"), {"patched": "receipt"})
            self.module.patch_tree.assert_called_once_with("system", "stage")
            self.assertEqual(self.ns["ally_adler_tree"](self.prof, "vendor", "rebuilt", verify=True),
                             {"verified": "receipt"})
            self.module.verify_tree.assert_called_once_with("vendor", "rebuilt")
            self.assertEqual(self.ns["changed_vendor_paths"](self.prof, ["existing"]),
                             ["existing", "existing-ifma", "new-adler"])

    def test_existing_transactions_include_patch_sets_and_readbacks_before_publish(self):
        functions = {n.name: ast.get_source_segment(self.source, n) for n in self.tree.body
                     if isinstance(n, ast.FunctionDef)}
        vendor = functions["patch_vendor_tree"]
        self.assertLess(vendor.index('ally_adler_tree(prof, "vendor", tree)'), vendor.index("PATCH_IFMA.run("))
        self.assertIn('tmp / "ally-adler-vendor.json"', vendor)
        verify = functions["verify_rebuilt_vendor_tree"]
        self.assertIn('ally_adler_tree(prof, "vendor", rebuilt_tree, verify=True)', verify)
        self.assertIn("adler_readback != adler_preparation", verify)
        system = functions["rebuild_system_partition"]
        self.assertLess(system.index("old_index ="), system.index('ally_adler_tree(prof, "system", sys_tree)'))
        self.assertLess(system.index('ally_adler_tree(prof, "system", sys_tree)'), system.index("PATCH_IFMA.run("))
        self.assertIn('ally_adler_changed_paths(prof, "system")', system)
        self.assertIn("adler_readback != adler_report", system)
        self.assertLess(system.index('ally_adler_tree(prof, "system", new_tree, verify=True)'),
                        system.index('"add_hashtree_footer"'))
        self.assertIn('summary["ally_adler_system"] = adler_report', system)
        vendor_script = (ROOT / "builder/vendor.sh").read_text()
        self.assertIn("ifma.verify_rebuilt_vendor_tree", vendor_script)
        self.assertIn('raise SystemExit(f"{label} final image readback differs after splice")', vendor_script)
        self.assertIn('("vendor", vendor_off, vendor_size, tmp / "vendor.img")', vendor_script)
        # vendor stage rewrites the stock ESP BEFORE image.py's four-target repair.
        build = (ROOT / "builder/build.sh").read_text()
        self.assertLess(build.index('bash "$HERE/vendor.sh"'), build.index('python3 "$HERE/image.py"'))
        image = (ROOT / "builder/image.py").read_text()
        self.assertIn('SRC = GBOS_OUT / "vendor/stage-vendor.img"', image)
        self.assertLess(image.index('run("cp", "--reflink=auto", SRC, IMG)'),
                        image.index("src, ally_bootstrap_preparation ="))


if __name__ == "__main__":
    unittest.main()
