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

for f in "$ROOT"/builder/*.sh "$ROOT"/builder/kmod/*.sh "$ROOT"/builder/camera/*.sh "$ROOT"/base/*.sh "$ROOT"/install/*.sh "$ROOT"/install/stick/*.sh "$ROOT"/tests/*/*.sh "$ROOT"/tools/release/*.sh; do
    [ -e "$f" ] || continue   # an unmatched glob (the release archive has no tools/release/*.sh)
    bash -n "$f" && ok "bash -n ${f#$ROOT/}" || bad "bash -n ${f#$ROOT/}"
done
for f in "$ROOT"/builder/*.py "$ROOT"/builder/lib/*.py "$ROOT"/builder/kmod/*.py "$ROOT"/base/*.py "$ROOT"/install/stick/*.py "$ROOT"/install/probe/*.py \
         "$ROOT"/profiles/*/*.py "$ROOT"/tests/*/*.py; do
    [ -e "$f" ] || continue
    python3 -m py_compile "$f" && ok "py_compile ${f#$ROOT/}" || bad "py_compile ${f#$ROOT/}"
done

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
# tools/release/png-ok.py: the only binary files the public export and the release archive accept (docs/images/*.png)
expect "png-ok accepts the screenshots and refuses hidden payloads (tests/builder/png-ok-test.py)" 0 "png-ok tests: [0-9]+ passed, 0 failed" -- python3 "$ROOT/tests/builder/png-ok-test.py"
# the release archive builds from this checkout (git only: the release archive itself has no .git)
if [ -f "$ROOT/tools/release/make-release.sh" ] && [ "$(git -C "$ROOT" rev-parse --show-toplevel 2> /dev/null)" = "$(cd "$ROOT" && pwd -P)" ]; then
    expect "make-release.sh builds an archive with the screenshots" 0 "googlebook-anywhere-v9.9.9.tar.gz" -- env GBOS_RELEASE_OUT="$T/rel" bash "$ROOT/tools/release/make-release.sh" v9.9.9 HEAD
    # list to a file first: under pipefail, `tar | grep -q` fails with SIGPIPE (141) when grep stops reading early (GNU tar)
    if tar -tzf "$T/rel/googlebook-anywhere-v9.9.9.tar.gz" > "$T/rel.list" 2> /dev/null && grep -q "/docs/images/kano-welcome.png$" "$T/rel.list"; then
        ok "the release archive carries docs/images"; else bad "the release archive lacks docs/images"; fi
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

# install.sh: argument guard
expect "install.sh requires --work" 2 "--work DIR is required" -- bash "$ROOT/install/install.sh"
# write-usb.sh refuses to run without root (it writes a block device)
if [ "$(id -u)" != 0 ]; then expect "write-usb.sh refuses non-root" 2 "run as root" -- bash "$ROOT/install/write-usb.sh" "$T/x.img" /dev/null; fi

echo "builder tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
