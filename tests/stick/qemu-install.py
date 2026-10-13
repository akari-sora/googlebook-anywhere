#!/usr/bin/env python3
"""QEMU test of the stick installer (install/stick/gbos-install.sh) on a copy of the installer stick.

usage: qemu-install.py <work dir> <final image> <stick head 1 MiB> <stick tail 1 MiB> <XBOOTLDR dump> <maint root dump>
                       <pristine dir>
  final image       googlebook-kano.img that <pristine dir> was made from (install/stick/make-pristine.py)
  stick head, tail  first and last MiB of the real installer stick (its GPT, with XBOOTLDR and kano-maint)
  XBOOTLDR dump     image of the stick's XBOOTLDR partition (FAT, holds vmlinuz-linux and initramfs-linux.img)
  maint root dump   image of the stick's kano-maint partition (ext4, the maintenance Arch root)
env: GBOS_QEMU, GBOS_QEMU_LIB, GBOS_OVMF_CODE, GBOS_OVMF_VARS (as for builder/build.sh); GBOS_STICK_KEEP=1 keeps the
     NVMe images; GBOS_STICK_SCENARIOS=<comma list> runs only some of refusals, super-mismatch, cancel, install-flex,
     install-blank, update (update: after install-blank, the update mode puts the image into slot b, then a second
     update puts it into slot a while slot b runs). Host tools: qemu-system-x86_64 with KVM, OVMF, mtools, e2fsprogs (debugfs, e2fsck). No root needed.

The stick copy runs the CURRENT repo installer, not whatever the maint root dump holds: gbos-install.sh
(-> /usr/local/sbin/gbos-install), gbos-installer.service with its multi-user.target.wants link, builtin-initcalls.txt
and module-blacklist.txt (the template's module_blacklist, as setup-stick.sh writes it) (-> /usr/local/share/gbos-install/)
and <pristine dir> (-> /var/lib/gbos-install/pristine) are written into a copy of the ext4 maint root with debugfs -w
and read back, and the XBOOTLDR entry is rendered from install/stick/gbos-install.entry.template and
builtin-initcalls.txt, as setup-stick.sh does.

Static checks: the entry blocks every module the image's entries block, keeps efi=noruntime and lockdown=integrity, and
every blocked module built into the maintenance kernel (its modules.builtin) is covered by builtin-initcalls.txt, whose
initcalls are all in the entry's initcall_blacklist.
Cases (the NVMe is a sparse file; "zero writes" = QMP query-blockstats wr/unmap counters of the NVMe are 0 when the
installer waits for its last Enter, and the NVMe file is unchanged afterwards):
  1. nolockdown      entry without lockdown=integrity -> refused before anything, zero writes
     noinitcall      entry whose initcall_blacklist lacks the built-in TPM/nvram initcalls -> refused, zero writes
     nomodule        entry whose module_blacklist lacks one name from the middle (spi_intel_pci) -> refused, zero writes
  2. super-mismatch  one byte of the stick's super changed, NVMe with a Flex-like GPT and data -> refused before the
                     serial prompt, zero writes, NVMe unchanged
  3. cancel          wrong confirmation on a blank NVMe -> "Cancelled", zero writes
  4. install-flex    install over the Flex-like NVMe
  5. install-blank   install on the blank NVMe
  Cancel, 4 and 5: the installer's runtime checks passed: the command line has the whole module_blacklist and the
  built-in initcalls, none of those modules is loaded, no /dev/tpm0, /dev/tpmrm0, /dev/nvram, /dev/cros_ec, /dev/mtd*,
  /dev/spidev*, nothing in /sys/class/tpm.
  After 4 and 5: INSTALL OK with lockdown active, zero writes before the confirmation, both GPT copies valid, every
  partition's start/end/type/name/attrs as in the manifest (userdata from the new start to the last usable sector),
  new unique GUIDs, SHA-256 of super and every unchanged partition, userdata head empty, ESP initrd = the image's with
  boot_part_uuid = the NVMe's boot_a, entries = the image's with only nvme_init lifted, exact loader.conf, the stick's
  XBOOTLDR kernel/initrd/entries for the internal disk and its default; then QEMU with only the NVMe: firmware -> NVMe
  ESP -> systemd-boot -> quiet entry -> Android writes /data, the highest write inside userdata.
"""
import hashlib, json, os, random, re, shutil, socket, struct, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gptlib  # noqa: E402

if len(sys.argv) != 8: sys.exit(__doc__)
W, IMG, HEAD, TAIL, XB, MAINT, PR = map(Path, sys.argv[1:8])
REPO = Path(__file__).resolve().parents[2]
STICKSRC = REPO / "install/stick"
QEMU = os.environ.get("GBOS_QEMU", "/usr/bin/qemu-system-x86_64")
QLIB = os.environ.get("GBOS_QEMU_LIB", "")
CODE = os.environ.get("GBOS_OVMF_CODE", "/usr/share/edk2/x64/OVMF_CODE.4m.fd")
VARS = os.environ.get("GBOS_OVMF_VARS", "/usr/share/edk2/x64/OVMF_VARS.4m.fd")
NVME_SERIAL = "GBOSTEST7Q4Z"
NVME_BYTES = 128 * 1000 ** 3
LIFT = "initcall_blacklist=nvme_init,mei_me_driver_init"
# boot entries of the image: quiet + serial-debug; images built before the plain entry was dropped also have lapis.conf
ENTRY_SETS = ({"lapis-debug.conf", "lapis-quiet.conf"}, {"lapis.conf", "lapis-debug.conf", "lapis-quiet.conf"})
BUILTIN_OK = r"ok +built-in drivers blocked"
CMDLINE_OK = r"ok +kernel command line: module_blacklist has all \d+ modules of the installer entry, none loaded"
for tool in ("debugfs", "e2fsck", "mtype", "mcopy", "mdel", "mdeltree", "mdir"):
    if not shutil.which(tool): sys.exit(f"needs {tool} (mtools, e2fsprogs)")
for p in (W, PR, STICKSRC):
    if re.search(r"\s", str(p.resolve())): sys.exit(f"path must not contain spaces (debugfs commands): {p}")
