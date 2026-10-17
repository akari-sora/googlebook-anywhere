#!/usr/bin/env bash
# Build Mesa RADV as /vendor/lib64/hw/vulkan.radeon.so for android-x86_64, plus the
# one-shot loader probe (radv-probe, libvkprop.so).
#
# License / source: Mesa is MIT (https://docs.mesa3d.org/license.html). libdrm is MIT.
# See tools/mesa/NOTICES.txt for full third-party license notices and pinned SHA-256 digests.
# The binaries are produced here and pinned by SHA-256; they are not committed.
#
# usage: tools/mesa/build-radv.sh [--clean] <work dir>
#   <work dir> holds dl/, ndk/, mesa-<ver>/, glslang/, venv/ (created or reused) and out/.
#   --clean removes all extracted/build trees and generated config files
#   (mesa-<ver>/, radv-build/, radv-install/, venv/, out/, ndk/, glslang/, pc/, radv-android.ini)
#   while keeping verified source archives in dl/ so a clean rebuild starts from fresh trees.
# Pinned inputs: Mesa, libdrm (Mesa wrap), Android NDK, glslang. Meson/Mako/PyYAML are pinned by version.
set -euo pipefail
CLEAN=0
if [ "${1:-}" = "--clean" ]; then
    CLEAN=1
    shift
fi
WORK=${1:?usage: build-radv.sh [--clean] <work dir>}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MESA_VERSION=26.1.3
MESA_URL=https://archive.mesa3d.org/mesa-26.1.3.tar.xz
MESA_SHA256=7725004e724b34c6d4fbaf5c48fc6c6223aa9f2741d6d7782c699b049356fc45
LIBDRM_VERSION=2.4.123
LIBDRM_SHA256=a2b98567a149a74b0f50e91e825f9c0315d86e7be9b74394dae8b298caadb79e
NDK_VERSION=r27c
NDK_URL=https://dl.google.com/android/repository/android-ndk-r27c-linux.zip
NDK_SHA256=59c2f6dc96743b5daf5d1626684640b20a6bd2b1d85b13156b90333741bad5cc
GLSLANG_VERSION=16.6.0
GLSLANG_URL=https://github.com/KhronosGroup/glslang/releases/download/16.6.0/glslang-16.6.0-linux-x86_64-release.tar.gz
GLSLANG_SHA256=2c34071f56ecf39d16233294d3739cf43f981fd39080f2fd9dccb782101191ce
MESON_VERSION=1.12.1
MAKO_VERSION=1.3.10
PYYAML_VERSION=6.0.2
# Filled after the first successful build of this script. Empty means "print the hash".
# Note: RADV_SHA pins the step-3 WSL build (--strip-debug, 19,194,688 B); running
# llvm-strip --strip-unneeded produces RADV_STRIP_UNNEEDED_SHA (18,182,336 B, -1,012,352 B) with the same .dynsym.
RADV_SHA=35e7084748bd7ed25a81acf2646019fd1e0e87474a313e18fed5b70b45221d81
RADV_STRIP_UNNEEDED_SHA=198f2e1f610c8f52442658f231dfc9fbeb679642ae2068f5bcec8b7ea9f5ad5c
PROBE_SHA=fe9831f3a68a414f830778d2f11ce5dc4f22b733d334d2a26d28a6960d758a75
VKPROP_SHA=94093697ab1e2d48be2784f672be2e1d0ddf78fb7fd9b4c4fd1a8477b18245a6

[ "$(uname -sm)" = "Linux x86_64" ] || { echo "build-radv.sh runs on Linux x86_64"; exit 2; }
for c in curl python3 sha256sum unzip tar xz; do command -v "$c" >/dev/null || { echo "missing $c"; exit 2; }; done
[ -f "$HERE/NOTICES.txt" ] || { echo "missing $HERE/NOTICES.txt"; exit 2; }
for d in "$MESA_SHA256" "$LIBDRM_SHA256" "$NDK_SHA256" "$GLSLANG_SHA256"; do
    grep -qF "$d" "$HERE/NOTICES.txt" || { echo "NOTICES.txt missing pinned digest $d"; exit 2; }
done
sha() { sha256sum "$1" | cut -d' ' -f1; }
fetch() { # url sha dest
    if [ -f "$3" ] && [ "$(sha "$3")" = "$2" ]; then echo "ok (cached) $(basename "$3")"; return; fi
    echo "fetch $1"
    curl -fL --retry 3 --max-time 3600 -o "$3.part" "$1"
    [ "$(sha "$3.part")" = "$2" ] || { echo "sha256 mismatch: $3"; exit 1; }
    mv "$3.part" "$3"
}

