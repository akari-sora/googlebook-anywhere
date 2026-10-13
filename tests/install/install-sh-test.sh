#!/usr/bin/env bash
# install/docker-install.sh and install/install.sh without Docker, downloads or a real build.
#  1. (any OS) docker-install.sh with a fake `docker` that records its arguments: empty argument lists under set -u
#     (also with /bin/bash when it is the bash 3.2 that macOS ships), paths with spaces, refusals before anything is
#     created (root, ':' in a path, missing files, unknown arguments), and the KVM device and group on Linux.
#  2. (Linux x86-64) install.sh in a copy of the repository whose base/build-base.sh, builder/fetch-inputs.sh and
#     builder/build.sh are stubs: what makes a finished base or image reusable, and what has to refuse reuse (a changed
#     or new source file, GBOS_DEFAULT_ENTRY, GBOS_LOADER_EDITOR, the adb key, unpinned firmware, a damaged base).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
T=$(mktemp -d "${TMPDIR:-/tmp}/gbos-install-test.XXXXXX"); trap 'rm -rf "$T"' EXIT
T=$(cd "$T" && pwd)   # the scripts print normalized paths (macOS TMPDIR ends in a slash)
export PYTHONDONTWRITEBYTECODE=1
pass=0; fail=0; skip=0
ok()  { echo "PASS $1"; pass=$((pass + 1)); }
bad() { echo "FAIL $1"; fail=$((fail + 1)); }
skp() { echo "SKIP $1"; skip=$((skip + 1)); }
finish() { echo "install script tests: $pass passed, $fail failed, $skip skipped"; [ "$fail" = 0 ]; exit; }
check() { if eval "$2"; then ok "$1"; else bad "$1 ($2)"; tail -n 5 <<< "$out" | sed 's/^/    /'; fi; }

# --- 1. docker-install.sh
mkdir -p "$T/fake"
cat > "$T/fake/docker" << 'STUB'
#!/bin/sh
# fake docker: one argument per line, one file per call
n=$(ls "$DOCKER_LOG" | wc -l | tr -d ' ')
for a in "$@"; do printf '%s\n' "$a"; done > "$DOCKER_LOG/call$n"
STUB
chmod +x "$T/fake/docker"
BASHES=(bash)   # plus the system bash when it is an old one (macOS: 3.2) and not already the bash in PATH
if [ -x /bin/bash ] && [ "$(command -v bash)" != /bin/bash ] && [ "$(/bin/bash -c 'echo ${BASH_VERSINFO[0]}')" -lt 5 ]; then
    BASHES+=(/bin/bash)
