"""Independent bounded LP readback for the miniature QEMU fixture only.

Input is an already-open regular-file descriptor and the super GPT entry from
the independent gptlib parser. Never opens a path or uses the product parser.
"""
import hashlib
import os
import stat
import struct

SECTOR = 512
MAX_IMAGE = 128 * 1024 * 1024
LOGICAL = ('system', 'vendor', 'product', 'system_ext', 'system_dlkm', 'vendor_dlkm')


def need(condition, message):
    if not condition:
        raise ValueError(message)


def snapshot(fd, super_part):
    st = os.fstat(fd)
    need(stat.S_ISREG(st.st_mode) and 0 < st.st_size <= MAX_IMAGE, 'not a bounded fixture file')
    need(isinstance(super_part, dict) and super_part.get('name') == 'super', 'missing super GPT entry')
    first, last = super_part.get('first'), super_part.get('last')
    need(type(first) is int and type(last) is int and 0 <= first <= last, 'invalid super geometry')
    start, size = first * SECTOR, (last - first + 1) * SECTOR
    need(size <= MAX_IMAGE and start + size <= st.st_size and size >= 1024 * 1024, 'super outside fixture')

    def read(off, length):
        need(type(off) is int and type(length) is int and 0 <= off <= size and
             0 <= length <= size - off, 'LP read outside super')
        data = os.pread(fd, length, start + off)
        need(len(data) == length, 'short LP read')
        return data

    def hash_extent(extents):
        digest = hashlib.sha256()
        for sector, count in extents:
            offset, left = sector * SECTOR, count * SECTOR
            while left:
                chunk = min(left, 1024 * 1024)
                digest.update(read(offset, chunk))
                offset += chunk
                left -= chunk
        return digest.hexdigest()

    geometry = [read(4096, 4096), read(8192, 4096)]
    need(geometry[0] == geometry[1], 'LP geometry copies differ')
    g = geometry[0]
    magic, gsize = struct.unpack_from('<II', g)
    need(magic == 0x616C4467 and gsize == 52, 'unsupported fixture geometry')
    expected_geo_hash = g[8:40]
    need(hashlib.sha256(g[:8] + bytes(32) + g[40:gsize]).digest() == expected_geo_hash,
         'LP geometry checksum')
    mx, slots, lbs = struct.unpack_from('<III', g, 40)
    need((mx, slots, lbs) == (65536, 3, 4096), 'unsupported fixture metadata geometry')
    need(12288 + 2 * slots * mx <= size, 'LP metadata outside super')

    blobs = []
    for index in range(2 * slots):
        raw = read(12288 + index * mx, mx)
        magic, major, minor, hsz = struct.unpack_from('<IHHI', raw)
        need((magic, major, minor, hsz) == (0x414C5030, 10, 2, 256), 'unsupported LP metadata header')
        header = raw[:hsz]
        need(hashlib.sha256(header[:12] + bytes(32) + header[44:]).digest() == header[12:44],
             'LP header checksum')
        tsize = struct.unpack_from('<I', header, 44)[0]
        need(tsize <= mx - hsz, 'LP tables outside metadata slot')
        tables = raw[hsz:hsz + tsize]
        need(hashlib.sha256(tables).digest() == header[48:80], 'LP tables checksum')
        blobs.append(header + tables)
    need(all(blob == blobs[0] for blob in blobs), 'LP metadata copies differ')
    blob = blobs[0]
    header, tables = blob[:256], blob[256:]
    entries, offset = [], 0
    for at, entry_size in ((80, 52), (92, 24), (104, 48), (116, 64)):
        pos, count, actual_size = struct.unpack_from('<III', header, at)
        need(actual_size == entry_size and pos == offset and count <= 512 and
             pos + count * actual_size <= len(tables), 'invalid LP table layout')
        entries.append((pos, count, actual_size))
        offset += count * actual_size
    need(offset == len(tables), 'unaccounted LP table bytes')
    (p_off, p_count, _), (e_off, e_count, _), (g_off, g_count, _), (b_off, b_count, _) = entries
    need(p_count == len(LOGICAL) * 2 and b_count == 1, 'unexpected LP fixture inventory')

    def name_at(offset):
        raw = tables[offset:offset + 36]
        name, separator, rest = raw.partition(b'\0')
        need(bool(separator) and not any(rest), 'invalid LP name padding')
        try:
            name = name.decode('ascii')
        except UnicodeDecodeError as exc:
            raise ValueError('invalid LP name') from exc
        need(bool(name), 'empty LP name')
        return name

    groups = [name_at(g_off + i * 48) for i in range(g_count)]
    need(len(set(groups)) == len(groups) and
         set(groups) == {'default', 'google_dynamic_partitions_a', 'google_dynamic_partitions_b'},
         'unexpected LP groups')
    first_data, align, align_offset, devsize = struct.unpack_from('<QIIQ', tables, b_off)
    need(devsize == size and first_data * SECTOR >= 12288 + 2 * slots * mx and
         0 < align <= size and align % SECTOR == 0 and align_offset == 0 and
         name_at(b_off + 24) == 'super', 'invalid LP block device')
    extent_rows = [struct.unpack_from('<QIQI', tables, e_off + i * 24) for i in range(e_count)]
    partitions, spans, used = {}, [], set()
    for i in range(p_count):
        off = p_off + i * 52
        name = name_at(off)
        attrs, first_extent, extent_count, group = struct.unpack_from('<IIII', tables, off + 36)
        need(name not in partitions and group < len(groups) and
             first_extent + extent_count <= e_count, 'invalid LP partition')
        extents = []
        for n in range(first_extent, first_extent + extent_count):
            count, kind, sector, source = extent_rows[n]
            need(n not in used and count > 0 and kind == 0 and source == 0 and
                 sector >= first_data and sector + count <= size // SECTOR, 'invalid LP extent')
            used.add(n)
            spans.append((sector, sector + count))
            extents.append([sector, count])
        partitions[name] = {'attrs': attrs, 'group': groups[group], 'extents': extents,
                            'size': sum(n for _, n in extents) * SECTOR, 'sha256': hash_extent(extents)}
    need(used == set(range(e_count)), 'unreferenced LP extent')
    spans.sort()
    need(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), 'overlapping LP extents')
    need(set(partitions) == {f'{name}_{slot}' for name in LOGICAL for slot in 'ab'},
         'unexpected LP logical partitions')
    after = os.fstat(fd)
    need((st.st_dev, st.st_ino, st.st_size) == (after.st_dev, after.st_ino, after.st_size),
         'fixture file changed identity')
    return {'geometry_sha256': hashlib.sha256(g).hexdigest(), 'metadata_sha256': hashlib.sha256(blob).hexdigest(),
            'metadata_copies': len(blobs), 'partitions': partitions}


def assert_fixture_change(before, release):
    for snap in (before, release):
        need(snap.get('metadata_copies') == 6 and set(snap.get('partitions', {})) ==
             {f'{name}_{slot}' for name in LOGICAL for slot in 'ab'}, 'incomplete fixture snapshot')
        for name in LOGICAL:
            need(snap['partitions'][name + '_a']['size'] > 0, 'missing source slot A content')
    old = before['partitions']['vendor_a']
    new = release['partitions']['vendor_a']
    need(old['sha256'] != new['sha256'], 'fixture vendor did not change')


def verify_update(before, after, release):
    assert_fixture_change(before, release)
    need(after.get('metadata_copies') == 6 and set(after.get('partitions', {})) ==
         set(before['partitions']), 'updated LP inventory differs')
    for name in LOGICAL:
        old, current = before['partitions'][name + '_a'], after['partitions'][name + '_a']
        need(old == current, 'old logical slot A changed: ' + name)
        src, new = release['partitions'][name + '_a'], after['partitions'][name + '_b']
        need(new['size'] == src['size'] and new['sha256'] == src['sha256'] and
             new['attrs'] == src['attrs'] and new['group'] == 'google_dynamic_partitions_b',
             'updated logical slot B differs from source: ' + name)
