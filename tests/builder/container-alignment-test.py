#!/usr/bin/env python3
"""Exercise ZIP local offsets and fail-closed alignment checks."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
spec = importlib.util.spec_from_file_location("container_alignment", ROOT / "builder/lib/container_alignment.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def archive(path, entries):
    with zipfile.ZipFile(path, "w") as out:
        for name, data, method, align in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = method
            if align:
                offset = out.fp.tell() + 30 + len(name.encode())
                padding = (-offset) % align
                if padding and padding < 4:
                    padding += align
                if padding:
                    info.extra = b"\x34\x12" + (padding - 4).to_bytes(2, "little") + bytes(padding - 4)
            out.writestr(info, data)


class ContainerAlignmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_aligned_apex_and_resource(self):
        p = self.root / "ok.apex"
        archive(p, [("apex_payload.img", b"payload", zipfile.ZIP_STORED, 4096),
                    ("resources.arsc", b"res", zipfile.ZIP_STORED, 4),
                    ("lib/x.so", b"shared", zipfile.ZIP_STORED, 16384)])
        gate.check_zip_layout(p)
        self.assertEqual(gate.check_tree(self.root), 1)

    def test_unreadable_subtree_fails_even_with_a_valid_container_elsewhere(self):
        if os.geteuid() == 0:
            self.skipTest("root can list a mode-0 directory")
        archive(self.root / "ok.apex", [("apex_payload.img", b"payload", zipfile.ZIP_STORED, 4096)])
        hidden = self.root / "vendor" / "apex"
        hidden.mkdir(parents=True)
        archive(hidden / "bad.apex", [("apex_payload.img", b"payload", zipfile.ZIP_STORED, 0)])
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o755)
        with self.assertRaisesRegex(ValueError, "unreadable directory"):
            gate.check_tree(self.root)

    def test_directory_classification_error_fails_even_if_a_later_stat_succeeds(self):
        archive(self.root / "ok.apex", [("apex_payload.img", b"payload", zipfile.ZIP_STORED, 4096)])
        hidden = self.root / "vendor" / "apex"
        hidden.mkdir(parents=True)
        archive(hidden / "bad.apex", [("apex_payload.img", b"payload", zipfile.ZIP_STORED, 0)])
        real, failed, hidden = Path.lstat, [], hidden.resolve()  # check_tree() walks the resolved root

        def flaky(path, *args, **kwargs):
            if path == hidden and not failed:
                failed.append(path)
                raise OSError(5, "Input/output error")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", flaky), self.assertRaisesRegex(ValueError, "cannot stat"):
            gate.check_tree(self.root)
        self.assertEqual(failed, [hidden])

    def test_resource_compressed_fails_even_if_offset_aligned(self):
        p = self.root / "bad.apk"
        archive(p, [("resources.arsc", b"a" * 100, zipfile.ZIP_DEFLATED, 4)])
        with self.assertRaisesRegex(ValueError, "resources.arsc is compressed"):
            gate.check_zip_layout(p)

    def test_resource_misaligned(self):
        p = self.root / "bad.apk"
        archive(p, [("x", b"a", zipfile.ZIP_DEFLATED, None),
                    ("resources.arsc", b"a", zipfile.ZIP_STORED, None)])
        with self.assertRaisesRegex(ValueError, "resources.arsc: offset .* not 4-aligned"):
            gate.check_zip_layout(p)

    def test_payload_misaligned(self):
        p = self.root / "bad.apex"
        archive(p, [("apex_payload.img", b"payload", zipfile.ZIP_STORED, 4)])
        with self.assertRaisesRegex(ValueError, "apex_payload.img offset .* not 4096-aligned"):
            gate.check_zip_layout(p)

    def test_payload_compressed_and_missing_fail(self):
        p = self.root / "bad.apex"
        archive(p, [("apex_payload.img", b"a" * 100, zipfile.ZIP_DEFLATED, None)])
        with self.assertRaisesRegex(ValueError, "apex_payload.img is compressed"):
            gate.check_zip_layout(p)
        archive(p, [("unrelated", b"x", zipfile.ZIP_DEFLATED, None)])
        with self.assertRaisesRegex(ValueError, "missing apex_payload.img"):
            gate.check_zip_layout(p)

    def test_other_stored_and_so_alignment(self):
        p = self.root / "bad.apk"
        archive(p, [("other", b"x", zipfile.ZIP_STORED, None)])
        with self.assertRaisesRegex(ValueError, "not 4-aligned"):
            gate.check_zip_layout(p)
        archive(p, [("lib/x.so", b"x", zipfile.ZIP_STORED, 4)])
        with self.assertRaisesRegex(ValueError, "not 16384-aligned"):
            gate.check_zip_layout(p)

    def test_zipalign_failure_is_propagated(self):
        p = self.root / "ok.apk"
        archive(p, [("resources.arsc", b"a", zipfile.ZIP_STORED, 4)])
        with mock.patch.object(gate.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "bad", "")) as run:
            with self.assertRaisesRegex(ValueError, "zipalign check failed: bad"):
                gate.check_archive(p, "/zipalign")
        self.assertEqual(run.call_args.args[0], ["/zipalign", "-c", "-P", "16", "4", str(p)])

    def test_empty_tree_cli_fails(self):
        with mock.patch.dict(os.environ, {"PATH": ""}, clear=True):
            self.assertEqual(gate.main(["--tree", str(self.root)]), 1)

    def test_symlinked_apk_cannot_hide_compressed_resources(self):
        archive(self.root / "good.apk", [("resources.arsc", b"a", zipfile.ZIP_STORED, 4)])
        archive(self.root / "bad.bin", [("resources.arsc", b"a" * 100, zipfile.ZIP_DEFLATED, 4)])
        (self.root / "bad.apk").symlink_to("bad.bin")
        with self.assertRaisesRegex(ValueError, "symlink.*container"):
            gate.check_tree(self.root)

    def test_symlink_escape_cycle_and_unknown_absolute_fail(self):
        p = self.root / "alias"
        for target in ("../outside", "alias", "/unscanned/path"):
            p.symlink_to(target)
            with self.subTest(target=target), self.assertRaises(ValueError):
                gate.check_tree(self.root)
            p.unlink()

    def test_internal_directory_alias_and_android_partition_alias(self):
        directory = self.root / "real"
        directory.mkdir()
        archive(directory / "ok.apk", [("resources.arsc", b"a", zipfile.ZIP_STORED, 4)])
        (self.root / "link").symlink_to("real")
        (self.root / "vendor").symlink_to("/vendor")
        self.assertEqual(gate.check_tree(self.root), 1)

    def test_image_cli_requires_zipalign(self):
        with mock.patch.dict(os.environ, {"PATH": ""}, clear=True), \
             mock.patch.object(gate, "check_image") as scan:
            self.assertEqual(gate.main(["--image", "image.img"]), 1)
            scan.assert_not_called()

    def test_image_scans_populated_partitions_and_skips_empty_slots(self):
        import ifma
        ota = self.root / "ota"
        (ota / "bin").mkdir(parents=True)
        fsck = ota / "bin/fsck.erofs"
        fsck.write_text("#!/bin/sh\nexit 0\n")
        fsck.chmod(0o755)
        seen = []
        populated = {"system_a", "system_b", "system_ext_a", "product_a", "vendor_a"}

        def extent(image, offset, name):
            if name in populated:
                return (4096, 8192)
            if name.endswith("_b"):
                raise SystemExit(f"{name} extents 0")
            raise SystemExit(f"{name} missing in super")

        def copy(image, offset, size, destination, *, partition_name):
            seen.append(partition_name)
            Path(destination).touch()

        def extract(command, **kwargs):
            tree = Path(command[1].split("=", 1)[1])
            tree.mkdir()
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(ifma, "_gpt_partitions", return_value={"super": {"start": 512, "size": 65536}}), \
             mock.patch.object(ifma, "_lp_Named_extent", side_effect=extent), \
             mock.patch.object(ifma, "_copy_erofs_extent", side_effect=copy), \
             mock.patch.object(gate.subprocess, "run", side_effect=extract), \
             mock.patch.object(gate, "check_tree", return_value=1):
            self.assertEqual(gate.check_image("image.img", ota), 5)
        self.assertEqual(set(seen), populated)

    def test_invalid_zip_and_duplicate_entries_fail(self):
        p = self.root / "bad.apk"
        p.write_text("bad")
        with self.assertRaises(ValueError):
            gate.check_zip_layout(p)
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("x", "a", compress_type=zipfile.ZIP_DEFLATED)
            with self.assertWarns(UserWarning):
                z.writestr("x", "b", compress_type=zipfile.ZIP_DEFLATED)
        with self.assertRaisesRegex(ValueError, "duplicate ZIP entry"):
            gate.check_zip_layout(p)


if __name__ == "__main__":
    unittest.main()
