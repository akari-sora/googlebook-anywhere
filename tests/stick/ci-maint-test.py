#!/usr/bin/env python3
"""The stick CI job runs the offline make-maint suite, and names platform skips.

Reads .gitlab-ci.yml as text. Does not call GitLab and does not run the suite.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".gitlab-ci.yml"

SKIP_LINES = (
    "#   gitlab-ci: qemu-install.py real boot (no KVM); tests/builder/e2e.sh; "
    "tests/install/run.sh pretend-disk cases (install-tools still runs and prints each SKIP)",
    "#   macos: qemu-install.py real boot; tests/builder/e2e.sh; "
    "tests/install/run.sh Linux guard section",
    "#   linux-dev: none of those three",
)
MAINT_LINE = "runuser -u tester -- python3 -I tests/stick/run_make_maint_test.py"
EXPORT_LINE = "runuser -u tester -- python3 -I tests/stick/public-export-test.py"
STICK_LINES = (
    "python3 tests/stick/selftest.py",
    "python3 tests/update/selftest.py",
    "python3 tests/update/mutate.py",
)


def fail(message):
    print(f"ci-maint: {message}", file=sys.stderr)
    sys.exit(1)


def job_body(text, name):
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == f"{name}:":
            start = index + 1
            break
    if start is None:
        fail(f"missing job {name}")
    body = []
    for line in lines[start:]:
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        body.append(line)
    return body


def main():
    if not CI.is_file():
        fail("missing .gitlab-ci.yml")
    text = CI.read_text(encoding="utf-8")
    body = job_body(text, "stick")
    script = [line.strip() for line in body if line.strip().startswith("- ")]
    joined = "\n".join(body)
    if "stage: test" not in joined or "<<: *deps" not in joined:
        fail("stick job is not a test-stage job with the shared deps")
    for line in STICK_LINES:
        if not any(line in item for item in script):
            fail(f"stick job no longer runs {line}")
    if not any(item == "- " + MAINT_LINE for item in script):
        fail("stick job does not run the offline make-maint suite as tester")
    useradd_at = [i for i, item in enumerate(script) if item.startswith("- useradd -m tester")]
    maint_at = [i for i, item in enumerate(script) if item == "- " + MAINT_LINE]
    if not useradd_at:
        fail("stick job does not create the tester user")
    useradd = useradd_at[0]
    maint = maint_at[0]
    if not useradd < maint:
        fail("tester user is created after the make-maint suite")
    export_at = [i for i, item in enumerate(script) if item == "- " + EXPORT_LINE]
    if len(export_at) != 1:
        fail("stick job does not run the public export scan as tester")
    if not useradd < export_at[0]:
        fail("public export scan runs before the tester user exists")
    for line in SKIP_LINES:
        if line not in text.splitlines():
            fail("platform skip record is missing a platform")
    for line in text.splitlines():
        if line.startswith("#   ") and "run_make_maint_test.py" in line:
            fail("platform skip record says the make-maint suite does not run")
    print("ci-maint: stick job runs the offline make-maint suite as tester")
    print("ci-maint: platform skips recorded for gitlab-ci, macos, linux-dev")
    print("ci-maint: clean")


if __name__ == "__main__":
    main()
