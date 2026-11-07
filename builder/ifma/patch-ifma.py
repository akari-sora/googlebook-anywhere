#!/usr/bin/env python3
"""Fail-closed patcher for 17 host IFMA ELFs and one explicit guest scope.

The manifest is trusted review input. The extracted root must be exclusively
owned by this operation: concurrent writers are outside the safety model.
"""

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
try:
    from elfutil import Elf
except ImportError:  # Support loading this file as a module from tests/tools.
    try:
        from .elfutil import Elf
    except ImportError:
        import importlib.util as _ilu
        _espec = _ilu.spec_from_file_location("elfutil", Path(__file__).with_name("elfutil.py"))
        _emod = _ilu.module_from_spec(_espec)
        _espec.loader.exec_module(_emod)
        Elf = _emod.Elf


SCHEMA = 1
TARGET_COUNT = 17
MANAGER_SCOPE = ("microdroid", "system")
MANAGER_MANIFEST_SHA256 = "4dc035949d55c4985c9f36c3d90012317261dce01cd07cb2d961434ac310d326"
ORIGINAL = bytes.fromhex("c4e369025e1005c4e2f1b4c3")
REPLACEMENT = bytes.fromhex("c5f1f44e10c5f9d4c10f1f00")
SITE_PREFIX_LENGTH = 7
CONTEXT_SITE_DELTA = 0x21
CONTEXT_PATCH_DELTA = CONTEXT_SITE_DELTA - SITE_PREFIX_LENGTH
CONTEXT_LENGTH = 0x49
TABLE_LENGTH = 128
EXPECTED_PATHS = frozenset({
    "system/lib64/libzmij.dylib.so",
    "system_ext/bin/trusty_security_vm_launcher",
    "vendor/lib64/libzmij.dylib.so",
    "system/lib64/libinput.so",
    "system/lib64/libinputflinger.so",
    "system/bin/prefetch",
    "system/bin/ectool",
    "vendor/bin/ectool",
    "system/bin/aisealhostservice",
    "vendor/bin/hw/android.hardware.usb-service.desktop",
    "apex/com.google.android.virt/bin/vm",
    "apex/com.google.android.virt/bin/virtmgr",
    "apex/com.google.android.virt/bin/early_virtmgr",
    "apex/com.google.android.virt/bin/crosvm_minimal",
    "apex/com.google.android.virt/bin/crosvm",
    "apex/com.android.hardware.audio.desktop/lib64/libzmij.dylib.so",
    "apex/com.android.hardware.fingerprint.crosfp/bin/hw/android.hardware.biometrics.fingerprint-service.crosfp",
})
SCOPED_COUNTS = {
    ("partition", "vendor_a"): 3,
    ("partition", "system_a"): 6,
    ("partition", "system_ext_a"): 1,
    ("apex", "system/apex/com.google.android.virt.apex"): 5,
    ("apex", "apex/com.android.hardware.audio.desktop.apex"): 1,
    ("apex", "apex/com.android.hardware.fingerprint.crosfp.apex"): 1,
}


class PatchError(Exception):
    pass


def _integer(value, name):
    if isinstance(value, bool):
        raise PatchError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as exc:
            raise PatchError(f"{name} is not an integer") from exc
    raise PatchError(f"{name} must be an integer")


