#!/usr/bin/env bash
# write-usb.sh tests. The only block device ever written is a loop device over a file of this test (part 3, root).
#  1. (any OS with python3) The Python writer embedded in install/write-usb.sh, extracted between its markers, must
#     refuse a regular file, a symlink, a character device and a missing path without creating or changing anything;
#     on Linux also a real block device node whose major:minor is not the confirmed one (refused before it is opened).
#     Its GPT step (move the backup GPT to the end of a larger disk, as sgdisk -e) runs on plain files in gpt-test.py.
#  2. (Linux) write-usb.sh --check-only with lsblk/findmnt replaced by stubs that describe a pretend USB disk, and with
#     GBOS_WUSB_TEST_SYSFS pointing at a fake /sys (write-usb.sh honours it only together with --check-only). The pretend
#     disk borrows the node of an unattached loop device: it is only stat'ed, and as root the exclusive-open probe also
#     opens it read-only. --check-only runs the same identity and in-use re-check that follows the serial prompt in a
#     real run, so the stubs can change the SERIAL or add a mount between the two rounds. The root-disk case runs against
#     this machine's real root disk and /sys. Without a spare loop device node the pretend-disk cases are counted as
#     skipped, one by one.
#  3. (Linux, root) the writer itself on a loop device attached to a file of this test: refused while another program
#     holds the device, then written, read back, backup GPT moved, and sgdisk -v clean.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
T=$(mktemp -d "${TMPDIR:-/tmp}/gbos-wusb.XXXXXX")
LOOP=''; HOLDER=''
cleanup() {
    if [ -n "$HOLDER" ]; then kill "$HOLDER" 2>/dev/null; wait "$HOLDER" 2>/dev/null; fi
    if [ -n "$LOOP" ]; then losetup -d "$LOOP" 2>/dev/null; fi
    rm -rf "$T"
}
trap cleanup EXIT
export PYTHONDONTWRITEBYTECODE=1
pass=0; fail=0; skip=0; out=''
DRY=0   # 1: the cases are listed and counted as skipped instead of run
skp()  { echo "SKIP $1"; skip=$((skip + 1)); }
ok()   { if [ "$DRY" = 1 ]; then skp "$1"; return; fi; echo "PASS $1"; pass=$((pass + 1)); }
bad()  { if [ "$DRY" = 1 ]; then skp "$1"; return; fi; echo "FAIL $1"; fail=$((fail + 1)); }
finish() {
    echo "write-usb tests: $pass passed, $fail failed, $skip skipped"
    if [ "$skip" -gt 0 ]; then echo "NOTE: $skip case(s) did not run on this host (SKIP lines above); they are not covered by this run"; fi
    [ "$fail" = 0 ]; exit
}
command -v python3 >/dev/null 2>&1 || { bad "python3 is needed"; finish; }
mkimg() {  # mkimg <file> <sectors>: a GPT image (protective MBR, both GPTs, one partition) and its .sha256
    python3 - "$1" "$2" "$ROOT/tests/stick" << 'PY' || return 1
import sys
sys.path.insert(0, sys.argv[3])
import gptlib
n = int(sys.argv[2])
with open(sys.argv[1], "wb") as f:
    f.truncate(n * 512)
    gptlib.write_gpt(f, n, [dict(num=1, name="data", type="0fc63daf-8483-4772-8e79-3d69d8477de4", first=40, last=n - 34)])
    f.seek(100 * 512)
    f.write(b"image data " * 100)
PY
    (cd "$(dirname "$1")" && python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest() + "  " + sys.argv[1])' \
        "$(basename "$1")" > "$(basename "$1").sha256")
}

# --- 1. the embedded writer
W="$T/write-usb.py"
sed -n '/^# >>> write-usb.py/,/^# <<< write-usb.py/p' "$ROOT/install/write-usb.sh" > "$W"
if grep -q '^def main' "$W" && python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$W"; then
    ok "the embedded writer extracts and parses"
