#!/usr/bin/env python3
"""Focused IFMA EROFS metadata guards and an optional real-tool round trip."""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))

import ifma
import rebuild


def _extended_inode_with_xattrs(entries, *, mtime_nsec=456_789_123):
    encoded = []
    xsize = 12
    prefixes = sorted(rebuild.PREFIX.items(), key=lambda item: len(item[1]), reverse=True)
    for name, raw in entries:
        for index, prefix in prefixes:
            if prefix and name.startswith(prefix):
                short_name = name[len(prefix):].encode("latin1")
                break
        else:
            index = 0
            short_name = name.encode("latin1")
        entry_size = (4 + len(short_name) + len(raw) + 3) & ~3
        encoded.append((index, short_name, raw, entry_size))
        xsize += entry_size
    xcount = 1 + (xsize - 12) // 4
    data = bytearray(64 + xsize)
    struct.pack_into("<HHH", data, 0, 1, xcount, 0o100644)
    struct.pack_into("<Q", data, 8, 3)
    struct.pack_into("<II", data, 24, 1000, 1000)
    struct.pack_into("<Q", data, 32, 1_726_000_123)
    struct.pack_into("<I", data, 40, mtime_nsec)
    struct.pack_into("<I", data, 44, 1)
    offset = 64 + 12
    for index, short_name, raw, entry_size in encoded:
        struct.pack_into("<BBH", data, offset, len(short_name), index, len(raw))
        start = offset + 4
        data[start:start + len(short_name)] = short_name
        data[start + len(short_name):start + len(short_name) + len(raw)] = raw
        offset += entry_size
    return bytes(data)


def _extended_inode_with_xattr(name, raw):
    return _extended_inode_with_xattrs([(name, raw)])


def _metadata(**overrides):
    value = {
        "file_type": 0o100000,
        "mode": 0o755,
        "uid": 0,
        "gid": 2000,
        "nlink": 1,
        "extended": 1,
        "mtime_sec": 1_726_000_123,
        "mtime_nsec": 456_789_123,
        "selinux": "u:object_r:system_file:s0",
        "caps": None,
        "xattrs": {"security.selinux": b"u:object_r:system_file:s0\0"},
    }
    value.update(overrides)
    return value


