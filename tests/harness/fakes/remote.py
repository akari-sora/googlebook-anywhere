#!/usr/bin/env python3
"""Stub block-device tools of the fake KANO maintenance OS (see fakes/ssh). Usage: remote.py <tool> [args...]

State lives under $FAKE_ROOT:
  dev/<name>              regular files standing in for /dev/<name> (disk images)
  disks/<name>/<COLUMN>   what `lsblk -o COLUMN /dev/<name>` prints (TRAN, SERIAL, SIZE, PKNAME, PARTLABEL, ...)
  sgdisk/<name>/p, i<N>   what `sgdisk -p` / `sgdisk -i N` print
  proc/self/mounts        mount table read by findmnt and the scripts
  faults.json             {"fail": ["tool", ...], "dd_write_exit": N,
                           "fail_mount": ["substring", ...], "unreadable_mount": ["substring", ...],
                           "rm_exit": N, "rm_eio": true, "rm_keep": true, "rm_error_after": true,
                           "fail_umount": ["/mnt/ud"], "fail_umount_nth": {"/mnt/ud": 2},
                           "plant_nonce_on_ro_remount": true}
EIO on os.lstat / os.listdir is not handled here. fakes/pyfault/sitecustomize.py reads the same
faults.json keys eio_lstat, eio_lstat_nth, and eio_listdir for the install script's python3 -c.
Logs: log/calls.log (every call), log/destructive.log (writes to a /dev target, blkdiscard, sgdisk changes).
"""
import hashlib, json, os, re, shutil, sys
from pathlib import Path

R = Path(os.environ["FAKE_ROOT"])
TOOL, ARGS = sys.argv[1], sys.argv[2:]
LOGDIR = R / "log"
LOGDIR.mkdir(exist_ok=True)
FAULTS = json.loads((R / "faults.json").read_text()) if (R / "faults.json").exists() else {}


def unfake(s):
    return s.replace(str(R), "") if isinstance(s, str) else s


def note(path, line):
    with open(LOGDIR / path, "a") as f:
        f.write(line + "\n")


note("calls.log", " ".join([TOOL] + [unfake(a) for a in ARGS]))
if TOOL in FAULTS.get("fail", []):
    print(f"{TOOL}: injected failure", file=sys.stderr)
    sys.exit(1)

