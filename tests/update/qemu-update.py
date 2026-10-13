#!/usr/bin/env python3
"""End-to-end test of install/gbos-update.py with real release images in QEMU (Linux, KVM, no root, no real disk).

usage: tests/update/qemu-update.py <empty work dir> <release N .img> <release N+1 .img (with .sha256, same AVB key)>
env:   GBOS_QEMU, GBOS_QEMU_LIB, GBOS_OVMF_CODE, GBOS_OVMF_VARS (as for builder/build.sh), GBOS_QEMU_MEM (MiB, 6144),
       GBOS_QEMU_SECONDS (420 per boot)

The disk is a copy of release N on a QEMU USB stick (writable this time: the test is about what stays on it).
  1. N boots (its serial-debug options, console on COM1): Android formats /data and comes up on slot a;
  2. gbos-update.py apply --mode stick puts N+1 into slot b; the data partitions must be unchanged byte for byte;
  3. firmware -> the disk's ESP -> systemd-boot -> the default entry (slot b, quiet): Android writes /data;
  4. the new serial-debug entry (slot b) booted directly: slot b's logical partitions are mapped, /data mounts as
     the same file system (f2fs UUID unchanged) and is not formatted again, system_server runs;
  5. the previous-version serial-debug entry (slot a) booted the same way: slot a, same /data, system_server runs;
  6. firmware -> systemd-boot, the "(previous version)" quiet entry chosen with keys sent over QMP: Android writes /data;
  7. the second update while slot b runs: N+1 again into slot a (the slot of the previous version); slot b's boot
     partitions and the data partitions unchanged, the menu starts a with b as the previous version;
  8. the boot chain to the default entry (slot a now) and slot a's serial-debug entry: slot a mounted, same /data;
  9. the previous version (slot b) serial-debug entry: slot b mounted, same /data.
Results go to <work dir>/RESULT.txt; exit 0 only when every check passes.
"""
import hashlib, json, os, re, shutil, socket, struct, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "install/gbos-update.py"
W = Path(sys.argv[1]).resolve()
N, N1 = Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()
if W.exists() and any(W.iterdir()):
    sys.exit(f"{W} must be empty")
W.mkdir(parents=True, exist_ok=True)
QEMU = os.environ.get("GBOS_QEMU", "/usr/bin/qemu-system-x86_64")
QLIB = os.environ.get("GBOS_QEMU_LIB", "")
OVMF_CODE = os.environ.get("GBOS_OVMF_CODE", "/usr/share/edk2/x64/OVMF_CODE.4m.fd")
OVMF_VARS = os.environ.get("GBOS_OVMF_VARS", "/usr/share/edk2/x64/OVMF_VARS.4m.fd")
MEM = os.environ.get("GBOS_QEMU_MEM", "6144")
SECS = int(os.environ.get("GBOS_QEMU_SECONDS", "420"))
ENV = dict(os.environ, MTOOLS_SKIP_CHECK="1")
spec = __import__("importlib.util").util.spec_from_file_location("gbu", ENGINE)
gbu = __import__("importlib.util").util.module_from_spec(spec)
spec.loader.exec_module(gbu)
RES = []
LOG = open(W / "RESULT.txt", "w")


def note(s):
    print(s, flush=True)
    LOG.write(s + "\n"); LOG.flush()


def ok(c, what):
    RES.append((bool(c), what))
    note(("PASS " if c else "FAIL ") + what)
    return c


D = W / "disk.img"
subprocess.run(["cp", "--reflink=auto", str(N), str(D)], check=True)
disk = gbu.Disk(str(D))
G = gbu.gpt_read(disk)
E = G["EFI-SYSTEM"]["start"]
UD = G["userdata"]


def rsha(name):
    p = G[name]
    return gbu.Disk(str(D)).sha(p["start"], p["len"])


def f2fs_uuid():
    with open(D, "rb") as f:
        f.seek(UD["start"] + 1024)
        sb = f.read(128)
    return sb[108:124].hex() if struct.unpack_from("<I", sb, 0)[0] == 0xF2F52010 else None   # f2fs_super_block.uuid


