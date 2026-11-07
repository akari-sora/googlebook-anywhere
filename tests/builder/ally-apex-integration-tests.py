#!/usr/bin/env python3
"""Synthetic executable APEX/system routing tests; no private payload or crypto acceptance."""
from contextlib import ExitStack, contextmanager
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
SPEC = importlib.util.spec_from_file_location("ally_apex_ifma", ROOT / "builder/lib/ifma.py")
ifma = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ifma)

RUNTIME = "system/apex/com.android.runtime.apex"
APPSEARCH = "system/apex/com.google.android.appsearch.apex"
VIRT = "system/apex/com.google.android.virt.apex"
ALLY = {"model": {"id": "rog-ally"}, "cpu_compat": {"patch_ifma": True, "patch_ally_bootstrap": True}}


class RoutingTests(unittest.TestCase):
    def test_guest_runtime_catalog_does_not_add_outer_system_path(self):
        rel = "microdroid/system/apex/com.android.runtime.apex"
        spec, scope = ifma._apex_repair_spec(rel, None, None, "microdroid_runtime")
        self.assertIsNone(scope)
        self.assertEqual(spec, ifma.MICRODROID_RUNTIME_SPEC)
        self.assertEqual(spec["stock_sha256"], "917a98300b0ccecdb05e5a413ea650d1a4c061c4bcb02fdcf8efa68ce10fb2b0")
        self.assertEqual(spec["stock_payload_sha256"], "5ebccf5d395e5be85159d52eb46a5d5c2229ee4ef155f1252afcdf6e698b5962")
        self.assertNotIn(rel, ifma.ally_adler_apex_specs(ALLY))
        for wrong in (None, "runtime", "appsearch"):
            with self.subTest(scope=wrong), self.assertRaises(SystemExit):
                ifma._apex_repair_spec(rel, None, None, wrong)
        with self.assertRaises(SystemExit):
            ifma._apex_repair_spec(RUNTIME, None, None, "microdroid_runtime")

    def test_guest_runtime_provenance_is_full_nested_chain(self):
        got = ifma._apex_payload_provenance("microdroid/system/apex/com.android.runtime.apex", "system_a")
        self.assertEqual(got, {"partition": "system_a", "container_chain": [
            {"kind": "tree_file", "path": VIRT},
            {"kind": "zip_member", "path": "apex_payload.img"},
            {"kind": "erofs_file", "path": "etc/fs/microdroid.img"},
            {"kind": "erofs_file", "path": RUNTIME},
            {"kind": "zip_member", "path": "apex_payload.img"},
        ]})
        self.assertEqual(len(ifma._apex_payload_provenance(RUNTIME, "system_a")["container_chain"]), 2)
        with self.assertRaises(SystemExit):
            ifma._apex_payload_provenance("microdroid/system/apex/com.android.runtime.apex", "vendor_a")

    def test_guest_runtime_wrong_stock_refuses_before_scratch_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            apex = Path(directory) / "substituted.apex"
            apex.write_bytes(b"not the pinned guest runtime")
            work = Path(directory) / "work"
            work.mkdir()
            marker = work / "keep"
            marker.write_bytes(b"keep")
            with mock.patch.object(ifma.shutil, "rmtree") as remove, \
                    mock.patch.object(ifma, "verify_apex_container") as verify:
                with self.assertRaisesRegex(SystemExit, "guest runtime.*SHA256"):
                    ifma.patch_apex_file(apex, "microdroid/system/apex/com.android.runtime.apex",
                                         work, "ot", {}, adler_scope="microdroid_runtime")
                remove.assert_not_called()
                verify.assert_not_called()
                self.assertEqual(marker.read_bytes(), b"keep")

    def test_guest_runtime_payload_pin_checked_before_scratch_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            apex = Path(directory) / "fixture.apex"
            with zipfile.ZipFile(apex, "w") as archive:
                archive.writestr("apex_payload.img", b"wrong guest payload")
            hashes = [types.SimpleNamespace(hexdigest=lambda: ifma.MICRODROID_RUNTIME_SPEC["stock_sha256"]),
                      types.SimpleNamespace(hexdigest=lambda: "0" * 64)]
            with mock.patch.object(ifma.hashlib, "sha256", side_effect=hashes), \
                    mock.patch.object(ifma.shutil, "rmtree") as remove, \
                    mock.patch.object(ifma, "verify_apex_container") as verify:
                with self.assertRaisesRegex(SystemExit, "guest runtime stock payload SHA256"):
                    ifma.patch_apex_file(apex, "microdroid/system/apex/com.android.runtime.apex",
                                         Path(directory) / "work", "ot", {}, adler_scope="microdroid_runtime")
                remove.assert_not_called()
                verify.assert_not_called()

    def test_catalog_is_separate_and_exact(self):
        self.assertEqual(set(ifma.APEX_SPECS), {VIRT, "apex/com.android.hardware.audio.desktop.apex",
                                              "apex/com.android.hardware.fingerprint.crosfp.apex"})
        self.assertEqual(set(ifma.ALLY_ADLER_APEX_SPECS), {RUNTIME, APPSEARCH})
        for rel, scope, key, salt in (
            (RUNTIME, "runtime", "com.android.runtime", "de3e3fbdc709c48105215c7eab4606070ec11445364693c477f5f92fed3ae84b"),
            (APPSEARCH, "appsearch", "com.android.appsearch", "9ec54af85657c40c3e59a3e631cf017b0f7a65e987f229d1f2b1e0577326da33"),
        ):
            self.assertEqual(ifma.ALLY_ADLER_APEX_SPECS[rel], {
                "partition": "system_a", "apex_key": key, "salt": salt, "allow_caps": False,
                "partition_name": "", "adler_scope": scope})

    def test_disabled_and_nonally_do_not_add_paths_or_import_adler(self):
        with mock.patch.dict(sys.modules, {"ally_adler": None}):
            for prof in ({}, {"model": {"id": "kano"}},
                         {"model": {"id": "kano"}, "cpu_compat": {"patch_ifma": True}},
                         {"cpu_compat": {"patch_ally_bootstrap": False}}):
                self.assertEqual(ifma.ally_adler_apex_specs(prof), {})
        self.assertEqual(ifma.ally_adler_apex_specs(ALLY), ifma.ALLY_ADLER_APEX_SPECS)

    def test_dependency_model_and_nonboolean_flag_fail_before_system_effects(self):
        for prof in ({"model": {"id": "rog-ally"}, "cpu_compat": {"patch_ally_bootstrap": True}},
                     {"model": {"id": "kano"}, "cpu_compat": ALLY["cpu_compat"]},
                     {"model": {"id": "rog-ally"}, "cpu_compat": {"patch_ifma": True, "patch_ally_bootstrap": "true"}}):
            with self.subTest(prof=prof), mock.patch.object(ifma, "ensure_apex_keys") as keys, \
                    mock.patch.object(ifma, "_gpt_partitions") as gpt:
                with self.assertRaises(SystemExit):
                    ifma.rebuild_system_partition(prof, "/not-present", "/not-present", otatools="ot")
                keys.assert_not_called()
                gpt.assert_not_called()

    def test_bad_rel_scope_spec_and_ifma_scope_refuse_before_scratch_mutation(self):
        cases = [(VIRT, {"adler_scope": "runtime"}),
                 (RUNTIME, {}), (RUNTIME, {"adler_scope": "appsearch"}),
                 (RUNTIME, {"adler_scope": "runtime", "scope": ("partition", "system_a")}),
                 (RUNTIME, {"adler_scope": "runtime", "spec": {"salt": "00"}}),
                 ("../" + RUNTIME, {"adler_scope": "runtime"})]
        for rel, kwargs in cases:
            for verify in (False, True):
                with self.subTest(rel=rel, kwargs=kwargs, verify=verify), \
                        mock.patch.object(ifma.shutil, "rmtree") as remove, \
                        mock.patch.object(ifma, "verify_apex_container") as container:
                    with self.assertRaisesRegex(SystemExit, "Adler APEX scope"):
                        if verify:
                            ifma.verify_apex_file("unused", rel, "unused", "ot", **kwargs)
                        else:
                            ifma.patch_apex_file("unused", rel, "unused", "ot", {}, **kwargs)
                    remove.assert_not_called()
                    container.assert_not_called()

    def test_empty_partition_name_checked_once_and_legacy_parser_unchanged(self):
        info = "Salt: ab\nProp: apex.key -> 'key'\nPartition Name:        \n"
        self.assertEqual(ifma._parse_apex_avb_info(info, "test", partition_name=""),
                         {"salt": "ab", "apex_key": "key"})
        for bad in (info.replace("Partition Name:        \n", ""),
                    info + "Partition Name: \n", info.replace("Partition Name:        \n", "Partition Name: apex_payload\n")):
            with self.assertRaisesRegex(SystemExit, "exactly one AVB partition name"):
                ifma._parse_apex_avb_info(bad, "test", partition_name="")
        self.assertEqual(ifma._parse_apex_avb_info(info.replace("Partition Name:        \n", ""), "legacy"),
                         {"salt": "ab", "apex_key": "key"})

    def test_empty_footer_name_propagates_to_sizing_and_real_command(self):
        calls = []
        def run(*args):
            calls.append(args)
            return "4096" if "--calc_max_image_size" in args else ""
        with mock.patch.object(ifma, "_run_ot", side_effect=run):
            self.assertEqual(ifma._apex_partition_size("ot", 8192, 1024, "key", "ab", "name", partition_name=""), 8192)
            ifma._add_apex_hashtree_footer("ot", "payload", 8192, "key", "ab", "name", partition_name="")
        self.assertEqual(calls[0][3:-1], calls[1][5:])
        self.assertEqual(calls[0][calls[0].index("--partition_name") + 1], "")
        legacy = ifma._apex_hashtree_args(8192, "key", "ab", "name")
        self.assertEqual(legacy[legacy.index("--partition_name") + 1], "apex_payload")


class ApexTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.apex = self.base / "stock.apex"
        with zipfile.ZipFile(self.apex, "w") as zf:
            zf.writestr("apex_payload.img", b"synthetic payload")
            zf.writestr("apex_pubkey", b"pubkey")
        self.original_apex = self.apex.read_bytes()
        cert = self.base / "cert.der"
        cert.write_bytes(b"certificate")
        self.keys = {"payload_key": "payload.pem", "container_key": "container.pem", "container_cert_der": cert}
        self.events = []
        self.rel = RUNTIME
        self.scope = "runtime"
        self.target = "bin/linker64"
        self.corrupt_other = False
        self.corrupt_receipt = False
        self.receipt = {"schema": 1, "sources": {"fixture": "not-real-source-proof"}, "scope": "runtime",
                        "targets": [{"path": self.target, "source_sha256": "original", "sha256": "patched"}]}

    def populate(self, root, data):
        root = Path(root)
        path = root / self.target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        (root / "untouched").write_bytes(b"unchanged")

    def patch_adler(self, scope, tree):
        self.assertEqual(scope, self.scope)
        self.events.append("patch-adler")
        (Path(tree) / self.target).write_bytes(b"patched")
        return copy.deepcopy(self.receipt)

    def verify_adler(self, scope, tree):
        self.assertEqual(scope, self.scope)
        self.assertEqual((Path(tree) / self.target).read_bytes(), b"patched")
        self.events.append("verify-adler")
        receipt = copy.deepcopy(self.receipt)
        if self.corrupt_receipt:
            receipt["sources"]["fixture"] = "drift"
        return receipt

    def run_ot(self, _ot, tool, *args):
        if tool == "fsck.erofs":
            root = Path(next(str(arg).split("=", 1)[1] for arg in args if str(arg).startswith("--extract=")))
            self.populate(root, b"original" if root.name == "payload-tree" else b"patched")
            if self.corrupt_other and root.name == "payload-tree-new":
                (root / "untouched").write_bytes(b"unexpected")
        elif tool == "mkfs.erofs":
            Path(args[-2]).write_bytes(b"rebuilt EROFS fixture")
        elif tool == "avbtool" and args[0] == "extract_public_key":
            Path(args[args.index("--output") + 1]).write_bytes(b"pubkey")
        elif tool == "avbtool" and args[0] == "info_image":
            spec = ifma.ALLY_ADLER_APEX_SPECS[self.rel]
            return f"Salt: {spec['salt']}\nProp: apex.key -> '{spec['apex_key']}'\nPartition Name: \n"
        self.events.append(tool + ":" + str(args[0] if args else ""))
        return ""

    @contextmanager
    def fixture(self):
        module = types.SimpleNamespace(patch_tree=self.patch_adler, verify_tree=self.verify_adler, Refused=ValueError)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(sys.modules, {"ally_adler": module}))
            stack.enter_context(mock.patch.object(ifma, "verify_apex_container", return_value={"v3_min_sdk": 33, "v3_max_sdk": 37}))
            stack.enter_context(mock.patch.object(ifma, "_strip_avb_footer", return_value=(b"stock EROFS fixture", 8192)))
            stack.enter_context(mock.patch.object(ifma, "_run_ot", side_effect=self.run_ot))
            stack.enter_context(mock.patch.object(ifma.rebuild, "superblock_params", return_value={
                "build_time": 1, "uuid": "fixture", "volume_name": "", "pcluster_size": 0}))
            stack.enter_context(mock.patch.object(ifma.rebuild, "fs_uuid", return_value="fixture"))
            for obj, name in ((ifma, "_assert_same_erofs_metadata"), (ifma, "scan_rebuilt_tree"),
                              (ifma.rebuild, "write_configs"), (ifma.rebuild, "restore_mtimes")):
                stack.enter_context(mock.patch.object(obj, name))
            stack.enter_context(mock.patch.object(ifma, "_erofs_meta_map", return_value={}))
            yield

    @contextmanager
    def patch_fixture(self):
        with self.fixture(), ExitStack() as stack:
            calc = stack.enter_context(mock.patch.object(ifma, "_apex_partition_size", return_value=8192))
            footer = stack.enter_context(mock.patch.object(ifma, "_add_apex_hashtree_footer",
                                                         side_effect=lambda *_a, **_k: self.events.append("footer")))
            stack.enter_context(mock.patch.object(ifma, "repack_apex_zip",
                                                 side_effect=lambda _src, dst, *_a: Path(dst).write_bytes(b"unsigned")))
            stack.enter_context(mock.patch.object(ifma, "sign_apex_container",
                                                 side_effect=lambda _src, _k, _c, dst, **_kw: Path(dst).write_bytes(b"signed")))
            signed = stack.enter_context(mock.patch.object(ifma, "verify_apex_file", return_value={"ally_adler": self.receipt}))
            legacy = stack.enter_context(mock.patch.object(ifma.PATCH_IFMA, "run"))
            yield calc, footer, signed, legacy

    def test_patch_dispatch_and_rebuilt_readback_precede_footer_and_publication(self):
        with self.patch_fixture() as (calc, footer, signed, legacy):
            report = ifma.patch_apex_file(self.apex, self.rel, self.base / "work", "ot", self.keys, adler_scope=self.scope)
        legacy.assert_not_called()
        self.assertEqual(report["targets"][0]["rel_path"], self.target)
        self.assertEqual((report["targets"][0]["before"], report["targets"][0]["after"]), ("original", "patched"))
        self.assertEqual(report["ally_adler"], self.receipt)
        self.assertEqual(calc.call_args.kwargs, {"partition_name": ""})
        self.assertEqual(footer.call_args.kwargs, {"partition_name": ""})
        self.assertEqual(signed.call_args.kwargs["adler_scope"], self.scope)
        self.assertLess(self.events.index("verify-adler"), self.events.index("footer"))
        self.assertEqual(self.apex.read_bytes(), b"signed")

    def test_appsearch_dispatches_its_distinct_path_and_scope(self):
        self.rel, self.scope, self.target = APPSEARCH, "appsearch", "lib64/libicing_anywhere.so"
        self.receipt["scope"] = self.scope
        self.receipt["targets"][0]["path"] = self.target
        with self.patch_fixture() as (_calc, _footer, signed, legacy):
            report = ifma.patch_apex_file(self.apex, self.rel, self.base / "work", "ot", self.keys, adler_scope=self.scope)
        self.assertEqual(report["targets"][0]["rel_path"], self.target)
        self.assertEqual(signed.call_args.kwargs["adler_scope"], self.scope)
        legacy.assert_not_called()

    def test_changed_set_drift_refuses_before_footer_and_preserves_stock(self):
        self.corrupt_other = True
        with self.patch_fixture() as (_calc, footer, signed, _legacy):
            with self.assertRaisesRegex(SystemExit, "unexpected payload file changes"):
                ifma.patch_apex_file(self.apex, self.rel, self.base / "work", "ot", self.keys, adler_scope=self.scope)
        footer.assert_not_called()
        signed.assert_not_called()
        self.assertEqual(self.apex.read_bytes(), self.original_apex)

    def test_rebuilt_receipt_drift_refuses_before_footer(self):
        self.corrupt_receipt = True
        with self.patch_fixture() as (_calc, footer, signed, _legacy):
            with self.assertRaisesRegex(SystemExit, "rebuilt Adler APEX receipt changed"):
                ifma.patch_apex_file(self.apex, self.rel, self.base / "work", "ot", self.keys, adler_scope=self.scope)
        footer.assert_not_called()
        signed.assert_not_called()
        self.assertEqual(self.apex.read_bytes(), self.original_apex)

    def test_rebuilt_target_hash_refusal_blocks_footer(self):
        module = types.SimpleNamespace(patch_tree=self.patch_adler,
                                       verify_tree=mock.Mock(side_effect=ValueError("exact repaired hash mismatch")),
                                       Refused=ValueError)
        with self.patch_fixture() as (_calc, footer, signed, _legacy), \
                mock.patch.dict(sys.modules, {"ally_adler": module}):
            with self.assertRaisesRegex(SystemExit, "Adler APEX repair refused"):
                ifma.patch_apex_file(self.apex, self.rel, self.base / "work", "ot", self.keys, adler_scope=self.scope)
        footer.assert_not_called()
        signed.assert_not_called()
        self.assertEqual(self.apex.read_bytes(), self.original_apex)

    def test_signed_receipt_drift_refuses_publication(self):
        with self.patch_fixture() as (_calc, _footer, signed, _legacy):
            signed.return_value = {"ally_adler": {"different": "receipt"}}
            with self.assertRaisesRegex(SystemExit, "signed Adler APEX receipt changed"):
                ifma.patch_apex_file(self.apex, self.rel, self.base / "work", "ot", self.keys, adler_scope=self.scope)
        self.assertEqual(self.apex.read_bytes(), self.original_apex)

    def test_verify_dispatches_adler_and_keeps_scanner(self):
        with self.fixture(), mock.patch.object(ifma, "_avbpubkey_to_pem", return_value=b"pem"), \
                mock.patch.object(ifma.PATCH_IFMA, "run") as legacy:
            report = ifma.verify_apex_file(self.apex, self.rel, self.base / "verify", "ot", adler_scope=self.scope)
            ifma.scan_rebuilt_tree.assert_called_once()
            extraction = next(call for call in ifma._run_ot.call_args_list if call.args[1] == "fsck.erofs")
            self.assertIn("--preserve-perms", extraction.args)
        legacy.assert_not_called()
        self.assertEqual(report["ally_adler"], self.receipt)
        self.assertEqual(report["targets"][0]["before"], "patched")
        self.assertEqual(report["targets"][0]["after"], "patched")

    def test_legacy_verify_still_uses_ifma_and_unchanged_extraction(self):
        rel = "apex/com.android.hardware.audio.desktop.apex"
        spec = ifma.APEX_SPECS[rel]
        with self.fixture(), mock.patch.object(ifma, "_avbpubkey_to_pem", return_value=b"pem"), \
                mock.patch.object(ifma, "_parse_apex_avb_info", return_value={"salt": spec["salt"], "apex_key": spec["apex_key"]}), \
                mock.patch.object(ifma.PATCH_IFMA, "run", return_value={"targets": [{"before": "patched", "after": "patched"}]}) as legacy:
            report = ifma.verify_apex_file(self.apex, rel, self.base / "verify-legacy", "ot")
            extraction = next(call for call in ifma._run_ot.call_args_list if call.args[1] == "fsck.erofs")
            self.assertNotIn("--preserve-perms", extraction.args)
        self.assertEqual(legacy.call_args.kwargs["scope"], ("apex", rel))
        self.assertNotIn("ally_adler", report)
        self.assertNotIn("verify-adler", self.events)

    def test_legacy_patch_still_uses_ifma_and_default_footer(self):
        rel = "apex/com.android.hardware.audio.desktop.apex"
        def legacy_run(tree, _manifest, _report, apply, *, scope):
            self.assertTrue(apply)
            self.assertEqual(scope, ("apex", rel))
            (Path(tree) / self.target).write_bytes(b"patched")
            return {"targets": [{"rel_path": self.target, "before": "original", "after": "patched"}]}
        with self.patch_fixture() as (calc, footer, signed, legacy), \
                mock.patch.object(ifma, "_stock_apex_avb_identity", return_value={"salt": "ab", "apex_key": "legacy"}):
            legacy.side_effect = legacy_run
            ifma.patch_apex_file(self.apex, rel, self.base / "legacy", "ot", self.keys)
        self.assertEqual(legacy.call_count, 1)
        self.assertNotIn("patch-adler", self.events)
        self.assertNotIn("verify-adler", self.events)
        self.assertEqual(calc.call_args.kwargs, {})
        self.assertEqual(footer.call_args.kwargs, {})
        self.assertIsNone(signed.call_args.kwargs["adler_scope"])


