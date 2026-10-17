"""Shared fixtures for the harness tests.

FakeKano builds a fake KANO maintenance OS in a temp dir: disk images with real GPTs standing in for /dev/sda (the
Kingston stick) and /dev/nvme0n1, lsblk/sgdisk answers, sysfs partition tables, /proc mounts and cmdline. The
scripts under test reach it only through fakes/ssh (put first on PATH), fakes/adb and the fake pyserial module.
HOME is a temp dir too, and every setting of kano-env.sh is passed explicitly in the environment with an obviously
fake value (TEST-NET addresses, TEST... serials), so the tests behave the same with or without a kano-env.local.sh
and nothing can reach a real device even if a fake were bypassed. No real device value appears in tests/harness.

KANO_HARNESS_DIR selects the harness under test (default: tools/harness of this repo); mutate.py points it at a
mutated copy.
"""
import atexit, json, os, shutil, struct, subprocess, sys, tempfile, unittest, uuid, zlib
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent.parent
HARNESS = Path(os.environ.get("KANO_HARNESS_DIR") or REPO / "tools" / "harness").resolve()
FAKES = TESTS / "fakes"
MiB = 1 << 20
STICK_SERIAL, NVME_SERIAL = "TESTSTICK0001", "TESTNVME0001"
BOOT_A_GUID = "00000000-0000-4000-8000-000000000001"
NVME_PCI = ("0x1234", "0x5678")

# (number, name, first MiB, end MiB)
IMAGE_PARTS = [(1, "userdata", 12, 15), (2, "super", 4, 10), (3, "vbmeta_a", 10, 11), (4, "vbmeta_b", 11, 12),
               (5, "EFI-SYSTEM", 1, 4)]
STICK_PARTS = IMAGE_PARTS + [(32, "XBOOTLDR", 16, 18), (33, "kano-maint", 18, 23)]
NVME_PARTS = [(1, "userdata", 20, 23)] + IMAGE_PARTS[1:]
IMAGE_SIZE, STICK_SIZE, NVME_SIZE = 16 * MiB, 24 * MiB, 24 * MiB
CHANGED_MIB = [2, 5, 6, 10]          # EFI-SYSTEM, super x2, vbmeta_a

REMOTE_TOOLS = ["lsblk", "findmnt", "dd", "blkdiscard", "sgdisk", "blockdev", "mount", "umount", "cmp", "sha256sum",
                "modprobe", "partx", "sync", "sleep", "resize.f2fs", "fsck.f2fs", "rm", "rmdir"]
SAFE_TOOLS = ["bash", "sh", "cat", "grep", "sed", "awk", "tr", "cut", "head", "tail", "ls", "basename", "dirname",
              "mkdir", "cp", "mv", "base64", "wc", "sort", "uniq", "printf", "env", "true", "false", "date",
              "seq", "expr", "tee", "touch", "od", "id", "uname", "test"]


def lbas(parts):
    return [(n, name, a * 2048, b * 2048 - 1) for n, name, a, b in parts]


def gpt_bytes(disk_size, parts):
    """Protective MBR + primary GPT (34 sectors) and backup GPT (33 sectors) for parts in MiB."""
    return gpt_bytes_lba(disk_size, lbas(parts))


