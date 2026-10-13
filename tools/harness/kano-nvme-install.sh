#!/usr/bin/env bash
# Install Googlebook (the copy on the stick) onto the KANO's internal NVMe, replacing what is there.
# Runs on the harness host; the KANO must be in the "KANO maintenance NVMe install" entry. Requires --go (the user
# approved). KANO_NVME_SRC is the build output dir (initrd.img, lapis*.conf) of the image on the stick: it is checked
# locally and against the stick's ESP (same initrd and entries, so the same vbmeta digest) before anything is
# written. Copies sectors 0..37244879 of the stick (everything up to ota_recovery_b; excludes XBOOTLDR/kano-maint),
# verifies, then: backup GPT to the disk end, drop the maint partitions, move userdata to the free tail and grow it,
# randomize all GUIDs, install an NVMe initrd (boot_part_uuid = NVMe boot_a) and nvme-enabled entries on the NVMe ESP,
# and add an "internal NVMe" entry on the stick's XBOOTLDR for the harness.
#
# Safety
# - Every remote step is ONE `bash -s` script with `set -euo pipefail` that starts with the target guards (NVMe
#   TRAN, PCI ID, serial; / on the Kingston stick; nothing on the NVMe mounted, swapped or held), so the guards and
#   the destructive commands after them run in the same invocation: a reboot in between cannot slip through. A guard
#   query that fails (unreadable /proc/self/mounts, /proc/swaps or holders) refuses; it never counts as "idle".
# - Every local input (initrd bootconfig, the three entries) is copied into this run's dir and checked before the
#   first remote call; the NVMe initrd and entries are made from those copies.
# - Each step's output goes to a file of THIS run (a fresh run dir under $KANO_RUNS/nvme-install/). A step passes
#   only if ssh exits 0 and that file holds the step's marker line (echoed as the script's last command, under
#   set -e); any failure stops before the next step.
#   Nothing is ever decided from an older run's output.
# - Before the discard, the NVMe's first and last MiB (primary and backup GPT) are copied to the run dir and must
#   match a sha256 computed on the KANO. Note that this is not a backup of the data on the NVMe.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
. "$HERE/kano-env.sh"
[ "${1:-}" = "--go" ] || { echo "refusing without --go"; exit 2; }
kano_require KANO_IP KANO_SSH_KEY KANO_NVME_SERIAL KANO_STICK_SERIAL KANO_NVME_PCI KANO_NVME_SRC KANO_RUNS
for v in "$KANO_NVME_SERIAL" "$KANO_STICK_SERIAL"; do
  [[ $v =~ ^[A-Za-z0-9]+$ ]] || { echo "refusing: odd serial '$v' in kano-env.sh"; exit 2; }
done
[[ $KANO_NVME_PCI =~ ^0x[0-9a-f]{4}:0x[0-9a-f]{4}$ ]] || { echo "refusing: odd KANO_NVME_PCI '$KANO_NVME_PCI'"; exit 2; }

SRC=$KANO_NVME_SRC
BASE=$KANO_RUNS/nvme-install
mkdir -p "$BASE"
RUN=$BASE/run-$(date +%Y%m%d-%H%M%S)-$$
mkdir "$RUN"
trap 'rc=$?; [ $rc -eq 0 ] || echo "kano-nvme-install: stopped (exit $rc); this run: $RUN" >&2' EXIT
log() { echo "[$(date +%T)] $*" | tee -a "$RUN/run.log"; }
die() { log "ABORT: $*"; exit 1; }
sha256() { if command -v sha256sum > /dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -c1-64; }
K() { "$HERE/kano-ssh.sh" "$@" < /dev/null; }
END_SECT=37244880                   # first sector of the stick's XBOOTLDR = end of the Googlebook region
BYTES=$((END_SECT * 512))

