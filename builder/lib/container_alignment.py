#!/usr/bin/env python3
"""Fail closed on misaligned APK/APEX ZIP entries in extracted trees or a disk image."""
import argparse
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PARTITIONS = ("system", "system_ext", "product", "vendor", "odm")
# Android runtime mounts outside the five factory partitions. They are not
# followed on the host; absolute references into factory partitions are also
# safe because those partitions are scanned separately by check_image().
ANDROID_MOUNTS = frozenset(PARTITIONS + ("apex", "system_dlkm", "vendor_dlkm"))
ANDROID_EXTERNAL_LINKS = {
    ("cache", "/data/cache"),
    ("bugreports", "/data/user_de/0/com.android.shell/files/bugreports"),
    ("d", "/sys/kernel/debug"),
    ("sdcard", "/storage/self/primary"),
}


def check_zip_layout(path):
    """Inspect local-header offsets, including extra fields; never rewrite signed ZIPs."""
    path = Path(path)
    try:
        with path.open("rb") as stream, zipfile.ZipFile(stream) as archive:
            names = set()
            for entry in archive.infolist():
                if entry.filename in names:
                    raise ValueError(f"duplicate ZIP entry {entry.filename!r}")
                names.add(entry.filename)
                stream.seek(entry.header_offset)
                header = stream.read(30)
                if len(header) != 30 or header[:4] != b"PK\x03\x04":
                    raise ValueError(f"invalid local header for {entry.filename!r}")
                flags, method = struct.unpack_from("<HH", header, 6)
                name_len, extra_len = struct.unpack_from("<HH", header, 26)
                local_name = stream.read(name_len)
                encoding = "utf-8" if flags & 0x800 else "cp437"
                if method != entry.compress_type or local_name != entry.filename.encode(encoding):
                    raise ValueError(f"local/central header mismatch for {entry.filename!r}")
                data_offset = entry.header_offset + 30 + name_len + extra_len
                if data_offset + entry.compress_size > path.stat().st_size:
                    raise ValueError(f"truncated ZIP entry {entry.filename!r}")
                if entry.filename == "resources.arsc" and method != zipfile.ZIP_STORED:
                    raise ValueError("resources.arsc is compressed")
                if path.suffix.lower() == ".apex" and entry.filename == "apex_payload.img":
                    if method != zipfile.ZIP_STORED:
                        raise ValueError("apex_payload.img is compressed")
                    if data_offset % 4096:
                        raise ValueError(f"apex_payload.img offset {data_offset} is not 4096-aligned")
                if method == zipfile.ZIP_STORED and data_offset % 4:
                    raise ValueError(f"{entry.filename}: offset {data_offset} is not 4-aligned")
                if method == zipfile.ZIP_STORED and entry.filename.endswith(".so") and data_offset % 16384:
                    raise ValueError(f"{entry.filename}: offset {data_offset} is not 16384-aligned")
            if path.suffix.lower() == ".apex" and "apex_payload.img" not in names:
                raise ValueError("missing apex_payload.img")
    except (OSError, zipfile.BadZipFile, UnicodeError, struct.error, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def check_archive(path, zipalign=None):
    check_zip_layout(path)
    if zipalign:
        result = subprocess.run([str(zipalign), "-c", "-P", "16", "4", str(path)],
                                capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f"{path}: zipalign check failed: {result.stderr.strip() or result.stdout.strip()}")


def _tree_entries(root):
    # Path.rglob() skips directories it cannot list, and os.walk() falls back to "not a directory" when classifying an
    # entry fails. List and lstat every entry explicitly: any error fails the gate. Symlinks are never descended.
    pending = [root]
    while pending:
        top = pending.pop()
        try:
            names = os.listdir(top)
        except OSError as exc:
            raise ValueError(f"unreadable directory: {top}: {exc.strerror}") from exc
        for name in names:
            path = top / name
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                raise ValueError(f"cannot stat {path}: {exc.strerror}") from exc
            if stat.S_ISDIR(mode):
                pending.append(path)
            yield path


def check_tree(root, zipalign=None):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    count = 0
    for path in sorted(_tree_entries(root)):
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise ValueError(f"cannot stat {path}: {exc.strerror}") from exc
        if stat.S_ISLNK(mode):
            if path.suffix.lower() in (".apk", ".apex"):
                raise ValueError(f"symlink container is not allowed: {path}")
            target = path.readlink()
            if target.is_absolute():
                if ".." in target.parts or (
                    len(target.parts) < 2 or target.parts[1] not in ANDROID_MOUNTS
                ) and (str(path.relative_to(root)), str(target)) not in ANDROID_EXTERNAL_LINKS:
                    raise ValueError(f"unscanned absolute symlink: {path} -> {target}")
            else:
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise ValueError(f"unresolvable symlink: {path} -> {target}") from exc
                if not resolved.is_relative_to(root):
                    raise ValueError(f"symlink escapes partition: {path} -> {target}")
            # The real directory entries are visited separately. Never follow
            # a symlink into the host filesystem or traverse a symlink cycle.
            continue
        if not stat.S_ISREG(mode) or path.suffix.lower() not in (".apk", ".apex"):
            continue
        check_archive(path, zipalign)
        count += 1
    return count


def find_zipalign(otatools):
    explicit = os.environ.get("GBOS_ZIPALIGN")
    if explicit:
        if not os.path.isfile(explicit) or not os.access(explicit, os.X_OK):
            raise ValueError(f"GBOS_ZIPALIGN is not executable: {explicit}")
        return explicit
    if otatools:
        candidate = Path(otatools) / "bin/zipalign"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("zipalign")


def check_image(image, otatools, zipalign=None):
    # Imported here so tree-only checks work independently of IFMA build pins.
    import ifma
    image = Path(image)
    super_part = ifma._gpt_partitions(image).get("super")
    if super_part is None:
        raise ValueError("image has no super partition")
    if not otatools:
        raise ValueError("--otatools is required for image scans")
    fsck = Path(otatools) / "bin/fsck.erofs"
    if not fsck.is_file() or not os.access(fsck, os.X_OK):
        raise ValueError(f"missing executable: {fsck}")
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(Path(otatools) / "lib64") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    count = 0
    found = set()
    with tempfile.TemporaryDirectory(prefix="gbos-align-") as temporary:
        tmp = Path(temporary)
        for partition in PARTITIONS:
            for slot in ("a", "b"):
                name = f"{partition}_{slot}"
                try:
                    relative, size = ifma._lp_Named_extent(image, super_part["start"], name)
                except SystemExit as exc:
                    if str(exc) in (f"{name} missing in super", f"{name} extents 0"):
                        print(f"alignment {name}: absent or empty", flush=True)
                        continue
                    raise
                if relative < 0 or relative + size > super_part["size"]:
                    raise ValueError(f"{name}: extent outside super partition")
                found.add(name)
                erofs = tmp / "partition.img"
                tree = tmp / "tree"
                ifma._copy_erofs_extent(image, super_part["start"] + relative, size, erofs, partition_name=name)
                subprocess.run([str(fsck), f"--extract={tree}", "--overwrite", str(erofs)],
                               check=True, env=env, stdout=subprocess.DEVNULL)
                checked = check_tree(tree, zipalign)
                count += checked
                print(f"alignment {name}: {checked} containers checked", flush=True)
                shutil.rmtree(tree)
                erofs.unlink()
    for required in ("system_a", "system_ext_a", "product_a", "vendor_a"):
        if required not in found:
            raise ValueError(f"required partition {required} is absent")
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path)
    source.add_argument("--tree", type=Path)
    parser.add_argument("--otatools", type=Path, default=os.environ.get("GBOS_OTATOOLS"))
    args = parser.parse_args(argv)
    try:
        zipalign = find_zipalign(args.otatools)
        if args.image and not zipalign:
            raise ValueError("zipalign is required for image scans; set GBOS_ZIPALIGN")
        print(f"zipalign oracle: {zipalign}" if zipalign else "SKIP zipalign oracle: tool unavailable", flush=True)
        count = check_image(args.image, args.otatools, zipalign) if args.image else check_tree(args.tree, zipalign)
        if not count:
            raise ValueError("no APK or APEX containers found")
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"FAIL container alignment: {exc}", file=sys.stderr)
        return 1
    print(f"PASS container alignment: {count} APK/APEX containers checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