else
    bad "the embedded writer does not extract between its markers"; finish
fi
mkimg "$T/x.img" 2048 || { bad "cannot make the test image"; finish; }
SHA=$(cut -c1-64 "$T/x.img.sha256")
wexpect() {  # wexpect <name> <exit code> <regex> <writer args...>
    local name=$1 code=$2 re=$3 out rc; shift 3
    if [ "$DRY" = 1 ]; then skp "$name"; return; fi
    out=$(python3 "$W" "$@" 2>&1 < /dev/null); rc=$?
    if [ "$rc" = "$code" ] && grep -Eq -- "$re" <<< "$out"; then ok "$name"; else bad "$name (rc=$rc): $(tail -n 2 <<< "$out")"; fi
}
GB=1073741824
printf 'precious\n' > "$T/plain"; cp "$T/plain" "$T/plain.orig"; ln -s "$T/plain" "$T/link"
wexpect "writer refuses a regular file" 2 "not a block device" write "$T/x.img" "$SHA" "$T/plain" 8:16 $GB /sys/x
wexpect "writer refuses a symlink to a regular file" 2 "not a block device" write "$T/x.img" "$SHA" "$T/link" 8:16 $GB /sys/x
wexpect "writer refuses a character device" 2 "not a block device" write "$T/x.img" "$SHA" /dev/null 8:16 $GB /sys/x
wexpect "probe refuses a regular file" 2 "not a block device" probe "$T/plain" 8:16
cmp -s "$T/plain" "$T/plain.orig" && ok "the regular file is unchanged" || bad "the regular file was changed"
wexpect "writer refuses a missing path" 2 "does not exist" write "$T/x.img" "$SHA" "$T/sdz" 8:16 $GB /sys/x
wexpect "writer refuses a path in a missing directory" 2 "does not exist" write "$T/x.img" "$SHA" "$T/nodir/sdz" 8:16 $GB /sys/x
wexpect "probe refuses a missing path" 2 "does not exist" probe "$T/sdz" 8:16
[ ! -e "$T/sdz" ] && [ ! -L "$T/sdz" ] && [ ! -e "$T/nodir" ] && ok "nothing was created at the missing paths" \
    || bad "the writer created a file at a missing path"
wexpect "writer refuses an image larger than the disk" 2 "larger than the disk" write "$T/x.img" "$SHA" "$T/sdz" 8:16 4096 /sys/x
wexpect "writer refuses a malformed sha256" 2 "bad sha256" write "$T/x.img" nothex "$T/sdz" 8:16 $GB /sys/x
wexpect "writer refuses a malformed command line" 2 "usage" write "$T/x.img"
wexpect "the image GPT check accepts a good image" 0 "image GPT OK" gptcheck "$T/x.img" $GB
cp "$T/x.img" "$T/bad.img"; printf '\001' | dd of="$T/bad.img" bs=1 seek=552 conv=notrunc 2>/dev/null
wexpect "the image GPT check refuses a damaged GPT header" 2 "CRC" gptcheck "$T/bad.img" $GB
wexpect "the writer refuses a damaged image GPT before touching the disk" 2 "CRC" write "$T/bad.img" "$SHA" "$T/sdz" 8:16 $GB /sys/x
mkdir -p "$T/gpt"
if out=$(python3 "$ROOT/tests/install/gpt-test.py" "$W" "$T/gpt" 2>&1); then ok "GPT step on plain files (gpt-test.py)"
else bad "GPT step on plain files (gpt-test.py)"; fi
printf '%s\n' "$out" | sed 's/^/    /'
if grep -q '^SKIP' <<< "$out"; then skp "gpt-test.py: comparison with sgdisk -e (sgdisk is not installed)"; fi

if [ "$(uname -s)" != Linux ]; then skp "write-usb.sh guard tests (Linux only)"; finish; fi

