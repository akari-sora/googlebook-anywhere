#!/bin/bash
# Drop-in /vendor/lib64/libminigbm_gralloc.so for the amd-hx370 image.
# The checks below pin this recipe on the host that runs the script.
# They do not pin the whole NDK tarball, the C++ headers, or each prebuilt
# link input, and they do not claim a second machine has reproduced the digest.
# LIB_SHA is the library this recipe must reproduce. LIB_FROM_* are the source
# pins recorded next to it. The script exits if those disagree with the source
# pins, or if the linked file is not LIB_SHA. The clang wrapper paths are
# checked to exist and to be scripts; that check is not a compiler pin.
# Pins AOSP external/minigbm android17-release ce79bbee67b3e2ea7abb110e1415e9ceceaa6471
# plus upstream-delta.patch (c662136 virtgpu params, 9d21b5c protected usage,
# 0c4f081 nvl_ids — Intel NVL-S/NVL-P PCI ids, not AMD Navi) and amdgpu_linear.c (-DDRV_AMDGPU).
# -DANDROID_API_LEVEL=37 matches soong platform_sdk_version so RGBA_FP16,
# RGBA_1010102, and RGBA_10101010 stay in cros_gralloc_convert_format.
# Re-execs under nice -n 19. Skips the build when /proc/pressure/io some avg60 > 40.
# Default WORK is ~/gbos-phase/minigbm4. Scratch is always /tmp/minigbm4. No sudo.
set -euo pipefail
export LC_ALL=C

if [[ "${NICE_WRAPPED:-}" != 1 ]]; then
  export NICE_WRAPPED=1
  exec nice -n 19 bash "$0" "$@"
fi

WORK="${WORK:-$HOME/gbos-phase/minigbm4}"
OUT="$WORK/out"
SRC="$WORK/src/minigbm"
AOSP="$WORK/src/aosp"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
LINEAR_SRC="$SCRIPT_DIR/amdgpu_linear.c"
PATCH="$SCRIPT_DIR/upstream-delta.patch"
STOCK_SYMS="$SCRIPT_DIR/stock-dynsyms.txt"
STOCK_UNDEF="$SCRIPT_DIR/stock-undefsyms.txt"
COMMIT=ce79bbee67b3e2ea7abb110e1415e9ceceaa6471
TREE=95c2eb6837a39a6ef62ae454c5d9767cf2066c53
# Uncompressed git archive --format=tar --prefix=minigbm/. gzip's sha256
# depends on the zlib and is not a pin.
TAR="$WORK/src/minigbm-${COMMIT}.tar"
TAR_SHA=54b1d07b4645ad73294eab40dbbc4578af43cdee192d4ba5e0b1c2747f5d8834
PATCH_SHA=0d2e6678b9c71a47a412245a85dfbf99702bda0f56da522fe65b996bb0bee022
LINEAR_SHA=ecf3d67c2163e789374979a58ee593b5a63cdd9c373e6a03056cc6ca72ade1bd
# Output pin and the source pins that produced it. Update these together
# when a pinned source changes. The comparisons are below.
LIB_SHA=297457940c9090754e71d4027e157167825edab4139eb7d8828d1660b98e0d42
LIB_FROM_LINEAR=ecf3d67c2163e789374979a58ee593b5a63cdd9c373e6a03056cc6ca72ade1bd
LIB_FROM_PATCH=0d2e6678b9c71a47a412245a85dfbf99702bda0f56da522fe65b996bb0bee022
LIB_FROM_TAR=54b1d07b4645ad73294eab40dbbc4578af43cdee192d4ba5e0b1c2747f5d8834
NDK_REV=30.0.16248370
# sha256 of readlink -f bin/clang (NDK r30 clang-21). Not the ~200-byte
# x86_64-linux-android37-clang wrapper. That wrapper is
# 7673632c25f0bdbaff3c04a456650df08465cf0ad5ea765059f5ae2dfae7e590 on
# several NDK revisions, so it does not identify the compiler.
CLANG_SHA=ad535f0b6fc303edba958b51636169ff0291c6e36463236c99ff8abcaa34247c
GIT_URL=https://android.googlesource.com/platform/external/minigbm
GIT_DIR="$WORK/src/minigbm.git"
NDK="${NDK:-$HOME/gbos-phase/fp/tools/android-ndk-r30}"
CC="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/x86_64-linux-android37-clang"
CXX="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/x86_64-linux-android37-clang++"
STRIP="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip"
READELF="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf"
OBJDUMP="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-objdump"
NM="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-nm"
CXXINC="$NDK/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/include/c++/v1"
LIBDRM_SRC="$WORK/src/libdrm-2.4.123"
LIBDRM_TAR="$WORK/src/libdrm-2.4.123.tar.xz"
LIBDRM_SHA=a2b98567a149a74b0f50e91e825f9c0315d86e7be9b74394dae8b298caadb79e
LIBDRM_URL=https://dri.freedesktop.org/libdrm/libdrm-2.4.123.tar.xz
AIDL="${AIDL:-$HOME/gbos-phase/fp/tools/bt/android-37.0/aidl}"
AIDL_SHA=a4ad150a7cf6ea297c299cb779da7a58c537cfc07923d1e15ccbc554a30cd4ba
IMG="${IMG:-$HOME/gbos-phase/amd-r11/out-amd-hx370/image/googlebook-amd-hx370.img}"
IMG_FALLBACKS=(
  "$HOME/gbos-phase/amd-r11/out-amd-hx370/image/googlebook-amd-hx370.img"
  "$HOME/gbos-phase/amd-r11/out-amd-hx370/googlebook-amd-hx370.img"
  "$HOME/gbos-phase/amd-r10/out-amd-hx370/image/googlebook-amd-hx370.img"
)
FSCK="${FSCK:-$HOME/gbos-phase/amd-t2/base/cache/otatools/bin/fsck.erofs}"
STOCK_SO_SHA=d48536951fd0e9643e71a5c8843e90833ce3e212e182782185470e2fbbfad673
TMP=/tmp/minigbm4

mkdir -p "$OUT" "$WORK/src" "$WORK/prebuilt" "$WORK/cxxsite" "$WORK/gen"
log() { printf '%s\n' "$*" | tee -a "$OUT/build.log"; }

