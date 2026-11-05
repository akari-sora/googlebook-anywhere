#!/usr/bin/env python3
"""Update an installed Googlebook (USB stick or internal disk) to a newer release and keep the user's data.

The disk has two slots, a and b, the way Android lays them out: the dynamic partitions <name>_a / <name>_b in super
(one LP metadata describes both), and boot_x, init_boot_x, vendor_boot_x, vbmeta_x, pvmfw_x, recovery_x, ota_recovery_x.
A release image fills slot a. An update writes the new release into the slot that is NOT started by default, while
the running one stays untouched, then points the boot menu at it:
  - the dynamic partitions go to free space in super (the old ones of that slot are dropped), the physical slot
    partitions are overwritten, the LP metadata is rewritten with the other slot's extents unchanged;
  - the ESP gets /gbos/<slot>/initrd.img (the release's ramdisk, lz4, plus its bootconfig with
    androidboot.slot_suffix=_<slot> and this disk's boot_a GUID) and, when the kernel differs from every kernel already
    there, /gbos/<slot>/kernel.efi;
  - the release's entries (lapis-quiet.conf, lapis-debug.conf; releases before v0.2 also lapis.conf) start the new slot;
    the running slot's quiet and serial-debug entries become prev-<entry> "... (previous version)" when the ESP has room
    for both slots' files, and a running-slot entry the release no longer has (the plain lapis.conf) is removed.
Never written: userdata, metadata, misc, frp_persist, desktop_security_persist, desktop_security_storage, the GPT, and
every partition that is not a slot partition, super or the ESP. Nothing else of the disk changes.

Refused before anything is written: a disk whose partition table differs from the release's (except where userdata
starts), another AVB key, another model, another KeyMint/Gatekeeper selection (the lock screen and keystore keys
depend on them), an older Google build, too little room in super, a release that fails its checksums, a FAT whose
size field reaches past its partition.

Order of the writes (each step is reported; a rerun after a stop finishes the same target slot):
  state file -> [installer stick: its internal-disk entries all start the running slot from the stick's own copy, and
  nothing on the stick points at the target slot] -> the target slot's old entries and files removed -> dynamic
  partitions -> physical slot partitions -> LP metadata -> new boot files -> [installer stick: its menu starts the new
  slot, "(previous version)" the running one] -> the disk's menu switched -> state file removed.
When the ESP cannot hold both versions' boot files (on the KANO's 64 MiB ESP: every Google build with a new kernel), the
old ones are removed before the new ones are written (--without-fallback, or an installer stick that keeps the running
version): from then until the menu is switched, the disk's own menu cannot start either version, and a stop there says
so; the rerun finishes it. (Only when the new files would still fit next to the old ones but two previous-version
entries would not, i.e. within a few KiB, the new files go first and the old ones after the switch.)

usage:
  gbos-update.py inspect  <disk>
  gbos-update.py plan     <disk> <source> [--mode stick|nvme]
  gbos-update.py apply    <disk> <source> --mode stick|nvme [--rdev MAJ:MIN [--syspath P] [--bytes N]]
                          [--without-fallback] [--stick-boot <XBOOTLDR device or image@@offset>]
  gbos-update.py rollback <disk> [--rdev MAJ:MIN [--syspath P] [--bytes N]] [--stick-boot ...] [--allow-build-change]
  <source>: --image <release.img> (checked against <release.img>.sha256)
         or --pristine <dir> --super <device holding the release's super> (the installer stick's data)
<disk> may be a regular file (tests). On a block device every write goes through one O_EXCL descriptor checked
against --rdev (and, when given, the realpath of /sys/dev/block/MAJ:MIN and the size in bytes); every written range of
a partition is read back (page cache dropped) and compared by SHA-256, every FAT file is read back after mcopy.
Exit: 0 done, 1 failed after writing started (run the same command again), 2 refused (nothing written).
"""
import hashlib, json, os, re, selectors, shutil, stat, struct, subprocess, sys, tempfile, time, uuid, zlib

SECTOR = 512
MIB = 1 << 20
SLOT_PHYS = ("boot", "init_boot", "vendor_boot", "vbmeta", "pvmfw", "recovery", "ota_recovery")
NEVER = ("userdata", "metadata", "misc", "frp_persist", "desktop_security_persist", "desktop_security_storage")
ESP = "EFI-SYSTEM"
ENTRIES = ("lapis-quiet.conf", "lapis-debug.conf", "lapis.conf")    # every entry name a release may have
REQUIRED_ENTRIES = ("lapis-quiet.conf", "lapis-debug.conf")           # lapis.conf (plain) was dropped in v0.2
PREV = "prev-"
PREV_TITLE = " (previous version)"
STATE = "/gbos/update-state.txt"
LIFT = "initcall_blacklist=nvme_init,mei_me_driver_init"
LZ4_LEGACY = b"\x02\x21\x4c\x18"
BCMAGIC = b"#BOOTCONFIG\n"
FAIL_AT = os.environ.get("GBOS_UPDATE_TEST_FAIL_AT", "")   # test hook: stop after the named step (exit 1)
WRITING = []                                                # set at the first change to the disk
WINDOW = []                                                 # set while the disk's own menu cannot start any version
STICK = []                                                  # set when the installer stick's menu is part of the change
HOLD = []                                                   # descriptors held O_EXCL until the end


class Refuse(Exception):
    """Nothing was written."""


class Failed(Exception):
    """Writing had started."""


def say(*a):
    print(*a, flush=True)


def run(cmd, data=None, text=False, pass_fds=()):
    r = subprocess.run(cmd, input=data, capture_output=True, text=text, pass_fds=pass_fds,
                       env=dict(os.environ, MTOOLS_SKIP_CHECK="1"))
    if r.returncode:
        err = r.stderr if text else r.stderr.decode(errors="replace")
        raise RuntimeError(f"{' '.join(map(str, cmd))} failed: {err.strip()[:300]}")
    return r.stdout


# ------------------------------------------------------------------ disks

class Disk:
    """A disk or disk image. write=True: block devices are opened O_EXCL and checked against rdev."""

    def __init__(self, path, write=False, rdev=None, syspath=None, nbytes=None):
        self.path = os.path.realpath(path)
        st = os.stat(self.path)
        self.blk = stat.S_ISBLK(st.st_mode)
        if not self.blk and not stat.S_ISREG(st.st_mode):
            raise Refuse(f"{path} is neither a block device nor a regular file")
        # Do not block opening a substituted FIFO before we can classify the fd.
        flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        if write and self.blk:
            flags |= os.O_EXCL
        try:
            fd = os.open(self.path, flags)
        except OSError as e:
            raise Refuse(f"cannot open {self.path}{' exclusively' if write and self.blk else ''}: {e.strerror}")
        try:
            fst = os.fstat(fd)
            before = (stat.S_IFMT(st.st_mode), st.st_dev, st.st_ino, st.st_rdev)
            opened = (stat.S_IFMT(fst.st_mode), fst.st_dev, fst.st_ino, fst.st_rdev)
            if opened != before:
                raise Refuse(f"{self.path} changed while opening")
            self.blk = stat.S_ISBLK(fst.st_mode)
            os.set_blocking(fd, True)
            if self.blk and rdev is not None and f"{os.major(fst.st_rdev)}:{os.minor(fst.st_rdev)}" != rdev:
                raise Refuse(f"{self.path} is {os.major(fst.st_rdev)}:{os.minor(fst.st_rdev)}, not the confirmed {rdev}")
            if write and self.blk and rdev is None:
                raise Refuse("a block device is only written with --rdev (its confirmed major:minor)")
            if self.blk and syspath is not None:
                sp = os.path.realpath(f"/sys/dev/block/{os.major(fst.st_rdev)}:{os.minor(fst.st_rdev)}")
                if sp != syspath:
                    raise Refuse(f"{self.path} is {sp} in sysfs, not the confirmed {syspath}")
            self.size = os.lseek(fd, 0, os.SEEK_END)
            if nbytes is not None and self.size != int(nbytes):
                raise Refuse(f"{self.path} has {self.size} bytes, not the confirmed {nbytes}")
            self.write = write
            self.forbidden = []          # (start, end, why): byte ranges no update may write
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        self.fd = fd

    def guard(self, ranges):
        self.forbidden = list(ranges)

    def pread(self, off, n):
        out = bytearray()
        while len(out) < n:
            b = os.pread(self.fd, min(n - len(out), 16 << 20), off + len(out))
            if not b:
                raise RuntimeError(f"{self.path}: short read at {off + len(out)}")
            out += b
        return bytes(out)

    def pwrite(self, off, data):
        assert self.write
        for a, b, why in self.forbidden:
            if off < b and off + len(data) > a:
                raise Failed(f"refusing to write {off}+{len(data)}: it overlaps {why}")
        mv = memoryview(data)
        while mv:
            n = os.pwrite(self.fd, mv, off)
            off += n
            mv = mv[n:]

    def sync_drop(self):
        os.fsync(self.fd)
        if self.blk:
            import fcntl
            fcntl.ioctl(self.fd, 0x1261)  # BLKFLSBUF: the next reads come from the disk
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_DONTNEED)

    def sha(self, off, n):
        h = hashlib.sha256()
        left = n
        while left:
            b = self.pread(off + n - left, min(left, 16 << 20))
            h.update(b)
            left -= len(b)
        return h.hexdigest()

    def vol(self, off):
        """mtools image spec of a FAT file system at byte offset off."""
        return f"/proc/self/fd/{self.fd}@@{off}"


def gpt_read(disk):
    """Primary GPT of a Disk: {name: dict(n, start, len, type, guid, attrs)}; checks CRCs and names."""
    mbr, hd = disk.pread(0, 512), disk.pread(512, 512)
    if mbr[510:512] != b"\x55\xaa" or mbr[450] != 0xEE:
        raise Refuse(f"{disk.path}: no protective MBR")
    if hd[:8] != b"EFI PART":
        raise Refuse(f"{disk.path}: no GPT")
    hsize, hcrc = struct.unpack_from("<II", hd, 12)
    if not 92 <= hsize <= 512 or zlib.crc32(hd[:16] + bytes(4) + hd[20:hsize]) != hcrc:
        raise Refuse(f"{disk.path}: GPT header CRC is wrong")
    lba, n, esz, ecrc = struct.unpack_from("<QIII", hd, 72)
    if esz < 128 or n * esz > MIB:
        raise Refuse(f"{disk.path}: odd GPT entry table")
    t = disk.pread(lba * 512, n * esz)
    if zlib.crc32(t) != ecrc:
        raise Refuse(f"{disk.path}: GPT entries CRC is wrong")
    out = {}
    for i in range(n):
        e = t[i * esz:(i + 1) * esz]
        if e[:16] == bytes(16):
            continue
        first, last, attrs = struct.unpack_from("<QQQ", e, 32)
        name = e[56:128].decode("utf-16le").rstrip("\0")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,36}", name) or name in out:
            raise Refuse(f"{disk.path}: partition {i + 1} has an odd or repeated name {name!r}")
        out[name] = dict(n=i + 1, start=first * 512, len=(last + 1 - first) * 512, type=str(uuid.UUID(bytes_le=e[:16])),
                         guid=str(uuid.UUID(bytes_le=e[16:32])), attrs=attrs)
    for need in ("super", ESP, "boot_a", "userdata") + tuple(f"{p}_{s}" for p in SLOT_PHYS for s in "ab"):
        if need not in out:
            raise Refuse(f"{disk.path}: no {need} partition (not a Googlebook disk of this layout)")
    return out


# ------------------------------------------------------------------ LP metadata (super)

GEO_MAGIC, HDR_MAGIC = 0x616C4467, 0x414C5030
RESERVED = 4096
GEO_SIZE = 4096


