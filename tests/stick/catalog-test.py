#!/usr/bin/env python3
"""Synthetic offline catalog checks; never reads devices or runs a writer."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('gbos_catalog', ROOT / 'install/stick/catalog.py')
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'payloads').mkdir()
        self.contents = {'kano': b'kano image', 'amd-hx370': b'hx image'}
        for profile, raw in self.contents.items():
            (self.root / 'payloads' / (profile + '.img')).write_bytes(raw)
        self.obj = {'schema': 1, 'payloads': [self.item(x) for x in self.contents]}
        self.save()

    def item(self, profile):
        data = self.contents[profile]
        return {'profile': profile, 'build_id': 'build-1',
                'image': {'path': 'payloads/' + profile + '.img', 'size': len(data), 'sha256': sha(data)},
                'boot_entries': ['safe', 'serial debug'], 'hardware_validation': 'unvalidated'}

    def save(self, raw=None):
        raw = json.dumps(self.obj).encode() if raw is None else raw
        (self.root / 'catalog.json').write_bytes(raw)
        self.digest = sha(raw)

    def validate(self):
        return catalog.validate(str(self.root), 'catalog.json', self.digest)

    def denied(self):
        with self.assertRaises((catalog.Refused, OSError, ValueError)):
            with self.validate():
                pass

    def test_two_profiles_hold_file_descriptors_and_never_allow_writes(self):
        with self.validate() as result:
            self.assertEqual(set(result.descriptors), {'kano', 'amd-hx370'})
            self.assertEqual(os.read(result.descriptors['kano'], 64), b'kano image')
            self.assertFalse(result.report()['write_allowed'])
            self.assertEqual(result.report()['boot_entries'], 'EXPECTATIONS_ONLY')
            self.assertEqual(result.report()['hardware'], 'NOT_VERIFIED')
            fd = result.descriptors['kano']
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_three_profiles_and_ally_corruption_are_checked_offline(self):
        self.contents['rog-ally'] = b'ally image'
        path = self.root / 'payloads/rog-ally.img'
        path.write_bytes(self.contents['rog-ally'])
        self.obj['payloads'].append(self.item('rog-ally'))
        self.save()
        with self.validate() as result:
            self.assertEqual(set(result.descriptors), {'kano', 'amd-hx370', 'rog-ally'})
            self.assertFalse(result.report()['write_allowed'])
            self.assertEqual(result.report()['hardware'], 'NOT_VERIFIED')
        path.write_bytes(b'wrong data')
        self.denied()

    def test_bad_independent_catalog_digest_even_with_matching_adjacent_digest(self):
        (self.root / 'catalog.json.sha256').write_text(self.digest)
        with self.assertRaises(catalog.Refused):
            catalog.validate(self.root, 'catalog.json', '0' * 64)

    def test_payload_corruption_size_missing_and_swap(self):
        path = self.root / 'payloads/kano.img'
        path.write_bytes(b'X' * len(self.contents['kano']))
        self.denied()
        path.write_bytes(self.contents['kano'] + b'!')
        self.denied()
        path.unlink()
        self.denied()
        path.write_bytes(self.contents['amd-hx370'])
        self.denied()

    def test_schema_unknown_duplicate_or_bad_types(self):
        for mutation in ('unknown', 'profile', 'duplicate_profile', 'image_field', 'duplicate_entries',
                         'bad_size', 'status'):
            with self.subTest(mutation=mutation):
                self.obj = {'schema': 1, 'payloads': [self.item(x) for x in self.contents]}
                first = self.obj['payloads'][0]
                if mutation == 'unknown':
                    self.obj['write_allowed'] = True
                if mutation == 'profile':
                    first['profile'] = 'other'
                if mutation == 'duplicate_profile':
                    self.obj['payloads'][1]['profile'] = first['profile']
                if mutation == 'image_field':
                    first['image']['secret'] = 'x'
                if mutation == 'duplicate_entries':
                    first['boot_entries'] = ['safe', 'safe']
                if mutation == 'bad_size':
                    first['image']['size'] = True
                if mutation == 'status':
                    first['hardware_validation'] = 'validated'
                self.save()
                self.denied()
        self.save(b'{"schema":1,"schema":1,"payloads":[]}')
        self.denied()
        self.save(b'{"schema":1,"payloads":[],"extra":NaN}')
        self.denied()

    def test_path_traversal_absolute_and_components(self):
        for path in ('../x', '/etc/passwd', 'payloads/../kano.img', 'payloads//kano.img',
                     'payloads/./kano.img', 'payloads\\x.img', '.hidden', 'payloads/'):
            with self.subTest(path=path):
                self.obj['payloads'][0]['image']['path'] = path
                self.save()
                self.denied()

    def test_symlinks_at_every_level_and_catalog(self):
        (self.root / 'alias').symlink_to('payloads', target_is_directory=True)
        self.obj['payloads'][0]['image']['path'] = 'alias/kano.img'
        self.save()
        self.denied()
        self.obj['payloads'][0]['image']['path'] = 'payloads/link.img'
        (self.root / 'payloads/link.img').symlink_to('kano.img')
        self.save()
        self.denied()
        (self.root / 'catlink').symlink_to('catalog.json')
        with self.assertRaises(OSError):
            catalog.validate(self.root, 'catlink', self.digest)

    def test_replacement_during_read_and_same_inode_mutation(self):
        for change in ('replace', 'mutate'):
            with self.subTest(change=change):
                target = self.root / 'payloads/kano.img'
                target.write_bytes(self.contents['kano'])
                original_read = os.read
                fired = False
                def altered(fd, amount):
                    nonlocal fired
                    data = original_read(fd, amount)
                    if not fired and data == self.contents['kano']:
                        fired = True
                        if change == 'replace':
                            (self.root / 'payloads/new.img').write_bytes(self.contents['kano'])
                            os.replace(self.root / 'payloads/new.img', target)
                        else:
                            target.write_bytes(b'X' * len(self.contents['kano']))
                    return data
                with mock.patch.object(catalog.os, 'read', side_effect=altered):
                    self.denied()
                self.assertTrue(fired)

    def test_catalog_mutation_during_read_is_refused_before_parse(self):
        original = os.read
        fired = False
        def alter(fd, amount):
            nonlocal fired
            data = original(fd, amount)
            if not fired and data.startswith(b'{"schema"'):
                fired = True
                with (self.root / 'catalog.json').open('ab') as f:
                    f.write(b' ')
            return data
        with mock.patch.object(catalog.os, 'read', side_effect=alter):
            self.denied()
        self.assertTrue(fired)

    def test_cli_output_no_paths_and_no_detector_dependency(self):
        cmd = [sys.executable, str(ROOT / 'install/stick/catalog.py'), '--root', str(self.root),
               '--catalog', 'catalog.json', '--sha256', self.digest]
        env = dict(os.environ, PATH='/nonexistent')
        good = subprocess.run(cmd, text=True, capture_output=True, env=env)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertNotIn(str(self.root), good.stdout + good.stderr)
        self.assertFalse(json.loads(good.stdout)['write_allowed'])
        (self.root / 'payloads/kano.img').unlink()
        bad = subprocess.run(cmd, text=True, capture_output=True, env=env)
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn(str(self.root), bad.stdout + bad.stderr)
        self.assertFalse(json.loads(bad.stdout)['write_allowed'])


if __name__ == '__main__':
    unittest.main()