fi
echo "bash under test: $(for b in "${BASHES[@]}"; do printf '%s (%s) ' "$b" "$("$b" -c 'echo $BASH_VERSION')"; done)"
kvm=0
# docker-install.sh refuses to run the container as root; as root (CI) the cases use an ordinary uid:gid instead
DUSER="$(id -u):$(id -g)"; if [ "$(id -u)" = 0 ]; then DUSER=1000:1000; fi
if [ "$(uname -s)" = Linux ] && [ -r /dev/kvm ] && [ -w /dev/kvm ]; then kvm=1; fi
dexpect() {  # dexpect <bash> <name> <exit> <regex or ''> <docker-install args...>; output in $out, docker calls in $T/dl
    local sh=$1 name=$2 code=$3 re=$4 rc; shift 4
    rm -rf "$T/dl"; mkdir -p "$T/dl"
    out=$(cd "$T" && DOCKER_LOG="$T/dl" GBOS_DOCKER="$T/fake/docker" GBOS_DOCKER_USER="$DUSER" "$sh" "$ROOT/install/docker-install.sh" "$@" 2>&1 < /dev/null); rc=$?
    if [ "$rc" = "$code" ] && { [ -z "$re" ] || grep -Eq -- "$re" <<< "$out"; }; then ok "[$sh] $name"
    else bad "[$sh] $name (rc=$rc)"; tail -n 5 <<< "$out" | sed 's/^/    /'; fi
}
calls() { find "$T/dl" -type f | wc -l | tr -d ' '; }
has_line() { grep -qxF -- "$2" "$T/dl/$1"; }   # has_line <call file> <exact argument>
for sh in "${BASHES[@]}"; do
    rm -rf "$T/w 1" "$T/in put"; mkdir -p "$T/in put"; echo zip > "$T/in put/x y.zip"
    dexpect "$sh" "--work alone (empty argument arrays under set -u)" 0 "" --work "$T/w 1"
    check "[$sh] docker is called twice (build, run)" '[ "$(calls)" = 2 ] && has_line call0 build && has_line call1 run'
    check "[$sh] the work dir with a space is one mount argument" 'has_line call1 "$T/w 1:/work"'
    if [ "$kvm" = 1 ]; then
        check "[$sh] KVM: the device and its group go into the container" \
            'has_line call1 /dev/kvm && has_line call1 "$(stat -c %g /dev/kvm)" && grep -qx -- --group-add "$T/dl/call1" && ! has_line call1 --no-selftest'
    else
        check "[$sh] no KVM here: --no-selftest is passed once, no device" \
            '[ "$(grep -cx -- --no-selftest "$T/dl/call1")" = 1 ] && ! has_line call1 --device'
    fi
    dexpect "$sh" "--no-selftest given" 0 "" --work "$T/w 1" --no-selftest
    check "[$sh] --no-selftest appears once and no KVM device is passed" \
        '[ "$(grep -cx -- --no-selftest "$T/dl/call1")" = 1 ] && ! has_line call1 --device && ! has_line call1 --group-add'
    dexpect "$sh" "a --zip path with spaces" 0 "" --work "$T/w 1" --zip "$T/in put/x y.zip" --skip-space-check
    check "[$sh] the zip is mounted read-only as one argument and passed as /in/zip" \
        'has_line call1 "$T/in put/x y.zip:/in/zip:ro" && has_line call1 /in/zip && has_line call1 --skip-space-check'
    rm -rf "$T/w2"
    dexpect "$sh" "refuses a missing --zip before calling docker" 2 "not a file" --work "$T/w2" --zip "$T/nope.zip"
    check "[$sh] ... and creates nothing" '[ "$(calls)" = 0 ] && [ ! -e "$T/w2" ]'
    dexpect "$sh" "refuses a work path with ':'" 2 "must not contain ':'" --work "$T/a:b"
    check "[$sh] ... before creating it or calling docker" '[ "$(calls)" = 0 ] && [ ! -e "$T/a:b" ]'
    echo zip > "$T/c:d.zip"
    dexpect "$sh" "refuses an input file path with ':'" 2 "must not contain ':'" --work "$T/w 1" --zip "$T/c:d.zip"
    check "[$sh] ... without calling docker" '[ "$(calls)" = 0 ]'
    out=$(cd "$T" && DOCKER_LOG="$T/dl" GBOS_DOCKER="$T/fake/docker" GBOS_DOCKER_USER=0:0 "$sh" "$ROOT/install/docker-install.sh" --work "$T/w3" 2>&1); rc=$?
    check "[$sh] refuses to run the container as root, before creating the work dir" \
        '[ "$rc" = 2 ] && grep -q "normal user" <<< "$out" && [ ! -e "$T/w3" ]'
    out=$(cd "$T" && GBOS_DOCKER="$T/fake/docker" GBOS_DOCKER_USER=me "$sh" "$ROOT/install/docker-install.sh" --work "$T/w3" 2>&1); rc=$?
    check "[$sh] refuses a malformed GBOS_DOCKER_USER" '[ "$rc" = 2 ] && grep -q "uid:gid" <<< "$out"'
    dexpect "$sh" "refuses an unknown argument" 2 "unknown argument" --work "$T/w 1" --bogus
    dexpect "$sh" "refuses --zip without a value" 2 "not a file" --work "$T/w 1" --zip
    dexpect "$sh" "refuses a missing --work" 2 "--work DIR is required"
done

