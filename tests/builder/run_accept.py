#!/usr/bin/env python3
"""Run tests/builder/run.sh. Acceptance argv is python, not a shell."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.exit(
    subprocess.run(
        ["bash", str(ROOT / "tests/builder/run.sh")],
        cwd=ROOT,
    ).returncode
)