def gpt_bytes_lba(disk_size, parts):
    """Same for parts given as (number, name, first LBA, last LBA)."""
    n, esz = 128, 128
    ents = bytearray(n * esz)
    for num, name, first, last in parts:
        e = bytearray(esz)
        e[0:16] = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4").bytes_le
        e[16:32] = uuid.uuid5(uuid.NAMESPACE_DNS, f"kano-test-{name}-{num}").bytes_le
        struct.pack_into("<QQQ", e, 32, first, last, 0)
        nm = name.encode("utf-16le")[:72]
        e[56:56 + len(nm)] = nm
        ents[(num - 1) * esz:num * esz] = e
    ecrc = zlib.crc32(bytes(ents))
    last = disk_size // 512 - 1

    def header(cur, alt, ent_lba):
        h = bytearray(92)
        h[0:8] = b"EFI PART"
        struct.pack_into("<II", h, 8, 0x00010000, 92)
        struct.pack_into("<QQQQ", h, 24, cur, alt, 34, last - 33)
        h[56:72] = uuid.uuid5(uuid.NAMESPACE_DNS, f"kano-test-disk-{disk_size}").bytes_le
        struct.pack_into("<QIII", h, 72, ent_lba, n, esz, ecrc)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)))
        return bytes(h) + bytes(512 - 92)
    mbr = bytearray(512)
    mbr[446:462] = bytes([0, 0, 2, 0, 0xEE, 0xFF, 0xFF, 0xFF]) + struct.pack("<II", 1, min(last, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr) + header(1, last, 2) + bytes(ents), bytes(ents) + header(last, 1, last - 32)


def make_disk(path, size, parts):
    primary, backup = gpt_bytes(size, parts)
    with open(path, "wb") as f:
        f.truncate(size)
        f.write(primary)
        f.seek(size - len(backup))
        f.write(backup)


def fill(path, mibs, tag):
    """Give each listed MiB of `path` distinct content."""
    with open(path, "r+b") as f:
        for m in mibs:
            f.seek(m * MiB)
            f.write(bytes([(m * 7 + tag) % 251 + 1]) * MiB)


def make_images(d, changed=CHANGED_MIB):
    """old.img / new.img with the same GPT; new differs in the `changed` MiB."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    old, new = d / "old.img", d / "new.img"
    data_mibs = [m for _, _, a, b in IMAGE_PARTS for m in range(a, b)]
    for p in (old, new):
        make_disk(p, IMAGE_SIZE, IMAGE_PARTS)
        fill(p, data_mibs, 1)
    fill(new, changed, 2)
    return old, new


def initrd(path, archive, guid="00000000-0000-0000-0000-000000000000", digest="aa"):
    bc = f"androidboot.boot_part_uuid={guid}\nandroidboot.vbmeta.digest={digest}\n".encode()
    Path(path).write_bytes(archive + bc + struct.pack("<II", len(bc), sum(bc) & 0xFFFFFFFF) + b"#BOOTCONFIG\n")


_SHARED = []


def shared_bin():
    """Stub and fake executables, created once per test process (macOS scans every new executable on first run)."""
    if not _SHARED:
        d = Path(tempfile.mkdtemp(prefix="kano-harness-bin-"))
        atexit.register(shutil.rmtree, d, True)
        for sub in ("remote", "safe", "local"):
            (d / sub).mkdir()
        for tool in REMOTE_TOOLS:
            p = d / "remote" / tool
            p.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{FAKES / 'remote.py'}' {tool} \"$@\"\n")
            p.chmod(0o755)
        for tool in SAFE_TOOLS:
            real = shutil.which(tool)
            if real:
                (d / "safe" / tool).symlink_to(real)
        (d / "safe" / "python3").symlink_to(sys.executable)
        for tool in ("ssh", "adb"):
            shutil.copy(FAKES / tool, d / "local" / tool)
            (d / "local" / tool).chmod(0o755)
        if not shutil.which("timeout"):
            (d / "local" / "timeout").write_text('#!/bin/sh\nshift\nexec "$@"\n')
            (d / "local" / "timeout").chmod(0o755)
        _SHARED.append(d)
    return _SHARED[0]


class FakeKano:
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.root = self.tmp / "fake"
        self.home = self.tmp / "home"
        for d in ("log", "dev", "disks", "proc/self", "sys", "mnt", "root", "etc", "sgdisk", "pyfault"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        shutil.copy(FAKES / "pyfault" / "sitecustomize.py", self.root / "pyfault" / "sitecustomize.py")
        self.home.mkdir()
        self.runs, self.work = self.home / "runs", self.home / "work"
        shared = shared_bin()
        (self.root / "bin").symlink_to(shared)
        self.bin = shared / "local"
        self.set("proc/self/mounts", "")
        self.set("proc/swaps", "Filename\tType\tSize\tUsed\tPriority\n")

    def set(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def disk(self, name, size, parts, tran, serial, kernel=True):
        make_disk(self.root / "dev" / name, size, parts)
        self.set(f"disks/{name}/TRAN", tran)
        self.set(f"disks/{name}/SERIAL", serial)
        self.set(f"disks/{name}/SIZE", str(size))
        (self.root / "sys" / "block" / name / "holders").mkdir(parents=True, exist_ok=True)
        for num, pname, first, last in lbas(parts):
            pn = f"{name}p{num}" if name[-1].isdigit() else f"{name}{num}"
            self.set(f"disks/{pn}/PKNAME", name)
            self.set(f"disks/{pn}/PARTLABEL", pname)
            self.set(f"disks/{pn}/SIZE", str((last - first + 1) * 512))
            if kernel:
                self.kpart(name, pn, num, first, last - first + 1)

    def kpart(self, disk, pn, num, start, size):
        d = self.root / "sys" / "block" / disk / pn
        (d / "holders").mkdir(parents=True, exist_ok=True)
        (d / "partition").write_text(f"{num}\n")
        (d / "start").write_text(f"{start}\n")
        (d / "size").write_text(f"{size}\n")

    def mount(self, src, mp):
        with open(self.root / "proc" / "self" / "mounts", "a") as f:
            f.write(f"{src} {mp} ext4 rw 0 0\n")

    def rules(self, rules):
        self.set("rules.json", json.dumps(rules))

    def faults(self, faults):
        self.set("faults.json", json.dumps(faults))

    def kano(self):
        """The KANO in the 'maintenance NVMe install' entry: / on sda33 of the stick, the NVMe idle."""
        self.set("proc/cmdline", "BOOT_IMAGE=/vmlinuz root=PARTUUID=x initcall_blacklist=mei_me_driver_init "
                                 "module_blacklist=cros_ec_lpcs console=ttyS0\n")
        self.disk("sda", STICK_SIZE, STICK_PARTS, "usb", STICK_SERIAL)
        self.set("sys/block/sda/sda32/start", "37244880\n")         # the real XBOOTLDR start the install checks
        self.disk("nvme0n1", NVME_SIZE, NVME_PARTS, "nvme", NVME_SERIAL + "      ")   # lsblk pads NVMe serials
        self.set("sys/class/nvme/nvme0/device/vendor", NVME_PCI[0] + "\n")
        self.set("sys/class/nvme/nvme0/device/device", NVME_PCI[1] + "\n")
        self.mount("/dev/sda33", "/")
        ud = "Partition GUID code: 0FC63DAF-8483-4772-8E79-3D69D8477DE4 (Linux filesystem)\n"
        self.set("sgdisk/sda/i1", ud + "First sector: 24576 (at 12.0 MiB)\nPartition size: 6144 sectors (3.0 MiB)\n"
                                       "Partition name: 'userdata'\n")
        self.set("sgdisk/nvme0n1/i1", ud + "First sector: 24576 (at 12.0 MiB)\nPartition size: 6144 sectors (3.0 MiB)\n"
                                           "Partition name: 'userdata'\n")
        self.set("sgdisk/nvme0n1/i13", f"Partition GUID code: FE3A2A5D-4F32-41A7-B725-ACCC3285A309 (ChromeOS kernel)\n"
                                       f"Partition unique GUID: {BOOT_A_GUID.upper()}\nPartition name: 'boot_a'\n")

    def bare_env(self, **over):
        """No KANO_* setting at all: only the sandbox (HOME, fakes first on PATH, fake pyserial)."""
        e = {k: v for k, v in os.environ.items() if not k.startswith(("KANO_", "FAKE_", "HARNESS_"))}
        e.update(HOME=str(self.home), PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_ROOT=str(self.root),
                 PYTHONPATH=str(FAKES / "pyserial"), PYTHONDONTWRITEBYTECODE="1",
                 FAKE_SERIAL_LOG=str(self.root / "log" / "serial.log"))
        if os.environ.get("HARNESS_BEHAVIOUR_ONLY"):
            e["HARNESS_BEHAVIOUR_ONLY"] = os.environ["HARNESS_BEHAVIOUR_ONLY"]
        e.update(over)
        return e

    def fixtures(self):
        """A fake value for every setting of kano-env.sh (test_env checks that none is missing)."""
        return dict(KANO_IP="192.0.2.10", KANO_SSH_KEY=str(self.tmp / "id_test"), KANO_ADB_ADDR="192.0.2.11:5555",
                    KANO_ADB=str(self.bin / "adb"), KANO_ANDROID_ID_PROP="init.svc.kano_bootfix",
                    KANO_RUNS=str(self.runs), KANO_AP_UART_LOG=str(self.runs / "ap-uart.log"), KANO_WORK=str(self.work),
                    KANO_NVME_SRC=str(self.work / "kano-final25"),
                    KANO_PYSERIAL_ROOT=str(self.tmp / "no-pyserial"), KANO_CR50_SERIAL="TESTCR50-0001",
                    KANO_AP_TTY=str(self.tmp / "no-ap-tty"), KANO_EC_TTY=str(self.tmp / "no-ec-tty"),
                    KANO_STICK_SERIAL=STICK_SERIAL, KANO_NVME_SERIAL=NVME_SERIAL, KANO_NVME_PCI=":".join(NVME_PCI),
                    KANO_NVME_BOOT_A_GUID=BOOT_A_GUID)

    def env(self, **over):
        e = self.bare_env(**self.fixtures())
        e.update(over)
        return e

    def run(self, argv, timeout=120, **over):
        return subprocess.run([str(a) for a in argv], env=self.env(**over), capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)

    # what happened
    def ssh_calls(self):
        f = self.root / "log" / "ssh.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def sent(self):
        return "\n".join(c["cmd"] + "\n" + (c["script"] or "") for c in self.ssh_calls())

    def lines(self, name):
        f = self.root / "log" / name
        return f.read_text().splitlines() if f.exists() else []

    def destructive(self):
        return self.lines("destructive.log")

    def writes(self):
        return [l for l in self.destructive() if l.startswith(("WRITE", "BLKDISCARD", "SGDISK"))]


def env_names(harness=HARNESS):
    import re
    line = re.compile(r'\s*: "\$\{(\w+):=(.*)\}"\s*(#.*)?')
    return [m.group(1) for m in map(line.fullmatch, (harness / "kano-env.sh").read_text().splitlines()) if m]


def harness_copy(dst, local=None):
    """Copy of the harness under test without kano-env.local.sh; `local` (text) becomes its local file. Without
    builder/ and profiles/ two levels up (as in the repo), the watchers refuse to start (they read their guards from the
    device profile through builder/lib/devprofile.py)."""
    shutil.copytree(HARNESS, dst, ignore=shutil.ignore_patterns("kano-env.local.sh", "__pycache__"))
    if local is not None:
        (Path(dst) / "kano-env.local.sh").write_text(local)
    return Path(dst)


class HarnessCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="kano-harness-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.fk = FakeKano(self._tmp)

    def reason(self, needle, haystack, msg=None):
        """Assert a refusal/diagnostic message. HARNESS_BEHAVIOUR_ONLY=1 (mutate.py --classify) skips these, so only
        the effect assertions remain: what was sent, written, discarded, powered off, and exit statuses."""
        if not os.environ.get("HARNESS_BEHAVIOUR_ONLY"):
            self.assertIn(needle, haystack, msg)

    def explain(self, r):
        return f"\n--- exit {r.returncode}\n--- stdout\n{r.stdout[-3000:]}\n--- stderr\n{r.stderr[-3000:]}"