def _hex(value, length, name):
    if not isinstance(value, str):
        raise PatchError(f"{name} must be a hex string")
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise PatchError(f"{name} is not valid hex") from exc
    if len(result) != length:
        raise PatchError(f"{name} must contain exactly {length} bytes")
    return result


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate(data, row):
    """Validate one complete ELF and return its state plus patched bytes."""
    required = {
        "path", "sha256", "patched_sha256", "site_va", "file_offset",
        "function_va", "function_size", "size", "table_va",
        "table_file_offset", "table_hex", "context_va", "context_offset",
        "context_hex",
    }
    missing = sorted(required - set(row))
    if missing:
        raise PatchError("missing target fields: " + ", ".join(missing))

    original_hash = str(row["sha256"]).lower()
    patched_hash = str(row["patched_sha256"]).lower()
    if len(original_hash) != 64 or len(patched_hash) != 64:
        raise PatchError("sha256 fields must contain 64 hex characters")
    try:
        bytes.fromhex(original_hash)
        bytes.fromhex(patched_hash)
    except ValueError as exc:
        raise PatchError("sha256 fields are not valid hex") from exc
    if original_hash == patched_hash:
        raise PatchError("original and patched SHA256 must differ")

    file_offset = _integer(row["file_offset"], "file_offset")
    site_va = _integer(row["site_va"], "site_va")
    function_va = _integer(row["function_va"], "function_va")
    function_size = _integer(row["function_size"], "function_size")
    file_size = _integer(row["size"], "size")
    table_va = _integer(row["table_va"], "table_va")
    table_offset = _integer(row["table_file_offset"], "table_file_offset")
    context_va = _integer(row["context_va"], "context_va")
    context_offset = _integer(row["context_offset"], "context_offset")
    context = _hex(row["context_hex"], CONTEXT_LENGTH, "context_hex")
    table = _hex(row["table_hex"], TABLE_LENGTH, "table_hex")
    patch_va = site_va - SITE_PREFIX_LENGTH
    patch_offset = file_offset - SITE_PREFIX_LENGTH

    if file_size != len(data):
        raise PatchError(f"file size is {len(data)}, expected {file_size}")
    if context_va + CONTEXT_SITE_DELTA != site_va:
        raise PatchError("context_va is not site_va - 0x21")
    if function_size <= 0 or not (
        function_va <= context_va
        and context_va + CONTEXT_LENGTH <= function_va + function_size
        and function_va <= patch_va
        and patch_va + len(ORIGINAL) <= function_va + function_size
    ):
        raise PatchError("context and patch site must be inside the declared function")
    if context[CONTEXT_PATCH_DELTA:CONTEXT_PATCH_DELTA + len(ORIGINAL)] != ORIGINAL:
        raise PatchError("manifest context does not contain the original instruction sequence")

    try:
        elf = Elf(data)
        mapped_site = elf.v2o(site_va, len(ORIGINAL) - SITE_PREFIX_LENGTH, executable=True)
        mapped_patch = elf.v2o(patch_va, len(ORIGINAL), executable=True)
        mapped_context = elf.v2o(context_va, CONTEXT_LENGTH, executable=True)
        mapped_table = elf.v2o(table_va, TABLE_LENGTH)
    except ValueError as exc:
        raise PatchError(str(exc)) from exc
    if mapped_site != file_offset:
        raise PatchError("file_offset does not match the ELF mapping")
    if mapped_patch != patch_offset:
        raise PatchError("replacement start does not match the ELF mapping")
    if mapped_context != context_offset:
        raise PatchError("context_offset does not match the ELF mapping")
    if mapped_table != table_offset:
        raise PatchError("table_file_offset does not match the ELF mapping")
    if not (0 <= patch_offset <= len(data) - len(ORIGINAL)):
        raise PatchError("patch lies outside the file")

    patched_context = bytearray(context)
    patched_context[CONTEXT_PATCH_DELTA:CONTEXT_PATCH_DELTA + len(REPLACEMENT)] = REPLACEMENT
    actual_hash = _sha256(data)
    if actual_hash == original_hash:
        state = "original"
        expected_site = ORIGINAL
        expected_context = context
    elif actual_hash == patched_hash:
        state = "patched"
        expected_site = REPLACEMENT
        expected_context = bytes(patched_context)
    else:
        raise PatchError(f"unknown full-file SHA256 {actual_hash}")

    if data[patch_offset:patch_offset + len(expected_site)] != expected_site:
        raise PatchError(f"{state} file has unexpected bytes at the patch site")
    if data[context_offset:context_offset + CONTEXT_LENGTH] != expected_context:
        raise PatchError(f"{state} file has unexpected function context")
    if data[table_offset:table_offset + TABLE_LENGTH] != table:
        raise PatchError("constant table differs from the manifest")

    patched = bytearray(data)
    patched[patch_offset:patch_offset + len(REPLACEMENT)] = REPLACEMENT
    patched = bytes(patched)
    if _sha256(patched) != patched_hash:
        raise PatchError("replacement does not produce patched_sha256")
    return {
        "state": state,
        "patched": patched,
        "sha256": actual_hash,
        "original_sha256": original_hash,
        "patched_sha256": patched_hash,
        "site_offset": file_offset,
        "patch_offset": patch_offset,
    }


