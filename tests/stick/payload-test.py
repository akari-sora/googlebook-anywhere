#!/usr/bin/env python3
"""Offline checks for install/stick/stick-payload.py. No root, no network, no block device."""
import importlib.util
import os
import random
import re
import shutil
import stat
import struct
import subprocess
import sys
import uuid
from pathlib import Path

W = Path(sys.argv[1])
REPO = Path(sys.argv[2])
W.mkdir(parents=True, exist_ok=False)
sys.path.insert(0, str(REPO / "tests/stick"))
import gptlib  # noqa: E402

SCRIPT = REPO / "install/stick/stick-payload.py"
STICK = REPO / "install/stick"
spec = importlib.util.spec_from_file_location("stick_payload", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = {}


def check(name, ok):
    results[name] = bool(ok)
    print("PASS" if ok else "FAIL", name, flush=True)


def run_apply(root, esp):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "apply",
         "--root", str(root),
         "--pristine", str(PR),
         "--xbootldr", str(XB),
         "--esp-dir", str(esp),
         "--image", str(IMG),
         "--partuuid", PART,
         "--esp-loader-out", str(root / "esp-loader.conf")],
        capture_output=True, text=True,
    )


PART = "12345678-1234-1234-1234-123456789abc"
TOOLS = (
    "python3", "zstd", "sgdisk", "partx", "blockdev", "blkdiscard", "udevadm",
    "lsblk", "findmnt", "mcopy", "mtype", "dd", "sha256sum", "cmp", "mount",
    "umount", "lz4", "mdir", "mmd", "mdel", "minfo",
)
BUILTINS = (
    "kernel/drivers/misc/mei/mei_me.ko\n"
    "kernel/drivers/char/tpm/tpm.ko\n"
    "kernel/drivers/char/tpm/tpm_tis.ko\n"
    "kernel/drivers/char/tpm/tpm_crb.ko\n"
    "kernel/drivers/char/nvram.ko\n"
)
KVER = "7.2.7-arch1-1"
KERNEL = b"vmlinuz-test-bytes\n"
INITRD = b"initramfs-test-bytes\n"

if shutil.which("zstd") is None:
    check("zstd is installed", False)
    sys.exit(1)
check("zstd is installed", True)
check(
    "tool list is the installer's list plus the update tools",
    mod.REQUIRED_TOOLS == TOOLS,
)

template = (STICK / "gbos-install.entry.template").read_text()
mapping = (STICK / "builtin-initcalls.txt").read_text()
try:
    initcalls, covered = mod.parse_map(mapping)
    parsed = True
except mod.PayloadError:
    initcalls, covered = "", []
    parsed = False
check("builtin-initcalls.txt parses", parsed and "tpm_init" in initcalls.split(","))
try:
    mod.parse_map("not a map\n")
    bad_map = False
except mod.PayloadError as err:
    bad_map = "bad line" in str(err)
check("a bad builtin-initcalls line is refused", bad_map)
try:
    mod.parse_map("tpm tpm_init\norphan - not_listed\n")
    bad_dep = False
except mod.PayloadError as err:
    bad_dep = "accepted only with not_listed blocked" in str(err)
check("a dependency initcall missing from the map is refused", bad_dep)

setup = (STICK / "setup-stick.sh").read_text()
bfun = re.search(r"(?m)^blacklist\(\) \{.*\}$", setup)
shell = subprocess.run(
    ["bash", "-c", bfun.group(0) + '\nblacklist "$1"', "bash",
     str(STICK / "gbos-install.entry.template")],
    capture_output=True, text=True, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
)
awk = subprocess.run(
    ["bash", "-c",
     "awk '{sub(/#.*/, \"\")} NF && $2 != \"-\" {print $2}' \"$1\" | paste -sd, -",
     "bash", str(STICK / "builtin-initcalls.txt")],
    capture_output=True, text=True, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
)
names = mod.module_names(template)
check(
    "module_blacklist matches setup-stick.sh",
    shell.returncode == 0 and shell.stdout.split() == names and names == sorted(set(names)),
)
check(
    "initcalls match setup-stick.sh",
    awk.returncode == 0 and awk.stdout.strip() == initcalls and set(covered) <= set(names),
)

