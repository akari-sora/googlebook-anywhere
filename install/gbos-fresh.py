#!/usr/bin/env python3
"""Internal fresh-install core. Caller owns admission, pristine trust and all fds.

Only the userdata prefix is cleared. Other partitions, ESP and old target gaps
may contain credentials/data; this API cannot prove a source is pristine.
No durable fresh-install recovery state or automatic rollback is provided.
"""
import hashlib
import os
import re
import struct
import uuid
import zlib

SECTOR = 512
MIB = 1 << 20
USERDATA_CLEAR = 16 * MIB
CHUNK = 4 * MIB
LIMITATIONS = ['NO_DURABLE_FRESH_RECOVERY', 'NOT_SECURE_ERASE', 'CALLER_PRISTINE_TRUST_REQUIRED']


def _read(u, reader, off, count):
    if off < 0 or count < 0 or off > reader.size or count > reader.size - off:
        raise u.Refuse('fresh: read outside source bounds')
    data = reader.pread(off, count)
    if len(data) != count:
        raise u.Refuse('fresh: short source read')
    return data


def read_gpt(u, reader):
    """Validate both GPTs before the existing parser adds project-layout checks."""
    if type(reader.size) is not int or reader.size % SECTOR or reader.size < 68 * SECTOR:
        raise u.Refuse('fresh: invalid whole-sector disk size')
    last = reader.size // SECTOR - 1
    mbr = _read(u, reader, 0, SECTOR)
    if (mbr[510:] != b'\x55\xaa' or mbr[450] != 0xee
            or struct.unpack_from('<II', mbr, 454) != (1, min(last, 0xffffffff))
            or any(mbr[462:510])):
        raise u.Refuse('fresh: invalid or hybrid protective MBR')

    def header(at, alternate, table_at):
        h = _read(u, reader, at * SECTOR, SECTOR)
        if (h[:8] != b'EFI PART' or struct.unpack_from('<II', h, 8) != (0x10000, 92)
                or struct.unpack_from('<I', h, 20)[0] or any(h[92:])):
            raise u.Refuse('fresh: unsupported GPT header')
        crc = struct.unpack_from('<I', h, 16)[0]
        if zlib.crc32(h[:16] + bytes(4) + h[20:92]) != crc:
            raise u.Refuse('fresh: GPT header CRC mismatch')
        current, alt, first, end = struct.unpack_from('<QQQQ', h, 24)
        tlba, count, width, tcrc = struct.unpack_from('<QIII', h, 72)
        if ((current, alt, tlba, count, width) != (at, alternate, table_at, 128, 128)
                or not 34 <= first <= end or end != last - 33
                or h[56:72] == bytes(16)):
            raise u.Refuse('fresh: unsupported or invalid GPT geometry')
        table = _read(u, reader, tlba * SECTOR, count * width)
        if zlib.crc32(table) != tcrc:
            raise u.Refuse('fresh: GPT array CRC mismatch')
        return h, table

    primary, table = header(1, last, 2)
    backup, other = header(last, 1, last - 32)
    if primary[40:72] != backup[40:72] or primary[80:92] != backup[80:92] or table != other:
        raise u.Refuse('fresh: GPT copies disagree')
    seen = {primary[56:72]}
    for i in range(128):
        entry = table[i * 128:(i + 1) * 128]
        if entry[:16] == bytes(16):
            if any(entry):
                raise u.Refuse('fresh: nonempty unused GPT entry')
            continue
        guid = entry[16:32]
        if guid == bytes(16) or guid in seen:
            raise u.Refuse('fresh: zero or duplicate GPT GUID')
        seen.add(guid)
        # Do not accept hidden nonzero characters after an embedded terminator.
        try:
            name = entry[56:128].decode('utf-16le', errors='strict').rstrip('\0')
        except UnicodeDecodeError as exc:
            raise u.Refuse('fresh: invalid GPT partition name encoding') from exc
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,36}', name):
            raise u.Refuse('fresh: invalid GPT partition name')
    layout = u.gpt_read(reader)
    u.validate_preview_layout(reader, layout)
    user = layout['userdata']
    if any(p['start'] + p['len'] > user['start'] for n, p in layout.items() if n != 'userdata'):
        raise u.Refuse('fresh: userdata is not physically last')
    return dict(layout=layout, disk_guid=str(uuid.UUID(bytes_le=primary[56:72])),
                first_usable=struct.unpack_from('<Q', primary, 40)[0], last_usable=last - 33,
                last_lba=last)