def esp(path):
    t = W / "esp-file"
    t.unlink(missing_ok=True)
    subprocess.run(["mcopy", "-n", "-i", f"{D}@@{E}", "::" + path, str(t)], check=True, env=ENV)
    return t.read_bytes()


def qmp(sock, *cmds):
    s = socket.socket(socket.AF_UNIX); s.settimeout(30); s.connect(str(sock)); f = s.makefile("rwb")

    def call(c):
        f.write(json.dumps(c).encode() + b"\n"); f.flush()
        while True:
            r = json.loads(f.readline())
            if "return" in r or "error" in r:
                return r
    json.loads(f.readline()); call({"execute": "qmp_capabilities"})
    out = [call(c) for c in cmds]
    s.close()
    return out


QDIR = Path(tempfile.mkdtemp(prefix="gbos-qu-"))


def boot(tag, extra, keys=None):
    """QEMU with the disk writable on USB; returns (alive at the end, usb write stats, serial log)."""
    serial, sock, vars_fd = W / f"{tag}-serial.log", QDIR / f"{tag}.qmp", W / f"{tag}-vars.fd"
    shutil.copy(OVMF_VARS, vars_fd)
    cmd = [QEMU, "-machine", "q35,accel=kvm", "-cpu", "host", "-m", MEM, "-smp", "4", "-no-reboot", "-vga", "none",
           "-display", "none", "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
           "-drive", f"if=pflash,format=raw,file={vars_fd}", "-device", "qemu-xhci,id=xhci,bus=pcie.0,addr=0x3",
           "-drive", f"file={D},if=none,id=usbdisk,format=raw,cache=unsafe",
           "-device", "usb-storage,bus=xhci.0,drive=usbdisk,bootindex=1",
           "-serial", f"file:{serial}", "-monitor", "none", "-qmp", f"unix:{sock},server=on,wait=off"] + extra
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    if QLIB:
        env["LD_LIBRARY_PATH"] = QLIB
    note(f"[{time.strftime('%T')}] boot {tag}")
    p = subprocess.Popen(list(map(str, cmd)), env=env)
    end = time.time() + SECS
    sent = False
    while time.time() < end and p.poll() is None:
        time.sleep(2)
        if keys and not sent and serial.exists() and "Boot in" in serial.read_text(errors="replace"):
            time.sleep(1)
            for k in keys:
                qmp(sock, {"execute": "send-key", "arguments": {"keys": [{"type": "qcode", "data": k}]}})
                time.sleep(0.4)
            sent = True
    alive, stats = p.poll() is None, None
    if alive:
        try:
            r = qmp(sock, {"execute": "query-blockstats"})[0]["return"]
            stats = next(d["stats"] for d in r if d.get("device") == "usbdisk" or d.get("qdev", "").endswith("usbdisk"))
        except Exception as e:
            note(f"{tag}: QMP failed: {e}")
        try:
            qmp(sock, {"execute": "quit"})
        except Exception:
            pass
        try:
            p.wait(60)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()
    log = serial.read_text(errors="replace") if serial.exists() else ""
    note(f"{tag}: alive {alive}, usb {None if stats is None else (stats.get('wr_bytes'), stats.get('wr_highest_offset'))}")
    return alive, stats, log, sent


def direct(tag, entry):
    e = gbu.entry_parse(esp("/loader/entries/" + entry).decode())
    k, i = W / f"{tag}-kernel.efi", W / f"{tag}-initrd.img"
    k.write_bytes(esp(e["linux"]))
    i.write_bytes(b"".join(esp(x) for x in e["initrd"]))
    opts = [o for o in e["options"].split() if not o.startswith(("console=", "earlycon="))] + \
           ["console=ttyS0,115200n8", "earlycon=uart8250,io,0x3f8,115200n8"]
    return boot(tag, ["-kernel", k, "-initrd", i, "-append", " ".join(opts)])


