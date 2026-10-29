#!/usr/bin/env python3
"""Small shell regression slices; never starts a builder or installer."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


class PreflightTests(unittest.TestCase):
    def run_preflight(self, enabled):
        source = (ROOT / "builder/build.sh").read_text()
        start = source.index('for c in python3 mcopy mtype mdir')
        stop = source.index('if [ "${GBOS_SKIP_SELFTEST:-}" = 1 ]; then', start)
        # All normal tools exist; only OpenSSL is deliberately unavailable.
        setup = '''set -euo pipefail
HERE=/unused
GBOS_PROFILE=/unused
command() { if [ "$1" = -v ]; then [ "$2" != openssl ]; else builtin command "$@"; fi; }
die() { echo "$*" >&2; exit 2; }
'''
        setup += f"python3() {{ echo {shlex.quote(enabled)}; }}\n"
        return subprocess.run(["bash", "-c", setup + source[start:stop]], text=True, capture_output=True)

    def test_switch_off_does_not_require_openssl(self):
        proc = self.run_preflight("false")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_switch_on_requires_openssl(self):
        proc = self.run_preflight("true")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing command: openssl", proc.stderr)


class VendorHardlinkTests(unittest.TestCase):
    def run_slice(self, enabled):
        script = (ROOT / "builder/vendor.sh").read_text()
        start = script.index("strict_metadata = bool(devprofile.load")
        stop = script.index('(tmp / "vendor.meta.pickle")', start)
        scope = {"rebuild": SimpleNamespace(walk_image=lambda *args, **kwargs: ({}, 1)),
                 "devprofile": SimpleNamespace(load=lambda _path: {"cpu_compat": {"patch_ifma": enabled}}),
                 "os": SimpleNamespace(environ={"GBOS_PROFILE": "/unused"}), "tmp": Path("/unused"),
                 "root": 0, "data": b"", "blksz": 4096, "meta_off": 1, "xattr": 0,
                 "pickle": SimpleNamespace(dumps=lambda _value: b"ignored")}
        return script[start:stop], scope

    def test_vendor_source_hardlinks_are_refused_when_ifma_is_enabled(self):
        script, scope = self.run_slice(True)
        with self.assertRaisesRegex(SystemExit, "vendor_a has 1 hardlinked entries"):
            exec(script, scope)

    def test_vendor_source_hardlinks_remain_accepted_when_ifma_is_disabled(self):
        script, scope = self.run_slice(False)
        exec(script, scope)


class ResidualBuildInfoTests(unittest.TestCase):
    def run_slice(self, vendor_log, enabled="true"):
        source = (ROOT / "builder/build.sh").read_text()
        start = source.index("# Record only residuals actually accepted")
        stop = source.index("grep -E '^(firmware check:", start)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "vendor.log").write_text(vendor_log)
            fingerprints = "APEX payload public key sha256: retained\n"
            (out / "build-info.txt").write_text(fingerprints)
            result = subprocess.run(
                ["bash", "-c", "set -euo pipefail\n" + source[start:stop]],
                env={**os.environ, "GBOS_OUT": str(out), "PATCH_IFMA": enabled},
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout, (out / "build-info.txt").read_text(), fingerprints

    def test_actual_accepted_residual_is_reported_once_without_losing_keys(self):
        accepted = 'ifma residual accepted: {"file_offset":"0x17b96d","instruction":"vpmadd52luq"}\n'
        stdout, info, keys = self.run_slice("vendor tree ok\n" + accepted + accepted)
        self.assertEqual(stdout, accepted)
        self.assertEqual(info, keys + accepted)

    def test_no_accepted_scan_does_not_invent_a_residual(self):
        stdout, info, keys = self.run_slice("ifma residual scan: no hits\nvendor tree ok\n")
        self.assertEqual(stdout, "")
        self.assertEqual(info, keys)

    def test_disabled_ifma_does_not_promote_residual_log_lines(self):
        stdout, info, keys = self.run_slice("ifma residual accepted: synthetic\n", "false")
        self.assertEqual(stdout, "")
        self.assertEqual(info, keys)


class InstallerFingerprintTests(unittest.TestCase):
    def test_cache_inputs_and_build_info_use_same_persistent_identity(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            work = base / "work"
            avb = base / "external/vbmeta.pem"
            avb.parent.mkdir(parents=True)
            avb.write_text("persistent AVB path anchor\n")
            env = dict(os.environ)
            env.pop("GBOS_APEX_KEYS_DIR", None)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            script = (ROOT / "install/install.sh").read_text()
            start = script.index("# Persistent APEX identities are build inputs too")
            stop = script.index('IMG="$WORK/build/image/$IMAGE_NAME"', start)
            setup = "set -euo pipefail\n"
            for key, value in (("ROOT", ROOT), ("PROFILE", ROOT / "profiles/kano"),
                               ("WORK", work), ("KEYS", work / "keys"), ("AVB_KEY", avb)):
                setup += f"{key}={shlex.quote(str(value))}\n"
            setup += 'PARAMS="unchanged-other-inputs"\n'
            run = lambda: subprocess.run(["bash", "-c", setup + script[start:stop] + '\nprintf "%s\\n" "$PARAMS"\n'],
                                         text=True, capture_output=True, env=env)
            first = run()
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue((work / "keys/apex-keys/apex_payload.pem").is_file())
            self.assertFalse((avb.parent / "apex-keys").exists())
            # --update without --key chooses the retained AVB key, not its external source.
            kept_avb = work / "keys/vbmeta.pem"
            kept_avb.write_bytes(avb.read_bytes())
            setup += f"AVB_KEY={shlex.quote(str(kept_avb))}\n"
            second = run()
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(first.stdout, second.stdout)
            fingerprints = first.stdout.split("unchanged-other-inputs\n", 1)[1].strip()
            self.assertEqual(len(fingerprints.splitlines()), 2)
            self.assertIn("APEX payload public key sha256:", fingerprints)
            self.assertIn("APEX container certificate sha256:", fingerprints)

            # Execute the actual build-info write with the exact captured public lines.
            build_script = (ROOT / "builder/build.sh").read_text()
            write_line = next(line for line in build_script.splitlines() if '> "$GBOS_OUT/build-info.txt"' in line)
            out = base / "new-output"
            out.mkdir()
            subprocess.run(["bash", "-c", write_line], check=True, env={**env,
                "PATCH_IFMA": "true", "APEX_KEY_INFO": fingerprints, "GBOS_OUT": str(out)})
            self.assertEqual((out / "build-info.txt").read_text(), fingerprints + "\n")

            # An explicit different keyset changes cache inputs while AVB/settings stay fixed.
            env["GBOS_APEX_KEYS_DIR"] = str(base / "alternate-keys")
            changed = run()
            self.assertEqual(changed.returncode, 0, changed.stderr)
            self.assertNotEqual(changed.stdout, first.stdout)
            (base / "alternate-keys/apex_container.der").unlink()
            partial = run()
            self.assertNotEqual(partial.returncode, 0)
            self.assertIn("partial APEX keyset", partial.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
