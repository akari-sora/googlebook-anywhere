#!/usr/bin/env python3
"""EROFS metadata helpers for the vendor rebuild: read every inode's mode/uid/gid/SELinux label/capabilities from an
image (pure Python, no dump.erofs), and write the fs_config + file_contexts that make mkfs.erofs reproduce them."""

import os
import re
import struct
import uuid

PREFIX = {
    0: "",
    1: "user.",
    2: "system.posix_acl_access",
    3: "system.posix_acl_default",
    4: "trusted.",
    5: "lustre.",
    6: "security.",
    7: "system.",
}
EROFS_MAGIC = 0xE0F5E1E2


def load_image(path):
    with open(path, "rb") as stream:
        data = stream.read()
    sb = 1024
    if struct.unpack_from("<I", data, sb)[0] != EROFS_MAGIC:
        raise SystemExit(f"not an EROFS image: {path}")
    blkszbits = data[sb + 12]
    blksz = 1 << blkszbits
    meta = struct.unpack_from("<I", data, sb + 40)[0]
    xattr_blk = struct.unpack_from("<I", data, sb + 44)[0]
    root = struct.unpack_from("<H", data, sb + 14)[0]
    return data, blksz, meta, xattr_blk, root


def superblock_params(path, *, strict_metadata=False):
    with open(path, "rb") as f:
        f.seek(1024)
        sb = f.read(144)
    if len(sb) < 128 or struct.unpack_from("<I", sb, 0)[0] != EROFS_MAGIC:
        raise SystemExit(f"not an EROFS image: {path}")
    blksz = 1 << sb[12]
    build_time = struct.unpack_from("<Q", sb, 24)[0]
    build_time_nsec = struct.unpack_from("<I", sb, 32)[0]
    if strict_metadata and build_time_nsec >= 1_000_000_000:
        raise SystemExit(f"invalid EROFS build-time nanoseconds {build_time_nsec}: {path}")
    u = str(uuid.UUID(bytes=sb[48:64]))
    compat = struct.unpack_from("<I", sb, 8)[0]
    volume_name = sb[64:80].split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
    incompat = struct.unpack_from("<I", sb, 80)[0]
    compr_algs = struct.unpack_from("<H", sb, 84)[0]
    pcluster_size = None
    if (incompat & 0x2) and (compr_algs & 0x1) and len(sb) >= 134:
        cfg_size, _max_dist, max_pclusterblks = struct.unpack_from("<HHH", sb, 128)
        if cfg_size >= 6 and max_pclusterblks > 0:
            pcluster_size = max_pclusterblks * blksz
    elif incompat & 0x2:
        pcluster_size = 32768
    return {
        "uuid": u,
        "build_time": build_time,
        "build_time_nsec": build_time_nsec,
        "compat": compat,
        "incompat": incompat,
        "volume_name": volume_name,
        "pcluster_size": pcluster_size,
    }


def fs_uuid(path):
    return superblock_params(path)["uuid"]


def parse_entry(data, off):
    nlen = data[off]
    nindex = data[off + 1]
    vsize = struct.unpack_from("<H", data, off + 2)[0]
    name = data[off + 4 : off + 4 + nlen]
    raw = data[off + 4 + nlen : off + 4 + nlen + vsize]
    full = PREFIX.get(nindex, f"idx{nindex}.") + name.decode("latin1")
    esize = (4 + nlen + vsize + 3) & ~3
    return full, raw, esize


def inode_raw(data, blksz, meta, nid, *, strict_metadata=False):
    """Inode header fields (EROFS on-disk compact/extended layouts)."""
    off = meta * blksz + nid * 32
    fmt, xcount, mode = struct.unpack_from("<HHH", data, off)
    extended = fmt & 1
    if extended:
        size = struct.unpack_from("<Q", data, off + 8)[0]
        uid, gid = struct.unpack_from("<II", data, off + 24)
        mtime_sec = struct.unpack_from("<Q", data, off + 32)[0]
        mtime_nsec = struct.unpack_from("<I", data, off + 40)[0]
        if strict_metadata and mtime_nsec >= 1_000_000_000:
            raise SystemExit(f"inode {nid}: invalid mtime nanoseconds {mtime_nsec}")
        nlink = struct.unpack_from("<I", data, off + 44)[0]
        isize = 64
    else:
        nlink, size = struct.unpack_from("<HI", data, off + 6)
        uid, gid = struct.unpack_from("<HH", data, off + 24)
        mtime_sec = None
        mtime_nsec = None
        isize = 32
    iu = struct.unpack_from("<I", data, off + 16)[0]
    xsize = 12 + 4 * (xcount - 1) if xcount else 0
    return dict(off=off, fmt=fmt, layout=(fmt >> 1) & 7, xcount=xcount, mode=mode, extended=extended,
                size=size, uid=uid, gid=gid, mtime_sec=mtime_sec, mtime_nsec=mtime_nsec,
                nlink=nlink, isize=isize, xsize=xsize, iu=iu)


