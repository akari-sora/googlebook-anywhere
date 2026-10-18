#!/usr/bin/env bash
# Fast builder checks (seconds, no image build, no network): syntax, input guards, truncated-image handling.
# The full end-to-end and reproducibility runs are tests/builder/e2e.sh and tests/builder/repro.sh.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
T=$(mktemp -d "${TMPDIR:-/tmp}/gbos-test.XXXXXX"); trap 'rm -rf "$T"' EXIT
export PYTHONPYCACHEPREFIX="$T/pycache"
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass + 1)); }
bad() { echo "FAIL $1"; fail=$((fail + 1)); }
expect() {  # expect <name> <exit code> <output regex> -- command...
    local name=$1 code=$2 re=$3; shift 4
    out=$("$@" 2>&1); rc=$?
    if [ "$rc" = "$code" ] && printf '%s' "$out" | grep -Eq -- "$re"; then ok "$name"; else bad "$name (rc=$rc): $(printf '%s' "$out" | tail -n 3)"; fi
}

for f in "$ROOT"/builder/*.sh "$ROOT"/builder/kmod/*.sh "$ROOT"/builder/camera/*.sh "$ROOT"/base/*.sh "$ROOT"/install/*.sh "$ROOT"/install/stick/*.sh "$ROOT"/tests/*/*.sh "$ROOT"/tools/release/*.sh "$ROOT"/tools/mesa/*.sh "$ROOT"/tools/minigbm/*.sh; do
    [ -e "$f" ] || continue   # an unmatched glob (the release archive has no tools/release/*.sh)
    bash -n "$f" && ok "bash -n ${f#$ROOT/}" || bad "bash -n ${f#$ROOT/}"
done
for f in "$ROOT"/builder/*.py "$ROOT"/builder/lib/*.py "$ROOT"/builder/kmod/*.py "$ROOT"/base/*.py "$ROOT"/install/stick/*.py "$ROOT"/install/probe/*.py \
         "$ROOT"/profiles/*/*.py "$ROOT"/tests/*/*.py "$ROOT"/tools/mesa/*.py; do
    [ -e "$f" ] || continue
    python3 -m py_compile "$f" && ok "py_compile ${f#$ROOT/}" || bad "py_compile ${f#$ROOT/}"
done

# install/gbos-report.sh: field checks, the read-only allowlist (including refusals), and a report from saved dumps.
# No adb server and no device. The release archive includes tests/report so this still runs from the tarball.
expect "gbos-report validators, allowlist and fixtures (tests/report/report-test.sh)" 0 "report tests: [0-9]+ passed, 0 failed" -- bash "$ROOT/tests/report/report-test.sh"

# build.sh input guards (all fail in preflight, before anything is written)
touch "$T/base.img" "$T/initrd.img"; printf -- '-----BEGIN PRIVATE KEY-----\n' > "$T/key.pem"; mkdir -p "$T/ota" "$T/fw"
B=(env GBOS_BASE="$T/base.img" GBOS_INITRD="$T/initrd.img" GBOS_OTATOOLS="$T/ota" GBOS_AVB_KEY="$T/key.pem" GBOS_INPUTS="$T/in")
expect "build.sh refuses an absolute GBOS_IMAGE_NAME" 2 "plain file name" -- "${B[@]}" GBOS_OUT="$T/o1" GBOS_IMAGE_NAME=/etc/passwd.img bash "$ROOT/builder/build.sh"
expect "build.sh refuses a GBOS_IMAGE_NAME with a path" 2 "plain file name" -- "${B[@]}" GBOS_OUT="$T/o1" GBOS_IMAGE_NAME=../x.img bash "$ROOT/builder/build.sh"
expect "build.sh refuses an unknown GBOS_EC feature" 2 "GBOS_EC must be" -- "${B[@]}" GBOS_OUT="$T/o1" GBOS_EC=sensors,chardev bash "$ROOT/builder/build.sh"
expect "build.sh refuses a malformed GBOS_EC list" 2 "GBOS_EC must be" -- "${B[@]}" GBOS_OUT="$T/o1" GBOS_EC=sensors, bash "$ROOT/builder/build.sh"
mkdir -p "$T/o2"; touch "$T/o2/leftover"
expect "build.sh refuses a non-empty GBOS_OUT" 2 "new or empty directory" -- "${B[@]}" GBOS_OUT="$T/o2" bash "$ROOT/builder/build.sh"
expect "build.sh requires GBOS_INPUTS" 2 "GBOS_INPUTS" -- env -u GBOS_INPUTS -u GBOS_FIRMWARE -u GBOS_AUDIO_SRC GBOS_BASE="$T/base.img" GBOS_INITRD="$T/initrd.img" GBOS_OTATOOLS="$T/ota" GBOS_AVB_KEY="$T/key.pem" GBOS_OUT="$T/o3" bash "$ROOT/builder/build.sh"
# device profiles: build.sh refuses an invalid one before anything else; the loader's own tests
mkdir -p "$T/badprof/badprof"; sed '/^image_name = /d' "$ROOT/profiles/kano/profile.toml" > "$T/badprof/badprof/profile.toml"
expect "build.sh refuses an invalid device profile" 2 "missing key image_name" -- "${B[@]}" GBOS_OUT="$T/o4" GBOS_PROFILE="$T/badprof/badprof" bash "$ROOT/builder/build.sh"
[ ! -e "$T/o4" ] && ok "build.sh wrote nothing for the invalid profile" || bad "build.sh created GBOS_OUT for an invalid profile"
expect "device profile loader (tests/builder/profile-test.py)" 0 "profile tests: [0-9]+ passed, 0 failed" -- python3 "$ROOT/tests/builder/profile-test.py"
expect "shipped module firmware (tests/builder/firmware-test.py)" 0 "firmware tests: [0-9]+ passed, 0 failed, 0 skipped" -- python3 "$ROOT/tests/builder/firmware-test.py"
# tools/release/png-ok.py: the only binary files the public export and the release archive accept (docs/images/*.png)
expect "png-ok accepts the screenshots and refuses hidden payloads (tests/builder/png-ok-test.py)" 0 "png-ok tests: [0-9]+ passed, 0 failed" -- python3 "$ROOT/tests/builder/png-ok-test.py"
# the release archive builds from this checkout (git only: the release archive itself has no .git)
if [ -f "$ROOT/tools/release/make-release.sh" ] && [ "$(git -C "$ROOT" rev-parse --show-toplevel 2> /dev/null)" = "$(cd "$ROOT" && pwd -P)" ]; then
    grep -qF '(installer scripts; no disk/firmware images)' "$ROOT/.gitlab-ci.yml" \
        && grep -qF '(no disk/firmware images,' "$ROOT/tools/release/make-release.sh" \
        && ok "release label and make-release.sh say no disk/firmware images" \
        || bad "release label or make-release.sh still says no images"
    expect "make-release.sh builds an archive with the screenshots" 0 "googlebook-anywhere-v9.9.9.tar.gz" -- env GBOS_RELEASE_OUT="$T/rel" bash "$ROOT/tools/release/make-release.sh" v9.9.9 HEAD
    # list to a file first: under pipefail, `tar | grep -q` fails with SIGPIPE (141) when grep stops reading early (GNU tar)
    if tar -tzf "$T/rel/googlebook-anywhere-v9.9.9.tar.gz" > "$T/rel.list" 2> /dev/null && grep -q "/docs/images/kano-welcome.png$" "$T/rel.list"; then
        ok "the release archive carries docs/images"; else bad "the release archive lacks docs/images"; fi
    if grep -q "/tools/mesa/build-radv.sh$" "$T/rel.list" && grep -q "/tools/mesa/check-radv-elf.py$" "$T/rel.list" && grep -q "/tools/mesa/NOTICES.txt$" "$T/rel.list"; then
        ok "the release archive carries tools/mesa (including NOTICES.txt)"; else bad "the release archive lacks tools/mesa"; fi
    if grep -q "/tools/minigbm/NOTICES.txt$" "$T/rel.list"; then
        ok "the release archive carries tools/minigbm/NOTICES.txt"; else bad "the release archive lacks tools/minigbm/NOTICES.txt"; fi
    if grep -q "/tools/minigbm/build-libminigbm-gralloc.sh$" "$T/rel.list" || grep -q "/tools/minigbm/amdgpu_linear.c$" "$T/rel.list" || grep -q "/tools/minigbm/upstream-delta.patch$" "$T/rel.list"; then
        bad "the release archive ships git-only minigbm build sources"; else ok "the release archive omits git-only minigbm build sources"; fi
    mkdir -p "$T/rel-tree"
    tar -xzf "$T/rel/googlebook-anywhere-v9.9.9.tar.gz" -C "$T/rel-tree"
    rel_root="$T/rel-tree/googlebook-anywhere-v9.9.9"
    if [ -f "$rel_root/tools/minigbm/NOTICES.txt" ] \
        && [ ! -e "$rel_root/tools/minigbm/build-libminigbm-gralloc.sh" ] \
        && [ ! -e "$rel_root/tools/minigbm/amdgpu_linear.c" ] \
        && [ ! -e "$rel_root/tools/minigbm/upstream-delta.patch" ]; then
        ok "extracted release archive has NOTICES.txt and not the minigbm build sources"
    else
        bad "extracted release archive has NOTICES.txt and not the minigbm build sources"
    fi
    expect "extracted release archive passes tests/builder/profile-test.py" 0 "profile tests: [0-9]+ passed, 0 failed" -- python3 "$rel_root/tests/builder/profile-test.py"
    expect "extracted release archive passes tests/builder/run.sh" 0 "builder tests: [0-9]+ passed, 0 failed" -- bash "$rel_root/tests/builder/run.sh"
else
    echo "SKIP make-release.sh archive tests (no .git or make-release.sh in extracted release tree)"
fi
# The path list is read from make-release.sh itself. git archive needs a commit, and this
# snapshot may not have one, so the tree under test is the working-tree files named by that
# list (what the archive ships once those paths are committed). An extracted release has no
# make-release.sh and skips here.
if [ -f "$ROOT/tools/release/make-release.sh" ]; then
    python3 - "$ROOT" "$T/rel-work" << 'PY'
import shlex, shutil, sys
from pathlib import Path
root, dest = Path(sys.argv[1]), Path(sys.argv[2])
text = (root / "tools/release/make-release.sh").read_text()
start = text.find('git -C "$ROOT" archive')
pipe = text.find("| gzip", start)
if start < 0 or pipe < 0 or " -- " not in text[start:pipe]:
    sys.exit("make-release.sh archive path list not found")
body = text[start:pipe].split(" -- ", 1)[1].replace("\\\n", " ")
paths = shlex.split(body)
if dest.exists():
    shutil.rmtree(dest)
for rel in paths:
    src = root / rel
    target = dest / rel
    if not src.exists():
        sys.exit("release path missing: " + rel)
    if src.is_dir():
        shutil.copytree(src, target, symlinks=False)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
print("materialized", len(paths), "paths")
PY
    if [ -f "$T/rel-work/tools/minigbm/NOTICES.txt" ] \
        && [ ! -e "$T/rel-work/tools/minigbm/build-libminigbm-gralloc.sh" ] \
        && [ ! -e "$T/rel-work/tools/minigbm/amdgpu_linear.c" ] \
        && [ ! -e "$T/rel-work/tools/minigbm/upstream-delta.patch" ]; then
        ok "release path list ships minigbm NOTICES.txt and not the build sources"
    else
        bad "release path list ships minigbm NOTICES.txt and not the build sources"
    fi
    expect "working-tree release paths pass tests/builder/run.sh (including amd-hx370 checks)" 0 "builder tests: [0-9]+ passed, 0 failed" -- bash "$T/rel-work/tests/builder/run.sh"
else
    echo "SKIP release-tree run.sh (extracted release has no make-release.sh)"
fi
# synthetic repo for make-public.sh and make-release.sh guard checks (pin list from exported commit; docs/images/* and *.png)
if command -v git > /dev/null && [ -f "$ROOT/tools/release/make-public.sh" ] && [ -f "$ROOT/tools/release/make-release.sh" ]; then
    git init -q "$T/r"
    git -C "$T/r" config user.email t@example.invalid
    git -C "$T/r" config user.name t
    for d in base builder install profiles docs/images tests/builder tests/install tests/stick tests/probe tests/report tests/update tools/mesa tools/release; do
        mkdir -p "$T/r/$d"; printf 'x\n' > "$T/r/$d/f"
    done
    rm "$T/r/docs/images/f" "$T/r/tools/release/f"
    printf 'x\n' > "$T/r/README.md"; printf 'x\n' > "$T/r/LICENSE"
    mkdir -p "$T/r/tools/minigbm"
    printf 'minigbm notices\n' > "$T/r/tools/minigbm/NOTICES.txt"
    cp "$ROOT/docs/images/kano-welcome.png" "$T/r/docs/images/kano-welcome.png"
    cp "$ROOT/tools/release/png-ok.py" "$ROOT/tools/release/make-public.sh" "$ROOT/tools/release/make-release.sh" "$T/r/tools/release/"
    grep 'kano-welcome\.png$' "$ROOT/tools/release/published-images.sha256" > "$T/r/tools/release/published-images.sha256"
    printf 'NEVER_MATCH_THIS_PRIVATE_SENTINEL\n' > "$T/r/tools/release/private-patterns.txt"
    git -C "$T/r" add README.md LICENSE base builder install profiles docs tests tools/mesa tools/minigbm/NOTICES.txt tools/release/png-ok.py tools/release/published-images.sha256
    git -C "$T/r" commit -q -m good
    C_GOOD=$(git -C "$T/r" rev-parse HEAD)
    printf '%064d  docs/images/kano-welcome.png\n' 0 > "$T/r/tools/release/published-images.sha256"
    expect "make-public.sh reads the pin list from the exported commit (ignores dirty working tree)" 0 "personal-data scan clean" \
        -- bash "$T/r/tools/release/make-public.sh" "$C_GOOD" "$T/pub-good"
    expect "make-release.sh reads the pin list from the exported commit (ignores dirty working tree)" 0 "googlebook-anywhere-v9.9.0.tar.gz" \
        -- env GBOS_RELEASE_OUT="$T/rel" bash "$T/r/tools/release/make-release.sh" v9.9.0 "$C_GOOD"
    git -C "$T/r" commit -q -am badpin
    C_BADPIN=$(git -C "$T/r" rev-parse HEAD)
    grep 'kano-welcome\.png$' "$ROOT/tools/release/published-images.sha256" > "$T/r/tools/release/published-images.sha256"
    expect "make-public.sh refuses an exported commit whose pin list does not match" 1 "differs from the pinned SHA-256" \
        -- bash "$T/r/tools/release/make-public.sh" "$C_BADPIN" "$T/pub-badpin"
    expect "make-release.sh refuses an exported commit whose pin list does not match" 1 "differs from the pinned SHA-256" \
        -- env GBOS_RELEASE_OUT="$T/rel" bash "$T/r/tools/release/make-release.sh" v9.9.1 "$C_BADPIN"
    git -C "$T/r" checkout -q "$C_GOOD" -- tools/release/published-images.sha256
    printf 'text without NUL\n' > "$T/r/docs/images/note.txt"
    git -C "$T/r" add docs/images/note.txt && git -C "$T/r" commit -q -m note
    expect "make-public.sh refuses a non-PNG without NUL in docs/images" 1 "refusing: docs/images/note.txt: not a PNG" \
        -- bash "$T/r/tools/release/make-public.sh" HEAD "$T/pub-note"
    expect "make-release.sh refuses a non-PNG without NUL in docs/images" 1 "refusing: .*/docs/images/note.txt: not a PNG" \
        -- env GBOS_RELEASE_OUT="$T/rel" bash "$T/r/tools/release/make-release.sh" v9.9.2 HEAD
    git -C "$T/r" rm -q docs/images/note.txt
    printf 'text without NUL\n' > "$T/r/docs/elsewhere.png"
    git -C "$T/r" add docs/elsewhere.png && git -C "$T/r" commit -q -m elsewhere
    expect "make-public.sh sends a .png outside docs/images without NUL through png-ok" 1 "refusing: docs/elsewhere.png: not a PNG" \
        -- bash "$T/r/tools/release/make-public.sh" HEAD "$T/pub-else"
    expect "make-release.sh sends a .png outside docs/images without NUL through png-ok" 1 "refusing: .*/docs/elsewhere.png: not a PNG" \
        -- env GBOS_RELEASE_OUT="$T/rel" bash "$T/r/tools/release/make-release.sh" v9.9.3 HEAD
    git -C "$T/r" rm -q docs/elsewhere.png
    printf 'text without NUL\n' > "$T/r/docs/elsewhere.PNG"
    git -C "$T/r" add docs/elsewhere.PNG && git -C "$T/r" commit -q -m elsewhere-upper
    expect "make-public.sh sends an uppercase .PNG outside docs/images without NUL through png-ok" 1 "refusing: docs/elsewhere.PNG: not a PNG" \
        -- bash "$T/r/tools/release/make-public.sh" HEAD "$T/pub-else-up"
    expect "make-release.sh sends an uppercase .PNG outside docs/images without NUL through png-ok" 1 "refusing: .*/docs/elsewhere.PNG: not a PNG" \
        -- env GBOS_RELEASE_OUT="$T/rel" bash "$T/r/tools/release/make-release.sh" v9.9.4 HEAD
    git -C "$T/r" rm -q docs/elsewhere.PNG
    cp "$ROOT/docs/images/kano-welcome.png" "$T/r/docs/elsewhere.png"
    cp "$ROOT/docs/images/kano-welcome.png" "$T/r/docs/images/renamed.txt"
    h=$(awk '{print $1}' "$T/r/tools/release/published-images.sha256")
    printf '%s  docs/images/kano-welcome.png\n%s  docs/elsewhere.png\n' "$h" "$h" > "$T/r/tools/release/published-images.sha256"
    git -C "$T/r" add docs/elsewhere.png tools/release/published-images.sha256 && git -C "$T/r" commit -q -m pinned-elsewhere
    expect "make-public.sh refuses a pinned .png outside docs/images" 1 "refusing: docs/elsewhere.png: only docs/images/\*\.png" \
        -- bash "$T/r/tools/release/make-public.sh" HEAD "$T/pub-pelse"
    expect "make-release.sh refuses a pinned .png outside docs/images" 1 "refusing: .*/docs/elsewhere.png: only docs/images/\*\.png" \
        -- env GBOS_RELEASE_OUT="$T/rel" bash "$T/r/tools/release/make-release.sh" v9.9.5 HEAD
    git -C "$T/r" rm -q docs/elsewhere.png
    printf '%s  docs/images/kano-welcome.png\n%s  docs/images/renamed.txt\n' "$h" "$h" > "$T/r/tools/release/published-images.sha256"
    git -C "$T/r" add docs/images/renamed.txt tools/release/published-images.sha256 && git -C "$T/r" commit -q -m pinned-txt
    expect "make-public.sh refuses a non-.png file in docs/images even if valid PNG and pinned" 1 "refusing: docs/images/renamed.txt: only docs/images/\*\.png" \
        -- bash "$T/r/tools/release/make-public.sh" HEAD "$T/pub-ptxt"
    expect "make-release.sh refuses a non-.png file in docs/images even if valid PNG and pinned" 1 "refusing: .*/docs/images/renamed.txt: only docs/images/\*\.png" \
        -- env GBOS_RELEASE_OUT="$T/rel" bash "$T/r/tools/release/make-release.sh" v9.9.6 HEAD
    git -C "$T/r" rm -q docs/images/renamed.txt
    mkdir -p "$T/r/docs/images/sub"
    cp "$ROOT/docs/images/kano-welcome.png" "$T/r/docs/images/sub/shot.png"
    printf '%s  docs/images/kano-welcome.png\n%s  docs/images/sub/shot.png\n' "$h" "$h" > "$T/r/tools/release/published-images.sha256"
    git -C "$T/r" add docs/images/sub/shot.png tools/release/published-images.sha256 && git -C "$T/r" commit -q -m pinned-sub
    expect "make-public.sh refuses a pinned .png in a subdirectory of docs/images" 1 "refusing: docs/images/sub/shot.png: only docs/images/\*\.png" \
        -- bash "$T/r/tools/release/make-public.sh" HEAD "$T/pub-psub"
    expect "make-release.sh refuses a pinned .png in a subdirectory of docs/images" 1 "refusing: .*/docs/images/sub/shot.png: only docs/images/\*\.png" \
        -- env GBOS_RELEASE_OUT="$T/rel" bash "$T/r/tools/release/make-release.sh" v9.9.7 HEAD