class LP:
    """LP metadata of one super: geometry, header template, groups and block-device tables kept as bytes,
    partitions as dicts with explicit extents (sector, count) relative to super."""

    @staticmethod
    def geometry(raw):
        magic, ssize = struct.unpack_from("<II", raw, 0)
        if magic != GEO_MAGIC or ssize < 52:
            raise Refuse("super: no LP geometry")
        g = bytearray(raw[:ssize]); csum = bytes(g[8:40]); g[8:40] = bytes(32)
        if hashlib.sha256(g).digest() != csum:
            raise Refuse("super: LP geometry checksum")
        mx, slots, lbs = struct.unpack_from("<III", raw, 40)
        return mx, slots, lbs

    @classmethod
    def read(cls, rd, size):
        """rd(off, n) reads super; size = super bytes. Reads metadata slot 0 (primary, else backup) and checks the
        other slots: a slot that differs means an interrupted write (reported, the newest valid one is not guessed)."""
        g1 = rd(RESERVED, GEO_SIZE)
        g2 = rd(RESERVED + GEO_SIZE, GEO_SIZE)
        mx, slots, lbs = cls.geometry(g1)
        if cls.geometry(g2) != (mx, slots, lbs):
            raise Refuse("super: the two LP geometry copies differ")
        copies = {}
        for s in range(slots):
            for kind, base in (("primary", RESERVED + 2 * GEO_SIZE), ("backup", RESERVED + 2 * GEO_SIZE + slots * mx)):
                raw = rd(base + s * mx, mx)
                try:
                    copies[(s, kind)] = cls.parse(raw)
                except Refuse as e:
                    copies[(s, kind)] = e
        good = [v for v in copies.values() if not isinstance(v, Refuse)]
        if not good:
            raise Refuse("super: no valid LP metadata copy")
        first = copies.get((0, "primary"))
        if isinstance(first, Refuse):
            first = copies.get((0, "backup"))
        if isinstance(first, Refuse):
            raise Refuse("super: slot 0 LP metadata is damaged in both copies")
        lp = first
        lp.mx, lp.slots, lp.lbs, lp.size = mx, slots, lbs, size
        lp.consistent = all(not isinstance(v, Refuse) and v.blob == first.blob for v in copies.values())
        lp.validate()
        return lp

    @classmethod
    def parse(cls, raw):
        magic, major, minor, hsz = struct.unpack_from("<IHHI", raw, 0)
        if magic != HDR_MAGIC or not 128 <= hsz <= 512:
            raise Refuse("super: no LP header")
        h = bytearray(raw[:hsz]); csum = bytes(h[12:44]); h[12:44] = bytes(32)
        if hashlib.sha256(h).digest() != csum:
            raise Refuse("super: LP header checksum")
        tsize = struct.unpack_from("<I", raw, 44)[0]
        t = raw[hsz:hsz + tsize]
        if len(t) != tsize or hashlib.sha256(t).digest() != raw[48:80]:
            raise Refuse("super: LP tables checksum")
        self = cls()
        self.header = bytes(raw[:hsz]); self.blob = bytes(raw[:hsz + tsize])
        d = lambda o: struct.unpack_from("<III", raw, o)
        pt, ex, gr, bd = d(80), d(92), d(104), d(116)
        if pt[2] != 52 or ex[2] != 24 or gr[2] != 48 or bd[2] != 64:
            raise Refuse("super: unknown LP table entry sizes")
        for off, num, esz in (pt, ex, gr, bd):
            if off + num * esz > tsize:
                raise Refuse("super: LP table outside the tables")
        self.groups_raw = t[gr[0]:gr[0] + gr[1] * 48]
        self.bdevs_raw = t[bd[0]:bd[0] + bd[1] * 64]
        self.groups = [t[gr[0] + i * 48:gr[0] + i * 48 + 36].split(b"\0")[0].decode() for i in range(gr[1])]
        self.group_max = [struct.unpack_from("<Q", t, gr[0] + i * 48 + 40)[0] for i in range(gr[1])]
        exts = [struct.unpack_from("<QIQI", t, ex[0] + i * 24) for i in range(ex[1])]
        self.parts = []
        for i in range(pt[1]):
            o = pt[0] + i * 52
            name = t[o:o + 36].split(b"\0")[0].decode()
            attrs, fe, ne, gi = struct.unpack_from("<IIII", t, o + 36)
            if fe + ne > len(exts) or gi >= len(self.groups):
                raise Refuse(f"super: LP partition {name} points outside the tables")
            el = []
            for j in range(fe, fe + ne):
                nsec, ttype, data, src = exts[j]
                if ttype != 0 or src != 0:
                    raise Refuse(f"super: LP partition {name} has a non-linear extent")
                el.append((data, nsec))
            self.parts.append(dict(name=name, attrs=attrs, group=gi, extents=el))
        if bd[1] != 1:
            raise Refuse("super: expected one LP block device")
        first, align, aoff, bsize = struct.unpack_from("<QIIQ", self.bdevs_raw, 0)
        self.first, self.align, self.bsize = first, align, bsize
        return self

    def validate(self):
        if self.align % SECTOR or self.first * SECTOR < RESERVED + 2 * GEO_SIZE + 2 * self.slots * self.mx or \
                self.bsize != self.size:
            raise Refuse("super: LP block device does not match the super partition")
        spans = sorted((a, a + n, p["name"]) for p in self.parts for a, n in p["extents"])
        for a, b, name in spans:
            if a < self.first or b * SECTOR > self.size:
                raise Refuse(f"super: extent of {name} outside the data area")
        # extents of the two slots may not overlap; within one LP table, overlapping extents mean damage
        for (a1, b1, n1), (a2, b2, n2) in zip(spans, spans[1:]):
            if a2 < b1:
                raise Refuse(f"super: extents of {n1} and {n2} overlap")

    def part(self, name):
        return next((p for p in self.parts if p["name"] == name), None)

    def size_of(self, name):
        p = self.part(name)
        return sum(n for _, n in p["extents"]) * SECTOR if p else 0

    def serialize(self):
        names = [p["name"] for p in self.parts]
        if len(set(names)) != len(names):
            raise RuntimeError("LP: repeated partition name")
        ptab, etab = bytearray(), bytearray()
        for p in self.parts:
            nm = p["name"].encode()
            if len(nm) > 35:
                raise RuntimeError(f"LP: partition name too long: {p['name']}")
            ptab += nm.ljust(36, b"\0") + struct.pack("<IIII", p["attrs"], len(etab) // 24, len(p["extents"]), p["group"])
            for a, n in p["extents"]:
                etab += struct.pack("<QIQI", n, 0, a, 0)
        tables = bytes(ptab) + bytes(etab) + self.groups_raw + self.bdevs_raw
        h = bytearray(self.header)
        struct.pack_into("<I", h, 44, len(tables))
        h[48:80] = hashlib.sha256(tables).digest()
        o = 0
        for at, num, esz in ((80, len(self.parts), 52), (92, len(etab) // 24, 24), (104, len(self.groups), 48),
                             (116, len(self.bdevs_raw) // 64, 64)):
            struct.pack_into("<III", h, at, o, num, esz)
            o += num * esz
        h[12:44] = bytes(32)
        h[12:44] = hashlib.sha256(h).digest()
        blob = bytes(h) + tables
        if len(blob) > self.mx:
            raise RuntimeError("LP: metadata larger than a metadata slot")
        return blob

    def slot_offsets(self):
        """(super-relative byte offset) of every metadata copy, in the order they are rewritten: the other slots'
        backup then primary, slot 0 (the one the running slot a may read) last."""
        p0 = RESERVED + 2 * GEO_SIZE
        b0 = p0 + self.slots * self.mx
        order = list(range(1, self.slots)) + [0]
        return [off for s in order for off in (b0 + s * self.mx, p0 + s * self.mx)]


def allocate(lp, sizes, keep):
    """Linear extents for sizes {name: bytes} in super, avoiding every extent of the partitions in keep and the
    metadata area; 1 MiB aligned, largest first, one extent when a gap is big enough."""
    al = max(lp.align // SECTOR, 1)
    used = sorted((a, a + n) for p in lp.parts if p["name"] in keep for a, n in p["extents"])
    free, pos = [], lp.first
    for a, b in used + [(lp.size // SECTOR, lp.size // SECTOR)]:
        start = -(-pos // al) * al
        if a > start:
            free.append([start, a])
        pos = max(pos, b)
    out = {}
    for name, nbytes in sorted(sizes.items(), key=lambda kv: -kv[1]):
        need = -(-nbytes // SECTOR)      # an extent only has to START aligned; its length need not be a multiple
        gap = next((g for g in free if g[1] - g[0] >= need), None)
        ext = []
        if gap:
            ext.append((gap[0], need))
            gap[0] = -(-(gap[0] + need) // al) * al
        else:
            left = need
            for g in free:
                if left == 0:
                    break
                take = min(left, g[1] - g[0])
                if take <= 0:
                    continue
                ext.append((g[0], take))
                left -= take
                g[0] = -(-(g[0] + take) // al) * al
            if left:
                raise Refuse(f"super has no room for {name} ({nbytes // MIB} MiB) next to the running slot")
        free = [g for g in free if g[1] > g[0]]
        out[name] = ext
    return out


# ------------------------------------------------------------------ AVB, bootconfig, ramdisk, entries

def vbmeta_key(raw):
    if raw[:4] != b"AVB0":
        raise Refuse("vbmeta: no AVB header")
    auth, aux = struct.unpack_from(">QQ", raw, 12)
    po, ps = struct.unpack_from(">QQ", raw, 64)
    a = 256 + auth + po
    if ps == 0 or a + ps > len(raw):
        raise Refuse("vbmeta: no public key")
    return hashlib.sha256(raw[a:a + ps]).hexdigest()


def split_initrd(d):
    """(ramdisk bytes, bootconfig lines) of an initrd that ends in a bootconfig."""
    if not d.endswith(BCMAGIC):
        raise Refuse("initrd has no bootconfig")
    end = len(d) - len(BCMAGIC)
    size, csum = struct.unpack_from("<II", d, end - 8)
    start = end - 8 - size
    bc = d[start:start + size]
    if start < 0 or (sum(bc) & 0xFFFFFFFF) != csum:
        raise Refuse("initrd bootconfig checksum")
    return d[:start], bc


def bc_get(bc, key):
    hits = [l for l in bc.split(b"\n") if l.startswith(key.encode() + b"=")]
    if len(hits) != 1:
        raise Refuse(f"bootconfig: {len(hits)} lines of {key}")
    return hits[0].split(b"=", 1)[1].decode()


def bc_set(bc, key, value):
    lines = bc.split(b"\n")
    hits = [i for i, l in enumerate(lines) if l.startswith(key.encode() + b"=")]
    if len(hits) != 1:
        raise Refuse(f"bootconfig: {len(hits)} lines of {key}")
    lines[hits[0]] = key.encode() + b"=" + value.encode()
    return b"\n".join(lines)


def bc_blob(bc):
    return bc + struct.pack("<II", len(bc), sum(bc) & 0xFFFFFFFF) + BCMAGIC


def ramdisk_plain(rd):
    if rd[:4] == LZ4_LEGACY:
        return run(["lz4", "-d", "-c"], rd)
    return rd


PREVIEW_RAMDISK_MAX = 256 * 1024 * 1024
PREVIEW_LZ4_TIMEOUT = 60


def preview_lz4(data, args, limit, action, tmp=None):
    """Run an LZ4 operation with output and wall-clock bounds, reaping our child."""
    with tempfile.TemporaryFile(dir=tmp) as source:
        source.write(data)
        source.seek(0)
        proc = subprocess.Popen(["lz4", *args], stdin=source, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        try:
            out = bytearray()
            deadline = time.monotonic() + PREVIEW_LZ4_TIMEOUT
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise Refuse(f"preview: ramdisk {action} timed out")
                    part = os.read(proc.stdout.fileno(), min(1024 * 1024, limit + 1 - len(out)))
                    if not part:
                        break
                    out.extend(part)
                    if len(out) > limit:
                        raise Refuse("preview: ramdisk exceeds output limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refuse(f"preview: ramdisk {action} timed out")
            try:
                status = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise Refuse(f"preview: ramdisk {action} timed out")
            if status:
                raise Refuse(f"preview: ramdisk {action} failed")
            return bytes(out)
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            proc.wait()


def preview_ramdisk_plain(rd, tmp=None):
    """Decode with an output and wall-clock bound; reap only our own child."""
    if rd[:4] != LZ4_LEGACY:
        if len(rd) > PREVIEW_RAMDISK_MAX:
            raise Refuse("preview: ramdisk exceeds output limit")
        return rd
    return preview_lz4(rd, ["-d", "-c"], PREVIEW_RAMDISK_MAX, "decoder", tmp)


def preview_ramdisk_compress(rd, tmp=None):
    if len(rd) > PREVIEW_RAMDISK_MAX:
        raise Refuse("preview: ramdisk exceeds input limit")
    # LZ4 framing can make output slightly larger than the input.
    return preview_lz4(rd, ["-l", "-12", "--favor-decSpeed", "-c"],
                       PREVIEW_RAMDISK_MAX + (PREVIEW_RAMDISK_MAX // 255) + (1 << 20), "encoder", tmp)


def ramdisk_prop(rd, key, preview=False, preview_tmp=None):
    """A property from system/etc/ramdisk/build.prop inside an (lz4 or plain) newc ramdisk."""
    body = preview_ramdisk_plain(rd, preview_tmp) if preview else ramdisk_plain(rd)
    off = 0
    while True:
        off = body.find(b"070701", off)
        if off < 0:
            break
        h = body[off:off + 110]
        try:
            ns, fs = int(h[94:102], 16), int(h[54:62], 16)
        except ValueError:
            off += 6
            continue
        nend = (off + 110 + ns + 3) & ~3
        name = body[off + 110:off + 110 + ns - 1]
        if name == b"system/etc/ramdisk/build.prop":
            for line in body[nend:nend + fs].decode(errors="replace").splitlines():
                if line.startswith(key + "="):
                    return line.split("=", 1)[1]
            break
        off = (nend + fs + 3) & ~3
    raise Refuse(f"ramdisk has no {key}")


def build_id(fp):
    m = re.fullmatch(r"[^/]+/[^/]+/[^:]+:[^/]+/[^/]+/(\d+):\w+/[\w-]+", fp)
    if not m:
        raise Refuse(f"odd build fingerprint {fp!r}")
    return int(m.group(1))


def entry_parse(text):
    e = {"initrd": []}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        k, _, v = line.partition(" ")
        if k == "initrd":
            e["initrd"].append(v.strip())
        else:
            e[k] = v.strip()
    if not e.get("title") or not e.get("linux") or not e["initrd"] or "options" not in e:
        raise Refuse("boot entry without title, linux, initrd or options")
    return e


def entry_text(title, linux, initrd, options):
    return f"title {title}\nlinux {linux}\n" + "".join(f"initrd {i}\n" for i in initrd) + f"options {options}\n"


def lift(options):
    """NVMe entries: only the NVMe initcall block lifted; module_blacklist and efi=noruntime must stay."""
    toks = options.split()
    bl = [x for x in toks if x.startswith("module_blacklist=")]
    if toks.count(LIFT) != 1 or "efi=noruntime" not in toks or len(bl) != 1 or \
            {"nvme", "nvme_core"} & set(bl[0][17:].split(",")):
        raise Refuse("a release entry cannot be changed for the internal disk safely")
    return " ".join("initcall_blacklist=mei_me_driver_init" if t == LIFT else t for t in toks)


def model_of(title):
    m = re.match(r"Googlebook (\S+) ", title)
    if not m:
        raise Refuse(f"boot entry title {title!r} does not name a model")
    return m.group(1)


# ------------------------------------------------------------------ FAT volumes (mtools)

def fat_fits(boot_sector, length, what):
    """Refuse a FAT whose BPB says it is larger than its partition: mtools would write past the partition's end."""
    bps = struct.unpack_from("<H", boot_sector, 11)[0]
    total = struct.unpack_from("<H", boot_sector, 19)[0] or struct.unpack_from("<I", boot_sector, 32)[0]
    if boot_sector[510:512] != b"\x55\xaa" or bps not in (512, 1024, 2048, 4096) or not total:
        raise Refuse(f"{what}: not a FAT file system")
    if total * bps > length:
        raise Refuse(f"{what}: its FAT says {total * bps} bytes, but the partition has {length}")


class Fat:
    def __init__(self, spec, tmp, fd=None):
        self.spec, self.tmp = spec, tmp
        self.pass_fds = (fd,) if fd is not None else ()
        self.blkfd = None            # a block device's held descriptor (open_stick)

    def sync_drop(self):
        """mtools wrote through its own descriptor: flush the device, so the checks that follow read the disk"""
        if self.blkfd is not None:
            import fcntl
            os.fsync(self.blkfd)
            fcntl.ioctl(self.blkfd, 0x1261)  # BLKFLSBUF

    def ls(self, path):
        r = subprocess.run(["mdir", "-b", "-i", self.spec, "::" + path], capture_output=True, text=True,
                           env=dict(os.environ, MTOOLS_SKIP_CHECK="1"), pass_fds=self.pass_fds)
        if r.returncode:
            return []
        return [l.strip().rstrip("/").split("/")[-1] for l in r.stdout.splitlines() if l.strip().rstrip("/")]

    def exists(self, path):
        d, _, f = path.rpartition("/")
        return f in self.ls(d or "/")

    def read(self, path):
        return run(["mcopy", "-n", "-i", self.spec, "::" + path, "-"], pass_fds=self.pass_fds)

    def write(self, path, data):
        d = path.rpartition("/")[0]
        if d:
            parts = d.strip("/").split("/")
            for i in range(len(parts)):
                sub = "/" + "/".join(parts[:i + 1])
                if not self.exists(sub):
                    run(["mmd", "-i", self.spec, "::" + sub], pass_fds=self.pass_fds)
        run(["mcopy", "-o", "-i", self.spec, "-", "::" + path], data=data, pass_fds=self.pass_fds)
        if self.read(path) != data:
            raise Failed(f"{path} on {self.spec} reads back different")

    def delete(self, path):
        if self.exists(path):
            run(["mdel", "-i", self.spec, "::" + path], pass_fds=self.pass_fds)

    def free(self):
        out = run(["minfo", "-i", self.spec, "::"], text=True, pass_fds=self.pass_fds)
        spc = int(re.search(r"cluster size:\s*(\d+) sectors", out).group(1))
        bps = int(re.search(r"sector size:\s*(\d+)", out).group(1))
        mdir = run(["mdir", "-i", self.spec, "::/"], text=True, pass_fds=self.pass_fds)
        free = int(re.search(r"([\d ]+) bytes free", mdir).group(1).replace(" ", ""))
        return free, spc * bps


class PreviewFatError(Refuse):
    """A preview FAT observation failed; it is not a damaged boot entry."""


class PreviewFat:
    """Read-only mtools view of a private ESP snapshot, with shared resource limits."""
    def __init__(self, spec, tmp, fd=None):
        self.spec = spec
        self.pass_fds = (fd,) if fd is not None else ()
        self.deadline = time.monotonic() + 120
        # A sealed shared ESP can remain open while a large source is hashed.
        # Charge only mtools execution time, not that unrelated waiting time.
        self.remaining_time = 120.0 if fd is not None else None
        self.remaining = 1024 * MIB
        self.calls = 0

    def _run(self, command, limit):
        started = time.monotonic()
        if self.remaining_time is not None:
            self.deadline = started + self.remaining_time
        self.calls += 1
        if self.calls > 4096:
            raise PreviewFatError("preview: too many FAT operations")
        limit = min(limit, self.remaining)
        if limit <= 0:
            raise PreviewFatError("preview: FAT output budget exhausted")
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=dict(os.environ, MTOOLS_SKIP_CHECK="1"),
                                pass_fds=self.pass_fds)
        try:
            out = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise PreviewFatError("preview: mtools timed out")
                    part = os.read(proc.stdout.fileno(), min(1 << 20, limit + 1 - len(out)))
                    if not part:
                        break
                    out.extend(part)
                    if len(out) > limit:
                        raise PreviewFatError("preview: FAT output limit exceeded")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise PreviewFatError("preview: mtools timed out")
            try:
                status = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise PreviewFatError("preview: mtools timed out")
            if status:
                raise PreviewFatError("preview: mtools failed")
            self.remaining -= len(out)
            return bytes(out)
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            proc.wait()
            if self.remaining_time is not None:
                self.remaining_time -= max(0, time.monotonic() - started)

    @staticmethod
    def _path(path):
        if (not isinstance(path, str) or not path.startswith('/') or len(path) > 4096
                or any(c in path for c in '*?[]\\:')
                or any(p in ('.', '..') for p in path.split('/'))):
            raise PreviewFatError("preview: invalid FAT path")
        return path.rstrip('/') or '/'

    def ls(self, path):
        path = self._path(path)
        if path != '/':
            parent, _, name = path.rpartition('/')
            if name not in self.ls(parent or '/'):
                return []
        data = self._run(["mdir", "-b", "-i", self.spec, "::" + path], MIB)
        try:
            names = [line.strip().rstrip('/').split('/')[-1]
                     for line in data.decode('utf-8').splitlines() if line.strip().rstrip('/')]
        except UnicodeDecodeError:
            raise PreviewFatError("preview: invalid FAT directory output")
        if len(names) != len(set(names)):
            raise PreviewFatError("preview: ambiguous FAT names")
        return names

    def exists(self, path):
        path = self._path(path)
        parent, _, name = path.rpartition('/')
        return name in self.ls(parent or '/')

    def read(self, path):
        path = self._path(path)
        return self._run(["mcopy", "-n", "-i", self.spec, "::" + path, "-"], PREVIEW_ESP_MAX)

    def free(self):
        out = self._run(["minfo", "-i", self.spec, "::"], MIB).decode('utf-8')
        listing = self._run(["mdir", "-i", self.spec, "::/"], MIB).decode('utf-8')
        try:
            spc = int(re.search(r"cluster size:\s*(\d+) sectors", out).group(1))
            bps = int(re.search(r"sector size:\s*(\d+)", out).group(1))
            free = int(re.search(r"([\d ]+) bytes free", listing).group(1).replace(" ", ""))
        except (AttributeError, ValueError):
            raise PreviewFatError("preview: invalid FAT space output")
        if not spc or not bps:
            raise PreviewFatError("preview: invalid FAT cluster size")
        return free, spc * bps


class BoundedMutableFat(PreviewFat):
    """Strict bounded mtools access to a held writable target descriptor."""
    def __init__(self, spec, tmp, fd):
        super().__init__(spec, tmp, fd=fd)
        self.input_remaining = 1024 * MIB

    def _mutate(self, command, data=None):
        if data is not None and (not isinstance(data, bytes) or len(data) > PREVIEW_ESP_MAX
                                 or len(data) > self.input_remaining):
            raise Failed('target FAT input limit exceeded')
        self.calls += 1
        if self.calls > 4096 or self.remaining_time is None or self.remaining_time <= 0:
            raise Failed('target FAT operation budget exhausted')
        started = time.monotonic()
        try:
            result = subprocess.run(command, input=data, stdin=subprocess.DEVNULL if data is None else None,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=dict(os.environ, MTOOLS_SKIP_CHECK='1'), pass_fds=self.pass_fds,
                                    timeout=self.remaining_time)
            if result.returncode:
                raise Failed('target FAT operation failed')
            if data is not None:
                self.input_remaining -= len(data)
        except subprocess.TimeoutExpired as exc:
            raise Failed('target FAT operation timed out') from exc
        finally:
            self.remaining_time -= max(0, time.monotonic() - started)

    def write(self, path, data):
        path = self._path(path)
        directory = path.rpartition('/')[0]
        if directory:
            parts = directory.strip('/').split('/')
            for i in range(len(parts)):
                sub = '/' + '/'.join(parts[:i + 1])
                if not self.exists(sub):
                    self._mutate(['mmd', '-i', self.spec, '::' + sub])
        self._mutate(['mcopy', '-o', '-i', self.spec, '-', '::' + path], data)
        if self.read(path) != data:
            raise Failed(f'{path} on target FAT reads back different')

    def delete(self, path):
        path = self._path(path)
        if self.exists(path):
            self._mutate(['mdel', '-i', self.spec, '::' + path])


# ------------------------------------------------------------------ sources (a release)

class Release:
    """What an update needs from a release: its GPT layout, its slot-a partitions, its super, its ESP files."""

    def __init__(self, layout, read_phys, super_rd, super_size, esp_fat, ident, preview=False, preview_tmp=None):
        self.layout, self.read_phys, self.super_rd, self.super_size, self.esp, self.ident = \
            layout, read_phys, super_rd, super_size, esp_fat, ident
        self.lp = LP.read(super_rd, super_size)
        self.dyn = {p["name"][:-2]: p for p in self.lp.parts if p["name"].endswith("_a") and p["extents"]}
        if "system" not in self.dyn or "vendor" not in self.dyn:
            raise Refuse("release: super has no system_a or vendor_a")
        self.kernel = esp_fat.read("/kernel.efi")
        initrd = esp_fat.read("/initrd.img")
        self.ramdisk, self.bc = split_initrd(initrd)
        if bc_get(self.bc, "androidboot.slot_suffix") != "_a":
            raise Refuse("release: its initrd is not for slot a")
        self.entries = {n: entry_parse(esp_fat.read("/loader/entries/" + n).decode()) for n in ENTRIES
                        if n in REQUIRED_ENTRIES or esp_fat.exists("/loader/entries/" + n)}
        self.loader = esp_fat.read("/loader/loader.conf").decode()
        self.vbmeta = b"".join(read_phys("vbmeta_a"))
        self.key = vbmeta_key(self.vbmeta)
        self.fingerprint = ramdisk_prop(self.ramdisk, "ro.bootimage.build.fingerprint", preview=preview, preview_tmp=preview_tmp)
        self.build = build_id(self.fingerprint)
        self.model = model_of(self.entries["lapis-quiet.conf"]["title"])
        if {model_of(e["title"]) for e in self.entries.values()} != {self.model}:
            raise Refuse("release: its entries name different models")


class SharedRelease(Release):
    """Release parsed from a catalog-backed shared source; requires its live binding."""


def image_release(path, tmp, check_sha=True):
    if check_sha:
        sp = path + ".sha256"
        if not os.path.isfile(sp):
            raise Refuse(f"{sp} is missing")
        want = open(sp).read().split()[0]
        say(f"checking {os.path.basename(path)} against its .sha256 (reads the whole image)...")
        img = Disk(path)
        if img.sha(0, img.size) != want:
            raise Refuse(f"{path} does not match its .sha256")
    img = Disk(path)
    return image_release_from_reader(img, tmp, f"image {os.path.basename(path)}")



# Preview-only limits: observed base ESP 64 MiB, vbmeta 4 MiB; the miniature
# fixture uses 64 KiB LP metadata and three slots. Legacy update behavior is unchanged.
PREVIEW_ESP_MAX = 512 * MIB
PREVIEW_VBMETA_MAX = 16 * MIB
PREVIEW_LP_MAX = 1024 * 1024
PREVIEW_LP_SLOTS = 4
PREVIEW_LP_REFERENCES = 16384


def preview_resource_limits(img, lay):
    if (lay[ESP]["len"] > PREVIEW_ESP_MAX or
            any(lay[f"vbmeta_{slot}"]["len"] > PREVIEW_VBMETA_MAX for slot in "ab")):
        raise Refuse("preview: ESP or vbmeta exceeds resource limit")
    sp = lay["super"]
    def read(off, n):
        if off < 0 or n < 0 or off > sp["len"] or n > sp["len"] - off:
            raise Refuse("preview: LP metadata outside super")
        return img.pread(sp["start"] + off, n)
    g1, g2 = read(RESERVED, GEO_SIZE), read(RESERVED + GEO_SIZE, GEO_SIZE)
    mx, slots, lbs = LP.geometry(g1)
    if LP.geometry(g2) != (mx, slots, lbs) or not 128 <= mx <= PREVIEW_LP_MAX or not 1 <= slots <= PREVIEW_LP_SLOTS:
        raise Refuse("preview: LP geometry exceeds resource limits")
    if RESERVED + 2 * GEO_SIZE + 2 * slots * mx > sp["len"]:
        raise Refuse("preview: LP metadata exceeds super")
    # LP.parse materializes each partition's referenced extent list. Bound both
    # table lengths and the sum of references before allowing that expansion.
    for slot in range(slots):
        for base in (RESERVED + 2 * GEO_SIZE, RESERVED + 2 * GEO_SIZE + slots * mx):
            raw = read(base + slot * mx, mx)
            if len(raw) < 128:
                raise Refuse("preview: short LP metadata")
            hsz, tsize = struct.unpack_from("<I", raw, 8)[0], struct.unpack_from("<I", raw, 44)[0]
            if not 128 <= hsz <= 512 or hsz + tsize > mx:
                raise Refuse("preview: invalid LP metadata length")
            off, count, esz = struct.unpack_from("<III", raw, 80)
            if esz != 52 or count > 4096 or off + count * esz > tsize:
                raise Refuse("preview: invalid LP partition table")
            refs = 0
            for i in range(count):
                refs += struct.unpack_from("<I", raw, hsz + off + i * esz + 44)[0]
                if refs > PREVIEW_LP_REFERENCES:
                    raise Refuse("preview: excessive LP extent references")


def validate_preview_layout(img, lay):
    # GPT parsing above authenticates the primary table, not its extent bounds.
    hd = img.pread(512, 512)
    current, backup, first, last = struct.unpack_from("<QQQQ", hd, 24)
    table_lba, count, entry_size = struct.unpack_from("<QII", hd, 72)
    table_end = table_lba * 512 + count * entry_size
    if (img.size % 512 or current != 1 or backup != img.size // 512 - 1 or table_lba < 2
            or not count or table_end > first * 512 or first > last
            or (last + 1) * 512 > img.size - 33 * 512):
        raise Refuse(f"{img.path}: invalid GPT bounds")
    ranges = sorted((p["start"], p["start"] + p["len"], name) for name, p in lay.items())
    end = first * 512
    for start, stop, name in ranges:
        if start < end or stop <= start or stop > (last + 1) * 512:
            raise Refuse(f"{img.path}: invalid or overlapping extent {name}")
        end = stop



class PreviewMetadataSnapshot:
    """Bounded immutable GPT and LP metadata captured from an already-open reader.

    This captures individual ranges once, not an atomic point-in-time disk image.
    The caller must separately check target identity and concurrent modification.
    """
    def __init__(self, disk, tmp=None, sealed_esp=False):
        self.size, self.path = disk.size, disk.path
        self.ranges = []
        self._esp_fd = None
        self._esp_range = None
        if sealed_esp and tmp is None:
            raise Refuse('sealed target ESP requires a preview directory')

        def capture(off, length):
            if (type(off) is not int or type(length) is not int or off < 0 or length < 0
                    or off > self.size or length > self.size - off):
                raise Refuse("preview: snapshot range outside target")
            data = disk.pread(off, length)
            if len(data) != length:
                raise Refuse("preview: short snapshot read")
            self.ranges.append((off, data))
            return data

        header = capture(0, 1024)
        lba, count, size = struct.unpack_from("<QII", header, 512 + 72)
        length = count * size
        if not length or length > MIB:
            raise Refuse("preview: GPT table exceeds snapshot limit")
        capture(lba * SECTOR, length)
        self.gpt = gpt_read(self)
        validate_preview_layout(self, self.gpt)
        sup = self.gpt["super"]
        if sup["len"] < RESERVED + 2 * GEO_SIZE:
            raise Refuse("preview: super too small for geometry")
        geo = capture(sup["start"] + RESERVED, 2 * GEO_SIZE)
        mx, slots, lbs = LP.geometry(geo[:GEO_SIZE])
        if (LP.geometry(geo[GEO_SIZE:]) != (mx, slots, lbs) or not 128 <= mx <= PREVIEW_LP_MAX
                or not 1 <= slots <= PREVIEW_LP_SLOTS):
            raise Refuse("preview: LP geometry exceeds snapshot limits")
        total = 2 * slots * mx
        if RESERVED + 2 * GEO_SIZE + total > sup["len"]:
            raise Refuse("preview: LP metadata exceeds super")
        capture(sup["start"] + RESERVED + 2 * GEO_SIZE, total)
        preview_resource_limits(self, self.gpt)
        self.lp = LP.read(lambda off, n: self.pread(sup["start"] + off, n), sup["len"])
        if tmp is not None:
            for slot in "ab":
                vb = self.gpt[f"vbmeta_{slot}"]
                capture(vb["start"], vb["len"])
            esp = self.gpt[ESP]
            if esp["len"] > PREVIEW_ESP_MAX:
                raise Refuse("preview: ESP exceeds resource limit")
            if sealed_esp:
                self._capture_sealed_esp(disk, esp, tmp)
            else:
                dest = os.path.join(tmp, "installed-esp.img")
                with open(dest, "xb") as out:
                    for offset in range(0, esp["len"], MIB):
                        n = min(MIB, esp["len"] - offset)
                        data = disk.pread(esp["start"] + offset, n)
                        if len(data) != n:
                            raise Refuse("preview: short ESP read")
                        out.write(data)
                with open(dest, "rb") as source:
                    fat_fits(source.read(512), esp["len"], "preview target ESP")
                self.fat = PreviewFat(dest, tmp)

    def _capture_sealed_esp(self, disk, esp, tmp):
        try:
            import fcntl
            fd = os.memfd_create('gbos-target-esp', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            self._esp_fd = fd
            for offset in range(0, esp['len'], MIB):
                n = min(MIB, esp['len'] - offset)
                data = disk.pread(esp['start'] + offset, n)
                if len(data) != n:
                    raise Refuse('preview: short ESP read')
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise Refuse('cannot populate sealed target ESP')
                    view = view[written:]
            seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
            if fcntl.fcntl(fd, fcntl.F_GET_SEALS) & seals != seals:
                raise Refuse('cannot seal target ESP')
            fat_fits(os.pread(fd, 512, 0), esp['len'], 'preview target ESP')
            self._esp_range = (esp['start'], esp['len'])
            self.fat = PreviewFat(f'/proc/self/fd/{fd}', tmp, fd=fd)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._esp_fd is not None:
            fd, self._esp_fd = self._esp_fd, None
            os.close(fd)

    def verify_unchanged(self, disk):
        if self._esp_fd is None or self._esp_range is None or disk.size != self.size:
            raise Refuse('sealed target snapshot is unavailable or size changed')
        for offset, data in self.ranges:
            if disk.pread(offset, len(data)) != data:
                raise Refuse('target metadata changed after preview')
        start, length = self._esp_range
        for offset in range(0, length, MIB):
            n = min(MIB, length - offset)
            if disk.pread(start + offset, n) != os.pread(self._esp_fd, n, offset):
                raise Refuse('target ESP changed after preview')

    def pread(self, off, length):
        if type(off) is not int or type(length) is not int or off < 0 or length < 0:
            raise Refuse("preview: invalid snapshot read")
        for start, data in self.ranges:
            if start <= off and off + length <= start + len(data):
                return data[off - start:off - start + length]
        raise Refuse("preview: read outside captured metadata")


def image_release_from_reader(img, tmp, ident, validate_layout=False, bounded_fat=False, preview_tmp=None,
                              sealed_esp=False):
    """Parse an image from an owned pread reader. The caller owns its lifetime."""
    lay = gpt_read(img)
    if validate_layout:
        validate_preview_layout(img, lay)
        preview_resource_limits(img, lay)

    def read_phys(name):
        p = lay[name]
        for o in range(0, p["len"], 16 << 20):
            yield img.pread(p["start"] + o, min(16 << 20, p["len"] - o))

    sp = lay["super"]
    if sealed_esp:
        # The binding owns this fd even if later parsing fails.  Each byte is
        # supplied by its verified reader, and no writable ESP path exists.
        try:
            import fcntl
            fd = os.memfd_create('gbos-shared-esp', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            img._esp_fd = fd
            for data in read_phys(ESP):
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise Refuse('cannot populate sealed ESP')
                    view = view[written:]
            seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
            if fcntl.fcntl(fd, fcntl.F_GET_SEALS) & seals != seals:
                raise Refuse('cannot seal shared ESP')
        except (AttributeError, ImportError, OSError) as e:
            raise Refuse(f'sealed shared ESP unavailable: {e}') from e
        esp = PreviewFat(f'/proc/self/fd/{fd}', tmp, fd=fd)
    else:
        esp_t = os.path.join(tmp, "release-esp.img")
        with open(esp_t, "wb") as f:
            for b in read_phys(ESP):
                f.write(b)
        esp = PreviewFat(esp_t, tmp) if bounded_fat else Fat(esp_t, tmp)
    def read_super(o, n):
        if validate_layout and (o < 0 or n < 0 or o > sp["len"] or n > sp["len"] - o):
            raise Refuse("release: super read out of bounds")
        return img.pread(sp["start"] + o, n)

    release_type = SharedRelease if sealed_esp else Release
    return release_type(lay, read_phys, read_super, sp["len"], esp, ident,
                        preview=validate_layout, preview_tmp=preview_tmp)


def pristine_release(pdir, superdev, tmp):
    """The installer stick's data: manifest + pristine partition files (checked by SHA-256) + super on the stick."""
    lines = open(os.path.join(pdir, "manifest.txt")).read().splitlines()
    img = lines[0].split()
    lay, sha = {}, {}
    for line in lines[2:]:
        _, n, name, start, length, attrs, typ, s, kind = line.split()
        lay[name] = dict(n=int(n), start=int(start), len=int(length), type=typ, attrs=int(attrs, 16), kind=kind)
        sha[name] = s
    sdev = Disk(superdev)
    if sdev.size != lay["super"]["len"]:
        raise Refuse("the stick's super is not the size the installer data says")
    say("checking the release's super on this stick against the installer data...")
    if sdev.sha(0, sdev.size) != sha["super"]:
        raise Refuse("super on this stick does not match the installer data")

    def read_phys(name):
        p = subprocess.Popen(["zstd", "-q", "-dc", os.path.join(pdir, f"p{lay[name]['n']}.img.zst")], stdout=subprocess.PIPE)
        h, n = hashlib.sha256(), 0
        while True:
            b = p.stdout.read(16 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
            yield b
        if p.wait() or h.hexdigest() != sha[name] or n != lay[name]["len"]:
            raise Refuse(f"installer data for {name} is damaged")

    esp_t = os.path.join(tmp, "release-esp.img")
    with open(esp_t, "wb") as f:
        for b in read_phys(ESP):
            f.write(b)
    for name in [f"{p}_a" for p in SLOT_PHYS]:     # check every file now, before anything is written
        for _ in read_phys(name):
            pass
    return Release(lay, read_phys, lambda o, n: sdev.pread(o, n), sdev.size, Fat(esp_t, tmp), f"installer data {img[1]}")


# ------------------------------------------------------------------ the installed disk

SELECT = ("androidboot.vendor.apex.com.android.hardware.keymint", "androidboot.vendor.apex.com.android.hardware.gatekeeper",
          "androidboot.hypervisor.protected_vm.supported")



class SharedBinding:
    """Owned reader/release association; catalog selection is the caller's boundary."""

    def __init__(self, fd, item, catalog_sha256, source_fds, tmp, bounded_fat=False, preview_tmp=None):
        self._closed = False
        self._esp_fd = None
        image = item['image']
        self.fd = fd
        self.size = image['size']
        self.profile = item['profile']
        self.catalog_sha256 = catalog_sha256
        self.image_sha256 = image['sha256']
        self.source_fds = tuple(source_fds)
        if (self.profile not in ('kano', 'amd-hx370') or type(self.size) is not int or self.size <= 0
                or self.size > 128 * 1024 ** 3
                or not re.fullmatch('[0-9a-f]{64}', self.catalog_sha256)
                or not re.fullmatch('[0-9a-f]{64}', self.image_sha256)
                or fd not in self.source_fds):
            raise Refuse('invalid shared source identity')
        self.source_stat = self._signature(os.fstat(fd))
        self._chunk_hashes = None
        self._verify_source()
        try:
            self.release = image_release_from_reader(self, tmp, 'shared synthetic release', validate_layout=True,
                                                     sealed_esp=True,
                                                     **({"bounded_fat": True} if bounded_fat else {}),
                                                     **({"preview_tmp": preview_tmp} if preview_tmp is not None else {}))
            self._verify_source()
        except BaseException:
            self.close()
            raise

    def close(self):
        """Release our ESP snapshot; the caller retains ownership of catalog fds."""
        self._closed = True
        if self._esp_fd is not None:
            fd, self._esp_fd = self._esp_fd, None
            os.close(fd)

    @staticmethod
    def _signature(st):
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns

    @property
    def path(self):
        return '<catalog-backed source>'

    def _read_exact(self, off, n):
        out = bytearray()
        while len(out) < n:
            data = os.pread(self.fd, n - len(out), off + len(out))
            if not data:
                raise Refuse('shared source short read')
            out.extend(data)
        return bytes(out)

    def pread(self, off, n):
        if self._closed:
            raise Refuse('shared binding is closed')
        if type(off) is not int or type(n) is not int or off < 0 or n < 0 or off > self.size or n > self.size - off:
            raise Refuse('shared source read out of bounds')
        if self._chunk_hashes is None:
            raise Refuse('shared source is not verified')
        out = bytearray()
        end = off + n
        while off < end:
            chunk_number = off // MIB
            chunk_start = chunk_number * MIB
            chunk = self._read_exact(chunk_start, min(MIB, self.size - chunk_start))
            digest_offset = chunk_number * hashlib.sha256().digest_size
            expected = self._chunk_hashes[digest_offset:digest_offset + hashlib.sha256().digest_size]
            if hashlib.sha256(chunk).digest() != expected:
                raise Refuse('shared source chunk changed')
            take = min(end - off, len(chunk) - (off - chunk_start))
            out.extend(chunk[off - chunk_start:off - chunk_start + take])
            off += take
        return bytes(out)

    def _verify_source(self):
        st = os.fstat(self.fd)
        if not stat.S_ISREG(st.st_mode) or self._signature(st) != self.source_stat or st.st_size != self.size:
            raise Refuse('shared source changed or is not a regular file')
        h = hashlib.sha256()
        chunk_hashes = bytearray() if self._chunk_hashes is None else None
        for off in range(0, self.size, MIB):
            n = min(MIB, self.size - off)
            chunk = self._read_exact(off, n) if chunk_hashes is not None else self.pread(off, n)
            h.update(chunk)
            if chunk_hashes is not None:
                chunk_hashes.extend(hashlib.sha256(chunk).digest())
        if h.hexdigest() != self.image_sha256 or self._signature(os.fstat(self.fd)) != self.source_stat:
            raise Refuse('shared source changed or hash differs')
        if chunk_hashes is not None:
            self._chunk_hashes = bytes(chunk_hashes)

    def verify(self, release, disk=None):
        if getattr(self, '_closed', True):
            raise Refuse('shared binding is closed')
        if release is not self.release:
            raise Refuse('release is not tied to this shared source')
        SharedBinding._verify_source(self)
        if disk is not None:
            target = os.fstat(disk.fd)
            for fd in self.source_fds:
                source = os.fstat(fd)
                if not stat.S_ISREG(source.st_mode):
                    raise Refuse('catalog payload is not a regular file')
                if (source.st_dev, source.st_ino) == (target.st_dev, target.st_ino):
                    raise Refuse('catalog payload and target are the same file')
        return {'profile': self.profile, 'catalog_sha256': self.catalog_sha256,
                'image_sha256': self.image_sha256, 'image_size': self.size}


def shared_state(st):
    """Strictly parse shared state; absence is legacy state, never shared proof."""
    lines = st.splitlines()
    shared = [line for line in lines if line.split(' ', 1)[0].startswith('shared-')]
    if not shared:
        return None
    pairs = [line.split(' ', 1) for line in lines]
    required = ('updating', 'from', 'build', 'fingerprint', 'key', 'model', 'release',
                *("select." + k for k in SELECT), 'shared-version', 'shared-digest')
    if any(len(p) != 2 or not p[1] for p in pairs):
        raise Refuse('shared update state malformed')
    keys = [p[0] for p in pairs]
    if set(keys) != set(required) or len(keys) != len(set(keys)):
        raise Refuse('shared update state missing or duplicated fields')
    values = dict(pairs)
    if (values['shared-version'] != '1' or not re.fullmatch('[0-9a-f]{64}', values['shared-digest'])
            or values['updating'] not in ('a', 'b') or values['from'] not in ('a', 'b')
            or values['updating'] == values['from'] or not values['build'].isdigit()):
        raise Refuse('shared update state malformed')
    return values['shared-digest']


def shared_identity(disk, inst, source, mode, without_fallback, device_identity=None):
    """Consistency identifier only; does not authenticate a physical device."""
    header = disk.pread(512, 512)
    partitions = [{k: p[k] for k in ('n', 'start', 'len', 'type', 'guid', 'attrs')} | {'name': name}
                  for name, p in sorted(inst.gpt.items())]
    identity = {'v': 1, 'operation': 'keep-data-update', 'source': source,
                'mode': mode, 'without_fallback': without_fallback,
                'current': inst.current, 'target': inst.target,
                'disk_size': disk.size, 'disk_guid': str(uuid.UUID(bytes_le=header[56:72])),
                'partitions': partitions}
    if device_identity is not None:
        if not isinstance(device_identity, str) or not re.fullmatch('[0-9a-f]{64}', device_identity):
            raise Refuse('invalid shared block device identity')
        identity['v'] = 2
        identity['target_device_sha256'] = device_identity
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Installed:
    """What is on the disk: partition table, super metadata, boot menu, which slot starts by default and the facts of
    that slot (Google build, AVB key, model, KeyMint/Gatekeeper selection). While an update is unfinished, the facts
    come from /gbos/update-state.txt, written before its first change."""

    def __init__(self, disk, tmp, preview=False, snapshot=None, preview_tmp=None):
        self.disk = disk
        self.gpt = snapshot.gpt if snapshot is not None else gpt_read(disk)
        self.super = self.gpt["super"]
        self.lp = snapshot.lp if snapshot is not None else LP.read(lambda o, n: disk.pread(self.super["start"] + o, n), self.super["len"])
        esp = self.gpt[ESP]
        if snapshot is not None:
            if not preview:
                raise Refuse("snapshot requires preview")
            self.fat = snapshot.fat
        else:
            fat_fits(disk.pread(esp["start"], 512), esp["len"], "the disk's ESP")
            self.fat = Fat(disk.vol(esp["start"]), tmp, fd=disk.fd)
        self.entries, self.slot_of = {}, {}
        for name in self.fat.ls("/loader/entries"):
            if not name.endswith(".conf"):
                continue
            try:
                e = entry_parse(self.fat.read("/loader/entries/" + name).decode())
            except PreviewFatError:
                raise
            except (Refuse, RuntimeError, UnicodeDecodeError):
                self.slot_of[name] = None
                continue
            self.entries[name] = e
            try:
                _, bc = split_initrd(self.fat.read(e["initrd"][-1]))
                self.slot_of[name] = bc_get(bc, "androidboot.slot_suffix").lstrip("_")
            except PreviewFatError:
                raise
            except (Refuse, RuntimeError):
                self.slot_of[name] = None        # file missing or damaged (an interrupted update)
        try:
            m = re.search(r"^default\s+(\S+)", self.fat.read("/loader/loader.conf").decode(), re.M)
        except RuntimeError:
            m = None
        self.default = m.group(1) if m else None
        st = self.fat.read(STATE).decode() if self.fat.exists(STATE) else ""
        self.shared_digest = shared_state(st) if st else None
        self.state_original = st
        if st:
            kv = dict(l.split(" ", 1) for l in st.splitlines() if " " in l)
            self.resume = kv.get("updating")
            if self.resume not in ("a", "b"):
                raise Refuse(f"{STATE} is damaged")
            self.current = "b" if self.resume == "a" else "a"
            self.build, self.fingerprint = int(kv["build"]), kv["fingerprint"]
            self.key, self.model = kv["key"], kv["model"]
            self.select = {k: kv["select." + k] for k in SELECT}
        else:
            self.resume = None
            self.current = self.slot_of.get(self.default)
            if self.current not in ("a", "b"):
                raise Refuse("cannot tell which slot starts by default (damaged boot menu, no update in progress)")
            cur = self.entries[self.default]
            rd, bc = split_initrd(self.fat.read(cur["initrd"][-1]))
            self.fingerprint = ramdisk_prop(rd, "ro.bootimage.build.fingerprint", preview=preview, preview_tmp=preview_tmp)
            self.build = build_id(self.fingerprint)
            vb = self.gpt[f"vbmeta_{self.current}"]
            self.key = vbmeta_key((snapshot if snapshot is not None else disk).pread(vb["start"], vb["len"]))
            self.model = model_of(cur["title"])
            self.select = {k: bc_get(bc, k) for k in SELECT}
        self.target = "b" if self.current == "a" else "a"

    def state_text(self, release):
        return "".join(f"{k} {v}\n" for k, v in [("updating", self.target), ("from", self.current), ("build", self.build),
                                                 ("fingerprint", self.fingerprint), ("key", self.key), ("model", self.model),
                                                 ("release", release)] + [("select." + k, v) for k, v in self.select.items()])

    def facts(self):
        prev = {self.slot_of[n] for n in self.entries if n.startswith(PREV)} - {None}
        return [f"disk {self.disk.path}", f"model {self.model}", f"current slot {self.current} ({self.default})",
                f"previous slot on the menu: {','.join(sorted(prev)) or 'none'}", f"google build {self.build} ({self.fingerprint})",
                f"avb key sha256 {self.key}", f"super metadata copies consistent: {self.lp.consistent}",
                f"update in progress: {self.resume or 'no'}"]


# ------------------------------------------------------------------ plan and apply

def check(inst, rel, mode):
    """Every reason to refuse; nothing is written here."""
    for name, p in rel.layout.items():
        q = inst.gpt.get(name)
        if q is None:
            raise Refuse(f"the disk has no {name}; this release needs a fresh install")
        same = (q["n"], q["type"]) == (p["n"], p["type"]) and (name == "userdata" or (q["start"], q["len"]) == (p["start"], p["len"]))
        if not same:
            raise Refuse(f"partition {name} differs between the disk and the release; this release needs a fresh install")
    if set(inst.gpt) != set(rel.layout):
        raise Refuse("the disk has partitions the release does not have; this release needs a fresh install")
    if rel.model != inst.model:
        raise Refuse(f"the release is for {rel.model}, the disk holds Googlebook for {inst.model}")
    if rel.key != inst.key:
        raise Refuse("the release is signed with another AVB key than the installed system. Build the update with the "
                     "same work directory (its base/keys/vbmeta.pem); otherwise only a fresh install works (data lost)")
    for k in SELECT:
        if bc_get(rel.bc, k) != inst.select[k]:
            raise Refuse(f"the release changes {k}; the lock screen and keystore keys would stop working")
    if rel.build < inst.build:
        raise Refuse(f"the release has an older Google build ({rel.build}) than the disk ({inst.build}); "
                     "going back needs a fresh install")
    keep = {p["name"] for p in inst.lp.parts if not p["name"].endswith("_" + inst.target)}
    sizes = {f"{b}_{inst.target}": sum(n for _, n in p["extents"]) * SECTOR for b, p in rel.dyn.items()}
    for b, p in rel.dyn.items():
        if inst.lp.part(f"{b}_{inst.target}") is None and inst.lp.part(f"{b}_{inst.current}") is None:
            raise Refuse(f"the release has a new dynamic partition {b}; this release needs a fresh install")
    gi = inst.lp.groups.index(f"google_dynamic_partitions_{inst.target}") if f"google_dynamic_partitions_{inst.target}" in inst.lp.groups else None
    if gi is None:
        raise Refuse("super has no group for the target slot")
    if sum(sizes.values()) > inst.lp.group_max[gi] and inst.lp.group_max[gi]:
        raise Refuse("the release's partitions are larger than the slot's group in super")
    ext = allocate(inst.lp, sizes, keep)
    return dict(sizes=sizes, extents=ext, group=gi)


def boot_files(inst, rel, mode, tmp, preview=False, preview_tmp=None):
    """The target slot's ESP files and entries, the running slot's entries as prev-<entry>, and whether the ESP has
    room for both slots' files (otherwise the previous version cannot stay on this disk's menu)."""
    t, c = inst.target, inst.current
    bc = bc_set(rel.bc, "androidboot.slot_suffix", "_" + t)
    bc = bc_set(bc, "androidboot.boot_part_uuid", inst.gpt["boot_a"]["guid"])
    raw = os.path.join(tmp, "ramdisk.cpio")
    if preview:
        if preview_tmp is None:
            initrd = preview_ramdisk_compress(preview_ramdisk_plain(rel.ramdisk)) + bc_blob(bc)
        else:
            initrd = preview_ramdisk_compress(preview_ramdisk_plain(rel.ramdisk, preview_tmp), preview_tmp) + bc_blob(bc)
    else:
        with open(raw, "wb") as f:
            f.write(ramdisk_plain(rel.ramdisk))
        initrd = run(["lz4", "-l", "-12", "--favor-decSpeed", "-c", raw]) + bc_blob(bc)
    ks = hashlib.sha256(rel.kernel).hexdigest()
    running = {n: e for n, e in inst.entries.items() if inst.slot_of.get(n) == c}
    kernel_path = None
    for p in sorted({e["linux"] for e in running.values()}):   # the running slot's kernel, when it is the same
        if hashlib.sha256(inst.fat.read(p)).hexdigest() == ks:
            kernel_path = p
            break
    files = {f"/gbos/{t}/initrd.img": initrd}
    if kernel_path is None:
        kernel_path = f"/gbos/{t}/kernel.efi"
        files[kernel_path] = rel.kernel
    entries = {}
    for n, e in rel.entries.items():
        opts = lift(e["options"]) if mode == "nvme" else e["options"]
        entries[n] = entry_text(e["title"], kernel_path, [f"/gbos/{t}/initrd.img"], opts)
    loader = "default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n" if mode == "nvme" else rel.loader
    prev = {}
    for n in ENTRIES:
        e = running.get(n)
        if e is not None and n in rel.entries:
            prev[PREV + n] = entry_text(e["title"] + PREV_TITLE, e["linux"], e["initrd"], e["options"])
    # running-slot entries the release no longer has (the plain lapis.conf of releases before v0.2): off the menu
    stale = sorted(n for n in running if not n.startswith(PREV) and n not in rel.entries)
    keep_files = {x for e in running.values() for x in [e["linux"], *e["initrd"]]}
    drop_files = {x for n, e in inst.entries.items() if inst.slot_of.get(n) != c for x in [e["linux"], *e["initrd"]]}
    drop_files |= {f"/gbos/{t}/{f}" for f in inst.fat.ls(f"/gbos/{t}")}     # leftovers of an unfinished update
    drop_files -= keep_files
    free, cl = inst.fat.free()
    rup = lambda n: -(-n // cl) * cl
    reclaim = sum(rup(len(inst.fat.read(x))) for x in drop_files if inst.fat.exists(x))
    need = sum(rup(len(v)) for v in files.values()) + rup(4096) * (len(entries) + len(prev) + 4)
    fits = free + reclaim >= need + 4 * cl
    if os.environ.get("GBOS_UPDATE_TEST") and os.environ.get("GBOS_UPDATE_TEST_NO_PREV_ROOM"):
        fits = False                                 # test hook: as if the ESP had no room for the previous version
    # without room for a previous version: can the new files still be written before the old ones are removed?
    new_first = free + reclaim >= sum(rup(len(v)) for v in files.values()) + rup(4096) * (len(entries) + 4) + 4 * cl
    return dict(files=files, entries=entries, prev=prev, stale=stale, loader=loader, drop=sorted(drop_files), keep=sorted(keep_files),
                fits=fits, new_first=new_first, free=free, reclaim=reclaim, need=need, cluster=cl, kernel=rel.kernel,
                initrd=initrd, running=running)


def plan_text(inst, rel, chk, bf):
    t = inst.target
    lines = [f"release: {rel.ident}, google build {rel.build}, model {rel.model}",
             f"update: slot {t} gets the release; slot {inst.current} (running) stays as it is",
             "super: " + ", ".join(f"{n} {s // MIB} MiB" for n, s in sorted(chk["sizes"].items())),
             "slot partitions: " + ", ".join(f"{p}_{t}" for p in SLOT_PHYS),
             f"boot files: {', '.join(bf['files'])} ({'kernel shared with the running slot' if len(bf['files']) == 1 else 'new kernel'})",
             "previous version kept on this disk's boot menu: " + ("yes" if bf["fits"] and bf["prev"] else
             "NO (the ESP has no room for both)" if bf["prev"] else "NO (the running version's boot files are gone)")
             + f" (free {bf['free'] // 1024} KiB, reclaimed {bf['reclaim'] // 1024} KiB, needed {bf['need'] // 1024} KiB)"
             + ("" if bf["fits"] else "; the new boot files are written " + ("before the old ones are removed" if bf["new_first"]
                else "only after the old ones are removed (the disk's own menu cannot start either version until the update finishes)")),
             "never written: " + ", ".join(NEVER) + ", the partition table, every other partition"]
    if rel.build != inst.build:
        lines.append(f"NOTE: new Google build {inst.build} -> {rel.build}. Once the new version has started, Android has"
                     " upgraded /data; the previous version may then not start with it")
    return lines


def write_stream(disk, ranges, chunks):
    """Write a stream of byte chunks across [(byte offset, length)] ranges; returns the SHA-256 of what was written."""
    h = hashlib.sha256()
    ri, roff = 0, 0
    try:
        iterator = iter(chunks)
    except Refuse as e:
        raise Failed(f"release source refused during write: {e}") from e
    while True:
        try:
            b = next(iterator)
        except StopIteration:
            break
        except Refuse as e:
            raise Failed(f"release source refused during write: {e}") from e
        h.update(b)
        mv = memoryview(b)
        while mv:
            if ri >= len(ranges):
                raise Failed("more data than room")
            off, ln = ranges[ri]
            n = min(len(mv), ln - roff)
            disk.pwrite(off + roff, mv[:n])
            mv, roff = mv[n:], roff + n
            if roff == ln:
                ri, roff = ri + 1, 0
    return h.hexdigest()


def readback(disk, ranges, total):
    h = hashlib.sha256()
    left = total
    for off, ln in ranges:
        n = min(ln, left)
        for o in range(0, n, 16 << 20):
            h.update(disk.pread(off + o, min(16 << 20, n - o)))
        left -= n
    return h.hexdigest()


def step(name):
    say(f"ok   {name}")
    if FAIL_AT and FAIL_AT == name.split(":")[0]:
        raise Failed(f"test hook: stopped after {name}")


def guard_ranges(inst):
    """Byte ranges an update never writes: the partition table areas and every partition except super, the ESP
    (written through mtools only) and the target slot's partitions."""
    g = inst.gpt
    first = min(p["start"] for p in g.values())
    last = max(p["start"] + p["len"] for p in g.values())
    out = [(0, first, "the partition table"), (last, inst.disk.size, "the backup partition table")]
    allowed = {"super"} | {f"{p}_{inst.target}" for p in SLOT_PHYS}
    out += [(p["start"], p["start"] + p["len"], name) for name, p in g.items() if name not in allowed]
    # inside super: the running slot's extents
    for q in inst.lp.parts:
        if not q["name"].endswith("_" + inst.target):
            out += [(inst.super["start"] + a * SECTOR, inst.super["start"] + (a + n) * SECTOR, q["name"]) for a, n in q["extents"]]
    return out


def stick_entries(sb):
    """{name: (entry or None, slot or None)} of the installer stick's internal-disk entries (gbos-nvme*.conf); the slot
    comes from the androidboot.slot_suffix in the bootconfig of the initrd the entry starts."""
    out = {}
    for n in sb.ls("/loader/entries"):
        if not (n.startswith("gbos-nvme") and n.endswith(".conf")):
            continue
        try:
            e = entry_parse(sb.read("/loader/entries/" + n).decode())
        except (Refuse, RuntimeError, UnicodeDecodeError):
            out[n] = (None, None)
            continue
        try:
            _, bc = split_initrd(sb.read(e["initrd"][-1]))
            out[n] = (e, bc_get(bc, "androidboot.slot_suffix").lstrip("_"))
        except (Refuse, RuntimeError):
            out[n] = (e, None)
    return out


def stick_source(sb, inst, c):
    """Where the stick's copy of the running slot c comes from: the internal disk's own running quiet entry (what slot
    c really holds; a copy on the stick may be older, after an update from another stick), or, when its files are
    gone (a rerun after a stop inside the --without-fallback window), the stick's entry for slot c that the first
    attempt wrote. Returns (entry, on_stick); refuses before anything is written when there is neither."""
    run_e = inst.entries.get(inst.default)
    if run_e is not None and inst.slot_of.get(inst.default) == c:
        return run_e, False
    ents = stick_entries(sb)
    for n in ("gbos-nvme.conf", "gbos-nvme-prev.conf"):
        e, s = ents.get(n, (None, None))
        if e is not None and s == c:
            return e, True
    raise Refuse("cannot find the running version's boot files, neither on the internal disk nor on the installer stick")


def stick_copy(sb, inst, src, on_stick, c):
    """{path on the stick: data} of the copy of slot c under /gbos-nvme/<c>/ (src's files, read from where they are)"""
    read = sb.read if on_stick else inst.fat.read
    return {f"/gbos-nvme/{c}/{os.path.basename(x)}": read(x) for x in [src["linux"], *src["initrd"]]}


def stick_needs(sb, bf, t, copy):
    """Room on the stick's boot partition for the copy of the running slot (what is not there yet) and the target
    slot's new files; the target slot's old files there are removed first."""
    free, cl = sb.free()
    rup = lambda n: -(-n // cl) * cl
    copy_c = sum(rup(len(d)) for y, d in copy.items() if not (sb.exists(y) and sb.read(y) == d))
    reclaim = sum(rup(len(sb.read(f"/gbos-nvme/{t}/{f}"))) for f in sb.ls(f"/gbos-nvme/{t}"))
    return free + reclaim >= copy_c + rup(len(bf["kernel"])) + rup(len(bf["initrd"])) + 16 * cl


def stick_prepare(sb, t, c, src, copy):
    """Before any slot is written: the stick's 'Googlebook (internal disk)' and '(previous version)' entries start the
    running slot c from the stick's copy under /gbos-nvme/<c>/, and nothing there points at the target slot t (its
    entries and files are removed). Runs again unchanged after a stop at any later step: the stick's entries then
    start slot c again until the rerun has written slot t."""
    for y, data in copy.items():
        if not sb.exists(y) or sb.read(y) != data:
            sb.write(y, data)
    linux = f"/gbos-nvme/{c}/{os.path.basename(src['linux'])}"
    initrd = [f"/gbos-nvme/{c}/{os.path.basename(x)}" for x in src["initrd"]]
    sb.write("/loader/entries/gbos-nvme-prev.conf",
             entry_text("Googlebook (internal disk, previous version)", linux, initrd, src["options"]).encode())
    sb.write("/loader/entries/gbos-nvme.conf", entry_text("Googlebook (internal disk)", linux, initrd, src["options"]).encode())
    for n, (e, s) in stick_entries(sb).items():
        if n not in ("gbos-nvme.conf", "gbos-nvme-prev.conf") and s != c:
            sb.delete("/loader/entries/" + n)      # e.g. the serial-debug entry of slot t (rewritten at the stick step)
    for f in sb.ls(f"/gbos-nvme/{t}"):
        sb.delete(f"/gbos-nvme/{t}/{f}")
    sb.sync_drop()
    left = stick_entries(sb)
    if (any(s != c for _, s in left.values()) or left.get("gbos-nvme.conf", (None, None))[1] != c
            or left.get("gbos-nvme-prev.conf", (None, None))[1] != c):
        raise Failed("the installer stick still points at the slot being written")


def stick_menu(sb, bf, t, c):
    """After the target slot is written and read back: the stick's 'Googlebook (internal disk)' entries start slot t
    from /gbos-nvme/<t>/; 'previous version' (written by stick_prepare) keeps starting slot c; files no entry uses any
    more are removed. Idempotent."""
    kp, ip = f"/gbos-nvme/{t}/kernel.efi", f"/gbos-nvme/{t}/initrd.img"
    sb.write(kp, bf["kernel"])
    sb.write(ip, bf["initrd"])
    for n, src, title in (("gbos-nvme.conf", "lapis-quiet.conf", "Googlebook (internal disk)"),
                          ("gbos-nvme-debug.conf", "lapis-debug.conf", "Googlebook (internal disk) serial-debug")):
        e = entry_parse(bf["entries"][src])
        sb.write("/loader/entries/" + n, entry_text(title, kp, [ip], e["options"]).encode())
    sb.sync_drop()
    ents = stick_entries(sb)
    if ents.get("gbos-nvme-prev.conf", (None, None))[1] != c or ents["gbos-nvme.conf"][1] != t:
        raise Failed("the installer stick's menu is not as written")
    used = {x for e, _ in ents.values() if e is not None for x in [e["linux"], *e["initrd"]]}
    for sub in ("", "/a", "/b"):
        for f in sb.ls("/gbos-nvme" + sub):
            x = f"/gbos-nvme{sub}/{f}"
            if f.endswith((".efi", ".img")) and x not in used:
                sb.delete(x)
    sb.sync_drop()


def open_stick(stick_boot, tmp):
    """The installer stick's boot partition, held O_EXCL (nothing can mount it while mtools writes it) and its FAT
    size field checked against the partition."""
    sb = Fat(stick_boot, tmp)
    dev, _, off = stick_boot.partition("@@")
    off = int(off or 0)
    if stat.S_ISBLK(os.stat(dev).st_mode):
        try:
            fd = os.open(dev, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC)
        except OSError as e:
            raise Refuse(f"the stick's boot partition {dev} is mounted or in use ({e.strerror})")
        HOLD.append(fd)
        sb.blkfd = fd
        length = os.lseek(fd, 0, os.SEEK_END) - off
        bs = os.pread(fd, 512, off)
    else:
        length = os.path.getsize(dev) - off
        with open(dev, "rb") as fh:
            fh.seek(off)
            bs = fh.read(512)
    fat_fits(bs, length, "the installer stick's boot partition")
    return sb


class SharedBlockOperation:
    """Own block-preview resources and bind one already-observed target.

    This association prevents accidental mixing of operations; trusted source,
    device confirmation and topology observations belong to its caller.
    """
    def __init__(self, disk, release, binding, tmp, device_identity, check_target):
        self._closed = False
        self._snapshots = []
        if (type(disk) is not Disk or type(release) is not SharedRelease or type(binding) is not SharedBinding
                or getattr(binding, '_closed', True) or getattr(binding, 'release', None) is not release
                or not disk.blk or not disk.write or not callable(check_target)
                or not isinstance(device_identity, str) or not re.fullmatch('[0-9a-f]{64}', device_identity)):
            raise Refuse('invalid shared block operation')
        st = os.fstat(disk.fd)
        if not stat.S_ISBLK(st.st_mode):
            raise Refuse('shared block target is not a block device')
        self.disk, self.release, self.binding, self.tmp = disk, release, binding, tmp
        self.device_identity, self.check_target = device_identity, check_target
        self._fd = disk.fd
        self._signature = (st.st_dev, st.st_ino, st.st_rdev, disk.size)
        try:
            self.snapshot = PreviewMetadataSnapshot(disk, tmp, sealed_esp=True)
            self._snapshots.append(self.snapshot)
            self.inst = Installed(disk, tmp, preview=True, snapshot=self.snapshot, preview_tmp=tmp)
        except BaseException:
            self.close()
            raise

    def verify(self, disk, release, binding):
        if (self._closed or disk is not self.disk or release is not self.release or binding is not self.binding
                or disk.fd != self._fd or getattr(binding, '_closed', True)
                or getattr(binding, 'release', None) is not release or not disk.blk or not disk.write):
            raise Refuse('shared block operation is closed or does not match')
        st = os.fstat(disk.fd)
        if (not stat.S_ISBLK(st.st_mode) or
                (st.st_dev, st.st_ino, st.st_rdev, disk.size) != self._signature or
                os.lseek(disk.fd, 0, os.SEEK_END) != disk.size):
            raise Refuse('shared block target changed')
        self.check_target()

    def before_write(self):
        self.verify(self.disk, self.release, self.binding)
        self.snapshot.verify_unchanged(self.disk)
        self.check_target()

    def writable_fat(self):
        self.verify(self.disk, self.release, self.binding)
        esp = self.inst.gpt[ESP]
        return BoundedMutableFat(self.disk.vol(esp['start']), self.tmp, self.disk.fd)

    def installed_after(self):
        self.verify(self.disk, self.release, self.binding)
        snapshot = PreviewMetadataSnapshot(self.disk, self.tmp, sealed_esp=True)
        self._snapshots.append(snapshot)
        installed = Installed(self.disk, self.tmp, preview=True, snapshot=snapshot, preview_tmp=self.tmp)
        self.verify(self.disk, self.release, self.binding)
        return installed

    def close(self):
        if self._closed:
            return
        self._closed = True
        while self._snapshots:
            self._snapshots.pop().close()


def apply(disk, rel, mode, without_fallback, stick_boot, tmp, shared_binding=None, *, pre_write_check=None,
          block_operation=None):
    if isinstance(rel, SharedRelease) and shared_binding is None:
        raise Refuse('shared release requires a live catalog binding')
    if block_operation is not None and shared_binding is None:
        raise Refuse('shared block operation requires a live catalog binding')
    if shared_binding is not None:
        if type(shared_binding) is not SharedBinding:
            raise Refuse('shared update requires a live catalog binding')
        st = os.fstat(disk.fd)
        if (not disk.write or mode not in ('stick', 'nvme') or without_fallback or stick_boot is not None):
            raise Refuse('shared update only supports bounded synthetic regular files with fallback')
        if disk.blk:
            if type(block_operation) is not SharedBlockOperation or not stat.S_ISBLK(st.st_mode):
                raise Refuse('shared update only supports bounded synthetic regular files with fallback without a block operation')
            block_operation.verify(disk, rel, shared_binding)
        elif (block_operation is not None or not stat.S_ISREG(st.st_mode) or disk.size > 128 * MIB):
            raise Refuse('shared update only supports bounded synthetic regular files with fallback')
        source = SharedBinding.verify(shared_binding, rel, disk)
        if block_operation is None:
            validate_preview_layout(disk, gpt_read(disk))
            preview_resource_limits(disk, gpt_read(disk))
    inst = (block_operation.inst if block_operation is not None else
            Installed(disk, tmp, preview=True) if shared_binding is not None else Installed(disk, tmp))
    if inst.shared_digest and shared_binding is None:
        raise Refuse('shared update requires matching shared binding')
    if shared_binding is not None:
        if inst.resume and not inst.shared_digest:
            raise Refuse('shared update cannot resume unmarked legacy state')
        binding_digest = shared_identity(disk, inst, source, mode, without_fallback,
                                         block_operation.device_identity if block_operation is not None else None)
        if inst.resume and inst.shared_digest != binding_digest:
            raise Refuse('shared update identity differs from saved state')
        if inst.resume and any(inst.slot_of.get(n) != inst.current for n in REQUIRED_ENTRIES):
            raise Refuse('shared update cannot safely resume this late menu phase')
    chk = check(inst, rel, mode)
    bf = boot_files(inst, rel, mode, tmp, preview=True, preview_tmp=tmp) if shared_binding is not None else \
        boot_files(inst, rel, mode, tmp)
    for l in plan_text(inst, rel, chk, bf):
        say("     " + l)
    sb = open_stick(stick_boot, tmp) if stick_boot else None
    if sb is not None:
        STICK.append(True)
    t, c = inst.target, inst.current
    if sb is not None:
        stick_src, on_stick = stick_source(sb, inst, c)
        copy = stick_copy(sb, inst, stick_src, on_stick, c)
        if not stick_needs(sb, bf, t, copy):
            raise Refuse("the installer stick's boot partition has no room for the internal disk's boot files")
    if not bf["fits"] and bf["prev"] and not without_fallback and sb is None:
        raise Refuse("the ESP has no room to keep the previous version; run again with --without-fallback "
                     "(if the update then stops halfway, run it again to finish) or update from the installer stick")
    if shared_binding is not None and (not bf['fits'] or not bf['prev']):
        raise Refuse('shared update requires previous boot fallback')
    disk.guard(guard_ranges(inst))
    orig = {q["name"]: list(q["extents"]) for q in inst.lp.parts}
    # Recheck an observed target after expensive source/boot preflight. This is
    # a discrete observation; the held fd, not this check, prevents redirection.
    if pre_write_check is not None:
        pre_write_check()
    if block_operation is not None:
        block_operation.before_write()
        inst.fat = block_operation.writable_fat()
    # ---- from here on the disk changes. First the state file: a rerun finishes this slot, never the running one.
    WRITING.append(True)
    if inst.resume and inst.slot_of.get(inst.default) is None:
        # a rerun after a stop inside the window: the old boot files are gone already, so the disk's own menu cannot
        # start any version until this run has switched it
        WINDOW.append(sb is not None)
    if shared_binding is None:
        inst.fat.write(STATE, inst.state_text(rel.ident).encode())
    elif not inst.resume:
        state = inst.state_text(rel.ident) + f'shared-version 1\nshared-digest {binding_digest}\n'
        inst.fat.write(STATE, state.encode())
        disk.sync_drop()
        if inst.fat.read(STATE) != state.encode():
            raise Failed('shared update state readback failed')
        step('sharedstate: synced and verified shared update state')
    if sb is not None:
        stick_prepare(sb, t, c, stick_src, copy)
        step(f"stickprep: the installer stick starts slot {c} only, from its own copy")
    for n, s in list(inst.slot_of.items()):
        if s != c:                                   # the old previous version (slot t) or broken entries
            inst.fat.delete("/loader/entries/" + n)
    for x in bf["drop"]:
        inst.fat.delete(x)
    if any(inst.slot_of.get(n) == t for n in inst.fat.ls("/loader/entries")):
        raise Failed("an entry of the disk's menu still starts the slot being written")
    step(f"removed: the boot entries and files of slot {t}")
    lp = inst.lp
    for b, p in rel.dyn.items():
        name = f"{b}_{t}"
        ext = chk["extents"][name]
        ranges = [(inst.super["start"] + a * SECTOR, n * SECTOR) for a, n in ext]
        src = [(a * SECTOR, n * SECTOR) for a, n in p["extents"]]

        def chunks(src=src):
            for off, ln in src:
                for o in range(0, ln, 16 << 20):
                    yield rel.super_rd(off + o, min(16 << 20, ln - o))
        want = write_stream(disk, ranges, chunks())
        disk.sync_drop()
        if readback(disk, ranges, chk["sizes"][name]) != want:
            raise Failed(f"{name} reads back different")
        q = lp.part(name)
        if q is None:
            lp.parts.append(dict(name=name, attrs=p["attrs"], group=chk["group"], extents=ext))
        else:
            q.update(attrs=p["attrs"], group=chk["group"], extents=ext)
    for q in lp.parts:                                   # target-slot partitions the release no longer has
        if q["name"].endswith("_" + t) and q["name"][:-2] not in rel.dyn:
            q["extents"] = []
    step(f"dynamic: partitions of slot {t} written and read back")
    for ph in SLOT_PHYS:
        d = inst.gpt[f"{ph}_{t}"]
        if rel.layout[f"{ph}_a"]["len"] != d["len"]:
            raise Failed(f"{ph}_{t} is not the size of the release's {ph}_a")
        want = write_stream(disk, [(d["start"], d["len"])], rel.read_phys(f"{ph}_a"))
        disk.sync_drop()
        if disk.sha(d["start"], d["len"]) != want:
            raise Failed(f"{ph}_{t} reads back different")
    step(f"physical: slot partitions of slot {t} written and read back")
    blob = lp.serialize()
    LP.parse(blob + bytes(lp.mx - len(blob)))
    disk.guard([g for g in disk.forbidden if not (inst.super["start"] <= g[0] < inst.super["start"] + inst.super["len"] and g[2] != "super")]
               + [(inst.super["start"] + lp.first * SECTOR, inst.super["start"] + inst.super["len"], "super data")])
    for off in lp.slot_offsets():
        disk.pwrite(inst.super["start"] + off, blob)
        disk.sync_drop()
        if disk.pread(inst.super["start"] + off, len(blob)) != blob:
            raise Failed("LP metadata reads back different")
    new = LP.read(lambda o, n: disk.pread(inst.super["start"] + o, n), inst.super["len"])
    if not new.consistent:
        raise Failed("LP metadata copies differ after the write")
    for q in new.parts:
        if not q["name"].endswith("_" + t) and q["extents"] != orig.get(q["name"]):
            raise Failed(f"LP metadata changed {q['name']} of the running slot")
    step("metadata: super metadata written (all copies) and read back")
    new_refs = {x for txt in bf["entries"].values() for en in [entry_parse(txt)] for x in [en["linux"], *en["initrd"]]}
    old_files = [x for x in bf["keep"] if x not in bf["files"] and x not in new_refs]   # e.g. a shared kernel stays
    if not bf["fits"]:
        for n, s in list(inst.slot_of.items()):
            if s == c and n.startswith(PREV):
                inst.fat.delete("/loader/entries/" + n)
        if not bf["new_first"]:
            # the ESP cannot hold the old and the new boot files together: from here until the menu is switched, the
            # disk's own menu cannot start either version (a rerun finishes it; the installer stick still starts one)
            WINDOW.append(sb is not None)
            for x in old_files:
                inst.fat.delete(x)
            say("     the ESP holds one version only: the old boot files are removed before the new ones are written")
            step(f"oldremoved: the old boot files of slot {c} removed")
    for x, data in bf["files"].items():
        inst.fat.write(x, data)
    step(f"bootfiles: boot files of slot {t} written and read back")
    if sb is not None:
        stick_menu(sb, bf, t, c)
        step("stick: the installer stick's menu for the internal disk")
    if bf["fits"]:
        for n, text in bf["prev"].items():
            inst.fat.write("/loader/entries/" + n, text.encode())
    for n in bf["stale"]:
        inst.fat.delete("/loader/entries/" + n)
    for n, text in bf["entries"].items():
        inst.fat.write("/loader/entries/" + n, text.encode())
        if shared_binding is not None:
            step('sharedmenu-' + n + ': wrote new boot entry')
    inst.fat.write("/loader/loader.conf", bf["loader"].encode())
    if shared_binding is not None:
        step('sharedmenu-loader: wrote boot default')
    WINDOW.clear()                                   # the menu starts the new slot now
    if not bf["fits"] and bf["new_first"]:
        for x in old_files:                          # nothing on the menu uses them any more
            inst.fat.delete(x)
        say("     the ESP holds one version only: the old boot files were removed after the switch")
    inst.fat.write(f"/gbos/{t}/release.txt", f"google build {rel.build}\n{rel.fingerprint}\n{rel.ident}\n".encode())
    if block_operation is not None:
        block_operation.verify(disk, rel, shared_binding)
    inst.fat.delete(STATE)
    disk.sync_drop()                                 # the ESP was written through mtools: flush, read the result from the disk
    step(f"switched: the boot menu starts slot {t}")
    done = block_operation.installed_after() if block_operation is not None else Installed(disk, tmp)
    if done.current != t or done.resume:
        raise Failed("the boot menu does not start the new slot")
    if bf["fits"] and bf["prev"] and not any(n.startswith(PREV) and done.slot_of.get(n) == c for n in done.entries):
        raise Failed("the previous version is not on the boot menu")
    if any(done.slot_of.get(n) is None for n in done.entries):
        raise Failed("an entry of the boot menu starts files that are not there")
    say(f"UPDATE OK: slot {t} holds google build {rel.build} and starts by default"
        + (f"; '{rel.entries['lapis-quiet.conf']['title']}{PREV_TITLE}' starts slot {c}" if bf["fits"] and bf["prev"] else ""))


def slot_build(fat, e):
    rd, _ = split_initrd(fat.read(e["initrd"][-1]))
    return build_id(ramdisk_prop(rd, "ro.bootimage.build.fingerprint"))


def rollback(disk, tmp, stick_boot=None, allow_build_change=False, *, pre_write_check=None):
    inst = Installed(disk, tmp)
    if inst.resume:
        raise Refuse("an update stopped halfway; finish it first (run the update again)")
    prev = {n[len(PREV):]: n for n in inst.entries if n.startswith(PREV)}
    if "lapis-quiet.conf" not in prev or inst.slot_of.get(prev["lapis-quiet.conf"]) != inst.target:
        raise Refuse("there is no previous version on this disk's boot menu")
    pb = slot_build(inst.fat, inst.entries[prev["lapis-quiet.conf"]])
    if pb != inst.build and not allow_build_change:
        raise Refuse(f"the previous version has Google build {pb}, the running one {inst.build}: once the newer build has "
                     "started, Android has upgraded /data and the older one may not start with it. Run again with "
                     "--allow-build-change to switch anyway")
    sb = open_stick(stick_boot, tmp) if stick_boot else None
    sents = stick_entries(sb) if sb is not None else {}
    if sb is not None and (sents.get("gbos-nvme.conf", (None, None))[1] != inst.current
                           or sents.get("gbos-nvme-prev.conf", (None, None))[1] != inst.target):
        raise Refuse("the installer stick's internal-disk entries do not match this disk's menu")
    for sn, dn in (("gbos-nvme.conf", inst.default), ("gbos-nvme-prev.conf", prev["lapis-quiet.conf"])) if sb is not None else ():
        se, de = sents[sn][0], inst.entries[dn]
        if [sb.read(x) for x in [se["linux"], *se["initrd"]]] != [inst.fat.read(x) for x in [de["linux"], *de["initrd"]]]:
            raise Refuse(f"the installer stick's {sn} starts other boot files than this disk's {dn} (the disk was updated "
                         "from another stick since); roll back without --stick-boot")
    swap = {}
    for n, pn in prev.items():
        e, pe = inst.entries.get(n), inst.entries[pn]
        swap[n] = entry_text(pe["title"][:-len(PREV_TITLE)] if pe["title"].endswith(PREV_TITLE) else pe["title"],
                             pe["linux"], pe["initrd"], pe["options"])
        if e is not None:
            swap[pn] = entry_text(e["title"] + PREV_TITLE, e["linux"], e["initrd"], e["options"])
    if pre_write_check is not None:
        pre_write_check()
    WRITING.append(True)
    if sb is not None:
        # the stick: 'internal disk' <-> 'previous version'; its serial-debug entry follows the new running slot with
        # the options of this disk's (previous-version) serial-debug entry
        cur, pv = sents["gbos-nvme.conf"][0], sents["gbos-nvme-prev.conf"][0]
        sb.write("/loader/entries/gbos-nvme.conf", entry_text("Googlebook (internal disk)", pv["linux"], pv["initrd"], pv["options"]).encode())
        sb.write("/loader/entries/gbos-nvme-prev.conf", entry_text("Googlebook (internal disk, previous version)",
                                                                   cur["linux"], cur["initrd"], cur["options"]).encode())
        pd = inst.entries.get(prev.get("lapis-debug.conf", ""))
        if pd is not None:
            sb.write("/loader/entries/gbos-nvme-debug.conf", entry_text("Googlebook (internal disk) serial-debug",
                                                                        pv["linux"], pv["initrd"], pd["options"]).encode())
        else:
            sb.delete("/loader/entries/gbos-nvme-debug.conf")
    for n, text in sorted(swap.items(), key=lambda kv: not kv[0].startswith(PREV)):
        inst.fat.write("/loader/entries/" + n, text.encode())
    disk.sync_drop()
    done = Installed(disk, tmp)
    if done.current != inst.target:
        raise Failed("rollback did not switch the boot menu")
    if sb is not None:
        sb.sync_drop()
        after = stick_entries(sb)
        if after.get("gbos-nvme.conf", (None, None))[1] != inst.target or after.get("gbos-nvme-prev.conf", (None, None))[1] != inst.current:
            raise Failed("the installer stick's menu did not follow the rollback")
    say(f"ROLLBACK OK: slot {done.current} starts by default; slot {inst.current} is the previous version")


# ------------------------------------------------------------------ command line

def main(argv):
    import argparse
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("cmd", choices=("inspect", "plan", "apply", "rollback"))
    ap.add_argument("disk")
    ap.add_argument("--image")
    ap.add_argument("--no-image-sha", action="store_true", help=argparse.SUPPRESS)  # tests only
    ap.add_argument("--pristine")
    ap.add_argument("--super")
    ap.add_argument("--mode", choices=("stick", "nvme"))
    ap.add_argument("--rdev")
    ap.add_argument("--syspath")
    ap.add_argument("--bytes")
    ap.add_argument("--without-fallback", action="store_true")
    ap.add_argument("--stick-boot")
    ap.add_argument("--allow-build-change", action="store_true")
    a = ap.parse_args(argv)
    tmp = tempfile.mkdtemp(prefix="gbos-update-")
    try:
        if a.cmd == "inspect":
            for l in Installed(Disk(a.disk), tmp).facts():
                say(l)
            return 0
        if a.cmd == "rollback":
            disk = Disk(a.disk, write=True, rdev=a.rdev, syspath=a.syspath, nbytes=a.bytes)
            rollback(disk, tmp, a.stick_boot, a.allow_build_change)
            return 0
        if bool(a.image) == bool(a.pristine) or (a.pristine and not a.super):
            raise Refuse("give --image <release.img>, or --pristine <dir> with --super <device>")
        if a.cmd == "apply" and not a.mode:
            raise Refuse("apply needs --mode stick|nvme")
        if a.no_image_sha and not os.environ.get("GBOS_UPDATE_TEST"):
            raise Refuse("--no-image-sha is for tests")
        rel = image_release(a.image, tmp, not a.no_image_sha) if a.image else pristine_release(a.pristine, a.super, tmp)
        if a.cmd == "plan":
            inst = Installed(Disk(a.disk), tmp)
            chk = check(inst, rel, a.mode or "stick")
            for l in inst.facts() + plan_text(inst, rel, chk, boot_files(inst, rel, a.mode or "stick", tmp)):
                say(l)
            say("PLAN OK (nothing written)")
            return 0
        disk = Disk(a.disk, write=True, rdev=a.rdev, syspath=a.syspath, nbytes=a.bytes)
        inst = Installed(disk, tmp)
        check(inst, rel, a.mode)                 # refuse here, before the first write
        apply(disk, rel, a.mode, a.without_fallback, a.stick_boot, tmp)
        return 0
    except Refuse as e:
        say(f"REFUSED: {e}" + ("" if WRITING else " (nothing was written)"))
        return 1 if WRITING else 2
    except (Failed, RuntimeError, OSError) as e:
        say(f"FAILED: {e}" + ("" if WRITING else " (nothing was written)"))
        if WRITING and WINDOW:
            say("The update stopped after the old boot files were removed and before the boot menu was switched: this"
                " disk's own boot menu cannot start Googlebook now. Run the same update again to finish it"
                + (" (meanwhile the installer stick's 'Googlebook (internal disk)' starts a complete version)." if WINDOW[0]
                   else "."))
            return 1
        if WRITING:
            say("The update stopped halfway. This disk's boot menu still starts the version that ran before by default"
                + (" (and the installer stick's 'Googlebook (internal disk)' starts a complete version)" if STICK else "")
                + "; run the same update again to finish it.")
            return 1
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
