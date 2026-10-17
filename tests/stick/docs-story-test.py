#!/usr/bin/env python3
"""Offline check that the installer-stick docs name one build sequence.

No network, no assemble, no write-usb. A restored claim that the repo
commands cannot build the stick fails this check.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STICK = REPO / "install/stick/README.md"
ROOT_README = REPO / "README.md"
INITCALLS = REPO / "install/stick/builtin-initcalls.txt"
RESULT = REPO / "tests/stick/RESULT.md"
PINS = REPO / "install/stick/maint.pins"
FIXTURE = REPO / "tests/stick/make-maint-test.sh"
INSTALL_INTERNAL = REPO / "docs/INSTALL-INTERNAL.md"
RELEASE = REPO / "docs/RELEASE-NOTES.md"

CANNOT = "它們做不到那一步"
SEQUENCE = [
    'mkdir -p "$HOME/gbos-maint/cache"',
    'install/stick/make-maint.sh fetch --cache "$HOME/gbos-maint/cache"',
    'install/stick/make-maint.sh assemble --cache "$HOME/gbos-maint/cache" --out "$HOME/gbos-maint/maint"',
    'install/stick/make-pristine.py ~/gbos-work/build/image/googlebook-kano.img "$HOME/gbos-maint/pristine"',
    '--xbootldr "$HOME/gbos-maint/maint/xbootldr"',
    '--root "$HOME/gbos-maint/maint/root"',
    '--pristine "$HOME/gbos-maint/pristine"',
    'sudo install/write-usb.sh "$HOME/gbos-maint/stick.img" /dev/disk/by-id/usb-<你的隨身碟>',
]
MENU = [
    "Install Googlebook to the internal disk",
    "Update Googlebook on the internal disk (keep data)",
    "Googlebook (internal disk)",
    "Googlebook (internal disk) serial-debug",
    "沒有普通 Maintenance",
]


def fail(message):
    print(f"docs-story: {message}", file=sys.stderr)
    sys.exit(1)


def read(path):
    if not path.is_file():
        fail(f"missing {path.relative_to(REPO)}")
    return path.read_text(encoding="utf-8")


def main():
    stick = read(STICK)
    root = read(ROOT_README)
    if CANNOT in stick or CANNOT in root or CANNOT in read(INSTALL_INTERNAL) or CANNOT in read(RELEASE):
        fail("the stick readme says the repo commands cannot build the stick")
    for name, text in (("install/stick/README.md", stick), ("README.md", root)):
        for piece in SEQUENCE:
            if piece not in text:
                fail(f"{name} is missing the user sequence: {piece}")
        if "--xbootldr DIR" in text or "--root DIR" in text:
            fail(f"{name} still uses a placeholder directory")
        if "Linux x86_64" not in text or "unshare -U -r -m -p --fork" not in text:
            fail(f"{name} does not state the Linux user-namespace requirement")
        if "`pack` 不是這段的輸入" not in text:
            fail(f"{name} does not keep pack off the user sequence")
        if "不要加 `--keep-data`" not in text:
            fail(f"{name} does not refuse --keep-data")
        for piece in ("54546417", "27927765504", "28000000000"):
            if piece not in text:
                fail(f"{name} is missing the stick size {piece}")
        for title in MENU:
            if title not in text:
                fail(f"{name} menu text drifted: {title}")
    if "沒有 `maintenance.conf`" not in stick:
        fail("stick readme lost the maintenance.conf refusal")
    if "下面這一段可以照做。" not in stick:
        fail("stick readme lost the user-sequence lead")
    if stick.find("下面這一段可以照做。") > stick.find("### 已經有維護系統時"):
        fail("the developer stick notes moved ahead of the user sequence")
    if "提交的 `maint.pins` 組出來的維護核心是 `7.2.7-arch1-1`" not in stick:
        fail("current kernel text is not 7.2.7-arch1-1")
    if "目前的維護核心（Arch 7.2.3-arch1-3）" in stick:
        fail("current kernel text still names 7.2.3-arch1-3")
    if "2026-10-13" not in stick or "7.2.3-arch1-3" not in stick:
        fail("the 2026-10-13 kernel measurement was removed")
    if "沒有 `systemd.mask=systemd-firstboot.service`" not in stick:
        fail("recorded cmdline note lost the absent firstboot mask")
    if (
        "目前的安裝選項以 `gbos.install=1` 結尾，更新選項以 `gbos.install=1 gbos.update=1` 結尾"
        not in stick
        or "目前的安裝與更新選項仍以 `gbos.install=1` 結尾" in stick
    ):
        fail("install and update ending tokens are not both described")
    if "This tree has no separate v0.2.0 blob for fcb0e6a, so the menu check cannot prove byte equality with that commit." not in stick:
        fail("the docs claim a byte-for-byte v0.2.0 cmdline that this tree cannot show")
    if "先在 userdata 前 16 MiB（安裝程式清空的那段）與起點後 64 MiB 各植入非零資料" not in stick:
        fail("stick readme does not state that non-zero data is planted in the userdata head before (d)")
    for phrase in ("maint-pin-arch.py", "只給維護者", "maint-db.pins", "maint.pins"):
        if phrase not in stick:
            fail(f"pin-tool note lost {phrase}")
    first = read(INITCALLS).splitlines()[0]
    if "7.2.7-arch1-1" not in first or "7.2.3-arch1-3" in first:
        fail("builtin-initcalls comment does not name 7.2.7-arch1-1")
    if "mei_me_driver_init" not in read(INITCALLS):
        fail("builtin-initcalls data changed")
    result = read(RESULT)
    if "Arch 7.2.3-arch1-3" not in result:
        fail("RESULT.md rewrote the 2026-10-13 measurement")
    if "committed maint.pins uses kernel 7.2.7-arch1-1" not in result:
        fail("RESULT.md does not name the pinned kernel")
    if "2026-10-13 measurement" not in result:
        fail("RESULT.md does not label 7.2.3-arch1-3 as the 2026-10-13 measurement")
    if "USER-DATA-HEAD--" not in result or "USER-DATA-MARKER" not in result:
        fail("RESULT.md does not name both the userdata head and +64 MiB plants")
    if 'kver = "7.2.3-arch1-3"' not in read(FIXTURE):
        fail("synthetic fixture kernel was rewritten")
    if "linux-7.2.7.arch1-1-x86_64.pkg.tar.zst" not in read(PINS):
        fail("maint.pins no longer pins linux 7.2.7.arch1-1")
    if "做不到的：自動旋轉與光感測" not in root:
        fail("unrelated hardware limit was rewritten")
    print("docs-story: clean")


if __name__ == "__main__":
    main()
