#!/usr/bin/env python3
"""Stage 2 of the Googlebook-on-KANO build: ESP initrd with pci-stub loaded first, three systemd-boot entries
(quiet, serial-debug, plain) with the KANO safety blacklist, and two QEMU self-tests of the finished USB image:
  1. functional: kernel + initrd + the serial-debug options handed to QEMU directly, Android checked on the serial log;
  2. boot chain: firmware -> the image's own ESP -> systemd-boot -> the default entry, checked by disk writes.
The image is built as <name>.partial and renamed to <name> only when every check passes."""
import hashlib, json, os, re, shutil, socket, struct, subprocess, sys, tempfile, time, uuid
from pathlib import Path
GBOS_OUT = Path(os.environ["GBOS_OUT"])
SRC = GBOS_OUT / "vendor/stage-vendor.img"
NAME = os.environ.get("GBOS_IMAGE_NAME", "googlebook-kano.img")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.img", NAME):
    sys.exit(f"GBOS_IMAGE_NAME must be a plain file name ending in .img, not {NAME!r}")
OUT = GBOS_OUT / "image"; OUT.mkdir(parents=True, exist_ok=True)
FINAL = OUT / NAME
IMG = OUT / (NAME + ".partial")
PCI_STUB = Path(os.environ["GBOS_PCI_STUB_KO"])
QEMU = os.environ.get("GBOS_QEMU", "/usr/bin/qemu-system-x86_64")
OVMF_CODE = os.environ.get("GBOS_OVMF_CODE", "/usr/share/edk2/x64/OVMF_CODE.4m.fd")
OVMF_VARS = os.environ.get("GBOS_OVMF_VARS", "/usr/share/edk2/x64/OVMF_VARS.4m.fd")
QEMU_LIB = os.environ.get("GBOS_QEMU_LIB", "")
QEMU_SECONDS = int(os.environ.get("GBOS_QEMU_SECONDS", "200"))
DEFAULT_ENTRY = os.environ.get("GBOS_DEFAULT_ENTRY", "lapis-quiet.conf")
EDITOR = os.environ.get("GBOS_LOADER_EDITOR", "no")
ADB = bool(os.environ.get("GBOS_ADB_PUBKEY"))
SKIP_SELFTEST = os.environ.get("GBOS_SKIP_SELFTEST") == "1"
if FINAL.exists():
    sys.exit(f"refusing: {FINAL} already exists")
assert DEFAULT_ENTRY in ("lapis.conf", "lapis-debug.conf", "lapis-quiet.conf"), DEFAULT_ENTRY
assert EDITOR in ("yes", "no"), EDITOR

def run(*a):
    print("+", " ".join(map(str, a)), flush=True); subprocess.run(list(map(str, a)), check=True)
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(16 << 20), b""): h.update(b)
    return h.hexdigest()
def gpt(path):
    with open(path, "rb") as f:
        f.seek(512); h = f.read(92)
        if h[:8] != b"EFI PART": sys.exit(f"{path}: no GPT")
        lba, n, esz = struct.unpack_from("<QII", h, 72)
        f.seek(lba * 512); t = f.read(n * esz)
    parts = {}
    for i in range(n):
        e = t[i * esz:(i + 1) * esz]
        if e[:16] == bytes(16): continue
        first, last = struct.unpack_from("<QQ", e, 32)
        parts[e[56:128].decode("utf-16le").rstrip("\0")] = dict(
            type=str(uuid.UUID(bytes_le=e[:16])), guid=str(uuid.UUID(bytes_le=e[16:32])), start=first * 512, end=(last + 1) * 512)
    return parts
PARTS = gpt(SRC)
if PARTS.get("EFI-SYSTEM", {}).get("type") != "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": sys.exit("no EFI-SYSTEM partition in the base image")
ESP = f"@@{PARTS['EFI-SYSTEM']['start']}"
USERDATA = PARTS["userdata"]
def mtype(img, path):
    return subprocess.run(["mtype", "-i", f"{img}{ESP}", path], check=True, capture_output=True, text=True).stdout.replace("\r", "")