# --- synthetic image and pristine data, same shape selftest.py uses
rnd = random.Random(7)
SECT = 96 * 2048
ESP, LIN = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "0fc63daf-8483-4772-8e79-3d69d8477de4"
layout = [
    (1, "EFI-SYSTEM", ESP, 2048, 8191, 0),
    (2, "boot_a", LIN, 8192, 12287, 0),
    (3, "vbmeta_a", LIN, 12288, 12295, 1 << 60),
    (4, "metadata", LIN, 14336, 18431, 0),
    (5, "super", LIN, 18432, 100351, 0),
    (6, "userdata", LIN, 102400, SECT - 34, 1 << 2),
]
IMG = W / "googlebook-kano.img"
with open(IMG, "wb") as handle:
    handle.truncate(SECT * 512)
    for _n, _name, _typ, start, end, _attrs in layout:
        handle.seek(start * 512)
        handle.write(rnd.randbytes(min(end + 1 - start, 2048) * 512))
    gptlib.write_gpt(
        handle, SECT,
        [dict(num=n, name=name, type=typ, first=start, last=end, attrs=attrs)
         for n, name, typ, start, end, attrs in layout],
    )
PR = W / "pristine"
made = subprocess.run(
    [sys.executable, "-I", str(STICK / "make-pristine.py"), str(IMG), str(PR)],
    capture_output=True, text=True,
)
check("make-pristine.py succeeds for the payload fixture", made.returncode == 0)
if made.returncode != 0:
    print(made.stderr)
    sys.exit(1)

XB = W / "xboot"
XB.mkdir()
(XB / "vmlinuz-linux").write_bytes(KERNEL)
(XB / "initramfs-linux.img").write_bytes(INITRD)


def blacklist_token():
    return ",".join(names)


def write_esp(path, debug_extra="console=ttyS0", extra_name=None, body=None):
    entries = path / "loader" / "entries"
    entries.mkdir(parents=True)
    if body is None:
        def one(title, extra):
            opts = (
                f"options root=x rw efi=noruntime module_blacklist={blacklist_token()} "
                f"initcall_blacklist=nvme_init,mei_me_driver_init {extra}"
            ).rstrip()
            return f"title {title}\nlinux /kernel.efi\ninitrd /initrd.img\n{opts}\n"
        (entries / "lapis-quiet.conf").write_text(one("Googlebook KANO quiet", ""))
        (entries / "lapis-debug.conf").write_text(one("Googlebook KANO serial-debug", debug_extra))
    else:
        (entries / "lapis-quiet.conf").write_text(body)
        (entries / "lapis-debug.conf").write_text(body)
    if extra_name:
        (entries / extra_name).write_text("title other\noptions module_blacklist=nvram\n")


def make_root(path, *, kver=KVER, kernel=KERNEL, builtin=BUILTINS, with_vmlinuz=True, drop_tool=None):
    path.mkdir()
    bindir = path / "usr/bin"
    bindir.mkdir(parents=True)
    for name in TOOLS:
        if name == drop_tool:
            continue
        tool = bindir / name
        tool.write_bytes(b"")
        tool.chmod(0o755)
    moddir = path / "usr/lib/modules" / kver
    moddir.mkdir(parents=True)
    (moddir / "modules.builtin").write_text(builtin)
    if with_vmlinuz:
        (moddir / "vmlinuz").write_bytes(kernel)
    (path / "KEEP").write_text("keep")
    return path


ESP_OK = W / "esp-ok"
write_esp(ESP_OK)
ROOT = make_root(W / "root")
proc = run_apply(ROOT, ESP_OK)
check("apply succeeds", proc.returncode == 0 and proc.stdout.splitlines()[0] == "stick-payload ok")
if proc.returncode != 0:
    print(proc.stderr)