# --- 2. install.sh (Linux x86-64)
if [ "$(uname -s)/$(uname -m)" != Linux/x86_64 ]; then skp "install.sh reuse cases (install.sh runs on Linux x86-64 only)"; finish; fi
if [ "$(id -u)" = 0 ]; then skp "install.sh reuse cases (install.sh refuses root; run as a normal user)"; finish; fi
R="$T/repo"; mkdir -p "$R/install" "$R/base/builds" "$R/builder/lib" "$R/profiles/kano" "$T/bin"
cp "$ROOT/install/install.sh" "$R/install/"
cp "$ROOT"/base/builds/*.sh "$R/base/builds/"
cp "$ROOT/profiles/kano/audio-src.list" "$ROOT/profiles/kano/profile.toml" "$ROOT/profiles/kano/vendor.py" "$R/profiles/kano/"
cp "$ROOT/profiles/_safety.toml" "$R/profiles/"
cp "$ROOT/builder/lib/devprofile.py" "$R/builder/lib/"     # install.sh reads the image name from the profile
echo "# stand-in for base/gbos_base.py" > "$R/base/gbos_base.py"
echo "# stand-in for builder/vendor.sh" > "$R/builder/vendor.sh"
echo "# stand-in" > "$R/builder/lib/rebuild.py"
cat > "$R/base/build-base.sh" << 'STUB'
#!/usr/bin/env bash
# stub of base/build-base.sh: a tiny "base" with the files install.sh reads
set -eu
while [ $# -gt 0 ]; do case "$1" in --out) OUT=$2; shift 2 ;; --key) KEY=$2; shift 2 ;; *) shift ;; esac; done
mkdir -p "$OUT/keys" "$OUT/cache/otatools/bin"
if [ -n "${KEY:-}" ]; then cp "$KEY" "$OUT/keys/vbmeta.pem"; else head -c 64 /dev/urandom | od -An -tx1 > "$OUT/keys/vbmeta.pem"; fi
printf '#!/bin/sh\n# stub avbtool: the "public key" is the key file itself\nwhile [ $# -gt 0 ]; do case "$1" in --key) k=$2; shift 2;; --output) o=$2; shift 2;; *) shift;; esac; done\ncp "$k" "$o"\n' \
    > "$OUT/cache/otatools/bin/avbtool"
chmod +x "$OUT/cache/otatools/bin/avbtool"
echo base-image > "$OUT/kano-real.img"; echo initrd > "$OUT/initrd.img"
(cd "$OUT" && sha256sum kano-real.img initrd.img > SHA256SUMS)
echo "public key sha1 $(sha1sum < "${KEY:-$OUT/keys/vbmeta.pem}" | cut -c1-40)" > "$OUT/build-info.txt"
touch "$OUT/BUILD-DONE"
echo "$OUT" >> "$STUB_LOG/base"
STUB
cat > "$R/builder/fetch-inputs.sh" << 'STUB'
#!/usr/bin/env bash
mkdir -p "$1/firmware" "$1/audio-src"
STUB
mkdir -p "$R/builder/kmod"
cat > "$R/builder/kmod/build-ac.sh" << 'STUB'
#!/usr/bin/env bash
# stub of builder/kmod/build-ac.sh <work> <out>: a fake ac.ko, and a work dir that install.sh must remove afterwards;
# the pin is the SHA-256 of the fake module, so install.sh's pin check accepts it on a reuse
AC_KO_SHA=a0e575a281219c53af7e810538a3d4ae21151b8b2d47816df018f90760ae24e7
set -eu
mkdir -p "$1" "$2"; echo toolchain > "$1/clang"; echo ac-module > "$2/ac.ko"
echo kmod >> "$STUB_LOG/kmod"
STUB
cat > "$R/builder/kmod/build-gbos_wakefilter.sh" << 'STUB'
#!/usr/bin/env bash
# stub of builder/kmod/build-gbos_wakefilter.sh <work prepared by build-ac.sh> <out>
WAKEFILTER_KO_SHA=5a1c5f0b3eeb254c34eca1f1796d49c9a543995128972eeba90445e720e6d02c
set -eu
[ -f "$1/clang" ] || { echo "work dir not prepared by build-ac.sh"; exit 2; }
echo wakefilter-module > "$2/gbos_wakefilter.ko"
echo wakefilter >> "$STUB_LOG/kmod-wf"
STUB
cat > "$R/builder/build.sh" << 'STUB'
#!/usr/bin/env bash
# stub of builder/build.sh: an "image" whose name and content record the settings it was built with
set -eu
mkdir -p "$GBOS_OUT/image"
I="$GBOS_OUT/image/${GBOS_IMAGE_NAME:-googlebook-kano.img}"
echo "entry ${GBOS_DEFAULT_ENTRY:-} editor ${GBOS_LOADER_EDITOR:-} adb ${GBOS_ADB_PUBKEY:-} kmod-ac ${GBOS_KMOD_AC:-} kmod-wf ${GBOS_KMOD_WAKEFILTER:-}" > "$I"
(cd "$GBOS_OUT/image" && sha256sum "${I##*/}" > "${I##*/}.sha256")
echo build >> "$STUB_LOG/build"
STUB
# commands install.sh's host check asks for but the stubs never run (CI images lack some of them)
for c in curl python3 lz4 mkfs.fat mmd mcopy mtype mdir openssl sha256sum truncate tar zstd cmp git make perl flex bison nice xargs base64; do
    command -v "$c" > /dev/null || { printf '#!/bin/sh\nexit 1\n' > "$T/bin/$c"; chmod +x "$T/bin/$c"; }
done
Z="$T/fake.zip"; echo zip > "$Z"
ZSHA=$(sed -n 's/^ZIP_SHA256=//p' "$R/base/builds/16452207.sh")
echo "$ZSHA $(stat -c '%s %Y' "$Z")" > "$Z.ok"   # the stamp of a verified zip, so the fake zip is not hashed
mkdir -p "$T/log"
iexpect() {  # iexpect <name> <exit> <regex> [VAR=value ...] -- <install.sh args>; output in $out
    local name=$1 code=$2 re=$3 rc; shift 3
    local -a envs=()
    while [ $# -gt 0 ] && [ "$1" != -- ]; do envs+=("$1"); shift; done
    shift
    out=$(env -u GBOS_DEFAULT_ENTRY -u GBOS_LOADER_EDITOR -u GBOS_EC -u GBOS_ALLOW_FIRMWARE_DRIFT -u GBOS_IMAGE_NAME \
        PATH="$T/bin:$PATH" STUB_LOG="$T/log" ${envs[@]+"${envs[@]}"} \
        bash "$R/install/install.sh" --zip "$Z" --skip-space-check --no-selftest "$@" 2>&1 < /dev/null); rc=$?
    if [ "$rc" = "$code" ] && grep -Eq -- "$re" <<< "$out"; then ok "$name"; else bad "$name (rc=$rc)"; tail -n 6 <<< "$out" | sed 's/^/    /'; fi
}
builds() { cat "$T/log/build" 2>/dev/null | wc -l | tr -d ' '; }
W="$T/work"
iexpect "first run builds base and image" 0 "^done: $W/build/image/googlebook-kano.img" -- --work "$W"
check "the stubs ran once each" '[ "$(builds)" = 1 ] && [ "$(wc -l < "$T/log/base")" = 1 ]'
for l in "^sources [0-9a-f]{64}$" "^adb-key none$" "^default-entry lapis-quiet.conf$" "^loader-editor no$" "^ec-features none$" "^selftest 0$" "^firmware-drift 0$"; do
    check "params records: $l" 'grep -Eq -- "$l" "$W/build/params"'
done
check "the base records its sources" 'grep -Eq "^base-sources [0-9a-f]{64}$" "$W/base/install-sources"'
check "the profile's ac.ko was built once and handed to build.sh" '[ "$(wc -l < "$T/log/kmod")" = 1 ] && grep -q "kmod-ac $W/inputs/kmod/ac.ko" "$W/build/image/googlebook-kano.img"'
check "... its 9 GB work dir removed afterwards" '[ ! -e "$W/inputs/kmod-work" ]'
check "params records the ac.ko" 'grep -Eq "^kmod-ac [0-9a-f]{64}$" "$W/build/params"'
check "the profile's gbos_wakefilter.ko was built once, in the same work dir, and handed to build.sh" '[ "$(wc -l < "$T/log/kmod-wf")" = 1 ] && grep -q "kmod-wf $W/inputs/kmod/gbos_wakefilter.ko" "$W/build/image/googlebook-kano.img"'
check "params records the gbos_wakefilter.ko" 'grep -Eq "^kmod-gbos_wakefilter [0-9a-f]{64}$" "$W/build/params"'
iexpect "same sources and settings: the image is reused" 0 "reusing $W/build \(same sources and settings\)" -- --work "$W"
check "... without building again" '[ "$(builds)" = 1 ]'
check "... nor ac.ko" '[ "$(wc -l < "$T/log/kmod")" = 1 ]'
check "... nor gbos_wakefilter.ko" '[ "$(wc -l < "$T/log/kmod-wf")" = 1 ]'
echo not-the-pinned-module > "$W/inputs/kmod/ac.ko"
iexpect "a cached ac.ko that is not the pinned one: both modules are built again" 0 "does not hold the modules pinned" -- --work "$W"
check "... once each, and the image is reused (the rebuilt modules are the pinned ones)" \
    '[ "$(wc -l < "$T/log/kmod")" = 2 ] && [ "$(wc -l < "$T/log/kmod-wf")" = 2 ] && [ "$(builds)" = 1 ]'
iexpect "GBOS_DEFAULT_ENTRY changed: refused" 2 "made with other settings" GBOS_DEFAULT_ENTRY=lapis-debug.conf -- --work "$W"
check "... and the change is shown" 'grep -q "now: default-entry lapis-debug.conf" <<< "$out"'
iexpect "GBOS_LOADER_EDITOR changed: refused" 2 "made with other settings" GBOS_LOADER_EDITOR=yes -- --work "$W"
iexpect "GBOS_EC changed: refused" 2 "made with other settings" GBOS_EC=sensors -- --work "$W"
echo "ssh-rsa AAAA test" > "$T/adb.pub"
iexpect "an adb key added: refused" 2 "made with other settings" -- --work "$W" --adb-pubkey "$T/adb.pub"
iexpect "--adb-pubkey that does not exist: refused at once" 2 "--adb-pubkey: not a file" -- --work "$W" --adb-pubkey "$T/none.pub"
check "... before any step ran" '! grep -q "host check" <<< "$out"'
iexpect "GBOS_ALLOW_FIRMWARE_DRIFT=1: never reused" 2 "never reused" GBOS_ALLOW_FIRMWARE_DRIFT=1 -- --work "$W"
echo "# edited" >> "$R/builder/vendor.sh"
iexpect "an edited builder file (uncommitted): refused" 2 "now: sources" -- --work "$W"
sed -i '$d' "$R/builder/vendor.sh"
iexpect "the edit undone: reused again" 0 "reusing $W/build" -- --work "$W"
echo "# new" > "$R/builder/lib/new-helper.py"
iexpect "a new untracked builder file: refused" 2 "now: sources" -- --work "$W"
rm "$R/builder/lib/new-helper.py"
echo "notes" > "$R/builder/NOTES.md"
iexpect "a Markdown file in builder/ does not count" 0 "reusing $W/build" -- --work "$W"
echo "x" >> "$R/profiles/kano/audio-src.list"
iexpect "an edited profile file: refused" 2 "now: sources" -- --work "$W"
sed -i '$d' "$R/profiles/kano/audio-src.list"
check "nothing was rebuilt by any refused run" '[ "$(builds)" = 1 ]'
echo "# edited" >> "$R/base/gbos_base.py"
iexpect "an edited base/ file: the base is refused" 2 "other base/ sources" -- --work "$W"
sed -i '$d' "$R/base/gbos_base.py"
echo damaged > "$W/base/kano-real.img"
iexpect "a damaged base file: refused" 2 "does not match its SHA256SUMS" -- --work "$W"
W2="$T/work2"
iexpect "a new work dir with GBOS_DEFAULT_ENTRY builds with it" 0 "^done:" GBOS_DEFAULT_ENTRY=lapis-debug.conf -- --work "$W2"
check "... and records it" 'grep -qx "default-entry lapis-debug.conf" "$W2/build/params" && grep -q "entry lapis-debug.conf" "$W2/build/image/googlebook-kano.img"'
iexpect "... then the same settings reuse it" 0 "reusing" GBOS_DEFAULT_ENTRY=lapis-debug.conf -- --work "$W2"
iexpect "... and the default settings do not" 2 "was: default-entry lapis-debug.conf" -- --work "$W2"
rm "$W2/base/install-sources"
iexpect "a base without a record of its sources: refused" 2 "without a record of its sources" -- --work "$W2"
# the AVB key is kept outside base/ and an update (--update) rebuilds with it
check "the first run kept the AVB key in work/keys" 'cmp -s "$W/keys/vbmeta.pem" "$W/base/keys/vbmeta.pem"'
check "... readable only by the user" '[ "$(stat -c %a "$W/keys/vbmeta.pem")" = 600 ]'
iexpect "GBOS_LAPIS_BUILD without pins: refused" 2 "no pins for Google build '1234'" GBOS_LAPIS_BUILD=1234 -- --work "$W"
echo damaged-fixed > /dev/null; rm -rf "$W/base"
iexpect "base/ deleted: rebuilt with the kept key (no new key)" 0 "^done:" -- --work "$W" --update
check "... the new base is signed with the kept key" 'cmp -s "$W/keys/vbmeta.pem" "$W/base/keys/vbmeta.pem"'
echo "# edited" >> "$R/base/gbos_base.py"
iexpect "--update with changed base/ sources: base moved to base.prev and rebuilt" 0 "moving it to $W/base.prev" -- --work "$W" --update
check "... with the kept key" 'cmp -s "$W/keys/vbmeta.pem" "$W/base/keys/vbmeta.pem" && [ -d "$W/base.prev" ]'
check "... and it says how to update a stick" 'grep -q "write-usb.sh --keep-data" <<< "$out"'
sed -i '$d' "$R/base/gbos_base.py"
echo "# edited" >> "$R/builder/vendor.sh"
iexpect "--update with changed builder sources: build moved to build.prev and rebuilt" 0 "moving it to $W/build.prev" -- --work "$W" --update
check "... a new image was built" 'grep -q "^build$" "$T/log/build" && [ -d "$W/build.prev" ] && [ -f "$W/build/image/googlebook-kano.img" ]'
sed -i '$d' "$R/builder/vendor.sh"
finish