def _new_guid(u, used):
    for _ in range(16):
        candidate = uuid.uuid4()
        if candidate.int and str(candidate) not in used:
            used.add(str(candidate))
            return str(candidate)
    raise u.Refuse('fresh: cannot generate distinct GUIDs')


def prepare_gpt(u, reader, target_size):
    source = read_gpt(u, reader)
    if type(target_size) is not int or target_size % SECTOR or target_size < 68 * SECTOR:
        raise u.Refuse('fresh: target size is not whole sectors')
    last = target_size // SECTOR - 1
    user_start = source['layout']['userdata']['start']
    if (last - 33 + 1) * SECTOR - user_start < USERDATA_CLEAR:
        raise u.Refuse('fresh: target has less than 16 MiB userdata space')
    used = {source['disk_guid']} | {p['guid'] for p in source['layout'].values()}
    disk_guid = _new_guid(u, used)
    layout = {}
    table = bytearray(128 * 128)
    for name, original in source['layout'].items():
        p = dict(original, guid=_new_guid(u, used))
        if name == 'userdata':
            p['len'] = (last - 33 + 1) * SECTOR - p['start']
        layout[name] = p
        entry = (uuid.UUID(p['type']).bytes_le + uuid.UUID(p['guid']).bytes_le
                 + struct.pack('<QQQ', p['start'] // SECTOR, (p['start'] + p['len']) // SECTOR - 1,
                               p['attrs']) + name.encode('utf-16le').ljust(72, b'\0'))
        table[(p['n'] - 1) * 128:p['n'] * 128] = entry
    table = bytes(table)

    def header(at, alt, tlba):
        h = bytearray(SECTOR)
        struct.pack_into('<8sIIIIQQQQ16sQIII', h, 0, b'EFI PART', 0x10000, 92, 0, 0,
                         at, alt, source['first_usable'], last - 33, uuid.UUID(disk_guid).bytes_le,
                         tlba, 128, 128, zlib.crc32(table))
        struct.pack_into('<I', h, 16, zlib.crc32(h[:92]))
        return bytes(h)
    mbr = bytearray(SECTOR)
    mbr[446:454] = bytes([0, 0, 2, 0, 0xee, 0xff, 0xff, 0xff])
    struct.pack_into('<II', mbr, 454, 1, min(last, 0xffffffff))
    mbr[510:512] = b'\x55\xaa'
    return dict(source=source, layout=layout, disk_guid=disk_guid, size=target_size,
                primary=header(1, last, 2), backup=header(last, 1, last - 32),
                table=table, pmbr=bytes(mbr), last_lba=last)


def _sealed_esp(u, binding, rel):
    import fcntl
    fd = binding._esp_fd
    required = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    if (fd is None or rel.esp.pass_fds != (fd,) or rel.esp.spec != f'/proc/self/fd/{fd}'
            or fcntl.fcntl(fd, fcntl.F_GET_SEALS) & required != required):
        raise u.Refuse('fresh: source ESP is not the held sealed snapshot')
    size = rel.layout[u.ESP]['len']
    u.fat_fits(os.pread(fd, SECTOR, 0), size, 'fresh sealed source ESP')
    if os.fstat(fd).st_size != size:
        raise u.Refuse('fresh: sealed ESP size mismatch')
    h = hashlib.sha256()
    for off in range(0, size, CHUNK):
        data = os.pread(fd, min(CHUNK, size - off), off)
        if len(data) != min(CHUNK, size - off):
            raise u.Refuse('fresh: short sealed ESP read')
        h.update(data)
    return h.hexdigest()


