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

D1 = "firmware name contains a local build path"
ADVISORY = "ADVISORY firmware pin: "
WITHHELD = "diagnostic withheld: it contained a local build path"




def vendor_sh_file(rel):
    """Round-4 Claude M6: the text builder/vendor.sh writes to vendor:<rel> (the Python string
    literal of its (tree / "<rel>").write_text(\"\"\"...\"\"\") call), so a fixture uses the
    script the builder really emits instead of a hand-written copy."""
    import ast
    script = (ROOT / "builder/vendor.sh").read_text()
    head = f'(tree / "{rel}").write_text("""'
    assert script.count(head) == 1, rel
    body = script.split(head, 1)[1].split('""")', 1)[0]
    return ast.literal_eval('"""' + body + '"""')


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


def newc(entries):
    """One newc archive of (name, body, ino, nlink, mode) records and its trailer."""
    data = bytearray()
    for name, body, ino, nlink, mode in [*entries, ("TRAILER!!!", b"", 0, 1, 0)]:
        rawname = name.encode() + b"\0"
        fields = [ino, mode, 0, 0, nlink, 0, len(body), 0, 0, 0, 0, len(rawname), 0]
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
        # Round-4 Claude M2: a positive control also proves the reverse check accepted every pin.
        self.assertEqual(report.get("advisories", []), [], report)
        return report

    def fails(self, *needles):
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIs(report.get("ok"), False, report)
        for needle in needles:
            self.assertIn(needle, proc.stderr)
        return report

    def advises(self, *needles):
        """gb-fwrev6 A1 (spec change): a reverse-check refusal is advisory in v0.2.3. The run
        passes (rc 0, ok true, no FAIL line), every needle is on an ADVISORY stdout line and in
        the report's top-level advisories list, and the PASS line counts the advisories."""
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIs(report.get("ok"), True, report)
        self.assertNotIn("FAIL ", proc.stderr)
        advisories = report.get("advisories")
        self.assertTrue(advisories, report)
        lines = [line for line in proc.stdout.splitlines() if line.startswith(ADVISORY)]
        self.assertEqual(lines, [ADVISORY + a for a in advisories])
        for advisory in advisories:
            self.assertRegex(advisory, r"^profile [^:]+: [^:]+: ")
        for needle in needles:
            self.assertTrue(any(needle in line for line in lines), (needle, proc.stdout))
            self.assertTrue(any(needle in a for a in advisories), (needle, advisories))
        self.assertIn(f"firmware check: PASS;", proc.stdout)
        self.assertIn(f"advisory: {len(advisories)}", proc.stdout)
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
        # delimiters: firmware and matched are data and must survive byte-for-byte.
        # D1 (round 1): a name that contains a host root's text fails the run, and the
        # log names it masked, never raw (this test passed before D1; see RESULT F1).
        name = f"intel+{self.vendor}+suffix/present.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        self.fw(name)
        report = self.fails(f"{D1}: 'intel+vendor+suffix/present.bin'")
        entry = next(m for m in report["modules"] if m["module"] == "btusb")
        # gb-fwrev5 decision: the report is an output too; the data field is withheld, not rewritten.
        self.assertEqual(entry["firmware"][0]["firmware"], WITHHELD)
        self.assertEqual(entry["firmware"][0]["matched"], [WITHHELD])
        self.assert_no_host_paths(self.report.read_text())

    def test_scrub_rewrites_diagnostics_only_never_data_fields(self):
        # "[" is a left and "]" a right delimiter, and both are valid in a firmware
        # name: the helper does map this name, so only "scrub diagnostics, never data"
        # keeps the firmware and matched fields byte-for-byte. D1: the run fails on it.
        check = load_checker()
        name = f"intel[{self.vendor}]/present.bin"
        self.assertEqual(check.scrub_paths(name, [(str(self.vendor), "vendor")]),
                         "intel[vendor]/present.bin")
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        self.fw(name)
        report = self.fails(f"{D1}: 'intel[vendor]/present.bin'")
        entry = next(m for m in report["modules"] if m["module"] == "btusb")
        # gb-fwrev5 decision: never rewritten, but withheld as a whole in the report.
        self.assertEqual(entry["firmware"][0]["firmware"], WITHHELD)
        self.assertEqual(entry["firmware"][0]["matched"], [WITHHELD])
        self.assert_no_host_paths(self.report.read_text())

    def test_bracket_firmware_name_with_a_host_root_fails_in_a_missing_error(self):
        # DeepSeek Important 1 / D1: 118ba0b printed this host path raw in the error.
        name = f"intel[{self.vendor}]/absent.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIs(report["ok"], False)
        self.assertIn(f"FAIL profile generic-x86: {D1}: 'intel[vendor]/absent.bin'", proc.stderr)
        self.assertIn("missing required firmware intel[vendor]/absent.bin", proc.stderr)
        self.assert_no_host_paths(proc.stdout + proc.stderr + json.dumps(report["errors"]))
        entry = next(m for m in report["modules"] if m["module"] == "btusb")
        self.assertEqual(entry["firmware"][0]["firmware"], WITHHELD)  # gb-fwrev5 decision
        self.assert_no_host_paths(self.report.read_text())

    def test_bracket_firmware_name_is_unchanged_in_a_missing_error(self):
        name = "intel[fw-x]/absent.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        report = self.fails(f"missing required firmware {name}")
        self.assertTrue(any(e.endswith(f"missing required firmware {name}") for e in report["errors"]),
                        report["errors"])
        self.assertFalse(any(D1 in e for e in report["errors"]), report["errors"])

    def test_bracket_firmware_name_with_a_host_root_fails_in_the_optional_line(self):
        name = f"intel[{self.vendor}]/optional.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        self.rule("optional", "btusb", name)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(report["optional"][0]["firmware"], WITHHELD)  # gb-fwrev5 decision
        self.assert_no_host_paths(self.report.read_text())
        self.assertIn("OPTIONAL firmware: profile generic-x86: module btusb: intel[vendor]/optional.bin "
                      "(optional-missing)", proc.stdout)
        self.assertIn(f"{D1}: 'intel[vendor]/optional.bin'", proc.stderr)
        self.assert_no_host_paths(proc.stdout + proc.stderr)

    def test_bracket_firmware_name_is_unchanged_in_the_optional_line(self):
        name = "intel[/fw]/optional.bin"  # "[" ends its part: a literal, not a class
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        self.rule("optional", "btusb", name)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(report["optional"][0]["firmware"], name)
        self.assertIn(f"OPTIONAL firmware: profile generic-x86: module btusb: {name} (optional-missing)",
                      proc.stdout)

    def test_matched_file_names_never_shield_a_host_path_codex_i2(self):
        # Codex I2: a matched file name may be any text ("cannot read <host>/x"); a
        # wildcard declaration matches it and a separate reason names the host path.
        # Only validated names are kept, so the OPTIONAL reason is still scrubbed.
        reason = f"cannot read {self.vendor}/reference.bin"
        relative = reason
        self.fw(relative)
        pattern = "/".join("*" for _ in relative.split("/"))
        self.required_ko(self.stock, "lib/modules/btusb.ko", pattern)
        self.rule("optional", "btusb", pattern, reason=reason)
        proc, report = self.run_check()
        # gb-fwrev5 decision: matched and reason hold the root; withheld in the report, run fails.
        self.assert_report_withheld(proc)
        self.assertEqual(report["optional"][0]["matched"], [WITHHELD])
        self.assertEqual(report["optional"][0]["reason"], WITHHELD)
        self.assertIn(f"(optional-present): cannot read vendor/reference.bin", proc.stdout)
        self.assert_no_host_paths(proc.stdout + proc.stderr)

    def assert_report_withheld(self, proc):
        # gb-fwrev5 decision: a report field that held a root is WITHHELD and the run fails.
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("a diagnostic contained a local build path and was withheld", proc.stderr)
        raw = self.report.read_text()
        self.assertIn(WITHHELD, raw)
        self.assert_no_host_paths(raw.replace("\\", ""))
        self.assertIs(json.loads(raw)["ok"], False)

    def test_data_reason_with_a_root_is_withheld_while_its_diagnostic_is_scrubbed(self):
        # Codex M3, changed by the gb-fwrev5 decision: the reason is data (never rewritten),
        # but it holds a root, so the report carries WITHHELD and the run fails; the printed
        # diagnostic is still scrubbed.
        reason = f"see {self.vendor}/reference.txt"
        self.required_ko(self.stock, "lib/modules/btusb.ko", "intel/opt.bin")
        self.rule("optional", "btusb", "intel/opt.bin", reason=reason)
        proc, report = self.run_check()
        self.assert_report_withheld(proc)
        self.assertEqual(report["optional"][0]["reason"], WITHHELD)
        self.assertIn("module btusb: intel/opt.bin (optional-missing): see vendor/reference.txt", proc.stdout)
        self.assert_no_host_paths(proc.stdout + proc.stderr)

    def test_unused_rule_names_pass_unchanged_codex_i3(self):
        name = "intel[fw-x]/unused.bin"
        self.rule("optional", "never_loaded", name)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"never_loaded ({name}):", report["warnings"][0])
        self.assertIn(f"UNUSED firmware rule: WARNING: profile generic-x86: unused optional firmware rule "
                      f"for report-only module never_loaded ({name}):", proc.stdout)

    def test_unused_rule_name_with_a_host_root_fails_codex_i3(self):
        # 118ba0b passed this run and printed intel[vendor]/unused.bin as if it were the rule.
        name = f"intel[{self.vendor}]/unused.bin"
        self.rule("optional", "never_loaded", name)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIs(report["ok"], False)
        self.assertIn(f"FAIL profile generic-x86: {D1}: 'intel[vendor]/unused.bin'", proc.stderr)
        self.assertIn("never_loaded (intel[vendor]/unused.bin)", proc.stdout)
        self.assert_no_host_paths(proc.stdout + proc.stderr + json.dumps(report["warnings"] + report["errors"]))

    def test_invalid_metadata_token_passes_unchanged(self):
        token = "intel[fw-x]/bad:colon.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", token)
        report = self.fails(f"invalid firmware name {token!r}")
        self.assertFalse(any(D1 in e for e in report["errors"]), report["errors"])

    def test_invalid_metadata_token_with_a_host_root_fails_with_d1(self):
        token = f"intel[{self.vendor}]/bad:colon.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", token)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"{D1}: 'intel[vendor]/bad:colon.bin'", proc.stderr)
        self.assert_no_host_paths(proc.stdout + proc.stderr + json.dumps(report["errors"]))

    def test_every_local_name_is_collected_and_fails(self):
        # Codex M4: two modules, two names with a host root; each one is reported.
        first = f"a[{self.vendor}]/one.bin"
        second = f"b[{self.stock}]/two.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", first, "plain.bin")
        self.required_ko(self.system, "lib/modules/snd-sof.ko", second)
        self.fw("plain.bin")
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"{D1}: 'a[vendor]/one.bin'", proc.stderr)
        self.assertIn(f"{D1}: 'b[vendor_dlkm]/two.bin'", proc.stderr)
        self.assertEqual(sum(D1 in e for e in report["errors"]), 2, report["errors"])
        self.assert_no_host_paths(proc.stdout + proc.stderr)

    def test_local_names_collect_every_root_name_longest_first(self):
        # F1 (round 3): keep is gone, so only the local half of protected_names remains.
        check = load_checker()
        mapping = [("/build/vendor", "vendor")]
        local = check.local_names(
            {"intel", "intel/a.bin", "", "x[/build/vendor]/b.bin", "y+/build/vendor+/c.bin"}, mapping)
        self.assertEqual(local, ["x[/build/vendor]/b.bin", "y+/build/vendor+/c.bin"])
        self.assertEqual(check.scrub_paths("see y+/build/vendor+/c.bin", mapping, local=local),
                         "see y+vendor+/c.bin")

    # Round 2 leak reproductions (F1-F3). Each runs the real CLI and asserts that no
    # host root text reaches stdout, stderr or a diagnostic field of the JSON (errors,
    # warnings, modules[].error). Data fields (firmware, matched, reason) stay raw by
    # contract and are not diagnostics. All of these leaked on f5c9768.
    def diagnostics(self, proc, report):
        texts = [proc.stdout, proc.stderr, *report.get("errors", []), *report.get("warnings", [])]
        texts += [m["error"] for m in report.get("modules", []) if "error" in m]
        return texts

    def assert_no_root_text(self, proc, report, *roots):
        roots = [str(self.root), str(self.root.resolve()), self.root.name, *roots]
        for text in self.diagnostics(proc, report):
            for root in roots:
                self.assertNotIn(root, text)
                self.assertNotIn(repr(root)[1:-1], text)

    def assert_withheld_failure(self, proc, report):
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIs(report.get("ok"), False, report)
        self.assert_no_root_text(proc, report)

    def root_prefix(self):
        return "/" + self.vendor.parts[1]  # e.g. "/tmp": the first part of every host root

    def test_r2_codex_i3_prefix_name_cannot_shield_a_reason(self):
        # Codex I3 entry 1: a declared name that is only a root prefix ("intel[/tmp").
        name = "intel[" + self.root_prefix()
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        self.rule("optional", "btusb", name, reason=f"see intel[{self.vendor}]/reference.bin")
        proc, report = self.run_check()
        # Without keep the boundary scrub maps this reason in the diagnostic; the report's
        # reason field holds the root, so it is withheld and the run fails (gb-fwrev5 decision).
        self.assert_report_withheld(proc)
        self.assertIn("see intel[vendor]/reference.bin", proc.stdout)
        self.assert_no_root_text(proc, report)

    def test_r2_codex_i3_unused_rule_prefix_cannot_shield_a_reason(self):
        # Codex I3 entry 2: the prefix name comes from an unused rule.
        name = "intel[" + self.root_prefix()
        self.rule("optional", "never_loaded", name, reason=f"see intel[{self.vendor}]/reference.bin")
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("see intel[vendor]/reference.bin", proc.stdout)
        self.assert_no_root_text(proc, report)

    def test_r2_codex_i3_invalid_token_cannot_shield_a_reason(self):
        # Codex I3 entry 3: an invalid report-only token ("cannot read /tmp").
        self.ko(self.stock, "lib/modules/junk.ko", "cannot read " + self.root_prefix())
        self.required_ko(self.stock, "lib/modules/btusb.ko", "intel/opt.bin")
        self.rule("optional", "btusb", "intel/opt.bin", reason=f"cannot read {self.vendor}/reference.bin")
        proc, report = self.run_check()
        self.assert_report_withheld(proc)  # gb-fwrev5 decision: the reason field holds the root
        self.assertIn("(optional-missing): cannot read vendor/reference.bin", proc.stdout)
        self.assert_no_root_text(proc, report)

    def test_r2_codex_i4_repr_escaped_tokens_never_print_a_root(self):
        # Codex I4: repr() of the token differs from the raw local name.
        for token in (f"intel\\driver+{self.vendor}+suffix/bad.bin",
                      f"intel\ndriver+{self.vendor}+suffix/bad.bin",
                      f"intel'\"driver+{self.vendor}+suffix/bad.bin"):
            with self.subTest(token=token):
                self.required_ko(self.stock, "lib/modules/btusb.ko", token)
                proc, report = self.run_check()
                self.assert_withheld_failure(proc, report)
                # r4 F1: the token is masked before repr, so its D1 line prints the
                # masked repr instead of being withheld.
                masked = token.replace(str(self.vendor), "vendor")
                self.assertIn(f"{D1}: {masked!r}", proc.stderr)

    def test_r2_claude_i2_s2_slash_token_cannot_disable_the_scrub(self):
        # Claude I-2 S2: a report-only "firmware=/" made a PASS print the host path.
        self.required_ko(self.stock, "lib/modules/iwlwifi.ko", "wifi.bin")
        self.rule("optional", "iwlwifi", "wifi.bin", reason=f"see {self.vendor}/reference.txt")
        self.ko(self.stock, "lib/modules/junk.ko", "/")
        proc, report = self.run_check()
        self.assert_report_withheld(proc)  # gb-fwrev5 decision: the reason field holds the root
        self.assertIn("(optional-missing): see vendor/reference.txt", proc.stdout)
        self.assert_no_root_text(proc, report)

    def test_r2_claude_i2_s3_token_cannot_override_the_d1_mask(self):
        # Claude I-2 S3: " intel+" started a keep match one character before the D1 name.
        name = f"intel+{self.vendor}+fw/a.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        self.ko(self.stock, "lib/modules/junk.ko", " intel+")
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"{D1}: 'intel+vendor+fw/a.bin'", proc.stderr)
        self.assert_no_root_text(proc, report)

    def test_r2_claude_i3_backslash_and_tab_tokens_never_print_a_root(self):
        for token in (f"x+{self.vendor}+\\y.bin", f"x+{self.vendor}+\ty.bin"):
            with self.subTest(token=token):
                self.required_ko(self.stock, "lib/modules/btusb.ko", token)
                proc, report = self.run_check()
                self.assert_withheld_failure(proc, report)
                # r4 F1: masked before repr, so the masked repr prints (no root to withhold).
                self.assertIn(f"{D1}: {token.replace(str(self.vendor), 'vendor')!r}", proc.stderr)

    def test_r2_claude_m1_d1_line_cannot_inject_a_log_line(self):
        token = f"x[{self.vendor}]\nFAKE firmware check: PASS"
        self.required_ko(self.stock, "lib/modules/btusb.ko", token)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"{D1}: 'x[vendor]\\nFAKE firmware check: PASS'", proc.stderr)
        for line in (proc.stdout + proc.stderr).splitlines():
            self.assertFalse(line.startswith("FAKE"), line)
        self.assert_no_root_text(proc, report)

    def test_r2_codex_m2_optional_line_masks_a_local_name(self):
        # Codex M2 (1): stdout must mask a local name that the boundary regex misses.
        name = f"intel+{self.vendor}+suffix/optional.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        self.rule("optional", "btusb", name)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("module btusb: intel+vendor+suffix/optional.bin (optional-missing)", proc.stdout)
        self.assertNotIn(WITHHELD, proc.stdout + proc.stderr)
        self.assert_no_root_text(proc, report)

    def test_r2_codex_m2_nested_local_names_mask_longest_first(self):
        # Codex M2 (2): a short local name that is a prefix of a longer one.
        short = f"intel+{self.vendor}"
        long = f"intel+{self.vendor}+{self.vendor}+suffix/optional.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", short, long)
        self.rule("optional", "btusb", long)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("module btusb: intel+vendor+vendor+suffix/optional.bin (optional-missing)", proc.stdout)
        self.assertNotIn(WITHHELD, proc.stdout + proc.stderr)
        self.assert_no_root_text(proc, report)

    def test_r2_claude_m4_repo_root_in_a_name_masks_as_repo(self):
        name = f"x+{ROOT}+y.bin"
        self.required_ko(self.stock, "lib/modules/btusb.ko", name)
        proc, report = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"{D1}: 'x+<repo>+y.bin'", proc.stderr)
        self.assert_no_root_text(proc, report, str(ROOT))

    def test_output_guard_withholds_an_unbounded_root_and_fails(self):
        # F2: a reason echoes the vendor root between "+": no path boundary, so the
        # scrub leaves it; the guard withholds the line and the run fails.
        self.required_ko(self.stock, "lib/modules/btusb.ko", "intel/opt.bin")
        self.rule("optional", "btusb", "intel/opt.bin", reason=f"see x+{self.vendor}+y")
        proc, report = self.run_check()
        self.assert_withheld_failure(proc, report)
        self.assertIn(WITHHELD, proc.stdout)
        self.assertIn("a diagnostic contained a local build path and was withheld", proc.stderr)
        self.assertIn("firmware check: FAIL", proc.stdout)

    def test_output_guard_covers_the_resolved_spelling(self):
        # F2: the caller names the trees through a symlink; the reason echoes the
        # resolved vendor root.
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.required_ko(self.stock, "lib/modules/btusb.ko", "intel/opt.bin")
        self.rule("optional", "btusb", "intel/opt.bin", reason=f"see x+{self.vendor.resolve()}+y")
        self.report.unlink(missing_ok=True)
        proc = subprocess.run([
            sys.executable, "-B", str(CHECKER), "--profile", str(alias / self.profile.name),
            "--vendor", str(alias / self.vendor.name), "--base-vendor", str(alias / self.base_vendor.name),
            "--module-tree", str(alias / self.stock.name), "--module-tree", str(alias / self.system.name),
            "--initrd", str(alias / self.initrd.name), "--report", str(alias / self.report.name),
        ], text=True, capture_output=True)
        report = json.loads(self.report.read_text())
        self.assert_withheld_failure(proc, report)
        self.assertIn(WITHHELD, proc.stdout)

    def test_output_guard_covers_the_repr_spelling(self):
        # F2: a vendor root with a backslash; repr() of an invalid token doubles it, so
        # only the repr-escaped form of the root appears in the invalid-name line.
        vendor = self.root / "vendor\\tree"
        shutil.copytree(self.vendor, vendor, symlinks=True)
        self.required_ko(self.stock, "lib/modules/btusb.ko", f"intel+{vendor}+\\bad.bin")
        proc, report = self.run_check(vendor=vendor)
        self.assert_withheld_failure(proc, report)
        # r4 F1: the token is masked before repr; the guard's own repr-spelling match
        # is covered by test_r4_f1_guard_matches_with_backslashes_removed.
        self.assertIn("invalid firmware name 'intel+vendor+\\\\bad.bin'", proc.stderr)
        check = load_checker()
        guard = check.OutputGuard([str(vendor), repr(str(vendor))[1:-1]])
        self.assertEqual(guard(f"x {repr(str(vendor))[1:-1]} y"), WITHHELD)

    def test_logical_name_resolves_dot_dot(self):
        # Claude m-5, DeepSeek 2: ".." and "a/.." name the directory they resolve to.
        check = load_checker()
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        (self.base_vendor / "sub").mkdir()
        os.chdir(self.base_vendor / "sub")
        self.assertEqual(check.logical_name(Path("..")), "vendor-tree-base")
        self.assertEqual(check.logical_name(Path("x/..")), "sub")
        self.assertEqual(check.logical_name(Path(".")), "sub")
        self.assertEqual(check.logical_name(Path("a/b.ko")), "b.ko")

    def test_logical_roots_module_tree_dot_has_its_own_name(self):
        # DeepSeek 5: the module-tree half alone (base vendor elsewhere).
        check = load_checker()
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.stock)
        args = types.SimpleNamespace(
            profile=self.profile, vendor=self.vendor, module_tree=[Path(".")],
            initrd=self.initrd, report=self.report, base_vendor=self.base_vendor)
        roots = dict(check.logical_roots(args))
        self.assertEqual(roots[str(self.stock.resolve())], "vendor_dlkm")

    def test_logical_roots_base_vendor_dot_dot_has_its_own_name(self):
        check = load_checker()
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        (self.base_vendor / "sub").mkdir()
        os.chdir(self.base_vendor / "sub")
        args = types.SimpleNamespace(
            profile=self.profile, vendor=self.vendor, module_tree=[self.stock],
            initrd=self.initrd, report=self.report, base_vendor=Path(".."))
        roots = dict(check.logical_roots(args))
        self.assertEqual(roots[str(self.base_vendor.resolve())], "vendor-tree-base")

    def test_module_tree_dot_source_label_codex_m5(self):
        # Codex M5: "--module-tree ." from inside firmware-vendor_dlkm labels its sources
        # vendor_dlkm:..., never ":lib/modules/...".
        self.required_ko(self.stock, "lib/modules/btusb.ko", "intel/missing.bin")
        self.report.unlink(missing_ok=True)
        proc = subprocess.run([
            sys.executable, "-B", str(CHECKER), "--profile", str(self.profile),
            "--vendor", str(self.vendor), "--base-vendor", str(self.base_vendor),
            "--module-tree", ".", "--module-tree", str(self.system),
            "--initrd", str(self.initrd), "--report", str(self.report),
        ], text=True, capture_output=True, cwd=self.stock,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        report = json.loads(self.report.read_text())
        entry = next(m for m in report["modules"] if m["module"] == "btusb")
        self.assertEqual(entry["source"], "vendor_dlkm:lib/modules/btusb.ko")
        self.assertIn("module btusb (vendor_dlkm:lib/modules/btusb.ko): missing required firmware", proc.stderr)

    def test_profile_dot_dot_names_the_logical_profile_codex_m5(self):
        (self.profile / "vendor.py").unlink()
        (self.profile / "sub").mkdir()
        self.report.unlink(missing_ok=True)
        proc = subprocess.run([
            sys.executable, "-B", str(CHECKER), "--profile", "..",
            "--vendor", str(self.vendor), "--base-vendor", str(self.base_vendor),
            "--module-tree", str(self.stock), "--module-tree", str(self.system),
            "--initrd", str(self.initrd), "--report", str(self.report),
        ], text=True, capture_output=True, cwd=self.profile / "sub",
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        raw = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, raw)
        self.assertEqual(json.loads(self.report.read_text())["profile"], "generic-x86")
        self.assertIn("FAIL profile generic-x86: firmware check failed", proc.stderr)
        self.assertNotIn("profile ..:", raw)
        self.assert_no_host_paths(raw)

    def test_scrub_right_boundary_dot_claude_m7(self):
        check = load_checker()
        mapping = [("/build/vendor", "vendor")]
        self.assertEqual(check.scrub_paths("scan /build/vendor...", mapping), "scan vendor...")
        self.assertEqual(check.scrub_paths("in /build/vendor.」", mapping), "in vendor.」")
        self.assertEqual(check.scrub_paths("see /x/repo...", [("/x/repo", "")]), "see ...")
        for sibling in ("cannot read /build/vendor.old/a.ko", "cannot read /build/vendor_1/a.ko",
                        "cannot read /build/vendor.9/a.ko"):
            self.assertEqual(check.scrub_paths(sibling, mapping), sibling)
        self.assertEqual(check.scrub_paths("cannot read /x/repo.bak/p", [("/x/repo", "")]),
                         "cannot read /x/repo.bak/p")

    def test_logical_roots_exclude_roots_without_a_name(self):
        # "/", "//" and "///" are absolute but name nothing: as a root they would rewrite
        # every path in a message, so none of them may enter the mapping.
        check = load_checker()
        args = types.SimpleNamespace(
            profile=Path("//"), vendor=Path("/"), module_tree=[Path("///"), self.stock],
            initrd=self.initrd, report=self.report, base_vendor=Path("//"))
        raws = [raw for raw, _ in check.logical_roots(args)]
        for nameless in ("", "/", "//", "///"):
            self.assertNotIn(nameless, raws)
        self.assertIn(str(self.stock), raws)

    def test_scrub_keeps_dotted_sibling_trees_but_maps_a_sentence_end(self):
        check = load_checker()
        mapping = [("/build/vendor", "vendor")]
        # A "." that continues a name is a different tree, not a path boundary.
        for sibling in ("cannot read /build/vendor.old/a.ko", "cannot read /build/vendor.d/a.ko"):
            self.assertEqual(check.scrub_paths(sibling, mapping), sibling)
        self.assertEqual(check.scrub_paths("cannot read /x/repo.bak/profiles/_safety.toml", [("/x/repo", "")]),
                         "cannot read /x/repo.bak/profiles/_safety.toml")
        # A sentence-ending "." after the root still maps.
        self.assertEqual(check.scrub_paths("not a file in /build/vendor.", mapping), "not a file in vendor.")
        self.assertEqual(check.scrub_paths("in /build/vendor. Next", mapping), "in vendor. Next")

    def test_relative_dot_arguments_keep_their_logical_names(self):
        # "--profile ." / "--base-vendor ." have an empty Path.name: the logical name is
        # the directory "." resolves to, never "." or the empty repo-root mapping.
        check = load_checker()
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.base_vendor)
        args = types.SimpleNamespace(
            profile=self.profile, vendor=self.vendor, module_tree=[Path(".")],
            initrd=self.initrd, report=self.report, base_vendor=Path("."))
        roots = dict(check.logical_roots(args))
        self.assertEqual(roots[str(self.base_vendor.resolve())], "vendor-tree-base")
        os.chdir(here)
        (self.profile / "vendor.py").unlink()
        self.report.unlink(missing_ok=True)
        proc = subprocess.run([
            sys.executable, "-B", str(CHECKER), "--profile", ".",
            "--vendor", str(self.vendor), "--base-vendor", str(self.base_vendor),
            "--module-tree", str(self.stock), "--module-tree", str(self.system),
            "--initrd", str(self.initrd), "--report", str(self.report),
        ], text=True, capture_output=True, cwd=self.profile,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        raw = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, raw)
        self.assertEqual(json.loads(self.report.read_text())["profile"], "generic-x86")
        self.assertIn("FAIL profile generic-x86: firmware check failed", proc.stderr)
        self.assertNotIn("profile .:", raw)
        self.assert_no_host_paths(raw)

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

    def pin(self, path, requested_by=None, sha256="a" * 64):
        toml_path = self.profile / "profile.toml"
        text = toml_path.read_text()
        text = text.replace('linux_firmware_tag = ""', 'linux_firmware_tag = "20260810"')
        entry = (
            f'\n[[firmware]]\n'
            f'path = {json.dumps(path)}\n'
            f'from = "linux-firmware"\n'
            f'src = {json.dumps(path)}\n'
            f'sha256 = {json.dumps(sha256)}\n'
        )
        if requested_by is not None:
            entry += f'requested_by = {json.dumps(requested_by)}\n'
        toml_path.write_text(text + entry)
        self.fw(path)

    def setup_real_profile_fixture(self, pid):
        for path in (self.vendor, self.base_vendor, self.stock, self.system):
            if path.exists():
                shutil.rmtree(path)
            path.mkdir(parents=True)
        self.firmware = self.vendor / "firmware"
        self.firmware.mkdir(parents=True)
        self.profile = self.root / pid
        if self.profile.exists():
            shutil.rmtree(self.profile)
        self.profile.mkdir(parents=True)
        src_dir = ROOT / "profiles" / pid
        for item in src_dir.iterdir():
            if item.is_file():
                shutil.copyfile(item, self.profile / item.name)
        prof = devprofile.load(self.profile)
        for fw in prof["firmware"]:
            self.fw(fw["path"])
        self.initrd.write_bytes(archive([]))
        if pid == "generic-x86":
            self.ko(self.vendor, "lib/kano/gbos_efifb.ko")
        elif pid == "kano":
            self.ko(self.vendor, "lib/kano/ac.ko")
            self.ko(self.vendor, "lib/kano/gbos_wakefilter.ko")
            self.ko(self.stock, "lib/modules/iwlmvm.ko")
            self.ko(self.stock, "lib/modules/pci-stub.ko")
            self.ko(self.stock, "lib/modules/xe.ko",
                    "i915/adlp_dmc.bin", "i915/adlp_guc_70.bin", "i915/tgl_huc.bin")
            self.ko(self.stock, "lib/modules/iwlwifi.ko",
                    "iwlwifi-so-a0-gf-a0-89.ucode", "iwlwifi-so-a0-gf-a0.pnvm")
            self.ko(self.stock, "lib/modules/btintel.ko",
                    "intel/ibt-11-5.sfi", "intel/ibt-11-5.ddc", "intel/ibt-12-16.sfi", "intel/ibt-12-16.ddc")
            self.ko(self.stock, "lib/modules/snd-sof-pci-intel-tgl.ko")
            (self.stock / "lib/modules/modules.load").write_text(
                "xe.ko\nbtintel.ko\nsnd-sof-pci-intel-tgl.ko\n"
            )
            (self.stock / "lib/modules/modules.dep").write_text(
                "/vendor/lib/modules/iwlmvm.ko: /vendor/lib/modules/iwlwifi.ko\n"
            )
            self.initrd.write_bytes(archive([
                ("lib/modules/xe.ko", module("i915/adlp_dmc.bin", "i915/adlp_guc_70.bin", "i915/tgl_huc.bin")),
                ("lib/modules/pci-stub.ko", module()),
                ("lib/modules/modules.load", b"xe.ko\n"),
            ]))
        elif pid == "amd-hx370":
            self.ko(self.vendor, "lib/64/gbos_efifb.ko")
            self.ko(self.vendor, "lib/dev-modules/bluetooth.ko")
            self.ko(self.stock, "lib/modules/btusb.ko")
            self.ko(self.stock, "lib/modules/snd_hda_codec_alc269.ko")
            self.ko(self.stock, "lib/modules/iwlmvm.ko")
            self.ko(self.stock, "lib/modules/snd_hda_intel.ko")
            self.ko(self.stock, "lib/modules/iwlwifi.ko",
                    "iwlwifi-ty-a0-gf-a0-89.ucode", "iwlwifi-ty-a0-gf-a0.pnvm")
            self.ko(self.stock, "lib/modules/btintel.ko",
                    "intel/ibt-11-5.sfi", "intel/ibt-11-5.ddc")
            # builder/vendor.sh writes this script; it is the loader that names amdgpu (round-4
            # Claude M6: the fixture takes the emitted text, not a hand-written line).
            script = self.vendor / "etc/amd-hx370-graphics.sh"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(vendor_sh_file("etc/amd-hx370-graphics.sh"))
            amdgpu_decls = [f["path"] for f in prof["firmware"] if f["path"].startswith("amdgpu/")]
            self.ko(self.vendor, "lib/dev-modules/amdgpu.ko", "amdgpu/navi10_ce.bin", *amdgpu_decls)
            (self.stock / "lib/modules/modules.load").write_text("btintel.ko\n")
            (self.stock / "lib/modules/modules.dep").write_text(
                "/vendor/lib/modules/iwlmvm.ko: /vendor/lib/modules/iwlwifi.ko\n"
                "/vendor/lib/modules/btusb.ko: /vendor/lib/modules/btintel.ko\n"
            )

    def test_real_profiles_pass_reverse_check_on_measured_declarations(self):
        self.setup_real_profile_fixture("kano")
        proc, kano_rep = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(kano_rep["ok"])
        self.assertEqual(kano_rep["summary"]["pins"],
                         {"total": 11, "declared": 5, "runtime": 6, "conditional": 0, "undeclared": 0})
        self.assertIn("pins: 11, declared: 5, runtime: 6, conditional: 0, advisory: 0", proc.stdout)
        pinned = [line for line in proc.stdout.splitlines() if line.startswith("PINNED firmware:")]
        self.assertEqual(len(pinned), 11)
        self.assertIn("PINNED firmware: profile kano: i915/adlp_dmc.bin: declared by "
                      "vendor_dlkm:lib/modules/xe.ko, initrd:lib/modules/xe.ko", pinned)
        self.assertIn("PINNED firmware: profile kano: iwlwifi-so-a0-gf-a0-89.ucode: declared by "
                      "vendor_dlkm:lib/modules/iwlwifi.ko", pinned)
        self.assertIn("PINNED firmware: profile kano: intel/ibt-0040-0041.sfi: runtime: btintel (Linux v6.18 "
                      "drivers/bluetooth/btintel.c btintel_get_fw_name_tlv() format; the file-end MODULE_FIRMWARE list) formats "
                      "intel/ibt-%04x-%04x.sfi at runtime from AX211 cnvi_top/cnvr_top over HCI; "
                      "MODULE_FIRMWARE only lists ibt-11-5 and ibt-12-16", pinned)
        by_path = {p["path"]: p for p in kano_rep["pins"]}
        for gpu_pin in ("i915/adlp_dmc.bin", "i915/adlp_guc_70.bin", "i915/tgl_huc.bin"):
            self.assertEqual(by_path[gpu_pin]["status"], "declared")
            self.assertEqual(by_path[gpu_pin]["declared_by"],
                             ["vendor_dlkm:lib/modules/xe.ko", "initrd:lib/modules/xe.ko"])
            self.assertEqual(by_path[gpu_pin]["modules"], ["xe"])
            self.assertEqual(by_path[gpu_pin]["kind"], "static")
            self.assertEqual(by_path[gpu_pin]["declarations"], [gpu_pin])
            self.assertIs(by_path[gpu_pin]["glob"], False)
        for wifi_pin in ("iwlwifi-so-a0-gf-a0-89.ucode", "iwlwifi-so-a0-gf-a0.pnvm"):
            self.assertEqual(by_path[wifi_pin]["status"], "declared")
            self.assertEqual(by_path[wifi_pin]["declared_by"], ["vendor_dlkm:lib/modules/iwlwifi.ko"])
            self.assertEqual(by_path[wifi_pin]["modules"], ["iwlwifi"])
        for rt_pin in ("intel/ibt-0040-0041.sfi", "intel/ibt-0040-0041.ddc",
                       "intel/sof/community/sof-adl.ri", "intel/sof-tplg/sof-adl-max98373-nau8825.tplg",
                       "intel/sof/community/sof-rpl.ri", "intel/sof-tplg/sof-rpl-max98373-nau8825.tplg"):
            self.assertEqual(by_path[rt_pin]["status"], "runtime")
            self.assertTrue(by_path[rt_pin]["requested_by"].startswith("runtime: "))

        self.setup_real_profile_fixture("amd-hx370")
        proc, amd_rep = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(amd_rep["ok"])
        self.assertEqual(amd_rep["summary"]["pins"],
                         {"total": 17, "declared": 2, "runtime": 2, "conditional": 13, "undeclared": 0})
        self.assertIn("pins: 17, declared: 2, runtime: 2, conditional: 13, advisory: 0", proc.stdout)
        self.assertIn("PINNED firmware: profile amd-hx370: amdgpu/gc_11_5_0_pfp.bin: conditional, declared by "
                      "loaded_by vendor:lib/dev-modules/amdgpu.ko", proc.stdout.splitlines())
        amd_by_path = {p["path"]: p for p in amd_rep["pins"]}
        for wifi_pin in ("iwlwifi-ty-a0-gf-a0-89.ucode", "iwlwifi-ty-a0-gf-a0.pnvm"):
            self.assertEqual(amd_by_path[wifi_pin]["status"], "declared")
            self.assertEqual(amd_by_path[wifi_pin]["declared_by"], ["vendor_dlkm:lib/modules/iwlwifi.ko"])
        for p in amd_rep["pins"]:
            if p["path"].startswith("amdgpu/"):
                self.assertEqual(p["status"], "conditional")
                self.assertEqual(p["kind"], "loaded_by")
                self.assertEqual(p["loaded_by"], "vendor:lib/dev-modules/amdgpu.ko")
                self.assertEqual(p["declarations"], [p["path"]])
                self.assertNotIn("requested_by", p)
            elif not p["path"].startswith("iwlwifi-"):
                self.assertEqual(p["status"], "runtime")
                self.assertTrue(p["requested_by"].startswith("runtime: "))

        self.setup_real_profile_fixture("generic-x86")
        proc, gx_rep = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(gx_rep["ok"])
        self.assertEqual(gx_rep["pins"], [])
        self.assertEqual(gx_rep["summary"]["pins"],
                         {"total": 0, "declared": 0, "runtime": 0, "conditional": 0, "undeclared": 0})
        self.assertIn("pins: 0, declared: 0, runtime: 0, conditional: 0, advisory: 0", proc.stdout)

    def test_kano_pin_fails_when_declaring_module_removed_or_renamed(self):
        with self.subTest(case="rename_-89_to_-90"):
            self.setup_real_profile_fixture("kano")
            self.ko(self.stock, "lib/modules/iwlwifi.ko",
                    "iwlwifi-so-a0-gf-a0-90.ucode", "iwlwifi-so-a0-gf-a0.pnvm")
            rep = self.advises("kano", "iwlwifi-so-a0-gf-a0-89.ucode", "not declared by any loaded/required module")
            pin = next(p for p in rep["pins"] if p["path"] == "iwlwifi-so-a0-gf-a0-89.ucode")
            self.assertEqual(pin["status"], "undeclared")
            self.assertEqual(pin["declared_by"], [])

        with self.subTest(case="remove_xe_from_closure"):
            self.setup_real_profile_fixture("kano")
            (self.stock / "lib/modules/modules.load").write_text("btintel.ko\nsnd-sof-pci-intel-tgl.ko\n")
            # xe is also on the initrd load list (as on the real image): drop it there too.
            self.initrd.write_bytes(archive([
                ("lib/modules/xe.ko", module("i915/adlp_dmc.bin", "i915/adlp_guc_70.bin", "i915/tgl_huc.bin")),
                ("lib/modules/pci-stub.ko", module()),
                ("lib/modules/modules.load", b"\n"),
            ]))
            rep = self.advises("kano", "i915/adlp_dmc.bin", "declared only by unloaded report-only")
            self.assertEqual(rep["summary"]["pins"]["undeclared"], 3)

    def test_pin_declared_only_by_report_only_module_outside_closure_fails(self):
        self.pin("dead/pin.bin")
        self.ko(self.stock, "lib/modules/unloaded.ko", "dead/pin.bin")
        self.ko(self.system, "lib/modules/sys-unloaded.ko", "dead/pin.bin")
        self.ko(self.vendor, "lib/dev-modules/dev-unloaded.ko", "dead/pin.bin")
        self.initrd.write_bytes(archive([("lib/modules/early-unloaded.ko", module("dead/pin.bin"))]))
        rep = self.advises(
            "generic-x86",
            "dead/pin.bin",
            "declared only by unloaded report-only",
            "vendor_dlkm:lib/modules/unloaded.ko",
        )
        self.assertEqual(rep["pins"][0]["status"], "undeclared")
        self.assertEqual(rep["pins"][0]["declared_by"], [])

    def test_pin_declared_by_module_reached_through_modules_dep_closure_passes(self):
        self.pin("deep/fw-1.bin")
        self.ko(self.stock, "lib/modules/top-mod.ko")
        self.ko(self.stock, "lib/modules/mid-mod.ko")
        self.ko(self.system, "lib/modules/leaf-mod.ko", "deep/fw-*.bin")
        (self.stock / "lib/modules/modules.load").write_text("top-mod.ko\n")
        (self.stock / "lib/modules/modules.dep").write_text(
            "/vendor/lib/modules/top-mod.ko: /vendor/lib/modules/mid-mod.ko\n"
            "/vendor/lib/modules/mid-mod.ko: /system/lib/modules/leaf-mod.ko\n"
        )
        rep = self.passes()
        self.assertEqual(rep["pins"], [{
            "path": "deep/fw-1.bin",
            "status": "declared",
            "kind": "static",
            "declared_by": ["system_dlkm:lib/modules/leaf-mod.ko"],
            "modules": ["leaf_mod"],
            "declarations": ["deep/fw-*.bin"],
            "glob": True,
        }])
        (self.stock / "lib/modules/modules.dep").write_text(
            "/vendor/lib/modules/top-mod.ko: /vendor/lib/modules/mid-mod.ko\n"
        )
        self.advises("deep/fw-1.bin", "declared only by unloaded report-only system_dlkm:lib/modules/leaf-mod.ko")

    def test_requested_by_validation_and_runtime_pin_counting(self):
        base_toml = (self.profile / "profile.toml").read_text()
        for bad in ("", "   ", "runtime:", "runtime:   ", "static: reason", "iwlwifi", "runtime: line1\nline2"):
            with self.subTest(bad=bad):
                (self.profile / "profile.toml").write_text(base_toml)
                self.pin("runtime/only.bin", requested_by=bad)
                with self.assertRaisesRegex(devprofile.ProfileError, "requested_by"):
                    devprofile.load(self.profile)
                self.fails("requested_by")

        (self.profile / "profile.toml").write_text(base_toml)
        self.required_ko(self.stock, "lib/modules/decl.ko", "declared/fw.bin")
        self.pin("declared/fw.bin")
        self.pin("runtime/only.bin", requested_by="runtime: driver formats filename from ACPI at runtime")
        proc, rep = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(rep["summary"]["pins"],
                         {"total": 2, "declared": 1, "runtime": 1, "conditional": 0, "undeclared": 0})
        self.assertIn("pins: 2, declared: 1, runtime: 1, conditional: 0", proc.stdout)
        self.assertEqual(rep["pins"][1]["status"], "runtime")
        self.assertEqual(rep["pins"][1]["requested_by"],
                         "runtime: driver formats filename from ACPI at runtime")

    def test_reverse_check_report_and_errors_contain_no_host_temp_paths(self):
        self.ko(self.stock, "lib/modules/loaded.ko", "declared/ok.bin")
        (self.stock / "lib/modules/modules.load").write_text("loaded.ko\n")
        self.ko(self.system, "lib/modules/unloaded.ko", "undeclared/fail.bin")
        self.pin("declared/ok.bin")
        self.pin("runtime/ok.bin", requested_by="runtime: built from DMI at runtime")
        self.pin("undeclared/fail.bin")
        proc, rep = self.run_check()
        # gb-fwrev6 A1 (spec change): the undeclared pin is an advisory; the run passes.
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(rep["ok"])
        self.assertEqual(len(rep["advisories"]), 1)
        raw = self.report.read_text() + proc.stdout + proc.stderr
        # Whole roots (given, resolved, random leaf), never each path segment (round-1 minor 7).
        self.assert_no_host_paths(raw)
        self.assertEqual(rep["pins"][0]["declared_by"], ["vendor_dlkm:lib/modules/loaded.ko"])

    def test_pin_names_and_module_labels_pass_the_scrub_unchanged(self):
        # gb-fwrev3 item 1: a pin path, its module labels (declared_by, loaded_by, the
        # unloaded declaring modules, the module names) and its matched declarations are
        # collected like declared names (v023-r3: local_names, no keep). Without a root's
        # text they read the same in a diagnostic as in the data, while a local root in the
        # same error line is still mapped; with a root's text they fail with D1, masked.
        check = load_checker()
        mapping = [(str(self.vendor), "vendor")]
        name = "fw[vendor-x]/pin.v2.bin"
        label = "vendor_dlkm:lib/modules/a[vendor-x]/m.ko"
        pin = {"path": name, "declared_by": [], "modules": ["m"], "declarations": [name], "loaded_by": ""}
        self.assertEqual(check.pin_texts(pin, [label]), {name, "m", label})
        local = check.local_names(check.pin_texts(pin, [label]), mapping)
        self.assertEqual(local, [])
        error = (f"profile generic-x86: {name}: not declared by any loaded/required module "
                 f"(declared only by unloaded report-only {label}; tree {self.vendor}/lib/modules/x.ko)")
        self.assertEqual(check.scrub_paths(error, mapping, local),
                         f"profile generic-x86: {name}: not declared by any loaded/required module "
                         f"(declared only by unloaded report-only {label}; tree vendor/lib/modules/x.ko)")
        local_label = f"vendor_dlkm:lib/modules/a[{self.vendor}]/m.ko"
        local = check.local_names(check.pin_texts(pin, [local_label]), mapping)
        self.assertEqual(local, [local_label])
        # End to end, unchanged: the "declared only by unloaded" error prints the label as is.
        self.pin("dead/pin.v1.bin")
        self.ko(self.stock, "lib/modules/a[vendor-x]/m.ko", "dead/pin.v1.bin")
        rep = self.advises("dead/pin.v1.bin: not declared by any loaded/required module "
                         "(declared only by unloaded report-only vendor_dlkm:lib/modules/a[vendor-x]/m.ko)")
        self.assertEqual(rep["pins"][0]["status"], "undeclared")
        self.assertFalse(any(D1 in e for e in rep["errors"]), rep["errors"])

    def test_pin_path_or_module_label_with_a_host_root_fails_with_d1(self):
        # gb-fwrev3 item 1: an unloaded declaring module label holding a root's text.
        self.pin("dead/pin.v1.bin")
        self.ko(self.stock, f"lib/modules/a[{self.vendor}]/m.ko", "dead/pin.v1.bin")
        proc, rep = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"{D1}: 'vendor_dlkm:lib/modules/a[vendor]/m.ko'", proc.stderr)
        # gb-fwrev6 A1: the refusal is now an ADVISORY stdout line; D1 still fails the run.
        self.assertIn("(declared only by unloaded report-only vendor_dlkm:lib/modules/a[vendor]/m.ko)", proc.stdout)
        self.assertIn(ADVISORY, proc.stdout)
        self.assert_no_host_paths(proc.stdout + proc.stderr + json.dumps(rep["errors"]))
        # A pin path holding a root's text (a plain relative path may contain one).
        self.setUp()
        self.pin(f"fw{self.vendor}/pin.bin")
        self.ko(self.stock, "lib/modules/btusb.ko", f"fw{self.vendor}/pin.bin")
        self.required_ko(self.system, "lib/modules/snd-sof.ko")
        proc, rep = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"{D1}: 'fwvendor/pin.bin'", proc.stderr)
        self.assert_no_host_paths(proc.stdout + proc.stderr + json.dumps(rep["errors"]))
        # A loaded_by label holding a root's text.
        self.setUp()
        self.pin("amdgpu/x.bin")
        toml_path = self.profile / "profile.toml"
        toml_path.write_text(toml_path.read_text() + f'loaded_by = "vendor:lib/x{self.vendor}/amdgpu.ko"\n')
        proc, rep = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"{D1}: 'vendor:lib/xvendor/amdgpu.ko'", proc.stderr)
        # gb-fwrev6 A1: the refusal is an ADVISORY stdout line now; D1 still fails the run.
        self.assertIn(ADVISORY + "profile generic-x86: amdgpu/x.bin: loaded_by module vendor:lib/xvendor/amdgpu.ko "
                      "is not in the image", proc.stdout)
        self.assert_no_host_paths(proc.stdout + proc.stderr + json.dumps(rep["errors"]))

    def assert_fails_with_advisory(self, proc, report, *needles):
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIs(report.get("ok"), False, report)
        self.assertTrue(report.get("advisories"), report)
        self.assertIn(ADVISORY, proc.stdout)
        self.assertIn("firmware check: FAIL;", proc.stdout)
        for needle in needles:
            self.assertIn(needle, proc.stderr)

    def test_a4_forward_failure_still_fails_with_the_reverse_check_present(self):
        # gb-fwrev6 A4: an advisory never masks the forward check.
        self.required_ko(self.system, "lib/modules/snd-sof.ko", "intel/missing.bin")
        self.pin("dead/pin.bin")
        proc, report = self.run_check()
        self.assert_fails_with_advisory(proc, report, "FAIL ", "missing required firmware intel/missing.bin")
        self.assertIn("dead/pin.bin: not declared by any loaded/required module", report["advisories"][0])

    def test_a4_d1_name_still_fails_with_the_reverse_check_present(self):
        # gb-fwrev6 A4: a pin name holding a root's text is D1 (an error), not an advisory.
        self.required_ko(self.system, "lib/modules/snd-sof.ko")
        self.pin("dead/pin.bin")
        self.pin(f"fw{self.vendor}/pin.bin")
        proc, report = self.run_check()
        self.assert_fails_with_advisory(proc, report, f"{D1}: 'fwvendor/pin.bin'")
        self.assert_no_host_paths(proc.stdout + proc.stderr + self.report.read_text())

    def test_a4_guard_hit_still_fails_with_the_reverse_check_present(self):
        # gb-fwrev6 A4: an output-guard hit fails the run even when the rest is advisory.
        self.required_ko(self.system, "lib/modules/snd-sof.ko")
        self.pin("dead/pin.bin")
        self.pin("intel/rt.bin", requested_by=f"runtime: see x+{self.vendor}+y")
        proc, report = self.run_check()
        self.assert_fails_with_advisory(proc, report, "a diagnostic contained a local build path and was withheld")
        self.assert_no_host_paths(proc.stdout + proc.stderr + self.report.read_text())

    def test_a4_advisory_line_passes_the_output_guard(self):
        # gb-fwrev6 A1: the ADVISORY line goes through scrub and guard like an error line.
        # The unloaded declaring module's label holds the vendor root: D1 masks it, and the
        # printed advisory line and the report's advisories show only the logical name.
        self.required_ko(self.system, "lib/modules/snd-sof.ko")
        self.pin("dead/pin.v1.bin")
        self.ko(self.stock, f"lib/modules/a[{self.vendor}]/m.ko", "dead/pin.v1.bin")
        proc, report = self.run_check()
        self.assert_fails_with_advisory(proc, report, D1)
        self.assertIn(ADVISORY + "profile generic-x86: dead/pin.v1.bin: not declared by any loaded/required module "
                      "(declared only by unloaded report-only vendor_dlkm:lib/modules/a[vendor]/m.ko)", proc.stdout)
        self.assert_no_host_paths(proc.stdout + json.dumps(report["advisories"]))

    def test_a1_reverse_check_never_changes_the_exit_status(self):
        # gb-fwrev6 A1: every kind of refusal at once still exits 0 with a PASS line.
        self.setup_real_profile_fixture("amd-hx370")
        self.loader_script()  # the amdgpu loader is gone: 13 loaded_by advisories
        for script in (self.vendor / "etc").rglob("*.sh"):
            if script.name != "fixture-loader.sh":
                script.write_text("#!/system/bin/sh\n")
        report = self.advises("is not named by any modprobe line the builder emits")
        self.assertEqual(len(report["advisories"]), 13)
        self.assertEqual(report["summary"]["pins"]["undeclared"], 13)
        self.assertTrue(all(p["status"] in ("declared", "runtime", "undeclared") for p in report["pins"]))

    def test_a2_loader_modprobe_ignores_comments_and_quoted_text(self):
        # gb-fwrev6 A2 (round-3 Codex I1, Claude M3): only a modprobe command names a module.
        check = load_checker()
        text = ("#!/system/bin/sh\n"
                '# "$MODPROBE" --dirname=/vendor/lib/dev-modules commented\n'
                '   # modprobe --dirname=/vendor/lib/dev-modules indented\n'
                'log "native entry: modprobe --dirname=/vendor/lib/dev-modules logged"\n'
                "echo 'modprobe --dirname=/vendor/lib/dev-modules echoed'\n"
                'true # "$MODPROBE" --dirname=/vendor/lib/dev-modules trailing\n'
                'if x; then "$MODPROBE" --dirname=/vendor/lib/dev-modules real1; fi\n'
                '"$MODPROBE" --dirname=/vendor/lib/dev-modules real2 && log "modprobe real2 ok"\n'
                "    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe "
                "--dirname=/vendor/lib/kano real3\n"
                '"${MODPROBE}" --dirname=/vendor/lib/modules real4  # comment\n')
        self.assertEqual(check.loader_modprobes(text),
                         [("/vendor/lib/dev-modules", "real1"), ("/vendor/lib/dev-modules", "real2"),
                          ("/vendor/lib/kano", "real3"), ("/vendor/lib/modules", "real4")])

    def test_a2_commented_loader_line_does_not_satisfy_loaded_by(self):
        # Round-3 Codex I1 reproduction: the real amd-hx370 profile, loader line commented out.
        self.setup_real_profile_fixture("amd-hx370")
        self.assertEqual(self.passes()["summary"]["pins"]["conditional"], 13)
        for script in (self.vendor / "etc").rglob("*.sh"):
            script.write_text("#!/system/bin/sh\n"
                              '# "$MODPROBE" --dirname=/vendor/lib/dev-modules amdgpu\n'
                              'log "native entry: modprobe --dirname=/vendor/lib/dev-modules amdgpu"\n')
        report = self.advises("amdgpu/gc_11_5_0_pfp.bin: loaded_by module vendor:lib/dev-modules/amdgpu.ko "
                              "is not named by any modprobe line the builder emits")
        self.assertEqual(report["summary"]["pins"]["conditional"], 0)

    def test_q2_passes_helper_rejects_a_run_with_advisories(self):
        # Round-4 Claude M2: an rc-0 run that still carries an advisory is not a positive control.
        self.pin("dead/pin.bin")
        self.required_ko(self.system, "lib/modules/snd-sof.ko")
        self.advises("dead/pin.bin")
        with self.assertRaises(AssertionError):
            self.passes()

    def test_q5_quoted_separator_never_creates_a_command(self):
        # Round-4 Codex M1: a quoted or escaped ; & | && || is an argument, not a separator.
        check = load_checker()
        for sep in ('";"', "'&'", '"|"', '"&&"', "'||'", "\\;"):
            with self.subTest(sep=sep):
                line = f'echo {sep} "$MODPROBE" --dirname=/vendor/lib/dev-modules m\n'
                self.assertEqual(check.loader_modprobes(line), [])
        self.assertEqual(check.loader_modprobes('echo ";"; "$MODPROBE" --dirname=/vendor/lib/dev-modules m\n'),
                         [("/vendor/lib/dev-modules", "m")])
        # Round-4 Claude M3: "#" inside a word is not a comment.
        self.assertEqual(check.loader_modprobes('x=a#b; "$MODPROBE" --dirname=/vendor/lib/dev-modules m\n'),
                         [("/vendor/lib/dev-modules", "m")])
        # End to end: only an echo runs, so the loaded_by target is named by nothing.
        self.loaded_by_pin("cond/fw.bin", "vendor:lib/dev-modules/m.ko")
        self.ko(self.vendor, "lib/dev-modules/m.ko", "cond/fw.bin")
        script = self.vendor / "etc/fixture-loader.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text('#!/system/bin/sh\necho ";" "$MODPROBE" --dirname=/vendor/lib/dev-modules m\n')
        report = self.advises("loaded_by module vendor:lib/dev-modules/m.ko is not named by any modprobe line")
        self.assertEqual(report["pins"][0]["status"], "undeclared")

    def test_q5_assignment_prefix_still_counts_as_a_modprobe_command(self):
        # Round-4 Codex M2 / Claude M3: NAME=value words and an env prefix precede the command word.
        check = load_checker()
        text = ('FOO=bar "$MODPROBE" --dirname=/vendor/lib/dev-modules a1\n'
                'A=1 B="x y" /vendor/bin/modprobe --dirname=/vendor/lib/kano a2\n'
                'env FOO=1 "$MODPROBE" --dirname=/vendor/lib/dev-modules a3\n'
                'if true; then LC_ALL=C "$MODPROBE" --dirname=/vendor/lib/modules a4; fi\n'
                '"FOO=1" "$MODPROBE" --dirname=/vendor/lib/dev-modules quoted_assignment_is_a_command\n'
                'FOO=1 echo "$MODPROBE" --dirname=/vendor/lib/dev-modules echoed\n')
        self.assertEqual(check.loader_modprobes(text),
                         [("/vendor/lib/dev-modules", "a1"), ("/vendor/lib/kano", "a2"),
                          ("/vendor/lib/dev-modules", "a3"), ("/vendor/lib/modules", "a4")])
        # End to end: the assignment-prefixed modprobe names the loaded_by target.
        self.loaded_by_pin("cond/fw.bin", "vendor:lib/dev-modules/m.ko")
        self.ko(self.vendor, "lib/dev-modules/m.ko", "cond/fw.bin")
        script = self.vendor / "etc/fixture-loader.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text('#!/system/bin/sh\nenv FOO=1 "$MODPROBE" --dirname=/vendor/lib/dev-modules m\n')
        self.assertEqual(self.passes()["pins"][0]["status"], "conditional")

    def test_h3_env_options_and_quoted_env_still_reach_the_modprobe(self):
        # Round-8 Claude m1: env's options and NAME=value words precede the command it runs; a
        # quoted "env" is still env.
        check = load_checker()
        d = "--dirname=/vendor/lib/dev-modules"
        text = (f'env -i FOO=1 "$MODPROBE" {d} e1\n'
                f'"env" FOO=1 "$MODPROBE" {d} e2\n'
                f'env -u NAME -i "BAR=x y" /vendor/bin/modprobe {d} e3\n'
                f'env --unset=NAME -- "$MODPROBE" {d} e4\n'
                f"env -S '\"$MODPROBE\" {d} e5'\n"
                f'env -C /tmp -0 A=1 "$MODPROBE" {d} e6\n'
                f'env -u MODPROBE echo "$MODPROBE" {d} not_run\n'
                f'env -i FOO=1 echo modprobe {d} echoed\n')
        self.assertEqual(check.loader_modprobes(text),
                         [("/vendor/lib/dev-modules", f"e{i}") for i in range(1, 7)])
        # End to end: env -i before the modprobe names the loaded_by target.
        self.loaded_by_pin("cond/fw.bin", "vendor:lib/dev-modules/m.ko")
        self.ko(self.vendor, "lib/dev-modules/m.ko", "cond/fw.bin")
        script = self.vendor / "etc/fixture-loader.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f'#!/system/bin/sh\n"env" -i FOO=1 "$MODPROBE" {d} m\n')
        self.assertEqual(self.passes()["pins"][0]["status"], "conditional")

    def test_h4_escaped_quote_inside_double_quotes_and_quoted_keyword(self):
        # Round-8 Claude m2 (FM3): \" inside double quotes does not end the string, so the
        # ";" after it is text; (FM4) a quoted "if" is a command named if, not a keyword.
        check = load_checker()
        d = "--dirname=/vendor/lib/dev-modules"
        self.assertEqual(check.loader_modprobes(f'echo "a\\"b;" "$MODPROBE" {d} m\n'), [])
        self.assertEqual(check.loader_modprobes(f'echo "a\\" ; $MODPROBE {d} m ; echo "z\\"\n'), [])
        self.assertEqual(check.loader_modprobes(f'echo "a\\"" ; $MODPROBE {d} m\n'),
                         [("/vendor/lib/dev-modules", "m")])
        self.assertEqual(check.loader_modprobes(f'"if" "$MODPROBE" {d} m\n'), [])
        self.assertEqual(check.loader_modprobes(f"'then' \"$MODPROBE\" {d} m\n"), [])
        self.assertEqual(check.loader_modprobes(f'if "$MODPROBE" {d} m; then :; fi\n'),
                         [("/vendor/lib/dev-modules", "m")])

    def test_q6_amd_loader_fixture_is_the_script_vendor_sh_emits(self):
        # Round-4 Claude M6: the emitted amd-hx370-graphics.sh names exactly amdgpu from dev-modules,
        # and the real-profile fixture carries that very text.
        check = load_checker()
        text = vendor_sh_file("etc/amd-hx370-graphics.sh")
        self.assertEqual(check.loader_modprobes(text), [("/vendor/lib/dev-modules", "amdgpu")])
        self.setup_real_profile_fixture("amd-hx370")
        self.assertEqual((self.vendor / "etc/amd-hx370-graphics.sh").read_text(), text)
        self.assertEqual(self.passes()["summary"]["pins"]["conditional"], 13)

    def test_q3_shadowing_label_with_a_host_root_fails_with_d1(self):
        # Round-4 Claude M4 (X3b): the shadowing module's label comes from modules.load text; one
        # holding a root's text is a D1 failure, not a silently scrubbed advisory.
        self.loaded_by_pin("cond/fw.bin", "vendor:lib/dev-modules/m.ko")
        self.ko(self.vendor, "lib/dev-modules/m.ko", "cond/fw.bin")
        self.loader_script("m")
        self.ko(self.stock, f"lib/modules/a={self.vendor}/m.ko", "other.bin")
        (self.stock / "lib/modules/modules.load").write_text(f"a={self.vendor}/m.ko\n")
        proc, rep = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"{D1}: 'vendor_dlkm:lib/modules/a=vendor/m.ko'", proc.stderr)
        self.assert_no_host_paths(proc.stdout + proc.stderr + json.dumps(rep))

    def test_pinned_line_passes_the_output_guard(self):
        # gb-fwrev4: every reverse-check line goes through the output guard. A runtime
        # pin's requested_by echoes the vendor root between "+" (no path boundary, so the
        # scrub leaves it): the PINNED line is withheld and the run fails.
        self.pin("intel/rt.bin", requested_by=f"runtime: see x+{self.vendor}+y")
        self.required_ko(self.system, "lib/modules/snd-sof.ko")
        proc, report = self.run_check()
        self.assert_withheld_failure(proc, report)
        self.assertIn(WITHHELD, proc.stdout)
        self.assertNotIn("PINNED firmware: profile generic-x86: intel/rt.bin", proc.stdout)
        self.assertIn("a diagnostic contained a local build path and was withheld", proc.stderr)
        self.assertIn("firmware check: FAIL", proc.stdout)
        self.assertEqual(report["pins"][0]["status"], "runtime")

    def assert_no_root_anywhere(self, proc, report, *roots):
        # F1: every surface, compared with all backslashes removed on both sides.
        surfaces = {"stdout": proc.stdout, "stderr": proc.stderr,
                    "errors": "\n".join(report.get("errors", [])),
                    "warnings": "\n".join(report.get("warnings", [])),
                    "modules[].error": "\n".join(m.get("error", "") for m in report.get("modules", []))}
        for root in roots:
            bare = str(root).replace("\\", "")
            for name, text in surfaces.items():
                self.assertNotIn(bare, text.replace("\\", ""), f"{name} holds {root!r}")
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_r4_f1_guard_matches_with_backslashes_removed(self):
        check = load_checker()
        guard = check.OutputGuard(["/t/vendor'tree"])
        self.assertEqual(guard("x /t/vendor\\'tree y"), WITHHELD)
        self.assertTrue(guard.tripped)
        guard = check.OutputGuard(["/t/a\\b"])
        self.assertEqual(guard("x /t/a\\\\b y"), WITHHELD)
        self.assertEqual(guard("clean"), "clean")

    def test_r4_f1_codex_quote_root_and_double_quote_token(self):
        # Codex: root vendor'tree, token with '"': repr escapes the ' of the root.
        vendor = self.root / "vendor'tree"
        shutil.copytree(self.vendor, vendor, symlinks=True)
        self.required_ko(self.stock, "lib/modules/btusb.ko", f'intel+{vendor}+"bad.bin')
        self.ko(self.stock, "lib/modules/junk.ko", f'x+{vendor}+"warn.bin')
        self.pin("intel/rt.bin", requested_by=f"runtime: see x+{vendor}+y")
        proc, report = self.run_check(vendor=vendor)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIs(report.get("ok"), False)
        self.assert_no_root_anywhere(proc, report, vendor)
        # The invalid token is masked before repr formats it.
        self.assertIn("invalid firmware name 'intel+vendor+\"bad.bin'", proc.stderr)
        self.assertNotIn("PINNED firmware: profile generic-x86: intel/rt.bin", proc.stdout)

    def test_r4_f1_claude_quote_in_the_temporary_root(self):
        # Claude: a ' in the whole build root (every role root holds it).
        tmp = tempfile.TemporaryDirectory(prefix="gbos-'q-")
        self.addCleanup(tmp.cleanup)
        top = Path(tmp.name)
        vendor = top / "vendor-tree-new"
        shutil.copytree(self.vendor, vendor, symlinks=True)
        for token in (f'x+{vendor}+"y.bin', f"x+{vendor}+\\y.bin"):
            with self.subTest(token=token):
                self.required_ko(self.stock, "lib/modules/btusb.ko", token)
                proc, report = self.run_check(vendor=vendor)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assert_no_root_anywhere(proc, report, vendor)

    def test_r4_f2_codex_negative_newc_size_is_a_format_error(self):
        for field in (b"-fffffff", b"0000zzzz", b"+0000001"):
            with self.subTest(field=field):
                data = bytearray(archive([("lib/modules/early.ko", module("early.bin"))]))
                data[54:62] = field
                self.initrd.write_bytes(bytes(data))
                proc, report = self.run_check()
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stdout + proc.stderr)
                self.assertIs(report.get("ok"), False)
                self.assertIn("invalid newc header", proc.stderr)

    def forced(self, target):
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('chk', {str(CHECKER)!r})\n"
            "m = importlib.util.module_from_spec(spec); sys.modules['chk'] = m; spec.loader.exec_module(m)\n"
            "def boom(*a, **k):\n"
            f"    raise RuntimeError({str(self.vendor) + chr(39)!r})\n"
            f"setattr(m, {target!r}, boom)\n"
            "sys.argv = ['check-firmware.py'] + sys.argv[1:]\n"
            "sys.exit(m.main())\n")
        self.report.unlink(missing_ok=True)
        proc = subprocess.run([
            sys.executable, "-B", "-c", code, "--profile", str(self.profile),
            "--vendor", str(self.vendor), "--base-vendor", str(self.base_vendor),
            "--module-tree", str(self.stock), "--module-tree", str(self.system),
            "--initrd", str(self.initrd), "--report", str(self.report),
        ], text=True, capture_output=True)
        report = json.loads(self.report.read_text()) if self.report.exists() else {}
        return proc, report

    def test_r4_f2_forced_exception_is_one_fixed_guarded_line(self):
        for target in ("check", "scrub_paths", "logical_roots"):
            with self.subTest(target=target):
                proc, report = self.forced(target)
                self.assertNotEqual(proc.returncode, 0)
                self.assertNotIn("Traceback", proc.stdout + proc.stderr)
                self.assertEqual(proc.stderr.splitlines(),
                                 ["FAIL profile generic-x86: firmware check failed: internal error (RuntimeError)"])
                self.assertEqual(report.get("ok"), False)
                self.assertEqual(report.get("errors"),
                                 ["profile generic-x86: firmware check failed: internal error (RuntimeError)"])
                self.assert_no_root_anywhere(proc, report, self.vendor)

    def initrd_body(self, *archives):
        return b"".join(archive(list(a)) for a in archives)

    def test_r1_dev_modules_copy_does_not_vouch_for_the_loaded_module(self):
        # Claude probe A: the loaded vendor_dlkm iwlwifi requests -90; an unloaded dev copy still declares -89.
        self.setup_real_profile_fixture("kano")
        self.ko(self.stock, "lib/modules/iwlwifi.ko", "iwlwifi-so-a0-gf-a0-90.ucode", "iwlwifi-so-a0-gf-a0.pnvm")
        self.ko(self.vendor, "lib/dev-modules/iwlwifi.ko", "iwlwifi-so-a0-gf-a0-89.ucode", "iwlwifi-so-a0-gf-a0.pnvm")
        rep = self.advises("iwlwifi-so-a0-gf-a0-89.ucode: not declared by any loaded/required module",
                         "vendor:lib/dev-modules/iwlwifi.ko")
        pin = next(p for p in rep["pins"] if p["path"] == "iwlwifi-so-a0-gf-a0-89.ucode")
        self.assertEqual((pin["status"], pin["declared_by"]), ("undeclared", []))

    def test_r1_same_name_module_in_another_tree_is_not_loaded(self):
        # Codex I1: vendor_dlkm dup.ko is loaded; system_dlkm dup.ko (different content) is not.
        self.pin("dead/pin.bin")
        self.ko(self.stock, "lib/modules/dup.ko", "live/other.bin")
        self.ko(self.system, "lib/modules/dup.ko", "dead/pin.bin")
        (self.stock / "lib/modules/modules.load").write_text("dup.ko\n")
        rep = self.advises("dead/pin.bin", "declared only by unloaded report-only system_dlkm:lib/modules/dup.ko")
        self.assertEqual(rep["pins"][0]["declared_by"], [])

    def test_r1_dev_module_counts_only_when_a_dev_row_or_builder_modprobe_names_it(self):
        self.pin("dev/pin.bin")
        self.ko(self.vendor, "lib/dev-modules/devmod.ko", "dev/pin.bin")
        self.advises("dev/pin.bin", "declared only by unloaded report-only vendor:lib/dev-modules/devmod.ko")
        rc = self.vendor / "etc/init/kano-hw.rc"
        rc.parent.mkdir(parents=True)
        rc.write_text("on property:sys.boot_completed=1\n"
                      "    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe "
                      "--dirname=/vendor/lib/dev-modules devmod\n")
        rep = self.passes()
        self.assertEqual(rep["pins"][0]["declared_by"], ["vendor:lib/dev-modules/devmod.ko"])

    def test_r2_stray_loader_files_never_extend_the_closure(self):
        # Codex I2: a modules.load / modules.dep outside the loaders' exact paths is not loader input.
        self.pin("dead/pin.bin")
        self.ko(self.system, "lib/modules/dead.ko", "dead/pin.bin")
        self.ko(self.stock, "lib/modules/top.ko")
        (self.stock / "lib/modules/modules.load").write_text("top.ko\n")
        stray = self.vendor / "etc/unrelated/modules.load"
        stray.parent.mkdir(parents=True)
        stray.write_text("dead.ko\n")
        (self.system / "lib/modules/backup").mkdir(parents=True)
        (self.system / "lib/modules/backup/modules.dep").write_text(
            "/vendor/lib/modules/top.ko: /system/lib/modules/dead.ko\n")
        (self.stock / "modules.load").write_text("dead.ko\n")
        self.initrd.write_bytes(archive([("etc/modules.load", b"dead.ko\n")]))
        rep = self.advises("dead/pin.bin", "declared only by unloaded report-only system_dlkm:lib/modules/dead.ko")
        self.assertEqual(rep["pins"][0]["status"], "undeclared")

    def test_r3_initrd_last_record_for_a_path_wins(self):
        # Codex I3: a later archive overwrites lib/modules/dup.ko; the old declaration never counts.
        self.pin("dead/pin.bin")
        self.initrd.write_bytes(self.initrd_body(
            [("lib/modules/dup.ko", module("dead/pin.bin"))],
            [("lib/modules/dup.ko", module("live/other.bin")), ("lib/modules/modules.load", b"dup.ko\n")]))
        rep = self.advises("dead/pin.bin: not declared by any loaded/required module")
        self.assertEqual(rep["pins"][0]["declared_by"], [])
        self.assertEqual([m["source"] for m in rep["modules"] if m["module"] == "dup"], ["initrd:lib/modules/dup.ko"])
        # The same holds for metadata: an overwritten load list no longer loads dup.ko.
        self.initrd.write_bytes(self.initrd_body(
            [("lib/modules/dup.ko", module("dead/pin.bin")), ("lib/modules/modules.load", b"dup.ko\n")],
            [("lib/modules/modules.load", b"\n")]))
        self.advises("dead/pin.bin", "declared only by unloaded report-only initrd:lib/modules/dup.ko")
        # And the reverse: the final record declares the pin and is loaded.
        self.initrd.write_bytes(self.initrd_body(
            [("lib/modules/dup.ko", module("live/other.bin"))],
            [("lib/modules/dup.ko", module("dead/pin.bin")), ("lib/modules/modules.load", b"dup.ko\n")]))
        self.assertEqual(self.passes()["pins"][0]["declared_by"], ["initrd:lib/modules/dup.ko"])

    def test_r4_invalid_initrd_labels_are_errors_and_never_leak(self):
        # Codex I4: an absolute archive entry naming the real vendor root, and a ".." entry.
        self.pin("dead/pin.bin")
        for name in (f"{self.vendor}/lib/modules/m.ko", "lib/modules/../../m.ko", "/lib/modules/m.ko"):
            with self.subTest(name=name):
                self.initrd.write_bytes(archive([(name, module("dead/pin.bin"))]))
                proc, rep = self.run_check()
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("initrd entry 0 has an invalid path", proc.stderr)
                raw = self.report.read_text() + proc.stdout + proc.stderr
                self.assertNotIn(str(self.vendor), raw)
                self.assertNotIn("m.ko", raw)
        check = load_checker()
        with self.assertRaisesRegex(ValueError, "invalid path"):
            check.initrd_label("/abs/x.ko", 3)
        self.assertEqual(check.initrd_label("./lib//modules/x.ko", 0), "lib/modules/x.ko")

    def test_r5_loaded_by_requires_the_exact_module_to_declare_the_pin(self):
        # Claude probe B: amdgpu's requests renamed; every loaded_by pin must fail.
        self.setup_real_profile_fixture("amd-hx370")
        self.ko(self.vendor, "lib/dev-modules/amdgpu.ko", "amdgpu/gc_11_5_9_pfp.bin", "amdgpu/psp_14_0_9_toc.bin")
        rep = self.advises("amdgpu/gc_11_5_0_pfp.bin: not declared by its loaded_by module "
                         "vendor:lib/dev-modules/amdgpu.ko")
        self.assertEqual(rep["summary"]["pins"]["undeclared"], 13)
        self.assertEqual(rep["summary"]["pins"]["conditional"], 0)
        # The exact file: an amdgpu.ko elsewhere does not stand in for it.
        self.setup_real_profile_fixture("amd-hx370")
        (self.vendor / "lib/dev-modules/amdgpu.ko").unlink()
        decls = [f["path"] for f in devprofile.load(self.profile)["firmware"] if f["path"].startswith("amdgpu/")]
        self.ko(self.stock, "lib/modules/amdgpu.ko", *decls)
        self.advises("amdgpu/gc_11_5_0_pfp.bin: loaded_by module vendor:lib/dev-modules/amdgpu.ko "
                   "is not in the image")

    def test_r5_runtime_pin_fails_when_a_shipped_module_declares_it(self):
        self.setup_real_profile_fixture("kano")
        self.ko(self.vendor, "lib/dev-modules/btintel.ko", "intel/ibt-0040-0041.sfi")
        rep = self.advises('intel/ibt-0040-0041.sfi: has requested_by "runtime: ..." but is statically '
                         'declared by vendor:lib/dev-modules/btintel.ko', "loaded_by")
        self.assertTrue(rep["ok"])  # gb-fwrev6 A1: advisory, the run passes

    def test_r5_loaded_by_schema(self):
        base_toml = (self.profile / "profile.toml").read_text()
        for bad in ("", "amdgpu", "vendor:", "vendor:/lib/a.ko", "vendor:../a.ko", "vendor:lib/../a.ko",
                    "boot:lib/a.ko", "vendor:lib/a.bin", "vendor:lib/a.ko\n"):
            with self.subTest(bad=bad):
                (self.profile / "profile.toml").write_text(base_toml)
                self.pin("cond/fw.bin")
                with (self.profile / "profile.toml").open("a") as f:
                    f.write(f"loaded_by = {json.dumps(bad)}\n")
                with self.assertRaisesRegex(devprofile.ProfileError, "loaded_by"):
                    devprofile.load(self.profile)
        (self.profile / "profile.toml").write_text(base_toml)
        self.pin("cond/fw.bin", requested_by="runtime: both set")
        with (self.profile / "profile.toml").open("a") as f:
            f.write('loaded_by = "vendor:lib/dev-modules/m.ko"\n')
        with self.assertRaisesRegex(devprofile.ProfileError, "mutually exclusive"):
            devprofile.load(self.profile)
        (self.profile / "profile.toml").write_text(base_toml)
        self.pin("cond/fw.bin")
        with (self.profile / "profile.toml").open("a") as f:
            f.write('loaded_by = "vendor:lib/dev-modules/m.ko"\n')
        self.ko(self.vendor, "lib/dev-modules/m.ko", "cond/*.bin")
        self.loader_script("m")  # gb-fwrev5 V6: a loaded_by target must be named by a builder loader
        proc, rep = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(rep["pins"], [{
            "path": "cond/fw.bin", "status": "conditional", "kind": "loaded_by", "declared_by": [], "modules": [],
            "declarations": ["cond/*.bin"], "glob": True, "loaded_by": "vendor:lib/dev-modules/m.ko"}])
        self.assertIn("pins: 1, declared: 0, runtime: 0, conditional: 1", proc.stdout)

    def test_r6_one_line_reasons_refuse_unicode_breaks_and_invisible_text(self):
        base_toml = (self.profile / "profile.toml").read_text()
        for bad in ("runtime: first\u2028second", "runtime: a\x85b", "runtime: a\x7fb", "runtime: \u200b",
                    "runtime: \u200b\u2060 ", "runtime: a\u2029b"):
            with self.subTest(requested_by=bad):
                (self.profile / "profile.toml").write_text(base_toml)
                self.pin("runtime/only.bin", requested_by=bad)
                with self.assertRaisesRegex(devprofile.ProfileError, "requested_by"):
                    devprofile.load(self.profile)
        for bad in ("first\u2028second", "a\x85b", "a\x7fb", "\u200b", "\ufeff "):
            with self.subTest(reason=bad):
                (self.profile / "profile.toml").write_text(base_toml)
                self.rule("optional", "btusb", "x.bin", reason=bad)
                with self.assertRaisesRegex(devprofile.ProfileError, "reason"):
                    devprofile.load(self.profile)

    def test_r7_pin_passes_only_through_initrd_load_list_and_initrd_modules_dep(self):
        self.pin("early/fw.bin")
        self.initrd.write_bytes(archive([
            ("lib/modules/early-top.ko", module()),
            ("lib/modules/early-leaf.ko", module("early/fw.bin")),
            ("lib/modules/modules.load", b"early-top.ko\n"),
            ("lib/modules/modules.dep", b"early-top.ko: early-leaf.ko\nearly-leaf.ko:\n"),
        ]))
        proc, rep = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(rep["pins"], [{
            "path": "early/fw.bin", "status": "declared", "kind": "static",
            "declared_by": ["initrd:lib/modules/early-leaf.ko"], "modules": ["early_leaf"],
            "declarations": ["early/fw.bin"], "glob": False}])
        self.assertIn("PINNED firmware: profile generic-x86: early/fw.bin: declared by initrd:lib/modules/early-leaf.ko",
                      proc.stdout.splitlines())

    def test_r8_blacklisted_and_blocklisted_modules_are_not_in_the_closure(self):
        self.pin("bl/pin.bin")
        # [blacklist].modules (also on every entry's module_blacklist=)
        self.ko(self.stock, "lib/modules/cros_ec_debugfs.ko", "bl/pin.bin")
        (self.stock / "lib/modules/modules.load").write_text("cros_ec_debugfs.ko\n")
        self.advises("bl/pin.bin", "declared only by unloaded report-only vendor_dlkm:lib/modules/cros_ec_debugfs.ko")
        # the stock vendor_dlkm modules.blocklist
        (self.stock / "lib/modules/cros_ec_debugfs.ko").unlink()
        self.ko(self.stock, "lib/modules/blk.ko", "bl/pin.bin")
        (self.stock / "lib/modules/modules.load").write_text("blk.ko\n")
        self.passes()
        (self.stock / "lib/modules/modules.blocklist").write_text("blocklist blk\n")
        self.advises("bl/pin.bin", "declared only by unloaded report-only vendor_dlkm:lib/modules/blk.ko")
        # module_blacklist= on every entry's kernel command line (debug_add reaches debug and quiet)
        (self.stock / "lib/modules/modules.blocklist").unlink()
        path = self.profile / "profile.toml"
        base = path.read_text()
        path.write_text(base.replace("debug_add = []", 'debug_add = ["blk"]', 1))
        self.advises("bl/pin.bin", "declared only by unloaded report-only vendor_dlkm:lib/modules/blk.ko")
        # a module only one entry blacklists (quiet_add) still loads on the other entry
        path.write_text(base.replace("debug_add = []", 'debug_add = []\nquiet_add = ["blk"]', 1))
        self.passes()
        # a blacklisted dependency is not followed
        path.write_text(base)
        self.ko(self.stock, "lib/modules/top.ko")
        (self.stock / "lib/modules/modules.load").write_text("top.ko\n")
        (self.stock / "lib/modules/modules.dep").write_text(
            "/vendor/lib/modules/top.ko: /vendor/lib/modules/blk.ko\n")
        self.passes()
        (self.stock / "lib/modules/modules.blocklist").write_text("blocklist blk\n")
        self.advises("bl/pin.bin")

    def test_r9_pins_record_matching_declarations_and_glob_matches(self):
        self.pin("iwlwifi-x-89.ucode")
        self.ko(self.stock, "lib/modules/wild.ko", "*")
        self.ko(self.stock, "lib/modules/exact.ko", "iwlwifi-x-89.ucode", "other.bin")
        (self.stock / "lib/modules/modules.load").write_text("wild.ko\nexact.ko\n")
        rep = self.passes()
        self.assertEqual(rep["pins"][0]["declarations"], ["*", "iwlwifi-x-89.ucode"])
        self.assertIs(rep["pins"][0]["glob"], True)
        (self.stock / "lib/modules/modules.load").write_text("exact.ko\n")
        rep = self.passes()
        self.assertEqual(rep["pins"][0]["declarations"], ["iwlwifi-x-89.ucode"])
        self.assertIs(rep["pins"][0]["glob"], False)

    def loader_script(self, *names, dirname="/vendor/lib/dev-modules"):
        script = self.vendor / "etc/fixture-loader.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/system/bin/sh\n" + "".join(f'"$MODPROBE" --dirname={dirname} {n}\n' for n in names))

    def loaded_by_pin(self, path, target):
        self.pin(path)
        with (self.profile / "profile.toml").open("a") as f:
            f.write(f"loaded_by = {json.dumps(target)}\n")

    def test_v1_a_root_stage_two_drops_no_longer_satisfies_a_pin(self):
        # Codex I1: the closure models the final image, i.e. the initrd load list after stage 2.
        toml_path = self.profile / "profile.toml"
        base = toml_path.read_text()
        self.assertEqual(base.count("first_stage_drop = []"), 1)
        self.pin("dead/first-stage.bin")
        pinned = toml_path.read_text()
        self.initrd.write_bytes(archive([
            ("lib/modules/drop.ko", module("dead/first-stage.bin")),
            ("lib/modules/modules.load", b"drop.ko\n"),
            ("lib/modules/modules.dep", b"drop.ko:\n")]))
        self.assertEqual(self.passes()["pins"][0]["declared_by"], ["initrd:lib/modules/drop.ko"])
        toml_path.write_text(pinned.replace("first_stage_drop = []", 'first_stage_drop = ["drop.ko"]'))
        rep = self.advises("dead/first-stage.bin: not declared by any loaded/required module",
                         "declared only by unloaded report-only initrd:lib/modules/drop.ko")
        self.assertEqual(rep["pins"][0]["status"], "undeclared")
        # One function: image.py and the checker share devprofile.stage2_modules_load.
        self.assertEqual(devprofile.stage2_modules_load("a.ko\ndrop.ko\n\nb.ko\n", devprofile.load(self.profile)),
                         "a.ko\nb.ko\n")
        for rel in ("builder/image.py", "builder/kmod/check-firmware.py"):
            src = (ROOT / rel).read_text()
            self.assertIn("devprofile.stage2_modules_load(", src, rel)
            self.assertNotIn('["first_stage_drop"]', src, rel)

    def test_v2_parent_of_a_kernel_blacklisted_dependency_is_not_loadable(self):
        # Codex I2: top.ko needs cros_ec.ko ([blacklist].modules); top never loads, nor does what needs top.
        self.pin("dead/pin.bin")
        self.pin("dead/grand.bin")
        self.ko(self.stock, "lib/modules/top.ko", "dead/pin.bin")
        self.ko(self.stock, "lib/modules/grand.ko", "dead/grand.bin")
        self.ko(self.stock, "lib/modules/cros_ec.ko")
        (self.stock / "lib/modules/modules.load").write_text("top.ko\n")
        (self.stock / "lib/modules/modules.dep").write_text(
            "/vendor/lib/modules/top.ko: /vendor/lib/modules/cros_ec.ko\n/vendor/lib/modules/cros_ec.ko:\n"
            "/vendor/lib/modules/grand.ko: /vendor/lib/modules/top.ko /vendor/lib/modules/cros_ec.ko\n")
        self.require("grand")  # a builder modprobe (no -b) cannot load it either
        rep = self.advises("dead/pin.bin: not declared by any loaded/required module",
                         "dead/grand.bin: not declared by any loaded/required module")
        self.assertEqual([p["declared_by"] for p in rep["pins"]], [[], []])
        # Control: without the blacklisted dependency both are declared.
        (self.stock / "lib/modules/modules.dep").write_text(
            "/vendor/lib/modules/top.ko:\n/vendor/lib/modules/grand.ko: /vendor/lib/modules/top.ko\n")
        self.assertEqual([p["status"] for p in self.passes()["pins"]], ["declared", "declared"])

    def test_v3_stock_blocklist_binds_only_the_loader_that_honors_it(self):
        # Codex I3: a builder modprobe without -b loads snd_hda_intel despite the stock blocklist.
        self.pin("sound/explicit.bin")
        self.required_ko(self.stock, "lib/modules/snd_hda_intel.ko", "sound/explicit.bin")
        (self.stock / "lib/modules/modules.blocklist").write_text("blocklist snd_hda_intel\n")
        rc = self.vendor / "etc/init/kano-hw.rc"
        rc.parent.mkdir(parents=True)
        rc.write_text("on early-boot\n    exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe "
                      "--dirname=/vendor/lib/modules snd_hda_intel\n")
        rep = self.passes()
        self.assertEqual(rep["pins"][0]["declared_by"], ["vendor_dlkm:lib/modules/snd_hda_intel.ko"])

    def test_v3_stock_blocklist_still_binds_its_own_load_list(self):
        self.pin("bl/pin.bin")
        self.ko(self.stock, "lib/modules/blk.ko", "bl/pin.bin")
        (self.stock / "lib/modules/modules.load").write_text("blk.ko\n")
        (self.stock / "lib/modules/modules.blocklist").write_text("blocklist blk\n")
        self.advises("bl/pin.bin", "declared only by unloaded report-only vendor_dlkm:lib/modules/blk.ko")

    def test_v3_vendor_blocklist_never_binds_the_initrd(self):
        # Codex I3 cross-tree: the vendor_dlkm blocklist never binds the initrd list (first-stage
        # init applies only the initrd's own modules.blocklist, which the checker does not read).
        self.pin("early/pin.bin")
        self.initrd.write_bytes(archive([
            ("lib/modules/blk.ko", module("early/pin.bin")), ("lib/modules/modules.load", b"blk.ko\n")]))
        (self.stock / "lib/modules").mkdir(parents=True)
        (self.stock / "lib/modules/modules.blocklist").write_text("blocklist blk\n")
        self.assertEqual(self.passes()["pins"][0]["declared_by"], ["initrd:lib/modules/blk.ko"])

    def test_v3_kernel_blacklist_binds_a_builder_modprobe(self):
        self.pin("bl/pin.bin")
        self.ko(self.stock, "lib/modules/cros_ec_debugfs.ko", "bl/pin.bin")
        rc = self.vendor / "etc/init/kano-hw.rc"
        rc.parent.mkdir(parents=True)
        rc.write_text("on early-boot\n    exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe "
                      "--dirname=/vendor/lib/modules cros_ec_debugfs\n")
        self.advises("bl/pin.bin", "declared only by unloaded report-only vendor_dlkm:lib/modules/cros_ec_debugfs.ko")

    def test_v4_hard_link_later_data_applies_to_every_name(self):
        # Codex I4 table 1: dup.ko and alias.ko share inode 77; alias's later data is dup's data too.
        self.pin("dead/pin.bin")
        self.initrd.write_bytes(newc([
            ("lib", b"", 2, 2, 0o40755), ("lib/modules", b"", 3, 2, 0o40755),
            ("lib/modules/dup.ko", module("dead/pin.bin"), 77, 2, 0o100644),
            ("lib/modules/alias.ko", module("live/other.bin"), 77, 2, 0o100644),
            ("lib/modules/modules.load", b"dup.ko\n", 78, 1, 0o100644),
            ("lib/modules/modules.dep", b"dup.ko:\n", 79, 1, 0o100644)]))
        rep = self.advises("dead/pin.bin: not declared by any loaded/required module")
        dup = next(m for m in rep["modules"] if m["source"] == "initrd:lib/modules/dup.ko")
        self.assertEqual([f["firmware"] for f in dup["firmware"]], ["live/other.bin"])
        # The link table is per archive: inode 77 in the next archive is a new file.
        self.initrd.write_bytes(newc([
            ("lib/modules/dup.ko", module("dead/pin.bin"), 77, 2, 0o100644),
            ("lib/modules/modules.load", b"dup.ko\n", 78, 1, 0o100644)])
            + newc([("lib/modules/alias.ko", module("live/other.bin"), 77, 2, 0o100644)]))
        self.assertEqual(self.passes()["pins"][0]["declared_by"], ["initrd:lib/modules/dup.ko"])

    def test_v4_hard_link_zero_size_placeholder_takes_the_later_data(self):
        # Codex I4 table 2: dup.ko is the zero-size first name; alias.ko carries the data.
        self.pin("valid/pin.bin")
        self.initrd.write_bytes(newc([
            ("lib", b"", 2, 2, 0o40755), ("lib/modules", b"", 3, 2, 0o40755),
            ("lib/modules/dup.ko", b"", 77, 2, 0o100644),
            ("lib/modules/alias.ko", module("valid/pin.bin"), 77, 2, 0o100644),
            ("lib/modules/modules.load", b"dup.ko\n", 78, 1, 0o100644),
            ("lib/modules/modules.dep", b"dup.ko:\n", 79, 1, 0o100644)]))
        rep = self.passes()
        self.assertEqual(rep["pins"][0]["declared_by"], ["initrd:lib/modules/dup.ko"])
        # A later plain record of a linked name truncates the shared inode: both names change.
        check = load_checker()
        names, inodes, links = {}, {}, {}
        check.unpack_entry(names, inodes, links, "a", 0o100644, 5, 2, (0, 0), b"one")
        check.unpack_entry(names, inodes, links, "b", 0o100644, 5, 2, (0, 0), b"")
        check.unpack_entry(names, inodes, links, "a", 0o100644, 9, 1, (0, 0), b"two")
        self.assertEqual((inodes[names["a"]][1], inodes[names["b"]][1]), (b"two", b"two"))

    def test_v5_whole_report_holds_no_root_text(self):
        # Codex I5 / Claude M4: requested_by, loaded_by and every pin field pass the guard in the JSON too.
        self.pin("intel/rt.bin", requested_by=f"runtime: see x+{self.vendor}+y")
        self.loaded_by_pin("amdgpu/x.bin", f"vendor:lib/x{self.vendor}/amdgpu.ko")
        self.pin(f"fw{self.vendor}/pin.bin")
        self.ko(self.stock, "lib/modules/btusb.ko", f"fw{self.vendor}/pin.bin")
        self.required_ko(self.system, "lib/modules/snd-sof.ko")
        proc, report = self.run_check()
        self.assert_report_withheld(proc)
        raw = self.report.read_text()
        for root in (self.root, self.root.resolve(), self.vendor, self.vendor.resolve()):
            self.assertNotIn(str(root), raw.replace("\\", ""))
            self.assertNotIn(repr(str(root))[1:-1], raw)
        pins = {p["kind"]: p for p in report["pins"]}
        self.assertEqual(pins["requested_by"]["requested_by"], WITHHELD)
        self.assertEqual(pins["loaded_by"]["loaded_by"], WITHHELD)
        self.assertIn(WITHHELD, [p["path"] for p in report["pins"]])

    def test_v6_loaded_by_target_must_be_named_by_a_builder_loader(self):
        self.loaded_by_pin("cond/fw.bin", "vendor:lib/dev-modules/m.ko")
        self.ko(self.vendor, "lib/dev-modules/m.ko", "cond/fw.bin")
        self.advises("loaded_by module vendor:lib/dev-modules/m.ko is not named by any modprobe line the builder emits")
        self.loader_script("m")
        self.assertEqual(self.passes()["pins"][0]["status"], "conditional")
        # A script the stock vendor already ships is not a builder loader.
        (self.base_vendor / "etc").mkdir(parents=True)
        shutil.copyfile(self.vendor / "etc/fixture-loader.sh", self.base_vendor / "etc/fixture-loader.sh")
        self.advises("is not named by any modprobe line the builder emits")

    def test_v6_loaded_by_target_already_in_the_closure_is_refused(self):
        self.loaded_by_pin("cond/fw.bin", "vendor_dlkm:lib/modules/m.ko")
        self.ko(self.stock, "lib/modules/m.ko", "cond/fw.bin")
        (self.stock / "lib/modules/modules.load").write_text("m.ko\n")
        self.loader_script("m", dirname="/vendor/lib/modules")
        self.advises("loaded_by module vendor_dlkm:lib/modules/m.ko is already in the load closure")

    def test_v6_loaded_by_target_that_is_kernel_blacklisted_is_refused(self):
        self.loaded_by_pin("cond/fw.bin", "vendor:lib/dev-modules/cros_ec_debugfs.ko")
        self.ko(self.vendor, "lib/dev-modules/cros_ec_debugfs.ko", "cond/fw.bin")
        self.loader_script("cros_ec_debugfs")
        self.advises("loaded_by module vendor:lib/dev-modules/cros_ec_debugfs.ko is kernel-blacklisted")

    def test_v6_loaded_by_target_shadowed_by_a_loaded_module_is_refused(self):
        # Claude M1 probe A: the loaded vendor_dlkm m requests other.bin; a stale dev copy still declares the pin.
        self.loaded_by_pin("cond/fw.bin", "vendor:lib/dev-modules/m.ko")
        self.ko(self.stock, "lib/modules/m.ko", "other.bin")
        (self.stock / "lib/modules/modules.load").write_text("m.ko\n")
        self.ko(self.vendor, "lib/dev-modules/m.ko", "cond/fw.bin")
        self.loader_script("m")
        self.advises("loaded_by module vendor:lib/dev-modules/m.ko is shadowed by vendor_dlkm:lib/modules/m.ko")

    def test_v7_safety_dev_row_loads_from_the_dev_tree(self):
        # Codex M2: the amd-hx370 bluetooth dev row is the only root; its file alone declares the pin.
        self.setup_real_profile_fixture("amd-hx370")
        self.ko(self.vendor, "lib/dev-modules/bluetooth.ko", "dev/bluetooth.bin")
        self.ko(self.stock, "lib/modules/bluetooth.ko")  # same name in vendor_dlkm, declares nothing
        self.pin("dev/bluetooth.bin")
        rep = self.passes()
        pin = next(p for p in rep["pins"] if p["path"] == "dev/bluetooth.bin")
        self.assertEqual(pin["declared_by"], ["vendor:lib/dev-modules/bluetooth.ko"])

    def test_v7_guards_without_tests_claude_m2_deepseek_m3_m6(self):
        # Ambiguous same-name module in one directory.
        self.pin("dead/pin.bin")
        self.ko(self.stock, "lib/modules/dup.ko", "live/other.bin")
        self.ko(self.stock, "lib/modules/old/dup.ko", "dead/pin.bin")
        (self.stock / "lib/modules/modules.load").write_text("dup.ko\n")
        self.fails("ambiguous module dup")
        # A loader file that is a symlink (to a file outside the image).
        shutil.rmtree(self.stock / "lib/modules/old")
        outside = self.root / "outside-load"
        outside.write_text("dead.ko\n")
        self.ko(self.system, "lib/modules/dead.ko", "dead/pin.bin")
        (self.system / "lib/modules/modules.load").symlink_to(outside)
        self.fails("loader file is not a regular file: system_dlkm:lib/modules/modules.load")
        (self.system / "lib/modules/modules.load").unlink()
        # /vendor_dlkm/ absolute dep paths, a commented load list and a continued dep row.
        (self.stock / "lib/modules/modules.load").write_text("# stock list\ntop.ko # first\n")
        self.ko(self.stock, "lib/modules/top.ko")
        (self.stock / "lib/modules/modules.dep").write_text(
            "/vendor_dlkm/lib/modules/top.ko: \\\n /system/lib/modules/dead.ko # leaf\n")
        self.assertEqual(self.passes()["pins"][0]["declared_by"], ["system_dlkm:lib/modules/dead.ko"])

    def test_v7_initrd_record_of_another_type_replaces_a_module(self):
        # DeepSeek M5: a later symlink record for the same path replaces the regular module.
        self.pin("dead/pin.bin")
        self.initrd.write_bytes(newc([
            ("lib/modules/dup.ko", module("dead/pin.bin"), 5, 1, 0o100644),
            ("lib/modules/modules.load", b"dup.ko\n", 6, 1, 0o100644)])
            + newc([("lib/modules/dup.ko", b"/elsewhere/dup.ko", 7, 1, 0o120777)]))
        self.fails("initrd dup.ko lib/modules/dup.ko is not a regular file")

    def test_f4_argument_errors_print_one_fixed_line_and_never_a_root(self):
        # Codex r4 I3 / Claude r4 m-6: argparse errors happen inside the guarded boundary.
        base = [sys.executable, "-B", str(CHECKER), "--profile", str(self.profile), "--vendor", str(self.vendor),
                "--base-vendor", str(self.base_vendor), "--module-tree", str(self.stock), "--initrd", str(self.initrd)]
        for argv in (base + ["--report", str(self.report), "--bogus", f"{self.vendor}/x"],
                     base,  # --report missing
                     base + ["--report", str(self.report), f"{self.vendor}/stray"],
                     [sys.executable, "-B", str(CHECKER), f"--profile={self.vendor}/p", "--vendor"]):
            with self.subTest(argv=argv[3:]):
                proc = subprocess.run(argv, text=True, capture_output=True)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stderr, "FAIL firmware check failed: invalid arguments (see --help)\n")
                self.assertEqual(proc.stdout, "")
                self.assert_no_host_paths(proc.stdout + proc.stderr)

    def test_f5_control_character_root_is_caught_by_the_repr_spelling(self):
        # Codex r4 M1 / Claude r4 m-1: a real tab in the vendor root; the reason carries its repr.
        vendor = self.root / "vendor\ttree"
        shutil.copytree(self.vendor, vendor, symlinks=True)
        self.required_ko(self.stock, "lib/modules/btusb.ko", "intel/opt.bin")
        self.rule("optional", "btusb", "intel/opt.bin", reason=f"see x+{str(vendor)!r}+y")
        proc, report = self.run_check(vendor=vendor)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(WITHHELD, proc.stdout)
        self.assertIn("a diagnostic contained a local build path and was withheld", proc.stderr)
        raw_report = self.report.read_text()
        for surface, text in (("stdout", proc.stdout), ("stderr", proc.stderr), ("report", raw_report)):
            for spelling in (str(vendor), repr(str(vendor))[1:-1], str(vendor.resolve()),
                             repr(str(vendor.resolve()))[1:-1]):
                self.assertNotIn(spelling, text, surface)
        self.assertEqual(report["optional"][0]["reason"], WITHHELD)

    def test_reverse_check_mutations_turn_red_in_temp_checker(self):
        global CHECKER
        saved = CHECKER
        src = saved.read_text()
        tdir = self.root / "mut-builder/kmod"
        tdir.mkdir(parents=True)
        (self.root / "mut-builder/lib").symlink_to(ROOT / "builder/lib")
        temp_checker = tdir / "check-firmware.py"
        mutations = (
            (
                "test_real_profiles_pass_reverse_check_on_measured_declarations",
                "omit modules.load from closure",
                "                add(lookup(tree, directory, name), tree)\n",
                "                pass\n",
            ),
            (
                "test_kano_pin_fails_when_declaring_module_removed_or_renamed",
                "skip reverse check loop over profile firmware pins",
                'for fw in profile["firmware"]:',
                "for fw in []:",
            ),
            (
                "test_pin_declared_only_by_report_only_module_outside_closure_fails",
                "count report-only modules outside closure as declarers",
                'if entry["source"] in closure:',
                "if True:",
            ),
            (
                "test_pin_declared_by_module_reached_through_modules_dep_closure_passes",
                "disable modules.dep closure expansion",
                "        for dep in sorted(deps.get(queue.pop(), ())):\n            if dep not in closure and dep not in dead:",
                "        for dep in [queue.pop()][:0]:\n            if dep not in closure and dep not in dead:",
            ),
            (
                "test_requested_by_validation_and_runtime_pin_counting",
                "ignore requested_by runtime pins in reverse check",
                "elif req_by:",
                "elif False:",
            ),
            (
                "test_reverse_check_report_and_errors_contain_no_host_temp_paths",
                "use absolute host path as module label",
                'source = f"{label}:{rel.as_posix()}"',
                "source = str(path)",
            ),
            (
                "test_r7_pin_passes_only_through_initrd_load_list_and_initrd_modules_dep",
                "ignore the initrd modules.load",
                'LOAD_LIST_DIRS = (("vendor_dlkm", "lib/modules"), ("system_dlkm", "lib/modules"), ("initrd", "lib/modules"))',
                'LOAD_LIST_DIRS = (("vendor_dlkm", "lib/modules"), ("system_dlkm", "lib/modules"))',
            ),
            (
                "test_r7_pin_passes_only_through_initrd_load_list_and_initrd_modules_dep",
                "ignore the initrd modules.dep",
                'DEP_DIRS = (("vendor_dlkm", "lib/modules"), ("system_dlkm", "lib/modules"), ("initrd", "lib/modules"),',
                'DEP_DIRS = (("vendor_dlkm", "lib/modules"), ("system_dlkm", "lib/modules"),',
            ),
            (
                "test_r7_pin_passes_only_through_initrd_load_list_and_initrd_modules_dep",
                "initrd reader drops modules.load/modules.dep",
                'if MODULE_FILE.fullmatch(base) or base in ("modules.load", "modules.dep"):',
                "if MODULE_FILE.fullmatch(base):",
            ),
        )
        try:
            for test_name, desc, old, new in mutations:
                with self.subTest(test=test_name, mutation=desc):
                    self.assertEqual(src.count(old), 1, old)
                    temp_checker.write_text(src.replace(old, new, 1))
                    temp_checker.chmod(0o755)
                    CHECKER = temp_checker
                    res = unittest.TestResult()
                    FirmwareTests(test_name).run(res)
                    self.assertFalse(res.wasSuccessful(), f"{test_name} stayed green on mutation: {desc}")
                    print(f"RED {test_name} [{desc}]")
        finally:
            CHECKER = saved


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FirmwareTests)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    failed = len({getattr(test, "test_case", test).id() for test, _ in result.failures + result.errors})
    skipped = len(result.skipped)
    print(f"firmware tests: {result.testsRun - failed - skipped} passed, {failed} failed, {skipped} skipped")
    sys.exit(0 if result.wasSuccessful() else 1)
