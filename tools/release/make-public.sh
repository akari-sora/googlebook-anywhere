#!/usr/bin/env bash
# Export the public repository tree (googlebook-anywhere) from a commit of this private repository: everything except
# the research notes and the developer's personal settings, then scan the result for personal data and refuse on a hit.
# A clean personal-data scan covers the public export tree only, not the private checkout and not an unscanned installer image.
# usage: tools/release/make-public.sh <commit> <empty destination dir>
# The scan patterns live in tools/release/private-patterns.txt (one extended regex per line), which is never exported.
# The export is built and scanned in a temporary directory; the destination is filled only when every check passed.
# Any scan error (bad pattern, unreadable file) fails the export; only grep's "no match" counts as clean.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
C=${1:?commit}; DEST=${2:?destination}
if [ -e "$DEST" ] && { [ ! -d "$DEST" ] || [ -n "$(ls -A "$DEST")" ]; }; then echo "destination must be new or an empty directory: $DEST"; exit 2; fi
PATTERNS="$ROOT/tools/release/private-patterns.txt"
[ -s "$PATTERNS" ] || { echo "missing $PATTERNS (patterns of personal data to scan for)"; exit 2; }
# an empty line would match everything and a comment line would be taken as a pattern: refuse both
if grep -nE '^(#|[[:space:]]*$)' "$PATTERNS" > /dev/null; then echo "$PATTERNS: empty or comment lines are not allowed"; exit 2; fi
rc=0; grep -E -f "$PATTERNS" /dev/null || rc=$?
[ "$rc" = 1 ] || { echo "$PATTERNS: grep cannot use these patterns (exit $rc)"; exit 2; }
SHA=$(git -C "$ROOT" rev-parse --verify --quiet "$C^{commit}") || { echo "not a commit: $C"; exit 2; }
# Reads the commit with git ls-tree / git archive only. Does not modify the source repository.
# A tree that already carries the public-export marker is an export, not a private source: refuse it.
if git -C "$ROOT" cat-file -e "$SHA:tools/release/PUBLIC-EXPORT" 2>/dev/null; then
    echo "refusing: $SHA already contains tools/release/PUBLIC-EXPORT"
    exit 1
fi
EXCLUDE=(research tools/harness/kano-env.local.sh tools/release/private-patterns.txt)

TMP=$(mktemp -d "${TMPDIR:-/tmp}/gbos-public.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
TREE="$TMP/tree"; mkdir "$TREE"

# 1. the file list, straight from the commit (NUL separated, so no name is ever split or globbed)
paths=(); : > "$TMP/names"
while IFS= read -r -d '' entry; do
    meta=${entry%%$'\t'*}; name=${entry#*$'\t'}
    mode=${meta%% *}
    skip=0
    for x in "${EXCLUDE[@]}"; do case "$name" in "$x"|"$x"/*) skip=1 ;; esac; done
    [ "$skip" = 0 ] || continue
    case "$mode" in
        100644|100755) ;;
        *) echo "refusing: $name is not a regular file in $SHA (mode $mode: symlink or submodule)"; exit 1 ;;
    esac
    case "$name" in *[[:cntrl:]]*) echo "refusing: a file name contains a control character"; exit 1 ;; esac
    paths+=("$name"); printf '%s\n' "$name" >> "$TMP/names"
done < <(git -C "$ROOT" ls-tree -r -z --full-tree "$SHA")
[ "${#paths[@]}" -gt 0 ] || { echo "nothing to export from $SHA"; exit 1; }

# 2. export exactly those paths (literal pathspecs: no glob or magic interpretation)
git -C "$ROOT" --literal-pathspecs archive --format=tar "$SHA" -- "${paths[@]}" | tar -C "$TREE" -xf -

# 3. checks on the extracted tree
other=$(find "$TREE" ! -type f ! -type d)
[ -z "$other" ] || { echo "refusing: the export contains something that is not a regular file or directory"; exit 1; }
nfiles=$(find "$TREE" -type f | wc -l | tr -d ' ')
[ "$nfiles" = "${#paths[@]}" ] || { echo "refusing: exported $nfiles files, expected ${#paths[@]}"; exit 1; }
while IFS= read -r -d '' f; do
    # the only binary files allowed: plain PNG screenshots in docs/images (no text/EXIF chunks; png-ok.py)
    case "${f#"$TREE"/}" in
        docs/images/*|*.[pP][nN][gG])
            why=$(python3 "$ROOT/tools/release/png-ok.py" --pinned "$TREE/tools/release/published-images.sha256" --as "${f#"$TREE"/}" "$f") \
                || { echo "refusing: ${f#"$TREE"/}: ${why#*: }"; exit 1; }
            case "${f#"$TREE"/}" in
                docs/images/*/*) echo "refusing: ${f#"$TREE"/}: only docs/images/*.png screenshots are allowed"; exit 1 ;;
                docs/images/*.png) ;;
                *) echo "refusing: ${f#"$TREE"/}: only docs/images/*.png screenshots are allowed"; exit 1 ;;
            esac ;;
        *)
            if ! LC_ALL=C tr -d '\000' < "$f" | cmp -s - "$f"; then
                echo "refusing: binary file (contains NUL bytes): ${f#"$TREE"/}"; exit 1
            fi ;;
    esac
done < <(find "$TREE" -type f -print0)
for x in "${EXCLUDE[@]}"; do [ ! -e "$TREE/$x" ] || { echo "excluded path present: $x"; exit 1; }; done

# Positive marker for the public tree: the source commit SHA. The scan below covers this file.
mkdir -p "$TREE/tools/release"
printf '%s\n' "$SHA" > "$TREE/tools/release/PUBLIC-EXPORT"

scan() {   # scan <what> <grep args...>: 1 = clean, 0 = personal data (file:line only, never the match), else error
    local what=$1 out rc=0; shift
    out=$(grep -iHnE -f "$PATTERNS" "$@") || rc=$?
    case "$rc" in
        1) return 0 ;;
        0) echo "personal data found in the export $what (fix the source, commit, export again):"
           printf '%s\n' "$out" | cut -d: -f1,2 | sed -e "s|^$TREE/||" -e "s|^$TMP/names:|file name #|"
           exit 1 ;;
        *) echo "personal-data scan of $what failed (grep exit $rc); refusing"; exit 1 ;;
    esac
}
scan "file contents" -r -- "$TREE"
scan "file names" -- "$TMP/names"

# the harness test that checks for personal data must not carry digests of the personal values
T="$TREE/tests/harness/test_no_personal_data.py"
if [ -e "$T" ]; then
    rc=0; grep -qE '[0-9A-Fa-f]{64}' "$T" || rc=$?
    case "$rc" in
        1) ;;
        0) echo "refusing: tests/harness/test_no_personal_data.py contains a 64-hex constant (a digest of a personal value?)"; exit 1 ;;
        *) echo "cannot read $T (grep exit $rc)"; exit 1 ;;
    esac
fi

# 4. publish
if [ -d "$DEST" ]; then rmdir "$DEST"; fi
mkdir -p "$(dirname "$DEST")"
mv "$TREE" "$DEST"
nfiles=$(find "$DEST" -type f | wc -l | tr -d ' ')
echo "exported $nfiles files from $SHA to $DEST; personal-data scan clean"
