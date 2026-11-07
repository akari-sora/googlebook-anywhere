#!/usr/bin/env python3
"""Public synthetic container-tree tests; no private ELF or image acceptance."""
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import ally_adler as adler  # noqa: E402


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SourceTests(unittest.TestCase):
    def test_explicit_optin_only(self):
        for prof in ({}, {"cpu_compat": {}}, {"cpu_compat": {"patch_ifma": True}},
                     {"cpu_compat": {"patch_ally_bootstrap": False}}):
            self.assertFalse(adler.enabled(prof))
        self.assertTrue(adler.enabled({"cpu_compat": {"patch_ally_bootstrap": True}}))
        for value in (1, "true", None):
            with self.assertRaises(adler.Refused):
                adler.enabled({"cpu_compat": {"patch_ally_bootstrap": value}})

    def test_disabled_caller_does_not_touch_tree(self):
        with patch.object(adler, "patch_tree") as apply:
            if adler.enabled({"cpu_compat": {"patch_ifma": True}}):
                adler.patch_tree("system", Path("not-present"))
            apply.assert_not_called()

    def test_current_checked_source_and_direct_provenance(self):
        identity = adler.source_identity()
        self.assertEqual(set(identity["targets"]),
                         {"system", "vendor", "runtime", "appsearch", "microdroid", "microdroid_runtime"})
        for name, pin in identity["sources"].items():
            self.assertEqual(sha((ROOT / name).read_bytes()), pin)
        self.assertEqual({row["id"] for rows in identity["targets"].values() for row in rows},
                         {27, 33, 67, 68, 73, 77, 79, 82, 84, 88, 122})
        self.assertEqual(adler.expected_changed_paths("system"), tuple(adler.TARGETS["system"]))
        json.dumps(identity)

    def test_literal_container_scopes_and_distinct_provenance(self):
        self.assertEqual(adler.TARGETS["runtime"], {"bin/linker64": 27})
        self.assertEqual(adler.TARGETS["appsearch"], {"lib64/libicing_anywhere.so": 33})
        self.assertEqual(adler.TARGETS["microdroid"], {
            "system/bin/bootstrap/linker64": 67, "system/bin/init": 68, "system/lib64/libz.so": 73})
        self.assertEqual(adler.TARGETS["microdroid_runtime"], {"bin/linker64": 27})
        self.assertEqual(adler.SOURCE_PREFIXES, {
            "system": "system_a/", "vendor": "vendor_a/",
            "runtime": "system_a/system/apex/com.android.runtime.apex!/apex_payload.img!/erofs/",
            "appsearch": "system_a/system/apex/com.google.android.appsearch.apex!/apex_payload.img!/erofs/",
            "microdroid": "system_a/system/apex/com.google.android.virt.apex!/apex_payload.img!/erofs/"
                          "etc/fs/microdroid.img!/erofs/",
            "microdroid_runtime": "system_a/system/apex/com.google.android.virt.apex!/apex_payload.img!/erofs/"
                                  "etc/fs/microdroid.img!/erofs/system/apex/com.android.runtime.apex!/"
                                  "apex_payload.img!/erofs/",
        })
        identity = adler.source_identity()
        self.assertEqual(sum(len(rows) for rows in identity["targets"].values()), 14)
        self.assertEqual([r["id"] for r in identity["targets"]["system"]], [67, 73, 77, 79, 82, 84, 88])
        self.assertEqual([r["id"] for r in identity["targets"]["vendor"]], [122])
        for scope in ("runtime", "appsearch", "microdroid", "microdroid_runtime"):
            self.assertEqual(adler.expected_changed_paths(scope), tuple(adler.TARGETS[scope]))
        # Equal linker hashes are deliberately different container receipts/scopes.
        self.assertEqual(identity["targets"]["runtime"], identity["targets"]["microdroid_runtime"])
        self.assertNotEqual(adler.SOURCE_PREFIXES["runtime"], adler.SOURCE_PREFIXES["microdroid_runtime"])

    def test_every_new_literal_alias_required_despite_other_same_elf_locations(self):
        original = (ROOT / adler.MANIFEST).read_text()
        for scope in ("runtime", "appsearch", "microdroid", "microdroid_runtime"):
            for path, target_id in adler.TARGETS[scope].items():
                with self.subTest(scope=scope, path=path):
                    manifest = json.loads(original)
                    row = next(row for row in manifest["targets"] if row["id"] == target_id)
                    alias = adler.SOURCE_PREFIXES[scope] + path
                    row["source_aliases"].remove(alias)
                    # Invented staging paths cannot substitute for the exact chain.
                    row["source_aliases"].append(scope + "/" + path)
                    with patch.object(adler.json, "loads", return_value=manifest), \
                            self.assertRaisesRegex(adler.Refused, "container provenance"):
                        adler.source_identity()

    def test_modified_source_refused(self):
        with patch.dict(adler.SOURCE_PINS, {adler.PATCHER: "0" * 64}):
            with self.assertRaisesRegex(adler.Refused, "source pin"):
                adler.source_identity()

    def test_unknown_scope_rejected_before_access(self):
        for scope in ("system_a", "vendor_a", "apex", "", "../system"):
            for method in (adler.expected_changed_paths,
                           lambda s: adler.patch_tree(s, "/not-present"),
                           lambda s: adler.verify_tree(s, "/not-present")):
                with self.assertRaisesRegex(adler.Refused, "scope"):
                    method(scope)

    def test_plain_sha_disjoint_from_ifma(self):
        adler_hashes = {r["source_sha256"] for rows in adler.source_identity()["targets"].values()
                        for r in rows}
        ifma = json.loads((ROOT / "builder/ifma/manifest.json").read_text())
        self.assertFalse(adler_hashes.intersection(r["sha256"] for r in ifma["targets"]))


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name).resolve() / "tree"
        self.tree.mkdir()
        self.original = b"abcdef"
        self.candidate = b"abCdef"
        self.rows = {scope: {
            path: {"id": target_id, "size": len(self.original),
                   "sha256": sha(self.original), "patched_sha256": sha(self.candidate)}
            for path, target_id in paths.items()} for scope, paths in adler.TARGETS.items()}
        self.calls = []
        self.validator = types.SimpleNamespace(validate=self.validate, PatchError=ValueError)
        self.pins = {"fixture": "not-product-evidence"}

    def validate(self, original, row):
        self.calls.append((original, row["id"]))
        return "original", self.candidate

    @contextmanager
    def fixture(self):
        # macOS Python here lacks xattr APIs. Empty synthetic xattrs exercise
        # orchestration only; real preservation test remains an explicit skip.
        with ExitStack() as stack:
            for name, function in (("listxattr", lambda *_: []), ("getxattr", lambda *_: b""),
                                   ("setxattr", lambda *_: None)):
                if not hasattr(os, name):
                    stack.enter_context(patch.object(os, name, function, create=True))
            stack.enter_context(patch.object(adler, "_context", return_value=(self.rows, self.validator, self.pins)))
            yield

    def create(self, scope="system"):
        for path in self.rows[scope]:
            target = self.tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.original)
            target.chmod(0o751)
            # Future atime avoids reads changing it on systems without O_NOATIME.
            os.utime(target, ns=(2000000000000000000, 1000000000000000000))
        other = self.tree / "unrelated"
        other.write_bytes(b"untouched")
        return {path: (self.tree / path).stat() for path in self.rows[scope]}

    def assert_originals(self, scope="system", except_paths=()):
        for path in self.rows[scope]:
            if path not in except_paths:
                self.assertEqual((self.tree / path).read_bytes(), self.original)
        self.assertFalse(list(self.tree.rglob(".ally-adler-*")))

    def test_system_all_original_validators_metadata_and_readback(self):
        metadata = self.create()
        with self.fixture():
            receipt = adler.patch_tree("system", self.tree)
            verified = adler.verify_tree("system", self.tree)
        self.assertEqual([r["id"] for r in receipt["targets"]], [67, 73, 77, 79, 82, 84, 88])
        self.assertEqual(receipt, verified)
        self.assertEqual(self.calls, [(self.original, value) for value in [67, 73, 77, 79, 82, 84, 88]])
        for path, before in metadata.items():
            after = (self.tree / path).stat()
            self.assertEqual((after.st_mode, after.st_uid, after.st_gid, after.st_atime_ns, after.st_mtime_ns),
                             (before.st_mode, before.st_uid, before.st_gid, before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual((self.tree / path).read_bytes(), self.candidate)
        self.assertEqual((self.tree / "unrelated").read_bytes(), b"untouched")
        self.assertFalse(list(self.tree.rglob(".ally-adler-*")))
        json.dumps(receipt)

    def test_vendor_only_exact_target(self):
        self.create("vendor")
        with self.fixture():
            receipt = adler.patch_tree("vendor", self.tree)
            self.assertEqual(receipt, adler.verify_tree("vendor", self.tree))
        self.assertEqual(self.calls, [(self.original, 122)])
        self.assertEqual((self.tree / "lib64/libz.so").read_bytes(), self.candidate)

    def test_new_container_scopes_roundtrip_preserves_metadata_and_other_files(self):
        for scope, ids in (("runtime", [27]), ("appsearch", [33]),
                           ("microdroid", [67, 68, 73]), ("microdroid_runtime", [27])):
            with self.subTest(scope=scope):
                self.tree = Path(self.temp.name).resolve() / scope
                self.tree.mkdir()
                self.calls.clear()
                before = self.create(scope)
                with self.fixture():
                    receipt = adler.patch_tree(scope, self.tree)
                    self.assertEqual(receipt, adler.verify_tree(scope, self.tree))
                self.assertEqual(receipt["scope"], scope)
                self.assertEqual([row["id"] for row in receipt["targets"]], ids)
                self.assertEqual(self.calls, [(self.original, target_id) for target_id in ids])
                for path, metadata in before.items():
                    after = (self.tree / path).stat()
                    self.assertEqual((after.st_mode, after.st_uid, after.st_gid,
                                      after.st_atime_ns, after.st_mtime_ns),
                                     (metadata.st_mode, metadata.st_uid, metadata.st_gid,
                                      metadata.st_atime_ns, metadata.st_mtime_ns))
                    self.assertEqual((self.tree / path).read_bytes(), self.candidate)
                self.assertEqual((self.tree / "unrelated").read_bytes(), b"untouched")
                self.assertFalse(list(self.tree.rglob(".ally-adler-*")))

    def test_new_container_bad_last_original_prevents_any_payload_write(self):
        for scope in ("runtime", "appsearch", "microdroid", "microdroid_runtime"):
            with self.subTest(scope=scope):
                self.tree = Path(self.temp.name).resolve() / scope
                self.tree.mkdir()
                self.create(scope)
                last = list(self.rows[scope])[-1]
                (self.tree / last).write_bytes(b"badbad")
                with self.fixture(), patch.object(adler.os, "open", wraps=os.open) as opened, \
                        patch.object(adler.os, "replace") as replace:
                    with self.assertRaisesRegex(adler.Refused, "exact-original"):
                        adler.patch_tree(scope, self.tree)
                    replace.assert_not_called()
                    for call in opened.call_args_list:
                        self.assertFalse(call.args[1] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
                self.assert_originals(scope, except_paths=(last,))

    def test_new_container_missing_last_original_prevents_any_payload_write(self):
        self.create("microdroid")
        last = list(self.rows["microdroid"])[-1]
        (self.tree / last).unlink()
        with self.fixture(), patch.object(adler.os, "open", wraps=os.open) as opened, \
                patch.object(adler.os, "replace") as replace:
            with self.assertRaises(FileNotFoundError):
                adler.patch_tree("microdroid", self.tree)
            replace.assert_not_called()
            for call in opened.call_args_list:
                self.assertFalse(call.args[1] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
        self.assert_originals("microdroid", except_paths=(last,))

    def test_bad_last_target_prevents_all_writes(self):
        self.create()
        last = list(self.rows["system"])[-1]
        (self.tree / last).write_bytes(b"badbad")
        with self.fixture(), patch.object(adler.os, "replace") as replace:
            with self.assertRaisesRegex(adler.Refused, "exact-original"):
                adler.patch_tree("system", self.tree)
            replace.assert_not_called()
        self.assert_originals(except_paths=(last,))

    def test_missing_last_target_prevents_all_writes(self):
        self.create()
        last = list(self.rows["system"])[-1]
        (self.tree / last).unlink()
        with self.fixture(), self.assertRaises(FileNotFoundError):
            adler.patch_tree("system", self.tree)
        self.assert_originals(except_paths=(last,))

    def test_already_patched_unknown_and_wrong_size_refused(self):
        self.create("vendor")
        target = self.tree / "lib64/libz.so"
        for data in (self.candidate, b"??????", self.original + b"!"):
            target.write_bytes(data)
            with self.fixture(), self.assertRaises(adler.Refused):
                adler.patch_tree("vendor", self.tree)
            self.assertEqual(target.read_bytes(), data)

    def test_candidate_state_hash_and_size_must_match(self):
        self.create("vendor")
        for state, data in (("patched", self.candidate), ("original", self.original),
                            ("original", self.candidate + b"!"), ("original", bytearray(self.candidate))):
            with self.fixture(), patch.object(self.validator, "validate", return_value=(state, data)), \
                 self.assertRaisesRegex(adler.Refused, "candidate"):
                adler.patch_tree("vendor", self.tree)
            self.assert_originals("vendor")

    def test_validator_error_refused_before_write(self):
        self.create("vendor")
        with self.fixture(), patch.object(self.validator, "validate", side_effect=ValueError("context")), \
             self.assertRaisesRegex(adler.Refused, "validator refused"):
            adler.patch_tree("vendor", self.tree)
        self.assert_originals("vendor")

    def test_symlink_file_and_parent_and_tree_refused(self):
        self.create("vendor")
        target = self.tree / "lib64/libz.so"
        target.unlink()
        target.symlink_to(self.tree / "unrelated")
        with self.fixture(), self.assertRaises(OSError):
            adler.patch_tree("vendor", self.tree)
        target.unlink()
        (self.tree / "lib64").rmdir()
        (self.tree / "lib64").symlink_to(self.tree)
        with self.fixture(), self.assertRaises(OSError):
            adler.patch_tree("vendor", self.tree)
        alias = self.tree.parent / "alias"
        alias.symlink_to(self.tree)
        with self.fixture(), self.assertRaises(OSError):
            adler.patch_tree("vendor", alias)
        self.assertEqual((self.tree / "unrelated").read_bytes(), b"untouched")

    def test_hardlinked_file_refused(self):
        self.create("vendor")
        os.link(self.tree / "lib64/libz.so", self.tree / "hardlink")
        with self.fixture(), self.assertRaisesRegex(adler.Refused, "type/link/size"):
            adler.patch_tree("vendor", self.tree)
        self.assert_originals("vendor")

    def test_directory_target_refused(self):
        self.create("vendor")
        target = self.tree / "lib64/libz.so"
        target.unlink()
        target.mkdir()
        with self.fixture(), self.assertRaises(adler.Refused):
            adler.patch_tree("vendor", self.tree)

    def test_concurrent_original_mutation_refused_before_replace(self):
        self.create("vendor")
        target = self.tree / "lib64/libz.so"
        def mutate(original, row):
            target.write_bytes(b"racing")
            return "original", self.candidate
        with self.fixture(), patch.object(self.validator, "validate", side_effect=mutate), \
             self.assertRaisesRegex(adler.Refused, "changed before replacement"):
            adler.patch_tree("vendor", self.tree)
        self.assertEqual(target.read_bytes(), b"racing")
        self.assertFalse(list(self.tree.rglob(".ally-adler-*")))

    def test_staging_io_failure_leaves_all_originals_and_no_temps(self):
        self.create()
        real_chown = os.fchown
        calls = 0
        def fail_second(fd, uid, gid):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("synthetic staging failure")
            return real_chown(fd, uid, gid)
        with self.fixture(), patch.object(adler.os, "fchown", side_effect=fail_second), \
             self.assertRaisesRegex(PermissionError, "staging failure"):
            adler.patch_tree("system", self.tree)
        self.assert_originals()

    def test_actual_final_readback_corruption_refused(self):
        self.create("vendor")
        real_replace = os.replace
        def corrupt(src, dst, **kwargs):
            real_replace(src, dst, **kwargs)
            (self.tree / "lib64/libz.so").write_bytes(b"broken")
        with self.fixture(), patch.object(adler.os, "replace", side_effect=corrupt), \
             self.assertRaisesRegex(adler.Refused, "final readback"):
            adler.patch_tree("vendor", self.tree)
        self.assertFalse(list(self.tree.rglob(".ally-adler-*")))

    def test_verify_rejects_original_or_wrong_output(self):
        self.create("vendor")
        with self.fixture(), self.assertRaisesRegex(adler.Refused, "final SHA256"):
            adler.verify_tree("vendor", self.tree)
        self.assert_originals("vendor")

    def test_missing_xattr_support_fails_before_writes(self):
        self.create("vendor")
        with self.fixture(), patch.object(adler.os, "listxattr", None), \
             self.assertRaisesRegex(adler.Refused, "xattr support"):
            adler.patch_tree("vendor", self.tree)
        self.assert_originals("vendor")

    def test_xattrs_preserved_when_supported(self):
        self.create("vendor")
        target = self.tree / "lib64/libz.so"
        try:
            os.setxattr(target, "user.ally-adler-test", b"public-fixture")
        except (OSError, AttributeError):
            self.skipTest("user xattrs unsupported on this test filesystem")
        with self.fixture():
            adler.patch_tree("vendor", self.tree)
        self.assertEqual(os.getxattr(target, "user.ally-adler-test"), b"public-fixture")


if __name__ == "__main__":
    unittest.main()