# Top of every remote script: strict mode, constants, guard functions.
prelude() {
  printf 'set -euo pipefail\nNVME_SERIAL=%s\nNVME_PCI=%s\nSTICK_SERIAL=%s\nBYTES=%s\n' \
    "$KANO_NVME_SERIAL" "$KANO_NVME_PCI" "$KANO_STICK_SERIAL" "$BYTES"
  cat <<'EOF'
D=/dev/nvme0n1; S=/dev/sda
refuse() { echo "REFUSE: $*" >&2; exit 1; }
# the right disks: the internal Samsung as target, the maintenance OS running from the Kingston stick as source
guard_id() {
  grep -q "initcall_blacklist=mei_me_driver_init " /proc/cmdline || refuse "not the NVMe install entry"
  [ -n "$NVME_SERIAL" ] && [ -n "$STICK_SERIAL" ] && [ -n "$NVME_PCI" ] || refuse "serials / PCI ID not configured"
  [ "$(lsblk -dno TRAN $D)" = nvme ] || refuse "nvme0n1 is not an NVMe-attached disk"
  [ "$(cat /sys/class/nvme/nvme0/device/vendor):$(cat /sys/class/nvme/nvme0/device/device)" = "$NVME_PCI" ] || refuse "nvme0 is not the internal Samsung ($NVME_PCI)"
  [ "$(lsblk -dno SERIAL $D | tr -d ' ')" = "$NVME_SERIAL" ] || refuse "nvme0n1 serial is not $NVME_SERIAL"
  [ "$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")" = sda ] || refuse "maintenance root not on sda"
  [ "$(lsblk -dno TRAN $S)" = usb ] && [ "$(lsblk -dno SERIAL $S | tr -d ' ')" = "$STICK_SERIAL" ] || refuse "sda is not the Kingston stick"
  [ "$(lsblk -no PARTLABEL /dev/sda32)" = XBOOTLDR ] && [ "$(blockdev --getsize64 /dev/sda32)" -gt 0 ] || refuse "stick layout changed"
  [ "$(cat /sys/block/sda/sda32/start)" = 37244880 ] || refuse "XBOOTLDR start moved"
}
# none <pattern> <file>: true if the file was read and no line matches; an unreadable file refuses (grep status 2)
none() { local rc=0; grep -q -- "$1" "$2" || rc=$?; [ "$rc" -le 1 ] || refuse "cannot read $2"; [ "$rc" -eq 1 ]; }
# the target is idle: nothing on the NVMe mounted, used as swap or held (device mapper, md, ...)
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
# line exists only if every command before it succeeded; stderr may arrive after it, so it is matched as a whole line); output in $RUN/<name>.out
remote() {
  local name=$1 marker=$2 rc=0
  { prelude; cat; printf 'echo %s\n' "$marker"; } > "$RUN/$name.sh"
  "$HERE/kano-ssh.sh" "bash -s" < "$RUN/$name.sh" > "$RUN/$name.out" 2>&1 || rc=$?
  sed 's/^/    /' "$RUN/$name.out" | tee -a "$RUN/run.log"
  [ "$rc" -eq 0 ] || die "$name: remote exit status $rc"
  grep -qx "$marker" "$RUN/$name.out" || die "$name: '$marker' missing from this run's output"
}
# remote_bin <name> < body: like remote, but stdout is data ($RUN/<name>.bin) and stderr goes to $RUN/<name>.err
remote_bin() {
  local name=$1 rc=0
  { prelude; cat; } > "$RUN/$name.sh"
  "$HERE/kano-ssh.sh" "bash -s" < "$RUN/$name.sh" > "$RUN/$name.bin" 2> "$RUN/$name.err" || rc=$?
  sed 's/^/    /' "$RUN/$name.err" | tee -a "$RUN/run.log"
  [ "$rc" -eq 0 ] || die "$name: remote exit status $rc"
}

# initrd_nvme <src> <dst> <guid>: <src> with androidboot.boot_part_uuid=<guid> in its bootconfig; <dst> "-" only
# checks <src> (bootconfig trailer + checksum, exactly one boot_part_uuid and one vbmeta.digest line)
initrd_nvme() {
  python3 - "$@" <<'PY'
import struct, sys
src, dst, guid = sys.argv[1], sys.argv[2], sys.argv[3]
d = open(src, "rb").read(); magic = b"#BOOTCONFIG\n"
if not d.endswith(magic): sys.exit("no bootconfig in " + src)
end = len(d) - len(magic); size, csum = struct.unpack_from("<II", d, end - 8); start = end - 8 - size
bc = d[start:start + size]
if start < 0 or (sum(bc) & 0xFFFFFFFF) != csum: sys.exit("bootconfig checksum mismatch")
lines = bc.decode().split("\n")
for key in ("androidboot.boot_part_uuid=", "androidboot.vbmeta.digest="):
    n = sum(1 for l in lines if l.startswith(key))
    if n != 1: sys.exit(f"{n} {key} lines in {src}")
if dst == "-": sys.exit(0)
lines = ["androidboot.boot_part_uuid=" + guid if l.startswith("androidboot.boot_part_uuid=") else l for l in lines]
raw = "\n".join(lines).encode()
open(dst, "wb").write(d[:start] + raw + struct.pack("<II", len(raw), sum(raw) & 0xFFFFFFFF) + magic)
print("initrd-nvme", len(d[:start] + raw) + 8 + len(magic), "boot_part_uuid", guid)
PY
}

log "run dir $RUN"
log "0. local inputs from $SRC (KANO_NVME_SRC), copied and checked before anything is sent"
[ -d "$SRC" ] || die "no source directory $SRC (KANO_NVME_SRC)"
# boot entries: quiet + serial-debug; builds made before the plain entry was dropped also have lapis.conf
ENTRIES=""
for f in lapis.conf lapis-debug.conf lapis-quiet.conf; do [ -e "$SRC/$f" ] && ENTRIES="$ENTRIES $f"; done
ENTRIES=${ENTRIES# }
case "$ENTRIES" in
  "lapis-debug.conf lapis-quiet.conf" | "lapis.conf lapis-debug.conf lapis-quiet.conf") ;;
  *) die "entries in $SRC: '$ENTRIES'; expected lapis-debug.conf and lapis-quiet.conf (plus lapis.conf in older builds)" ;;
