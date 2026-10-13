#!/usr/bin/env python3
"""png-ok.py [--pinned LIST --as RELPATH] <file>...: exit 0 only if every file is a plain screenshot PNG that can be
published.

With --pinned, the file must ALSO be listed in LIST (lines "<sha256>  <relpath>", tools/release/published-images.sha256)
under RELPATH with exactly that SHA-256: a structural check cannot prove that a compressed stream carries no hidden bits
(a zero-length stored deflate block ignores the padding bits before its LEN, RFC 1951 3.2.4), so the release and the
public export only accept screenshots a human has looked at and pinned. New screenshots: add them to the list in the
same commit (the review sees the new hash).

The public export and the release archive refuse binary files except PNGs in docs/images that pass this check. It
accepts exactly the shape of a screenshot: 8-bit RGB or RGBA, not interlaced; chunks IHDR, optionally sBIT / sRGB
(each with its exact specified length and valid values), one run of IDAT, and an EMPTY IEND; valid CRCs; the IDAT zlib stream
must decompress to exactly height * (1 + width * channels) bytes with valid filter bytes, end cleanly, and nothing may
follow it (no hidden payload in or after the image data). At most 2 MiB and 4096x4096. Text, EXIF, time and private
chunks are refused (they could carry personal data). The visible picture itself still needs a human look."""
import struct
import sys
import zlib

MAX_FILE = 2 << 20
MAX_SIDE = 4096
SIG = b"\x89PNG\r\n\x1a\n"


def check(path):
    with open(path, "rb") as f:
        data = f.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        return "larger than 2 MiB"
    if data[:8] != SIG:
        return "not a PNG"
    chunks, p = [], 8
    while p < len(data):
        if p + 12 > len(data):
            return "truncated chunk"
        n, = struct.unpack(">I", data[p:p + 4])
        kind = data[p + 4:p + 8]
        if p + 12 + n > len(data):
            return "truncated chunk"
        body = data[p + 8:p + 8 + n]
        crc, = struct.unpack(">I", data[p + 8 + n:p + 12 + n])
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            return f"bad CRC in {kind!r}"
        chunks.append((kind, body))
        p += 12 + n
        if kind == b"IEND":
            break
    if p != len(data):
        return "data after IEND"
    kinds = [k for k, _ in chunks]
    if not kinds or kinds[0] != b"IHDR" or kinds[-1] != b"IEND" or kinds.count(b"IHDR") != 1 or kinds.count(b"IEND") != 1:
        return "IHDR/IEND missing, repeated or misplaced"
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        return "IHDR is not 13 bytes"
    w, h, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr)
    if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE):
        return f"odd size {w}x{h}"
    if depth != 8 or ctype not in (2, 6) or comp != 0 or filt != 0 or interlace != 0:
        return "only 8-bit RGB/RGBA, non-interlaced PNGs are accepted"
    channels = 3 if ctype == 2 else 4
    if chunks[-1][1]:
        return "IEND is not empty"
    lengths = {b"sBIT": channels, b"sRGB": 1}
    idat = [i for i, k in enumerate(kinds) if k == b"IDAT"]
    if not idat:
        return "no IDAT"
    if idat != list(range(idat[0], idat[-1] + 1)):
        return "IDAT chunks are not consecutive"
    for i, (kind, body) in enumerate(chunks[1:-1], start=1):
        if kind == b"IDAT":
            continue
        if kind not in lengths:
            return f"chunk {kind.decode('latin-1')!r} is not allowed (only sBIT and sRGB besides the image data)"
        if len(body) != lengths[kind]:
            return f"chunk {kind.decode('latin-1')!r} has length {len(body)}, not {lengths[kind]}"
        if kinds.count(kind) != 1 or i > idat[0]:
            return f"chunk {kind.decode('latin-1')!r} is repeated or after the image data"
        if kind == b"sBIT" and not all(1 <= v <= 8 for v in body):
            return "sBIT values must be 1..8"
        if kind == b"sRGB" and body[0] > 3:
            return "sRGB rendering intent must be 0..3"
    stride = 1 + w * channels
    want = h * stride
    d = zlib.decompressobj()
    try:
        raw = d.decompress(b"".join(chunks[i][1] for i in idat), want + 1)
    except zlib.error as e:
        return f"image data does not decompress ({e})"
    if len(raw) != want or d.unconsumed_tail or not d.eof or d.unused_data:
        return "image data is not exactly one complete image stream (extra or missing bytes)"
    if any(raw[r * stride] > 4 for r in range(h)):
        return "invalid PNG filter byte"
    return None


def pinned(listfile, rel, path):
    import hashlib
    want = {}
    with open(listfile, encoding="ascii") as f:
        for line in f:
            h, _, name = line.rstrip("\n").partition("  ")
            if len(h) == 64 and name:
                if name in want:
                    return f"{name} is listed twice in {listfile}"
                want[name] = h
    if rel not in want:
        return f"{rel} is not in {listfile} (a human must look at a new screenshot and pin it)"
    with open(path, "rb") as f:
        got = hashlib.sha256(f.read(MAX_FILE + 1)).hexdigest()
    return None if got == want[rel] else f"{rel} differs from the pinned SHA-256 in {listfile}"


def main():
    args, bad = sys.argv[1:], 0
    listfile = rel = None
    if args[:1] == ["--pinned"]:
        if len(args) != 5 or args[2] != "--as" or not args[1] or not args[3]:
            print("usage: png-ok.py [--pinned LIST --as RELPATH] FILE...")
            return 2
        listfile, rel, args = args[1], args[3], args[4:]
    for f in args:
        why = check(f) or (pinned(listfile, rel, f) if listfile is not None else None)
        if why:
            print(f"{f}: {why}")
            bad = 1
    return bad


sys.exit(main())
