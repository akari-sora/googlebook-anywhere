#!/usr/bin/env python3
"""Self-test of install/gbos-update.py on small synthetic Googlebook disks (no root, no QEMU, no real disk).

usage: tests/update/selftest.py <empty work dir>
Needs python3, mtools (mformat, mcopy, mdir, mmd, mdel, minfo, mtype), lz4 and zstd.

A fake release has the real layout in miniature: a GPT with the slot partitions (a and b), super with Android LP
metadata (three metadata slots, groups for a and b, system_b holding "system_other" like the real images), an ESP with
kernel.efi, an initrd ending in a bootconfig, loader.conf and the three entries, a vbmeta with an AVB public key.
Every case checks the result byte for byte; every refusal checks that the disk did not change at all.
"""
import hashlib, os, random, shutil, struct, subprocess, sys, uuid, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = Path(os.environ.get("GBOS_UPDATE_ENGINE") or ROOT / "install/gbos-update.py")   # tests/update/mutate.py sets it
W = Path(sys.argv[1]).resolve()
if W.exists() and any(W.iterdir()):
    sys.exit(f"{W} must be empty")
MISSING_MTOOLS = [
    name for name in ("mformat", "mmd", "mcopy", "mdir", "mdel", "minfo", "mtype")
    if shutil.which(name) is None
]
if MISSING_MTOOLS:
    sys.exit(
        "needs mtools (package mtools: mformat, mmd, mcopy, mdir, mdel, minfo, mtype); missing: "
        + ", ".join(MISSING_MTOOLS)
    )
W.mkdir(parents=True, exist_ok=True)
ENV = dict(os.environ, MTOOLS_SKIP_CHECK="1", GBOS_UPDATE_TEST="1", PYTHONDONTWRITEBYTECODE="1")
MIB = 1 << 20
RESULTS = []


def ok(cond, what):
    RESULTS.append((bool(cond), what))
    print(("PASS " if cond else "FAIL ") + what, flush=True)
    return cond


def sh(*cmd, data=None):
    r = subprocess.run(list(map(str, cmd)), input=data, capture_output=True, env=ENV)
    if r.returncode:
        raise RuntimeError(f"{cmd} failed: {r.stderr.decode()[:300]}")
    return r.stdout


def rnd(seed, n):
    return random.Random(seed).randbytes(n)


# ------------------------------------------------------------------ fake release images

LAYOUT = [  # name, MiB (userdata last, at the tail)
    ("EFI-SYSTEM", 8), ("boot_a", 1), ("boot_b", 1), ("init_boot_a", 1), ("init_boot_b", 1), ("vendor_boot_a", 1),
    ("vendor_boot_b", 1), ("vbmeta_a", 1), ("vbmeta_b", 1), ("pvmfw_a", 1), ("pvmfw_b", 1), ("recovery_a", 1),
    ("recovery_b", 1), ("ota_recovery_a", 1), ("ota_recovery_b", 1), ("metadata", 1), ("misc", 1), ("frp_persist", 1),
    ("desktop_security_persist", 1), ("desktop_security_storage", 1), ("super", 40), ("userdata", 6)]
ESP_T = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
LIN_T = "0fc63daf-8483-4772-8e79-3d69d8477de4"
VBM_T = "88434509-d9d1-487d-b82c-15ef964cbd4b"
DYN = {"system": 4, "vendor": 2, "product": 2, "system_ext": 1, "system_dlkm": 1, "vendor_dlkm": 1}  # MiB
LIFT = "initcall_blacklist=nvme_init,mei_me_driver_init"
OPTS = "root=/dev/ram0 bootconfig module_blacklist=cros_ec,tpm,spi_intel " + LIFT + " efi=noruntime"


