#!/usr/bin/env python3
"""Private miniature GPT/LP/FAT update interrupted by a source mutation.

Linux only: requires O_PATH, mtools and lz4. This never opens a block device.
"""
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
from unittest.mock import patch
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('shared_preview_test', ROOT / 'install/stick/shared-source.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@unittest.skipUnless(hasattr(os, 'O_PATH') and all(shutil.which(x) for x in
                     ('mformat', 'mmd', 'mcopy', 'mdir', 'mdel', 'minfo', 'lz4')),
                     'requires Linux O_PATH, mtools and lz4')
class MutationIntegration(unittest.TestCase):
    def test_mutation_after_first_target_write_stops_before_menu_switch(self):
        engine = m.update
        with tempfile.TemporaryDirectory(prefix='gbos-shared-mutation-') as tmp:
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
            source, target = root / 'source.img', root / 'target.img'
            ns['make_release'](source, model='KANO', plain=False, vendor='v2')
            ns['make_release'](target, model='KANO', plain=False, vendor='v1')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            catalog = {'schema': 1, 'payloads': [{'profile': 'kano', 'build_id': '16452207',
                       'image': {'path': source.name, 'size': source.stat().st_size, 'sha256': digest},
                       'boot_entries': ['lapis-debug.conf', 'lapis-quiet.conf'],
                       'hardware_validation': 'unvalidated'}]}
            raw = json.dumps(catalog).encode()
            (root / 'catalog.json').write_bytes(raw)
            work = root / 'work'
            work.mkdir()
            def ranges_hash(disk, ranges):
                h = hashlib.sha256()
                for off, length in ranges:
                    for start in range(0, length, 1048576):
                        h.update(disk.pread(off + start, min(1048576, length - start)))
                return h.hexdigest()
            with m.shared_release(str(root), 'catalog.json', hashlib.sha256(raw).hexdigest(),
                                  'kano', str(work)) as (release, binding):
                disk = engine.Disk(str(target), write=True)
                try:
                    self.assertFalse(disk.blk)
                    original = engine.Installed(disk, str(work))
                    gpt = original.gpt
                    keep = [(gpt[p + '_a']['start'], gpt[p + '_a']['len']) for p in engine.SLOT_PHYS]
                    keep.append((gpt['userdata']['start'], gpt['userdata']['len']))
                    old_keep = ranges_hash(disk, keep)
                    old_dynamic = {p['name']: (list(p['extents']), ranges_hash(disk,
                        [(original.super['start'] + start * 512, count * 512) for start, count in p['extents']]))
                        for p in original.lp.parts if p['name'].endswith('_a')}
                    boot_paths = {'/loader/loader.conf'}
                    for entry in engine.REQUIRED_ENTRIES:
                        self.assertEqual(original.slot_of[entry], original.current)
                        boot_paths.add('/loader/entries/' + entry)
                        boot_paths.add(original.entries[entry]['linux'])
                        boot_paths.update(original.entries[entry]['initrd'])
                    old_boot = {path: original.fat.read(path) for path in boot_paths}
                    dynamic = list(release.dyn.items())
                    self.assertGreaterEqual(len(dynamic), 2)
                    first_name, first_part = dynamic[0]
                    plan = engine.check(original, release, 'nvme')
                    target_ranges = [(original.super['start'] + start * 512, count * 512)
                                     for start, count in plan['extents'][first_name + '_b']]
                    first_before = ranges_hash(disk, target_ranges)
                    expected = hashlib.sha256()
                    for start, count in first_part['extents']:
                        expected.update(release.super_rd(start * 512, count * 512))
                    mutation = release.layout['super']['start'] + dynamic[1][1]['extents'][0][0] * 512
                    first_write = engine.write_stream
                    writes = []
                    def change_source_after_write(*args):
                        result = first_write(*args)
                        writes.append(True)
                        if len(writes) == 1:
                            fd = os.open(source, os.O_RDWR)
                            try:
                                value = os.pread(fd, 1, mutation)
                                self.assertEqual(len(value), 1)
                                os.pwrite(fd, bytes([value[0] ^ 0xff]), mutation)
                            finally:
                                os.close(fd)
                        return result
                    with patch.object(engine, 'write_stream', side_effect=change_source_after_write):
                        with self.assertRaisesRegex(engine.Failed, 'shared source chunk') as failure:
                            engine.apply(disk, release, 'nvme', False, None, str(work), binding)
                    self.assertIsInstance(failure.exception.__cause__, engine.Refuse)
                    self.assertEqual(len(writes), 1)
                    self.assertNotEqual(first_before, expected.hexdigest())
                    self.assertEqual(ranges_hash(disk, target_ranges), expected.hexdigest())
                    self.assertTrue(engine.WRITING)
                    after = engine.Installed(disk, str(work))
                    self.assertEqual(after.gpt, gpt)
                    self.assertEqual(after.current, original.current)
                    self.assertIsNotNone(after.shared_digest)
                    self.assertIn('shared-version 1', after.state_original)
                    self.assertEqual(old_keep, ranges_hash(disk, keep))
                    for name, (extents, old_hash) in old_dynamic.items():
                        self.assertEqual(after.lp.part(name)['extents'], extents)
                        self.assertEqual(old_hash, ranges_hash(disk,
                            [(after.super['start'] + start * 512, count * 512) for start, count in extents]))
                    for path, data in old_boot.items():
                        self.assertEqual(after.fat.read(path), data)
                finally:
                    os.close(disk.fd)
                    engine.WRITING.clear()
                    engine.WINDOW.clear()
                    engine.STICK.clear()


if __name__ == '__main__':
    unittest.main()
