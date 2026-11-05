#!/usr/bin/env python3
"""Synthetic topology tests; never opens host block devices."""

import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "shared_block_test", ROOT / "install/stick/shared-source.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.node = self.root / "devices/disk"
        self.node.mkdir(parents=True)
        for name in ("holders", "slaves"):
            (self.node / name).mkdir()
        (self.node / "dev").write_text("8:0\n")
        (self.node / "size").write_text("2048\n")
        self.part = self.node / "disk1"
        self.part.mkdir()
        (self.part / "holders").mkdir()
        (self.part / "partition").write_text("1\n")
        (self.part / "dev").write_text("8:1\n")
        self.classes = self.root / "class"
        self.classes.mkdir()
        (self.classes / "disk").symlink_to(self.node)
        (self.classes / "disk1").symlink_to(self.part)
        self.mount = self.root / "mountinfo"
        self.mount.write_text("1 0 0:1 / / rw - tmpfs tmpfs rw\n")
        self.swaps = self.root / "swaps"
        self.swaps.write_text("Filename Type Size Used Priority\n")
        self.source = (self.root / "source").open("w+b")
        self.addCleanup(self.source.close)
        self.target_fd = 234567
        self.disk = type("Disk", (), {"fd": self.target_fd, "size": 1048576})()
        st = type(
            "Stat",
            (),
            {
                "st_mode": stat.S_IFBLK | 0o600,
                "st_rdev": os.makedev(8, 0),
                "st_dev": 42,
                "st_ino": 123,
            },
        )()
        real_fstat, real_stat, real_lseek = os.fstat, os.stat, os.lseek

        def fstat(fd):
            return st if fd == self.target_fd else real_fstat(fd)

        def stat_path(path, *args, **kwargs):
            return (
                st
                if str(path) == "/dev/test-disk"
                else real_stat(path, *args, **kwargs)
            )

        def lseek(fd, *args):
            return 1048576 if fd == self.target_fd else real_lseek(fd, *args)

        real_path = m.Path

        def paths(path):
            return {
                "/sys/class/block": self.classes,
                "/proc/self/mountinfo": self.mount,
                "/proc/1/mountinfo": self.mount,
                "/proc/swaps": self.swaps,
            }.get(str(path), real_path(path))

        def sysdev(number):
            if number == "8:0":
                return self.node
            if number == "8:1":
                return self.part
            raise ValueError("unknown topology")

        for obj, attr, side in [
            (m.os, "fstat", fstat),
            (m.os, "stat", stat_path),
            (m.os, "lseek", lseek),
            (m, "Path", paths),
            (m, "_sys_device", sysdev),
            (m, "_filesystem_ancestors", lambda number: {"9:0"}),
            (m, "_read_bounded", lambda path, limit: paths(path).read_text()),
        ]:
            p = patch.object(obj, attr, side_effect=side)
            p.start()
            self.addCleanup(p.stop)

    def observe(self):
        return m._target_observation(
            self.disk, "/dev/test-disk", (self.source.fileno(),)
        )

    def test_whole_and_partition_inventory(self):
        self.assertEqual(self.observe()[0], "8:0")
        (self.node / "partition").write_text("1\n")
        with self.assertRaisesRegex(ValueError, "whole"):
            self.observe()

    def test_mounted_partition_and_holders_refused(self):
        self.mount.write_text("1 0 8:1 / /mnt rw - ext4 /dev/disk1 rw\n")
        with self.assertRaisesRegex(ValueError, "mounted"):
            self.observe()
        self.mount.write_text("1 0 0:1 / / rw - tmpfs tmpfs rw\n")
        (self.part / "holders/dm-0").touch()
        with self.assertRaisesRegex(ValueError, "holders"):
            self.observe()

    def test_source_alias_and_unknown_refused(self):
        with patch.object(m, "_filesystem_ancestors", return_value={"8:1"}):
            with self.assertRaisesRegex(ValueError, "source is on target"):
                self.observe()
        with patch.object(
            m, "_filesystem_ancestors", side_effect=ValueError("unknown topology")
        ):
            with self.assertRaisesRegex(ValueError, "unknown topology"):
                self.observe()

    def test_loop_backing_unknown_or_source_alias_refused(self):
        loop = self.node / "loop"
        loop.mkdir()
        with self.assertRaisesRegex(ValueError, "unknown loop backing"):
            self.observe()
        (loop / "backing_file").write_text(str(self.root / "source") + "\n")
        with patch.object(m, "_ancestors", return_value={"8:0"}):
            with self.assertRaisesRegex(ValueError, "aliases loop backing"):
                self.observe()

    def test_target_observation_changes_when_path_inode_changes(self):
        first = self.observe()
        replacement = type(
            "Stat",
            (),
            {
                "st_mode": stat.S_IFBLK | 0o600,
                "st_rdev": os.makedev(8, 0),
                "st_dev": 42,
                "st_ino": 124,
            },
        )()
        original = m.os.stat

        def changed(path, *args, **kwargs):
            return (
                replacement
                if str(path) == "/dev/test-disk"
                else original(path, *args, **kwargs)
            )

        with patch.object(m.os, "stat", side_effect=changed):
            self.assertNotEqual(first, self.observe())

    def test_size_and_malformed_mount_refused(self):
        (self.node / "size").write_text("2049\n")
        with self.assertRaisesRegex(ValueError, "size changed"):
            self.observe()
        (self.node / "size").write_text("2048\n")
        self.mount.write_text("bad\n")
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.observe()


