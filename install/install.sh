#!/usr/bin/env bash
# One command from Google's official lapis recovery image to a KANO USB image, as a normal user (no sudo):
#   1. download the official recovery zip (or use --zip) and check its SHA-256
#   2. base/build-base.sh: the intermediate base image (software KeyMint, signed with your own AVB key); it is only an
#      input for step 4 and does not boot on the KANO by itself
#   3. builder/fetch-inputs.sh: pinned linux-firmware / sof-bin files and the ChromeOS audio configuration
#   4. builder/build.sh: the KANO image plus its QEMU self-tests
# Then write it with: sudo install/write-usb.sh <image> /dev/disk/by-id/usb-...
#
# usage: install/install.sh --work DIR [--zip recovery.zip] [--key avb-rsa4096.pem] [--adb-pubkey ~/.android/adbkey.pub]
#                           [--skip-space-check] [--no-selftest]
# A finished base or image in DIR is reused only when everything that made it is the same: the files under base/,
# builder/ and the profile (also uncommitted ones), the keys, and the settings, including the GBOS_* variables that
# builder/build.sh reads (GBOS_DEFAULT_ENTRY, GBOS_LOADER_EDITOR, ...). Otherwise it stops and says what to delete.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZIP_URL=https://dl.google.com/device/recovery/lapis-user/16452207/recovery.zip
ZIP_SHA256=5bc64936c11d9eb268da03063a443b8fd0bbc0f84d266f9a014231653939f247
die() { echo "install.sh: $*" >&2; exit 2; }
log() { printf '\n[%s] == %s\n' "$(date +%H:%M:%S)" "$*"; }

WORK='' ZIP='' KEY='' ADB='' SKIP_SPACE=0 SELFTEST=1
while [ $# -gt 0 ]; do
    case "$1" in
        --work) [ $# -ge 2 ] || die "--work needs a directory"; WORK=$2; shift 2 ;;
        --zip|--key|--adb-pubkey)
            [ $# -ge 2 ] && [ -f "$2" ] || die "$1: not a file: ${2:-}"
            case "$1" in --zip) ZIP=$2 ;; --key) KEY=$2 ;; *) ADB=$2 ;; esac
            shift 2 ;;
        --skip-space-check) SKIP_SPACE=1; shift ;;
        --no-selftest) SELFTEST=0; shift ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) die "unknown argument $1 (see --help)" ;;
    esac
done
[ -n "$WORK" ] || die "--work DIR is required (about 80 GB of space)"
[ "$(id -u)" -ne 0 ] || die "run as a normal user; only the final USB write needs root"
[ "$(uname -s)/$(uname -m)" = Linux/x86_64 ] || die "needs Linux on x86_64 (KVM for the self-test)"
mkdir -p "$WORK"; WORK="$(cd "$WORK" && pwd)"

log "host check"
missing=()
for c in curl python3 lz4 mkfs.fat mmd mcopy mtype mdir openssl sha256sum truncate tar zstd cmp; do
    command -v "$c" >/dev/null || missing+=("$c")
