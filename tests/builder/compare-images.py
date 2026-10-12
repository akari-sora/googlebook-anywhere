#!/usr/bin/env python3
"""Compare two Googlebook disk images and check the new one's boot binding.

usage: compare-images.py <reference.img> <new.img> [--allow-esp-diff path,path,...]

- both GPTs: valid header and entries CRC32; the same disk GUID and, for every partition, the same name, start, end,
  type GUID, unique GUID and attributes
- every partition except the ESP: same SHA-256
- the ESP file by file (FAT timestamps and cluster placement ignored); --allow-esp-diff lists files whose content is
  allowed to differ (intentional changes since the reference)
- the new image's ESP initrd bootconfig names the new image's own boot_a GUID and the SHA-256 of its vbmeta_a struct
Exit 0 only when everything matches.
"""
import hashlib, re, struct, subprocess, sys, tempfile, uuid, zlib
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("--")]
allowed = set()
for a in sys.argv[1:]:
    if a.startswith("--allow-esp-diff"):
        allowed |= set(filter(None, a.split("=", 1)[1].split(","))) if "=" in a else set()
if len(args) != 2: sys.exit(__doc__)
ref, new = args

def read(path, off, n):
    with open(path, "rb") as f:
        f.seek(off); b = f.read(n)
    if len(b) != n: sys.exit(f"{path}: short read at {off}")
    return b

def gpt(path):
    h = read(path, 512, 92)
    if h[:8] != b"EFI PART": sys.exit(f"{path}: no GPT")
    hsize = struct.unpack_from("<I", h, 12)[0]; hdr = read(path, 512, hsize)
    crc = struct.unpack_from("<I", hdr, 16)[0]
    ok_h = zlib.crc32(hdr[:16] + b"\0\0\0\0" + hdr[20:]) & 0xFFFFFFFF == crc
    disk = str(uuid.UUID(bytes_le=hdr[56:72]))
    lba, n, esz, ecrc = struct.unpack_from("<QIII", hdr, 72)
    t = read(path, lba * 512, n * esz)
    ok_e = zlib.crc32(t) & 0xFFFFFFFF == ecrc
    parts = {}
    for i in range(n):
        e = t[i * esz:(i + 1) * esz]
        if e[:16] == bytes(16): continue
        a, b, attrs = struct.unpack_from("<QQQ", e, 32)
        parts[i + 1] = dict(name=e[56:128].decode("utf-16le").rstrip("\0"), start=a, end=b, attrs=attrs,
                            type=str(uuid.UUID(bytes_le=e[:16])), guid=str(uuid.UUID(bytes_le=e[16:32])))
    return dict(header_crc_ok=ok_h, entries_crc_ok=ok_e, disk=disk, parts=parts)

def sha(path, start, length):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(start); left = length
        while left:
            b = f.read(min(left, 16 << 20))
            if not b: sys.exit(f"{path} ends before byte {start + length}")
            h.update(b); left -= len(b)
    return h.hexdigest()

def esp_files(img, off, dest):
    subprocess.run(["mcopy", "-s", "-n", "-i", f"{img}@@{off}", "::/", str(dest)], check=True,
                   env={"MTOOLS_SKIP_CHECK": "1", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    return {str(p.relative_to(dest)): p.read_bytes() for p in sorted(dest.rglob("*")) if p.is_file()}

diff = 0
def report(same, what):
    global diff
    print(("same" if same else "DIFF"), what); diff += not same

A, B = gpt(ref), gpt(new)
for tag, g in (("reference", A), ("new", B)):
    report(g["header_crc_ok"] and g["entries_crc_ok"], f"{tag} GPT header and entries CRC32")
report(A["disk"] == B["disk"], "disk GUID")
report(sorted(A["parts"]) == sorted(B["parts"]), "partition numbers")
for n in sorted(set(A["parts"]) & set(B["parts"])):
    pa, pb = A["parts"][n], B["parts"][n]
    report(pa == pb, f"GPT entry {n} {pa['name']} (name, start, end, type, GUID, attributes)")
    if pa["name"] == "EFI-SYSTEM" or (pa["start"], pa["end"]) != (pb["start"], pb["end"]): continue
    report(sha(ref, pa["start"] * 512, (pa["end"] + 1 - pa["start"]) * 512) ==
           sha(new, pb["start"] * 512, (pb["end"] + 1 - pb["start"]) * 512), f"partition {n} {pa['name']} content")
names = {v["name"]: v for v in B["parts"].values()}
esp_a = next(v for v in A["parts"].values() if v["name"] == "EFI-SYSTEM")["start"] * 512
esp_b = names["EFI-SYSTEM"]["start"] * 512
with tempfile.TemporaryDirectory() as t:
    fa = esp_files(ref, esp_a, Path(t) / "a"); fb = esp_files(new, esp_b, Path(t) / "b")
for k in sorted(set(fa) | set(fb)):
    same = fa.get(k) == fb.get(k)
    if not same and k in allowed and k in fa and k in fb:
        print("allowed-diff ESP", k); continue
    report(same, f"ESP {k}")
initrd = fb.get("initrd.img", b"")
m = re.search(rb"androidboot\.boot_part_uuid=([0-9a-f-]{36})", initrd)
report(bool(m) and m.group(1).decode() == names["boot_a"]["guid"], "new ESP initrd names the new image's boot_a GUID")
vb = names["vbmeta_a"]; head = read(new, vb["start"] * 512, 256)
auth, aux = struct.unpack_from(">QQ", head, 12)
digest = sha(new, vb["start"] * 512, 256 + auth + aux)
report(f"androidboot.vbmeta.digest={digest}".encode() in initrd, "new ESP initrd carries the SHA-256 of the new vbmeta_a")
print("IDENTICAL CONTENT" if not diff else f"{diff} differences")
sys.exit(1 if diff else 0)