class PlanningOrderTests(unittest.TestCase):
    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "requires Linux procfs")
    def test_scratch_path_replacement_does_not_redirect_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "scratch"
            original.mkdir()
            fd = os.open(original, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fstat(fd)  # represents the completed backing-device observation
                original.rename(root / "observed")
                original.mkdir()
                with m._pinned_scratch(fd) as pinned:
                    Path(pinned, "proof").write_bytes(b"anchored")
                    self.assertEqual(list(original.iterdir()), [])
                    entries = list((root / "observed").iterdir())
                    self.assertEqual(len(entries), 1)
                    self.assertEqual((entries[0] / "proof").read_bytes(), b"anchored")
                self.assertEqual(list((root / "observed").iterdir()), [])
            finally:
                os.close(fd)

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "requires Linux procfs")
    def test_plan_uses_observed_directory_after_scratch_path_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "catalog.json").write_text("{}")
            scratch = root / "scratch"
            scratch.mkdir()
            disk = type(
                "Disk", (), {"fd": os.open("/dev/null", os.O_RDONLY), "blk": True}
            )()

            class Verified:
                payloads = [{"profile": "kano"}]
                descriptors = {"kano": disk.fd}

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

            def observe(*args):
                scratch.rename(root / "observed")
                scratch.mkdir()
                return "observed"

            def binding(*args, **kwargs):
                path = Path(args[4])
                (path / "proof").write_bytes(b"anchored")
                self.assertEqual(list(scratch.iterdir()), [])
                self.assertEqual(len(list((root / "observed").iterdir())), 1)
                self.assertEqual(kwargs["preview_tmp"], str(path))
                raise LookupError("scratch verified")

            with (
                patch.object(
                    m.detector,
                    "load_rules",
                    return_value=[("kano", {"sys_vendor": "Google"})],
                ),
                patch.object(
                    m.detector, "read_identity", return_value={"sys_vendor": "Google"}
                ),
                patch.object(
                    m,
                    "safe_open_relative",
                    side_effect=lambda rootfd, path: os.open(
                        path, os.O_RDONLY, dir_fd=rootfd
                    ),
                ),
                patch.object(m.catalog, "validate", return_value=Verified()),
                patch.object(m.catalog, "verify_file"),
                patch.object(m.update, "Disk", return_value=disk),
                patch.object(m, "_target_observation", side_effect=observe),
                patch.object(m.update, "SharedBinding", side_effect=binding),
                patch.dict(os.environ, {"TMPDIR": str(scratch)}),
            ):
                with self.assertRaisesRegex(LookupError, "scratch verified"):
                    m.plan_observed_block(
                        str(root), "catalog.json", "0" * 64, "/dev/example"
                    )
            self.assertEqual(list((root / "observed").iterdir()), [])
            self.assertEqual(list(scratch.iterdir()), [])

    def test_topology_refusal_precedes_scratch_creation_and_source_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "catalog.json").write_text("{}")
            disk = type(
                "Disk", (), {"fd": os.open("/dev/null", os.O_RDONLY), "blk": True}
            )()

            class Verified:
                payloads = [{"profile": "kano"}]
                descriptors = {"kano": disk.fd}

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

            with (
                patch.object(
                    m.detector,
                    "load_rules",
                    return_value=[("kano", {"sys_vendor": "Google"})],
                ),
                patch.object(
                    m.detector, "read_identity", return_value={"sys_vendor": "Google"}
                ),
                patch.object(
                    m,
                    "safe_open_relative",
                    side_effect=lambda rootfd, path: os.open(
                        path, os.O_RDONLY, dir_fd=rootfd
                    ),
                ),
                patch.object(m.catalog, "validate", return_value=Verified()),
                patch.object(m.catalog, "verify_file"),
                patch.object(m.update, "Disk", return_value=disk) as open_disk,
                patch.object(
                    m, "_target_observation", side_effect=ValueError("unknown backing")
                ),
                patch.object(m, "_pinned_scratch") as scratch,
                patch.object(m.update, "SharedBinding") as binding,
                patch.dict(os.environ, {"TMPDIR": str(root)}),
            ):
                with self.assertRaisesRegex(ValueError, "unknown backing"):
                    m.plan_observed_block(
                        str(root), "catalog.json", "0" * 64, "/dev/example"
                    )
            open_disk.assert_called_once_with("/dev/example", write=False)
            scratch.assert_not_called()
            binding.assert_not_called()
            with self.assertRaises(OSError):
                os.fstat(disk.fd)


if __name__ == "__main__":
    unittest.main()