# initrd: drop the LPSS UART drivers from first-stage modules.load, keep the bootconfig as built
IMG.unlink(missing_ok=True)
run("cp", "--reflink=auto", SRC, IMG)
run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/initrd.img", OUT / "src-initrd.img")
src = (OUT / "src-initrd.img").read_bytes()
assert src.endswith(b"#BOOTCONFIG\n")
size, _ = struct.unpack("<II", src[-20:-12]); bc = src[-20 - size:]; body = src[:-20 - size]
digest = (GBOS_OUT / "vendor/logs/vbmeta.digest.txt").read_text().split()[0]
assert re.fullmatch(r"[0-9a-f]{64}", digest) and f"androidboot.vbmeta.digest={digest}\n".encode() in bc, \
    "ESP bootconfig must carry the re-signed vbmeta digest"
DROP = {"8250_dw.ko", "intel-lpss-pci.ko", "intel-lpss.ko"}
out = bytearray(); off = 0; changed = 0; added = 0; depchanged = 0
while off < len(body):
    if body[off:off+6] != b"070701":
        nxt = body.find(b"070701", off)
        if nxt < 0: out += body[off:]; break
        out += body[off:nxt]; off = nxt
    h = bytearray(body[off:off+110]); ns = int(h[94:102], 16); fs = int(h[54:62], 16)
    nend = (off + 110 + ns + 3) & ~3; name = body[off+110:off+110+ns-1].decode(); data = body[nend:nend+fs]
    nxt = (nend + fs + 3) & ~3
    if name == "lib/modules/modules.load":
        data = ("\n".join(["pci-stub.ko"] + [l for l in data.decode().splitlines() if l and l not in DROP]) + "\n").encode()
        h[54:62] = b"%08X" % len(data); changed += 1
        out += h + body[off+110:nend] + data + b"\0" * ((-len(data)) % 4)
        # new entry: lib/modules/pci-stub.ko (first stage loads it before ueventd can load intel-lpss-pci)
        ko = PCI_STUB.read_bytes()
        nm = b"lib/modules/pci-stub.ko\0"
        fields = [0x7FFF0001, 0o100644, 0, 0, 1, int(h[46:54], 16), len(ko), 0, 0, 0, 0, len(nm), 0]
        hdr = b"070701" + b"".join(b"%08X" % v for v in fields)
        out += hdr + nm + b"\0" * ((-(110 + len(nm))) % 4) + ko + b"\0" * ((-len(ko)) % 4)
        added += 1
    elif name == "lib/modules/modules.dep":
        data = data + b"/lib/modules/pci-stub.ko:\n"
        h[54:62] = b"%08X" % len(data); depchanged += 1
        out += h + body[off+110:nend] + data + b"\0" * ((-len(data)) % 4)
    else:
        out += body[off:nxt]
    off = nxt
assert changed == 1 and added == 1 and depchanged == 1, (changed, added, depchanged)
(OUT / "initrd.img").write_bytes(bytes(out) + bc)

# entries
orig = mtype(IMG, "::/loader/entries/lapis.conf")
opts = re.search(r"^options (.*)$", orig, re.M).group(1).split()
drop = {"kvm-intel.pkvm=1", "fred=on", "spi-intel.writeable=1", "spi-intel.ignore_protection_status=1",
        "softlockup_panic=1", "nmi_watchdog=panic", "pm.dpm_watchdog_enabled=1", "cpufreq.default_governor=performance",
        "snd_intel_sdw_acpi.sdw_link_mask=0x5",
        # superseded later on the line (xe.enable_psr=0, snd_sof_pci.ipc_type=0); dropped to keep the line short
        "xe.enable_psr=1", "snd_sof_pci.ipc_type=1"}
