#!/usr/bin/env python3
"""Packet gate for omg accept. Exit 0 only when the maintenance stick is actually done.

The offline contract (arguments, pin refusals, layout) is one story. This stays
red until the committed pin list names real files, ./RESULT.md records the
builder run, shellcheck, and the Linux test host qemu-install pass count, and the
offline test still passes. An empty pin list must not verify the run.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKET = REPO.parent


def fail(msg):
    print(f"maint_goal: {msg}", file=sys.stderr)
    raise SystemExit(2)


def main():
    test = subprocess.run(["bash", str(REPO / "tests/stick/make-maint-test.sh")])
    if test.returncode != 0:
        fail(f"offline test exited {test.returncode}")
    n = 0
    for line in (REPO / "install/stick/maint.pins").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            n += 1
    if n < 1:
        fail("committed pin list has no files; the maintenance rootfs is not buildable yet")
    result = PACKET / "RESULT.md"
    if not result.is_file():
        fail("RESULT.md is missing")
    text = result.read_text(encoding="utf-8")
    for needle in ("builder:", "shellcheck:"):
        if needle not in text:
            fail(f"RESULT.md has no {needle} line")
    if not re.search(r"^qemu-install: [0-9]+ passed, 0 failed$", text, re.M):
        fail("RESULT.md has no qemu-install pass line")
    print("maint_goal: ok")


if __name__ == "__main__":
    main()
