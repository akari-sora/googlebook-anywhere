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
    out=$(cd "$T" && env -u GBOS_TEST_AUTOOFF -u GBOS_USB_SMOKE DOCKER_LOG="$T/dl" GBOS_DOCKER="$T/fake/docker" GBOS_DOCKER_USER="$DUSER" "$sh" "$ROOT/install/docker-install.sh" "$@" 2>&1 < /dev/null); rc=$?
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
    for value in 1 invalid; do
        rm -rf "$T/usb-work" "$T/dl"; mkdir -p "$T/dl"
        out=$(cd "$T" && env -u GBOS_TEST_AUTOOFF GBOS_USB_SMOKE="$value" DOCKER_LOG="$T/dl" GBOS_DOCKER="$T/fake/docker" GBOS_DOCKER_USER="$DUSER" \
            "$sh" "$ROOT/install/docker-install.sh" --work "$T/usb-work" 2>&1 < /dev/null); rc=$?
        check "[$sh] USB smoke $value refused before docker and workdir" \
            '[ "$rc" = 2 ] && grep -q "GBOS_USB_SMOKE is unsupported" <<< "$out" && [ "$(calls)" = 0 ] && [ ! -e "$T/usb-work" ]'
    done
    rm -rf "$T/usb-work" "$T/dl"; mkdir -p "$T/dl"
    out=$(cd "$T" && env -u GBOS_TEST_AUTOOFF GBOS_USB_SMOKE='' DOCKER_LOG="$T/dl" GBOS_DOCKER="$T/fake/docker" GBOS_DOCKER_USER="$DUSER" \
        "$sh" "$ROOT/install/docker-install.sh" --work "$T/usb-work" --no-selftest 2>&1 < /dev/null); rc=$?
    check "[$sh] empty USB smoke calls docker" '[ "$rc" = 0 ] && [ "$(calls)" = 2 ] && [ -d "$T/usb-work" ]'
    rm -rf "$T/w4" "$T/dl"; mkdir -p "$T/dl"
    out=$(cd "$T" && DOCKER_LOG="$T/dl" GBOS_DOCKER="$T/fake/docker" GBOS_DOCKER_USER="$DUSER" GBOS_TEST_AUTOOFF=2400 \
        "$sh" "$ROOT/install/docker-install.sh" --work "$T/w4" 2>&1 < /dev/null); rc=$?
    check "[$sh] refuses GBOS_TEST_AUTOOFF before docker" \
        '[ "$rc" = 2 ] && grep -q "developer-only" <<< "$out" && [ "$(calls)" = 0 ] && [ ! -e "$T/w4" ]'
    rm -rf "$T/dl"; mkdir -p "$T/dl"
    out=$(cd "$T" && DOCKER_LOG="$T/dl" GBOS_DOCKER="$T/fake/docker" GBOS_DOCKER_USER="$DUSER" GBOS_TEST_AUTOOFF='' \
        "$sh" "$ROOT/install/docker-install.sh" --work "$T/w 1" --no-selftest 2>&1 < /dev/null); rc=$?
    check "[$sh] empty GBOS_TEST_AUTOOFF still calls docker" '[ "$rc" = 0 ] && [ "$(calls)" = 2 ]'
    dexpect "$sh" "refuses a work path with ':'" 2 "must not contain ':'" --work "$T/a:b"
    check "[$sh] ... before creating it or calling docker" '[ "$(calls)" = 0 ] && [ ! -e "$T/a:b" ]'
    echo zip > "$T/c:d.zip"
    dexpect "$sh" "refuses an input file path with ':'" 2 "must not contain ':'" --work "$T/w 1" --zip "$T/c:d.zip"
    check "[$sh] ... without calling docker" '[ "$(calls)" = 0 ]'
    out=$(cd "$T" && env -u GBOS_TEST_AUTOOFF -u GBOS_USB_SMOKE DOCKER_LOG="$T/dl" GBOS_DOCKER="$T/fake/docker" GBOS_DOCKER_USER=0:0 "$sh" "$ROOT/install/docker-install.sh" --work "$T/w3" 2>&1); rc=$?
    check "[$sh] refuses to run the container as root, before creating the work dir" \
        '[ "$rc" = 2 ] && grep -q "normal user" <<< "$out" && [ ! -e "$T/w3" ]'
    out=$(cd "$T" && env -u GBOS_TEST_AUTOOFF GBOS_DOCKER="$T/fake/docker" GBOS_DOCKER_USER=me "$sh" "$ROOT/install/docker-install.sh" --work "$T/w3" 2>&1); rc=$?
    check "[$sh] refuses a malformed GBOS_DOCKER_USER" '[ "$rc" = 2 ] && grep -q "uid:gid" <<< "$out"'
    dexpect "$sh" "refuses an unknown argument" 2 "unknown argument" --work "$T/w 1" --bogus
    dexpect "$sh" "refuses --zip without a value" 2 "not a file" --work "$T/w 1" --zip
    dexpect "$sh" "refuses a missing --work" 2 "--work DIR is required"
