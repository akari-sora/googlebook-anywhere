#!/usr/bin/env python3
"""Exercise the generated one-shot audio script with a fake data directory and commands."""
import ast
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def generated_script():
    source = (ROOT / 'builder/vendor.sh').read_text()
    start = source.index('(tree / "etc/kano-audiotest.sh").write_text(')
    end = source.index('\n(tree / "etc/init/kano-audiotest.rc")', start)
    call = ast.parse(source[start:end]).body[0].value
    script = ast.literal_eval(call.args[0])
    start = source.index('at = tree / "etc/kano-audiotest.sh"')
    end = source.index('# More hardware state', start)
    class FakeFile:
        def read_text(self):
            return script
        def write_text(self, value):
            self.value = value
    class FakeTree:
        def __truediv__(self, other):
            return file
    file = FakeFile()
    exec(compile(source[start:end], 'vendor.sh audio gate', 'exec'), {'tree': FakeTree()})
    return file.value


class AudioTest(unittest.TestCase):
    def run_script(self, marker, rm_behavior):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            flag = data / 'kano-audiotest.enable'
            if marker == 'file':
                flag.touch()
            elif marker == 'symlink':
                (data / 'target').touch()
                flag.symlink_to(data / 'target')
            log = root / 'calls'
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            (bin_dir / 'rm').write_text('#!/bin/sh\n' + rm_behavior + '\n')
            for name in ('sleep', 'timeout'):
                (bin_dir / name).write_text('#!/bin/sh\nprintf "%s\\n" "' + name + ' $*" >> "$CALLS"\n')
            for file in bin_dir.iterdir():
                file.chmod(0o755)
            script = generated_script().replace('/data/', str(data) + '/')
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], CALLS=str(log))
            result = subprocess.run(['/bin/sh'], input=script, text=True, env=env, capture_output=True)
            return result, log.read_text().splitlines() if log.exists() else [], flag.is_symlink() or flag.exists()

    def test_failed_removal_stops_before_audio(self):
        result, calls, remains = self.run_script('file', 'exit 1')
        self.assertTrue(remains)
        self.assertEqual(calls, [])
        self.assertNotEqual(result.returncode, 0)

    def test_successful_rm_with_residual_marker_stops(self):
        for marker in ('file', 'symlink'):
            with self.subTest(marker=marker):
                result, calls, remains = self.run_script(marker, 'exit 0')
                self.assertTrue(remains)
                self.assertEqual(calls, [])
                self.assertNotEqual(result.returncode, 0)

    def test_dangling_residual_symlink_stops(self):
        result, calls, remains = self.run_script('symlink', '/bin/rm "$(dirname "$2")/target"; exit 0')
        self.assertTrue(remains)
        self.assertEqual(calls, [])
        self.assertNotEqual(result.returncode, 0)

    def test_consumed_marker_runs_existing_sequence(self):
        for marker in ('file', 'symlink'):
            with self.subTest(marker=marker):
                result, calls, remains = self.run_script(marker, 'exec /bin/rm "$@"')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(remains)
                self.assertEqual([line.split()[0] for line in calls],
                                 ['sleep', 'timeout', 'timeout', 'timeout', 'timeout', 'timeout',
                                  'sleep', 'timeout', 'sleep', 'timeout', 'sleep', 'timeout', 'sleep', 'timeout'])

    def test_absent_marker_is_silent(self):
        result, calls, _ = self.run_script(None, 'exit 1')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
