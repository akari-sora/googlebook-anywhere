#!/usr/bin/env python3
"""Self-test of the stick installer's checks without QEMU, root or disks (python3 and zstd only):
make-pristine.py on a small synthetic image, then the verifier embedded in gbos-install.sh (verify-data, verify-target,
check-gpt, bootconfig-*, lift-entry, check-cmdline) on good and damaged inputs, cross-checked against tests/stick/gptlib.py,
and the installer's busy check of the target (in_use) on fake /proc and /sys files.
With an optional raw disk image made by another GPT tool (e.g. sgdisk, hdiutil), both parsers must accept its CRCs.
usage: selftest.py <empty work dir> [reference GPT disk image]"""
import hashlib, random, re, shutil, struct, subprocess, sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gptlib  # noqa: E402

W = Path(sys.argv[1]); W.mkdir(parents=True, exist_ok=False)
REPO = Path(__file__).resolve().parents[2]
src = (REPO / "install/stick/gbos-install.sh").read_text()
m = re.search(r"cat > \"\$W/gbos.py\" << 'PY'\n(.*?)\nPY\n", src, re.S)
if not m: sys.exit("no embedded verifier in gbos-install.sh")
(W / "gbos.py").write_text(m.group(1) + "\n")
results = {}
def check(name, ok):
    results[name] = bool(ok); print("PASS" if ok else "FAIL", name, flush=True)
