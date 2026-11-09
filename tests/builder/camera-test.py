#!/usr/bin/env python3
"""Offline camera overlay integrity, expected delta, and CMake wrap regression tests."""
import configparser
import contextlib
import hashlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("kano_vendor", ROOT / "profiles/kano/vendor.py")
hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hooks)


class CameraTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.tree = self.work / "tree"
        self.overlay = self.work / "out/vendor"
        self.tree.mkdir()
        self.overlay.mkdir(parents=True)
        self.vmap = {}
        fixture_pins = {}
        for rel in hooks.CAMERA_GRALLOC_SHA256:
            data = ("fixture:" + rel).encode()
            path = self.tree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            fixture_pins[rel] = hashlib.sha256(data).hexdigest()
        self.pin_patch = mock.patch.dict(hooks.CAMERA_GRALLOC_SHA256, fixture_pins, clear=True)
        self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)
        for rel, content, label in (
            ("lib64/libcamera", None, "vendor_file"),
            ("lib64/libcamera_internal.so", b"stock-lib", "same_process_hal_file"),
            ("lib64/hw/camera.libcamera_hal_base.so", b"stock-hal", "same_process_hal_file"),
            ("etc/init/fwupd.rc", b"stock-init", "vendor_configs_file"),
            ("etc/selinux/vendor_sepolicy.cil", b";; stock policy\n", "vendor_configs_file"),
            ("etc/init/init.fatcat.rc", b"on early-init\n    setprop ro.hardware.camera libcamera_hal_base\n", "vendor_configs_file"),
        ):
            path = self.tree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                path.mkdir()
            else:
                path.write_bytes(content)
            self.vmap[rel] = {"selinux": f"u:object_r:{label}:s0", "uid": 0, "gid": 0}
        for rel, content in (
            ("lib64/hw/camera.kano.so", b"test-hal"),
            ("lib64/kanocam/libcamera.so.0.7.2", b"test-lib"),
            ("etc/libcamera/camera_hal.yaml", b"cameras: {}\n"),
        ):
            path = self.overlay / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.write_sums()

    def write_sums(self):
        entries = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  vendor/{p.relative_to(self.overlay)}\n"
                   for p in sorted(self.overlay.rglob("*")) if p.is_file()]
        (self.overlay.parent / "SHA256SUMS").write_text("".join(entries))

    def run_hook(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.camera(SimpleNamespace(tree=self.tree, vmap=self.vmap), self.overlay)

    def test_expected_delta_and_labels(self):
        before = {p.relative_to(self.tree).as_posix(): p.read_bytes() if p.is_file() else None
                  for p in self.tree.rglob("*")}
        expected_new, expected_changed = hooks.camera_expected(self.overlay, set(before))
        self.run_hook()
        after = {p.relative_to(self.tree).as_posix(): p.read_bytes() if p.is_file() else None
                 for p in self.tree.rglob("*")}
        self.assertEqual(sorted(set(after) - set(before)), expected_new)
        self.assertEqual(sorted(p for p in before if before[p] != after[p]), expected_changed)
        self.assertIn(b"setprop ro.hardware.camera kano\n", after["etc/init/init.fatcat.rc"])
        self.assertEqual(after["etc/selinux/vendor_sepolicy.cil"],
                         before["etc/selinux/vendor_sepolicy.cil"] +
                         b";; KANO camera: udmabuf allocator\n"
                         b"(allow hal_camera_default udmabuf_device (chr_file (open read write ioctl)))\n")
        for rel in ("lib64/hw/camera.kano.so", "lib64/kanocam/libcamera.so.0.7.2"):
            self.assertEqual(self.vmap[rel]["selinux"], "u:object_r:same_process_hal_file:s0")
        self.assertEqual(self.vmap["etc/libcamera/camera_hal.yaml"]["selinux"],
                         "u:object_r:vendor_configs_file:s0")

    def test_each_gralloc_binary_must_match_before_vendor_mutation(self):
        for rel in hooks.CAMERA_GRALLOC_SHA256:
            with self.subTest(rel=rel):
                path = self.tree / rel
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                with self.assertRaisesRegex(SystemExit, "unsupported vendor binary"):
                    self.run_hook()
                self.assertFalse((self.tree / "lib64/hw/camera.kano.so").exists())
                self.assertEqual((self.tree / "etc/selinux/vendor_sepolicy.cil").read_bytes(),
                                 b";; stock policy\n")
                path.write_bytes(original)

    def test_gralloc_symlink_and_missing_dependency_refused(self):
        rel = "lib64/libminigbm_gralloc.so"
        path = self.tree / rel
        saved = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(SystemExit, "missing or non-regular"):
            self.run_hook()
        target = self.work / "external-gralloc.so"
        target.write_bytes(saved)
        path.symlink_to(target)
        with self.assertRaisesRegex(SystemExit, "missing or non-regular"):
            self.run_hook()
        self.assertFalse((self.tree / "lib64/hw/camera.kano.so").exists())

    def test_policy_missing_or_already_present_refused_before_copy(self):
        policy = self.tree / "etc/selinux/vendor_sepolicy.cil"
        original = policy.read_text()
        rule = "(allow hal_camera_default udmabuf_device (chr_file (open read write ioctl)))"
        for content in (None, original + rule + "\n"):
            with self.subTest(content=content):
                if content is None:
                    policy.unlink()
                else:
                    policy.write_text(content)
                with self.assertRaisesRegex(SystemExit, "camera policy missing|already present"):
                    self.run_hook()
                self.assertFalse((self.tree / "lib64/hw/camera.kano.so").exists())
                self.assertEqual((self.tree / "etc/init/init.fatcat.rc").read_text(),
                                 "on early-init\n    setprop ro.hardware.camera libcamera_hal_base\n")

    def test_without_camera_hook_policy_remains_stock(self):
        policy = self.tree / "etc/selinux/vendor_sepolicy.cil"
        before = policy.read_bytes()
        hooks.expected({"audio": {"config_dir": ""},
                        "input": {"kbd_backlight_leds": "", "power_button": "acpi"}})
        self.assertEqual(policy.read_bytes(), before)

    def test_corrupt_file_refused_before_copy(self):
        (self.overlay / "lib64/hw/camera.kano.so").write_bytes(b"corrupt")
        with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
            self.run_hook()
        self.assertFalse((self.tree / "lib64/hw/camera.kano.so").exists())

    def test_extra_file_refused(self):
        (self.overlay / "extra").write_bytes(b"extra")
        with self.assertRaisesRegex(SystemExit, "files differ"):
            self.run_hook()

    def test_missing_file_refused(self):
        (self.overlay / "lib64/hw/camera.kano.so").unlink()
        with self.assertRaisesRegex(SystemExit, "files differ"):
            self.run_hook()

    def test_symlink_refused(self):
        path = self.overlay / "lib64/hw/camera.kano.so"
        path.unlink()
        path.symlink_to(self.tree / "lib64/hw/camera.libcamera_hal_base.so")
        self.write_sums()
        with self.assertRaisesRegex(SystemExit, "not a regular file"):
            self.run_hook()

    def test_stock_file_collision_refused(self):
        path = self.tree / "lib64/hw/camera.kano.so"
        path.write_bytes(b"keep-stock")
        with self.assertRaisesRegex(SystemExit, "would replace a lapis file"):
            self.run_hook()
        self.assertEqual(path.read_bytes(), b"keep-stock")

    def test_changed_selector_refused(self):
        (self.tree / "etc/init/init.fatcat.rc").write_text("on early-init\n")
        with self.assertRaisesRegex(SystemExit, "camera line not as expected"):
            self.run_hook()

    def test_cmake_wrap_keeps_pinned_revision(self):
        # The upstream 0.7.2 wrap omits the method; Meson 1.12 then requires meson.build.
        source = self.work / "source"
        (source / "subprojects").mkdir(parents=True)
        wrap = source / "subprojects/libyuv.wrap"
        original = ("# SPDX-License-Identifier: CC0-1.0\n\n[wrap-git]\ndirectory = libyuv\n"
                    "url = https://chromium.googlesource.com/libyuv/libyuv.git\n"
                    "revision = 500f45652c459cfccd20f83f297eb66cb7b015cb\n"
                    "diff_files = libyuv/0004-CMakeLists.txt-Do-not-enable-NEON-for-armel-armhf.patch\n")
        wrap.write_text(original)
        patch = ROOT / "builder/camera/patches/0005-libyuv-cmake-wrap.patch"
        result = subprocess.run(["patch", "-d", str(source), "-p1", "-N", "-s"],
                                input=patch.read_bytes(), capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        config = configparser.ConfigParser()
        config.read(wrap)
        self.assertEqual(config["wrap-git"]["method"], "cmake")
        self.assertEqual(wrap.read_text().replace("method = cmake\n", ""), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