def _resolve_host_path(path, allow_missing_leaf=False):
    """Resolve host parents while refusing a symlink at the named leaf."""
    path = Path(path).absolute()
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if not allow_missing_leaf:
            raise PatchError(f"path does not exist: {path}")
        try:
            parent = path.parent.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PatchError(f"path parent does not exist: {path.parent}") from exc
        return parent / path.name
    if stat.S_ISLNK(mode):
        raise PatchError(f"symlink path refused: {path}")
    return path.resolve(strict=True)


def _target_path(root, relative):
    if not isinstance(relative, str) or not relative:
        raise PatchError("target path must be a non-empty relative path")
    rel = Path(relative)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise PatchError(f"unsafe target path: {relative!r}")
    target = root
    for part in rel.parts:
        target /= part
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError as exc:
            raise PatchError(f"target path component does not exist: {target}") from exc
        if stat.S_ISLNK(mode):
            raise PatchError(f"symlink target path component refused: {target}")
    target_stat = target.lstat()
    mode = target_stat.st_mode
    if not stat.S_ISREG(mode):
        raise PatchError(f"target is not a regular file: {relative}")
    if target_stat.st_nlink != 1:
        raise PatchError(
            f"duplicate target inode / hardlink target refused: {relative} "
            f"has st_nlink={target_stat.st_nlink}, expected 1"
        )
    return target


def _same_file_or_path(first, second):
    if first == second:
        return True
    try:
        return os.path.samefile(first, second)
    except FileNotFoundError:
        return False


