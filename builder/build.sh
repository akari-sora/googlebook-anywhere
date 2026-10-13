#!/usr/bin/env bash
# Build a Googlebook USB image for one device profile (default profiles/kano) from a lapis base image (base/build-base.sh output).
# Required env: GBOS_BASE GBOS_INITRD GBOS_OUT GBOS_OTATOOLS GBOS_AVB_KEY GBOS_INPUTS (builder/fetch-inputs.sh <dir>;
# GBOS_FIRMWARE and GBOS_AUDIO_SRC default to <dir>/firmware and <dir>/audio-src, a linux-firmware tree with the
# pinned versions such as Arch's /usr/lib/firmware also works as GBOS_FIRMWARE).
# Optional: GBOS_PROFILE (profiles/kano; every model-specific value is in <profile>/profile.toml), GBOS_ADB_PUBKEY (opt-in
# network adb, an adbkey.pub), GBOS_DEFAULT_ENTRY (lapis-quiet.conf), GBOS_LOADER_EDITOR (no), GBOS_IMAGE_NAME (the
# profile's model.image_name, googlebook-kano.img for KANO), GBOS_QEMU, GBOS_QEMU_LIB,
# GBOS_OVMF_CODE, GBOS_OVMF_VARS, GBOS_QEMU_SECONDS (200), GBOS_ALLOW_FIRMWARE_DRIFT=1, GBOS_SKIP_SPACE_CHECK=1,
# GBOS_SKIP_SELFTEST=1 (no KVM, e.g. Docker on macOS: the image is built but NOT booted in QEMU).
# GBOS_KMOD_AC=<ac.ko from builder/kmod/build-ac.sh>: required when the profile lists kmods = ["ac"] (KANO does: the ACPI
# AC adapter driver, so Android shows the charger); install/install.sh builds it.
# GBOS_KMOD_WAKEFILTER=<gbos_wakefilter.ko from builder/kmod/build-gbos_wakefilter.sh>: required when the profile lists
# kmods "gbos_wakefilter" (KANO does: dark resumes stay dark); install/install.sh builds it with ac.ko.
# A profile with display.stack = gop-swiftshader (profiles/generic-x86) needs vulkan.pastel.so (GBOS_GENERIC_PASTEL,
# default <GBOS_INPUTS>/generic/vulkan.pastel.so from builder/fetch-generic-inputs.sh) and builds gbos_efifb.ko from the
# base image's own kernel headers with the pinned Android clang (builder/kmod/build-gbos_efifb.sh; downloaded once to
# GBOS_KMOD_TOOLCHAIN, default <GBOS_INPUTS>/kmod-toolchain; or GBOS_GENERIC_KO=<that pinned gbos_efifb.ko>).
# GBOS_CAMERA_OVERLAY=<the out/vendor directory of builder/camera/build-libcamera.sh>: the built-in camera (KANO: IPU6 +
# OV2740, upstream libcamera HAL); opt-in until proven on the hardware, required when the profile says camera.libcamera = true.
# GBOS_EC: opt-in ChromeOS EC features, a comma list of sensors (accelerometers -> auto-rotate), typec (USB-C
# USB3/DisplayPort), buttons (side volume keys). Empty (default): the EC stays unreachable. See docs/SAFETY.md.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { echo "build.sh: $*" >&2; exit 2; }
export GBOS_PROFILE="${GBOS_PROFILE:-$HERE/../profiles/kano}"
: "${GBOS_BASE:?}" "${GBOS_INITRD:?}" "${GBOS_OUT:?}" "${GBOS_OTATOOLS:?}" "${GBOS_AVB_KEY:?}"
[ -n "${GBOS_INPUTS:-}" ] || { [ -n "${GBOS_FIRMWARE:-}" ] && [ -n "${GBOS_AUDIO_SRC:-}" ]; } \
    || die "set GBOS_INPUTS (builder/fetch-inputs.sh <dir>), or both GBOS_FIRMWARE and GBOS_AUDIO_SRC"
export GBOS_FIRMWARE="${GBOS_FIRMWARE:-$GBOS_INPUTS/firmware}" GBOS_AUDIO_SRC="${GBOS_AUDIO_SRC:-$GBOS_INPUTS/audio-src}"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 >= 3.11 needed (tomllib, for the device profile)"
python3 "$HERE/lib/devprofile.py" check "$GBOS_PROFILE" || die "device profile $GBOS_PROFILE is not valid"
export GBOS_IMAGE_NAME="${GBOS_IMAGE_NAME:-$(python3 "$HERE/lib/devprofile.py" get "$GBOS_PROFILE" model.image_name)}"
export GBOS_QEMU="${GBOS_QEMU:-/usr/bin/qemu-system-x86_64}"
export GBOS_OVMF_CODE="${GBOS_OVMF_CODE:-/usr/share/edk2/x64/OVMF_CODE.4m.fd}"
export GBOS_OVMF_VARS="${GBOS_OVMF_VARS:-/usr/share/edk2/x64/OVMF_VARS.4m.fd}"

