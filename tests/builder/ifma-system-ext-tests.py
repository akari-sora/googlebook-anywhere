#!/usr/bin/env python3
"""Focused system_ext_a IFMA rebuild and fail-closed integration tests."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import ifma


def _real_otatools():
    root = os.environ.get("GBOS_OTATOOLS")
    if not root:
        return None
    root = Path(root)
    required = ("mkfs.erofs", "fsck.erofs", "avbtool")
    if not all((root / "bin" / tool).is_file() for tool in required):
        return None
    return root


REAL_OTATOOLS = _real_otatools()


class SystemExtRebuildTests(unittest.TestCase):
    category = "synthetic"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.image = self.base / "stage.img"
        self.image.write_bytes(b"\0" * 32768)
        self.tmp = self.base / "vendor/tmp"
        self.tmp.mkdir(parents=True)
        (self.tmp / "offsets.env").write_text("VENDOR_OFF=0\n", encoding="utf-8")
        self.manifest = self.base / "manifest.json"
        self.manifest.write_text('{"schema":1,"targets":[]}\n', encoding="utf-8")
        self.profile = {"cpu_compat": {"patch_ifma": True}}
        self.stock = b"stock trusty launcher"
        self.patched = b"patched trusty launch"
        self.ot_calls = []
        self.scan_calls = []
        self.metadata_checks = []
        self.extra_readback = False

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def superblock():
        return {
            "uuid": ifma.SYSTEM_EXT_UUID,
            "build_time": 1230768000,
            "build_time_nsec": 0,
            "compat": 0,
            "incompat": 1,
            "volume_name": "",
            "pcluster_size": None,
        }

    def fake_run_ot(self, _otatools, tool, *args):
        self.ot_calls.append((tool, args))
        if tool == "fsck.erofs":
            destination = Path(next(arg.split("=", 1)[1] for arg in args if str(arg).startswith("--extract=")))
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / "bin/trusty_security_vm_launcher"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.stock if destination.name == "system-ext-tree" else self.patched)
            (destination / "etc/unchanged").parent.mkdir(parents=True, exist_ok=True)
            (destination / "etc/unchanged").write_bytes(b"same")
            if self.extra_readback and destination.name == "system-ext-tree-new":
                (destination / "etc/extra").write_bytes(b"unexpected")
        elif tool == "mkfs.erofs":
            Path(args[-2]).write_bytes(b"rebuilt erofs")
        elif tool == "avbtool":
            if args[0] == "info_image":
                return "\n".join((
                    "    Hashtree descriptor:",
                    "    Version of dm-verity: 1",
                    "    Data Block Size: 4096 bytes",
                    "    Hash Block Size: 4096 bytes",
                    "    FEC num roots: 2",
                    "    Hash Algorithm: sha256",
                    "    Partition Name: system_ext",
                    f"    Salt: {ifma.SYSTEM_EXT_SALT}",
                    "    Flags: 0",
                    f"    Prop: com.android.build.system_ext.os_version -> '{ifma.SYSTEM_EXT_OS_VERSION}'",
                    f"    Prop: com.android.build.system_ext.fingerprint -> '{ifma.SYSTEM_EXT_FINGERPRINT}'",
                    f"    Prop: com.android.build.system_ext.security_patch -> '{ifma.SYSTEM_EXT_SECURITY_PATCH}'",
                ))
            image = Path(args[args.index("--image") + 1])
            partition_size = int(args[args.index("--partition_size") + 1])
            with image.open("ab") as stream:
                stream.write(b"\0" * (partition_size - image.stat().st_size))
        return ""

    def fake_patch(self, tree, _manifest, _report, apply, scope=None):
        self.assertEqual(scope, ("partition", "system_ext_a"))
        target = Path(tree) / "bin/trusty_security_vm_launcher"
        if apply:
            self.assertEqual(target.read_bytes(), self.stock)
            target.write_bytes(self.patched)
            before, after = "original", "patched"
        else:
            self.assertEqual(target.read_bytes(), self.patched)
            before = after = "patched"
        return {"targets": [{"path": "system_ext/bin/trusty_security_vm_launcher",
                              "before": before, "after": after}]}

    def fake_scan(self, tree, label, _otatools, _scratch, **kwargs):
        self.scan_calls.append((Path(tree).name, label, kwargs["provenance"]))
        return {"accepted_residuals": []}

    def run_rebuild(self):
        def copy_extent(_image, _offset, _size, destination, *, partition_name):
            self.assertEqual(partition_name, "system_ext_a")
            Path(destination).write_bytes(b"stock erofs")

        patches = (
            mock.patch.object(ifma, "_gpt_partitions", return_value={"super": {"start": 4096, "size": 16384}}),
            mock.patch.object(ifma, "_lp_Named_extent", return_value=(4096, 8192)),
            mock.patch.object(ifma, "_copy_erofs_extent", side_effect=copy_extent),
            mock.patch.object(ifma.rebuild, "superblock_params", return_value=self.superblock()),
            mock.patch.object(ifma, "_erofs_meta_map", return_value={"": {}}),
            mock.patch.object(ifma.rebuild, "write_configs", side_effect=lambda _m, _t, _meta, fc, fs, **_kw:
                              (Path(fc).write_text("fc\n"), Path(fs).write_text("fs\n"))),
            mock.patch.object(ifma.rebuild, "restore_mtimes"),
            mock.patch.object(ifma.rebuild, "fs_uuid", return_value=ifma.SYSTEM_EXT_UUID),
            mock.patch.object(
                ifma,
                "_assert_same_erofs_metadata",
                side_effect=lambda *args, **kwargs: self.metadata_checks.append((args, kwargs)),
            ),
            mock.patch.object(ifma, "_run_ot", side_effect=self.fake_run_ot),
            mock.patch.object(ifma.PATCH_IFMA, "run", side_effect=self.fake_patch),
            mock.patch.object(ifma, "scan_rebuilt_tree", side_effect=self.fake_scan),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
            return ifma.rebuild_system_ext_partition(
                self.profile, self.image, self.tmp, otatools=self.base / "otatools",
                manifest_path=self.manifest,
            )

    def test_rebuild_patches_exactly_one_file_preserves_identity_and_adds_footer(self):
        result = self.run_rebuild()
        self.assertEqual(result["system_ext_off"], 8192)
        self.assertEqual(result["system_ext_size"], 8192)
        self.assertEqual(result["system_ext_img"].stat().st_size, 8192)
        self.assertIn("SYSTEM_EXT_OFF=8192\nSYSTEM_EXT_SIZE=8192\n", (self.tmp / "offsets.env").read_text())
        avb_args = next(args for tool, args in self.ot_calls if tool == "avbtool" and args[0] == "add_hashtree_footer")
        self.assertEqual(avb_args[avb_args.index("--partition_name") + 1], "system_ext")
        self.assertEqual(avb_args[avb_args.index("--salt") + 1], ifma.SYSTEM_EXT_SALT)
        self.assertEqual(avb_args[avb_args.index("--hash_algorithm") + 1], "sha256")
        self.assertEqual(avb_args[avb_args.index("--block_size") + 1], "4096")
        self.assertEqual(avb_args[avb_args.index("--fec_num_roots") + 1], "2")
        self.assertIn(f"com.android.build.system_ext.fingerprint:{ifma.SYSTEM_EXT_FINGERPRINT}", avb_args)
        self.assertEqual(
            self.scan_calls,
            [
                ("system-ext-tree", "system_ext_a patched", {"partition": "system_ext_a", "container_chain": []}),
                ("system-ext-tree-new", "system_ext_a rebuilt", {"partition": "system_ext_a", "container_chain": []}),
            ],
        )
        self.assertEqual(len(self.metadata_checks), 1)
        self.assertEqual(self.metadata_checks[0][0][0].name, "system_ext.erofs.src")
        self.assertEqual(self.metadata_checks[0][0][1].name, "system_ext.erofs")
        self.assertEqual(self.metadata_checks[0][0][2], "system_ext_a")
        self.assertEqual(self.metadata_checks[0][1], {})
        report = json.loads((self.tmp.parent / "logs/ifma-report.json").read_text())
        self.assertEqual(report["system_ext_uuid"], ifma.SYSTEM_EXT_UUID)
        self.assertEqual(len(report["system_ext_plain"]), 1)

    def test_stock_avb_property_drift_is_refused_before_extraction(self):
        stock_info = self.fake_run_ot

        def drifted_info(otatools, tool, *args):
            if tool == "avbtool" and args[0] == "info_image":
                return "\n".join((
                    "Hashtree descriptor:",
                    "Version of dm-verity: 1",
                    "Data Block Size: 4096 bytes",
                    "Hash Block Size: 4096 bytes",
                    "FEC num roots: 2",
                    "Hash Algorithm: sha256",
                    "Partition Name: system_ext",
                    f"Salt: {ifma.SYSTEM_EXT_SALT}",
                    "Flags: 0",
                    f"Prop: com.android.build.system_ext.os_version -> '{ifma.SYSTEM_EXT_OS_VERSION}'",
                    "Prop: com.android.build.system_ext.fingerprint -> 'unexpected'",
                    f"Prop: com.android.build.system_ext.security_patch -> '{ifma.SYSTEM_EXT_SECURITY_PATCH}'",
                ))
            return stock_info(otatools, tool, *args)

        with mock.patch.object(ifma, "_gpt_partitions", return_value={"super": {"start": 4096, "size": 16384}}), \
             mock.patch.object(ifma, "_lp_Named_extent", return_value=(4096, 8192)), \
             mock.patch.object(ifma, "_run_ot", side_effect=drifted_info), \
             mock.patch.object(ifma, "_copy_erofs_extent") as copy_erofs:
            with self.assertRaisesRegex(SystemExit, "stock AVB identity drifted"):
                ifma.rebuild_system_ext_partition(
                    self.profile, self.image, self.tmp, otatools=self.base / "otatools",
                    manifest_path=self.manifest,
                )
            copy_erofs.assert_not_called()
            self.assertFalse((self.tmp / "system_ext.stock.img").exists())

    def test_stock_avb_hashtree_parameter_drift_is_refused_before_extraction(self):
        fields = {
            "Version of dm-verity": "2",
            "Data Block Size": "8192 bytes",
            "Hash Block Size": "8192 bytes",
            "FEC num roots": "0",
            "Hash Algorithm": "sha512",
            "Flags": "1",
        }
        for field, bad_value in fields.items():
            with self.subTest(field=field):
                def drifted_info(otatools, tool, *args, field=field, bad_value=bad_value):
                    result = self.fake_run_ot(otatools, tool, *args)
                    if tool == "avbtool" and args[0] == "info_image":
                        lines = result.splitlines()
                        return "\n".join(
                            f"{line.split(':', 1)[0]}: {bad_value}"
                            if line.strip().startswith(field + ":") else line
                            for line in lines
                        )
                    return result

                with mock.patch.object(ifma, "_run_ot", side_effect=drifted_info):
                    with self.assertRaisesRegex(SystemExit, "stock AVB identity drifted"):
                        ifma._assert_system_ext_stock_avb(
                            self.base / "otatools", self.base / "system_ext.img"
                        )

    def test_rebuild_refuses_any_second_changed_path(self):
        self.extra_readback = True
        with self.assertRaisesRegex(SystemExit, "expected 1 IFMA file.*etc/extra"):
            self.run_rebuild()

    def test_extent_outside_super_is_refused_before_copy(self):
        with mock.patch.object(ifma, "_gpt_partitions", return_value={"super": {"start": 4096, "size": 4096}}), \
             mock.patch.object(ifma, "_lp_Named_extent", return_value=(2048, 4096)), \
             mock.patch.object(ifma, "_copy_erofs_extent") as copy:
            with self.assertRaisesRegex(SystemExit, "system_ext_a extent runs past"):
                ifma.rebuild_system_ext_partition(
                    self.profile, self.image, self.tmp, otatools=self.base / "otatools",
                    manifest_path=self.manifest,
                )
            copy.assert_not_called()

    def test_switch_off_is_noop(self):
        self.assertIsNone(ifma.rebuild_system_ext_partition(
            {"cpu_compat": {"patch_ifma": False}}, self.image, self.tmp,
            otatools=self.base / "otatools", manifest_path=self.manifest,
        ))
        self.assertFalse((self.tmp / "system_ext.img").exists())


class KeyVerificationTests(unittest.TestCase):
    category = "unit"

    def test_create_false_refuses_to_generate_a_missing_keyset(self):
        with tempfile.TemporaryDirectory() as directory:
            keys = Path(directory) / "keys"
            with self.assertRaisesRegex(SystemExit, "refusing creation during verification"):
                ifma.ensure_apex_keys(keys, create=False)
            self.assertFalse(keys.exists())


@unittest.skipIf(REAL_OTATOOLS is None, "real system_ext test requires GBOS_OTATOOLS")
class RealToolSystemExtTests(unittest.TestCase):
    category = "real-tool"

    def test_real_erofs_extent_metadata_and_avb_round_trip(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory)
            source = base / "source"
            target = source / "bin/trusty_security_vm_launcher"
            target.parent.mkdir(parents=True)
            stock = b"S" * 64
            patched = b"P" * 64
            target.write_bytes(stock)
            target.chmod(0o755)

            file_contexts = base / "file_contexts"
            file_contexts.write_text(
                "/system_ext u:object_r:system_file:s0\n"
                "/system_ext/bin u:object_r:system_file:s0\n"
                "/system_ext/bin/trusty_security_vm_launcher u:object_r:system_file:s0\n",
                encoding="utf-8",
            )
            fs_config = base / "fs_config"
            fs_config.write_text(
                " 0 0 0755\n"
                "system_ext/bin 0 0 0755\n"
                "system_ext/bin/trusty_security_vm_launcher 0 0 0755\n",
                encoding="utf-8",
            )
            erofs = base / "stock.erofs"
            ifma._run_ot(
                REAL_OTATOOLS,
                "mkfs.erofs",
                "-z", "lz4hc,level=9",
                "-T", "1230768000",
                "-U", ifma.SYSTEM_EXT_UUID,
                "--mount-point", "/system_ext",
                "--file-contexts", file_contexts,
                "--fs-config-file", fs_config,
                erofs,
                source,
            )

            partition_size = 8 * 1024 * 1024
            stock_partition = base / "stock-system_ext.img"
            stock_partition.write_bytes(erofs.read_bytes())
            ifma._run_ot(
                REAL_OTATOOLS,
                "avbtool",
                "add_hashtree_footer",
                "--image", stock_partition,
                "--partition_name", "system_ext",
                "--partition_size", str(partition_size),
                "--hash_algorithm", "sha256",
                "--salt", ifma.SYSTEM_EXT_SALT,
                "--block_size", "4096",
                "--fec_num_roots", "2",
                "--prop", f"com.android.build.system_ext.os_version:{ifma.SYSTEM_EXT_OS_VERSION}",
                "--prop", f"com.android.build.system_ext.fingerprint:{ifma.SYSTEM_EXT_FINGERPRINT}",
                "--prop", f"com.android.build.system_ext.security_patch:{ifma.SYSTEM_EXT_SECURITY_PATCH}",
            )
            self.assertEqual(stock_partition.stat().st_size, partition_size)

            super_start = 4096
            relative_offset = 4096
            partition_offset = super_start + relative_offset
            stage = base / "stage.img"
            stage.write_bytes(b"\0" * partition_offset + stock_partition.read_bytes())
            tmp = base / "vendor/tmp"
            tmp.mkdir(parents=True)
            (tmp / "offsets.env").write_text("VENDOR_OFF=0\n", encoding="utf-8")
            manifest = base / "manifest.json"
            manifest.write_text('{"schema":1,"targets":[]}\n', encoding="utf-8")

            def patch_target(tree, _manifest, _report, apply, scope=None):
                self.assertEqual(scope, ("partition", "system_ext_a"))
                path = Path(tree) / "bin/trusty_security_vm_launcher"
                if apply:
                    self.assertEqual(path.read_bytes(), stock)
                    path.write_bytes(patched)
                    before, after = "original", "patched"
                else:
                    self.assertEqual(path.read_bytes(), patched)
                    before = after = "patched"
                return {"targets": [{
                    "path": "system_ext/bin/trusty_security_vm_launcher",
                    "before": before,
                    "after": after,
                }]}

            with mock.patch.object(
                ifma, "_gpt_partitions",
                return_value={"super": {"start": super_start, "size": relative_offset + partition_size}},
            ), mock.patch.object(
                ifma, "_lp_Named_extent", return_value=(relative_offset, partition_size),
            ), mock.patch.object(
                ifma.PATCH_IFMA, "run", side_effect=patch_target,
            ), mock.patch.object(
                ifma, "scan_rebuilt_tree", return_value={"accepted_residuals": []},
            ):
                result = ifma.rebuild_system_ext_partition(
                    {"cpu_compat": {"patch_ifma": True}},
                    stage,
                    tmp,
                    otatools=REAL_OTATOOLS,
                    manifest_path=manifest,
                )

            rebuilt_partition = result["system_ext_img"]
            ifma._assert_system_ext_stock_avb(REAL_OTATOOLS, rebuilt_partition)
            rebuilt_erofs = base / "rebuilt.erofs"
            ifma._copy_erofs_extent(
                rebuilt_partition,
                0,
                partition_size,
                rebuilt_erofs,
                partition_name="system_ext_a",
            )
            extracted = base / "rebuilt-tree"
            extracted.mkdir()
            ifma._run_ot(
                REAL_OTATOOLS,
                "fsck.erofs",
                f"--extract={extracted}",
                "--overwrite",
                "--preserve-perms",
                rebuilt_erofs,
            )
            self.assertEqual(
                (extracted / "bin/trusty_security_vm_launcher").read_bytes(),
                patched,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