class SystemTransactionTests(unittest.TestCase):
    @contextmanager
    def transaction(self, prof, *, corrupt_receipt=False, extra_change=False):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            base = Path(temp)
            tmp = base / "tmp"
            tmp.mkdir()
            cert = base / "cert"
            cert.write_bytes(b"certificate")
            snapshot = {}
            events = []
            adler_path = "system/lib64/public-adler-fixture.so"
            enabled = prof.get("cpu_compat", {}).get("patch_ally_bootstrap", False)
            receipt = {"sources": "public fixture", "targets": []}
            all_paths = set(ifma.SYSTEM_EXPECTED_CHANGED) | {RUNTIME, APPSEARCH, adler_path}
            def write(root, path, data):
                path = Path(root) / path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            def run_ot(_ot, tool, *args):
                if tool == "fsck.erofs":
                    root = Path(next(str(a).split("=", 1)[1] for a in args if str(a).startswith("--extract=")))
                    rows = {path: b"original" for path in all_paths} if root.name == "system-tree" else snapshot
                    for path, data in rows.items():
                        write(root, path, data)
                    if extra_change and root.name == "system-tree-new":
                        write(root, RUNTIME, b"unexpected when disabled")
                elif tool == "mkfs.erofs":
                    tree = Path(args[-1])
                    snapshot.update({str(p.relative_to(tree)): p.read_bytes() for p in tree.rglob("*") if p.is_file()})
                    Path(args[-2]).write_bytes(b"rebuilt")
                elif tool == "avbtool":
                    events.append("system-footer")
                    Path(args[args.index("--image") + 1]).write_bytes(b"x" * 4096)
                return ""
            def patch_plain(tree, _manifest, _report, apply, *, scope):
                self.assertEqual(scope, ("partition", "system_a"))
                if apply:
                    for path in set(ifma.SYSTEM_EXPECTED_CHANGED) - {VIRT}:
                        write(tree, path, b"patched")
                return {"targets": [{"before": "original" if apply else "patched", "after": "patched"}]}
            def adler(_prof, scope, tree, *, verify=False):
                self.assertEqual(scope, "system")
                if not enabled:
                    return None
                if not verify:
                    write(tree, adler_path, b"patched")
                return receipt
            def patch_apex(path, rel, _work, _ot, _keys, **kwargs):
                Path(path).write_bytes(b"patched")
                events.append(("patch", rel, kwargs))
                return {"targets": [], "ally_adler": receipt}
            def verify_apex(_path, rel, _work, _ot, **kwargs):
                events.append(("verify", rel, kwargs))
                return {"ally_adler": {"drift": True} if corrupt_receipt and rel == RUNTIME else receipt}
            for name, value in {
                # This suite isolates outer-system/APEX routing; the guest chain
                # has its own executable transaction and scan-binding fixtures.
                "microdroid_preflight": mock.Mock(return_value={"fixture": "guest-tools"}),
                "_GuestChain": mock.Mock(),
                "ensure_apex_keys": mock.Mock(return_value={"container_cert_der": cert}),
                "_gpt_partitions": mock.Mock(return_value={"super": {"start": 0, "size": 4096}}),
                "_lp_Named_extent": mock.Mock(return_value=(0, 4096)),
                "_copy_erofs_extent": mock.Mock(side_effect=lambda _a, _b, _c, dst: Path(dst).write_bytes(b"stock")),
                "_erofs_meta_map": mock.Mock(return_value={}), "_assert_same_erofs_metadata": mock.Mock(),
                "_run_ot": mock.Mock(side_effect=run_ot), "scan_rebuilt_tree": mock.Mock(),
                "ally_adler_tree": mock.Mock(side_effect=adler),
                "ally_adler_changed_paths": mock.Mock(return_value=(adler_path,) if enabled else ()),
                "patch_apex_file": mock.Mock(side_effect=patch_apex), "verify_apex_file": mock.Mock(side_effect=verify_apex),
            }.items():
                stack.enter_context(mock.patch.object(ifma, name, value))
            stack.enter_context(mock.patch.object(ifma.PATCH_IFMA, "run", side_effect=patch_plain))
            stack.enter_context(mock.patch.object(ifma.rebuild, "superblock_params", return_value={
                "uuid": ifma.SYSTEM_UUID, "build_time": 0, "volume_name": "", "pcluster_size": 0}))
            stack.enter_context(mock.patch.object(ifma.rebuild, "fs_uuid", return_value=ifma.SYSTEM_UUID))
            stack.enter_context(mock.patch.object(ifma.rebuild, "write_configs"))
            stack.enter_context(mock.patch.object(ifma.rebuild, "restore_mtimes"))
            if enabled:
                (tmp / "ally-adler-vendor.json").write_text("{}")
            yield tmp, events

    def test_enabled_system_patches_and_verifies_both_extra_apexes_then_reports(self):
        with self.transaction(ALLY) as (tmp, events):
            result = ifma.rebuild_system_partition(ALLY, "fixture-image", tmp, "ot", keys_dir="keys")
            self.assertEqual(result["system_size"], 4096)
            patched = [row[1] for row in events if isinstance(row, tuple) and row[0] == "patch"]
            verified = [row[1] for row in events if isinstance(row, tuple) and row[0] == "verify"]
            self.assertEqual(patched, [VIRT, RUNTIME, APPSEARCH])
            self.assertEqual(verified, [VIRT, RUNTIME, APPSEARCH])
            self.assertLess(events.index(next(e for e in events if isinstance(e, tuple) and e[:2] == ("verify", APPSEARCH))),
                            events.index("system-footer"))
            report = (tmp.parent / "logs/ifma-report.json").read_text()
            self.assertIn('"ally_adler_apexes"', report)
            self.assertIn(RUNTIME, report)
            self.assertIn(APPSEARCH, report)
            for kind, rel, kwargs in [e for e in events if isinstance(e, tuple) and e[1] in (RUNTIME, APPSEARCH)]:
                self.assertEqual(kwargs["adler_scope"], ifma.ALLY_ADLER_APEX_SPECS[rel]["adler_scope"])

    def test_disabled_system_only_legacy_apex_and_no_adler_report(self):
        prof = {"model": {"id": "kano"}, "cpu_compat": {"patch_ifma": True}}
        with self.transaction(prof) as (tmp, events):
            ifma.rebuild_system_partition(prof, "fixture-image", tmp, "ot", keys_dir="keys")
            self.assertEqual([e[:2] for e in events if isinstance(e, tuple)], [("patch", VIRT), ("verify", VIRT)])
            self.assertNotIn('"ally_adler_apexes"', (tmp.parent / "logs/ifma-report.json").read_text())

    def test_disabled_extra_apex_change_rejected_by_exact_changed_set(self):
        prof = {"model": {"id": "kano"}, "cpu_compat": {"patch_ifma": True}}
        with self.transaction(prof, extra_change=True) as (tmp, events):
            with self.assertRaisesRegex(SystemExit, "exact CPU patch set"):
                ifma.rebuild_system_partition(prof, "fixture-image", tmp, "ot", keys_dir="keys")
            self.assertNotIn("system-footer", events)

    def test_final_extra_apex_receipt_drift_blocks_system_footer(self):
        with self.transaction(ALLY, corrupt_receipt=True) as (tmp, events):
            with self.assertRaisesRegex(SystemExit, "final system Adler APEX receipt changed"):
                ifma.rebuild_system_partition(ALLY, "fixture-image", tmp, "ot", keys_dir="keys")
            self.assertNotIn("system-footer", events)


if __name__ == "__main__":
    unittest.main()