def _inventory(u, fat, expected):
    names = fat.ls('/loader/entries')
    if set(names) != set(expected) or len(names) != len(expected):
        raise u.Refuse('fresh: source ESP boot entry inventory differs')
    root_names = {n.casefold() for n in fat.ls('/')}
    if {'gbos', 'gbos-nvme'} & root_names:
        raise u.Refuse('fresh: source ESP contains known transaction/previous files')
    if any(n not in u.ENTRIES for n in names):
        raise u.Refuse('fresh: source ESP contains unknown boot entries')


def prepare_boot(u, rel, boot_guid):
    """Prepare bytes using the sealed source view; no decompression/recompression."""
    _inventory(u, rel.esp, rel.entries)
    if not set(u.REQUIRED_ENTRIES) <= set(rel.entries) <= set(u.ENTRIES):
        raise u.Refuse('fresh: unsupported source boot entries')
    original = rel.esp.read('/initrd.img')
    ramdisk, bc = u.split_initrd(original)
    if (ramdisk != rel.ramdisk or bc != rel.bc or u.bc_get(bc, 'androidboot.slot_suffix') != '_a'
            or u.bc_get(bc, 'androidboot.boot_part_uuid') != rel.layout['boot_a']['guid']
            or rel.esp.read('/kernel.efi') != rel.kernel):
        raise u.Refuse('fresh: source boot bytes differ from release')
    entries = {}
    for name, entry in rel.entries.items():
        actual = u.entry_parse(rel.esp.read('/loader/entries/' + name).decode('utf-8'))
        if (actual != entry or entry['linux'] != '/kernel.efi' or entry['initrd'] != ['/initrd.img']
                or u.PREV_TITLE in entry['title']):
            raise u.Refuse('fresh: source entry is not original slot-a layout')
        entries[name] = u.entry_text(entry['title'], '/kernel.efi', ['/initrd.img'],
                                     u.lift(entry['options'])).encode()
    initrd = ramdisk + u.bc_blob(u.bc_set(bc, 'androidboot.boot_part_uuid', boot_guid))
    loader = b'default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n'
    files = {'/initrd.img': initrd, '/loader/loader.conf': loader}
    files.update({'/loader/entries/' + n: data for n, data in entries.items()})
    free, cluster = rel.esp.free()
    if type(free) is not int or type(cluster) is not int or free < 0 or not 0 < cluster <= MIB:
        raise u.Refuse('fresh: invalid FAT free space')
    rounded = lambda size: -(-size // cluster) * cluster
    # Existing files are replaced. Reserve their positive cluster growth and
    # margin for directory updates; later write failures still leave a partial target.
    needed = sum(max(0, rounded(len(data)) - rounded(len(rel.esp.read(path))))
                 for path, data in files.items()) + 4 * cluster
    if needed > free:
        raise u.Refuse('fresh: insufficient ESP replacement space')
    return dict(files=files, entries=entries, kernel=rel.kernel, initrd=initrd, loader=loader,
                required_free=needed)


def _copy(u, disk, binding, part):
    h = hashlib.sha256()
    for off in range(0, part['len'], CHUNK):
        data = _read(u, binding, part['start'] + off, min(CHUNK, part['len'] - off))
        h.update(data)
        disk.pwrite(part['start'] + off, data)
    disk.sync_drop()
    digest = h.hexdigest()
    if disk.sha(part['start'], part['len']) != digest:
        raise u.Failed('fresh: partition copy readback differs')
    return digest


def install(update, disk, binding, tmp, check_target, *, after_write_check):
    """Consume admitted caller-owned objects; never open/close a source/target path.

    Both callbacks are required. check_target runs only before writes;
    after_write_check must tolerate this operation's own mtime/ctime changes.
    The caller classifies exceptions using update.WRITING, and owns cleanup.
    """
    u = update
    if any(name.startswith('GBOS_UPDATE_TEST') for name in os.environ):
        raise u.Refuse('fresh: updater test environment is forbidden')
    if not callable(check_target) or not callable(after_write_check) or not disk.write:
        raise u.Refuse('fresh: missing target admission callbacks or writable held disk')
    rel = binding.release
    u.SharedBinding.verify(binding, rel, disk)
    if not isinstance(rel, u.SharedRelease):
        raise u.Refuse('fresh: requires a live shared release')
    plan = prepare_gpt(u, binding, disk.size)
    if plan['source']['layout'] != rel.layout:
        raise u.Refuse('fresh: source layout differs from selected release')
    esp_digest = _sealed_esp(u, binding, rel)
    boot = prepare_boot(u, rel, plan['layout']['boot_a']['guid'])
    # The potentially expensive complete source verification precedes the final target check.
    u.SharedBinding.verify(binding, rel, disk)
    check_target()
    u.WRITING.append(True)  # Record this operation even if a parent marker exists.
    disk.pwrite(SECTOR, bytes(SECTOR))
    disk.pwrite(plan['last_lba'] * SECTOR, bytes(SECTOR))
    disk.sync_drop()
    copied = {}
    for name, part in sorted(rel.layout.items(), key=lambda item: item[1]['start']):
        if name != 'userdata':
            copied[name] = _copy(u, disk, binding, part)
    if copied[u.ESP] != esp_digest:
        raise u.Failed('fresh: copied ESP differs from sealed source snapshot')
    user = plan['layout']['userdata']
    for off in range(0, USERDATA_CLEAR, CHUNK):
        disk.pwrite(user['start'] + off, bytes(CHUNK))
    disk.sync_drop()
    if disk.sha(user['start'], USERDATA_CLEAR) != hashlib.sha256(bytes(USERDATA_CLEAR)).hexdigest():
        raise u.Failed('fresh: userdata prefix clear readback differs')
    esp = plan['layout'][u.ESP]
    fat = u.BoundedMutableFat(disk.vol(esp['start']), tmp, disk.fd)
    u.fat_fits(disk.pread(esp['start'], SECTOR), esp['len'], 'fresh target ESP')
    for path, data in boot['files'].items():
        fat.write(path, data)
    disk.sync_drop()
    for path, data in boot['files'].items():
        if fat.read(path) != data:
            raise u.Failed('fresh: final boot file readback differs')
    if fat.read('/kernel.efi') != boot['kernel']:
        raise u.Failed('fresh: final kernel differs')
    _inventory(u, fat, boot['entries'])
    final_esp = disk.sha(esp['start'], esp['len'])
    if final_esp == esp_digest:
        raise u.Failed('fresh: ESP unexpectedly unchanged')
    disk.pwrite((plan['last_lba'] - 32) * SECTOR, plan['table'])
    disk.pwrite(plan['last_lba'] * SECTOR, plan['backup'])
    disk.sync_drop()
    disk.pwrite(2 * SECTOR, plan['table'])
    disk.pwrite(SECTOR, plan['primary'])
    disk.pwrite(0, plan['pmbr'])
    disk.sync_drop()
    final = read_gpt(u, disk)
    if final['layout'] != plan['layout'] or final['disk_guid'] != plan['disk_guid']:
        raise u.Failed('fresh: published GPT readback differs')
    u.SharedBinding.verify(binding, rel, disk)
    after_write_check()
    return dict(operation='fresh-install', disk_guid=plan['disk_guid'], layout=plan['layout'],
                copied_sha256=copied, source_esp_sha256=esp_digest, final_esp_sha256=final_esp,
                userdata_cleared_bytes=USERDATA_CLEAR, limitations=list(LIMITATIONS))
