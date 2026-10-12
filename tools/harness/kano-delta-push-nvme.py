#!/usr/bin/env python3
"""Push the changed 1 MiB blocks of a Googlebook image to the KANO's internal NVMe (maintenance OS, NVMe install entry).

usage: kano-delta-push-nvme.py <old.img> <new.img> <kano-ip> [--dry-run]

Only blocks inside super, vbmeta_a and vbmeta_b are written (vendor lives in super). Changed EFI-SYSTEM blocks are
skipped and reported: the NVMe ESP carries its own initrd (NVMe boot_part_uuid) that is updated separately. Any other
changed block (GPT, userdata — moved on the NVMe — or another partition) makes the push refuse. --dry-run only
compares the two images (no ssh).

Before anything is written, one remote call collects the facts; the push refuses unless
- /dev/nvme0n1 has TRAN=nvme and the internal disk's serial (KANO_NVME_SERIAL; it and KANO_STICK_SERIAL must be set
  and alphanumeric, checked before any ssh);
- the running system is the maintenance OS: its root filesystem is on a USB disk with the Kingston serial
  (KANO_STICK_SERIAL), and the NVMe is not the root disk;
- nothing on the NVMe is mounted, used as swap or held by another device (dm, md, ...);
- super, vbmeta_a and vbmeta_b each exist exactly once in the NVMe's on-disk GPT with the image's start and end, the
  kernel's partition table agrees, and no written range overlaps any other NVMe partition.
Every write re-checks, in the same remote command as its dd (kano-delta-push.py write_guard): TRAN and serial of the
NVMe, that / is on the Kingston stick (identified positively: an lsblk that fails or prints nothing refuses), the GPT,
the kernel's table of the written partitions and that neither the NVMe nor those partitions are mounted, used as
swap or held. Each pushed run is read back per MiB and compared by SHA-256.
"""
import importlib.util, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True                    # no __pycache__ next to the harness
_spec = importlib.util.spec_from_file_location("kano_delta_push", HERE / "kano-delta-push.py")
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)

ALLOW = ("super", "vbmeta_a", "vbmeta_b")
SKIP = {"EFI-SYSTEM"}
DISK = "nvme0n1"
DEV = "/dev/" + DISK


def check_nvme(f, img, writes):
    """All reasons to refuse a push of `writes` (byte ranges) to the NVMe described by facts `f`."""
    err = []
    nvme_serial, stick_serial = os.environ["KANO_NVME_SERIAL"], os.environ["KANO_STICK_SERIAL"]
    if f.get("DISK") != DISK or f.get("TRAN") != "nvme":
        err.append(f"{DEV} is not an NVMe-attached disk (TRAN={f.get('TRAN')!r})")
    if not nvme_serial or f.get("SERIAL", "").strip() != nvme_serial:
        err.append(f"{DEV} serial {f.get('SERIAL')!r} is not the internal NVMe ({nvme_serial})")
    if f.get("ROOTDISK") == DISK or f.get("ROOTSRC", "").startswith(DEV):
        err.append(f"the running system's root ({f.get('ROOTSRC')}) is on the NVMe")
    if f.get("ROOTTRAN") != "usb" or not stick_serial or f.get("ROOTSERIAL", "").strip() != stick_serial:
        err.append(f"the running system is not the maintenance OS on the Kingston stick (root {f.get('ROOTSRC')!r} on "
                   f"{f.get('ROOTDISK')!r}, TRAN={f.get('ROOTTRAN')!r}, serial {f.get('ROOTSERIAL')!r})")
    for name, why in sorted(dp.busy_parts(f).items()):
        if name == DISK or name.startswith(DISK + "p"):
            err.append(f"{name} is in use: {why}")
    allowed = set()
    for name in ALLOW:
        mine = [p for p in img if p[1] == name]
        theirs = [p for p in f["PARTS"] if p[1] == name]
        if len(mine) != 1 or len(theirs) != 1:
            err.append(f"{name!r} is not exactly once in the image ({len(mine)}) and on the NVMe ({len(theirs)})")
            continue
        if theirs[0][2:] != mine[0][2:]:
            err.append(f"{name!r}: image {dp.span(mine[0])}, NVMe GPT {dp.span(theirs[0])}")
            continue
        k = f["KPART"].get(theirs[0][0])
        if k is None or k[1:] != mine[0][2:]:
            err.append(f"{name!r}: the kernel's table ({k}) differs from the image ({dp.span(mine[0])})")
            continue
        allowed.add(theirs[0][0])
    err += dp.overlap_errors(f, writes, allowed)
    return err


