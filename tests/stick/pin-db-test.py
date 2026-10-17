#!/usr/bin/env python3
"""maint-pin-arch.py checks a pre-pinned database digest before parsing.

No network. The committed maint-db.pins digests are the bytes recorded for
the 2026/10/01 archive databases.
"""
import gzip
import hashlib
import importlib.util
import io
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "install/stick/maint-pin-arch.py"
PINS = REPO / "install/stick/maint-db.pins"
DATE = "2026/10/01"
CORE_SHA = "48984a2b5a339954dc1a7c21bce7ff651b6b0c7747d0545c36ca454bee005a73"
CORE_SIZE = 129871
EXTRA_SHA = "b2c0d01420d9bc1da17589c622fbf12c49af15d60e54f46cad8f6c307b5497ba"
EXTRA_SIZE = 8875065
CORE_URL = (
    "https://archive.archlinux.org/repos/2026/10/01/core/os/x86_64/core.db"
)


def fail(message):
    print(f"pin-db: {message}", file=sys.stderr)
    sys.exit(1)


def load_mod():
    spec = importlib.util.spec_from_file_location("maint_pin_arch_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sample_db():
    text = (
        "%FILENAME%\nexample-1-any.pkg.tar.zst\n\n"
        "%NAME%\nexample\n\n"
        "%SHA256SUM%\n" + ("ab" * 32) + "\n\n"
        "%CSIZE%\n12\n\n"
        "%DEPENDS%\n\n"
    )
    data = text.encode("utf-8")
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w") as tar:
        info = tarfile.TarInfo("example-1-any/desc")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return gzip.compress(bio.getvalue(), mtime=0)


def capture(mod, fn):
    err = io.StringIO()
    old = sys.stderr
    sys.stderr = err
    try:
        try:
            fn()
        except SystemExit as exc:
            return exc.code, err.getvalue()
        return None, err.getvalue()
    finally:
        sys.stderr = old


def main():
    if not TOOL.is_file() or not PINS.is_file():
        fail("missing pin tool or maint-db.pins")
    doc = TOOL.read_text(encoding="utf-8")
    if "Maintainer-only" not in doc.split('"""', 2)[1]:
        fail("pin tool is not documented as maintainer-only")
    readme = (REPO / "install/stick/README.md").read_text(encoding="utf-8")
    if "maint-pin-arch.py" not in readme or "只給維護者" not in readme:
        fail("README does not say the pin tool is maintainer-only")
    if "maint-db.pins" not in readme or "maint.pins" not in readme:
        fail("README does not name the database pins and maint.pins")
    for rel in ("install/stick/make-maint.sh", "install/make-installer-stick.sh"):
        text = (REPO / rel).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("#")
        )
        if "maint-pin-arch" in code or "core.db" in code or "extra.db" in code:
            fail(f"{rel} downloads or calls the database pin tool")
    maint = (REPO / "install/stick/make-maint.sh").read_text(encoding="utf-8")
    if "maint-fetch.py" not in maint or "maint.pins" not in maint:
        fail("fetch no longer uses maint.pins")

    mod = load_mod()
    pins = mod.read_db_pins(str(PINS))
    core = pins.get((DATE, "core"))
    extra = pins.get((DATE, "extra"))
    if core != {"sha": CORE_SHA, "size": CORE_SIZE, "name": "core.db", "url": CORE_URL}:
        fail("committed core.db digest is not the recorded pin")
    extra_url = CORE_URL.replace("/core/", "/extra/").replace("core.db", "extra.db")
    if extra != {"sha": EXTRA_SHA, "size": EXTRA_SIZE, "name": "extra.db", "url": extra_url}:
        fail("committed extra.db digest is not the recorded pin")

    blob = sample_db()
    good = {
        "sha": hashlib.sha256(blob).hexdigest(),
        "size": len(blob),
        "name": "core.db",
        "url": CORE_URL,
    }
    downloaded = []

    def fake_get(url):
        downloaded.append(url)
        return blob

    mod.get_bytes = fake_get
    by_name = {}
    mod.load_repo(DATE, "core", by_name, {}, {(DATE, "core"): good})
    if downloaded != [CORE_URL] or "example" not in by_name:
        fail("a matching digest did not parse the fixture database")

    opened = {"n": 0}
    real_open = mod.open_db

    def spy_open(raw):
        opened["n"] += 1
        return real_open(raw)

    mod.open_db = spy_open
    bad = dict(good)
    bad["sha"] = "0" * 64
    downloaded.clear()

    def run_bad():
        mod.load_repo(DATE, "core", {}, {}, {(DATE, "core"): bad})

    code, err = capture(mod, run_bad)
    if code != 2 or "pre-pinned digest" not in err:
        fail("a wrong digest was accepted")
    if opened["n"] != 0:
        fail("parsed the database before the digest matched")

    downloaded.clear()

    def run_missing():
        mod.load_repo(DATE, "extra", {}, {}, {})

    code, err = capture(mod, run_missing)
    if code != 2 or "no pre-pinned digest" not in err or downloaded:
        fail("a missing pin still downloaded a database")

    one = f"{'ab' * 32} 1 core.db {CORE_URL}\n"
    code, err = capture(mod, lambda: read_pins(mod, one.replace("\n", "\r\n")))
    if code != 2 or "LF line endings" not in err:
        fail("a CRLF pin list was accepted")
    code, err = capture(mod, lambda: read_pins(mod, one + one))
    if code != 2 or "duplicate database" not in err:
        fail("a duplicate database pin was accepted")

    print("pin-db: clean")


def read_pins(mod, text):
    directory = Path(tempfile.mkdtemp(prefix="gbos-pins-"))
    path = directory / "pins.txt"
    try:
        path.write_bytes(text.encode("utf-8"))
        mod.read_db_pins(str(path))
    finally:
        path.unlink(missing_ok=True)
        directory.rmdir()


if __name__ == "__main__":
    main()
