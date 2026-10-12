#!/usr/bin/env python3
"""Push only the changed 1 MiB blocks of a Googlebook image to the stick in the running KANO maintenance OS.

usage: kano-delta-push.py <old.img> <new.img> <kano-ip> [--dry-run]

The target is the disk that holds the running maintenance OS root (the Kingston stick). Its GPT has extra partitions
(XBOOTLDR, kano-maint) that the image lacks, so the first MiB (primary GPT) is never written and every changed block
must lie inside a partition of the image. --dry-run only compares the two images (no ssh).

Before anything is written, one remote call collects the target's identity and layout; the push refuses unless
- the maintenance root is on a /dev/sdX disk with TRAN=usb, the stick's serial (KANO_STICK_SERIAL, which must be set
  and alphanumeric: an empty one would match a disk without a serial) and at least the image's size;
- the stick's on-disk GPT has every partition of the image at the same number with the same name, start and end, and
  the kernel's partition table agrees with it;
- no written range overlaps a stick partition that is not part of the image (XBOOTLDR, kano-maint, ...) or a partition
  that is mounted, used as swap or held by another device (dm, md, ...).
Every write re-checks, in the same remote command as its dd (write_guard): TRAN, serial and that the maintenance root is
still on that disk; the primary GPT (SHA-256 of the first MiB, as read for the checks); the kernel's start and size of
every partition the write overlaps; and that neither the disk nor those partitions are mounted, used as swap or held.
A query that fails refuses like a difference does. Each pushed run is read back and compared per MiB by SHA-256.

kano-delta-push-nvme.py imports the helpers below (GPT parsing, remote facts, overlap checks, the push loop).
"""
import base64, hashlib, os, re, struct, subprocess, sys, time, zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
BS = 1 << 20
IDCHECK_FAILED = 97   # exit status of the remote identity check that precedes every write


def load_env():
    """Fill os.environ from kano-env.local.sh (optional: the developer's own values) and then kano-env.sh, lines
    `: "${NAME:=value}"`: the first non-empty value wins, and a value already in the environment wins over both."""
    for name in ("kano-env.local.sh", "kano-env.sh"):
        if not (HERE / name).exists():
            continue
        for line in (HERE / name).read_text().splitlines():
            m = re.fullmatch(r'\s*: "\$\{(\w+):=(.*)\}"\s*(#.*)?', line)
            if m and not os.environ.get(m.group(1)):
                os.environ[m.group(1)] = os.path.expandvars(m.group(2))


def require(*names, serial=False):
    """Refuse (exit 2) unless every name is set (and, with serial=True, alphanumeric), before any action."""
    for n in names:
        v = os.environ.get(n, "")
        if not v or (serial and not re.fullmatch(r"[A-Za-z0-9]+", v)):
            print(f"refusing: {n} is {'not set' if not v else 'not an alphanumeric serial'}; put it in "
                  f"kano-env.local.sh or the environment (see README.md)", file=sys.stderr)
            sys.exit(2)


def refuse(msg):
    sys.exit(f"refusing: {msg}")


def gpt_parts(buf):
    """Used entries of the GPT at the start of `buf` (512-byte sectors) as (number, name, start_byte, end_byte).

    Raises ValueError unless the primary header and the entry array are intact (signature, both CRC32s)."""
    if len(buf) < 1024 or buf[512:520] != b"EFI PART":
        raise ValueError("no GPT header at LBA 1 (512-byte sectors)")
    hsize = struct.unpack_from("<I", buf, 512 + 12)[0]
    if not 92 <= hsize <= 512:
        raise ValueError(f"implausible GPT header size {hsize}")
    hdr = bytearray(buf[512:512 + hsize])
    want = struct.unpack_from("<I", hdr, 16)[0]
    hdr[16:20] = bytes(4)
    if zlib.crc32(bytes(hdr)) != want:
        raise ValueError("GPT header CRC mismatch")
    lba, n, size, ecrc = struct.unpack_from("<QIII", buf, 512 + 72)
    if size < 128 or n > 1024:
        raise ValueError(f"implausible GPT entry array ({n} x {size} bytes)")
    ents = buf[lba * 512:lba * 512 + n * size]
    if len(ents) != n * size:
        raise ValueError("GPT entry array lies beyond the data read")
    if zlib.crc32(ents) != ecrc:
        raise ValueError("GPT entry array CRC mismatch")
    out = []
    for i in range(n):
        e = ents[i * size:(i + 1) * size]
        if e[:16] != bytes(16):
            first, last = struct.unpack_from("<QQ", e, 32)
            out.append((i + 1, e[56:128].decode("utf-16le", "replace").split("\0")[0], first * 512, (last + 1) * 512))
    return out


