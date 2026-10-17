#!/usr/bin/env python3
"""Prove gbos-install.sh does not write, mount, or discard the NVMe before confirmation.

The product script is not modified. A temp copy retargets absolute /proc, /sys,
/dev, /var, /run, and /usr/local/share paths into a fixture tree, and changes
`[ -b "$SRC" ]` to `[ -f "$SRC" ]` so the run never opens a real block device.
Control order is otherwise the script's own. On a host whose bash is older than
4.4 (macOS /bin/bash is 3.2), the copy runs under the already-local
python:3.13-slim image. This test does not pull an image, install packages, or
use sudo.

The update-mode engine is a fixture, not install/gbos-update.py. A passing run
shows this script's control flow: inspect may run before the prompt, apply may
not. It does not prove the real updater's writer.

The confirmation boundary is the CONFIRMED trace event, logged from the
installer's own "confirmed:" line after the serial matches and the identity
and in-use recheck. A cancel, a wrong serial, or a blank Enter has no
CONFIRMED event: any target write, mount, or discard in the whole trace fails.
Mutant copies insert those operations before the read, between the read and
the comparison, and on the cancel branch, in both modes. Inspect stays
allowed; it is not one of those operations. The repo file is left unchanged.
"""
import hashlib, os, re, shutil, struct, subprocess, sys, tempfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gptlib  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "install/stick/gbos-install.sh"
SERIAL = "DEADBEEF1234"
NVME_BYTES = 30000000000
PHRASES = (
    "before the NVMe driver is loaded",
    "The disk is visible from here",
    "Do not write, mount, or discard",
    "leaves the disk untouched",
)
FORBIDDEN = (
    "invisible",
    "not visible",
    "not be visible",
    "確認前看不到",
    "driver not bound until",
    "完全沒動",
)
DOC_FILES = (
    REPO / "install/stick/gbos-install.sh",
    REPO / "install/stick/README.md",
    REPO / "install/stick/maint-assemble.py",
)
FORBIDDEN_FILES = DOC_FILES + (
    REPO / "README.md",
    REPO / "docs/INSTALL-INTERNAL.md",
)
PREFIXES = (
    "/sys/kernel/security/lockdown",
    "/sys/class/block/",
    "/sys/class/tpm",
    "/sys/dev/block/",
    "/sys/block/",
    "/var/lib/gbos-install",
    "/usr/local/share/gbos-install",
    "/run/gbos-install",
    "/proc/cmdline",
    "/proc/modules",
    "/proc/mounts",
    "/proc/swaps",
    "/var/log",
    "/dev/",
)
MUTATES = {
    "NVME_MOUNT",
    "NVME_UMOUNT",
    "NVME_DISCARD",
    "DD_OF",
    "DD_IF",
    "SGDISK_WRITE",
    "ENGINE_APPLY",
}
INSTALL_READ = 'read -r -p "  Type the last 4 characters of the serial ($SERIAL) to install, or just Enter to cancel: "'
UPDATE_READ = 'read -r -p "  Type the last 4 characters of the serial ($SERIAL) to update, or just Enter to cancel: "'
CANCEL = '[ -n "$A" ] && [ "${A^^}" = "${LAST4^^}" ] || finish 0 poweroff "Cancelled. Nothing was written."'
SUPER_TIMING = (
    "Update mode hashes the stick super after the serial matches, "
    "before the target is opened for write."
)
PREAMBLE = """trap 'case "$BASH_COMMAND" in
  *"ok   boot files of the image"*) printf "%s\\n" CHECKS >> "$GBOS_TRACE" ;;
  *"ok   super on this stick matches the installer data"*) printf "%s\\n" SUPER_OK >> "$GBOS_TRACE" ;;
  *"Type the last 4 characters of the serial"*) printf "%s\\n" PROMPT >> "$GBOS_TRACE" ;;
  *"confirmed: installing"*|*"confirmed: updating"*) printf "%s\\n" CONFIRMED >> "$GBOS_TRACE" ;;
esac' DEBUG
"""


def fail(message):
    print(f"nvme-confirm-gate: {message}", file=sys.stderr)
    sys.exit(1)


def require_words():
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for phrase in PHRASES:
            if phrase not in text:
                fail(f"{path.relative_to(REPO)} is missing {phrase!r}")
    for path in FORBIDDEN_FILES:
        text = path.read_text(encoding="utf-8")
        for phrase in FORBIDDEN:
            if phrase in text:
                fail(f"{path.relative_to(REPO)} still says {phrase!r}")
    script = INSTALLER.read_text(encoding="utf-8")
    if "install nvme /bin/false in modprobe.d" not in script:
        fail("installer lost the modprobe.d autoload block")
    if script.count('[ -b "$SRC" ]') != 1:
        fail("installer no longer has exactly one [ -b \"$SRC\" ] check")
    anchor = script.find("the stick's super partition is not where the installer data puts it")
    load = script.find("modprobe --ignore-install nvme_core && modprobe --ignore-install nvme")
    if anchor < 0 or load < 0 or load < anchor:
        fail("NVMe modprobe is no longer after the v0.2.0 stick checks")
    if INSTALL_READ not in script or UPDATE_READ not in script:
        fail("install-mode or update-mode serial read not found")
    if script.count(CANCEL) != 2:
        fail("the two cancel branches are no longer the same line")
    require_super_timing()


