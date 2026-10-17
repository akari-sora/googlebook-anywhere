#!/usr/bin/env python3
"""Offline checks for the cheap round-2 minors.

No network, no assemble, no QEMU. Each fix is undone in a temp copy and the
same check must fail on that copy. Items that are not cheap stay out of this
file; the iteration note records those reasons.
"""
import importlib.util
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STICK = REPO / "install/stick/README.md"
INTERNAL = REPO / "docs/INSTALL-INTERNAL.md"
WRITER = REPO / "install/write-usb.sh"
SELFTEST = REPO / "tests/update/selftest.py"
PIN_TOOL = REPO / "install/stick/maint-pin-arch.py"
PUBLISHED = "29790865"
STALE = "29793540"
UNTESTED_SENTENCE = (
    "映像旁會有 `.not-self-tested`，因為 `make-installer-stick.sh` 沒有把這支碟開機。"
    "`write-usb.sh` 印出那個檔的第一行，下一個提示要輸入 `untested` 才寫入。"
    "這只是多一次確認。"
)
MARKER_ECHO = 'printf \'%s\\n\' "         marker: $REASON"'
MTOOLS_BLOCK = """MISSING_MTOOLS = [
    name for name in ("mformat", "mmd", "mcopy", "mdir", "mdel", "minfo", "mtype")
    if shutil.which(name) is None
]
if MISSING_MTOOLS:
    sys.exit(
        "needs mtools (package mtools: mformat, mmd, mcopy, mdir, mdel, minfo, mtype); missing: "
        + ", ".join(MISSING_MTOOLS)
    )
"""
CRLF_CHECK = (
    '    if "\\r" in text:\n'
    '        refuse(f"{path}: pin list must be LF line endings")\n'
)
DUP_CHECK = (
    "        if key in pins:\n"
    '            refuse(f"{where}: duplicate database {repo}.db {date}")\n'
)
PIN_LINE = (
    "ab" * 32
    + " 1 core.db https://archive.archlinux.org/repos/2026/10/01/core/os/x86_64/core.db\n"
)


def fail(message):
    print(f"minors-gate: {message}", file=sys.stderr)
    raise SystemExit(1)


def read(path):
    if not path.is_file():
        fail(f"missing {path.relative_to(REPO)}")
    return path.read_text(encoding="utf-8")


def require_initramfs(stick, internal):
    for label, text in (("install/stick/README.md", stick), ("docs/INSTALL-INTERNAL.md", internal)):
        if STALE in text or PUBLISHED not in text or "那次 assemble 印出的" not in text:
            raise SystemExit(f"{label} does not record the published initramfs size")


def require_untested_sentence(stick):
    if UNTESTED_SENTENCE not in stick:
        raise SystemExit("stick readme lost the untested confirmation")


def require_marker_warning(text):
    if "built with --no-selftest" in text:
        raise SystemExit("write-usb still says the marker was built with --no-selftest")
    if MARKER_ECHO not in text:
        raise SystemExit("write-usb does not print the marker's first line")
    if "Type untested to write this image anyway:" not in text:
        raise SystemExit("the untested prompt was removed")
    if '[ "$ANSWER" = untested ]' not in text:
        raise SystemExit("the untested answer check was removed")


def require_mtools_guard(text):
    guard = text.find(MTOOLS_BLOCK)
    call = text.find('sh("mformat"')
    if guard < 0 or call < 0 or guard > call:
        raise SystemExit("selftest does not refuse missing mtools before mformat")


def must_reject(label, fn):
    try:
        fn()
    except SystemExit:
        return
    fail(f"mutant still passed: {label}")


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pins_rejected(module, raw, needle):
    directory = Path(tempfile.mkdtemp(prefix="gbos-minors-pins-"))
    path = directory / "pins.txt"
    err = io.StringIO()
    old = sys.stderr
    sys.stderr = err
    try:
        path.write_bytes(raw)
        try:
            module.read_db_pins(str(path))
        except SystemExit as exc:
            return exc.code == 2 and needle in err.getvalue()
        return False
    finally:
        sys.stderr = old
        shutil.rmtree(directory)


def check_pin_lines():
    live = load_module(PIN_TOOL, "maint_pin_arch_live")
    crlf = PIN_LINE.replace("\n", "\r\n").encode("utf-8")
    duplicate = (PIN_LINE + PIN_LINE).encode("utf-8")
    if not pins_rejected(live, crlf, "LF line endings"):
        fail("a CRLF pin list was accepted")
    if not pins_rejected(live, duplicate, "duplicate database"):
        fail("a duplicate database pin was accepted")
    source = read(PIN_TOOL)
    if CRLF_CHECK not in source or DUP_CHECK not in source:
        fail("pin line checks moved; the mutant would not undo them")
    mutant_dir = Path(tempfile.mkdtemp(prefix="gbos-minors-pin-"))
    mutant_path = mutant_dir / "maint-pin-arch.py"
    try:
        mutant_path.write_text(source.replace(CRLF_CHECK, "").replace(DUP_CHECK, ""), encoding="utf-8")
        mutant = load_module(mutant_path, "maint_pin_arch_mutant")
        if pins_rejected(mutant, crlf, "LF line endings"):
            fail("mutant still rejected a CRLF pin list")
        if pins_rejected(mutant, duplicate, "duplicate database"):
            fail("mutant still rejected a duplicate database pin")
    finally:
        shutil.rmtree(mutant_dir)


def check_mtools_process():
    if shutil.which("mformat") is not None:
        return
    work = Path(tempfile.mkdtemp(prefix="gbos-minors-mtools-"))
    try:
        live = subprocess.run(
            [sys.executable, "-I", str(SELFTEST), str(work)],
            capture_output=True,
            text=True,
        )
        joined = live.stdout + live.stderr
        if live.returncode == 0 or "missing:" not in joined or "mformat" not in joined or "Traceback" in joined:
            fail("missing mtools did not stop selftest before mformat")
        mutant = REPO / "tests/update/selftest-mtools-mutant.py"
        try:
            mutant.write_text(read(SELFTEST).replace(MTOOLS_BLOCK, ""), encoding="utf-8")
            broken = subprocess.run(
                [sys.executable, "-I", str(mutant), str(work / "mutant")],
                capture_output=True,
                text=True,
            )
            broken_out = broken.stdout + broken.stderr
            if "FileNotFoundError" not in broken_out or "mformat" not in broken_out:
                fail("mtools mutant did not reach mformat")
        finally:
            mutant.unlink(missing_ok=True)
    finally:
        shutil.rmtree(work)


def main():
    stick = read(STICK)
    internal = read(INTERNAL)
    writer = read(WRITER)
    selftest = read(SELFTEST)
    require_initramfs(stick, internal)
    require_untested_sentence(stick)
    require_marker_warning(writer)
    require_mtools_guard(selftest)
    must_reject(
        "stale initramfs size",
        lambda: require_initramfs(stick.replace(PUBLISHED, STALE), internal),
    )
    must_reject(
        "deleted untested sentence",
        lambda: require_untested_sentence(stick.replace(UNTESTED_SENTENCE, "")),
    )
    must_reject(
        "hardcoded selftest flag",
        lambda: require_marker_warning(
            writer.replace(MARKER_ECHO, 'echo "WARNING: built with --no-selftest"')
        ),
    )
    must_reject("removed mtools guard", lambda: require_mtools_guard(selftest.replace(MTOOLS_BLOCK, "")))
    check_pin_lines()
    check_mtools_process()
    print("minors-gate: clean")


if __name__ == "__main__":
    main()