def _load_manifest(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PatchError(f"cannot read manifest: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise PatchError(f"manifest schema must be {SCHEMA}")
    rows = value.get("targets")
    if not isinstance(rows, list) or len(rows) != TARGET_COUNT:
        raise PatchError(f"manifest must contain exactly {TARGET_COUNT} targets")
    paths = [row.get("path") if isinstance(row, dict) else None for row in rows]
    if frozenset(paths) != EXPECTED_PATHS or len(set(paths)) != TARGET_COUNT:
        raise PatchError("manifest target paths do not match the fixed 17-target set")
    return rows


def _load_manager_manifest(path):
    """The separate guest scope cannot expand the reviewed host target set."""
    try:
        with path.open("rb") as stream:
            data = stream.read(8193)
    except OSError as exc:
        raise PatchError(f"cannot read manager manifest: {exc}") from exc
    if len(data) > 8192 or _sha256(data) != MANAGER_MANIFEST_SHA256:
        raise PatchError("manager manifest does not match the fixed SHA256")
    try:
        row = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PatchError(f"cannot decode manager manifest: {exc}") from exc
    if not isinstance(row, dict) or row.get("path") != "system/bin/microdroid_manager":
        raise PatchError("manager manifest has an unapproved target path")
    return row


def _validate_manager(data, row):
    result = validate(data, row)
    function_hashes = {
        key: _hex(row.get(key), 32, f"{key} function SHA256").hex()
        for key in ("function_sha256", "patched_function_sha256")
    }
    try:
        offset = Elf(data).v2o(_integer(row["function_va"], "function_va"),
                               _integer(row["function_size"], "function_size"), executable=True)
    except ValueError as exc:
        raise PatchError(f"manager function mapping refused: {exc}") from exc
    size = _integer(row["function_size"], "function_size")
    source_key = "function_sha256" if result["state"] == "original" else "patched_function_sha256"
    for content, key in ((data, source_key), (result["patched"], "patched_function_sha256")):
        if _sha256(content[offset:offset + size]) != function_hashes[key]:
            raise PatchError(f"manager function SHA256 mismatch: {key}")
    return result


def _select_scope(rows, scope):
    if scope is None:
        return [(row["path"], row) for row in rows]
    kind, name = scope
    if (kind, name) not in SCOPED_COUNTS:
        raise PatchError(f"unknown patch scope: {kind}:{name}")
    selected = []
    for row in rows:
        if kind == "partition":
            if row.get("partition") == name and "image_path" in row and "apex_path" not in row:
                selected.append((row["image_path"], row))
        elif kind == "apex":
            if row.get("apex_path") == name and isinstance(row.get("payload_path"), str):
                selected.append((row["payload_path"].lstrip("/"), row))
    expected_count = SCOPED_COUNTS[(kind, name)]
    if len(selected) != expected_count:
        raise PatchError(f"scope {kind}:{name} selected {len(selected)} targets, expected {expected_count}")
    return selected


def _atomic_replace(path, data, mode):
    descriptor = None
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".patch-ifma-", dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, stat.S_IMODE(mode))
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def run(root, manifest_path, report_path, apply, scope=None):
    root = _resolve_host_path(root)
    if not root.is_dir():
        raise PatchError("--root must be a directory")
    manifest_path = _resolve_host_path(manifest_path)
    if not manifest_path.is_file():
        raise PatchError("manifest must be a regular file")
    report_path = _resolve_host_path(report_path, allow_missing_leaf=True)
    if report_path.exists() and not stat.S_ISREG(report_path.lstat().st_mode):
        raise PatchError("existing report path must be a regular file")

    if scope == MANAGER_SCOPE:
        row = _load_manager_manifest(manifest_path)
        selected = [(row["path"], row)]
        validator = _validate_manager
    else:
        rows = _load_manifest(manifest_path)
        selected = _select_scope(rows, scope)
        validator = validate
    protected = [manifest_path, Path(__file__).absolute(), Path(__file__).with_name("elfutil.py").absolute()]
    targets = []
    for rel_path, row in selected:
        target = _target_path(root, rel_path)
        if any(_same_file_or_path(target, existing[0]) for existing in targets):
            raise PatchError(f"duplicate target inode: {row['path']}")
        if any(_same_file_or_path(target, item) for item in protected):
            raise PatchError(f"target aliases a protected input/helper: {row['path']}")
        if _same_file_or_path(report_path, target):
            raise PatchError("report path aliases a target binary")
        data = target.read_bytes()
        result = validator(data, row)
        targets.append((target, target.stat().st_mode, result, row, rel_path))

    if any(_same_file_or_path(report_path, item) for item in protected):
        raise PatchError("report path aliases the manifest or a helper")

    changed = []
    if apply:
        for target, mode, result, row, rel_path in targets:
            if result["state"] == "original":
                _atomic_replace(target, result["patched"], mode)
                readback_hash = _sha256(target.read_bytes())
                if readback_hash != result["patched_sha256"]:
                    raise PatchError(f"post-replace SHA256 mismatch: {row['path']}")
                changed.append(row["path"])

    report = {
        "schema": SCHEMA,
        "mode": "apply" if apply else "verify",
        "ok": True,
        "targets": [
            {
                "path": row["path"],
                "rel_path": rel_path,
                "before": result["state"],
                "after": "patched" if apply else result["state"],
                "changed": apply and result["state"] == "original",
                "original_sha256": result["original_sha256"],
                "patched_sha256": result["patched_sha256"],
                "site_offset": result["site_offset"],
                "patch_offset": result["patch_offset"],
            }
            for _, _, result, row, rel_path in targets
        ],
        "changed": changed,
        "limitations": [
            "The batch is fully preflighted but cannot be crash-atomic across multiple files.",
            "Replacement preserves ordinary permission bits, not ownership, SELinux labels, or extended attributes; packaging must restore those attributes.",
            "The manifest is trusted review input and the extracted root must have no concurrent writers during verification or apply.",
        ],
    }
    if scope is not None:
        report["scope"] = f"{scope[0]}:{scope[1]}"
    report_path.parent.mkdir(parents=False, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="root of the extracted ELF tree")
    parser.add_argument("--report", required=True, help="JSON report output path")
    parser.add_argument("--manifest",
                        help="trusted reviewed manifest (default: host manifest, or pinned manager manifest for microdroid:system)")
    parser.add_argument("--apply", action="store_true", help="atomically replace each validated original")
    parser.add_argument("--scope", help="optional partition:<name>, apex:<apex_path>, or explicit microdroid:system scope")
    args = parser.parse_args(argv)
    scope = None
    if args.scope:
        if ":" not in args.scope:
            print(f"patch-ifma: refused: invalid --scope {args.scope!r}", file=sys.stderr)
            return 1
        scope = tuple(args.scope.split(":", 1))
    manifest = args.manifest or str(Path(__file__).with_name(
        "microdroid-manager.json" if scope == MANAGER_SCOPE else "manifest.json"))
    try:
        report = run(args.root, manifest, args.report, args.apply, scope=scope)
    except (OSError, PatchError) as exc:
        print(f"patch-ifma: refused: {exc}", file=sys.stderr)
        return 1
    total = len(report["targets"])
    originals = sum(row["before"] == "original" for row in report["targets"])
    patched = total - originals
    print(f"verified {total} targets: {originals} original, {patched} already patched; changed {len(report['changed'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
