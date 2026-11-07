"""Exact-original Adler repairs for four Ally initrd members.

The init and linker64 repairs additionally compose their scalar transforms.

This module never reads ELF payload files, writes files, or accepts caller hash
overrides. The image builder owns CPIO handling and final ESP member readback.
It must call these helpers only under the explicit patch_ally_bootstrap opt-in.
This is not a claim that the rest of the image is CPU-compatible.
"""
import hashlib
import json
from pathlib import Path
import stat
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
DEVICE = "devices/rog-ally/"
SOURCE_PINS = {
    DEVICE + "step2/patch-adler-step2.py": "f0dec511b0ef4e5aa69f7257e918d65a1eb7752843cc1258647f7279008c785a",
    DEVICE + "step2/adler-manifest-step2.json": "17705a5bf9250c8704a0d0d048e10bf213fb333e7c542d5b1c45d49af30d6b8e",
    DEVICE + "step3/patch-init-scalar.py": "baf33e65e971dfd07b044cfcb8bcfcc9e35af78cf66f0dec0257db497f886053",
    DEVICE + "step4/patch-linker-scalar.py": "2cb60b6c8c260d7cb1fe13662c979be566f145c3e7cd921c1b91340ecf25bb09",
}
TARGETS = ("init", "first_stage_ramdisk/system/bin/linker64",
           "first_stage_ramdisk/system/lib64/libz.so", "system/bin/snapuserd_ramdisk")
_TARGET_METADATA = {
    "init": {"mode": stat.S_IFREG | 0o750, "uid": 0, "gid": 0, "nlink": 1},
    "first_stage_ramdisk/system/bin/linker64": {
        "mode": stat.S_IFREG | 0o755, "uid": 0, "gid": 0, "nlink": 1},
    "first_stage_ramdisk/system/lib64/libz.so": {
        "mode": stat.S_IFREG | 0o644, "uid": 0, "gid": 0, "nlink": 1},
    "system/bin/snapuserd_ramdisk": {
        "mode": stat.S_IFREG | 0o755, "uid": 0, "gid": 0, "nlink": 1},
}
# init/linker composition: ally-bootstrap-hashes.json, 2026-10-25.
# Adler-only outputs: pinned step2 manifest; modes: initrd-adler-metadata.json.
# No ELF payload was transferred to this source checkout.
COMBINED_SHA256 = {
    "init": "a78a95a73b2277eef5198c114d916fded9ab7ca4c9d98d27e8f1de301a91395d",
    "first_stage_ramdisk/system/bin/linker64": "80cb5d97f3f2da13de6d9be2cb3b22ff7ba572704ddd9576873dc3257a9baba1",
    "first_stage_ramdisk/system/lib64/libz.so": "effa6896a877e497ccddd4361cd4ba18cb1cfd20ba52c0dc5b0089917694aa12",
    "system/bin/snapuserd_ramdisk": "000157a67dfb7bb4e2ba76325330d9ef71fc701fb98901e7f4c996a8cb4c34f6",
}
_SPECS = {
    "init": {
        "size": 3137864,
        "sha256": "02c8c532e922d215e0516ac967bd719072b24861ee3de7a9d81bc1934b00037f",
        "adler_id": 3,
        "adler_delta": {1447009: (2, 0)},
        "scalar_source": DEVICE + "step3/patch-init-scalar.py",
        "scalar_sha256": "23408e3b2a65474ca5e801bde5da3493a600c744547686e20a74dc37450ea2ab",
        "scalar_delta": {0x2b8495: (0x73, 0x90), 0x2b8496: (0x13, 0x90)},
    },
    "first_stage_ramdisk/system/bin/linker64": {
        "size": 2542872,
        "sha256": "3631a5c65adecb306579974a74ea27cdfabf28ee264676136139f7b15922e3ea",
        "adler_id": 1,
        "adler_delta": {1944705: (2, 0)},
        "scalar_source": DEVICE + "step4/patch-linker-scalar.py",
        "scalar_sha256": "5b238d5200cda2059a6617d5c150c2929c475f4284ada5b4f5f93debdb3f1443",
        "scalar_delta": {0x18e18e: (0x73, 0x90), 0x18e18f: (0x12, 0x90),
                         0x192151: (0x73, 0x90), 0x192152: (0x0f, 0x90),
                         0x192453: (0x73, 0x90), 0x192454: (0x5f, 0x90)},
    },
    "first_stage_ramdisk/system/lib64/libz.so": {
        "size": 134464,
        "sha256": "b94c1f08820baf8672b97662ff578fd73af34c7585d9c2b24d65285dd173bd1b",
        "adler_id": 2,
        "adler_delta": {71249: (2, 0)},
    },
    "system/bin/snapuserd_ramdisk": {
        "size": 1387328,
        "sha256": "a36f32f0f4030d233c70c1f0f64c26d4c9a331f8983420659f38b9d428ee91f4",
        "adler_id": 4,
        "adler_delta": {1170337: (2, 0)},
    },
}