if [ "$CLEAN" = 1 ]; then
    rm -rf "$WORK/mesa-${MESA_VERSION}" "$WORK/radv-build" "$WORK/radv-install" "$WORK/venv" "$WORK/out" \
           "$WORK/ndk" "$WORK/glslang" "$WORK/pc" "$WORK/radv-android.ini"
fi
mkdir -p "$WORK/dl" "$WORK/out"
cp -f "$HERE/NOTICES.txt" "$WORK/out/NOTICES.txt"
fetch "$MESA_URL" "$MESA_SHA256" "$WORK/dl/mesa-${MESA_VERSION}.tar.xz"
fetch "$NDK_URL" "$NDK_SHA256" "$WORK/dl/android-ndk-${NDK_VERSION}-linux.zip"
fetch "$GLSLANG_URL" "$GLSLANG_SHA256" "$WORK/dl/glslang-${GLSLANG_VERSION}-linux-x86_64-release.tar.gz"

if [ ! -x "$WORK/ndk/android-ndk-${NDK_VERSION}/toolchains/llvm/prebuilt/linux-x86_64/bin/x86_64-linux-android35-clang" ]; then
    rm -rf "$WORK/ndk/android-ndk-${NDK_VERSION}"
    mkdir -p "$WORK/ndk"
    unzip -q "$WORK/dl/android-ndk-${NDK_VERSION}-linux.zip" -d "$WORK/ndk"
fi
NDK="$WORK/ndk/android-ndk-${NDK_VERSION}"
CC="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/x86_64-linux-android35-clang"
CXX="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/x86_64-linux-android35-clang++"
STRIP="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip"
READELF="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf"