opts = [o for o in opts if o not in drop and not o.startswith(("module_blacklist=", "initcall_blacklist="))]
BL = ["cros_ec_debugfs", "tpm_tis_i2c_cr50", "cros_ec_lpcs", "cros_ec_usb", "spi_intel_pci", "spi_intel", "i2c_dev",
      "thunderbolt", "tpm", "tpm_tis", "tpm_tis_core", "tpm_tis_spi", "cros_ec", "cros_ec_proto",
      "cros_ec_dev", "cros_ec_chardev", "gsmi", "chromeos_acpi", "nvram", "mtd", "spi_nor", "spidev",
      "intel_quicki2c", "intel_quickspi",
      # with LPSS I2C live: EEPROM writer, HPS, EC I2C tunnel, peripheral charger stay unbound
      "at24", "cros_hps_i2c", "i2c_cros_ec_tunnel", "cros_peripheral_charger",
      # cros_ec_proto (library only) is allowed so cros_kbd_led_backlight binds ACPI GOOG0002; every other module that
      # depends on it stays out, transports first (no EC device is ever registered, so no EC host command can be sent)
      "cros_ec_spi", "cros_ec_i2c", "cros_ec_ishtp", "cros_ec_sysfs", "cros_ec_vbc", "cros_ec_typec", "cros_typec_switch",
      "cros_usbpd_notify", "cros_usbpd_charger", "cros_usbpd_logger", "cros_charge_control", "cros_ec_ucsi", "cros_ec_keyb",
      "cros_ec_sensorhub", "cros_ec_sensors_core", "cros_ec_sensors", "cros_ec_accel_legacy", "cros_ec_baro",
      "cros_ec_lid_angle", "cros_ec_light_prox", "cros_ec_lightbar", "cros_ec_pwrmon", "cros_ec_cec", "leds_cros_ec",
      "hid_google_hammer", "wilco_ec", "wilco_charger", "wilco_ec_debugfs", "wilco_ec_telem", "rtc_wilco_ec"]
BL.remove("cros_ec_proto")
tail = ["initcall_blacklist=nvme_init,mei_me_driver_init", "efi=noruntime", "efi_pstore.pstore_disable=1", "panic=0",
        "softlockup_panic=0", "nmi_watchdog=0", "pm.dpm_watchdog_enabled=0", "cpufreq.default_governor=schedutil",
        # xe on 6.18.21 faults scanning tiled framebuffers through a DPT in stolen memory (CVE-2026-80915, upstream fc648757908304aedbad74f74bf58192aec383db)
        "xe.enable_dpt=0", "xe.enable_psr=0", "xe.enable_fbc=0",
        # KANO is max98373 + nau8825 on SOF; that topology ships for IPC3 only
        "snd_sof_pci.ipc_type=0"]
normal = opts + ["module_blacklist=" + ",".join(BL)] + tail
debug = [o for o in opts if o != "8250.nr_uarts=4"] + \
        ["module_blacklist=" + ",".join(BL + ["8250_dw", "intel_lpss_acpi"])] + tail + ["keep_bootcon", "printk.devkmsg=on",
        # LPSS UART 00:1e.0 (51a8-51aa) and fingerprint GSPI (51ab) go to pci-stub, so intel-lpss-pci binds only the I2C functions
        "pci-stub.ids=8086:51a8,8086:51a9,8086:51aa,8086:51ab"]
def entry(title, o): return f"title {title}\nlinux /kernel.efi\ninitrd /initrd.img\noptions {' '.join(o)}\n"
# quiet: the debug entry's hardware set without any serial console (the 115200-baud console is synchronous)
SERIAL = ("console=", "earlycon=")
quiet = [o for o in debug if not o.startswith(SERIAL) and o not in ("keep_bootcon", "printk.devkmsg=on", "ignore_loglevel", "loglevel=8")] + ["loglevel=4"]
assert "pci-stub.ids=8086:51a8,8086:51a9,8086:51aa,8086:51ab" in quiet and not any(o.startswith(SERIAL) for o in quiet)
# quiet differs from the QEMU-tested serial-debug options only by the serial console/log-level parameters
assert sorted(set(debug) ^ set(quiet)) == sorted(set(o for o in debug if o.startswith(SERIAL)) |
       {"keep_bootcon", "printk.devkmsg=on", "loglevel=4"} | (set(debug) & {"ignore_loglevel", "loglevel=8"})), set(debug) ^ set(quiet)
