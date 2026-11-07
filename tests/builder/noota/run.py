#!/usr/bin/env python3
"""Run the native mock Binder lifecycle test with a bounded wall time."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
compiler = os.environ.get('CXX', 'c++')
if not shutil.which(compiler):
    print('SKIP no C++ compiler available')
    sys.exit(77)
deadline = time.monotonic() + 30
try:
    with tempfile.TemporaryDirectory(prefix='gbos-noota-test-') as tmp:
        binary = Path(tmp) / 'test'
        compile_cmd = [compiler, '-std=c++17', '-pthread', '-I', str(HERE / 'include'),
                       '-I', str(ROOT / 'builder/noota'), str(HERE / 'test.cpp'), '-o', str(binary)]
        if sys.platform.startswith('linux'):
            compile_cmd.append('-ldl')
        subprocess.run(compile_cmd, check=True, timeout=max(.001, deadline - time.monotonic()))
        subprocess.run([str(binary)], check=True, timeout=max(.001, deadline - time.monotonic()))
except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
    print(f'FAIL no-OTA lifecycle: {exc}', file=sys.stderr)
    sys.exit(1)