if [[ -r /proc/pressure/io ]]; then
  avg60=$(awk '/^some / {
    for (i = 1; i <= NF; i++) {
      if (index($i, "avg60=") == 1) {
        split($i, a, "=")
        print a[2]
        exit
      }
    }
  }' /proc/pressure/io)
  log "psi some avg60=${avg60:-missing}"
  if awk -v v="${avg60:-0}" 'BEGIN { exit !(v+0 > 40) }'; then
    log "SKIP build: /proc/pressure/io some avg60 ${avg60} > 40"
    printf 'SKIP avg60=%s\n' "$avg60" > "$OUT/BUILD-STATUS.txt"
    exit 0
  fi
else
  log "no /proc/pressure/io; continuing"
  avg60=missing
fi
rm -f "$OUT/BUILD-STATUS.txt"

for req in "$LINEAR_SRC" "$PATCH" "$STOCK_SYMS" "$STOCK_UNDEF" "$CC" "$CXX" "$STRIP" "$AIDL" "$FSCK"; do
  if [[ ! -e "$req" ]]; then
    log "missing required path: $req"
    exit 1
  fi
done
echo "$AIDL_SHA  $AIDL" | sha256sum -c -
echo "$PATCH_SHA  $PATCH" | sha256sum -c -
echo "$LINEAR_SHA  $LINEAR_SRC" | sha256sum -c -
if [[ "$LINEAR_SHA" != "$LIB_FROM_LINEAR" || "$PATCH_SHA" != "$LIB_FROM_PATCH" || "$TAR_SHA" != "$LIB_FROM_TAR" ]]; then
  log "pinned source sha does not match the library built-from record"
  exit 1
fi
log "library built-from pins match the source pins"
CLANG_BIN=$(readlink -f "$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/clang")
if [[ ! -f "$CLANG_BIN" ]]; then
  log "missing clang compiler binary: $CLANG_BIN"
  exit 1
fi
if head -n 1 "$CLANG_BIN" | grep -q '^#!'; then
  log "resolved clang is a wrapper script, not the compiler binary: $CLANG_BIN"
  exit 1
fi
echo "$CLANG_SHA  $CLANG_BIN" | sha256sum -c -
# Wrappers are the paths $CC and $CXX execute. They must exist and be
# scripts. Their digest is not a compiler pin: the same wrapper text is
# shared by several NDK revisions. The compiler pin is CLANG_SHA above.
for wrap in "$CC" "$CXX"; do
  if [[ ! -e "$wrap" ]]; then
    log "missing clang wrapper: $wrap"
    exit 1
  fi
  if [[ ! -f "$wrap" || -d "$wrap" ]]; then
    log "clang wrapper is not a regular file: $wrap"
    exit 1
  fi
  if ! head -n 1 "$wrap" | grep -q '^#!'; then
    log "clang wrapper is not a script: $wrap"
    exit 1
  fi
done
log "clang wrappers exist and are scripts"
ndk_rev=$(awk -F= '/Pkg.Revision/ {gsub(/ /,"",$2); print $2}' "$NDK/source.properties")
if [[ "$ndk_rev" != "$NDK_REV" ]]; then
  log "NDK revision $ndk_rev != pinned $NDK_REV"
  exit 1
fi
log "ndk $ndk_rev clang binary pinned"

clone_pin() {
  local url="$1" dest="$2" rev="$3"
  shift 3
  if [[ -d "$dest/.git" ]] && [[ "$(git -C "$dest" rev-parse HEAD)" == "$rev" ]]; then
    if [[ -z "$(git -C "$dest" status --porcelain --untracked-files=all)" ]]; then
      return 0
    fi
    log "dirty pinned tree $dest; refetching"
  fi
  rm -rf "$dest"
  mkdir -p "$dest"
  git -C "$dest" init
  git -C "$dest" remote add origin "$url"
  if ! git -C "$dest" fetch --filter=blob:none --depth 1 origin "$rev"; then
    log "fetch $rev failed; trying android17-release"
    git -C "$dest" fetch --filter=blob:none --depth 1 origin android17-release
  fi
  git -C "$dest" sparse-checkout init --no-cone
  git -C "$dest" sparse-checkout set --skip-checks "$@"
  git -C "$dest" checkout --detach FETCH_HEAD
  if [[ "$(git -C "$dest" rev-parse HEAD)" != "$rev" ]]; then
    log "HEAD $(git -C "$dest" rev-parse HEAD) != pinned $rev for $url"
    exit 1
  fi
}

log "pinning header trees"
clone_pin https://android.googlesource.com/platform/system/core \
  "$AOSP/system-core" 545d2487e38192a2ce25040897ced877cf6b4f53 \
  libcutils/include libsystem/include libsync/include include
clone_pin https://android.googlesource.com/platform/system/logging \
  "$AOSP/system-logging" d35ba620451b28512f46ebb5421f783fd77968d5 \
  liblog/include
clone_pin https://android.googlesource.com/platform/hardware/libhardware \
  "$AOSP/libhardware" 97ee3e33ec5ba48c0eca8341ed377ac4497a0678 \
  include include_all include_vendor
clone_pin https://android.googlesource.com/platform/system/memory/libdmabufheap \
  "$AOSP/libdmabufheap" 09fe72b6ce0feead1b97c2859979b125f815d9b0 \
  include
clone_pin https://android.googlesource.com/platform/external/libcxx \
  "$AOSP/libcxx" 88894a62bb97313ed9bf79a546f1486de4b2e723 \
  include
clone_pin https://android.googlesource.com/platform/external/libcxxabi \
  "$AOSP/libcxxabi" 84450eba5a24fe5b7df77ed1307bfada05b14dc7 \
  include
clone_pin https://android.googlesource.com/platform/frameworks/native \
  "$AOSP/frameworks-native" ae266dcb706d083868578cfedce381ef44488a07 \
  libs/nativebase/include libs/nativewindow/include libs/arect/include libs/binder/ndk/include_cpp
clone_pin https://android.googlesource.com/platform/hardware/interfaces \
  "$AOSP/hardware-interfaces" 0162af698935100a590b7359581ac8b1b80693e5 \
  graphics/common/aidl
clone_pin https://android.googlesource.com/platform/system/libbase \
  "$AOSP/libbase" fef4173e19d97cd4b4fc09a06bccbf800d0d3937 \
  include

# drm_hwcomposer is citation-only (Plan B). Record the pin when the tree is present.
HWC_DIR="$WORK/src/drm_hwcomposer"
HWC_URL=https://android.googlesource.com/platform/external/drm_hwcomposer
HWC_REV=87daba89b05fabc7f1e9316e3f283f19fd0c45cd
if [[ ! -d "$HWC_DIR/.git" ]]; then
  git init "$HWC_DIR"
  git -C "$HWC_DIR" remote add origin "$HWC_URL"