for name, o in (("normal", normal), ("debug", debug), ("quiet", quiet)):
    n = len(" ".join(o)) + len("initrd=\\initrd.img ")
    print(name, "command line length", n); assert n < 2000, name
    bl = [x for x in o if x.startswith("module_blacklist=")]; assert len(bl) == 1
    bls = bl[0].split("=", 1)[1].split(",")
    for m in ("cros_ec", "cros_ec_lpcs", "cros_ec_dev", "cros_ec_chardev", "cros_ec_debugfs", "cros_ec_sysfs", "cros_ec_vbc", "tpm", "spi_intel_pci", "thunderbolt", "i2c_dev", "gsmi", "nvram"):
        assert m in bls, (name, m)
    assert "cros_ec_proto" not in bls and "cros_kbd_led_backlight" not in bls
    assert "efi=noruntime" in o and not any(x.startswith("spi-intel.") for x in o), name
(OUT / "lapis.conf").write_text(entry("Googlebook KANO plain (no pci-stub, for comparison)", normal))
(OUT / "lapis-quiet.conf").write_text(entry("Googlebook KANO quiet", quiet))
(OUT / "lapis-debug.conf").write_text(entry("Googlebook KANO safe serial-debug", debug))
# editor no (default): the menu cannot edit an entry's options, so the blacklist cannot be removed by a stray key;
# auto-firmware no: no "Reboot Into Firmware" menu item (it sets the OsIndications EFI variable)
(OUT / "loader.conf").write_text(f"default {DEFAULT_ENTRY}\ntimeout 5\neditor {EDITOR}\nauto-firmware no\n")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "initrd.img", "::/initrd.img")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "lapis.conf", "::/loader/entries/lapis.conf")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "lapis-debug.conf", "::/loader/entries/lapis-debug.conf")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "lapis-quiet.conf", "::/loader/entries/lapis-quiet.conf")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "loader.conf", "::/loader/loader.conf")
for f in ("lapis.conf", "lapis-debug.conf", "lapis-quiet.conf"):
    assert mtype(IMG, f"::/loader/entries/{f}") == (OUT / f).read_text(), f
assert mtype(IMG, "::/loader/loader.conf") == (OUT / "loader.conf").read_text()
run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/initrd.img", OUT / "readback-initrd.img")
assert (OUT / "readback-initrd.img").read_bytes() == (OUT / "initrd.img").read_bytes()
run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/kernel.efi", OUT / "kernel.efi")
print("ESP free:", subprocess.run(["mdir", "-i", f"{IMG}{ESP}", "::/"], capture_output=True, text=True).stdout.strip().splitlines()[-1])

# QEMU runs: image on USB (writes go to a throw-away snapshot), blank NVMe attached, until QEMU_SECONDS have passed.
# QEMU still running at the end means the guest neither crashed nor powered off; disk statistics come over QMP.
blank = OUT / "blank-nvme.img"
with open(blank, "wb") as fh: fh.truncate(1 << 30)
def qmp_query(path, *cmds):
    s = socket.socket(socket.AF_UNIX); s.settimeout(30); s.connect(str(path)); f = s.makefile("rwb")
    def call(cmd):
        f.write(json.dumps({"execute": cmd}).encode() + b"\n"); f.flush()
        while True:
            r = json.loads(f.readline())
            if "return" in r or "error" in r: return r
    json.loads(f.readline()); call("qmp_capabilities")
    res = [call(c) for c in cmds]; s.close(); return res
