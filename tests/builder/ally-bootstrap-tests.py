#!/usr/bin/env python3
"""Public synthetic composition/container tests; no private ELF or boot claim."""
import hashlib
import json
from pathlib import Path
import stat
import sys
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import ally_bootstrap as bootstrap  # noqa: E402


def sha(data):
    return hashlib.sha256(data).hexdigest()


def record(name, data=b"", *, ino=1, mode=stat.S_IFREG | 0o755, nlink=1, uid=0, gid=0):
    raw = name.encode() + b"\0"
    values = [ino, mode, uid, gid, nlink, 12345, len(data), 0, 0, 0, 0, len(raw), 0]
    result = b"070701" + b"".join(f"{value:08x}".encode() for value in values) + raw
    result += bytes(-len(result) % 4)
    result += data
    return result + bytes(-len(result) % 4)


PARENTS = ("first_stage_ramdisk", "first_stage_ramdisk/system", "first_stage_ramdisk/system/bin",
           "first_stage_ramdisk/system/lib64", "system", "system/bin")
MEMBERS = (("init", b"abcd", 2, stat.S_IFREG | 0o750),
           (bootstrap.TARGETS[1], b"efgh", 3, stat.S_IFREG | 0o755),
           (bootstrap.TARGETS[2], b"ijkl", 6, stat.S_IFREG | 0o644),
           (bootstrap.TARGETS[3], b"mnop", 7, stat.S_IFREG | 0o755))


def archive(*extra, trailer=True, target_options=None, omit_parents=(), omit_targets=()):
    result = record(".", ino=1, mode=stat.S_IFDIR | 0o755)
    for i, parent in enumerate(PARENTS):
        if parent not in omit_parents:
            result += record(parent, ino=90 + i, mode=stat.S_IFDIR | 0o755)
    for name, data, ino, mode in MEMBERS:
        if name in omit_targets:
            continue
        options = {"ino": ino, "mode": mode, **(target_options or {}).get(name, {})}
        result += record(name, data, **options)
    result += b"".join(extra)
    if trailer:
        result += record("TRAILER!!!", ino=0, mode=0)
    return result


