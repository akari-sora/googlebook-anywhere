#!/usr/bin/env python3
"""Executable synthetic integration boundaries, not crypto/device acceptance."""

import builtins
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import ifma  # noqa: E402
import ally_microdroid as micro  # noqa: E402


class GuestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.payload = self.root / "payload"
        self.payload.mkdir()
        (self.payload / "probe").write_bytes(b"checked-artifact")
        self.system = self.root / "system"
        self.system.mkdir()
        self.apex = self.system / ifma.VIRT_APEX_REL
        self.apex.parent.mkdir(parents=True)
        self.apex.write_bytes(b"public-synthetic-apex")
        self.guest = self.proof(self.payload)
        self.binding = ifma._guest_scan_binding(self.guest, self.apex)
        self.policy = ([], [], [])
        self.stack.enter_context(
            patch.object(ifma, "_manifest_scan_policy", return_value=self.policy)
        )
        self.overlay = self.stack.enter_context(
            patch.object(
                micro, "policy_overlay", side_effect=lambda proof, tree, policy: policy
            )
        )
        self.scan = self.stack.enter_context(patch.object(ifma.SCAN_IFMA, "scan_tree"))
        self.scan.side_effect = self.good_scan
        self.profile = {
            "model": {"id": "rog-ally"},
            "cpu_compat": {"patch_ifma": True, "patch_ally_bootstrap": True},
        }

    def proof(self, tree):
        return micro._verified(
            str(tree.resolve()),
            (("probe", hashlib.sha256((tree / "probe").read_bytes()).hexdigest()),),
            "synthetic-kernel-continuity",
            micro._sha(micro.__file__),
            str(self.root / "stock"),
        )

    def observed(self, **overrides):
        row = {
            "partition": "system_a",
            "container_chain": [{"kind": "tree_file", "path": ifma.VIRT_APEX_REL}],
            "kind": "zip",
            "classification": "container",
            "sha256": hashlib.sha256(self.apex.read_bytes()).hexdigest(),
        }
        row.update(overrides)
        return row

    def good_scan(self, tree, **kwargs):
        callback = kwargs.get("coverage_callback")
        if callback is not None:
            callback(self.observed())
        return {"accepted_residuals": [], "synthetic": True}

    def scan_system(self, binding=None, **kwargs):
        with redirect_stdout(io.StringIO()):
            return ifma.scan_rebuilt_tree(
                self.system,
                "synthetic-system",
                provenance=ifma._partition_provenance("system_a"),
                root_kind="tree_file",
                microdroid=self.binding if binding is None else binding,
                **kwargs,
            )

    def scan_payload(self):
        with redirect_stdout(io.StringIO()):
            return ifma.scan_rebuilt_tree(
                self.payload,
                "synthetic-payload",
                provenance=ifma._apex_payload_provenance(
                    ifma.VIRT_APEX_REL, "system_a"
                ),
                root_kind="erofs_file",
                microdroid=self.binding,
            )

    def test_verified_system_callback_happy_path(self):
        self.assertTrue(self.scan_system()["synthetic"])
        self.overlay.assert_called_once()
        self.assertIn("coverage_callback", self.scan.call_args.kwargs)

    def test_verified_payload_happy_path_without_system_callback(self):
        self.scan_payload()
        self.assertNotIn("coverage_callback", self.scan.call_args.kwargs)

    def test_forged_binding_cannot_overlay(self):
        forged = ifma._GuestScanBinding(self.guest, self.binding.apex_sha256)
        with self.assertRaises(SystemExit):
            self.scan_system(forged)
        self.overlay.assert_not_called()
        self.scan.assert_not_called()

    def test_external_receipt_not_binding(self):
        with self.assertRaises(SystemExit):
            self.scan_system({"ok": True})
        self.overlay.assert_not_called()

    def test_wrong_scope_cannot_overlay(self):
        for provenance, kind in (
            (ifma._partition_provenance("vendor_a"), "tree_file"),
            (
                ifma._apex_payload_provenance(
                    "system/apex/com.android.runtime.apex", "system_a"
                ),
                "erofs_file",
            ),
            (ifma._partition_provenance("system_a"), "erofs_file"),
        ):
            with self.subTest(provenance=provenance), self.assertRaises(SystemExit):
                ifma.scan_rebuilt_tree(
                    self.payload,
                    "wrong",
                    provenance=provenance,
                    root_kind=kind,
                    microdroid=self.binding,
                )
        self.overlay.assert_not_called()

    def test_stale_apex_rejected_before_overlay(self):
        self.apex.write_bytes(b"stale-or-swapped")
        with self.assertRaises(SystemExit):
            self.scan_system()
        self.overlay.assert_not_called()

    def test_symlink_apex_rejected(self):
        self.apex.unlink()
        self.apex.symlink_to(self.payload / "probe")
        with self.assertRaises(SystemExit):
            self.scan_system()

    def test_missing_callback_rejected(self):
        self.scan.side_effect = lambda *args, **kwargs: {"accepted_residuals": []}
        with self.assertRaises(SystemExit):
            self.scan_system()

    def test_duplicate_callback_rejected(self):
        def duplicate(tree, **kwargs):
            kwargs["coverage_callback"](self.observed())
            kwargs["coverage_callback"](self.observed())
            return {"accepted_residuals": []}

        self.scan.side_effect = duplicate
        with self.assertRaises(SystemExit):
            self.scan_system()

    def test_callback_wrong_type_partition_digest_or_classification_rejected(self):
        for override in (
            {"kind": "elf"},
            {"partition": "vendor_a"},
            {"sha256": "0" * 64},
            {"classification": "unsupported"},
        ):

            def wrong(tree, **kwargs):
                kwargs["coverage_callback"](self.observed(**override))
                return {"accepted_residuals": []}

            self.scan.side_effect = wrong
            with self.subTest(override=override), self.assertRaises(SystemExit):
                self.scan_system()

    def test_untyped_alias_callback_not_counted(self):
        def wrong(tree, **kwargs):
            kwargs["coverage_callback"](
                self.observed(
                    container_chain=[{"kind": "erofs_file", "path": ifma.VIRT_APEX_REL}]
                )
            )
            return {"accepted_residuals": []}

        self.scan.side_effect = wrong
        with self.assertRaises(SystemExit):
            self.scan_system()

    def test_apex_changed_after_callback_rejected(self):
        def changed(tree, **kwargs):
            kwargs["coverage_callback"](self.observed())
            self.apex.write_bytes(b"changed-after-coverage")
            return {"accepted_residuals": []}

        self.scan.side_effect = changed
        with self.assertRaises(SystemExit):
            self.scan_system()

    def test_guest_changed_during_payload_scan_rejected(self):
        def changed(tree, **kwargs):
            (self.payload / "probe").write_bytes(b"changed-after-overlay")
            return {"accepted_residuals": []}

        self.scan.side_effect = changed
        with self.assertRaises(micro.Refused):
            self.scan_payload()

    def test_system_requires_signed_apex_binding(self):
        unsigned = ifma._guest_scan_binding(self.guest)
        with self.assertRaises(SystemExit):
            self.scan_system(unsigned)

    def test_disabled_kano_no_guest_import_or_tool_loading(self):
        original = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == "ally_microdroid":
                raise AssertionError("disabled profile imported guest module")
            return original(name, *args, **kwargs)

        for profile in (
            {},
            {
                "model": {"id": "kano"},
                "cpu_compat": {"patch_ifma": True, "patch_ally_bootstrap": False},
            },
        ):
            with (
                patch("builtins.__import__", side_effect=guarded),
                patch.object(micro, "preflight") as preflight,
            ):
                self.assertIsNone(ifma.microdroid_preflight(profile, "unused"))
                preflight.assert_not_called()

    def test_enabled_preflight_uses_module(self):
        with patch.object(
            micro, "preflight", return_value={"checked": True}
        ) as preflight:
            self.assertEqual(
                ifma.microdroid_preflight(self.profile, "tools"), {"checked": True}
            )
            preflight.assert_called_once_with("tools")

    def test_fresh_chain_context_each_readback(self):
        module = SimpleNamespace(
            rebuild_chain=Mock(return_value=self.guest), verify_chain=Mock()
        )
        module.verify_chain.side_effect = lambda prof, stock, tree, *args: self.proof(
            Path(tree)
        )
        with patch.object(ifma, "_guest_module", return_value=module):
            chain = ifma._GuestChain(self.profile, {"existing": "keys"})
            initial = chain.rebuild(self.payload, self.root / "build", "tools")
            first = chain.verify(self.payload, self.root / "readback1", "tools")
            second = chain.verify(
                self.payload, self.root / "readback2", "tools", apex=self.apex
            )
            self.assertIsNot(first, initial)
            self.assertIsNot(second, first)
            self.assertIsNot(first.guest, second.guest)
            self.assertEqual(module.verify_chain.call_count, 2)
            for call in module.verify_chain.call_args_list:
                self.assertEqual(call.args[1], self.guest.stock_payload_root)
            self.assertEqual(
                second.apex_sha256, hashlib.sha256(self.apex.read_bytes()).hexdigest()
            )

    def test_apex_mutation_inside_fresh_verify_chain_refused(self):
        module = SimpleNamespace(
            rebuild_chain=Mock(return_value=self.guest), verify_chain=Mock()
        )

        def mutate(prof, stock, tree, *args):
            self.apex.write_bytes(b"mutated-inside-verify-chain")
            return self.proof(Path(tree))

        module.verify_chain.side_effect = mutate
        with patch.object(ifma, "_guest_module", return_value=module):
            chain = ifma._GuestChain(self.profile, {})
            chain.rebuild(self.payload, self.root / "build", "tools")
            with self.assertRaises(SystemExit):
                chain.verify(
                    self.payload, self.root / "verify", "tools", apex=self.apex
                )

    def test_chain_requires_stock_before_verify(self):
        with patch.object(ifma, "_guest_module", return_value=SimpleNamespace()):
            chain = ifma._GuestChain(self.profile, {})
            with self.assertRaises(SystemExit):
                chain.verify(self.payload, self.root / "verify", "tools")

    def test_chain_only_exact_outer_virt_scope(self):
        with patch.object(ifma, "_guest_module", return_value=SimpleNamespace()):
            chain = ifma._GuestChain(self.profile, {})
        ifma._guest_apex_scope(chain, ifma.VIRT_APEX_REL, None)
        for ctx, rel, scope in (
            ({}, ifma.VIRT_APEX_REL, None),
            (chain, ifma.MICRODROID_RUNTIME_REL, None),
            (chain, ifma.VIRT_APEX_REL, "runtime"),
        ):
            with self.assertRaises(SystemExit):
                ifma._guest_apex_scope(ctx, rel, scope)

    def test_environment_sanitizes_loaders_python_path_and_preserves_tmpdir(self):
        hostile = {
            "PATH": "/hostile",
            "LD_PRELOAD": "bad",
            "LD_LIBRARY_PATH": "/bad",
            "DYLD_INSERT_LIBRARIES": "bad",
            "DYLD_LIBRARY_PATH": "bad",
            "PYTHONPATH": "bad",
            "PYTHONHOME": "bad",
            "PYTHONUSERBASE": "bad",
            "PYTHONOPTIMIZE": "2",
            "TMPDIR": str(self.root / "caller-temp"),
        }
        with patch.dict(os.environ, hostile, clear=True):
            env = ifma._ot_env(self.root / "tools")
        self.assertEqual(
            env["PATH"], str((self.root / "tools").resolve() / "bin") + ":/usr/bin:/bin"
        )
        self.assertEqual(
            env["LD_LIBRARY_PATH"], str((self.root / "tools").resolve() / "lib64")
        )
        self.assertEqual(env["TMPDIR"], hostile["TMPDIR"])
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        for key in hostile.keys() - {"PATH", "LD_LIBRARY_PATH", "TMPDIR"}:
            self.assertNotIn(key, env)