fi
# base/gbos_base.py fetch-zip: ci.android.com's signed redirect expires (300 s) while the members are fetched
expect "fetch-zip re-resolves an expired signed URL (tests/builder/fetch-zip-test.py)" 0 "fetch-zip tests: [0-9]+ passed, 0 failed" -- python3 "$ROOT/tests/builder/fetch-zip-test.py"

# vendor.sh: GBOS_ADB_PUBKEY must be an adb public key, never a private key or another file
mkdir -p "$T/audio"; printf 'x' > "$T/audio/HiFi.conf"; printf 'y' > "$T/audio/sof-nau8825.card_settings"
V=(env GBOS_BASE="$T/nonexistent.img" GBOS_INITRD="$T/initrd.img" GBOS_OUT="$T/v" GBOS_OTATOOLS="$T/ota" GBOS_AVB_KEY="$T/key.pem" GBOS_PROFILE="$ROOT/profiles/kano" GBOS_FIRMWARE="$T/fw" GBOS_AUDIO_SRC="$T/audio")
printf -- '-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n' > "$T/adbkey"
expect "vendor.sh refuses an adb private key" 2 "must be an adb public key" -- "${V[@]}" GBOS_ADB_PUBKEY="$T/adbkey" bash "$ROOT/builder/vendor.sh"
printf 'QAAAAPRIVATEKEYlookalike\nsecond line\n' > "$T/two-lines.pub"
expect "vendor.sh refuses a multi-line adb key file" 2 "must be an adb public key" -- "${V[@]}" GBOS_ADB_PUBKEY="$T/two-lines.pub" bash "$ROOT/builder/vendor.sh"
# a structurally valid adb public key: base64 of u32 64, n0inv, n[64], rr[64], exponent 65537
python3 - "$T" << 'PY'
import base64, os, struct, sys
k = struct.pack("<II", 64, 0x12345678) + os.urandom(256) + os.urandom(256) + struct.pack("<I", 65537)
b = base64.b64encode(k).decode()
d = sys.argv[1]
open(f"{d}/adbkey.pub", "w").write(b + " user@host\n")
open(f"{d}/nonl2.pub", "w").write(b + " user@host\nsecond-line-without-newline")
open(f"{d}/cr.pub", "w").write(b + " user@host\r\n")
open(f"{d}/nul.pub", "wb").write(b.encode() + b" user\0host\n")
open(f"{d}/badb64.pub", "w").write(b[:-4] + "!!!! user@host\n")
open(f"{d}/short.pub", "w").write(base64.b64encode(k[:-4]).decode() + " user@host\n")
open(f"{d}/badwords.pub", "w").write(base64.b64encode(struct.pack("<I", 32) + k[4:]).decode() + "\n")
PY
for f in nonl2 cr nul badb64 short badwords; do
    expect "vendor.sh refuses an adb key file: $f" 2 "must be an adb public key" -- "${V[@]}" GBOS_ADB_PUBKEY="$T/$f.pub" bash "$ROOT/builder/vendor.sh"