def image_parts(path):
    with open(path, "rb") as f:
        buf = f.read(BS)
    try:
        return gpt_parts(buf)
    except ValueError as e:
        refuse(f"{path}: {e}")


def changed_blocks(old, new, size):
    out = []
    with open(old, "rb") as fo, open(new, "rb") as fn:
        for off in range(0, size, BS):
            if fo.read(BS) != fn.read(BS):
                out.append(off)
    return out


def make_runs(offsets, longest=32):
    """Contiguous changed blocks go out as one dd per run (one fsync per run); runs are read back per MiB."""
    runs = []
    for off in offsets:
        if runs and off == runs[-1][0] + runs[-1][1] * BS and runs[-1][1] < longest:
            runs[-1][1] += 1
        else:
            runs.append([off, 1])
    return runs


def ssh(cmd, **kw):
    return subprocess.run(["sh", str(HERE / "kano-ssh.sh"), cmd], **kw)


# One remote call: identity of the target disk and of the disk holding /, mounts, swaps, holders, the kernel's
# partition table and the first MiB of the target (its primary GPT). TARGET= is prepended; empty = the root disk.
FACTS = r'''
set -euo pipefail
ROOTSRC=$(findmnt -no SOURCE /)
ROOTDISK=$(lsblk -no PKNAME "$ROOTSRC")
D=${TARGET:-/dev/$ROOTDISK}
B=${D##*/}
echo "ROOTSRC $ROOTSRC"
echo "ROOTDISK $ROOTDISK"
echo "ROOTTRAN $(lsblk -dno TRAN "/dev/$ROOTDISK")"
echo "ROOTSERIAL $(lsblk -dno SERIAL "/dev/$ROOTDISK")"
echo "DISK $B"
echo "TRAN $(lsblk -dno TRAN "$D")"
echo "SERIAL $(lsblk -dno SERIAL "$D")"
echo "SIZE $(lsblk -dbno SIZE "$D")"
while read -r src mp rest; do echo "MOUNT $src $mp"; done < /proc/self/mounts
tail -n +2 /proc/swaps | while read -r src rest; do echo "SWAP $src"; done
[ -d "/sys/block/$B/holders" ] || { echo "cannot read the holders of $B" >&2; exit 3; }
for h in /sys/block/$B/holders/*; do if [ -e "$h" ]; then echo "HOLDER $B ${h##*/}"; fi; done
for p in /sys/block/$B/$B*; do
  if [ -e "$p/partition" ]; then
    n=${p##*/}
    [ -d "$p/holders" ] || { echo "cannot read the holders of $n" >&2; exit 3; }
    echo "KPART $n $(cat "$p/partition") $(cat "$p/start") $(cat "$p/size")"
    for h in "$p"/holders/*; do if [ -e "$h" ]; then echo "HOLDER $n ${h##*/}"; fi; done
  fi
done
echo "GPT $(dd if="$D" bs=1048576 count=1 status=none | base64 | tr -d '\n')"
echo FACTS_OK
'''


def remote_facts(target=""):
    r = ssh("bash -s", input=f"TARGET={target}\n{FACTS}", capture_output=True, text=True)
    lines = r.stdout.splitlines()
    if r.returncode != 0 or not lines or lines[-1] != "FACTS_OK":
        refuse(f"could not read the target's identity and layout (ssh exit {r.returncode}): {r.stderr.strip()[-400:]}")
    f = {"MOUNT": [], "SWAP": [], "HOLDER": [], "KPART": {}}
    for line in lines[:-1]:
        key, _, val = line.partition(" ")
        if key == "MOUNT":
            src, _, mp = val.partition(" ")
            f["MOUNT"].append((src, mp))
        elif key == "SWAP":
            f["SWAP"].append(val.strip())
        elif key == "HOLDER":
            owner, _, holder = val.partition(" ")
            f["HOLDER"].append((owner, holder.strip()))
        elif key == "KPART":
            name, num, start, size = val.split()
            f["KPART"][int(num)] = (name, int(start) * 512, (int(start) + int(size)) * 512)
        elif key == "GPT":
            f["GPT"] = base64.b64decode(val)
        else:
            f[key] = val.strip()
    try:
        f["PARTS"] = gpt_parts(f.get("GPT", b""))
    except ValueError as e:
        refuse(f"target {f.get('DISK')}: {e}")
    return f


def busy_parts(f):
    """Block device names (without /dev/) that are mounted, swap or held, with the reason."""
    busy = {}
    for src, mp in f["MOUNT"]:
        if src.startswith("/dev/"):
            busy.setdefault(src[5:], f"mounted on {mp}")
    for src in f["SWAP"]:
        busy.setdefault(src[5:] if src.startswith("/dev/") else src, "used as swap")
    for owner, holder in f["HOLDER"]:
        busy.setdefault(owner, f"held by {holder}")
    return busy