class CompositionTests(unittest.TestCase):
    def test_disabled_default_and_separate_ifma(self):
        for prof in ({}, {"cpu_compat": {}}, {"cpu_compat": {"patch_ifma": True}},
                     {"cpu_compat": {"patch_ally_bootstrap": False}}):
            self.assertFalse(bootstrap.enabled(prof))
        self.assertTrue(bootstrap.enabled({"cpu_compat": {"patch_ally_bootstrap": True}}))
        with self.assertRaises(bootstrap.Refused):
            bootstrap.enabled({"cpu_compat": {"patch_ally_bootstrap": 1}})

    def test_checked_source_identity(self):
        identity = bootstrap.source_identity()
        self.assertEqual(identity["sources"], bootstrap.SOURCE_PINS)
        for relative, pin in identity["sources"].items():
            self.assertEqual(sha((ROOT / relative).read_bytes()), pin)
        self.assertEqual(set(identity["members"]), set(bootstrap.TARGETS))
        self.assertEqual(identity["members"]["init"]["combined_sha256"],
                         "a78a95a73b2277eef5198c114d916fded9ab7ca4c9d98d27e8f1de301a91395d")

    def test_four_target_specs_match_pinned_manifest_and_actual_metadata(self):
        manifest = json.loads((ROOT / bootstrap.DEVICE / "step2/adler-manifest-step2.json").read_text())
        rows = {row["id"]: row for row in manifest["targets"]}
        self.assertEqual([bootstrap._SPECS[name]["adler_id"] for name in bootstrap.TARGETS], [3, 1, 2, 4])
        for name in bootstrap.TARGETS:
            spec = bootstrap._SPECS[name]
            row = rows[spec["adler_id"]]
            self.assertEqual(row["path"], "initrd-all/" + name)
            self.assertEqual((row["size"], row["sha256"]), (spec["size"], spec["sha256"]))
            original = bytes.fromhex(row["original_hex"])
            replacement = bytes.fromhex(row["replacement_hex"])
            expected = {row["patch_offset"] + i: (old, new)
                        for i, (old, new) in enumerate(zip(original, replacement)) if old != new}
            self.assertEqual(spec["adler_delta"], expected)
            self.assertEqual(bootstrap._TARGET_METADATA[name], {
                "mode": next(mode for target, _, _, mode in MEMBERS if target == name),
                "uid": 0, "gid": 0, "nlink": 1})
            if "scalar_source" not in spec:
                self.assertEqual(bootstrap.COMBINED_SHA256[name], row["patched_sha256"])

    def test_adler_only_targets_do_not_load_scalar_and_refuse_intermediates(self):
        for name in bootstrap.TARGETS[2:]:
            with self.subTest(name=name):
                original, candidate = b"abcd", b"Abcd"
                spec = {"size": 4, "sha256": sha(original), "adler_id": bootstrap._SPECS[name]["adler_id"],
                        "adler_delta": {0: (97, 65)}}
                row = {"id": spec["adler_id"], "path": "initrd-all/" + name,
                       "size": 4, "sha256": sha(original), "patched_sha256": sha(candidate)}
                adler = types.SimpleNamespace(validate=lambda data, row: ("original", candidate),
                                              PatchError=ValueError)
                sources = {bootstrap.DEVICE + "step2/patch-adler-step2.py": b"",
                           bootstrap.DEVICE + "step2/adler-manifest-step2.json": json.dumps({
                               "schema": 1, "policy": "force_scalar_adler_only_preserve_crc_gate",
                               "targets": [row]}).encode()}
                with patch.dict(bootstrap._SPECS, {name: spec}), \
                     patch.dict(bootstrap.COMBINED_SHA256, {name: sha(candidate)}), \
                     patch.object(bootstrap, "_sources", return_value=sources), \
                     patch.object(bootstrap, "_load", return_value=adler) as loader:
                    self.assertEqual(bootstrap.patch_member(name, original), candidate)
                    bootstrap.verify_member(name, candidate)
                    loader.assert_called_once_with(bootstrap.DEVICE + "step2/patch-adler-step2.py", b"")
                    with self.assertRaisesRegex(bootstrap.Refused, "intermediates refused"):
                        bootstrap.patch_member(name, candidate)

    def test_adler_only_target_wrong_delta_refused_even_with_matching_output_hash(self):
        name = bootstrap.TARGETS[2]
        spec = {"size": 4, "sha256": sha(b"abcd"), "adler_id": 2, "adler_delta": {0: (97, 65)}}
        row = {"id": 2, "path": "initrd-all/" + name, "size": 4,
               "sha256": sha(b"abcd"), "patched_sha256": sha(b"AbcD")}
        adler = types.SimpleNamespace(validate=lambda data, row: ("original", b"AbcD"), PatchError=ValueError)
        sources = {bootstrap.DEVICE + "step2/patch-adler-step2.py": b"",
                   bootstrap.DEVICE + "step2/adler-manifest-step2.json": json.dumps({
                       "schema": 1, "policy": "force_scalar_adler_only_preserve_crc_gate", "targets": [row]}).encode()}
        with patch.dict(bootstrap._SPECS, {name: spec}), \
             patch.dict(bootstrap.COMBINED_SHA256, {name: sha(b"AbcD")}), \
             patch.object(bootstrap, "_sources", return_value=sources), \
             patch.object(bootstrap, "_load", return_value=adler):
            with self.assertRaisesRegex(bootstrap.Refused, "exact delta mismatch"):
                bootstrap.patch_member(name, b"abcd")

    def test_modified_tool_source_refused(self):
        with patch.object(bootstrap, "SOURCE_PINS", {"devices/rog-ally/step2/patch-adler-step2.py": "0" * 64}):
            with self.assertRaisesRegex(bootstrap.Refused, "source pin"):
                bootstrap.source_identity()

    def test_unset_final_pin_refused(self):
        with patch.dict(bootstrap.COMBINED_SHA256, {"init": None}):
            with self.assertRaisesRegex(bootstrap.Refused, "not pinned"):
                bootstrap._combined_pin("init")

    def test_unknown_member_original_type_size_and_hash_refused(self):
        for name in ("./init", "/init", "initrd-all/init", "system/bin/linker64", "anything"):
            with self.assertRaises(bootstrap.Refused):
                bootstrap.patch_member(name, b"")
        for value in (b"", bytearray(3137864), bytes(3137864)):
            with self.assertRaises(bootstrap.Refused):
                bootstrap.patch_member("init", value)
        with self.assertRaises(bootstrap.Refused):
            bootstrap.verify_member("init", bytes(3137864))

    def test_nonoverlap_composition(self):
        original = b"abcd"
        result = bootstrap._merge(original, [(b"Abcd", {0: (97, 65)}),
                                             (b"abcD", {3: (100, 68)})], sha(b"AbcD"))
        self.assertEqual(result, b"AbcD")
        self.assertEqual(original, b"abcd")

    def test_overlap_even_identical_refused(self):
        variant = (b"Abcd", {0: (97, 65)})
        with self.assertRaisesRegex(bootstrap.Refused, "overlapping"):
            bootstrap._merge(b"abcd", [variant, variant], sha(b"Abcd"))

    def test_extra_missing_wrong_delta_and_length_refused(self):
        for variant, expected in ((b"AbcD", {0: (97, 65)}), (b"abcd", {0: (97, 65)}),
                                  (b"Abcd", {0: (98, 65)}), (b"Abcd!", {0: (97, 65)})):
            with self.assertRaises(bootstrap.Refused):
                bootstrap._merge(b"abcd", [(variant, expected)], sha(variant))

    def test_wrong_combined_hash_refused(self):
        with self.assertRaisesRegex(bootstrap.Refused, "combined SHA256"):
            bootstrap._merge(b"abcd", [(b"Abcd", {0: (97, 65)})], "0" * 64)

    def test_both_validators_receive_original_not_intermediate(self):
        # Mocks exercise orchestration, not a substitute acceptance ELF.
        original = b"abcd"
        spec = {"size": 4, "sha256": sha(original), "adler_id": 3,
                "adler_delta": {0: (97, 65)}, "scalar_source": "scalar",
                "scalar_sha256": sha(b"abcD"), "scalar_delta": {3: (100, 68)}}
        row = {"id": 3, "path": "initrd-all/init", "size": 4,
               "sha256": sha(original), "patched_sha256": sha(b"Abcd")}
        calls = []
        def adler_call(data, actual_row):
            calls.append(data)
            self.assertEqual(actual_row, row)
            return "original", b"Abcd"
        def scalar_call(data):
            calls.append(data)
            return b"abcD"
        adler = types.SimpleNamespace(validate=adler_call, PatchError=ValueError)
        scalar = types.SimpleNamespace(transform=scalar_call, Refused=ValueError)
        sources = {bootstrap.DEVICE + "step2/patch-adler-step2.py": b"", "scalar": b"",
                   bootstrap.DEVICE + "step2/adler-manifest-step2.json": json.dumps({
                       "schema": 1, "policy": "force_scalar_adler_only_preserve_crc_gate", "targets": [row]}).encode()}
        with patch.dict(bootstrap._SPECS, {"init": spec}), \
             patch.dict(bootstrap.COMBINED_SHA256, {"init": sha(b"AbcD")}), \
             patch.object(bootstrap, "_sources", return_value=sources), \
             patch.object(bootstrap, "_load", side_effect=[adler, scalar]):
            self.assertEqual(bootstrap.patch_member("init", original), b"AbcD")
        self.assertEqual(calls, [original, original])


