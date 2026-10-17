#!/usr/bin/env python3
"""Refuse a GBOS_TEST_AUTOOFF vendor inside the disk image a harness script copies.

kano-nvme-install.sh copies the first BYTES of the stick. kano-nvme-update.sh
scans the local new image before it pushes super. Both call this first:

  1. Exactly one GPT partition whose name is "super" the way the pinned kernel
     names it (utf16_le_to_7bit: the low 7 bits of each UTF-16LE unit, stopped
     at a NUL). The kernel also replaces a non-printable unit with '!'; this
     checker keeps the low 7 bits. ASCII names such as "super" match either way.
     The partition must lie fully inside the copied range. Primary
     header CRC, entry CRC, my_lba, and a 128-byte entry must match; if they
     do not, the backup GPT is used. Both copies invalid, or more than one
     super, is a refusal.
  2. Android LP geometry: primary at super+4096, backup at super+8192. Use the
     primary when ParseGeometry would accept it, otherwise the backup. Neither
     valid is a refusal, not a clean vendor.
  3. Every metadata slot the geometry declares, primary then backup, the way
     liblp ReadMetadata reads one slot, then AdjustMetadataForSlot. The
     installer's initrd keeps whatever androidboot.slot_suffix the image
     already has, so slot 0 alone is not the slot that will boot. A copy liblp
     would reject is not a clean vendor: the backup of that same slot is read.
     Both copies invalid is a refusal. A checker limit (more than 4096 table
     entries, or tables larger than 1 MiB) is not a bad copy: the image is
     refused and the backup is not selected. After a copy is chosen,
     LP_PARTITION_ATTR_SLOT_SUFFIXED names gain _a on slot 0 and _b on slot 1
     (utility.cpp SlotSuffixForSlotNumber). A name that does not fit in 36
     bytes, or that flag on slot 2 or later, refuses the image and does not
     select the other copy. A name with an interior NUL and a non-NUL last byte
     refuses the image the same way: device-mapper would stop at the first NUL.
     vendor_a or vendor_b is then what gets scanned.
     Extent count 0 and LP_PARTITION_ATTR_DISABLED are not mounted
     (CreateLogicalPartitions) and are not read. vendor_b with extents and an
     empty vendor_a is the unused-slot shape read from the other name.
  4. Walk that vendor as EROFS the way the pinned lapis kernel does
     (kernel/common 3938c8d97142 fs/erofs/erofs_fs.h). Directories are inode
     modes. 48-bit root nids and metabox nids are followed. Anything the
     walker does not understand is a refusal. etc/kano-autooff.nonce and
     etc/init/kano-autooff.rc are opened through directory symlinks, with a
     follow limit. A loop, a symlink that leaves the vendor, or an inode that
     cannot be read is a refusal.
  5. Either of those paths resolving to a regular file is a TEST image.

--file reads a regular image. --dd reads a block device through dd so the
harness tests' stubbed dd is what touches /dev/sda; this process does not
open the device itself. --bytes is the copied length (required with --dd).
"""
import argparse
import hashlib
import os
import re
import stat
import struct
import subprocess
import sys
import zlib

FORBIDDEN = (
    "etc/kano-autooff.nonce",
    "etc/init/kano-autooff.rc",
)
EROFS_MAGIC = 0xE0F5E1E2
# kernel/common 3938c8d97142 fs/erofs/erofs_fs.h (builder/kmod/build-ac.sh KSRC_COMMIT).
# EROFS_ALL_FEATURE_INCOMPAT is every incompat bit that kernel understands.
# 48BIT is 0x80 (not 0x400). METABOX is 0x100. A bit above that mask is an
# unidentified incompat feature: the kernel refuses to mount, and so do we.
EROFS_FEATURE_INCOMPAT_48BIT = 0x80
EROFS_FEATURE_INCOMPAT_METABOX = 0x100
EROFS_ALL_FEATURE_INCOMPAT = (EROFS_FEATURE_INCOMPAT_METABOX << 1) - 1
EROFS_DIRENT_NID_METABOX_BIT = 63
EROFS_DIRENT_NID_MASK = (1 << EROFS_DIRENT_NID_METABOX_BIT) - 1
EROFS_I_ALL = (1 << 5) - 1
EROFS_I_NLINK_1_BIT = 4
EROFS_NULL_ADDR = (1 << 64) - 1
GEO_MAGIC = 0x616C4467
HDR_MAGIC = 0x414C5030
# AOSP system/core fs_mgr/liblp include/liblp/metadata_format.h and reader.cpp
# (android16-release, the v1.0/v1.2 rules: major 10, minor at most 2).
LP_MAJOR = 10
LP_MINOR_MAX = 2
LP_HEADER_V10 = 128
LP_HEADER_V12 = 256
# metadata_format.h (android16-release). V0 is readonly | slot-suffixed.
# V1 (minor >= 1) adds updated | disabled. UPDATED is not a refusal: a Virtual
# A/B snapshot COW is outside this checker, which reads the extent in the table.
LP_PARTITION_ATTR_READONLY = 1 << 0
LP_PARTITION_ATTR_SLOT_SUFFIXED = 1 << 1
LP_PARTITION_ATTR_UPDATED = 1 << 2
LP_PARTITION_ATTR_DISABLED = 1 << 3
LP_ATTR_V0 = LP_PARTITION_ATTR_READONLY | LP_PARTITION_ATTR_SLOT_SUFFIXED
LP_ATTR_V1 = LP_PARTITION_ATTR_UPDATED | LP_PARTITION_ATTR_DISABLED
LP_BLOCK_DEVICE_SLOT_SUFFIXED = 1 << 0
LP_GROUP_SLOT_SUFFIXED = 1 << 0
LP_NAME_LEN = 36
# Not liblp rules. liblp's ValidateTableBounds is offset and num*entry_size
# against tables_size, with no entry-count cap. Exceeding either limit refuses
# the image; it must not be treated as a damaged copy and select the backup.
LP_ENTRY_CAP = 4096
LP_TABLES_CAP = 1 << 20
LP_RESERVED = 4096
LP_GEOMETRY_SIZE = 4096
LP_PRIMARY_GEOMETRY = LP_RESERVED
LP_BACKUP_GEOMETRY = LP_RESERVED + LP_GEOMETRY_SIZE
LP_METADATA_BASE = LP_BACKUP_GEOMETRY + LP_GEOMETRY_SIZE
# Same geometry the lapis super uses (research/base-analysis/lp.real.json).
LP_METADATA_MAX = 65536
LP_SLOTS = 3
# vendor.img on the reference build is about 432 MiB. Above this, refuse
# rather than mapping an unbounded extent.
MAX_VENDOR = 1 << 30
MAX_SYMLINK_FOLLOWS = 40


