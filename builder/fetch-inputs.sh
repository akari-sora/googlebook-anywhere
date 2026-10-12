#!/usr/bin/env bash
# Fetch the third-party inputs the builder needs besides the base image, each checked against a pinned SHA-256:
#   <dir>/firmware   KANO device firmware (RAM-loaded by the drivers, nothing is flashed), laid out like /lib/firmware,
#                    from linux-firmware and sof-bin releases
#   <dir>/audio-src  the profile's ChromeOS audio configuration (profiles/<model>/audio-src.list), from board-overlays
# usage: builder/fetch-inputs.sh <dir> [profile dir]     then: export GBOS_INPUTS=<dir>
set -euo pipefail
OUTDIR=${1:?usage: fetch-inputs.sh <dir> [profile dir]}
PROFILE=${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../profiles/kano" && pwd)}
DEST="$OUTDIR/firmware"
BO=https://chromium.googlesource.com/chromiumos/overlays/board-overlays
LF=https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git/plain
LF_TAG=20260810
SOF_URL=https://github.com/thesofproject/sof-bin/releases/download/v2025.12.2/sof-bin-2025.12.2.tar.gz
SOF_SHA=533f63e3a6d94c09ce05a782657b675fa683ff20787c0979226cf563ec79f517
# <path in DEST> <path in linux-firmware> <sha256>
LF_FILES="
i915/adlp_dmc.bin i915/adlp_dmc.bin 3516de2e134ddcf3b319c75d2e437779fecbd58cbb77234bd6f297c544e92ccb
i915/adlp_guc_70.bin i915/adlp_guc_70.bin b7a800cbdcdf687e7b1c81f3316d040ab75fa3e5c698ebc356f077af17462ef8
i915/tgl_huc.bin i915/tgl_huc.bin dbb1316bac13a76427ffa7827e52b5b9affe8a4cadd23dc22e2a54e35c53bdad
iwlwifi-so-a0-gf-a0-89.ucode intel/iwlwifi/iwlwifi-so-a0-gf-a0-89.ucode 7b53254bb28115f91b6eaa33b1f9a3526e32966105eaac587e259dd17790f384
iwlwifi-so-a0-gf-a0.pnvm intel/iwlwifi/iwlwifi-so-a0-gf-a0.pnvm 2aee7592ddbd6c88f1aa51be4ada188fce44765405e52d7f3056b46e62cf3f91
intel/ibt-0040-0041.sfi intel/ibt-0040-0041.sfi 8dcfb3a7c1592c11c3e80505d21ee78dbde3de0c830d8e3e856371c37717b62c
intel/ibt-0040-0041.ddc intel/ibt-0040-0041.ddc fe272982577efdc289cfe3e8bedafca607c0b7d6c9df1aab1880e22ef930d077
"
# <path in DEST> <path in the sof-bin tarball> <sha256>
SOF_FILES="
intel/sof/community/sof-adl.ri sof-bin-2025.12.2/sof/community/sof-tgl.ri e3489dcb277e87eee8bc091ab7d51163a63c85541c22522775122b54263769f3
intel/sof-tplg/sof-adl-max98373-nau8825.tplg sof-bin-2025.12.2/sof-tplg/sof-adl-max98373-nau8825.tplg 4beb29400d9beed0c80bcc11b3f4e82aad70a54d6e851638c81eb087a10d4de5
"
sha() { sha256sum "$1" | cut -d' ' -f1; }
mkdir -p "$DEST"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/gbos-fw.XXXXXX"); trap 'rm -rf "$TMP"' EXIT
echo "$LF_FILES" | while read -r dst src want; do
    [ -n "$dst" ] || continue
    if [ -f "$DEST/$dst" ] && [ "$(sha "$DEST/$dst")" = "$want" ]; then echo "ok (cached) $dst"; continue; fi
    curl -fsSL --retry 3 --max-time 300 -o "$TMP/f" "$LF/$src?h=$LF_TAG"
    [ "$(sha "$TMP/f")" = "$want" ] || { echo "sha256 mismatch: linux-firmware $LF_TAG $src"; exit 1; }
    mkdir -p "$(dirname "$DEST/$dst")"; mv "$TMP/f" "$DEST/$dst"; echo "ok $dst"
done
curl -fsSL --retry 3 --max-time 600 -o "$TMP/sof.tgz" "$SOF_URL"
[ "$(sha "$TMP/sof.tgz")" = "$SOF_SHA" ] || { echo "sha256 mismatch: $SOF_URL"; exit 1; }
echo "$SOF_FILES" | while read -r dst src want; do
    [ -n "$dst" ] || continue
    tar -xzf "$TMP/sof.tgz" -C "$TMP" "$src"
    f=$(readlink -f "$TMP/$src")
    [ "$(sha "$f")" = "$want" ] || { echo "sha256 mismatch: $src"; exit 1; }
    mkdir -p "$(dirname "$DEST/$dst")"; cp "$f" "$DEST/$dst"; echo "ok $dst"
done
commit=$(sed -n 's/.* at commit \([0-9a-f]\{40\}\).*/\1/p' "$PROFILE/audio-src.list" | head -1)
[ -n "$commit" ] || { echo "no pinned commit in $PROFILE/audio-src.list"; exit 1; }
mkdir -p "$OUTDIR/audio-src"
grep -v '^#' "$PROFILE/audio-src.list" | while read -r dst src want; do
    [ -n "$dst" ] || continue
    if [ -f "$OUTDIR/audio-src/$dst" ] && [ "$(sha "$OUTDIR/audio-src/$dst")" = "$want" ]; then echo "ok (cached) audio-src/$dst"; continue; fi
    curl -fsSL --retry 3 --max-time 120 "$BO/+/$commit/$src?format=TEXT" | base64 -d > "$TMP/a"
    [ "$(sha "$TMP/a")" = "$want" ] || { echo "sha256 mismatch: board-overlays $commit $src"; exit 1; }
    mv "$TMP/a" "$OUTDIR/audio-src/$dst"; echo "ok audio-src/$dst"
done
echo "inputs ready in $OUTDIR (export GBOS_INPUTS=$OUTDIR)"
