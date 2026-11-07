#!/usr/bin/env python3
"""Public-only startup key policy tests; never starts a build/install or uses owner keys."""
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'builder/lib'))
SPEC = importlib.util.spec_from_file_location('startup_policy_ifma', ROOT / 'builder/lib/ifma.py')
ifma = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ifma)
VAR = 'GBOS_APEX_KEYS_EXISTING_ONLY'


class StartupPolicyTests(unittest.TestCase):
    def test_default_and_zero_preserve_creation(self):
        for env in ({}, {VAR: '0'}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), mock.patch.object(ifma, 'ensure_apex_keys', return_value='keys') as ensure:
                self.assertEqual(ifma.ensure_apex_keys_for_startup(), 'keys')
                ensure.assert_called_once_with(create=True)

    def test_one_validates_without_creation(self):
        with mock.patch.dict(os.environ, {VAR: '1'}, clear=True), mock.patch.object(ifma, 'ensure_apex_keys', return_value='keys') as ensure:
            self.assertEqual(ifma.ensure_apex_keys_for_startup(), 'keys')
            ensure.assert_called_once_with(create=False)

    def test_invalid_values_never_enter_key_api(self):
        for value in ('', 'true', 'false', '01', '2', '-1', ' 1', '1\n'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {VAR: value}, clear=True), mock.patch.object(ifma, 'ensure_apex_keys') as ensure:
                with self.assertRaisesRegex(SystemExit, 'GBOS_APEX_KEYS_EXISTING_ONLY'):
                    ifma.ensure_apex_keys_for_startup()
                ensure.assert_not_called()

    def test_missing_and_partial_existing_only_never_generate(self):
        with tempfile.TemporaryDirectory(prefix='public-startup-policy-') as td:
            keys = Path(td) / 'keys'
            env = {VAR: '1', 'GBOS_APEX_KEYS_DIR': str(keys)}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(ifma, '_openssl_output') as openssl:
                with self.assertRaisesRegex(SystemExit, 'refusing creation'):
                    ifma.ensure_apex_keys_for_startup()
                self.assertFalse(keys.exists())
                keys.mkdir(mode=0o700)
                fixture = keys / 'apex_payload.pem'
                fixture.write_bytes(b'PUBLIC INCOMPLETE FIXTURE; NOT A KEY')
                with self.assertRaisesRegex(SystemExit, 'partial APEX keyset'):
                    ifma.ensure_apex_keys_for_startup()
                self.assertEqual(fixture.read_bytes(), b'PUBLIC INCOMPLETE FIXTURE; NOT A KEY')
                self.assertEqual(list(keys.iterdir()), [fixture])
                openssl.assert_not_called()

    def test_complete_existing_keyset_is_validated_without_writes(self):
        with tempfile.TemporaryDirectory(prefix='public-startup-policy-') as td:
            keys = Path(td) / 'keys'
            keys.mkdir(mode=0o700)
            names = ('apex_payload.pem', 'apex_container.pem', 'apex_container.der')
            for name in names:
                (keys / name).write_bytes(b'PUBLIC MOCK KEYSET VALIDATOR FIXTURE')
            before = {name: (keys / name).read_bytes() for name in names}
            with mock.patch.dict(os.environ, {VAR: '1', 'GBOS_APEX_KEYS_DIR': str(keys)}, clear=True), mock.patch.object(ifma, '_validate_apex_keyset') as validate, mock.patch.object(ifma, 'apex_key_fingerprints', return_value={'payload_public_sha256': 'a'*64, 'container_cert_sha256': 'b'*64}), mock.patch.object(ifma, '_openssl_output') as openssl:
                result = ifma.ensure_apex_keys_for_startup()
                validate.assert_called_once_with(keys, result)
                openssl.assert_not_called()
            self.assertEqual(before, {name: (keys / name).read_bytes() for name in names})


class ShellPolicyTests(unittest.TestCase):
    def test_invalid_values_fail_before_any_output_directory(self):
        for rel in ('builder/build.sh', 'install/install.sh'):
            for value in ('', '2', 'true', '01', '1\n'):
                with self.subTest(rel=rel, value=value), tempfile.TemporaryDirectory(prefix='public-startup-shell-') as td:
                    out = Path(td) / 'must-not-exist'
                    env = {k: v for k, v in os.environ.items() if not k.startswith('GBOS_')}
                    env.update({VAR: value, 'GBOS_OUT': str(out), 'PYTHONDONTWRITEBYTECODE': '1'})
                    args = ['bash', str(ROOT / rel)]
                    if rel.startswith('install/'):
                        args += ['--work', str(out)]
                    proc = subprocess.run(args, env=env, text=True, capture_output=True, timeout=10)
                    self.assertEqual(proc.returncode, 2)
                    self.assertIn('GBOS_APEX_KEYS_EXISTING_ONLY', proc.stderr)
                    self.assertFalse(out.exists())

    def test_actual_shell_startup_assignments_use_existing_only_api(self):
        for rel, anchor in (('builder/build.sh', 'HERE'), ('install/install.sh', 'ROOT')):
            source = (ROOT / rel).read_text().splitlines()
            start = next(i for i, line in enumerate(source) if 'APEX_KEY_INFO=$(' in line)
            stop = start
            while not source[stop].endswith('lib")'):
                stop += 1
            command = '\n'.join(source[start:stop + 1])
            with self.subTest(rel=rel), tempfile.TemporaryDirectory(prefix='public-startup-shell-') as td:
                keys = Path(td) / 'absent-keys'
                env = {**os.environ, VAR: '1', 'GBOS_APEX_KEYS_DIR': str(keys), 'PYTHONDONTWRITEBYTECODE': '1'}
                setup = 'set -euo pipefail\n' + anchor + '=' + shlex.quote(str(ROOT / 'builder' if anchor == 'HERE' else ROOT)) + '\n'
                setup += 'AVB_KEY=/unused\nWORK=' + shlex.quote(td) + '\n'
                proc = subprocess.run(['bash', '-c', setup + command], env=env, text=True, capture_output=True, timeout=10)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn('refusing creation', proc.stderr)
                self.assertFalse(keys.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