# --- 2. write-usb.sh guards (Linux)
REAL_LSBLK=$(command -v lsblk) || { bad "lsblk is missing"; finish; }
REAL_FINDMNT=$(command -v findmnt) || { bad "findmnt is missing"; finish; }
majmin_of() { local t; t=$(stat -L -c '%t %T' -- "$1") && printf '%d:%d\n' "0x${t% *}" "0x${t#* }"; }
rootsrc=$(findmnt -n -v -o SOURCE / 2>/dev/null | head -n 1); rootdisk=
if [ -b "$rootsrc" ]; then rootdisk=$(lsblk -n -r -p -o NAME,TYPE -s "$rootsrc" 2>/dev/null | awk '$2 == "disk" {print $1; exit}'); fi
spare=
for d in /dev/loop[0-9]*; do   # an unattached loop device: nothing on it, nothing mounted, no holders
    [ -b "$d" ] && [ "$d" != "$rootdisk" ] || continue
    [ "$(cat "/sys/block/${d##*/}/size" 2>/dev/null)" = 0 ] || continue
    spare=$d; break
done
if [ -z "$spare" ] && [ "$(id -u)" = 0 ]; then   # e.g. snaps hold loop0-7: ask loop-control for a free node (attaches nothing)
    d=$(losetup -f 2>/dev/null) && [ -b "$d" ] && [ "$d" != "$rootdisk" ] && spare=$d
fi

mkdir -p "$T/bin"
cat > "$T/bin/lsblk" << 'STUB'
#!/usr/bin/env bash
# fake lsblk: answers write-usb.sh's queries about the pretend disk $FAKE_DEV; -s and anything else go to the real one
cols='' dev='' inverse=0 prev=''
for a in "$@"; do
    if [ "$prev" = -o ]; then cols=$a; fi
    case $a in -s | --inverse) inverse=1 ;; /*) dev=$a ;; esac
    prev=$a
done
if [ "$inverse" = 1 ] || [ -z "${FAKE_DEV:-}" ] || [ "$dev" != "$FAKE_DEV" ]; then exec "$REAL_LSBLK" "$@"; fi
if [ -n "${FAKE_LSBLK_FAIL:-}" ] && [ "${cols/"$FAKE_LSBLK_FAIL"/}" != "$cols" ]; then
    echo "lsblk: $dev: failed to read the device" >&2; exit 32
fi
nth() {  # nth <query>: how many times this query has been answered in this test, this time included
    local f="$FAKE_STATE/$1" c=0
    if [ -f "$f" ]; then c=$(cat "$f"); fi
    c=$((c + 1)); echo "$c" > "$f"; echo "$c"
}
case $cols in
    TYPE) echo "${FAKE_TYPE:-disk}" ;;
    TRAN) echo "${FAKE_TRAN:-usb}" ;;
    SERIAL)
        if [ "$(nth serial)" -ge 2 ] && [ -n "${FAKE_SERIAL_2+set}" ]; then echo "$FAKE_SERIAL_2"; else echo "${FAKE_SERIAL-FAKE123}"; fi ;;
    NAME,MAJ:MIN,MOUNTPOINT)
        mp=${FAKE_MOUNT:-}
        if [ "$(nth tree)" -ge 2 ] && [ -n "${FAKE_MOUNT_2:-}" ]; then mp=$FAKE_MOUNT_2; fi
        echo "$dev $FAKE_MAJMIN "
        echo "${dev}p1 0:0 $mp" ;;
    NAME,SIZE,TRAN,MODEL,SERIAL) printf 'NAME SIZE TRAN MODEL SERIAL\n%s 1G usb Fake %s\n' "${dev##*/}" "${FAKE_SERIAL-FAKE123}" ;;
    *) exec "$REAL_LSBLK" "$@" ;;