class OuterPayloadDeltaTests(unittest.TestCase):
    """Real patch_apex_file delta gate with synthetic EROFS/signing tools."""

    def transaction(self, original_count, *, unexpected=None):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            apex = root / "stock.apex"
            with zipfile.ZipFile(apex, "w") as archive:
                archive.writestr("apex_payload.img", b"synthetic stock payload")
            original_apex = apex.read_bytes()
            hosts = [f"bin/{name}" for name in
                     ("vm", "virtmgr", "early_virtmgr", "crosvm_minimal", "crosvm")]
            guests = [micro.KERNEL, micro.SYSTEM, micro.VBMETA, *micro.INITRDS.values()]
            self.assertEqual(len(guests), 5)
            files = hosts + guests + ["bin/host-six", "etc/fs/guest-six"]
            initial = {rel: b"original" for rel in files}
            initial.update({rel: b"patched" for rel in hosts[original_count:]})
            report = {"targets": [
                {"path": f"apex/com.google.android.virt/{rel}",
                 "rel_path": rel, "before": "original" if i < original_count else "patched",
                 "after": "patched", "changed": i < original_count}
                for i, rel in enumerate(hosts)
            ], "changed": [f"apex/com.google.android.virt/{rel}"
                           for rel in hosts[:original_count]]}

            def write(tree, rel, data):
                target = Path(tree) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

            def patch_host(tree, *_args, **_kwargs):
                for rel in hosts[:original_count]:
                    write(tree, rel, b"patched")
                return report

            def rebuild_guest(tree, *_args):
                for rel in guests:
                    write(tree, rel, b"rebuilt guest")
                return SimpleNamespace(guest=SimpleNamespace(artifacts=tuple(
                    (rel, "synthetic-pin") for rel in guests)))

            def run_ot(_tools, tool, *args):
                if tool == "fsck.erofs":
                    tree = Path(next(str(arg).split("=", 1)[1] for arg in args
                                     if str(arg).startswith("--extract=")))
                    if tree.name == "payload-tree":
                        for rel, data in initial.items():
                            write(tree, rel, data)
                    else:
                        source = root / "work/payload-tree"
                        for rel in files:
                            write(tree, rel, (source / rel).read_bytes())
                        if unexpected:
                            write(tree, unexpected, b"unexpected mutation")
                        self.assertEqual(len({rel for rel in files
                                              if (tree / rel).read_bytes() != initial[rel]}),
                                         original_count + 5 + bool(unexpected))
                elif tool == "mkfs.erofs":
                    Path(args[-2]).write_bytes(b"synthetic rebuilt EROFS")
                return ""

            for obj, name, kwargs in (
                (ifma, "verify_apex_container", {"return_value": {}}),
                (ifma, "_stock_apex_avb_identity", {"return_value": {
                    "salt": "synthetic", "apex_key": "synthetic"}}),
                (ifma, "_strip_avb_footer", {"return_value": (b"synthetic EROFS", 8192)}),
                (ifma, "_erofs_meta_map", {"return_value": {}}),
                (ifma, "_run_ot", {"side_effect": run_ot}),
                (ifma, "scan_rebuilt_tree", {}),
                (ifma, "_assert_same_erofs_metadata", {}),
                (ifma.rebuild, "superblock_params", {"return_value": {
                    "build_time": 1, "uuid": "fixture", "volume_name": "", "pcluster_size": 0}}),
                (ifma.rebuild, "fs_uuid", {"return_value": "fixture"}),
                (ifma.rebuild, "write_configs", {}),
                (ifma.rebuild, "restore_mtimes", {}),
                (ifma.PATCH_IFMA, "run", {"side_effect": patch_host}),
                (ifma._GuestChain, "rebuild", {"side_effect": rebuild_guest}),
                (ifma._GuestChain, "verify", {}),
                (ifma, "_guest_module", {"return_value": SimpleNamespace()}),
            ):
                stack.enter_context(patch.object(obj, name, **kwargs))
            chain = ifma._GuestChain({"synthetic": True}, {})
            # Stop just after the exact delta check, before any real AVB/signing.
            class DeltaAccepted(Exception):
                pass
            footer = stack.enter_context(patch.object(
                ifma, "_apex_partition_size", side_effect=DeltaAccepted))
            expected = SystemExit if unexpected else DeltaAccepted
            with self.assertRaises(expected) as raised:
                ifma.patch_apex_file(apex, ifma.VIRT_APEX_REL, root / "work",
                                     "synthetic-tools", {"payload_key": "not-a-real-key"},
                                     microdroid=chain)
            if unexpected:
                self.assertIn("unexpected payload file changes", str(raised.exception))
                footer.assert_not_called()
                ifma._GuestChain.verify.assert_not_called()
            else:
                footer.assert_called_once()
                ifma._GuestChain.verify.assert_called_once()
            ifma._assert_same_erofs_metadata.assert_called_once()
            self.assertEqual(apex.read_bytes(), original_apex)

    def test_original_hosts_require_ten_exact_changes(self):
        self.transaction(5)

    def test_prepatched_hosts_require_only_five_guest_changes(self):
        self.transaction(0)

    def test_mixed_hosts_require_only_original_subset_plus_five_guests(self):
        for count in range(1, 5):
            with self.subTest(original_hosts=count):
                self.transaction(count)

    def test_unexpected_sixth_guest_mutation_refused(self):
        self.transaction(0, unexpected="etc/fs/guest-six")

    def test_unexpected_sixth_host_mutation_refused(self):
        self.transaction(2, unexpected="bin/host-six")

    def test_prepatched_host_mutation_is_not_allowed_by_target_membership(self):
        self.transaction(2, unexpected="bin/crosvm")