class ArchiveTests(unittest.TestCase):
    def assert_refused(self, data):
        with self.assertRaises(bootstrap.Refused):
            bootstrap._archive_members(data)

    def test_valid_single_newc_zero_tail(self):
        entries = bootstrap._archive_members(archive() + bytes(512))
        self.assertEqual(set(entries), {".", *PARENTS, *bootstrap.TARGETS})

    def test_valid_three_archives_targets_separated_and_inodes_scoped(self):
        # Representative public container structure, not the private archive.
        def directory(ino):
            return record("first_stage_ramdisk", ino=ino, mode=stat.S_IFDIR | 0o755)
        trailer = record("TRAILER!!!", mode=0)
        first = directory(1)
        first += record(PARENTS[1], ino=4, mode=stat.S_IFDIR | 0o755)
        first += record(PARENTS[2], ino=5, mode=stat.S_IFDIR | 0o755)
        first += record(PARENTS[3], ino=8, mode=stat.S_IFDIR | 0o755)
        first += record(bootstrap.TARGETS[1], b"efgh", ino=2)
        first += record(bootstrap.TARGETS[2], b"ijkl", ino=6, mode=stat.S_IFREG | 0o644) + trailer
        second = directory(10) + record("other", b"z", ino=2) + trailer
        third = record("init", b"abcd", ino=2, mode=stat.S_IFREG | 0o750)
        third += record("system", ino=8, mode=stat.S_IFDIR | 0o755)
        third += record("system/bin", ino=9, mode=stat.S_IFDIR | 0o755)
        third += record(bootstrap.TARGETS[3], b"mnop", ino=7) + trailer
        body = first + bytes(116) + second + bytes(220) + third + bytes(32)
        entries = bootstrap._archive_members(body)
        self.assertEqual(entries[bootstrap.TARGETS[1]]["archive"], 0)
        self.assertEqual(entries["init"]["archive"], 2)
        self.assertEqual(entries["init"]["mode"], stat.S_IFREG | 0o750)
        self.assertEqual(entries[bootstrap.TARGETS[2]]["archive"], 0)
        self.assertEqual(entries[bootstrap.TARGETS[3]]["archive"], 2)

    def test_cross_archive_repeated_directory_metadata_or_type_mismatch_refused(self):
        directory = record("dir", ino=4, mode=stat.S_IFDIR | 0o755)
        body = archive(directory)
        trailer = record("TRAILER!!!", mode=0)
        for options in ({"mode": stat.S_IFDIR | 0o700}, {"mode": stat.S_IFDIR | 0o755, "uid": 1},
                        {"mode": stat.S_IFDIR | 0o755, "gid": 1},
                        {"mode": stat.S_IFDIR | 0o755, "nlink": 2}, {"mode": stat.S_IFLNK | 0o777}):
            self.assert_refused(body + record("dir", ino=5, **options) + trailer)
        self.assert_refused(body + record("dir", b"x", ino=5, mode=stat.S_IFDIR | 0o755) + trailer)

    def test_same_archive_identical_zero_size_directory_duplicates_allowed(self):
        directory = record("dev", ino=4, mode=stat.S_IFDIR | 0o755)
        for repeated_ino in (4, 5):
            body = archive(directory, record("dev", ino=repeated_ino, mode=stat.S_IFDIR | 0o755))
            entries = bootstrap._archive_members(body)
            self.assertEqual(entries["dev"]["mode"], stat.S_IFDIR | 0o755)
            self.assertEqual(entries["dev"]["size"], 0)

    def test_same_archive_directory_duplicate_metadata_payload_or_type_mismatch_refused(self):
        directory = record("dev", ino=4, mode=stat.S_IFDIR | 0o755)
        for options in ({"mode": stat.S_IFDIR | 0o700}, {"mode": stat.S_IFDIR | 0o755, "uid": 1},
                        {"mode": stat.S_IFDIR | 0o755, "gid": 1},
                        {"mode": stat.S_IFDIR | 0o755, "nlink": 2},
                        {"mode": stat.S_IFREG | 0o755}, {"mode": stat.S_IFLNK | 0o777}):
            self.assert_refused(archive(directory, record("dev", ino=5, **options)))
        self.assert_refused(archive(directory, record("dev", b"x", ino=5, mode=stat.S_IFDIR | 0o755)))

    def test_cross_archive_target_and_non_directory_duplicates_refused(self):
        trailer = record("TRAILER!!!", mode=0)
        self.assert_refused(archive() + record("init", b"abcd", ino=99) + trailer)
        self.assert_refused(archive(record("other", b"x", ino=4)) + record("other", b"x", ino=99) + trailer)

    def test_interarchive_padding_alignment_unknown_and_compressed_refused(self):
        for extra in (b"\0" + record("TRAILER!!!", mode=0), b"\0\0\0\0X",
                      b"\0\0\0\0\x1f\x8b", b"070702" + record("TRAILER!!!", mode=0)[6:]):
            self.assert_refused(archive() + extra)

    def test_cross_archive_symlink_parent_refused(self):
        self.assert_refused(archive() + record("first_stage_ramdisk", b"x", ino=4,
                                              mode=stat.S_IFLNK | 0o777) + record("TRAILER!!!", mode=0))

    def test_truncated_header_name_data_and_missing_trailer(self):
        body = archive()
        for data in (body[:50], body[:111], body[:119], body[:-1], archive(trailer=False)):
            self.assert_refused(data)

    def test_compressed_crc_magic_and_nonhex_refused(self):
        body = archive()
        for data in (b"\x1f\x8b" + body, b"070702" + body[6:], body[:6] + b"-" + body[7:]):
            self.assert_refused(data)

    def test_duplicate_alias_escape_and_controls_refused(self):
        for name in ("init", "./init", "x/../init", "/init", "x//y", "x/", "", "x\ny", "x\0y"):
            self.assert_refused(archive(record(name, b"x", ino=90)))

    def test_second_archive_with_duplicate_targets_or_nonzero_tail_refused(self):
        self.assert_refused(archive() + archive())
        self.assert_refused(archive() + b"\0\0X")

    def test_trailer_payload_and_nonzero_padding_refused(self):
        self.assert_refused(archive(trailer=False) + record("TRAILER!!!", b"x", mode=0))
        body = bytearray(archive())
        body[112] = 1  # Root name's required alignment padding.
        self.assert_refused(bytes(body))

    def test_missing_target_refused(self):
        self.assert_refused(record("init", b"abcd") + record("TRAILER!!!", mode=0))
        for name in bootstrap.TARGETS:
            with self.subTest(name=name):
                self.assert_refused(archive(omit_targets=(name,)))

    def test_every_target_duplicate_and_inode_alias_refused(self):
        for name, data, ino, mode in MEMBERS:
            with self.subTest(name=name):
                self.assert_refused(archive(record(name, data, ino=99, mode=mode)))
                self.assert_refused(archive(record("alias", b"q", ino=ino)))

    def test_target_hardlink_symlink_directory_noexec_and_inode_alias_refused(self):
        for options in ({"nlink": 2}, {"mode": stat.S_IFLNK | 0o777},
                        {"mode": stat.S_IFDIR | 0o755}, {"mode": stat.S_IFREG | 0o644}):
            self.assert_refused(archive(target_options={"init": options}))
        self.assert_refused(archive(record("alias", b"q", ino=2)))

    def test_each_target_exact_mode_root_owner_and_special_bits(self):
        for name, _, _, mode in MEMBERS:
            for options in ({"uid": 1}, {"gid": 1}, {"mode": mode | stat.S_ISUID},
                            {"mode": mode | stat.S_ISGID}, {"mode": mode | stat.S_ISVTX},
                            {"mode": mode ^ 0o005}):
                with self.subTest(name=name, options=options):
                    with self.assertRaisesRegex(bootstrap.Refused, "member metadata"):
                        bootstrap._archive_members(archive(target_options={name: options}))

    def test_every_target_ancestor_must_exist(self):
        for parent in PARENTS:
            with self.subTest(parent=parent):
                with self.assertRaisesRegex(bootstrap.Refused, "ancestor missing"):
                    bootstrap._archive_members(archive(omit_parents=(parent,)))

    def test_target_ancestor_created_later_in_same_or_later_archive_refused(self):
        target = record(bootstrap.TARGETS[1], b"efgh", ino=3)
        trailer = record("TRAILER!!!", mode=0)
        parents = b"".join(record(name, ino=90 + i, mode=stat.S_IFDIR | 0o755)
                           for i, name in enumerate(PARENTS))
        remainder = parents + record("init", b"abcd", ino=2, mode=stat.S_IFREG | 0o750) + trailer
        for separator in (b"", trailer + bytes(16)):
            with self.subTest(new_archive=bool(separator)):
                with self.assertRaisesRegex(bootstrap.Refused, "preceding directory"):
                    bootstrap._archive_members(target + separator + remainder)

    def test_target_prefix_symlink_or_file_refused(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFREG | 0o644):
            self.assert_refused(archive(record("first_stage_ramdisk", b"x", ino=4, mode=mode)))
        self.assert_refused(archive(record("init/child", b"x", ino=4)))

    def test_equal_length_changes_only_member_data(self):
        original = archive(record("other", b"untouched", ino=4)) + bytes(32)
        entries = bootstrap._archive_members(original)
        def transform(name, data):
            return data.upper()
        def verify(name, data):
            self.assertEqual(data, next(raw.upper() for target, raw, _, _ in MEMBERS if target == name))
        with patch.object(bootstrap, "patch_member", side_effect=transform) as transform_mock, \
             patch.object(bootstrap, "verify_member", side_effect=verify) as verify_mock:
            updated, receipt = bootstrap.patch_archive(original)
        self.assertEqual(len(updated), len(original))
        changed = {i for i, (a, b) in enumerate(zip(original, updated)) if a != b}
        expected = {entries[n]["offset"] + i for n in bootstrap.TARGETS for i in range(4)}
        self.assertEqual(changed, expected)
        self.assertEqual(transform_mock.call_count, 4)
        self.assertEqual(verify_mock.call_count, 4)
        self.assertEqual([row["name"] for row in receipt["members"]], list(bootstrap.TARGETS))
        self.assertEqual(receipt["archive_sha256"], sha(updated))
        self.assertEqual(receipt["input_archive_sha256"], sha(original))

    def test_length_changing_transform_refused_before_return(self):
        with patch.object(bootstrap, "patch_member", return_value=b"too long"):
            with self.assertRaisesRegex(bootstrap.Refused, "length changed"):
                bootstrap.patch_archive(archive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
