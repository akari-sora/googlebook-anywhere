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
        script = script.replace('/system/bin/log', 'fake-log')
        script = script.replace('/vendor/etc/kano-adb_keys', str(tree / 'etc/kano-adb_keys'))
        script = script.replace('/data/', str(self.root / 'data') + '/')
        self.script = self.root / 'generated.sh'
        self.script.write_text(script)
        bindir = self.root / 'bin'
        bindir.mkdir()
        programs = {
            'fake-log': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$MARKERS"\n[ "$LOG_FAIL" != 1 ]\n',
            'timeout': '#!/bin/sh\n[ "$1" != -k ] || shift 2\nshift\nexec "$@"\n',
            'fake-cmd': '''#!/bin/sh
for fd in 0 1 2; do
  [ -p /dev/fd/$fd ] || { echo "non-pipe fd $fd" >&2; exit 88; }
done
printf '%s %s\\n' "$2" "$4" >> "$EVENTS"
if [ "$FAIL_AT" = "$2:$4" ]; then
  case "$MODE" in
    fail) echo "diagnostic failure" >&2; exit 42 ;;
    timeout) exit 124 ;;
    timeout_once) [ "$(grep -Fxc "$2 $4" "$EVENTS")" -ne 1 ] || exit 124 ;;
  esac
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
        self.markers = self.root / 'markers'
        self.env = dict(os.environ, PATH=str(bindir) + os.pathsep + '/usr/bin:/bin', EVENTS=str(self.events), MARKERS=str(self.markers))

    def run_generated(self, mode='', fail_at=''):
        # This host Bash test does not establish behavior on Android mksh.
        result = subprocess.run(['/bin/bash', str(self.script)],
                                env=dict(self.env, MODE=mode, FAIL_AT=fail_at),
                                capture_output=True, text=True, timeout=10)
        events = self.events.read_text().splitlines() if self.events.exists() else []
        log = self.root / 'data/kano-diag/adb-settings.txt'
        return result.returncode, events, log.read_text() if log.exists() else ''

    def test_marker_failure_privacy_and_retry(self):
        rc, events, log = self.run_generated('timeout_once', f'put:{KEYS[0]}')
        self.assertEqual(rc, 0)
        markers = self.markers.read_text()
        self.assertIn(f'step=put key={KEYS[0]} attempt=1 rc=124', markers)
        self.assertIn(f'step=put key={KEYS[0]} attempt=2 rc=0', markers)
        self.events.unlink(); self.markers.unlink()
        rc, events, log = self.run_generated('fail', f'get:{KEYS[1]}')
        self.assertEqual(rc, 42)
        self.assertIn('diagnostic failure', log)
        self.assertNotIn('diagnostic failure', self.markers.read_text())
        self.assertIn(f'step=get key={KEYS[1]} attempt=1 rc=42', self.markers.read_text())
        self.assertFalse(any(event.startswith('setprop ') for event in events))

    def test_log_failure_does_not_change_existing_flow(self):
        self.env['LOG_FAIL'] = '1'
        rc, events, log = self.run_generated()
        self.assertEqual(rc, 0)
        self.assertIn('setprop ctl.start adbd', events)
        self.assertIn(f'{KEYS[0]} get rc=0', log)

    def test_initial_failure_marker_does_not_change_exit(self):
        data = self.root / 'data'; data.mkdir(); (data / 'kano-diag').write_text('not a directory')
        rc, events, log = self.run_generated()
        self.assertEqual(rc, 1)
        markers = self.markers.read_text()
        self.assertRegex(markers, r'step=mkdir key=none attempt=0 rc=[1-9]')
        self.assertRegex(markers, r'step=open key=none attempt=0 rc=[1-9]')
        self.assertFalse(any(event.startswith('setprop ') for event in events))

    def test_posix_redirection_failure_emits_open_marker(self):
        data = self.root / 'data'; data.mkdir(); (data / 'kano-diag').write_text('not a directory')
        prefix = self.script.read_text().split('# cmd passes stdio over Binder', 1)[0]
        result = subprocess.run(['/bin/bash', '--posix'], input=prefix, text=True,
                                env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(self.markers.read_text(), r'step=open key=none attempt=0 rc=[1-9]')

    def test_missing_key_file_marker_but_original_flow_continues(self):
        (self.root / 'vendor/etc/kano-adb_keys').unlink()
        rc, events, log = self.run_generated()
        self.assertEqual(rc, 0)
        self.assertRegex(self.markers.read_text(), r'step=key_copy key=none attempt=0 rc=[1-9]')
        self.assertIn('setprop ctl.start adbd', events)

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

    def test_first_timeout_is_retried_for_put_and_get(self):
        for operation in ('put', 'get'):
            for key in KEYS:
                with self.subTest(operation=operation, key=key):
                    self.events.unlink(missing_ok=True)
                    rc, events, log = self.run_generated('timeout_once', f'{operation}:{key}')
                    self.assertEqual(rc, 0)
                    self.assertEqual(events.count(f'{operation} {key}'), 2)
                    self.assertIn(f'{key} {operation} attempt=1 rc=124', log)
                    self.assertIn(f'{key} {operation} attempt=2 rc=0', log)
                    self.assertIn('setprop ctl.start adbd', events)

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
                    self.assertEqual(events.count(f'{operation} {key}'), 2 if mode == 'timeout' else 1)
                    if mode == 'timeout':
                        self.assertIn(f'{key} {operation} attempt=2 rc=124', log)
                    if mode == 'fail':
                        self.assertIn('diagnostic failure', log)
                    if mode == 'mismatch':
                        self.assertIn(f'{key} readback mismatch rc=1', log)


if __name__ == '__main__':
    unittest.main()
