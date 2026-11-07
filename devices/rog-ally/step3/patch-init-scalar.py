#!/usr/bin/env python3
"""Single-artifact, two-byte scalar gate repair. Never modifies the input."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct

SOURCE_SHA256 = '02c8c532e922d215e0516ac967bd719072b24861ee3de7a9d81bc1934b00037f'
SOURCE_SIZE = 3137864
FUNCTION_SHA256 = '094ff3994dd8294e4d6731183f2d62bd87ffde5a840bb5101d8b1894689deefc'
ENTRY_VA = 0x4b8440
GATE_VA = 0x4b8495
GATE_OFFSET = 0x2b8495
OLD = bytes.fromhex('7313')
NEW = bytes.fromhex('9090')


class Refused(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def loads(data):
    require(len(data) >= 64 and data[:7] == b'\x7fELF\x02\x01\x01', 'not ELF64 LE')
    require(struct.unpack_from('<H', data, 18)[0] == 62, 'not x86-64')
    phoff = struct.unpack_from('<Q', data, 32)[0]
    entsize, count = struct.unpack_from('<HH', data, 54)
    require(entsize == 56 and 0 < count <= 128 and phoff + count * 56 <= len(data), 'invalid program headers')
    result = []
    for i in range(count):
        typ, flags, off, va, _, size, mem, align = struct.unpack_from('<IIQQQQQQ', data, phoff + i * 56)
        if typ != 1:
            continue
        require(size <= mem and off + size <= len(data) and va + mem < 2**64, 'invalid PT_LOAD')
        require(align in (0, 1) or (align & (align - 1) == 0 and va % align == off % align), 'invalid alignment')
        result.append((off, va, size, mem, flags))
    require(result, 'no PT_LOAD')
    intervals = sorted((va, va + mem) for _, va, _, mem, _ in result if mem)
    require(all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:])), 'overlapping PT_LOAD ranges')
    require(max(b for _, b in intervals) - min(a for a, _ in intervals) <= 64 * 1024 * 1024, 'excessive mapping span')
    return result


def transform(data):
    # No caller-supplied hash/offset/replacement and no force/allow-unknown switch.
    require(len(data) == SOURCE_SIZE, 'source size mismatch')
    require(digest(data) == SOURCE_SHA256, 'source SHA256 mismatch (already patched also refused)')
    require(digest(data[0x2b8440:0x2b8878]) == FUNCTION_SHA256,
            'function context mismatch')
    segments = loads(data)
    mappings = [off + GATE_VA - va for off, va, size, _, flags in segments
                if flags & 1 and va <= ENTRY_VA and GATE_VA + 2 <= va + size]
    require(mappings == [GATE_OFFSET], 'entry/gate executable mapping mismatch')
    require(data[GATE_OFFSET:GATE_OFFSET + 2] == OLD, 'exact old gate bytes mismatch')
    result = data[:GATE_OFFSET] + NEW + data[GATE_OFFSET + 2:]
    require(len(result) == len(data), 'length changed')
    return result


def plain_path(path, output=False):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if output and current == path:
            require(not current.exists() and not current.is_symlink(), 'output already exists')
        else:
            require(not stat.S_ISLNK(current.lstat().st_mode), 'symlink component refused')
    return path


def patch(source, destination):
    source = plain_path(source)
    destination = plain_path(destination, output=True)
    require(source != destination, 'input/output alias')
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    created = False
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'input must be plain, non-hardlinked file')
        require(hasattr(os, 'listxattr'), 'Linux xattr inspection unavailable')
        require(not before.st_mode & 0o7000 and not os.listxattr(fd), 'special mode/xattrs require separate integration')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            original = stream.read()
        updated = transform(original)
        now = os.fstat(fd)
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (now.st_size, now.st_mtime_ns, now.st_ctime_ns), 'input changed while reading')
        outfd = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        try:
            with os.fdopen(os.dup(outfd), 'wb') as stream:
                stream.write(updated)
                stream.flush()
            os.fchmod(outfd, stat.S_IMODE(before.st_mode))
            os.fsync(outfd)
            os.lseek(outfd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(outfd), 'rb') as stream:
                require(stream.read() == updated, 'output readback mismatch')
            require(stat.S_IMODE(os.fstat(outfd).st_mode) == stat.S_IMODE(before.st_mode), 'mode mismatch')
        finally:
            os.close(outfd)
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), 'rb') as stream:
            require(stream.read() == original, 'input changed before final readback')
        require(os.stat(source, follow_symlinks=False).st_ino == before.st_ino, 'input path changed')
        dfd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return {'source_sha256': digest(original), 'output_sha256': digest(updated),
                'size': len(updated), 'changed_offsets': [GATE_OFFSET, GATE_OFFSET + 1],
                'entry_va': hex(ENTRY_VA), 'gate_va': hex(GATE_VA),
                'old_hex': OLD.hex(), 'new_hex': NEW.hex(), 'mode': oct(stat.S_IMODE(before.st_mode))}
    except BaseException:
        if created:
            destination.unlink()
        raise
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('output', help='new file only; never overwritten')
    args = parser.parse_args()
    print(json.dumps(patch(args.source, args.output), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, struct.error) as error:
        raise SystemExit('REFUSED: ' + str(error))