QMP_DIR = Path(tempfile.mkdtemp(prefix="gbos-qmp-"))   # unix socket paths are limited to about 108 bytes
def qemu(tag, extra):
    serial, sock, vars_fd = OUT / f"{tag}-serial.log", QMP_DIR / f"{tag}.qmp", OUT / f"{tag}-vars.fd"
    for p in (serial, sock): p.unlink(missing_ok=True)
    shutil.copy(OVMF_VARS, vars_fd)
    cmd = [QEMU, "-machine", "q35,accel=kvm", "-cpu", "host", "-m", "8192",
           "-smp", "4", "-no-reboot", "-vga", "none", "-display", "none",
           "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
           "-drive", f"if=pflash,format=raw,file={vars_fd}",
           "-device", "qemu-xhci,id=xhci,bus=pcie.0,addr=0x3",
           "-drive", f"file={IMG},if=none,id=usbdisk,format=raw,snapshot=on",
           "-device", "usb-storage,bus=xhci.0,drive=usbdisk,bootindex=1",
           "-drive", f"file={blank},if=none,id=nv,format=raw", "-device", "nvme,drive=nv,serial=BLANKNVME,bus=pcie.0,addr=0x2",
           "-serial", f"file:{serial}", "-monitor", "none", "-qmp", f"unix:{sock},server=on,wait=off"] + extra
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    if QEMU_LIB: env["LD_LIBRARY_PATH"] = QEMU_LIB
    print("+", " ".join(map(str, cmd)), flush=True)
    p = subprocess.Popen(list(map(str, cmd)), env=env)
    end = time.time() + QEMU_SECONDS
    while time.time() < end and p.poll() is None: time.sleep(2)
    alive, stats = p.poll() is None, None
    if alive:
        try:
            r = qmp_query(sock, "query-blockstats")[0]["return"]
            stats = next(d["stats"] for d in r if d.get("device") == "usbdisk" or d.get("qdev", "").endswith("usbdisk"))
        except Exception as e:
            print(tag, "QMP query failed:", e)
        p.terminate()
        try: p.wait(30)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
    print(tag, "QEMU", "ran the full", QEMU_SECONDS, "s" if alive else f"exited early with {p.returncode}", "usbdisk", stats)
    log = serial.read_text(errors="replace") if serial.exists() else ""
    return alive, stats, log

before = sha256(IMG)
checks = {}
if SKIP_SELFTEST:
    print("SKIP QEMU self-tests (GBOS_SKIP_SELFTEST=1): this image is NOT self-tested")
    after = before
    tmp_sum = OUT / (FINAL.name + ".sha256.partial")
    tmp_sum.write_text(f"{after}  {FINAL.name}\n")
    (OUT / (FINAL.name + ".not-self-tested")).write_text("built with GBOS_SKIP_SELFTEST=1: never booted in QEMU before release\n")
    tmp_sum.replace(OUT / (FINAL.name + ".sha256"))
    IMG.replace(FINAL)
    shutil.rmtree(QMP_DIR, ignore_errors=True)
    print(FINAL.name, "sha256", after)
    print("IMAGE OK (NOT SELF-TESTED)", FINAL)
    sys.exit(0)