fi
if [[ "$(git -C "$HWC_DIR" rev-parse HEAD 2>/dev/null || true)" != "$HWC_REV" ]]; then
  git -C "$HWC_DIR" fetch --filter=blob:none --depth 1 origin "$HWC_REV"
  git -C "$HWC_DIR" checkout --detach FETCH_HEAD
fi
if [[ "$(git -C "$HWC_DIR" rev-parse HEAD)" != "$HWC_REV" ]]; then
  log "drm_hwcomposer HEAD $(git -C "$HWC_DIR" rev-parse HEAD) != $HWC_REV"
  exit 1
fi

req_hdr() {
  if [[ ! -e "$1" ]]; then
    log "missing header: $1"
    exit 1
  fi
}
req_hdr "$AOSP/system-core/libcutils/include/cutils/native_handle.h"
req_hdr "$AOSP/system-core/libcutils/include/cutils/properties.h"
req_hdr "$AOSP/system-core/libsystem/include/system/graphics.h"
req_hdr "$AOSP/system-core/libsync/include/sync/sync.h"
req_hdr "$AOSP/system-logging/liblog/include/log/log.h"
req_hdr "$AOSP/libhardware/include_all/hardware/gralloc.h"
req_hdr "$AOSP/frameworks-native/libs/nativewindow/include/system/window.h"
req_hdr "$AOSP/frameworks-native/libs/binder/ndk/include_cpp/android/binder_enums.h"
req_hdr "$AOSP/libdmabufheap/include/BufferAllocator/BufferAllocator.h"
req_hdr "$AOSP/libbase/include/android-base/unique_fd.h"
req_hdr "$AOSP/libcxx/include/__config"

if [[ ! -f "$LIBDRM_TAR" ]] || ! echo "$LIBDRM_SHA  $LIBDRM_TAR" | sha256sum -c -; then
  log "downloading libdrm"
  curl -fsSL -o "$LIBDRM_TAR.partial" "$LIBDRM_URL"
  mv "$LIBDRM_TAR.partial" "$LIBDRM_TAR"
  echo "$LIBDRM_SHA  $LIBDRM_TAR" | sha256sum -c -
fi
rm -rf "$LIBDRM_SRC"
tar -xJf "$LIBDRM_TAR" -C "$WORK/src"

if [[ ! -d "$GIT_DIR" ]]; then
  git init --bare "$GIT_DIR"
fi
if ! git --git-dir="$GIT_DIR" cat-file -e "${COMMIT}^{commit}" 2>/dev/null; then
  log "fetching minigbm $COMMIT"
  if ! git --git-dir="$GIT_DIR" fetch --depth 1 "$GIT_URL" "$COMMIT"; then
    log "commit fetch failed; fetching android17-release"
    git --git-dir="$GIT_DIR" fetch --depth 1 "$GIT_URL" android17-release
  fi
fi
got_tree=$(git --git-dir="$GIT_DIR" rev-parse "${COMMIT}^{tree}")
if [[ "$got_tree" != "$TREE" ]]; then
  log "minigbm tree $got_tree != pinned $TREE"
  exit 1
fi
git --git-dir="$GIT_DIR" archive --format=tar --prefix=minigbm/ "$COMMIT" -o "$TAR"
echo "$TAR_SHA  $TAR" | sha256sum -c -
rm -rf "$SRC" "$WORK/src/minigbm-unpack"
mkdir -p "$WORK/src/minigbm-unpack"
tar -xf "$TAR" -C "$WORK/src/minigbm-unpack"
mv "$WORK/src/minigbm-unpack/minigbm" "$SRC"
rm -rf "$WORK/src/minigbm-unpack"
patch --batch -p1 -d "$SRC" -i "$PATCH"
cp "$LINEAR_SRC" "$SRC/amdgpu_linear.c"
log "minigbm $COMMIT unpacked and patched"

AIDL_ROOT="$AOSP/hardware-interfaces/graphics/common/aidl"
rm -rf "$WORK/gen/aidl"
mkdir -p "$WORK/gen/aidl"
"$AIDL" --lang=ndk -I "$AIDL_ROOT" -h "$WORK/gen/aidl" -o "$WORK/gen/aidl-src" \
  --structured --stability=vintf --min_sdk_version=29 \
  "$AIDL_ROOT/android/hardware/graphics/common/BlendMode.aidl" \
  "$AIDL_ROOT/android/hardware/graphics/common/Dataspace.aidl" \
  "$AIDL_ROOT/android/hardware/graphics/common/Cta861_3.aidl" \
  "$AIDL_ROOT/android/hardware/graphics/common/XyColor.aidl" \
  "$AIDL_ROOT/android/hardware/graphics/common/Smpte2086.aidl"
req_hdr "$WORK/gen/aidl/aidl/android/hardware/graphics/common/BlendMode.h"
req_hdr "$WORK/gen/aidl/aidl/android/hardware/graphics/common/Dataspace.h"
req_hdr "$WORK/gen/aidl/aidl/android/hardware/graphics/common/Cta861_3.h"
req_hdr "$WORK/gen/aidl/aidl/android/hardware/graphics/common/Smpte2086.h"

cat > "$WORK/cxxsite/__config_site" << 'EOF'
#ifndef _LIBCPP___CONFIG_SITE
#define _LIBCPP___CONFIG_SITE
#define _LIBCPP_ABI_VERSION 1
#define _LIBCPP_ABI_NAMESPACE __1
#define _LIBCPP_ABI_FORCE_ITANIUM 0
#define _LIBCPP_ABI_FORCE_MICROSOFT 0
#define _LIBCPP_HAS_THREADS 1
#define _LIBCPP_HAS_MONOTONIC_CLOCK 1
#define _LIBCPP_HAS_TERMINAL 1
#define _LIBCPP_HAS_MUSL_LIBC 0
#define _LIBCPP_HAS_THREAD_API_PTHREAD 0
#define _LIBCPP_HAS_THREAD_API_EXTERNAL 0
#define _LIBCPP_HAS_THREAD_API_WIN32 0
#define _LIBCPP_HAS_THREAD_API_C11 0
#define _LIBCPP_HAS_VENDOR_AVAILABILITY_ANNOTATIONS 0
#define _LIBCPP_HAS_FILESYSTEM 1
#define _LIBCPP_HAS_RANDOM_DEVICE 1
#define _LIBCPP_HAS_LOCALIZATION 1
#define _LIBCPP_HAS_UNICODE 1
#define _LIBCPP_HAS_WIDE_CHARACTERS 1
#define _LIBCPP_HAS_TIME_ZONE_DATABASE 0
#define _LIBCPP_INSTRUMENTED_WITH_ASAN 0
#define _LIBCPP_PSTL_BACKEND_STD_THREAD
#define _LIBCPP_HARDENING_MODE_DEFAULT 2
#endif
EOF