else:
    text = proc.stdout
    check("apply reports the kernel, initcalls, super and installer default",
          f"kernel {KVER}" in text and f"initcalls {initcalls}" in text
          and "esp_loader default gbos-install.conf" in text and "super ok" in text
          and "lockdown support is checked by gbos-install at boot" in text)
    install = (XB / "loader/entries/gbos-install.conf").read_text()
    update = (XB / "loader/entries/gbos-update.conf").read_text()
    nvme = (XB / "loader/entries/gbos-nvme.conf").read_text()
    nvme_dbg = (XB / "loader/entries/gbos-nvme-debug.conf").read_text()
    entry_names = sorted(p.name for p in (XB / "loader/entries").iterdir())
    install_opts = [line for line in install.splitlines() if line.startswith("options ")]
    update_opts = [line for line in update.splitlines() if line.startswith("options ")]
    check("installer and update entries are rendered from the template",
          "title Install Googlebook to the internal disk" in install.splitlines()
          and len(install_opts) == 1 and f"root=PARTUUID={PART} " in install_opts[0]
          and install_opts[0].endswith(" gbos.install=1") and "gbos.update=1" not in install
          and "@" not in install_opts[0] and f"initcall_blacklist={initcalls} " in install_opts[0]
          and "title Update Googlebook on the internal disk (keep data)" in update.splitlines()
          and len(update_opts) == 1
          and update_opts[0].endswith(" gbos.install=1 gbos.update=1")
          and "@" not in update_opts[0])
    def firmware_blocks(text):
        opts = [line for line in text.splitlines() if line.startswith("options ")]
        return (
            len(opts) == 1
            and "linux /vmlinuz-linux" in text.splitlines()
            and "lockdown=integrity" in opts[0]
            and "efi=noruntime" in opts[0]
            and "module_blacklist=" in opts[0]
            and "initcall_blacklist=" in opts[0]
        )
    check("the user stick has no plain Maintenance entry",
          entry_names == [
              "gbos-install.conf", "gbos-nvme-debug.conf",
              "gbos-nvme.conf", "gbos-update.conf",
          ]
          and not (XB / "loader/entries/maintenance.conf").exists()
          and "title Maintenance" not in install
          and "title Maintenance" not in update)
    check("install and update entries keep the firmware protections",
          firmware_blocks(install) and firmware_blocks(update))
    check("internal-disk entries come from the image and do not block nvme_init",
          nvme.startswith("title Googlebook (internal disk)\n")
          and "linux /gbos-nvme/kernel.efi\n" in nvme and "initrd /gbos-nvme/initrd.img\n" in nvme
          and "nvme_init" not in nvme and "console=ttyS0" not in nvme
          and nvme_dbg.startswith("title Googlebook (internal disk) serial-debug\n")
          and "console=ttyS0" in nvme_dbg and "nvme_init" not in nvme_dbg)
    link = ROOT / "etc/systemd/system/multi-user.target.wants/gbos-installer.service"
    installed = ROOT / "usr/local/sbin/gbos-install"
    check("installer files and the enable link are in the root",
          installed.read_bytes() == (STICK / "gbos-install.sh").read_bytes()
          and stat.S_IMODE(installed.stat().st_mode) == 0o755
          and (ROOT / "etc/systemd/system/gbos-installer.service").read_bytes()
          == (STICK / "gbos-installer.service").read_bytes()
          and link.is_symlink() and os.readlink(link) == "/etc/systemd/system/gbos-installer.service"
          and (ROOT / "usr/local/share/gbos-install/builtin-initcalls.txt").read_bytes()
          == (STICK / "builtin-initcalls.txt").read_bytes()
          and (ROOT / "usr/local/share/gbos-install/gbos-update.py").read_bytes()
          == (REPO / "install/gbos-update.py").read_bytes())
    listed = (ROOT / "usr/local/share/gbos-install/module-blacklist.txt").read_text().splitlines()
    check("module-blacklist.txt is the template's list, one name per line",
          listed[0].startswith("# module_blacklist of the installer entry")
          and listed[2:] == names)
    copied = sorted(p.name for p in (ROOT / "var/lib/gbos-install/pristine").iterdir())
    source = sorted(p.name for p in PR.iterdir())
    same = all(
        (ROOT / "var/lib/gbos-install/pristine" / name).read_bytes() == (PR / name).read_bytes()
        for name in source
    )
    check("pristine data in the root matches the source", copied == source and same)
    check("ESP loader.conf default is the installer entry",
          (ROOT / "esp-loader.conf").read_text() == (
              "default gbos-install.conf\n"
              "timeout 5\n"
              "editor no\n"
              "auto-firmware no\n"
          ))
    check("apply leaves the maintenance kernel bytes in place",
          (XB / "vmlinuz-linux").read_bytes() == KERNEL
          and (XB / "initramfs-linux.img").read_bytes() == INITRD)
    planted = XB / "loader/entries/maintenance.conf"
    planted.write_text(
        "title Maintenance\n"
        "linux /vmlinuz-linux\n"
        "initrd /initramfs-linux.img\n"
        f"options root=PARTUUID={PART} rw rootwait console=tty0 quiet loglevel=3\n"
    )
    again = run_apply(ROOT, ESP_OK)
    copied_again = sorted(p.name for p in (ROOT / "var/lib/gbos-install/pristine").iterdir())
    same_again = all(
        (ROOT / "var/lib/gbos-install/pristine" / name).read_bytes() == (PR / name).read_bytes()
        for name in source
    )
    check("a second apply replaces the payload in place",
          again.returncode == 0 and installed.read_bytes() == (STICK / "gbos-install.sh").read_bytes()
          and copied_again == source and same_again)
    check("a second apply removes a planted plain Maintenance entry",
          again.returncode == 0 and not planted.exists()
          and sorted(p.name for p in (XB / "loader/entries").iterdir()) == entry_names)
    if again.returncode != 0:
        print(again.stderr)