def nvme_ident():
    """Shell test: /dev/nvme0n1 is still the internal NVMe, and / is on the Kingston stick (not on the NVMe)."""
    serial, stick = os.environ["KANO_NVME_SERIAL"], os.environ["KANO_STICK_SERIAL"]
    return (f'[ "$(lsblk -dno TRAN {DEV})" = nvme ] && [ "$(lsblk -dno SERIAL {DEV} | tr -d \' \')" = {serial} ] && '
            f'R=$(findmnt -no SOURCE /) && P=$(lsblk -no PKNAME "$R") && [ -n "$P" ] && [ "$P" != {DISK} ] && '
            f'[ "$(lsblk -dno TRAN "/dev/$P")" = usb ] && [ "$(lsblk -dno SERIAL "/dev/$P" | tr -d \' \')" = {stick} ]')


def main(argv):
    dp.load_env()
    old, new, ip, dry = dp.parse_args(argv)
    if not dry:
        os.environ["KANO_IP"] = ip                   # kano-ssh.sh connects to KANO_IP
        dp.require("KANO_IP", "KANO_SSH_KEY")
        dp.require("KANO_NVME_SERIAL", "KANO_STICK_SERIAL", serial=True)
    po, pn = dp.image_parts(old), dp.image_parts(new)
    if po != pn:
        dp.refuse("partition tables differ")
    size = os.path.getsize(new)
    if size != os.path.getsize(old):
        dp.refuse("image sizes differ")
    changed = dp.changed_blocks(old, new, size)

    def owner(off):
        for _, name, s, e in pn:
            if s <= off and off + dp.BS <= e:
                return name
        return None
    by = {}
    for o in changed:
        by.setdefault(owner(o), []).append(o)
    print(f"changed blocks: {len(changed)} MiB; by partition: " + ", ".join(f"{k}={len(v)}" for k, v in by.items()))
    bad = {k: v for k, v in by.items() if k not in set(ALLOW) | SKIP}
    if bad:
        dp.refuse(f"changed blocks outside {sorted(ALLOW)} (+ skipped {sorted(SKIP)}): {sorted(map(str, bad))}")
    todo = sorted(o for k in ALLOW for o in by.get(k, []))
    print(f"to write on the NVMe: {len(todo)} MiB; skipped ESP blocks: {len(by.get('EFI-SYSTEM', []))}")
    if dry:
        sys.exit(0)
    runs = dp.make_runs(todo)
    f = dp.remote_facts(DEV)
    errors = check_nvme(f, pn, [(o, o + n * dp.BS) for o, n in runs])
    if errors:
        for e in errors:
            print("  -", e, file=sys.stderr)
        dp.refuse(f"{len(errors)} check(s) failed on the target")
    print(f"target disk: {DEV} serial {f['SERIAL']}; maintenance root {f['ROOTSRC']} on the stick; "
          f"bounds match for {list(ALLOW)}", flush=True)
    with open(new, "rb") as fn:
        dp.push(DEV, runs, fn, lambda a, b: dp.write_guard(
            f, nvme_ident(), "not the internal NVMe of the maintenance OS on the Kingston stick any more", a, b), pause=5)
    print(f"pushed and verified {len(todo)} MiB to the NVMe")


if __name__ == "__main__":
    main(sys.argv[1:])