class Refuse(Exception):
    pass


class CopyInvalid(Refuse):
    """liblp would not use this copy. The other copy of the same slot may be read."""


class HardRefuse(Refuse):
    """The chosen copy, or a checker limit, failed. Do not select another copy."""


def _cannot(detail):
    raise Refuse("cannot list vendor in the image being copied: " + detail)


def _bad_copy(detail):
    raise CopyInvalid("cannot list vendor in the image being copied: " + detail)


def _limit(detail):
    raise HardRefuse("cannot list vendor in the image being copied: " + detail)


class FileReader:
    def __init__(self, path, limit):
        if os.path.islink(path):
            raise Refuse("image path is a symlink")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise Refuse("cannot open the image being copied") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise Refuse("image path is not a regular file")
            self._fd = fd
            self.size = info.st_size
        except Exception:
            os.close(fd)
            raise
        if limit is not None:
            self.size = min(self.size, limit)
        self._owns = True

    def close(self):
        if self._owns:
            os.close(self._fd)
            self._owns = False

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise Refuse("read outside the copied image")
        buf = bytearray()
        left = length
        pos = offset
        while left:
            chunk = os.pread(self._fd, min(left, 1 << 20), pos)
            if not chunk:
                raise Refuse("short read of the image being copied")
            buf += chunk
            pos += len(chunk)
            left -= len(chunk)
        return bytes(buf)


class DdReader:
    """Read a block device only through dd. The harness fake maps if=/dev/sda."""

    def __init__(self, dev, limit):
        if not re.fullmatch(r"/dev/[A-Za-z0-9]+", dev or ""):
            raise Refuse("odd device path")
        if limit is None or limit <= 0 or limit > (1 << 44):
            raise Refuse("byte limit")
        proc = subprocess.run(
            ["blockdev", "--getsize64", dev],
            check=False, capture_output=True,
        )
        if proc.returncode != 0:
            raise Refuse("cannot read the size of the image being copied")
        try:
            dev_size = int(proc.stdout.decode().strip())
        except ValueError as exc:
            raise Refuse("cannot read the size of the image being copied") from exc
        self.dev = dev
        self.size = min(dev_size, limit)

    def close(self):
        return None

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise Refuse("read outside the copied image")
        proc = subprocess.run(
            [
                "dd", "if=" + self.dev, "bs=1048576",
                "skip=" + str(offset), "count=" + str(length),
                "iflag=skip_bytes,count_bytes", "status=none",
            ],
            check=False, capture_output=True,
        )
        if proc.returncode != 0 or len(proc.stdout) != length:
            raise Refuse("short read of the image being copied")
        return proc.stdout


