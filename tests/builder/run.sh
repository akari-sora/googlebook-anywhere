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

for f in "$ROOT"/builder/*.sh "$ROOT"/base/*.sh "$ROOT"/install/*.sh "$ROOT"/install/stick/*.sh "$ROOT"/tests/*/*.sh "$ROOT"/tools/release/*.sh; do
    bash -n "$f" && ok "bash -n ${f#$ROOT/}" || bad "bash -n ${f#$ROOT/}"
done
for f in "$ROOT"/builder/*.py "$ROOT"/builder/lib/*.py "$ROOT"/base/*.py "$ROOT"/install/stick/*.py "$ROOT"/tests/*/*.py; do
    python3 -m py_compile "$f" && ok "py_compile ${f#$ROOT/}" || bad "py_compile ${f#$ROOT/}"
done

# build.sh input guards (all fail in preflight, before anything is written)
touch "$T/base.img" "$T/initrd.img"; printf -- '-----BEGIN PRIVATE KEY-----\n' > "$T/key.pem"; mkdir -p "$T/ota" "$T/fw"
B=(env GBOS_BASE="$T/base.img" GBOS_INITRD="$T/initrd.img" GBOS_OTATOOLS="$T/ota" GBOS_AVB_KEY="$T/key.pem" GBOS_INPUTS="$T/in")
expect "build.sh refuses an absolute GBOS_IMAGE_NAME" 2 "plain file name" -- "${B[@]}" GBOS_OUT="$T/o1" GBOS_IMAGE_NAME=/etc/passwd.img bash "$ROOT/builder/build.sh"
expect "build.sh refuses a GBOS_IMAGE_NAME with a path" 2 "plain file name" -- "${B[@]}" GBOS_OUT="$T/o1" GBOS_IMAGE_NAME=../x.img bash "$ROOT/builder/build.sh"
mkdir -p "$T/o2"; touch "$T/o2/leftover"
expect "build.sh refuses a non-empty GBOS_OUT" 2 "new or empty directory" -- "${B[@]}" GBOS_OUT="$T/o2" bash "$ROOT/builder/build.sh"
expect "build.sh requires GBOS_INPUTS" 2 "GBOS_INPUTS" -- env -u GBOS_INPUTS -u GBOS_FIRMWARE -u GBOS_AUDIO_SRC GBOS_BASE="$T/base.img" GBOS_INITRD="$T/initrd.img" GBOS_OTATOOLS="$T/ota" GBOS_AVB_KEY="$T/key.pem" GBOS_OUT="$T/o3" bash "$ROOT/builder/build.sh"

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

# install.sh: argument guard
expect "install.sh requires --work" 2 "--work DIR is required" -- bash "$ROOT/install/install.sh"
# write-usb.sh refuses to run without root (it writes a block device)
if [ "$(id -u)" != 0 ]; then expect "write-usb.sh refuses non-root" 2 "run as root" -- bash "$ROOT/install/write-usb.sh" "$T/x.img" /dev/null; fi

echo "builder tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