# header fallback: no modules/vmlinuz copy, version comes from the bzImage header
hdr = bytearray(0x240)
hdr[0x202:0x206] = b"HdrS"
struct.pack_into("<H", hdr, 0x20E, 0x20)
version = b"9.9.9-test \0"
hdr[0x220:0x220 + len(version)] = version
HDR = W / "xboot-hdr"
HDR.mkdir()
(HDR / "vmlinuz-linux").write_bytes(bytes(hdr))
(HDR / "initramfs-linux.img").write_bytes(INITRD)
saved = (XB / "vmlinuz-linux").read_bytes(), (XB / "initramfs-linux.img").read_bytes()
# point the next roots at the header kernel via a dedicated xboot by swapping paths in a direct call
root_hdr = make_root(W / "root-hdr", kver="9.9.9-test", with_vmlinuz=False)
proc = subprocess.run(
    [sys.executable, "-I", str(SCRIPT), "apply",
     "--root", str(root_hdr), "--pristine", str(PR), "--xbootldr", str(HDR),
     "--esp-dir", str(ESP_OK), "--image", str(IMG), "--partuuid", PART],
    capture_output=True, text=True,
)
check("kernel version falls back to the bzImage header",
      proc.returncode == 0 and "kernel 9.9.9-test" in proc.stdout)
if proc.returncode != 0:
    print(proc.stderr)

# refusals leave the root without an installer
def refused(name, root, esp, image, expect):
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "apply",
         "--root", str(root), "--pristine", str(PR), "--xbootldr", str(XB),
         "--esp-dir", str(esp), "--image", str(image), "--partuuid", PART],
        capture_output=True, text=True,
    )
    clean = (root / "KEEP").read_text() == "keep" and not (root / "usr/local").exists()
    check(name, proc.returncode == 2 and expect in proc.stderr and clean and not proc.stdout)
    if proc.returncode != 2 or expect not in proc.stderr:
        print(proc.stderr)