esac
STUB
cat > "$T/bin/findmnt" << 'STUB'
#!/usr/bin/env bash
# fake findmnt: FAKE_FINDMNT_FAIL=root|all fails the query for / or for the whole mount table; FAKE_FINDMNT_ADD adds a
# source to the whole table; everything else is the real findmnt
last=${!#}
case "${FAKE_FINDMNT_FAIL:-}:$last" in
    root:/ | all:SOURCE) echo "findmnt: cannot read /proc/self/mountinfo" >&2; exit 1 ;;
esac
if [ "$last" = SOURCE ] && [ -n "${FAKE_FINDMNT_ADD:-}" ]; then
    "$REAL_FINDMNT" "$@" || exit
    echo "$FAKE_FINDMNT_ADD"; exit 0
fi
exec "$REAL_FINDMNT" "$@"
STUB
chmod +x "$T/bin/lsblk" "$T/bin/findmnt"
(cd "$T" && sha256sum x.img > x.img.sha256)

expect() {  # expect <name> <exit code> <regex> [VAR=value ...] -- <write-usb.sh args...>; the output stays in $out
    local name=$1 code=$2 re=$3 st rc; shift 3
    if [ "$DRY" = 1 ]; then skp "$name"; return; fi
    local -a envs=()
    while [ $# -gt 0 ] && [ "$1" != -- ]; do envs+=("$1"); shift; done
    shift
    st=$(mktemp -d "$T/state.XXXXXX")
    out=$(env PATH="$T/bin:$PATH" REAL_LSBLK="$REAL_LSBLK" REAL_FINDMNT="$REAL_FINDMNT" FAKE_STATE="$st" \
        FAKE_DEV="$spare" FAKE_MAJMIN="$spare_mm" GBOS_WUSB_TEST_SYSFS="$T/sys" ${envs[@]+"${envs[@]}"} \
        bash "$ROOT/install/write-usb.sh" "$@" 2>&1 < /dev/null); rc=$?
    if [ "$rc" = "$code" ] && grep -Eq -- "$re" <<< "$out"; then ok "$name"; else bad "$name (rc=$rc): $(tail -n 2 <<< "$out")"; fi
}
has() {  # has <name> <regex>: the last expect's output must match
    if [ "$DRY" = 1 ]; then skp "$1"; return; fi
    if grep -Eq -- "$2" <<< "$out"; then ok "$1"; else bad "$1"; fi
}
lacks() {  # lacks <name> <regex>: the last expect's output must not match
    if [ "$DRY" = 1 ]; then skp "$1"; return; fi
    if grep -Eq -- "$2" <<< "$out"; then bad "$1"; else ok "$1"; fi
}
mksys() {  # mksys [sectors] [logical sector size]: a fresh fake /sys/block/<spare> for the pretend disk
    local b="$T/sys/block/$sname"
    rm -rf "$T/sys"; mkdir -p "$b/queue" "$b/holders" "$b/${sname}p1/holders"
    echo "$spare_mm" > "$b/dev"; echo "${1:-2097152}" > "$b/size"; echo "${2:-512}" > "$b/queue/logical_block_size"
    echo 1 > "$b/removable"
}
spare_mm=; sname=
C=(--check-only "$T/x.img")

expect "refuses a character device" 2 "not a block device" -- "${C[@]}" /dev/null
expect "refuses a missing path" 2 "does not exist" -- "${C[@]}" "$T/no-such-disk"
expect "refuses --without-fallback without --keep-data" 2 "goes with --keep-data" -- --without-fallback "$T/x.img" /dev/null
if [ -n "$rootdisk" ]; then
    expect "refuses the disk holding / even when it claims to be USB" 2 "holds the running system" \
        FAKE_DEV="$rootdisk" FAKE_MAJMIN="$(majmin_of "$rootdisk")" GBOS_WUSB_TEST_SYSFS= -- "${C[@]}" "$rootdisk"
else
    skp "root-disk case: / is not mounted from a disk node here (${rootsrc:-unknown})"
fi

# a PATH that has everything write-usb.sh and the stubs use, except sgdisk
mkdir -p "$T/nosg"
for c in bash sh env id readlink stat grep cat python3 sha256sum cut head sed tail awk tr ls mktemp rm; do
    p=$(command -v "$c") && ln -s "$p" "$T/nosg/$c"
done

pretend_cases() {  # every case on the pretend disk; with DRY=1 each one is only counted as skipped
    [ "$DRY" = 1 ] || spare_mm=$(majmin_of "$spare"); sname=${spare##*/}
    wexpect "writer refuses a block device that is not the confirmed major:minor, before opening it" 2 "not the confirmed" \
        write "$T/x.img" "$SHA" "$spare" 0:0 $GB /sys/x
    wexpect "probe refuses a block device that is not the confirmed major:minor" 2 "not the confirmed" probe "$spare" 0:0

    mksys
    expect "sysfs test hook is refused without --check-only" 2 "only with --check-only" -- "$T/x.img" "$spare"
    expect "accepts a USB whole disk (check only)" 0 "CHECKS OK" -- "${C[@]}" "$spare"
    has "check-only runs the after-prompt re-check" "== re-checking"
    if ! command -v lz4 > /dev/null || ! command -v minfo > /dev/null; then
        skp "--keep-data check only (needs lz4 and mtools)"
    elif [ "$(id -u)" != 0 ]; then
        expect "--keep-data: the same checks pass (check only)" 0 "CHECKS OK" -- --check-only --keep-data "$T/x.img" "$spare"
        has "--keep-data as a user says it did not read the disk" "gbos-update.py inspect needs root"
    else
        expect "--keep-data refuses a disk without Googlebook on it" 2 "holds no Googlebook that can be updated" -- --check-only --keep-data "$T/x.img" "$spare"
    fi
    if [ "$(id -u)" != 0 ]; then
        has "check-only as a user says it skipped the root-only probe" "exclusive-open probe .* skipped"
    fi
    expect "refuses a non-USB transport" 2 "not on the USB transport" FAKE_TRAN=nvme -- "${C[@]}" "$spare"
    expect "refuses a partition" 2 "not a whole disk" FAKE_TYPE=part -- "${C[@]}" "$spare"
    expect "refuses a disk without a serial" 2 "no serial" FAKE_SERIAL= -- "${C[@]}" "$spare"
    expect "refuses a mounted partition (lsblk MOUNTPOINT)" 2 "p1 is mounted or used as swap \(/media/usb\)" \
        FAKE_MOUNT=/media/usb -- "${C[@]}" "$spare"
    expect "refuses a partition that only findmnt lists as mounted" 2 "p1 is mounted or used as swap \(findmnt\)" \
        FAKE_FINDMNT_ADD="${spare}p1" -- "${C[@]}" "$spare"
    expect "refuses when lsblk fails (SERIAL)" 2 "refusing: lsblk cannot report SERIAL" FAKE_LSBLK_FAIL=SERIAL -- "${C[@]}" "$spare"
    expect "refuses when lsblk fails (TRAN)" 2 "refusing: lsblk cannot report TRAN" FAKE_LSBLK_FAIL=TRAN -- "${C[@]}" "$spare"
    expect "refuses when lsblk fails (partitions and mounts)" 2 "refusing: lsblk cannot list" \
        FAKE_LSBLK_FAIL=MOUNTPOINT -- "${C[@]}" "$spare"
    expect "refuses when findmnt fails (source of /)" 2 "refusing: findmnt cannot tell" FAKE_FINDMNT_FAIL=root -- "${C[@]}" "$spare"
    expect "refuses when findmnt fails (mount table)" 2 "refusing: findmnt cannot list" FAKE_FINDMNT_FAIL=all -- "${C[@]}" "$spare"
    expect "refuses when the SERIAL changes between the prompt and the write" 2 "refusing \(re-check\): .* changed since it was confirmed" \
        FAKE_SERIAL_2=OTHER456 -- "${C[@]}" "$spare"
    lacks "nothing is accepted after the SERIAL changed" "CHECKS OK|== writing"
    has "the refusal shows the changed identity" "serial OTHER456"
    expect "refuses a disk that got mounted during the confirmation" 2 "refusing \(re-check\): .*mounted or used as swap" \
        FAKE_MOUNT_2=/media/usb -- "${C[@]}" "$spare"
    lacks "nothing is accepted after the mount appeared" "CHECKS OK|== writing"

    mksys 8
    expect "refuses a disk smaller than the image" 2 "smaller than the image" -- "${C[@]}" "$spare"
    mksys 2097152 4096
    expect "refuses 4096-byte logical sectors" 2 "4096-byte logical sectors" -- "${C[@]}" "$spare"
    mksys; touch "$T/sys/block/$sname/${sname}p1/holders/dm-7"
    expect "refuses a disk whose partition has a holder" 2 "in use by dm-7" -- "${C[@]}" "$spare"
    mksys; touch "$T/sys/block/$sname/holders/md127"
    expect "refuses a disk with a holder" 2 "in use by md127" -- "${C[@]}" "$spare"
    mksys; rmdir "$T/sys/block/$sname/holders"
    expect "refuses when the holders cannot be listed" 2 "cannot list the holders" -- "${C[@]}" "$spare"
    mksys; echo 1:1 > "$T/sys/block/$sname/dev"
    expect "refuses when sysfs and the node disagree on major:minor" 2 "dev says 1:1 but" -- "${C[@]}" "$spare"
    mksys; rm "$T/sys/block/$sname/size"
    expect "refuses when the size cannot be read" 2 "cannot read .*/size" -- "${C[@]}" "$spare"

    mksys
    expect "refuses when sgdisk is missing (needed for the final GPT check), nothing written" 2 "sgdisk is needed" \
        PATH="$T/bin:$T/nosg" -- "${C[@]}" "$spare"
    touch "$T/x.img.not-self-tested"
    expect "warns about an image that was never booted in QEMU" 0 "WARNING: .*--no-selftest" -- "${C[@]}" "$spare"
    rm "$T/x.img.not-self-tested"
    lacks "... and the warning is the only difference (no refusal)" "refusing"
    (cd "$T" && sha256sum bad.img > bad.img.sha256)
    expect "refuses an image whose GPT is damaged, before the prompt" 2 "GPT cannot be moved" -- --check-only "$T/bad.img" "$spare"
    printf '%064d  x.img\n' 0 > "$T/x.img.sha256"
    expect "refuses an image whose sha256 does not match" 1 "sha256 mismatch" -- "${C[@]}" "$spare"
    (cd "$T" && sha256sum x.img > x.img.sha256)
}
if [ -n "$spare" ]; then DRY=0; pretend_cases
else
    echo "SKIP no unattached loop device node (/dev/loopN with size 0) to borrow: the pretend-disk cases below do not run"
    DRY=1; pretend_cases; DRY=0
fi

# --- 3. the writer on a loop device over a file of this test (Linux, root)
if [ "$(id -u)" != 0 ]; then skp "writer on a loop device (needs root)"; finish; fi
command -v losetup > /dev/null && command -v sgdisk > /dev/null || { skp "writer on a loop device (needs losetup and sgdisk)"; finish; }
mkimg "$T/lo.img" 32768 || { bad "cannot make the loop test image"; finish; }
LOSHA=$(cut -c1-64 "$T/lo.img.sha256")
head -c $((24 << 20)) /dev/urandom > "$T/lo-disk"
LOOP=$(losetup --find --show "$T/lo-disk" 2>/dev/null) || { LOOP=''; skp "writer on a loop device (no loop device available here)"; finish; }
if [ "$(losetup -n -O BACK-FILE "$LOOP")" != "$T/lo-disk" ]; then bad "the loop device is not backed by the test file"; finish; fi
lmm=$(majmin_of "$LOOP"); lsys=$(readlink -f "/sys/dev/block/$lmm"); lbytes=$(( $(cat "/sys/block/${LOOP##*/}/size") * 512 ))
before=$(sha256sum < "$T/lo-disk")
python3 -c 'import os, sys, time; fd = os.open(sys.argv[1], os.O_RDONLY | os.O_EXCL); print("held", flush=True); time.sleep(60)' \
    "$LOOP" > "$T/holder.out" 2>&1 &
HOLDER=$!
for _ in $(seq 1 50); do grep -q held "$T/holder.out" && break; sleep 0.1; done
wexpect "the writer refuses a device another program holds exclusively" 2 "is in use" \
    write "$T/lo.img" "$LOSHA" "$LOOP" "$lmm" "$lbytes" "$lsys"
kill "$HOLDER"; wait "$HOLDER" 2>/dev/null; HOLDER=''
[ "$(sha256sum < "$T/lo-disk")" = "$before" ] && ok "... and wrote nothing" || bad "the refused write changed the device"
wexpect "the writer writes, reads back and moves the backup GPT to the end of the device" 0 "GPT read back: .*backup at LBA 49151" \
    write "$T/lo.img" "$LOSHA" "$LOOP" "$lmm" "$lbytes" "$lsys"
sg=$(sgdisk -v "$LOOP" 2>&1)
grep -q "No problems found" <<< "$sg" && ok "sgdisk -v finds no problems on the device" || bad "sgdisk -v on the device: $sg"
# write-usb.sh --keep-data hands the confirmed identity to gbos-update.py (--rdev, --syspath, --bytes); its own checks
# on a real block device, before anything else is read (rollback: the one command that needs no release)
uexp() {  # uexp <name> <output regex> -- <extra args>: refused (exit 2) with that output, the device unchanged
    local name=$1 re=$2 b out rc=0; shift 3
    b=$(sha256sum < "$LOOP")
    out=$(python3 -I "$ROOT/install/gbos-update.py" rollback "$LOOP" "$@" 2>&1) || rc=$?
    if [ "$rc" = 2 ] && grep -Eq -- "$re" <<< "$out" && [ "$(sha256sum < "$LOOP")" = "$b" ]; then ok "$name"; else bad "$name (rc=$rc): $(tail -n 2 <<< "$out")"; fi
}
uexp "gbos-update.py refuses a block device that is not the confirmed major:minor" "not the confirmed 7:9999" -- --rdev 7:9999
uexp "gbos-update.py refuses a block device whose sysfs path is not the confirmed one" "in sysfs, not the confirmed" -- \
    --rdev "$lmm" --syspath /sys/devices/virtual/block/loop-not-this --bytes "$lbytes"
uexp "gbos-update.py refuses a block device whose size is not the confirmed one" "bytes, not the confirmed" -- \
    --rdev "$lmm" --syspath "$lsys" --bytes $((lbytes + 512))
uexp "gbos-update.py with the confirmed identity goes on to read the disk (not Googlebook here: refused)" "no super partition" -- \
    --rdev "$lmm" --syspath "$lsys" --bytes "$lbytes"
losetup -d "$LOOP"; LOOP=''
if python3 - "$T/lo-disk" "$T/lo.img" "$ROOT/tests/stick" << 'PY'
import sys
sys.path.insert(0, sys.argv[3])
import gptlib
h, parts = gptlib.read_gpt(sys.argv[1])
hi, pi = gptlib.read_gpt(sys.argv[2])
d, i = open(sys.argv[1], "rb").read(), open(sys.argv[2], "rb").read()
assert h["alt"] == len(d) // 512 - 1 and parts == pi, "GPT"
assert d[1024:len(i)] == i[1024:], "image content"
PY
then ok "the device holds the image with a valid GPT whose backup is at its end (checked with tests/stick/gptlib.py)"
else bad "the device content or GPT is not what the writer reported"; fi
finish
