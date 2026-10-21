"""GPT step of the write-usb.sh writer, on regular files (any OS with python3).

usage: python3 gpt-test.py <extracted write-usb.py> <work dir>

The writer's gpt_plan / gpt_write / gpt_check move the backup GPT of an image written to a larger disk to the end of
the disk. Here they run on plain files: the result is checked with tests/stick/gptlib.py (written separately), and
when sgdisk is installed it is compared byte for byte with what `sgdisk -e` makes of the same image. Malformed images
must be refused by gpt_plan before anything is written. Prints PASS/FAIL lines; exit 0 only when all pass.
"""
import hashlib
import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "stick"))
import gptlib  # noqa: E402

spec = importlib.util.spec_from_file_location("wusb", sys.argv[1])
wusb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wusb)
T = sys.argv[2]
fails = 0


def ok(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + detail))
    if not cond:
        fails += 1


def image(path, sectors):
    """A sparse file with a protective MBR, primary and backup GPT and three partitions (the shape of the KANO image:
    the last partition runs to the last usable sector)."""
    with open(path, "wb") as f:
        f.truncate(sectors * 512)
        gptlib.write_gpt(f, sectors, [
            dict(num=1, name="EFI", type="c12a7328-f81f-11d2-ba4b-00a0c93ec93b", first=2048, last=4095),
            dict(num=2, name="boot_a", type="0fc63daf-8483-4772-8e79-3d69d8477de4", first=4096, last=8191, attrs=1 << 48),
            dict(num=3, name="userdata", type="0fc63daf-8483-4772-8e79-3d69d8477de4", first=8192, last=sectors - 34),
        ])
        f.seek(5000 * 512)
        f.write(b"partition data" * 10)


def sparse_copy(src, dst, size):
    """Copy the data regions of a sparse file into a new sparse file of `size` bytes (holes stay holes)."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fo.truncate(size)
        end = os.fstat(fi.fileno()).st_size
        pos = 0
        while pos < end:
            try:
                pos = os.lseek(fi.fileno(), pos, os.SEEK_DATA)
                hole = os.lseek(fi.fileno(), pos, os.SEEK_HOLE)
            except (AttributeError, OSError):  # no SEEK_DATA here: copy the rest as it is
                hole = end
            fi.seek(pos)
            fo.seek(pos)
            left = hole - pos
            while left:
                b = fi.read(min(left, 1 << 20))
                fo.write(b)
                left -= len(b)
            pos = hole


def relocate(img, disk, disk_sectors):
    """Copy img to a larger sparse disk file and run the writer's GPT step on it; returns the plan."""
    sparse_copy(img, disk, disk_sectors * 512)
    ifd = os.open(img, os.O_RDONLY)
    plan = wusb.gpt_plan(ifd, os.fstat(ifd).st_size, disk_sectors * 512)
    os.close(ifd)
    wfd = os.open(disk, os.O_RDWR)
    wusb.gpt_write(wfd, plan, disk)
    wusb.gpt_check(wfd, plan, disk)
    os.close(wfd)
    return plan


def region(path, lba, count):
    with open(path, "rb") as f:
        f.seek(lba * 512)
        return f.read(count * 512)


def refused(name, path, disk_sectors, want):
    ifd = os.open(path, os.O_RDONLY)
    try:
        wusb.gpt_plan(ifd, os.fstat(ifd).st_size, disk_sectors * 512)
        ok(name, False, "(accepted)")
    except wusb.Stop as e:
        ok(name, want in str(e), "(%s)" % e)
    finally:
        os.close(ifd)


IMG_N = 32768            # 16 MiB image
DISK_N = 49152           # 24 MiB disk
img = os.path.join(T, "g.img")
image(img, IMG_N)
before = hashlib.sha256(open(img, "rb").read()).hexdigest()

disk = os.path.join(T, "g-disk.img")
relocate(img, disk, DISK_N)
try:
    h, parts = gptlib.read_gpt(disk)
    ok("after the GPT step the disk has a valid GPT with the backup at its last sector", h["alt"] == DISK_N - 1)
    ok("the usable area now reaches the new backup entries (as sgdisk -e)", h["last"] == DISK_N - 34, str(h["last"]))
    hi, pi = gptlib.read_gpt(img)
    ok("partitions, GUIDs and attributes are unchanged", parts == pi and h["disk"] == hi["disk"])
except ValueError as e:
    ok("after the GPT step the disk has a valid GPT with the backup at its last sector", False, str(e))
mbr = region(disk, 0, 1)
ok("the protective MBR covers the disk", struct.unpack_from("<I", mbr, 458)[0] == DISK_N - 1)
ok("the image part of the disk is unchanged apart from the MBR and primary header",
   region(disk, 2, IMG_N - 2) == region(img, 2, IMG_N - 2))