if [[ ! -f "$IMG" ]]; then
  for cand in "${IMG_FALLBACKS[@]}"; do
    if [[ -f "$cand" ]]; then
      IMG="$cand"
      break
    fi
  done
fi
REUSE_EXTRACT=0
if [[ ! -f "$IMG" ]]; then
  if [[ -d "$TMP/vendor" || -d "$TMP/system" ]]; then
    log "image file missing; reusing existing $TMP extract"
    REUSE_EXTRACT=1
  else
    log "image not found at $IMG"
    exit 1
  fi
fi

declare -A OFF LEN
if [[ "$REUSE_EXTRACT" == 0 ]]; then
log "parsing GPT super / liblp"
mapfile -t PARTS < <(python3 - "$IMG" << 'PY'
import struct, sys
img = sys.argv[1]
with open(img, "rb") as f:
    f.seek(512)
    hdr = f.read(92)
    if hdr[:8] != b"EFI PART":
        sys.exit("image is not a raw GPT (no EFI PART at LBA 1)")
    part_lba, num, entsize = struct.unpack_from("<QII", hdr, 72)
    f.seek(part_lba * 512)
    super_off = None
    for _ in range(num):
        ent = f.read(entsize)
        start, end = struct.unpack_from("<QQ", ent, 32)
        name = ent[56:128].decode("utf-16le", "replace").split("\x00", 1)[0]
        if name == "super":
            super_off = start * 512
            break
    if super_off is None:
        sys.exit("GPT has no super partition")
    f.seek(super_off + 0x1000)
    magic = struct.unpack("<I", f.read(4))[0]
    if magic != 0x616C4467:
        sys.exit("bad liblp geometry magic %#x" % magic)
    f.seek(super_off + 0x3000)
    header = f.read(256)
    hmagic, major, minor, header_size = struct.unpack_from("<IHHI", header, 0)
    if hmagic != 0x414C5030:
        sys.exit("bad liblp header magic %#x" % hmagic)
    def desc(off):
        return struct.unpack_from("<III", header, off)
    p_off, p_num, p_esz = desc(80)
    e_off, e_num, e_esz = desc(92)
    tables = super_off + 0x3000 + header_size
    f.seek(tables + p_off)
    parts = []
    for _ in range(p_num):
        raw = f.read(p_esz)
        name = raw[:36].split(b"\x00", 1)[0].decode()
        _attrs, first, n, _group = struct.unpack_from("<IIII", raw, 36)
        parts.append((name, first, n))
    f.seek(tables + e_off)
    extents = []
    for _ in range(e_num):
        raw = f.read(e_esz)
        num_sectors, target_type, target_data, target_source = struct.unpack_from("<QIQI", raw, 0)
        extents.append((num_sectors, target_type, target_data, target_source))
    want = ("vendor_a", "system_a", "system_ext_a")
    for name, first, n in parts:
        if name not in want:
            continue
        if n != 1:
            sys.exit("%s has %d extents" % (name, n))
        num_sectors, ttype, tdata, _tsrc = extents[first]
        if ttype != 0:
            sys.exit("%s extent is not LINEAR" % name)
        disk = super_off + tdata * 512
        length = num_sectors * 512
        print("%s\t%d\t%d" % (name, disk, length))
PY
)
declare -A OFF LEN
for row in "${PARTS[@]}"; do
  IFS=$'\t' read -r name disk length <<<"$row"
  OFF["$name"]=$disk
  LEN["$name"]=$length
  log "partition $name disk=$disk length=$length"
done
for name in vendor_a system_a system_ext_a; do
  if [[ -z "${OFF[$name]:-}" ]]; then
    log "liblp did not yield $name"
    exit 1
  fi
done

log "extracting vendor_a system_a system_ext_a (read-only)"
rm -rf "$TMP"
mkdir -p "$TMP/vendor" "$TMP/system" "$TMP/system_ext"
"$FSCK" --offset="${OFF[vendor_a]}" --extract="$TMP/vendor" "$IMG"
"$FSCK" --offset="${OFF[system_a]}" --extract="$TMP/system" "$IMG"
"$FSCK" --offset="${OFF[system_ext_a]}" --extract="$TMP/system_ext" "$IMG"
else
  log "not re-extracting; $TMP already holds the read-only partitions"
  OFF[vendor_a]=reuse
  LEN[vendor_a]=reuse
  OFF[system_a]=reuse
  LEN[system_a]=reuse
  OFF[system_ext_a]=reuse
  LEN[system_ext_a]=reuse
fi

find_lib() {
  local name="$1" hit="" root
  for root in "$TMP/vendor" "$TMP/system" "$TMP/system_ext"; do
    hit=$(find "$root" -name "$name" -path '*/lib64/*' ! -path '*/arm64/*' -print -quit)
    if [[ -n "$hit" ]]; then
      printf '%s\n' "$hit"
      return 0
    fi
  done
  return 1
}

STOCK_SO=$(find_lib libminigbm_gralloc.so)
echo "$STOCK_SO_SHA  $STOCK_SO" | sha256sum -c -
log "stock so $STOCK_SO"

rm -rf "$WORK/prebuilt"
mkdir -p "$WORK/prebuilt"
for lib in libcutils.so libdrm.so liblog.so libsync.so libnativewindow.so \
           libbinder_ndk.so libdmabufheap.so libgralloctypes.so libc++.so; do
  src=$(find_lib "$lib")
  cp -a "$src" "$WORK/prebuilt/$lib"
  log "prebuilt $lib <= $src"
done

