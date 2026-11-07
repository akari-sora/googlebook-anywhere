"""Exact-original Adler repairs for explicit Ally container ELF locations.

Only disposable extracted builder trees belong here. Container rebuilding,
metadata-index validation, nested AVB/APEX signing and final image readback remain
caller responsibilities; these helpers are not whole-image CPU acceptance.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
PATCHER = "devices/rog-ally/step2/patch-adler-step2.py"
MANIFEST = "devices/rog-ally/step2/adler-manifest-step2.json"
SOURCE_PINS = {
    PATCHER: "f0dec511b0ef4e5aa69f7257e918d65a1eb7752843cc1258647f7279008c785a",
    MANIFEST: "17705a5bf9250c8704a0d0d048e10bf213fb333e7c542d5b1c45d49af30d6b8e",
}
TARGETS = {
    "system": {
        "system/bin/bootstrap/linker64": 67,
        "system/lib64/libz.so": 73,
        "system/bin/bpftool": 77,
        "system/bin/init": 79,
        "system/bin/snapuserd": 82,
        "system/bin/update_engine": 84,
        "system/lib64/libfdtrack.so": 88,
    },
    "vendor": {"lib64/libz.so": 122},
    "runtime": {"bin/linker64": 27},
    "appsearch": {"lib64/libicing_anywhere.so": 33},
    "microdroid": {
        "system/bin/bootstrap/linker64": 67,
        "system/bin/init": 68,
        "system/lib64/libz.so": 73,
    },
    "microdroid_runtime": {"bin/linker64": 27},
}
# These are literal manifest container chains, not extraction/staging paths.
# The same ELF ID in two scopes does not establish the same containing image.
SOURCE_PREFIXES = {
    "system": "system_a/",
    "vendor": "vendor_a/",
    "runtime": "system_a/system/apex/com.android.runtime.apex!/apex_payload.img!/erofs/",
    "appsearch": "system_a/system/apex/com.google.android.appsearch.apex!/apex_payload.img!/erofs/",
    "microdroid": "system_a/system/apex/com.google.android.virt.apex!/apex_payload.img!/erofs/"
                  "etc/fs/microdroid.img!/erofs/",
    "microdroid_runtime": "system_a/system/apex/com.google.android.virt.apex!/apex_payload.img!/erofs/"
                          "etc/fs/microdroid.img!/erofs/system/apex/com.android.runtime.apex!/"
                          "apex_payload.img!/erofs/",
}
_ALL_IDS = {1, 2, 3, 4, 27, 33, 67, 68, 73, 77, 79, 82, 84, 88, 122}


class Refused(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise Refused(message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def enabled(prof):
    """Use the existing explicit Ally repair opt-in; IFMA alone is insufficient."""
    value = prof.get("cpu_compat", {}).get("patch_ally_bootstrap", False)
    _require(type(value) is bool, "patch_ally_bootstrap must be a boolean")
    return value


def expected_changed_paths(scope):
    _require(scope in TARGETS, "unknown Adler tree scope")
    return tuple(TARGETS[scope])


def _context():
    sources = {}
    for name, pin in SOURCE_PINS.items():
        data = (ROOT / name).read_bytes()
        _require(_sha(data) == pin, "Adler source pin mismatch: " + name)
        sources[name] = data
    manifest = json.loads(sources[MANIFEST])
    _require(manifest.get("schema") == 1 and
             manifest.get("policy") == "force_scalar_adler_only_preserve_crc_gate",
             "Adler manifest schema/policy mismatch")
    rows = manifest.get("targets", [])
    _require(len(rows) == 15 and {row["id"] for row in rows} == _ALL_IDS,
             "Adler manifest target set mismatch")
    _require(len({row["sha256"] for row in rows}) == 15,
             "duplicate Adler original SHA256")
    by_id = {row["id"]: row for row in rows}
    selected = {}
    for scope, paths in TARGETS.items():
        selected[scope] = {}
        for path, target_id in paths.items():
            row = by_id[target_id]
            _require(row["name"] == f"id-{target_id:03d}" and
                     SOURCE_PREFIXES[scope] + path in row["source_aliases"],
                     "Adler container provenance mismatch")
            selected[scope][path] = row
    module = types.ModuleType("_ally_adler_checked_patcher")
    module.__file__ = str(ROOT / PATCHER)
    exec(compile(sources[PATCHER], module.__file__, "exec"), module.__dict__)
    pins = {**SOURCE_PINS, "builder/lib/ally_adler.py": _sha(Path(__file__).read_bytes())}
    return selected, module, pins


def source_identity():
    """Checked source/cache identity; no assertion about private image contents."""
    rows, _, pins = _context()
    return {"schema": 1, "sources": pins, "targets": {
        scope: [{"id": row["id"], "path": path, "size": row["size"],
                 "source_sha256": row["sha256"], "patched_sha256": row["patched_sha256"]}
                for path, row in paths.items()]
        for scope, paths in rows.items()}}


def _identity(info):
    # atime may change from reads on hosts lacking O_NOATIME; preserve its
    # original value on replacement, but do not mistake our read for a writer.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory(stack, path):
    """Bind every ancestor, refusing symlinks rather than resolving through one."""
    path = Path(path).absolute()
    _require(".." not in path.parts, "Adler tree traversal refused")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    stack.callback(os.close, fd)
    for part in path.parts[1:]:
        fd = os.open(part, flags, dir_fd=fd)
        stack.callback(os.close, fd)
    return fd


def _read_target(stack, root_fd, path, row):
    parent = root_fd
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parts = path.split("/")
    for part in parts[:-1]:
        parent = os.open(part, flags, dir_fd=parent)
        stack.callback(os.close, parent)
    fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                 getattr(os, "O_NOATIME", 0), dir_fd=parent)
    stack.callback(os.close, fd)
    info = os.fstat(fd)
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
             info.st_size == row["size"], "Adler target type/link/size mismatch: " + path)
    chunks = []
    remaining = info.st_size
    while remaining:
        part = os.read(fd, min(remaining, 1024 * 1024))
        _require(bool(part), "Adler target short read: " + path)
        chunks.append(part)
        remaining -= len(part)
    _require(not os.read(fd, 1), "Adler target grew: " + path)
    data = b"".join(chunks)
    _require(_identity(info) == _identity(os.fstat(fd)), "Adler target changed while reading: " + path)
    return {"path": path, "row": row, "fd": fd, "parent": parent,
            "name": parts[-1], "metadata": info, "data": data}


def _unchanged(item):
    expected = _identity(item["metadata"])
    _require(expected == _identity(os.fstat(item["fd"])) ==
             _identity(os.stat(item["name"], dir_fd=item["parent"], follow_symlinks=False)),
             "Adler target changed before replacement: " + item["path"])


def _remove_temporary(parent, name):
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass


def _receipt(scope, items, pins):
    return {"schema": 1, "scope": scope, "sources": pins, "targets": [
        {"id": item["row"]["id"], "path": item["path"], "size": item["row"]["size"],
         "source_sha256": item["row"]["sha256"],
         "sha256": item["row"]["patched_sha256"],
         "mode": item["metadata"].st_mode, "uid": item["metadata"].st_uid,
         "gid": item["metadata"].st_gid}
        for item in items]}


def patch_tree(scope, tree):
    """Preflight every exact original, then atomically replace staged files.

    Validation failures precede all payload writes. I/O failures or concurrent
    mutation during commit may leave a partial *disposable* tree: callers must
    discard it, never publish/reuse it as stock. No already-patched input passes.
    """
    expected_changed_paths(scope)
    _require(all(callable(getattr(os, name, None)) for name in ("listxattr", "getxattr", "setxattr")),
             "Adler tree patching requires fd-based xattr support")
    rows, validator, pins = _context()
    with ExitStack() as stack:
        root_fd = _directory(stack, tree)
        items = [_read_target(stack, root_fd, path, row) for path, row in rows[scope].items()]
        for item in items:
            row, original = item["row"], item["data"]
            _require(_sha(original) == row["sha256"],
                     "Adler exact-original SHA256 mismatch: " + item["path"])
            try:
                state, candidate = validator.validate(original, row)
            except validator.PatchError as error:
                raise Refused("Adler validator refused: " + str(error)) from error
            _require(state == "original" and type(candidate) is bytes and
                     len(candidate) == len(original) and _sha(candidate) == row["patched_sha256"],
                     "Adler validated candidate mismatch: " + item["path"])
            item["candidate"] = candidate
        for item in items:
            _unchanged(item)
        # All originals and candidates pass before any staging payload is written.
        for item in items:
            temporary = ".ally-adler-" + secrets.token_hex(16)
            fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=item["parent"])
            stack.callback(_remove_temporary, item["parent"], temporary)
            try:
                with os.fdopen(fd, "wb", closefd=False) as output:
                    output.write(item["candidate"])
                    output.flush()
                metadata = item["metadata"]
                os.fchown(fd, metadata.st_uid, metadata.st_gid)
                for name in os.listxattr(item["fd"]):
                    os.setxattr(fd, name, os.getxattr(item["fd"], name))
                os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
                os.utime(fd, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                os.fsync(fd)
            finally:
                os.close(fd)
            item["temporary"] = temporary
        for item in items:
            _unchanged(item)
        for item in items:
            _unchanged(item)
            temporary = item["temporary"]
            os.replace(temporary, item["name"], src_dir_fd=item["parent"], dst_dir_fd=item["parent"])
            os.fsync(item["parent"])
        # Re-open the actual paths, not the unlinked original descriptors.
        verified = [_read_target(stack, root_fd, path, row) for path, row in rows[scope].items()]
        for original, output in zip(items, verified):
            _require(_sha(output["data"]) == output["row"]["patched_sha256"], "Adler final readback mismatch")
            before, after = original["metadata"], output["metadata"]
            _require((before.st_mode, before.st_uid, before.st_gid, before.st_atime_ns, before.st_mtime_ns) ==
                     (after.st_mode, after.st_uid, after.st_gid, after.st_atime_ns, after.st_mtime_ns),
                     "Adler output metadata changed")
            _require({name: os.getxattr(original["fd"], name) for name in os.listxattr(original["fd"])} ==
                     {name: os.getxattr(output["fd"], name) for name in os.listxattr(output["fd"])},
                     "Adler output xattrs changed")
        return _receipt(scope, items, pins)


def verify_tree(scope, tree):
    """Check exact repaired hashes in an extracted rebuilt container readback."""
    expected_changed_paths(scope)
    rows, _, pins = _context()
    with ExitStack() as stack:
        root_fd = _directory(stack, tree)
        items = [_read_target(stack, root_fd, path, row) for path, row in rows[scope].items()]
        for item in items:
            _require(_sha(item["data"]) == item["row"]["patched_sha256"],
                     "Adler final SHA256 mismatch: " + item["path"])
            _unchanged(item)
        return _receipt(scope, items, pins)


if __name__ == "__main__":
    _require(sys.argv[1:] == ["identity"], "only the identity command is supported")
    print(json.dumps(source_identity(), sort_keys=True, separators=(",", ":")))
