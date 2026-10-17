#!/usr/bin/env python3
"""tools/release/png-ok.py: the shipped screenshots pass (also against their pinned SHA-256); every way to hide bytes in
a PNG that a reviewer found fails; with --pinned, an unlisted or changed file fails even when its structure is fine."""
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools/release/png-ok.py"
passed = failed = 0


def run(path, pin=()):
    return subprocess.run([sys.executable, "-I", "-B", str(CHECK), *pin, str(path)], capture_output=True, text=True)


def expect(name, path, code, why="", pin=()):
    global passed, failed
    r = run(path, pin)
    good = r.returncode == code and (code == 0 or why in r.stdout)
    print(("PASS " if good else "FAIL ") + name + ("" if good else f": rc={r.returncode} {r.stdout.strip()}"))
    passed, failed = passed + good, failed + (not good)


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def png(w=2, h=2, ctype=2, extra_before_idat=b"", idat=None, iend=b"", after=b""):
    ch = 3 if ctype == 2 else 4
    raw = b"".join(b"\x00" + b"\x7f" * (w * ch) for _ in range(h))
    idat = zlib.compress(raw) if idat is None else idat
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)) + extra_before_idat
            + chunk(b"IDAT", idat) + chunk(b"IEND", iend) + after)


with tempfile.TemporaryDirectory() as td:
    t = Path(td)
    pins = ROOT / "tools/release/published-images.sha256"
    shipped = sorted((ROOT / "docs/images").glob("*.png"))
    for f in shipped:
        rel = str(f.relative_to(ROOT))
        expect(f"shipped {rel}", f, 0)
        expect(f"shipped {rel} matches its pinned SHA-256", f, 0, pin=("--pinned", str(pins), "--as", rel))
    listed = sorted(line.split("  ", 1)[1].strip() for line in pins.read_text().splitlines() if line.strip())
    good = listed == [str(f.relative_to(ROOT)) for f in shipped]
    print(("PASS " if good else "FAIL ") + "the pin list names exactly the shipped screenshots" + ("" if good else f": {listed}"))
    passed, failed = passed + good, failed + (not good)
    cases = {
        "minimal RGB": (png(), 0, ""),
        "minimal RGBA with sRGB and sBIT": (png(ctype=6, extra_before_idat=chunk(b"sRGB", b"\x00") + chunk(b"sBIT", b"\x08" * 4)), 0, ""),
        "pHYs chunk": (png(extra_before_idat=chunk(b"pHYs", b"\x00" * 9)), 1, "not allowed"),
        "gAMA chunk": (png(extra_before_idat=chunk(b"gAMA", b"\x00\x00\xb1\x8f")), 1, "not allowed"),
        "sRGB intent out of range": (png(extra_before_idat=chunk(b"sRGB", b"\x07")), 1, "rendering intent"),
        "sBIT value 0": (png(extra_before_idat=chunk(b"sBIT", b"\x08\x00\x08")), 1, "sBIT"),
        "sBIT value over 8": (png(extra_before_idat=chunk(b"sBIT", b"\x08\xff\x08")), 1, "sBIT"),
        "oversized sBIT": (png(extra_before_idat=chunk(b"sBIT", b"\x08" * 200)), 1, "length"),
        "tEXt chunk": (png(extra_before_idat=chunk(b"tEXt", b"Author\x00someone")), 1, "not allowed"),
        "eXIf chunk": (png(extra_before_idat=chunk(b"eXIf", b"MM\x00*")), 1, "not allowed"),
        "IEND with content": (png(iend=zlib.compress(b"secret")), 1, "IEND is not empty"),
        "no IDAT": (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)) + chunk(b"IEND", b""), 1, "no IDAT"),
        "IDAT is not image data": (png(idat=zlib.compress(b"x" * 100)), 1, "image data"),
        "second stream after the image": (png(idat=zlib.compress(b"".join(b"\x00" + b"\x7f" * 6 for _ in range(2))) + zlib.compress(b"secret")), 1, "image data"),
        "bytes after IEND": (png(after=b"secret"), 1, "after IEND"),
        "16-bit image": (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 16, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00" * 6)) + chunk(b"IEND", b""), 1, "8-bit"),
        "not a PNG": (b"not a png\x00", 1, "not a PNG"),
    }
    for name, (data, code, why) in cases.items():
        p = t / (name.replace(" ", "-") + ".png")
        p.write_bytes(data)
        expect(name, p, code, why)
    if shipped:
        rel = str(shipped[0].relative_to(ROOT))
        p = t / "valid-but-unpinned.png"
        p.write_bytes(png())
        expect("--pinned: a valid PNG under a name that is not in the list", p, 1, "is not in",
               pin=("--pinned", str(pins), "--as", "docs/images/new-shot.png"))
        expect("--pinned: a valid PNG that differs from the pinned file", p, 1, "differs from the pinned",
               pin=("--pinned", str(pins), "--as", rel))
        expect("--pinned without --as is a usage error", p, 2, "--pinned LIST --as RELPATH FILE", pin=("--pinned", str(pins)))
        expect("--pinned with two files is a usage error", p, 2, "--pinned LIST --as RELPATH FILE",
               pin=("--pinned", str(pins), "--as", rel, str(shipped[0])))
        expect("--pinned with an empty LIST is a usage error, not a skipped pin", shipped[0], 2, "usage",
               pin=("--pinned", "", "--as", "docs/images/not-reviewed.png"))
        expect("--pinned with an empty RELPATH is a usage error", shipped[0], 2, "usage", pin=("--pinned", str(pins), "--as", ""))
        r0 = subprocess.run([sys.executable, "-I", "-B", str(CHECK)], capture_output=True, text=True)
        good = r0.returncode == 2 and "usage:" in r0.stdout
        print(("PASS " if good else "FAIL ") + "no arguments is a usage error" + ("" if good else f": rc={r0.returncode} {r0.stdout.strip()}"))
        passed, failed = passed + good, failed + (not good)
        dup = t / "dup.sha256"
        dup.write_text(pins.read_text() + pins.read_text().splitlines()[0] + "\n")
        expect("--pinned: a list that names a path twice is refused", shipped[0], 1, "listed twice",
               pin=("--pinned", str(dup), "--as", str(shipped[0].relative_to(ROOT))))
print(f"png-ok tests: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