def require_super_timing():
    """Install hashes the stick super before modprobe. Update hashes it later, inside apply."""
    for rel in (
        "install/stick/gbos-install.sh",
        "install/stick/README.md",
        "install/stick/maint-assemble.py",
        "docs/INSTALL-INTERNAL.md",
    ):
        text = (REPO / rel).read_text(encoding="utf-8")
        if SUPER_TIMING not in text:
            fail(f"{rel} does not limit the super hash to the real order")
    script = INSTALLER.read_text(encoding="utf-8")
    if "A damaged stick stops in this step and never asks for the serial number" in script:
        fail("installer step 2 still claims every stick damage stops before the serial prompt")
    if "in update mode, a damaged stick super fails later, at that post-serial hash" not in script:
        fail("installer step 2 does not note that update mode's super hash fails after the serial matches")
    if "Only the installer loads it" in script or "generated initramfs path" not in script:
        fail("installer comment does not scope the pre-load guarantee to the generated initramfs path")
    assemble = (REPO / "install/stick/maint-assemble.py").read_text(encoding="utf-8")
    if (
        "prebuilt initramfs or a kernel with builtin NVMe is not unpacked or rejected" not in assemble
        or "a prebuilt boot/initramfs-linux.img is left\n# as-is without unpacking" not in assemble
        or "refusing a builtin NVMe in assemble would be a\n# separate check and is not implemented" not in assemble
    ):
        fail("maint-assemble docstring or comment does not state the prebuilt/builtin limit")
    readme = (REPO / "install/stick/README.md").read_text(encoding="utf-8")
    if (
        "這一步核對的項目有任何一項不符就停止" not in readme
        or "更新模式的隨身碟 super 雜湊在序號相符後、打開目標寫入前才做，若 super 內容損壞會在那時停止" not in readme
    ):
        fail("install/stick/README.md step 2 does not state the update-mode super exception")


def zstd_env():
    env = os.environ.copy()
    if shutil.which("zstd", path=env.get("PATH")):
        return env
    for cand in ("/opt/homebrew/bin/zstd", "/usr/bin/zstd", "/usr/local/bin/zstd"):
        if os.access(cand, os.X_OK):
            env["PATH"] = os.path.dirname(cand) + os.pathsep + env.get("PATH", "")
            return env
    fail("zstd is not on PATH; cannot build the fixture image")
    raise AssertionError


def bash_ok():
    for cand in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash", "/usr/bin/bash"):
        if not os.access(cand, os.X_OK):
            continue
        got = subprocess.run(
            [cand, "-c", 'printf "%d.%d" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
            capture_output=True, text=True,
        )
        if got.returncode != 0 or "." not in got.stdout:
            continue
        major, minor = (int(x) for x in got.stdout.strip().split("."))
        if (major, minor) >= (4, 4):
            return cand
    return None


def have_slim_image():
    got = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "python:3.13-slim"],
        capture_output=True, text=True,
    )
    return got.returncode == 0


def retarget(text, fix):
    old_exec = 'exec < "/dev/$TTY" > "/dev/$TTY" 2>&1'
    new_exec = 'exec < "$GBOS_ANSWERS" > "$GBOS_SCREEN" 2>&1'
    if text.count(old_exec) != 1:
        fail("installer tty redirect not found exactly once")
    text = text.replace(old_exec, new_exec, 1)
    text = text.replace('[ -b "$SRC" ]', '[ -f "$SRC" ]', 1)
    for prefix in PREFIXES:
        text = text.replace(prefix, fix + prefix)
    if not text.startswith("#!/usr/bin/env bash\n"):
        fail("installer shebang changed")
    return text.replace("#!/usr/bin/env bash\n", "#!/usr/bin/env bash\n" + PREAMBLE, 1)


def insert_before_install_read(text, line):
    if text.count(INSTALL_READ) != 1:
        fail("temp copy lost the install-mode serial read")
    return text.replace(INSTALL_READ, line + "\n" + INSTALL_READ, 1)


def insert_after_unique(text, needle, line):
    found = [i for i, item in enumerate(text.splitlines()) if needle in item]
    if len(found) != 1:
        fail(f"{needle!r} found {len(found)} times")
    lines = text.splitlines(keepends=True)
    lines.insert(found[0] + 1, line + "\n")
    return "".join(lines)


def rewrite_cancel(text, index, line):
    """index 0 is the update branch; index 1 is the install branch."""
    parts = text.split(CANCEL)
    if len(parts) != 3:
        fail(f"cancel line count is {len(parts) - 1}")
    branch = (
        '[ -n "$A" ] && [ "${A^^}" = "${LAST4^^}" ] || { '
        + line
        + '; finish 0 poweroff "Cancelled. Nothing was written."; }'
    )
    if index == 0:
        return parts[0] + branch + parts[1] + CANCEL + parts[2]
    if index == 1:
        return parts[0] + CANCEL + parts[1] + branch + parts[2]
    fail(f"cancel index {index}")
    raise AssertionError