W.mkdir(parents=True, exist_ok=True)
W = W.resolve()
STICK, BLANK, FLEX = W / "stick.img", W / "nvme-blank.img", W / "nvme-flex.img"
results = {}
def check(name, ok, detail=""):
    results[name] = bool(ok); print("PASS" if ok else "FAIL", name, "" if ok or not detail else f"({detail})", flush=True)
    return bool(ok)
def run(*a, **kw):
    return subprocess.run([str(x) for x in a], **kw)
def mtools(cmd, img, off, *args, ok=True):
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
    r = subprocess.run([cmd, "-i", f"{img}@@{off}", *map(str, args)], capture_output=True, env=env)
    if ok and r.returncode: raise SystemExit(f"{cmd} {args}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout
def esp_entries(img, off):
    out = mtools("mdir", img, off, "-b", "::/loader/entries").decode().replace("\r", "").split("\n")
    return sorted(l.rsplit("/", 1)[-1] for l in (x.strip() for x in out) if l.startswith("::/loader/entries/") and not l.endswith("/"))
def options(entry):
    return next(l for l in entry.splitlines() if l.startswith("options ")).split()[1:]
def blacklist(entry):
    bl = [o for o in options(entry) if o.startswith("module_blacklist=")]
    return set(bl[0].split("=", 1)[1].split(",")) if len(bl) == 1 else None

# --- inputs: the pristine dir belongs to the image; the stick head has the image's super and ESP in place
manifest = [l.split(" ") for l in (PR / "manifest.txt").read_text().splitlines()]
mhead = next(l for l in manifest if l[0] == "head")
mparts = {int(l[1]): dict(name=l[2], start=int(l[3]), len=int(l[4]), attrs=int(l[5], 16), type=l[6], sha=l[7], kind=l[8])
          for l in manifest if l[0] == "part"}
mnum = {v["name"]: k for k, v in mparts.items()}
if gptlib.sha(IMG, 0, 1 << 20) != mhead[3]: sys.exit("the pristine dir was not made from this image (head SHA-256)")
_, ig = gptlib.read_gpt(IMG, backup=True)
for n, m in mparts.items():
    if (ig[n]["name"], ig[n]["first"] * 512, (ig[n]["last"] + 1) * 512 - m["start"]) != (m["name"], m["start"], m["len"]):
        sys.exit(f"image partition {n} differs from the manifest")
LAST = max((m["start"] + m["len"]) // 512 for m in mparts.values() if m["name"] != "userdata")
UD_START = (LAST + 2047) // 2048 * 2048
sh, sg = gptlib.read_gpt(HEAD, backup=False)
snum = {v["name"]: k for k, v in sg.items()}
for name in ("super", "EFI-SYSTEM"):
    m = mparts[mnum[name]]; s = sg.get(mnum[name], {})
    if (s.get("name"), s.get("first", 0) * 512, (s.get("last", 0) + 1) * 512 - m["start"]) != (name, m["start"], m["len"]):
        sys.exit(f"the stick's {name} is not where the image has it")
XBOFF, MAINTOFF = sg[snum["XBOOTLDR"]]["first"] * 512, sg[snum["kano-maint"]]["first"] * 512
ESPOFF = mparts[mnum["EFI-SYSTEM"]]["start"]          # same on the image and on the stick
SUPER = mparts[mnum["super"]]
def part_bytes(name): p = sg[snum[name]]; return (p["last"] + 1 - p["first"]) * 512

# --- maint root: a copy with the current installer, service, enable link and pristine data written by debugfs
DEBUGFS_ENV = dict(os.environ, DEBUGFS_PAGER="__none__", PAGER="__none__")
def debugfs(fs, cmd):
    return subprocess.run(["debugfs", "-R", cmd, str(fs)], capture_output=True, env=DEBUGFS_ENV).stdout
def debugfs_w(fs, cmds):
    f = W / "debugfs.cmds"; f.write_text("\n".join(cmds) + "\n")
    r = subprocess.run(["debugfs", "-w", "-f", str(f), str(fs)], capture_output=True, env=DEBUGFS_ENV)
    (W / "debugfs.log").write_bytes(r.stdout + r.stderr)
def debugfs_ls(fs, path):  # [(name, mode, size)] from "ls -p": /ino/mode/uid/gid/name/size/
    out = []
    for line in debugfs(fs, f"ls -p {path}").decode(errors="replace").splitlines():
        f = line.split("/")
        if len(f) >= 7 and f[5] not in (".", ".."): out.append((f[5], int(f[2], 8), int(f[6] or 0)))
    return out
def debugfs_sha(fs, path):
    p = subprocess.Popen(["debugfs", "-R", f"cat {path}", str(fs)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=DEBUGFS_ENV)
    h = hashlib.sha256()
    for b in iter(lambda: p.stdout.read(16 << 20), b""): h.update(b)
    p.wait(); return h.hexdigest()
def file_sha(path): return gptlib.sha(path, 0, path.stat().st_size)
def rmtree_cmds(fs, path):
    if not debugfs(fs, f"stat {path}"): return []
    cmds = []
    for name, mode, _ in debugfs_ls(fs, path):
        if mode & 0o170000 == 0o040000: cmds += rmtree_cmds(fs, f"{path}/{name}")
        else: cmds += [f"cd {path}/.", f"rm {name}"]
    return cmds + ["cd /", f"rmdir {path}"]
def put(cmds, host, path, mode):
    d, name = path.rsplit("/", 1)
    cmds += [f"cd {d}/.", f"rm {name}", f"write {host} {name}", f"sif {name} mode 0{0o100000 | mode:o}", f"sif {name} uid 0", f"sif {name} gid 0", "cd /"]

maint = W / "maint.img"
maint.unlink(missing_ok=True)
run("cp", "--sparse=always", MAINT, maint, check=True)
if maint.stat().st_size > part_bytes("kano-maint"): sys.exit("maint root dump is larger than the stick's kano-maint partition")
if run("e2fsck", "-fp", maint).returncode not in (0, 1): sys.exit("e2fsck could not clean the maint root copy")
PRISTINE = "/var/lib/gbos-install/pristine"
prfiles = sorted(p for p in PR.iterdir() if p.is_file())
old = {n: s for n, _, s in debugfs_ls(maint, PRISTINE)}
st = re.search(r"(?s)Block size:\s+(\d+).*?Free blocks:\s+(\d+)|Free blocks:\s+(\d+).*?Block size:\s+(\d+)",
              debugfs(maint, "stats").decode(errors="replace"))
if not st: sys.exit("debugfs stats: no block size / free blocks")
bs, fb = (st.group(1), st.group(2)) if st.group(1) else (st.group(4), st.group(3))
free = int(bs) * int(fb)
if free + sum(old.values()) < sum(p.stat().st_size for p in prfiles) + (64 << 20):
    sys.exit("the maint root copy has no room for the pristine data")
# module-blacklist.txt as setup-stick.sh writes it: the template's module_blacklist, '-' as '_', sorted
template = (STICKSRC / "gbos-install.entry.template").read_text()
MODLIST = W / "module-blacklist.txt"
MODLIST.write_text("# module_blacklist of the installer entry (from gbos-install.entry.template, by setup-stick.sh): gbos-install\n"
                   "# refuses to run when the kernel command line does not block every one of these\n"
                   + "".join(f"{m}\n" for m in sorted({x.replace("-", "_") for x in blacklist(template)})))
cmds = ["mkdir /usr/local", "mkdir /usr/local/sbin", "mkdir /usr/local/share", "mkdir /usr/local/share/gbos-install",
        "mkdir /var/lib/gbos-install", "mkdir /etc/systemd/system/multi-user.target.wants"]
put(cmds, STICKSRC / "gbos-install.sh", "/usr/local/sbin/gbos-install", 0o755)
put(cmds, STICKSRC / "builtin-initcalls.txt", "/usr/local/share/gbos-install/builtin-initcalls.txt", 0o644)
put(cmds, MODLIST, "/usr/local/share/gbos-install/module-blacklist.txt", 0o644)
put(cmds, STICKSRC / "gbos-installer.service", "/etc/systemd/system/gbos-installer.service", 0o644)
ENGINE = REPO / "install/gbos-update.py"
put(cmds, ENGINE, "/usr/local/share/gbos-install/gbos-update.py", 0o644)
cmds += ["cd /etc/systemd/system/multi-user.target.wants/.", "rm gbos-installer.service",
         "symlink gbos-installer.service /etc/systemd/system/gbos-installer.service", "cd /"]
cmds += rmtree_cmds(maint, PRISTINE) + rmtree_cmds(maint, PRISTINE + ".new") + [f"mkdir {PRISTINE}"]
for p in prfiles: put(cmds, p, f"{PRISTINE}/{p.name}", 0o644)
debugfs_w(maint, cmds)
inj = []
for host, path, mode in ((STICKSRC / "gbos-install.sh", "/usr/local/sbin/gbos-install", "0755"),
                         (STICKSRC / "builtin-initcalls.txt", "/usr/local/share/gbos-install/builtin-initcalls.txt", "0644"),
                         (MODLIST, "/usr/local/share/gbos-install/module-blacklist.txt", "0644"),
                         (STICKSRC / "gbos-installer.service", "/etc/systemd/system/gbos-installer.service", "0644"),
                         (ENGINE, "/usr/local/share/gbos-install/gbos-update.py", "0644")):
    stat = debugfs(maint, f"stat {path}").decode(errors="replace")
    inj.append(debugfs(maint, f"cat {path}") == host.read_bytes() and re.search(rf"Type: regular\s+Mode:\s+0*{mode[1:]}\b", stat)
               and re.search(r"User:\s+0\s+Group:\s+0\b", stat))
link = debugfs(maint, "stat /etc/systemd/system/multi-user.target.wants/gbos-installer.service").decode(errors="replace")
inj.append("Type: symlink" in link and 'link dest: "/etc/systemd/system/gbos-installer.service"' in link)
inj.append(sorted(n for n, _, _ in debugfs_ls(maint, PRISTINE)) == sorted(p.name for p in prfiles))
inj += [debugfs_sha(maint, f"{PRISTINE}/{p.name}") == file_sha(p) for p in prfiles]
check("maint root holds the current gbos-install, its service, the enable link, builtin-initcalls.txt, module-blacklist.txt"
      " and the pristine data (read back)", all(inj),
      f"see {W / 'debugfs.log'}")
# debugfs does not keep every summary counter exact: let e2fsck settle them on this test copy, then it must be clean
fix = run("e2fsck", "-fy", maint, capture_output=True); (W / "e2fsck-fix.log").write_bytes(fix.stdout + fix.stderr)
chk = run("e2fsck", "-fn", maint, capture_output=True); (W / "e2fsck-check.log").write_bytes(chk.stdout + chk.stderr)
check("maint root copy is clean for e2fsck -fn after the injection (summary counters settled by e2fsck -fy)",
      fix.returncode in (0, 1) and chk.returncode == 0, f"see {W / 'e2fsck-fix.log'}")

# the maintenance kernel (XBOOTLDR vmlinuz-linux) must not have any blocked module built in
mtools("mcopy", XB, 0, "-o", "::/vmlinuz-linux", W / "vmlinuz-linux")
kimg = (W / "vmlinuz-linux").read_bytes()
kver = None
if kimg[0x202:0x206] == b"HdrS":
    o = struct.unpack_from("<H", kimg, 0x20E)[0] + 0x200
    kver = kimg[o:kimg.index(b"\0", o)].split(b" ")[0].decode(errors="replace")
builtin = debugfs(maint, f"cat /usr/lib/modules/{kver}/modules.builtin").decode(errors="replace") if kver else ""
BUILTIN = {Path(l).name.removesuffix(".ko").replace("-", "_") for l in builtin.split()}

# --- the test stick: the final image, the real stick's GPT, its XBOOTLDR and the injected maint root
if XB.stat().st_size > part_bytes("XBOOTLDR"): sys.exit("XBOOTLDR dump is larger than the partition")
stick_size = sh["alt"] * 512 + 512
STICK.unlink(missing_ok=True)
run("cp", "--reflink=auto", "--sparse=always", IMG, STICK, check=True)
with open(STICK, "r+b") as f:
    f.truncate(stick_size)
    f.seek(0); f.write(HEAD.read_bytes()); f.seek(stick_size - TAIL.stat().st_size); f.write(TAIL.read_bytes())
    for off, dump in ((XBOFF, XB), (MAINTOFF, maint)):
        f.seek(off)
        with open(dump, "rb") as d: shutil.copyfileobj(d, f, 16 << 20)
maint.unlink()
# a stick that never installed: no entry for the internal disk yet
mtools("mdeltree", STICK, XBOFF, "::/gbos-nvme", ok=False)
for e in ("gbos-nvme.conf", "gbos-nvme-debug.conf"): mtools("mdel", STICK, XBOFF, f"::/loader/entries/{e}", ok=False)
IMAP = gptlib.read_initcall_map(STICKSRC / "builtin-initcalls.txt")
entry = template.replace("@ROOT_PARTUUID@", sg[snum["kano-maint"]]["guid"]).replace("@INITCALL_BLACKLIST@", gptlib.initcalls(IMAP))
(W / "gbos-install.conf").write_text(entry)
mtools("mcopy", STICK, XBOFF, "-o", W / "gbos-install.conf", "::/loader/entries/gbos-install.conf")
check("stick XBOOTLDR has the entry rendered from the template",
      mtools("mtype", STICK, XBOFF, "::/loader/entries/gbos-install.conf").decode() == entry
      and template.count("@ROOT_PARTUUID@") == 1 and template.count("@INITCALL_BLACKLIST@") == 1 and "@" not in entry)
# QEMU-only entries: installer I/O on the serial port, no serial getty fighting for it; one without lockdown
serial_opts = " console=ttyS0,115200 gbos.install.tty=ttyS0 systemd.mask=serial-getty@ttyS0.service"
test_entry = re.sub(r"(?m)^title .*$", "title gbos-install-test", entry)
test_entry = re.sub(r"(?m)^(options .*)$", lambda m: m.group(1) + serial_opts, test_entry)
nold_entry = re.sub(r"(?m)^title .*$", "title gbos-install-nolockdown", test_entry).replace(" lockdown=integrity", "")
noic_entry = re.sub(r"(?m)^title .*$", "title gbos-install-noinitcall", test_entry).replace(
    " initcall_blacklist=" + gptlib.initcalls(IMAP) + " ", " initcall_blacklist=mei_me_driver_init ")
if noic_entry.count("initcall_blacklist=mei_me_driver_init ") != 1: sys.exit("cannot build the noinitcall entry")
nomod_entry = re.sub(r"(?m)^title .*$", "title gbos-install-nomodule", test_entry).replace(",spi_intel_pci,", ",")
if test_entry.count(",spi_intel_pci,") != 1 or "spi_intel_pci" in nomod_entry: sys.exit("cannot build the nomodule entry")
upd_entry = re.sub(r"(?m)^title .*$", "title gbos-update-test", test_entry)
upd_entry = re.sub(r"(?m)^(options .*)$", lambda m: m.group(1) + " gbos.update=1", upd_entry)
for name, text in (("gbos-install-test.conf", test_entry), ("gbos-install-nolockdown.conf", nold_entry),
                   ("gbos-install-noinitcall.conf", noic_entry), ("gbos-install-nomodule.conf", nomod_entry),
                   ("gbos-update-test.conf", upd_entry)):
    (W / name).write_text(text); mtools("mcopy", STICK, XBOFF, "-o", W / name, f"::/loader/entries/{name}")
def stick_default(name):
    (W / "loader.conf").write_text(f"default {name}\ntimeout 1\neditor no\n")
    mtools("mcopy", STICK, ESPOFF, "-o", W / "loader.conf", "::/loader/loader.conf")

# --- static checks of the installer entry against the image and the maintenance kernel
ENTRIES = esp_entries(IMG, ESPOFF)
check(f"the image's ESP holds a known entry set ({' '.join(ENTRIES)})", set(ENTRIES) in ENTRY_SETS, ENTRIES)
img_entries = {e: mtools("mtype", IMG, ESPOFF, f"::/loader/entries/{e}").decode() for e in ENTRIES}
tbl = blacklist(entry)
missing = sorted(set().union(*(blacklist(t) or set() for t in img_entries.values())) - (tbl or set()))
check("installer entry blocks every module the image's entries block", tbl and not missing, f"missing {missing}")
check("installer entry: lockdown=integrity, efi=noruntime, gbos.install=1, nvme allowed",
      {"lockdown=integrity", "efi=noruntime", "gbos.install=1"} <= set(options(entry)) and tbl and not {"nvme", "nvme_core"} & tbl
      and not any("nvme" in o for o in options(entry) if o.startswith("initcall_blacklist=")))
check(f"maintenance kernel {kver}: modules.builtin readable", bool(BUILTIN))
built_blocked = BUILTIN & (tbl or set())
print("blocked modules built into the maintenance kernel:", sorted(built_blocked))
check("every blocked built-in module is covered by builtin-initcalls.txt", BUILTIN and not built_blocked - set(IMAP),
      f"not covered: {sorted(built_blocked - set(IMAP))}")
ic = set(",".join(o.split("=", 1)[1] for o in options(entry) if o.startswith("initcall_blacklist=")).split(","))
check("entry's initcall_blacklist has every initcall of builtin-initcalls.txt; its modules are in module_blacklist",
      set(gptlib.initcalls(IMAP).split(",")) <= ic and set(IMAP) <= (tbl or set()))

# --- QEMU with the serial port as the installer's terminal and QMP for the NVMe's write counters
class QMP:
    def __init__(self, path, proc):
        end = time.time() + 60
        while True:
            s = socket.socket(socket.AF_UNIX)
            try: s.connect(str(path)); break
            except OSError:
                s.close()
                if time.time() > end or proc.poll() is not None: raise RuntimeError("no QMP socket")
                time.sleep(0.2)
        self.f = s.makefile("rwb"); self.read(); self.cmd("qmp_capabilities")
    def read(self):
        line = self.f.readline()
        if not line: raise RuntimeError("QMP closed")
        return json.loads(line)
    def cmd(self, name):
        self.f.write(json.dumps({"execute": name}).encode() + b"\n"); self.f.flush()
        while True:
            r = self.read()
            if "return" in r: return r["return"]
            if "error" in r: raise RuntimeError(r["error"])
    def nvme(self):
        return next((d["stats"] for d in self.cmd("query-blockstats") if d.get("device") == "nv"), None)

def clean(out): return re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", out).decode(errors="replace")

def qemu(tag, drives, steps=(), seconds=900, serial=True):
    """steps: [(regex, reply, label)]: when regex shows up on the serial port, record the NVMe counters under label,
    then send reply. An unexpected "Press Enter to poweroff/reboot" is answered too (label "unexpected-end")."""
    vars_fd = W / f"{tag}-vars.fd"; shutil.copy(VARS, vars_fd)
    tmp = Path(tempfile.mkdtemp(prefix="gbq"))     # short path: unix socket names are limited to 108 bytes
    qmp_path, sock = tmp / "qmp", tmp / "serial"
    cmd = [QEMU, "-machine", "q35,accel=kvm", "-cpu", "host", "-m", "8192", "-smp", "4", "-no-reboot",
           "-display", "none", "-vga", "none",
           "-drive", f"if=pflash,format=raw,readonly=on,file={CODE}", "-drive", f"if=pflash,format=raw,file={vars_fd}",
           "-device", "qemu-xhci,id=xhci,bus=pcie.0,addr=0x3", "-qmp", f"unix:{qmp_path},server=on,wait=off",
           "-monitor", "none", "-nic", "none"] + drives
    cmd += ["-serial", f"unix:{sock},server=on,wait=on"] if serial else ["-serial", f"file:{W / (tag + '-serial.log')}"]
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    if QLIB: env["LD_LIBRARY_PATH"] = QLIB
    print("+", " ".join(map(str, cmd)), flush=True)
    p = subprocess.Popen(cmd, env=env)
    res = dict(alive=False, text="", stats={})
    out = b""
    try:
        if serial:
            end = time.time() + 60
            while True:
                s = socket.socket(socket.AF_UNIX)
                try: s.connect(str(sock)); break
                except OSError:
                    s.close()
                    if time.time() > end or p.poll() is not None: raise RuntimeError("no serial socket")
                    time.sleep(0.2)
            s.settimeout(1)
        q = QMP(qmp_path, p)
        steps, pos, end = list(steps), 0, time.time() + seconds
        while time.time() < end and p.poll() is None:
            if not serial:
                time.sleep(2); continue
            try:
                b = s.recv(65536)
                if not b: break
                out += b
            except socket.timeout:
                pass
            text = clean(out)
            m = re.search(steps[0][0], text[pos:]) if steps else None
            if m: _, reply, label = steps.pop(0)
            else:
                m = re.search(r"Press Enter to (poweroff|reboot)", text[pos:])
                if not m: continue
                reply, label = "\r", "unexpected-end"
            pos += m.end()
            if label: res["stats"][label] = q.nvme()
            print(f"[{tag}] saw {m.group(0)!r}, sending {reply!r}", flush=True)
            time.sleep(0.5); s.sendall(reply.encode())
            out += f"\n<<<sent {reply!r}>>>\n".encode()
        if serial:
            try: p.wait(60)
            except subprocess.TimeoutExpired: pass
        res["alive"] = p.poll() is None
        if res["alive"]:
            try: res["stats"]["end"] = q.nvme()
            except Exception as e: print(f"[{tag}] QMP: {e}", flush=True)
    finally:
        if p.poll() is None:
            p.terminate()
            try: p.wait(30)
            except subprocess.TimeoutExpired: p.kill(); p.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    if serial: (W / f"{tag}-serial.log").write_bytes(out)
    res["text"] = clean(out)
    return res

STICK_DRV = ["-drive", f"file={STICK},if=none,id=stick,format=raw", "-device", "usb-storage,bus=xhci.0,drive=stick,bootindex=1"]
def nvme_drv(path): return ["-drive", f"file={path},if=none,id=nv,format=raw", "-device", f"nvme,drive=nv,serial={NVME_SERIAL},bus=pcie.0,addr=0x2"]
def no_writes(st):
    return st is not None and st.get("wr_bytes") == 0 and st.get("wr_operations") == 0 and st.get("unmap_operations", 0) == 0
def fingerprint(path):
    """SHA-256 over the data extents (offsets and bytes) of a sparse file, and its allocated blocks."""
    h = hashlib.sha256(); fd = os.open(path, os.O_RDONLY); off = 0; size = os.fstat(fd).st_size
    try:
        while off < size:
            try: d = os.lseek(fd, off, os.SEEK_DATA)
            except OSError: break
            e = os.lseek(fd, d, os.SEEK_HOLE); h.update(struct.pack("<QQ", d, e))
            while d < e:
                b = os.pread(fd, min(e - d, 16 << 20), d)
                if not b: raise ValueError(f"{path}: short read")
                h.update(b); d += len(b)
            off = e
    finally:
        os.close(fd)
    return h.hexdigest(), os.stat(path).st_blocks
CONFIRM = r"Type the last 4 characters of the serial"
STOPPED = "Stopped before anything was written"

def refused(tag, nv, steps, reason):
    before = fingerprint(nv)
    r = qemu(tag, STICK_DRV + nvme_drv(nv), steps, 1800)
    t = r["text"]
    check(f"{tag}: refused ({reason}) before the serial prompt", reason in t and STOPPED in t and not re.search(CONFIRM, t))
    check(f"{tag}: zero writes to the NVMe (QMP)", no_writes(r["stats"].get("stop") or r["stats"].get("unexpected-end")),
          r["stats"])
    check(f"{tag}: NVMe file unchanged", fingerprint(nv) == before)

# --- NVMe images: blank, and one with a ChromeOS-Flex-like GPT (12 partitions) and data where the image will go
for p in (BLANK, FLEX): p.unlink(missing_ok=True)
with open(BLANK, "wb") as f: f.truncate(NVME_BYTES)
SECT = NVME_BYTES // 512
CROS_KERN, CROS_ROOT, LINUX = "fe3a2a5d-4f32-41a7-b725-accc3285a309", "3cb8e202-3b7e-47dd-8a3c-7ff2a13cfcec", "0fc63daf-8483-4772-8e79-3d69d8477de4"
layout = [(11, "RWFW", "cab6e88e-abf3-4102-a07a-d4bb9be3c1d3", 8), (6, "KERN-C", CROS_KERN, 1), (7, "ROOT-C", CROS_ROOT, 1),
          (9, "reserved", "2e0a753d-9e48-43b0-8337-b15192cb1b5e", 1), (10, "reserved", "2e0a753d-9e48-43b0-8337-b15192cb1b5e", 1),
          (2, "KERN-A", CROS_KERN, 64), (4, "KERN-B", CROS_KERN, 64), (8, "OEM", LINUX, 16),
          (12, "EFI-SYSTEM", "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", 64), (5, "ROOT-B", CROS_ROOT, 4096),
          (3, "ROOT-A", CROS_ROOT, 4096), (1, "STATE", LINUX, None)]
flex, lba = [], 2048
for num, name, typ, mib in layout:
    last = SECT - 34 if mib is None else lba + mib * 2048 - 1
    flex.append(dict(num=num, name=name, type=typ, first=lba, last=last, attrs=(1 << 48) | (1 << 56) if name == "KERN-A" else 0))
    lba = last + 1
rnd = random.Random(20261012)
with open(FLEX, "wb") as f:
    f.truncate(NVME_BYTES)
    for off in sorted({p["first"] * 512 for p in flex} | {m["start"] for m in mparts.values()} | {SECT * 512 - (4 << 20)}):
        f.seek(off); f.write(rnd.randbytes(1 << 20))
    f.seek(UD_START * 512); f.write(rnd.randbytes(16 << 20))
    state = next(p for p in flex if p["name"] == "STATE")["first"] * 512
    f.seek(state + 1024 + 56); f.write(b"\x53\xef")   # ext4 magic in STATE
    gptlib.write_gpt(f, SECT, flex)
check("Flex-like NVMe: 12 partitions, valid GPT", len(gptlib.read_gpt(FLEX)[1]) == 12)

# --- install checks
def nvme_entry(title, t):
    t = re.sub(r"(?m)^title .*$", f"title {title}", t)
    t = re.sub(r"(?m)^linux .*$", "linux /gbos-nvme/kernel.efi", t)
    return re.sub(r"(?m)^initrd .*$", "initrd /gbos-nvme/initrd.img", t)

def installed(tag, nv, r):
    t = r["text"]
    check(f"{tag}: installer reports INSTALL OK", "INSTALL OK" in t and not re.search(r"(?m)^ERROR: ", t))
    check(f"{tag}: kernel lockdown was on in the installer boot", re.search(r"ok +kernel lockdown: (integrity|confidentiality)", t))
    check(f"{tag}: built-in TPM/nvram drivers blocked in the installer boot (runtime check)", re.search(BUILTIN_OK, t))
    check(f"{tag}: whole module_blacklist on the command line, none of it loaded (runtime check)", re.search(CMDLINE_OK, t))
    check(f"{tag}: zero writes to the NVMe before the confirmation (QMP)", no_writes(r["stats"].get("prompt")), r["stats"].get("prompt"))
    try:
        nh, ng = gptlib.read_gpt(nv, backup=True); gerr = ""
    except ValueError as e:
        nh, ng, gerr = None, {}, str(e)
    check(f"{tag}: NVMe GPT valid (MBR, both headers and entry arrays, CRCs)", nh is not None, gerr)
    if nh is None: return None
    bad = []
    for n, m in mparts.items():
        e = ng.get(n)
        first, last = (UD_START, nh["last"]) if m["name"] == "userdata" else (m["start"] // 512, (m["start"] + m["len"]) // 512 - 1)
        if not e or (e["name"], e["type"], e["first"], e["last"], e["attrs"]) != (m["name"], m["type"], first, last, m["attrs"]):
            bad.append((n, m["name"], e))
    check(f"{tag}: every partition's start/end/type/name/attrs as in the manifest, nothing else", not bad and sorted(ng) == sorted(mparts), bad)
    ud = ng.get(mnum["userdata"])
    if not ud: return None
    check(f"{tag}: userdata from {UD_START} (new start) to {nh['last']} (last usable sector)", (ud["first"], ud["last"]) == (UD_START, nh["last"]))
    guids = [v["guid"] for v in ng.values()]
    others = {v["guid"] for v in sg.values()} | {v["guid"] for v in ig.values()}
    check(f"{tag}: NVMe partition GUIDs new and unique", len(set(guids)) == len(guids) and not set(guids) & others)
    bad = []
    for n, m in mparts.items():
        if m["kind"] == "new" or m["name"] == "EFI-SYSTEM": continue
        try: ok = gptlib.sha(nv, m["start"], m["len"]) == m["sha"]
        except ValueError: ok = False
        if not ok: bad.append(m["name"])
    check(f"{tag}: super and every unchanged partition equal the image (SHA-256)", not bad, bad)
    with open(nv, "rb") as f: f.seek(UD_START * 512); udhead = f.read(16 << 20)
    check(f"{tag}: userdata head empty (Android formats it)", udhead == bytes(16 << 20))
    eoff = ng[mnum["EFI-SYSTEM"]]["first"] * 512
    boot_a = ng[mnum["boot_a"]]["guid"]
    initrd = mtools("mtype", nv, eoff, "::/initrd.img")
    want = gptlib.bootconfig_set_boot_part(mtools("mtype", IMG, ESPOFF, "::/initrd.img"), boot_a)
    check(f"{tag}: NVMe ESP initrd = the image's with boot_part_uuid = NVMe boot_a", initrd == want)
    kernel = mtools("mtype", nv, eoff, "::/kernel.efi")
    check(f"{tag}: NVMe ESP kernel = the image's", kernel == mtools("mtype", IMG, ESPOFF, "::/kernel.efi"))
    ok = True
    for e in ENTRIES:
        orig, got = img_entries[e], mtools("mtype", nv, eoff, f"::/loader/entries/{e}").decode()
        o = options(got)
        ok &= orig.count(LIFT) == 1 and got == orig.replace(LIFT, "initcall_blacklist=mei_me_driver_init") and "nvme_init" not in got \
            and "efi=noruntime" in o and blacklist(got) == blacklist(orig)
    check(f"{tag}: NVMe entries = the image's with only nvme_init lifted (module_blacklist, efi=noruntime kept)", ok)
    check(f"{tag}: NVMe ESP holds exactly the image's entries", esp_entries(nv, eoff) == ENTRIES, esp_entries(nv, eoff))
    loader = mtools("mtype", nv, eoff, "::/loader/loader.conf").decode()
    check(f"{tag}: NVMe loader.conf exact", loader == "default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n", repr(loader))
    xk = mtools("mtype", STICK, XBOFF, "::/gbos-nvme/kernel.efi", ok=False)
    xi = mtools("mtype", STICK, XBOFF, "::/gbos-nvme/initrd.img", ok=False)
    check(f"{tag}: stick XBOOTLDR kernel and initrd for the internal disk = the NVMe ESP's", xk == kernel and xi == initrd)
    quiet = mtools("mtype", nv, eoff, "::/loader/entries/lapis-quiet.conf").decode()
    debug = mtools("mtype", nv, eoff, "::/loader/entries/lapis-debug.conf").decode()
    xq = mtools("mtype", STICK, XBOFF, "::/loader/entries/gbos-nvme.conf", ok=False).decode()
    xd = mtools("mtype", STICK, XBOFF, "::/loader/entries/gbos-nvme-debug.conf", ok=False).decode()
    check(f"{tag}: stick entries for the internal disk = the NVMe entries pointing at /gbos-nvme",
          xq == nvme_entry("Googlebook (internal disk)", quiet) and xd == nvme_entry("Googlebook (internal disk) serial-debug", debug)
          and "linux /gbos-nvme/kernel.efi\n" in xq and "initrd /gbos-nvme/initrd.img\n" in xq)
    sl = mtools("mtype", STICK, ESPOFF, "::/loader/loader.conf").decode()
    check(f"{tag}: stick menu starts the internal disk by default", sl == "default gbos-nvme.conf\ntimeout 5\neditor no\nauto-firmware no\n", repr(sl))
    return ud

def nvme_boot(tag, nv, ud):
    r = qemu(f"{tag}-boot", ["-drive", f"file={nv},if=none,id=nv,format=raw,snapshot=on",
                             "-device", f"nvme,drive=nv,serial={NVME_SERIAL},bus=pcie.0,addr=0x2,bootindex=1"], serial=False, seconds=240)
    st = r["stats"].get("end") or {}
    hi = st.get("wr_highest_offset", 0)
    print(f"{tag}-boot: wr_bytes", st.get("wr_bytes"), "wr_highest_offset", hi, "userdata", ud["first"] * 512, "-", (ud["last"] + 1) * 512)
    check(f"{tag}: NVMe-only boot still running at the end", r["alive"])
    check(f"{tag}: Android wrote /data (>= 16 MiB), highest write inside userdata",
          st.get("wr_bytes", 0) >= 16 << 20 and ud["first"] * 512 < hi <= (ud["last"] + 1) * 512)

ONLY = set(filter(None, os.environ.get("GBOS_STICK_SCENARIOS", "").split(",")))
want = lambda name: not ONLY or name in ONLY
if ONLY: print("scenarios:", ",".join(sorted(ONLY)))

def update_ranges(nv, ud, slot):
    """byte ranges an update into the other slot must not change: GPT copies, a userdata marker, the data partitions
    and the running slot's boot partitions"""
    nh, ng = gptlib.read_gpt(nv, backup=True)
    rng = {"head": (0, 1 << 20), "tail": (NVME_BYTES - (1 << 20), 1 << 20),
           "userdata-marker": (ud["first"] * 512 + (64 << 20), 1 << 20)}
    for name in ("metadata", "misc", "frp_persist", "desktop_security_persist", "desktop_security_storage",
                 f"boot_{slot}", f"init_boot_{slot}", f"vendor_boot_{slot}", f"vbmeta_{slot}"):
        e = ng[mnum[name]]; rng[name] = (e["first"] * 512, (e["last"] + 1 - e["first"]) * 512)
    return {"ranges": rng, "sha": {k: gptlib.sha(nv, a, n) for k, (a, n) in rng.items()}}

def updated(tag, nv, r, before, t="b", c="a", c_initrd="/initrd.img"):
    """after the update mode of gbos-install: slot t holds the image, slot c and every data partition unchanged, both
    menus start t with c as the previous version (c_initrd: slot c's initrd on the NVMe's ESP)"""
    txt = r["text"]
    check(f"{tag}: update mode reports UPDATE OK", f"UPDATE OK: slot {t}" in txt and "Update done" in txt and not re.search(r"(?m)^(ERROR|FAILED|REFUSED): ", txt))
    check(f"{tag}: runtime checks passed in the update boot", re.search(BUILTIN_OK, txt) and re.search(CMDLINE_OK, txt))
    # (the answer is sent as soon as the prompt starts, so the transcript may split the prompt line)
    check(f"{tag}: the updater showed the installed slot before asking", f"current slot {c}" in txt and re.search(CONFIRM, txt)
          and "to update, or just Enter to cancel" in txt and txt.index(f"current slot {c}") < re.search(CONFIRM, txt).start())
    check(f"{tag}: the stick's menu was prepared before any slot was written",
          "ok   stickprep:" in txt and txt.index("ok   stickprep:") < txt.index("ok   removed:") < txt.index("ok   dynamic:"))
    check(f"{tag}: zero writes to the NVMe before the confirmation (QMP)", no_writes(r["stats"].get("prompt")), r["stats"].get("prompt"))
    after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}
    check(f"{tag}: GPT, userdata marker, metadata, misc, frp and slot {c} unchanged byte for byte",
          after == before["sha"], [k for k in after if after[k] != before["sha"][k]])
    ins = run(sys.executable, ENGINE, "inspect", nv, capture_output=True, text=True)
    check(f"{tag}: inspect: current slot {t}, previous version slot {c}", f"current slot {t}" in ins.stdout and f"previous slot on the menu: {c}" in ins.stdout, ins.stdout[-400:])
    nh, ng = gptlib.read_gpt(nv, backup=True)
    eoff = ng[mnum["EFI-SYSTEM"]]["first"] * 512
    q = mtools("mtype", nv, eoff, "::/loader/entries/lapis-quiet.conf").decode()
    pq = mtools("mtype", nv, eoff, "::/loader/entries/prev-lapis-quiet.conf", ok=False).decode()
    check(f"{tag}: NVMe menu: current entries start slot {t} (lifted), previous version slot {c}",
          f"initrd /gbos/{t}/initrd.img" in q and "initcall_blacklist=mei_me_driver_init" in q and "nvme_init" not in q
          and f"initrd {c_initrd}" in pq and "(previous version)" in pq)
    xq = mtools("mtype", STICK, XBOFF, "::/loader/entries/gbos-nvme.conf", ok=False).decode()
    xd = mtools("mtype", STICK, XBOFF, "::/loader/entries/gbos-nvme-debug.conf", ok=False).decode()
    xp = mtools("mtype", STICK, XBOFF, "::/loader/entries/gbos-nvme-prev.conf", ok=False).decode()
    check(f"{tag}: stick menu: internal disk (and serial-debug) = slot {t}, a previous-version entry with copies of slot {c}'s files",
          f"initrd /gbos-nvme/{t}/initrd.img" in xq and f"initrd /gbos-nvme/{t}/initrd.img" in xd and f"initrd /gbos-nvme/{c}/initrd.img" in xp
          and mtools("mtype", STICK, XBOFF, f"::/gbos-nvme/{c}/initrd.img", ok=False) == mtools("mtype", nv, eoff, "::" + c_initrd)
          and mtools("mtype", STICK, XBOFF, f"::/gbos-nvme/{t}/initrd.img", ok=False) == mtools("mtype", nv, eoff, f"::/gbos/{t}/initrd.img"))
    check(f"{tag}: the stick's menu starts the internal disk by default",
          mtools("mtype", STICK, ESPOFF, "::/loader/loader.conf", ok=False).decode().startswith("default gbos-nvme.conf\n"))

# 1. no lockdown: refused at once
if want("refusals"):
    stick_default("gbos-install-nolockdown.conf")
    refused("nolockdown", BLANK, [(r"Press Enter to poweroff", "\r", "stop")], "kernel lockdown is off")
    check("nolockdown: NVMe still has no data blocks", os.stat(BLANK).st_blocks == 0)
    stick_default("gbos-install-noinitcall.conf")
    refused("noinitcall", BLANK, [(r"Press Enter to poweroff", "\r", "stop")], "built-in drivers are not blocked")
    check("noinitcall: NVMe still has no data blocks", os.stat(BLANK).st_blocks == 0)
    stick_default("gbos-install-nomodule.conf")
    refused("nomodule", BLANK, [(r"Press Enter to poweroff", "\r", "stop")], "module_blacklist lacks spi_intel_pci")
    check("nomodule: NVMe still has no data blocks", os.stat(BLANK).st_blocks == 0)

# 2. one byte of the stick's super changed: refused before the serial prompt; the Flex NVMe is untouched
if want("super-mismatch"):
    stick_default("gbos-install-test.conf")
    BAD = SUPER["start"] + (1 << 20) + 7
    with open(STICK, "r+b") as f:
        f.seek(BAD); orig = f.read(1); f.seek(BAD); f.write(bytes([orig[0] ^ 0xFF]))
    try:
        refused("super-mismatch", FLEX, [(r"Press Enter to poweroff", "\r", "stop")], "super on this stick does not match")
    finally:
        with open(STICK, "r+b") as f: f.seek(BAD); f.write(orig)
    check("super-mismatch: stick super restored", gptlib.sha(STICK, SUPER["start"], SUPER["len"]) == SUPER["sha"])

# 3. cancel on the blank NVMe
if want("cancel"):
    stick_default("gbos-install-test.conf")
    r = qemu("cancel", STICK_DRV + nvme_drv(BLANK), [(CONFIRM, "ZZZZ\r", "prompt"), (r"Press Enter to poweroff", "\r", "stop")], 1800)
    check("cancel: installer shows the NVMe and asks for its serial", NVME_SERIAL in r["text"] and re.search(CONFIRM, r["text"]))
    check("cancel: wrong answer cancels", "Cancelled. Nothing was written." in r["text"])
    check("cancel: installer's runtime check of the built-in drivers passed", re.search(BUILTIN_OK, r["text"]))
    check("cancel: installer's runtime check of the whole module_blacklist passed", re.search(CMDLINE_OK, r["text"]))
    check("cancel: zero writes to the NVMe (QMP)", no_writes(r["stats"].get("stop")), r["stats"].get("stop"))
    check("cancel: NVMe still has no data blocks", os.stat(BLANK).st_blocks == 0)

# 4. install over the Flex-like NVMe, 5. install on the blank NVMe; each then boots alone
for tag, nv in (("install-flex", FLEX), ("install-blank", BLANK)):
    if not (want(tag) or (tag == "install-blank" and want("update"))):
        continue
    stick_default("gbos-install-test.conf")
    r = qemu(tag, STICK_DRV + nvme_drv(nv), [(CONFIRM, NVME_SERIAL[-4:].lower() + "\r", "prompt"), (r"Press Enter to reboot", "\r", "done")], 3600)
    ud = installed(tag, nv, r)
    if ud: nvme_boot(tag, nv, ud)
    # 6. update mode on the installed NVMe: the same image into slot b, /data and slot a untouched
    if ud and tag == "install-blank" and want("update"):
        with open(nv, "r+b") as f:                      # pretend user data: must survive the update untouched
            f.seek(ud["first"] * 512 + (64 << 20)); f.write(b"USER-DATA-MARKER" * 4096)
        before = update_ranges(nv, ud, "a")
        stick_default("gbos-update-test.conf")
        r = qemu("update-blank", STICK_DRV + nvme_drv(nv), [(CONFIRM, NVME_SERIAL[-4:].lower() + "\r", "prompt"),
                                                         (r"Press Enter to reboot", "\r", "done")], 5400)
        updated("update-blank", nv, r, before)
        nvme_boot("update-blank", nv, ud)
        # 7. the second update, slot b running: the image goes into slot a (the stick's 'previous version' was slot a)
        before = update_ranges(nv, ud, "b")
        stick_default("gbos-update-test.conf")
        r = qemu("update2-blank", STICK_DRV + nvme_drv(nv), [(CONFIRM, NVME_SERIAL[-4:].lower() + "\r", "prompt"),
                                                          (r"Press Enter to reboot", "\r", "done")], 5400)
        updated("update2-blank", nv, r, before, t="a", c="b", c_initrd="/gbos/b/initrd.img")
        nvme_boot("update2-blank", nv, ud)
    if not os.environ.get("GBOS_STICK_KEEP"): nv.unlink()

failed = [k for k, v in results.items() if not v]
print("STICK INSTALLER TEST", "OK" if not failed else "FAILED", f"({sum(results.values())}/{len(results)})")
for k in failed: print("  failed:", k)
sys.exit(0 if not failed else 1)
