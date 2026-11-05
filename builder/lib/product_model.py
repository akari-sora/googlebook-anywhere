#!/usr/bin/env python3
"""Opt-in, pinned Android product model correction for the KANO candidate."""
import base64
import binascii
import hashlib
import os
import re
import shutil
import struct
import stat
from pathlib import Path

import ifma
import rebuild

BUILD = "16658739"
UUID = "4ac2bb47-a593-529d-a81e-c5683f4c56ee"
PARTITION_SIZE = 1970524160
ORIGINAL_SIZE = 1933541376
SALT = "948f2e15ed1a9b70a1260b551529feb39ace85747fddf9ef78397ff9c15ccab3"
ROOT_DIGEST = "05b5595c88d60998d92db90af6f14b6ccad20c733597367511b4731eb7a7de66"
FINGERPRINT = "ASUS/lapis/lapis:17/CL3B.260622.276.R4/16658739:user/release-keys"
PROPERTY = b"ro.product.product.model"
OLD_VALUE = b"ASUS Googlebook 14"
MODEL = "Acer Chromebook Spin 714"

# The reviewed prebuilt APK is data, not a new build-time signing dependency.
SKIP_APK_SHA256 = "23936a909cf3777539405f5c449cb3a3d28a65c92f0cef380f42fa4ce79ba236"
SKIP_APK = Path(__file__).resolve().parents[1] / "setup/optional-screen-lock/GooglebookOptionalScreenLockOverlay.apk.base64"
SKIP_DEST = "overlay/GooglebookOptionalScreenLockOverlay.apk"
SKIP_TEMPLATE = "overlay/SettingsDesktopOverlay.apk"
SKIP_TEMPLATE_SHA256 = "c7a0fd405bbaaba83c8ea81ee3bbef0f740429324d056ba7fce7be9133cafd1c"


def validate_options(profile, env):
    value = env.get("GBOS_ANDROID_MODEL", "")
    skip = env.get("GBOS_SETUP_PIN_SKIP", "")
    if skip not in ("", "1"):
        raise ValueError("GBOS_SETUP_PIN_SKIP must be empty or 1")
    if value == "":
        if skip:
            raise ValueError("GBOS_SETUP_PIN_SKIP=1 requires GBOS_ANDROID_MODEL=profile")
        return False
    if value != "profile":
        raise ValueError("GBOS_ANDROID_MODEL must be empty or profile")
    if (profile["model"]["id"] != "kano" or profile["model"].get("android_model") != MODEL):
        raise ValueError("GBOS_ANDROID_MODEL=profile requires the pinned KANO model")
    if env.get("GBOS_LAPIS_BUILD", "16452207") != BUILD:
        raise ValueError(f"GBOS_ANDROID_MODEL=profile requires Google build {BUILD}")
    return True


