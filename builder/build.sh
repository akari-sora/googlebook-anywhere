#!/usr/bin/env bash
# Build a KANO Googlebook USB image from a lapis base image (base/build-base.sh output).
# Required env: GBOS_BASE GBOS_INITRD GBOS_OUT GBOS_OTATOOLS GBOS_AVB_KEY GBOS_INPUTS (builder/fetch-inputs.sh <dir>;
# GBOS_FIRMWARE and GBOS_AUDIO_SRC default to <dir>/firmware and <dir>/audio-src, a linux-firmware tree with the
# pinned versions such as Arch's /usr/lib/firmware also works as GBOS_FIRMWARE).
# Optional: GBOS_PROFILE (profiles/kano), GBOS_ADB_PUBKEY (opt-in network adb, an adbkey.pub), GBOS_DEFAULT_ENTRY
# (lapis-quiet.conf), GBOS_LOADER_EDITOR (no), GBOS_IMAGE_NAME (googlebook-kano.img), GBOS_QEMU, GBOS_QEMU_LIB,
# GBOS_OVMF_CODE, GBOS_OVMF_VARS, GBOS_QEMU_SECONDS (200), GBOS_ALLOW_FIRMWARE_DRIFT=1, GBOS_SKIP_SPACE_CHECK=1,
# GBOS_SKIP_SELFTEST=1 (no KVM, e.g. Docker on macOS: the image is built but NOT booted in QEMU).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { echo "build.sh: $*" >&2; exit 2; }
export GBOS_PROFILE="${GBOS_PROFILE:-$HERE/../profiles/kano}"
: "${GBOS_BASE:?}" "${GBOS_INITRD:?}" "${GBOS_OUT:?}" "${GBOS_OTATOOLS:?}" "${GBOS_AVB_KEY:?}"
[ -n "${GBOS_INPUTS:-}" ] || { [ -n "${GBOS_FIRMWARE:-}" ] && [ -n "${GBOS_AUDIO_SRC:-}" ]; } \
    || die "set GBOS_INPUTS (builder/fetch-inputs.sh <dir>), or both GBOS_FIRMWARE and GBOS_AUDIO_SRC"
export GBOS_FIRMWARE="${GBOS_FIRMWARE:-$GBOS_INPUTS/firmware}" GBOS_AUDIO_SRC="${GBOS_AUDIO_SRC:-$GBOS_INPUTS/audio-src}"
export GBOS_IMAGE_NAME="${GBOS_IMAGE_NAME:-googlebook-kano.img}"
export GBOS_QEMU="${GBOS_QEMU:-/usr/bin/qemu-system-x86_64}"
export GBOS_OVMF_CODE="${GBOS_OVMF_CODE:-/usr/share/edk2/x64/OVMF_CODE.4m.fd}"
export GBOS_OVMF_VARS="${GBOS_OVMF_VARS:-/usr/share/edk2/x64/OVMF_VARS.4m.fd}"

