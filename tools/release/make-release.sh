#!/usr/bin/env bash
# Make the installer release archive from a commit: what a user needs to run install/install.sh (no images, no
# third-party files, no research notes, no developer harness). usage: tools/release/make-release.sh <version> [commit]
# The version must look like v1.2.3. An existing archive of that version is never overwritten; the archive and its
# checksum are built and checked in a temporary directory and moved into out/release/ only when every check passed.
set -euo pipefail
V=${1:?version, e.g. v0.1.0}; C=${2:-HEAD}
[[ "$V" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must look like v1.2.3: $V"; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHA=$(git -C "$ROOT" rev-parse --verify --quiet "$C^{commit}") || { echo "not a commit: $C"; exit 2; }
OUT="$ROOT/out/release"; mkdir -p "$OUT"
N="googlebook-anywhere-$V"
for f in "$OUT/$N.tar.gz" "$OUT/$N.tar.gz.sha256"; do
    [ ! -e "$f" ] || { echo "refusing to overwrite $f (remove it first if you really mean to rebuild $V)"; exit 1; }
done
TMP=$(mktemp -d "$ROOT/out/.release-tmp.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
git -C "$ROOT" diff --quiet "$SHA" -- || echo "note: the working tree differs from $C; the archive is made from $SHA"
git -C "$ROOT" archive --format=tar --prefix="$N/" "$SHA" -- README.md LICENSE base builder install profiles docs \
    tests/builder tests/install tests/stick | gzip -n -9 > "$TMP/$N.tar.gz"

# content checks: only regular files and directories, nothing that looks like an image or a binary
tar -tvzf "$TMP/$N.tar.gz" > "$TMP/list"
if grep -vE '^[-d]' "$TMP/list" > /dev/null; then echo "refusing: the archive contains a link or special file"; exit 1; fi
tar -tzf "$TMP/$N.tar.gz" > "$TMP/names"
if grep -E '\.(img|bin|zip|pem|apex|ko)$' "$TMP/names"; then echo "refusing: binary-looking file in the archive"; exit 1; fi
mkdir "$TMP/x"; tar -C "$TMP/x" -xzf "$TMP/$N.tar.gz"
while IFS= read -r -d '' f; do
    if ! LC_ALL=C tr -d '\000' < "$f" | cmp -s - "$f"; then echo "refusing: binary file in the archive: ${f#"$TMP/x/"}"; exit 1; fi
done < <(find "$TMP/x" -type f -print0)

if command -v sha256sum > /dev/null; then ( cd "$TMP" && sha256sum "$N.tar.gz" > "$N.tar.gz.sha256" )
else ( cd "$TMP" && shasum -a 256 "$N.tar.gz" > "$N.tar.gz.sha256" ); fi

for f in "$N.tar.gz" "$N.tar.gz.sha256"; do
    [ ! -e "$OUT/$f" ] || { echo "refusing to overwrite $OUT/$f"; exit 1; }
done
mv "$TMP/$N.tar.gz" "$OUT/$N.tar.gz"
mv "$TMP/$N.tar.gz.sha256" "$OUT/$N.tar.gz.sha256"
echo "$OUT/$N.tar.gz ($(wc -c < "$OUT/$N.tar.gz" | tr -d ' ') bytes, $(wc -l < "$TMP/names" | tr -d ' ') entries, from $SHA)"
cat "$OUT/$N.tar.gz.sha256"