class MetadataGuardTests(unittest.TestCase):
    category = "unit"

    def test_unknown_xattr_is_refused_with_raw_name(self):
        data = _extended_inode_with_xattr("user.reviewmeta", b"opaque\x00value")
        with self.assertRaisesRegex(SystemExit, r"unsupported xattr 'user\.reviewmeta' on nid 0"):
            rebuild.inode_meta(data, 4096, 0, 0, 0, strict_metadata=True)

    def test_switch_off_parser_ignores_unknown_xattr(self):
        data = _extended_inode_with_xattr("user.reviewmeta", b"opaque\x00value")
        self.assertEqual(rebuild.inode_meta(data, 4096, 0, 0, 0)["xattrs"], {})

    def test_duplicate_xattr_is_strict_only(self):
        first = b"u:object_r:first:s0\0"
        second = b"u:object_r:second:s0\0"
        data = _extended_inode_with_xattrs([
            ("security.selinux", first),
            ("security.selinux", second),
        ])
        with self.assertRaisesRegex(SystemExit, r"duplicate xattr 'security\.selinux' on nid 0"):
            rebuild.inode_meta(data, 4096, 0, 0, 0, strict_metadata=True)
        permissive = rebuild.inode_meta(data, 4096, 0, 0, 0)
        self.assertEqual(permissive["xattrs"], {"security.selinux": second})
        self.assertEqual(permissive["selinux"], "u:object_r:second:s0")

    def test_invalid_inode_mtime_nanoseconds_is_strict_only(self):
        data = _extended_inode_with_xattrs([], mtime_nsec=1_000_000_000)
        self.assertEqual(rebuild.inode_meta(data, 4096, 0, 0, 0)["mtime_nsec"], 1_000_000_000)
        with self.assertRaisesRegex(SystemExit, "invalid mtime nanoseconds"):
            rebuild.inode_meta(data, 4096, 0, 0, 0, strict_metadata=True)

    def test_known_xattr_raw_value_and_extended_mtime_are_retained(self):
        raw = b"u:object_r:system_file:s0\0"
        info = rebuild.inode_meta(_extended_inode_with_xattr("security.selinux", raw), 4096, 0, 0, 0)
        self.assertEqual(info["xattrs"], {"security.selinux": raw})
        self.assertEqual((info["mtime_sec"], info["mtime_nsec"]), (1_726_000_123, 456_789_123))
        self.assertEqual(info["nlink"], 1)
        self.assertEqual(info["file_type"], 0o100000)

    def test_superblock_features_volume_and_build_time_strictness(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            image = Path(directory) / "fixture.erofs"
            data = bytearray(1024 + 144)
            struct.pack_into("<I", data, 1024, rebuild.EROFS_MAGIC)
            struct.pack_into("<I", data, 1024 + 8, 0x11223344)
            data[1024 + 12] = 12
            struct.pack_into("<I", data, 1024 + 32, 1_000_000_000)
            data[1024 + 48:1024 + 64] = bytes(range(16))
            data[1024 + 64:1024 + 80] = b"IFMA-fixture\0\0\0\0"
            struct.pack_into("<I", data, 1024 + 80, 0x55667788)
            image.write_bytes(data)
            params = rebuild.superblock_params(image)
            self.assertEqual(params["compat"], 0x11223344)
            self.assertEqual(params["incompat"], 0x55667788)
            self.assertEqual(params["volume_name"], "IFMA-fixture")
            with self.assertRaisesRegex(SystemExit, "invalid EROFS build-time nanoseconds"):
                rebuild.superblock_params(image, strict_metadata=True)

    def test_hardlinked_erofs_is_refused_before_rebuild(self):
        with mock.patch.object(rebuild, "load_image", return_value=(b"", 4096, 1, 0, 0)), mock.patch.object(
            rebuild, "walk_image", return_value=({"": _metadata(file_type=0o040000)}, 1)
        ):
            with self.assertRaisesRegex(SystemExit, "has 1 hardlinked entries; handle before rebuild"):
                ifma._erofs_meta_map("system.erofs")

    def test_comparison_rejects_each_new_metadata_field(self):
        base = {"": _metadata(file_type=0o040000), "bin/tool": _metadata()}
        mutations = {
            "file_type": 0o120000,
            "nlink": 2,
            "mtime_sec": 1_726_000_124,
            "mtime_nsec": 456_789_124,
            "xattrs": {"security.selinux": b"u:object_r:other:s0\0"},
        }
        for field, changed in mutations.items():
            with self.subTest(field=field):
                newer = {key: dict(value) for key, value in base.items()}
                newer["bin/tool"][field] = changed
                with mock.patch.object(ifma, "_erofs_meta_map", side_effect=[base, newer]), mock.patch.object(
                    rebuild,
                    "superblock_params",
                    side_effect=[
                        {
                            "uuid": "fixture", "build_time": 1, "build_time_nsec": 2,
                            "compat": 3, "incompat": 4, "volume_name": "fixture",
                            "pcluster_size": 32768,
                        },
                        {
                            "uuid": "fixture", "build_time": 1, "build_time_nsec": 2,
                            "compat": 3, "incompat": 4, "volume_name": "fixture",
                            "pcluster_size": 32768,
                        },
                    ],
                ):
                    with self.assertRaisesRegex(SystemExit, rf"bin/tool.*{field}"):
                        ifma._assert_same_erofs_metadata("old", "new", "fixture")

    def test_comparison_rejects_erofs_feature_and_volume_drift(self):
        inode_map = {"": _metadata(file_type=0o040000)}
        original = {
            "uuid": "fixture", "build_time": 1, "build_time_nsec": 2,
            "compat": 3, "incompat": 4, "volume_name": "original",
            "pcluster_size": 32768,
        }
        for field, changed in (("compat", 5), ("incompat", 6), ("volume_name", "changed")):
            with self.subTest(field=field):
                rebuilt = dict(original)
                rebuilt[field] = changed
                with mock.patch.object(ifma, "_erofs_meta_map", side_effect=[inode_map, inode_map]), mock.patch.object(
                    rebuild, "superblock_params", side_effect=[original, rebuilt],
                ):
                    with self.assertRaisesRegex(SystemExit, rf"superblock metadata drifted.*{field}"):
                        ifma._assert_same_erofs_metadata("old", "new", "fixture")

    def test_compact_inode_uses_superblock_effective_mtime_with_nanoseconds(self):
        data = bytearray(8192)
        struct.pack_into("<Q", data, 1024 + 24, 1_726_000_321)
        struct.pack_into("<I", data, 1024 + 32, 987_654_321)
        struct.pack_into("<HHH", data, 4096, 0, 0, 0o040755)
        struct.pack_into("<HI", data, 4096 + 6, 2, 0)
        meta, hardlinks = rebuild.walk_image("compact.erofs", 0, data, 4096, 1, 0)
        self.assertEqual(hardlinks, 0)
        self.assertEqual((meta[""]["mtime_sec"], meta[""]["mtime_nsec"]), (1_726_000_321, 987_654_321))

    def test_restore_mtimes_uses_seconds_and_nanoseconds(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            root = Path(directory)
            target = root / "tool"
            target.write_bytes(b"elf")
            meta = {
                "": _metadata(file_type=0o040000, mtime_sec=None, mtime_nsec=None),
                "tool": _metadata(mtime_sec=1_726_000_123, mtime_nsec=456_789_123),
            }
            rebuild.restore_mtimes(root, meta)
            self.assertEqual(target.stat().st_mtime_ns, 1_726_000_123_456_789_123)


def _real_tools():
    roots = []
    if os.environ.get("GBOS_OTATOOLS"):
        roots.append(Path(os.environ["GBOS_OTATOOLS"]))
    found = {}
    for name in ("mkfs.erofs", "fsck.erofs", "avbtool", "openssl"):
        candidates = [root / "bin" / name for root in roots] + [root / name for root in roots]
        path = next((candidate for candidate in candidates if candidate.is_file() and os.access(candidate, os.X_OK)), None)
        found[name] = str(path) if path else shutil.which(name)
    return found


REAL_TOOLS = _real_tools()
MISSING_REAL_TOOLS = [name for name, path in REAL_TOOLS.items() if not path]


@unittest.skipIf(
    bool(MISSING_REAL_TOOLS),
    "real-tool metadata test missing: " + ", ".join(MISSING_REAL_TOOLS),
)
class RealToolMetadataTests(unittest.TestCase):
    category = "real-tool"

    def test_real_erofs_and_avb_round_trip(self):
        def run(command):
            try:
                return subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                self.fail(
                    f"real tool failed ({exc.returncode}): {' '.join(map(str, command))}\n"
                    f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
                )

        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            target = source / "tool"
            target.write_bytes(b"real erofs fixture\n")
            target.chmod(0o750)
            os.utime(target, ns=(1_726_000_123_456_789_123,) * 2)
            first = base / "first.erofs"
            run([REAL_TOOLS["mkfs.erofs"], "-T", "1726000000", str(first), str(source)])
            first_meta = ifma._erofs_meta_map(first)
            extracted = base / "extracted"
            run([REAL_TOOLS["fsck.erofs"], f"--extract={extracted}", "--overwrite", "--preserve-perms", str(first)])
            self.assertEqual((extracted / "tool").read_bytes(), target.read_bytes())
            rebuild.restore_mtimes(extracted, first_meta)
            second = base / "second.erofs"
            run([REAL_TOOLS["mkfs.erofs"], "-T", "1726000000",
                 "-U", rebuild.superblock_params(first)["uuid"], str(second), str(extracted)])
            ifma._assert_same_erofs_metadata(first, second, "real-tool fixture")
            avb_image = base / "payload.img"
            shutil.copyfile(second, avb_image)
            avb_key = base / "avb.pem"
            run([REAL_TOOLS["openssl"], "genrsa", "-out", str(avb_key), "4096"])
            avb_public_key = base / "avb-public.pem"
            run([
                REAL_TOOLS["openssl"], "pkey", "-in", str(avb_key),
                "-pubout", "-out", str(avb_public_key),
            ])
            run(
                [
                    REAL_TOOLS["avbtool"], "add_hashtree_footer",
                    "--image", str(avb_image),
                    "--partition_name", "payload",
                    "--partition_size", str(8 * 1024 * 1024),
                    "--key", str(avb_key),
                    "--algorithm", "SHA256_RSA4096",
                    "--hash_algorithm", "sha256",
                    "--block_size", "4096",
                    "--do_not_generate_fec",
                ],
            )
            self.assertEqual(avb_image.stat().st_size, 8 * 1024 * 1024)
            run([
                REAL_TOOLS["avbtool"], "verify_image", "--image", str(avb_image),
                "--key", str(avb_public_key),
            ])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(
        "ifma metadata tests: "
        f"{result.testsRun} run, {len(result.skipped)} skipped, "
        f"{len(result.failures)} failures, {len(result.errors)} errors"
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
