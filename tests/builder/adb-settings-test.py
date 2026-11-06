#!/usr/bin/env python3
"""Exercise the generated KANO ADB startup with stubbed Android commands."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
KEYS = ('development_settings_enabled', 'adb_enabled')


def generated(tree):
    vendor = (ROOT / 'builder/vendor.sh').read_text()
    start = '(tree / "etc/kano-adb_keys").write_text('
    middle = '# Heavy diag files are written with one fsync each as well.'
    first = vendor[vendor.index(start):vendor.index(middle)]
    start2 = 'ka = tree / "etc/kano-adb.sh"'
    end2 = '# udsattestation asks servicemanager'
    second = vendor[vendor.index(start2):vendor.index(end2)]
    namespace = {'tree': tree, 'vmap': {'etc/init/fwupd.rc': {}}, 'Path': Path, 'os': os}
    with mock.patch.dict(os.environ, {}, clear=True):
        exec(first, namespace)
        exec(second, namespace)
    return (tree / 'etc/kano-adb.sh').read_text()


class AdbSettings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tree = self.root / 'vendor'
        (tree / 'etc/init').mkdir(parents=True)
        script = generated(tree)
        # Run the actual generated text with only system paths redirected into
        # the temporary test root; every Android side-effect command is a stub.
        script = script.replace('/system/bin/cmd', 'fake-cmd')
        script = script.replace('/vendor/etc/kano-adb_keys', str(tree / 'etc/kano-adb_keys'))
        script = script.replace('/data/', str(self.root / 'data') + '/')
        self.script = self.root / 'generated.sh'
        self.script.write_text(script)
        bindir = self.root / 'bin'
        bindir.mkdir()
        programs = {
            'timeout': '#!/bin/sh\nshift\nexec "$@"\n',
            'fake-cmd': '''#!/bin/sh
for fd in 0 1 2; do
  [ -p /dev/fd/$fd ] || { echo "non-pipe fd $fd" >&2; exit 88; }
done
printf '%s %s\\n' "$2" "$4" >> "$EVENTS"
if [ "$FAIL_AT" = "$2:$4" ]; then
  case "$MODE" in fail) echo "diagnostic failure" >&2; exit 42 ;; timeout) exit 124 ;; esac
fi
if [ "$2" = get ]; then
  if [ "$FAIL_AT" = "get:$4" ] && [ "$MODE" = mismatch ]; then echo 0; else echo 1; fi
fi
''',
            'setprop': '#!/bin/sh\nprintf "setprop %s\\n" "$*" >> "$EVENTS"\n',
            'getprop': '#!/bin/sh\necho running\n',
            'chown': '#!/bin/sh\nexit 0\n',
            'chmod': '#!/bin/sh\nexit 0\n',
            'restorecon': '#!/bin/sh\nexit 0\n',
            'sleep': '#!/bin/sh\nexit 0\n',
            'ls': '#!/bin/sh\nexit 0\n',
        }
        for name, body in programs.items():
            path = bindir / name
            path.write_text(body)
            path.chmod(0o755)
        self.events = self.root / 'events'
        self.env = dict(os.environ, PATH=str(bindir) + os.pathsep + '/usr/bin:/bin', EVENTS=str(self.events))

    def run_generated(self, mode='', fail_at=''):
        # This host Bash test does not establish behavior on Android mksh.
        result = subprocess.run(['/bin/bash', str(self.script)],
                                env=dict(self.env, MODE=mode, FAIL_AT=fail_at),
                                capture_output=True, text=True, timeout=10)
        events = self.events.read_text().splitlines() if self.events.exists() else []
        log = self.root / 'data/kano-diag/adb-settings.txt'
        return result.returncode, events, log.read_text() if log.exists() else ''

    def test_success_preserves_adbd_start(self):
        rc, events, log = self.run_generated()
        self.assertEqual(rc, 0)
        self.assertEqual(events[:4], ['put ' + KEYS[0], 'get ' + KEYS[0],
                                     'put ' + KEYS[1], 'get ' + KEYS[1]])
        self.assertIn('setprop service.adb.listen_addrs tcp:5555', events)
        self.assertIn('setprop service.adb.tcp.port 5555', events)
        self.assertIn('setprop ctl.stop adbd', events)
        self.assertIn('setprop ctl.start adbd', events)
        for key in KEYS:
            self.assertIn(f'{key} put rc=0', log)
            self.assertIn(f'{key} get rc=0', log)

    def test_failures_never_start_adbd_and_record_failure(self):
        cases = [('fail', 'put', 42), ('timeout', 'put', 124),
                 ('fail', 'get', 42), ('timeout', 'get', 124), ('mismatch', 'get', 1)]
        for mode, operation, expected in cases:
            for key in KEYS:
                with self.subTest(mode=mode, operation=operation, key=key):
                    self.events.unlink(missing_ok=True)
                    rc, events, log = self.run_generated(mode, f'{operation}:{key}')
                    self.assertEqual(rc, expected)
                    self.assertFalse(any(event.startswith('setprop ') for event in events), events)
                    self.assertIn(f'{key} {operation} rc={0 if mode == "mismatch" else expected}', log)
                    if mode == 'fail':
                        self.assertIn('diagnostic failure', log)
                    if mode == 'mismatch':
                        self.assertIn(f'{key} readback mismatch rc=1', log)


if __name__ == '__main__':
    unittest.main()
