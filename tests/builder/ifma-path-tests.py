#!/usr/bin/env python3
"""Focused host-parent and extracted-root path tests for patch-ifma.py."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PATCHER_PATH = Path(os.environ.get("PATCH_IFMA_PATH", ROOT / "builder/ifma/patch-ifma.py"))
SPEC = importlib.util.spec_from_file_location("patch_ifma_path_tests", PATCHER_PATH)
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)


class PathSafetyTests(unittest.TestCase):
    def test_symlinked_host_parent_is_allowed(self):
        with tempfile.TemporaryDirectory(prefix="ifma-path-") as directory:
            base = Path(directory)
            real = base / "real-parent"
            real.mkdir()
            alias = base / "parent-alias"
            alias.symlink_to(real, target_is_directory=True)

            root = real / "root"
            root.mkdir()
            manifest = real / "manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            report = alias / "report.json"

            with mock.patch.object(PATCHER, "_load_manifest", return_value=[]), \
                    mock.patch.object(PATCHER, "_select_scope", return_value=[]):
                result = PATCHER.run(
                    alias / "root",
                    alias / "manifest.json",
                    report,
                    False,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(report.resolve(), (real / "report.json").resolve())
            self.assertTrue(report.is_file())

    def test_root_manifest_and_report_leaf_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory(prefix="ifma-path-") as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            manifest = base / "manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            report = base / "report.json"
            report.write_text("{}\n", encoding="utf-8")

            cases = []
            root_link = base / "root-link"
            root_link.symlink_to(root, target_is_directory=True)
            cases.append(root_link)
            manifest_link = base / "manifest-link.json"
            manifest_link.symlink_to(manifest)
            cases.append(manifest_link)
            report_link = base / "report-link.json"
            report_link.symlink_to(report)
            cases.append(report_link)

            for path in cases:
                with self.subTest(path=path), self.assertRaisesRegex(PATCHER.PatchError, "symlink path refused"):
                    PATCHER._resolve_host_path(path, allow_missing_leaf=path == report_link)

    def test_every_component_inside_root_must_not_be_a_symlink(self):
        with tempfile.TemporaryDirectory(prefix="ifma-path-") as directory:
            root = Path(directory).resolve()
            real = root / "real"
            real.mkdir()
            (real / "target").write_bytes(b"elf")
            (root / "inside-link").symlink_to(real, target_is_directory=True)

            with self.assertRaisesRegex(PATCHER.PatchError, "symlink target path component refused"):
                PATCHER._target_path(root, "inside-link/target")


if __name__ == "__main__":
    unittest.main(verbosity=2)