# 1. functional: the debug entry's options with the console on COM1
q = [o for o in debug if not o.startswith(("console=", "earlycon="))] + ["console=ttyS0,115200n8", "earlycon=uart8250,io,0x3f8,115200n8"]
alive, _stats, log = qemu("functional", ["-kernel", OUT / "kernel.efi", "-initrd", OUT / "initrd.img", "-append", " ".join(q)])
has = lambda p: re.search(p, log) is not None
checks.update({
    "QEMU still running at the end (functional)": alive,
    "init second stage": has(r"init second stage started"),
    "no vbmeta mismatch": not has(r"(?i)vbmeta digest (error|mismatch)"),
    "no nvme controller": not has(r"nvme nvme\d"),
    "system_server running": has(r"u:r:system_server:s0"),
    "keymint HAL present": has(r"IKeyMintDevice/default"),
    "fwupd never started": not has(r"starting service 'fwupd"),
    "no rollforward": not has(r"kernel_max_rollforward"),
    "no gsmi": not has(r"gsmi:"),
    "no kernel panic": not has(r"Kernel panic"),
    "first stage did not load LPSS UART": not has(r"Loading module /lib/modules/(8250_dw|intel-lpss|intel-lpss-pci)\.ko"),
    "split policy compiled (no failure)": has(r"No precompiled sepolicy at /vendor") and not has(r"(?i)(failed to compile|could not load selinux policy|secilc: (?!.*linker: )[^\n]*(error|failed))"),
    "kano-logcat on serial": has(r"kano-logcat: "),
    "data mounted as f2fs": has(r"target=/data,type=f2fs\)=0"),
    "IWeaver not declared": not has(r"Found android.hardware.weaver.IWeaver/default in device VINTF manifest"),
    "kano_bootfix ran": has(r"Service .kano_bootfix. .*exited with status 0"),
    "kano_diag started": has(r"starting service .kano_diag."),
    "iwlmvm modprobe ran": has(r"modprobe --dirname=/vendor/lib/modules iwlmvm"),
    "first stage loads pci-stub": has(r"Loading module /lib/modules/pci-stub.ko"),
    "no EC transport/core/chardev module loaded": not has(r"Loaded kernel module \S*/(cros_ec|cros_ec_lpcs|cros_ec_dev|cros_ec_chardev|cros_ec_sysfs|cros_ec_vbc|cros-ec-typec|cros_typec_switch)\.ko"),
    "kano audiotest rc parsed": has(r"init: Parsing file /vendor/etc/init/kano-audiotest.rc"),
    "kano-services rc parsed": has(r"init: Parsing file /vendor/etc/init/kano-services.rc"),
    "no vendor rc rejections": not has(r"(?i)init: .*(kano-services|kano-audiotest|kano-hw|kano-diag|kano-adb).*(error|invalid|failed to parse|not allowed)"),
})
if ADB:
    checks["kano-adb rc parsed (GBOS_ADB_PUBKEY set)"] = has(r"init: Parsing file /vendor/etc/init/kano-adb.rc")
else:
    checks["no kano-adb (GBOS_ADB_PUBKEY not set)"] = not has(r"kano-adb|kano_adb")

# 2. boot chain: no -kernel, so OVMF boots the image's ESP, systemd-boot reads loader.conf and starts the default
# entry. The quiet entry has no serial console; Android formatting and writing /data shows the chain booted. The other
# entries put earlycon on the KANO's LPSS UART (MMIO 0xfe03e000), which QEMU does not have, so they crawl there.
if DEFAULT_ENTRY == "lapis-quiet.conf":
    alive, stats, clog = qemu("chain", [])
    wrote = (stats or {}).get("wr_bytes", 0); top = (stats or {}).get("wr_highest_offset", 0)
    checks["QEMU still running at the end (chain)"] = alive
    checks["chain: default entry keeps the serial console silent"] = "Linux version" not in clog and "Boot in" in clog
    checks["chain: Android wrote /data (>= 16 MiB, into userdata)"] = wrote >= (16 << 20) and USERDATA["start"] <= top <= USERDATA["end"]
    print("chain: usbdisk wr_bytes", wrote, "wr_highest_offset", top, "userdata", USERDATA["start"], "-", USERDATA["end"])
    template = Path(OVMF_VARS).read_bytes(); after_vars = (OUT / "chain-vars.fd").read_bytes()
    print("chain: OVMF variable store bytes changed in QEMU (firmware boot entries; info only):",
          sum(a != b for a, b in zip(template, after_vars)))
else:
    print(f"SKIP boot chain test: default entry {DEFAULT_ENTRY} sends earlycon to the KANO's LPSS UART, which QEMU lacks")

after = sha256(IMG)
checks["image unchanged by the QEMU runs"] = before == after
for k, v in checks.items(): print("PASS" if v else "FAIL", k)
if not all(checks.values()):
    print(f"image left at {IMG} (not renamed: a check failed)")
    shutil.rmtree(QMP_DIR, ignore_errors=True)
    sys.exit(1)
# checksum first, then the image: a name that looks finished always comes with its checksum
tmp_sum = OUT / (FINAL.name + ".sha256.partial")
tmp_sum.write_text(f"{after}  {FINAL.name}\n")
tmp_sum.replace(OUT / (FINAL.name + ".sha256"))
IMG.replace(FINAL)
shutil.rmtree(QMP_DIR, ignore_errors=True)
print(FINAL.name, "sha256", after)
print("IMAGE OK", FINAL)