ok("the image file itself was only read", hashlib.sha256(open(img, "rb").read()).hexdigest() == before)

same = os.path.join(T, "g-same.img")
plan = relocate(img, same, IMG_N)
ok("a disk exactly the image's size is left as the image", open(same, "rb").read() == open(img, "rb").read())

if shutil.which("sgdisk"):
    # sgdisk -e recomputes the MBR's CHS end for small disks; past 8 GiB both saturate, so compare on sparse 9/10 GiB
    big_n, bigdisk_n = 9 << 21, 10 << 21
    big = os.path.join(T, "big.img")
    image(big, big_n)
    mine, theirs = os.path.join(T, "big-mine.img"), os.path.join(T, "big-sgdisk.img")
    relocate(big, mine, bigdisk_n)
    sparse_copy(big, theirs, bigdisk_n * 512)
    r = subprocess.run(["sgdisk", "-e", theirs], capture_output=True, text=True)
    ok("sgdisk -e runs on the reference copy", r.returncode == 0, r.stdout + r.stderr)
    for name, lba, count in (("MBR, primary header and entries", 0, 34), ("old backup", big_n - 33, 33),
                             ("new backup", bigdisk_n - 33, 33)):
        ok("byte for byte what sgdisk -e writes: " + name, region(mine, lba, count) == region(theirs, lba, count))
    r = subprocess.run(["sgdisk", "-v", mine], capture_output=True, text=True)
    ok("sgdisk -v finds no problems in the result", "No problems found" in r.stdout, r.stdout + r.stderr)
    for p in (big, mine, theirs):
        os.unlink(p)
else:
    print("SKIP comparison with sgdisk -e (sgdisk is not installed)")


# --- grow (write-usb.sh's default): userdata takes the free space at the end of the disk
def grown(img, disk, disk_sectors, fill=True):
    """Like relocate with grow=True; the disk beyond the image is filled with 0xA5 first (an old stick's contents)."""
    sparse_copy(img, disk, disk_sectors * 512)
    isz = os.path.getsize(img)
    if fill:
        with open(disk, "r+b") as f:
            f.seek(isz)
            left = disk_sectors * 512 - isz
            while left:
                k = min(left, 1 << 20)
                f.write(b"\xa5" * k)
                left -= k
    ifd = os.open(img, os.O_RDONLY)
    plan = wusb.gpt_plan(ifd, isz, disk_sectors * 512, grow=True)
    os.close(ifd)
    wfd = os.open(disk, os.O_RDWR)
    wusb.gpt_write(wfd, plan, disk)
    wusb.gpt_check(wfd, plan, disk)
    os.close(wfd)
    return plan


GDISK_N = 262144         # 128 MiB disk
# (a) userdata is the last partition: extended in place
g1 = os.path.join(T, "g-grow1.img")
plan = grown(img, g1, GDISK_N)
h, parts = gptlib.read_gpt(g1)
hi, pi = gptlib.read_gpt(img)
ok("grow: userdata that is the last partition keeps its start and ends at the last usable sector",
   parts[3]["first"] == pi[3]["first"] and parts[3]["last"] == GDISK_N - 34, str(parts[3]))
ok("grow: type, GUID, name and attributes of every partition are kept; only userdata's end moved",
   all(parts[k] == pi[k] for k in (1, 2)) and {k: v for k, v in parts[3].items() if k != "last"}
   == {k: v for k, v in pi[3].items() if k != "last"} and h["disk"] == hi["disk"])
ok("grow: the first 16 MiB of userdata are zeroed", region(g1, parts[3]["first"], 32768) == bytes(16 << 20))
ok("grow: the partitions before userdata are unchanged", region(g1, 2048, 6144) == region(img, 2048, 6144))
ok("grow: backup GPT at the last sector", h["alt"] == GDISK_N - 1)
os.unlink(g1)

# (b) the KANO layout: userdata is partition 1 in the middle, another partition (ota_recovery_b) after it
kimg = os.path.join(T, "k.img")
K_N = 65536              # 32 MiB image
with open(kimg, "wb") as f:
    f.truncate(K_N * 512)
    gptlib.write_gpt(f, K_N, [
        dict(num=1, name="userdata", type="0fc63daf-8483-4772-8e79-3d69d8477de4", first=40960, last=61439,
             guid="11111111-2222-3333-4444-555555555555", attrs=1 << 55),
        dict(num=2, name="EFI-SYSTEM", type="c12a7328-f81f-11d2-ba4b-00a0c93ec93b", first=2048, last=40959),
        dict(num=10, name="ota_recovery_b", type="0fc63daf-8483-4772-8e79-3d69d8477de4", first=61440, last=65400),
    ])
    f.seek(50000 * 512)
    f.write(b"old userdata" * 10)