def module_list():
    template = (REPO / "install/stick/gbos-install.entry.template").read_text(encoding="utf-8")
    found = re.search(r"module_blacklist=(\S+)", template)
    if not found:
        fail("entry template has no module_blacklist")
    mods = found.group(1).split(",")
    if "nvme" in mods or "nvme_core" in mods:
        fail("entry template blacklists nvme; the installer could not load it")
    return mods


def bootconfig_initrd():
    guid = b"11111111-2222-3333-4444-555555555555"
    body = b"androidboot.boot_part_uuid=" + guid + b"\n"
    return b"fixture-initrd\n" + body + struct.pack("<II", len(body), sum(body) & 0xFFFFFFFF) + gptlib.MAGIC


STUB = r'''#!/usr/bin/env python3
import os, sys
name = os.path.basename(sys.argv[0])

def log(line):
    with open(os.environ["GBOS_TRACE"], "a", encoding="utf-8") as handle:
        handle.write(line + "\n")

def positional(args):
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in ("-o", "-i", "-n"):
            skip = True
            continue
        if arg.startswith("-"):
            continue
        out.append(arg)
    return out

def nvme(path):
    return path is not None and ("/dev/nvme" in path or os.path.basename(path).startswith("nvme"))

if name == "modprobe":
    log("MODPROBE " + " ".join(sys.argv[1:]))
    target = os.environ["GBOS_NVME"]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, "ab").close()
elif name == "lsblk":
    text = " ".join(sys.argv[1:])
    if "PKNAME" in text:
        print("sda")
    elif "NAME,TRAN,TYPE" in text:
        print(os.environ["GBOS_NVME"] + " nvme disk")
    elif "MODEL,SIZE,SERIAL" in text:
        print("QEMU-NVMe 30G " + "DEADBEEF1234")
    elif "MOUNTPOINTS" in text:
        pass
    elif "MODEL" in text and "SERIAL" not in text and "SIZE" not in text:
        print("QEMU NVMe Ctrl")
    elif "SERIAL" in text and "SIZE" not in text:
        print("DEADBEEF1234")
    elif "SIZE" in text and "MODEL" not in text and "SERIAL" not in text:
        print(os.environ["GBOS_NVME_SIZE"])
    elif "TRAN" in text:
        print("usb")
    else:
        sys.stderr.write("lsblk stub: unhandled: %s\n" % text)
        sys.exit(1)
elif name == "findmnt":
    print(os.environ["GBOS_ROOTSRC"])
elif name == "mount":
    pos = positional(sys.argv[1:])
    if len(pos) != 2:
        sys.stderr.write("mount stub: %r\n" % (sys.argv[1:],))
        sys.exit(1)
    device, target = pos
    if nvme(device) or nvme(target):
        log("NVME_MOUNT " + device + " " + target)
    else:
        log("MOUNT " + device + " " + target)
    if target.endswith("/xb"):
        os.makedirs(os.path.join(target, "loader", "entries"), exist_ok=True)
    elif target.endswith("/esp"):
        loader = os.path.join(target, "loader")
        os.makedirs(loader, exist_ok=True)
        flag = os.path.join(loader, "loader.conf")
        if not os.path.exists(flag):
            open(flag, "ab").close()
elif name == "umount":
    if any(nvme(arg) for arg in sys.argv[1:]):
        log("NVME_UMOUNT " + " ".join(sys.argv[1:]))
    else:
        log("UMOUNT " + " ".join(sys.argv[1:]))
elif name == "blkdiscard":
    if any(nvme(arg) for arg in sys.argv[1:]):
        log("NVME_DISCARD " + " ".join(sys.argv[1:]))
    else:
        log("DISCARD " + " ".join(sys.argv[1:]))
elif name == "sgdisk":
    writes = {"-e", "-d", "-n", "-c", "-z", "-o", "-g", "-G", "-N", "-u", "-t", "-A"}
    if "-p" in sys.argv[1:] and not (set(sys.argv[1:]) & writes):
        sys.stdout.write("1 EFI-SYSTEM\n32 XBOOTLDR\n")
    else:
        log("SGDISK_WRITE " + " ".join(sys.argv[1:]))
elif name == "dd":
    kv = {}
    for arg in sys.argv[1:]:
        if "=" in arg:
            key, val = arg.split("=", 1)
            if key in ("if", "of", "bs", "count", "skip", "status", "iflag", "oflag", "conv"):
                kv[key] = val
    src, dst = kv.get("if"), kv.get("of")
    if nvme(src):
        log("DD_IF " + src)
    if nvme(dst):
        log("DD_OF " + dst)
    if nvme(src) or nvme(dst):
        blob = b"\0" * (1 << 20)
        if dst:
            with open(dst, "wb") as handle:
                handle.write(blob)
        else:
            sys.stdout.buffer.write(blob)
    else:
        real = "/bin/dd" if os.path.exists("/bin/dd") else "/usr/bin/dd"
        os.execv(real, [real, *sys.argv[1:]])
elif name == "mcopy":
    pos = positional(sys.argv[1:])
    files = [item for item in pos if not item.startswith("::")]
    if not files:
        sys.exit("mcopy stub: no destination")
    dest = files[-1]
    if "initrd" in dest:
        payload = open(os.environ["GBOS_INITRD"], "rb").read()
    elif "kernel" in dest:
        payload = b"kernel-efi-fixture\n"
    else:
        payload = b""
    with open(dest, "wb") as handle:
        handle.write(payload)
elif name == "mtype":
    mods = open(os.environ["GBOS_MODFILE"], encoding="utf-8").read().strip()
    sys.stdout.write(
        "title Googlebook\n"
        "linux /kernel.efi\n"
        "initrd /initrd.img\n"
        "options efi=noruntime module_blacklist=%s initcall_blacklist=nvme_init,mei_me_driver_init\n" % mods
    )
elif name == "mdir":
    sys.stdout.write("::/loader/entries/lapis-debug.conf\n::/loader/entries/lapis-quiet.conf\n")
elif name == "systemctl":
    log("SYSTEMCTL " + " ".join(sys.argv[1:]))
elif name in ("udevadm", "partx", "blockdev", "lz4", "mmd", "mdel", "minfo"):
    pass
else:
    sys.stderr.write("stub: unknown tool %s\n" % name)
    sys.exit(1)
'''