esac
for f in initrd.img $ENTRIES; do
  cp "$SRC/$f" "$RUN/src-$f" || die "cannot copy $SRC/$f"
done
initrd_nvme "$RUN/src-initrd.img" - - || die "initrd $SRC/initrd.img: bootconfig unusable"
for f in $ENTRIES; do
  [ "$(grep -c "initcall_blacklist=nvme_init,mei_me_driver_init " "$RUN/src-$f")" = 1 ] || die "entry $f: no single initcall_blacklist=nvme_init,mei_me_driver_init"
  grep -qE '^options (.* )?module_blacklist=[^ ]' "$RUN/src-$f" && grep -qE '^options .* efi=noruntime( |$)' "$RUN/src-$f" ||
    die "entry $f: module_blacklist= or efi=noruntime missing"
  sed -e 's/initcall_blacklist=nvme_init,mei_me_driver_init/initcall_blacklist=mei_me_driver_init/' "$RUN/src-$f" > "$RUN/$f" || die "entry $f"
  grep -q "initcall_blacklist=mei_me_driver_init " "$RUN/$f" || die "entry $f"
done
sed -e 's/^title .*/title Googlebook KANO internal NVMe serial-debug/; s|^linux .*|linux /gbos-nvme/kernel.efi|; s|^initrd .*|initrd /gbos-nvme/initrd.img|' \
  "$RUN/lapis-debug.conf" > "$RUN/gbos-nvme.conf" || die "gbos-nvme.conf"

log "1. guards"
remote 01-guards GUARDS_OK <<'EOF'
grep -q "initcall_blacklist=mei_me_driver_init " /proc/cmdline || refuse "not the NVMe install entry"
modprobe --ignore-install nvme_core; modprobe --ignore-install nvme; sleep 2
guard
EOF

log "1b. the source must be the image on the stick: same ESP initrd and entries (read-only mount)"
remote 01-source SOURCE_OK <<'EOF'
guard
mkdir -p /mnt/sesp
mount -o ro /dev/sda12 /mnt/sesp
trap 'umount /mnt/sesp 2>/dev/null || true' EXIT
for f in /mnt/sesp/initrd.img /mnt/sesp/loader/entries/*.conf; do
  sum=$(sha256sum "$f" | cut -c1-64)
  echo "SUM $sum ${f##*/}"