if [ ! -x "$WORK/glslang/bin/glslangValidator" ]; then
    rm -rf "$WORK/glslang"
    mkdir -p "$WORK/glslang"
    tar -xzf "$WORK/dl/glslang-${GLSLANG_VERSION}-linux-x86_64-release.tar.gz" -C "$WORK/glslang" --strip-components=0
    # the release tarball has bin/ at the top
    if [ ! -x "$WORK/glslang/bin/glslangValidator" ] && [ -d "$WORK/glslang/glslang-${GLSLANG_VERSION}-linux-x86_64" ]; then
        mv "$WORK/glslang/glslang-${GLSLANG_VERSION}-linux-x86_64"/* "$WORK/glslang/" || true
    fi
fi
[ -x "$WORK/glslang/bin/glslangValidator" ] || { echo "glslangValidator missing after extract"; find "$WORK/glslang" -name glslangValidator -type f | head; exit 1; }

venv_ok() {
    [ -x "$WORK/venv/bin/meson" ] && [ -x "$WORK/venv/bin/python" ] || return 1
    "$WORK/venv/bin/python" -c "import importlib.metadata as m, sys; sys.exit(0 if (m.version('meson'), m.version('Mako'), m.version('PyYAML')) == ('${MESON_VERSION}', '${MAKO_VERSION}', '${PYYAML_VERSION}') else 1)" 2>/dev/null
}
if ! venv_ok; then
    rm -rf "$WORK/venv"
    python3 -m venv "$WORK/venv"
    "$WORK/venv/bin/pip" install -q "meson==${MESON_VERSION}" "mako==${MAKO_VERSION}" "pyyaml==${PYYAML_VERSION}"
fi
venv_ok || { echo "venv packages do not match meson==$MESON_VERSION mako==$MAKO_VERSION pyyaml==$PYYAML_VERSION"; exit 1; }
echo "toolchain: $("$CC" --version | head -n 1); glslang: $("$WORK/glslang/bin/glslangValidator" --version | head -n 1); meson: $("$WORK/venv/bin/meson" --version)"

# Probe binaries do not depend on the Mesa compile.
"$CC" -shared -fPIC -O2 -Wl,-soname,libvkprop.so -Wl,--build-id=none \
    -Wl,--version-script,"$HERE/vkprop.map" \
    -o "$WORK/out/libvkprop.so" "$HERE/vkprop.c" -ldl
"$CC" -O2 -Wl,--build-id=none -o "$WORK/out/radv-probe" "$HERE/radv-probe.c" -lvulkan -llog
"$STRIP" --strip-unneeded "$WORK/out/libvkprop.so" "$WORK/out/radv-probe"
python3 "$HERE/check-radv-elf.py" --strip-rpath "$WORK/out/libvkprop.so" >/dev/null || true
# libvkprop is not the ICD; only confirm it has no RUNPATH and the versioned symbol in .dynsym (--dyn-syms without -s).
"$READELF" --dyn-syms "$WORK/out/libvkprop.so" | grep -q '__system_property_find' \
    || { echo "libvkprop.so did not export __system_property_find"; exit 1; }

if [ ! -f "$WORK/mesa-${MESA_VERSION}/meson.build" ]; then
    tar -xJf "$WORK/dl/mesa-${MESA_VERSION}.tar.xz" -C "$WORK"
fi
MESA="$WORK/mesa-${MESA_VERSION}"
# libdrm comes from Mesa's wrap. If the tarball is already cached, pin it; meson downloads it otherwise.
DRM_TAR="$MESA/subprojects/packagecache/libdrm-${LIBDRM_VERSION}.tar.xz"
if [ ! -f "$DRM_TAR" ] && [ -f "$WORK/mesa-${MESA_VERSION}/subprojects/packagecache/libdrm-${LIBDRM_VERSION}.tar.xz" ]; then
    DRM_TAR="$WORK/mesa-${MESA_VERSION}/subprojects/packagecache/libdrm-${LIBDRM_VERSION}.tar.xz"
fi

python3 - "$MESA/src/amd/vulkan/meson.build" << 'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
text = p.read_text()
# --exclude-libs,ALL hides HMI when it comes from a static archive, and --gc-sections
# then drops the driver. SONAME only.
text2 = text.replace("'-Wl,--exclude-libs,ALL',", "")
needle = "ld_args_build_id, ld_args_bsymbolic, ld_args_gc_sections, vulkan_icd_link_args,"
insert = needle + "\n    '-Wl,-soname,vulkan.radeon.so',"
if "-soname,vulkan.radeon.so" not in text2:
    if needle not in text2:
        raise SystemExit("meson.build link_args pattern not found")
    text2 = text2.replace(needle, insert, 1)
    print("patched SONAME link args")
if text2 != text:
    p.write_text(text2)
    print("meson.build link args updated")
PY

# zlib.pc so meson does not pick the host libzstd/libudev/cairo.
mkdir -p "$WORK/pc"
cat > "$WORK/pc/zlib.pc" << EOF
prefix=${NDK}/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr
libdir=\${prefix}/lib/x86_64-linux-android/35
includedir=\${prefix}/include
Name: zlib
Description: NDK zlib
Version: 1.3
Libs: -L\${libdir} -lz
Cflags: -I\${includedir}
EOF

cat > "$WORK/radv-android.ini" << EOF
[binaries]
ar = '${NDK}/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-ar'
c = '${CC}'
cpp = ['${CXX}', '-fno-exceptions', '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '--start-no-unused-arguments', '-static-libstdc++', '--end-no-unused-arguments']
c_ld = 'lld'
cpp_ld = 'lld'
strip = '${STRIP}'
pkg-config = 'pkg-config'

[properties]
sys_root = '${NDK}/toolchains/llvm/prebuilt/linux-x86_64/sysroot'
pkg_config_libdir = ['${WORK}/pc']
needs_exe_wrapper = true

[host_machine]
system = 'android'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
EOF

export PATH="$WORK/glslang/bin:$WORK/venv/bin:$PATH"
export SOURCE_DATE_EPOCH=0
BUILD="$WORK/radv-build"
if [ ! -f "$BUILD/build.ninja" ]; then
    meson setup "$BUILD" "$MESA" \
        --cross-file "$WORK/radv-android.ini" \
        -Dplatforms=android \
        -Dplatform-sdk-version=35 \
        -Dandroid-stub=true \
        -Dandroid-libbacktrace=disabled \
        -Degl=disabled \
        -Dgallium-drivers= \
        -Dvulkan-drivers=amd \
        -Dllvm=disabled \
        -Dzstd=disabled \
        -Dallow-fallback-for=libdrm \
        -Dlibdrm:default_library=static \
        -Dlibdrm:intel=disabled \
        -Dlibdrm:radeon=disabled \
        -Dlibdrm:nouveau=disabled \
        -Dlibdrm:vmwgfx=disabled \
        -Dlibdrm:freedreno=disabled \
        -Dlibdrm:vc4=disabled \
        -Dlibdrm:etnaviv=disabled \
        -Dlibdrm:cairo-tests=disabled \
        -Dlibdrm:man-pages=disabled \
        -Dlibdrm:valgrind=disabled \
        -Dlibdrm:tests=false \
        -Dbuildtype=release \
        -Db_ndebug=true \
        -Dc_args="-ffile-prefix-map=${MESA}=mesa -ffile-prefix-map=${WORK}=work" \
        -Dcpp_args="-ffile-prefix-map=${MESA}=mesa -ffile-prefix-map=${WORK}=work"
fi
if [ -f "$DRM_TAR" ]; then
    [ "$(sha "$DRM_TAR")" = "$LIBDRM_SHA256" ] || { echo "libdrm tarball sha256 mismatch"; exit 1; }
    echo "ok libdrm ${LIBDRM_VERSION}"
else
    echo "libdrm tarball not in packagecache yet; meson setup should have fetched it"
    DRM_TAR=$(find "$MESA/subprojects" -name "libdrm-${LIBDRM_VERSION}.tar.xz" | head -1)
    [ -n "$DRM_TAR" ] && [ "$(sha "$DRM_TAR")" = "$LIBDRM_SHA256" ] || { echo "libdrm ${LIBDRM_VERSION} not pinned"; exit 1; }
fi

meson compile -C "$BUILD"
meson install -C "$BUILD" --destdir "$WORK/radv-install" --tags runtime
SO=$(find "$WORK/radv-install" -name 'libvulkan_radeon.so' -o -name 'vulkan.radeon.so' | head -1)
[ -n "$SO" ] || { echo "installed RADV library not found"; find "$WORK/radv-install" -name '*.so' | head; exit 1; }
cp -f "$SO" "$WORK/out/vulkan.radeon.so"
# Android's libvulkan.so loads the HAL module via its GLOBAL HMI symbol in .dynsym
# (vk_icdGetInstanceProcAddr is a LOCAL symbol reached via HMI.common.methods->open -> radv_hal_open).
# --strip-unneeded drops .symtab (~1.01 MB saved: 19,194,688 B -> 18,182,336 B) while keeping .dynsym intact.
"$STRIP" --strip-unneeded "$WORK/out/vulkan.radeon.so"
python3 "$HERE/check-radv-elf.py" --strip-rpath "$WORK/out/vulkan.radeon.so"
python3 "$HERE/check-radv-elf.py" "$WORK/out/vulkan.radeon.so"
sz=$(stat -c%s "$WORK/out/vulkan.radeon.so")
[ "$sz" -gt 5000000 ] || { echo "vulkan.radeon.so is unexpectedly small ($sz bytes)"; exit 1; }
# Read .dynsym only (--dyn-syms without -s; -s also dumps .symtab). Avoid grep -q under pipefail (SIGPIPE).
dynsyms=$("$READELF" --dyn-syms "$WORK/out/vulkan.radeon.so")
printf '%s\n' "$dynsyms" | grep -Eq '[[:space:]]GLOBAL[[:space:]]+DEFAULT[[:space:]]+[0-9]+[[:space:]]+HMI([[:space:]]|$)' \
    || { echo "vulkan.radeon.so does not export GLOBAL HMI in .dynsym"; exit 1; }

{
    echo "===== dynamic ====="
    "$READELF" -d "$WORK/out/vulkan.radeon.so"
    echo "===== dyn-syms HMI ====="
    "$READELF" --dyn-syms "$WORK/out/vulkan.radeon.so" | grep -E '[[:space:]]HMI([[:space:]]|$)' || true
} | tee "$WORK/out/readelf.txt"

pin_one() { # name file var [alt_var]
    local got
    got=$(sha "$2")
    if [ -z "$3" ]; then
        echo "$1=$got"
    elif [ "$got" = "$3" ] || { [ -n "${4:-}" ] && [ "$got" = "$4" ]; }; then
        echo "ok $1 $got"
    else
        echo "$1 mismatch: built $got pinned $3"
        exit 1
    fi
}
pin_one RADV_SHA "$WORK/out/vulkan.radeon.so" "$RADV_SHA" "$RADV_STRIP_UNNEEDED_SHA"
pin_one PROBE_SHA "$WORK/out/radv-probe" "$PROBE_SHA"
pin_one VKPROP_SHA "$WORK/out/libvkprop.so" "$VKPROP_SHA"
echo "out $WORK/out"