done

# --- 2. install.sh
if [ "$(id -u)" = 0 ]; then skp "install.sh reuse cases (install.sh refuses root; run as a normal user)"; finish; fi
R="$T/repo"; mkdir -p "$R/install" "$R/base/builds" "$R/builder/lib" "$R/profiles/kano" "$R/profiles/amd-hx370" "$R/tools/mesa" "$T/bin"
if [ "$(uname -s)/$(uname -m)" != Linux/x86_64 ]; then
    cat > "$T/bin/uname" << 'STUB'
#!/bin/sh
case "${1:-}" in
    -s) echo Linux ;;
    -m) echo x86_64 ;;
    -sm) echo "Linux x86_64" ;;
    *) echo Linux ;;
esac
STUB
    chmod +x "$T/bin/uname"
fi
if ! stat -c '%s %Y' /dev/null >/dev/null 2>&1; then
    cat > "$T/bin/stat" << 'STUB'
#!/usr/bin/env python3
import os, sys
if len(sys.argv) >= 3 and sys.argv[1] == "-c":
    fmt = sys.argv[2]
    for p in sys.argv[3:]:
        st = os.stat(p)
        out = (fmt.replace("%n", p)
                  .replace("%s", str(st.st_size))
                  .replace("%Y", str(int(st.st_mtime)))
                  .replace("%i", str(st.st_ino))
                  .replace("%a", oct(st.st_mode & 0o777)[2:])
                  .replace("%g", str(st.st_gid)))
        print(out)
    sys.exit(0)
os.execvp("/usr/bin/stat", ["stat"] + sys.argv[1:])
STUB
    chmod +x "$T/bin/stat"
fi
if ! command -v sha256sum >/dev/null 2>&1; then
    printf '#!/bin/sh\nexec shasum -a 256 "$@"\n' > "$T/bin/sha256sum"; chmod +x "$T/bin/sha256sum"
fi
if ! command -v sha1sum >/dev/null 2>&1; then
    printf '#!/bin/sh\nexec shasum -a 1 "$@"\n' > "$T/bin/sha1sum"; chmod +x "$T/bin/sha1sum"
fi
if ! printf '' | xargs -0 -r true >/dev/null 2>&1; then
    cat > "$T/bin/xargs" << 'STUB'
#!/usr/bin/env python3
import os, sys
args = [a for a in sys.argv[1:] if a != "-r"]
os.execvp("/usr/bin/xargs", ["xargs"] + args)
STUB
    chmod +x "$T/bin/xargs"
