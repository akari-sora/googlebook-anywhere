#!/usr/bin/env bash
# Reproducibility: build with the developer settings of a known image (serial-debug default, menu editor on, the same
# AVB key and adb key) and compare it with that image: GPT identity, every partition, every ESP file, boot binding.
# usage: GBOS_BASE=... GBOS_INITRD=... GBOS_OTATOOLS=... GBOS_AVB_KEY=... GBOS_INPUTS=... GBOS_ADB_PUBKEY=... GBOS_KMOD_AC=... \
#        tests/builder/repro.sh <reference.img> <new, empty GBOS_OUT> [allowed ESP differences] [ESP files removed since]
#        (both comma separated)
# Against final26 (2026-10-12): loader/loader.conf differs (auto-firmware no) and loader/entries/lapis.conf is removed
# (the plain entry is no longer built). Since v0.2 the KANO profile also ships ac.ko (GBOS_KMOD_AC, made by
# builder/kmod/build-ac.sh), so vendor, its vbmeta digest and the initrd's bootconfig differ from a v0.1 reference.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REF=${1:?reference image}; export GBOS_OUT=${2:?new output dir}; ALLOW=${3:-}; REMOVED=${4:-}
GBOS_DEFAULT_ENTRY=lapis-debug.conf GBOS_LOADER_EDITOR=yes bash "$ROOT/builder/build.sh"
python3 "$ROOT/tests/builder/compare-images.py" "$REF" "$GBOS_OUT/image/${GBOS_IMAGE_NAME:-googlebook-kano.img}" "--allow-esp-diff=$ALLOW" "--allow-esp-removed=$REMOVED"