ZSTD = r'''#!/usr/bin/env python3
"""zstd -q -dc / -t / -o using libzstd. The slim image has the library and no zstd CLI."""
import ctypes, sys

lib = ctypes.CDLL("libzstd.so.1")

class InBuf(ctypes.Structure):
    _fields_ = [("src", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]

class OutBuf(ctypes.Structure):
    _fields_ = [("dst", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]

lib.ZSTD_createDCtx.restype = ctypes.c_void_p
lib.ZSTD_freeDCtx.argtypes = [ctypes.c_void_p]
lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(OutBuf), ctypes.POINTER(InBuf)]
lib.ZSTD_decompressStream.restype = ctypes.c_size_t
lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
lib.ZSTD_isError.restype = ctypes.c_uint
lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
lib.ZSTD_getErrorName.restype = ctypes.c_char_p
lib.ZSTD_DStreamOutSize.restype = ctypes.c_size_t

def decompress(data):
    src = ctypes.create_string_buffer(data, len(data))
    dctx = lib.ZSTD_createDCtx()
    if not dctx:
        sys.exit("zstd: ZSTD_createDCtx failed")
    inn = InBuf(ctypes.cast(src, ctypes.c_void_p), len(data), 0)
    out_size = lib.ZSTD_DStreamOutSize() or 131072
    dst = ctypes.create_string_buffer(out_size)
    chunks, guard = [], 0
    try:
        while inn.pos < inn.size:
            guard += 1
            if guard > 1000000:
                sys.exit("zstd: decompress did not finish")
            out = OutBuf(ctypes.cast(dst, ctypes.c_void_p), out_size, 0)
            before = inn.pos
            ret = lib.ZSTD_decompressStream(dctx, ctypes.byref(out), ctypes.byref(inn))
            if lib.ZSTD_isError(ret):
                sys.exit("zstd: " + lib.ZSTD_getErrorName(ret).decode())
            if out.pos:
                chunks.append(dst.raw[:out.pos])
            if ret == 0 and inn.pos >= inn.size:
                break
            if inn.pos == before and out.pos == 0:
                sys.exit("zstd: stuck")
    finally:
        lib.ZSTD_freeDCtx(dctx)
    return b"".join(chunks)

def parse(argv):
    mode, out, files, i = "compress", None, [], 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-q", "-f", "-k"):
            i += 1
            continue
        if arg in ("-d", "-c"):
            mode = "decompress"
            i += 1
            continue
        if arg == "-dc":
            mode = "decompress"
            i += 1
            continue
        if arg == "-t":
            mode = "test"
            i += 1
            continue
        if arg == "-o":
            out = argv[i + 1]
            i += 2
            continue
        if arg.startswith("-T") or (arg.startswith("-") and arg[1:].isdigit()):
            i += 1
            continue
        if arg.startswith("-"):
            sys.exit("zstd wrapper: unsupported " + arg)
        files.append(arg)
        i += 1
    return mode, out, files

def main():
    mode, out, files = parse(sys.argv[1:])
    if mode == "compress":
        sys.exit("zstd wrapper: compression is done on the host")
    raw = open(files[0], "rb").read() if files else sys.stdin.buffer.read()
    data = decompress(raw)
    if mode == "test":
        return
    if out:
        with open(out, "wb") as handle:
            handle.write(data)
    else:
        sys.stdout.buffer.write(data)

if __name__ == "__main__":
    main()
'''

