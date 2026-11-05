#!/usr/bin/env python3
"""Opt-in root Linux test: creates, checks and detaches only its own disposable loop."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('shared_block_loop', ROOT / 'install/stick/shared-source.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@unittest.skipUnless(os.environ.get('GBOS_TEST_BLOCK_LOOP') == '1' and os.geteuid() == 0
                     and all(shutil.which(x) for x in ('losetup', 'mformat', 'mcopy', 'mmd', 'mdir', 'minfo', 'lz4')),
                     'requires explicit GBOS_TEST_BLOCK_LOOP=1, root and Linux loop/mtools/lz4')
class LoopIntegration(unittest.TestCase):
    def test_owned_loop_is_read_only_and_alias_refused(self):
        with tempfile.TemporaryDirectory(prefix='gbos-block-fixture-', dir=os.environ.get('GBOS_TEST_LOOP_ROOT')) as tmp:
            root = Path(tmp).resolve()
            tree = ast.parse((ROOT / 'tests/update/selftest.py').read_text())
            names = {'rnd', 'sh', 'gpt_bytes', 'lp_super', 'newc', 'bootconfig', 'avb', 'make_release'}
            constants = {'LAYOUT', 'ESP_T', 'LIN_T', 'VBM_T', 'DYN', 'LIFT', 'OPTS'}
            nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names or
                     isinstance(n, ast.Assign) and len(n.targets) == 1 and
                     isinstance(n.targets[0], ast.Name) and n.targets[0].id in constants]
            ns = {'Path': Path, 'W': root, 'MIB': 1 << 20, 'os': os, 'random': random,
                  'struct': struct, 'uuid': uuid, 'zlib': zlib, 'hashlib': hashlib,
                  'subprocess': subprocess, 'ENV': dict(os.environ, MTOOLS_SKIP_CHECK='1')}
            exec(compile(ast.Module(body=nodes, type_ignores=[]), '<fixture-definitions>', 'exec'), ns)
            image = root / 'source.img'
            target = root / 'target.img'
            ns['make_release'](image, model='KANO', plain=False)
            ns['make_release'](target, model='KANO', plain=False)
            payload = {'profile': 'kano', 'build_id': '16452207',
                       'image': {'path': image.name, 'size': image.stat().st_size,
                                 'sha256': hashlib.sha256(image.read_bytes()).hexdigest()},
                       'boot_entries': ['lapis-debug.conf', 'lapis-quiet.conf'], 'hardware_validation': 'unvalidated'}
            raw = json.dumps({'schema': 1, 'payloads': [payload]}).encode()
            (root / 'catalog.json').write_bytes(raw)
            dmi = root / 'dmi'
            dmi.mkdir()
            (dmi / 'sys_vendor').write_text('Google\n')
            (dmi / 'product_name').write_text('Kano\n')
            before = hashlib.sha256(target.read_bytes()).hexdigest()
            loop = subprocess.check_output(['losetup', '--find', '--show', str(target)], text=True).strip()
            try:
                result = m.plan_observed_block(str(root), 'catalog.json', hashlib.sha256(raw).hexdigest(), loop, dmi)
                self.assertEqual(result['result'], 'PLAN_OBSERVED')
                self.assertFalse(result['write_allowed'])
                self.assertIn('NOT_GLOBALLY_EXCLUSIVE', result['limitations'])
                self.assertEqual(before, hashlib.sha256(target.read_bytes()).hexdigest())
                payload['image'] = {'path': target.name, 'size': target.stat().st_size, 'sha256': before}
                raw = json.dumps({'schema': 1, 'payloads': [payload]}).encode()
                (root / 'catalog.json').write_bytes(raw)
                with self.assertRaisesRegex(ValueError, 'aliases loop backing'):
                    m.plan_observed_block(str(root), 'catalog.json', hashlib.sha256(raw).hexdigest(), loop, dmi)
            finally:
                subprocess.run(['losetup', '--detach', loop], check=True)


if __name__ == '__main__':
    unittest.main()