def overlap_errors(f, writes, allowed):
    """Writes may only touch on-disk partitions whose number is in `allowed`, and no busy partition or disk."""
    err, busy = [], busy_parts(f)
    if f["DISK"] in busy:
        err.append(f"the whole disk {f['DISK']} is in use ({busy[f['DISK']]})")
    for a, b in writes:
        for num, name, s, e in f["PARTS"]:
            if s < b and a < e and num not in allowed:
                err.append(f"write at MiB {a // BS}-{b // BS} overlaps partition {num} {name!r}, which may not be written")
        for num, (kname, s, e) in sorted(f["KPART"].items()):
            if s < b and a < e and kname in busy:
                err.append(f"write at MiB {a // BS}-{b // BS} overlaps {kname}, which is {busy[kname]}")
    return list(dict.fromkeys(err))


def span(p):
    return f"{p[2]}-{p[3]}" if p else "missing"


def check_stick(f, img, size, writes):
    """All reasons to refuse a push of `writes` (byte ranges) to the stick described by facts `f`."""
    err = []
    disk = f.get("ROOTDISK", "")
    if not re.fullmatch(r"sd[a-z]+", disk):
        err.append(f"the maintenance root {f.get('ROOTSRC')!r} is not on a /dev/sdX disk")
    if f.get("TRAN") != "usb":
        err.append(f"target {disk} is not a USB disk (TRAN={f.get('TRAN')!r})")
    if not os.environ.get("KANO_STICK_SERIAL") or f.get("SERIAL", "").strip() != os.environ["KANO_STICK_SERIAL"]:
        err.append(f"target serial {f.get('SERIAL')!r} is not the Kingston stick ({os.environ['KANO_STICK_SERIAL']})")
    if not f.get("SIZE", "").isdigit() or int(f["SIZE"]) < size:
        err.append(f"target size {f.get('SIZE')!r} is smaller than the image ({size})")
    remote = {p[0]: p for p in f["PARTS"]}
    for p in img:
        if remote.get(p[0]) != p:
            err.append(f"partition {p[0]} {p[1]!r}: image {span(p)}, stick GPT {span(remote.get(p[0]))} {remote.get(p[0], (0, ''))[1]!r}")
        k = f["KPART"].get(p[0])
        if k is None or k[1:] != p[2:]:
            err.append(f"partition {p[0]} {p[1]!r}: the kernel's table ({k}) differs from the image ({span(p)})")
    err += overlap_errors(f, writes, allowed={p[0] for p in img if remote.get(p[0]) == p})
    return err


