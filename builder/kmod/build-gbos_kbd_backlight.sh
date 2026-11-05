#!/usr/bin/env bash
# Build the KANO keyboard LED driver against the pinned lapis GKI and its stock
# cros-ec-proto vendor module. No kernel symbol protections are changed.
# WORK uses build-ac.sh's ksrc/clang/rust/bin/gki.config/vmlinux.symvers layout;
# an independently prepared out-of-tree kernel output may live at WORK/gki.
# Set PAHOLE and its library path as needed for the GKI's pinned BTF config.
# usage: build-gbos_kbd_backlight.sh <prepared work dir> <stock cros-ec-proto.ko> <output dir>
set -euo pipefail
WORK=${1:?usage: build-gbos_kbd_backlight.sh <prepared work dir> <stock cros-ec-proto.ko> <output dir>}
PROVIDER=${2:?usage: build-gbos_kbd_backlight.sh <prepared work dir> <stock cros-ec-proto.ko> <output dir>}
OUTDIR=${3:?usage: build-gbos_kbd_backlight.sh <prepared work dir> <stock cros-ec-proto.ko> <output dir>}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KSRC_COMMIT=3938c8d97142d5cf31543aa96f864a2cf832dda0
CONFIG_SHA=046971f3fb011e5ec31b4ddad2610ee4a459d2000aba4520b10faafe6a27be40
SYMVERS_SHA=db64611c9710f624c60f57219649645d5539c392579a7a8d8d5066b1cc099598
PROVIDER_SHA=5ddce2fcfe24e9f1451783bb42d7bbdb23b99955fae211c8bc1ae8f32098ad48
KBD_BACKLIGHT_KO_SHA=39413aefb76ea0a4c28b2bfb9d7b93b0551bfc7bb6eff88918cd4c8335f3ed5a
VERMAGIC_TAIL="SMP preempt mod_unload modversions "
sha() { sha256sum "$1" | cut -d' ' -f1; }
tree_sha() {
    ( cd "$1" && { find . -type f -print0 | sort -z | xargs -0 sha256sum; find . -type l -printf '%p -> %l\n' | sort; } ) | sha256sum | cut -d' ' -f1
}
die() { echo "$*" >&2; exit 2; }
case "$WORK$PROVIDER$OUTDIR" in *[[:space:]:]*) die "work, provider and output paths must not contain spaces or ':'" ;; esac
[ "$(uname -sm)" = "Linux x86_64" ] || die "build-gbos_kbd_backlight.sh runs on Linux x86-64 only"
[ ! -e "$OUTDIR/gbos_kbd_backlight.ko" ] && [ ! -L "$OUTDIR/gbos_kbd_backlight.ko" ] || die "refusing to replace an existing gbos_kbd_backlight.ko"
WORK=$(cd "$WORK" && pwd -P)
K=$(cd "$WORK/ksrc" && pwd -P)
KOUT=$K
[ ! -d "$WORK/gki" ] || KOUT="$WORK/gki"
[ "$(git -C "$K" rev-parse HEAD)" = "$KSRC_COMMIT" ] || die "kernel source revision differs"
[ -z "$(git -C "$K" status --porcelain --untracked-files=no)" ] || die "kernel source has tracked changes"
[ "$(sha "$WORK/gki.config")" = "$CONFIG_SHA" ] && cmp -s "$KOUT/.config" "$WORK/gki.config" || die "kernel configuration differs"
[ "$(sha "$WORK/vmlinux.symvers")" = "$SYMVERS_SHA" ] && cmp -s "$KOUT/Module.symvers" "$WORK/vmlinux.symvers" || die "GKI symbol versions differ"
[ "$(sha "$PROVIDER")" = "$PROVIDER_SHA" ] || die "stock EC provider differs"
[ -f "$KOUT/include/generated/autoconf.h" ] || die "kernel modules_prepare is missing"
[ "$(tree_sha "$WORK/clang")" = 66d6efb5a82c10e3750104276b0140e852744ec4234487aabe5ce21af08341ca ] || die "clang toolchain differs"
[ "$(tree_sha "$WORK/rust")" = c1f13faac52876c5875c0b86bf630975e6991c6080f2579cbc73840f8e78e757 ] || die "Rust toolchain differs"
[ "$(sha "$WORK/bin/bindgen")" = 066435d1670d6680a4363ad4e3a12e7d6b9dc866fa956b24069e5d2cd08d0258 ] || die "bindgen differs"
[ "$(sha "$WORK/bin/bc")" = 3bee4353161cbe49a417fda21c4ea932f509ba63b3f3d56c4cac447731673997 ] || die "bc differs"
export PATH="$WORK/clang/bin:$WORK/rust/bin:$WORK/bin:$PATH" LIBCLANG_PATH="$WORK/clang/lib"
E=$(mktemp -d "$WORK/ext-kbd.XXXXXX")
PUBLISH=
trap 'rm -rf "$E"; [ -z "$PUBLISH" ] || rm -f "$PUBLISH"' EXIT
cp "$HERE/gbos_kbd_backlight.c" "$E/"
printf 'obj-m := gbos_kbd_backlight.o\n' > "$E/Kbuild"
# A separate, pinned vendor provider: never label this export as vmlinux-owned.
printf '0x53f88a75\tcros_ec_cmd_xfer_status\tcros-ec-proto\tEXPORT_SYMBOL\t\n' > "$E/vendor.symvers"
nice -n 19 make -s -C "$K" O="$KOUT" ARCH=x86_64 LLVM=1 HOSTCC=clang HOSTLD=ld.lld \
    "RUSTC=$WORK/rust/bin/rustc" "BINDGEN=$WORK/bin/bindgen" \
    LOCALVERSION=-android17-5 "-j$(nproc)" KBUILD_BUILD_TIMESTAMP=0 KBUILD_BUILD_USER=gbos KBUILD_BUILD_HOST=gbos \
    M="$E" "KBUILD_EXTRA_SYMBOLS=$E/vendor.symvers" \
    "KCFLAGS=-ffile-prefix-map=$WORK/=/gbos-kmod/ -ffile-prefix-map=$K/=/gbos-kernel/ -ffile-prefix-map=$E=/gbos-kbd" modules
cmp -s "$KOUT/.config" "$WORK/gki.config" || die "build changed the pinned GKI configuration"
llvm-strip --strip-debug -o "$E/gbos_kbd_backlight.stripped.ko" "$E/gbos_kbd_backlight.ko"
python3 "$HERE/check-ko.py" "$E/gbos_kbd_backlight.stripped.ko" "$WORK/vmlinux.symvers" "$VERMAGIC_TAIL" --kano-backlight-provider "$PROVIDER"
got=$(sha "$E/gbos_kbd_backlight.stripped.ko")
[ "$got" = "$KBD_BACKLIGHT_KO_SHA" ] || die "gbos_kbd_backlight.ko differs from the pinned build ($got)"
mkdir -p "$OUTDIR"
PUBLISH=$(mktemp "$OUTDIR/.gbos_kbd_backlight.XXXXXX")
cp "$E/gbos_kbd_backlight.stripped.ko" "$PUBLISH"
chmod 0644 "$PUBLISH"
# Publish without replacing an existing file, including a dangling symlink.
ln "$PUBLISH" "$OUTDIR/gbos_kbd_backlight.ko"
echo "gbos_kbd_backlight.ko $got -> $OUTDIR/gbos_kbd_backlight.ko"