done
# the valid key passes the key check and the build stops at the next check (the fake audio sources)
expect "vendor.sh accepts a valid adb public key (reaches the audio check)" 1 "do not match" -- "${V[@]}" GBOS_ADB_PUBKEY="$T/adbkey.pub" bash "$ROOT/builder/vendor.sh"
expect "vendor.sh refuses audio sources that differ from the pinned list" 1 "do not match" -- "${V[@]}" bash "$ROOT/builder/vendor.sh"
GOOD_KCFG='CONFIG_KALLSYMS=y
CONFIG_MODULES=y
CONFIG_MODULE_UNLOAD=y
CONFIG_MODVERSIONS=y
CONFIG_IKCONFIG=y
CONFIG_IKCONFIG_PROC=y
CONFIG_CMDLINE_BOOL=y
CONFIG_CMDLINE="console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"
# CONFIG_CMDLINE_OVERRIDE is not set
CONFIG_BLK_DEV_NVME=y
CONFIG_INTEL_MEI_ME=y
CONFIG_DRM_AMDGPU=m
'
write_kdir_cfg() { # write_kdir_cfg <kdir> <cfg_text>
    python3 - "$ROOT/builder/lib" "$1" "$2" << 'PY'
import gzip, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import devprofile
kd = Path(sys.argv[2])
cfg = sys.argv[3]
(kd / ".config").write_text(cfg)
vmlinux = devprofile.build_test_vmlinux_elf(cfg)
(kd / "bzImage").write_bytes(devprofile.wrap_hdrs_bzimage(gzip.compress(vmlinux)))
PY
}
mkdir -p "$T/devk/modules"
expect "vendor.sh refuses GBOS_DEV_KERNEL_DIR without bzImage" 2 "must have bzImage" -- "${V[@]}" GBOS_DEV_KERNEL_DIR="$T/devk" bash "$ROOT/builder/vendor.sh"
printf 'MZ' > "$T/devk/bzImage"
expect "vendor.sh refuses GBOS_DEV_KERNEL_DIR without modules/*.ko" 2 "must have modules" -- "${V[@]}" GBOS_DEV_KERNEL_DIR="$T/devk" bash "$ROOT/builder/vendor.sh"
printf 'x' > "$T/devk/modules/test.ko"; printf 'test.ko:\n' > "$T/devk/modules/modules.dep"
expect "vendor.sh refuses GBOS_DEV_KERNEL_DIR without .config" 2 "missing .config" -- "${V[@]}" GBOS_DEV_KERNEL_DIR="$T/devk" bash "$ROOT/builder/vendor.sh"
write_kdir_cfg "$T/devk" "$GOOD_KCFG"
expect "vendor.sh refuses GBOS_DEV_KERNEL_DIR without System.map when CONFIG_BLK_DEV_NVME=y" 2 "missing System.map" -- "${V[@]}" GBOS_DEV_KERNEL_DIR="$T/devk" bash "$ROOT/builder/vendor.sh"
printf 'ffffffff81000999 t nvme_init\nffffffff81000100 t mei_me_driver_init\n' > "$T/devk/System.map"
expect "vendor.sh refuses GBOS_DEV_KERNEL_DIR with forged System.map mismatched against bzImage vmlinux" 2 "System.map does not match" -- "${V[@]}" GBOS_DEV_KERNEL_DIR="$T/devk" bash "$ROOT/builder/vendor.sh"
printf 'ffffffff81000000 t nvme_init\nffffffff81000100 t mei_me_driver_init\n' > "$T/devk/System.map"
expect "vendor.sh accepts a structurally valid GBOS_DEV_KERNEL_DIR (reaches the audio check)" 1 "do not match" -- "${V[@]}" GBOS_DEV_KERNEL_DIR="$T/devk" bash "$ROOT/builder/vendor.sh"

# image.py refuses an output name with a path before creating anything
expect "image.py refuses GBOS_IMAGE_NAME with a path" 1 "plain file name" -- env GBOS_OUT="$T/i" GBOS_IMAGE_NAME=/tmp/x.img python3 "$ROOT/builder/image.py"
[ ! -e "$T/i" ] && ok "image.py created nothing for a refused name" || bad "image.py created $T/i"

# lp-dump.py: a logical partition whose extent runs past the end of a truncated image must fail, not loop; one that
# lies inside the image but outside super must fail too, before any output
python3 - "$T" << 'PY'
import struct, sys, uuid
from pathlib import Path
t = Path(sys.argv[1])
def make(path, extent_sectors, file_bytes, super_sectors=4096):
    img = bytearray(file_bytes)
    super_lba = 64
    hdr = bytearray(92); hdr[:8] = b"EFI PART"; struct.pack_into("<QII", hdr, 72, 2, 1, 128); img[512:604] = hdr
    e = bytearray(128); e[:16] = uuid.uuid4().bytes_le; struct.pack_into("<QQ", e, 32, super_lba, super_lba + super_sectors - 1)
    e[56:56 + 10] = "super".encode("utf-16le"); img[1024:1152] = e
    so = super_lba * 512
    img[so + 4096:so + 4100] = b"gDla"
    tables = bytearray(52 + 24)
    tables[:13] = b"vendor_dlkm_a"; struct.pack_into("<IIII", tables, 36, 0, 0, 1, 0)
    struct.pack_into("<QIQI", tables, 52, extent_sectors, 0, 2048, 0)
    h = bytearray(256); h[:4] = b"0PLA"; struct.pack_into("<I", h, 8, 256); struct.pack_into("<I", h, 44, len(tables))
    struct.pack_into("<III", h, 80, 0, 1, 52); struct.pack_into("<III", h, 92, 52, 1, 24)
    img[so + 12288:so + 12288 + 256] = h; img[so + 12288 + 256:so + 12288 + 256 + len(tables)] = tables
    path.write_bytes(bytes(img))
make(t / "lp-ok.img", 8, (64 + 2048 + 8) * 512 + 4096)
make(t / "lp-trunc.img", 4096, (64 + 2048 + 8) * 512 + 4096, super_sectors=8192)
make(t / "lp-outside.img", 4096, (64 + 2048 + 4096) * 512 + 4096)
PY
expect "lp-dump.py extracts a partition inside the image" 0 "vendor_dlkm_a 4096" -- python3 "$ROOT/builder/lib/lp-dump.py" "$T/lp-ok.img" "$T/lp1" vendor_dlkm_a
expect "lp-dump.py stops on an extent past the end of the image" 1 "past the end" -- timeout 20 python3 "$ROOT/builder/lib/lp-dump.py" "$T/lp-trunc.img" "$T/lp2" vendor_dlkm_a
[ ! -e "$T/lp2/vendor_dlkm_a.img" ] && ok "lp-dump.py leaves no partial output named like a result" || bad "lp-dump.py left a result file behind"
expect "lp-dump.py refuses an extent inside the image but outside super" 1 "outside super" -- python3 "$ROOT/builder/lib/lp-dump.py" "$T/lp-outside.img" "$T/lp3" vendor_dlkm_a
[ -z "$(ls -A "$T/lp3")" ] && ok "lp-dump.py wrote nothing for an extent outside super" || bad "lp-dump.py wrote $(ls "$T/lp3")"

# base/gbos_base.py writes only regular files: a symlink (even to a regular file) is refused and its target is not cut,
# a device or a FIFO is refused, a regular file is truncated only after the check; lp-build refuses a non-regular output
# before reading anything
printf 'keep-me' > "$T/target"; ln -s "$T/target" "$T/link"; mkfifo "$T/fifo"; printf 'abcdef' > "$T/regular"
expect "gbos_base refuses to write through a symlink to a regular file" 0 "^symlink refused, target intact$" -- timeout 20 python3 - "$ROOT/base" "$T" << 'PY'
import os, sys; from pathlib import Path
sys.path.insert(0, sys.argv[1]); import gbos_base as g
t = Path(sys.argv[2]); res = []
for how in (lambda: g.open(t / "link", "wb"), lambda: (t / "link").write_bytes(b"x"), lambda: g.open(t / "link", "r+b")):
    try: how(); res.append("opened")
    except SystemExit as e: res.append(e.code)
print("symlink refused, target intact" if res == [1, 1, 1] and (t / "target").read_bytes() == b"keep-me" else f"{res} {(t / 'target').read_bytes()!r}")
PY
expect "gbos_base refuses a character device and a FIFO (without blocking)" 0 "^refused both$" -- timeout 20 python3 - "$ROOT/base" "$T" << 'PY'
import sys; from pathlib import Path
sys.path.insert(0, sys.argv[1]); import gbos_base as g
res = []
for path in ("/dev/null", str(Path(sys.argv[2]) / "fifo")):
    try: g.open(path, "wb"); res.append("opened")
    except SystemExit as e: res.append(e.code)
print("refused both" if res == [1, 1] else res)
PY
expect "gbos_base truncates a regular file only for w, keeps it for r+" 0 "^ok$" -- python3 - "$ROOT/base" "$T" << 'PY'
import sys; from pathlib import Path
sys.path.insert(0, sys.argv[1]); import gbos_base as g
f = Path(sys.argv[2]) / "regular"
with g.open(f, "r+b") as o: o.write(b"X")
a = f.read_bytes()
with g.open(f, "wb") as o: o.write(b"y")
with g.open(Path(sys.argv[2]) / "new-file", "wb") as o: o.write(b"z")
print("ok" if (a, f.read_bytes(), (Path(sys.argv[2]) / "new-file").read_bytes()) == (b"Xbcdef", b"y", b"z") else (a, f.read_bytes()))
PY
expect "gbos_base lp-build refuses a symlink output before reading anything" 1 "not a regular file" -- python3 "$ROOT/base/gbos_base.py" lp-build "$T/nonexistent.json" "$T" "$T/link"
expect "gbos_base lp-build refuses a device output" 1 "not a regular file" -- python3 "$ROOT/base/gbos_base.py" lp-build "$T/nonexistent.json" "$T" /dev/null
[ "$(cat "$T/target")" = keep-me ] && ok "lp-build left the symlink's target alone" || bad "lp-build changed the symlink's target"
if grep -nE 'shutil\.(copy|copyfile|copy2|copytree|move)\(' "$ROOT/base/gbos_base.py"; then bad "gbos_base.py copies files outside the checked open"; else ok "gbos_base.py has no shutil.copy*/move (every write goes through the checked open)"; fi

# rebuild.write_configs refuses a file capability it cannot carry into the fs_config
expect "rebuild.write_configs refuses security.capability" 1 "security.capability" -- python3 - "$ROOT/builder/lib" "$T" << 'PY'
import sys; from pathlib import Path
sys.path.insert(0, sys.argv[1]); import rebuild
root = Path(sys.argv[2]) / "tree"; (root / "bin").mkdir(parents=True); (root / "bin/x").write_text("x")
m = {"": dict(mode=0o755, uid=0, gid=0, selinux="u:object_r:vendor_file:s0", caps=None),
     "bin": dict(mode=0o755, uid=0, gid=2000, selinux="u:object_r:vendor_file:s0", caps=None),
     "bin/x": dict(mode=0o755, uid=0, gid=2000, selinux="u:object_r:vendor_file:s0", caps=b"\x00\x00\x00\x02")}