fi
export PATH="$T/bin:$PATH"
undo_append() {
    python3 - "$1" << 'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
lines = p.read_text().splitlines()
p.write_text("\n".join(lines[:-1]) + "\n")
PY
}
cp "$ROOT/install/install.sh" "$R/install/"
cp "$ROOT"/base/builds/*.sh "$R/base/builds/"
# A second selectable build with the same fake zip pin; create it before any build so the source tree is unchanged.
cp "$R/base/builds/16452207.sh" "$R/base/builds/99999999.sh"
# The model-option cache test uses the same tiny fake zip for the newer build.
cp "$R/base/builds/16452207.sh" "$R/base/builds/16658739.sh"
cp "$ROOT/profiles/kano/audio-src.list" "$ROOT/profiles/kano/profile.toml" "$ROOT/profiles/kano/vendor.py" "$R/profiles/kano/"
cp "$ROOT/profiles/amd-hx370/profile.toml" "$ROOT/profiles/amd-hx370/vendor.py" "$R/profiles/amd-hx370/"
cp "$ROOT/profiles/_safety.toml" "$R/profiles/"
cp "$ROOT/builder/lib/devprofile.py" "$R/builder/lib/"     # install.sh reads the image name from the profile
# stand-in for builder/lib/ifma.py: install.sh's IFMA step (cpu_compat.patch_ifma = true) imports it and puts the
# key-info line ensure_apex_keys() prints into build/params. Each call is logged; GBOS_TEST_IFMA_KEYINFO changes the line.
cat > "$R/builder/lib/ifma.py" << 'STUB'
"""stand-in for builder/lib/ifma.py (install-sh-test.sh)"""
import os

def ensure_apex_keys():
    with open(os.path.join(os.environ["STUB_LOG"], "ifma"), "a") as log:
        log.write(os.environ.get("GBOS_APEX_KEYS_DIR", "") + "\n")
    print(os.environ.get("GBOS_TEST_IFMA_KEYINFO", "apex-keys stand-in-fingerprint-v1"))
STUB
if ! python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import ifma; ifma.ensure_apex_keys' "$R/builder/lib" > /dev/null 2>&1; then
    bad "the fake repo has no importable builder/lib/ifma.py with ensure_apex_keys(): install.sh's IFMA step (patch_ifma = true) would fail every build"
    finish
fi
echo "# stand-in for base/gbos_base.py" > "$R/base/gbos_base.py"
echo "# stand-in for builder/vendor.sh" > "$R/builder/vendor.sh"
echo "# stand-in" > "$R/builder/lib/rebuild.py"
echo "# stand-in for tools/mesa/build-radv.sh" > "$R/tools/mesa/build-radv.sh"
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
echo "base-image ${GBOS_LAPIS_BUILD:-16452207}" > "$OUT/kano-real.img"; echo initrd > "$OUT/initrd.img"
(cd "$OUT" && sha256sum kano-real.img initrd.img > SHA256SUMS)
echo "public key sha1 $(sha1sum < "${KEY:-$OUT/keys/vbmeta.pem}" | cut -c1-40)" > "$OUT/build-info.txt"
touch "$OUT/BUILD-DONE"
echo "$OUT" >> "$STUB_LOG/base"
STUB
cat > "$R/builder/fetch-inputs.sh" << 'STUB'
#!/usr/bin/env bash
mkdir -p "$1/firmware" "$1/audio-src"
STUB
cat > "$R/builder/fetch-generic-inputs.sh" << 'STUB'
#!/usr/bin/env bash
mkdir -p "$1/generic"
[ -f "$1/generic/vulkan.pastel.so" ] || echo pastel-v1 > "$1/generic/vulkan.pastel.so"
[ -f "$1/generic/gbos_efifb.ko" ] || echo efifb-v1 > "$1/generic/gbos_efifb.ko"
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
# stub of builder/build.sh. The file it writes is text, not a GPT/ESP disk image. A _safety.toml edit
# changes the cache identity, and the cmdline below is produced by the real devprofile.make_entries().
# This does not by itself prove the bytes of a finished disk image.
set -eu
BROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$GBOS_OUT/image"
I="$GBOS_OUT/image/${GBOS_IMAGE_NAME:-googlebook-kano.img}"
CMDLINE=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import devprofile; print(" ".join(devprofile.make_entries(devprofile.load(sys.argv[2]), "console=ttyS0")["quiet"]))' "$BROOT/builder/lib" "${GBOS_PROFILE:-$BROOT/profiles/kano}")
echo "entry ${GBOS_DEFAULT_ENTRY:-} editor ${GBOS_LOADER_EDITOR:-} adb ${GBOS_ADB_PUBKEY:-} kmod-ac ${GBOS_KMOD_AC:-} kmod-wf ${GBOS_KMOD_WAKEFILTER:-} cmdline $CMDLINE" > "$I"
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
mkdir -p "$T/log" "$T/inc/openssl"
touch "$T/inc/gelf.h" "$T/inc/dwarf.h" "$T/inc/openssl/opensslv.h"   # the host headers the ac.ko build needs
export GBOS_HOST_INCLUDE="$T/inc"
iexpect() {  # iexpect <name> <exit> <regex> [VAR=value ...] -- <install.sh args>; output in $out
    local name=$1 code=$2 re=$3 rc; shift 3
    local -a envs=()
    while [ $# -gt 0 ] && [ "$1" != -- ]; do envs+=("$1"); shift; done
    shift
    out=$(env -u GBOS_DEFAULT_ENTRY -u GBOS_LOADER_EDITOR -u GBOS_EC -u GBOS_ALLOW_FIRMWARE_DRIFT -u GBOS_IMAGE_NAME -u GBOS_TEST_AUTOOFF \
        -u GBOS_DEV_KERNEL_DIR -u GBOS_RADV -u GBOS_RADV_PROBE -u GBOS_RADV_VKPROP \
        -u GBOS_GENERIC_PASTEL -u GBOS_GENERIC_KO -u GBOS_PROFILE -u GBOS_LAPIS_BUILD -u GBOS_USB_SMOKE -u GBOS_KMOD_KBD_BACKLIGHT -u GBOS_ANDROID_MODEL \
        PATH="$T/bin:$PATH" STUB_LOG="$T/log" GBOS_NO_UPDATE_CHECK=1 ${envs[@]+"${envs[@]}"} \
        bash "$R/install/install.sh" --zip "$Z" --skip-space-check --no-selftest "$@" 2>&1 < /dev/null); rc=$?
    if [ "$rc" = "$code" ] && grep -Eq -- "$re" <<< "$out"; then ok "$name"; else bad "$name (rc=$rc)"; tail -n 6 <<< "$out" | sed 's/^/    /'; fi
}
builds() { cat "$T/log/build" 2>/dev/null | wc -l | tr -d ' '; }
W="$T/work"
for value in 1 0 invalid; do
    iexpect "USB smoke $value refused before cache/build" 2 "GBOS_USB_SMOKE is unsupported" GBOS_USB_SMOKE="$value" -- --work "$W"
    check "USB smoke $value creates nothing" '[ ! -e "$W" ] && [ ! -e "$T/log/build" ]'
done
iexpect "GBOS_TEST_AUTOOFF is refused before any build" 2 "developer-only" GBOS_TEST_AUTOOFF=2400 -- --work "$W"
check "GBOS_TEST_AUTOOFF creates nothing" '[ ! -e "$W" ] && [ ! -e "$T/log/build" ]'
iexpect "first run builds base and image" 0 "^done: $W/build/image/googlebook-kano.img" -- --work "$W"
check "the stubs ran once each" '[ "$(builds)" = 1 ] && [ "$(wc -l < "$T/log/base" | tr -d " ")" = 1 ]'
ifma_calls() { cat "$T/log/ifma" 2>/dev/null | wc -l | tr -d ' '; }
check "IFMA (kano: patch_ifma = true): ensure_apex_keys() ran once for the build" '[ "$(ifma_calls)" = 1 ]'
check "IFMA: its key-info line is recorded in build/params" 'grep -qx "apex-keys stand-in-fingerprint-v1" "$W/build/params"'
check "IFMA: the stand-in saw the persistent key dir GBOS_APEX_KEYS_DIR=\$W/keys/apex-keys (never build/keys)" '[ "$(sort -u "$T/log/ifma")" = "$W/keys/apex-keys" ]'
for l in "^sources [0-9a-f]{64}$" "^adb-key none$" "^default-entry lapis-quiet.conf$" "^loader-editor no$" "^ec-features none$" "^android-model none$" "^selftest 0$" "^firmware-drift 0$"; do
    check "params records: $l" 'grep -Eq -- "$l" "$W/build/params"'
done
check "the base records its sources" 'grep -Eq "^base-sources [0-9a-f]{64} build 16452207$" "$W/base/install-sources"'
check "the profile's ac.ko was built once and handed to build.sh" '[ "$(wc -l < "$T/log/kmod" | tr -d " ")" = 1 ] && grep -q "kmod-ac $W/inputs/kmod/ac.ko" "$W/build/image/googlebook-kano.img"'
check "... its 9 GB work dir removed afterwards" '[ ! -e "$W/inputs/kmod-work" ]'
check "params records the ac.ko" 'grep -Eq "^kmod-ac [0-9a-f]{64}$" "$W/build/params"'
check "the profile's gbos_wakefilter.ko was built once, in the same work dir, and handed to build.sh" '[ "$(wc -l < "$T/log/kmod-wf" | tr -d " ")" = 1 ] && grep -q "kmod-wf $W/inputs/kmod/gbos_wakefilter.ko" "$W/build/image/googlebook-kano.img"'
check "params records the gbos_wakefilter.ko" 'grep -Eq "^kmod-gbos_wakefilter [0-9a-f]{64}$" "$W/build/params"'
iexpect "same sources and settings: the image is reused" 0 "reusing $W/build \(same sources and settings\)" -- --work "$W"
iexpect "USB smoke cannot reuse ordinary cache" 2 "GBOS_USB_SMOKE is unsupported" GBOS_USB_SMOKE=1 -- --work "$W"
iexpect "empty USB smoke reuses ordinary cache" 0 "reusing $W/build" GBOS_USB_SMOKE= -- --work "$W"
check "... without building again" '[ "$(builds)" = 1 ]'
check "... nor ac.ko" '[ "$(wc -l < "$T/log/kmod" | tr -d " ")" = 1 ]'
check "... nor gbos_wakefilter.ko" '[ "$(wc -l < "$T/log/kmod-wf" | tr -d " ")" = 1 ]'
check "... the IFMA key step still runs (once more) before params are compared" '[ "$(ifma_calls)" = 3 ]'
iexpect "IFMA key-info line changed: refused (cache invalidated)" 2 "now: apex-keys stand-in-fingerprint-v2" \
    GBOS_TEST_IFMA_KEYINFO="apex-keys stand-in-fingerprint-v2" -- --work "$W"
check "... and was: names the recorded key-info line" 'grep -q "was: apex-keys stand-in-fingerprint-v1" <<< "$out"'
iexpect "IFMA key-info line restored: reused again" 0 "reusing $W/build" -- --work "$W"
echo not-the-pinned-module > "$W/inputs/kmod/ac.ko"
iexpect "a cached ac.ko that is not the pinned one: both modules are built again" 0 "does not hold the modules pinned" -- --work "$W"
check "... once each, and the image is reused (the rebuilt modules are the pinned ones)" \
    '[ "$(wc -l < "$T/log/kmod" | tr -d " ")" = 2 ] && [ "$(wc -l < "$T/log/kmod-wf" | tr -d " ")" = 2 ] && [ "$(builds)" = 1 ]'
iexpect "GBOS_DEFAULT_ENTRY changed: refused" 2 "made with other settings" GBOS_DEFAULT_ENTRY=lapis-debug.conf -- --work "$W"
check "... and the change is shown" 'grep -q "now: default-entry lapis-debug.conf" <<< "$out"'
iexpect "GBOS_LOADER_EDITOR changed: refused" 2 "made with other settings" GBOS_LOADER_EDITOR=yes -- --work "$W"
iexpect "GBOS_EC changed: refused" 2 "made with other settings" GBOS_EC=sensors -- --work "$W"
mkdir -p "$T/devk-test"; echo k > "$T/devk-test/bzImage"
echo r > "$T/radv-test.so"; echo p > "$T/probe-test"; echo v > "$T/vkprop-test.so"
iexpect "GBOS_DEV_KERNEL_DIR changed: refused" 2 "now: GBOS_DEV_KERNEL_DIR [0-9a-f]{64}" GBOS_DEV_KERNEL_DIR="$T/devk-test" -- --work "$W"
iexpect "GBOS_RADV changed: refused" 2 "now: GBOS_RADV [0-9a-f]{64}" GBOS_RADV="$T/radv-test.so" -- --work "$W"
iexpect "GBOS_RADV_PROBE changed: refused" 2 "now: GBOS_RADV_PROBE [0-9a-f]{64}" GBOS_RADV_PROBE="$T/probe-test" -- --work "$W"
iexpect "GBOS_RADV_VKPROP changed: refused" 2 "now: GBOS_RADV_VKPROP [0-9a-f]{64}" GBOS_RADV_VKPROP="$T/vkprop-test.so" -- --work "$W"
echo "ssh-rsa AAAA test" > "$T/adb.pub"
iexpect "an adb key added: refused" 2 "made with other settings" -- --work "$W" --adb-pubkey "$T/adb.pub"
iexpect "--adb-pubkey that does not exist: refused at once" 2 "--adb-pubkey: not a file" -- --work "$W" --adb-pubkey "$T/none.pub"
check "... before any step ran" '! grep -q "host check" <<< "$out"'
iexpect "GBOS_ALLOW_FIRMWARE_DRIFT=1: never reused" 2 "never reused" GBOS_ALLOW_FIRMWARE_DRIFT=1 -- --work "$W"
echo "# edited" >> "$R/builder/vendor.sh"
iexpect "an edited builder file (uncommitted): refused" 2 "now: sources" -- --work "$W"
undo_append "$R/builder/vendor.sh"
iexpect "the edit undone: reused again" 0 "reusing $W/build" -- --work "$W"
echo "# edited" >> "$R/tools/mesa/build-radv.sh"
iexpect "an edited tools/mesa/ file: refused" 2 "now: sources" -- --work "$W"
undo_append "$R/tools/mesa/build-radv.sh"
iexpect "the tools/mesa/ edit undone: reused again" 0 "reusing $W/build" -- --work "$W"
echo "# new" > "$R/builder/lib/new-helper.py"
iexpect "a new untracked builder file: refused" 2 "now: sources" -- --work "$W"
rm "$R/builder/lib/new-helper.py"
echo "notes" > "$R/builder/NOTES.md"
iexpect "a Markdown file in builder/ does not count" 0 "reusing $W/build" -- --work "$W"
echo "x" >> "$R/profiles/kano/audio-src.list"
iexpect "an edited profile file: refused" 2 "now: sources" -- --work "$W"
undo_append "$R/profiles/kano/audio-src.list"
cp "$R/profiles/_safety.toml" "$T/_safety.toml.orig"
python3 - "$R/profiles/_safety.toml" << 'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
p.write_text(p.read_text().replace(
    '"efi=noruntime",',
    '"efi=noruntime",\n    "gbos.safety_test=1",',
    1,
))
PY
iexpect "an edited profiles/_safety.toml: refused (cache invalidated)" 2 "now: sources" -- --work "$W"
cp "$T/_safety.toml.orig" "$R/profiles/_safety.toml"
iexpect "the profiles/_safety.toml edit undone: reused again" 0 "reusing $W/build" -- --work "$W"
check "nothing was rebuilt by any refused run" '[ "$(builds)" = 1 ]'
echo "# edited" >> "$R/base/gbos_base.py"
iexpect "an edited base/ file: the base is refused" 2 "other base/ sources" -- --work "$W"
undo_append "$R/base/gbos_base.py"
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
undo_append "$R/base/gbos_base.py"
echo "# edited" >> "$R/builder/vendor.sh"
# Switching Google build must invalidate the base even when all base/ sources are unchanged.
WS="$T/work-switch"
iexpect "build-switch: initial build" 0 "^done:" -- --work "$WS"
cp "$WS/base/install-sources" "$T/build-switch-sources"
iexpect "build-switch without --update refuses cached base" 2 "other base/ sources" GBOS_LAPIS_BUILD=99999999 -- --work "$WS"
check "... cached base still identifies its original build" 'cmp -s "$T/build-switch-sources" "$WS/base/install-sources"'
iexpect "build-switch --update moves base and rebuilds" 0 "moving it to $WS/base.prev" GBOS_LAPIS_BUILD=99999999 -- --work "$WS" --update
check "... records the new build and preserves the previous base" 'grep -Eq "^base-sources [0-9a-f]{64} build 99999999$" "$WS/base/install-sources" && cmp -s "$T/build-switch-sources" "$WS/base.prev/install-sources" && grep -qx "base-image 99999999" "$WS/base/kano-real.img" && grep -qx "base-image 16452207" "$WS/base.prev/kano-real.img"'
iexpect "build-switch with unchanged selection reuses base and image" 0 "reusing $WS/build" GBOS_LAPIS_BUILD=99999999 -- --work "$WS"
iexpect "--update with changed builder sources: build moved to build.prev and rebuilt" 0 "moving it to $W/build.prev" -- --work "$W" --update
check "... a new image was built" 'grep -q "^build$" "$T/log/build" && [ -d "$W/build.prev" ] && [ -f "$W/build/image/googlebook-kano.img" ]'
undo_append "$R/builder/vendor.sh"
iexpect "--update after restoring builder sources: rebuilds clean state" 0 "moving it to $W/build.prev" -- --work "$W" --update
# Changing profiles/_safety.toml invalidates cache and --update rebuilds with the changed cmdline
python3 - "$R/profiles/_safety.toml" << 'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
p.write_text(p.read_text().replace(
    '"efi=noruntime",',
    '"efi=noruntime",\n    "gbos.safety_test=1",',
    1,
))
PY
iexpect "--update with changed profiles/_safety.toml: build moved to build.prev and rebuilt" 0 "moving it to $W/build.prev" -- --work "$W" --update
check "... the rebuilt image has the new cmdline from profiles/_safety.toml and differs from build.prev" \
    'grep -q "gbos.safety_test=1" "$W/build/image/googlebook-kano.img" && ! grep -q "gbos.safety_test=1" "$W/build.prev/image/googlebook-kano.img" && ! cmp -s "$W/build/image/googlebook-kano.img" "$W/build.prev/image/googlebook-kano.img"'
cp "$T/_safety.toml.orig" "$R/profiles/_safety.toml"
iexpect "--update after restoring profiles/_safety.toml: rebuilds original cmdline" 0 "moving it to $W/build.prev" -- --work "$W" --update
check "... the restored image no longer has gbos.safety_test=1" '! grep -q "gbos.safety_test=1" "$W/build/image/googlebook-kano.img"'
# profiles/amd-hx370 default input paths ($WORK/inputs/amd-hx370/* and $WORK/inputs/generic/*) are hashed into PARAMS even when GBOS_RADV* / GBOS_GENERIC_* are not set in env
W3="$T/work-amd"; mkdir -p "$W3/inputs/amd-hx370" "$W3/inputs/generic"
echo radv-v1 > "$W3/inputs/amd-hx370/vulkan.radeon.so"
echo probe-v1 > "$W3/inputs/amd-hx370/radv-probe"
echo vkprop-v1 > "$W3/inputs/amd-hx370/libvkprop.so"
echo pastel-v1 > "$W3/inputs/generic/vulkan.pastel.so"
echo efifb-v1 > "$W3/inputs/generic/gbos_efifb.ko"
iexpect "amd-hx370 with default input paths builds image" 0 "^done: $W3/build/image/googlebook-amd-hx370.img" \
    GBOS_PROFILE="$R/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devk-test" -- --work "$W3"
for v in GBOS_RADV GBOS_RADV_PROBE GBOS_RADV_VKPROP GBOS_GENERIC_PASTEL GBOS_GENERIC_KO GBOS_DEV_KERNEL_DIR; do
    check "amd-hx370 params records default $v" 'grep -Eq "^'"$v"' [0-9a-f]{64}$" "$W3/build/params"'
done
iexpect "amd-hx370 with unchanged default inputs reuses build" 0 "reusing $W3/build" \
    GBOS_PROFILE="$R/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devk-test" -- --work "$W3"
for pair in \
    "GBOS_RADV:$W3/inputs/amd-hx370/vulkan.radeon.so" \
    "GBOS_RADV_PROBE:$W3/inputs/amd-hx370/radv-probe" \
    "GBOS_RADV_VKPROP:$W3/inputs/amd-hx370/libvkprop.so" \
    "GBOS_GENERIC_PASTEL:$W3/inputs/generic/vulkan.pastel.so" \
    "GBOS_GENERIC_KO:$W3/inputs/generic/gbos_efifb.ko"; do
    var="${pair%%:*}"; f="${pair#*:}"; orig=$(cat "$f")
    echo "$orig-modified" > "$f"
    iexpect "amd-hx370 default $var modified in place: refused" 2 "now: $var [0-9a-f]{64}" \
        GBOS_PROFILE="$R/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devk-test" -- --work "$W3"
    printf '%s\n' "$orig" > "$f"
done
iexpect "amd-hx370 default inputs restored: reused again" 0 "reusing $W3/build" \
    GBOS_PROFILE="$R/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devk-test" -- --work "$W3"
# The update notice in the final summary (install/check-updates.py --hint), offline: fixture documents, CI unset.
cp "$ROOT/install/check-updates.py" "$R/install/"; mkdir -p "$R/docs"; cp "$ROOT/docs/RELEASE-NOTES.md" "$R/docs/"
UFX="$ROOT/tests/update/fixtures/updcheck"
UNEW=("GBOS_NO_UPDATE_CHECK=" "CI=" "GBOS_UPDCHECK_GOOGLE_URL=file://$UFX/recovery-newer.json" "GBOS_UPDCHECK_RELEASES_URL=file://$UFX/releases-newer.json")
iexpect "update notice: a newer Google build and release are noted after the image" 0 "^NOTE \(google\): newer Google build available: 16471258" "${UNEW[@]}" -- --work "$W"
check "... the project notice too, after the done: line" 'grep -q "^NOTE (project): newer release available: v9.0.0" <<< "$out" && [ "$(grep -n "^done: " <<< "$out" | cut -d: -f1)" -lt "$(grep -n "^NOTE (google)" <<< "$out" | cut -d: -f1)" ]'
iexpect "update notice: --no-update-check prints none" 0 "^done: " "${UNEW[@]}" -- --work "$W" --no-update-check
check "... no NOTE line" '! grep -q "^NOTE (" <<< "$out"'
iexpect "update notice: none after update.sh already checked (GBOS_UPDATE_CHECKED=1)" 0 "^done: " "${UNEW[@]}" GBOS_UPDATE_CHECKED=1 -- --work "$W"
check "... no NOTE line" '! grep -q "^NOTE (" <<< "$out"'
iexpect "update notice: a network error does not fail the install" 0 "^done: " "GBOS_NO_UPDATE_CHECK=" "CI=" "GBOS_UPDCHECK_GOOGLE_URL=http://127.0.0.1:9/x" "GBOS_UPDCHECK_RELEASES_URL=http://127.0.0.1:9/x" -- --work "$W"
check "... and prints nothing about it" '! grep -q "^NOTE (\|could not check" <<< "$out"'
# A profile with cpu_compat.patch_ifma = false (every shipped profile has true): a copy of kano made here, after the
# cases above, so it does not change their source identity.
cp -R "$R/profiles/kano" "$R/profiles/kano-noifma"
sed -i.bak -e 's/^patch_ifma = true$/patch_ifma = false/' -e 's/^id = "kano"$/id = "kano-noifma"/' "$R/profiles/kano-noifma/profile.toml"; rm -f "$R/profiles/kano-noifma/profile.toml.bak"
check "the kano-noifma copy says patch_ifma = false" '[ "$(python3 "$R/builder/lib/devprofile.py" get "$R/profiles/kano-noifma" cpu_compat.patch_ifma)" = false ]'
i0=$(ifma_calls); export i0   # the eval use on the next check line is invisible to the linter (SC2034), same as k0 below
iexpect "patch_ifma = false builds" 0 "^done: " GBOS_PROFILE="$R/profiles/kano-noifma" -- --work "$T/work-noifma"
check "... without calling ensure_apex_keys()" '[ "$(ifma_calls)" = "$i0" ] && [ -f "$T/work-noifma/build/params" ] && ! grep -q "^apex-keys" "$T/work-noifma/build/params"'
mkdir -p "$T/noinc/openssl"; touch "$T/noinc/gelf.h" "$T/noinc/openssl/opensslv.h"   # libelf and OpenSSL, no libdw
k0=$(cat "$T/log/kmod" 2>/dev/null | wc -l | tr -d ' '); export k0
iexpect "no dwarf.h on the host: refused before the kernel build, with the libdw-dev hint" 2 \
    "missing \(to build ac.ko\): dwarf.h$|missing \(to build ac.ko\): dwarf.h;" GBOS_HOST_INCLUDE="$T/noinc" -- --work "$T/work-nodw"
check "... the hint names libdw-dev for Debian/Ubuntu, and ac.ko was not built" 'grep -q "apt install .*libdw-dev" <<< "$out" && [ "$(cat "$T/log/kmod" 2>/dev/null | wc -l | tr -d " ")" = "$k0" ]'
# Selected backlight module bytes are validated and recorded before a cache hit.
mkdir -p "$T/kbd-profile"
cp -R "$R/profiles/kano" "$T/kbd-profile/kano"
python3 - "$T/kbd-profile/kano/profile.toml" <<'PYKBD'
import sys
from pathlib import Path
p = Path(sys.argv[1]); s = p.read_text()
s = s.replace('kmods = ["ac", "gbos_wakefilter"]', 'kmods = ["ac", "gbos_wakefilter", "gbos_kbd_backlight"]')
s = s.replace('modules = ["cros_ec_debugfs"', 'modules = ["cros_kbd_led_backlight", "cros_ec_debugfs"')
s = s.replace('allow = ["cros_ec_proto", "cros_kbd_led_backlight"]', 'allow = ["cros_ec_proto"]')
p.write_text(s)
PYKBD
echo pinned-backlight > "$T/kbd.ko"
KBD_SHA=$(sha256sum < "$T/kbd.ko" | cut -c1-64)
printf 'KBD_BACKLIGHT_KO_SHA=%s\n' "$KBD_SHA" > "$R/builder/kmod/build-gbos_kbd_backlight.sh"
KBENV=(GBOS_PROFILE="$T/kbd-profile/kano" GBOS_EC=drain GBOS_KMOD_KBD_BACKLIGHT="$T/kbd.ko")
iexpect "selected pinned backlight builds" 0 '^done:' "${KBENV[@]}" -- --work "$T/work-kbd"
check "selected module hash recorded in cache identity" 'grep -qx "kmod-gbos_kbd_backlight $KBD_SHA" "$T/work-kbd/build/params"'
iexpect "same backlight bytes reuse cache" 0 'reusing .*build' "${KBENV[@]}" -- --work "$T/work-kbd"
echo changed > "$T/kbd.ko"
iexpect "changed backlight bytes cannot reuse cache" 2 'must be the pinned module' "${KBENV[@]}" -- --work "$T/work-kbd"
KBD_NEW_SHA=$(sha256sum < "$T/kbd.ko" | cut -c1-64)
printf 'KBD_BACKLIGHT_KO_SHA=%s\n' "$KBD_NEW_SHA" > "$R/builder/kmod/build-gbos_kbd_backlight.sh"
iexpect "new pinned backlight invalidates old cache" 2 'made with other settings' "${KBENV[@]}" -- --work "$T/work-kbd"
check "cache diff records both backlight hashes" 'grep -q "was: kmod-gbos_kbd_backlight $KBD_SHA" <<< "$out" && grep -q "now: kmod-gbos_kbd_backlight $KBD_NEW_SHA" <<< "$out"'
iexpect "unselected backlight module rejected" 2 'profile does not list' GBOS_KMOD_KBD_BACKLIGHT="$T/kbd.ko" -- --work "$T/work-kbd"
iexpect "invalid model option rejected before cache reuse" 2 'GBOS_ANDROID_MODEL must be empty or profile' GBOS_ANDROID_MODEL=invalid -- --work "$W"
iexpect "model option refuses unsupported build" 2 'requires the pinned KANO profile and Google build 16658739' GBOS_ANDROID_MODEL=profile -- --work "$W"
python3 - "$W/build/params" << 'PYMODEL'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text()
assert s.count('android-model none\n') == 1
p.write_text(s.replace('android-model none\n', 'android-model profile\n'))
PYMODEL
iexpect "model option change cannot reuse candidate cache" 2 'now: android-model none' -- --work "$W"
check "cache mismatch reports prior model option" 'grep -q "was: android-model profile" <<< "$out"'
MW="$T/work-model"
iexpect "model candidate builds with pinned build" 0 '^done:' GBOS_LAPIS_BUILD=16658739 GBOS_ANDROID_MODEL=profile -- --work "$MW"
check "candidate model flag recorded in cache identity" 'grep -qx "android-model profile" "$MW/build/params"'
iexpect "same model flag reuses candidate cache" 0 'reusing .*build' GBOS_LAPIS_BUILD=16658739 GBOS_ANDROID_MODEL=profile -- --work "$MW"
iexpect "turning model flag off refuses candidate cache" 2 'now: android-model none' GBOS_LAPIS_BUILD=16658739 -- --work "$MW"
iexpect "turning model flag off with update rebuilds" 0 'moving it to .*build.prev' GBOS_LAPIS_BUILD=16658739 -- --work "$MW" --update
check "updated cache records model disabled" 'grep -qx "android-model none" "$MW/build/params"'
iexpect "turning model flag on refuses disabled cache" 2 'now: android-model profile' GBOS_LAPIS_BUILD=16658739 GBOS_ANDROID_MODEL=profile -- --work "$MW"
finish
