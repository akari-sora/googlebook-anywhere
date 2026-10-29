#!/usr/bin/env python3
"""Fail-closed edited-file build selection and vbmeta rollback pin checks."""
import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import time
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("gbos_base", ROOT / "base/gbos_base.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
passed = 0

def expect(label, fn):
    global passed
    fn()
    passed += 1
    print("PASS", label)

def require(value):
    if not value:
        raise AssertionError("condition failed")


def refuses(fn):
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            fn()
        except SystemExit:
            return
    raise AssertionError("expected refusal")

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "build.prop"
    p.write_text("old-build-edited\n")
    old_sha = hashlib.sha256(p.read_bytes()).hexdigest()
    new = b"new-build-edited\n"
    new_sha = hashlib.sha256(new).hexdigest()
    g.EDITED_BUILD_PROP_SHA["16452207"] = old_sha
    g.EDITED_BUILD_PROP_SHA["16658739"] = new_sha
    os.environ.pop("GBOS_LAPIS_BUILD", None)
    expect("default build selects old pin", lambda: g.check_edited(td, "build.prop"))
    os.environ["GBOS_LAPIS_BUILD"] = "16658739"
    expect("new build rejects old edited file", lambda: refuses(lambda: g.check_edited(td, "build.prop")))
    p.write_bytes(new)
    expect("new build accepts its exact edited file", lambda: g.check_edited(td, "build.prop"))
    p.write_bytes(new + b"x")
    expect("new build rejects changed edited file", lambda: refuses(lambda: g.check_edited(td, "build.prop")))
    os.environ["GBOS_LAPIS_BUILD"] = "99999999"
    expect("unknown build fails closed", lambda: refuses(lambda: g.check_edited(td, "build.prop")))

    img = Path(td) / "vbmeta.img"
    b = bytearray(256)
    b[:4] = b"AVB0"
    struct.pack_into(">Q", b, 112, 1369)
    img.write_bytes(b)
    helper = ROOT / "base/gbos_base.py"
    def rollback(expected, image=img):
        return subprocess.run([sys.executable, "-I", "-B", str(helper),
                               "check-vbmeta-rollback", str(image), expected],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode
    expect("stock rollback index matches", lambda: require(rollback("1369") == 0))
    expect("lower rollback index rejected", lambda: require(rollback("1339") != 0))
    expect("malformed index rejected", lambda: require(rollback("x") != 0))
    img.write_bytes(b"AVB0")
    expect("truncated vbmeta rejected", lambda: require(rollback("1369") != 0))
    img.write_bytes(b"BAD!" + bytes(252))
    expect("bad vbmeta magic rejected", lambda: require(rollback("1369") != 0))
# Exercise the real final-hash stage on a small file: a strict build must not
# report success when the final reference-key image differs from its pin.
source = (ROOT / "base/build-base.sh").read_text()
checker = source.split("check() {", 1)[1].split("# ---------------------------------------------------------------- host tools", 1)[0]
checker = "check() {" + checker
final = source.split('log "step 11: final hashes"', 1)[1].split("INITRD_SHA=", 1)[0]
with tempfile.TemporaryDirectory() as td:
    image = Path(td) / "image.img"
    image.write_bytes(b"final-image-fixture")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    def final_stage(pin, drift="0", refkey="1"):
        script = ("set -euo pipefail\n"
                  "log() { echo \"$*\"; }\n"
                  "warn() { echo \"WARNING: $*\"; }\n"
                  "die() { echo \"ERROR: $*\"; exit 1; }\n"
                  "declare -A EXPECT=() EXPECT_REFKEY=()\n"
                  'IMG=$1; EXPECT_REFKEY[kano-real.img]=$2; ALLOW_DRIFT=$3; REFKEY=$4; LAPIS_BUILD=16658739\n'
                  + checker + final)
        return subprocess.run(["bash", "-c", script, "final-stage", str(image), pin, drift, refkey],
                              capture_output=True, text=True)
    expect("strict final image accepts exact pin", lambda: require(final_stage(digest).returncode == 0))
    expect("strict final image rejects wrong pin", lambda: require(final_stage("0" * 64).returncode != 0))
    result = final_stage("0" * 64, "1")
    expect("discovery final image records measured mismatch", lambda: require(result.returncode == 0 and digest in result.stdout and "WARNING:" in result.stdout))
    expect("other signing key skips reference image pin", lambda: require(final_stage("0" * 64, refkey="0").returncode == 0))
# The actual ESP formatting stage must not leak the wall clock into its volume
# label. Some mkfs.fat 4.2 builds ignore SOURCE_DATE_EPOCH for that directory entry.
if shutil.which("mkfs.fat"):
    format_stage = source.split('ESPIMG="$W/esp.img"', 1)[1].split('export SOURCE_DATE_EPOCH=$ESP_EPOCH_E', 1)[0]
    with tempfile.TemporaryDirectory() as td:
        def formatted(name):
            work = Path(td) / name
            work.mkdir()
            script = ('set -euo pipefail\nW=$1; ESPIMG=$W/esp.img; ESP_SIZE=67108864; LAPIS_BUILD=16658739; '
                      'ESP_EPOCH_E_MKFS=1791694048; ESP_LABEL=ESP; ESP_SERIAL=461813AC\n' + format_stage)
            subprocess.run(["bash", "-c", script, "format-stage", str(work)], check=True, capture_output=True)
            return work / "esp.img"
        first = formatted("first")
        time.sleep(3)
        second = formatted("second")
        with first.open("rb") as f:
            boot = f.read(512)
        expect("new ESP keeps pinned volume serial", lambda: require(struct.unpack_from("<I", boot, 67)[0] == 0x461813AC))
        expect("new ESP format is independent of wall clock", lambda: require(first.read_bytes() == second.read_bytes()))
else:
    print("SKIP real ESP reproducibility fixture (mkfs.fat unavailable)")
print(f"lapis base pins tests: {passed} passed, 0 failed")