rebuild.write_configs("/vendor", root, m, root.parent / "fc", root.parent / "fs")
PY

# builder/kmod: the profile's extra kernel modules (KANO: ac.ko, the ACPI AC adapter). The full build needs the network
# and about 9 GB; only the guards here.
expect "build-ac.sh needs a work and an output directory" 1 "usage" -- bash "$ROOT/builder/kmod/build-ac.sh"
expect "build-ac.sh refuses a path with a space" 2 "must not contain spaces" -- bash "$ROOT/builder/kmod/build-ac.sh" "$T/a b" "$T/out"
expect "build-ac.sh refuses a path with ':'" 2 "must not contain spaces or ':'" -- bash "$ROOT/builder/kmod/build-ac.sh" "$T/a:b" "$T/out"
mkdir -p "$T/acout"; touch "$T/acout/ac.ko"
expect "build-ac.sh never overwrites an existing ac.ko" 2 "already exists" -- bash "$ROOT/builder/kmod/build-ac.sh" "$T/acw" "$T/acout"
n=$(grep -cE '^(BZIMAGE|SYMVERS|CLANG_TREE|RUST_TREE|BINDGEN|BC|CONFIG|AC_KO)_SHA=[0-9a-f]{64}$' "$ROOT/builder/kmod/build-ac.sh")
[ "$n" = 8 ] && ok "build-ac.sh pins eight SHA-256 values" || bad "build-ac.sh pins: $n of 8 well-formed"
grep -qE '^KSRC_COMMIT=[0-9a-f]{40}$' "$ROOT/builder/kmod/build-ac.sh" && ok "build-ac.sh pins the kernel commit" || bad "build-ac.sh kernel commit pin"
[ "$(sed -n 's/^AC_KO_SHA=\([0-9a-f]\{64\}\)$/\1/p' "$ROOT/builder/kmod/build-ac.sh" | wc -l | tr -d ' ')" = 1 ] \
    && ok "vendor.sh can read the one ac.ko pin" || bad "vendor.sh cannot read exactly one ac.ko pin"
printf 'not an elf' > "$T/fake.ko"; : > "$T/symvers"
expect "check-ko.py refuses a file that is not a relocatable ELF" 1 "not a 64-bit" -- python3 "$ROOT/builder/kmod/check-ko.py" "$T/fake.ko" "$T/symvers" "SMP "
[ "$(python3 "$ROOT/builder/lib/devprofile.py" kmods "$ROOT/profiles/kano" | tr '\n' ' ')" = "ac gbos_wakefilter " ] && ok "the KANO profile ships ac.ko and gbos_wakefilter.ko" \
    || bad "the KANO profile does not list kmods = [\"ac\", \"gbos_wakefilter\"]"
# gbos_wakefilter.ko: built in build-ac.sh's work dir; only the guards and the pin here
expect "build-gbos_wakefilter.sh needs a work and an output directory" 1 "usage" -- bash "$ROOT/builder/kmod/build-gbos_wakefilter.sh"
expect "build-gbos_wakefilter.sh refuses a path with ':'" 2 "must not contain spaces or ':'" -- bash "$ROOT/builder/kmod/build-gbos_wakefilter.sh" "$T/a:b" "$T/out"
mkdir -p "$T/wfout"; touch "$T/wfout/gbos_wakefilter.ko"
expect "build-gbos_wakefilter.sh never overwrites an existing module" 2 "already exists" -- bash "$ROOT/builder/kmod/build-gbos_wakefilter.sh" "$T/wfw" "$T/wfout"
if [ "$(uname -sm)" = "Linux x86_64" ]; then
    expect "build-gbos_wakefilter.sh refuses a work dir build-ac.sh did not prepare" 2 "run build-ac.sh first" -- bash "$ROOT/builder/kmod/build-gbos_wakefilter.sh" "$T/wfw" "$T/wfout2"
fi
[ "$(grep -cE '^WAKEFILTER_KO_SHA=[0-9a-f]{64}$' "$ROOT/builder/kmod/build-gbos_wakefilter.sh")" = 1 ] && grep -qF 's/^WAKEFILTER_KO_SHA=' "$ROOT/builder/vendor.sh" \
    && ok "build-gbos_wakefilter.sh pins gbos_wakefilter.ko and vendor.sh checks that pin" || bad "gbos_wakefilter.ko pin"
[ "$(sed -n 's/^KSRC_COMMIT=//p' "$ROOT/builder/kmod/build-ac.sh")" = "$(sed -n 's/^KSRC_COMMIT=//p' "$ROOT/builder/kmod/build-gbos_wakefilter.sh")" ] \
    && ok "build-ac.sh and build-gbos_wakefilter.sh name the same kernel commit" || bad "kernel commit differs between build-ac.sh and build-gbos_wakefilter.sh"
mkdir -p "$T/wfprof/kano"; cp "$ROOT/profiles/kano/"* "$T/wfprof/kano/"
sed 's/^power_button = .*/power_button = "stock"/' "$ROOT/profiles/kano/profile.toml" > "$T/wfprof/kano/profile.toml"
expect "devprofile.py refuses gbos_wakefilter without the ACPI power button" 1 "needs input.power_button = acpi" -- python3 "$ROOT/builder/lib/devprofile.py" check "$T/wfprof/kano"
mkdir -p "$T/kmprof/kano"; cp "$ROOT/profiles/kano/"* "$T/kmprof/kano/"
sed 's/^kmods = .*/kmods = ["ac", "bogus"]/' "$ROOT/profiles/kano/profile.toml" > "$T/kmprof/kano/profile.toml"
expect "devprofile.py refuses an unknown kmod" 1 "kmods: expected distinct names" -- python3 "$ROOT/builder/lib/devprofile.py" check "$T/kmprof/kano"
mkdir -p "$T/in/firmware"
expect "build.sh requires GBOS_KMOD_AC for a profile that ships ac.ko" 2 "set GBOS_KMOD_AC" -- "${B[@]}" GBOS_OUT="$T/o5" GBOS_SKIP_SELFTEST=1 bash "$ROOT/builder/build.sh"
expect "build.sh refuses a GBOS_KMOD_AC that is not a file" 2 "GBOS_KMOD_AC is not a file" -- "${B[@]}" GBOS_OUT="$T/o6" GBOS_SKIP_SELFTEST=1 GBOS_KMOD_AC="$T/no-ac.ko" bash "$ROOT/builder/build.sh"
touch "$T/ac-any.ko"
expect "build.sh requires GBOS_KMOD_WAKEFILTER for a profile that ships it" 2 "set GBOS_KMOD_WAKEFILTER" -- "${B[@]}" GBOS_OUT="$T/o7" GBOS_SKIP_SELFTEST=1 GBOS_KMOD_AC="$T/ac-any.ko" bash "$ROOT/builder/build.sh"
expect "build.sh refuses a GBOS_KMOD_WAKEFILTER that is not a file" 2 "GBOS_KMOD_WAKEFILTER is not a file" -- "${B[@]}" GBOS_OUT="$T/o8" GBOS_SKIP_SELFTEST=1 GBOS_KMOD_AC="$T/ac-any.ko" GBOS_KMOD_WAKEFILTER="$T/no-wf.ko" bash "$ROOT/builder/build.sh"
# profiles/generic-x86 (machines without a GPU driver: GOP framebuffer + SwiftShader)
expect "the generic-x86 profile validates" 0 "profile generic-x86 ok" -- python3 "$ROOT/builder/lib/devprofile.py" check "$ROOT/profiles/generic-x86"
mkdir -p "$T/genprof/generic-x86"; sed 's/^kmods = .*/kmods = []/' "$ROOT/profiles/generic-x86/profile.toml" > "$T/genprof/generic-x86/profile.toml"
expect "devprofile.py refuses display.stack gop-swiftshader without the gbos_efifb kmod" 1 "go together" -- python3 "$ROOT/builder/lib/devprofile.py" check "$T/genprof/generic-x86"
expect "build.sh refuses the generic profile without vulkan.pastel.so" 2 "needs vulkan.pastel.so" -- "${B[@]}" GBOS_OUT="$T/o7" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/generic-x86" bash "$ROOT/builder/build.sh"
# profiles/amd-hx370: default safe entry (GOP + SwiftShader) + experimental native amdgpu entry.
expect "the amd-hx370 profile validates" 0 "profile amd-hx370 ok" -- python3 "$ROOT/builder/lib/devprofile.py" check "$ROOT/profiles/amd-hx370"
expect "build.sh refuses amd-hx370 without the self-built kernel" 2 "self-built Track 2 GKI" -- "${B[@]}" GBOS_OUT="$T/o-amd" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" bash "$ROOT/builder/build.sh"
mkdir -p "$T/devamd/modules"
printf 'MZ' > "$T/devamd/bzImage"
printf 'x' > "$T/devamd/modules/test.ko"
printf 'test.ko:\n' > "$T/devamd/modules/modules.dep"
expect "build.sh refuses an amd-hx370 kernel dir without amdgpu.ko" 2 "no modules/amdgpu.ko" -- "${B[@]}" GBOS_OUT="$T/o-amd2" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" bash "$ROOT/builder/build.sh"
printf 'x' > "$T/devamd/modules/amdgpu.ko"
printf 'amdgpu.ko:\ntest.ko:\n' > "$T/devamd/modules/modules.dep"
expect "build.sh refuses an amd-hx370 kernel dir without .config" 2 "missing .config" -- "${B[@]}" GBOS_OUT="$T/o-amd2b" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" bash "$ROOT/builder/build.sh"
write_kdir_cfg "$T/devamd" "$GOOD_KCFG"
expect "build.sh refuses an amd-hx370 kernel dir without System.map when CONFIG_BLK_DEV_NVME=y" 2 "missing System.map" -- "${B[@]}" GBOS_OUT="$T/o-amd2sys" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" bash "$ROOT/builder/build.sh"
printf 'ffffffff81000999 t nvme_init\nffffffff81000100 t mei_me_driver_init\n' > "$T/devamd/System.map"
expect "build.sh refuses an amd-hx370 kernel dir with forged System.map" 2 "System.map does not match" -- "${B[@]}" GBOS_OUT="$T/o-amd2sysbad" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" bash "$ROOT/builder/build.sh"
printf 'ffffffff81000000 t nvme_init\nffffffff81000100 t mei_me_driver_init\n' > "$T/devamd/System.map"
write_kdir_cfg "$T/devamd" "${GOOD_KCFG}CONFIG_TCG_CRB=y
"
expect "build.sh refuses an amd-hx370 kernel dir with built-in CONFIG_TCG_CRB=y" 2 "CONFIG_TCG_CRB=y is forbidden" -- "${B[@]}" GBOS_OUT="$T/o-amd2c" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" bash "$ROOT/builder/build.sh"
write_kdir_cfg "$T/devamd" "${GOOD_KCFG/CONFIG_DRM_AMDGPU=m/CONFIG_DRM_AMDGPU=y}"
expect "build.sh refuses an amd-hx370 kernel dir with built-in CONFIG_DRM_AMDGPU=y" 2 "CONFIG_DRM_AMDGPU=y is forbidden" -- "${B[@]}" GBOS_OUT="$T/o-amd2d" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" bash "$ROOT/builder/build.sh"
write_kdir_cfg "$T/devamd" "$GOOD_KCFG"
expect "build.sh accepts a compliant amd-hx370 kernel dir (reaches the audio check)" 2 "GBOS_AUDIO_SRC is not a directory" -- "${B[@]}" GBOS_OUT="$T/o-amd3" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" bash "$ROOT/builder/build.sh"
grep -q 'prof\["model"\]\["id"\] == "amd-hx370"' "$ROOT/builder/vendor.sh" \
    && grep -q 'on early-boot' "$ROOT/builder/vendor.sh" \
    && ! grep -q 'on property:ro\.boot\.gbos_amd=' "$ROOT/builder/vendor.sh" \
    && ok "vendor.sh runs amd-hx370-graphics.sh once on early-boot (no duplicate early_amd trigger)" || bad "amd-hx370 early-boot trigger"