CFLAGS=(
  --target=x86_64-linux-android37
  -O2 -fPIC -std=gnu11
  -D_GNU_SOURCE -DANDROID -DNDEBUG
  -DHAS_DMABUF_SYSTEM_HEAP -DDRV_I915 -DDRV_XE -DDRV_AMDGPU
  -DANDROID_API_LEVEL=37
  -DPAGE_SIZE=4096
  -Wno-unused-parameter -Wno-unused-function -Wno-unused-but-set-variable
  -ffunction-sections -fdata-sections
  -fstack-protector-strong -D_FORTIFY_SOURCE=2 -ftrivial-auto-var-init=zero -funwind-tables
  -I "$WORK/gen/aidl"
  -I "$AOSP/frameworks-native/libs/binder/ndk/include_cpp"
  -I "$AOSP/system-core/libcutils/include"
  -I "$AOSP/system-core/libsystem/include"
  -I "$AOSP/system-core/libsync/include"
  -I "$AOSP/system-logging/liblog/include"
  -I "$AOSP/libhardware/include"
  -I "$AOSP/frameworks-native/libs/nativebase/include"
  -I "$AOSP/frameworks-native/libs/nativewindow/include"
  -I "$AOSP/frameworks-native/libs/arect/include"
  -I "$AOSP/libdmabufheap/include"
  -I "$AOSP/libbase/include"
  -I "$SRC"
  -I "$LIBDRM_SRC"
  -I "$LIBDRM_SRC/include/drm"
)
CXXFLAGS=(
  --target=x86_64-linux-android37
  -O2 -fPIC -std=gnu++20
  -fno-exceptions -fno-rtti -nostdinc++
  -D_GNU_SOURCE -DANDROID -DNDEBUG
  -DHAS_DMABUF_SYSTEM_HEAP -DDRV_I915 -DDRV_XE -DDRV_AMDGPU
  -DANDROID_API_LEVEL=37
  -DPAGE_SIZE=4096
  -Wno-unused-parameter -Wno-unused-function -Wno-unused-but-set-variable
  -ffunction-sections -fdata-sections
  -fstack-protector-strong -D_FORTIFY_SOURCE=2 -ftrivial-auto-var-init=zero -funwind-tables
  -fvisibility-inlines-hidden
  -isystem "$WORK/cxxsite"
  -isystem "$CXXINC"
  -I "$WORK/gen/aidl"
  -I "$AOSP/frameworks-native/libs/binder/ndk/include_cpp"
  -I "$AOSP/system-core/libcutils/include"
  -I "$AOSP/system-core/libsystem/include"
  -I "$AOSP/system-core/libsync/include"
  -I "$AOSP/system-logging/liblog/include"
  -I "$AOSP/libhardware/include"
  -I "$AOSP/frameworks-native/libs/nativebase/include"
  -I "$AOSP/frameworks-native/libs/nativewindow/include"
  -I "$AOSP/frameworks-native/libs/arect/include"
  -I "$AOSP/libdmabufheap/include"
  -I "$AOSP/libbase/include"
  -I "$SRC"
  -I "$LIBDRM_SRC"
  -I "$LIBDRM_SRC/include/drm"
)

rm -rf "$OUT/obj"
mkdir -p "$OUT/obj"
C_SRCS=(
  drv.c drv_helpers.c drv_array_helpers.c dumb_driver.c backend_mock.c
  virtgpu.c virtgpu_cross_domain.c virtgpu_virgl.c dma_heap.c
  i915.c xe.c mediatek.c rockchip.c msm.c vc4.c amdgpu_linear.c
)
for src in "${C_SRCS[@]}"; do
  base=$(basename "$src" .c)
  log "cc $src"
  "$CC" "${CFLAGS[@]}" -c "$SRC/$src" -o "$OUT/obj/$base.o"
done
for src in cros_gralloc_helpers.cc cros_gralloc_buffer.cc cros_gralloc_driver.cc; do
  base=$(basename "$src" .cc)
  log "cxx $src"
  "$CXX" "${CXXFLAGS[@]}" -c "$SRC/cros_gralloc/$src" -o "$OUT/obj/$base.o"
done

log "per-object hardening imports"
python3 - "$NM" "$OUT/obj" << 'PY'
import subprocess, sys
from pathlib import Path
nm, objdir = sys.argv[1:]
root = Path(objdir)

def undef(path):
    proc = subprocess.run([nm, "-u", str(path)], capture_output=True, text=True)
    if proc.returncode != 0 and not proc.stdout:
        sys.exit("nm failed on %s: %s" % (path.name, proc.stderr.strip()))
    names = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and "U" in parts[:-1]:
            names.add(parts[-1].split("@")[0])
    return names

need_stack = (
    "cros_gralloc_helpers.o",
    "cros_gralloc_buffer.o",
    "cros_gralloc_driver.o",
)
c_stack = []
for path in sorted(root.glob("*.o")):
    syms = undef(path)
    has_stack = "__stack_chk_fail" in syms
    has_open2 = "__open_2" in syms
    print("obj", path.name, "stack_chk", int(has_stack), "open_2", int(has_open2))
    if path.name in need_stack and not has_stack:
        sys.exit(path.name + " missing __stack_chk_fail")
    if path.name == "virtgpu_virgl.o" and not has_open2:
        sys.exit(path.name + " missing __open_2")
    if not path.name.startswith("cros_gralloc_") and has_stack:
        c_stack.append(path.name)
if not c_stack:
    sys.exit("no C object imports __stack_chk_fail")
print("object_hardening_ok")
PY

OBJS=(
  "$OUT/obj/amdgpu_linear.o"
  "$OUT/obj/backend_mock.o"
  "$OUT/obj/cros_gralloc_buffer.o"
  "$OUT/obj/cros_gralloc_driver.o"
  "$OUT/obj/cros_gralloc_helpers.o"
  "$OUT/obj/dma_heap.o"
  "$OUT/obj/drv.o"
  "$OUT/obj/drv_array_helpers.o"
  "$OUT/obj/drv_helpers.o"
  "$OUT/obj/dumb_driver.o"
  "$OUT/obj/i915.o"
  "$OUT/obj/mediatek.o"
  "$OUT/obj/msm.o"
  "$OUT/obj/rockchip.o"
  "$OUT/obj/vc4.o"
  "$OUT/obj/virtgpu.o"
  "$OUT/obj/virtgpu_cross_domain.o"
  "$OUT/obj/virtgpu_virgl.o"
  "$OUT/obj/xe.o"
)
log "link"
"$CXX" --target=x86_64-linux-android37 -shared -O2 -fPIC -nostdlib++ \
  -Wl,-soname,libminigbm_gralloc.so \
  -Wl,-z,relro -Wl,-z,now \
  -Wl,-z,max-page-size=16384 -Wl,-z,noexecstack -Wl,--no-undefined \
  -Wl,--gc-sections -Wl,--hash-style=gnu \
  "${OBJS[@]}" \
  -L "$WORK/prebuilt" -Wl,-rpath-link,"$WORK/prebuilt" \
  -lc++ -lcutils -ldmabufheap -ldrm -lgralloctypes -llog -lnativewindow -lsync -lbinder_ndk \
  -o "$OUT/libminigbm_gralloc.so"
