#!/usr/bin/env bash
# Update the internal-NVMe Googlebook from final<OLD> to final<NEW>: push super/vbmeta blocks to the NVMe, rewrite the
# bootconfig of the NVMe ESP initrd and of the stick XBOOTLDR copy (vbmeta digest/size; NVMe boot_part_uuid kept),
# then boot the default entry under the NVMe watcher.
# usage: kano-nvme-update.sh <old-final-number> <new-final-number>
#
# Safety
# - Results of an earlier run for the same version (bootconfig.txt, initrd-nvme.sha256, ...) are deleted first; every
#   step's exit status is checked and its result is read from a file this run wrote.
# - The NVMe's boot_a GUID is read from the disk and must equal KANO_NVME_BOOT_A_GUID before anything is written.
# - Remote scripts run with `set -euo pipefail` behind the target guards (see prelude); the block push has its own.
# - adb talks to the fixed address KANO_ADB_ADDR only, and powers it off only if that device carries this project's
#   vendor image (KANO_ANDROID_ID_PROP is set there); after the update boot, nothing is sent to it unless it was
#   identified that way. No network scan.
# - The watcher's exit status is checked: danger, power cut not confirmed or "unobservable" all fail the run.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
. "$HERE/kano-env.sh"
[ $# -eq 2 ] && [[ $1 =~ ^[0-9]+$ ]] && [[ $2 =~ ^[0-9]+$ ]] || { echo "usage: $0 <old-final-number> <new-final-number>" >&2; exit 2; }
kano_require KANO_IP KANO_SSH_KEY KANO_ADB KANO_ADB_ADDR KANO_NVME_SERIAL KANO_STICK_SERIAL KANO_NVME_PCI \
  KANO_NVME_BOOT_A_GUID KANO_EC_TTY KANO_AP_TTY KANO_RUNS KANO_WORK KANO_AP_UART_LOG
for v in "$KANO_NVME_SERIAL" "$KANO_STICK_SERIAL"; do
  [[ $v =~ ^[A-Za-z0-9]+$ ]] || { echo "refusing: odd serial '$v' in kano-env.sh"; exit 2; }
done
[[ $KANO_NVME_PCI =~ ^0x[0-9a-f]{4}:0x[0-9a-f]{4}$ ]] || { echo "refusing: odd KANO_NVME_PCI '$KANO_NVME_PCI'"; exit 2; }
[[ $KANO_NVME_BOOT_A_GUID =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || { echo "refusing: odd KANO_NVME_BOOT_A_GUID"; exit 2; }
O=$1; N=$2
R=$KANO_RUNS; W=$KANO_WORK; ADB=$KANO_ADB; UART=$KANO_AP_UART_LOG
OLDIMG=$W/kano-final$O/kano-final$O.img; NEWIMG=$W/kano-final$N/kano-final$N.img
OUT=$R/nvme-update-$N
mkdir -p "$OUT"
# Nothing from an earlier run of this version may stand in for this run's results.
rm -f "$OUT"/{bootconfig.txt,initrd-nvme.sha256,initrd-prep.txt,dryrun.txt,push.txt,esp.txt,esp.sh,esp.out,watch.txt,boot-maint.txt,nvme-id.sh,nvme-id.out}
[ ! -f "$OUT/run.log" ] || mv -f "$OUT/run.log" "$OUT/run.log.prev"
log() { echo "[$(date +%T)] $*" | tee -a "$OUT/run.log"; }
die() { log "ABORT: $*"; exit 1; }
show() { sed 's/^/    /' "$1" | tee -a "$OUT/run.log"; }
sha256() { if command -v sha256sum > /dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -c1-64; }
lower() { printf '%s' "$1" | tr 'A-F' 'a-f'; }
K() { "$HERE/kano-ssh.sh" "$@" < /dev/null; }
A() { timeout 60 "$ADB" -s "$KANO_ADB_ADDR" "$@" < /dev/null; }
# connect to the one fixed address and check that it is this project's Android before anything is sent to it
adb_kano() {
  timeout 30 "$ADB" connect "$KANO_ADB_ADDR" > /dev/null 2>&1 || true
  [ -n "$(A shell getprop "$KANO_ANDROID_ID_PROP" 2> /dev/null | tr -d '\r\n')" ]
}
uart_has() { tr -d '\000' < "$UART" 2> /dev/null | tail -c 4000 | grep -a -c "$1" > /dev/null; }
# only watchers that read this harness's AP UART log
stop_watchers() {
  ps -axo pid=,command= | awk -v a="gbos" -v b="-watch" -v lp="$UART" 'index($0, a b) && index($0, lp) && !/awk/ {print $1}' |
    while read -r p; do kill "$p" 2> /dev/null || true; done
}
EXTRA=""
# Top of every remote script: strict mode, constants, guard functions.
prelude() {
  printf 'set -euo pipefail\nNVME_SERIAL=%s\nNVME_PCI=%s\nSTICK_SERIAL=%s\n%s\n' \
    "$KANO_NVME_SERIAL" "$KANO_NVME_PCI" "$KANO_STICK_SERIAL" "$EXTRA"
  cat <<'EOF'
D=/dev/nvme0n1; S=/dev/sda
refuse() { echo "REFUSE: $*" >&2; exit 1; }
guard_id() {
  grep -q "initcall_blacklist=mei_me_driver_init " /proc/cmdline || refuse "not the maintenance NVMe install entry"
  [ -n "$NVME_SERIAL" ] && [ -n "$STICK_SERIAL" ] && [ -n "$NVME_PCI" ] || refuse "serials / PCI ID not configured"
  [ "$(lsblk -dno TRAN $D)" = nvme ] || refuse "nvme0n1 is not an NVMe-attached disk"
  [ "$(cat /sys/class/nvme/nvme0/device/vendor):$(cat /sys/class/nvme/nvme0/device/device)" = "$NVME_PCI" ] || refuse "nvme0 is not the internal Samsung ($NVME_PCI)"
  [ "$(lsblk -dno SERIAL $D | tr -d ' ')" = "$NVME_SERIAL" ] || refuse "nvme0n1 serial is not $NVME_SERIAL"
  [ "$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")" = sda ] || refuse "maintenance root not on sda"
  [ "$(lsblk -dno TRAN $S)" = usb ] && [ "$(lsblk -dno SERIAL $S | tr -d ' ')" = "$STICK_SERIAL" ] || refuse "sda is not the Kingston stick"
}
# none <pattern> <file>: true if the file was read and no line matches; an unreadable file refuses (grep status 2)
none() { local rc=0; grep -q -- "$1" "$2" || rc=$?; [ "$rc" -le 1 ] || refuse "cannot read $2"; [ "$rc" -eq 1 ]; }
guard_idle() {
  none '^/dev/nvme0n1' /proc/self/mounts || refuse "something on the NVMe is mounted"
  none '^/dev/nvme0n1' /proc/swaps || refuse "swap on the NVMe"
  [ -d /sys/block/nvme0n1/holders ] || refuse "cannot read the holders of the NVMe"
  for p in /sys/block/nvme0n1/nvme0n1*; do
    if [ -e "$p" ] && [ ! -d "$p/holders" ]; then refuse "cannot read the holders of ${p##*/}"; fi
  done
  for h in /sys/block/nvme0n1/holders/* /sys/block/nvme0n1/nvme0n1*/holders/*; do
    if [ -e "$h" ]; then refuse "the NVMe is held by ${h##*/}"; fi
  done
}
guard() { guard_id; guard_idle; echo "guards passed: $D is the idle internal NVMe $NVME_SERIAL, / is on the Kingston stick" >&2; }
EOF
}
# remote <name> <marker> < body: prelude + body + "echo <marker>" as one remote script (set -e: the marker
# line exists only if every command before it succeeded; stderr may arrive after it, so it is matched as a whole line); output in $OUT/<name>.out
remote() {
  local name=$1 marker=$2 rc=0
  { prelude; cat; printf 'echo %s\n' "$marker"; } > "$OUT/$name.sh"
  "$HERE/kano-ssh.sh" "bash -s" < "$OUT/$name.sh" > "$OUT/$name.out" 2>&1 || rc=$?
  show "$OUT/$name.out"
  [ "$rc" -eq 0 ] || die "$name: remote exit status $rc"
  grep -qx "$marker" "$OUT/$name.out" || die "$name: '$marker' missing from this run's output"
}

log "1. final$N QEMU + dry run"
[ -f "$W/kano-final$N.log" ] || die "no build log $W/kano-final$N.log"
if grep -q FAIL "$W/kano-final$N.log"; then die "QEMU FAIL in kano-final$N.log"; fi
grep -q "kano-final$N.img sha256" "$W/kano-final$N.log" || die "final$N not built"
rc=0; python3 "$HERE/kano-delta-push-nvme.py" "$OLDIMG" "$NEWIMG" "$KANO_IP" --dry-run > "$OUT/dryrun.txt" 2>&1 || rc=$?
show "$OUT/dryrun.txt"
[ $rc -eq 0 ] || die "dry run refused (exit $rc)"

log "2. NVMe initrd for final$N (bootconfig only)"
rc=0
python3 - "$W/kano-final$O/initrd.img" "$W/kano-final$N/initrd.img" "$(lower "$KANO_NVME_BOOT_A_GUID")" "$OUT" > "$OUT/initrd-prep.txt" 2>&1 <<'PY' || rc=$?
import hashlib, struct, sys
old, new, guid, out = sys.argv[1:5]
def split(d):
    magic = b"#BOOTCONFIG\n"
    if not d.endswith(magic): sys.exit("no bootconfig trailer")
    end = len(d) - len(magic); size, csum = struct.unpack_from("<II", d, end - 8); start = end - 8 - size
    bc = d[start:start + size]
    if (sum(bc) & 0xFFFFFFFF) != csum: sys.exit("bootconfig checksum mismatch")
    return d[:start], bc.decode()
ao, _ = split(open(old, "rb").read()); an, bn = split(open(new, "rb").read())
if ao != an: sys.exit("initrd archives differ: a full initrd copy would be needed")
lines = bn.split("\n"); k = 0
for i, l in enumerate(lines):
    if l.startswith("androidboot.boot_part_uuid="):
        lines[i] = "androidboot.boot_part_uuid=" + guid; k += 1
if k != 1: sys.exit(f"{k} boot_part_uuid lines")
raw = "\n".join(lines).encode()
open(out + "/bootconfig.txt", "wb").write(raw)
full = an + raw + struct.pack("<II", len(raw), sum(raw) & 0xFFFFFFFF) + b"#BOOTCONFIG\n"
open(out + "/initrd-nvme.sha256", "w").write(hashlib.sha256(full).hexdigest() + "\n")
print("archive unchanged; expected NVMe initrd sha256", hashlib.sha256(full).hexdigest())
PY
show "$OUT/initrd-prep.txt"
[ $rc -eq 0 ] || die "initrd prep failed (exit $rc)"
[ -s "$OUT/bootconfig.txt" ] && [ -s "$OUT/initrd-nvme.sha256" ] || die "initrd prep left no results"

log "3. graceful power-off, maint NVMe entry"
stop_watchers
if adb_kano; then
  if A reboot -p > /dev/null 2>&1; then
    for _ in $(seq 1 36); do uart_has "Power down" && break; sleep 5; done
  else
    log "adb reboot -p failed; the EC power-off follows"
  fi
else
  log "no KANO Android at $KANO_ADB_ADDR ($KANO_ANDROID_ID_PROP not set); the EC power-off follows"
fi
bash "$HERE/kano-boot.sh" "KANO maintenance NVMe install" > "$OUT/boot-maint.txt" 2>&1 < /dev/null || { show "$OUT/boot-maint.txt"; die "maint menu"; }
up=0; for _ in $(seq 1 72); do if K true 2> /dev/null; then up=1; break; fi; sleep 5; done
[ $up -eq 1 ] || die "the maintenance OS does not answer over ssh"

log "3b. NVMe identity; boot_a GUID read from the disk"
remote nvme-id NVME_ID_OK <<'EOF'
modprobe --ignore-install nvme_core; modprobe --ignore-install nvme; sleep 2
guard
P13=$(sgdisk -i 13 "$D")
NAME=$(printf '%s\n' "$P13" | sed -n "s/^Partition name: '\(.*\)'.*/\1/p")
[ "$NAME" = boot_a ] || refuse "NVMe partition 13 is '$NAME', not boot_a"
echo "BOOT_A_GUID=$(printf '%s\n' "$P13" | sed -n 's/^Partition unique GUID: \([0-9A-Fa-f-]*\).*/\1/p')"
EOF
GUID=$(sed -n 's/^BOOT_A_GUID=//p' "$OUT/nvme-id.out" | tail -n 1)
[ "$(lower "$GUID")" = "$(lower "$KANO_NVME_BOOT_A_GUID")" ] || die "NVMe boot_a GUID is '$GUID', expected $KANO_NVME_BOOT_A_GUID"

log "4. push super/vbmeta to the NVMe"
rc=0; python3 "$HERE/kano-delta-push-nvme.py" "$OLDIMG" "$NEWIMG" "$KANO_IP" > "$OUT/push.txt" 2>&1 || rc=$?
show "$OUT/push.txt"
[ $rc -eq 0 ] || die "nvme push failed (exit $rc)"
grep -q "^pushed and verified" "$OUT/push.txt" || die "nvme push: no 'pushed and verified' in this run's output"

log "5. rewrite the bootconfig of the NVMe ESP initrd and the stick XBOOTLDR copy"
"$HERE/kano-ssh.sh" "cat > /root/bootconfig-new.txt" < "$OUT/bootconfig.txt" || die "bootconfig transfer failed"
EXTRA="WANT=$(cut -c1-64 "$OUT/initrd-nvme.sha256"); BC=$(sha256 "$OUT/bootconfig.txt")"
remote esp INITRD_OK <<'EOF'
guard
[ "$(sha256sum /root/bootconfig-new.txt | cut -c1-64)" = "$BC" ] || refuse "bootconfig-new.txt arrived damaged"
mkdir -p /mnt/nesp /mnt/xb
trap 'umount /mnt/nesp /mnt/xb 2>/dev/null || true' EXIT
mount /dev/nvme0n1p12 /mnt/nesp
mount /dev/sda32 /mnt/xb
python3 - /mnt/nesp/initrd.img /root/bootconfig-new.txt /root/initrd-nvme.new <<'PY'
import struct, sys
d = open(sys.argv[1], "rb").read(); magic = b"#BOOTCONFIG\n"
if not d.endswith(magic): sys.exit("no bootconfig trailer on the NVMe ESP initrd")
end = len(d) - len(magic); size, csum = struct.unpack_from("<II", d, end - 8); start = end - 8 - size
raw = open(sys.argv[2], "rb").read()
open(sys.argv[3], "wb").write(d[:start] + raw + struct.pack("<II", len(raw), sum(raw) & 0xFFFFFFFF) + magic)
PY
[ "$(sha256sum /root/initrd-nvme.new | cut -c1-64)" = "$WANT" ] || refuse "rebuilt initrd sha256 mismatch"
cp /root/initrd-nvme.new /mnt/nesp/initrd.img
cp /root/initrd-nvme.new /mnt/xb/gbos-nvme/initrd.img
sync
[ "$(sha256sum /mnt/nesp/initrd.img | cut -c1-64)" = "$WANT" ] || refuse "NVMe ESP initrd differs after the copy"
[ "$(sha256sum /mnt/xb/gbos-nvme/initrd.img | cut -c1-64)" = "$WANT" ] || refuse "stick initrd differs after the copy"
umount /mnt/nesp /mnt/xb
trap - EXIT
EOF
EXTRA=""

log "6. default boot (no menu keys)"
stop_watchers
python3 "$HERE/kano-ser.py" ec apshutdown 2 > /dev/null || die "EC apshutdown failed"
sleep 5; : > "$UART"
python3 "$HERE/kano-ser.py" ec powerbtn 2 > /dev/null || die "EC powerbtn failed"
python3 "$HERE/kano-gbos-watch-nvme.py" "$UART" 420 "$R/frames-nvme-$N" > "$OUT/watch.txt" 2>&1 &
WP=$!
up=0; for _ in $(seq 1 30); do kill -0 $WP 2> /dev/null || break; if adb_kano; then up=1; break; fi; sleep 15; done
booted=0
if [ $up -eq 1 ]; then
  for _ in $(seq 1 30); do [ "$(A shell getprop sys.boot_completed 2> /dev/null | tr -d '\r')" = 1 ] && { booted=1; break; }; sleep 5; done
fi
if [ $up -eq 1 ]; then
  log "boot: $(A shell 'ls -l /dev/block/by-name/userdata; getprop sys.boot_completed' 2>&1 | tr '\n' ' ')"
  log "excluded input devices: $(A shell dumpsys input 2>&1 | grep -m1 ExcludedDeviceNames)"
else
  log "no KANO Android at $KANO_ADB_ADDR after the update boot; nothing sent to it"
fi
if [ $booted -eq 1 ]; then
  sleep 30
  log "power button test: before $(A shell dumpsys power 2>&1 | grep -m1 mWakefulness=)"
  python3 "$HERE/kano-ser.py" ec powerbtn 2 > /dev/null || die "EC powerbtn failed"; sleep 4
  log "after 1st press: $(A shell dumpsys power 2>&1 | grep -m1 mWakefulness=)"
  python3 "$HERE/kano-ser.py" ec powerbtn 2 > /dev/null || die "EC powerbtn failed"; sleep 4
  log "after 2nd press: $(A shell dumpsys power 2>&1 | grep -m1 mWakefulness=)"
fi
wrc=0; wait $WP || wrc=$?
log "watch: $(tail -n 1 "$OUT/watch.txt")"
[ $wrc -eq 0 ] || die "watcher exit $wrc (2/3 danger + power cut, 4 unobservable, 6 power cut not confirmed); see $OUT/watch.txt"
[ $booted -eq 1 ] || die "Android at $KANO_ADB_ADDR never reported sys.boot_completed=1"
log "done"