def inode_meta(data, blksz, meta, xattr_blk, nid, *, strict_metadata=False):
    r = inode_raw(data, blksz, meta, nid, strict_metadata=strict_metadata)
    off, isize, xcount = r["off"], r["isize"], r["xcount"]
    selinux = None
    caps = None
    xattrs = {}
    if xcount:
        xoff = off + isize
        shared_count = data[xoff + 4]
        end = xoff + r["xsize"]
        ids = [struct.unpack_from("<I", data, xoff + 12 + 4 * i)[0] for i in range(shared_count)]
        base = xattr_blk * blksz

        def take(name, raw):
            nonlocal selinux, caps
            if strict_metadata and name in xattrs:
                raise SystemExit(f"duplicate xattr {name!r} on nid {nid}")
            if name == "security.selinux":
                xattrs[name] = raw
                selinux = raw.split(b"\x00", 1)[0].decode()
            elif name == "security.capability":
                xattrs[name] = raw
                caps = raw
            elif strict_metadata:
                raise SystemExit(f"unsupported xattr {name!r} on nid {nid}")

        ip = xoff + 12 + 4 * shared_count
        while ip + 4 <= end:
            name, raw, esize = parse_entry(data, ip)
            take(name, raw)
            ip += esize
        for sid in ids:
            name, raw, _ = parse_entry(data, base + sid * 4)
            take(name, raw)
    return {
        "mode": r["mode"] & 0o7777,
        "file_type": r["mode"] & 0o170000,
        "uid": r["uid"],
        "gid": r["gid"],
        "nlink": r["nlink"],
        "mtime_sec": r["mtime_sec"],
        "mtime_nsec": r["mtime_nsec"],
        "selinux": selinux,
        "caps": caps,
        "xattrs": xattrs,
        "extended": r["extended"],
    }


def list_dir(data, blksz, meta, nid):
    """(child nid, file type, name) for every entry of a directory except . and .."""
    r = inode_raw(data, blksz, meta, nid)
    if r["layout"] not in (0, 2):  # 0 flat plain, 2 flat inline; compressed/chunked directories are not expected
        raise SystemExit(f"directory nid {nid}: unsupported data layout {r['layout']}")
    size = r["size"]
    full = size // blksz if r["layout"] == 2 else (size + blksz - 1) // blksz
    chunks = []
    for i in range(full):
        start = (r["iu"] + i) * blksz
        chunks.append(data[start:start + min(blksz, size - i * blksz)])
    if r["layout"] == 2 and size % blksz:
        start = r["off"] + r["isize"] + r["xsize"]
        chunks.append(data[start:start + size % blksz])
    ents = []
    for c in chunks:
        cnt = struct.unpack_from("<H", c, 8)[0] // 12
        dirents = [struct.unpack_from("<QHB", c, i * 12) for i in range(cnt)]
        for i, (cnid, noff, ftype) in enumerate(dirents):
            nend = dirents[i + 1][1] if i + 1 < cnt else len(c)
            name = c[noff:nend].split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
            if name not in (".", ".."):
                ents.append((cnid, ftype, name))
    return ents


def walk_image(img, root_nid, data, blksz, meta, xattr_blk, *, strict_metadata=False):
    """Metadata of every path in the image (img is kept for call compatibility; data is the image's bytes)."""
    meta_by_path = {}
    hard = 0
    build_time_sec = struct.unpack_from("<Q", data, 1024 + 24)[0]
    build_time_nsec = struct.unpack_from("<I", data, 1024 + 32)[0]
    if strict_metadata and build_time_nsec >= 1_000_000_000:
        raise SystemExit(f"{img}: invalid EROFS build-time nanoseconds {build_time_nsec}")

    def effective_meta(nid):
        info = inode_meta(data, blksz, meta, xattr_blk, nid, strict_metadata=strict_metadata)
        if info["mtime_sec"] is None:
            info["mtime_sec"] = build_time_sec
            info["mtime_nsec"] = build_time_nsec
        return info

    def rec(nid, rel):
        nonlocal hard
        meta_by_path[rel] = effective_meta(nid)
        for enid, typ, name in list_dir(data, blksz, meta, nid):
            child = name if rel == "" else rel + "/" + name
            if typ == 2:
                rec(enid, child)
            else:
                info_c = effective_meta(enid)
                meta_by_path[child] = info_c
                if info_c["nlink"] > 1:
                    hard += 1

    rec(root_nid, "")
    return meta_by_path, hard