android_up = lambda log: "init second stage started" in log and "u:r:system_server:s0" in log
FMT = re.compile(r"(?i)(Format(ting)? \S*userdata|fs_mgr_do_format|make_f2fs|mkfs\.f2fs)")
def mounted(log):
    """{mount point: logical partition} of the first-stage mounts: every dynamic partition with extents is mapped
    (both slots), the slot that runs is the one whose partitions carry verity and are mounted."""
    dm = dict((d, n) for n, d in re.findall(r"Created logical partition (\w+) on device /dev/block/(dm-\d+)", log))
    out, under = {}, None
    for line in log.splitlines():
        m = re.search(r"Built verity table: '\d+ /dev/block/(dm-\d+)", line)
        if m:
            under = m.group(1)
        m = re.search(r"__mount\(source=/dev/block/(dm-\d+),target=(/[\w/]+),", line)
        if m:
            out[m.group(2)] = dm.get(under or m.group(1), dm.get(m.group(1)))
            under = None
    return out


def slot_mounted(log, s):
    m = mounted(log)
    want = {"/system": "system", "/vendor": "vendor", "/product": "product", "/system_ext": "system_ext"}
    return all(m.get(k) == f"{v}_{s}" for k, v in want.items()), m
writes_data = lambda st: st and st.get("wr_bytes", 0) >= 16 << 20 and UD["start"] <= st.get("wr_highest_offset", 0) <= UD["start"] + UD["len"]

# 1. release N on slot a, first boot
alive, st, log, _ = direct("n-first", "lapis-debug.conf")
ok(alive and android_up(log), "N boots on slot a (system_server runs)")
good, m = slot_mounted(log, "a")
ok(good, f"N mounts slot a's partitions: {m}")
fmt1 = FMT.findall(log)
ok("target=/data,type=f2fs)=0" in log, "N mounts /data as f2fs")
U1 = f2fs_uuid()
ok(U1 is not None, f"/data is an f2fs file system after N's first boot (uuid {U1}; formatted in this boot: {bool(fmt1)})")

# 2. the update
keep = ("userdata", "metadata", "misc", "frp_persist", "desktop_security_persist", "desktop_security_storage",
        "boot_a", "init_boot_a", "vendor_boot_a", "vbmeta_a")
before = {n: rsha(n) for n in keep}
r = subprocess.run([sys.executable, str(ENGINE), "apply", str(D), "--image", str(N1), "--mode", "stick"],
                   capture_output=True, text=True)
note(r.stdout + r.stderr)
ok(r.returncode == 0 and "UPDATE OK: slot b" in r.stdout, "gbos-update.py apply: N+1 into slot b")
ok(all(rsha(n) == before[n] for n in keep), "the update left userdata, metadata, misc, frp_persist, desktop_security_*"
   " and slot a's boot partitions byte for byte unchanged")
r = subprocess.run([sys.executable, str(ENGINE), "inspect", str(D)], capture_output=True, text=True)
note(r.stdout)
ok("current slot b" in r.stdout and "previous slot on the menu: a" in r.stdout, "inspect: current slot b, previous a")

# 3. the boot chain to the default entry (slot b, quiet)
alive, st, clog, _ = boot("chain-b", [])
menu_log = clog
ok(alive and "Boot in" in clog and "Linux version" not in clog, "chain: systemd-boot starts the default entry (quiet: no serial output)")
ok(writes_data(st), "chain: Android on slot b writes /data")
ok(f2fs_uuid() == U1, "chain: /data is the same file system")

# 4. slot b, serial-debug entry, booted directly
alive, st, log, _ = direct("b-debug", "lapis-debug.conf")
ok(alive and android_up(log), "slot b (N+1) boots, system_server runs")
good, m = slot_mounted(log, "b")
ok(good, f"slot b's partitions are the ones mounted (verity): {m}")
ok("target=/data,type=f2fs)=0" in log and not FMT.search(log), "slot b mounts the existing /data without formatting it")
ok(f2fs_uuid() == U1, "slot b: /data is the same file system (f2fs UUID unchanged)")