echo "== preflight"
[[ "$GBOS_IMAGE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.img$ ]] || die "GBOS_IMAGE_NAME must be a plain file name ending in .img"
[[ "${GBOS_EC:-}" =~ ^((sensors|typec|buttons)(,(sensors|typec|buttons))*)?$ ]] \
    || die "GBOS_EC must be empty or a comma list of sensors, typec, buttons"
[ -z "${GBOS_EC:-}" ] || echo "NOTE: GBOS_EC=$GBOS_EC: this image lets audited EC drivers talk to the EC (docs/SAFETY.md)"
if [ -e "$GBOS_OUT" ] && [ -n "$(ls -A "$GBOS_OUT")" ]; then die "GBOS_OUT ($GBOS_OUT) must be a new or empty directory"; fi
for f in "$GBOS_BASE" "$GBOS_INITRD" "$GBOS_AVB_KEY"; do [ -f "$f" ] || die "missing file: $f"; done
if [ "${GBOS_SKIP_SELFTEST:-}" != 1 ]; then
    for f in "$GBOS_OVMF_CODE" "$GBOS_OVMF_VARS"; do [ -f "$f" ] || die "missing file: $f"; done
fi
[ -d "$GBOS_FIRMWARE" ] || die "GBOS_FIRMWARE is not a directory: $GBOS_FIRMWARE"
if python3 "$HERE/lib/devprofile.py" kmods "$GBOS_PROFILE" | grep -qx ac; then
    [ -n "${GBOS_KMOD_AC:-}" ] || die "the profile ships ac.ko: set GBOS_KMOD_AC (builder/kmod/build-ac.sh <work dir> <out dir> builds it)"
    [ -f "$GBOS_KMOD_AC" ] || die "GBOS_KMOD_AC is not a file: $GBOS_KMOD_AC"
fi
if python3 "$HERE/lib/devprofile.py" kmods "$GBOS_PROFILE" | grep -qx gbos_wakefilter; then
    [ -n "${GBOS_KMOD_WAKEFILTER:-}" ] || die "the profile ships gbos_wakefilter.ko: set GBOS_KMOD_WAKEFILTER (builder/kmod/build-gbos_wakefilter.sh <work dir of build-ac.sh> <out dir> builds it)"
    [ -f "$GBOS_KMOD_WAKEFILTER" ] || die "GBOS_KMOD_WAKEFILTER is not a file: $GBOS_KMOD_WAKEFILTER"
fi
if [ -n "${GBOS_CAMERA_OVERLAY:-}" ]; then
    [ -d "$GBOS_CAMERA_OVERLAY" ] && [ -f "$GBOS_CAMERA_OVERLAY/../SHA256SUMS" ] \
        || die "GBOS_CAMERA_OVERLAY must be the out/vendor directory of builder/camera/build-libcamera.sh (with ../SHA256SUMS): $GBOS_CAMERA_OVERLAY"
    export GBOS_CAMERA_OVERLAY
fi
STACK=$(python3 "$HERE/lib/devprofile.py" get "$GBOS_PROFILE" display.stack)
if [ "$STACK" = gop-swiftshader ]; then
    export GBOS_GENERIC_PASTEL="${GBOS_GENERIC_PASTEL:-${GBOS_INPUTS:-}/generic/vulkan.pastel.so}"
    [ -f "$GBOS_GENERIC_PASTEL" ] || die "this profile needs vulkan.pastel.so: run builder/fetch-generic-inputs.sh <inputs dir> or set GBOS_GENERIC_PASTEL"
    if [ -n "${GBOS_GENERIC_KO:-}" ]; then [ -f "$GBOS_GENERIC_KO" ] || die "GBOS_GENERIC_KO not found: $GBOS_GENERIC_KO"
    else for c in curl lz4 xz tar; do command -v "$c" >/dev/null || die "the gbos_efifb.ko build needs $c (or set GBOS_GENERIC_KO)"; done; fi
fi
AUDIO_LIST=$(python3 "$HERE/lib/devprofile.py" get "$GBOS_PROFILE" audio.sources)
[ -z "$AUDIO_LIST" ] || [ -f "$GBOS_PROFILE/$AUDIO_LIST" ] || die "GBOS_PROFILE has no $AUDIO_LIST: $GBOS_PROFILE"
[ -d "$GBOS_AUDIO_SRC" ] || die "GBOS_AUDIO_SRC is not a directory: $GBOS_AUDIO_SRC"
for c in python3 mcopy mtype mdir zstd sha256sum cp readlink cmp; do command -v "$c" >/dev/null || die "missing command: $c"; done
if [ "${GBOS_SKIP_SELFTEST:-}" = 1 ]; then
    echo "WARNING: GBOS_SKIP_SELFTEST=1: the image will NOT be booted in QEMU before you write it"
else
    [ -x "$GBOS_QEMU" ] || die "QEMU not executable: $GBOS_QEMU (set GBOS_QEMU)"
    [ -r /dev/kvm ] && [ -w /dev/kvm ] || die "/dev/kvm is not usable by $(id -un) (the QEMU self-test needs KVM; without it set GBOS_SKIP_SELFTEST=1)"
fi
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

if [ "$STACK" = gop-swiftshader ] && [ -z "${GBOS_GENERIC_KO:-}" ]; then
    echo "== gbos_efifb.ko against the base image's own kernel headers and export CRCs"
    python3 "$HERE/kmod/prepare.py" "$GBOS_BASE" "$GBOS_OTATOOLS" "$GBOS_OUT/kmod" || die "kernel headers/CRCs from the base image"
    bash "$HERE/kmod/build-gbos_efifb.sh" "$GBOS_OUT/kmod/kh" "$GBOS_OUT/kmod/symvers.txt" "$GBOS_OUT/kmod/gbos_efifb.ko" \
        "${GBOS_KMOD_TOOLCHAIN:-${GBOS_INPUTS:-$GBOS_OUT}/kmod-toolchain}" || die "gbos_efifb.ko build"
fi
[ "$STACK" != gop-swiftshader ] || export GBOS_GENERIC_KO="${GBOS_GENERIC_KO:-$GBOS_OUT/kmod/gbos_efifb.ko}"
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