done
QEMU=${GBOS_QEMU:-$(command -v qemu-system-x86_64 || true)}
[ "$SELFTEST" = 0 ] || [ -n "$QEMU" ] || missing+=(qemu-system-x86_64)
if [ ${#missing[@]} -gt 0 ]; then
    echo "missing: ${missing[*]}"
    echo "Arch:          sudo pacman -S --needed curl python lz4 dosfstools mtools openssl coreutils tar zstd diffutils qemu-base edk2-ovmf"
    echo "Debian/Ubuntu: sudo apt install curl python3 lz4 dosfstools mtools openssl coreutils tar zstd diffutils qemu-system-x86 ovmf"
    echo "Fedora:        sudo dnf install curl python3 lz4 dosfstools mtools openssl coreutils tar zstd diffutils qemu-system-x86 edk2-ovmf"
    exit 2
fi
if [ "$SELFTEST" = 1 ] && [ -z "${GBOS_OVMF_CODE:-}" ]; then
    for pair in /usr/share/edk2/x64/OVMF_CODE.4m.fd:/usr/share/edk2/x64/OVMF_VARS.4m.fd \
                /usr/share/OVMF/OVMF_CODE_4M.fd:/usr/share/OVMF/OVMF_VARS_4M.fd \
                /usr/share/edk2/ovmf/OVMF_CODE.fd:/usr/share/edk2/ovmf/OVMF_VARS.fd \
                /usr/share/OVMF/OVMF_CODE.fd:/usr/share/OVMF/OVMF_VARS.fd; do
        if [ -f "${pair%%:*}" ] && [ -f "${pair##*:}" ]; then GBOS_OVMF_CODE=${pair%%:*}; GBOS_OVMF_VARS=${pair##*:}; break; fi
    done
fi
[ "$SELFTEST" = 0 ] || [ -n "${GBOS_OVMF_CODE:-}" ] || die "OVMF (UEFI for QEMU) not found; install edk2-ovmf/ovmf or set GBOS_OVMF_CODE and GBOS_OVMF_VARS"
if [ "$SELFTEST" = 1 ]; then
    [ -r /dev/kvm ] && [ -w /dev/kvm ] || die "/dev/kvm is not usable by $(id -un) (add yourself to the kvm group; without KVM use --no-selftest)"
else
    echo "WARNING: --no-selftest: the image will NOT be booted in QEMU before you write it"
fi
if [ "$SKIP_SPACE" != 1 ]; then
    free=$(df -Pk "$WORK" | awk 'NR==2 {print $4}')
    [ "$free" -ge $((80 * 1024 * 1024)) ] || die "less than 80 GB free in $WORK (--skip-space-check if most of it is already built)"
fi
export GBOS_QEMU="$QEMU" GBOS_OVMF_CODE="${GBOS_OVMF_CODE:-}" GBOS_OVMF_VARS="${GBOS_OVMF_VARS:-}"
[ "$SELFTEST" = 0 ] || echo "QEMU $GBOS_QEMU, OVMF $GBOS_OVMF_CODE"

# content identity of source directories: names and contents of every file except Markdown documents, committed or not
tree_digest() {
    local d
    for d in "$@"; do
        [ -d "$d" ] || die "missing source directory $d"
        echo "dir $d"
        (cd "$d" && find . -type f ! -name '*.md' ! -name '*.pyc' ! -path '*/__pycache__/*' -print0 \
            | LC_ALL=C sort -z | xargs -0 -r sha256sum) || die "cannot read $d"
    done | sha256sum | cut -c1-64
}

log "1/4 official recovery zip (7.7 GB)"
if [ -z "$ZIP" ]; then
    ZIP="$WORK/downloads/lapis-user-16452207-recovery.zip"
    mkdir -p "$WORK/downloads"
    if [ ! -f "$ZIP.ok" ]; then
        curl -fL --retry 5 -C - -o "$ZIP" "$ZIP_URL"
    fi
fi
# the .ok marker remembers a verified hash for this exact file (size and modification time); anything else is re-hashed
stamp() { echo "$ZIP_SHA256 $(stat -c '%s %Y' "$ZIP")"; }
if [ ! -f "$ZIP.ok" ] || [ "$(cat "$ZIP.ok")" != "$(stamp)" ]; then
    echo "checking SHA-256"
    [ "$(sha256sum < "$ZIP" | cut -d' ' -f1)" = "$ZIP_SHA256" ] || die "$ZIP is not the expected lapis build 16452207 recovery zip"
    if [ -w "$(dirname "$ZIP")" ]; then stamp > "$ZIP.ok"; fi
fi

log "2/4 base image (base/build-base.sh, a few minutes)"
B="$WORK/base"
BASE_ID=$(tree_digest "$ROOT/base"); BASE_SOURCES="base-sources $BASE_ID"
# what the base files looked like when they last matched SHA256SUMS; anything else is hashed again
base_stamp() { (cd "$B" && sha256sum SHA256SUMS | cut -c1-64 && stat -c '%n %s %Y %i' kano-real.img initrd.img); }
if [ -f "$B/BUILD-DONE" ]; then
    [ -f "$B/install-sources" ] \
        || die "$B was built without a record of its sources (older install.sh); delete $B or use a new --work"
    [ "$(cat "$B/install-sources")" = "$BASE_SOURCES" ] \
        || die "$B was built from other base/ sources than this checkout; delete $B or use a new --work"
    [ "$(grep -cE '^[0-9a-f]{64}  (kano-real|initrd)\.img$' "$B/SHA256SUMS" 2>/dev/null)" = 2 ] \
        || die "$B/SHA256SUMS does not list kano-real.img and initrd.img; delete $B"
    if [ ! -f "$B/install-verified" ] || [ "$(cat "$B/install-verified")" != "$(base_stamp)" ]; then
        echo "checking $B against its SHA256SUMS (about a minute)"
        (cd "$B" && sha256sum --quiet -c SHA256SUMS) || die "$B does not match its SHA256SUMS; delete $B and run again"
        base_stamp > "$B/install-verified"
    fi
    echo "reusing $B"
else
    bash "$ROOT/base/build-base.sh" --zip "$ZIP" --out "$B" ${KEY:+--key "$KEY"}
    printf '%s\n' "$BASE_SOURCES" > "$B/install-sources"
    base_stamp > "$B/install-verified"   # build-base.sh hashed exactly these files a moment ago
fi
AVB_KEY=${KEY:-$WORK/base/keys/vbmeta.pem}
[ -f "$AVB_KEY" ] || die "AVB key not found: $AVB_KEY (pass the --key that built $WORK/base)"
# the base must have been signed with this key: compare the public key fingerprint recorded by build-base.sh
OT="$WORK/base/cache/otatools"; PUB=$(mktemp)
LD_LIBRARY_PATH="$OT/lib64" "$OT/bin/avbtool" extract_public_key --key "$AVB_KEY" --output "$PUB" || die "cannot read $AVB_KEY"
KEY_FP=$(sha1sum < "$PUB" | cut -c1-40); rm -f "$PUB"
grep -q "public key sha1 $KEY_FP" "$WORK/base/build-info.txt" \
    || die "$WORK/base was signed with a different AVB key than $AVB_KEY; use a new --work (or the key that built it)"

log "3/4 device firmware (linux-firmware 20260810, sof-bin v2025.12.2) and ChromeOS audio configuration"
bash "$ROOT/builder/fetch-inputs.sh" "$WORK/inputs"

log "4/4 KANO image + QEMU self-tests (about 10 minutes)"
# what this build is made of; an existing build is reused only when all of it is the same. The firmware and audio inputs
# are pinned by SHA-256 in builder/vendor.sh, so their paths are enough, unless GBOS_ALLOW_FIRMWARE_DRIFT=1.
PROFILE=${GBOS_PROFILE:-$ROOT/profiles/kano}
if [ -n "$ADB" ]; then ADB_ID=$(sha256sum < "$ADB" | cut -c1-64) || die "cannot read $ADB"; else ADB_ID=none; fi
SRC_ID=$(tree_digest "$ROOT/builder" "$PROFILE")
BASE_SUMS=$(tr '\n' ' ' < "$B/SHA256SUMS")
PARAMS="sources $SRC_ID
base $BASE_SUMS
avb-key $KEY_FP
adb-key $ADB_ID
selftest $SELFTEST
qemu-seconds ${GBOS_QEMU_SECONDS:-200}
default-entry ${GBOS_DEFAULT_ENTRY:-lapis-quiet.conf}
loader-editor ${GBOS_LOADER_EDITOR:-no}
image-name ${GBOS_IMAGE_NAME:-googlebook-kano.img}
profile $PROFILE
firmware ${GBOS_FIRMWARE:-$WORK/inputs/firmware}
audio-src ${GBOS_AUDIO_SRC:-$WORK/inputs/audio-src}
firmware-drift ${GBOS_ALLOW_FIRMWARE_DRIFT:-0}"
IMG="$WORK/build/image/${GBOS_IMAGE_NAME:-googlebook-kano.img}"
if [ -e "$WORK/build" ]; then
    [ -f "$IMG" ] && [ -f "$WORK/build/params" ] || die "$WORK/build holds an unfinished build; look at $WORK/build/*.log, then delete $WORK/build and run again"
    [ "${GBOS_ALLOW_FIRMWARE_DRIFT:-0}" = 0 ] \
        || die "GBOS_ALLOW_FIRMWARE_DRIFT=1: the inputs are not pinned, so $WORK/build is never reused; delete it"
    if [ "$(cat "$WORK/build/params")" != "$PARAMS" ]; then
        diff "$WORK/build/params" <(printf '%s\n' "$PARAMS") | sed -n 's/^< /  was: /p; s/^> /  now: /p' || true
        die "$WORK/build was made with other settings or sources (above); delete $WORK/build or use a new --work"
    fi
    echo "reusing $WORK/build (same sources and settings)"
else
    GBOS_BASE="$WORK/base/kano-real.img" GBOS_INITRD="$WORK/base/initrd.img" GBOS_OUT="$WORK/build" \
    GBOS_OTATOOLS="$WORK/base/cache/otatools" GBOS_AVB_KEY="$AVB_KEY" GBOS_INPUTS="$WORK/inputs" \
    GBOS_ADB_PUBKEY="$ADB" GBOS_SKIP_SPACE_CHECK=$SKIP_SPACE GBOS_SKIP_SELFTEST=$((1 - SELFTEST)) bash "$ROOT/builder/build.sh"
    printf '%s\n' "$PARAMS" > "$WORK/build/params"
    git -C "$ROOT" describe --always --dirty > "$WORK/build/revision" 2>/dev/null || echo unknown > "$WORK/build/revision"
fi
echo "checking the image against its SHA-256"
[ "$(sha256sum < "$IMG" | cut -c1-64)" = "$(cut -c1-64 "$IMG.sha256")" ] || die "$IMG does not match $IMG.sha256"
echo
echo "done: $IMG"
cat "$IMG.sha256"
[ ! -f "$IMG.not-self-tested" ] || echo "NOTE: built with --no-selftest; it was not booted in QEMU before release."
echo
echo "Write it to a USB drive (everything on that drive is erased):"
echo "  ls -l /dev/disk/by-id/ | grep usb-"
echo "  sudo $ROOT/install/write-usb.sh $IMG /dev/disk/by-id/usb-<your-drive>"
