#!/usr/bin/env python3
"""Focused build preflight tests; no image build or key generation is run."""

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ApexKeyCreationTimingTests(unittest.TestCase):
    def run_slice(self, *, valid_avb=True, enough_disk=True):
        source = (ROOT / "builder/build.sh").read_text()
        start = source.index("for c in python3 mcopy mtype mdir")
        stop = source.index('if [ "$STACK" = gop-swiftshader ]', start)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            otatools = base / "otatools"
            (otatools / "bin").mkdir(parents=True)
            (otatools / "lib64").mkdir()
            for name in ("avbtool", "mkfs.erofs", "fsck.erofs"):
                tool = otatools / "bin" / name
                tool.write_text("#!/bin/sh\nexit 0\n")
                tool.chmod(0o755)
            avb_key = base / "vbmeta.pem"
            avb_key.write_text(
                "-----BEGIN PRIVATE KEY-----\nfixture\n"
                if valid_avb else "this is not a private key\n"
            )
            out = base / "out"
            marker = base / "apex-keys-created"
            setup = f"""set -euo pipefail
HERE={shlex.quote(str(ROOT / 'builder'))}
GBOS_PROFILE=/unused
GBOS_AVB_KEY={shlex.quote(str(avb_key))}
GBOS_OTATOOLS={shlex.quote(str(otatools))}
GBOS_OUT={shlex.quote(str(out))}
GBOS_SKIP_SELFTEST=1
GBOS_SKIP_SPACE_CHECK={'1' if enough_disk else '0'}
KEY_MARKER={shlex.quote(str(marker))}
die() {{ echo "build.sh: $*" >&2; exit 2; }}
command() {{ if [ "$1" = -v ]; then return 0; else builtin command "$@"; fi; }}
awk() {{
    if [ "${{@: -1}}" = /proc/meminfo ]; then printf '99999999\n';
    else builtin command awk "$@"; fi
}}
python3() {{
    if [ "$1" = -c ] && [[ "$2" == *ensure_apex_keys* ]]; then
        : > "$KEY_MARKER"
        printf 'APEX key fixture\n'
    else
        printf 'true\n'
    fi
}}
"""
            if not enough_disk:
                setup += "df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted-on\\nfake 100 99 1 99%% /\\n'; }\n"
            result = subprocess.run(
                ["bash", "-c", setup + source[start:stop]],
                text=True,
                capture_output=True,
                env=os.environ,
            )
            return result, marker.exists()

    def test_invalid_avb_key_fails_before_apex_key_creation(self):
        result, created = self.run_slice(valid_avb=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GBOS_AVB_KEY is not a PEM private key", result.stderr)
        self.assertFalse(created)

    def test_insufficient_disk_fails_before_apex_key_creation(self):
        result, created = self.run_slice(enough_disk=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("less than 45 GB free", result.stderr)
        self.assertFalse(created)

    def test_successful_preflight_reaches_apex_key_creation(self):
        result, created = self.run_slice()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(created)


if __name__ == "__main__":
    unittest.main(verbosity=2)
