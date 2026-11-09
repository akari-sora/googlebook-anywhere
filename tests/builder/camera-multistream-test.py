#!/usr/bin/env python3
"""Apply the product 0015 patch to pinned fixtures and exercise real HAL blocks."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--patch', type=Path, help='patch to test (default: product 0015)')
args = parser.parse_args()
patch = args.patch or HERE.parents[1] / 'builder/camera/patches/0015-camera-multistream.patch'
patch = patch.resolve(strict=True)
with tempfile.TemporaryDirectory(prefix='camera-multistream-') as directory:
    root = Path(directory)
    shutil.copytree(HERE / 'fixtures/camera-after-0014', root, dirs_exist_ok=True)
    shutil.copytree(HERE / 'camera-multistream-cases', root / 'tests')
    with patch.open('rb') as data:
        subprocess.run(['patch', '--batch', '--forward', '-s', '-p1', '-d', str(root)], stdin=data, check=True)
    for case in sorted((root / 'tests').glob('*_test.py')):
        subprocess.run(['python3', str(case)], check=True)
    # The patch must not silently apply to a second time.
    with patch.open('rb') as data:
        again = subprocess.run(['patch', '--dry-run', '--forward', '-s', '-p1', '-d', str(root)],
                               stdin=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert again.returncode != 0

    if args.patch is None:
        # Exercise the builder's actual idempotence, incompatibility and hash guards.
        script = (HERE.parents[1] / 'builder/camera/build-libcamera.sh').read_text()
        digest = hashlib.sha256(patch.read_bytes()).hexdigest()
        checksum = next(line for line in script.splitlines()
                        if digest + '  $HERE/patches/' + patch.name in line)
        start = script.index('for p in "$HERE"/patches/*.patch; do')
        loop = script[start:script.index('\n# libyuv (static', start)]
        builder = root / 'builder'
        (builder / 'patches').mkdir(parents=True)
        target = builder / 'patches' / patch.name
        shutil.copyfile(patch, target)
        env = {**os.environ, 'HERE': str(builder), 'S': str(root)}
        def apply():
            return subprocess.run(['bash', '-e', '-c', loop], env=env,
                                  capture_output=True, text=True)
        assert apply().returncode == 0
        device = root / 'src/android/camera_device.cpp'
        source = device.read_text()
        assert 'bool consolidateJpegNv12(' in source
        device.write_text(source.replace('bool consolidateJpegNv12(',
                                         'bool incompatibleConsolidation(', 1))
        rejected = apply()
        assert rejected.returncode != 0 and '0015 camera multistream patch is incompatible' in rejected.stderr
        if sys.platform == 'darwin':
            checksum = checksum.replace('sha256sum -c --quiet', 'shasum -a 256 -c')
        def check():
            return subprocess.run(['bash', '-c', checksum], env=env,
                                  capture_output=True).returncode
        assert check() == 0
        target.write_bytes(target.read_bytes() + b'tampered')
        assert check() != 0
        target.unlink()
        assert check() != 0
print('camera multistream simulation PASS')