done
umount /mnt/sesp
trap - EXIT
EOF
[ "$(sed -n 's/^SUM [0-9a-f]\{64\} \(.*\)$/\1/p' "$RUN/01-source.out" | LC_ALL=C sort | tr '\n' ' ')" = \
  "$(printf '%s\n' initrd.img $ENTRIES | LC_ALL=C sort | tr '\n' ' ')" ] ||
  die "the stick's ESP does not hold the same boot entries as $SRC ($ENTRIES)"
for f in initrd.img $ENTRIES; do
  [ "$(sed -n "s/^SUM \([0-9a-f]\{64\}\) $f\$/\1/p" "$RUN/01-source.out")" = "$(sha256 "$RUN/src-$f")" ] ||
    die "$SRC/$f is not what the stick's ESP holds; KANO_NVME_SRC must be the build of the image on the stick"
done

log "2. save the NVMe's first and last MiB (primary + backup GPT) to $RUN"
remote_bin 02-head <<'EOF'
guard
dd if=$D bs=1048576 count=1 status=none
EOF
remote_bin 02-tail <<'EOF'
guard
SIZE=$(blockdev --getsize64 $D)
dd if=$D bs=1048576 count=1 skip=$((SIZE - 1048576)) iflag=skip_bytes status=none
EOF
remote 02-sums BACKUP_SUMS_OK <<'EOF'
guard
SIZE=$(blockdev --getsize64 $D)
HEAD=$(dd if=$D bs=1048576 count=1 status=none | sha256sum | cut -c1-64)
TAIL=$(dd if=$D bs=1048576 count=1 skip=$((SIZE - 1048576)) iflag=skip_bytes status=none | sha256sum | cut -c1-64)
echo "SIZE=$SIZE"; echo "HEAD=$HEAD"; echo "TAIL=$TAIL"
EOF
for p in head:HEAD tail:TAIL; do
  f=$RUN/02-${p%%:*}.bin
  n=$(wc -c < "$f" | tr -d ' ')
  [ "$n" = 1048576 ] || die "GPT backup ${p%%:*}: $n bytes, expected 1048576"
  want=$(sed -n "s/^${p#*:}=\([0-9a-f]\{64\}\)$/\1/p" "$RUN/02-sums.out")
  [ -n "$want" ] && [ "$(sha256 "$f")" = "$want" ] || die "GPT backup ${p%%:*}: sha256 differs from the NVMe's"
done
log "GPT backup verified: $RUN/02-head.bin, $RUN/02-tail.bin"

log "3. copy the stick's Googlebook region to the NVMe ($BYTES bytes)"
remote 03-copy DD_OK <<'EOF'
guard
blkdiscard -f "$D"
dd if="$S" of="$D" bs=16M count="$BYTES" iflag=fullblock,count_bytes oflag=direct conv=fsync status=none
EOF
remote 04-verify CMP_OK <<'EOF'
guard
cmp -n "$BYTES" "$S" "$D"
EOF