def v(*args):
    r = subprocess.run([sys.executable, "-I", str(W / "gbos.py"), *map(str, args)], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()

# --- a 96 MiB image with the partitions the installer cares about
rnd = random.Random(7)
SECT = 96 * 2048
ESP, LIN = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "0fc63daf-8483-4772-8e79-3d69d8477de4"
layout = [(1, "EFI-SYSTEM", ESP, 2048, 8191, 0), (2, "boot_a", LIN, 8192, 12287, 0), (3, "vbmeta_a", LIN, 12288, 12295, 1 << 60),
          (4, "metadata", LIN, 14336, 18431, 0), (5, "super", LIN, 18432, 100351, 0), (6, "userdata", LIN, 102400, SECT - 34, 1 << 2)]
img = W / "googlebook-kano.img"
with open(img, "wb") as f:
    f.truncate(SECT * 512)
    for n, name, typ, a, z, attrs in layout:
        f.seek(a * 512); f.write(rnd.randbytes(min(z + 1 - a, 2048) * 512))
    gptlib.write_gpt(f, SECT, [dict(num=n, name=name, type=t, first=a, last=z, attrs=at) for n, name, t, a, z, at in layout])
h, parts = gptlib.read_gpt(img)
check("gptlib reads back the GPT it wrote (both copies)", [p["name"] for p in parts.values()] == [l[1] for l in layout])
PR = W / "pristine"
r = subprocess.run([sys.executable, "-I", str(REPO / "install/stick/make-pristine.py"), str(img), str(PR)], capture_output=True, text=True)
check("make-pristine.py succeeds", r.returncode == 0)
man = (PR / "manifest.txt").read_text().splitlines()
check("manifest: image, head, 6 parts; super stick, userdata new, attrs kept",
      man[0].startswith("image googlebook-kano.img ") and man[1].startswith("head 0 1048576 ") and len(man) == 8
      and man[6].endswith(" stick") and man[7].endswith(" - new") and " 1000000000000000 " in man[4] and " 0000000000000004 " in man[7])
ok, out = v("verify-data", PR, W / "head.img")
check("verify-data accepts good data", ok and (W / "head.img").read_bytes() == img.read_bytes()[:1 << 20])
if not ok: print(out)

def damaged(label, mutate, expect):
    d = W / ("bad-" + re.sub(r"\W+", "-", label)); shutil.copytree(PR, d); mutate(d)
    ok, out = v("verify-data", d, d / "head.out")
    check(f"verify-data refuses {label}", not ok and re.search(expect, out) and not (d / "head.out").exists())
def recompress(path, change):
    raw = bytearray(subprocess.run(["zstd", "-q", "-dc", str(path)], check=True, capture_output=True).stdout); change(raw)
    path.unlink(); subprocess.run(["zstd", "-q", "-o", str(path)], input=bytes(raw), check=True)
def edit_manifest(d, f):
    p = d / "manifest.txt"; p.write_text(f(p.read_text()))
damaged("a changed pristine partition", lambda d: recompress(d / "p2.img.zst", lambda b: b.__setitem__(100, b[100] ^ 1)), "p2.img.zst .*does not match")
damaged("a truncated pristine file", lambda d: (d / "p4.img.zst").write_bytes((d / "p4.img.zst").read_bytes()[:-9]), "zstd")
damaged("a missing pristine file", lambda d: (d / "p3.img.zst").unlink(), "zstd")
damaged("a changed head", lambda d: recompress(d / "head.img.zst", lambda b: b.__setitem__(4000, b[4000] ^ 1)), "head.img.zst does not match")
def head_and_digest(d, change):  # a head whose SHA-256 is right but whose GPT is not
    recompress(d / "head.img.zst", change)
    raw = subprocess.run(["zstd", "-q", "-dc", str(d / "head.img.zst")], check=True, capture_output=True).stdout
    edit_manifest(d, lambda t: re.sub(r"(?m)^head 0 1048576 [0-9a-f]{64}$", "head 0 1048576 " + hashlib.sha256(raw).hexdigest(), t))
damaged("a head with a bad header CRC", lambda d: head_and_digest(d, lambda b: b.__setitem__(512 + 40, b[512 + 40] ^ 1)), "header at LBA 1: bad CRC")
damaged("a head with bad entry CRC", lambda d: head_and_digest(d, lambda b: b.__setitem__(1024 + 60, b[1024 + 60] ^ 1)), "entries: bad CRC")
damaged("a head without protective MBR", lambda d: head_and_digest(d, lambda b: b.__setitem__(510, 0)), "protective MBR")
damaged("a manifest whose part differs from the head", lambda d: edit_manifest(d, lambda t: t.replace(" 0000000000000000 ", " 0000000000000001 ", 1)), "partition 1 is")
damaged("a manifest line with a bad field", lambda d: edit_manifest(d, lambda t: t.replace(" pristine\n", " pristin\n", 1)), "bad line")
damaged("a manifest without super", lambda d: edit_manifest(d, lambda t: re.sub(r"(?m)^part 5 .*\n", "", t)), "needs super")
damaged("a manifest with overlapping parts", lambda d: edit_manifest(d, lambda t: re.sub(r"(?m)^(part 3 vbmeta_a) 6291456 ", r"\1 6290944 ", t)), "overlap")

# --- verify-target on a disk laid out as the installer leaves it: image head, userdata from UD_START to the end,
#     backup at the end, new GUIDs
mparts = {int(l.split()[1]): l.split() for l in man[2:]}
LAST = max((int(p[3]) + int(p[4])) // 512 for p in mparts.values() if p[2] != "userdata")
UD = (LAST + 2047) // 2048 * 2048
TS = 160 * 2048
def target(path, change=lambda parts: None, sectors=TS):
    tp = [dict(num=n, name=p[2], type=p[6], first=int(p[3]) // 512, last=(int(p[3]) + int(p[4])) // 512 - 1, attrs=int(p[5], 16))
          for n, p in mparts.items()]
    for p in tp:
        if p["name"] == "userdata": p["first"], p["last"] = UD, sectors - 34
    change(tp)
    with open(path, "wb") as f:
        f.truncate(sectors * 512); gptlib.write_gpt(f, sectors, tp)
    return next(p for p in tp if p["name"] == "boot_a")
tgt = W / "nvme.img"; target(tgt)
bg = gptlib.read_gpt(tgt)[1][2]["guid"]
ok, out = v("verify-target", tgt, PR / "manifest.txt", img, UD, bg)
check("verify-target accepts the expected result", ok)
if not ok: print(out)
def bad_target(label, change=lambda parts: None, after=None, guid=None, expect=""):
    p = W / ("t-" + re.sub(r"\W+", "-", label)); target(p, change)
    if after: after(p)
    g = guid or gptlib.read_gpt(p, backup=False)[1][2]["guid"]
    ok, out = v("verify-target", p, PR / "manifest.txt", img, UD, g)
    check(f"verify-target refuses {label}", not ok and re.search(expect, out))
def setattr_(name, key, val): return lambda parts: next(p for p in parts if p["name"] == name).__setitem__(key, val)
bad_target("userdata ending early", setattr_("userdata", "last", TS - 2048), expect="partition 6")
bad_target("userdata at the image's old start", setattr_("userdata", "first", 102400), expect="partition 6")
bad_target("lost userdata attributes", setattr_("userdata", "attrs", 0), expect="partition 6")
bad_target("a wrong type", setattr_("metadata", "type", ESP), expect="partition 4")
bad_target("a wrong name", setattr_("vbmeta_a", "name", "vbmeta_b"), expect="partition 3")
bad_target("an extra partition", lambda parts: parts.append(dict(num=9, name="STATE", type=LIN, first=TS - 4096, last=TS - 2049)), expect="partitions")
bad_target("a GUID shared with the stick", setattr_("super", "guid", gptlib.read_gpt(img)[1][5]["guid"]), expect="GUIDs")
bad_target("boot_a GUID not the one in the initrd", guid=str(uuid.uuid4()), expect="boot_a GUID")
def corrupt(off):
    def f(p):
        with open(p, "r+b") as d: d.seek(off); b = d.read(1); d.seek(off); d.write(bytes([b[0] ^ 1]))
    return f
bad_target("a damaged backup GPT header", after=corrupt((TS - 1) * 512 + 40), expect="LBA %d" % (TS - 1))
bad_target("a damaged backup entry array", after=corrupt((TS - 33) * 512 + 70), expect="backup GPT differs")
def stale_backup(p):  # backup header still where the image's was (sgdisk -e not done)
    with open(p, "r+b") as d:
        d.seek((TS - 1) * 512); d.write(bytes(512))
bad_target("no backup GPT at the end", after=stale_backup, expect="LBA %d" % (TS - 1))
ok, _ = v("check-gpt", img)
check("check-gpt accepts the stick's GPT", ok)

# --- bootconfig and entries
def initrd(bc):
    bc = bc.encode(); return b"\x07" * 333 + bc + struct.pack("<II", len(bc), sum(bc) & 0xFFFFFFFF) + gptlib.MAGIC
old = "65398d24-ffb3-40d7-bfbc-b472b4b24885"
ird = initrd(f"androidboot.hardware=android-desktop\nandroidboot.boot_part_uuid={old}\nandroidboot.slot_suffix=_a\n\0\0")
(W / "initrd.img").write_bytes(ird)
check("bootconfig-check accepts the image's initrd", v("bootconfig-check", W / "initrd.img", "-")[0])
new = str(uuid.uuid4())
ok, _ = v("bootconfig-set", W / "initrd.img", new)
check("bootconfig-set = gptlib's independent patch, same size", ok and (W / "initrd.img").read_bytes() == gptlib.bootconfig_set_boot_part(ird, new)
      and len(ird) == (W / "initrd.img").stat().st_size)
check("bootconfig-check accepts the new GUID, refuses the old", v("bootconfig-check", W / "initrd.img", new)[0]
      and not v("bootconfig-check", W / "initrd.img", old)[0])
bad = bytearray(ird); bad[340] ^= 1; (W / "bad-initrd.img").write_bytes(bytes(bad))
check("bootconfig-check refuses a bad checksum", not v("bootconfig-check", W / "bad-initrd.img", "-")[0])
(W / "two.img").write_bytes(initrd(f"androidboot.boot_part_uuid={old}\nandroidboot.boot_part_uuid={old}\n"))
check("bootconfig-check refuses two boot_part_uuid lines", not v("bootconfig-check", W / "two.img", "-")[0])
opts = ("options root=x module_blacklist=cros_ec,tpm initcall_blacklist=nvme_init,mei_me_driver_init efi=noruntime panic=0")
(W / "e.conf").write_bytes(f"title Googlebook KANO quiet\nlinux /kernel.efi\ninitrd /initrd.img\n{opts}\n".encode())
ok, _ = v("lift-entry", W / "e.conf", W / "e.out")
check("lift-entry lifts only nvme_init", ok and (W / "e.out").read_bytes() == (W / "e.conf").read_bytes().replace(
      b"initcall_blacklist=nvme_init,mei_me_driver_init", b"initcall_blacklist=mei_me_driver_init"))
for label, text in (("no NVMe block", opts.replace("nvme_init,", "")), ("no efi=noruntime", opts.replace(" efi=noruntime", "")),
                    ("no module_blacklist", opts.replace(" module_blacklist=cros_ec,tpm", "")),
                    ("nvme in module_blacklist", opts.replace("cros_ec,tpm", "cros_ec,nvme"))):
    (W / "x.conf").write_text(f"title t\n{text}\n")
    check(f"lift-entry refuses an entry with {label}", not v("lift-entry", W / "x.conf", W / "x.out")[0])

# --- the installer entry: template + builtin-initcalls.txt, as setup-stick.sh renders them
STICKSRC = REPO / "install/stick"
imap = gptlib.read_initcall_map(STICKSRC / "builtin-initcalls.txt")
ics = gptlib.initcalls(imap)
r = subprocess.run(["sh", "-c", "awk '{sub(/#.*/, \"\")} NF && $2 != \"-\" {print $2}' \"$1\" | paste -sd, -", "sh",
                    str(STICKSRC / "builtin-initcalls.txt")], capture_output=True, text=True)
check("builtin-initcalls.txt: shell (setup-stick/gbos-install) and Python parsers give the same initcalls", r.stdout.strip() == ics)
check("builtin-initcalls.txt covers tpm, tpm_tis, tpm_crb, nvram by initcall and tpm_tis_core only behind them",
      all(imap.get(m, (None,))[0] for m in ("tpm", "tpm_tis", "tpm_crb", "nvram", "mei_me"))
      and imap.get("tpm_tis_core") == (None, ["tpm_init", "init_tis", "crb_acpi_driver_init"]))
tpl = (STICKSRC / "gbos-install.entry.template").read_text()
ent = tpl.replace("@ROOT_PARTUUID@", str(uuid.uuid4())).replace("@INITCALL_BLACKLIST@", ics)
opts = next(l for l in ent.splitlines() if l.startswith("options ")).split()[1:]
tbl = set(next(o for o in opts if o.startswith("module_blacklist=")).split("=", 1)[1].split(","))
import tomllib
with open(REPO / "profiles/kano/profile.toml", "rb") as f:
    bl_img = set(tomllib.load(f)["blacklist"]["modules"])
check("rendered entry: no placeholder left, initcall_blacklist = the mapping, lockdown + efi=noruntime, nvme allowed",
      "@" not in ent and f"initcall_blacklist={ics}" in opts and {"lockdown=integrity", "efi=noruntime"} <= set(opts)
      and not {"nvme", "nvme_core"} & tbl and "nvme" not in ics)
check("entry's module_blacklist covers the profile's blacklist.modules and every mapped module", bl_img <= tbl and set(imap) <= tbl)
check("entry's options line is short enough (< 1900 bytes)", len(next(l for l in ent.splitlines() if l.startswith("options "))) < 1900)

# --- check-cmdline: the installer's runtime check of the kernel command line, against module-blacklist.txt made by
#     setup-stick.sh's own blacklist() from the template and the rendered entry's options
setup_src = (STICKSRC / "setup-stick.sh").read_text()
bfun = re.search(r"(?m)^blacklist\(\) \{.*\}$", setup_src)
r = subprocess.run(["bash", "-c", bfun.group(0) + '\nblacklist "$1"', "bash", str(STICKSRC / "gbos-install.entry.template")],
                   capture_output=True, text=True, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
mods = r.stdout.split()
check("setup-stick's blacklist() reads the template's whole module_blacklist", r.returncode == 0 and set(mods) == tbl and mods == sorted(mods))
(W / "module-blacklist.txt").write_text("# made by setup-stick.sh\n" + "".join(m + "\n" for m in mods))
(W / "modules-none").write_text("nvme 1 0 - Live 0x0\nvfat 2 1 - Live 0x0\n")
cmd = " ".join(opts)
def cc(label, line, expect=None, mods_file=W / "module-blacklist.txt", loaded=W / "modules-none"):
    (W / "cmdline").write_text(line + "\n")
    ok, out = v("check-cmdline", W / "cmdline", mods_file, STICKSRC / "builtin-initcalls.txt", loaded)
    if expect is None: check(f"check-cmdline accepts {label}", ok and re.search(r"module_blacklist has all \d+ modules", out))
    else: check(f"check-cmdline refuses {label}", not ok and re.search(expect, out))
cc("the rendered installer entry", cmd)
mid = mods[len(mods) // 2]
bl_tok = next(o for o in opts if o.startswith("module_blacklist="))
names_ = bl_tok.split("=", 1)[1].split(",")
cc(f"a module_blacklist without {mid} (from the middle)", cmd.replace(bl_tok, "module_blacklist=" + ",".join(x for x in names_ if x != mid)),
   f"module_blacklist lacks {mid}")
cc("a second, shorter module_blacklist after the first (the kernel keeps the last)", cmd + " module_blacklist=nvram", "2 module_blacklist=")
cc("no module_blacklist", cmd.replace(bl_tok, ""), "0 module_blacklist=")
cc("an initcall_blacklist without tpm_init", cmd.replace(f"initcall_blacklist={ics}", "initcall_blacklist=" + ics.replace("tpm_init,", "").replace(",tpm_init", "")),
   "built-in drivers are not blocked: initcall_blacklist lacks tpm_init")
cc("the initcalls split over two initcall_blacklist=", cmd.replace(f"initcall_blacklist={ics}", "initcall_blacklist=" + ics.replace(",", " initcall_blacklist=", 1)))
cc("no efi=noruntime", cmd.replace(" efi=noruntime", ""), "no efi=noruntime")
(W / "modules-tpm").write_text("nvme 1 0 - Live 0x0\ntpm_crb 2 1 - Live 0x0\n")
cc("a blocked module that is loaded", cmd, "blocked modules are loaded: tpm_crb", loaded=W / "modules-tpm")
(W / "mods-empty").write_text("# nothing\n")
cc("an empty module-blacklist.txt", cmd, "empty", mods_file=W / "mods-empty")
cc("a missing module-blacklist.txt", cmd, "No such file", mods_file=W / "nonexistent.txt")
(W / "mods-short").write_text("cros_ec\n")
cc("a module-blacklist.txt without the mapped modules", cmd, "lacks modules of", mods_file=W / "mods-short")

# --- in_use: the installer's busy check of the target, on fake /proc and /sys files; a query that fails refuses
inst_src = (REPO / "install/stick/gbos-install.sh").read_text()
fun = re.search(r"(?ms)^in_use\(\) \{.*?^\}\n", inst_src).group(0)
FX = W / "fx"
fun = fun.replace("/proc/mounts", f"{FX}/mounts").replace("/proc/swaps", f"{FX}/swaps").replace("/sys/block/", f"{FX}/sys/block/")
def in_use(label, setup, expect):
    shutil.rmtree(FX, ignore_errors=True)
    (FX / "sys/block/nvme0n1/holders").mkdir(parents=True); (FX / "sys/block/nvme0n1/dev").write_text("259:0\n")
    (FX / "sys/block/nvme0n1/nvme0n1p1/holders").mkdir(parents=True)
    (FX / "mounts").write_text("/dev/sda33 / ext4 rw 0 0\n"); (FX / "swaps").write_text("Filename Type Size Used Priority\n")
    env = {"PATH": "/usr/bin:/bin", "LSBLK_OUT": "\n\n"}
    setup(env)
    sh = ('T=/dev/nvme0n1; DEVNO=259:0\nlsblk() { [ -z "${LSBLK_FAIL:-}" ] || return 32; printf "%s" "$LSBLK_OUT"; }\n'
          + fun + "in_use\n")
    r = subprocess.run(["bash", "-c", sh], capture_output=True, text=True, env=env)
    out = r.stdout.strip()
    check(f"in_use: {label}", re.search(expect, out) if expect else out == "")
def w(rel, text): return lambda env: (FX / rel).write_text(text)
in_use("a disk in no use says nothing", lambda env: None, None)
in_use("a mounted partition in /proc/mounts", w("mounts", "/dev/nvme0n1p3 /mnt ext4 rw 0 0\n"), "is mounted or used as swap")
in_use("a swap partition", w("swaps", "Filename Type\n/dev/nvme0n1p2 partition 1 0 -2\n"), "is mounted or used as swap")
in_use("a mount point shown by lsblk", lambda env: env.update(LSBLK_OUT="\n/run/x\n"), "has mounted partitions")
in_use("a holder of a partition (dm-crypt)", lambda env: (FX / "sys/block/nvme0n1/nvme0n1p1/holders/dm-0").write_text(""), "in use by dm-0")
in_use("/proc/swaps unreadable -> refuses", lambda env: (FX / "swaps").unlink(), r"cannot read \S*/mounts or \S*/swaps")   # (the paths in the message are the fake ones)
in_use("/sys/block/<disk> gone -> refuses", lambda env: shutil.rmtree(FX / "sys/block/nvme0n1"), "cannot check")
in_use("another device number -> refuses", w("sys/block/nvme0n1/dev", "259:7\n"), "cannot check")
in_use("lsblk failing -> refuses", lambda env: env.update(LSBLK_FAIL="1"), "cannot read the mount points")

# --- the image's boot entries: esp_entries() of gbos-install.sh and setup-stick.sh (the same code) on a fake mdir, and
#     the entry sets both accept: quiet + serial-debug, plus the plain lapis.conf of images built before it was dropped
efun = [re.search(r"(?ms)^esp_entries\(\) \{.*?; \}$", t) for t in (src, setup_src)]
check("gbos-install.sh and setup-stick.sh define the same esp_entries()", all(efun) and efun[0].group(0) == efun[1].group(0))
acc = [re.search(r'(?m)^case "\$ENTRIES" in\n\s*(.*?)\) ;;$', t) for t in (src, setup_src)]
check("gbos-install.sh and setup-stick.sh accept the same entry sets", all(acc) and acc[0].group(1) == acc[1].group(1))
FB = W / "fakebin"; FB.mkdir()
(FB / "mdir").write_text('#!/bin/sh\n[ -z "$MDIR_FAIL" ] || { echo "mdir: cannot open" >&2; exit 1; }\nprintf "%b" "$MDIR_OUT"\n')
(FB / "mdir").chmod(0o755)
def ee(out, fail=""):
    sh = efun[0].group(0) + "\nE=$(esp_entries x.img)\necho \"[$E]\"\ncase \"$E\" in\n    " + acc[0].group(1) + ") echo ACCEPT ;;\n    *) echo REFUSE ;;\nesac"
    r = subprocess.run(["bash", "-c", sh], capture_output=True, text=True,
                       env={"PATH": f"{FB}:/usr/bin:/bin", "LC_ALL": "C", "MDIR_OUT": out, "MDIR_FAIL": fail})
    return r.stdout.strip().splitlines()
L = "::/loader/entries/"
check("esp_entries: quiet + serial-debug (current images) accepted",
      ee(f"{L}lapis-quiet.conf\\r\\n{L}lapis-debug.conf\\r\\n") == ["[lapis-debug.conf lapis-quiet.conf ]", "ACCEPT"])
check("esp_entries: with the plain lapis.conf (older images) accepted",
      ee(f"{L}lapis.conf\\n{L}lapis-debug.conf\\n{L}lapis-quiet.conf\\n") == ["[lapis-debug.conf lapis-quiet.conf lapis.conf ]", "ACCEPT"])
check("esp_entries: a subdirectory line is not an entry", ee(f"{L}sub/\\n{L}lapis-debug.conf\\n{L}lapis-quiet.conf\\n")[-1] == "ACCEPT")
check("esp_entries: serial-debug missing -> refused", ee(f"{L}lapis-quiet.conf\\n{L}lapis.conf\\n")[-1] == "REFUSE")
check("esp_entries: an extra unknown entry -> refused",
      ee(f"{L}lapis-debug.conf\\n{L}lapis-quiet.conf\\n{L}other.conf\\n")[-1] == "REFUSE")
check("esp_entries: mdir failing -> empty, refused", ee("", fail="1") == ["[]", "REFUSE"])

# --- optional: a GPT written by another tool
if len(sys.argv) > 2:
    ref = Path(sys.argv[2])
    try: gptlib.read_gpt(ref); gl = True
    except ValueError as e: print(e); gl = False
    check(f"gptlib accepts the reference GPT {ref.name}", gl)
    ok, out = v("check-gpt", ref)
    check(f"installer check-gpt accepts the reference GPT {ref.name}", ok)

failed = [k for k, x in results.items() if not x]
print("STICK SELFTEST", "OK" if not failed else "FAILED", f"({sum(results.values())}/{len(results)})")
sys.exit(1 if failed else 0)