# Execute /vendor/etc/amd-hx370-graphics.sh against safe and experimental native boot entries + 2-GPU sysfs fixtures
expect "amd-hx370-graphics.sh safe vs native entry behavior and sysfs DRIVER=amdgpu card selection" 0 "^gfx-sh ok$" -- python3 - "$ROOT/builder/vendor.sh" "$T" << 'PY'
import os, subprocess, sys
from pathlib import Path

vendor_sh = Path(sys.argv[1]).read_text()
tmp = Path(sys.argv[2]) / "gfx-test"
tmp.mkdir(parents=True, exist_ok=True)
gfx_body = vendor_sh.split('(tree / "etc/amd-hx370-graphics.sh").write_text("""', 1)[1].split('""")', 1)[0]
assert gfx_body.startswith("#!/system/bin/sh\n"), gfx_body[:40]
gfx_script = tmp / "amd-hx370-graphics.sh"
gfx_script.write_text("#!/bin/sh\n" + gfx_body.split("\n", 1)[1].replace("\\\\", "\\"))
gfx_script.chmod(0o755)


def run_case(name, cmdline, cards, efifb_cards=(), delayed_cards=(), omit_uevent=False, return_kmsg=False, modprobe_rc=0):
    case = tmp / name
    case.mkdir(parents=True, exist_ok=True)
    log = case / "calls.log"
    drm = case / "drm"
    drm.mkdir(parents=True, exist_ok=True)
    for tool, rc_val in (("modprobe", modprobe_rc), ("setprop", 0)):
        p = case / tool
        p.write_text(f"#!/bin/sh\necho \"{tool} $*\" >> {log}\nexit {rc_val}\n")
        p.chmod(0o755)
    ef = case / "efifb"
    ef_body = [f"#!/bin/sh\necho \"efifb $*\" >> {log}"]
    for cname, drv, conns in efifb_cards:
        ef_body.append(f"mkdir -p {drm}/{cname}/device")
        if not omit_uevent:
            ef_body.append(f"printf 'DRIVER={drv}\\n' > {drm}/{cname}/device/uevent")
        for conn, st in conns:
            ef_body.append(f"mkdir -p {drm}/{cname}/{conn}")
            ef_body.append(f"printf '{st}\\n' > {drm}/{cname}/{conn}/status")
    ef_body.append("exit 0\n")
    ef.write_text("\n".join(ef_body))
    ef.chmod(0o755)
    sl = case / "sleep"
    sl_count = case / "sleep.count"
    sl_body = [
        "#!/bin/sh",
        f"n=$(cat {sl_count} 2>/dev/null || echo 0)",
        "n=$((n + 1))",
        f"echo $n > {sl_count}",
        f"echo \"sleep $*\" >> {log}",
    ]
    if delayed_cards:
        sl_body.append("if [ \"$n\" -ge 2 ]; then")
        for cname, drv, conns in delayed_cards:
            sl_body.append(f"  mkdir -p {drm}/{cname}/device")
            sl_body.append(f"  printf 'DRIVER={drv}\\nPCI_ID=1002:150E\\n' > {drm}/{cname}/device/uevent")
            for conn, st in conns:
                sl_body.append(f"  mkdir -p {drm}/{cname}/{conn}")
                sl_body.append(f"  printf '{st}\\n' > {drm}/{cname}/{conn}/status")
        sl_body.append("fi")
    sl_body.append("exit 0\n")
    sl.write_text("\n".join(sl_body))
    sl.chmod(0o755)
    gp = case / "getprop"
    gp.write_text("#!/bin/sh\necho \"val-$1\"\n")
    gp.chmod(0o755)
    cmd_f = case / "cmdline"
    cmd_f.write_text(cmdline + "\n")
    mods_f = case / "modules"
    mods_f.write_text("amdgpu 12345 0 - Live 0x0\n")
    kmsg_f = case / "kmsg"
    for cname, drv, conns in cards:
        cdir = drm / cname
        (cdir / "device").mkdir(parents=True, exist_ok=True)
        if not omit_uevent:
            (cdir / "device" / "uevent").write_text(f"DRIVER={drv}\nPCI_ID=1002:150E\n")
        for conn, st in conns:
            (cdir / conn).mkdir(parents=True, exist_ok=True)
            (cdir / conn / "status").write_text(st + "\n")
    env = dict(os.environ,
               GBOS_KMSG_FILE=str(kmsg_f),
               GBOS_CMDLINE_FILE=str(cmd_f),
               GBOS_MODULES_FILE=str(mods_f),
               GBOS_SYSFS_DRM=str(drm),
               GBOS_MODPROBE_BIN=str(case / "modprobe"),
               GBOS_SETPROP_BIN=str(case / "setprop"),
               GBOS_GETPROP_BIN=str(gp),
               GBOS_EFIFB_SH=str(case / "efifb"),
               GBOS_SLEEP_BIN=str(case / "sleep"),
               GBOS_SH_BIN="/bin/sh")
    subprocess.run(["/bin/sh", str(gfx_script)], env=env, capture_output=True, text=True, check=True)
    calls = log.read_text().splitlines() if log.exists() else []
    if return_kmsg:
        return calls, (kmsg_f.read_text() if kmsg_f.exists() else "")
    return calls


# 1. Safe default entry (module_blacklist includes amdgpu, no amdgpu.modeset=1): never loads amdgpu, starts efifb BEFORE setting vendor.hwc.drm.device, sets SwiftShader properties.
#    gbos_efifb.ko gives BOTH devices a Virtual connector and DRIVER=gbos_efifb (card0 allocator, card1 scanout).
#    The selector must keep the higher card index, not the first match.
safe_calls, safe_kmsg = run_case(
    "safe",
    "console=tty0 initcall_blacklist=nvme_init,mei_me_driver_init efi=noruntime module_blacklist=tpm,amdgpu loglevel=4",
    [("card0", "amdgpu", [("card0-DP-1", "connected")])],
    efifb_cards=[("card0", "gbos_efifb", [("card0-Virtual-1", "connected")]),
                 ("card1", "gbos_efifb", [("card1-Virtual-1", "connected")])],
    return_kmsg=True,
)
assert safe_calls == [
    "setprop vendor.gbos.radv_probe radeon",
    "setprop ro.hardware.vulkan pastel",
    "setprop ro.hwui.use_vulkan false",
    "setprop debug.renderengine.vulkan false",
    "efifb ",
    "setprop vendor.hwc.drm.device /dev/dri/card1",
], f"unexpected safe_calls: {safe_calls}"
assert "safe entry: amdgpu blacklisted" in safe_kmsg and "safe entry: ro.hardware.vulkan=" in safe_kmsg, (
    f"safe path must keep both kmsg lines ({safe_kmsg!r})"
)

# 1b. Safe entry when card0 is already occupied before gbos_efifb.ko loads.
#     Both gbos_efifb devices expose Virtual-* (card1 allocator, card2 scanout) -> selects /dev/dri/card2
safe_shifted = run_case(
    "safe-shifted-cards",
    "console=tty0 initcall_blacklist=nvme_init,mei_me_driver_init efi=noruntime module_blacklist=tpm,amdgpu loglevel=4",
    [("card0", "udl", [("card0-DVI-I-1", "disconnected")])],
    efifb_cards=[("card1", "gbos_efifb", [("card1-Virtual-1", "connected")]),
                 ("card2", "gbos_efifb", [("card2-Virtual-1", "connected")])],
)
assert safe_shifted == [
    "setprop vendor.gbos.radv_probe radeon",
    "setprop ro.hardware.vulkan pastel",
    "setprop ro.hwui.use_vulkan false",
    "setprop debug.renderengine.vulkan false",
    "efifb ",
    "setprop vendor.hwc.drm.device /dev/dri/card2",
], f"unexpected safe_shifted: {safe_shifted}"

# 1c. Safe entry when sysfs device/uevent files are missing/unreadable: falls back to /dev/dri/card1 (never card0 allocator)
safe_no_uevent = run_case(
    "safe-missing-uevent",
    "console=tty0 initcall_blacklist=nvme_init,mei_me_driver_init efi=noruntime module_blacklist=tpm,amdgpu loglevel=4",
    [],
    efifb_cards=[("card0", "gbos_efifb", [("card0-Virtual-1", "connected")]),
                 ("card1", "gbos_efifb", [("card1-Virtual-1", "connected")])],
    omit_uevent=True,
)
assert safe_no_uevent == [
    "setprop vendor.gbos.radv_probe radeon",
    "setprop ro.hardware.vulkan pastel",
    "setprop ro.hwui.use_vulkan false",
    "setprop debug.renderengine.vulkan false",
    "efifb ",
    "setprop vendor.hwc.drm.device /dev/dri/card1",
], f"unexpected safe_no_uevent: {safe_no_uevent}"

# 2. Experimental native entry on a 2-GPU system where card0 is vkms (connected) and card1 is amdgpu (connected):
#    loads amdgpu once, never starts efifb, selects /dev/dri/card1 by DRIVER=amdgpu (not card0), sets RADV properties
nat_calls = run_case(
    "native-2gpu",
    "console=tty0 amdgpu.modeset=1 amdgpu.dc=1 initcall_blacklist=nvme_init,mei_me_driver_init efi=noruntime module_blacklist=tpm",
    [("card0", "vkms", [("card0-Virtual-1", "connected")]),
     ("card1", "amdgpu", [("card1-DP-1", "connected")])],
)
assert nat_calls == [
    "setprop vendor.gbos.radv_probe radeon",
    "modprobe --dirname=/vendor/lib/dev-modules amdgpu",
    "setprop ro.hardware.vulkan radeon",
    "setprop ro.hwui.use_vulkan true",
    "setprop debug.renderengine.vulkan true",
    "setprop vendor.hwc.drm.device /dev/dri/card1",
], f"unexpected nat_calls: {nat_calls}"

# 3. Experimental native entry with two amdgpu cards where card0 is disconnected and card2 is connected -> prefers card2
nat_conn = run_case(
    "native-prefer-connected",
    "amdgpu.modeset=1 module_blacklist=tpm",
    [("card0", "amdgpu", [("card0-eDP-1", "disconnected")]),
     ("card2", "amdgpu", [("card2-HDMI-A-1", "connected")])],
)
assert "setprop vendor.hwc.drm.device /dev/dri/card2" in nat_conn, f"unexpected nat_conn: {nat_conn}"

# 4. Experimental native entry where sysfs DRIVER=amdgpu node appears asynchronously after modprobe (polls up to 20x0.1s)
nat_delayed = run_case(
    "native-delayed-bind",
    "amdgpu.modeset=1 module_blacklist=tpm",
    [],
    delayed_cards=[("card0", "amdgpu", [("card0-eDP-1", "connected")])],
)
assert nat_delayed == [
    "setprop vendor.gbos.radv_probe radeon",
    "modprobe --dirname=/vendor/lib/dev-modules amdgpu",
    "sleep 0.1",
    "sleep 0.1",
    "setprop ro.hardware.vulkan radeon",
    "setprop ro.hwui.use_vulkan true",
    "setprop debug.renderengine.vulkan true",
    "setprop vendor.hwc.drm.device /dev/dri/card0",
], f"unexpected nat_delayed: {nat_delayed}"

