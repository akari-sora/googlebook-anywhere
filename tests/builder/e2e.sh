#!/usr/bin/env bash
# End to end as a new user: official zip -> new AVB key -> base -> upstream inputs -> image with the default settings
# (quiet entry, no adb, menu editor off) -> both QEMU self-tests.
# usage: tests/builder/e2e.sh <new or empty work dir> [install.sh args, e.g. --zip already-downloaded.zip]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W=${1:?work dir}; shift
if [ -e "$W" ] && [ -n "$(ls -A "$W")" ]; then echo "e2e needs a new or empty work dir (nothing may be reused): $W"; exit 2; fi
START=$(date +%s)
bash "$ROOT/install/install.sh" --work "$W" "$@"
L="$W/build/image.log"; I="$W/build/image/googlebook-kano.img"
# everything below must come from this run
[ "$(stat -c %Y "$I")" -ge "$START" ] && [ "$(stat -c %Y "$W/base/kano-real.img")" -ge "$START" ] || { echo "stale result"; exit 1; }
grep -q "reference key: no" "$W/base/build-info.txt" || { echo "base was not signed with a new key"; exit 1; }
grep -q "^IMAGE OK " "$L" && ! grep -q "^FAIL " "$L" && grep -q "^PASS no kano-adb" "$L" \
    && grep -q "^PASS chain: default entry keeps the serial console silent" "$L" && grep -q "^PASS chain: Android wrote /data" "$L"
printf 'default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n' | cmp -s - "$W/build/image/loader.conf"
grep -q "^adb-key none$" "$W/build/params"
echo "E2E OK: $(grep -c '^PASS ' "$L") checks passed, fresh base and image from this run"
