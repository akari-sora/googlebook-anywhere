#!/usr/bin/env python3
"""Product-model candidate: fail-closed options, stock pins and rebuild roundtrip."""
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import devprofile
import ifma
import product_model as pm


def info():
    return "\n".join(("Hashtree descriptor:", "Version of dm-verity: 1", "Data Block Size: 4096 bytes",
                      "Hash Block Size: 4096 bytes", "FEC num roots: 2", "Hash Algorithm: sha256",
                      "Partition Name: product", f"Salt: {pm.SALT}", f"Root Digest: {pm.ROOT_DIGEST}",
                      "Flags: 0", "Prop: com.android.build.product.os_version -> '17'",
                      f"Prop: com.android.build.product.fingerprint -> '{pm.FINGERPRINT}'",
                      "Prop: com.android.build.product.security_patch -> '2026-10-01'"))


class ProductModelTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"model": {"id": "kano", "android_model": pm.MODEL}}
        self.env = {"GBOS_ANDROID_MODEL": "profile", "GBOS_LAPIS_BUILD": pm.BUILD}
        self.original = b"# hi\nro.product.product.model=ASUS Googlebook 14\nro.test=value\n"

    def test_options_and_real_profiles(self):
        self.assertFalse(pm.validate_options(self.profile, {}))
        self.assertTrue(pm.validate_options(self.profile, self.env))
        for name in ("kano", "amd-hx370", "generic-x86"):
            prof = devprofile.load(ROOT / "profiles" / name)
            self.assertEqual(prof["model"]["android_model"], pm.MODEL if name == "kano" else "")
            self.assertFalse(pm.validate_options(prof, {}))
            if name != "kano":
                with self.assertRaises(ValueError):
                    pm.validate_options(prof, self.env)
        for env in ({"GBOS_ANDROID_MODEL": "yes"}, {"GBOS_ANDROID_MODEL": "profile"}):
            with self.assertRaises(ValueError):
                pm.validate_options(self.profile, env)
        with self.assertRaises(ValueError):
            pm.validate_options({"model": {"id": "kano", "android_model": "unexpected"}}, self.env)

    def test_precise_property_and_drift(self):
        self.assertEqual(pm.replace_model(self.original, pm.MODEL),
                         b"# hi\nro.product.product.model=Acer Chromebook Spin 714\nro.test=value\n")
        for data in (b"", self.original.replace(b"Googlebook", b"other"),
                     self.original + b" ro.product.product.model =another\n",
                     self.original.replace(b"model=", b"model ="),
                     self.original.replace(b"14\n", b"14\r\n"),
                     self.original + b"ro.product.product.model\n"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                pm.replace_model(data, pm.MODEL)

    def test_stock_avb_drift(self):
        pm.assert_stock_avb(info())
        for changed in (info().replace(pm.ROOT_DIGEST, "00" * 32),
                        info().replace(pm.SALT, "00" * 32), info().replace("FEC num roots: 2", "FEC num roots: 0"),
                        info().replace("2026-10-01", "2026-09-01"), info() + "\nRoot Digest: " + pm.ROOT_DIGEST):
            with self.subTest(changed=changed), self.assertRaises(SystemExit):
                pm.assert_stock_avb(changed)

    def test_root_vbmeta_product_identity(self):
        root = "Hashtree descriptor:\n  Partition Name: system\n  Root Digest: aa\n" + info()
        pm.assert_root_product_descriptor(root)
        aligned = root.replace("Partition Name: product", "      Partition Name:        product").replace("Root Digest: " + pm.ROOT_DIGEST, "      Root Digest:           " + pm.ROOT_DIGEST)
        pm.assert_root_product_descriptor(aligned)
        for changed in (root.replace(pm.ROOT_DIGEST, "ff" * 32), root.replace("Partition Name: product", "Partition Name: vendor"), root + "\n" + info(),
                        aligned.replace("Partition Name:        product", "Partition Name:        product\n      Partition Name:        product"),
                        aligned.replace("Partition Name:        product", "Partition Name:        product\n      Partition Name:        vendor"),
                        aligned.replace("Root Digest:           " + pm.ROOT_DIGEST, "Root Digest:           " + pm.ROOT_DIGEST + "\n      Root Digest:           " + pm.ROOT_DIGEST)):
            with self.assertRaises(SystemExit):
                pm.assert_root_product_descriptor(changed)

    def test_lp_extent_exclusivity(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "super.img"
            header = bytearray(256)
            header[:4] = b"0PLA"
            struct.pack_into("<I", header, 8, 256)
            struct.pack_into("<I", header, 44, 48)
            struct.pack_into("<III", header, 92, 0, 2, 24)
            def write(second, source=0):
                table = struct.pack("<QIQI", 16, 0, 16, source) + struct.pack("<QIQI", 8, 0, second, 0)
                path.write_bytes(bytes(12288) + header + table)
            write(40)
            pm.assert_exclusive_extent(path, 0, 8192, 8192)
            for second, source in ((24, 0), (40, 1), (16, 0)):
                write(second, source)
                with self.assertRaises(SystemExit):
                    pm.assert_exclusive_extent(path, 0, 8192, 8192)

    def test_rebuild_roundtrip_geometry_and_metadata_refusal(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            image = tmp / "image"
            image.write_bytes(bytes(32768))
            (tmp / "offsets.env").write_text("VENDOR_OFF=0\n")
            sb = {"uuid": pm.UUID, "build_time": 123, "volume_name": "", "pcluster_size": None}
            readback_drift = [False]
            def run_ot(_ot, tool, *args):
                if tool == "avbtool" and args[0] == "info_image":
                    return info()
                if tool == "avbtool" and args[0] == "verify_image":
                    target = Path(args[args.index("--image") + 1])
                    self.assertEqual(target.name, "product.img")
                    self.assertTrue(target.is_file())
                if tool == "avbtool" and args[0] == "add_hashtree_footer":
                    p = Path(args[args.index("--image") + 1])
                    p.write_bytes(p.read_bytes().ljust(8192, b"\0"))
                if tool == "fsck.erofs":
                    tree = Path(next(str(a).split("=", 1)[1] for a in args if str(a).startswith("--extract=")))
                    (tree / "etc").mkdir(parents=True, exist_ok=True)
                    value = self.original if tree.name == "product-tree" else pm.replace_model(self.original, pm.MODEL)
                    (tree / "etc/build.prop").write_bytes(value)
                    (tree / "etc/other").write_bytes(b"drift" if readback_drift[0] and tree.name == "product-tree-new" else b"unchanged")
                if tool == "mkfs.erofs":
                    Path(args[-2]).write_bytes(b"rebuilt")
                return ""
            def copy(_image, _off, _size, out, **_kw):
                Path(out).write_bytes(b"stock")
            patches = [mock.patch.dict(os.environ, self.env), mock.patch.object(pm, "PARTITION_SIZE", 8192),
                       mock.patch.object(pm, "ORIGINAL_SIZE", 5),
                       mock.patch.object(ifma, "_gpt_partitions", return_value={"super": {"start": 4096, "size": 16384}}),
                       mock.patch.object(ifma, "_lp_Named_extent", return_value=(4096, 8192)),
                       mock.patch.object(pm, "assert_exclusive_extent"),
                       mock.patch.object(ifma, "_copy_partition_extent", side_effect=copy),
                       mock.patch.object(ifma, "_copy_erofs_extent", side_effect=copy),
                       mock.patch.object(ifma, "_run_ot", side_effect=run_ot),
                       mock.patch.object(ifma, "_erofs_meta_map", return_value={}),
                       mock.patch.object(pm.rebuild, "superblock_params", return_value=sb),
                       mock.patch.object(pm.rebuild, "fs_uuid", return_value=pm.UUID),
                       mock.patch.object(pm.rebuild, "write_configs"), mock.patch.object(pm.rebuild, "restore_mtimes"),
                       mock.patch.object(ifma, "_assert_same_erofs_metadata")]
            import contextlib
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                result = pm.rebuild_product_partition(self.profile, image, tmp, tmp)
                self.assertEqual((result["product_off"], result["product_size"]), (8192, 8192))
                self.assertIn("PRODUCT_OFF=8192\nPRODUCT_SIZE=8192\n", (tmp / "offsets.env").read_text())
                with mock.patch.object(ifma, "_lp_Named_extent", return_value=(12288, 8192)):
                    with self.assertRaises(SystemExit):
                        pm.rebuild_product_partition(self.profile, image, tmp, tmp)
                with mock.patch.object(ifma, "_assert_same_erofs_metadata", side_effect=SystemExit("metadata")):
                    with self.assertRaises(SystemExit):
                        pm.rebuild_product_partition(self.profile, image, tmp, tmp)
                readback_drift[0] = True
                with self.assertRaisesRegex(SystemExit, "tree changed"):
                    pm.rebuild_product_partition(self.profile, image, tmp, tmp)


if __name__ == "__main__":
    unittest.main()