log "5. GPT: backup header to the end, drop maint partitions, userdata to the tail, randomize GUIDs"
remote 05-gpt GPT_OK <<'EOF'
guard
UD_TYPE=$(sgdisk -i 1 "$S" | sed -n 's/^Partition GUID code: \([0-9A-F-]*\).*/\1/p')
[ -n "$UD_TYPE" ] || refuse "cannot read the userdata type GUID from the stick"
[ "$(sgdisk -i 1 "$D" | sed -n "s/^Partition name: '\(.*\)'/\1/p")" = userdata ] || refuse "p1 is not userdata"
OLD_START=$(sgdisk -i 1 "$D" | sed -n 's/^First sector: \([0-9]*\).*/\1/p')
OLD_SECT=$(sgdisk -i 1 "$D" | sed -n 's/^Partition size: \([0-9]*\) sectors.*/\1/p')
[ -n "$OLD_START" ] && [ -n "$OLD_SECT" ] || refuse "cannot read the bounds of p1"
sgdisk -e -d 32 -d 33 "$D"
sgdisk -d 1 -n 1:37246976:0 -c 1:userdata -t "1:$UD_TYPE" "$D"
sgdisk -G "$D"
partx -u "$D" 2>/dev/null || blockdev --rereadpt "$D"
sleep 2
NEW_START=$(sgdisk -i 1 "$D" | sed -n 's/^First sector: \([0-9]*\).*/\1/p')
[ -n "$NEW_START" ] || refuse "cannot read the new start of p1"
dd if="$D" of="$D" bs=1M skip=$((OLD_START * 512)) seek=$((NEW_START * 512)) count=$((OLD_SECT * 512)) iflag=skip_bytes,count_bytes,fullblock oflag=seek_bytes,direct conv=notrunc,fsync status=none
cmp -n $((OLD_SECT * 512)) <(dd if="$D" bs=1M skip=$((OLD_START * 512)) count=$((OLD_SECT * 512)) iflag=skip_bytes,count_bytes status=none) /dev/nvme0n1p1
echo UD_COPY_OK
# an rw mount replays the f2fs checkpoint, which resize.f2fs needs
mkdir -p /mnt/ud
mount -t f2fs /dev/nvme0n1p1 /mnt/ud
umount /mnt/ud
resize.f2fs /dev/nvme0n1p1 | tail -n 2
fsck.f2fs /dev/nvme0n1p1 | tail -n 2
sgdisk -v "$D" | tail -n 1
sgdisk -A 15:show "$D"
P13=$(sgdisk -i 13 "$D")
[ "$(printf '%s\n' "$P13" | sed -n "s/^Partition name: '\(.*\)'/\1/p")" = boot_a ] || refuse "p13 is not boot_a"
echo "BOOT_A_GUID=$(printf '%s\n' "$P13" | sed -n 's/^Partition unique GUID: \([0-9A-F-]*\)/\1/p')"
lsblk -o NAME,SIZE,PARTLABEL "$D" | sed -n 1,6p
EOF
BG=$(sed -n 's/^BOOT_A_GUID=\([0-9A-F-]*\)$/\1/p' "$RUN/05-gpt.out" | tail -n 1 | tr 'A-F' 'a-f')
[ ${#BG} -eq 36 ] || die "boot_a guid '$BG'"
log "NVMe boot_a GUID $BG"

log "6. NVMe initrd (the entries were made in step 0)"
initrd_nvme "$RUN/src-initrd.img" "$RUN/initrd-nvme.img" "$BG" || die "initrd-nvme"

log "7. install on the NVMe ESP and the stick XBOOTLDR"
remote 07-mount MOUNT_OK <<'EOF'
guard
mkdir -p /mnt/nesp /mnt/xb
mount /dev/nvme0n1p12 /mnt/nesp
mount /dev/sda32 /mnt/xb
mkdir -p /mnt/xb/gbos-nvme
EOF
COPIES=(initrd-nvme.img:/mnt/nesp/initrd.img)
for f in $ENTRIES; do COPIES+=("$f:/mnt/nesp/loader/entries/$f"); done
COPIES+=(initrd-nvme.img:/mnt/xb/gbos-nvme/initrd.img gbos-nvme.conf:/mnt/xb/loader/entries/gbos-nvme.conf)
for f in "${COPIES[@]}"; do
  s=${f%%:*}; t=${f#*:}
  "$HERE/kano-ssh.sh" "cat > $t" < "$RUN/$s" || die "copy $s -> $t"
  [ "$(K "sha256sum $t" | cut -c1-64)" = "$(sha256 "$RUN/$s")" ] || die "copy $s -> $t: sha256 differs"
done
remote 08-esp ESP_OK <<'EOF'
guard_id
cp /mnt/nesp/kernel.efi /mnt/xb/gbos-nvme/kernel.efi
cmp /mnt/nesp/kernel.efi /mnt/xb/gbos-nvme/kernel.efi
sync
umount /mnt/nesp /mnt/xb
EOF
log "installed; next: boot 'Googlebook KANO internal NVMe' with the NVMe watcher"