echo "== preflight"
[[ "$GBOS_IMAGE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.img$ ]] || die "GBOS_IMAGE_NAME must be a plain file name ending in .img"
if [ -e "$GBOS_OUT" ] && [ -n "$(ls -A "$GBOS_OUT")" ]; then die "GBOS_OUT ($GBOS_OUT) must be a new or empty directory"; fi
for f in "$GBOS_BASE" "$GBOS_INITRD" "$GBOS_AVB_KEY"; do [ -f "$f" ] || die "missing file: $f"; done
if [ "${GBOS_SKIP_SELFTEST:-}" != 1 ]; then
    for f in "$GBOS_OVMF_CODE" "$GBOS_OVMF_VARS"; do [ -f "$f" ] || die "missing file: $f"; done
fi
[ -d "$GBOS_FIRMWARE" ] || die "GBOS_FIRMWARE is not a directory: $GBOS_FIRMWARE"
[ -f "$GBOS_PROFILE/audio-src.list" ] || die "GBOS_PROFILE has no audio-src.list: $GBOS_PROFILE"
[ -d "$GBOS_AUDIO_SRC" ] || die "GBOS_AUDIO_SRC is not a directory: $GBOS_AUDIO_SRC"
for c in python3 mcopy mtype mdir zstd sha256sum cp readlink cmp; do command -v "$c" >/dev/null || die "missing command: $c"; done
if [ "${GBOS_SKIP_SELFTEST:-}" = 1 ]; then
    echo "WARNING: GBOS_SKIP_SELFTEST=1: the image will NOT be booted in QEMU before you write it"
else
    [ -x "$GBOS_QEMU" ] || die "QEMU not executable: $GBOS_QEMU (set GBOS_QEMU)"
    [ -r /dev/kvm ] && [ -w /dev/kvm ] || die "/dev/kvm is not usable by $(id -un) (the QEMU self-test needs KVM; without it set GBOS_SKIP_SELFTEST=1)"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "python3 >= 3.8 needed"
for b in avbtool mkfs.erofs fsck.erofs; do [ -x "$GBOS_OTATOOLS/bin/$b" ] || die "otatools lacks bin/$b"; done
LD_LIBRARY_PATH="$GBOS_OTATOOLS/lib64" "$GBOS_OTATOOLS/bin/avbtool" update_partition_descriptor --help >/dev/null 2>&1 \
    || die "this avbtool has no update_partition_descriptor (use the otatools from base/build-base.sh, AOSP build 16373615)"
head -c 64 "$GBOS_AVB_KEY" | grep -q "PRIVATE KEY" || die "GBOS_AVB_KEY is not a PEM private key"
mkdir -p "$GBOS_OUT"
if [ "${GBOS_SKIP_SPACE_CHECK:-}" != 1 ]; then
    free=$(df -Pk "$GBOS_OUT" | awk 'NR==2 {print $4}')
    [ "$free" -ge $((45 * 1024 * 1024)) ] || die "less than 45 GB free under $GBOS_OUT (two 19 GB images plus work files; GBOS_SKIP_SPACE_CHECK=1 on a reflink filesystem)"
fi
avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
[ "${GBOS_SKIP_SELFTEST:-}" = 1 ] || [ "$avail" -ge $((9 * 1024 * 1024)) ] || echo "warning: under 9 GB of free memory; the QEMU self-test gives the guest 8 GB"

echo "== pci-stub.ko from the base image vendor_dlkm"
X="$GBOS_OUT/extract"; mkdir -p "$X"
python3 "$HERE/lib/lp-dump.py" "$GBOS_BASE" "$X" vendor_dlkm_a
LD_LIBRARY_PATH="$GBOS_OTATOOLS/lib64" "$GBOS_OTATOOLS/bin/fsck.erofs" --extract="$X/vendor_dlkm" --overwrite "$X/vendor_dlkm_a.img" >/dev/null
export GBOS_PCI_STUB_KO="$X/vendor_dlkm/lib/modules/pci-stub.ko"
[ -s "$GBOS_PCI_STUB_KO" ] || { echo "pci-stub.ko missing in the base image"; exit 1; }

echo "== stage 1: vendor (log: $GBOS_OUT/vendor.log)"
if ! bash "$HERE/vendor.sh" > "$GBOS_OUT/vendor.log" 2>&1; then
    tail -n 30 "$GBOS_OUT/vendor.log"; echo "stage 1 failed"; exit 1
fi
grep -q "^vendor tree ok$" "$GBOS_OUT/vendor.log" || { tail -n 30 "$GBOS_OUT/vendor.log"; echo "stage 1 ended without 'vendor tree ok'"; exit 1; }

echo "== stage 2: image + QEMU self-tests (about 8 minutes; log: $GBOS_OUT/image.log)"
if ! python3 "$HERE/image.py" > "$GBOS_OUT/image.log" 2>&1; then
    grep -E '^(PASS|FAIL) ' "$GBOS_OUT/image.log" || true
    tail -n 20 "$GBOS_OUT/image.log"; echo "stage 2 failed"; exit 1
fi
grep -E '^(PASS|FAIL|SKIP) |sha256 [0-9a-f]{64}$|^IMAGE OK' "$GBOS_OUT/image.log"
