#!/usr/bin/env python3
"""Offline check that the installer-stick docs name one build sequence.

No network, no assemble, no write-usb. A restored claim that the repo
commands cannot build the stick fails this check.
"""
import re
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
    neg_amdgpu = re.compile(r"(?:lacks\s+an?|no|沒有)\s*`?amdgpu`?\s*`?backend`?", re.IGNORECASE)
    en = read(REPO / "README.en.md")
    for phrase in ("BSD-3-Clause", "GBOS_MINIGBM", "tools/minigbm/NOTICES.txt"):
        if phrase not in root or phrase not in en:
            fail(f"README parity lost {phrase}")
    for rname, rtext in (("README.md", root), ("README.en.md", en)):
        row = next((ln for ln in rtext.splitlines() if ln.startswith("| `tools/minigbm/` |")), None)
        if row is None or not all(
            tok in row
            for tok in (
                "/vendor/lib64/libminigbm_gralloc.so",
                "NOTICES.txt",
                "amdgpu_linear.c",
                "upstream-delta.patch",
            )
        ):
            fail(f"README parity lost tools/minigbm/ layout row in {rname}")
        pkg = next((p for p in rtext.split("\n\n") if "googlebook-anywhere-<版本>.tar.gz" in p), None)
        if pkg is None or not all(
            tok in pkg for tok in ("tools/minigbm/NOTICES.txt", "amdgpu_linear.c", "upstream-delta.patch")
        ):
            fail(f"README parity lost tools/minigbm/NOTICES.txt in {rname} release-package list")
    if (REPO / "docs/RELEASE-PROCESS.md").exists():
        fail("docs/RELEASE-PROCESS.md must be folded into README.md and README.en.md")
    if "只發布該版本的段落" not in root or "指向同一個 tag 的完整 `docs/RELEASE-NOTES.md` 歷史絕對連結" not in root:
        fail("README.md release rules lost the folded RELEASE-PROCESS sentence")
    if (
        "publishes only that version’s section (preserving headings inside fenced code blocks)" not in en
        or "appends an absolute link to the full `docs/RELEASE-NOTES.md` history at the same tag" not in en
    ):
        fail("README.en.md release rules lost the folded RELEASE-PROCESS sentence")
    if "只能出現在頂格" not in root:
        fail("README.md lost the version-heading rule that the version string `Googlebook Anywhere v…` may appear only in a top-level `# Googlebook Anywhere v…` heading line")
    if "may appear only in a top-level" not in en:
        fail("README.en.md lost the version-heading rule that the version string `Googlebook Anywhere v…` may appear only in a top-level `# Googlebook Anywhere v…` heading line")
    if "都只記錄不攔截（report-only），所以 `PASS` 不代表這些韌體釘選已被這個檢查核對" not in read(RELEASE):
        fail("docs/RELEASE-NOTES.md lost the phrase saying firmware-declaring drivers are report-only, so a PASS does not attest the firmware pins")
    for path in (
        REPO / "docs/COMPATIBILITY.md",
        REPO / "docs/COMPATIBILITY.en.md",
        REPO / "docs/TROUBLESHOOTING.md",
        REPO / "docs/TROUBLESHOOTING.en.md",
    ):
        lines = read(path).splitlines()
        if not any("GBOS_DEV_KERNEL_DIR" in ln and "GBOS_MINIGBM" in ln for ln in lines):
            fail(
                f"{path.relative_to(REPO)} build-requirement bullet does not name both "
                "GBOS_DEV_KERNEL_DIR and GBOS_MINIGBM"
            )
    for path in (
        REPO / "README.md",
        REPO / "README.en.md",
        REPO / "docs/COMPATIBILITY.md",
        REPO / "docs/COMPATIBILITY.en.md",
        REPO / "docs/TROUBLESHOOTING.md",
        REPO / "docs/TROUBLESHOOTING.en.md",
        REPO / "profiles/amd-hx370/profile.toml",
    ):
        text = read(path)
        lines = text.splitlines()
        if neg_amdgpu.search(text):
            fail(f"{path.relative_to(REPO)} still says minigbm has no amdgpu backend")
        if path.parent == REPO / "docs":
            has_gralloc = any("backend_amdgpu" in ln and "sha256" in ln and "[gralloc]" in ln for ln in lines)
        elif path.name == "profile.toml":
            has_gralloc = (
                any("backend_amdgpu" in ln and "backend_vkms" in ln for ln in lines)
                and any("[gralloc]" in ln and "libminigbm_gralloc.so" in ln for ln in lines)
                and any("GBOS_MINIGBM" in ln and "sha256" in ln for ln in lines)
                and bool(re.search(r"^\[gralloc\]\nsha256 = \"[0-9a-f]{64}\"$", text, re.MULTILINE))
            )
        else:
            has_gralloc = (
                any("backend_amdgpu" in ln and "backend_vkms" in ln and "libminigbm_gralloc.so" in ln for ln in lines)
                and any("[gralloc]" in ln and "GBOS_MINIGBM" in ln and "sha256" in ln for ln in lines)
            )
        if not has_gralloc:
            fail(f"{path.relative_to(REPO)} does not describe the pinned backend_amdgpu gralloc")
    print("docs-story: clean")


if __name__ == "__main__":
    main()
