#!/usr/bin/env python3
"""Run tests/stick/make-maint-test.sh. Acceptance argv is python, not bash."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.exit(subprocess.run(["bash", str(ROOT / "tests/stick/make-maint-test.sh")]).returncode)