PASS = {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"}


def devpath(p):
    """(local path, device name or None) for a path argument."""
    if p.startswith("/dev/") and p not in PASS and not p.startswith("/dev/fd/"):
        return R / "dev" / p[5:], p[5:]
    return Path(p), None


def column(name, col):
    if col == "NAME":
        return name
    f = R / "disks" / name / col
    return f.read_text().rstrip("\n") if f.exists() else ""


def flags_and_cols(args):
    flags, cols, rest, i = set(), [], [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            for ch in a[1:]:
                if ch == "o":
                    i += 1
                    cols = args[i].split(",")
                else:
                    flags.add(ch)
        else:
            rest.append(a)
        i += 1
    return flags, cols, rest


def size_arg(v):
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    return int(v[:-1]) * mult[v[-1]] if v[-1] in mult else int(v)


def mounts():
    f = R / "proc" / "self" / "mounts"
    return [l.split() for l in f.read_text().splitlines() if l.strip()] if f.exists() else []


def lsblk():
    flags, cols, devs = flags_and_cols(ARGS)
    if "n" not in flags:
        print(" ".join(cols))
    for d in devs:
        name = d.rsplit("/", 1)[-1]
        if not (R / "disks" / name).is_dir():
            print(f"lsblk: {d}: not a block device", file=sys.stderr)
            sys.exit(32)
        print(" ".join(column(name, c) for c in cols))


def findmnt():
    flags, cols, rest = flags_and_cols(ARGS)
    rows = mounts()
    if rest:
        rows = [r for r in rows if r[1] == rest[0]]
        if not rows:
            sys.exit(1)
    for r in rows:
        print(r[0])


def dd():
    op = dict(a.split("=", 1) for a in ARGS)
    bs = size_arg(op.get("bs", "512"))
    iflag, oflag = op.get("iflag", "").split(","), op.get("oflag", "").split(",")
    skip = int(op.get("skip", 0)) * (1 if "skip_bytes" in iflag else bs)
    seek = int(op.get("seek", 0)) * (1 if "seek_bytes" in oflag else bs)
    count = None if "count" not in op else int(op["count"]) * (1 if "count_bytes" in iflag else bs)
    if "if" in op:
        src, _ = devpath(op["if"])
        if not src.exists():
            print(f"dd: failed to open '{op['if']}': No such file or directory", file=sys.stderr)
            sys.exit(1)
        with open(src, "rb") as f:
            f.seek(skip)
            data = f.read() if count is None else f.read(count)
    else:
        data = sys.stdin.buffer.read() if count is None else sys.stdin.buffer.read(count)
    if "of" in op:
        dst, dev = devpath(op["of"])
        if dev and not dst.exists():
            print(f"dd: failed to open '{op['of']}': No such file or directory", file=sys.stderr)
            sys.exit(1)
        with open(dst, "r+b" if dst.exists() else "wb") as f:
            f.seek(seek)
            f.write(data)
        if dev:
            note("destructive.log", f"WRITE {dev} offset={seek} len={len(data)}")
            if FAULTS.get("dd_write_exit"):
                print("dd: injected write error", file=sys.stderr)
                sys.exit(FAULTS["dd_write_exit"])
    else:
        sys.stdout.buffer.write(data)


def blkdiscard():
    note("destructive.log", "BLKDISCARD " + " ".join(ARGS))


def sgdisk():
    dev = ARGS[-1].rsplit("/", 1)[-1]
    opts = ARGS[:-1]
    readonly = (opts in (["-p"], ["-v"]) or (len(opts) == 2 and opts[0] == "-i" and opts[1].isdigit())
                or (len(opts) == 2 and opts[0] == "-A" and opts[1].endswith(":show")))
    if not readonly:
        note("destructive.log", "SGDISK " + " ".join(ARGS))
        # The install rewrites p1 to 1:37246976:0. This 24 MiB disk cannot hold that
        # LBA, so the fake answer moves from the initial 24576/6144 to 30720/8192
        # (15 MiB, 4 MiB). 8192 >= the old 6144, so the happy path's new p1 is long
        # enough for the userdata copy. The script's OLD_START and NEW_START then
        # differ, and the kernel node is checked against the new start and length.
        # FAULTS["nvme_p1_new_sectors"] forces a shorter new p1 for the refuse test.
        if dev == "nvme0n1" and any("1:37246976:0" in a for a in opts):
            f = R / "sgdisk" / dev / "i1"
            if f.exists():
                text = f.read_text()
                new_sectors = int(FAULTS.get("nvme_p1_new_sectors", 8192))
                text, n1 = re.subn(r"(?m)^First sector: [0-9]+", "First sector: 30720", text, count=1)
                text, n2 = re.subn(r"(?m)^Partition size: [0-9]+ sectors",
                                   f"Partition size: {new_sectors} sectors", text, count=1)
                if n1 == 1 and n2 == 1:
                    f.write_text(text)
        return
    if "-p" in opts:
        f = R / "sgdisk" / dev / "p"
        print(f.read_text() if f.exists() else "", end="")
    elif "-i" in opts:
        n = opts[opts.index("-i") + 1]
        f = R / "sgdisk" / dev / f"i{n}"
        print(f.read_text() if f.exists() else f"Partition #{n} does not exist.\n", end="")
    elif "-v" in opts:
        print("No problems found. 0 free sectors (0 bytes) available in 0 segments.")
    else:
        print("15:48:set\n15:56:set")


def blockdev():
    if "--getsize64" in ARGS:
        name = ARGS[-1].rsplit("/", 1)[-1]
        f = R / "disks" / name / "SIZE"
        print(f.read_text().strip() if f.exists() else devpath(ARGS[-1])[0].stat().st_size)


def mount():
    blob = " ".join(ARGS)
    for needle in FAULTS.get("fail_mount", []):
        if needle in blob:
            print("mount: injected failure", file=sys.stderr)
            sys.exit(32)
    src, mp = [a for a in ARGS if not a.startswith("-") and a != "f2fs"][-2:]
    with open(R / "proc" / "self" / "mounts", "a") as f:
        f.write(f"{src} {unfake(mp)} auto rw 0 0\n")
    for needle in FAULTS.get("unreadable_mount", []):
        if needle in blob:
            # Mode 000 fails -r/-x for a normal user. Root still passes those checks, so
            # replace the mountpoint with a non-directory and fail -d as well.
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                if os.path.isdir(mp) and not os.path.islink(mp):
                    os.rmdir(mp)
                Path(mp).write_text("not-a-directory")
            else:
                os.chmod(mp, 0)
    if FAULTS.get("plant_nonce_on_ro_remount") and "-o" in ARGS and "ro" in ARGS and "f2fs" in ARGS:
        if unfake(mp).rstrip("/") == "/mnt/ud":
            Path(mp, "kano-autooff.nonce").write_text("still-here\n")


def _bump(name, key):
    path = LOGDIR / name
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = int(data.get(key, 0)) + 1
    path.write_text(json.dumps(data))
    return data[key]


def rm():
    if FAULTS.get("rm_eio"):
        print("rm: Input/output error", file=sys.stderr)
        sys.exit(1)
    if FAULTS.get("rm_keep"):
        return
    if FAULTS.get("rm_exit"):
        print("rm: injected failure", file=sys.stderr)
        sys.exit(int(FAULTS["rm_exit"]))
    recursive = any(a.startswith("-") and ("r" in a[1:] or "R" in a[1:]) for a in ARGS)
    force = any(a.startswith("-") and "f" in a[1:] for a in ARGS)
    paths = [a for a in ARGS if not a.startswith("-")]
    status = 0
    for p in paths:
        path = Path(p)
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink()
        elif path.is_dir():
            if not recursive:
                print(f"rm: cannot remove '{p}': Is a directory", file=sys.stderr)
                status = 1
            else:
                shutil.rmtree(path)
        elif not force:
            print(f"rm: cannot remove '{p}': No such file or directory", file=sys.stderr)
            status = 1
    if FAULTS.get("rm_error_after"):
        print("rm: Input/output error", file=sys.stderr)
        sys.exit(1)
    if status:
        sys.exit(status)


def rmdir_cmd():
    """rmdir(2) the path. A symlink is not followed and is not a directory."""
    paths = [a for a in ARGS if not a.startswith("-")]
    if not paths:
        print("rmdir: missing operand", file=sys.stderr)
        sys.exit(1)
    for p in paths:
        path = Path(p)
        if path.is_symlink() or not path.is_dir():
            print(f"rmdir: failed to remove '{p}': Not a directory", file=sys.stderr)
            sys.exit(1)
        try:
            path.rmdir()
        except OSError as e:
            print(f"rmdir: failed to remove '{p}': {e.strerror}", file=sys.stderr)
            sys.exit(1)


def umount():
    logical = [unfake(a).rstrip("/") for a in ARGS if not a.startswith("-")]
    nth = FAULTS.get("fail_umount_nth") or {}
    if isinstance(nth, dict):
        for target in logical:
            for key, want in nth.items():
                if target == str(key).rstrip("/"):
                    if _bump("umount-counts.json", target) == int(want):
                        print("umount: injected failure", file=sys.stderr)
                        sys.exit(1)
    for needle in FAULTS.get("fail_umount") or []:
        if any(target == str(needle).rstrip("/") for target in logical):
            print("umount: injected failure", file=sys.stderr)
            sys.exit(1)
    targets = set(logical)
    f = R / "proc" / "self" / "mounts"
    f.write_text("".join(" ".join(r) + "\n" for r in mounts() if r[1].rstrip("/") not in targets and r[0] not in targets))


def cmp():
    args, n = list(ARGS), None
    if args[0] == "-n":
        n = int(args[1])
        args = args[2:]
    a, b = (devpath(x)[0] for x in args[:2])
    with open(a, "rb") as fa, open(b, "rb") as fb:
        da, db = (fa.read(n), fb.read(n)) if n is not None else (fa.read(), fb.read())
    if da != db:
        print(f"{args[0]} {args[1]} differ", file=sys.stdout)
        sys.exit(1)


def sha256sum():
    files = [a for a in ARGS if not a.startswith("-")]
    if not files:
        print(f"{hashlib.sha256(sys.stdin.buffer.read()).hexdigest()}  -")
    for p in files:
        print(f"{hashlib.sha256(devpath(p)[0].read_bytes()).hexdigest()}  {unfake(p)}")


def noop():
    pass


{"lsblk": lsblk, "findmnt": findmnt, "dd": dd, "blkdiscard": blkdiscard, "sgdisk": sgdisk, "blockdev": blockdev,
 "mount": mount, "umount": umount, "cmp": cmp, "sha256sum": sha256sum, "rm": rm, "rmdir": rmdir_cmd}.get(TOOL, noop)()
