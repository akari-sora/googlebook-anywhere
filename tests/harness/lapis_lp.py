"""Rebuild tests/harness/fixtures/lapis-super-lp.hex into the LP metadata bytes.

The public export refuses NUL bytes outside docs/images/*.png. The fixture is
stored as text: a size and sha256 header, then one line per contiguous non-zero
run (`<offset hex> <bytes hex>`). Zeros are the gaps between runs.
"""
import hashlib
from pathlib import Path


def decode(path):
    """Return the exact bytes. Checks the header size and sha256 before return."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise ValueError("lapis fixture: missing header")
    size_label, size_text = lines[0].split()
    sha_label, expect = lines[1].split()
    if size_label != "size" or sha_label != "sha256":
        raise ValueError("lapis fixture: bad header")
    size = int(size_text)
    if size < 0 or len(expect) != 64 or any(c not in "0123456789abcdef" for c in expect):
        raise ValueError("lapis fixture: bad size or sha256")
    buf = bytearray(size)
    filled = bytearray(size)
    for line in lines[2:]:
        if not line:
            raise ValueError("lapis fixture: blank line")
        off_text, hex_bytes = line.split()
        off = int(off_text, 16)
        data = bytes.fromhex(hex_bytes)
        if not data or any(b == 0 for b in data):
            raise ValueError("lapis fixture: run is empty or contains a NUL")
        end = off + len(data)
        if off < 0 or end > size:
            raise ValueError("lapis fixture: run outside the declared size")
        if any(filled[off:end]):
            raise ValueError("lapis fixture: overlapping run")
        buf[off:end] = data
        filled[off:end] = b"\x01" * len(data)
    raw = bytes(buf)
    if len(raw) != size:
        raise ValueError("lapis fixture: size mismatch")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expect:
        raise ValueError("lapis fixture: sha256 mismatch")
    return raw
