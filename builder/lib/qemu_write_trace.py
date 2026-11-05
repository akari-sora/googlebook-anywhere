"""Fail-closed accounting of QEMU USB writes within the userdata partition.

The caller supplies a fresh per-run trace with only the events below and a
same-run QMP query-blockstats snapshot. Guest CPUs must already be paused. QMP
completed bytes/operations must exactly reconcile with the complete trace;
neither an unattributed SCSI request nor an incomplete asynchronous I/O can
grant a pass. Values describe requested bytes, including repeated writes.
"""
import re

SCSI = re.compile(r'scsi_disk_dma_command_WRITE Write .*\(sector (\d+), count (\d+)\)\n')
NVME = re.compile(r"pci_nvme_io_cmd cid \d+ nsid 0x[0-9a-fA-F]+ sqid \d+ opc 0x([0-9a-fA-F]+) opname '[^'\n]*'\n")
NVME_ADMIN = re.compile(r"pci_nvme_admin_cmd cid \d+ sqid \d+ opc 0x([0-9a-fA-F]+) opname '[^'\n]*'\n")
BLOCK = re.compile(r'blk_co_pwritev blk (0x[0-9a-fA-F]+) bs (0x[0-9a-fA-F]+) '
                   r'offset (\d+) bytes (\d+) flags (0x[0-9a-fA-F]+)\n')

def nvme_trace_safe(path):
    """Reject unrecognized or write-capable NVMe commands in the complete trace."""
    try:
        with open(path, 'r', encoding='ascii', newline='') as stream:
            for line in stream:
                m = NVME.fullmatch(line)
                if m and int(m[1], 16) in (0x00, 0x02):
                    continue
                m = NVME_ADMIN.fullmatch(line)
                if m and int(m[1], 16) in (0x00, 0x01, 0x02, 0x04, 0x05, 0x06, 0x08, 0x09, 0x0a, 0x0c):
                    continue
                if SCSI.fullmatch(line) or BLOCK.fullmatch(line):
                    continue
                raise ValueError('NVMe trace contains unknown or modifying event')
    except (OSError, UnicodeError) as exc:
        raise ValueError('cannot read complete NVMe trace') from exc

def userdata_write_bytes(path, blockstats, start, end, disk_size):
    """Return the USB write-byte intersection with [start,end), or raise ValueError."""
    def invalid(reason):
        raise ValueError('chain write trace: ' + reason)
    if (any(type(x) is not int for x in (start, end, disk_size)) or not 0 <= start < end <= disk_size
            or any(x % 512 for x in (start, end, disk_size))):
        invalid('invalid userdata geometry')
    if not isinstance(blockstats, list):
        invalid('missing QMP block statistics')
    usb = [x for x in blockstats if isinstance(x, dict) and x.get('device') == 'usbdisk']
    scsi = [x for x in blockstats if isinstance(x, dict) and isinstance(x.get('qdev'), str)
            and '/scsi.' in x['qdev']]
    if len(usb) != 1 or len(scsi) != 1 or scsi[0] is not usb[0]:
        invalid('USB or SCSI device missing or ambiguous')
    stats = usb[0].get('stats')
    if not isinstance(stats, dict):
        invalid('USB counters missing')
    keys = ('wr_bytes', 'wr_operations', 'failed_wr_operations', 'invalid_wr_operations')
    if any(type(stats.get(k)) is not int or stats[k] < 0 for k in keys):
        invalid('USB counters invalid or missing')
    if stats['failed_wr_operations'] or stats['invalid_wr_operations']:
        invalid('USB write failed or was invalid')

    by_node, candidates, blk_nodes = {}, set(), {}
    pending = None
    scsi_bytes = 0
    try:
        with open(path, 'r', encoding='ascii', newline='') as stream:
            for line in stream:
                m = SCSI.fullmatch(line)
                if m:
                    offset, length = int(m[1]) * 512, int(m[2]) * 512
                    if length <= 0 or offset + length > disk_size:
                        invalid('SCSI request out of bounds')
                    scsi_bytes += length
                    pending = (offset, length)
                    continue
                m = NVME.fullmatch(line)
                if m:
                    # A command can be outstanding while QMP counters are zero.
                    # Permit only read and flush; reject all write-like/unknown opcodes.
                    if int(m[1], 16) not in (0x00, 0x02):
                        invalid('NVMe command may modify the device')
                    pending = None
                    continue
                m = NVME_ADMIN.fullmatch(line)
                if m:
                    # Conservative allowlist for controller setup and queries;
                    # format, sanitize, and unknown admin commands fail closed.
                    if int(m[1], 16) not in (0x00, 0x01, 0x02, 0x04, 0x05, 0x06, 0x08, 0x09, 0x0a, 0x0c):
                        invalid('NVMe admin command may modify the device')
                    pending = None
                    continue
                m = BLOCK.fullmatch(line)
                if not m:
                    invalid('missing, truncated or unknown trace event')
                blk, bs, offset, length = m[1], m[2], int(m[3]), int(m[4])
                if length <= 0:
                    invalid('empty block write')
                pair = (blk, bs)
                blk_nodes.setdefault(blk, set()).add(bs)
                by_node.setdefault(pair, []).append((offset, length))
                if pending == (offset, length):
                    candidates.add(pair)
                pending = None
    except (OSError, UnicodeError) as exc:
        raise ValueError('chain write trace: cannot read complete trace') from exc
    if len(candidates) != 1:
        invalid('USB trace identity missing or ambiguous')
    pair = next(iter(candidates))
    if len(blk_nodes[pair[0]]) != 1:
        invalid('USB block pointer changed node')
    writes = by_node[pair]
    if any(offset + length > disk_size for offset, length in writes):
        invalid('USB block request out of bounds')
    total = sum(length for _, length in writes)
    if (scsi_bytes != total or total != stats['wr_bytes'] or len(writes) != stats['wr_operations']):
        invalid('trace and completed USB counters do not reconcile')
    return sum(max(0, min(offset + length, end) - max(offset, start)) for offset, length in writes)