DRIVER = r'''#!/bin/bash
set -u
fix="__FIX__"
export GBOS_NVME="$fix/dev/nvme0n1"
export GBOS_NVME_SIZE="__SIZE__"
export GBOS_ROOTSRC="$fix/dev/sda2"
export GBOS_MODFILE="$fix/modlist.txt"
export GBOS_INITRD="$fix/initrd.bin"
export PYTHONUNBUFFERED=1
export PATH="$fix/bin:${PATH:-/usr/bin:/bin}"
run() {
    cp "$3" "$fix/proc/cmdline"
    rm -f "$GBOS_NVME"
    export GBOS_TRACE="$fix/trace-$1"
    export GBOS_SCREEN="$fix/screen-$1"
    export GBOS_ANSWERS="$fix/answers-$1"
    : > "$GBOS_TRACE"
    : > "$GBOS_SCREEN"
    bash "$2"
    echo $? > "$fix/rc-$1"
}
run install-wrong "$fix/installer.sh" "$fix/cmdline.install"
run install-empty "$fix/installer.sh" "$fix/cmdline.install"
run install-ok "$fix/installer.sh" "$fix/cmdline.install"
run update-wrong "$fix/installer.sh" "$fix/cmdline.update"
run update-ok "$fix/installer.sh" "$fix/cmdline.update"
run mutant-mount "$fix/mutant-mount.sh" "$fix/cmdline.install"
run mutant-dd "$fix/mutant-dd.sh" "$fix/cmdline.install"
run mutant-after-read-install "$fix/mutant-after-read-install.sh" "$fix/cmdline.install"
run mutant-after-read-update "$fix/mutant-after-read-update.sh" "$fix/cmdline.update"
run mutant-cancel-install "$fix/mutant-cancel-install.sh" "$fix/cmdline.install"
run mutant-cancel-update "$fix/mutant-cancel-update.sh" "$fix/cmdline.update"
run mutant-before-compare-ok "$fix/mutant-after-read-install.sh" "$fix/cmdline.install"
exit 0
'''

ENGINE = r'''#!/usr/bin/env python3
import os, sys
kind = sys.argv[1] if len(sys.argv) > 1 else ""
token = {"inspect": "ENGINE_INSPECT", "apply": "ENGINE_APPLY"}.get(kind, "ENGINE_OTHER")
with open(os.environ["GBOS_TRACE"], "a", encoding="utf-8") as handle:
    handle.write(token + "\n")
if kind == "inspect":
    print("fixture: this disk can be updated")
    sys.exit(0)
if kind == "apply":
    sys.exit(0)
sys.exit(2)
'''

TOOLS = (
    "modprobe", "mount", "umount", "dd", "blkdiscard", "sgdisk", "lsblk", "findmnt",
    "udevadm", "partx", "blockdev", "mcopy", "mtype", "mdir", "mmd", "mdel", "minfo",
    "lz4", "systemctl",
)


