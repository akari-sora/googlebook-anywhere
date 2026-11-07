#!/usr/bin/env python3
"""Exercise the actual 0011 patch loop on fresh, applied and incompatible inputs."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'builder/camera/patches/0011-config-diagnostics.patch'
SCRIPT = ROOT / 'builder/camera/build-libcamera.sh'


def original_files():
    # Recreate the original hunk context from the published patch itself.
    files = {}
    name = None
    for line in PATCH.read_text().splitlines(keepends=True):
        if line.startswith('--- a/'):
            name = line[6:].strip()
            files[name] = []
        elif line.startswith(('+++ ', '@@')):
            continue
        elif line.startswith((' ', '-')):
            files[name].append(line[1:])
    return files


class PatchApplication(unittest.TestCase):
    def test_forward_reverse_and_refusal(self):
        text = SCRIPT.read_text()
        start = text.index('for p in "$HERE"/patches/*.patch; do')
        end = text.index('\n# libyuv (static', start)
        loop = text[start:end]
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            for name, lines in original_files().items():
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(''.join(lines))
            def run():
                return subprocess.run(['bash', '-e', '-c', loop],
                    env={'HERE': str(ROOT / 'builder/camera'), 'S': str(source), 'PATH': '/usr/bin:/bin'},
                    capture_output=True, text=True)
            fresh = run()
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            content = {p: p.read_bytes() for p in source.rglob('*') if p.is_file()}
            applied = run()
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(content, {p: p.read_bytes() for p in content})
            target = source / 'src/android/camera_capabilities.cpp'
            target.write_text(''.join(original_files()['src/android/camera_capabilities.cpp']))
            partial = run()
            self.assertNotEqual(partial.returncode, 0)
            self.assertIn('0011 camera diagnostics patch is incompatible', partial.stderr)
            target.write_text('incompatible source\n')
            bad = run()
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn('0011 camera diagnostics patch is incompatible', bad.stderr)


if __name__ == '__main__':
    unittest.main()
