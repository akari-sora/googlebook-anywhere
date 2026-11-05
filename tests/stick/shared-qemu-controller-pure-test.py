#!/usr/bin/env python3
"""Controller-mode routing checks, with no QEMU, devices, or physical writes."""

from contextlib import nullcontext
import importlib.util
import io
from contextlib import redirect_stdout
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guest = load("controller_guest_pure", HERE / "shared-qemu-guest.py")
host = load("controller_host_pure", HERE / "shared-qemu-operation.py")


class Routing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "fixtures").mkdir()
        (self.root / "controller-fresh").mkdir()
        self.payloads = {
            p: {"path": p + ".img", "size": 4, "sha256": "1" * 64}
            for p in guest.PROFILES
        }
        self.raw = json.dumps(
            {
                "payloads": [
                    {"profile": p, "build_id": "16452207", "image": x}
                    for p, x in self.payloads.items()
                ]
            }
        ).encode()
        (self.root / "fixtures" / "fresh-catalog.json").write_bytes(self.raw)
        (self.root / "controller-fresh" / "catalog.json").write_bytes(self.raw)
        self.req = {
            "schema": 1,
            "action": "fresh",
            "mode": "controller",
            "expected_profile": "kano",
            "expected_catalog_sha256": "2" * 64,
            "payloads": self.payloads,
        }
        catalog = types.SimpleNamespace(
            MAX_CATALOG=10000,
            schema=lambda raw: json.loads(raw)["payloads"],
            validate=Mock(return_value=nullcontext()),
        )
        self.shared = types.SimpleNamespace(
            catalog=catalog,
            _observed_profile=Mock(return_value="kano"),
            _target_observation=lambda: (),
            _block_facts=lambda: {},
        )
        self.ctl = types.SimpleNamespace(run=Mock(side_effect=self.controller_run))
        self.answers = None
        patches = [
            patch.object(guest, "BUNDLE", self.root),
            patch.object(guest, "read_only_mount"),
            patch.object(guest, "activate_virtual_nvme"),
            patch.object(guest, "require_virtual_target"),
            patch.object(
                guest,
                "load",
                side_effect=lambda name, path: (
                    self.shared if name == "guest_shared_source" else self.ctl
                ),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def controller_run(self, action, **kw):
        self.answers = (
            kw["snapshot"](),
            kw["enumerate_targets"](),
            kw["read_line"](16),
            kw["read_line"](128),
            kw["shared"],
        )
        return {
            "status": "CANCELLED"
            if self.req["mode"] == "controller-cancel"
            else "COMPLETED",
            "hardware": "NOT_VERIFIED",
        }

    def test_exact_routing_and_cancel(self):
        for mode, expected in [
            ("controller", "INSTALL /dev/nvme0n1\n"),
            ("controller-cancel", "\n"),
        ]:
            with self.subTest(mode=mode):
                self.req["mode"] = mode
                r = guest.run(self.req)
                self.assertEqual(
                    self.answers,
                    (
                        ("kano", "16452207", "2" * 64),
                        ["/dev/nvme0n1"],
                        "1\n",
                        expected,
                        self.shared,
                    ),
                )
                self.assertEqual(
                    r["status"], "CANCELLED" if mode.endswith("cancel") else "COMPLETED"
                )
        self.assertEqual(guest.require_virtual_target.call_count, 2)

    def test_changed_controller_catalog_refuses_before_dispatch(self):
        (self.root / "controller-fresh" / "catalog.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "catalog differs"):
            guest.run(self.req)
        self.ctl.run.assert_not_called()

    def test_unrecognized_target_profile_refuses(self):
        self.shared._observed_profile.side_effect = ValueError("unknown")
        with self.assertRaisesRegex(ValueError, "recognized profile"):
            guest.run(self.req)
        self.ctl.run.assert_not_called()


class Profiler(unittest.TestCase):
    def test_unknown_controller_emits_nested_observations(self):
        def target():
            return tuple(range(14))

        def facts():
            return {"rdev": "240:1"}

        backend = types.SimpleNamespace(_target_observation=target, _block_facts=facts)

        def controller():
            backend._target_observation()
            backend._block_facts()
            return {"status": "UNKNOWN"}

        out = io.StringIO()
        with redirect_stdout(out):
            result = guest.observe_operation(backend, controller)
        self.assertEqual(result, {"status": "UNKNOWN"})
        record = json.loads(out.getvalue().split("GBOS_QEMU_OBSERVATION ", 1)[1])
        self.assertEqual(record["target_observation"]["count"], 1)
        self.assertEqual(record["block_facts"]["count"], 1)


class FixtureStaging(unittest.TestCase):
    def test_fresh_and_update_catalogs_keep_distinct_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fixtures"
            fixture.mkdir()
            result = {}
            for label, catalog, key in [
                ("v1", "fresh-catalog.json", "fresh_payloads"),
                ("v2", "catalog.json", "payloads"),
            ]:
                names = {}
                for profile in guest.PROFILES:
                    name = label + "-" + profile + ".img"
                    (fixture / name).write_text(label + profile)
                    names[profile] = {"path": name}
                (fixture / catalog).write_text(label)
                result[key] = names
            guest.stage_controller_catalogs(root, fixture, result)
            for mode, version, key in [
                ("fresh", "v1", "fresh_payloads"),
                ("update", "v2", "payloads"),
            ]:
                directory = root / ("controller-" + mode)
                self.assertEqual((directory / "catalog.json").read_text(), version)
                for profile, info in result[key].items():
                    self.assertEqual(
                        (directory / info["path"]).read_text(), version + profile
                    )


class HostChecks(unittest.TestCase):
    def test_cancel_requires_zero_all_write_counters_and_exact_result(self):
        zero = {"wr_bytes": 0, "wr_operations": 0, "unmap_operations": 0}
        req = {"mode": "controller-cancel"}
        result = {
            "status": "CANCELLED",
            "controller": {"status": "CANCELLED", "hardware": "NOT_VERIFIED"},
        }
        host.check_controller_result(result, {"stick": zero, "nv": zero}, req, "cancel")
        with self.assertRaises(RuntimeError):
            host.check_controller_result(
                result,
                {"stick": zero, "nv": dict(zero, wr_operations=1)},
                req,
                "cancel",
            )
        with self.assertRaises(RuntimeError):
            host.check_controller_result(
                dict(result, status="UNKNOWN"),
                {"stick": zero, "nv": zero},
                req,
                "cancel",
            )

    def test_controller_success_requires_real_nv_write_and_profile(self):
        zero = {"wr_bytes": 0, "wr_operations": 0, "unmap_operations": 0}
        result = {
            "status": "COMPLETED",
            "profile": "kano",
            "controller": {"status": "COMPLETED", "hardware": "NOT_VERIFIED"},
        }
        req = {"mode": "controller", "expected_profile": "kano"}
        host.check_controller_result(
            result, {"stick": zero, "nv": dict(zero, wr_operations=1)}, req, "ok"
        )
        with self.assertRaises(RuntimeError):
            host.check_controller_result(result, {"stick": zero, "nv": zero}, req, "ok")
        with self.assertRaises(RuntimeError):
            host.check_controller_result(
                dict(result, profile="amd-hx370"),
                {"stick": zero, "nv": dict(zero, wr_operations=1)},
                req,
                "ok",
            )


if __name__ == "__main__":
    unittest.main()