"$STRIP" --strip-unneeded "$OUT/libminigbm_gralloc.so"

log "abi checks"
python3 - "$OUT/libminigbm_gralloc.so" "$STOCK_SYMS" "$TMP" "$NM" "$READELF" "$OBJDUMP" "$STOCK_UNDEF" << 'PY'
import os, pathlib, struct, subprocess, sys
so, stock_path, tmp, nm, readelf, objdump, undef_path = sys.argv[1:]

def run(cmd):
    return subprocess.check_output(cmd, text=True, errors="replace")

def defined(path):
    names = set()
    try:
        out = run([nm, "-D", "--defined-only", path])
    except subprocess.CalledProcessError:
        return names
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-2] not in ("U",):
            names.add(parts[-1].split("@")[0])
    return names

def undefined(path):
    names = set()
    out = run([nm, "-D", "--undefined-only", path])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            names.add(parts[-1].split("@")[0])
    return names

stock = set()
for line in open(stock_path, encoding="utf-8"):
    line = line.strip()
    if line:
        stock.add(line.split("@")[0])
new = defined(so)
missing = sorted(stock - new)
extra = sorted(new - stock)
print("missing_count", len(missing))
for m in missing:
    print("MISSING", m)
print("extra_count", len(extra))
for e in extra:
    print("EXTRA", e)
if missing:
    sys.exit("stock dynsym missing from new lib")
for need in ("backend_amdgpu", "backend_vkms"):
    if need not in new:
        sys.exit("missing export " + need)

dyn = run([readelf, "-d", so])
needed = []
soname = None
for line in dyn.splitlines():
    if "Shared library:" in line:
        needed.append(line.split("[", 1)[1].split("]", 1)[0])
    if "(SONAME)" in line:
        soname = line.split("[", 1)[1].split("]", 1)[0]
print("SONAME", soname)
if soname != "libminigbm_gralloc.so":
    sys.exit("soname mismatch")
stock_needed = {
    "libbinder_ndk.so", "libcutils.so", "libdmabufheap.so", "libdrm.so",
    "libgralloctypes.so", "liblog.so", "libnativewindow.so", "libsync.so",
    "libc++.so", "libc.so", "libm.so", "libdl.so",
}
print("NEEDED", " ".join(needed))
if set(needed) != stock_needed:
    sys.exit("NEEDED set != stock set")