# 5. Experimental native entry failure path: no DRIVER=amdgpu card ever appears -> polls 20x, never starts efifb, logs warning
nat_nocard, nat_nocard_kmsg = run_case(
    "native-no-card",
    "amdgpu.modeset=1 module_blacklist=tpm",
    [],
    return_kmsg=True,
)
assert nat_nocard == (
    ["setprop vendor.gbos.radv_probe radeon", "modprobe --dirname=/vendor/lib/dev-modules amdgpu"]
    + ["sleep 0.1"] * 20
    + ["setprop ro.hardware.vulkan radeon", "setprop ro.hwui.use_vulkan true", "setprop debug.renderengine.vulkan true"]
), f"unexpected nat_nocard: {nat_nocard}"
assert not any(c.startswith("efifb") for c in nat_nocard), f"native entry must never start efifb: {nat_nocard}"
assert "no DRIVER=amdgpu card in" in nat_nocard_kmsg and "Googlebook AMD HX370 safe" in nat_nocard_kmsg, f"unexpected kmsg: {nat_nocard_kmsg}"

# 6. Experimental native entry when modprobe amdgpu fails with non-zero exit (rc=1): logs exit 1, never starts efifb, logs warning
nat_mpfail, nat_mpfail_kmsg = run_case(
    "native-modprobe-fail",
    "amdgpu.modeset=1 module_blacklist=tpm",
    [],
    return_kmsg=True,
    modprobe_rc=1,
)
assert "native entry: modprobe amdgpu exit 1" in nat_mpfail_kmsg, f"expected modprobe exit 1 in kmsg: {nat_mpfail_kmsg}"
assert not any(c.startswith("efifb") for c in nat_mpfail), f"native entry must never start efifb on modprobe failure: {nat_mpfail}"
assert "no DRIVER=amdgpu card in" in nat_mpfail_kmsg, f"unexpected kmsg: {nat_mpfail_kmsg}"
print("gfx-sh ok")
PY
expect "check-radv-elf.py self-test" 0 "elf self-test ok" -- python3 "$ROOT/tools/mesa/check-radv-elf.py" --self-test
STEP2="${GBOS_RADV_STEP2:-$HOME/gbos-amd/build-android-x86_64/src/amd/vulkan/libvulkan_radeon.so}"
if [ -f "$STEP2" ]; then
    expect "check-radv-elf.py rejects the step-2 RADV binary" 1 "SONAME|RUNPATH|libdrm_amdgpu" -- python3 "$ROOT/tools/mesa/check-radv-elf.py" "$STEP2"
else
    echo "SKIP check-radv-elf.py step-2 RADV binary check ($STEP2 not present on this host)"
fi
mkdir -p "$T/audio-amd"
expect "build.sh refuses amd-hx370 without vulkan.radeon.so" 2 "vulkan.radeon.so" -- "${B[@]}" GBOS_OUT="$T/o-amd4" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" GBOS_AUDIO_SRC="$T/audio-amd" bash "$ROOT/builder/build.sh"
touch "$T/fake-radv.so"
expect "build.sh refuses amd-hx370 without vulkan.pastel.so (needed by safe default entry)" 2 "needs vulkan.pastel.so" -- "${B[@]}" GBOS_OUT="$T/o-amd5" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" GBOS_AUDIO_SRC="$T/audio-amd" GBOS_RADV="$T/fake-radv.so" GBOS_RADV_PROBE="$T/fake-radv.so" GBOS_RADV_VKPROP="$T/fake-radv.so" bash "$ROOT/builder/build.sh"
mkdir -p "$T/in/generic"; touch "$T/in/generic/vulkan.pastel.so"
expect "build.sh refuses amd-hx370 without gbos_efifb.ko (needed by safe default entry)" 2 "needs gbos_efifb.ko" -- "${B[@]}" GBOS_OUT="$T/o-amd6" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" GBOS_AUDIO_SRC="$T/audio-amd" GBOS_RADV="$T/fake-radv.so" GBOS_RADV_PROBE="$T/fake-radv.so" GBOS_RADV_VKPROP="$T/fake-radv.so" bash "$ROOT/builder/build.sh"
rm -rf "$T/in/generic"
mkdir -p "$T/in/generic" "$T/in/amd-hx370"
touch "$T/in/generic/vulkan.pastel.so" "$T/in/generic/gbos_efifb.ko"
expect "build.sh refuses amd-hx370 without libminigbm_gralloc.so" 2 "needs libminigbm_gralloc.so" -- "${B[@]}" GBOS_OUT="$T/o-amd7" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" GBOS_AUDIO_SRC="$T/audio-amd" GBOS_RADV="$T/fake-radv.so" GBOS_RADV_PROBE="$T/fake-radv.so" GBOS_RADV_VKPROP="$T/fake-radv.so" bash "$ROOT/builder/build.sh"
printf 'not-the-library\n' > "$T/in/amd-hx370/libminigbm_gralloc.so"
expect "build.sh refuses amd-hx370 when libminigbm_gralloc.so sha256 differs" 2 "^build.sh: GBOS_MINIGBM sha256 .* differs" -- "${B[@]}" GBOS_OUT="$T/o-amd8" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" GBOS_AUDIO_SRC="$T/audio-amd" GBOS_RADV="$T/fake-radv.so" GBOS_RADV_PROBE="$T/fake-radv.so" GBOS_RADV_VKPROP="$T/fake-radv.so" bash "$ROOT/builder/build.sh"
printf 'also-wrong\n' > "$T/wrong-minigbm.so"
expect "build.sh refuses GBOS_MINIGBM whose sha256 differs" 2 "^build.sh: GBOS_MINIGBM sha256 .* differs" -- "${B[@]}" GBOS_OUT="$T/o-amd9" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" GBOS_AUDIO_SRC="$T/audio-amd" GBOS_RADV="$T/fake-radv.so" GBOS_RADV_PROBE="$T/fake-radv.so" GBOS_RADV_VKPROP="$T/fake-radv.so" GBOS_MINIGBM="$T/wrong-minigbm.so" bash "$ROOT/builder/build.sh"
printf 'link-target\n' > "$T/real-minigbm.so"
ln -s "$T/real-minigbm.so" "$T/link-minigbm.so"
expect "build.sh refuses a symlink GBOS_MINIGBM" 2 "GBOS_MINIGBM is a symlink" -- "${B[@]}" GBOS_OUT="$T/o-amd9b" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" GBOS_AUDIO_SRC="$T/audio-amd" GBOS_RADV="$T/fake-radv.so" GBOS_RADV_PROBE="$T/fake-radv.so" GBOS_RADV_VKPROP="$T/fake-radv.so" GBOS_MINIGBM="$T/link-minigbm.so" bash "$ROOT/builder/build.sh"
real_python=$(command -v python3)
mkdir -p "$T/fakepy"
cat > "$T/fakepy/python3" << EOF
#!/bin/bash
if [ "\$1" = -c ] && printf '%s' "\$2" | grep -q 'p.get("gralloc")'; then
    echo "profile load failed: boom" >&2
    exit 1