def write_guard(f, ident, why, a, b):
    """Shell prefix for one dd of bytes [a, b) to the disk of facts `f`, run in the same remote command: the identity
    test `ident` (refused with `why`), then a fresh look at what the checks established: the primary GPT (SHA-256 of
    the first MiB, which no push writes), the kernel's start and size of every partition the range overlaps, and that
    neither the disk nor those partitions are mounted, used as swap or held. Any failed query or difference exits
    IDCHECK_FAILED before the dd starts."""
    disk = f["DISK"]
    gpt = hashlib.sha256(f["GPT"]).hexdigest()
    parts = [(k, s // 512, (e - s) // 512) for _, (k, s, e) in sorted(f["KPART"].items()) if s < b and a < e]
    sh = [f'no() {{ echo "REFUSE: /dev/{disk}: $*" >&2; exit {IDCHECK_FAILED}; }}',
          f'{{ {ident}; }} || no "{why}"',
          f'G=$(dd if=/dev/{disk} bs={BS} count=1 status=none | sha256sum) || no "cannot read the GPT"',
          f'[ "${{G%% *}}" = {gpt} ] || no "the partition table changed since the checks"',
          'M=$(cat /proc/self/mounts) || no "cannot read /proc/self/mounts"',
          'W=$(cat /proc/swaps) || no "cannot read /proc/swaps"']
    for n, start, size in [(disk, None, None)] + parts:
        sysd = f"/sys/block/{disk}" + ("" if n == disk else f"/{n}")
        if start is not None:
            sh.append(f'[ "$(cat {sysd}/start)" = {start} ] && [ "$(cat {sysd}/size)" = {size} ] || '
                      f'no "{n} is not where the checks saw it"')
        sh += [f'printf "%s\\n" "$M" | grep -q "^/dev/{n} "; [ $? -eq 1 ] || no "{n} is mounted"',
               f'printf "%s\\n" "$W" | grep -q "^/dev/{n}[[:space:]]"; [ $? -eq 1 ] || no "{n} is used as swap"',
               f'H=$(ls -A {sysd}/holders) || no "cannot read the holders of {n}"',
               f'[ -z "$H" ] || no "{n} is held by $H"']
    return "; ".join(sh) + "; "


VERIFY = ("python3 -c 'import sys,hashlib\nwhile True:\n b=sys.stdin.buffer.read(1048576)\n"
          " if not b: break\n print(hashlib.sha256(b).hexdigest())'")


def push(dev, runs, fn, guard, pause):
    """Write each run of the open image `fn` to `dev` (remote), guard(start, end) prefixed to every dd; return MiB
    skipped."""
    def disk_shas(off, n):
        r = ssh(f"dd if={dev} bs={BS} skip={off // BS} count={n} iflag=direct status=none | {VERIFY}",
                capture_output=True, text=True)
        return r.stdout.split() if r.returncode == 0 else []
    skipped = 0
    for i, (off, n) in enumerate(runs):
        fn.seek(off)
        data = fn.read(n * BS)
        want = [hashlib.sha256(data[k * BS:(k + 1) * BS]).hexdigest() for k in range(n)]
        for attempt in range(6):
            if disk_shas(off, n) == want:
                if attempt == 0:
                    skipped += n
                break
            w = ssh(guard(off, off + n * BS) +
                    f"dd of={dev} bs={BS} seek={off // BS} count={n} iflag=fullblock conv=notrunc,fsync status=none",
                    input=data)
            if w.returncode == IDCHECK_FAILED:
                sys.exit(f"refusing: {dev} failed the identity check in front of the write at MiB {off // BS}; stopped")
            if w.returncode == 0 and disk_shas(off, n) == want:
                break
            time.sleep(pause * (attempt + 1))
        else:
            sys.exit(f"run at {off} ({n} MiB) not written after 6 attempts")
        print(f"run {i + 1}/{len(runs)}: {n} MiB at {off // BS} ok", flush=True)
    return skipped


def stick_ident(disk):
    """Shell test: /dev/<disk> is still the USB stick with KANO_STICK_SERIAL that holds the maintenance root."""
    serial = os.environ["KANO_STICK_SERIAL"]
    return (f'R=$(findmnt -no SOURCE /) && [ "$(lsblk -no PKNAME "$R")" = {disk} ] && '
            f'[ "$(lsblk -dno TRAN /dev/{disk})" = usb ] && '
            f'[ "$(lsblk -dno SERIAL /dev/{disk} | tr -d \' \')" = {serial} ]')


def parse_args(argv):
    args = [a for a in argv if a != "--dry-run"]
    if len(args) != 3:
        sys.exit(__doc__)
    return args[0], args[1], args[2], "--dry-run" in argv


def main(argv):
    load_env()
    old, new, ip, dry = parse_args(argv)
    if not dry:
        os.environ["KANO_IP"] = ip                   # kano-ssh.sh connects to KANO_IP
        require("KANO_IP", "KANO_SSH_KEY")
        require("KANO_STICK_SERIAL", serial=True)
    po, pn = image_parts(old), image_parts(new)
    if po != pn:
        refuse("partition tables differ")
    size = os.path.getsize(new)
    if size != os.path.getsize(old):
        refuse("image sizes differ")
    changed = changed_blocks(old, new, size)

    def inside(off):
        return any(s <= off and off + BS <= e for _, _, s, e in pn)
    bad = [o for o in changed if o < BS or not inside(o)]
    print(f"changed blocks: {len(changed)} ({len(changed)} MiB); outside partitions: {len(bad)}")
    for _, name, s, e in pn:
        k = sum(1 for o in changed if s <= o < e)
        if k:
            print(f"  {name}: {k} MiB")
    if bad:
        refuse(f"changed blocks outside partition bounds at {bad[:5]}")
    if dry:
        sys.exit(0)
    runs = make_runs(changed)
    f = remote_facts()
    errors = check_stick(f, pn, size, [(o, o + n * BS) for o, n in runs])
    if errors:
        for e in errors:
            print("  -", e, file=sys.stderr)
        refuse(f"{len(errors)} check(s) failed on the target")
    disk = f["ROOTDISK"]
    print(f"target disk: /dev/{disk} serial {f['SERIAL']} ({f['TRAN']}, {f['SIZE']} bytes); partitions match the image",
          flush=True)
    with open(new, "rb") as fn:
        skipped = push(f"/dev/{disk}", runs, fn, lambda a, b: write_guard(
            f, stick_ident(disk), "no longer the Kingston stick holding the maintenance root", a, b), pause=10)
    print(f"already on the stick: {skipped} MiB")
    print(f"pushed and verified {len(changed)} MiB")


if __name__ == "__main__":
    main(sys.argv[1:])