stock_undef = set()
for line in open(undef_path, encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    stock_undef.add(line.split()[-1].split("@")[0])
new_undef = undefined(so)
if "__stack_chk_fail" not in new_undef:
    sys.exit("new lib does not import __stack_chk_fail")
# Stock imports no __*_chk symbol. __stack_chk_fail does not match that
# suffix, so a missing_chk set was always empty and was not a gate.
# FORTIFY of open() is __open_2, which the stock library imports and which
# -D_FORTIFY_SOURCE=2 produces for open() in virtgpu_virgl.c.
if "__open_2" not in new_undef:
    sys.exit("new lib does not import __open_2 (_FORTIFY_SOURCE)")
print("hardening_imports_ok", "__stack_chk_fail", "__open_2")

present = set()
for root, dirs, files in os.walk(tmp):
    for fn in files:
        if ".so" in fn:
            present.add(fn)
missing_libs = sorted(set(needed) - present)
print("needed_not_in_image", " ".join(missing_libs))
if missing_libs:
    sys.exit("NEEDED library is not in the extracted image")

def find_consumer(name):
    for root, dirs, files in os.walk(tmp):
        if name in files and "arm64" not in root.replace("\\", "/"):
            return os.path.join(root, name)
    return None

consumers = [
    "gralloc.minigbm.so",
    "mapper.minigbm.so",
    "libminigbm_gralloc4_utils.so",
    "android.hardware.graphics.allocator-service.minigbm",
]
for name in consumers:
    path = find_consumer(name)
    if not path:
        sys.exit("consumer missing: " + name)
    und = undefined(path)
    gap = sorted((und & stock) - new)
    print("consumer", name, "und", len(und), "stock_gap", len(gap))
    for g in gap:
        print("GAP", name, g)
    if gap:
        sys.exit("consumer stock gap")

data = open(so, "rb").read()
sym = run([readelf, "-W", "-s", so])
nvl = None
for line in sym.splitlines():
    if line.endswith(" nvl_ids") or " nvl_ids" in line.split():
        parts = line.split()
        if parts[-1].split("@")[0] == "nvl_ids":
            nvl = (int(parts[1], 16), int(parts[2]))
            break
if not nvl or nvl[1] != 30:
    sys.exit("nvl_ids missing or size != 30: %s" % (nvl,))
e_phoff = struct.unpack_from("<Q", data, 32)[0]
e_phentsize = struct.unpack_from("<H", data, 54)[0]
e_phnum = struct.unpack_from("<H", data, 56)[0]
file_off = None
vaddr, size = nvl
for i in range(e_phnum):
    off = e_phoff + i * e_phentsize
    p_type, _flags, p_offset, p_vaddr, _p, p_filesz, _m, _a = struct.unpack_from("<IIQQQQQQ", data, off)
    if p_type == 1 and p_vaddr <= vaddr < p_vaddr + p_filesz:
        file_off = p_offset + (vaddr - p_vaddr)
        break
if file_off is None:
    sys.exit("nvl_ids vaddr not in a LOAD segment")
blob = data[file_off:file_off + size]
expect = struct.pack("<15H",
    0xD740, 0xD741, 0xD742, 0xD743, 0xD744, 0xD745,
    0xD750, 0xD751, 0xD752, 0xD753, 0xD754, 0xD755, 0xD756, 0xD757, 0xD75F)
if blob != expect:
    sys.exit("nvl_ids bytes differ: " + blob.hex())
print("nvl_ids_ok", blob.hex())

dis = run([objdump, "-d", "--disassemble-symbols=drv_is_protected_usage_permitted", so])
if "drv_is_protected_usage_permitted" not in dis or "0xa0" not in dis:
    print(dis)
    sys.exit("drv_is_protected_usage_permitted does not load backend+0xa0")
print("protected_usage_offset_ok")
print("ABI_OK")
PY

"$NM" -D --defined-only "$OUT/libminigbm_gralloc.so" | awk '{print $NF}' | sed 's/@.*//' | sort -u > "$OUT/new-dynsyms.txt"

sha=$(sha256sum "$OUT/libminigbm_gralloc.so" | awk '{print $1}')
size=$(stat -c %s "$OUT/libminigbm_gralloc.so")
log "built $sha size=$size"

# SOURCES.txt is copied back into the repo. Write $HOME, not this machine's home directory.
home_literal() {
  local p="$1"
  if [[ -n ${HOME} && ( $p == "$HOME" || $p == "$HOME"/* ) ]]; then
    printf '$HOME%s\n' "${p#"$HOME"}"
  else
    printf '%s\n' "$p"
  fi
}

{
  echo "# Sources for the amd-hx370 libminigbm_gralloc.so drop-in"
  echo "# sha256 values are of the pinned artifact named on the previous line."
  echo
  echo "minigbm"
  echo "url: $GIT_URL"
  echo "commit: $COMMIT"
  echo "tree: $TREE"
  echo "git-archive-tar-sha256: $TAR_SHA"
  echo "note: archive prefix minigbm/. The pin is the uncompressed git archive and the tree id. gzip sha256 is not stable across zlib implementations and is not checked."
  echo "note: upstream-delta.patch is c662136 + 9d21b5c + 0c4f081 applied on ce79bbee. No single public branch tip matches the stock binary: android17-release still has VIRTGPU_PARAM_GUEST_VRAM and lacks nvl_ids; upstream-main has nvl_ids but dropped the pastel/lavapipe lock path whose strings are in the stock .so."
  echo "note: compile defines ANDROID_API_LEVEL=37 so cros_gralloc_convert_format keeps RGBA_FP16, RGBA_1010102, and RGBA_10101010."
  echo "patch-sha256: $(sha256sum "$PATCH" | awk '{print $1}')"
  echo "amdgpu_linear.c-sha256: $(sha256sum "$LINEAR_SRC" | awk '{print $1}')"
  echo
  echo "libdrm"
  echo "url: $LIBDRM_URL"
  echo "version: 2.4.123"
  echo "sha256: $LIBDRM_SHA"
  echo
  echo "aidl"
  echo "path: $(home_literal "$AIDL")"
  echo "sha256: $AIDL_SHA"
  echo "note: --lang=ndk --structured --stability=vintf --min_sdk_version=29 on BlendMode Dataspace Cta861_3 XyColor Smpte2086 only."
  echo
  echo "ndk"
  echo "path: $(home_literal "$NDK")"
  echo "revision: $NDK_REV"
  echo "clang-binary-sha256: $CLANG_SHA"
  echo "note: clang-binary-sha256 is readlink -f of toolchains/llvm/prebuilt/linux-x86_64/bin/clang (the compiler), not the x86_64-linux-android37-clang wrapper script."
  echo "note: compile uses NDK r30 libc++ headers at sysroot/usr/include/c++/v1 with cxxsite/__config_site forcing _LIBCPP_ABI_NAMESPACE __1."
  echo "note: the NDK revision and this clang binary are checked. The full NDK tarball, the C++ headers, and the nine prebuilt link inputs are not individually pinned. No second-host rebuild is recorded."
  echo "note: the NDK __config_site sets __ndk1 unconditionally, so a command-line -D_LIBCPP_ABI_NAMESPACE=__1 is overwritten. An earlier -isystem directory supplies the __1 site header."
  echo "cxxsite-sha256: $(sha256sum "$WORK/cxxsite/__config_site" | awk '{print $1}')"
  echo
  echo "output"
  echo "libminigbm_gralloc.so-sha256: $sha"
  echo "size: $size"
  echo "built-from-amdgpu_linear.c-sha256: $LIB_FROM_LINEAR"
  echo "built-from-patch-sha256: $LIB_FROM_PATCH"
  echo "built-from-tar-sha256: $LIB_FROM_TAR"
  echo "note: profile [gralloc] pin. The built-from lines are the source pins that produced this library. The build script exits when they differ from LINEAR_SHA, PATCH_SHA, or TAR_SHA, or when the linked file is not this library pin. LIB_SHA, LIB_FROM_LINEAR, LIB_FROM_PATCH, and LIB_FROM_TAR update together, only after a rebuild whose output sha256 is LIB_SHA. EXTRA dynsyms vs stock: 9 (was 13 before -fvisibility-inlines-hidden)."
  echo
  echo "image"
  echo "path: $(home_literal "$IMG")"
  echo "stock-libminigbm_gralloc.so-sha256: $STOCK_SO_SHA"
  echo "vendor_a_disk: ${OFF[vendor_a]} length: ${LEN[vendor_a]}"
  echo "system_a_disk: ${OFF[system_a]} length: ${LEN[system_a]}"
  echo "system_ext_a_disk: ${OFF[system_ext_a]} length: ${LEN[system_ext_a]}"
  echo "note: partitions come from the GPT super liblp metadata. The image file itself is not rehashed here."
  echo
  record() {
    local name="$1" url="$2" dest="$3"
    echo "$name"
    echo "url: $url"
    echo "commit: $(git -C "$dest" rev-parse HEAD)"
    echo "tree: $(git -C "$dest" rev-parse 'HEAD^{tree}')"
    echo "sparse-index-sha256: $(git -C "$dest" ls-files -s | sha256sum | awk '{print $1}')"
    echo
  }
  record system/core https://android.googlesource.com/platform/system/core "$AOSP/system-core"
  echo "note: log/log.h is not in this snap of system/core. It is in system/logging. system/window.h is in frameworks/native libs/nativewindow. sync/sync.h is system/core libsync/include."
  echo
  record system/logging https://android.googlesource.com/platform/system/logging "$AOSP/system-logging"
  record hardware/libhardware https://android.googlesource.com/platform/hardware/libhardware "$AOSP/libhardware"
  echo "note: include/hardware/gralloc.h is a symlink to include_all/hardware/gralloc.h. Both paths are checked out."
  echo
  record system/memory/libdmabufheap https://android.googlesource.com/platform/system/memory/libdmabufheap "$AOSP/libdmabufheap"
  record external/libcxx https://android.googlesource.com/platform/external/libcxx "$AOSP/libcxx"
  echo "note: this android17-release external/libcxx is the Trusty LLVM 8 tree. _LIBCPP_VERSION in include/__config is $(grep -m1 '_LIBCPP_VERSION ' "$AOSP/libcxx/include/__config" | awk '{print $3}'). Android.bp exposes it to Trusty, not to the platform libc++.so on this image. The image libc++.so exports std::__1 (nm St3__1), including bad_expected_access and __libcpp_verbose_abort, and file(1) reports an NDK-built libc++. Those headers were fetched and are not the compile -I path."
  echo
  record external/libcxxabi https://android.googlesource.com/platform/external/libcxxabi "$AOSP/libcxxabi"
  echo "note: fetched with external/libcxx. Not used as a compile include path. The link uses the image libc++.so."
  echo
  record frameworks/native https://android.googlesource.com/platform/frameworks/native "$AOSP/frameworks-native"
  record hardware/interfaces https://android.googlesource.com/platform/hardware/interfaces "$AOSP/hardware-interfaces"
  record system/libbase https://android.googlesource.com/platform/system/libbase "$AOSP/libbase"
  echo "note: BufferAllocator.h includes android-base/unique_fd.h. That header is header-only here. libbase.so is not a NEEDED entry."
  echo
  record external/drm_hwcomposer "$HWC_URL" "$HWC_DIR"
  echo "note: citation tree for Plan B. Not linked into libminigbm_gralloc.so."
  echo
  if [[ -d "$HOME/gbos-phase/amd-t2/kernel-amd/ksrc/.git" ]]; then
    echo "kernel/common (citation, already on the build host)"
    echo "path: $(home_literal "$HOME/gbos-phase/amd-t2/kernel-amd/ksrc")"
    echo "commit: $(git -C "$HOME/gbos-phase/amd-t2/kernel-amd/ksrc" rev-parse HEAD)"
    echo "note: amdgpu_dma_buf.c prime import. Not a compile input."
    echo
  fi
} > "$OUT/SOURCES.txt"

log "string diff vs stock"
python3 - "$OUT/libminigbm_gralloc.so" "$STOCK_SO" "$OUT/strings-diff.txt" << 'PY'
import struct, sys
from pathlib import Path

def rodata(path):
    data = Path(path).read_bytes()
    e_shoff = struct.unpack_from("<Q", data, 40)[0]
    e_shentsize = struct.unpack_from("<H", data, 58)[0]
    e_shnum = struct.unpack_from("<H", data, 60)[0]
    e_shstrndx = struct.unpack_from("<H", data, 62)[0]
    def shdr(i):
        off = e_shoff + i * e_shentsize
        return struct.unpack_from("<IIQQQQIIQQ", data, off)
    strsh = shdr(e_shstrndx)
    strtab = data[strsh[4]:strsh[4] + strsh[5]]
    def name_of(sh):
        return strtab[sh[0]:strtab.index(b"\x00", sh[0])].decode()
    blobs = []
    for i in range(e_shnum):
        sh = shdr(i)
        if name_of(sh).startswith(".rodata"):
            blobs.append(data[sh[4]:sh[4] + sh[5]])
    return b"".join(blobs)

def cstrings(blob, n=8):
    out, cur = [], bytearray()
    for b in blob:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= n:
                out.append(cur.decode())
            cur = bytearray()
    if len(cur) >= n:
        out.append(cur.decode())
    return set(out)

new_s = cstrings(rodata(sys.argv[1]))
stock_s = cstrings(rodata(sys.argv[2]))
amdgpu_ok = (
    "amdgpu", "AMDGPU", "minigbm-amdgpu", "minigbm_amdgpu",
    "backend_amdgpu",
)
def amdgpuish(s):
    return any(k in s for k in amdgpu_ok)

def layoutish(s):
    # Compiler-merged fourcc tables and the unfolder DRM_FOURCC_ literal.
    if s == "DRM_FOURCC_":
        return True
    if len(s) >= 8 and all(c.isalnum() or c in " +" for c in s) and any(tok in s for tok in ("AB24", "AB30", "AB4H", "NV12", "XR24")):
        return True
    return False

stock_only = sorted(s for s in stock_s - new_s if not layoutish(s))
new_only = sorted(s for s in new_s - stock_s if not amdgpuish(s) and not layoutish(s))
lines = []
lines.append("stock_only_nonlayout %d" % len(stock_only))
for s in stock_only:
    lines.append("STOCK_ONLY " + s)
lines.append("new_only_non_amdgpu %d" % len(new_only))
for s in new_only:
    lines.append("NEW_ONLY " + s)
text = "\n".join(lines) + "\n"
Path(sys.argv[3]).write_text(text)
sys.stdout.write(text)
missing = [s for s in (
    "Attempted to lock() a buffer that was not allocated with a BufferUsage::CPU_* usage.",
    "pastel",
) if s not in new_s and not any(s in x for x in new_s)]
# pastel is 6 chars and may be below the length cutoff; check the raw file.
raw = Path(sys.argv[1]).read_bytes()
for needle in (b"Attempted to lock", b"pastel", b"ro.hardware.vulkan", b"AIBinder_getCallingUid"):
    if needle not in raw:
        sys.exit("new lib missing stock marker %s" % needle.decode())
for needle in (b"VIRTGPU_PARAM_GUEST_VRAM", b"VIRTGPU_PARAM_RESOURCE_SYNC"):
    if needle in raw:
        sys.exit("new lib still has %s" % needle.decode())
if any("GUEST_VRAM" in s or "RESOURCE_SYNC" in s for s in new_only):
    sys.exit("virtgpu param strings leaked")
allowed_new = []
if stock_only:
    sys.exit("stock_only_nonlayout is not 0")
if new_only != allowed_new:
    sys.exit("new_only_non_amdgpu is not the allowlist")
print("STRINGS_OK")
PY

if [[ "$sha" != "$LIB_SHA" ]]; then
  log "built $sha != pinned library $LIB_SHA"
  log "refusing PASS: a pinned source changed or the library pin was not updated"
  log "removing /tmp scratch"
  rm -rf /tmp/minigbm4
  exit 1
fi

{
  echo "PASS"
  echo "sha256=$sha"
  echo "size=$size"
  echo "soname=libminigbm_gralloc.so"
  echo "commit=$COMMIT"
  echo "tree=$TREE"
  echo "tar_sha256=$TAR_SHA"
  echo "psi_avg60_at_start=$avg60"
  echo "stock_so_sha256=$STOCK_SO_SHA"
  echo "vendor_a_disk=${OFF[vendor_a]}"
  echo "system_a_disk=${OFF[system_a]}"
  echo "system_ext_a_disk=${OFF[system_ext_a]}"
} > "$OUT/BUILD-STATUS.txt.tmp"
mv "$OUT/BUILD-STATUS.txt.tmp" "$OUT/BUILD-STATUS.txt"

log "removing /tmp scratch"
rm -rf /tmp/minigbm4
log "PASS $sha"