fi
exec "$real_python" "\$@"
EOF
chmod +x "$T/fakepy/python3"
expect "build.sh refuses when the [gralloc] probe cannot load the profile" 2 "^build.sh: could not read \\[gralloc\\]" -- env PATH="$T/fakepy:$PATH" "${B[@]}" GBOS_OUT="$T/o-amd10" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/amd-hx370" GBOS_DEV_KERNEL_DIR="$T/devamd" GBOS_AUDIO_SRC="$T/audio-amd" GBOS_RADV="$T/fake-radv.so" GBOS_RADV_PROBE="$T/fake-radv.so" GBOS_RADV_VKPROP="$T/fake-radv.so" bash "$ROOT/builder/build.sh"
for pair in "kano|$T/audio-kano" "generic-x86|$T/audio-gx"; do
    model=${pair%%|*}; adir=${pair#*|}
    mkdir -p "$adir" "$T/o-$model"
    out=$( "${B[@]}" GBOS_OUT="$T/o-$model" GBOS_SKIP_SELFTEST=1 GBOS_PROFILE="$ROOT/profiles/$model" GBOS_AUDIO_SRC="$adir" bash "$ROOT/builder/build.sh" 2>&1 ) && rc=0 || rc=$?
    if [ "$rc" != 0 ] && ! printf '%s\n' "$out" | grep -q 'libminigbm' && ! printf '%s\n' "$out" | grep -q 'could not read \[gralloc\]'; then
        ok "$model build.sh does not ask for libminigbm_gralloc.so"
    else
        bad "$model build.sh does not ask for libminigbm_gralloc.so (rc=$rc): $(printf '%s\n' "$out" | tail -n 3)"
    fi
done
rm -rf "$T/in/generic" "$T/in/amd-hx370"
grep -q '^MESA_SHA256=[0-9a-f]\{64\}$' "$ROOT/tools/mesa/build-radv.sh" \
    && grep -q '^LIBDRM_SHA256=[0-9a-f]\{64\}$' "$ROOT/tools/mesa/build-radv.sh" \
    && grep -q '^NDK_SHA256=[0-9a-f]\{64\}$' "$ROOT/tools/mesa/build-radv.sh" \
    && [ -f "$ROOT/tools/mesa/NOTICES.txt" ] \
    && grep -q '7725004e724b34c6d4fbaf5c48fc6c6223aa9f2741d6d7782c699b049356fc45' "$ROOT/tools/mesa/NOTICES.txt" \
    && grep -q 'a2b98567a149a74b0f50e91e825f9c0315d86e7be9b74394dae8b298caadb79e' "$ROOT/tools/mesa/NOTICES.txt" \
    && [ "$(grep -cE '^RADV_SHA=[0-9a-f]{64}$' "$ROOT/tools/mesa/build-radv.sh")" = 1 ] \
    && [ "$(grep -cE '^RADV_STRIP_UNNEEDED_SHA=[0-9a-f]{64}$' "$ROOT/tools/mesa/build-radv.sh")" = 1 ] \
    && [ "$(grep -cE '^PROBE_SHA=[0-9a-f]{64}$' "$ROOT/tools/mesa/build-radv.sh")" = 1 ] \
    && [ "$(grep -cE '^VKPROP_SHA=[0-9a-f]{64}$' "$ROOT/tools/mesa/build-radv.sh")" = 1 ] \
    && grep -q -- '--strip-unneeded' "$ROOT/tools/mesa/build-radv.sh" \
    && grep -q '"$READELF" --dyn-syms ' "$ROOT/tools/mesa/build-radv.sh" \
    && ! grep -q '"$READELF" -s --dyn-syms' "$ROOT/tools/mesa/build-radv.sh" \
    && grep -q '"$WORK/ndk"' "$ROOT/tools/mesa/build-radv.sh" \
    && grep -q '"RADV_SHA"' "$ROOT/builder/vendor.sh" \
    && grep -q '"RADV_STRIP_UNNEEDED_SHA"' "$ROOT/builder/vendor.sh" \
    && grep -q 'etc/NOTICES-amd-hx370-radv.txt' "$ROOT/builder/vendor.sh" \
    && grep -q '0-9a-f]{{64}}' "$ROOT/builder/vendor.sh" \
    && ok "build-radv.sh pins vulkan.radeon.so (--strip-unneeded, --dyn-syms GLOBAL HMI, NOTICES.txt); vendor.sh static wiring checks the four pins and place()s etc/NOTICES-amd-hx370-radv.txt" || bad "RADV pin"
grep -qE '^PASTEL_SHA=[0-9a-f]{64}$' "$ROOT/builder/fetch-generic-inputs.sh" && grep -q '"5232e4829d8e6bba2d81cc0ff64d227504601090d2ac7b9fa105536e38c680a3"' "$ROOT/builder/vendor.sh" \
    && [ "$(sed -n 's/^PASTEL_SHA=//p' "$ROOT/builder/fetch-generic-inputs.sh")" = 5232e4829d8e6bba2d81cc0ff64d227504601090d2ac7b9fa105536e38c680a3 ] \
    && ok "fetch-generic-inputs.sh and vendor.sh pin the same vulkan.pastel.so" || bad "vulkan.pastel.so pins differ"
expect "build-gbos_efifb.sh needs the headers, the CRC list, the output and a toolchain directory" 1 "toolchain dir" -- \
    bash "$ROOT/builder/kmod/build-gbos_efifb.sh" "$T/kh" "$T/symvers" "$T/efifb.ko"
for v in CLANG_URL CLANG_TREE_SHA; do
    [ -n "$(sed -n "s/^$v=//p" "$ROOT/builder/kmod/build-ac.sh")" ] \
        && [ "$(sed -n "s/^$v=//p" "$ROOT/builder/kmod/build-ac.sh")" = "$(sed -n "s/^$v=//p" "$ROOT/builder/kmod/build-gbos_efifb.sh")" ] \
        && ok "build-ac.sh and build-gbos_efifb.sh pin the same $v" || bad "$v differs between build-ac.sh and build-gbos_efifb.sh"
done
[ "$(grep -cE '^EFIFB_KO_SHA=[0-9a-f]{64}$' "$ROOT/builder/kmod/build-gbos_efifb.sh")" = 1 ] && grep -qF 'EFIFB_KO_SHA=([0-9a-f]{64})' "$ROOT/builder/vendor.sh" \
    && ok "build-gbos_efifb.sh pins gbos_efifb.ko and vendor.sh checks that pin" || bad "gbos_efifb.ko pin"
expect "image.py's AC SSDT is one ACPI0003 device with a valid checksum" 0 "^ssdt ok$" -- python3 - "$ROOT/builder/image.py" "$T/ssdt.aml" << 'PY'
import ast, struct, sys
from pathlib import Path
tree = ast.parse(Path(sys.argv[1]).read_text())
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ac_ssdt")
ns = {"struct": struct, "Path": Path}
exec(compile(ast.Module([fn], []), "image.py", "exec"), ns)
ns["ac_ssdt"](sys.argv[2])
t = Path(sys.argv[2]).read_bytes()
assert t[:4] == b"SSDT" and struct.unpack_from("<I", t, 4)[0] == len(t) and sum(t) % 256 == 0
assert b"\\_SB_\x5b\x82" in t and b"AC__\x08_HID\x0dACPI0003\x00" in t and b"_PSR\x00\xa4\x01" in t
print("ssdt ok")
PY

# builder/camera/build-libcamera.sh (the opt-in camera overlay): argument and toolchain guards (nothing fetched or built)
expect "build-libcamera.sh requires a work directory" 1 "usage: build-libcamera.sh" -- bash "$ROOT/builder/camera/build-libcamera.sh"
expect "build-libcamera.sh refuses a missing NDK" 2 "NDK clang not found|CMake not found" -- env NDK="$T/no-ndk" CMAKE="$T/no-cmake" bash "$ROOT/builder/camera/build-libcamera.sh" "$T/cam"
expect "build-libcamera.sh pins every download" 0 "^4$" -- sh -c "grep -cE '^fetch https://' '$ROOT/builder/camera/build-libcamera.sh'"
for p in "$ROOT"/builder/camera/patches/*.patch; do
    grep -q '^--- a/' "$p" && ok "camera patch ${p##*/} is a -p1 diff" || bad "camera patch ${p##*/} is a -p1 diff"
done
[ "$(python3 "$ROOT/builder/lib/devprofile.py" get "$ROOT/profiles/kano" camera.libcamera)" = false ] \
    && ok "the KANO profile leaves the camera overlay off by default" || bad "the KANO profile turns the camera overlay on"
mkdir -p "$T/camprof/generic-x86"; cp "$ROOT/profiles/generic-x86/"* "$T/camprof/generic-x86/" 2>/dev/null
sed 's/^\[hooks\]$/[camera]\nlibcamera = true\n\n[hooks]/; s/^vendor = .*/vendor = ""/' "$ROOT/profiles/generic-x86/profile.toml" > "$T/camprof/generic-x86/profile.toml"
expect "devprofile.py refuses camera.libcamera = true without a hooks.vendor file" 1 "camera.libcamera" -- python3 "$ROOT/builder/lib/devprofile.py" check "$T/camprof/generic-x86"
expect "build.sh refuses a GBOS_CAMERA_OVERLAY that is not an overlay directory" 2 "GBOS_CAMERA_OVERLAY must be" -- "${B[@]}" GBOS_OUT="$T/o9" GBOS_SKIP_SELFTEST=1 GBOS_KMOD_AC="$T/ac-any.ko" GBOS_KMOD_WAKEFILTER="$T/ac-any.ko" GBOS_CAMERA_OVERLAY="$T/no-overlay" bash "$ROOT/builder/build.sh"
mkdir -p "$T/devk2/modules"
expect "build.sh refuses GBOS_DEV_KERNEL_DIR without bzImage" 2 "must be a directory with bzImage" -- "${B[@]}" GBOS_OUT="$T/o10" GBOS_SKIP_SELFTEST=1 GBOS_KMOD_AC="$T/ac-any.ko" GBOS_KMOD_WAKEFILTER="$T/ac-any.ko" GBOS_DEV_KERNEL_DIR="$T/devk2" bash "$ROOT/builder/build.sh"
printf 'MZ' > "$T/devk2/bzImage"
expect "build.sh refuses GBOS_DEV_KERNEL_DIR without modules/*.ko" 2 "must be a directory with bzImage" -- "${B[@]}" GBOS_OUT="$T/o10" GBOS_SKIP_SELFTEST=1 GBOS_KMOD_AC="$T/ac-any.ko" GBOS_KMOD_WAKEFILTER="$T/ac-any.ko" GBOS_DEV_KERNEL_DIR="$T/devk2" bash "$ROOT/builder/build.sh"
printf 'x' > "$T/devk2/modules/test.ko"
expect "build.sh refuses GBOS_DEV_KERNEL_DIR without modules/modules.dep" 2 "modules/modules.dep" -- "${B[@]}" GBOS_OUT="$T/o10" GBOS_SKIP_SELFTEST=1 GBOS_KMOD_AC="$T/ac-any.ko" GBOS_KMOD_WAKEFILTER="$T/ac-any.ko" GBOS_DEV_KERNEL_DIR="$T/devk2" bash "$ROOT/builder/build.sh"

# GBOS_TEST_AUTOOFF: empty = off; otherwise an integer 60..7200. install.sh refuses it (developer-only).
expect "build.sh refuses GBOS_TEST_AUTOOFF=59" 2 "60\\.\\.7200" -- "${B[@]}" GBOS_OUT="$T/o-a59" GBOS_SKIP_SELFTEST=1 GBOS_TEST_AUTOOFF=59 bash "$ROOT/builder/build.sh"
expect "build.sh refuses GBOS_TEST_AUTOOFF=7201" 2 "60\\.\\.7200" -- "${B[@]}" GBOS_OUT="$T/o-a7201" GBOS_SKIP_SELFTEST=1 GBOS_TEST_AUTOOFF=7201 bash "$ROOT/builder/build.sh"
expect "build.sh refuses GBOS_TEST_AUTOOFF=abc" 2 "60\\.\\.7200" -- "${B[@]}" GBOS_OUT="$T/o-aabc" GBOS_SKIP_SELFTEST=1 GBOS_TEST_AUTOOFF=abc bash "$ROOT/builder/build.sh"
expect "build.sh refuses GBOS_TEST_AUTOOFF=60s" 2 "60\\.\\.7200" -- "${B[@]}" GBOS_OUT="$T/o-a60s" GBOS_SKIP_SELFTEST=1 GBOS_TEST_AUTOOFF=60s bash "$ROOT/builder/build.sh"
expect "build.sh refuses GBOS_TEST_AUTOOFF=0060" 2 "60\\.\\.7200" -- "${B[@]}" GBOS_OUT="$T/o-a0060" GBOS_SKIP_SELFTEST=1 GBOS_TEST_AUTOOFF=0060 bash "$ROOT/builder/build.sh"
expect "build.sh refuses GBOS_TEST_AUTOOFF=18446744073709551676" 2 "60\\.\\.7200" -- "${B[@]}" GBOS_OUT="$T/o-awrap60" GBOS_SKIP_SELFTEST=1 GBOS_TEST_AUTOOFF=18446744073709551676 bash "$ROOT/builder/build.sh"
expect "build.sh refuses GBOS_TEST_AUTOOFF=18446744073709558816" 2 "60\\.\\.7200" -- "${B[@]}" GBOS_OUT="$T/o-awrap7200" GBOS_SKIP_SELFTEST=1 GBOS_TEST_AUTOOFF=18446744073709558816 bash "$ROOT/builder/build.sh"
mkdir -p "$T/in/firmware"
expect "build.sh accepts GBOS_TEST_AUTOOFF=60 (reaches the ac.ko check)" 2 "set GBOS_KMOD_AC" -- "${B[@]}" GBOS_OUT="$T/o-a60" GBOS_SKIP_SELFTEST=1 GBOS_TEST_AUTOOFF=60 bash "$ROOT/builder/build.sh"
expect "build.sh accepts GBOS_TEST_AUTOOFF=7200 (reaches the ac.ko check)" 2 "set GBOS_KMOD_AC" -- "${B[@]}" GBOS_OUT="$T/o-a7200" GBOS_SKIP_SELFTEST=1 GBOS_TEST_AUTOOFF=7200 bash "$ROOT/builder/build.sh"
expect "vendor.sh refuses GBOS_TEST_AUTOOFF=59" 2 "60\\.\\.7200" -- "${V[@]}" GBOS_TEST_AUTOOFF=59 bash "$ROOT/builder/vendor.sh"
expect "vendor.sh refuses GBOS_TEST_AUTOOFF=0060" 2 "60\\.\\.7200" -- "${V[@]}" GBOS_TEST_AUTOOFF=0060 bash "$ROOT/builder/vendor.sh"
expect "vendor.sh refuses GBOS_TEST_AUTOOFF=18446744073709551676" 2 "60\\.\\.7200" -- "${V[@]}" GBOS_TEST_AUTOOFF=18446744073709551676 bash "$ROOT/builder/vendor.sh"
expect "vendor.sh refuses GBOS_TEST_AUTOOFF=18446744073709558816" 2 "60\\.\\.7200" -- "${V[@]}" GBOS_TEST_AUTOOFF=18446744073709558816 bash "$ROOT/builder/vendor.sh"
expect "vendor.sh accepts GBOS_TEST_AUTOOFF=2400 (reaches the audio check)" 1 "do not match" -- "${V[@]}" GBOS_TEST_AUTOOFF=2400 bash "$ROOT/builder/vendor.sh"
expect "install.sh refuses GBOS_TEST_AUTOOFF" 2 "developer-only" -- env GBOS_TEST_AUTOOFF=2400 bash "$ROOT/install/install.sh"
expect "GBOS_TEST_AUTOOFF files, gate, and definition order" 0 "^AUTOOFF OK$" -- python3 - "$ROOT" "$T/autooff" << 'PY'
import os, re, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "builder/lib"))
import kano_autooff

root = Path(sys.argv[1])
dest = Path(sys.argv[2])
off = dest / "off"
on = dest / "on"
for tree in (off, on):
    (tree / "etc/init").mkdir(parents=True)
like = {"mode": 0o644, "uid": 0, "gid": 0, "selinux": "u:object_r:vendor_configs_file:s0", "caps": 0}
vmap_off = {"etc/init/fwupd.rc": dict(like)}
vmap_on = {"etc/init/fwupd.rc": dict(like)}
# switch off: the installer is not called, so neither file exists
assert not (off / "etc/kano-autooff.sh").exists()
assert not (off / "etc/init/kano-autooff.rc").exists()
assert not (off / "etc/kano-autooff.nonce").exists()
os.environ["GBOS_OUT"] = str(dest / "out1")
kano_autooff.install(on, vmap_on, 2400)
script_path = on / "etc/kano-autooff.sh"
rc_path = on / "etc/init/kano-autooff.rc"
nonce_path = on / "etc/kano-autooff.nonce"
side_path = dest / "out1" / "image" / "autooff.nonce"
assert script_path.is_file() and rc_path.is_file() and nonce_path.is_file() and side_path.is_file()
assert (script_path.stat().st_mode & 0o777) == 0o644
assert (rc_path.stat().st_mode & 0o777) == 0o644
assert (nonce_path.stat().st_mode & 0o777) == 0o444
assert (side_path.stat().st_mode & 0o777) == 0o644
nonce_meta = dict(like)
nonce_meta["mode"] = 0o444
assert vmap_on["etc/kano-autooff.sh"] == like
assert vmap_on["etc/init/kano-autooff.rc"] == like
assert vmap_on["etc/kano-autooff.nonce"] == nonce_meta
assert "etc/kano-autooff.sh" not in vmap_off
assert "etc/init/kano-autooff.rc" not in vmap_off
assert "etc/kano-autooff.nonce" not in vmap_off
nonce1 = nonce_path.read_bytes()
assert nonce1 == side_path.read_bytes()
assert re.fullmatch(rb"[0-9a-f]{32}\n", nonce1)
on2 = dest / "on2"
(on2 / "etc/init").mkdir(parents=True)
vmap2 = {"etc/init/fwupd.rc": dict(like)}
os.environ["GBOS_OUT"] = str(dest / "out2")
kano_autooff.install(on2, vmap2, 2400)
nonce2 = (on2 / "etc/kano-autooff.nonce").read_bytes()
assert nonce2 == (dest / "out2" / "image" / "autooff.nonce").read_bytes()
assert re.fullmatch(rb"[0-9a-f]{32}\n", nonce2)
assert nonce1 != nonce2
assert script_path.read_bytes() == (on2 / "etc/kano-autooff.sh").read_bytes()
assert nonce1 not in script_path.read_bytes()
bare = dest / "bare"
(bare / "etc/init").mkdir(parents=True)
vbare = {"etc/init/fwupd.rc": dict(like)}
os.environ.pop("GBOS_OUT", None)
try:
    kano_autooff.install(bare, vbare, 60)
except ValueError:
    pass
else:
    raise SystemExit("install without GBOS_OUT succeeded")
assert not (bare / "etc/kano-autooff.nonce").exists()
for bad in (59, 7201, True, 60.0, "60"):
    try:
        kano_autooff.script(bad)
    except ValueError:
        pass
    else:
        raise SystemExit(f"script accepted {bad!r}")
rc = rc_path.read_text()
assert "user root" in rc
assert "seclabel u:r:kano_kmsg_logcat:s0" in rc
assert "on nonencrypted" in rc
assert "on property:sys.boot_completed=1" in rc
assert rc.count("start kano_autooff") == 2
body = script_path.read_text()
assert 'kano_log "nonce gate closed, staying on"' in body
assert 'kano_log "powering off"' in body
assert "_kano_wait=2400\n" in body
assert "reboot -p\n" in body
assert "setprop" not in body
assert "/sys/" not in body
assert "uevent" not in body
assert "readlink" not in body
assert "getprop" not in body
for bad in ("readlink", "getprop", "setprop", "uevent", "/sys/", "awk", "eval"):
    assert bad not in "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
lines = body.splitlines()
code = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
assert [ln.strip() for ln in code if ln.strip().startswith("reboot")] == ["reboot -p"]
def_i = next(i for i, line in enumerate(lines) if line.startswith("kano_read_body()"))
call_i = next(i for i, line in enumerate(lines)
              if "kano_read_body " in line and not line.startswith("kano_read_body()"))
assert def_i < call_i, (def_i, call_i)
hex_def = next(i for i, line in enumerate(lines) if line.startswith("kano_nonce_hex()"))
hex_call = next(i for i, line in enumerate(lines)
                if "kano_nonce_hex " in line and not line.startswith("kano_nonce_hex()"))
assert hex_def < hex_call, (hex_def, hex_call)
vendor = (root / "builder/vendor.sh").read_text()
base_list = vendor.split("expect_new = sorted(set([", 1)[1].split("] + p_new + fw_new))", 1)[0]
assert "kano-autooff" not in base_list
guard = vendor.index('if os.environ.get("GBOS_TEST_AUTOOFF"):')
added = vendor[guard:guard + 500]
assert '["etc/kano-autooff.sh", "etc/init/kano-autooff.rc", "etc/kano-autooff.nonce"]' in added
install_at = vendor.index("kano_autooff.install")
assert "if _autooff:" in vendor[install_at - 250:install_at]
syntax = subprocess.run(["sh", "-n", str(script_path)], capture_output=True, text=True)
if syntax.returncode != 0:
    sys.stderr.write(syntax.stderr)
    raise SystemExit("sh -n failed")
print("AUTOOFF OK")
PY
expect "GBOS_TEST_AUTOOFF runs the generated script" 0 "^AUTOOFF SCRIPT OK$" -- python3 "$ROOT/tests/builder/autooff-script-test.py"
expect "vendor.sh switch off writes no autooff files" 0 "^VENDOR OFF OK$" -- python3 "$ROOT/tests/builder/autooff-vendor-off-test.py"
expect "image.py autooff log checks" 0 "^AUTOOFF LOG OK$" -- python3 - "$ROOT/builder/image.py" << 'PY'
import ast, re, sys
from pathlib import Path
tree = ast.parse(Path(sys.argv[1]).read_text())
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "autooff_log_checks")
ns = {"re": re}
exec(compile(ast.Module([fn], []), "image.py", "exec"), ns)
checks = ns["autooff_log_checks"]
good = "\n".join([
    "init: Parsing file /vendor/etc/init/kano-autooff.rc",
    "KANO-AUTOOFF: nonce gate closed, staying on",
    "init: Service 'kano_autooff' (pid 42) exited with status 0",
])
quiet = "\n".join([
    "init: Service 'kano_bootfix' (pid 7) exited with status 0",
    "kano-logcat: hello",
])
def need(label, cond):
    if not cond:
        raise SystemExit(f"FAIL {label}")
    print(f"PASS {label}")
