#!/usr/bin/env python3
"""Exercise the 0013 patch, repeat/refusal, and its actual timing helpers."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'builder/camera/patches/0013-fixed-sensor-duration.patch'
SCRIPT = ROOT / 'builder/camera/build-libcamera.sh'
EXPECTED = '35ba3c3fa732b5b9c097ca22298d25c81f3e858a37f41fe649f56a5c47b551e1'



class FixedDuration(unittest.TestCase):
    def test_pin_rejects_changed_and_missing_patch(self):
        line = next(line for line in SCRIPT.read_text().splitlines()
                    if EXPECTED in line and 'sha256sum -c --quiet' in line)
        if sys.platform == 'darwin':
            line = line.replace('sha256sum -c --quiet', 'shasum -a 256 -c')
        with tempfile.TemporaryDirectory() as tmp:
            builder = Path(tmp)
            (builder / 'patches').mkdir()
            target = builder / 'patches' / PATCH.name
            shutil.copyfile(PATCH, target)
            def check():
                return subprocess.run(['bash', '-c', line],
                                      env={**os.environ, 'HERE': str(builder)},
                                      capture_output=True)
            self.assertEqual(check().returncode, 0)
            target.write_bytes(target.read_bytes() + b'tampered')
            self.assertNotEqual(check().returncode, 0)
            target.unlink()
            self.assertNotEqual(check().returncode, 0)

    def test_patch_loop_and_helpers(self):
        self.assertEqual(hashlib.sha256(PATCH.read_bytes()).hexdigest(), EXPECTED)
        text = SCRIPT.read_text()
        self.assertIn(EXPECTED + '  $HERE/patches/0013-fixed-sensor-duration.patch', text)
        start = text.index('for p in "$HERE"/patches/*.patch; do')
        loop = text[start:text.index('\n# libyuv (static', start)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            builder = root / 'builder'
            (builder / 'patches').mkdir(parents=True)
            for name in ('0011-config-diagnostics.patch', '0012-camera-probe-colorspace.patch', PATCH.name):
                shutil.copy2(PATCH.parent / name, builder / 'patches' / name)
            shutil.copytree(ROOT / 'tests/builder/fixtures/camera-after-0010', source)
            env = {**os.environ, 'HERE': str(builder), 'S': str(source)}
            def apply():
                return subprocess.run(['bash', '-e', '-c', loop], env=env,
                                      capture_output=True, text=True)
            first = apply()
            self.assertEqual(first.returncode, 0, first.stderr)
            second = apply()
            self.assertEqual(second.returncode, 0, second.stderr)
            capabilities = (source / 'src/android/camera_capabilities.cpp').read_text()
            self.assertIn('Camera init stage=stream-configurations ret=', capabilities)
            self.assertEqual(capabilities.count('cfg.colorSpace.reset();'), 2)
            simple = source / 'src/libcamera/pipeline/simple/simple.cpp'
            self.assertIn('ipa::soft::IPAConfigInfo configInfo{};', simple.read_text())
            test = root / 'timing.cpp'
            test.write_text('''#include "src/libcamera/pipeline/simple/fixed_frame_duration.h"
#include "src/android/fixed_frame_rate.h"
#include <cassert>
#include <cstdint>
int main() {
 int32_t blank = -1; int64_t duration = 0;
 assert(libcamera::fixedFrameTiming(1920,1000,240,72000000,10,1000,1,&blank,&duration));
 assert(blank == 111 && duration == 33330);
 assert(libcamera::fixedFrameTiming(1920,1000,240,72000000,10,1000,8,&blank,&duration));
 assert(blank == 106 && duration == 33180);
 assert(!libcamera::fixedFrameTiming(1920,1000,240,72000000,112,1000,1,&blank,&duration));
 assert(!libcamera::fixedFrameTiming(1920,1000,240,72000000,10,1000,0,&blank,&duration));
 assert(!libcamera::frameDuration(INT64_MAX,INT64_MAX,INT64_MAX,1,INT64_MAX,&duration));
 assert(nominalMaxFps(33330000,true) == 30);
 assert(nominalMinFps(33330000,30,true) == 30);
 assert(nominalMinFps(33330000,30,false) == 31);
 assert(nominalMaxFps(34000000,true) == 29);
 assert(nominalMinFps(32000000,30,true) == 32);
}
''')
            binary = root / 'timing'
            subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=undefined', '-fno-sanitize-recover=undefined',
                            '-I', str(source), str(test), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)
            simple.write_text(simple.read_text().replace('int SimpleCameraData::configureFixedFrameDuration()',
                                                         'int incompatible()', 1))
            bad = apply()
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn('0013 fixed sensor duration patch is incompatible', bad.stderr)


if __name__ == '__main__':
    unittest.main()