# 5. the previous version (slot a), serial-debug entry, booted directly
alive, st, log, _ = direct("a-prev", "prev-lapis-debug.conf")
ok(alive and android_up(log), "previous version (slot a, N) boots, system_server runs")
good, m = slot_mounted(log, "a")
ok(good, f"previous version mounts slot a's partitions: {m}")
ok("target=/data,type=f2fs)=0" in log and not FMT.search(log), "previous version mounts the same /data without formatting it")
ok(f2fs_uuid() == U1, "previous version: f2fs UUID unchanged")

# 6. systemd-boot with the previous-version quiet entry chosen from the menu
titles = []
for e in sorted(n for n in gbu.Fat(f"{D}@@{E}", str(W)).ls("/loader/entries") if n.endswith(".conf")):
    titles.append(gbu.entry_parse(esp("/loader/entries/" + e).decode())["title"])
note("menu titles: " + " | ".join(titles))
menu = re.findall(r"\x1b\[\d+;\d+H(?:\x1b\[[\d;]*m)*\s*(Googlebook[^\x1b\r\n]*?)\s{2,}", menu_log)
seen = []
for m in menu:
    if m not in seen:
        seen.append(m)
note("menu as drawn: " + " | ".join(seen))
want = next((t for t in seen if t.startswith("Googlebook KANO quiet (previous version)")), None)
if want:
    idx = seen.index(want)
    alive, st, clog, sent = boot("chain-prev2", [], keys=["home"] + ["down"] * idx + ["ret"])
    ok(sent and alive and writes_data(st), f"systemd-boot: '{want}' chosen from the menu (entry {idx}), Android writes /data")
    ok(f2fs_uuid() == U1, "after the previous version from the menu: f2fs UUID unchanged")
else:
    ok(False, "the previous-version entry is on the systemd-boot menu")

# 7. the second update, slot b running: N+1 into slot a
keep = ("userdata", "metadata", "misc", "frp_persist", "desktop_security_persist", "desktop_security_storage",
        "boot_b", "init_boot_b", "vendor_boot_b", "vbmeta_b")
before = {n: rsha(n) for n in keep}
r = subprocess.run([sys.executable, str(ENGINE), "apply", str(D), "--image", str(N1), "--mode", "stick"],
                   capture_output=True, text=True)
note(r.stdout + r.stderr)
ok(r.returncode == 0 and "UPDATE OK: slot a" in r.stdout, "second update: N+1 into slot a while slot b runs")
ok(all(rsha(n) == before[n] for n in keep), "the second update left the data partitions and slot b's boot partitions"
   " byte for byte unchanged")
r = subprocess.run([sys.executable, str(ENGINE), "inspect", str(D)], capture_output=True, text=True)
note(r.stdout)
ok("current slot a" in r.stdout and "previous slot on the menu: b" in r.stdout, "inspect: current slot a, previous b")

# 8. the boot chain to the default entry (slot a again) and slot a's serial-debug entry
alive, st, clog, _ = boot("chain-a2", [])
ok(alive and "Boot in" in clog and "Linux version" not in clog and writes_data(st), "chain after the second update: the default entry runs, Android writes /data")
ok(f2fs_uuid() == U1, "chain after the second update: /data is the same file system")
alive, st, log, _ = direct("a2-debug", "lapis-debug.conf")
good, m = slot_mounted(log, "a")
ok(alive and android_up(log) and good, f"slot a (N+1, second update) boots with slot a's partitions: {m}")
ok("target=/data,type=f2fs)=0" in log and not FMT.search(log) and f2fs_uuid() == U1, "slot a mounts the same /data without formatting it")

# 9. the previous version is slot b now
alive, st, log, _ = direct("b2-prev", "prev-lapis-debug.conf")
good, m = slot_mounted(log, "b")
ok(alive and android_up(log) and good, f"previous version (slot b) boots with slot b's partitions: {m}")
ok("target=/data,type=f2fs)=0" in log and not FMT.search(log) and f2fs_uuid() == U1, "previous version (slot b) mounts the same /data")

shutil.rmtree(QDIR, ignore_errors=True)
bad = [w for c, w in RES if not c]
note(f"{len(RES) - len(bad)}/{len(RES)} passed")
note("QEMU UPDATE TEST " + ("FAILED" if bad else "OK"))
sys.exit(1 if bad else 0)