on = checks(good, "60")
need("rc parsed", on["kano-autooff rc parsed (GBOS_TEST_AUTOOFF)"])
need("staying on", on["kano-autooff refuses QEMU (no /data nonce, staying on)"])
need("clean stay-on did not power off", on["kano-autooff did not power off (no /data nonce)"])
both = good + "\nKANO-AUTOOFF: powering off\n"
need("stay-on plus powering off is not accepted",
     checks(both, "60")["kano-autooff did not power off (no /data nonce)"] is False)
need("exited 0", on["kano_autooff exited 0"])
no_exit = good.replace("init: Service 'kano_autooff' (pid 42) exited with status 0", "")
need("stay-on without exit is not exited 0", checks(no_exit, "60")["kano_autooff exited 0"] is False)
status_1 = good.replace("exited with status 0", "exited with status 1")
need("status 1 is not exited 0", checks(status_1, "60")["kano_autooff exited 0"] is False)
bootfix_only = good.replace("kano_autooff", "kano_bootfix")
need("bootfix exit is not the autooff exit", checks(bootfix_only, "2400")["kano_autooff exited 0"] is False)
no_stay = good.replace("KANO-AUTOOFF: nonce gate closed, staying on", "KANO-AUTOOFF: powering off")
need("powering off is not the QEMU stay-on line", checks(no_stay, "60")["kano-autooff refuses QEMU (no /data nonce, staying on)"] is False)
need("quiet log has no autooff", checks(quiet, "")["no kano-autooff (GBOS_TEST_AUTOOFF not set)"])
for needle in ("kano-autooff.rc", "Service 'kano_autooff'", "KANO-AUTOOFF: nonce gate closed, staying on"):
    need(f"off rejects {needle}", checks(quiet + "\n" + needle, "")["no kano-autooff (GBOS_TEST_AUTOOFF not set)"] is False)
need("empty enabled is the off check", "kano_autooff exited 0" not in checks(good, ""))
print("AUTOOFF LOG OK")
PY

# install.sh: argument guard, _safety.toml in SRC_ID, and default input resolution before PARAMS hashing
expect "install.sh requires --work" 2 "--work DIR is required" -- bash "$ROOT/install/install.sh"
if grep -q 'SRC_ID=\$(tree_digest "\$ROOT/builder" "\$ROOT/tools/mesa" "\$ROOT/profiles/_safety.toml" "\$PROFILE")' "$ROOT/install/install.sh" \
    && grep -q 'export GBOS_RADV="\${GBOS_RADV:-\$WORK/inputs/amd-hx370/vulkan.radeon.so}"' "$ROOT/install/install.sh" \
    && grep -q 'export GBOS_RADV_PROBE="\${GBOS_RADV_PROBE:-\$WORK/inputs/amd-hx370/radv-probe}"' "$ROOT/install/install.sh" \
    && grep -q 'export GBOS_RADV_VKPROP="\${GBOS_RADV_VKPROP:-\$WORK/inputs/amd-hx370/libvkprop.so}"' "$ROOT/install/install.sh" \
    && grep -q 'export GBOS_GENERIC_PASTEL="\${GBOS_GENERIC_PASTEL:-\$WORK/inputs/generic/vulkan.pastel.so}"' "$ROOT/install/install.sh" \
    && grep -q 'export GBOS_GENERIC_KO="\${GBOS_GENERIC_KO:-\$WORK/inputs/generic/gbos_efifb.ko}"' "$ROOT/install/install.sh"; then
    ok "install.sh hashes profiles/_safety.toml into SRC_ID and resolves amd-hx370 default inputs before hashing PARAMS"
else
    bad "install.sh hashes profiles/_safety.toml into SRC_ID and resolves amd-hx370 default inputs before hashing PARAMS"
fi
expect "install.sh cache reuse and _safety.toml invalidation (tests/install/install-sh-test.sh)" 0 "install script tests: [0-9]+ passed, 0 failed" -- bash "$ROOT/tests/install/install-sh-test.sh"
# write-usb.sh refuses to run without root (it writes a block device)
if [ "$(id -u)" != 0 ]; then expect "write-usb.sh refuses non-root" 2 "run as root" -- bash "$ROOT/install/write-usb.sh" "$T/x.img" /dev/null; fi

echo "builder tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
