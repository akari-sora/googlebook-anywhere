#!/usr/bin/env python3
"""Offline ELF/module, installed-firmware, profile-policy and vendor gate fixtures.

No image, network, kernel tools, compiler or root required. The vendor gate test
executes its real shell block with only partition extraction replaced by fixtures.
"""
import gzip
import importlib.util
import json
import lzma
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "builder/kmod/check-firmware.py"
sys.path.insert(0, str(ROOT / "builder/lib"))
import devprofile


def load_checker():
    """Import the checker (its filename has a dash) for helper-level checks."""
    spec = importlib.util.spec_from_file_location("check_firmware", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def module(*firmware, extra=b""):
    """Minimal ELF64 relocatable with a real section table, not a strings mock."""
    names = b"\0.shstrtab\0.modinfo\0"
    info = b"license=GPL\0name=fixture\0" + b"".join(b"firmware=" + f.encode() + b"\0" for f in firmware)
    data = bytearray(64)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", data, 16, 1, 62, 1)
    data += names + info + extra
    shoff = len(data)
    struct.pack_into("<Q", data, 40, shoff)
    struct.pack_into("<HHHH", data, 52, 64, 0, 0, 64)
    struct.pack_into("<HH", data, 60, 3, 1)
    data += bytes(64)
    data += struct.pack("<IIQQQQIIQQ", 1, 3, 0, 0, 64, len(names), 0, 0, 1, 0)
    data += struct.pack("<IIQQQQIIQQ", 11, 1, 0, 0, 64 + len(names), len(info), 0, 0, 1, 0)
    return bytes(data)


def archive(entries):
    data = bytearray()
    for name, body in [*entries, ("TRAILER!!!", b"")]:
        rawname = name.encode() + b"\0"
        fields = [1, 0o100644, 0, 0, 1, 0, len(body), 0, 0, 0, 0, len(rawname), 0]
        data += b"070701" + b"".join(f"{v:08x}".encode() for v in fields) + rawname
        data += b"\0" * (-len(data) % 4)
        data += body
        data += b"\0" * (-len(data) % 4)
    return bytes(data)


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gbos-firmware-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / "generic-x86"
        self.profile.mkdir()
        for filename in ("profile.toml", "vendor.py"):
            shutil.copyfile(ROOT / "profiles/generic-x86" / filename, self.profile / filename)
        self.base_vendor = self.root / "vendor-tree-base"
        self.base_vendor.mkdir()
        self.vendor = self.root / "vendor-tree-new"
        self.firmware = self.vendor / "firmware"
        self.firmware.mkdir(parents=True)
        self.stock = self.root / "firmware-vendor_dlkm"
        self.system = self.root / "firmware-system_dlkm"
        self.stock.mkdir()
        self.system.mkdir()
        self.initrd = self.root / "initrd.new.img"
        self.initrd.write_bytes(archive([]))
        self.out = self.root / "out"
        self.out.mkdir()
        self.report = self.root / "firmware-report.json"
        self.ko(self.vendor, "lib/kano/gbos_efifb.ko")

    def ko(self, root, name, *firmware):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(module(*firmware))
        return path

    def require(self, *names):
        # Model explicit profile loads; do not make every stock fixture required.
        path = self.profile / "profile.toml"
        text = path.read_text()
        old = devprofile.load(self.profile)["wifi"]["modprobe"]
        text = text.replace("modprobe = " + json.dumps(old),
                            "modprobe = " + json.dumps(sorted(set(old) | set(names))))
        path.write_text(text)

    def required_ko(self, root, name, *firmware):
        self.require(Path(name).name.split(".ko")[0].replace("-", "_"))
        return self.ko(root, name, *firmware)

    def fw(self, name, data=b"firmware fixture"):
        path = self.firmware / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def rule(self, kind, module_name, firmware, reason="fixture driver's documented fallback", files=None):
        text = f"\n[[firmware_check.{kind}]]\n"
        for key, value in (("module", module_name), ("firmware", firmware), ("reason", reason)):
            text += f"{key} = {json.dumps(value)}\n"
        if files is not None:
            text += f"files = {json.dumps(files)}\n"
        with (self.profile / "profile.toml").open("a") as f:
            f.write(text)

    def run_check(self, extra_env=None, cwd=None, vendor=None, initrd=None):
        self.report.unlink(missing_ok=True)
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run([
            sys.executable, "-B", str(CHECKER), "--profile", str(self.profile),
            "--vendor", str(self.vendor if vendor is None else vendor),
            "--base-vendor", str(self.base_vendor),
            "--module-tree", str(self.stock),
            "--module-tree", str(self.system),
            "--initrd", str(self.initrd if initrd is None else initrd),
            "--report", str(self.report),
        ], text=True, capture_output=True, env=env, cwd=cwd)
        report = json.loads(self.report.read_text()) if self.report.exists() else {}
        return proc, report

    def passes(self):
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(report.get("ok"), report)
        return report

    def fails(self, *needles):
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIs(report.get("ok"), False, report)
        for needle in needles:
            self.assertIn(needle, proc.stderr)
        return report

    def test_all_present_across_every_shipped_module_source(self):
        self.required_ko(self.stock, "lib/modules/iwlwifi.ko", "wifi.bin")
        self.required_ko(self.system, "lib/modules/snd-sof.ko", "audio.bin")
        self.required_ko(self.vendor, "lib/dev-modules/amdgpu.ko", "gpu.bin")
        self.ko(self.vendor, "lib/kano/gbos_efifb.ko", "extra.bin")
        self.initrd.write_bytes(archive([("lib/modules/first.ko", module("early.bin"))]))
        for name in ("wifi.bin", "audio.bin", "gpu.bin", "extra.bin", "early.bin"):
            self.fw(name)
        report = self.passes()
        self.assertEqual(len(report["modules"]), 5)
        self.assertEqual(
            {m["module"]: m["source"] for m in report["modules"]},
            {
                "iwlwifi": "vendor_dlkm:lib/modules/iwlwifi.ko",
                "snd_sof": "system_dlkm:lib/modules/snd-sof.ko",
                "amdgpu": "vendor:lib/dev-modules/amdgpu.ko",
                "gbos_efifb": "vendor:lib/kano/gbos_efifb.ko",
                "first": "initrd:lib/modules/first.ko",
            },
        )

    def assert_no_host_paths(self, raw):
        # Only the tree root (in both spellings) and its random leaf name are secrets.
        self.assertNotIn(str(self.root), raw)
        self.assertNotIn(str(self.root.resolve()), raw)
        self.assertNotIn(self.root.name, raw)

    def test_unreadable_required_module_fails_without_host_path(self):
        if os.geteuid() == 0:
            print("SKIP unreadable-module test (running as root: chmod 000 is not enforced)")
            self.skipTest("chmod 000 does not stop root")
        blocked = self.required_ko(self.stock, "lib/modules/unreadable.ko", "intel/missing.bin")
        blocked.chmod(0)
        self.addCleanup(blocked.chmod, 0o644)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(report["profile"], "generic-x86")
        self.assertIn(
            "module unreadable (vendor_dlkm:lib/modules/unreadable.ko): Permission denied",
            proc.stderr)
        self.assert_no_host_paths(self.report.read_text() + proc.stdout + proc.stderr)

    def test_missing_vendor_hook_fails_without_host_path(self):
        (self.profile / "vendor.py").unlink()
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(report["profile"], "generic-x86")
        self.assertIn("hooks.vendor", proc.stderr)
        self.assertIn("generic-x86", proc.stderr)
        self.assert_no_host_paths(self.report.read_text() + proc.stdout + proc.stderr)

    def test_missing_base_vendor_tree_fails_without_host_path(self):
        self.base_vendor.rmdir()
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(report["profile"], "generic-x86")
        self.assertIn("missing original vendor module tree: vendor-tree-base", proc.stderr)
        self.assert_no_host_paths(self.report.read_text() + proc.stdout + proc.stderr)

    def test_unreadable_base_vendor_module_fails_without_host_path(self):
        # The untouched base vendor tree is a logical root like any other: a message
        # raised while reading the original module names the tree, never the host path.
        check = load_checker()
        args = types.SimpleNamespace(
            profile=self.profile, vendor=self.vendor, module_tree=[self.stock],
            initrd=self.initrd, report=self.report, base_vendor=self.base_vendor)
        message = f"cannot read {self.base_vendor}/lib/kano/gbos_efifb.ko"
        self.assertEqual(check.scrub_paths(message, check.logical_roots(args)),
                         "cannot read vendor-tree-base/lib/kano/gbos_efifb.ko")
        if os.geteuid() == 0:
            print("SKIP unreadable-base-vendor test (running as root: chmod 000 is not enforced)")
            self.skipTest("chmod 000 does not stop root")
        blocked = self.ko(self.base_vendor, "lib/kano/gbos_efifb.ko")
        blocked.chmod(0)
        self.addCleanup(blocked.chmod, 0o644)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(report["profile"], "generic-x86")
        self.assertIn("Permission denied: gbos_efifb.ko", proc.stderr)
        self.assert_no_host_paths(self.report.read_text() + proc.stdout + proc.stderr)

    def test_unsupported_safety_schema_fails_without_host_path(self):
        # A checkout copy whose profiles/_safety.toml is broken: the message must name
        # the logical file, not the fixture root or the copy's checkout root.
        checkout = self.root / "checkout"
        (checkout / "builder/kmod").mkdir(parents=True)
        (checkout / "builder/lib").mkdir()
        (checkout / "profiles").mkdir()
        shutil.copyfile(CHECKER, checkout / "builder/kmod/check-firmware.py")
        shutil.copyfile(ROOT / "builder/lib/devprofile.py", checkout / "builder/lib/devprofile.py")
        safety = (ROOT / "profiles/_safety.toml").read_text()
        (checkout / "profiles/_safety.toml").write_text(safety.replace("schema = 1", "schema = 2", 1))
        self.report.unlink(missing_ok=True)
        proc = subprocess.run([
            sys.executable, "-B", str(checkout / "builder/kmod/check-firmware.py"),
            "--profile", str(self.profile), "--vendor", str(self.vendor),
            "--base-vendor", str(self.base_vendor), "--module-tree", str(self.stock),
            "--module-tree", str(self.system), "--initrd", str(self.initrd),
            "--report", str(self.report),
        ], text=True, capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        raw = self.report.read_text() + proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, raw)
        self.assertIn("profiles/_safety.toml: schema 2 is not supported", raw)
        self.assertIn("generic-x86", raw)
        self.assert_no_host_paths(raw)
        self.assertNotIn(str(checkout.resolve()), raw)

    def test_profile_inside_a_checkout_keeps_its_logical_name(self):
        # build.sh's default GBOS_PROFILE is "$HERE/../profiles/<name>": a parent-detour
        # spelling of a directory inside the checkout. That root is a prefix of the
        # profile path, so a mapping applied shortest-root-first would rewrite the
        # profile into its detour spelling instead of the logical name.
        # The checkout lives under the resolved temp root: the checker anchors on its own
        # resolved path, so a macOS "/var" -> "/private/var" salt must not spell the same
        # directory two ways and hide the shallowest root.
        checkout = self.root.resolve() / "checkout"
        (checkout / "builder/kmod").mkdir(parents=True)
        (checkout / "builder/lib").mkdir()
        shutil.copyfile(CHECKER, checkout / "builder/kmod/check-firmware.py")
        shutil.copyfile(ROOT / "builder/lib/devprofile.py", checkout / "builder/lib/devprofile.py")
        shutil.copytree(ROOT / "profiles", checkout / "profiles")
        # A missing hooks.vendor file makes the loader name the profile directory.
        (checkout / "profiles/generic-x86/vendor.py").unlink()
        profile = checkout / "builder" / ".." / "profiles" / "generic-x86"
        self.report.unlink(missing_ok=True)
        proc = subprocess.run([
            sys.executable, "-B", str(checkout / "builder/kmod/check-firmware.py"),
            "--profile", str(profile), "--vendor", str(self.vendor),
            "--base-vendor", str(self.base_vendor), "--module-tree", str(self.stock),
            "--module-tree", str(self.system), "--initrd", str(self.initrd),
            "--report", str(self.report),
        ], text=True, capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        raw = self.report.read_text() + proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, raw)
        self.assertIn("hooks.vendor", raw)
        self.assertIn("generic-x86", raw)
        self.assertNotIn("builder/../profiles", raw)
        self.assert_no_host_paths(raw)
        self.assertNotIn(str(checkout.resolve()), raw)

    def test_relative_vendor_and_initrd_spellings_keep_names(self):
        # A caller may spell the trees relatively (--vendor v, --initrd i). The short
        # spellings are not host paths: the module, the profile and the message text
        # must survive untouched, unlike a blind substring replace of "v"/"i".
        self.vendor.rename(self.root / "v")
        self.vendor = self.root / "v"
        self.initrd.rename(self.root / "i")
        self.initrd = self.root / "i"
        self.required_ko(self.stock, "lib/modules/uvcvideo.ko", "intel/missing.bin")
        proc, report = self.run_check(cwd=self.root, vendor=Path("v"), initrd=Path("i"))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(report["profile"], "generic-x86")
        entry = next(m for m in report["modules"] if m["module"] == "uvcvideo")
        self.assertEqual(entry["source"], "vendor_dlkm:lib/modules/uvcvideo.ko")
        raw = self.report.read_text() + proc.stdout + proc.stderr
        self.assertIn("missing required firmware intel/missing.bin", raw)
        self.assertNotIn("uvendorcvendorideo", raw)
        self.assertNotIn("generinitrdc-x86", raw)

    def test_scrub_only_rewrites_absolute_roots_on_a_path_boundary(self):
        check = load_checker()
        mapping = [("/build/firmware-vendor_dlkm", "vendor_dlkm"), ("/build/vendor", "vendor")]
        self.assertEqual(check.scrub_paths("/build/vendor/lib/a.ko", mapping), "vendor/lib/a.ko")
        # A sibling whose name merely starts with the root is a different tree.
        self.assertEqual(check.scrub_paths("/build/vendor2/lib/a.ko", mapping),
                         "/build/vendor2/lib/a.ko")
        self.assertEqual(check.scrub_paths("/build/vendorish", mapping), "/build/vendorish")
        self.assertEqual(check.scrub_paths("/build/vendor", mapping), "vendor")

    def test_scrub_drops_the_repo_root_and_its_separator(self):
        check = load_checker()
        mapping = [("/x/repo", "")]
        self.assertEqual(check.scrub_paths("/x/repo/profiles/_safety.toml", mapping),
                         "profiles/_safety.toml")
        self.assertEqual(check.scrub_paths("/x/repo", mapping), "")
        # Only a path boundary may precede the root: a longer sibling name is left alone.
        self.assertEqual(check.scrub_paths("/x/repo-other/profiles/_safety.toml", mapping),
                         "/x/repo-other/profiles/_safety.toml")

    def test_scrub_leaves_logical_names_in_a_message_alone(self):
        # A message may already name logical sources. A caller's relative spelling must
        # never enter the mapping as text that rewrites them, in either direction.
        check = load_checker()
        args = types.SimpleNamespace(
            profile=self.profile, vendor=Path("v"), module_tree=[self.stock],
            initrd=Path("i"), report=self.report, base_vendor=self.base_vendor)
        message = ("vendor:lib/modules/uvcvideo.ko missing; "
                   "initrd:lib/modules/early.ko present")
        self.assertEqual(check.scrub_paths(message, check.logical_roots(args)), message)

    def test_logical_roots_skips_relative_spellings(self):
        check = load_checker()
        args = types.SimpleNamespace(
            profile=self.profile, vendor=Path("v"), module_tree=[self.stock],
            initrd=Path("i"), report=self.report, base_vendor=self.base_vendor)
        raws = [raw for raw, _ in check.logical_roots(args)]
        self.assertEqual([raw for raw in raws if not raw.startswith("/")], [])
        self.assertIn(str(Path("v").resolve()), raws)
        self.assertIn(str(Path("i").resolve()), raws)
        self.assertIn(str(self.stock.resolve()), raws)
        self.assertIn(str(self.base_vendor.resolve()), raws)

    def test_declared_firmware_name_is_never_rewritten(self):
        # A data field. The firmware name here echoes the vendor tree between "+"
        # delimiters; the same text inside an error would be mapped, but firmware and
        # matched are data and must survive byte-for-byte.
        name = f"intel+{self.vendor}+suffix/present.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        self.fw(name)
        report = self.passes()
        entry = next(m for m in report["modules"] if m["module"] == "btusb")
        self.assertEqual(entry["firmware"][0]["firmware"], name)
        self.assertEqual(entry["firmware"][0]["matched"], [name])

    def test_scrub_keeps_sibling_trees_with_plus_and_non_ascii_names(self):
        check = load_checker()
        mapping = [("/build/vendor", "vendor")]
        # "+" and a non-ASCII letter are not path delimiters: a sibling tree whose name
        # merely continues the root is a different tree and survives, even in an error.
        self.assertEqual(check.scrub_paths("cannot read /build/vendor+extra/a.ko", mapping),
                         "cannot read /build/vendor+extra/a.ko")
        self.assertEqual(check.scrub_paths("cannot read /build/vendor甲/a.ko", mapping),
                         "cannot read /build/vendor甲/a.ko")

    def test_scrub_requires_a_left_boundary_before_a_root(self):
        check = load_checker()
        mapping = [("/build/vendor", "vendor")]
        # The left delimiter is an explicit set, not "any non-path character": a root
        # reached through a longer path is not a root.
        self.assertEqual(check.scrub_paths("/prefix/build/vendor/a.ko", mapping),
                         "/prefix/build/vendor/a.ko")
        # A real delimiter before the root still maps, and the delimiter is kept.
        self.assertEqual(check.scrub_paths("(/build/vendor)/a.ko", mapping), "(vendor)/a.ko")
        self.assertEqual(check.scrub_paths("x=/build/vendor,y", mapping), "x=vendor,y")

    def test_scrub_escapes_regex_metacharacters_in_a_root(self):
        check = load_checker()
        # A root is escaped before it enters the pattern: "[1]+." is a literal path, not
        # a character class followed by "one-or-more of anything".
        mapping = [("/x/repo[1]+.copy", "")]
        self.assertEqual(
            check.scrub_paths("/x/repo[1]+.copy/profiles/_safety.toml", mapping),
            "profiles/_safety.toml")

    def test_scrub_maps_a_root_that_is_only_slashes_without_a_key_error(self):
        check = load_checker()
        # The repo root maps to nothing and swallows one separator. A root of only
        # slashes must not make replace() strip a "/" back off to find its key.
        self.assertEqual(check.scrub_paths("//", [("//", "")]), "")
        self.assertEqual(check.scrub_paths("///", [("///", "")]), "")

    def test_profile_of_only_slashes_fails_without_a_traceback(self):
        # "//" is absolute but names nothing, so it is not a root. The loader still
        # fails on it, cleanly, and the scrub never rewrites every path to "".
        self.report.unlink(missing_ok=True)
        proc = subprocess.run([
            sys.executable, "-B", str(CHECKER), "--profile", "//",
            "--vendor", str(self.vendor), "--base-vendor", str(self.base_vendor),
            "--module-tree", str(self.stock), "--module-tree", str(self.system),
            "--initrd", str(self.initrd), "--report", str(self.report),
        ], text=True, capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("firmware check failed", proc.stderr)

    def test_relative_profile_names_the_logical_profile_on_a_broken_hook(self):
        # build.sh spells the profile relatively. The loader resolves it, so its error
        # carries an absolute path the scrub maps back to the logical "generic-x86".
        nested = self.root / "sub"
        nested.mkdir()
        copy = nested / "generic-x86"
        shutil.copytree(self.profile, copy)
        (copy / "vendor.py").unlink()
        self.report.unlink(missing_ok=True)
        proc = subprocess.run([
            sys.executable, "-B", str(CHECKER), "--profile", "sub/generic-x86",
            "--vendor", str(self.vendor), "--base-vendor", str(self.base_vendor),
            "--module-tree", str(self.stock), "--module-tree", str(self.system),
            "--initrd", str(self.initrd), "--report", str(self.report),
        ], text=True, capture_output=True, cwd=self.root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        raw = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, raw)
        self.assertIn("hooks.vendor", raw)
        self.assertIn("generic-x86", raw)
        self.assertNotIn("sub/generic-x86", raw)
        self.assert_no_host_paths(raw)

    def test_unwritable_report_fails_without_host_path(self):
        # An unwritable --report must fail closed with the logical file name and the
        # reason, never a traceback naming the host directory.
        self.report = self.root / "absent-parent" / "firmware-report.json"
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(report, {})
        self.assertIn("firmware check: cannot write report firmware-report.json:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("absent-parent", proc.stderr)
        self.assert_no_host_paths(proc.stdout + proc.stderr)

    def test_missing_names_profile_module_and_firmware(self):
        self.required_ko(self.stock, "lib/modules/btusb.ko", "intel/present.bin", "intel/missing.bin")
        self.fw("intel/present.bin")
        report = self.fails("generic-x86", "btusb", "intel/missing.bin")
        self.assertEqual(len(report["errors"]), 1)

    def test_optional_missing_is_reported_with_reason(self):
        self.required_ko(self.stock, "lib/modules/btusb.ko", "intel/diagnostic.bin")
        self.rule("optional", "btusb", "intel/diagnostic.bin", reason="Diagnostic dump only; no transport dependency")
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OPTIONAL firmware:", proc.stdout)
        self.assertEqual(report["optional"][0]["status"], "optional-missing")
        self.assertIn("Diagnostic", report["optional"][0]["reason"])

    def test_optional_present_still_reported(self):
        self.required_ko(self.stock, "btusb.ko", "optional.bin")
        self.fw("optional.bin")
        self.rule("optional", "btusb", "optional.bin")
        self.assertEqual(self.passes()["optional"][0]["status"], "optional-present")

    def test_version_range_satisfied_by_one_accepted_file(self):
        self.required_ko(self.stock, "iwlwifi.ko", "iwlwifi-so-a0-gf-a0-99.ucode")
        self.fw("iwlwifi-so-a0-gf-a0-89.ucode")
        self.rule("alternatives", "iwlwifi", "iwlwifi-so-a0-gf-a0-99.ucode",
                  files=["iwlwifi-so-a0-gf-a0-8[3-9].ucode", "iwlwifi-so-a0-gf-a0-9[0-9].ucode"])
        self.assertEqual(self.passes()["alternatives"][0]["matched"], ["iwlwifi-so-a0-gf-a0-89.ucode"])

    def test_versions_are_not_implicitly_interchangeable(self):
        self.required_ko(self.stock, "iwlwifi.ko", "iwlwifi-so-a0-gf-a0-99.ucode")
        self.fw("iwlwifi-so-a0-gf-a0-89.ucode")
        self.fails("iwlwifi-so-a0-gf-a0-99.ucode")

    def test_range_rejects_wrong_family_and_out_of_range(self):
        self.required_ko(self.stock, "iwlwifi.ko", "iwlwifi-so-a0-gf-a0-99.ucode")
        self.fw("iwlwifi-so-a0-gf-a0-82.ucode")
        self.fw("iwlwifi-ty-a0-gf-a0-89.ucode")
        self.rule("alternatives", "iwlwifi", "iwlwifi-so-a0-gf-a0-99.ucode",
                  files=["iwlwifi-so-a0-gf-a0-8[3-9].ucode"])
        self.fails("iwlwifi-so-a0-gf-a0-99.ucode")

    def test_wildcard_declaration_matches_installed_file(self):
        self.required_ko(self.stock, "btusb.ko", "intel/ibt-*.sfi")
        self.fw("intel/ibt-0040-0041.sfi")
        self.passes()

    def test_wildcard_without_match_fails(self):
        self.required_ko(self.stock, "btusb.ko", "intel/ibt-*.sfi")
        self.fw("intel/ibt-0040-0041.ddc")
        self.fails("ibt-*.sfi")

    def test_wildcard_does_not_cross_directories(self):
        self.required_ko(self.stock, "btusb.ko", "intel/ibt-*.sfi")
        self.fw("intel/ibt-other/firmware.sfi")
        self.fails("ibt-*.sfi")

    def test_optional_is_scoped_to_module(self):
        self.required_ko(self.stock, "btusb.ko", "same.bin")
        self.required_ko(self.stock, "iwlwifi.ko", "same.bin")
        self.rule("optional", "btusb", "same.bin")
        self.fails("iwlwifi", "same.bin")

    def test_optional_does_not_hide_another_firmware_entry(self):
        self.required_ko(self.stock, "btusb.ko", "optional.bin", "required.bin")
        self.rule("optional", "btusb", "optional.bin")
        self.fails("required.bin")

    def test_blank_reason_rejected_by_profile_loader(self):
        self.rule("optional", "btusb", "optional.bin", reason="  ")
        with self.assertRaisesRegex(devprofile.ProfileError, "reason"):
            devprofile.load(self.profile)
        self.fails("reason")

    def test_unknown_rule_key_rejected(self):
        self.rule("optional", "btusb", "optional.bin")
        with (self.profile / "profile.toml").open("a") as f:
            f.write('typo = "not accepted"\n')
        self.fails("unknown key")

    def test_bad_rule_shapes_rejected(self):
        original = (self.profile / "profile.toml").read_text()
        for kind, mod, name, files in (
            ("alternatives", "btusb", "good.bin", []),
            ("optional", "*", "good.bin", None),
            ("optional", "btusb", "../host.bin", None),
            ("optional", "btusb", "/host.bin", None),
        ):
            with self.subTest(kind=kind, mod=mod, name=name):
                (self.profile / "profile.toml").write_text(original)
                self.rule(kind, mod, name, files=files)
                with self.assertRaises(devprofile.ProfileError):
                    devprofile.load(self.profile)

    def test_overlapping_rules_fail(self):
        self.required_ko(self.stock, "btusb.ko", "intel/test.bin")
        self.rule("optional", "btusb", "intel/*")
        self.rule("alternatives", "btusb", "intel/test.bin", files=["other.bin"])
        self.fails("overlapping", "intel/test.bin")

    def test_cache_only_file_does_not_count(self):
        self.required_ko(self.stock, "btusb.ko", "missing.bin")
        cache = self.root / "download-cache"
        cache.mkdir()
        (cache / "missing.bin").write_bytes(b"not installed")
        self.fails("missing.bin")

    def test_empty_file_or_directory_does_not_count(self):
        self.required_ko(self.stock, "btusb.ko", "empty.bin", "dir.bin")
        self.fw("empty.bin", b"")
        (self.firmware / "dir.bin").mkdir()
        self.fails("empty.bin", "dir.bin")

    def test_internal_relative_firmware_symlink(self):
        self.required_ko(self.stock, "snd-sof.ko", "intel/sof/sof-adl.ri")
        self.fw("intel/sof/sof-tgl.ri")
        (self.firmware / "intel/sof/sof-adl.ri").symlink_to("sof-tgl.ri")
        self.passes()

    def test_in_tree_absolute_firmware_symlink_resolves_inside_tree(self):
        self.required_ko(self.stock, "btusb.ko", "intel/ibt-0040-0041.sfi")
        self.fw("intel/real.sfi")
        (self.firmware / "intel/ibt-0040-0041.sfi").symlink_to("/vendor/firmware/intel/real.sfi")
        self.passes()

    def test_dangling_or_escaping_in_tree_absolute_symlink_fails(self):
        self.required_ko(self.stock, "btusb.ko", "intel/ibt-0040-0041.sfi")
        host = self.root / "host.bin"
        host.write_bytes(b"host only")
        outside_vendor = self.vendor / "etc/outside.bin"
        outside_vendor.parent.mkdir(parents=True)
        outside_vendor.write_bytes(b"outside firmware")
        # Real files that a wrong remap would resolve to: an absolute target outside
        # /vendor/firmware must stay missing, not be rebased onto one of these.
        self.fw("target.bin")
        (self.firmware / "vendor/etc").mkdir(parents=True)
        (self.firmware / "vendor/etc/outside.bin").write_bytes(b"wrongly rebased")
        link = self.firmware / "intel/ibt-0040-0041.sfi"
        link.parent.mkdir(parents=True, exist_ok=True)
        for target in (
            "/vendor/firmware/intel/missing.sfi",
            "/vendor/firmware/../../host.bin",
            "/vendor/etc/outside.bin",
            "/firmware/target.bin",
            "/target.bin",
            "/../../host.bin",
        ):
            with self.subTest(target=target):
                link.unlink(missing_ok=True)
                link.symlink_to(target)
                self.fails("ibt-0040-0041.sfi")

    def test_broken_escaping_absolute_and_looping_links_fail(self):
        self.required_ko(self.stock, "btusb.ko", "bad.bin")
        host = self.root / "host.bin"
        host.write_bytes(b"host only")
        for target in ("missing.bin", "../../host.bin", str(host), "bad.bin"):
            with self.subTest(target=target):
                link = self.firmware / "bad.bin"
                link.unlink(missing_ok=True)
                link.symlink_to(target)
                self.fails("bad.bin")

    def test_absolute_link_in_relative_chain_fails(self):
        self.required_ko(self.stock, "btusb.ko", "bad.bin")
        good = self.fw("present.bin")
        (self.firmware / "absolute.bin").symlink_to(good)
        (self.firmware / "bad.bin").symlink_to("absolute.bin")
        self.fails("bad.bin")

    def test_internal_directory_alias_matches_wildcard(self):
        self.required_ko(self.stock, "snd-sof.ko", "intel/sof/sof-*.ri")
        self.fw("intel/sof-v2/sof-adl.ri")
        (self.firmware / "intel/sof").symlink_to("sof-v2")
        (self.firmware / "intel/sof-v2/cycle").symlink_to("..")
        self.passes()

    def test_link_cannot_traverse_a_regular_file_as_directory(self):
        self.required_ko(self.stock, "btusb.ko", "bad.bin")
        self.fw("present.bin")
        self.fw("plain.bin")
        (self.firmware / "bad.bin").symlink_to("plain.bin/../present.bin")
        self.fails("bad.bin")

    def test_compressed_modules_are_inspected(self):
        self.require("btusb")
        for suffix, compress in (("gz", gzip.compress), ("xz", lzma.compress)):
            with self.subTest(suffix=suffix):
                path = self.stock / f"btusb.ko.{suffix}"
                path.write_bytes(compress(module("compressed.bin")))
                self.fails("btusb", "compressed.bin")
                self.fw("compressed.bin")
                self.passes()
                path.unlink()
                (self.firmware / "compressed.bin").unlink()

    def test_zst_compressed_module_is_inspected(self):
        if shutil.which("zstd") is None:
            print("SKIP zst module test (zstd not installed)")
            self.skipTest("zstd not installed")
        self.require("btusb")
        path = self.stock / "btusb.ko.zst"
        compressed = subprocess.run(
            ["zstd", "-q", "-c"], input=module("compressed-zst.bin"),
            capture_output=True, check=True,
        ).stdout
        path.write_bytes(compressed)
        self.fails("btusb", "compressed-zst.bin")
        self.fw("compressed-zst.bin")
        self.passes()

    def test_missing_zstd_names_the_command_not_the_module(self):
        self.require("btusb")
        (self.stock / "btusb.ko.zst").write_bytes(module("zst.bin"))
        # The checker resolves "zstd" on the PATH it is handed. Hand it an empty
        # directory, so the lookup cannot succeed on any host and the test never skips.
        empty = self.root / "empty-bin"
        empty.mkdir()
        proc, report = self.run_check(extra_env={"PATH": str(empty)})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("No such file or directory: zstd", proc.stderr)
        self.assertNotIn("No such file or directory: vendor_dlkm", proc.stderr)
        self.assert_no_host_paths(self.report.read_text() + proc.stdout + proc.stderr)

    def test_invalid_module_metadata_fails_closed(self):
        self.require("bad")
        path = self.stock / "bad.ko"
        for blob in (b"", b"firmware=not-an-elf\0", module("foo")[:90], module().replace(b".modinfo", b".missing")):
            with self.subTest(length=len(blob)):
                path.write_bytes(blob)
                self.fails("module bad")

    def test_non_x86_64_or_non_relocatable_elf_module_fails_closed(self):
        self.require("bad")
        self.fw("ok.bin")
        path = self.stock / "bad.ko"
        aarch64 = bytearray(module("ok.bin"))
        struct.pack_into("<H", aarch64, 18, 183)
        exec_elf = bytearray(module("ok.bin"))
        struct.pack_into("<H", exec_elf, 16, 2)
        for label, blob in (("aarch64", bytes(aarch64)), ("et_exec", bytes(exec_elf))):
            with self.subTest(label=label):
                path.write_bytes(blob)
                self.fails("module bad", "not an x86-64 little-endian relocatable ELF module")

    def test_invalid_modinfo_firmware_name_fails_closed(self):
        self.require("bad")
        path = self.stock / "bad.ko"
        for bad_name in ("bad space.bin", "../escape.bin", "/abs.bin", "./dot.bin", "a//b.bin"):
            with self.subTest(bad_name=bad_name):
                path.write_bytes(module(bad_name))
                self.fails("module bad", "invalid firmware name")

    def test_only_modinfo_records_count(self):
        (self.stock / "btusb.ko").write_bytes(module(extra=b"firmware=not-declared.bin\0"))
        self.passes()

    def test_missing_system_partition_module_firmware(self):
        self.required_ko(self.system, "lib/modules/audio.ko", "system-only.bin")
        self.fails("audio", "system-only.bin")

    def test_missing_developer_module_firmware(self):
        self.required_ko(self.vendor, "lib/dev-modules/amdgpu.ko", "amdgpu/required.bin")
        self.fails("amdgpu", "amdgpu/required.bin")

    def test_initrd_concatenated_archives_and_bootconfig(self):
        self.require("first")
        bc = b"androidboot.test=1\n"
        body = archive([]) + bytes(512) + archive([("lib/modules/first.ko", module("early.bin"))])
        self.initrd.write_bytes(body + bc + struct.pack("<II", len(bc), sum(bc)) + b"#BOOTCONFIG\n")
        self.fails("first", "early.bin")
        self.fw("early.bin")
        self.passes()

    def test_unreadable_initrd_fails_closed(self):
        self.initrd.write_bytes(b"unknown archive format")
        self.fails("initrd")

    def test_missing_module_tree_fails_closed(self):
        self.system.rmdir()
        self.fails("missing shipped module tree")

    def test_declared_but_unshipped_module_fails(self):
        (self.vendor / "lib/kano/gbos_efifb.ko").unlink()
        self.fails("requested module gbos_efifb", "absent")

    def test_blacklisted_stock_module_is_reported_without_failing(self):
        self.ko(self.stock, "cros-ec.ko", "blacklisted-but-shipped.bin")
        report = self.passes()
        entry = next(m for m in report["modules"] if m["module"] == "cros_ec")
        self.assertEqual(entry["scope"], "report-only")
        self.assertEqual(entry["firmware"][0]["status"], "missing")

    def test_unselected_stock_modules_in_every_tree_are_report_only(self):
        self.ko(self.stock, "iwlwifi.ko", "wifi.bin")
        self.ko(self.system, "audio.ko", "audio.bin")
        self.ko(self.vendor, "lib/dev-modules/unselected.ko", "dev.bin")
        for root in (self.vendor, self.base_vendor):
            self.ko(root, "lib/stock.ko", "stock.bin")
        self.initrd.write_bytes(archive([("lib/modules/first.ko", module("early.bin"))]))
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["summary"]["report-only"],
                         {"modules": 5, "wanted": 5, "missing": 5, "metadata_errors": 0})
        self.assertIn("firmware report-only: 5 stock modules; 5 missing firmware declarations", proc.stdout)
        self.assertIn("firmware check: PASS; required: 1 modules, 0 firmware declarations; 6 modules;", proc.stdout)
        for entry in report["modules"]:
            if entry["module"] != "gbos_efifb":
                self.assertEqual(entry["scope"], "report-only")
                self.assertEqual(entry["firmware"][0]["status"], "missing")

    def test_optional_rule_does_not_promote_stock_module(self):
        self.ko(self.stock, "btusb.ko", "diagnostic.bin")
        self.ko(self.stock, "iwlwifi.ko", "iwlwifi-so-a0-gf-a0-99.ucode")
        self.fw("iwlwifi-so-a0-gf-a0-89.ucode")
        self.rule("optional", "btusb", "diagnostic.bin", reason="btusb is report-only here")
        self.rule("alternatives", "iwlwifi", "iwlwifi-so-a0-gf-a0-99.ucode",
                  reason="iwlwifi is report-only here",
                  files=["iwlwifi-so-a0-gf-a0-8[3-9].ucode"])
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(report["ok"])
        self.assertEqual(report["optional"], [])
        self.assertEqual(report["alternatives"], [])
        self.assertEqual(report["summary"]["report-only"]["missing"], 2)
        self.assertEqual(len(report["warnings"]), 2)
        self.assertIn("unused optional firmware rule for report-only module btusb (diagnostic.bin)", report["warnings"][0])
        self.assertIn("unused alternatives firmware rule for report-only module iwlwifi (iwlwifi-so-a0-gf-a0-99.ucode)", report["warnings"][1])
        warning_lines = [line for line in proc.stdout.splitlines() if line.startswith("UNUSED firmware rule:")]
        self.assertEqual(len(warning_lines), 2)
        self.assertIn("btusb", warning_lines[0])
        self.assertIn("iwlwifi", warning_lines[1])

    def test_stock_metadata_errors_are_report_only(self):
        (self.stock / "bad.ko").write_bytes(b"not ELF")
        report = self.passes()
        self.assertEqual(report["summary"]["report-only"]["metadata_errors"], 1)
        self.assertIn("module bad", report["warnings"][0])

    def test_added_and_replaced_vendor_modules_are_required(self):
        self.ko(self.base_vendor, "lib/stock.ko", "old.bin")
        self.ko(self.vendor, "lib/stock.ko", "replacement.bin")
        self.ko(self.vendor, "lib/hook-added.ko", "added.bin")
        report = self.fails("stock", "replacement.bin", "hook_added", "added.bin")
        for entry in report["modules"]:
            self.assertEqual(entry["scope"], "required")
            self.assertIn("builder vendor addition/replacement", entry["required_by"])

    def test_base_vendor_absolute_module_links_scope(self):
        (self.base_vendor / "lib/modules/real").mkdir(parents=True)
        (self.base_vendor / "lib/modules/real/stock.ko").write_bytes(module("stock.bin"))
        (self.vendor / "lib/modules").mkdir(parents=True)
        (self.vendor / "lib/modules/stock.ko").write_bytes(module("stock.bin"))
        self.fw("stock.bin")
        link = self.base_vendor / "lib/modules/stock.ko"
        # An absolute link to the /vendor mount whose bytes match the final module is
        # the original file, not a builder addition: report-only.
        link.symlink_to("/vendor/lib/modules/real/stock.ko")
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        entry = next(m for m in report["modules"] if m["source"] == "vendor:lib/modules/stock.ko")
        self.assertEqual(entry["scope"], "report-only")
        self.assertEqual(entry["required_by"], [])
        # "/lib/..." and "//vendor/..." are not the "/vendor" mount: pathlib anchors a
        # leading "//" separately from "/", so the link text is read as a builder vendor
        # addition/replacement and the module becomes required (Linux reads //vendor as
        # /vendor, but the checker compares the link text against the vendor mount).
        for target in ("/lib/modules/real/stock.ko", "//vendor/lib/modules/real/stock.ko"):
            with self.subTest(target=target):
                link.unlink()
                link.symlink_to(target)
                proc, report = self.run_check()
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                entry = next(m for m in report["modules"]
                             if m["source"] == "vendor:lib/modules/stock.ko")
                self.assertEqual(entry["scope"], "required")
                self.assertIn("builder vendor addition/replacement", entry["required_by"])

    def test_profile_kmod_is_required_even_when_already_in_base(self):
        for root in (self.vendor, self.base_vendor):
            self.ko(root, "lib/kano/gbos_efifb.ko", "extra.bin")
        report = self.fails("gbos_efifb", "extra.bin")
        self.assertEqual(report["modules"][0]["required_by"], ["profile.kmods"])

    def test_profile_modules_load_and_unblock_are_required(self):
        self.profile = self.root / "amd-hx370"
        self.profile.mkdir()
        for name in ("profile.toml", "vendor.py"):
            shutil.copyfile(ROOT / "profiles/amd-hx370" / name, self.profile / name)
        # The real profile names these stock modules; none is a builder addition.
        for name in ("bluetooth", "btusb", "snd_hda_codec_alc269", "iwlmvm", "snd_hda_intel"):
            self.ko(self.stock, name + ".ko")
        self.ko(self.stock, "btusb.ko", "load.bin")
        self.ko(self.stock, "snd_hda_intel.ko", "unblock.bin")
        report = self.fails("btusb", "load.bin", "snd_hda_intel", "unblock.bin")
        entries = {m["module"]: m for m in report["modules"]}
        self.assertEqual(entries["btusb"]["required_by"], ["profile.modules.load"])
        self.assertEqual(entries["snd_hda_intel"]["required_by"], ["profile.modules.unblock"])

    def test_builder_initrd_addition_is_required_before_stage_two(self):
        path = self.profile / "profile.toml"
        path.write_text(path.read_text().replace('pci_stub_ids = []', 'pci_stub_ids = ["8086:51a8"]'))
        self.fails("requested module pci_stub", "absent")
        # image.py copies this module from vendor_dlkm after the vendor gate.
        self.ko(self.stock, "pci-stub.ko", "early.bin")
        report = self.fails("pci_stub", "early.bin")
        entry = next(m for m in report["modules"] if m["module"] == "pci_stub")
        self.assertEqual(entry["required_by"], ["builder initrd addition"])
        self.fw("early.bin")
        self.passes()

    def test_missing_original_vendor_tree_fails_closed(self):
        self.base_vendor.rmdir()
        self.fails("missing original vendor module tree")

    def vendor_gate(self):
        script = (ROOT / "builder/vendor.sh").read_text()
        block = script.split('echo "check module firmware"\n', 1)[1].split('echo "replace ESP initrd"', 1)[0]
        # Replace only EROFS extraction. The real checker, profile loader, trees,
        # initrd and shell set -e are used; a removed checker lets PUBLISHED run.
        setup = "set -euo pipefail\n"
        for key, value in (("HERE", ROOT / "builder"), ("TMP", self.root),
                           ("IMG", self.root / "stage-vendor.img"), ("GBOS_PROFILE", self.profile),
                           ("GBOS_OUT", self.out)):
            setup += f"{key}={shlex.quote(str(value))}\n"
        setup += '''
python3() {
    if [[ "$1" = */lp-dump.py ]]; then
        [ "$2" = "$IMG" ] && [ "$3" = "$TMP" ]
        printf '%s\\n' "$4" >> "$TMP/partitions"
    else
        command python3 "$@"
    fi
}
ot() { [ "$1" = fsck.erofs ] && [ -d "${2#--extract=}" ]; }
'''
        return subprocess.run(["bash", "-c", setup + block + '\necho PUBLISHED\n'],
                              capture_output=True, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_vendor_wiring_runs_gate_before_publish(self):
        self.required_ko(self.system, "lib/modules/audio.ko", "wiring.bin")
        proc = self.vendor_gate()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("wiring.bin", proc.stderr)
        self.assertNotIn("PUBLISHED", proc.stdout)

    def test_vendor_wiring_accepts_complete_image_and_reports(self):
        self.required_ko(self.system, "lib/modules/audio.ko", "wiring.bin")
        self.fw("wiring.bin")
        proc = self.vendor_gate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PUBLISHED", proc.stdout)
        self.assertFalse(self.report.exists())
        self.assertTrue((self.out / "firmware-report.json").is_file())
        self.assertTrue(json.loads((self.out / "firmware-report.json").read_text())["ok"])
        self.assertEqual((self.root / "partitions").read_text().splitlines(), ["vendor_dlkm_a", "system_dlkm_a"])

    def test_vendor_wiring_reports_missing_stock_firmware_and_publishes(self):
        self.ko(self.stock, "other-hardware.ko", "unused.bin")
        proc = self.vendor_gate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("firmware report-only: 1 stock modules; 1 missing firmware declarations", proc.stdout)
        self.assertIn("firmware check: PASS; required: 1 modules, 0 firmware declarations; 2 modules; report: firmware-report.json", proc.stdout)
        self.assertIn("PUBLISHED", proc.stdout)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FirmwareTests)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    failed = len({getattr(test, "test_case", test).id() for test, _ in result.failures + result.errors})
    skipped = len(result.skipped)
    print(f"firmware tests: {result.testsRun - failed - skipped} passed, {failed} failed, {skipped} skipped")
    sys.exit(0 if result.wasSuccessful() else 1)