class Refused(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise Refused(message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def enabled(prof):
    """False by default; an IFMA opt-in does not enable this separate repair."""
    value = prof.get("cpu_compat", {}).get("patch_ally_bootstrap", False)
    _require(type(value) is bool, "patch_ally_bootstrap must be a boolean")
    return value


def _sources():
    result = {}
    for name, pin in SOURCE_PINS.items():
        data = (ROOT / name).read_bytes()
        _require(_sha(data) == pin, "bootstrap source pin mismatch: " + name)
        result[name] = data
    return result


def source_identity():
    """Pinned inputs for build identity/cache receipts, not runtime acceptance."""
    _sources()
    return {"sources": dict(SOURCE_PINS), "members": {
        name: {"size": spec["size"], "source_sha256": spec["sha256"],
               "combined_sha256": COMBINED_SHA256[name], **_TARGET_METADATA[name]}
        for name, spec in _SPECS.items()}}


def _load(name, data):
    # Compile the exact bytes whose hash was checked, not a later path read.
    module = types.ModuleType("_ally_bootstrap_" + Path(name).stem.replace("-", "_"))
    module.__file__ = str(ROOT / name)
    exec(compile(data, module.__file__, "exec"), module.__dict__)
    return module


def _spec(name):
    _require(name in TARGETS, "unknown bootstrap member")
    return _SPECS[name]


def _combined_pin(name):
    pin = COMBINED_SHA256[name]
    _require(isinstance(pin, str) and len(pin) == 64 and
             all(c in "0123456789abcdef" for c in pin),
             "reviewed combined SHA256 is not pinned: " + name)
    return pin


def _merge(original, variants, expected_sha):
    """Check every exact standalone delta, reject overlaps, then combine once."""
    merged = bytearray(original)
    changed = set()
    for variant, expected_delta in variants:
        _require(len(variant) == len(original), "standalone output length changed")
        actual = {i: (old, new) for i, (old, new) in enumerate(zip(original, variant)) if old != new}
        _require(actual == expected_delta and bool(actual), "standalone exact delta mismatch")
        _require(not changed.intersection(actual), "overlapping standalone deltas")
        changed.update(actual)
        for offset, (_, new) in actual.items():
            merged[offset] = new
    result = bytes(merged)
    _require(_sha(result) == expected_sha, "combined SHA256 mismatch")
    return result


def patch_member(name, data):
    """Return a combined candidate from untouched, exactly pinned originals."""
    spec = _spec(name)
    _require(type(data) is bytes and len(data) == spec["size"], "bootstrap original size/type mismatch")
    _require(_sha(data) == spec["sha256"], "bootstrap original SHA256 mismatch (intermediates refused)")
    pin = _combined_pin(name)
    sources = _sources()
    adler = _load(DEVICE + "step2/patch-adler-step2.py", sources[DEVICE + "step2/patch-adler-step2.py"])
    manifest = json.loads(sources[DEVICE + "step2/adler-manifest-step2.json"])
    _require(manifest.get("schema") == 1 and manifest.get("policy") == "force_scalar_adler_only_preserve_crc_gate",
             "Adler manifest schema/policy mismatch")
    rows = [r for r in manifest["targets"] if r["id"] == spec["adler_id"]]
    _require(len(rows) == 1, "Adler member row mismatch")
    row = rows[0]
    _require(row["path"] == "initrd-all/" + name and row["sha256"] == spec["sha256"] and
             row["size"] == spec["size"], "Adler member identity mismatch")
    try:
        state, adler_output = adler.validate(data, row)
    except adler.PatchError as error:
        raise Refused("standalone validator refused: " + str(error)) from error
    _require(state == "original", "Adler validator did not accept an original")
    _require(_sha(adler_output) == row["patched_sha256"], "Adler standalone SHA256 mismatch")
    variants = [(adler_output, spec["adler_delta"])]
    if "scalar_source" in spec:
        scalar = _load(spec["scalar_source"], sources[spec["scalar_source"]])
        try:
            scalar_output = scalar.transform(data)  # Same untouched original, never adler_output.
        except scalar.Refused as error:
            raise Refused("standalone validator refused: " + str(error)) from error
        _require(_sha(scalar_output) == spec["scalar_sha256"], "scalar standalone SHA256 mismatch")
        variants.append((scalar_output, spec["scalar_delta"]))
    return _merge(data, variants, pin)


def verify_member(name, data):
    """Validate the actual output member read back from the final ESP archive."""
    spec = _spec(name)
    _require(type(data) is bytes and len(data) == spec["size"], "bootstrap final size/type mismatch")
    _require(_sha(data) == _combined_pin(name), "bootstrap final SHA256 mismatch")


def _archive_members(body):
    """Parse a strict sequence of raw newc archives, never search for magic."""
    _require(type(body) is bytes, "newc body must be bytes")
    pos = 0
    archive = 0
    entries = {}
    all_entries = []
    while True:
        _require(pos + 110 <= len(body), "truncated newc header / missing trailer")
        header = body[pos:pos + 110]
        _require(header[:6] == b"070701", "only uncompressed 070701 newc archives are supported")
        _require(all(c in b"0123456789abcdefABCDEF" for c in header[6:]), "invalid newc hex header")
        fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
        ino, mode, uid, gid, nlink, _, size, devmajor, devminor, _, _, namesize, check = fields
        _require(check == 0 and 0 < namesize <= 4096, "invalid newc checksum/name size")
        start = pos + 110
        end = start + namesize
        _require(end <= len(body), "truncated newc name")
        raw = body[start:end]
        _require(raw[-1:] == b"\0" and b"\0" not in raw[:-1], "invalid newc name terminator")
        try:
            name = raw[:-1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise Refused("newc name is not UTF-8") from error
        data_start = (end + 3) & ~3
        data_end = data_start + size
        next_pos = (data_end + 3) & ~3
        _require(next_pos <= len(body), "truncated newc member/padding")
        _require(not any(body[end:data_start]) and not any(body[data_end:next_pos]), "nonzero newc alignment padding")
        if name == "TRAILER!!!":
            _require(size == 0, "newc trailer has payload")
            pos = next_pos
            while pos < len(body) and body[pos] == 0:
                pos += 1
            if pos == len(body):
                break
            _require(pos % 4 == 0 and body[pos:pos + 6] == b"070701",
                     "unsupported archive or nonzero interarchive padding")
            archive += 1
            continue
        _require((name == "." and stat.S_ISDIR(mode)) or
                 (bool(name) and not name.startswith("/") and
                  all(part not in ("", ".", "..") for part in name.split("/"))),
                 "noncanonical or escaping newc member")
        _require(all(ord(c) >= 32 and ord(c) != 127 for c in name), "control character in newc name")
        if name in TARGETS:
            components = name.split("/")
            for count in range(1, len(components)):
                parent = "/".join(components[:count])
                _require(parent in entries and stat.S_ISDIR(entries[parent]["mode"]),
                         "bootstrap ancestor missing or not a preceding directory: " + parent)
        entry = {"offset": data_start, "size": size, "mode": mode, "uid": uid, "gid": gid,
                 "nlink": nlink, "inode": (devmajor, devminor, ino), "archive": archive}
        if name in entries:
            previous = entries[name]
            _require(stat.S_ISDIR(mode) and stat.S_ISDIR(previous["mode"]) and size == 0 and
                     all(entry[key] == previous[key] for key in ("mode", "uid", "gid", "nlink", "size")),
                     "duplicate newc name/type shadow: " + name)
        else:
            entries[name] = entry
        all_entries.append(entry)
        pos = next_pos
    _require(all(name in entries for name in TARGETS), "missing bootstrap newc member")
    for name in TARGETS:
        entry = entries[name]
        _require(all(entry[key] == value for key, value in _TARGET_METADATA[name].items()),
                 "bootstrap member metadata differs from reviewed root-owned mode: " + name)
        _require(sum(e["archive"] == entry["archive"] and e["inode"] == entry["inode"]
                     for e in all_entries) == 1,
                 "bootstrap member inode alias: " + name)
    for name in entries:
        components = name.split("/")
        for count in range(1, len(components)):
            parent = "/".join(components[:count])
            _require(parent not in entries or stat.S_ISDIR(entries[parent]["mode"]),
                     "newc member parent is not a directory: " + parent)
    return entries


def _archive_receipt(body, entries):
    return {"schema": 1, "archive_sha256": _sha(body), "members": [
        {"name": name, "size": entries[name]["size"],
         "archive_index": entries[name]["archive"], "mode": entries[name]["mode"],
         "uid": entries[name]["uid"], "gid": entries[name]["gid"], "nlink": entries[name]["nlink"],
         "sha256": _sha(body[entries[name]["offset"]:entries[name]["offset"] + entries[name]["size"]])}
        for name in TARGETS]}


def patch_archive(body):
    """Patch exactly four members, preserving every other byte and all metadata."""
    entries = _archive_members(body)
    candidates = {}
    for name in TARGETS:
        entry = entries[name]
        original = body[entry["offset"]:entry["offset"] + entry["size"]]
        candidate = patch_member(name, original)
        _require(len(candidate) == entry["size"], "bootstrap member length changed")
        candidates[name] = candidate
    updated = bytearray(body)
    for name, candidate in candidates.items():
        entry = entries[name]
        updated[entry["offset"]:entry["offset"] + entry["size"]] = candidate
    result = bytes(updated)
    receipt = verify_archive(result)
    receipt["input_archive_sha256"] = _sha(body)
    return result, receipt


def verify_archive(body):
    """Verify combined targets in the actual final ESP archive readback."""
    entries = _archive_members(body)
    for name in TARGETS:
        entry = entries[name]
        verify_member(name, body[entry["offset"]:entry["offset"] + entry["size"]])
    return _archive_receipt(body, entries)


if __name__ == "__main__":
    if sys.argv[1:] != ["identity"]:
        raise SystemExit("usage: ally_bootstrap.py identity")
    print(json.dumps(source_identity(), sort_keys=True, separators=(",", ":")))