def _gpt_name_7bit(field):
    """Kernel block/partitions/efi.c utf16_le_to_7bit (3938c8d97142).

    Each UTF-16LE unit contributes its low 7 bits. A 0 stops the C string.
    The kernel also stores '!' for a non-printable unit. This checker keeps the
    low 7 bits. ASCII names such as "super" match either way.
    ueventd by-name uses that string, so "which partition is super" follows it.
    """
    chars = []
    for i in range(len(field) // 2):
        unit = field[i * 2] | (field[i * 2 + 1] << 8)
        char = unit & 0x7F
        if char == 0:
            break
        chars.append(chr(char))
    return "".join(chars)


def _gpt_at(reader, lba):
    """(entries, count, entry_size) when this GPT header matches, else None.

    Header CRC is computed with the CRC field zeroed, the same way UEFI and
    gbos-update.py check it. my_lba must be the LBA being read, and
    sizeof_partition_entry must be 128 (gpt_entry), the way efi.c is_gpt_valid
    checks them. A failure here is not a clean disk: the caller tries the backup header.
    """
    try:
        sector = reader.read(lba * 512, 512)
    except Refuse:
        return None
    if sector[:8] != b"EFI PART":
        return None
    hsize = struct.unpack_from("<I", sector, 12)[0]
    if not 92 <= hsize <= 512:
        return None
    header = bytearray(sector[:hsize])
    stored = struct.unpack_from("<I", header, 16)[0]
    header[16:20] = b"\x00" * 4
    if (zlib.crc32(header) & 0xFFFFFFFF) != stored:
        return None
    my_lba = struct.unpack_from("<Q", sector, 24)[0]
    if my_lba != lba:
        return None
    ents, nent, esz, ecrc = struct.unpack_from("<QIII", sector, 72)
    if not (1 <= nent <= 256) or esz != 128:
        return None
    try:
        data = reader.read(ents * 512, nent * esz)
    except Refuse:
        return None
    if (zlib.crc32(data) & 0xFFFFFFFF) != ecrc:
        return None
    return data, nent, esz


def find_supers(reader):
    """The one GPT partition named super, fully inside the copied range.

    Primary header at LBA 1 when its header and entry CRCs, my_lba, and entry
    size match, otherwise the backup header named by the primary's alternate-LBA
    field when that LBA is inside the copied image. Both copies invalid is a
    refusal. More than one 7-bit name "super" is a refusal: the kernel's
    by-name link is then ambiguous, and this checker will not pick one.
    """
    parsed = _gpt_at(reader, 1)
    if parsed is None:
        alt = None
        try:
            hdr = reader.read(512, 40)
        except Refuse:
            hdr = b""
        if len(hdr) >= 40 and hdr[:8] == b"EFI PART":
            alt = struct.unpack_from("<Q", hdr, 32)[0]
        if alt is None or alt in (0, 1) or alt > (1 << 40):
            raise Refuse("no GPT in the image being copied")
        parsed = _gpt_at(reader, alt)
    if parsed is None:
        raise Refuse("no GPT in the image being copied")
    data, nent, esz = parsed
    found = []
    for i in range(nent):
        entry = data[i * esz:(i + 1) * esz]
        if entry[:16] == b"\x00" * 16:
            continue
        name = _gpt_name_7bit(entry[56:128])
        if name != "super":
            continue
        first, last = struct.unpack_from("<QQ", entry, 32)
        if last < first:
            raise Refuse("super partition is not usable")
        start, end = first * 512, (last + 1) * 512
        if start < 0 or end > reader.size:
            raise Refuse("super partition is not fully inside the copied image")
        found.append((start, end))
    if not found:
        raise Refuse("no super in the copied image")
    if len(found) != 1:
        raise Refuse("more than one GPT partition is named super")
    return found


def _geometry(raw):
    """(metadata_max_size, slot_count, logical_block_size) or None when liblp ParseGeometry would reject this copy.

    sizeof(LpMetadataGeometry) is 52. struct_size must be exactly that. The SHA-256 covers those
    52 bytes with the checksum field zeroed. metadata_max_size must be a multiple of 512.
    logical_block_size is not a validity gate: ParseGeometry does not read it.
    """
    if len(raw) < 52:
        return None
    magic, ssize = struct.unpack_from("<II", raw, 0)
    if magic != GEO_MAGIC or ssize != 52:
        return None
    body = bytearray(raw[:52])
    csum = bytes(body[8:40])
    body[8:40] = b"\x00" * 32
    if hashlib.sha256(body).digest() != csum:
        return None
    mx, slots, lbs = struct.unpack_from("<III", raw, 40)
    if slots == 0 or mx % 512 != 0:
        return None
    return mx, slots, lbs


def _metadata_offset(mx, slots, slot, backup):
    primary = LP_METADATA_BASE + slot * mx
    if not backup:
        return primary
    return primary + slots * mx


def _fixed_name(raw):
    """reader.cpp NameFromFixedArray: a trailing NUL means C-string, otherwise all `len` bytes.

    An interior NUL whose last byte is not NUL is not that C string: device-mapper
    stops at the first NUL. Refuse the image instead of comparing a different name.
    """
    if raw and b"\0" in raw[:-1] and raw[-1] != 0:
        _limit("LP name has an interior NUL")
    if raw and raw[-1] == 0:
        return raw.split(b"\0", 1)[0]
    return raw


def _decode_name(raw):
    return _fixed_name(raw).decode("utf-8", "replace")


def _parse_metadata(raw, geometry=None):
    """(name -> extents, first_sector, device_bytes).

    Same gates as liblp ReadMetadataHeader + ParseMetadata: magic, major 10, minor <= 2,
    header_size 128 for minor 0/1 and 256 for minor 2, header and table SHA-256, table bounds,
    partition attribute mask, and metadata not overlapping first_logical_sector.
    Those failures are CopyInvalid: the backup of this slot may be read.
    `geometry` is (metadata_max_size, slot_count, logical_block_size) from the geometry copy
    already chosen. Without it, the overlap check is skipped (the direct fixture parse).

    A table liblp would accept that has more than LP_ENTRY_CAP entries, or tables larger
    than LP_TABLES_CAP, raises HardRefuse. That is not a damaged copy.

    parts['vendor_a'] is the first literal vendor_a extent list, which is what the lapis
    fixture test reads. Slot suffixes are applied later, by _adjust_metadata_for_slot.
    Every partition is in parts['\\x00records'] as (name_bytes, attrs, extents).
    """
    if len(raw) < LP_HEADER_V10:
        _bad_copy("LP metadata is short")
    magic, major, minor, hsz = struct.unpack_from("<IHHI", raw, 0)
    if magic != HDR_MAGIC:
        _bad_copy("no LP header")
    if major != LP_MAJOR or minor > LP_MINOR_MAX:
        _bad_copy("LP metadata version")
    expected = LP_HEADER_V12 if minor >= 2 else LP_HEADER_V10
    if hsz != expected or hsz > len(raw):
        _bad_copy("LP header size")
    header = bytearray(raw[:hsz])
    csum = bytes(header[12:44])
    header[12:44] = b"\x00" * 32
    if hashlib.sha256(header).digest() != csum:
        _bad_copy("LP header checksum")
    tsize = struct.unpack_from("<I", raw, 44)[0]
    if tsize < 1 or hsz + tsize > len(raw):
        _bad_copy("LP tables size")
    if geometry is not None and tsize > geometry[0]:
        _bad_copy("LP tables size")
    tables = raw[hsz:hsz + tsize]
    if hashlib.sha256(tables).digest() != bytes(raw[48:80]):
        _bad_copy("LP tables checksum")
    desc = [struct.unpack_from("<III", raw, off) for off in (80, 92, 104, 116)]
    pt, ex, gr, bd = desc
    # Partition, extent, and group entry sizes are fixed. Block-device entries are 64 bytes of
    # fields; liblp memcpy's those 64 and advances by entry_size, so a larger stride is still
    # this copy (rejecting it would skip a TEST vendor and read the backup).
    if pt[2] != 52 or ex[2] != 24 or gr[2] != 48 or bd[2] < 64:
        _bad_copy("unknown LP table entry sizes")
    over_cap = False
    for off, num, esz in desc:
        if off > tsize or num * esz > tsize - off:
            _bad_copy("LP table outside the tables")
        if num > LP_ENTRY_CAP:
            over_cap = True
    if bd[1] < 1:
        _bad_copy("expected one LP block device")
    bdev = tables[bd[0]:bd[0] + 64]
    first, _align, _aoff, bsize = struct.unpack_from("<QIIQ", bdev, 0)
    if geometry is not None:
        mx, slots, _lbs = geometry
        metadata_region = LP_RESERVED + (LP_GEOMETRY_SIZE + mx * slots) * 2
        if metadata_region > first * 512:
            _bad_copy("LP metadata overlaps logical partitions")
    extents = []
    for i in range(ex[1]):
        nsec, target_type, target, source = struct.unpack_from("<QIQI", tables, ex[0] + i * 24)
        if target_type == 0 and source >= bd[1]:
            _bad_copy("LP extent device")
        extents.append((nsec, target_type, target, source))
    valid_attr = LP_ATTR_V0 | (LP_ATTR_V1 if minor >= 1 else 0)
    records = []
    parts = {}
    for i in range(pt[1]):
        off = pt[0] + i * 52
        name_b = _fixed_name(tables[off:off + LP_NAME_LEN])
        name = name_b.decode("utf-8", "replace")
        attrs, fe, ne, gi = struct.unpack_from("<IIII", tables, off + 36)
        if attrs & ~valid_attr:
            _bad_copy((name or "partition") + " attributes")
        if fe + ne > 0xFFFFFFFF:
            _bad_copy((name or "partition") + " extents")
        if gi >= gr[1] or fe > len(extents) or ne > len(extents) or fe + ne > len(extents):
            _bad_copy((name or "partition") + " extents")
        extlist = [extents[j] for j in range(fe, fe + ne)]
        records.append((name_b, attrs, extlist))
        if name and name not in parts:
            parts[name] = extlist
    groups = []
    for i in range(gr[1]):
        off = gr[0] + i * 48
        groups.append((_fixed_name(tables[off:off + LP_NAME_LEN]), struct.unpack_from("<I", tables, off + 36)[0]))
    devices = []
    for i in range(bd[1]):
        off = bd[0] + i * bd[2]
        raw_dev = tables[off:off + 64]
        devices.append((_fixed_name(raw_dev[24:60]), struct.unpack_from("<I", raw_dev, 60)[0]))
    parts["\x00records"] = records
    parts["\x00groups"] = groups
    parts["\x00block_devices"] = devices
    # liblp accepted this copy. A checker cap must not select the backup.
    if tsize > LP_TABLES_CAP:
        _limit("LP tables exceed the checker limit")
    if over_cap:
        _limit("LP table has more entries than this checker reads")
    return parts, first, bsize


def _suffix_name(raw_name, suffix):
    """Bytes of raw_name + suffix, or None when they do not fit in 36 bytes.

    AdjustMetadataForSlot compares the C++ string size with sizeof(name), which is 36.
    A longer name makes ReadMetadata return null. The other copy is not tried: the
    suffix is applied only after a copy has been chosen.
    """
    suffixed = _fixed_name(raw_name) + suffix
    if len(suffixed) > LP_NAME_LEN:
        return None
    return suffixed


def _adjust_metadata_for_slot(parts, slot):
    """Apply reader.cpp AdjustMetadataForSlot for this metadata slot.

    utility.cpp SlotSuffixForSlotNumber is _a for 0 and _b for 1, and CHECK-fails
    otherwise. A slot >= 2 is a metadata slot, not a boot slot. Lapis stores the
    same unsuffixed table in slot 2, so a slot >= 2 with no suffixed name is left
    as written. A suffixed name on slot >= 2, or a suffixed name that does not fit,
    refuses this image instead of selecting the backup.
    """
    records = list(parts.get("\x00records") or [])
    groups = list(parts.get("\x00groups") or [])
    devices = list(parts.get("\x00block_devices") or [])
    needs = (
        any(attrs & LP_PARTITION_ATTR_SLOT_SUFFIXED for _name, attrs, _ext in records)
        or any(flags & LP_GROUP_SLOT_SUFFIXED for _name, flags in groups)
        or any(flags & LP_BLOCK_DEVICE_SLOT_SUFFIXED for _name, flags in devices)
    )
    if slot == 0:
        suffix = b"_a"
    elif slot == 1:
        suffix = b"_b"
    elif needs:
        _limit("LP slot %d has slot-suffixed names" % slot)
        suffix = None
    else:
        suffix = None
    if suffix is not None:
        renamed = []
        for name, attrs, extents in records:
            if attrs & LP_PARTITION_ATTR_SLOT_SUFFIXED:
                new_name = _suffix_name(name, suffix)
                if new_name is None:
                    _limit("LP partition name too long")
                name = new_name
                attrs &= ~LP_PARTITION_ATTR_SLOT_SUFFIXED
            renamed.append((name, attrs, extents))
        records = renamed
        for label, rows, bit in (
            ("group", groups, LP_GROUP_SLOT_SUFFIXED),
            ("block device", devices, LP_BLOCK_DEVICE_SLOT_SUFFIXED),
        ):
            for name, flags in rows:
                if flags & bit and _suffix_name(name, suffix) is None:
                    _limit("LP %s name too long" % label)
    copies = []
    for name_b, attrs, extents in records:
        text = name_b.decode("utf-8", "replace")
        if text in ("vendor_a", "vendor_b"):
            copies.append((text, extents, attrs))
    parts["\x00copies"] = copies
    parts["\x00records"] = records
    return parts


def _metadata_slot(reader, super_off, mx, slots, slot):
    """Primary metadata of this slot, else the backup. Both invalid raises Refuse.

    HardRefuse (a checker limit, or AdjustMetadataForSlot failing on the chosen
    copy) is not a reason to read the other copy.
    """
    parsed = None
    for backup in (False, True):
        try:
            raw = reader.read(super_off + _metadata_offset(mx, slots, slot, backup), mx)
        except HardRefuse:
            raise
        except Refuse:
            parsed = None
            continue
        try:
            parsed = _parse_metadata(raw, (mx, slots, 0))
            break
        except HardRefuse:
            raise
        except Refuse:
            parsed = None
    if parsed is None:
        _cannot("slot %d metadata is damaged in both copies" % slot)
    parts, first, bsize = parsed
    return _adjust_metadata_for_slot(parts, slot), first, bsize


def logical_partitions(reader, super_off, super_end):
    """One parsed table per metadata slot: primary geometry else backup, then each slot the same way.

    The installer does not force androidboot.slot_suffix=_a (initrd_nvme rewrites boot_part_uuid
    and leaves the suffix). A TEST vendor in any slot this image carries is visible to the caller.
    Returns a list of (parts, first_logical_sector).
    """
    super_len = super_end - super_off
    if super_len < LP_METADATA_BASE:
        _cannot("super is too small for LP geometry")
    geo = _geometry(reader.read(super_off + LP_PRIMARY_GEOMETRY, LP_GEOMETRY_SIZE))
    if geo is None:
        geo = _geometry(reader.read(super_off + LP_BACKUP_GEOMETRY, LP_GEOMETRY_SIZE))
    if geo is None:
        _cannot("no valid LP geometry (primary and backup)")
    mx, slots, _lbs = geo
    region = LP_METADATA_BASE + 2 * slots * mx
    if slots > 64 or region > super_len:
        _cannot("LP metadata extends past super")
    chosen = []
    for slot in range(slots):
        parts, first, bsize = _metadata_slot(reader, super_off, mx, slots, slot)
        if bsize != super_len:
            _cannot("LP block device size does not match super")
        chosen.append((parts, first))
    return chosen


class _Mem:
    def __init__(self, data):
        self.size = len(data)
        self._data = data

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise SystemExit("read outside the vendor image")
        return self._data[offset:offset + length]


class ExtentView:
    """Bytes of one logical partition, read through the image reader. Does not load the whole extent."""

    def __init__(self, reader, super_off, super_end, extents, first, name):
        self._reader = reader
        self._spans = []
        self.size = 0
        for nsec, target_type, target, source in extents:
            if target_type != 0 or source != 0:
                _cannot(name + " is not linear")
            if nsec == 0 or nsec > (MAX_VENDOR // 512) or target > (1 << 42):
                _cannot(name + " extent is too large")
            if target < first:
                _cannot(name + " extent overlaps LP metadata")
            start = super_off + target * 512
            length = nsec * 512
            if start < super_off or start + length > super_end:
                _cannot(name + " lies outside super")
            self.size += length
            if self.size > MAX_VENDOR:
                _cannot(name + " is too large")
            self._spans.append((self.size - length, start, length))

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise SystemExit("read outside the vendor image")
        out = []
        pos = offset
        left = length
        while left:
            span = None
            for base, disk_off, span_len in self._spans:
                if base <= pos < base + span_len:
                    span = (base, disk_off, span_len)
                    break
            if span is None:
                raise SystemExit("read outside the vendor image")
            base, disk_off, span_len = span
            take = min(left, base + span_len - pos)
            out.append(self._reader.read(disk_off + (pos - base), take))
            pos += take
            left -= take
        return b"".join(out)


def _as_view(data):
    if isinstance(data, (bytes, bytearray)):
        return _Mem(data)
    return data


class ErofsFS:
    """Pinned-kernel EROFS superblock (kernel/common 3938c8d97142 fs/erofs/erofs_fs.h, super.c).

    48BIT moves the root nid to rootnid_8b when that field is non-zero; offset 14 is then
    blocks_hi. METABOX stores inode bytes in the metabox inode instead of the metadata area.
    A dirent nid with bit 63 set is a metabox nid. An incompat bit outside
    EROFS_ALL_FEATURE_INCOMPAT, a non-zero dirblkbits, or extra devices is a refusal:
    this walker does not understand them.
    """

    def __init__(self, view):
        self.view = view
        self._metabox = None
        self._metabox_loading = False
        if view.size < 1024 + 128:
            raise SystemExit("vendor is not erofs")
        sb = view.read(1024, 128)
        if struct.unpack_from("<I", sb, 0)[0] != EROFS_MAGIC:
            raise SystemExit("vendor is not erofs")
        blkszbits = sb[12]
        if not 9 <= blkszbits <= 16:
            raise SystemExit("vendor erofs block size")
        sb_size = 128 + sb[13] * 16
        if sb_size > 4096 - 1024:
            raise SystemExit("vendor erofs superblock is too large")
        if sb_size > len(sb):
            sb = view.read(1024, sb_size)
        if sb[90]:
            raise SystemExit("vendor erofs dirblkbits is not supported")
        if struct.unpack_from("<H", sb, 86)[0]:
            raise SystemExit("vendor erofs extra devices are not supported")
        incompat = struct.unpack_from("<I", sb, 80)[0]
        unknown = incompat & ~EROFS_ALL_FEATURE_INCOMPAT
        if unknown:
            raise SystemExit("vendor erofs unidentified incompatible feature %s" % hex(unknown))
        self.blksz = 1 << blkszbits
        self.meta = struct.unpack_from("<I", sb, 40)[0]
        self.has_48 = bool(incompat & EROFS_FEATURE_INCOMPAT_48BIT)
        root_2b = struct.unpack_from("<H", sb, 14)[0]
        root_8b = struct.unpack_from("<Q", sb, 112)[0] if self.has_48 and len(sb) >= 120 else 0
        self.root = root_8b if self.has_48 and root_8b else root_2b
        self.has_metabox = bool(incompat & EROFS_FEATURE_INCOMPAT_METABOX)
        self.metabox_nid = 0
        if self.has_metabox:
            if sb_size <= 128:
                raise SystemExit("vendor erofs METABOX superblock is truncated")
            self.metabox_nid = struct.unpack_from("<Q", sb, 128)[0]
            if self.metabox_nid & (1 << EROFS_DIRENT_NID_METABOX_BIT):
                raise SystemExit("vendor erofs metabox nid loops")

    def metabox_bytes(self):
        if self._metabox is None:
            if self._metabox_loading:
                raise SystemExit("vendor erofs metabox nid loops")
            self._metabox_loading = True
            try:
                raw = inode_raw(self, self.metabox_nid)
                if raw["layout"] not in (0, 2):
                    raise SystemExit("vendor erofs metabox inode layout is not supported")
                _raw, chunks = _inode_bytes(self, self.metabox_nid)
            finally:
                self._metabox_loading = False
            self._metabox = _Mem(b"".join(chunks))
        return self._metabox

    def inode_loc(self, nid):
        """(view, offset) of this inode. Bit 63 selects the metabox file (erofs_iloc)."""
        in_box = bool(nid & (1 << EROFS_DIRENT_NID_METABOX_BIT))
        nid_lo = nid & EROFS_DIRENT_NID_MASK
        if in_box:
            if not self.has_metabox:
                raise SystemExit("vendor erofs dirent uses a metabox nid")
            return self.metabox_bytes(), nid_lo * 32
        return self.view, self.meta * self.blksz + nid_lo * 32


def _erofs_super(view):
    return ErofsFS(_as_view(view))


def inode_raw(fs, nid):
    if not isinstance(fs, ErofsFS):
        fs = _erofs_super(fs)
    view, off = fs.inode_loc(nid)
    if off < 0 or off + 32 > view.size:
        raise SystemExit("inode " + str(nid) + " is outside the vendor image")
    head = view.read(off, 32)
    fmt, xcount, mode = struct.unpack_from("<HHH", head, 0)
    if fmt & ~EROFS_I_ALL:
        raise SystemExit("inode " + str(nid) + " has an unsupported format")
    extended = fmt & 1
    layout = (fmt >> 1) & 7
    if extended and off + 64 > view.size:
        raise SystemExit("inode " + str(nid) + " is outside the vendor image")
    if extended:
        size = struct.unpack_from("<Q", head, 8)[0]
        isize = 64
        startblk_hi = struct.unpack_from("<H", head, 6)[0]
    else:
        size = struct.unpack_from("<I", head, 8)[0]
        isize = 32
        # Compact directories, and compact files without the nlink-is-1 bit, keep a 32-bit
        # start block. The kernel clears startblk_hi in that case (fs/erofs/inode.c).
        if (not stat.S_ISDIR(mode)) and ((fmt >> EROFS_I_NLINK_1_BIT) & 1):
            startblk_hi = struct.unpack_from("<H", head, 6)[0]
        else:
            startblk_hi = 0
    startblk = struct.unpack_from("<I", head, 16)[0] | (startblk_hi << 32)
    xsize = 12 + 4 * (xcount - 1) if xcount else 0
    return dict(off=off, view=view, fs=fs, fmt=fmt, layout=layout, xcount=xcount, mode=mode,
                extended=extended, size=size, isize=isize, xsize=xsize, startblk=startblk)


def _inode_bytes(fs, nid):
    raw = inode_raw(fs, nid)
    if raw["layout"] not in (0, 2):
        raise SystemExit("inode " + str(nid) + ": unsupported data layout " + str(raw["layout"]))
    size = raw["size"]
    if size > MAX_VENDOR:
        raise SystemExit("inode " + str(nid) + " is too large")
    fs = raw["fs"]
    blksz = fs.blksz
    addrmask = (1 << 48) - 1
    if not raw["extended"] and (stat.S_ISDIR(raw["mode"]) or not ((raw["fmt"] >> EROFS_I_NLINK_1_BIT) & 1)):
        addrmask = (1 << 32) - 1
    hole = raw["layout"] == 0 and ((raw["startblk"] ^ EROFS_NULL_ADDR) & addrmask) == 0
    chunks = []
    if hole:
        if size:
            chunks.append(bytes(size))
        return raw, chunks
    device = fs.view
    full = size // blksz if raw["layout"] == 2 else (size + blksz - 1) // blksz
    for i in range(full):
        start = (raw["startblk"] + i) * blksz
        take = min(blksz, size - i * blksz)
        if start < 0 or take < 0 or start + take > device.size:
            raise SystemExit("inode " + str(nid) + " is outside the vendor image")
        chunks.append(device.read(start, take))
    if raw["layout"] == 2 and size % blksz:
        start = raw["off"] + raw["isize"] + raw["xsize"]
        take = size % blksz
        if start < 0 or start + take > raw["view"].size:
            raise SystemExit("inode " + str(nid) + " inline tail is outside the vendor image")
        chunks.append(raw["view"].read(start, take))
    return raw, chunks


def list_dir(fs, nid):
    raw, chunks = _inode_bytes(fs, nid)
    if not stat.S_ISDIR(raw["mode"]):
        raise SystemExit("directory nid " + str(nid) + " is not a directory inode")
    entries = []
    seen = []
    for chunk in chunks:
        if len(chunk) < 12:
            raise SystemExit("directory nid " + str(nid) + " block is short")
        nameoff0 = struct.unpack_from("<H", chunk, 8)[0]
        if nameoff0 < 12:
            raise SystemExit("directory nid " + str(nid) + " name offset")
        count = nameoff0 // 12
        if count > len(chunk) // 12:
            raise SystemExit("directory nid " + str(nid) + " dirent count")
        dirents = [struct.unpack_from("<QHB", chunk, i * 12) for i in range(count)]
        for i, (child, nameoff, ftype) in enumerate(dirents):
            name_end = dirents[i + 1][1] if i + 1 < count else len(chunk)
            if nameoff < 12 or nameoff > name_end or name_end > len(chunk):
                raise SystemExit("directory nid " + str(nid) + " name offset")
            name = chunk[nameoff:name_end].split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
            if name in seen:
                raise SystemExit("directory nid " + str(nid) + " has a duplicate name")
            seen.append(name)
            if name not in (".", ".."):
                entries.append((child, ftype, name))
    return entries


def erofs_paths(data):
    """Literal directory names. Descent follows the inode mode, not the dirent file-type byte."""
    fs = _erofs_super(data)
    found = set()

    def walk(nid, rel, seen):
        if nid in seen:
            raise SystemExit("vendor directory loop")
        seen.add(nid)
        for child, _ftype, name in list_dir(fs, nid):
            if not name or "/" in name or name in (".", ".."):
                raise SystemExit("vendor directory name")
            path = name if rel == "" else rel + "/" + name
            found.add(path)
            mode = inode_raw(fs, child)["mode"]
            if stat.S_ISDIR(mode):
                walk(child, path, seen)

    walk(fs.root, "", set())
    return found


def _lookup(fs, dir_nid, name):
    for child, _ftype, ent_name in list_dir(fs, dir_nid):
        if ent_name == name:
            return child, inode_raw(fs, child)
    return None


def _read_symlink(fs, nid):
    raw, chunks = _inode_bytes(fs, nid)
    if not stat.S_ISLNK(raw["mode"]):
        raise SystemExit("vendor symlink inode is not a symlink")
    text = b"".join(chunks).split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
    if text == "":
        raise SystemExit("vendor symlink target")
    return text


def _resolve(view, path):
    """'reg' when `path` opens a regular file, following symlinks. 'missing' when a component is absent."""
    fs = _erofs_super(view)
    comps = path.split("/")
    if len(comps) > 64:
        raise SystemExit("vendor path")
    dir_stack = [fs.root]
    follows = 0
    i = 0
    while i < len(comps):
        name = comps[i]
        if name in ("", "."):
            i += 1
            continue
        if name == "..":
            if len(dir_stack) <= 1:
                raise SystemExit("vendor symlink leaves the image")
            dir_stack.pop()
            i += 1
            continue
        if not name or "/" in name:
            raise SystemExit("vendor directory name")
        found = _lookup(fs, dir_stack[-1], name)
        if found is None:
            return "missing"
        nid, raw = found
        mode = raw["mode"]
        if stat.S_ISLNK(mode):
            follows += 1
            if follows > MAX_SYMLINK_FOLLOWS:
                raise SystemExit("vendor symlink loop")
            target = _read_symlink(fs, nid)
            if target == "/vendor" or target.startswith("/vendor/"):
                extra = [c for c in target[len("/vendor"):].lstrip("/").split("/") if c != ""]
                dir_stack[:] = [fs.root]
            elif target.startswith("/"):
                raise SystemExit("vendor symlink leaves the image")
            else:
                extra = target.split("/")
            comps = extra + comps[i + 1:]
            if len(comps) > 128:
                raise SystemExit("vendor symlink loop")
            i = 0
            continue
        if stat.S_ISDIR(mode):
            if i == len(comps) - 1:
                return "dir"
            dir_stack.append(nid)
            i += 1
            continue
        if stat.S_ISREG(mode) and i == len(comps) - 1:
            return "reg"
        if stat.S_ISREG(mode):
            return "missing"
        raise SystemExit("vendor inode type is not a file, directory, or symlink")
    return "dir"


def vendor_hits(view):
    hit = []
    for path in FORBIDDEN:
        if _resolve(view, path) == "reg" and path not in hit:
            hit.append(path)
    return hit


def _scan_parts(reader, super_off, super_end, parts, first):
    """Scan every vendor_a / vendor_b CreateLogicalPartitions would map.

    fs_mgr_dm_linear.cpp skips a partition with no extents and a partition with
    LP_PARTITION_ATTR_DISABLED. A repeated name is still scanned: the second
    CreateDevice fails the whole slot, so a TEST extent under either name is
    not treated as absent. A slot whose only mounted vendor is vendor_b (the
    slot-1 name of a SLOT_SUFFIXED "vendor") is scanned; it is not "no vendor_a".
    """
    copies = parts.get("\x00copies") or []
    vendors = [item for item in copies if item[0] in ("vendor_a", "vendor_b")]
    if not vendors:
        _cannot("no vendor_a")
    hit = []
    scanned = False
    for name, extents, attrs in vendors:
        if attrs & LP_PARTITION_ATTR_DISABLED or not extents:
            continue
        scanned = True
        view = ExtentView(reader, super_off, super_end, extents, first, name)
        try:
            for path in vendor_hits(view):
                if path not in hit:
                    hit.append(path)
        except SystemExit as exc:
            detail = exc.code if isinstance(exc.code, str) else "vendor walk failed"
            _cannot(detail)
    if not scanned:
        _cannot("vendor_a has no extents")
    return hit


def _scan_super(reader, super_off, super_end):
    hit = []
    for parts, first in logical_partitions(reader, super_off, super_end):
        for path in _scan_parts(reader, super_off, super_end, parts, first):
            if path not in hit:
                hit.append(path)
    return hit


def inspect_reader(reader):
    hit = []
    for super_off, super_end in find_supers(reader):
        for path in _scan_super(reader, super_off, super_end):
            if path not in hit:
                hit.append(path)
    if hit:
        raise Refuse("vendor contains " + " and ".join(hit))


def inspect_file(path, limit):
    reader = FileReader(path, limit)
    try:
        inspect_reader(reader)
    finally:
        reader.close()


def inspect_dd(dev, limit):
    inspect_reader(DdReader(dev, limit))


def _dir_block(entries):
    count = len(entries)
    buf = bytearray(4096)
    pos = count * 12
    packed = []
    for name, nid, ftype in entries:
        raw = name.encode() + b"\0"
        packed.append((nid, pos, ftype, raw))
        pos += len(raw)
    if pos > 4096:
        raise ValueError("directory block overflow")
    for index, (nid, nameoff, ftype, raw) in enumerate(packed):
        struct.pack_into("<QHB", buf, index * 12, nid, nameoff, ftype)
        buf[nameoff:nameoff + len(raw)] = raw
    return buf


def build_erofs(paths, links=None):
    """Minimal EROFS whose directory names are `paths`. `links` maps a path to a symlink target.

    Used by the harness tests. Symlink targets are a flat block (layout 0), so they do not share
    the inode table with their inline tail.
    """
    links = dict(links or {})
    root = {}
    for raw in list(paths) + list(links):
        parts = raw.split("/")
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise ValueError(raw)
        node = root
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            if last and raw in links:
                if part in node:
                    raise ValueError(raw)
                node[part] = ("symlink", links[raw])
                break
            if last:
                if isinstance(node.get(part), dict):
                    raise ValueError(raw)
                node[part] = None
            else:
                nxt = node.get(part)
                if nxt is None:
                    nxt = {}
                    node[part] = nxt
                elif not isinstance(nxt, dict):
                    raise ValueError(raw)
                node = nxt
    dirs = []

    def collect(rel, node):
        for name in sorted(node):
            child = node[name]
            child_rel = name if rel == "" else rel + "/" + name
            if isinstance(child, dict):
                dirs.append((child_rel, rel, child))
                collect(child_rel, child)

    collect("", root)
    files = []
    symlinks = []

    def collect_rest(rel, node):
        for name in sorted(node):
            child = node[name]
            child_rel = name if rel == "" else rel + "/" + name
            if child is None:
                files.append(child_rel)
            elif isinstance(child, tuple):
                symlinks.append((child_rel, child[1]))
            else:
                collect_rest(child_rel, child)

    collect_rest("", root)
    nid_of = {"": 0}
    nxt = 1
    for rel, _parent, _node in dirs:
        nid_of[rel] = nxt
        nxt += 1
    for rel in files:
        nid_of[rel] = nxt
        nxt += 1
    for rel, _target in symlinks:
        nid_of[rel] = nxt
        nxt += 1
    if nxt * 32 > 4096:
        raise ValueError("too many inodes for one metadata block")
    dir_nodes = [("", "", root)] + dirs
    image = bytearray((2 + len(dir_nodes) + len(symlinks)) * 4096)
    struct.pack_into("<I", image, 1024, EROFS_MAGIC)
    image[1024 + 12] = 12
    struct.pack_into("<H", image, 1024 + 14, 0)
    struct.pack_into("<I", image, 1024 + 40, 1)

    def put_inode(nid, mode, nlink, size, raw_blk):
        off = 4096 + nid * 32
        struct.pack_into("<HHHH", image, off, 0, 0, mode, nlink)
        struct.pack_into("<I", image, off + 8, size)
        struct.pack_into("<I", image, off + 16, raw_blk)

    for index, (rel, parent, node) in enumerate(dir_nodes):
        entries = [(".", nid_of[rel], 2), ("..", nid_of[parent], 2)]
        for name in sorted(node):
            child = node[name]
            child_rel = name if rel == "" else rel + "/" + name
            if isinstance(child, dict):
                ftype = 2
            elif isinstance(child, tuple):
                ftype = 7
            else:
                ftype = 1
            entries.append((name, nid_of[child_rel], ftype))
        blkno = 2 + index
        image[blkno * 4096:(blkno + 1) * 4096] = _dir_block(entries)
        put_inode(nid_of[rel], 0o040755, 2, 4096, blkno)
    for rel in files:
        put_inode(nid_of[rel], 0o100644, 1, 0, 0)
    base = 2 + len(dir_nodes)
    for index, (rel, target) in enumerate(symlinks):
        raw = target.encode()
        if len(raw) > 4096:
            raise ValueError("symlink target too long")
        blkno = base + index
        image[blkno * 4096:blkno * 4096 + len(raw)] = raw
        put_inode(nid_of[rel], 0o120777, 1, len(raw), blkno)
    return bytes(image)


def _geometry_slot(mx, slots, lbs):
    blob = bytearray(LP_GEOMETRY_SIZE)
    struct.pack_into("<II", blob, 0, GEO_MAGIC, 52)
    struct.pack_into("<III", blob, 40, mx, slots, lbs)
    blob[8:40] = hashlib.sha256(blob[:52]).digest()
    return bytes(blob)


def _metadata_blob(specs, super_size, first, groups=None, limit=None):
    """specs: (name, extents) or (name, extents, attrs). An empty extent list is an unused slot.

    `groups` is (name, flags, maximum_size). The default is the three lapis groups.
    `limit` is the metadata slot size the blob must fit in (default LP_METADATA_MAX).
    """
    ptab, etab = bytearray(), bytearray()
    for spec in specs:
        if len(spec) == 2:
            name, extents = spec
            attrs = 1
        else:
            name, extents, attrs = spec
        raw = name.encode()
        if not raw or len(raw) > LP_NAME_LEN:
            raise ValueError(name)
        group = 1 if name.endswith("_a") else 2
        ptab += raw.ljust(LP_NAME_LEN, b"\0") + struct.pack("<IIII", attrs, len(etab) // 24, len(extents), group)
        for nsec, target in extents:
            etab += struct.pack("<QIQI", nsec, 0, target, 0)
    if groups is None:
        group_rows = (
            ("default", 0, 0),
            ("google_dynamic_partitions_a", 0, super_size),
            ("google_dynamic_partitions_b", 0, super_size),
        )
    else:
        group_rows = groups
    groups_b = b"".join(
        (gname.encode() if isinstance(gname, str) else gname).ljust(LP_NAME_LEN, b"\0")
        + struct.pack("<IQ", flags, gmax)
        for gname, flags, gmax in group_rows
    )
    bdev = struct.pack("<QIIQ", first, 1 << 20, 0, super_size) + b"super".ljust(LP_NAME_LEN, b"\0") + struct.pack("<I", 0)
    tables = bytes(ptab) + bytes(etab) + groups_b + bdev
    header = bytearray(256)
    struct.pack_into("<IHHI", header, 0, HDR_MAGIC, 10, 2, 256)
    struct.pack_into("<I", header, 44, len(tables))
    header[48:80] = hashlib.sha256(tables).digest()
    off = 0
    for at, num, esz in (
        (80, len(ptab) // 52, 52),
        (92, len(etab) // 24, 24),
        (104, len(group_rows), 48),
        (116, 1, 64),
    ):
        struct.pack_into("<III", header, at, off, num, esz)
        off += num * esz
    struct.pack_into("<I", header, 128, 1)
    header[12:44] = hashlib.sha256(header).digest()
    blob = bytes(header) + tables
    if len(blob) > (LP_METADATA_MAX if limit is None else limit):
        raise ValueError("LP metadata larger than a metadata slot")
    return blob


def _write_lp(handle, super_off, super_size, specs):
    geo = _geometry_slot(LP_METADATA_MAX, LP_SLOTS, 4096)
    blob = _metadata_blob(specs, super_size, 2048)
    slot = blob + bytes(LP_METADATA_MAX - len(blob))
    handle.seek(super_off + LP_PRIMARY_GEOMETRY)
    handle.write(geo)
    handle.seek(super_off + LP_BACKUP_GEOMETRY)
    handle.write(geo)
    for slot_index in range(LP_SLOTS):
        for backup in (False, True):
            handle.seek(super_off + _metadata_offset(LP_METADATA_MAX, LP_SLOTS, slot_index, backup))
            handle.write(slot)


def write_vendor_bytes(disk, a_image, b_image=None):
    """Plant a checksummed LP into `disk`'s super.

    vendor_a is `a_image` at 1 MiB inside super. `b_image` None is the lapis shape: vendor_b is
    listed and has no extents. A bytes `b_image` is planted at 2 MiB and given one linear extent.
    """
    reader = FileReader(str(disk), None)
    try:
        bounds = find_supers(reader)
    finally:
        reader.close()
    if len(bounds) != 1:
        raise ValueError("expected one super")
    super_off, super_end = bounds[0]
    super_size = super_end - super_off
    images = [("vendor_a", a_image, 1 << 20)]
    if b_image is not None:
        images.append(("vendor_b", b_image, 2 << 20))
    specs = [("vendor_a", []), ("vendor_b", [])]
    blobs = []
    for name, image, rel in images:
        if super_off + rel + len(image) > super_end:
            raise ValueError(name + " does not fit in super")
        nsec = (len(image) + 511) // 512
        padded = image + bytes(nsec * 512 - len(image))
        target = rel // 512
        if target < 2048:
            raise ValueError(name + " overlaps LP metadata")
        specs = [(n, ([(nsec, target)] if n == name else e)) for n, e in specs]
        blobs.append((rel, padded))
    region = LP_METADATA_BASE + 2 * LP_SLOTS * LP_METADATA_MAX
    if region > 1 << 20 or super_off + region > super_end:
        raise ValueError("LP metadata does not fit in super")
    with open(disk, "r+b") as handle:
        _write_lp(handle, super_off, super_size, specs)
        for rel, padded in blobs:
            handle.seek(super_off + rel)
            handle.write(padded)


def write_vendor_fixture(disk, paths, b_paths=None, links=None):
    """Plant LP + EROFS vendor_a (and vendor_b if b_paths is set) into `disk`'s super.

    vendor_b is always in the table. Without b_paths it has no extents, which is what a lapis
    super records for the unused slot.
    """
    b_image = None if b_paths is None else build_erofs(b_paths)
    write_vendor_bytes(disk, build_erofs(paths, links), b_image)


def write_broken_lp(disk):
    """LP geometry magic without a checksummed geometry. The checker must refuse, not allow."""
    reader = FileReader(str(disk), None)
    try:
        bounds = find_supers(reader)
    finally:
        reader.close()
    super_off, super_end = bounds[0]
    if super_off + LP_METADATA_BASE + 4 > super_end:
        raise ValueError("super is too small")
    with open(disk, "r+b") as handle:
        handle.seek(super_off + LP_PRIMARY_GEOMETRY)
        handle.write(b"gDla")
        handle.seek(super_off + LP_METADATA_BASE)
        handle.write(b"XXXX")


def main(argv):
    parser = argparse.ArgumentParser(prog="kano-autooff-image.py")
    parser.add_argument("--file", help="regular GPT disk image")
    parser.add_argument("--dd", help="block device to read through dd")
    parser.add_argument("--bytes", type=int, help="copied length; required with --dd")
    args = parser.parse_args(argv)
    if (args.file is None) == (args.dd is None):
        parser.error("pass exactly one of --file and --dd")
    if args.dd is not None and args.bytes is None:
        parser.error("--dd requires --bytes")
    try:
        if args.file is not None:
            inspect_file(args.file, args.bytes)
        else:
            inspect_dd(args.dd, args.bytes)
    except Refuse as exc:
        print("REFUSE: " + str(exc), file=sys.stderr)
        return 2
    print("vendor clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
