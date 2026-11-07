#!/usr/bin/env bash
# Build the pinned HX370 shim with an already installed Android NDK; no downloads.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd -P)
: "${ANDROID_NDK_HOME:?set ANDROID_NDK_HOME to Android NDK 29.0.14206865}"
: "${1:?provide a new output path}"
out=$1
[[ ! -e "$out" && ! -L "$out" ]] || { echo 'output already exists' >&2; exit 1; }
ndk=$(cd -- "$ANDROID_NDK_HOME" && pwd -P)
grep -Fx 'Pkg.Revision = 29.0.14206865' "$ndk/source.properties" >/dev/null
cc="$ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/x86_64-linux-android35-clang++"
check() { printf '%s  %s\n' "$1" "$2" | sha256sum -c --status; }
check ad5ad02c7a1b75eb7fe763f2fe840f0659beafbe23cb1f0ebf0599ca270fb53b "$HERE/shim.cpp"
tmp=$(mktemp "${out}.tmp.XXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT
"$cc" -std=c++17 -O2 -Wall -Wextra -Werror -static-libstdc++ "$HERE/shim.cpp" -lbinder_ndk -llog -ldl -o "$tmp"
check 449c6ed1d4060d0f01430a97a6b9ebdc7af3a820058d93c51b1b3e2315d329cf "$tmp"
# Hard-link creation is atomic and fails if the requested output appears concurrently.
ln -- "$tmp" "$out"