def read_skip_apk():
    """Load only the reviewed in-tree artifact and reject a placeholder pin."""
    if not re.fullmatch(r"[0-9a-f]{64}", SKIP_APK_SHA256):
        raise SystemExit("setup PIN skip APK digest is not pinned")
    if SKIP_APK.is_symlink() or not SKIP_APK.is_file():
        raise SystemExit("setup PIN skip APK is not a regular in-tree file")
    if SKIP_APK.stat().st_size > 2 * 1024 * 1024:
        raise SystemExit("setup PIN skip APK encoding is oversized")
    try:
        data = base64.b64decode(b"".join(SKIP_APK.read_bytes().splitlines()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SystemExit("setup PIN skip APK encoding is invalid") from exc
    if not data or len(data) > 1024 * 1024 or hashlib.sha256(data).hexdigest() != SKIP_APK_SHA256:
        raise SystemExit("setup PIN skip APK digest drifted")
    return data


def install_skip_overlay(tree, index, metadata, apk):
    """Add exactly one overlay using the pinned stock overlay's metadata."""
    if index.get("overlay") != ("dir", None):
        raise SystemExit("product overlay directory missing or not a directory")
    if index.get(SKIP_TEMPLATE) != ("file", SKIP_TEMPLATE_SHA256):
        raise SystemExit("stock desktop Settings overlay drifted")
    if SKIP_DEST in index or SKIP_DEST in metadata:
        raise SystemExit("setup PIN skip overlay already exists")
    # Any config tree can affect overlay ordering; its contents require review.
    if any(path == "overlay/config" or path.startswith("overlay/config/") for path in index):
        raise SystemExit("product overlay configuration needs explicit precedence review")
    source = metadata.get(SKIP_TEMPLATE)
    if (not isinstance(source, dict) or source.get("file_type") != stat.S_IFREG
            or source.get("mode") != 0o644 or source.get("uid") != 0
            or source.get("gid") != 0 or source.get("nlink") != 1
            or source.get("caps") or source.get("selinux") != "u:object_r:system_file:s0"
            or source.get("mtime_sec") != 1230768000 or source.get("mtime_nsec") != 0
            or source.get("xattrs") != {"security.selinux": b"u:object_r:system_file:s0"}):
        raise SystemExit("stock Settings overlay metadata is unsafe to clone")
    dest = tree / SKIP_DEST
    if dest.exists() or dest.is_symlink():
        raise SystemExit("setup PIN skip destination already exists")
    with dest.open("xb") as out:
        out.write(apk)
    dest.chmod(0o644)
    metadata[SKIP_DEST] = dict(source, xattrs=dict(source["xattrs"]))


def assert_skip_metadata(old_erofs, new_erofs, expected):
    """Retain all existing EROFS metadata and allow only the exact new inode."""
    fields = ("uuid", "build_time", "build_time_nsec", "compat", "incompat", "volume_name", "pcluster_size")
    old_sb = rebuild.superblock_params(old_erofs, strict_metadata=True)
    new_sb = rebuild.superblock_params(new_erofs, strict_metadata=True)
    if any(old_sb.get(field) != new_sb.get(field) for field in fields):
        raise SystemExit("product_a EROFS superblock metadata drifted")
    old = ifma._erofs_meta_map(old_erofs)
    new = ifma._erofs_meta_map(new_erofs)
    if SKIP_DEST in old or set(new) != set(old) | {SKIP_DEST}:
        raise SystemExit("product_a EROFS inode paths drifted")
    inode_fields = ("file_type", "mode", "uid", "gid", "nlink", "mtime_sec", "mtime_nsec", "xattrs")
    for path in new:
        wanted = expected if path == SKIP_DEST else old[path]
        if any(new[path].get(field) != wanted.get(field) for field in inode_fields):
            raise SystemExit(f"product_a EROFS inode metadata drifted on {path}")


def assert_product_tree(index, actual_index, rebuilt, expected_prop, skip_apk):
    additions = {SKIP_DEST} if skip_apk is not None else set()
    if (set(actual_index) != set(index) | additions or
            any(index[p] != actual_index[p] for p in index if p != "etc/build.prop") or
            (rebuilt / "etc/build.prop").read_bytes() != expected_prop or
            (skip_apk is not None and actual_index.get(SKIP_DEST) !=
             ("file", hashlib.sha256(skip_apk).hexdigest()))):
        raise SystemExit("product_a tree changed outside the approved model property and optional setup overlay")


def replace_model(data, model):
    """Change only one exact property line, preserving every other byte."""
    if model != MODEL:
        raise ValueError("unapproved product model")
    lines = data.splitlines(keepends=True)
    keyed = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(b"#") and b"=" in stripped:
            key = stripped.split(b"=", 1)[0].strip()
            if key == PROPERTY:
                keyed.append(i)
        # A malformed active property without '=' must not be silently ignored.
        elif not stripped.startswith(b"#") and stripped.startswith(PROPERTY):
            raise ValueError("malformed product model property")
    if len(keyed) != 1:
        raise ValueError("product model property must occur exactly once")
    i = keyed[0]
    if lines[i] != PROPERTY + b"=" + OLD_VALUE + b"\n":
        raise ValueError("original product model property drifted")
    lines[i] = PROPERTY + b"=" + model.encode("ascii") + b"\n"
    return b"".join(lines)


def assert_stock_avb(info):
    actual = ifma._parse_partition_avb_info(info, "product_a")
    expected = {
        "partition_name": "product", "salt": SALT,
        "props": {"com.android.build.product.os_version": "17",
                  "com.android.build.product.fingerprint": FINGERPRINT,
                  "com.android.build.product.security_patch": "2026-10-01"},
        "hashtree": ifma.SYSTEM_EXT_HASHTREE,
    }
    digests = re.findall(r"^\s*Root Digest:\s*([0-9a-fA-F]+)\s*$", info, re.MULTILINE)
    if actual != expected or len(digests) != 1 or digests[0].lower() != ROOT_DIGEST:
        raise SystemExit("product_a stock AVB identity drifted")


def assert_root_product_descriptor(info):
    descriptors = []
    block = None
    for line in info.splitlines():
        item = line.strip()
        if item.endswith("descriptor:"):
            if block is not None:
                descriptors.append(block)
            block = [] if item == "Hashtree descriptor:" else None
        elif item.startswith("Prop:"):
            if block is not None:
                descriptors.append(block)
            block = None
        elif block is not None:
            block.append(item)
    if block is not None:
        descriptors.append(block)
    def values(block, key):
        return [value.strip() for line in block if ":" in line
                for name, value in [line.split(":", 1)] if name.strip() == key]

    product = [block for block in descriptors if "product" in values(block, "Partition Name")]
    if len(product) != 1:
        raise SystemExit("root vbmeta must contain one product hashtree descriptor")
    if values(product[0], "Partition Name") != ["product"]:
        raise SystemExit("root vbmeta product partition name is ambiguous")
    digest = [value.lower() for value in values(product[0], "Root Digest")]
    if digest != [ROOT_DIGEST]:
        raise SystemExit("root vbmeta product digest differs from stock product")


def assert_exclusive_extent(img_path, super_off, expected_off, expected_size):
    """Check the product LP extent's source and overlap against the full extent table."""
    with Path(img_path).open("rb") as f:
        f.seek(super_off + 12288)
        header = f.read(256)
        if len(header) != 256 or header[:4] != b"0PLA":
            raise SystemExit("invalid LP metadata header")
        header_size = struct.unpack_from("<I", header, 8)[0]
        tables_size = struct.unpack_from("<I", header, 44)[0]
        ext_off, ext_num, ext_size = struct.unpack_from("<III", header, 92)
        if header_size < 128 or tables_size > 16 * 1024 * 1024 or ext_size < 24:
            raise SystemExit("invalid LP extent table geometry")
        if ext_off + ext_num * ext_size > tables_size:
            raise SystemExit("LP extent table exceeds metadata")
        f.seek(super_off + 12288 + header_size)
        table = f.read(tables_size)
    if len(table) != tables_size:
        raise SystemExit("truncated LP extent table")
    matching = 0
    for n in range(ext_num):
        sectors, kind, start, source = struct.unpack_from("<QIQI", table, ext_off + n * ext_size)
        if kind != 0 or not sectors:
            continue
        off, size = start * 512, sectors * 512
        if (off, size) == (expected_off, expected_size):
            if source != 0:
                raise SystemExit("product_a extent uses another block device")
            matching += 1
        elif source == 0 and off < expected_off + expected_size and expected_off < off + size:
            raise SystemExit("product_a extent overlaps another LP extent")
    if matching != 1:
        raise SystemExit("product_a extent must be unique in LP metadata")


def rebuild_product_partition(profile, img_path, tmp, otatools=None):
    if not validate_options(profile, os.environ):
        return None
    skip_apk = read_skip_apk() if os.environ.get("GBOS_SETUP_PIN_SKIP", "") == "1" else None
    img_path, tmp = Path(img_path), Path(tmp)
    otatools = Path(otatools or os.environ["GBOS_OTATOOLS"])
    super_part = ifma._gpt_partitions(img_path)["super"]
    rel_off, size = ifma._lp_Named_extent(img_path, super_part["start"], "product_a")
    if rel_off < 0 or size != PARTITION_SIZE or rel_off + size > super_part["size"]:
        raise SystemExit("product_a geometry does not match pinned super extent")
    assert_exclusive_extent(img_path, super_part["start"], rel_off, size)
    offset = super_part["start"] + rel_off
    # avbtool resolves the hashtree descriptor's partition name to this basename.
    stock_partition = tmp / "product.img"
    ifma._copy_partition_extent(img_path, offset, size, stock_partition, partition_name="product_a")
    try:
        assert_stock_avb(ifma._run_ot(otatools, "avbtool", "info_image", "--image", stock_partition))
        ifma._run_ot(otatools, "avbtool", "verify_image", "--image", stock_partition)
    finally:
        stock_partition.unlink(missing_ok=True)
    source = tmp / "product.erofs.src"
    ifma._copy_erofs_extent(img_path, offset, size, source, partition_name="product_a")
    if source.stat().st_size != ORIGINAL_SIZE:
        raise SystemExit("product_a original EROFS length drifted")
    sb = rebuild.superblock_params(source, strict_metadata=True)
    if sb["uuid"].lower() != UUID:
        raise SystemExit("product_a EROFS UUID drifted")
    metadata = ifma._erofs_meta_map(source)
    tree = tmp / "product-tree"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    ifma._run_ot(otatools, "fsck.erofs", f"--extract={tree}", "--overwrite", "--preserve-perms", source)
    index = ifma._index_tree(tree)
    prop = tree / "etc/build.prop"
    if index.get("etc/build.prop", (None,))[0] != "file":
        raise SystemExit("product build.prop is missing or not a regular file")
    expected_prop = replace_model(prop.read_bytes(), profile["model"]["android_model"])
    prop.write_bytes(expected_prop)
    if skip_apk is not None:
        install_skip_overlay(tree, index, metadata, skip_apk)
    fc, fs = tmp / "product_file_contexts", tmp / "product_fs_config"
    rebuild.write_configs("/product", tree, metadata, fc, fs, allow_caps=True)
    new = tmp / "product.erofs"
    new.unlink(missing_ok=True)
    args = ["-z", "lz4hc,level=9", "-b", "4096", "-T", str(sb["build_time"]), "-U", sb["uuid"]]
    if sb["volume_name"]:
        args += ["-L", sb["volume_name"]]
    if sb["pcluster_size"]:
        args += ["-C", str(sb["pcluster_size"])]
    rebuild.restore_mtimes(tree, metadata)
    args += ["--mount-point", "/product", "--fs-config-file", str(fs),
             "--file-contexts", str(fc), str(new), str(tree)]
    ifma._run_ot(otatools, "mkfs.erofs", *args)
    if rebuild.fs_uuid(new).lower() != UUID:
        raise SystemExit("product_a rebuilt UUID drifted")
    if skip_apk is None:
        ifma._assert_same_erofs_metadata(source, new, "product_a")
    else:
        assert_skip_metadata(source, new, metadata[SKIP_DEST])
    rebuilt = tmp / "product-tree-new"
    if rebuilt.exists():
        shutil.rmtree(rebuilt)
    rebuilt.mkdir(parents=True)
    ifma._run_ot(otatools, "fsck.erofs", f"--extract={rebuilt}", "--overwrite", "--preserve-perms", new)
    actual_index = ifma._index_tree(rebuilt)
    assert_product_tree(index, actual_index, rebuilt, expected_prop, skip_apk)
    partition = tmp / "product.img"
    shutil.copyfile(new, partition)
    ifma._run_ot(otatools, "avbtool", "add_hashtree_footer", "--image", partition,
                 "--partition_name", "product", "--partition_size", str(size),
                 "--hash_algorithm", "sha256", "--salt", SALT, "--block_size", "4096",
                 "--fec_num_roots", "2", "--prop", "com.android.build.product.os_version:17",
                 "--prop", f"com.android.build.product.fingerprint:{FINGERPRINT}",
                 "--prop", "com.android.build.product.security_patch:2026-10-01")
    if partition.stat().st_size != size:
        raise SystemExit("rebuilt product_a partition size drifted")
    ifma._run_ot(otatools, "avbtool", "verify_image", "--image", partition)
    offsets = tmp / "offsets.env"
    content = offsets.read_text(encoding="utf-8")
    if re.search(r"^PRODUCT_(OFF|SIZE)=", content, re.MULTILINE):
        raise SystemExit("product_a offsets already present")
    offsets.write_text(content + f"PRODUCT_OFF={offset}\nPRODUCT_SIZE={size}\n", encoding="utf-8")
    return {"product_off": offset, "product_size": size, "product_img": partition}
