#!/usr/bin/env bash
# Offline checks for install/stick/make-maint.sh and install/make-installer-stick.sh:
# arguments, pin-list refusals, fake cache files, stick layout math, and one
# sparse installer-stick image. No root, no network, no block device.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MM="$ROOT/install/stick/make-maint.sh"
T=$(mktemp -d "${TMPDIR:-/tmp}/gbos-maint.XXXXXX")
trap 'rm -rf "$T"' EXIT
export PYTHONPYCACHEPREFIX="$T/pycache" PYTHONDONTWRITEBYTECODE=1
pass=0
fail=0
ok()  { echo "PASS $1"; pass=$((pass + 1)); }
bad() { echo "FAIL $1"; fail=$((fail + 1)); }
expect() {  # expect <name> <exit code> <output regex> -- command...
    local name=$1 code=$2 re=$3
    shift 4
    out=$("$@" 2>&1); rc=$? || true
    if [ "$rc" = "$code" ] && printf '%s\n' "$out" | grep -Eq -- "$re"; then
        ok "$name"
    else
        bad "$name (rc=$rc): $(printf '%s\n' "$out" | tail -n 3)"
    fi
}

# A curl earlier on PATH records a call. Nothing in this test may reach it.
mkdir -p "$T/bin"
cat > "$T/bin/curl" << 'EOF'
#!/bin/sh
echo called >> "${GBOS_CURL_LOG:?}"
exit 99
EOF
chmod +x "$T/bin/curl"
export PATH="$T/bin:$PATH" GBOS_CURL_LOG="$T/curl.log"

LIN=0fc63daf-8483-4772-8e79-3d69d8477de4
python3 - "$T" "$ROOT" "$LIN" << 'PY'
import sys
sys.path.insert(0, sys.argv[2] + "/tests/stick")
import gptlib
from pathlib import Path
t = Path(sys.argv[1])
lin = sys.argv[3]

def write(name, sectors, parts):
    p = t / name
    with open(p, "wb") as f:
        f.truncate(sectors * 512)
        gptlib.write_gpt(f, sectors, parts)
    return p

def part(num, name, first, last):
    return dict(num=num, name=name, type=lin, first=first, last=last)

write("small.img", 8192, [part(1, "super", 2048, 4095)])
write("overlap.img", 8192, [part(1, "super", 2048, 3000), part(2, "userdata", 3000, 4095)])
write("past.img", 8192, [part(1, "super", 2048, 8160)])
write("early.img", 8192, [part(1, "super", 10, 100)])
write("p32.img", 8192, [part(32, "extra", 2048, 4095)])
write("named.img", 8192, [part(1, "XBOOTLDR", 2048, 4095)])
write("empty-gpt.img", 8192, [])
raw = bytearray((t / "small.img").read_bytes())
bad_hdr = bytearray(raw); bad_hdr[512 + 56] ^= 0x01; (t / "bad-header.img").write_bytes(bad_hdr)
bad_ent = bytearray(raw); bad_ent[1024 + 56] ^= 0x01; (t / "bad-entries.img").write_bytes(bad_ent)
bad_bak = bytearray(raw); bad_bak[-512 + 56] ^= 0x01; (t / "bad-backup.img").write_bytes(bad_bak)
(t / "empty.img").write_bytes(b"")
(t / "odd.img").write_bytes(b"x" * 100)
(t / "nombr.img").write_bytes(b"\x00" * (8192 * 512))
(t / "small.img").chmod(0o644)
(t / "link.img").symlink_to(t / "small.img")
(t / "not-a-dir").write_bytes(b"x")
PY

expect "no arguments prints usage" 2 "usage: make-maint.sh layout" -- bash "$MM"
expect "unknown command is refused" 2 "unknown command" -- bash "$MM" not-a-command
expect "layout without --image is refused" 2 "--image is required" -- bash "$MM" layout
expect "layout refuses a missing image" 2 "cannot read" -- bash "$MM" layout --image "$T/missing.img"
expect "layout refuses a directory" 2 "not a regular file" -- bash "$MM" layout --image "$T"
expect "layout refuses a symlink" 2 "is a symlink" -- bash "$MM" layout --image "$T/link.img"
expect "layout refuses an empty file" 2 "empty or not whole" -- bash "$MM" layout --image "$T/empty.img"
expect "layout refuses a size that is not whole sectors" 2 "empty or not whole" -- bash "$MM" layout --image "$T/odd.img"
expect "layout refuses a file with no protective MBR" 2 "no protective MBR" -- bash "$MM" layout --image "$T/nombr.img"
expect "layout refuses a bad primary GPT header CRC" 2 "GPT header CRC at LBA 1" -- bash "$MM" layout --image "$T/bad-header.img"
expect "layout refuses a bad GPT entries CRC" 2 "GPT entries CRC" -- bash "$MM" layout --image "$T/bad-entries.img"
expect "layout refuses a bad backup GPT header" 2 "GPT header CRC at LBA 8191" -- bash "$MM" layout --image "$T/bad-backup.img"
expect "layout refuses a partition before the usable area" 2 "before the usable area" -- bash "$MM" layout --image "$T/early.img"
expect "layout refuses a partition past the usable area" 2 "past the usable area" -- bash "$MM" layout --image "$T/past.img"
expect "layout refuses overlapping partitions" 2 "overlap" -- bash "$MM" layout --image "$T/overlap.img"
expect "layout refuses partition 32 inside the image" 2 "already has partition 32" -- bash "$MM" layout --image "$T/p32.img"
expect "layout refuses an image that already names XBOOTLDR" 2 "named XBOOTLDR" -- bash "$MM" layout --image "$T/named.img"
expect "layout refuses a GPT with no partitions" 2 "no partitions" -- bash "$MM" layout --image "$T/empty-gpt.img"
expect "layout refuses a repeated --image" 2 "given twice" -- bash "$MM" layout --image "$T/small.img" --image "$T/small.img"
expect "layout refuses a disk-sectors value that is not a number" 2 "positive integer" -- bash "$MM" layout --image "$T/small.img" --disk-sectors 0
expect "layout refuses a disk smaller than the maintenance region" 2 "needs 17305633" -- bash "$MM" layout --image "$T/small.img" --disk-sectors 17305632

before=$(python3 -c 'import os,sys; print(os.stat(sys.argv[1]).st_mtime_ns)' "$T/small.img")
out=$(bash "$MM" layout --image "$T/small.img" --disk-sectors 17305633 2>"$T/layout.err"); rc=$? || true
after=$(python3 -c 'import os,sys; print(os.stat(sys.argv[1]).st_mtime_ns)' "$T/small.img")
want="layout ok
image_sectors 8192
googlebook_end_lba 4096
googlebook_copy_sectors 4096
xbootldr_name XBOOTLDR
xbootldr_type bc13c2ff-59e6-4262-a352-b275fd6f7172
xbootldr_first 4096
xbootldr_last 1052671
xbootldr_sectors 1048576
kano_maint_name kano-maint
kano_maint_type 0fc63daf-8483-4772-8e79-3d69d8477de4
kano_maint_first 1052672
kano_maint_last 17305599
kano_maint_sectors 16252928
maint_end_lba 17305600
stick_min_sectors 17305633
disk_sectors 17305633
disk ok"
if [ "$rc" = 0 ] && [ "$out" = "$want" ] && [ ! -s "$T/layout.err" ] && [ "$before" = "$after" ]; then
    ok "layout of a small image matches the stick geometry"
else
    bad "layout of a small image (rc=$rc)"
    printf '%s\n' "$out" | diff -u - <(printf '%s\n' "$want") || true
fi
# The copy stops at the last Googlebook sector, not at the image's backup GPT.
if printf '%s\n' "$out" | grep -qx "googlebook_copy_sectors 4096" \
        && printf '%s\n' "$out" | grep -qx "image_sectors 8192"; then
    ok "layout copies only up to the last partition, not the image's backup GPT"
else
    bad "layout copy length followed the file size"
fi

# 512 MiB XBOOTLDR and 7936 MiB kano-maint: tests/harness/test_real_layout.py
if printf '%s\n' "$out" | grep -qx "xbootldr_sectors 1048576" \
        && printf '%s\n' "$out" | grep -qx "kano_maint_sectors 16252928"; then
    ok "XBOOTLDR is 512 MiB and kano-maint is 7936 MiB"
else
    bad "maintenance partition sizes drifted"