class CacheClosureTests(unittest.TestCase):
    """Execute extracted real shell gates; guest preflight itself is NOT mocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "repo"
        lib = self.fixture / "builder/lib"
        lib.mkdir(parents=True)
        for name in ("ifma.py", "ally_microdroid.py", "rebuild.py", "container_alignment.py"):
            (lib / name).symlink_to(ROOT / "builder/lib" / name)
        (lib / "devprofile.py").write_text(
            "import json,sys\n"
            "def load(path):\n return json.load(open(path))\n"
            'if __name__=="__main__":\n'
            ' p=load(sys.argv[2]); print(str(p["cpu_compat"]["patch_ally_bootstrap"]).lower())\n'
        )
        (lib / "ally_bootstrap.py").write_text(
            'print("synthetic-bootstrap-source-identity")\n'
        )
        self.profile = self.root / "profile.json"
        self.profile.write_text(
            __import__("json").dumps(
                {
                    "model": {"id": "rog-ally"},
                    "cpu_compat": {"patch_ifma": True, "patch_ally_bootstrap": True},
                }
            )
        )
        self.work = self.root / "work"
        self.cached = self.work / "build/image/cached.img"
        self.cached.parent.mkdir(parents=True)
        self.cached.write_bytes(b"preexisting-cached-image-must-survive")
        self.marker = self.root / "cache-hit-marker"
        self.output = self.root / "output-must-not-be-created"
        self.tools = self.root / "external-tools"

    def gate(self, installer):
        text = (
            ROOT / ("install/install.sh" if installer else "builder/build.sh")
        ).read_text()
        prefix = (
            'if [ "$(python3 "$ROOT/builder/lib/devprofile.py" get "$PROFILE" cpu_compat.patch_ally_bootstrap)'
            if installer
            else 'if [ "$(python3 "$HERE/lib/devprofile.py" get "$GBOS_PROFILE" cpu_compat.patch_ally_bootstrap)'
        )
        start = text.index(prefix)
        end = text.index("\nfi\n", start) + len("\nfi\n")
        if installer:
            self.assertLessEqual(end, text.index('IMG="$WORK/build/image/$IMAGE_NAME"'))
        else:
            self.assertEqual(text[end:].splitlines()[0], 'mkdir -p "$GBOS_OUT"')
        return text[start:end]

    def run_gate(self, installer, tool_mode, *, disabled=False):
        import subprocess

        env = os.environ.copy()
        env.pop("GBOS_MICRODROID_TOOLS_DIR", None)
        env.pop("PYTHONOPTIMIZE", None)
        if tool_mode != "missing":
            self.tools.mkdir()
            # Real source_identity must reject these before avbtool/version execution.
            (self.tools / "sign_virt_apex.py").write_text("not approved signer")
            if tool_mode == "wrong-initrd":
                source = (
                    ROOT.parent
                    / "goal-execution-20261025/microdroid-host-tool-src/sign_virt_apex.py"
                )
                (self.tools / "sign_virt_apex.py").write_bytes(source.read_bytes())
            (self.tools / "initrd_bootconfig").write_text("not approved initrd utility")
            env["GBOS_MICRODROID_TOOLS_DIR"] = str(self.tools)
        if disabled:
            self.profile.write_text(
                __import__("json").dumps(
                    {
                        "model": {"id": "kano"},
                        "cpu_compat": {
                            "patch_ifma": True,
                            "patch_ally_bootstrap": False,
                        },
                    }
                )
            )
            guest = self.fixture / "builder/lib/ally_microdroid.py"
            guest.unlink()
            guest.write_text(
                'raise RuntimeError("KANO must not import guest module or load tools")\n'
            )
        env.update(
            {
                "ROOT": str(self.fixture),
                "HERE": str(self.fixture / "builder"),
                "WORK": str(self.work),
                "PROFILE": str(self.profile),
                "GBOS_PROFILE": str(self.profile),
                "GBOS_OTATOOLS": str(self.root / "nonexistent-otatools"),
                "GBOS_OUT": str(self.output),
                "MARKER": str(self.marker),
                "CACHED": str(self.cached),
            }
        )
        trailer = 'test -f "$CACHED"\nprintf cache-hit > "$MARKER"\n'
        if not installer:
            trailer = 'mkdir -p "$GBOS_OUT"\n' + trailer
        script = (
            'set -eu\ndie() { echo "$*" >&2; exit 17; }\nPARAMS=""\n'
            + self.gate(installer)
            + trailer
        )
        before = self.cached.read_bytes()
        result = subprocess.run(
            ["bash", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
        self.assertEqual(self.cached.read_bytes(), before)
        if disabled:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.marker.read_text(), "cache-hit")
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("Ally Microdroid preflight failed", result.stderr)
            self.assertFalse(self.marker.exists())
            self.assertFalse(self.output.exists())
            self.assertNotIn("Ally bootstrap source identity failed", result.stderr)
        return result

    def test_installer_missing_external_tools_fails_before_cached_hit(self):
        result = self.run_gate(True, "missing")
        self.assertIn("GBOS_MICRODROID_TOOLS_DIR is required", result.stderr)

    def test_installer_wrong_pinned_tools_fails_before_cached_hit(self):
        result = self.run_gate(True, "wrong")
        self.assertIn("Microdroid tool pin mismatch", result.stderr)

    def test_installer_exact_signer_wrong_initrd_fails_before_cache_hit(self):
        source = (
            ROOT.parent
            / "goal-execution-20261025/microdroid-host-tool-src/sign_virt_apex.py"
        )
        if not source.is_file():
            self.skipTest("pinned public signer source fixture unavailable")
        result = self.run_gate(True, "wrong-initrd")
        self.assertIn("Microdroid tool pin mismatch", result.stderr)

    def test_installer_disabled_kano_reaches_cached_hit_without_guest_import(self):
        self.run_gate(True, "missing", disabled=True)

    def test_direct_builder_missing_tools_fails_before_output_mkdir(self):
        self.run_gate(False, "missing")

    def test_direct_builder_wrong_pinned_tools_fails_before_output_mkdir(self):
        self.run_gate(False, "wrong")

    def test_direct_builder_disabled_kano_skips_guest_tools(self):
        self.run_gate(False, "missing", disabled=True)


if __name__ == "__main__":
    unittest.main()