def gpt_bytes(parts, size, disk_guid):
    """protective MBR + primary GPT (first MiB) and backup GPT (last 33 sectors) for parts [(name, first, last, type, guid, attrs)]"""
    ents = bytearray(128 * 128)
    for i, (name, first, last, typ, guid, attrs) in enumerate(parts):
        e = uuid.UUID(typ).bytes_le + uuid.UUID(guid).bytes_le + struct.pack("<QQQ", first, last, attrs) + name.encode("utf-16le").ljust(72, b"\0")
        ents[i * 128:(i + 1) * 128] = e
    ecrc = zlib.crc32(ents)
    last_lba = size // 512 - 1

    def hdr(my, alt, elba):
        h = bytearray(92)
        struct.pack_into("<8sIIIIQQQQ16sQIII", h, 0, b"EFI PART", 0x10000, 92, 0, 0, my, alt, 2048, last_lba - 33,
                         uuid.UUID(disk_guid).bytes_le, elba, 128, 128, ecrc)
        struct.pack_into("<I", h, 16, zlib.crc32(h))
        return bytes(h).ljust(512, b"\0")
    mbr = bytearray(512)
    mbr[446:462] = bytes([0, 0, 2, 0, 0xEE, 0xFF, 0xFF, 0xFF]) + struct.pack("<II", 1, min(last_lba, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"
    head = bytes(mbr) + hdr(1, last_lba, 2) + bytes(ents)
    tail = bytes(ents) + hdr(last_lba, 1, last_lba - 32)
    return head, tail


def lp_super(size, data_by_part, other, group=None):
    """super with LP metadata (3 slots, groups default/a/b) holding <name>_a for each data_by_part and system_b=other."""
    mx, slots = 65536, 3
    geo = bytearray(52)
    struct.pack_into("<II32sIII", geo, 0, 0x616C4467, 52, bytes(32), mx, slots, 4096)
    geo[8:40] = hashlib.sha256(geo).digest()
    first = 2048
    parts, exts, pos = [], [], first
    order = list(data_by_part.items()) + [("system_b_other", other)]
    for name, data in order:
        nsec = len(data) // 512
        pname = name + "_a" if name != "system_b_other" else "system_b"
        parts.append((pname, pos, nsec, data))
        pos += -(-nsec // 2048) * 2048
    allnames = [n + "_a" for n in data_by_part] + [n + "_b" for n in data_by_part]
    ptab, etab = bytearray(), bytearray()
    for n in allnames:
        got = [p for p in parts if p[0] == n]
        gi = 1 if n.endswith("_a") else 2
        if got:
            ptab += n.encode().ljust(36, b"\0") + struct.pack("<IIII", 1, len(etab) // 24, 1, gi)
            etab += struct.pack("<QIQI", got[0][2], 0, got[0][1], 0)
        else:
            ptab += n.encode().ljust(36, b"\0") + struct.pack("<IIII", 1, len(etab) // 24, 0, gi)
    groups = b"".join(n.encode().ljust(36, b"\0") + struct.pack("<IQ", 0, m) for n, m in
                      (("default", 0), ("google_dynamic_partitions_a", group or size - MIB), ("google_dynamic_partitions_b", group or size - MIB)))
    bdev = struct.pack("<QIIQ", first, MIB, 0, size) + b"super".ljust(36, b"\0") + struct.pack("<I", 0)
    tables = bytes(ptab) + bytes(etab) + groups + bdev
    h = bytearray(256)
    struct.pack_into("<IHHI", h, 0, 0x414C5030, 10, 2, 256)
    struct.pack_into("<I", h, 44, len(tables))
    h[48:80] = hashlib.sha256(tables).digest()
    o = 0
    for at, num, esz in ((80, len(ptab) // 52, 52), (92, len(etab) // 24, 24), (104, 3, 48), (116, 1, 64)):
        struct.pack_into("<III", h, at, o, num, esz)
        o += num * esz
    struct.pack_into("<I", h, 128, 1)
    h[12:44] = hashlib.sha256(h).digest()
    blob = bytes(h) + tables
    sup = bytearray(size)
    sup[4096:4096 + 52] = geo
    sup[8192:8192 + 52] = geo
    base = 12288
    for s in range(slots):
        for off in (base + s * mx, base + slots * mx + s * mx):
            sup[off:off + len(blob)] = blob
    for name, a, nsec, data in parts:
        sup[a * 512:a * 512 + len(data)] = data
    return bytes(sup)


def newc(files):
    out = bytearray()
    for i, (name, data) in enumerate(files + [("TRAILER!!!", b"")]):
        nm = name.encode() + b"\0"
        fields = [i + 1, 0o100644 if data or name != "TRAILER!!!" else 0, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(nm), 0]
        out += b"070701" + b"".join(b"%08X" % v for v in fields) + nm
        out += b"\0" * ((-len(out)) % 4) + data + b"\0" * ((-len(data)) % 4)
    return bytes(out)


def bootconfig(lines):
    bc = ("\n".join(lines) + "\n").encode()
    return bc + struct.pack("<II", len(bc), sum(bc) & 0xFFFFFFFF) + b"#BOOTCONFIG\n"


def avb(key_seed):
    pk = rnd("key" + key_seed, 520)
    aux = pk + bytes((-len(pk)) % 64)
    h = bytearray(256)
    h[:4] = b"AVB0"
    struct.pack_into(">II", h, 4, 1, 0)
    struct.pack_into(">QQ", h, 12, 0, len(aux))
    struct.pack_into(">QQ", h, 64, 0, len(pk))
    return bytes(h) + aux


def make_release(path, build=16452207, key="k1", vendor="v1", kernel="kern1", model="KANO", keymint="rust_nonsecure",
                 super_mib=40, esp_mib=8, kernel_kib=256, big=None, dyn_extra=0, plain=True, new_dyn=None, group_mib=None):
    lay = [(n, super_mib if n == "super" else esp_mib if n == "EFI-SYSTEM" else m) for n, m in LAYOUT]
    size = (2 + sum(m for _, m in lay)) * MIB
    guids = {n: str(uuid.UUID(bytes=rnd(f"guid{n}", 16), version=4)) for n, _ in lay}
    parts, pos = [], 2048
    for n, m in lay:
        typ = ESP_T if n == "EFI-SYSTEM" else VBM_T if n.startswith("vbmeta") else LIN_T
        parts.append((n, pos, pos + m * 2048 - 1, typ, guids[n], 0x010F000000000000 if n == "vbmeta_b" else 0))
        pos += m * 2048
    head, tail = gpt_bytes(parts, size, str(uuid.UUID(int=7)))
    img = bytearray(size)
    img[:len(head)] = head
    img[-len(tail):] = tail
    start = {n: a * 512 for n, a, *_ in parts}
    for n, m in lay:
        if n in ("EFI-SYSTEM", "super", "userdata"):
            continue
        d = avb(key) if n == "vbmeta_a" else rnd(f"{n}-{vendor if n.endswith('_a') else 'stock'}", m * MIB)
        img[start[n]:start[n] + len(d)] = d
    dyn = {k: rnd(f"{k}-{vendor if k == 'vendor' else 'sys'}", (v + (dyn_extra if k == 'product' else 0)) * MIB) for k, v in DYN.items()}
    if big:
        dyn["product"] = rnd("big", big * MIB)
    if new_dyn:
        dyn[new_dyn] = rnd(new_dyn, MIB)
    sup = lp_super(super_mib * MIB, dyn, rnd("other", 4 * MIB), group_mib * MIB if group_mib else None)
    img[start["super"]:start["super"] + len(sup)] = sup
    # ESP
    esp = W / "esp-build.img"
    esp.unlink(missing_ok=True)
    with open(esp, "wb") as f:
        f.truncate(esp_mib * MIB)
    sh("mformat", "-i", esp, "-v", "ESP", "-T", esp_mib * 2048, "-h", 2, "-s", 32, "::")
    fp = f"ASUS/lapis/lapis:17/CL3B.260622.271.R1/{build}:user/release-keys"
    rd = newc([("system/etc/ramdisk/build.prop", f"ro.bootimage.build.fingerprint={fp}\n".encode()),
               ("lib/modules/x.ko", rnd("ko" + vendor, 300 * 1024))])
    bc = bootconfig(["androidboot.hardware=android-desktop", "androidboot.slot_suffix=_a",
                     f"androidboot.boot_part_uuid={guids['boot_a']}", "androidboot.vbmeta.size=6720",
                     "androidboot.vbmeta.digest=" + hashlib.sha256(vendor.encode()).hexdigest(),
                     "androidboot.hypervisor.protected_vm.supported=0",
                     f"androidboot.vendor.apex.com.android.hardware.keymint=com.android.hardware.keymint.{keymint}",
                     "androidboot.vendor.apex.com.android.hardware.gatekeeper=com.android.hardware.gatekeeper.nonsecure"])
    tmp = W / "f"
    tmp.write_bytes(rd + bc); sh("mcopy", "-i", esp, tmp, "::/initrd.img")
    tmp.write_bytes(rnd(kernel, kernel_kib * 1024)); sh("mcopy", "-i", esp, tmp, "::/kernel.efi")
    sh("mmd", "-i", esp, "::/loader"); sh("mmd", "-i", esp, "::/loader/entries"); sh("mmd", "-i", esp, "::/EFI")
    tmp.write_bytes(b"default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n"); sh("mcopy", "-i", esp, tmp, "::/loader/loader.conf")
    for n, title in (("lapis-quiet.conf", "quiet"), ("lapis-debug.conf", "safe serial-debug"), ("lapis.conf", "plain"))[:3 if plain else 2]:
        tmp.write_bytes(f"title Googlebook {model} {title}\nlinux /kernel.efi\ninitrd /initrd.img\noptions {OPTS} x={title}\n".encode())
        sh("mcopy", "-i", esp, tmp, f"::/loader/entries/{n}")
    e = esp.read_bytes()
    img[start["EFI-SYSTEM"]:start["EFI-SYSTEM"] + len(e)] = e
    Path(path).write_bytes(img)
    Path(str(path) + ".sha256").write_text(hashlib.sha256(img).hexdigest() + f"  {Path(path).name}\n")
    return dict(start=start, guids=guids, dyn=dyn, rd=rd, bc=bc, layout=dict(lay))


def run(*args, env=None):
    r = subprocess.run([sys.executable, str(ENGINE), *map(str, args)], capture_output=True, text=True, env=env or ENV)
    return r.returncode, r.stdout + r.stderr


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def esp_read(img, start, path):
    return sh("mtype", "-i", f"{img}@@{start}", "::" + path)


def esp_has(img, start, path):
    return subprocess.run(["mdir", "-b", "-i", f"{img}@@{start}", "::" + path], capture_output=True, env=ENV).returncode == 0


def region(img, off, n):
    with open(img, "rb") as f:
        f.seek(off)
        return f.read(n)


def menu_slots(img, start):
    """{entry name: slot of the initrd it starts, or None} for the menu of a FAT at img@@start"""
    out = {}
    for line in subprocess.run(["mdir", "-b", "-i", f"{img}@@{start}", "::/loader/entries"], capture_output=True, text=True, env=ENV).stdout.splitlines():
        n = line.strip().rstrip("/").split("/")[-1]
        if not n.endswith(".conf"):
            continue
        e = gbu.entry_parse(esp_read(img, start, "/loader/entries/" + n).decode())
        try:
            _, bc = gbu.split_initrd(esp_read(img, start, e["initrd"][-1]))
            out[n] = gbu.bc_get(bc, "androidboot.slot_suffix").lstrip("_")
        except Exception:                   # noqa: BLE001 (a missing or damaged initrd)
            out[n] = None
    return out


def files_of(img, start, name):
    e = gbu.entry_parse(esp_read(img, start, "/loader/entries/" + name).decode())
    return [e["linux"], *e["initrd"]]


eng = {}
spec = __import__("importlib.util").util.spec_from_file_location("gbu", ENGINE)
gbu = __import__("importlib.util").util.module_from_spec(spec)
spec.loader.exec_module(gbu)

# ------------------------------------------------------------------ cases
print("== building fake releases", flush=True)
N = W / "n.img"; info_n = make_release(N, vendor="v1")
N1 = W / "n1.img"; info_n1 = make_release(N1, vendor="v2")
N2 = W / "n2.img"; info_n2 = make_release(N2, vendor="v3", kernel="kern3")
D = W / "disk.img"
shutil.copy(N, D)

rc, out = run("inspect", D)
ok(rc == 0 and "current slot a" in out and "google build 16452207" in out and "model KANO" in out, "inspect: a fresh install is slot a, build and model read")
before = sha(D)
rc, out = run("plan", D, "--image", N1)
ok(rc == 0 and "PLAN OK" in out and "previous version kept on this disk's boot menu: yes" in out and sha(D) == before,
   "plan: nothing written, previous version fits")

# refusals: nothing written
def refused(tag, rel, *extra, want="REFUSED"):
    b = sha(D)
    rc, out = run("apply", D, "--image", rel, "--mode", "stick", *extra)
    ok(rc == 2 and want in out and "nothing was written" in out and sha(D) == b, f"refuses {tag}; the disk is unchanged")
    return out

make_release(W / "key2.img", vendor="v2", key="k2"); refused("another AVB key", W / "key2.img")
make_release(W / "model.img", vendor="v2", model="OTHER"); refused("another model", W / "model.img")
make_release(W / "old.img", vendor="v2", build=16452100); refused("an older Google build", W / "old.img")
make_release(W / "km.img", vendor="v2", keymint="other"); refused("another KeyMint selection", W / "km.img")
make_release(W / "lay.img", vendor="v2", super_mib=48); refused("another partition layout", W / "lay.img")
make_release(W / "big.img", vendor="v2", big=20); refused("a release that does not fit in super", W / "big.img")
make_release(W / "newdyn.img", vendor="v2", new_dyn="odm"); refused("a release with a new dynamic partition", W / "newdyn.img", want="new dynamic partition")
Dgrp = W / "grp.img"; make_release(Dgrp, vendor="v1", group_mib=8)
b = sha(Dgrp); rc, out = run("apply", Dgrp, "--image", N1, "--mode", "stick")
ok(rc == 2 and "larger than the slot's group" in out and sha(Dgrp) == b, "refuses a release larger than the slot's group in super; the disk is unchanged")
b = sha(D); rc, out = run("apply", D, "--image", N1, "--mode", "stick", "--bytes", str(os.path.getsize(D) + 512))
ok(rc == 2 and "not the confirmed" in out and sha(D) == b, "refuses a disk whose size is not the confirmed --bytes; the disk is unchanged")
Dbpb = W / "bpb.img"; shutil.copy(N, Dbpb)
with open(Dbpb, "r+b") as fh:             # the ESP's FAT claims more sectors than its partition has
    e0 = info_n["start"]["EFI-SYSTEM"]
    fh.seek(e0 + 19); fh.write(b"\0\0"); fh.seek(e0 + 32); fh.write(struct.pack("<I", 1 << 22))
b = sha(Dbpb); rc, out = run("apply", Dbpb, "--image", N1, "--mode", "stick")
ok(rc == 2 and "its FAT says" in out and sha(Dbpb) == b, "refuses an ESP whose FAT reaches past its partition; the disk is unchanged")
shutil.copy(N1, W / "bad.img"); shutil.copy(str(N1) + ".sha256", str(W / "bad.img") + ".sha256")
with open(W / "bad.img", "r+b") as f:
    f.seek(info_n1["start"]["vendor_boot_a"] + 5); f.write(b"X")
refused("a release that fails its .sha256", W / "bad.img")
os.unlink(str(W / "bad.img") + ".sha256"); refused("a release without .sha256", W / "bad.img")
b = sha(D); rc, out = run("apply", D, "--image", N1)
ok(rc == 2 and sha(D) == b, "refuses apply without --mode")

# the update itself
keep_ranges = {n: (info_n["start"][n], info_n["layout"][n] * MIB) for n in
               ("userdata", "metadata", "misc", "frp_persist", "desktop_security_persist", "desktop_security_storage",
                "boot_a", "init_boot_a", "vendor_boot_a", "vbmeta_a", "pvmfw_a", "recovery_a", "ota_recovery_a")}
# a "user's /data": something in userdata and metadata that must survive
with open(D, "r+b") as f:
    f.seek(info_n["start"]["userdata"]); f.write(b"USERDATA-MARKER" * 100)
    f.seek(info_n["start"]["metadata"]); f.write(b"METADATA-MARKER" * 10)
keep_sha = {n: hashlib.sha256(region(D, a, l)).hexdigest() for n, (a, l) in keep_ranges.items()}
head_sha = hashlib.sha256(region(D, 0, MIB)).hexdigest()
tail_sha = hashlib.sha256(region(D, os.path.getsize(D) - 33 * 512, 33 * 512)).hexdigest()
lp0 = gbu.LP.read(lambda o, n: region(D, info_n["start"]["super"] + o, n), info_n["layout"]["super"] * MIB)
a_ext = {p["name"]: p["extents"] for p in lp0.parts if p["name"].endswith("_a")}
a_data = {n: hashlib.sha256(b"".join(region(D, info_n["start"]["super"] + x * 512, c * 512) for x, c in e)).hexdigest()
          for n, e in a_ext.items()}

rc, out = run("apply", D, "--image", N1, "--mode", "stick")
print(out)
ok(rc == 0 and "UPDATE OK: slot b" in out, "apply: same Google build, slot b updated")
ok(all(hashlib.sha256(region(D, a, l)).hexdigest() == keep_sha[n] for n, (a, l) in keep_ranges.items()),
   "data partitions (userdata, metadata, misc, frp_persist, desktop_security_*) and slot a's partitions untouched")
ok(hashlib.sha256(region(D, 0, MIB)).hexdigest() == head_sha and
   hashlib.sha256(region(D, os.path.getsize(D) - 33 * 512, 33 * 512)).hexdigest() == tail_sha, "partition table untouched")
S = info_n["start"]["super"]
lp1 = gbu.LP.read(lambda o, n: region(D, S + o, n), info_n["layout"]["super"] * MIB)
ok(lp1.consistent and all(lp1.part(n)["extents"] == e for n, e in a_ext.items()), "super: every metadata copy valid and equal; slot a's extents unchanged")
ok(all(hashlib.sha256(b"".join(region(D, S + x * 512, c * 512) for x, c in a_ext[n])).hexdigest() == h for n, h in a_data.items()),
   "super: slot a's data unchanged")
ok(all(b"".join(region(D, S + x * 512, c * 512) for x, c in lp1.part(f"{k}_b")["extents"]) == v for k, v in info_n1["dyn"].items()),
   "super: slot b holds the new release's dynamic partitions")
ok(all(region(D, info_n["start"][f"{p}_b"], MIB) == region(N1, info_n1["start"][f"{p}_a"], MIB) for p in gbu.SLOT_PHYS),
   "slot b's boot/init_boot/vendor_boot/vbmeta/pvmfw/recovery/ota_recovery = the release's slot a")
E = info_n["start"]["EFI-SYSTEM"]
q = esp_read(D, E, "/loader/entries/lapis-quiet.conf").decode()
pq = esp_read(D, E, "/loader/entries/prev-lapis-quiet.conf").decode()
ok("initrd /gbos/b/initrd.img" in q and "linux /kernel.efi" in q and "title Googlebook KANO quiet\n" in q,
   "menu: lapis-quiet.conf starts slot b (kernel shared: same kernel)")
ok("title Googlebook KANO quiet (previous version)" in pq and "initrd /initrd.img" in pq, "menu: prev-lapis-quiet.conf starts slot a")
ird = esp_read(D, E, "/gbos/b/initrd.img")
rd, bc = gbu.split_initrd(ird)
ok(rd[:4] == gbu.LZ4_LEGACY and gbu.ramdisk_plain(rd) == info_n1["rd"], "slot b initrd: the release's ramdisk, lz4 (legacy)")
ok(gbu.bc_get(bc, "androidboot.slot_suffix") == "_b" and gbu.bc_get(bc, "androidboot.boot_part_uuid") == info_n["guids"]["boot_a"]
   and gbu.bc_get(bc, "androidboot.vbmeta.digest") == hashlib.sha256(b"v2").hexdigest(), "slot b bootconfig: slot _b, this disk's boot_a, the release's vbmeta digest")
rc, out = run("inspect", D)
ok("current slot b" in out and "previous slot on the menu: a" in out, "inspect after the update: current b, previous a")

# second update: back into slot a, new kernel
rc, out = run("apply", D, "--image", N2, "--mode", "stick")
print(out)
ok(rc == 0 and "UPDATE OK: slot a" in out, "second update (new kernel) goes into slot a")
q = esp_read(D, E, "/loader/entries/lapis-quiet.conf").decode()
pq = esp_read(D, E, "/loader/entries/prev-lapis-quiet.conf").decode()
ok("linux /gbos/a/kernel.efi" in q and "initrd /gbos/a/initrd.img" in q and "initrd /gbos/b/initrd.img" in pq,
   "menu: current a with its own kernel, previous b")
ok(not esp_has(D, E, "/initrd.img"), "the first release's /initrd.img (old slot a) is gone")
ok(esp_has(D, E, "/kernel.efi"), "/kernel.efi stays: slot b (previous) still uses it")
lp2 = gbu.LP.read(lambda o, n: region(D, S + o, n), info_n["layout"]["super"] * MIB)
ok(all(lp2.part(n)["extents"] == lp1.part(n)["extents"] for n in [p["name"] for p in lp1.parts if p["name"].endswith("_b")]),
   "second update: slot b's extents unchanged")
ok(all(hashlib.sha256(region(D, a, l)).hexdigest() == keep_sha[n] for n, (a, l) in keep_ranges.items() if not n.endswith("_a")),
   "second update: data partitions still untouched")

# rollback and back
rc, out = run("rollback", D)
ok(rc == 0 and "ROLLBACK OK: slot b" in out, "rollback: slot b starts by default again")
rc, out = run("inspect", D)
ok("current slot b" in out and "previous slot on the menu: a" in out, "inspect after rollback")
rc, out = run("rollback", D)
ok(rc == 0 and "ROLLBACK OK: slot a" in out, "rollback twice: back to slot a")

# interrupted updates finish on a rerun, the running slot stays bootable meanwhile
for stop in ("removed:", "dynamic:", "physical:", "metadata:", "bootfiles:"):
    shutil.copy(N, D)
    env = dict(ENV, GBOS_UPDATE_TEST_FAIL_AT=stop[:-1])
    rc, out = run("apply", D, "--image", N1, "--mode", "stick", env=env)
    q = esp_read(D, E, "/loader/entries/lapis-quiet.conf").decode()
    r2, o2 = run("inspect", D)
    ok(rc == 1 and "initrd /initrd.img" in q and "update in progress: b" in o2, f"stopped after '{stop}': slot a still the default, state says b")
    ok(not [n for n, sl in menu_slots(D, E).items() if sl == "b"], f"stopped after '{stop}': no entry of the menu starts slot b")
    if stop == "dynamic:":
        rr, ro = run("rollback", D)
        ok(rr == 2 and "stopped halfway" in ro, "rollback is refused while an update is unfinished")
    rc, out = run("apply", D, "--image", N1, "--mode", "stick")
    ok(rc == 0 and "UPDATE OK: slot b" in out, f"stopped after '{stop}': the rerun finishes the update")

# from a release with the plain lapis.conf (v0.1) to one without it (v0.2 and later)
D3 = W / "disk3.img"; shutil.copy(N, D3)
N3 = W / "n3.img"; make_release(N3, vendor="v4", plain=False)
rc, out = run("apply", D3, "--image", N3, "--mode", "stick")
ok(rc == 0 and "UPDATE OK: slot b" in out, "update to a release without lapis.conf: slot b updated")
q3 = esp_read(D3, E, "/loader/entries/lapis-quiet.conf").decode()
ok("initrd /gbos/b/initrd.img" in q3 and esp_has(D3, E, "/loader/entries/prev-lapis-quiet.conf")
   and esp_has(D3, E, "/loader/entries/prev-lapis-debug.conf"), "... quiet starts slot b; quiet and serial-debug of slot a are the previous version")
ok(not esp_has(D3, E, "/loader/entries/lapis.conf") and not esp_has(D3, E, "/loader/entries/prev-lapis.conf"),
   "... the old plain entry is gone from the menu (no lapis.conf, no prev-lapis.conf)")
rc, out = run("inspect", D3)
ok(rc == 0 and "current slot b" in out and "previous slot on the menu: a" in out, "... inspect: current b, previous a")
rc, out = run("rollback", D3)
ok(rc == 0 and "ROLLBACK OK: slot a" in out and not esp_has(D3, E, "/loader/entries/lapis.conf"), "... rollback to slot a works without lapis.conf")

# NVMe mode with the installer stick's boot partition
shutil.copy(N, D)
for n in ("lapis-quiet.conf", "lapis-debug.conf", "lapis.conf"):    # an internal disk: lifted entries, as gbos-install.sh writes them
    t = esp_read(D, E, "/loader/entries/" + n).decode().replace(LIFT, "initcall_blacklist=mei_me_driver_init")
    (W / "f").write_text(t); sh("mcopy", "-o", "-i", f"{D}@@{E}", W / "f", "::/loader/entries/" + n)
X = W / "xboot.img"
with open(X, "wb") as f:
    f.truncate(16 * MIB)
sh("mformat", "-i", X, "-v", "XBOOT", "-T", 32768, "-h", 2, "-s", 32, "::")
sh("mmd", "-i", X, "::/loader"); sh("mmd", "-i", X, "::/loader/entries"); sh("mmd", "-i", X, "::/gbos-nvme")
(W / "f").write_bytes(esp_read(D, E, "/kernel.efi")); sh("mcopy", "-i", X, W / "f", "::/gbos-nvme/kernel.efi")
(W / "f").write_bytes(esp_read(D, E, "/initrd.img")); sh("mcopy", "-i", X, W / "f", "::/gbos-nvme/initrd.img")
(W / "f").write_text("title Googlebook (internal disk)\nlinux /gbos-nvme/kernel.efi\ninitrd /gbos-nvme/initrd.img\noptions x\n")
sh("mcopy", "-i", X, W / "f", "::/loader/entries/gbos-nvme.conf")
rc, out = run("apply", D, "--image", N1, "--mode", "nvme", "--stick-boot", X)
print(out)
ok(rc == 0 and "UPDATE OK: slot b" in out, "nvme mode: update OK")
q = esp_read(D, E, "/loader/entries/lapis-quiet.conf").decode()
ok("initcall_blacklist=mei_me_driver_init" in q and "nvme_init" not in q and "module_blacklist=cros_ec,tpm,spi_intel" in q,
   "nvme mode: the new entries lift only the NVMe initcall block")
xs = esp_read(X, 0, "/loader/entries/gbos-nvme.conf").decode()
xp = esp_read(X, 0, "/loader/entries/gbos-nvme-prev.conf").decode()
ok("initrd /gbos-nvme/b/initrd.img" in xs and "initrd /gbos-nvme/a/initrd.img" in xp and "previous version" in xp,
   "nvme mode: the stick's menu starts slot b, its previous-version entry slot a")
ok(esp_read(X, 0, "/gbos-nvme/a/initrd.img") == esp_read(N, E, "/initrd.img") and not esp_has(X, 0, "/gbos-nvme/initrd.img"),
   "nvme mode: the stick keeps a copy of slot a's files; the old top-level copy is gone")

# NVMe + installer stick, the SECOND update (slot b -> a): stop after every step, then rerun (review I1)
Dn1, Xn1 = W / "nvme-after1.img", W / "xboot-after1.img"
shutil.copy(D, Dn1); shutil.copy(X, Xn1)               # the state after the first update: running b, previous a
N2b = W / "n2b.img"; make_release(N2b, vendor="v5")
for stop in ("stickprep:", "removed:", "dynamic:", "physical:", "metadata:", "bootfiles:", "stick:"):
    Dw, Xw = W / "nvme-w.img", W / "xboot-w.img"
    shutil.copy(Dn1, Dw); shutil.copy(Xn1, Xw)
    rc, out = run("apply", Dw, "--image", N2b, "--mode", "nvme", "--stick-boot", Xw, env=dict(ENV, GBOS_UPDATE_TEST_FAIL_AT=stop[:-1]))
    dm, xm = menu_slots(Dw, E), menu_slots(Xw, 0)
    # before 'removed' slot a is untouched, so its previous-version entries may still be there; from 'removed' on none
    ok(rc == 1 and dm.get("lapis-quiet.conf") == "b"
       and not [n for n, sl in dm.items() if sl == "a" and (stop != "stickprep:" or not n.startswith("prev-"))],
       f"2nd internal-disk update stopped after '{stop}': the disk's menu starts b, nothing starts a (being written)")
    if stop == "stick:":
        ok(xm.get("gbos-nvme.conf") == "a" and xm.get("gbos-nvme-prev.conf") == "b",
           f"... stick after '{stop}': 'internal disk' starts the finished slot a, 'previous version' still b")
    else:
        ok(xm.get("gbos-nvme-prev.conf") == "b" and not [n for n, sl in xm.items() if sl == "a"],
           f"... stick after '{stop}': 'previous version' starts b, no stick entry starts a")
    rc, out = run("apply", Dw, "--image", N2b, "--mode", "nvme", "--stick-boot", Xw)
    dm, xm = menu_slots(Dw, E), menu_slots(Xw, 0)
    ok(rc == 0 and "UPDATE OK: slot a" in out and dm.get("lapis-quiet.conf") == "a" and dm.get("prev-lapis-quiet.conf") == "b"
       and xm.get("gbos-nvme.conf") == "a" and xm.get("gbos-nvme-debug.conf") == "a" and xm.get("gbos-nvme-prev.conf") == "b",
       f"... the rerun after '{stop}' finishes: disk and stick start a, '(previous version)' b on both")
    ok(all(esp_has(Xw, 0, x) for n in ("gbos-nvme.conf", "gbos-nvme-prev.conf") for x in files_of(Xw, 0, n))
       and esp_read(Xw, 0, "/gbos-nvme/b/initrd.img") == esp_read(Dn1, E, "/gbos/b/initrd.img"),
       f"... the stick keeps an exact copy of slot b's files for its previous-version entry")
# rollback with the stick: both menus swap
shutil.copy(Dn1, W / "rb.img"); shutil.copy(Xn1, W / "rbx.img")
rc, out = run("rollback", W / "rb.img", "--stick-boot", W / "rbx.img")
dm, xm = menu_slots(W / "rb.img", E), menu_slots(W / "rbx.img", 0)
ok(rc == 0 and dm.get("lapis-quiet.conf") == "a" and xm.get("gbos-nvme.conf") == "a" and xm.get("gbos-nvme-prev.conf") == "b"
   and xm.get("gbos-nvme-debug.conf") == "a", "rollback --stick-boot: the disk's and the stick's menus start a, previous version b")
# two installer sticks: a stick's own copy of the running slot can be older than what the slot now holds (another
# stick updated the disk since); the copy for 'previous version' comes from the internal disk
Dts, Xold, Xnew = W / "two.img", W / "two-x1.img", W / "two-x2.img"
shutil.copy(Dn1, Dts); shutil.copy(Xn1, Xold)          # running b; Xold: 'internal disk' b, 'previous version' a (v1)
with open(Xnew, "wb") as fh:
    fh.truncate(16 * MIB)
sh("mformat", "-i", Xnew, "-v", "XNEW", "-T", 32768, "-h", 2, "-s", 32, "::")
sh("mmd", "-i", Xnew, "::/loader"); sh("mmd", "-i", Xnew, "::/loader/entries")
r1, _ = run("apply", Dts, "--image", N2b, "--mode", "nvme", "--stick-boot", Xnew)     # the other stick: b -> a (v5)
# ... and that stick updates the disk once more (a -> b): the old stick's menu has the slots right, its files not
Dc, Yc, Xc = W / "two-c.img", W / "two-yc.img", W / "two-xc.img"
shutil.copy(Dts, Dc); shutil.copy(Xnew, Yc); shutil.copy(Xn1, Xc)
make_release(W / "n7.img", vendor="v7")
rc, _ = run("apply", Dc, "--image", W / "n7.img", "--mode", "nvme", "--stick-boot", Yc)
b = sha(Dc); rr, ro = run("rollback", Dc, "--stick-boot", Xc)
ok(rc == 0 and rr == 2 and "starts other boot files" in ro and sha(Dc) == b,
   "rollback --stick-boot with a stick whose copies are not the disk's boot files: refused, nothing written")
make_release(W / "n6.img", vendor="v6")
r2, out = run("apply", Dts, "--image", W / "n6.img", "--mode", "nvme", "--stick-boot", Xold)   # the old stick: a -> b
xm = menu_slots(Xold, 0)
ok(r1 == 0 and r2 == 0 and xm.get("gbos-nvme-prev.conf") == "a" and xm.get("gbos-nvme.conf") == "b"
   and esp_read(Xold, 0, "/gbos-nvme/a/initrd.img") == esp_read(Dts, E, "/gbos/a/initrd.img")
   and esp_read(Xn1, 0, "/gbos-nvme/a/initrd.img") != esp_read(Dts, E, "/gbos/a/initrd.img"),
   "a stick with an older copy of the running slot: its 'previous version' gets what the internal disk's slot holds")
# internal disk + installer stick on an ESP without room for both versions (a Google build with a new kernel): stop
# inside the window, rerun with the same stick: the running version's files are gone from the disk, its copy comes
# from the stick (review round 2, N3 X1)
Dw2 = W / "nvme-small.img"; info_w2 = make_release(Dw2, vendor="v1", esp_mib=8, kernel_kib=3900)
Ew2 = info_w2["start"]["EFI-SYSTEM"]
for n in ("lapis-quiet.conf", "lapis-debug.conf", "lapis.conf"):
    t_ = esp_read(Dw2, Ew2, "/loader/entries/" + n).decode().replace(LIFT, "initcall_blacklist=mei_me_driver_init")
    (W / "f").write_text(t_); sh("mcopy", "-o", "-i", f"{Dw2}@@{Ew2}", W / "f", "::/loader/entries/" + n)
a_initrd = esp_read(Dw2, Ew2, "/initrd.img")
S1n = W / "small1n.img"; make_release(S1n, vendor="v2", esp_mib=8, kernel_kib=3900, kernel="kern9")
Xw2 = W / "xboot-small.img"
with open(Xw2, "wb") as fh:
    fh.truncate(16 * MIB)
sh("mformat", "-i", Xw2, "-v", "XW2", "-T", 32768, "-h", 2, "-s", 32, "::")
sh("mmd", "-i", Xw2, "::/loader"); sh("mmd", "-i", Xw2, "::/loader/entries")
r1, o1 = run("apply", Dw2, "--image", S1n, "--mode", "nvme", "--stick-boot", Xw2,
             env=dict(ENV, GBOS_UPDATE_TEST_FAIL_AT="oldremoved"))
xm1 = menu_slots(Xw2, 0)
r2, o2 = run("apply", Dw2, "--image", S1n, "--mode", "nvme", "--stick-boot", Xw2)
xm2 = menu_slots(Xw2, 0)
ok(r1 == 1 and "cannot start Googlebook now" in o1 and "installer stick" in o1 and xm1.get("gbos-nvme.conf") == "a"
   and r2 == 0 and menu_slots(Dw2, Ew2).get("lapis-quiet.conf") == "b" and xm2.get("gbos-nvme.conf") == "b"
   and xm2.get("gbos-nvme-prev.conf") == "a" and esp_read(Xw2, 0, "/gbos-nvme/a/initrd.img") == a_initrd,
   "internal disk + stick, window: stopped after the old files went (stick still starts a), the rerun takes slot a's copy from the stick")
# a stick whose FAT claims more than its partition: refused before mtools touches either disk (N3 X5)
Xbpb = W / "xbpb.img"; shutil.copy(Xn1, Xbpb); Dbs = W / "nvme-bpb.img"; shutil.copy(Dn1, Dbs)
with open(Xbpb, "r+b") as fh:
    fh.seek(19); fh.write(b"\0\0"); fh.seek(32); fh.write(struct.pack("<I", 1 << 22))
b1, b2 = sha(Dbs), sha(Xbpb)
rc, out = run("apply", Dbs, "--image", N2b, "--mode", "nvme", "--stick-boot", Xbpb)
ok(rc == 2 and "its FAT says" in out and sha(Dbs) == b1 and sha(Xbpb) == b2,
   "refuses an installer stick whose FAT reaches past its partition; neither disk changed")
# rollback --stick-boot: a stick whose entries are the other way round, and one whose copy has another kernel (N3 X3, N4)
Drb, Xrb = W / "rb2.img", W / "rbx2.img"
shutil.copy(Dn1, Drb); shutil.copy(Xn1, Xrb)
cur_t, prev_t = esp_read(Xrb, 0, "/loader/entries/gbos-nvme.conf"), esp_read(Xrb, 0, "/loader/entries/gbos-nvme-prev.conf")
(W / "f").write_bytes(prev_t); sh("mcopy", "-o", "-i", Xrb, W / "f", "::/loader/entries/gbos-nvme.conf")
(W / "f").write_bytes(cur_t); sh("mcopy", "-o", "-i", Xrb, W / "f", "::/loader/entries/gbos-nvme-prev.conf")
b1, b2 = sha(Drb), sha(Xrb)
rc, out = run("rollback", Drb, "--stick-boot", Xrb)
ok(rc == 2 and "do not match this disk's menu" in out and sha(Drb) == b1 and sha(Xrb) == b2,
   "rollback --stick-boot refuses a stick whose entries start the slots the other way round; nothing written")
shutil.copy(Xn1, Xrb)
(W / "f").write_bytes(b"not the kernel of slot a"); sh("mcopy", "-o", "-i", Xrb, W / "f", "::/gbos-nvme/a/kernel.efi")
b1, b2 = sha(Drb), sha(Xrb)
rc, out = run("rollback", Drb, "--stick-boot", Xrb)
ok(rc == 2 and "starts other boot files" in out and sha(Drb) == b1 and sha(Xrb) == b2,
   "rollback --stick-boot refuses a stick whose copy has another kernel than the disk's; nothing written")
# a stick that has no room for the internal disk's files (review I2: M8)
Xt = W / "xtiny.img"
with open(Xt, "wb") as fh:
    fh.truncate(1 * MIB)
sh("mformat", "-i", Xt, "-v", "XT", "-T", 2048, "-h", 2, "-s", 32, "::")
sh("mmd", "-i", Xt, "::/loader"); sh("mmd", "-i", Xt, "::/loader/entries")
b = sha(Dn1); rc, out = run("apply", Dn1, "--image", N2b, "--mode", "nvme", "--stick-boot", Xt)
ok(rc == 2 and "no room" in out and sha(Dn1) == b, "refuses a stick without room for the internal disk's boot files; nothing written")
# rollback across Google builds needs --allow-build-change
Db = W / "build.img"; shutil.copy(N, Db)
make_release(W / "newbuild.img", vendor="v6", build=16452300)
rc, out = run("apply", Db, "--image", W / "newbuild.img", "--mode", "stick")
rr, ro = run("rollback", Db)
ok(rc == 0 and rr == 2 and "--allow-build-change" in ro and menu_slots(Db, E).get("lapis-quiet.conf") == "b",
   "rollback to an older Google build is refused without --allow-build-change")
rr, ro = run("rollback", Db, "--allow-build-change")
ok(rr == 0 and menu_slots(Db, E).get("lapis-quiet.conf") == "a", "... and done with it")

# no room for both versions on the ESP
Dsmall = W / "small.img"; info_s = make_release(Dsmall, vendor="v1", esp_mib=8, kernel_kib=3900)
make_release(W / "small1.img", vendor="v2", esp_mib=8, kernel_kib=3900, kernel="kern9")
b = sha(Dsmall)
rc, out = run("apply", Dsmall, "--image", W / "small1.img", "--mode", "stick")
ok(rc == 2 and "no room to keep the previous version" in out and sha(Dsmall) == b, "no room on the ESP: refused without --without-fallback")
rc, out = run("apply", Dsmall, "--image", W / "small1.img", "--mode", "stick", "--without-fallback")
Es = info_s["start"]["EFI-SYSTEM"]
ok(rc == 0 and "UPDATE OK: slot b" in out and not esp_has(Dsmall, Es, "/loader/entries/prev-lapis-quiet.conf"),
   "--without-fallback: updated, no previous-version entry")
make_release(Dsmall, vendor="v1", esp_mib=8, kernel_kib=3900)
rc, out = run("apply", Dsmall, "--image", W / "small1.img", "--mode", "stick", "--without-fallback",
              env=dict(ENV, GBOS_UPDATE_TEST_FAIL_AT="oldremoved"))
ok(rc == 1 and "cannot start Googlebook now" in out, "--without-fallback, no room even for the new files first: a stop inside the window says so")
rc, out = run("apply", Dsmall, "--image", W / "small1.img", "--mode", "stick", "--without-fallback")
ok(rc == 0 and menu_slots(Dsmall, Es).get("lapis-quiet.conf") == "b" and all(esp_has(Dsmall, Es, x) for x in files_of(Dsmall, Es, "lapis-quiet.conf")),
   "... the rerun finishes it: the menu starts b and every file it names is there")
# the window lasts until the menu is switched: a stop after the new boot files says so too, and a rerun whose
# running files are gone is inside the window from its first write (review round 2, N1)
for stops in (("bootfiles",), ("oldremoved", "removed"), ("oldremoved", "dynamic")):
    make_release(Dsmall, vendor="v1", esp_mib=8, kernel_kib=3900)
    said = []
    for st in stops:
        rc, out = run("apply", Dsmall, "--image", W / "small1.img", "--mode", "stick", "--without-fallback",
                      env=dict(ENV, GBOS_UPDATE_TEST_FAIL_AT=st))
        said.append(rc == 1 and "cannot start Googlebook now" in out and "installer stick" not in out
                    and "still starts the version that ran before" not in out)
    rc, out = run("apply", Dsmall, "--image", W / "small1.img", "--mode", "stick", "--without-fallback")
    ok(all(said) and rc == 0 and menu_slots(Dsmall, Es).get("lapis-quiet.conf") == "b",
       f"window: a stop after {' then '.join(stops)} says the disk's own menu starts nothing (no stick named); the rerun finishes")
# new files first: the ESP has room for the new files next to the old ones, but not for a previous version
Dnf = W / "newfirst.img"; shutil.copy(N, Dnf)
for stop in ("bootfiles", None):
    env = dict(ENV, GBOS_UPDATE_TEST_NO_PREV_ROOM="1", **({"GBOS_UPDATE_TEST_FAIL_AT": stop} if stop else {}))
    rc, out = run("apply", Dnf, "--image", N1, "--mode", "stick", "--without-fallback", env=env)
    m = menu_slots(Dnf, E)
    if stop:
        ok(rc == 1 and m.get("lapis-quiet.conf") == "a" and all(esp_has(Dnf, E, x) for x in files_of(Dnf, E, "lapis-quiet.conf")),
           "no room for a previous version: stopped after the new boot files, the old version still starts with all its files")
ok(rc == 0 and m.get("lapis-quiet.conf") == "b" and not esp_has(Dnf, E, "/initrd.img") and esp_has(Dnf, E, "/kernel.efi")
   and all(esp_has(Dnf, E, x) for x in files_of(Dnf, E, "lapis-quiet.conf")) and not any(n.startswith("prev-") for n in m),
   "... rerun: the menu starts b; the old initrd is gone after the switch, the kernel both slots share stays")

# the write guard refuses a range outside what an update may write
g = gbu.Disk(str(W / "n1.img"), write=True)
g.guard([(0, MIB, "the partition table")])
try:
    g.pwrite(512, b"x"); ok(False, "write guard")
except gbu.Failed:
    ok(True, "write guard: a write into a forbidden range fails before it happens")

bad = [w for c, w in RESULTS if not c]
print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
if bad:
    sys.exit("UPDATE SELFTEST FAILED")
print("UPDATE SELFTEST OK")
