"""GPT and bootconfig helpers for the stick installer tests. Written separately from the installer's own checks
(gbos-install.sh) so a mistake in one is not repeated in the other."""
import hashlib, struct, uuid, zlib

def write_gpt(f, sectors, parts, n=128, esz=128):
    """Protective MBR, primary and backup GPT for a disk of `sectors` 512-byte sectors, written into the open file f.
    parts: dicts with num, name, type, first, last and optional attrs, guid."""
    ents = bytearray(n * esz)
    for p in parts:
        e = (uuid.UUID(p["type"]).bytes_le + uuid.UUID(p.get("guid") or str(uuid.uuid4())).bytes_le +
             struct.pack("<QQQ", p["first"], p["last"], p.get("attrs", 0)) + p["name"].encode("utf-16le").ljust(72, b"\0"))
        ents[(p["num"] - 1) * esz:(p["num"] - 1) * esz + 128] = e
    esec = (n * esz + 511) // 512
    disk = uuid.uuid4().bytes_le
    def header(my, alt, elba):
        h = bytearray(struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x10000, 92, 0, 0, my, alt, 2 + esec,
                                  sectors - 2 - esec, disk, elba, n, esz, zlib.crc32(ents)))
        struct.pack_into("<I", h, 16, zlib.crc32(h))
        return bytes(h) + bytes(512 - 92)
    mbr = bytearray(512)
    mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\x00\x02\x00", 0xEE, b"\xff\xff\xff", 1, min(sectors - 1, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"
    f.seek(0); f.write(bytes(mbr)); f.write(header(1, sectors - 1, 2)); f.write(bytes(ents))
    f.seek((sectors - 1 - esec) * 512); f.write(bytes(ents)); f.write(header(sectors - 1, 1, sectors - 1 - esec))

def _read(f, off, n):
    f.seek(off); b = f.read(n)
    if len(b) != n: raise ValueError(f"short read at {off}")
    return b

def _header(b, lba):
    if b[:8] != b"EFI PART": raise ValueError(f"no GPT header at LBA {lba}")
    hsize, hcrc = struct.unpack_from("<II", b, 12)
    if not 92 <= hsize <= 512 or zlib.crc32(b[:16] + bytes(4) + b[20:hsize]) != hcrc: raise ValueError(f"GPT header CRC at LBA {lba}")
    my, alt, first, last = struct.unpack_from("<QQQQ", b, 24)
    elba, n, esz, ecrc = struct.unpack_from("<QIII", b, 72)
    if my != lba: raise ValueError(f"GPT header at LBA {lba} names LBA {my}")
    return dict(my=my, alt=alt, first=first, last=last, disk=str(uuid.UUID(bytes_le=b[56:72])), elba=elba, n=n, esz=esz, ecrc=ecrc)

def read_gpt(path, backup=True):
    """(header, {num: partition}) of a disk image; ValueError unless MBR, header and entry CRCs (and, with backup, the
    backup GPT at the last sector, identical to the primary) are valid."""
    with open(path, "rb") as f:
        f.seek(0, 2); size = f.tell()
        mbr = _read(f, 0, 512)
        if mbr[510:512] != b"\x55\xaa" or mbr[450] != 0xEE: raise ValueError("no protective MBR")
        h = _header(_read(f, 512, 512), 1)
        t = _read(f, h["elba"] * 512, h["n"] * h["esz"])
        if zlib.crc32(t) != h["ecrc"]: raise ValueError("GPT entries CRC")
        if backup:
            last = size // 512 - 1
            if h["alt"] != last: raise ValueError("backup GPT not at the last sector")
            b = _header(_read(f, last * 512, 512), last)
            if b["alt"] != 1 or _read(f, b["elba"] * 512, b["n"] * b["esz"]) != t or \
                    [b[k] for k in ("first", "last", "disk", "ecrc")] != [h[k] for k in ("first", "last", "disk", "ecrc")]:
                raise ValueError("backup GPT differs from the primary")
    parts = {}
    for i in range(h["n"]):
        e = t[i * h["esz"]:(i + 1) * h["esz"]]
        if e[:16] == bytes(16): continue
        a, z, attrs = struct.unpack_from("<QQQ", e, 32)
        parts[i + 1] = dict(name=e[56:128].decode("utf-16le").rstrip("\0"), first=a, last=z, attrs=attrs,
                            type=str(uuid.UUID(bytes_le=e[:16])), guid=str(uuid.UUID(bytes_le=e[16:32])))
    return h, parts

def sha(path, start, length):
    """SHA-256 of a byte range; ValueError if the file ends early."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(start); left = length
        while left:
            b = f.read(min(left, 16 << 20))
            if not b: raise ValueError(f"{path} ends before byte {start + length}")
            h.update(b); left -= len(b)
    return h.hexdigest()

MAGIC = b"#BOOTCONFIG\n"

def bootconfig(initrd):
    """The bootconfig text at the end of an initrd (checksum verified)."""
    if not initrd.endswith(MAGIC): raise ValueError("no bootconfig")
    end = len(initrd) - len(MAGIC); size, csum = struct.unpack_from("<II", initrd, end - 8)
    bc = initrd[end - 8 - size:end - 8]
    if len(bc) != size or sum(bc) & 0xFFFFFFFF != csum: raise ValueError("bootconfig checksum")
    return bc

def bootconfig_set_boot_part(initrd, guid):
    """The initrd with androidboot.boot_part_uuid set to guid and the bootconfig checksum recomputed."""
    bc = bootconfig(initrd)
    lines = bc.split(b"\n")
    hits = [i for i, l in enumerate(lines) if l.startswith(b"androidboot.boot_part_uuid=")]
    if len(hits) != 1: raise ValueError("boot_part_uuid lines: %d" % len(hits))
    lines[hits[0]] = b"androidboot.boot_part_uuid=" + guid.encode()
    new = b"\n".join(lines)
    return initrd[:len(initrd) - len(MAGIC) - 8 - len(bc)] + new + struct.pack("<II", len(new), sum(new) & 0xFFFFFFFF) + MAGIC

def read_initcall_map(path):
    """install/stick/builtin-initcalls.txt as {module: (initcall or None, [initcalls that must be blocked instead])};
    ValueError on a bad line or on a "-" row that needs an initcall the file does not block."""
    import re
    rows = {}
    for line in open(path).read().splitlines():
        f = line.split("#", 1)[0].split()
        if not f: continue
        if len(f) == 2 and re.fullmatch(r"[a-z0-9_]+", f[1]) and f[1] != "-": rows[f[0].replace("-", "_")] = (f[1], [])
        elif len(f) == 3 and f[1] == "-" and re.fullmatch(r"[a-z0-9_]+(,[a-z0-9_]+)*", f[2]): rows[f[0].replace("-", "_")] = (None, f[2].split(","))
        else: raise ValueError(f"bad line: {line!r}")
    blocked = [c for c, _ in rows.values() if c]
    for mod, (_, need) in rows.items():
        if set(need) - set(blocked): raise ValueError(f"{mod} needs {sorted(set(need) - set(blocked))} blocked")
    return rows

def initcalls(rows):
    """The initcall_blacklist value the entry gets: the file's initcalls in file order."""
    return ",".join(c for c, _ in rows.values() if c)