def restore_mtimes(root, meta):
    """Restore timestamps represented by extended EROFS inodes before mkfs.erofs."""
    root = os.fspath(root)
    for rel, info in sorted(meta.items(), key=lambda item: item[0].count("/"), reverse=True):
        sec = info.get("mtime_sec")
        nsec = info.get("mtime_nsec")
        if sec is None and nsec is None:
            continue
        if not isinstance(sec, int) or not isinstance(nsec, int) or not 0 <= nsec < 1_000_000_000:
            raise SystemExit(f"invalid EROFS mtime for {rel!r}")
        path = root if rel == "" else os.path.join(root, rel)
        timestamp_ns = sec * 1_000_000_000 + nsec
        try:
            os.utime(path, ns=(timestamp_ns, timestamp_ns), follow_symlinks=False)
        except (NotImplementedError, OSError) as exc:
            raise SystemExit(f"cannot restore EROFS mtime on {rel!r}: {exc}") from exc
        got = os.lstat(path).st_mtime_ns
        if got != timestamp_ns:
            raise SystemExit(f"cannot represent EROFS mtime on {rel!r}: wanted {timestamp_ns}, got {got}")


def esc_regex(path):
    return re.sub(r"([.^$\\*+?()\[\]{}|])", r"\\\1", path)


def decode_capability(raw, where):
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 20:
        raise SystemExit(f"security.capability on {where} is not supported by this rebuild")
    magic_etc, p0, i0, p1, i1 = struct.unpack("<5I", raw)
    if magic_etc != 0x02000001 or i0 != 0 or i1 != 0 or (p0 | p1) == 0:
        raise SystemExit(f"security.capability on {where} is not supported by this rebuild")
    return (p1 << 32) | p0


def write_configs(mount, root, meta, out_fc, out_fs, *, allow_caps=False):
    rows = []

    def add(rel, info):
        rows.append((rel, info))

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        # symlink dirs are listed in dirnames
        real_dirs = []
        for name in dirnames:
            p = os.path.join(dirpath, name)
            rel = name if rel_dir == "" else rel_dir + "/" + name
            if os.path.islink(p):
                add(rel, meta[rel])
            else:
                real_dirs.append(name)
                add(rel, meta[rel])
        dirnames[:] = real_dirs
        for name in filenames:
            rel = name if rel_dir == "" else rel_dir + "/" + name
            add(rel, meta[rel])

    # root
    rows.append(("", meta[""]))
    # unique, longer paths last so a prefix regex does not override
    uniq = {}
    for rel, info in rows:
        uniq[rel] = info
    items = sorted(uniq.items(), key=lambda kv: (len(kv[0]), kv[0]))

    fc_lines = []
    fs_lines = []
    missing = []
    for rel, info in items:
        if info.get("selinux") is None:
            missing.append(rel)
            continue
        where = f"{mount.rstrip('/')}/{rel}"
        cap_suffix = ""
        if info.get("caps"):
            if not allow_caps:
                raise SystemExit(f"security.capability on {where} is not supported by this rebuild")
            cap_val = decode_capability(info["caps"], where)
            cap_suffix = f" capabilities=0x{cap_val:x}"
        if mount == "/":
            full = "/" if rel == "" else "/" + rel
            fs_path = rel
        else:
            full = mount if rel == "" else mount + "/" + rel
            fs_path = "" if rel == "" else mount[1:] + "/" + rel  # vendor/foo
        fc_lines.append(f"{esc_regex(full)} {info['selinux']}")
        mode = f"0{info['mode']:o}"
        if rel == "":
            fs_lines.append(f" {info['uid']} {info['gid']} {mode}{cap_suffix}")
        else:
            fs_lines.append(f"{fs_path} {info['uid']} {info['gid']} {mode}{cap_suffix}")
    if missing:
        raise SystemExit("missing selinux for: " + ", ".join(missing[:20]))
    out_fc.write_text("\n".join(fc_lines) + "\n")
    out_fs.write_text("\n".join(fs_lines) + "\n")
    return len(fc_lines)
