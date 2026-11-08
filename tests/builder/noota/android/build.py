#!/usr/bin/env python3
"""Build a QEMU-only Binder probe jar; never stage or run it."""
import argparse
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--sdk', type=Path, required=True)
    p.add_argument('--jdk', type=Path, required=True)
    p.add_argument('--build-tools', required=True)
    p.add_argument('--api', type=int, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,2}', a.build_tools) or not 34 <= a.api <= 999:
        p.error('invalid build-tools or API')
    android = a.sdk / 'platforms' / f'android-{a.api}' / 'android.jar'
    javac = a.jdk / 'bin' / 'javac'
    d8 = a.sdk / 'build-tools' / a.build_tools / 'd8'
    for f in (android, javac, d8):
        if not f.is_file():
            raise ValueError('missing build tool or platform')
    if os.path.lexists(a.out):
        raise FileExistsError('output already exists')
    env = dict(os.environ, JAVA_HOME=str(a.jdk.resolve()))
    env['PATH'] = str(a.jdk.resolve() / 'bin') + os.pathsep + env.get('PATH', '')
    source = Path(__file__).resolve().with_name('Probe.java')
    with tempfile.TemporaryDirectory(prefix='noota-probe-') as t:
        root = Path(t); classes = root / 'classes'; dex = root / 'dex'
        classes.mkdir(); dex.mkdir()
        subprocess.run([str(javac), '--release', '8', '-cp', str(android), '-d', str(classes), str(source)], check=True, timeout=120, env=env)
        inputs = sorted((classes / 'org/googlebook/noota').glob('*.class'))
        if not inputs:
            raise ValueError('no compiled classes')
        subprocess.run([str(d8), '--lib', str(android), '--min-api', '34', '--output', str(dex), *map(str, inputs)], check=True, timeout=120, env=env)
        entries = list(dex.iterdir())
        if len(entries) != 1 or entries[0].name != 'classes.dex' or not entries[0].is_file() or entries[0].stat().st_size == 0:
            raise ValueError('unexpected D8 output')
        buf = io.BytesIO()
        entry = zipfile.ZipInfo('classes.dex', (1980, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_STORED; entry.create_system = 3; entry.external_attr = 0o100644 << 16
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr(entry, entries[0].read_bytes())
        with a.out.open('xb') as out:
            out.write(buf.getvalue())


if __name__ == '__main__':
    main()
