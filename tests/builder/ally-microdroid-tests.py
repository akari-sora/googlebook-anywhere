#!/usr/bin/env python3
"""Public synthetic unit gates; NOT actual guest AVB/signing acceptance."""

import copy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import ally_microdroid as m  # noqa: E402


class MicrodroidTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prof = {
            "model": {"id": "rog-ally"},
            "cpu_compat": {"patch_ifma": True, "patch_ally_bootstrap": True},
        }
        self.digest = "a" * 64
        self.bc = (
            b'mode = "normal"\nandroidboot.vbmeta.avb_version = "1.4"\n'
            b'androidboot.vbmeta.digest = "'
            + b"b" * 64
            + b'"\nandroidboot.vbmeta.size = 2432\n'
        )
        self.new = self.bc.replace(b"b" * 64, b"a" * 64)

    def test_source_contract(self):
        self.assertEqual(m.source_identity()["contract_sha256"], m.CONTRACT_SHA256)

    def test_enabled_only_ally_and_both_flags(self):
        self.assertTrue(m.enabled(self.prof))
        for device in ("kano", "generic-x86", ""):
            p = copy.deepcopy(self.prof)
            p["model"]["id"] = device
            self.assertFalse(m.enabled(p))
        for flag in ("patch_ifma", "patch_ally_bootstrap"):
            p = copy.deepcopy(self.prof)
            p["cpu_compat"][flag] = False
            self.assertFalse(m.enabled(p))

    def test_boolean_required(self):
        for flag in ("patch_ifma", "patch_ally_bootstrap"):
            p = copy.deepcopy(self.prof)
            p["cpu_compat"][flag] = 1
            with self.assertRaises(m.Refused):
                m.enabled(p)

    def test_disabled_rebuild_does_not_load_tools_or_modify(self):
        original = self.root / "original"
        original.write_bytes(b"untouched")
        with patch.object(m, "_tools", side_effect=AssertionError("must not load")):
            with self.assertRaises(m.Refused):
                m.rebuild_chain({}, self.root, self.root / "scratch", None, {})
        self.assertEqual(original.read_bytes(), b"untouched")
        self.assertFalse((self.root / "scratch").exists())

    def test_wrong_tool_preflight(self):
        for name in ("sign_virt_apex.py", "initrd_bootconfig"):
            (self.root / name).write_text("not tool")
        with self.assertRaises(m.Refused):
            m.source_identity(self.root)

    def test_good_bootconfig(self):
        m._bootconfig(self.bc, self.new, self.digest, 2432)

    def test_bootconfig_negative_matrix(self):
        cases = [
            self.new + self.new,
            self.new.replace(b"2432", b"65536"),
            self.new.replace(b"a" * 64, b"c" * 64),
            self.new.replace(b"1.4", b"1.5"),
            self.new.replace(b"normal", b"debug"),
            self.new.replace(b"androidboot.vbmeta.digest", b"other.digest"),
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(m.Refused):
                m._bootconfig(self.bc, candidate, self.digest, 2432)

    def test_contract_avb_text_all_rows(self):
        c = m._contract()
        for rel in (m.SYSTEM, m.VBMETA, m.KERNEL):
            h, d = m._parse_info(c["files"][rel]["info_image"])
            self.assertEqual(h["Algorithm"], "SHA256_RSA4096")
            self.assertTrue(d)

    def test_duplicate_header(self):
        with self.assertRaises(m.Refused):
            m._parse_info("Flags: 0\nFlags: 0\n")

    def test_duplicate_descriptor_field(self):
        with self.assertRaises(m.Refused):
            m._parse_info(
                "Descriptors:\n    Hash descriptor:\n      Salt: a\n      Salt: a\n"
            )

    def test_unsupported_descriptor(self):
        with self.assertRaises(m.Refused):
            m._parse_info("Descriptors:\n    Chain Partition descriptor:\n")

    def test_duplicate_mixed_missing_descriptors(self):
        row = {"Partition Name": "system"}
        for rows in (
            [("Hashtree descriptor", row)] * 2,
            [("Hash descriptor", row)],
            [],
            [("Hashtree descriptor", row), ("Hash descriptor", row)],
        ):
            with self.subTest(rows=rows), self.assertRaises(m.Refused):
                m._descs(({}, rows), "Hashtree descriptor", ["system"])

    def fixture(self):
        c = m._contract()
        for rel in c["files"]:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(rel.encode())
        files = tuple(sorted((p, m._sha(self.root / p)) for p in c["files"]))
        ctx = m._verified(
            str(self.root.resolve()), files, "kernel-continuity", m._sha(m.__file__)
        )
        base = json.loads((ROOT / "builder/ifma/manifest.json").read_text())
        return ctx, base

    def test_frozen_context(self):
        ctx, _ = self.fixture()
        with self.assertRaises(FrozenInstanceError):
            ctx.source_sha256 = "forged"

    def test_external_receipt_refused(self):
        ctx, base = self.fixture()
        forged = m.VerifiedMicrodroid(
            ctx.payload_root,
            ctx.artifacts,
            ctx.kernel_payload_sha256,
            ctx.source_sha256,
        )
        with self.assertRaises(m.Refused):
            m.policy_overlay(forged, self.root, base)
        with self.assertRaises(m.Refused):
            m.policy_overlay({"ok": True}, self.root, base)

    def test_swapped_or_stale_artifact_refused(self):
        ctx, base = self.fixture()
        (self.root / m.KERNEL).write_bytes(b"swapped")
        with self.assertRaises(m.Refused):
            m.policy_overlay(ctx, self.root, base)

    def test_wrong_tree_context_refused(self):
        ctx, base = self.fixture()
        with self.assertRaises(m.Refused):
            m.policy_overlay(ctx, self.root / "elsewhere", base)

    def test_overlay_only_exact_changes_and_copy(self):
        ctx, base = self.fixture()
        before = copy.deepcopy(base)
        result = m.policy_overlay(ctx, self.root, base)
        self.assertEqual(base, before)
        self.assertEqual(
            result["known_non_instructions"], base["known_non_instructions"]
        )
        self.assertEqual(len(result["residuals"]), len(base["residuals"]) - 1)
        kernel = [
            r
            for r in result["residuals"]
            if any(n["path"] == m.KERNEL for n in r["container_chain"])
        ]
        self.assertEqual(len(kernel), 2)
        for row in kernel:
            self.assertEqual(
                row["container_chain"][-1]["sha256"],
                "ac5b5281c859544b388080331087633e287e1e6217725614a8f28886fde592e3",
            )
            self.assertEqual(
                row["container_chain"][-2]["sha256"], m._sha(self.root / m.KERNEL)
            )

    def test_overlay_provenance_inner_kernel_and_unsupported_drift(self):
        ctx, base = self.fixture()
        for key in ("residuals", "unsupported_allowlist"):
            expected = m._contract()["policy_rows"][key]
            for wanted in expected:
                altered = copy.deepcopy(base)
                row = altered[key][altered[key].index(wanted)]
                row["container_chain"][-1]["path"] = "unexpected"
                with self.assertRaises(m.Refused):
                    m.policy_overlay(ctx, self.root, altered)
        altered = copy.deepcopy(base)
        altered["residuals"] += m._contract()["policy_rows"]["residuals"]
        with self.assertRaises(m.Refused):
            m.policy_overlay(ctx, self.root, altered)

    def test_extra_kernel_legacy_service_inventory(self):
        _, _ = self.fixture()
        c = m._contract()
        for extra in (
            "etc/fs/microdroid_gki-extra_kernel",
            "etc/fs/super.img",
            "etc/service_vm.img",
        ):
            p = self.root / extra
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            with self.assertRaises(m.Refused):
                m._inventory(self.root, c, stock=False)
            p.unlink()

    def test_wrong_stock_original(self):
        self.fixture()
        with self.assertRaises(m.Refused):
            m._inventory(self.root, m._contract(), stock=True)


class TransactionTests(unittest.TestCase):
    """Mock command boundaries only: these tests are not crypto/format proof."""

    def setUp(self):
        from contextlib import ExitStack

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        self.prof = {
            "model": {"id": "rog-ally"},
            "cpu_compat": {"patch_ifma": True, "patch_ally_bootstrap": True},
        }
        self.c = copy.deepcopy(m._contract())
        self.stock, self.candidate = self.root / "stock", self.root / "candidate"
        for root in (self.stock, self.candidate):
            for rel in self.c["files"]:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                size = 65536 if rel == m.VBMETA else 64
                data = b"A" * 16 + (b"B" if root == self.stock else b"C") * (size - 16)
                if rel == m.VBMETA:
                    data = (b"B" if root == self.stock else b"C") * 2432 + b"\0" * (
                        size - 2432
                    )
                path.write_bytes(data)
                self.c["files"][rel]["size"] = size
        self.c["kernel_original_size"] = 16
        self.pairs = {
            rel: m._parse_info(self.c["files"][rel]["info_image"])
            for rel in (m.SYSTEM, m.VBMETA, m.KERNEL)
        }
        for name, desc in m._descs(
            self.pairs[m.KERNEL], "Hash descriptor", ["boot", *m.INITRDS]
        ).items():
            if name in m.INITRDS:
                data = (self.candidate / m.INITRDS[name]).read_bytes()
                desc["Image Size"] = f"{len(data)} bytes"
                desc["Digest"] = (
                    __import__("hashlib")
                    .sha256(bytes.fromhex(desc["Salt"]) + data)
                    .hexdigest()
                )
        # Inputs are fixed synthetic bytes; signature/stock/tool commands mocked.
        self.stack.enter_context(patch.object(m, "_contract", return_value=self.c))
        self.stack.enter_context(
            patch.object(m, "_tools", return_value=(self.root, {}))
        )
        self.stack.enter_context(patch.object(m, "_verify_stock"))
        self.stack.enter_context(patch.object(m, "_verify_images"))
        self.stack.enter_context(
            patch.object(
                m,
                "_info",
                side_effect=lambda ot, p: copy.deepcopy(
                    self.pairs[str(p.relative_to(self.candidate))]
                ),
            )
        )
        import ifma

        self.ifma = ifma
        self.stack.enter_context(patch.object(ifma, "_run_ot", return_value="a" * 64))
        self.stack.enter_context(
            patch.object(
                m,
                "_inventory",
                side_effect=lambda root, c, stock: ifma._index_tree(root),
            )
        )
        bc = (
            b'androidboot.vbmeta.avb_version = "1.4"\nandroidboot.vbmeta.digest = "'
            + b"a" * 64
            + b'"\nandroidboot.vbmeta.size = 2432\n'
        )
        self.stack.enter_context(
            patch.object(m, "_roundtrip", return_value=(b"body", bc))
        )
        self.guest = self.stack.enter_context(patch.object(m, "_guest_readback"))

    def verify(self):
        return m.verify_chain(
            self.prof,
            self.stock,
            self.candidate,
            self.root / "verify",
            None,
            {"payload_key": "existing"},
        )

    def test_mocked_verify_transaction(self):
        result = self.verify()
        result.assert_bound(self.candidate)
        self.assertEqual(result.stock_payload_root, str(self.stock.resolve()))
        self.guest.assert_called_once()

    def test_header_geometry_drift_refused(self):
        for field in (
            "Image size",
            "VBMeta offset",
            "VBMeta size",
            "Authentication Block",
            "Auxiliary Block",
        ):
            old = self.pairs[m.SYSTEM][0].get(field)
            self.pairs[m.SYSTEM][0][field] = "99999999 bytes"
            with self.subTest(field=field), self.assertRaises(m.Refused):
                self.verify()
            self.pairs[m.SYSTEM][0][field] = old
            __import__("shutil").rmtree(self.root / "verify")

    def test_actual_file_size_drift_refused(self):
        with (self.candidate / m.SYSTEM).open("ab") as stream:
            stream.write(b"extra")
        with self.assertRaises(m.Refused):
            self.verify()

    def test_sixth_outer_file_refused(self):
        (self.stock / "other").write_bytes(b"before")
        (self.candidate / "other").write_bytes(b"after")
        with self.assertRaises(m.Refused):
            self.verify()

    def test_nested_runtime_other_file_or_metadata_refused(self):
        import zipfile

        old, new = self.root / "old.apex", self.root / "new.apex"
        for p in (old, new):
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("apex_payload.img", b"synthetic")
        with (
            patch.object(self.ifma, "_strip_avb_footer", return_value=(b"erofs", 0)),
            patch.object(m, "_extract"),
            patch.object(self.ifma, "_assert_same_erofs_metadata") as metadata,
            patch.object(
                self.ifma,
                "_index_tree",
                side_effect=[
                    {"bin/linker64": "a", "other": "a"},
                    {"bin/linker64": "b", "other": "b"},
                ],
            ),
        ):
            with self.assertRaises(m.Refused):
                m._runtime_readback(old, new, self.root / "runtime", None)
            metadata.assert_called_once()
        with (
            patch.object(self.ifma, "_strip_avb_footer", return_value=(b"erofs", 0)),
            patch.object(m, "_extract"),
            patch.object(
                self.ifma,
                "_assert_same_erofs_metadata",
                side_effect=SystemExit("xattr drift"),
            ),
        ):
            with self.assertRaises(SystemExit):
                m._runtime_readback(old, new, self.root / "runtime2", None)

    def test_sixth_guest_file_delta_refused(self):
        expected = {"adler1", "adler2", "adler3", "manager", "runtime"}
        old = {p: "old" for p in expected | {"sixth"}}
        new = {p: "new" for p in old}
        with self.assertRaises(m.Refused):
            m._exact_delta(old, new, expected, "sixth guest file")

    def test_run_timeout_and_closed_stdin(self):
        import subprocess

        with patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")
        ) as command:
            self.assertEqual(m._run(["tool"], {}), "ok")
            self.assertEqual(command.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(command.call_args.kwargs["timeout"], 600)

    def test_nested_runtime_happy_metadata_and_exact_delta(self):
        import zipfile
        import ally_adler

        old, new = self.root / "old.apex", self.root / "new.apex"
        for p in (old, new):
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("apex_payload.img", b"synthetic")
        with (
            patch.object(self.ifma, "_strip_avb_footer", return_value=(b"erofs", 0)),
            patch.object(m, "_extract"),
            patch.object(self.ifma, "_assert_same_erofs_metadata") as metadata,
            patch.object(
                self.ifma,
                "_index_tree",
                side_effect=[
                    {"bin/linker64": "a", "other": "same"},
                    {"bin/linker64": "b", "other": "same"},
                ],
            ),
            patch.object(ally_adler, "verify_tree") as target,
        ):
            m._runtime_readback(old, new, self.root / "runtime", None)
            metadata.assert_called_once()
            target.assert_called_once()

    def rebuild(self, failure=None):
        import ally_adler
        import rebuild
        import zipfile
        import hashlib

        before = self.ifma._index_tree(self.stock)
        self.c["system_original_size"] = 64
        payload = b"runtime-payload"
        apex = self.root / "runtime.apex"
        with zipfile.ZipFile(apex, "w") as z:
            z.writestr("apex_payload.img", payload)
        self.c["runtime"]["sha256"] = hashlib.sha256(apex.read_bytes()).hexdigest()
        self.c["runtime"]["payload_sha256"] = hashlib.sha256(payload).hexdigest()

        def extract(image, tree, ot):
            tree.mkdir()
            runtime = tree / self.c["runtime"]["path"]
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(apex.read_bytes())

        def command(ot, tool, *args):
            if tool == "mkfs.erofs":
                Path(args[-2]).write_bytes(b"new-erofs")
            return ""

        def signer(argv, env):
            if failure == "signer":
                raise m.Refused("synthetic signer failure")
            candidate = Path(argv[-1])
            for rel in self.c["files"]:
                (candidate / rel).write_bytes(b"new " + rel.encode())
            return ""

        def verified(prof, stock, candidate, *args, **kwargs):
            if failure == "verifier":
                raise m.Refused("synthetic verifier failure")
            return m._verified(
                str(candidate.resolve()),
                tuple(sorted((p, m._sha(candidate / p)) for p in self.c["files"])),
                "continuity",
                m._sha(m.__file__),
                str(stock.resolve()),
            )

        original_replace = m.os.replace
        replaced = []

        def replace(source, destination):
            replaced.append((source, destination))
            if failure == "replace" and len(replaced) == 2:
                raise OSError("synthetic SECOND publication replacement failure")
            return original_replace(source, destination)

        with (
            patch.object(m, "_extract", side_effect=extract),
            patch.object(self.ifma, "_strip_avb_footer", return_value=(b"x" * 64, 64)),
            patch.object(
                rebuild,
                "superblock_params",
                return_value={
                    "build_time": 0,
                    "uuid": "synthetic",
                    "volume_name": "",
                    "pcluster_size": None,
                },
            ),
            patch.object(self.ifma, "_erofs_meta_map", return_value={}),
            patch.object(ally_adler, "patch_tree"),
            patch.object(self.ifma.PATCH_IFMA, "run"),
            patch.object(self.ifma, "patch_apex_file"),
            patch.object(rebuild, "write_configs"),
            patch.object(rebuild, "restore_mtimes"),
            patch.object(self.ifma, "_run_ot", side_effect=command),
            patch.object(self.ifma, "_assert_same_erofs_metadata"),
            patch.object(m, "_run", side_effect=signer),
            patch.object(m, "verify_chain", side_effect=verified),
            patch.object(m.os, "replace", side_effect=replace),
        ):
            if failure:
                with self.assertRaises(OSError if failure == "replace" else m.Refused):
                    m.rebuild_chain(
                        self.prof,
                        self.stock,
                        self.root / "rebuild",
                        self.root,
                        {"payload_key": "synthetic"},
                    )
                self.assertEqual(self.ifma._index_tree(self.stock), before)
            else:
                result = m.rebuild_chain(
                    self.prof,
                    self.stock,
                    self.root / "rebuild",
                    self.root,
                    {"payload_key": "synthetic"},
                )
                result.assert_bound(self.stock)
                self.assertEqual(
                    {
                        p
                        for p in before
                        if before[p] != self.ifma._index_tree(self.stock)[p]
                    },
                    set(self.c["files"]),
                )

    def test_mocked_rebuild_transaction(self):
        self.rebuild()

    def test_signer_failure_leaves_original_untouched(self):
        self.rebuild("signer")

    def test_verifier_failure_leaves_original_untouched(self):
        self.rebuild("verifier")

    def test_second_publication_replace_failure_rolls_back_original(self):
        self.rebuild("replace")


if __name__ == "__main__":
    unittest.main()