fi
# END_SECT 37244880 plus this region plus 33 backup sectors must fit 28*10^9 bytes.
# write() refuses a shorter disk before it opens a device. A disk of exactly
# 28000000000 bytes is not refused for size.
if fit=$(python3 - "$ROOT" "$T/small.img" 2>&1 << 'PY'
import importlib.util
import os
import sys

repo, image = sys.argv[1], sys.argv[2]

def load(filename):
    path = os.path.join(repo, "install/stick", filename)
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_")[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

layout = load("maint-layout.py")
ext4 = load("stick-ext4.py")
assemble = load("maint-assemble.py")
end = layout.REPRESENTATIVE_END_LBA
budget = layout.STICK_BUDGET_BYTES
if end != 37244880 or budget != 28000000000:
    raise SystemExit("representative end or 28.0 GB budget changed")
kano = layout.MAINT_REGION_SECTORS - layout.XBOOTLDR_SECTORS
stick_min = end + layout.MAINT_REGION_SECTORS + layout.BACKUP_SECTORS
stick_bytes = stick_min * 512
if stick_bytes > budget:
    raise SystemExit("representative image is %s bytes, over %s" % (stick_bytes, budget))
if layout.XBOOTLDR_SECTORS != 1048576 or kano != 16252928 or stick_min != 54546417 or stick_bytes != 27927765504:
    raise SystemExit("representative geometry drifted: %s %s %s %s" % (layout.XBOOTLDR_SECTORS, kano, stick_min, stick_bytes))
if ext4.BLOCKS * ext4.BLOCK != kano * 512 or assemble.KANO_MAINT_BYTES != kano * 512:
    raise SystemExit("ext4 or pack size does not match the layout")
if ext4.GROUPS * 32 > ext4.BLOCK or 81 in ext4.SPARSE:
    raise SystemExit("group descriptors do not match 62 groups")
# Captured so a refusal does not look like a successful plan.
import io
from contextlib import redirect_stderr, redirect_stdout
buf = io.StringIO()
with redirect_stdout(buf):
    layout.plan(end, end, budget // 512)
if "stick_min_sectors %s" % stick_min not in buf.getvalue() or "disk ok" not in buf.getvalue().splitlines():
    raise SystemExit("a 28000000000-byte disk was not accepted")
errbuf = io.StringIO()
try:
    with redirect_stdout(io.StringIO()), redirect_stderr(errbuf):
        layout.plan(end, end, stick_min - 1)
except SystemExit as err:
    if err.code != 2 or ("needs %s" % stick_min) not in errbuf.getvalue():
        raise SystemExit("short representative disk: %s %s" % (err.code, errbuf.getvalue()))
else:
    raise SystemExit("one sector under the representative minimum was accepted")
writer = os.path.join(repo, "install/write-usb.sh")
text = open(writer, encoding="utf-8").read()
shell_gate = '[ "$I_BYTES" -ge "$SIZE" ] || refuse "$I_NODE ($I_BYTES bytes) is smaller than the image ($SIZE bytes)"'
if shell_gate not in text:
    raise SystemExit("write-usb.sh lost the shell size gate")
lines = text.splitlines(keepends=True)
start = next(i for i, line in enumerate(lines) if line.startswith("# >>> write-usb.py"))
end_mark = next(i for i, line in enumerate(lines) if line.startswith("# <<< write-usb.py"))
extracted = os.path.join(os.path.dirname(image), "write-usb.py")
open(extracted, "w", encoding="utf-8").write("".join(lines[start:end_mark]))
mod_spec = importlib.util.spec_from_file_location("write_usb", extracted)
wusb = importlib.util.module_from_spec(mod_spec)
mod_spec.loader.exec_module(wusb)
size = os.path.getsize(image)
sha = "ab" * 32
try:
    wusb.write(image, sha, "/no/such-gbos-device", "1:1", str(size - 1), "/sys/no-such")
except wusb.Stop as err:
    if "larger than the disk" not in str(err):
        raise SystemExit("short disk: %s" % err)
else:
    raise SystemExit("write() accepted a disk smaller than the image")
ifd = os.open(image, os.O_RDONLY)
try:
    try:
        wusb.gpt_plan(ifd, size, size - 512)
    except wusb.Stop as err:
        if "smaller than the image" not in str(err):
            raise SystemExit("gpt_plan short disk: %s" % err)
    else:
        raise SystemExit("gpt_plan accepted a disk smaller than the image")
finally:
    os.close(ifd)
rep = os.path.join(os.path.dirname(image), "representative.img")
fd = os.open(rep, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
os.ftruncate(fd, stick_bytes)
try:
    wusb.write(rep, sha, "/no/such-gbos-device", "1:1", str(budget), "/sys/no-such")
except wusb.Stop as err:
    if "larger than the disk" in str(err):
        raise SystemExit("28000000000 bytes was treated as smaller than the representative image")
else:
    raise SystemExit("write() passed the device open")
finally:
    os.close(fd)
print("fit ok")
PY
); then
    if [ "$fit" = "fit ok" ]; then
        ok "representative image fits 28000000000 bytes and a smaller disk is refused"
    else
        bad "representative image fit ($fit)"
    fi
else
    bad "representative image fit failed (${fit:-no output})"
fi

pins_n=$(grep -Ev '^[[:space:]]*(#|$)' "$ROOT/install/stick/maint.pins" | wc -l | tr -d ' ')
expect "committed pin list parses" 0 "pins ${pins_n}\$" -- bash "$MM" check-pins
if [ "$pins_n" = 0 ]; then
    expect "fetch refuses the empty committed pin list" 2 "no pinned files" -- bash "$MM" fetch
    expect "fetch --cache refuses the empty committed pin list" 2 "no pinned files" -- bash "$MM" fetch --cache "$T/no-such-cache"
else
    expect "fetch checks the committed pins and does not download" 0 "pins ${pins_n}\$" -- bash "$MM" fetch
    expect "fetch --cache refuses a missing directory before downloading" 2 "cannot read" -- bash "$MM" fetch --cache "$T/no-such-cache"
fi
expect "fetch rejects an alternate pin file" 2 "takes no --file" -- bash "$MM" fetch --file "$T/other.pins"
expect "fetch rejects --file after --cache" 2 "takes no --file" -- bash "$MM" fetch --cache "$T" --file "$T/other.pins"
expect "fetch rejects a fixture directory" 2 "takes no --from-dir" -- bash "$MM" fetch --from-dir "$T"
expect "fetch rejects an unknown argument" 2 "unknown argument" -- bash "$MM" fetch --side
expect "fetch refuses --cache without a path" 2 "--cache needs a path" -- bash "$MM" fetch --cache
expect "fetch refuses a repeated --cache" 2 "given twice" -- bash "$MM" fetch --cache "$T" --cache "$T"
expect "check-pins refuses a missing file" 2 "cannot read" -- bash "$MM" check-pins --file "$T/no.pins"
expect "check-pins refuses a directory" 2 "not a regular file" -- bash "$MM" check-pins --file "$T"
expect "check-pins refuses a symlink" 2 "is a symlink" -- bash "$MM" check-pins --file "$T/link.img"

sha_abc=$(python3 -c 'import hashlib; print(hashlib.sha256(b"abc").hexdigest())')
sha_def=$(python3 -c 'import hashlib; print(hashlib.sha256(b"def").hexdigest())')
mkdir -p "$T/cache-ok"
printf 'abc' > "$T/cache-ok/aaa"
printf 'def' > "$T/cache-ok/bbb"
cat > "$T/good.pins" << EOF
# temporary pins for the offline test
${sha_abc} 3 aaa https://example.invalid/aaa
${sha_def} 3 bbb https://example.invalid/bbb
EOF
# Local bytes only. The wrapper fails the test if a socket is opened.
cat > "$T/nosocket.py" << 'EOF'
import socket
import sys
import runpy
def boom(*_a, **_k):
    sys.stderr.write("socket opened\n")
    raise SystemExit(3)
socket.socket = boom
socket.create_connection = boom
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
EOF
FP="$ROOT/install/stick/maint-fetch.py"
if [ "$pins_n" != 0 ]; then
    expect "committed pins against a missing cache open no socket" 2 "cannot read" -- \
        python3 -I "$T/nosocket.py" "$FP" --file "$ROOT/install/stick/maint.pins" --cache "$T/no-such-cache"
fi
mkdir -p "$T/fix" "$T/fcache"
printf 'abc' > "$T/fix/aaa"
printf 'def' > "$T/fix/bbb"
expect "fixture fetch publishes both pinned files" 0 "fetch ok" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache" --from-dir "$T/fix"
if [ "$(cat "$T/fcache/aaa")" = "abc" ] && [ "$(cat "$T/fcache/bbb")" = "def" ] \
        && [ ! -e "$T/fcache/aaa.gbos-partial" ]; then
    ok "fetched bytes match the pin"
else
    bad "fetched bytes do not match the pin"
fi
rm -f "$T/fix/aaa"
expect "a matching cache file is kept without the fixture" 0 "kept aaa" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache" --from-dir "$T/fix"
printf 'abc' > "$T/fix/aaa"
mkdir -p "$T/fix-bad" "$T/fcache-bad"
printf 'abX' > "$T/fix-bad/aaa"
printf 'def' > "$T/fix-bad/bbb"
expect "a wrong fixture hash is refused" 2 "sha256 mismatch for aaa" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache-bad" --from-dir "$T/fix-bad"
if [ ! -e "$T/fcache-bad/aaa" ] && [ ! -e "$T/fcache-bad/aaa.gbos-partial" ] \
        && [ ! -e "$T/fcache-bad/bbb" ]; then
    ok "a hash mismatch publishes nothing"
else
    bad "a hash mismatch left a file"
fi
mkdir -p "$T/fix-long" "$T/fcache-long"
printf 'abcd' > "$T/fix-long/aaa"
expect "a longer fixture is refused" 2 "download has 4, pin says 3" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache-long" --from-dir "$T/fix-long"
if [ ! -e "$T/fcache-long/aaa" ] && [ ! -e "$T/fcache-long/aaa.gbos-partial" ]; then
    ok "a size mismatch leaves no partial"
else
    bad "a size mismatch left a file"
fi
mkdir -p "$T/fix-one" "$T/fcache-one"
printf 'zzz' > "$T/fcache-one/aaa"
printf 'zzz' > "$T/fcache-one/bbb"
printf 'abc' > "$T/fix-one/aaa"
printf 'xyz' > "$T/fix-one/bbb"
expect "a bad second file leaves the first replacement" 2 "sha256 mismatch for bbb" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache-one" --from-dir "$T/fix-one"
if [ "$(cat "$T/fcache-one/aaa" 2>/dev/null)" = "abc" ] && [ "$(cat "$T/fcache-one/bbb" 2>/dev/null)" = "zzz" ] \
        && [ ! -e "$T/fcache-one/bbb.gbos-partial" ]; then
    ok "only the verified file is replaced"
else
    bad "partial publish left the wrong bytes"
fi
mkdir -p "$T/fcache-link"
ln -s "$T/fix/aaa" "$T/fcache-link/aaa"
expect "a symlinked cache file is refused" 2 "not a regular file" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache-link" --from-dir "$T/fix"
if [ -L "$T/fcache-link/aaa" ]; then ok "the cache symlink stays"; else bad "the cache symlink was replaced"; fi
mkdir -p "$T/fix-link" "$T/fcache-fl"
ln -s "$T/fix/aaa" "$T/fix-link/aaa"
printf 'def' > "$T/fix-link/bbb"
expect "a symlinked fixture is refused" 2 "cannot read aaa" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache-fl" --from-dir "$T/fix-link"
if [ ! -e "$T/fcache-fl/aaa" ]; then ok "a symlinked fixture publishes nothing"; else bad "a symlinked fixture was copied"; fi
mkdir -p "$T/fcache-part"
ln -s "$T/fix/aaa" "$T/fcache-part/aaa.gbos-partial"
expect "a symlinked partial is refused" 2 "partial is not a regular file" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache-part" --from-dir "$T/fix"
if [ -L "$T/fcache-part/aaa.gbos-partial" ] && [ ! -e "$T/fcache-part/aaa" ]; then
    ok "a symlinked partial stays unpublished"
else
    bad "a symlinked partial was followed"
fi
rm -f "$T/fcache/aaa"
printf 'junk' > "$T/fcache/aaa.gbos-partial"
expect "a stale partial is replaced by the pinned bytes" 0 "fetched aaa" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache" --from-dir "$T/fix"
if [ "$(cat "$T/fcache/aaa")" = "abc" ] && [ ! -e "$T/fcache/aaa.gbos-partial" ]; then
    ok "the stale partial is gone"
else
    bad "the stale partial survived"
fi
echo '# none' > "$T/empty.pins"
expect "fetch refuses an empty fixture list" 2 "no pinned files" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/empty.pins" --cache "$T/fcache" --from-dir "$T/fix"
expect "fetch refuses a missing cache" 2 "cannot read" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/no-such-cache" --from-dir "$T/fix"
expect "fetch refuses a cache that is a file" 2 "not a directory" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/not-a-dir" --from-dir "$T/fix"
expect "fetch refuses a fixture that is a file" 2 "not a directory" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache" --from-dir "$T/not-a-dir"
cat > "$T/http.pins" << EOF
${sha_abc} 3 aaa http://example.invalid/aaa
EOF
expect "fetch refuses a non-https pin" 2 "url must be https" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/http.pins" --cache "$T/fcache" --from-dir "$T/fix"
expect "fetch refuses an unknown python argument" 2 "unknown argument" -- \
    python3 -I "$T/nosocket.py" "$FP" --file "$T/good.pins" --cache "$T/fcache" --side "$T/fix"
expect "verify-cache accepts files that match the pins" 0 "cache ok" -- bash "$MM" verify-cache --cache "$T/cache-ok" --file "$T/good.pins"
mkdir -p "$T/cache-bad"
printf 'abX' > "$T/cache-bad/aaa"
printf 'def' > "$T/cache-bad/bbb"
expect "verify-cache refuses a sha256 mismatch" 2 "sha256 mismatch for aaa" -- bash "$MM" verify-cache --cache "$T/cache-bad" --file "$T/good.pins"
mkdir -p "$T/cache-short"; printf 'ab' > "$T/cache-short/aaa"; printf 'def' > "$T/cache-short/bbb"
expect "verify-cache refuses a size mismatch" 2 "size mismatch for aaa" -- bash "$MM" verify-cache --cache "$T/cache-short" --file "$T/good.pins"
mkdir -p "$T/cache-miss"; printf 'def' > "$T/cache-miss/bbb"
expect "verify-cache refuses a missing file" 2 "aaa is not in the cache" -- bash "$MM" verify-cache --cache "$T/cache-miss" --file "$T/good.pins"
mkdir -p "$T/cache-link"; ln -s "$T/cache-ok/aaa" "$T/cache-link/aaa"; printf 'def' > "$T/cache-link/bbb"
expect "verify-cache refuses a symlinked cache file" 2 "aaa is not a regular file" -- bash "$MM" verify-cache --cache "$T/cache-link" --file "$T/good.pins"
ln -s "$T/cache-ok" "$T/cache-dir-link"
expect "verify-cache refuses a symlinked cache directory" 2 "is a symlink" -- bash "$MM" verify-cache --cache "$T/cache-dir-link" --file "$T/good.pins"
expect "verify-cache refuses a cache that is a file" 2 "not a directory" -- bash "$MM" verify-cache --cache "$T/not-a-dir" --file "$T/good.pins"

cat > "$T/http.pins" << EOF
${sha_abc} 3 aaa http://example.invalid/aaa
EOF
expect "check-pins refuses an http url" 2 "url must be https" -- bash "$MM" check-pins --file "$T/http.pins"
cat > "$T/user.pins" << EOF
${sha_abc} 3 aaa https://user:secret@example.invalid/aaa
EOF
expect "check-pins refuses userinfo in a url" 2 "without userinfo" -- bash "$MM" check-pins --file "$T/user.pins"
cat > "$T/query.pins" << EOF
${sha_abc} 3 aaa https://example.invalid/aaa?token=1
EOF
expect "check-pins refuses a query string" 2 "without a query" -- bash "$MM" check-pins --file "$T/query.pins"
cat > "$T/upper.pins" << EOF
$(printf '%s' "$sha_abc" | tr 'a-f' 'A-F') 3 aaa https://example.invalid/aaa
EOF
expect "check-pins refuses an uppercase sha256" 2 "lowercase hex" -- bash "$MM" check-pins --file "$T/upper.pins"
cat > "$T/zero.pins" << EOF
${sha_abc} 0 aaa https://example.invalid/aaa
EOF
expect "check-pins refuses a zero size" 2 "size must be a positive integer" -- bash "$MM" check-pins --file "$T/zero.pins"
cat > "$T/lead.pins" << EOF
${sha_abc} 03 aaa https://example.invalid/aaa
EOF
expect "check-pins refuses a leading zero in the size" 2 "no leading zero" -- bash "$MM" check-pins --file "$T/lead.pins"
cat > "$T/slash.pins" << EOF
${sha_abc} 3 a/aa https://example.invalid/aaa
EOF
expect "check-pins refuses a slash in the cache name" 2 "single cache file name" -- bash "$MM" check-pins --file "$T/slash.pins"
cat > "$T/dot.pins" << EOF
${sha_abc} 3 .. https://example.invalid/aaa
EOF
expect "check-pins refuses a dot-dot cache name" 2 "single cache file name" -- bash "$MM" check-pins --file "$T/dot.pins"
cat > "$T/dup.pins" << EOF
${sha_abc} 3 aaa https://example.invalid/aaa
${sha_def} 3 aaa https://example.invalid/other
EOF
expect "check-pins refuses a duplicate name" 2 "duplicate name aaa" -- bash "$MM" check-pins --file "$T/dup.pins"
cat > "$T/dupurl.pins" << EOF
${sha_abc} 3 aaa https://example.invalid/aaa
${sha_def} 3 bbb https://example.invalid/aaa
EOF
expect "check-pins refuses a duplicate url" 2 "duplicate url" -- bash "$MM" check-pins --file "$T/dupurl.pins"
cat > "$T/short.pins" << EOF
${sha_abc} 3 aaa
EOF
expect "check-pins refuses a short line" 2 "want sha256, size, name, url" -- bash "$MM" check-pins --file "$T/short.pins"
printf '%s 3 aaa https://example.invalid/aaa\r\n' "$sha_abc" > "$T/crlf.pins"
expect "check-pins refuses CR LF" 2 "LF line endings" -- bash "$MM" check-pins --file "$T/crlf.pins"

# A refusal must not create anything next to the cache.
stamp=$(find "$T/cache-bad" -type f | wc -l | tr -d ' ')
bash "$MM" verify-cache --cache "$T/cache-bad" --file "$T/good.pins" >/dev/null 2>&1 || true
now=$(find "$T/cache-bad" -type f | wc -l | tr -d ' ')
if [ "$stamp" = "$now" ]; then ok "verify-cache writes nothing on a mismatch"; else bad "verify-cache wrote into the cache"; fi

# Fixture archives. The committed pin list stays empty; these files are temporary.
python3 - "$T" "$ROOT" << 'PY'
import hashlib, importlib.util, io, os, re, struct, sys, tarfile
from pathlib import Path

t = Path(sys.argv[1])
root = Path(sys.argv[2])
cache = t / "acache"
cache.mkdir()
kver = "7.2.3-arch1-3"

spec = importlib.util.spec_from_file_location(
    "maint_assemble", root / "install/stick/maint-assemble.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
tools = list(mod.REQUIRED_TOOLS)

def command_v_names(path):
    text = Path(path).read_text(encoding="utf-8")
    names = []
    for match in re.finditer(r"for c in ([^;]+); do", text):
        window = text[match.end():match.end() + 160]
        if "command -v" in window:
            names.extend(match.group(1).split())
    return names

found = command_v_names(root / "install/stick/gbos-install.sh")
found += command_v_names(root / "install/stick/setup-stick.sh")
need = {"python3", "zstd", "sgdisk", "mcopy", "lz4", "mdir", "minfo", "umount"}
missing = need - set(found)
if missing:
    sys.stderr.write("drift parse missed %s\n" % " ".join(sorted(missing)))
    sys.exit(1)
extra = sorted(set(found) - set(tools))
if extra:
    sys.stderr.write("assembler omits %s\n" % " ".join(extra))
    sys.exit(1)
stick = (root / "install/stick/setup-stick.sh").read_text(encoding="utf-8")
if "blkid" not in stick or "systemctl" not in stick:
    sys.stderr.write("setup-stick.sh no longer runs blkid and systemctl\n")
    sys.exit(1)
if "systemctl" not in tools or "blkid" not in tools:
    sys.stderr.write("assembler does not require systemctl and blkid\n")
    sys.exit(1)
(t / "drift-ok").write_text("ok\n", encoding="utf-8")

def bz(version):
    buf = bytearray(1024)
    buf[0x202:0x206] = b"HdrS"
    payload = version.encode("ascii") + b" #1\0"
    off = 0x280
    buf[0x20E:0x210] = struct.pack("<H", off - 0x200)
    buf[off:off + len(payload)] = payload
    return bytes(buf)

KERNEL = bz(kver)

def add_bytes(tar, name, data, mode):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.type = tarfile.REGTYPE
    tar.addfile(info, io.BytesIO(data))

def add_link(tar, name, target):
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    tar.addfile(info)

def tree(builtin, *, initrd=b"initramfs-bytes", modules_kernel=KERNEL,
         mode_for=None, skip=(), extra_links=(), extra_files=(), kver_dir=kver,
         skip_boot=False, skip_initrd=False):
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w") as tar:
        if not skip_boot:
            add_bytes(tar, "boot/vmlinuz-linux", KERNEL, 0o644)
        if not skip_initrd:
            add_bytes(tar, "boot/initramfs-linux.img", initrd, 0o644)
        base = "usr/lib/modules/%s" % kver_dir
        add_bytes(tar, base + "/modules.builtin", builtin, 0o644)
        add_bytes(tar, base + "/vmlinuz", modules_kernel, 0o644)
        add_bytes(tar, "usr/bin/python3.real", b"#!/bin\n", 0o755)
        for name in tools:
            if name in skip:
                continue
            mode = 0o755 if mode_for is None else mode_for(name)
            if name == "python3":
                add_link(tar, "usr/bin/python3", "python3.real")
                continue
            if name == "blkid":
                add_bytes(tar, "bin/blkid", b"#!/bin\n", mode)
                continue
            add_bytes(tar, "usr/bin/" + name, b"#!/bin\n", mode)
        add_link(tar, "bin/systemctl", "../usr/bin/systemctl")
        for name, target in extra_links:
            add_link(tar, name, target)
        for name, data, mode in extra_files:
            add_bytes(tar, name, data, mode)
    return bio.getvalue()

BUILTIN = b"kernel/drivers/char/tpm/tpm.ko\nkernel/drivers/char/nvram.ko\nkernel/sound/core/snd.ko\n"

def save(name, blob):
    (cache / name).write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    return "%s %d %s https://example.invalid/%s\n" % (digest, len(blob), name, name)

def pins(name, rows):
    (t / name).write_text("# fixture\n" + "".join(rows), encoding="utf-8")

base = tree(BUILTIN, skip={"minfo"})
extra = io.BytesIO()
with tarfile.open(fileobj=extra, mode="w") as tar:
    add_bytes(tar, "usr/bin/minfo", b"#!/bin\n", 0o755)
pins("root.pins", [save("base.tar", base), save("extra.tar", extra.getvalue())])
pins("one.pins", [save("full.tar", tree(BUILTIN))])
pins("tz.pins", [save("tz.tar", tree(
    BUILTIN,
    extra_files=(("usr/share/zoneinfo/UTC", b"TZif2", 0o644),),
))])
pins("snd.pins", [save("snd.tar", tree(b"kernel/sound/core/snd.ko\n"))])
pins("i915.pins", [save("i915.tar", tree(b"kernel/drivers/gpu/drm/i915/i915.ko\n"))])
pins("mismatch.pins", [save("mismatch.tar", tree(BUILTIN, modules_kernel=bz("7.2.3-arch1-3") + b"\nnope"))])
pins("nokver.pins", [save("nokver.tar", tree(BUILTIN, kver_dir="0.0.0-test"))])
pins("emptyinit.pins", [save("emptyinit.tar", tree(BUILTIN, initrd=b""))])
pins("notexec.pins", [save("notexec.tar", tree(
    BUILTIN, mode_for=lambda name: 0o644 if name == "sgdisk" else 0o755))])
pins("nottar.pins", [save("notes.txt", b"this is not an archive\n")])

def raw_tar(header_and_body):
    return header_and_body

def ustar(name, size, typeflag=b"0", linkname=""):
    buf = bytearray(512)
    nb = name.encode("ascii")
    buf[0:len(nb)] = nb
    buf[100:108] = b"0000644\0"
    buf[108:116] = b"0000000\0"
    buf[116:124] = b"0000000\0"
    sb = ("%011o" % size).encode("ascii")
    buf[124:136] = sb + b"\0"
    buf[136:148] = b"00000000000\0"
    buf[156] = typeflag[0]
    lb = linkname.encode("ascii")
    buf[157:157 + len(lb)] = lb
    buf[257:263] = b"ustar\0"
    buf[263:265] = b"00"
    for i in range(148, 156):
        buf[i] = 0x20
    checksum = sum(buf)
    buf[148:156] = ("%06o\0 " % checksum).encode("ascii")
    return bytes(buf)

pins("escape.pins", [save("escape.tar", ustar("../../etc/passwd", 0) + b"\0" * 512)])
pins("abslink.pins", [save("abslink.tar", ustar("usr/bin/evil", 0, b"2", "/etc/passwd") + b"\0" * 512)])
pins("rellink.pins", [save("rellink.tar", ustar("usr/bin/bad", 0, b"2", "../../../../etc/passwd") + b"\0" * 512)])
pins("archbad.pins", [save("archbad.pkg.tar", ustar("usr/bin/evil", 0, b"2", "/etc/../passwd") + b"\0" * 512)])
pins("archok.pins", [save("archok.pkg.tar", tree(
    BUILTIN,
    extra_links=(("bin/sh", "/usr/bin/zstd"),),
    extra_files=(
        (".PKGINFO", b"pkgname = fixture\n", 0o644),
        (".BUILDINFO", b"build\n", 0o644),
        (".MTREE", b"mtree\n", 0o644),
        (".INSTALL", b"echo should-not-run\n", 0o644),
    ),
))])
pins("copied.pins", [save("copied.tar", tree(BUILTIN, skip_boot=True))])
pins("noinit.pins", [save("noinit.tar", tree(BUILTIN, skip_initrd=True))])
pins("mklink.pins", [save("mklink.tar", tree(
    BUILTIN, skip_initrd=True,
    extra_links=(("usr/bin/mkinitcpio", "python3.real"),),
))])
mk_ok = """#!/bin/sh
k=
g=
c=
if [ -z "${GBOS_MAINT_ROOT:-}" ]; then
    echo "GBOS_MAINT_ROOT is unset" >&2
    exit 1
fi
while [ $# -gt 0 ]; do
    case "$1" in
        -k) k=$2; shift 2 ;;
        -g) g=$2; shift 2 ;;
        -c) c=$2; shift 2 ;;
        *) echo "unexpected $1" >&2; exit 1 ;;
    esac
done
[ "$k" = "7.2.3-arch1-3" ] || { echo "wrong kernel $k" >&2; exit 1; }
[ "$g" = "/boot/initramfs-linux.img" ] || { echo "wrong output" >&2; exit 1; }
[ "$c" = "/etc/mkinitcpio-gbos.conf" ] || { echo "wrong config" >&2; exit 1; }
conf="${GBOS_MAINT_ROOT}/etc/mkinitcpio-gbos.conf"
grep -F 'HOOKS=(base systemd modconf block fsck)' "$conf" >/dev/null \\
    || { echo "config hooks" >&2; exit 1; }
grep -F 'MODULES=(usb_storage uas vfat)' "$conf" >/dev/null \\
    || { echo "config modules" >&2; exit 1; }
if grep -E '(^|[^A-Za-z0-9_])nvme(_core)?([^A-Za-z0-9_]|$)' "$conf" >/dev/null; then
    echo "config names nvme" >&2
    exit 1
fi
rule="${GBOS_MAINT_ROOT}/etc/modprobe.d/gbos-nvme.conf"
[ -f "$rule" ] && [ ! -L "$rule" ] || { echo "nvme modprobe rule missing" >&2; exit 1; }
printf '%s' 'install nvme /bin/false
install nvme_core /bin/false
' | cmp -s - "$rule" || { echo "nvme modprobe rule" >&2; exit 1; }
printf 'initramfs-bytes' > "${GBOS_MAINT_ROOT}${g}"
"""
mk_fail = """#!/bin/sh
echo "fixture refused" >&2
exit 1
"""
mk_empty = """#!/bin/sh
exit 0
"""
pins("mkok.pins", [save("mkok.tar", tree(
    BUILTIN, skip_initrd=True,
    extra_files=(("usr/bin/mkinitcpio", mk_ok.encode("ascii"), 0o755),),
))])
pins("mkfail.pins", [save("mkfail.tar", tree(
    BUILTIN, skip_initrd=True,
    extra_files=(("usr/bin/mkinitcpio", mk_fail.encode("ascii"), 0o755),),
))])
pins("mkempty.pins", [save("mkempty.tar", tree(
    BUILTIN, skip_initrd=True,
    extra_files=(("usr/bin/mkinitcpio", mk_empty.encode("ascii"), 0o755),),
))])
pins("dev.pins", [save("dev.tar", ustar("dev/zero", 0, b"3") + b"\0" * 512)])
pins("bomb.pins", [save("bomb.tar", ustar("big", 2 * 1024 * 1024 * 1024 + 1) + b"\0" * 512)])

slip = io.BytesIO()
with tarfile.open(fileobj=slip, mode="w") as tar:
    add_link(tar, "subdir/link", "..")
    add_bytes(tar, "subdir/link/pwned", b"pwned", 0o644)
pins("slip.pins", [save("slip.tar", slip.getvalue())])

hops = io.BytesIO()
with tarfile.open(fileobj=hops, mode="w") as tar:
    add_bytes(tar, "boot/vmlinuz-linux", KERNEL, 0o644)
    add_bytes(tar, "boot/initramfs-linux.img", b"initramfs-bytes", 0o644)
    add_bytes(tar, "usr/lib/modules/%s/modules.builtin" % kver, BUILTIN, 0o644)
    add_bytes(tar, "usr/lib/modules/%s/vmlinuz" % kver, KERNEL, 0o644)
    add_bytes(tar, "usr/bin/python3.real", b"#!/bin\n", 0o755)
    prev = "python3.real"
    for n in range(9, 0, -1):
        add_link(tar, "usr/bin/hop%d" % n, prev)
        prev = "hop%d" % n
    for name in tools:
        if name == "python3":
            add_link(tar, "usr/bin/python3", prev)
            continue
        if name == "blkid":
            add_bytes(tar, "bin/blkid", b"#!/bin\n", 0o755)
            continue
        add_bytes(tar, "usr/bin/" + name, b"#!/bin\n", 0o755)
pins("hops.pins", [save("hops.tar", hops.getvalue())])

pins("badsha.pins", [save("base.tar", base)])  # cache matches base; pin file rewritten below
digest_line = (t / "badsha.pins").read_text(encoding="utf-8").splitlines()[-1]
fields = digest_line.split()
fields[0] = "0" * 64
(t / "badsha.pins").write_text("# fixture\n" + " ".join(fields) + "\n", encoding="utf-8")

zst_blob = tree(b"kernel/sound/core/snd.ko\n")
(cache / "plain.tar").write_bytes(zst_blob)
import gzip
gz_blob = gzip.compress(zst_blob, mtime=0)
(cache / "plain.tar.gz").write_bytes(gz_blob)
gz_line = "%s %d plain.tar.gz https://example.invalid/plain.tar.gz\n" % (
    hashlib.sha256(gz_blob).hexdigest(), len(gz_blob))
(t / "gz.pins").write_text("# fixture\n" + gz_line, encoding="utf-8")
PY
fix_rc=$?
if [ -f "$T/drift-ok" ]; then ok "tool list covers setup-stick.sh and gbos-install.sh"; else bad "tool list drifted"; fi
if [ "$fix_rc" != 0 ]; then bad "fixture builder exited $fix_rc"; fi

if command -v zstd >/dev/null 2>&1; then
    zstd -q -f -c "$T/acache/plain.tar" > "$T/acache/plain.tar.zst"
    python3 - "$T" << 'PY'
import hashlib, sys
from pathlib import Path
t = Path(sys.argv[1])
blob = (t / "acache/plain.tar.zst").read_bytes()
line = "%s %d plain.tar.zst https://example.invalid/plain.tar.zst\n" % (
    hashlib.sha256(blob).hexdigest(), len(blob))
(t / "zst.pins").write_text("# fixture\n" + line, encoding="utf-8")
PY
    rm -f "$T/acache/plain.tar"
else
    bad "zstd is not on PATH; tar.zst assemble was not checked"
fi

absent() {
    if [ -e "$2" ] || [ -L "$2" ]; then bad "$1 wrote an output"; else ok "$1 left no output"; fi
}

expect "assemble without --cache is refused" 2 "--cache is required" -- bash "$MM" assemble --out "$T/no-cache"
expect "assemble without --out is refused" 2 "--out is required" -- bash "$MM" assemble --cache "$T/acache"
expect "build rejects an alternate pin file" 2 "takes no --file" -- bash "$MM" build --cache "$T/acache" --out "$T/build-file" --file "$T/root.pins"
absent "build --file" "$T/build-file"
if [ "$pins_n" = 0 ]; then
    expect "build refuses the empty committed pin list" 2 "no pinned files" -- bash "$MM" build --cache "$T/acache" --out "$T/build-empty"
else
    expect "build refuses a cache without the pinned files" 2 "is not in the cache" -- bash "$MM" build --cache "$T/acache" --out "$T/build-empty"
fi
absent "build with no pins" "$T/build-empty"
expect "assemble refuses a text file" 2 "not a tar archive" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-nottar" --file "$T/nottar.pins"
absent "text file" "$T/out-nottar"
expect "assemble refuses a path that escapes" 2 "escapes the root" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-escape" --file "$T/escape.pins"
absent "escape path" "$T/out-escape"
expect "assemble refuses a symlink to /etc/passwd" 2 "leaves the root" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-abslink" --file "$T/abslink.pins"
absent "absolute symlink" "$T/out-abslink"
expect "assemble refuses a relative symlink that escapes" 2 "leaves the root" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-rellink" --file "$T/rellink.pins"
absent "relative symlink" "$T/out-rellink"
expect "an arch package still refuses an absolute symlink that uses .." 2 "leaves the root" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-archbad" --file "$T/archbad.pins"
absent "arch absolute escape" "$T/out-archbad"
expect "assemble refuses a device node" 2 "unsupported type" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-dev" --file "$T/dev.pins"
absent "device node" "$T/out-dev"
expect "assemble refuses a member over 2 GiB" 2 "larger than" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-bomb" --file "$T/bomb.pins"
absent "oversize member" "$T/out-bomb"
expect "assemble refuses to follow a symlink while extracting" 2 "follow a symlink" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-slip" --file "$T/slip.pins"
absent "symlink write-through" "$T/out-slip"
expect "assemble refuses a sha256 mismatch and writes nothing" 2 "sha256 mismatch" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-badsha" --file "$T/badsha.pins"
absent "sha mismatch" "$T/out-badsha"
expect "assemble refuses i915 built in and not covered" 2 "i915" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-i915" --file "$T/i915.pins"
absent "i915 builtin" "$T/out-i915"
expect "assemble refuses a kernel copy that differs" 2 "does not match" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-mismatch" --file "$T/mismatch.pins"
absent "vmlinuz mismatch" "$T/out-mismatch"
expect "assemble refuses a modules directory for another version" 2 "modules.builtin" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-nokver" --file "$T/nokver.pins"
absent "kver mismatch" "$T/out-nokver"
expect "assemble refuses an empty initramfs" 2 "initramfs" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-emptyinit" --file "$T/emptyinit.pins"
absent "empty initramfs" "$T/out-emptyinit"
expect "assemble refuses a tool that is not executable" 2 "no executable sgdisk" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-notexec" --file "$T/notexec.pins"
absent "non-executable tool" "$T/out-notexec"
expect "assemble refuses a symlink chain past 8 hops" 2 "exceeded 8 hops" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-hops" --file "$T/hops.pins"
absent "symlink hops" "$T/out-hops"

expect "assemble accepts covered builtins and an unrelated module" 0 "assemble ok" -- bash "$MM" assemble --cache "$T/acache" --out "$T/tree" --file "$T/root.pins"
nvme_rule=$'install nvme /bin/false\ninstall nvme_core /bin/false\n'
prebuilt_rule="$T/tree/root/etc/modprobe.d/gbos-nvme.conf"
if [ -f "$prebuilt_rule" ] && [ ! -L "$prebuilt_rule" ] \
        && printf '%s' "$nvme_rule" | cmp -s - "$prebuilt_rule" \
        && [ ! -e "$T/tree/root/etc/mkinitcpio-gbos.conf" ]; then
    ok "a prebuilt initramfs still gets the NVMe install rules"
else
    bad "a prebuilt root is missing the NVMe install rules"
fi
machine_id=$'67626f73000000000000000000000001\n'
mid="$T/tree/root/etc/machine-id"
mid_mode=$(python3 -c 'import os,sys; print(format(os.lstat(sys.argv[1]).st_mode & 0o777, "o"))' "$mid" 2>/dev/null || true)
if [ -f "$mid" ] && [ ! -L "$mid" ] && [ "$mid_mode" = 444 ] \
        && printf '%s' "$machine_id" | cmp -s - "$mid" \
        && [ ! -e "$T/tree/root/etc/localtime" ] && [ ! -L "$T/tree/root/etc/localtime" ]; then
    ok "a root without zoneinfo gets the fixed machine-id and no localtime"
else
    bad "machine-id or unexpected localtime on a root without zoneinfo"
fi
expect "assemble links localtime when zoneinfo UTC is present" 0 "assemble ok" -- bash "$MM" assemble --cache "$T/acache" --out "$T/tree-tz" --file "$T/tz.pins"
tz_mid="$T/tree-tz/root/etc/machine-id"
tz_mode=$(python3 -c 'import os,sys; print(format(os.lstat(sys.argv[1]).st_mode & 0o777, "o"))' "$tz_mid" 2>/dev/null || true)
if [ -f "$tz_mid" ] && [ ! -L "$tz_mid" ] && [ "$tz_mode" = 444 ] \
        && printf '%s' "$machine_id" | cmp -s - "$tz_mid" \
        && [ -L "$T/tree-tz/root/etc/localtime" ] \
        && [ "$(readlink "$T/tree-tz/root/etc/localtime")" = "../usr/share/zoneinfo/UTC" ] \
        && [ -f "$T/tree-tz/root/usr/share/zoneinfo/UTC" ]; then
    ok "zoneinfo UTC becomes a relative /etc/localtime"
else
    bad "localtime was not a relative link to zoneinfo UTC"
fi
if python3 - "$ROOT" << 'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
tpl = (root / "install/stick/gbos-install.entry.template").read_text(encoding="utf-8")
calls = []
for line in (root / "install/stick/builtin-initcalls.txt").read_text(encoding="utf-8").splitlines():
    fields = line.split("#", 1)[0].split()
    if len(fields) >= 2 and fields[1] != "-":
        calls.append(fields[1])
part = "11111111-2222-3333-4444-555555555555"
rendered = tpl.replace("@ROOT_PARTUUID@", part).replace("@INITCALL_BLACKLIST@", ",".join(calls))
opts = [ln for ln in rendered.splitlines() if ln.startswith("options ")]
if len(opts) != 1:
    raise SystemExit("options")
opt = opts[0]
if "systemd.mask=systemd-firstboot.service" in opt:
    raise SystemExit("mask")
if not opt.endswith(" gbos.install=1"):
    raise SystemExit("install ending")
if len(opt) + 1 >= 1900:
    raise SystemExit("install length")
upd = opt[: -len(" gbos.install=1")] + " gbos.install=1 gbos.update=1"
if not upd.endswith(" gbos.install=1 gbos.update=1") or len(upd) + 1 >= 1900:
    raise SystemExit("update")
PY
then
    ok "reviewed cmdline has no firstboot mask and stays under 1900 bytes"
else
    bad "reviewed cmdline grew a firstboot mask or passed 1900 bytes"
fi
expect "assemble stores an arch absolute symlink inside the root" 0 "assemble ok" -- bash "$MM" assemble --cache "$T/acache" --out "$T/tree-arch" --file "$T/archok.pins"
link=$(readlink "$T/tree-arch/root/bin/sh" || true)
meta=0
for n in .PKGINFO .BUILDINFO .MTREE .INSTALL; do
    if [ -e "$T/tree-arch/root/$n" ] || [ -L "$T/tree-arch/root/$n" ]; then
        meta=1
    fi
done
if [ "$link" = "../usr/bin/zstd" ] && [ "$meta" = 0 ]; then
    ok "arch symlink is relative and package metadata was not extracted"
else
    bad "arch symlink or metadata: link=${link}"
fi
expect "assemble copies the modules kernel when boot has none" 0 "assemble ok" -- bash "$MM" assemble --cache "$T/acache" --out "$T/tree-copied" --file "$T/copied.pins"
if cmp -s "$T/tree-copied/root/boot/vmlinuz-linux" "$T/tree-copied/root/usr/lib/modules/7.2.3-arch1-3/vmlinuz" \
        && cmp -s "$T/tree-copied/root/boot/vmlinuz-linux" "$T/tree-copied/xbootldr/vmlinuz-linux"; then
    ok "boot kernel is the modules vmlinuz"
else
    bad "boot kernel was not copied from modules"
fi
expect "assemble refuses a root with no initramfs and no mkinitcpio" 2 "no boot/initramfs-linux.img" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-noinit" --file "$T/noinit.pins"
absent "missing initramfs" "$T/out-noinit"
expect "assemble refuses a mkinitcpio symlink" 2 "mkinitcpio is not an executable file" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-mklink" --file "$T/mklink.pins"
absent "mkinitcpio symlink" "$T/out-mklink"
cat > "$T/bin/unshare" << 'EOF'
#!/bin/sh
set -eu
if [ "$#" -ne 9 ] || [ "$1" != "-U" ] || [ "$2" != "-r" ] || [ "$3" != "-m" ] \
        || [ "$4" != "-p" ] || [ "$5" != "--fork" ]; then
    echo "fake unshare: unexpected arguments" >&2
    exit 1
fi
helper=$6
root=$7
kver=$8
tool=$9
case "$tool" in
    /*) ;;
    *) echo "fake unshare: tool is not absolute" >&2; exit 1 ;;
esac
printf '%s\n' "$helper" "$root" "$kver" "$tool" >> "${GBOS_UNSHARE_LOG:?}"
GBOS_MAINT_ROOT=$root
export GBOS_MAINT_ROOT
exec /bin/sh "${root}${tool}" -k "$kver" -g /boot/initramfs-linux.img -c /etc/mkinitcpio-gbos.conf
EOF
chmod +x "$T/bin/unshare"
export GBOS_UNSHARE_LOG="$T/unshare.log"
: > "$T/unshare.log"
expect "assemble builds an initramfs with the root mkinitcpio" 0 "^initramfs 15$" -- bash "$MM" assemble --cache "$T/acache" --out "$T/tree-initrd" --file "$T/mkok.pins"
helper_line=$(sed -n '1p' "$T/unshare.log")
kver_line=$(sed -n '3p' "$T/unshare.log")
tool_line=$(sed -n '4p' "$T/unshare.log")
case "$helper_line" in
    *initramfs-mount.sh) helper_ok=1 ;;
    *) helper_ok=0 ;;
esac
if [ "$helper_ok" = 1 ] \
        && [ "$kver_line" = "7.2.3-arch1-3" ] \
        && [ "$tool_line" = "/usr/bin/mkinitcpio" ] \
        && [ "$(cat "$T/tree-initrd/root/boot/initramfs-linux.img")" = "initramfs-bytes" ] \
        && grep -F 'HOOKS=(base systemd modconf block fsck)' "$T/tree-initrd/root/etc/mkinitcpio-gbos.conf" >/dev/null \
        && grep -F 'MODULES=(usb_storage uas vfat)' "$T/tree-initrd/root/etc/mkinitcpio-gbos.conf" >/dev/null \
        && ! grep -E '(^|[^A-Za-z0-9_])nvme(_core)?([^A-Za-z0-9_]|$)' "$T/tree-initrd/root/etc/mkinitcpio-gbos.conf" >/dev/null \
        && ! grep -F 'autodetect' "$T/tree-initrd/root/etc/mkinitcpio-gbos.conf" >/dev/null \
        && [ -f "$T/tree-initrd/root/etc/modprobe.d/gbos-nvme.conf" ] \
        && [ ! -L "$T/tree-initrd/root/etc/modprobe.d/gbos-nvme.conf" ] \
        && printf '%s' "$nvme_rule" | cmp -s - "$T/tree-initrd/root/etc/modprobe.d/gbos-nvme.conf" \
        && cmp -s "$T/tree-initrd/root/boot/initramfs-linux.img" "$T/tree-initrd/xbootldr/initramfs-linux.img"; then
    ok "built initramfs is the fixture bytes and the config skips autodetect"
else
    bad "built initramfs or its config"
fi
inst="$ROOT/install/stick/gbos-install.sh"
anchor=$(grep -n "the stick's super partition is not where the installer data puts it" "$inst" | head -n 1 | cut -d: -f1)
load=$(grep -n 'modprobe --ignore-install nvme_core && modprobe --ignore-install nvme' "$inst" | head -n 1 | cut -d: -f1)
if [ -n "$anchor" ] && [ -n "$load" ] && [ "$load" -gt "$anchor" ] \
        && grep -F -q 'install nvme /bin/false in modprobe.d' "$inst"; then
    ok "installer still loads NVMe only after the v0.2.0 checks"
else
    bad "installer NVMe load is no longer after the v0.2.0 checks"
fi
expect "assemble refuses when mkinitcpio exits non-zero" 2 "mkinitcpio could not write the initramfs: fixture refused" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-mkfail" --file "$T/mkfail.pins"
absent "mkinitcpio failure" "$T/out-mkfail"
expect "assemble refuses when mkinitcpio writes nothing" 2 "mkinitcpio did not write boot/initramfs-linux.img" -- bash "$MM" assemble --cache "$T/acache" --out "$T/out-mkempty" --file "$T/mkempty.pins"
absent "mkinitcpio wrote nothing" "$T/out-mkempty"
expect "assemble reports the kernel version" 0 "kver 7.2.3-arch1-3" -- bash "$MM" assemble --cache "$T/acache" --out "$T/tree-again" --file "$T/one.pins"
if [ -f "$T/tree/xbootldr/vmlinuz-linux" ] && [ ! -L "$T/tree/xbootldr/vmlinuz-linux" ] \
        && [ -f "$T/tree/xbootldr/initramfs-linux.img" ] && [ ! -L "$T/tree/xbootldr/initramfs-linux.img" ] \
        && cmp -s "$T/tree/root/boot/vmlinuz-linux" "$T/tree/xbootldr/vmlinuz-linux" \
        && cmp -s "$T/tree/root/boot/initramfs-linux.img" "$T/tree/xbootldr/initramfs-linux.img"; then
    ok "XBOOTLDR files are copies of the kernel and initramfs"
else
    bad "XBOOTLDR files are not plain copies"
fi
if [ -L "$T/tree/root/usr/bin/python3" ] && [ -x "$T/tree/root/usr/bin/python3.real" ] \
        && [ -x "$T/tree/root/bin/blkid" ] && [ -x "$T/tree/root/usr/bin/minfo" ]; then
    ok "relative tool symlinks and the second archive are in the root"
else
    bad "assembled root is missing a tool or a relative symlink"
fi
expect "assemble refuses an output that already exists" 2 "output already exists" -- bash "$MM" assemble --cache "$T/acache" --out "$T/tree" --file "$T/root.pins"
expect "assemble accepts a builtin list with no blocked modules" 0 "assemble ok" -- bash "$MM" assemble --cache "$T/acache" --out "$T/tree-snd" --file "$T/snd.pins"

before=$(find "$T/tree" -type f | wc -l | tr -d ' ')
expect "pack refuses while files would not be owned by root" 2 "refusing to pack: files would be owned by uid " -- bash "$MM" pack --out "$T/tree"
after=$(find "$T/tree" -type f | wc -l | tr -d ' ')
if [ "$before" = "$after" ] && [ ! -e "$T/tree/kano-maint.img" ] && [ ! -e "$T/tree/xbootldr.img" ]; then
    ok "pack writes no image"
else
    bad "pack wrote an image or changed the tree"
fi
expect "pack without --out is refused" 2 "--out is required" -- bash "$MM" pack

mkdir -p "$T/imgtools" "$T/sdk/platform-tools"
ln -s "$(command -v python3)" "$T/imgtools/python3"
ln -s "$(command -v dirname)" "$T/imgtools/dirname"
ln -s "$(command -v bash)" "$T/imgtools/bash"
ln -s "$(command -v python3)" "$T/bin/python3"
ln -s "$(command -v dirname)" "$T/bin/dirname"
ln -s "$(command -v bash)" "$T/bin/bash"
cat > "$T/imgtools/mke2fs" << 'EOF'
#!/bin/sh
if [ "$1" = "-h" ]; then
    echo "mke2fs [-d root-directory|tarball]"
    exit 0
fi
printf '%s\n' "$*" >> "${GBOS_MKE2FS_LOG:?}"
if [ "${GBOS_MKE2FS_FAIL:-}" = 1 ]; then
    echo "forced failure"
    exit 1
fi
exit 0
EOF
cat > "$T/imgtools/mkfs.vfat" << 'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "${GBOS_MKFS_LOG:?}"
exit 0
EOF
cat > "$T/imgtools/mcopy" << 'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "${GBOS_MCOPY_LOG:?}"
exit 0
EOF
cat > "$T/sdk/platform-tools/mke2fs" << 'EOF'
#!/bin/sh
echo called >> "${GBOS_ANDROID_MKE2FS_LOG:?}"
echo "mke2fs [-d root-directory]"
exit 0
EOF
chmod +x "$T/imgtools/mke2fs" "$T/imgtools/mkfs.vfat" "$T/imgtools/mcopy" "$T/sdk/platform-tools/mke2fs"
IMGPATH="$T/imgtools:$T/bin"

expect "pack refuses without image tools even under FAKEROOTKEY" 2 "mke2fs -d, mkfs.vfat, and mcopy are required" -- env FAKEROOTKEY=1 PATH="$T/bin" bash "$MM" pack --out "$T/tree-again"
if [ ! -e "$T/tree-again/kano-maint.img" ] && [ ! -e "$T/tree-again/xbootldr.img" ]; then
    ok "pack without image tools writes nothing"
else
    bad "pack without image tools wrote an image"
fi

: > "$T/android-mke2fs.log"
expect "pack does not run an mke2fs under platform-tools" 2 "mke2fs -d, mkfs.vfat, and mcopy are required" -- env FAKEROOTKEY=1 PATH="$T/sdk/platform-tools:$T/bin" GBOS_ANDROID_MKE2FS_LOG="$T/android-mke2fs.log" bash "$MM" pack --out "$T/tree-again"
if [ ! -s "$T/android-mke2fs.log" ]; then
    ok "platform-tools mke2fs was not executed"
else
    bad "platform-tools mke2fs was executed"
fi

cp -R "$T/tree-snd" "$T/tree-miss"
rm -f "$T/tree-miss/xbootldr/vmlinuz-linux"
expect "pack refuses a missing kernel before calling tools" 2 "xbootldr is missing vmlinuz-linux" -- env FAKEROOTKEY=1 PATH="$IMGPATH" bash "$MM" pack --out "$T/tree-miss"
if [ ! -e "$T/tree-miss/kano-maint.img" ] && [ ! -e "$T/tree-miss/kano-maint.img.partial" ]; then
    ok "a missing kernel creates no image"
else
    bad "a missing kernel created an image"
fi

cp -R "$T/tree-snd" "$T/tree-emptyk"
: > "$T/tree-emptyk/xbootldr/vmlinuz-linux"
expect "pack refuses an empty kernel" 2 "xbootldr vmlinuz-linux is empty" -- env FAKEROOTKEY=1 PATH="$IMGPATH" bash "$MM" pack --out "$T/tree-emptyk"

cp -R "$T/tree-snd" "$T/tree-linkk"
rm -f "$T/tree-linkk/xbootldr/vmlinuz-linux"
ln -s "$T/tree-snd/xbootldr/vmlinuz-linux" "$T/tree-linkk/xbootldr/vmlinuz-linux"
expect "pack refuses a symlink kernel" 2 "xbootldr vmlinuz-linux is not a regular file" -- env FAKEROOTKEY=1 PATH="$IMGPATH" bash "$MM" pack --out "$T/tree-linkk"

cp -R "$T/tree-snd" "$T/tree-partial"
printf 'keep' > "$T/tree-partial/kano-maint.img.partial"
expect "pack refuses an existing partial image" 2 "kano-maint.img.partial already exists" -- env FAKEROOTKEY=1 PATH="$IMGPATH" GBOS_MKE2FS_LOG="$T/no-mke2fs.log" GBOS_MKFS_LOG="$T/no-mkfs.log" GBOS_MCOPY_LOG="$T/no-mcopy.log" bash "$MM" pack --out "$T/tree-partial"
if [ "$(wc -c < "$T/tree-partial/kano-maint.img.partial" | tr -d ' ')" = 4 ] && [ ! -e "$T/no-mke2fs.log" ]; then
    ok "an existing partial image is left unchanged"
else
    bad "an existing partial image was changed or tools ran"
fi

: > "$T/fail-mke2fs.log"
: > "$T/fail-mkfs.log"
cp -R "$T/tree-snd" "$T/tree-fail"
expect "pack removes a partial image when mke2fs fails" 2 "mke2fs exited 1" -- env FAKEROOTKEY=1 PATH="$IMGPATH" GBOS_MKE2FS_FAIL=1 GBOS_MKE2FS_LOG="$T/fail-mke2fs.log" GBOS_MKFS_LOG="$T/fail-mkfs.log" GBOS_MCOPY_LOG="$T/fail-mcopy.log" bash "$MM" pack --out "$T/tree-fail"
if [ ! -e "$T/tree-fail/kano-maint.img" ] && [ ! -e "$T/tree-fail/kano-maint.img.partial" ] \
        && [ ! -e "$T/tree-fail/xbootldr.img" ] && [ ! -e "$T/tree-fail/xbootldr.img.partial" ] \
        && [ ! -s "$T/fail-mkfs.log" ]; then
    ok "a failed mke2fs leaves no image and does not format FAT"
else
    bad "a failed mke2fs left an image or formatted FAT"
fi

root_before=$(find "$T/tree-snd/root" -type f | wc -l | tr -d ' ')
expect "pack writes both images under FAKEROOTKEY" 0 "pack ok" -- env FAKEROOTKEY=1 PATH="$IMGPATH" GBOS_MKE2FS_LOG="$T/mke2fs.log" GBOS_MKFS_LOG="$T/mkfs.log" GBOS_MCOPY_LOG="$T/mcopy.log" bash "$MM" pack --out "$T/tree-snd"
root_after=$(find "$T/tree-snd/root" -type f | wc -l | tr -d ' ')
ext_size=$(python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$T/tree-snd/kano-maint.img")
fat_size=$(python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$T/tree-snd/xbootldr.img")
packed_root=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$T/tree-snd/root")
if [ "$root_before" = "$root_after" ] && [ "$ext_size" = 8321499136 ] && [ "$fat_size" = 536870912 ] \
        && grep -F -- "-F -q -t ext4 -b 4096 -L kano-maint -d $packed_root " "$T/mke2fs.log" >/dev/null \
        && grep -F -- "2031616" "$T/mke2fs.log" >/dev/null \
        && grep -F -- "-F 32 -n XBOOTLDR" "$T/mkfs.log" >/dev/null \
        && grep -F -- "::vmlinuz-linux" "$T/mcopy.log" >/dev/null \
        && grep -F -- "::initramfs-linux.img" "$T/mcopy.log" >/dev/null; then
    ok "pack arguments and image sizes match the stick geometry"
else
    bad "pack arguments or image sizes (ext=$ext_size fat=$fat_size root=$root_before/$root_after)"
fi

python3 - "$T" "$ROOT" > "$T/own.out" 2>&1 << 'PY'
import importlib.util
import os
import sys

tmp, repo = sys.argv[1], sys.argv[2]
script = os.path.join(repo, "install", "stick", "maint-assemble.py")
spec = importlib.util.spec_from_file_location("maint_assemble", script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
tree = os.path.join(tmp, "own-tree")
outside = os.path.join(tmp, "own-outside")
os.mkdir(tree, 0o755)
os.mkdir(outside, 0o755)
os.mkdir(os.path.join(tree, "sub"), 0o755)
open(os.path.join(tree, "sub", "f"), "w").close()
open(os.path.join(outside, "secret"), "w").close()
os.symlink(os.path.join(outside, "secret"), os.path.join(tree, "link"))
os.symlink(outside, os.path.join(tree, "dirlink"))
calls = []
mod.os.geteuid = lambda: 0
mod.os.lchown = lambda path, uid, gid: calls.append(os.path.abspath(path))
mod.own_as_root(tree)
inside = {
    os.path.abspath(tree),
    os.path.abspath(os.path.join(tree, "sub")),
    os.path.abspath(os.path.join(tree, "sub", "f")),
    os.path.abspath(os.path.join(tree, "link")),
    os.path.abspath(os.path.join(tree, "dirlink")),
}
got = set(calls)
if got != inside:
    sys.exit("owned set mismatch")
outside_abs = os.path.abspath(outside)
for path in calls:
    if path == outside_abs or path.startswith(outside_abs + os.sep):
        sys.exit("followed a symlink")
calls.clear()
mod.os.geteuid = lambda: 501
mod.own_as_root(tree)
if calls:
    sys.exit("chowned while not root")
print("own-as-root ok")

import io
import stat
import tarfile

def extract_modes(items, dest):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, mode, kind in items:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = 2
                tar.addfile(info, io.BytesIO(b"hi"))
            else:
                tar.addfile(info)
    buf.seek(0)
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=buf, mode="r:") as tar:
        for member in tar:
            mod.extract_one(tar, member, dest, 0, False)

sample = os.path.join(tmp, "mode-root")
extract_modes([
    ("usr", 0o755, tarfile.DIRTYPE),
    ("usr/bin", 0o755, tarfile.DIRTYPE),
    ("usr/bin/su", 0o4755, tarfile.REGTYPE),
    ("usr/lib", 0o755, tarfile.DIRTYPE),
    ("usr/lib/dbus-daemon-launch-helper", 0o4110, tarfile.REGTYPE),
    ("tmp", 0o1777, tarfile.DIRTYPE),
], sample)
su = os.lstat(os.path.join(sample, "usr/bin/su"))
helper = os.lstat(os.path.join(sample, "usr/lib/dbus-daemon-launch-helper"))
tmpdir = os.lstat(os.path.join(sample, "tmp"))
if stat.S_IMODE(su.st_mode) != 0o4755:
    sys.exit("su mode %o" % stat.S_IMODE(su.st_mode))
if stat.S_IMODE(helper.st_mode) != 0o4110:
    sys.exit("helper mode %o" % stat.S_IMODE(helper.st_mode))
if stat.S_IMODE(tmpdir.st_mode) != 0o1777:
    sys.exit("tmp mode %o" % stat.S_IMODE(tmpdir.st_mode))
mod.os.geteuid = lambda: 501
mod.grant_owner_read(sample)
if stat.S_IMODE(os.lstat(os.path.join(sample, "usr/lib/dbus-daemon-launch-helper")).st_mode) != 0o4110:
    sys.exit("changed mode while not root")
mod.os.geteuid = lambda: 0
mod.grant_owner_read(sample)
if stat.S_IMODE(os.lstat(os.path.join(sample, "usr/lib/dbus-daemon-launch-helper")).st_mode) != 0o4510:
    sys.exit("helper was not given owner-read")
if stat.S_IMODE(os.lstat(os.path.join(sample, "usr/bin/su")).st_mode) != 0o4755:
    sys.exit("su mode changed")
print("mode-bits ok")
PY
if [ "$?" = 0 ] && grep -q "own-as-root ok" "$T/own.out" && grep -q "mode-bits ok" "$T/own.out"; then
    ok "pack records uid 0 without following a symlink"
    ok "archive setuid and sticky bits survive and owner-read is added for the copy"
else
    bad "pack uid 0 walk: $(tail -n 5 "$T/own.out")"
fi

expect "assemble reads a tar.gz archive" 0 "assemble ok" -- bash "$MM" assemble --cache "$T/acache" --out "$T/tree-gz" --file "$T/gz.pins"
if [ -f "$T/zst.pins" ]; then
    expect "assemble reads a tar.zst archive" 0 "assemble ok" -- bash "$MM" assemble --cache "$T/acache" --out "$T/tree-zst" --file "$T/zst.pins"
    mkdir -p "$T/nozstd"
    ln -s "$(command -v python3)" "$T/nozstd/python3"
    ln -s "$(command -v dirname)" "$T/nozstd/dirname"
    expect "assemble refuses tar.zst when zstd is absent" 2 "zstd is required" -- env PATH="$T/nozstd" "$(command -v bash)" "$MM" assemble --cache "$T/acache" --out "$T/out-nozstd" --file "$T/zst.pins"
    absent "tar.zst without zstd" "$T/out-nozstd"
fi

STICK="$ROOT/install/make-installer-stick.sh"
mkdir -p "$T/xb" "$T/xb-miss" "$T/xb-empty" "$T/xb-link"
python3 - "$T" << 'PY'
from pathlib import Path
t = Path(__import__("sys").argv[1])
raw = bytearray((t / "small.img").read_bytes())
raw[34 * 512] = 0xA5
raw[34 * 512 + 1] = 0x5A
raw[100 * 512 + 10] = 0x42
raw[4095 * 512 + 511] = 0x3C
(t / "stick-src.img").write_bytes(raw)
vmlinuz = bytes((i * 17 + 3) & 0xFF for i in range(4096))
initrd = bytes((i * 31 + 9) & 0xFF for i in range(4097))
(t / "xb" / "vmlinuz-linux").write_bytes(vmlinuz)
(t / "xb" / "initramfs-linux.img").write_bytes(initrd)
(t / "xb-miss" / "initramfs-linux.img").write_bytes(initrd)
(t / "xb-empty" / "vmlinuz-linux").write_bytes(b"")
(t / "xb-empty" / "initramfs-linux.img").write_bytes(initrd)
(t / "xb-link" / "initramfs-linux.img").write_bytes(initrd)
(t / "xb-link" / "vmlinuz-linux").symlink_to(t / "xb" / "vmlinuz-linux")
(t / "xb-dir-link").symlink_to(t / "xb")
(t / "stick-exists.img").write_bytes(b"keep")
PY
if python3 - "$T" "$ROOT" "$LIN" << 'PY'
import importlib.util, os, subprocess, sys
from pathlib import Path
t = Path(sys.argv[1])
root = Path(sys.argv[2])
lin = sys.argv[3]
esp_type = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
sys.path.insert(0, str(root / "tests/stick"))
import gptlib

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, root / "install/stick" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

esp = load("esp_fat", "esp-fat.py")
payload = load("stick_payload", "stick-payload.py")
kernel = (t / "xb" / "vmlinuz-linux").read_bytes()
initrd = (t / "xb" / "initramfs-linux.img").read_bytes()
names = payload.module_names((root / "install/stick/gbos-install.entry.template").read_text())
base = (
    "options root=x rw efi=noruntime module_blacklist=%s "
    "initcall_blacklist=nvme_init,mei_me_driver_init" % ",".join(names)
)
quiet = "title Googlebook KANO quiet\nlinux /kernel.efi\ninitrd /initrd.img\n%s\n" % base
debug = "title Googlebook KANO serial-debug\nlinux /kernel.efi\ninitrd /initrd.img\n%s console=ttyS0\n" % base
loader_src = "default lapis-quiet.conf\ntimeout 3\neditor no\n"
loader_new = payload.esp_loader_text()
if loader_src == loader_new or "console=ttyS0" in quiet or "console=ttyS0" not in debug:
    raise SystemExit("loader fixture is wrong")
blob = esp.format_fat12(1024, {
    "loader/loader.conf": loader_src.encode(),
    "loader/entries/lapis-quiet.conf": quiet.encode(),
    "loader/entries/lapis-debug.conf": debug.encode(),
})
rt = t / "esp-rt.img"
rt.write_bytes(blob)
fd = os.open(rt, os.O_RDWR)
try:
    if esp.read_file(fd, 0, len(blob), "loader/loader.conf") != loader_src.encode():
        raise SystemExit("loader.conf round trip")
    listed = esp.list_files(fd, 0, len(blob), "loader/entries")
    if listed.get("lapis-quiet.conf") != quiet.encode() or listed.get("lapis-debug.conf") != debug.encode():
        raise SystemExit("entry round trip")
    boot = os.pread(fd, 512, 0)
    changed = esp.patch_file(fd, 0, len(blob), "loader/loader.conf", loader_new.encode())
    if not changed or esp.read_file(fd, 0, len(blob), "loader/loader.conf") != loader_new.encode():
        raise SystemExit("patch round trip")
    if os.pread(fd, 512, 0) != boot:
        raise SystemExit("patch touched the boot sector")
finally:
    os.close(fd)

def make_root(path):
    path.mkdir()
    bindir = path / "usr/bin"
    bindir.mkdir(parents=True)
    for name in payload.REQUIRED_TOOLS:
        tool = bindir / name
        tool.write_bytes(b"")
        tool.chmod(0o755)
    moddir = path / "usr/lib/modules" / "7.2.7-arch1-1"
    moddir.mkdir(parents=True)
    (moddir / "modules.builtin").write_bytes(b"")
    (moddir / "vmlinuz").write_bytes(kernel)
    helper = path / "usr/lib/dbus-daemon-launch-helper"
    helper.write_bytes(b"dbus-helper-bytes\n")
    os.chmod(helper, 0o4110)
    try:
        os.open(helper, os.O_RDONLY)
    except PermissionError:
        pass
    else:
        raise SystemExit("helper is readable before the copy")

def write_image(path, parts, super_byte):
    with open(path, "wb") as handle:
        handle.truncate(8192 * 512)
        handle.seek(3200 * 512)
        handle.write(bytes([super_byte]) + b"\x5a" * 64)
        handle.seek(3072 * 512)
        handle.write(b"\xB0")
        handle.seek(4000 * 512)
        handle.write(b"\xDA")
        handle.seek(2048 * 512)
        handle.write(blob)
        gptlib.write_gpt(handle, 8192, parts)

full_parts = [
    dict(num=1, name="EFI-SYSTEM", type=esp_type, first=2048, last=3071),
    dict(num=2, name="boot_a", type=lin, first=3072, last=3199),
    dict(num=3, name="super", type=lin, first=3200, last=3999),
    dict(num=4, name="userdata", type=lin, first=4000, last=4095),
]
write_image(t / "full.img", full_parts, 0x11)
dense_parts = [dict(num=1, name="p1", type=lin, first=100, last=100)]
slot = 101
for num in list(range(6, 11)) + list(range(12, 31)) + [31]:
    dense_parts.append(dict(num=num, name="p%d" % num, type=lin, first=slot, last=slot))
    slot += 1
dense_parts.extend([
    dict(num=2, name="EFI-SYSTEM", type=esp_type, first=2048, last=3071),
    dict(num=3, name="boot_a", type=lin, first=3072, last=3199),
    dict(num=4, name="super", type=lin, first=3200, last=3999),
    dict(num=5, name="userdata", type=lin, first=4000, last=4095),
])
write_image(t / "dense.img", dense_parts, 0x22)
make_root(t / "root")
make_root(t / "root-dense")
xb_dense = t / "xb-dense"
xb_dense.mkdir()
(xb_dense / "vmlinuz-linux").write_bytes(kernel)
(xb_dense / "initramfs-linux.img").write_bytes(initrd)

def make_pristine(image, dest):
    proc = subprocess.run(
        [sys.executable, "-I", str(root / "install/stick/make-pristine.py"), str(image), str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr or proc.stdout)

def super_hash(path):
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) > 7 and fields[2] == "super":
            return fields[7]
    raise SystemExit("no super hash in %s" % path)

make_pristine(t / "full.img", t / "pristine")
make_pristine(t / "dense.img", t / "pristine-dense")
if super_hash(t / "pristine/manifest.txt") == super_hash(t / "pristine-dense/manifest.txt"):
    raise SystemExit("dense super hash matches full.img")
print("fixtures ok")
PY
then ok "installer fixtures include an ESP, a root and pristine data"
else bad "installer fixtures failed"
fi
DUMMY=(--root "$T/xb" --pristine "$T/xb")

expect "installer stick without arguments is refused" 2 "usage: make-installer-stick.sh" -- bash "$STICK"
expect "installer stick without --image is refused" 2 "--image is required" -- bash "$STICK" --out "$T/no-image.img" --xbootldr "$T/xb"
expect "installer stick without --out is refused" 2 "--out is required" -- bash "$STICK" --image "$T/stick-src.img" --xbootldr "$T/xb"
expect "installer stick without --xbootldr is refused" 2 "--xbootldr is required" -- bash "$STICK" --image "$T/stick-src.img" --out "$T/no-xb.img"
expect "installer stick without --root is refused" 2 "--root is required" -- bash "$STICK" --image "$T/stick-src.img" --out "$T/no-root.img" --xbootldr "$T/xb" --pristine "$T/xb"
expect "installer stick without --pristine is refused" 2 "--pristine is required" -- bash "$STICK" --image "$T/stick-src.img" --out "$T/no-pristine.img" --xbootldr "$T/xb" --root "$T/xb"
expect "installer stick refuses an unknown argument" 2 "unknown argument" -- bash "$STICK" --image "$T/stick-src.img" --out "$T/no-xb.img" --xbootldr "$T/xb" --extra
expect "installer stick refuses a repeated --image" 2 "given twice" -- bash "$STICK" --image "$T/stick-src.img" --image "$T/stick-src.img" --out "$T/no-xb.img" --xbootldr "$T/xb"
expect "installer stick refuses /dev/null" 2 "under /dev" -- bash "$STICK" --image "$T/stick-src.img" --out /dev/null --xbootldr "$T/xb" "${DUMMY[@]}"
if [ -c /dev/null ]; then ok "/dev/null is still a character device"; else bad "/dev/null changed"; fi
expect "installer stick refuses a disk path under /dev" 2 "under /dev" -- bash "$STICK" --image "$T/stick-src.img" --out /dev/disk/by-id/usb-test --xbootldr "$T/xb" "${DUMMY[@]}"
absent "a /dev/disk path" /dev/disk/by-id/usb-test
expect "installer stick refuses a device image" 2 "refusing to read a device" -- bash "$STICK" --image /dev/zero --out "$T/from-dev.img" --xbootldr "$T/xb" "${DUMMY[@]}"
absent "a device image" "$T/from-dev.img"
expect "installer stick refuses an existing output" 2 "output already exists" -- bash "$STICK" --image "$T/stick-src.img" --out "$T/stick-exists.img" --xbootldr "$T/xb" "${DUMMY[@]}"
if [ "$(wc -c < "$T/stick-exists.img" | tr -d ' ')" = 4 ]; then ok "an existing output is left unchanged"; else bad "an existing output was changed"; fi
expect "installer stick refuses a disk smaller than the maintenance region" 2 "needs 17305633" -- bash "$STICK" --image "$T/stick-src.img" --out "$T/stick-small.img" --xbootldr "$T/xb" --disk-sectors 17305632 "${DUMMY[@]}"
absent "a short disk" "$T/stick-small.img"
expect "installer stick refuses a missing kernel" 2 "missing vmlinuz-linux" -- bash "$STICK" --image "$T/stick-src.img" --out "$T/stick-miss.img" --xbootldr "$T/xb-miss" "${DUMMY[@]}"
absent "a missing kernel" "$T/stick-miss.img"
expect "installer stick refuses an empty kernel" 2 "vmlinuz-linux is empty" -- bash "$STICK" --image "$T/stick-src.img" --out "$T/stick-empty.img" --xbootldr "$T/xb-empty" "${DUMMY[@]}"
absent "an empty kernel" "$T/stick-empty.img"
expect "installer stick refuses a symlink kernel" 2 "vmlinuz-linux is a symlink" -- bash "$STICK" --image "$T/stick-src.img" --out "$T/stick-link.img" --xbootldr "$T/xb-link" "${DUMMY[@]}"
absent "a symlink kernel" "$T/stick-link.img"
expect "installer stick refuses a symlink directory" 2 "is a symlink" -- bash "$STICK" --image "$T/stick-src.img" --out "$T/stick-dlink.img" --xbootldr "$T/xb-dir-link" "${DUMMY[@]}"
absent "a symlink directory" "$T/stick-dlink.img"
absent "a partial from the refusals" "$T/stick-small.img.gbos-partial"

cp "$T/stick-src.img" "$T/mark-src.img"
printf 'secret-reason\n' > "$T/secret-marker"
ln -s "$T/secret-marker" "$T/mark-src.img.not-self-tested"
expect "installer stick refuses a symlink source marker" 2 "\.not-self-tested is a symlink" -- bash "$STICK" --image "$T/mark-src.img" --out "$T/from-link.img" --xbootldr "$T/xb" "${DUMMY[@]}"
if printf '%s\n' "secret-reason" | cmp -s - "$T/secret-marker"; then
    ok "a symlink source marker is not followed"
else
    bad "a symlink source marker was followed"
fi
absent "a symlink source marker" "$T/from-link.img"
rm "$T/mark-src.img.not-self-tested"
mkdir "$T/mark-src.img.not-self-tested"
expect "installer stick refuses a directory source marker" 2 "\.not-self-tested is not a regular file" -- bash "$STICK" --image "$T/mark-src.img" --out "$T/from-dir.img" --xbootldr "$T/xb" "${DUMMY[@]}"
absent "a directory source marker" "$T/from-dir.img"
rmdir "$T/mark-src.img.not-self-tested"
python3 -c 'import sys; open(sys.argv[1], "wb").write(b"x" * 4097)' "$T/mark-src.img.not-self-tested"
expect "installer stick refuses a source marker over 4096 bytes" 2 "larger than 4096 bytes" -- bash "$STICK" --image "$T/mark-src.img" --out "$T/from-huge.img" --xbootldr "$T/xb" "${DUMMY[@]}"
absent "a huge source marker" "$T/from-huge.img"
rm "$T/mark-src.img.not-self-tested"
printf 'keep-me\n' > "$T/mark-kept.img.not-self-tested"
expect "installer stick refuses an existing untested marker" 2 "output marker already exists" -- bash "$STICK" --image "$T/mark-src.img" --out "$T/mark-kept.img" --xbootldr "$T/xb" "${DUMMY[@]}"
if printf '%s\n' "keep-me" | cmp -s - "$T/mark-kept.img.not-self-tested"; then
    ok "an existing untested marker is left unchanged"
else
    bad "an existing untested marker was changed"
fi
absent "an existing untested marker" "$T/mark-kept.img"
printf 'safe\n' > "$T/marker-target"
ln -s "$T/marker-target" "$T/mark-symlink-out.img.not-self-tested"
expect "installer stick refuses a symlink output marker" 2 "output marker already exists" -- bash "$STICK" --image "$T/mark-src.img" --out "$T/mark-symlink-out.img" --xbootldr "$T/xb" "${DUMMY[@]}"
if printf '%s\n' "safe" | cmp -s - "$T/marker-target"; then
    ok "a symlink output marker is not followed"
else
    bad "a symlink output marker was followed"
fi
absent "a symlink output marker" "$T/mark-symlink-out.img"

reason_line='built with GBOS_SKIP_SELFTEST=1: never booted in QEMU before release'
stick_line='installer stick was not booted in QEMU by make-installer-stick'
printf '%s\n' "$reason_line" > "$T/full.img.not-self-tested"
out=$(bash "$STICK" --image "$T/full.img" --out "$T/stick.img" --xbootldr "$T/xb" --root "$T/root" --pristine "$T/pristine" 2>"$T/stick.err"); rc=$? || true
want="stick ok
image_sectors 8192
googlebook_copy_sectors 4096
xbootldr_first 4096
kano_maint_first 1052672
disk_sectors 17305633
minimum_stick_sectors 17305633
xbootldr files 6"
head8=$(printf '%s\n' "$out" | awk 'NR <= 8 { print }')
if [ "$rc" = 0 ] && [ "$head8" = "$want" ] && [ ! -s "$T/stick.err" ] \
        && printf '%s\n' "$out" | grep -q "does not open a block device" \
        && printf '%s\n' "$out" | grep -q "write-usb.sh" \
        && printf '%s\n' "$out" | grep -q "disk serial" \
        && printf '%s\n' "$out" | grep -q "reads the image back" \
        && printf '%s\n' "$out" | grep -q -- "--keep-data" \
        && printf '%s\n' "$out" | grep -q "gbos-update.py" \
        && printf '%s\n' "$out" | grep -q "kano-maint ext4" \
        && printf '%s\n' "$out" | grep -q "owners uid 0" \
        && printf '%s\n' "$out" | grep -q "Install Googlebook to the internal disk" \
        && printf '%s\n' "$out" | grep -Eq '^sha256 [0-9a-f]{64}$' \
        && ! printf '%s\n' "$out" | grep -q "setup-stick.sh"; then
    ok "installer stick reports the layout and the user steps"
else
    bad "installer stick report (rc=$rc): $(printf '%s\n' "$out" | tail -n 8) $(tail -n 8 "$T/stick.err")"
fi
if py=$(python3 - "$T" "$ROOT" 2>&1 << 'PY'
import hashlib, importlib.util, os, stat, struct, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2] + "/tests/stick")
import gptlib
t = Path(sys.argv[1])
root = Path(sys.argv[2])
stick = t / "stick.img"
src = t / "full.img"
disk = 17305633

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, root / "install/stick" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def read_at(path, off, n):
    with open(path, "rb") as handle:
        handle.seek(off)
        got = handle.read(n)
    if len(got) != n:
        raise SystemExit("short read at %s" % off)
    return got

def same_range(start, end):
    with src.open("rb") as a, stick.open("rb") as b:
        a.seek(start)
        b.seek(start)
        left = end - start
        while left:
            n = min(left, 1 << 20)
            if a.read(n) != b.read(n):
                raise SystemExit("range %s-%s differs" % (start, end))
            left -= n

if os.path.getsize(stick) != disk * 512:
    raise SystemExit("stick size")
mbr = read_at(stick, 0, 512)
want_mbr = bytearray(512)
want_mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\x00\x02\x00", 0xEE, b"\xff\xff\xff", 1, min(disk - 1, 0xFFFFFFFF))
want_mbr[510:512] = b"\x55\xaa"
if mbr != bytes(want_mbr) or any(mbr[462:510]):
    raise SystemExit("protective MBR")
sh, sp = gptlib.read_gpt(str(src))
hh, hp = gptlib.read_gpt(str(stick))
if hh["disk"] == sh["disk"] or hh["first"] != 34 or hh["last"] != disk - 34 or hh["alt"] != disk - 1:
    raise SystemExit("stick GPT header")
if set(hp) != {1, 2, 3, 4, 32, 33}:
    raise SystemExit("partition numbers %s" % sorted(hp))
if (hp[1]["first"], hp[1]["last"], hp[1]["name"], hp[1]["type"], hp[1]["guid"]) != (
        2048, 3071, "EFI-SYSTEM", sp[1]["type"], sp[1]["guid"]):
    raise SystemExit("googlebook partition was not copied")
if read_at(src, 1024, 128) != read_at(stick, 1024, 128):
    raise SystemExit("partition 1 bytes differ")
for num, name, first, last in ((2, "boot_a", 3072, 3199), (3, "super", 3200, 3999), (4, "userdata", 4000, 4095)):
    if (hp[num]["name"], hp[num]["first"], hp[num]["last"], hp[num]["type"], hp[num]["guid"]) != (
            name, first, last, sp[num]["type"], sp[num]["guid"]):
        raise SystemExit("partition %s changed" % num)
if (hp[32]["name"], hp[32]["first"], hp[32]["last"], hp[32]["type"]) != (
        "XBOOTLDR", 4096, 1052671, "bc13c2ff-59e6-4262-a352-b275fd6f7172"):
    raise SystemExit("XBOOTLDR partition %s" % hp[32])
if (hp[33]["name"], hp[33]["first"], hp[33]["last"], hp[33]["type"]) != (
        "kano-maint", 1052672, 17305599, "0fc63daf-8483-4772-8e79-3d69d8477de4"):
    raise SystemExit("kano-maint partition %s" % hp[33])
same_range(34 * 512, 2048 * 512)
same_range(3072 * 512, 4096 * 512)
same_range(2048 * 512, 2048 * 512 + 512)
if read_at(stick, 8191 * 512, 512) != bytes(512):
    raise SystemExit("sector 8191 is not empty")
if read_at(stick, 1052672 * 512, 512) != bytes(512):
    raise SystemExit("kano-maint sector 1052672 is not empty")
if read_at(stick, 17305599 * 512, 512) != bytes(512):
    raise SystemExit("kano-maint sector 17305599 is not empty")
if read_at(stick, 1052674 * 512 + 0x38, 2) != b"\x53\xef":
    raise SystemExit("ext4 magic")
esp = load("esp_fat", "esp-fat.py")
payload = load("stick_payload", "stick-payload.py")
ext4 = load("stick_ext4", "stick-ext4.py")
esp_off, esp_len = 2048 * 512, 1024 * 512
src_fd = os.open(src, os.O_RDONLY)
out_fd = os.open(stick, os.O_RDONLY)
try:
    for name in ("lapis-quiet.conf", "lapis-debug.conf"):
        rel = "loader/entries/" + name
        if esp.read_file(src_fd, esp_off, esp_len, rel) != esp.read_file(out_fd, esp_off, esp_len, rel):
            raise SystemExit("%s changed" % name)
    src_loader = esp.read_file(src_fd, esp_off, esp_len, "loader/loader.conf")
    out_loader = esp.read_file(out_fd, esp_off, esp_len, "loader/loader.conf")
    if out_loader != payload.esp_loader_text().encode() or out_loader == src_loader:
        raise SystemExit("loader.conf was not patched")
    part_off = 1052672 * 512
    part_len = (17305599 - 1052672 + 1) * 512
    mode, uid, gid, data, is_link = ext4.read_path(out_fd, part_off, part_len, "usr/local/sbin/gbos-install")
    if uid != 0 or gid != 0 or is_link or not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o755 or not data.startswith(b"#!"):
        raise SystemExit("gbos-install owner %s %s" % (uid, gid))
    link_mode, link_uid, link_gid, target, link = ext4.read_path(
        out_fd, part_off, part_len, "etc/systemd/system/multi-user.target.wants/gbos-installer.service")
    if link_uid != 0 or link_gid != 0 or not link or target != b"/etc/systemd/system/gbos-installer.service":
        raise SystemExit("service link")
    hmode, huid, hgid, hdata, hlink = ext4.read_path(
        out_fd, part_off, part_len, "usr/lib/dbus-daemon-launch-helper")
    if (
        hlink or huid != 0 or hgid != 0 or not stat.S_ISREG(hmode)
        or stat.S_IMODE(hmode) != 0o4110 or hdata != b"dbus-helper-bytes\n"
    ):
        raise SystemExit(
            "helper in image mode=%s uid=%s gid=%s link=%s data=%r"
            % (oct(stat.S_IMODE(hmode)), huid, hgid, hlink, hdata)
        )
finally:
    os.close(src_fd)
    os.close(out_fd)
src_helper = t / "root/usr/lib/dbus-daemon-launch-helper"
if stat.S_IMODE(src_helper.lstat().st_mode) != 0o4110:
    raise SystemExit("source helper mode changed")
try:
    os.open(src_helper, os.O_RDONLY)
except PermissionError:
    pass
else:
    raise SystemExit("source helper stayed readable")
print("helper mode ok")
if (t / "root/usr/local/sbin/gbos-install").exists():
    raise SystemExit("source root was modified")

def checksum(sfn):
    total = 0
    for byte in sfn:
        total = ((total & 1) << 7) + (total >> 1) + byte
        total &= 0xFF
    return total

def lfn_name(entries, sfn):
    expect = checksum(sfn)
    ordered = sorted(entries, key=lambda entry: entry[0] & 0x1F)
    if [entry[0] & 0x1F for entry in ordered] != list(range(1, len(ordered) + 1)):
        raise SystemExit("lfn sequence")
    if not ordered[-1][0] & 0x40 or any(entry[0] & 0x40 for entry in ordered[:-1]):
        raise SystemExit("lfn last flag")
    chars = []
    for entry in ordered:
        if entry[11] != 0x0F or entry[13] != expect or entry[26:28] != b"\x00\x00":
            raise SystemExit("lfn checksum")
        raw = entry[1:11] + entry[14:26] + entry[28:32]
        for offset in range(0, 26, 2):
            unit = struct.unpack_from("<H", raw, offset)[0]
            if unit == 0:
                return "".join(chars)
            if unit == 0xFFFF:
                raise SystemExit("lfn padding")
            chars.append(chr(unit))
    return "".join(chars)

def parse_blob(blob):
    found = []
    pending = []
    for index in range(0, len(blob), 32):
        entry = blob[index:index + 32]
        if entry[0] == 0:
            break
        if entry[11] == 0x0F:
            pending.append(entry)
            continue
        sfn = entry[:11]
        cluster = struct.unpack_from("<H", entry, 26)[0] | (struct.unpack_from("<H", entry, 20)[0] << 16)
        size = struct.unpack_from("<I", entry, 28)[0]
        if sfn in (b".          ", b"..         "):
            if pending:
                raise SystemExit("dot entry has a long name")
            name = "." if sfn == b".          " else ".."
            found.append({"name": name, "sfn": sfn, "cluster": cluster, "size": size, "attr": entry[11], "dot": True})
            continue
        if not pending:
            raise SystemExit("short name without a long name")
        name = lfn_name(pending, sfn)
        pending = []
        found.append({"name": name, "sfn": sfn, "cluster": cluster, "size": size, "attr": entry[11], "dot": False, "data": b""})
    if pending:
        raise SystemExit("long name without a file")
    return found

with stick.open("rb") as handle:
    handle.seek(4096 * 512)
    boot = handle.read(512)
    if boot[510:512] != b"\x55\xaa" or boot[82:90] != b"FAT32   " or boot[13] != 8:
        raise SystemExit("FAT32 boot sector")
    reserved, fats = struct.unpack_from("<HB", boot, 14)
    fat_sectors = struct.unpack_from("<I", boot, 36)[0]
    root_cluster = struct.unpack_from("<I", boot, 44)[0]
    if reserved != 32 or fats != 2 or fat_sectors != 1024 or root_cluster != 2:
        raise SystemExit("FAT32 geometry")
    handle.seek((4096 + 6) * 512)
    if handle.read(512) != boot:
        raise SystemExit("FAT32 backup boot sector")
    handle.seek((4096 + 1) * 512)
    info = handle.read(512)
    if info[:4] != b"RRaA" or info[484:488] != b"rrAa":
        raise SystemExit("FSInfo")
    handle.seek((4096 + 32) * 512)
    fat = handle.read(1024 * 512)
    if handle.read(1024 * 512) != fat:
        raise SystemExit("FAT copies differ")
    fat_entries = [struct.unpack_from("<I", fat, i)[0] for i in range(0, len(fat), 4)]

    def cluster_bytes(cluster):
        handle.seek((4096 + 2080 + (cluster - 2) * 8) * 512)
        return handle.read(4096)

    def file_bytes(item):
        blob = bytearray()
        current = item["cluster"]
        left = item["size"]
        seen = set()
        while left:
            if current in seen or current < 2:
                raise SystemExit("cluster loop in %s" % item["name"])
            seen.add(current)
            take = min(left, 4096)
            handle.seek((4096 + 2080 + (current - 2) * 8) * 512)
            blob += handle.read(take)
            left -= take
            current = fat_entries[current] & 0x0FFFFFFF
            if left and current >= 0x0FFFFFF8:
                raise SystemExit("chain ended early for %s" % item["name"])
        if current < 0x0FFFFFF8:
            raise SystemExit("chain too long for %s" % item["name"])
        return bytes(blob)

    def directory(cluster):
        return parse_blob(cluster_bytes(cluster))

    root_items = directory(2)
    if any(item["dot"] for item in root_items):
        raise SystemExit("root has a dot entry")
    root_names = {item["name"]: item for item in root_items}
    if set(root_names) != {"vmlinuz-linux", "initramfs-linux.img", "loader"}:
        raise SystemExit("FAT root %s" % sorted(root_names))
    loader = root_names["loader"]
    if loader["attr"] & 0x10 == 0 or loader["size"] != 0:
        raise SystemExit("loader is not a directory")
    loader_items = directory(loader["cluster"])
    dots = [item["name"] for item in loader_items if item["dot"]]
    if dots != [".", ".."]:
        raise SystemExit("loader dots %s" % dots)
    if loader_items[0]["sfn"] != b".          " or loader_items[1]["sfn"] != b"..         ":
        raise SystemExit("loader dot short names")
    entries = {item["name"]: item for item in loader_items if not item["dot"]}
    if set(entries) != {"entries"}:
        raise SystemExit("loader children %s" % sorted(entries))
    entry_items = directory(entries["entries"]["cluster"])
    if [item["name"] for item in entry_items if item["dot"]] != [".", ".."]:
        raise SystemExit("entries dots")
    confs = {item["name"]: item for item in entry_items if not item["dot"]}
    expect_confs = {"gbos-install.conf", "gbos-update.conf", "gbos-nvme.conf", "gbos-nvme-debug.conf"}
    if set(confs) != expect_confs or "maintenance.conf" in confs:
        raise SystemExit("entries %s" % sorted(confs))
    sfn = {
        "vmlinuz-linux": b"VMLINU~1   ",
        "initramfs-linux.img": b"INITRA~1IMG",
        "loader": b"LOADER     ",
        "entries": b"ENTRIES    ",
        "gbos-install.conf": b"GBOS-I~1CON",
        "gbos-update.conf": b"GBOS-U~1CON",
        "gbos-nvme.conf": b"GBOS-N~1CON",
        "gbos-nvme-debug.conf": b"GBOS-N~2CON",
    }
    found_sfn = {
        "vmlinuz-linux": root_names["vmlinuz-linux"]["sfn"],
        "initramfs-linux.img": root_names["initramfs-linux.img"]["sfn"],
        "loader": loader["sfn"],
        "entries": entries["entries"]["sfn"],
    }
    found_sfn.update({name: item["sfn"] for name, item in confs.items()})
    if found_sfn != sfn:
        raise SystemExit("short names %s" % found_sfn)
    if file_bytes(root_names["vmlinuz-linux"]) != (t / "xb/vmlinuz-linux").read_bytes():
        raise SystemExit("vmlinuz bytes")
    if file_bytes(root_names["initramfs-linux.img"]) != (t / "xb/initramfs-linux.img").read_bytes():
        raise SystemExit("initrd bytes")
    texts = {name: file_bytes(item).decode() for name, item in confs.items()}
    partuuid = hp[33]["guid"]
    if any("title Maintenance" in body or name == "maintenance.conf" for name, body in texts.items()):
        raise SystemExit("plain maintenance entry")
    protections = ("lockdown=integrity", "efi=noruntime", "module_blacklist=", "initcall_blacklist=")
    for name in ("gbos-install.conf", "gbos-update.conf"):
        body = texts[name]
        if "linux /vmlinuz-linux" not in body or any(token not in body for token in protections):
            raise SystemExit("firmware protection missing from %s" % name)
    if "root=PARTUUID=%s" % partuuid not in texts["gbos-install.conf"]:
        raise SystemExit("install entry partuuid")
    if "gbos.install=1" not in texts["gbos-install.conf"] or "gbos.update=1" in texts["gbos-install.conf"]:
        raise SystemExit("install entry")
    if "gbos.update=1" not in texts["gbos-update.conf"]:
        raise SystemExit("update entry")
    if "linux /gbos-nvme/kernel.efi" not in texts["gbos-nvme.conf"]:
        raise SystemExit("internal entry")
    if "console=ttyS0" not in texts["gbos-nvme-debug.conf"]:
        raise SystemExit("debug entry")
digest = hashlib.sha256()
with stick.open("rb") as handle:
    while True:
        chunk = handle.read(8 << 20)
        if not chunk:
            break
        digest.update(chunk)
sha_text = (t / "stick.img.sha256").read_text()
if sha_text != "%s  stick.img\n" % digest.hexdigest():
    raise SystemExit("sha256 file")
print("stick bytes ok")
PY
); then
    ok "stick image keeps Googlebook, the loader entries and the ext4 payload"
else
    bad "stick image bytes do not match: $(printf '%s\n' "$py" | tail -n 4)"
fi
if printf '%s\n' "$py" | grep -qx "helper mode ok"; then
    ok "a mode-4110 helper is copied and the saved mode is restored"
else
    bad "mode-4110 helper: $(printf '%s\n' "$py" | tail -n 6)"
fi
{
    printf '%s\n' "$stick_line"
    printf '%s\n' "$reason_line"
} > "$T/marker-want"
if [ -f "$T/stick.img.not-self-tested" ] && [ ! -L "$T/stick.img.not-self-tested" ] \
        && cmp -s "$T/marker-want" "$T/stick.img.not-self-tested" \
        && printf '%s\n' "$reason_line" | cmp -s - "$T/full.img.not-self-tested"; then
    ok "a source .not-self-tested is copied onto the assembled stick"
else
    bad "assembled untested marker: $(python3 -c 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); print(p.read_bytes() if p.is_file() else "missing")' "$T/stick.img.not-self-tested" 2>/dev/null || echo missing)"
fi
if python3 - "$ROOT/install/write-usb.sh" "$T/untested-gate.sh" << 'PY'
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
start = text.index("UNTESTED=0\n")
end = text.index("# between the prompt and the write")
snippet = text[start:end]
need = (
    '[ -e "$IMG.not-self-tested" ]',
    "Type untested to write this image anyway:",
    '[ "$ANSWER" = untested ]',
    "not acknowledged, nothing written",
    "Type its serial",
)
for item in need:
    if item not in snippet:
        raise SystemExit("missing " + item)
pathlib.Path(sys.argv[2]).write_text(snippet)
PY
then
    gate_out=$(IMG="$T/stick.img" CHECK_ONLY=0 KEEP=0 I_NODE="disk" I_SERIAL="ABC123" \
        bash "$T/untested-gate.sh" <<< "nope" 2>&1); gate_rc=$? || true
    # read -p prints its prompt only on a terminal. A wrong word still has to
    # hit the untested check, which exits before the serial check.
    if [ "$gate_rc" = 1 ] \
            && printf '%s\n' "$gate_out" | grep -F -q "not acknowledged, nothing written" \
            && printf '%s\n' "$gate_out" | grep -F -q "WARNING:" \
            && ! printf '%s\n' "$gate_out" | grep -F -q "serial not confirmed"; then
        ok "write-usb.sh still requires untested when the assembled marker exists"
    else
        bad "untested confirmation (rc=$gate_rc): $(printf '%s\n' "$gate_out" | tail -n 6)"
    fi
    gate_out=$(IMG="$T/no-such-marker.img" CHECK_ONLY=0 KEEP=0 I_NODE="disk" I_SERIAL="ABC123" \
        bash "$T/untested-gate.sh" <<< "nope" 2>&1); gate_rc=$? || true
    if [ "$gate_rc" = 1 ] \
            && printf '%s\n' "$gate_out" | grep -F -q "serial not confirmed, nothing written" \
            && ! printf '%s\n' "$gate_out" | grep -F -q "not acknowledged, nothing written" \
            && ! printf '%s\n' "$gate_out" | grep -F -q "WARNING:"; then
        ok "write-usb.sh skips the untested confirmation when the marker is absent"
    else
        bad "marker-absent confirmation (rc=$gate_rc): $(printf '%s\n' "$gate_out" | tail -n 6)"
    fi
else
    bad "could not extract the write-usb untested gate"
fi
out=$(bash "$STICK" --image "$T/dense.img" --out "$T/dense-stick.img" --xbootldr "$T/xb-dense" --root "$T/root-dense" --pristine "$T/pristine-dense" 2>"$T/dense.err"); rc=$? || true
if [ "$rc" = 0 ] && [ ! -s "$T/dense.err" ] && dense=$(python3 - "$T" "$ROOT" 2>&1 << 'PY'
import sys
sys.path.insert(0, sys.argv[2] + "/tests/stick")
import gptlib
from pathlib import Path
t = Path(sys.argv[1])
_h, hp = gptlib.read_gpt(str(t / "dense-stick.img"))
want = list(range(1, 11)) + list(range(12, 32)) + [32, 33]
if sorted(hp) != want or 11 in hp:
    raise SystemExit("partition numbers %s" % sorted(hp))
if hp[32]["name"] != "XBOOTLDR" or hp[33]["name"] != "kano-maint":
    raise SystemExit("maintenance names")
if hp[1]["name"] != "p1" or hp[1]["first"] != 100 or hp[31]["name"] != "p31":
    raise SystemExit("googlebook numbers moved")
print("dense ok")
PY
) && [ "$dense" = "dense ok" ] && dense_sha=$(printf '%s\n' "$out" | awk '/^sha256 [0-9a-f]{64}$/ { print $2; exit }') \
        && [ -n "$dense_sha" ] && [ "$(cat "$T/dense-stick.img.sha256")" = "$dense_sha  dense-stick.img" ]; then
    ok "a Googlebook numbering keeps hole 11 and adds partitions 32 and 33"
else
    bad "dense numbering (rc=$rc): ${dense:-$(tail -n 8 "$T/dense.err")}"
fi
if [ ! -e "$T/dense.img.not-self-tested" ] \
        && printf '%s\n' "$stick_line" | cmp -s - "$T/dense-stick.img.not-self-tested"; then
    ok "a source without .not-self-tested still marks the assembled stick"
else
    bad "dense untested marker"
fi
before=$(python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$T/stick.img")
expect "installer stick refuses to overwrite" 2 "output already exists" -- bash "$STICK" --image "$T/full.img" --out "$T/stick.img" --xbootldr "$T/xb" --root "$T/root" --pristine "$T/pristine"
after=$(python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$T/stick.img")
if [ "$before" = "$after" ]; then ok "a second run leaves the image in place"; else bad "a second run changed the image"; fi
out=$(bash "$STICK" --image "$T/full.img" --out "$T/stick-big.img" --xbootldr "$T/xb" --root "$T/root" --pristine "$T/pristine" --disk-sectors 17305634 2>"$T/stick-big.err"); rc=$? || true
big=$(python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$T/stick-big.img" 2>/dev/null || echo missing)
big_sha=$(printf '%s\n' "$out" | awk '/^sha256 [0-9a-f]{64}$/ { print $2; exit }')
if [ "$rc" = 0 ] && [ "$big" = "$((17305634 * 512))" ] && printf '%s\n' "$out" | grep -qx "disk_sectors 17305634" \
        && [ -n "$big_sha" ] && [ "$(cat "$T/stick-big.img.sha256")" = "$big_sha  stick-big.img" ]; then
    ok "a larger disk keeps the partitions and moves the backup GPT"
else
    bad "larger disk (rc=$rc size=$big): $(tail -n 8 "$T/stick-big.err")"
fi
if grep -F -q 'GBOS_WUSB_TEST_SYSFS is a test hook and is honoured only with --check-only' "$ROOT/install/write-usb.sh" \
        && grep -F -q 'run as root (it writes a block device)' "$ROOT/install/write-usb.sh"; then
    ok "write-usb.sh still has the test-hook and root gates"
else
    bad "write-usb.sh lost a guard"
fi
if grep -n 'gptlib' "$ROOT/install/stick/maint-stick.py" "$ROOT/install/stick/stick-payload.py" \
        "$ROOT/install/stick/stick-ext4.py" "$ROOT/install/stick/esp-fat.py"; then
    bad "maint-stick.py or stick-payload.py imports the test GPT helper"
else
    ok "maint-stick.py does not import the test GPT helper"
fi

payload_rc=0
payload_out=$(python3 -I "$ROOT/tests/stick/payload-test.py" "$T/payload-work" "$ROOT" 2>&1) || payload_rc=$?
payload_fail=0
while IFS= read -r line; do
    case "$line" in
        PASS\ *) ok "${line#PASS }" ;;
        FAIL\ *) bad "${line#FAIL }"; payload_fail=1 ;;
        "") ;;
        *) printf '%s\n' "$line" ;;
    esac
done << EOF
$payload_out
EOF
if ! printf '%s\n' "$payload_out" | grep -q '^PASS '; then
    bad "payload test reported no PASS line"
fi
if [ "$payload_rc" != 0 ] && [ "$payload_fail" = 0 ]; then
    bad "payload test exited $payload_rc"
fi

menu_rc=0
menu_out=$(python3 -I "$ROOT/tests/stick/qemu-install.py" --replay-produced-menu 2>&1) || menu_rc=$?
menu_fail=0
while IFS= read -r line; do
    case "$line" in
        PASS\ *) ok "${line#PASS }" ;;
        FAIL\ *) bad "${line#FAIL }"; menu_fail=1 ;;
        "") ;;
        *) printf '%s\n' "$line" ;;
    esac
done << EOF
$menu_out
EOF
if ! printf '%s\n' "$menu_out" | grep -q '^PASS '; then
    bad "produced menu replay reported no PASS line"
fi
if [ "$menu_rc" != 0 ] && [ "$menu_fail" = 0 ]; then
    bad "produced menu replay exited $menu_rc"
fi

if grep -nE '(^|[[:space:]])(sudo|curl)([[:space:]]|$)' \
        "$MM" \
        "$STICK" \
        "$ROOT/install/stick/maint-layout.py" \
        "$ROOT/install/stick/maint-pins.py" \
        "$ROOT/install/stick/maint-assemble.py" \
        "$ROOT/install/stick/maint-fetch.py" \
        "$ROOT/install/stick/maint-pin-arch.py" \
        "$ROOT/install/stick/maint-stick.py" \
        "$ROOT/install/stick/stick-payload.py" \
        "$ROOT/install/stick/stick-ext4.py" \
        "$ROOT/install/stick/esp-fat.py"; then
    bad "maintenance scripts call sudo or curl"
else
    ok "maintenance scripts do not call sudo or curl"
fi
if [ -s "$T/curl.log" ]; then bad "a command invoked curl"; else ok "no command invoked curl"; fi

pin_rc=0
pin_out=$(python3 -I "$ROOT/tests/stick/pin-db-test.py" 2>&1) || pin_rc=$?
if [ "$pin_rc" = 0 ] && printf '%s\n' "$pin_out" | grep -qx 'pin-db: clean'; then
    ok "pin tool checks a pre-pinned database digest before parsing"
else
    bad "pin tool digest check failed"
    printf '%s\n' "$pin_out"
fi

docs_rc=0
docs_out=$(python3 -I "$ROOT/tests/stick/docs-story-test.py" 2>&1) || docs_rc=$?
if [ "$docs_rc" = 0 ] && printf '%s\n' "$docs_out" | grep -qx 'docs-story: clean'; then
    ok "docs name one user sequence and the pinned kernel"
else
    bad "docs story check failed"
    printf '%s\n' "$docs_out"
fi

gate_rc=0
gate_out=$(python3 -I "$ROOT/tests/stick/nvme-confirm-gate.py" 2>&1) || gate_rc=$?
if [ "$gate_rc" = 0 ] && printf '%s\n' "$gate_out" | grep -qx 'nvme-confirm-gate: clean'; then
    ok "nvme confirm gate: no write, mount, or discard before the serial matches"
else
    bad "nvme confirm gate failed"
    printf '%s\n' "$gate_out"
fi

asis_rc=0
asis_out=$(python3 -I "$ROOT/tests/stick/asis-gate.py" 2>&1) || asis_rc=$?
if [ "$asis_rc" = 0 ] && printf '%s\n' "$asis_out" | grep -qx 'asis-gate: clean'; then
    ok "asis gate: boot copy is hashed itself and the as-is path does not rewrite the menu"
else
    bad "asis gate failed"
    printf '%s\n' "$asis_out"
fi

minors_rc=0
minors_out=$(python3 -I "$ROOT/tests/stick/minors-gate.py" 2>&1) || minors_rc=$?
if [ "$minors_rc" = 0 ] && printf '%s\n' "$minors_out" | grep -qx 'minors-gate: clean'; then
    ok "minors gate: initramfs record, marker warning, mtools guard, pin line checks"
else
    bad "minors gate failed"
    printf '%s\n' "$minors_out"
fi

echo "make-maint tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