g2 = os.path.join(T, "g-grow2.img")
plan = grown(kimg, g2, GDISK_N)
h, parts = gptlib.read_gpt(g2)
hk, pk = gptlib.read_gpt(kimg)
ud = parts[1]
ok("grow: userdata before another partition moves to the first 1 MiB boundary after the last partition and the image",
   ud["first"] == 65536 and ud["last"] == GDISK_N - 34, str(ud))
ok("grow: the moved userdata keeps its type, GUID, name and attributes",
   {k: v for k, v in ud.items() if k not in ("first", "last")} == {k: v for k, v in pk[1].items() if k not in ("first", "last")})
ok("grow: the other partitions are unchanged", parts[2] == pk[2] and parts[10] == pk[10] and len(parts) == len(pk))
ok("grow: the moved userdata starts with 16 MiB of zeros", region(g2, ud["first"], 32768) == bytes(16 << 20))
ok("grow: the image range of the disk is unchanged apart from the MBR and GPT",
   region(g2, 34, K_N - 34 - 33) == region(kimg, 34, K_N - 34 - 33))
ok("grow: the old stick contents after the zeroed range are left alone (not a full wipe)",
   region(g2, ud["first"] + 32768, 1) == b"\xa5" * 512)
if shutil.which("sgdisk"):
    r = subprocess.run(["sgdisk", "-v", g2], capture_output=True, text=True)
    ok("grow: sgdisk -v finds no problems", "No problems found" in r.stdout, r.stdout + r.stderr)
else:
    print("SKIP grow: sgdisk -v (sgdisk is not installed)")
os.unlink(g2)

# (c) a disk barely larger than the image: userdata is not grown (it would shrink); only the backup GPT moves
g3 = os.path.join(T, "g-grow3.img")
plan = grown(kimg, g3, K_N + 4096)
h, parts = gptlib.read_gpt(g3)
ok("grow: skipped when the free space is smaller than userdata (GPT moved as without grow)",
   plan["grow"] is None and parts == pk and h["alt"] == K_N + 4095)
os.unlink(g3)
ifd = os.open(kimg, os.O_RDONLY)
try:
    wusb.gpt_plan(ifd, K_N * 512, GDISK_N * 512, grow=True)
    p0 = wusb.gpt_plan(ifd, K_N * 512, GDISK_N * 512, grow=False)
    ok("no grow (--no-grow): the table is the image's", p0["grow"] is None and p0["table"] == region(kimg, 2, 32))
finally:
    os.close(ifd)
os.unlink(kimg)


def broken(name, edit, want, disk_sectors=DISK_N):
    p = os.path.join(T, "bad.img")
    sparse_copy(img, p, IMG_N * 512)
    with open(p, "r+b") as f:
        edit(f)
    refused("refuses an image with " + name, p, disk_sectors, want)


def poke(off, data):
    return lambda f: (f.seek(off), f.write(data))


broken("no protective MBR", poke(510, b"\0\0"), "protective MBR")
broken("a second MBR partition", poke(462, b"\x80" + b"\x01" * 15), "protective MBR")
broken("a primary GPT header CRC error", poke(512 + 40, b"\x01"), "CRC")
broken("a primary entry table CRC error", poke(2 * 512 + 100, b"\x01"), "entry table CRC")
broken("a damaged backup GPT", poke((IMG_N - 1) * 512 + 40, b"\x01"), "CRC")
broken("a backup GPT that is not at the last sector", poke((IMG_N - 1) * 512, b"\0" * 512), "no GPT header")


def outside(f):
    """Partition 3 ends past the last usable sector (headers and CRCs consistent)."""
    f.seek(512)
    hb = bytearray(f.read(512))
    f.seek(2 * 512)
    t = bytearray(f.read(16384))
    struct.pack_into("<Q", t, 2 * 128 + 40, IMG_N - 2)
    struct.pack_into("<I", hb, 88, zlib.crc32(t))
    struct.pack_into("<I", hb, 16, 0)
    struct.pack_into("<I", hb, 16, zlib.crc32(bytes(hb[:92])))
    f.seek(512)
    f.write(hb)
    f.seek(2 * 512)
    f.write(t)
    bb = bytearray(hb)
    for off, v in ((24, IMG_N - 1), (32, 1), (72, IMG_N - 33)):
        struct.pack_into("<Q", bb, off, v)
    struct.pack_into("<I", bb, 16, 0)
    struct.pack_into("<I", bb, 16, zlib.crc32(bytes(bb[:92])))
    f.seek((IMG_N - 33) * 512)
    f.write(t)
    f.write(bb)


broken("a partition outside the usable area", outside, "outside the usable area")
refused("refuses a disk smaller than the image", img, IMG_N - 1, "smaller than the image")
for p in (disk, same, img, os.path.join(T, "bad.img")):
    os.unlink(p)
print("gpt tests: %s" % ("all passed" if fails == 0 else "%d failed" % fails))
sys.exit(1 if fails else 0)