def write_exe(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def build_image(work):
    sectors = 10240
    esp, linux = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "0fc63daf-8483-4772-8e79-3d69d8477de4"
    layout = [
        (1, "EFI-SYSTEM", esp, 2048, 2559, 0),
        (2, "boot_a", linux, 2560, 3071, 0),
        (3, "super", linux, 3072, 5119, 0),
        (4, "userdata", linux, 6144, 10205, 0),
    ]
    image = work / "googlebook-kano.img"
    with open(image, "wb") as handle:
        handle.truncate(sectors * 512)
        for num, _name, _typ, first, last, _attrs in layout:
            handle.seek(first * 512)
            handle.write(bytes([num]) * min((last + 1 - first) * 512, 65536))
        gptlib.write_gpt(handle, sectors, [
            dict(num=n, name=name, type=typ, first=a, last=z, attrs=attrs)
            for n, name, typ, a, z, attrs in layout
        ])
    pristine = work / "var/lib/gbos-install/pristine"
    pristine.parent.mkdir(parents=True)
    made = subprocess.run(
        [sys.executable, "-I", str(REPO / "install/stick/make-pristine.py"), str(image), str(pristine)],
        capture_output=True, text=True, env=zstd_env(),
    )
    if made.returncode != 0:
        fail("make-pristine.py failed\n" + made.stdout + made.stderr)
    manifest = (pristine / "manifest.txt").read_text(encoding="utf-8").splitlines()
    super_line = next(line.split() for line in manifest if line.startswith("part ") and " super " in f" {line} ")
    number, start, length = int(super_line[1]), int(super_line[3]), int(super_line[4])
    dev = work / "dev"
    dev.mkdir()
    shutil.copyfile(image, dev / "sda")
    (dev / f"sda{number}").write_bytes(image.read_bytes()[start:start + length])
    (dev / "null").write_bytes(b"")
    block = work / "sys/class/block" / f"sda{number}"
    block.mkdir(parents=True)
    (block / "start").write_text(f"{start // 512}\n", encoding="utf-8")
    (block / "size").write_text(f"{length // 512}\n", encoding="utf-8")
    for disk in ("sda", "nvme0n1"):
        queue = work / "sys/block" / disk / "queue"
        queue.mkdir(parents=True)
        (queue / "logical_block_size").write_text("512\n", encoding="utf-8")
    nvme = work / "sys/block/nvme0n1"
    (nvme / "dev").write_text("259:0\n", encoding="utf-8")
    (nvme / "holders").mkdir()
    return image


def build_tree(work, fix):
    build_image(work)
    mods = module_list()
    share = work / "usr/local/share/gbos-install"
    share.mkdir(parents=True)
    shutil.copyfile(REPO / "install/stick/builtin-initcalls.txt", share / "builtin-initcalls.txt")
    (share / "module-blacklist.txt").write_text("".join(f"{name}\n" for name in mods), encoding="utf-8")
    write_exe(share / "gbos-update.py", ENGINE)
    (work / "modlist.txt").write_text(",".join(mods), encoding="utf-8")
    calls = gptlib.initcalls(gptlib.read_initcall_map(share / "builtin-initcalls.txt"))
    common = " ".join([
        "efi=noruntime",
        "lockdown=integrity",
        "module_blacklist=" + ",".join(mods),
        "initcall_blacklist=" + calls,
        "gbos.install=1",
        "gbos.install.tty=gate",
    ])
    (work / "cmdline.install").write_text(common + "\n", encoding="utf-8")
    (work / "cmdline.update").write_text(common + " gbos.update=1\n", encoding="utf-8")
    proc = work / "proc"
    proc.mkdir()
    for name in ("modules", "mounts", "swaps"):
        (proc / name).write_text("", encoding="utf-8")
    (work / "sys/kernel/security").mkdir(parents=True)
    (work / "sys/kernel/security/lockdown").write_text("none [integrity] confidentiality\n", encoding="utf-8")
    (work / "sys/class/tpm").mkdir(parents=True)
    (work / "var/log").mkdir(parents=True)
    (work / "run/gbos-install").mkdir(parents=True)
    (work / "initrd.bin").write_bytes(bootconfig_initrd())
    answers = {
        "install-wrong": "9999\n\n",
        "install-empty": "\n\n",
        "install-ok": "1234\n\n",
        "update-wrong": "9999\n\n",
        "update-ok": "1234\n\n",
        "mutant-mount": "9999\n\n",
        "mutant-dd": "9999\n\n",
        "mutant-after-read-install": "9999\n\n",
        "mutant-after-read-update": "9999\n\n",
        "mutant-cancel-install": "\n\n",
        "mutant-cancel-update": "9999\n\n",
        "mutant-before-compare-ok": "1234\n\n",
    }
    for name, text in answers.items():
        (work / f"answers-{name}").write_text(text, encoding="utf-8")
    original = INSTALLER.read_text(encoding="utf-8")
    copied = retarget(original, fix)
    write_exe(work / "installer.sh", copied)
    write_exe(work / "mutant-mount.sh", insert_before_install_read(copied, 'mount "$T" "$MNT/esp"'))
    write_exe(work / "mutant-dd.sh", insert_before_install_read(
        copied, 'dd if=/dev/zero of="$T" bs=512 count=1 status=none'))
    after_read = insert_after_unique(copied, INSTALL_READ, 'mount "$T" "$MNT/esp"')
    # A matching serial must still record CONFIRMED after this mount. Stop there so the
    # case does not repeat the whole install write.
    after_read = after_read.replace(
        'log "confirmed: installing on $T ($MODEL, serial $SERIAL)"\n',
        'log "confirmed: installing on $T ($MODEL, serial $SERIAL)"\nexit 0\n',
        1,
    )
    write_exe(work / "mutant-after-read-install.sh", after_read)
    write_exe(work / "mutant-after-read-update.sh", insert_after_unique(
        copied, UPDATE_READ, 'dd if=/dev/zero of="$T" bs=512 count=1 status=none'))
    write_exe(work / "mutant-cancel-install.sh", rewrite_cancel(copied, 1, 'blkdiscard "$T"'))
    write_exe(work / "mutant-cancel-update.sh", rewrite_cancel(copied, 0, 'mount "$T" "$MNT/esp"'))
    bindir = work / "bin"
    bindir.mkdir()
    write_exe(bindir / "stub.py", STUB)
    for tool in TOOLS:
        os.symlink("stub.py", bindir / tool)
    write_exe(work / "run-cases.sh", DRIVER.replace("__FIX__", fix).replace("__SIZE__", str(NVME_BYTES)))


def run_cases(work, host_bash):
    env = zstd_env()
    env["PYTHONUNBUFFERED"] = "1"
    env["PATH"] = str(work / "bin") + os.pathsep + env.get("PATH", "")
    if host_bash:
        got = subprocess.run([host_bash, str(work / "run-cases.sh")], cwd=work, capture_output=True, text=True, env=env, timeout=900)
        return got
    if not have_slim_image():
        fail("need bash >= 4.4 or a local python:3.13-slim image; not pulling and not installing packages")
    write_exe(work / "bin/zstd", ZSTD)
    uid, gid = os.getuid(), os.getgid()
    got = subprocess.run(
        [
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "--user", f"{uid}:{gid}",
            "-v", f"{work}:/fix",
            "-w", "/fix",
            "-e", "PATH=/fix/bin:/usr/local/bin:/usr/bin:/bin",
            "-e", "PYTHONUNBUFFERED=1",
            "python:3.13-slim", "bash", "/fix/run-cases.sh",
        ],
        capture_output=True, text=True, timeout=900,
    )
    return got


def read_case(work, name):
    trace = (work / f"trace-{name}").read_text(encoding="utf-8", errors="replace")
    screen = (work / f"screen-{name}").read_text(encoding="utf-8", errors="replace")
    rc_text = (work / f"rc-{name}").read_text(encoding="utf-8").strip()
    return trace, screen, int(rc_text)


def kind_of(line):
    return line.split(" ", 1)[0]


def find_line(lines, token):
    for index, line in enumerate(lines):
        if line == token or line.startswith(token + " "):
            return index
    return None


def mod_at(lines, which):
    for index, line in enumerate(lines):
        parts = line.split()
        if parts[:2] == ["MODPROBE", "--ignore-install"] and parts[2:3] == [which]:
            return index
    return None


def before_confirm(lines):
    """Destructive ops before confirmation. No CONFIRMED event means the whole trace."""
    mark = find_line(lines, "CONFIRMED")
    span = lines if mark is None else lines[:mark]
    return [line for line in span if kind_of(line) in MUTATES]


def show(name, trace, screen, rc):
    tail = "\n".join(screen.splitlines()[-40:])
    return f"{name}: rc={rc}\n-- trace --\n{trace}\n-- screen --\n{tail}"


def expect_order(lines, *, super_ok):
    checks = find_line(lines, "CHECKS")
    core = mod_at(lines, "nvme_core")
    nvme = mod_at(lines, "nvme")
    prompt = find_line(lines, "PROMPT")
    if None in (checks, core, nvme, prompt):
        return "missing CHECKS, modprobe, or PROMPT"
    if not (checks < core < nvme < prompt):
        return "CHECKS, modprobe, and PROMPT are out of order"
    if super_ok:
        mark = find_line(lines, "SUPER_OK")
        if mark is None or not (checks < mark < core):
            return "super check is not before the NVMe driver load"
    elif find_line(lines, "SUPER_OK") is not None:
        return "update mode hashed super before the prompt"
    return None


def clean_problem(name, trace, screen, rc, *, super_ok, cancelled, engine):
    lines = [line for line in trace.splitlines() if line.strip()]
    shown = show(name, trace, screen, rc)
    problem = expect_order(lines, super_ok=super_ok)
    if problem:
        return problem + "\n" + shown
    early = before_confirm(lines)
    if early:
        return "NVMe op before confirmation: " + "; ".join(early) + "\n" + shown
    confirmed = find_line(lines, "CONFIRMED")
    if cancelled:
        if confirmed is not None:
            return "cancel reached confirmation\n" + shown
        if rc != 0 or "Cancelled. Nothing was written." not in screen:
            return "cancel did not leave the disk untouched\n" + shown
    else:
        if confirmed is None:
            return "matching serial did not record confirmation\n" + shown
        if engine == "none":
            later = [line for line in lines[confirmed + 1:] if kind_of(line) in MUTATES]
            if not later or "confirmed: installing" not in screen:
                return "matching serial did not reach a post-confirm NVMe op\n" + shown
        elif engine == "apply":
            apply_at = find_line(lines, "ENGINE_APPLY")
            if apply_at is None or apply_at < confirmed or "confirmed: updating" not in screen:
                return "update apply did not follow confirmation\n" + shown
    if engine == "none" and any(line.startswith("ENGINE_") for line in lines):
        return "install mode called the updater\n" + shown
    if engine in ("inspect", "apply"):
        prompt = find_line(lines, "PROMPT")
        inspected = find_line(lines, "ENGINE_INSPECT")
        if inspected is None or prompt is None or inspected > prompt:
            return "update inspect did not run before the prompt\n" + shown
        if engine == "inspect" and any(line.startswith("ENGINE_APPLY") for line in lines):
            return "update apply ran without a matching serial\n" + shown
    return None


def expect_clean(name, trace, screen, rc, *, super_ok, cancelled, engine):
    problem = clean_problem(name, trace, screen, rc, super_ok=super_ok, cancelled=cancelled, engine=engine)
    if problem:
        fail(problem)


def prove_boundary():
    """The reviewer's six traces: a legal prefix, then an NVMe op, then a clean cancel."""
    cancel = "Cancelled. Nothing was written."
    install = (
        "CHECKS\nSUPER_OK\n"
        "MODPROBE --ignore-install nvme_core\n"
        "MODPROBE --ignore-install nvme\n"
        "PROMPT\n"
    )
    update = (
        "CHECKS\n"
        "MODPROBE --ignore-install nvme_core\n"
        "MODPROBE --ignore-install nvme\n"
        "ENGINE_INSPECT\n"
        "PROMPT\n"
    )
    ops = (
        "NVME_MOUNT /dev/nvme0n1 /mnt",
        "DD_OF /dev/nvme0n1",
        "NVME_DISCARD -f /dev/nvme0n1",
    )
    for label, prefix, super_ok, engine in (
        ("install", install, True, "none"),
        ("update", update, False, "inspect"),
    ):
        if clean_problem(label + "-clean", prefix, cancel, 0, super_ok=super_ok, cancelled=True, engine=engine):
            fail("a clean cancel trace was rejected")
        for op in ops:
            problem = clean_problem(
                label + "-after", prefix + op + "\n", cancel, 0,
                super_ok=super_ok, cancelled=True, engine=engine,
            )
            if problem is None or "NVMe op before confirmation" not in problem:
                fail(f"after-prompt {op.split()[0]} on {label} cancel was accepted")
    gap = install + "NVME_MOUNT /dev/nvme0n1 /mnt\nCONFIRMED\nDD_OF /dev/nvme0n1\n"
    problem = clean_problem(
        "install-gap", gap, "confirmed: installing\n", 0,
        super_ok=True, cancelled=False, engine="none",
    )
    if problem is None or "NVMe op before confirmation" not in problem:
        fail("an NVMe mount between the prompt and confirmation was accepted")
    good = install + "CONFIRMED\nDD_OF /dev/nvme0n1\n"
    if clean_problem(
        "install-good", good, "confirmed: installing\n", 0,
        super_ok=True, cancelled=False, engine="none",
    ):
        fail("a post-confirmation write was rejected")


def expect_mutant(name, trace, screen, rc, token):
    lines = [line for line in trace.splitlines() if line.strip()]
    early = before_confirm(lines)
    if not any(line.startswith(token) for line in early):
        fail(f"mutant {token} before confirmation was not detected\n" + show(name, trace, screen, rc))


CASES = (
    "install-wrong", "install-empty", "install-ok", "update-wrong", "update-ok",
    "mutant-mount", "mutant-dd", "mutant-after-read-install", "mutant-after-read-update",
    "mutant-cancel-install", "mutant-cancel-update", "mutant-before-compare-ok",
)


def main():
    require_words()
    prove_boundary()
    before = hashlib.sha256(INSTALLER.read_bytes()).hexdigest()
    host_bash = bash_ok()
    fix = "" if host_bash else "/fix"
    with tempfile.TemporaryDirectory(prefix=".nvme-gate-", dir=REPO) as tmp:
        work = Path(tmp)
        if host_bash:
            fix = str(work)
        build_tree(work, fix)
        ran = run_cases(work, host_bash)
        if ran.returncode != 0:
            fail(f"case runner exited {ran.returncode}\n{ran.stdout}\n{ran.stderr}")
        missing = [name for name in CASES if not (work / f"rc-{name}").is_file()]
        if missing:
            fail("runner produced no result for " + ", ".join(missing) + "\n" + ran.stdout + ran.stderr)
        wrong = read_case(work, "install-wrong")
        empty = read_case(work, "install-empty")
        good = read_case(work, "install-ok")
        update = read_case(work, "update-wrong")
        updated = read_case(work, "update-ok")
        mounted = read_case(work, "mutant-mount")
        written = read_case(work, "mutant-dd")
        after_install = read_case(work, "mutant-after-read-install")
        after_update = read_case(work, "mutant-after-read-update")
        cancel_install = read_case(work, "mutant-cancel-install")
        cancel_update = read_case(work, "mutant-cancel-update")
        before_ok = read_case(work, "mutant-before-compare-ok")
        expect_clean("install-wrong", *wrong, super_ok=True, cancelled=True, engine="none")
        expect_clean("install-empty", *empty, super_ok=True, cancelled=True, engine="none")
        expect_clean("install-ok", *good, super_ok=True, cancelled=False, engine="none")
        expect_clean("update-wrong", *update, super_ok=False, cancelled=True, engine="inspect")
        expect_clean("update-ok", *updated, super_ok=False, cancelled=False, engine="apply")
        expect_mutant("mutant-mount", *mounted, "NVME_MOUNT")
        expect_mutant("mutant-dd", *written, "DD_OF")
        expect_mutant("mutant-after-read-install", *after_install, "NVME_MOUNT")
        expect_mutant("mutant-after-read-update", *after_update, "DD_OF")
        expect_mutant("mutant-cancel-install", *cancel_install, "NVME_DISCARD")
        expect_mutant("mutant-cancel-update", *cancel_update, "NVME_MOUNT")
        expect_mutant("mutant-before-compare-ok", *before_ok, "NVME_MOUNT")
    after = hashlib.sha256(INSTALLER.read_bytes()).hexdigest()
    if before != after:
        fail("the gate rewrote install/stick/gbos-install.sh")
    print("nvme-confirm-gate: clean")


if __name__ == "__main__":
    main()