refused(
    "a root missing a maintenance tool is refused before it is changed",
    make_root(W / "root-tool", drop_tool="lz4"), ESP_OK, IMG,
    "the maintenance root needs lz4",
)
bare = make_root(W / "root-builtin")
builtin = bare / "usr/lib/modules" / KVER / "modules.builtin"
builtin.write_text(BUILTINS + "kernel/drivers/spi/spi_intel.ko\n")
refused(
    "a built-in blocked module missing from the map is refused",
    bare, ESP_OK, IMG,
    "spi_intel",
)
bad_esp = W / "esp-bad"
write_esp(bad_esp, extra_name="other.conf")
refused(
    "an unexpected ESP entry is refused",
    make_root(W / "root-esp"), bad_esp, IMG,
    "unexpected boot entries",
)
nolift = W / "esp-nolift"
write_esp(
    nolift,
    body="title t\nlinux /kernel.efi\ninitrd /initrd.img\noptions module_blacklist=nvram\n",
)
refused(
    "an image entry that cannot be lifted is refused",
    make_root(W / "root-nolift"), nolift, IMG,
    "cannot be changed safely",
)
damaged = W / "pristine-damaged"
shutil.copytree(PR, damaged)
blob = bytearray((damaged / "p2.img.zst").read_bytes())
blob[-1] ^= 0x01
(damaged / "p2.img.zst").write_bytes(bytes(blob))
root_d = make_root(W / "root-damaged")
proc = subprocess.run(
    [sys.executable, "-I", str(SCRIPT), "apply",
     "--root", str(root_d), "--pristine", str(damaged), "--xbootldr", str(XB),
     "--esp-dir", str(ESP_OK), "--image", str(IMG), "--partuuid", PART],
    capture_output=True, text=True,
)
check("damaged pristine data is refused and the root stays unchanged",
      proc.returncode == 2 and "pristine data is damaged" in proc.stderr
      and not (root_d / "usr/local").exists() and not (root_d / "var").exists())
flipped = W / "image-flipped.img"
shutil.copy(IMG, flipped)
with open(flipped, "r+b") as handle:
    handle.seek(18432 * 512)
    first = handle.read(1)
    handle.seek(18432 * 512)
    handle.write(bytes([first[0] ^ 0xFF]))
refused(
    "a super that is not the manifest's super is refused",
    make_root(W / "root-super"), ESP_OK, flipped,
    "not the manifest's super",
)
proc = subprocess.run(
    [sys.executable, "-I", str(SCRIPT), "apply",
     "--root", str(ROOT), "--pristine", str(PR), "--xbootldr", str(XB),
     "--esp-dir", str(ESP_OK), "--image", str(IMG), "--partuuid", "nope"],
    capture_output=True, text=True,
)
check("a bad partuuid is refused", proc.returncode == 2 and "partuuid" in proc.stderr)
link_root = W / "root-link"
link_root.symlink_to(ROOT)
proc = subprocess.run(
    [sys.executable, "-I", str(SCRIPT), "apply",
     "--root", str(link_root), "--pristine", str(PR), "--xbootldr", str(XB),
     "--esp-dir", str(ESP_OK), "--image", str(IMG), "--partuuid", PART],
    capture_output=True, text=True,
)
check("a symlink root is refused", proc.returncode == 2 and "symlink" in proc.stderr)
proc = subprocess.run([sys.executable, "-I", str(SCRIPT)], capture_output=True, text=True)
check("no arguments print usage", proc.returncode == 2 and "usage" in proc.stderr)
# the shared xbootldr still holds the kernel the refusal cases used
check("refusals did not replace the maintenance kernel",
      (XB / "vmlinuz-linux").read_bytes() == saved[0]
      and (XB / "initramfs-linux.img").read_bytes() == saved[1])

failed = [name for name, ok in results.items() if not ok]
sys.exit(1 if failed else 0)
