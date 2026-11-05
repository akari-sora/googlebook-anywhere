#!/usr/bin/env python3
"""Linux-only miniature GPT/LP/ESP integration; requires mtools and lz4."""
import ast
import hashlib
import importlib.util
import itertools
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
spec = importlib.util.spec_from_file_location('preview', ROOT / 'install/stick/shared-source.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@unittest.skipUnless(hasattr(os, 'O_PATH') and all(shutil.which(x) for x in ('mformat', 'mmd', 'mcopy', 'mdir', 'mdel', 'minfo', 'lz4')),
                     'requires Linux O_PATH, mtools and lz4')
class Integration(unittest.TestCase):
    def test_real_parser_both_profiles_and_installed_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Extract only the existing fixture definitions; never execute its top-level suite.
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
            payloads = []
            for profile, model in [('kano', 'KANO'), ('amd-hx370', 'AMD')]:
                path = root / (profile + '.img')
                ns['make_release'](path, model=model, plain=False)
                payloads.append({'profile': profile, 'build_id': '16452207',
                    'image': {'path': path.name, 'size': path.stat().st_size,
                              'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                    'boot_entries': ['lapis-debug.conf', 'lapis-quiet.conf'], 'hardware_validation': 'unvalidated'})
            raw = json.dumps({'schema': 1, 'payloads': payloads}).encode()
            (root / 'catalog.json').write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            for profile in ('kano', 'amd-hx370'):
                target = root / (profile + '.img')
                before = hashlib.sha256(target.read_bytes()).hexdigest()
                result = m.preview(str(root), 'catalog.json', digest, profile, str(target))
                self.assertTrue(result['compatible_synthetic_target'])
                self.assertFalse(result['write_allowed'])
                self.assertEqual(before, hashlib.sha256(target.read_bytes()).hexdigest())


    def test_observed_plan_both_profiles_on_large_regular_targets(self):
        with tempfile.TemporaryDirectory(prefix='gbos-observed-plan-test-') as tmp:
            root = Path(tmp)
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
            payloads = []
            for profile, model in [('kano', 'KANO'), ('amd-hx370', 'AMD')]:
                path = root / (profile + '.img')
                ns['make_release'](path, model=model, plain=False)
                payloads.append({'profile': profile, 'build_id': '16452207',
                    'image': {'path': path.name, 'size': path.stat().st_size,
                              'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                    'boot_entries': ['lapis-debug.conf', 'lapis-quiet.conf'], 'hardware_validation': 'unvalidated'})
            raw = json.dumps({'schema': 1, 'payloads': payloads}).encode()
            (root / 'catalog.json').write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            ns['LAYOUT'] = [(name, 160 if name == 'userdata' else size) for name, size in ns['LAYOUT']]
            for profile, model, identity in [
                ('kano', 'KANO', {'sys_vendor': 'Google', 'product_name': 'Kano'}),
                ('amd-hx370', 'AMD', {'sys_vendor': 'ONE-NETBOOK', 'product_name': 'ONEXPLAYER X1Pro',
                  'board_vendor': 'ONE-NETBOOK', 'board_name': 'ONEXPLAYER X1Pro'})]:
                target = root / (profile + '-target.img')
                ns['make_release'](target, model=model, plain=False)
                self.assertGreater(target.stat().st_size, 128 * 1024 * 1024)
                before = hashlib.sha256(target.read_bytes()).hexdigest()
                dmi = root / (profile + '-dmi')
                dmi.mkdir()
                for key, value in identity.items():
                    (dmi / key).write_text(value + '\n')
                plan = m.plan_observed_image(str(root), 'catalog.json', digest, str(target), dmi)
                self.assertEqual(plan['profile'], profile)
                self.assertEqual(plan['result'], 'PLAN_OBSERVED')
                self.assertFalse(plan['write_allowed'])
                self.assertIn('IN_USE_NOT_VERIFIED', plan['limitations'])
                self.assertEqual(before, hashlib.sha256(target.read_bytes()).hexdigest())
                with self.assertRaises((ValueError, m.update.Refuse)):
                    m.plan_observed_image(str(root), 'catalog.json', digest, str(root / (profile + '.img')), dmi)

    def test_catalog_reader_apply_only_to_private_synthetic_fixtures(self):
        # The writer exercised here is the existing regular-file test engine. No
        # path is accepted from a caller, and neither /dev nor a block device is used.
        with tempfile.TemporaryDirectory(prefix='gbos-synthetic-update-') as tmp:
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
            engine = m.update
            def sha(path):
                h = hashlib.sha256()
                with path.open('rb') as f:
                    for data in iter(lambda: f.read(1024 * 1024), b''):
                        h.update(data)
                return h.hexdigest()
            def ranges_hash(disk, ranges):
                h = hashlib.sha256()
                for off, size in ranges:
                    for start in range(0, size, 1024 * 1024):
                        h.update(disk.pread(off + start, min(1024 * 1024, size - start)))
                return h.hexdigest()
            def assert_release_bytes(disk, result, release):
                # Compare the target slot to the actual selected catalog release.
                for name in engine.SLOT_PHYS:
                    expected = hashlib.sha256()
                    for chunk in release.read_phys(name + '_a'):
                        expected.update(chunk)
                    part = result.gpt[name + '_b']
                    self.assertEqual(expected.hexdigest(), ranges_hash(disk, [(part['start'], part['len'])]))
                for name, source_part in release.dyn.items():
                    expected = hashlib.sha256()
                    expected_bytes = 0
                    for sector, count in source_part['extents']:
                        size = count * engine.SECTOR
                        expected_bytes += size
                        for offset in range(0, size, 1024 * 1024):
                            expected.update(release.super_rd(sector * engine.SECTOR + offset,
                                                             min(1024 * 1024, size - offset)))
                    target_part = result.lp.part(name + '_b')
                    self.assertIsNotNone(target_part)
                    self.assertEqual(expected_bytes, sum(count * engine.SECTOR for _, count in target_part['extents']))
                    actual = [(result.super['start'] + sector * engine.SECTOR, count * engine.SECTOR)
                              for sector, count in target_part['extents']]
                    self.assertEqual(expected.hexdigest(), ranges_hash(disk, actual))

            def open_target(path):
                # These paths are freshly created below, in this test's private dir.
                self.assertEqual(path.parent, root)
                self.assertTrue(path.is_file() and not path.is_symlink())
                disk = engine.Disk(str(path), write=True)
                self.assertFalse(disk.blk)
                return disk

            items = []
            for profile, model in [('kano', 'KANO'), ('amd-hx370', 'AMD')]:
                source = root / (profile + '.img')
                ns['make_release'](source, model=model, plain=False, vendor='v2')
                items.append({'profile': profile, 'build_id': '16452207',
                    'image': {'path': source.name, 'size': source.stat().st_size, 'sha256': sha(source)},
                    'boot_entries': ['lapis-debug.conf', 'lapis-quiet.conf'], 'hardware_validation': 'unvalidated'})
            raw = json.dumps({'schema': 1, 'payloads': items}).encode()
            (root / 'catalog.json').write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            original_sources = {i['profile']: sha(root / i['image']['path']) for i in items}
            with m.catalog.validate(str(root), 'catalog.json', digest) as verified:
                for item in items:
                    profile = item['profile']
                    model = 'KANO' if profile == 'kano' else 'AMD'
                    work = root / ('work-' + profile)
                    work.mkdir()
                    reader = m.Reader(verified.descriptors[profile], item['image']['size'])
                    rel = engine.image_release_from_reader(reader, str(work), 'synthetic fixture', validate_layout=True)
                    self.assertEqual(str(rel.build), item['build_id'])
                    self.assertEqual(sorted(rel.esp.ls('/loader/entries')), sorted(item['boot_entries']))
                    for label, target_model, target_key in [('model', 'OTHER', 'k1'), ('key', model, 'other')]:
                        target = root / (profile + '-' + label + '.img')
                        ns['make_release'](target, model=target_model, key=target_key, plain=False)
                        before = sha(target)
                        disk = open_target(target)
                        try:
                            with self.assertRaises(engine.Refuse) as refusal:
                                engine.apply(disk, rel, 'stick', False, None, str(work))
                            self.assertIn('release is for' if label == 'model' else 'another AVB key', str(refusal.exception))
                        finally:
                            os.close(disk.fd)
                        self.assertEqual(sha(target), before)
                        target.unlink()
                    target = root / (profile + '-target.img')
                    ns['make_release'](target, model=model, plain=False, vendor='v1')
                    disk = open_target(target)
                    try:
                        inst = engine.Installed(disk, str(work))
                        self.assertEqual(inst.current, 'a')
                        engine.check(inst, rel, 'stick')
                        boot = engine.boot_files(inst, rel, 'stick', str(work))
                        self.assertTrue(boot['fits'])
                        self.assertIn('prev-lapis-quiet.conf', boot['prev'])
                        physical = [(inst.gpt[p + '_a']['start'], inst.gpt[p + '_a']['len']) for p in engine.SLOT_PHYS]
                        original_phys = ranges_hash(disk, physical)
                        data = inst.gpt['userdata']
                        original_userdata = ranges_hash(disk, [(data['start'], data['len'])])
                        original_dynamic = {p['name']: (list(p['extents']), ranges_hash(disk,
                            [(inst.super['start'] + start * 512, count * 512) for start, count in p['extents']]))
                            for p in inst.lp.parts if p['name'].endswith('_a')}
                        engine.apply(disk, rel, 'stick', False, None, str(work))
                        result = engine.Installed(disk, str(work))
                        self.assertEqual(result.current, 'b')
                        self.assertEqual(result.slot_of.get('prev-lapis-quiet.conf'), 'a')
                        assert_release_bytes(disk, result, rel)
                        self.assertEqual(original_phys, ranges_hash(disk, physical))
                        self.assertEqual(original_userdata, ranges_hash(disk, [(data['start'], data['len'])]))
                        for name, (extents, expected) in original_dynamic.items():
                            part = result.lp.part(name)
                            self.assertEqual(part['extents'], extents)
                            self.assertEqual(expected, ranges_hash(disk,
                                [(result.super['start'] + start * 512, count * 512) for start, count in extents]))
                    finally:
                        os.close(disk.fd)
                        engine.WRITING.clear()
                        engine.WINDOW.clear()
                        engine.STICK.clear()
                    for source_item in items:
                        self.assertEqual(sha(root / source_item['image']['path']), original_sources[source_item['profile']])
                    self.assertEqual(reader.pread(0, 1), b'\0')  # catalog still owns selected fd
                    # Shared consistency is synthetic-only: stop after each durable
                    # phase and resume while the catalog descriptor remains live.
                    template = root / (profile + '-resume-template.img')
                    ns['make_release'](template, model=model, plain=False, vendor='v1')
                    with m.shared_release(str(root), 'catalog.json', digest, profile, str(work)) as (shared_rel, binding):
                        direct_file = root / (profile + '-nvme-direct.img')
                        shutil.copyfile(template, direct_file)
                        direct = open_target(direct_file)
                        try:
                            original = engine.Installed(direct, str(work))
                            gpt = original.gpt
                            data = gpt['userdata']
                            preserved = [(data['start'], data['len'])] + [
                                (gpt[p + '_a']['start'], gpt[p + '_a']['len']) for p in engine.SLOT_PHYS]
                            before = ranges_hash(direct, preserved)
                            engine.apply(direct, shared_rel, 'nvme', False, None, str(work), binding)
                            result = engine.Installed(direct, str(work))
                            self.assertEqual(result.current, 'b')
                            assert_release_bytes(direct, result, shared_rel)
                            self.assertEqual(result.slot_of.get('prev-lapis-quiet.conf'), 'a')
                            self.assertEqual(result.gpt, gpt)
                            self.assertEqual(before, ranges_hash(direct, preserved))
                            for entry in engine.REQUIRED_ENTRIES:
                                opts = result.entries[entry]['options'].split()
                                self.assertIn('initcall_blacklist=mei_me_driver_init', opts)
                                self.assertIn('efi=noruntime', opts)
                                self.assertNotIn(engine.LIFT, opts)
                        finally:
                            os.close(direct.fd)
                            engine.WRITING.clear()
                            engine.WINDOW.clear()
                            engine.STICK.clear()
                        direct_file.unlink()
                        for mode, phase in itertools.product(('stick', 'nvme'),
                                ('sharedstate', 'removed', 'dynamic', 'physical', 'metadata', 'bootfiles',
                                 'sharedmenu-lapis-quiet.conf', 'sharedmenu-lapis-debug.conf', 'sharedmenu-loader')):
                            fixture = root / (profile + '-resume.img')
                            shutil.copyfile(template, fixture)
                            synthetic = open_target(fixture)
                            try:
                                before_inst = engine.Installed(synthetic, str(work))
                                original_gpt = before_inst.gpt
                                userdata = before_inst.gpt['userdata']
                                keep = [(before_inst.gpt[p + '_a']['start'], before_inst.gpt[p + '_a']['len'])
                                        for p in engine.SLOT_PHYS]
                                keep.append((userdata['start'], userdata['len']))
                                before_keep = ranges_hash(synthetic, keep)
                                original_dynamic = {p['name']: (list(p['extents']), ranges_hash(synthetic,
                                    [(before_inst.super['start'] + start * engine.SECTOR, count * engine.SECTOR)
                                     for start, count in p['extents']]))
                                    for p in before_inst.lp.parts if p['name'].endswith('_a')}
                                boot_paths = {'/loader/loader.conf'}
                                for entry in engine.REQUIRED_ENTRIES:
                                    self.assertEqual(before_inst.slot_of[entry], before_inst.current)
                                    boot_paths.add('/loader/entries/' + entry)
                                    boot_paths.add(before_inst.entries[entry]['linux'])
                                    boot_paths.update(before_inst.entries[entry]['initrd'])
                                old_boot = {path: before_inst.fat.read(path) for path in boot_paths}
                                old_quiet = before_inst.entries['lapis-quiet.conf']
                                old_files = {path: old_boot[path] for entry in engine.REQUIRED_ENTRIES
                                             for path in [before_inst.entries[entry]['linux'], *before_inst.entries[entry]['initrd']]}
                                engine.FAIL_AT = phase
                                with self.assertRaises(engine.Failed):
                                    engine.apply(synthetic, shared_rel, mode, False, None, str(work), binding)
                                engine.FAIL_AT = ''
                                before_retry = sha(fixture)
                                interrupted = engine.Installed(synthetic, str(work))
                                state_before = interrupted.state_original
                                self.assertEqual(interrupted.gpt, original_gpt)
                                self.assertEqual(before_keep, ranges_hash(synthetic, keep))
                                for name, (extents, expected) in original_dynamic.items():
                                    self.assertEqual(interrupted.lp.part(name)['extents'], extents)
                                    self.assertEqual(expected, ranges_hash(synthetic,
                                        [(interrupted.super['start'] + start * engine.SECTOR, count * engine.SECTOR)
                                         for start, count in extents]))
                                for path, content in old_files.items():
                                    self.assertEqual(interrupted.fat.read(path), content)
                                if phase.startswith('sharedmenu-'):
                                    previous = interrupted.entries['prev-lapis-quiet.conf']
                                    self.assertEqual(interrupted.slot_of['prev-lapis-quiet.conf'], before_inst.current)
                                    self.assertEqual(previous['linux'], old_quiet['linux'])
                                    self.assertEqual(previous['initrd'], old_quiet['initrd'])
                                    self.assertEqual(previous['options'], old_quiet['options'])
                                else:
                                    self.assertEqual(interrupted.default, before_inst.default)
                                    for entry in engine.REQUIRED_ENTRIES:
                                        self.assertEqual(interrupted.slot_of[entry], before_inst.current)
                                    for path, content in old_boot.items():
                                        self.assertEqual(interrupted.fat.read(path), content)
                                self.assertIn('shared-version 1', state_before)
                                # Legacy resumes cannot consume the shared marker.
                                with self.assertRaises(engine.Refuse):
                                    engine.apply(synthetic, shared_rel, mode, False, None, str(work))
                                self.assertEqual(before_retry, sha(fixture))
                                if phase == 'sharedstate':
                                    with self.assertRaises(engine.Refuse):
                                        engine.apply(synthetic, shared_rel, 'nvme' if mode == 'stick' else 'stick', False, None, str(work), binding)
                                    self.assertEqual(before_retry, sha(fixture))
                                    with self.assertRaises(engine.Refuse):
                                        engine.apply(synthetic, shared_rel, mode, True, None, str(work), binding)
                                    self.assertEqual(before_retry, sha(fixture))
                                    # Corrupt and legacy marker variants are tested on fresh clones.
                                    for variant in ('duplicate', 'missing', 'malformed', 'legacy'):
                                        badfile = root / (profile + '-bad-state.img')
                                        shutil.copyfile(fixture, badfile)
                                        bad = open_target(badfile)
                                        try:
                                            state_inst = engine.Installed(bad, str(work))
                                            saved = state_inst.state_original
                                            if variant == 'duplicate':
                                                changed_state = saved + 'shared-version 1\n'
                                            elif variant == 'missing':
                                                changed_state = '\n'.join(line for line in saved.splitlines() if not line.startswith('shared-version ')) + '\n'
                                            elif variant == 'malformed':
                                                changed_state = saved.replace('shared-version 1', 'shared-version broken')
                                            else:
                                                changed_state = '\n'.join(line for line in saved.splitlines() if not line.startswith('shared-')) + '\n'
                                            state_inst.fat.write(engine.STATE, changed_state.encode())
                                            bad.sync_drop()
                                            bad_before = sha(badfile)
                                            with self.assertRaises(engine.Refuse):
                                                engine.apply(bad, shared_rel, mode, False, None, str(work), binding)
                                            self.assertEqual(bad_before, sha(badfile))
                                        finally:
                                            os.close(bad.fd)
                                        badfile.unlink()
                                    # Simulate interruption after the synced state step;
                                    # the saved record remains available for resumption.
                                    cancelled_path = root / (profile + '-cancel.img')
                                    shutil.copyfile(template, cancelled_path)
                                    cancelled = open_target(cancelled_path)
                                    try:
                                        def interrupt_after_state(name):
                                            if name.startswith('sharedstate:'):
                                                raise KeyboardInterrupt()
                                        with patch.object(engine, 'step', side_effect=interrupt_after_state):
                                            with self.assertRaises(KeyboardInterrupt):
                                                engine.apply(cancelled, shared_rel, mode, False, None, str(work), binding)
                                        self.assertIsNotNone(engine.Installed(cancelled, str(work)).shared_digest)
                                    finally:
                                        os.close(cancelled.fd)
                                    cancelled_path.unlink()
                                    with tempfile.TemporaryDirectory(dir=root) as other_tmp:
                                        other_profile = 'amd-hx370' if profile == 'kano' else 'kano'
                                        with m.shared_release(str(root), 'catalog.json', digest, other_profile, other_tmp) as (other_rel, other_binding):
                                            with self.assertRaises(engine.Refuse):
                                                engine.apply(synthetic, other_rel, mode, False, None, str(work), other_binding)
                                        self.assertEqual(before_retry, sha(fixture))
                                        # Same payload, independently hashed different catalog bytes.
                                        alt = b' ' + raw
                                        (root / 'alt-catalog.json').write_bytes(alt)
                                        with m.shared_release(str(root), 'alt-catalog.json', hashlib.sha256(alt).hexdigest(), profile, other_tmp) as (other_rel, other_binding):
                                            with self.assertRaises(engine.Refuse):
                                                engine.apply(synthetic, other_rel, mode, False, None, str(work), other_binding)
                                        self.assertEqual(before_retry, sha(fixture))
                                        # Same profile, but a distinct valid payload/catalog.
                                        alt_image = root / ('alternate-' + profile + '.img')
                                        shutil.copyfile(root / item['image']['path'], alt_image)
                                        with alt_image.open('r+b') as out:
                                            out.seek(-1, os.SEEK_END)
                                            out.write(b'X')
                                        altered_items = json.loads(raw)['payloads']
                                        alternate = next(x for x in altered_items if x['profile'] == profile)
                                        alternate['image']['path'] = alt_image.name
                                        alternate['image']['sha256'] = sha(alt_image)
                                        alt = json.dumps({'schema': 1, 'payloads': altered_items}).encode()
                                        (root / 'alt-catalog.json').write_bytes(alt)
                                        with m.shared_release(str(root), 'alt-catalog.json', hashlib.sha256(alt).hexdigest(), profile, other_tmp) as (other_rel, other_binding):
                                            with self.assertRaises(engine.Refuse):
                                                engine.apply(synthetic, other_rel, mode, False, None, str(work), other_binding)
                                        self.assertEqual(before_retry, sha(fixture))
                                        alt_image.unlink()
                                        (root / 'alt-catalog.json').unlink()
                                    # Mutate valid primary GPT metadata on this private fixture;
                                    # recalculate CRCs so refusals test identity, not corruption.
                                    original_hd = synthetic.pread(512, 512)
                                    lba, count, esize = struct.unpack_from('<QII', original_hd, 72)
                                    original_table = synthetic.pread(lba * 512, count * esize)
                                    for kind in ('disk-guid', 'partition-guid', 'geometry'):
                                        hd, table = bytearray(original_hd), bytearray(original_table)
                                        if kind == 'disk-guid':
                                            hd[56] ^= 1
                                        else:
                                            index = before_inst.gpt['userdata']['n'] - 1
                                            at = index * esize
                                            if kind == 'partition-guid':
                                                table[at + 16] ^= 1
                                            else:
                                                last = struct.unpack_from('<Q', table, at + 40)[0]
                                                struct.pack_into('<Q', table, at + 40, last - 1)
                                            struct.pack_into('<I', hd, 88, zlib.crc32(table))
                                        struct.pack_into('<I', hd, 16, 0)
                                        struct.pack_into('<I', hd, 16, zlib.crc32(hd[:struct.unpack_from('<I', hd, 12)[0]]))
                                        os.pwrite(synthetic.fd, table, lba * 512)
                                        os.pwrite(synthetic.fd, hd, 512)
                                        os.fsync(synthetic.fd)
                                        changed = sha(fixture)
                                        with self.assertRaises(engine.Refuse):
                                            engine.apply(synthetic, shared_rel, mode, False, None, str(work), binding)
                                        self.assertEqual(changed, sha(fixture))
                                        os.pwrite(synthetic.fd, original_table, lba * 512)
                                        os.pwrite(synthetic.fd, original_hd, 512)
                                        os.fsync(synthetic.fd)
                                    self.assertEqual(before_retry, sha(fixture))
                                if phase.startswith('sharedmenu-'):
                                    with self.assertRaises(engine.Refuse):
                                        engine.apply(synthetic, shared_rel, mode, False, None, str(work), binding)
                                    self.assertEqual(before_retry, sha(fixture))
                                    self.assertEqual(state_before, engine.Installed(synthetic, str(work)).state_original)
                                else:
                                    engine.apply(synthetic, shared_rel, mode, False, None, str(work), binding)
                                    self.assertEqual(engine.Installed(synthetic, str(work)).current, 'b')
                                    self.assertEqual(before_keep, ranges_hash(synthetic, keep))
                                    after_inst = engine.Installed(synthetic, str(work))
                                    self.assertEqual(after_inst.gpt, original_gpt)
                                    assert_release_bytes(synthetic, after_inst, shared_rel)
                                    if mode == 'nvme':
                                        for entry in engine.REQUIRED_ENTRIES:
                                            opts = after_inst.entries[entry]['options']
                                            self.assertIn('initcall_blacklist=mei_me_driver_init', opts.split())
                                            self.assertIn('efi=noruntime', opts.split())
                                            self.assertNotIn(engine.LIFT, opts.split())
                                    for name, (extents, expected) in original_dynamic.items():
                                        self.assertEqual(after_inst.lp.part(name)['extents'], extents)
                                        self.assertEqual(expected, ranges_hash(synthetic,
                                            [(after_inst.super['start'] + start * engine.SECTOR, count * engine.SECTOR)
                                             for start, count in extents]))
                            finally:
                                engine.FAIL_AT = ''
                                engine.WRITING.clear()
                                engine.WINDOW.clear()
                                engine.STICK.clear()
                                os.close(synthetic.fd)
                            fixture.unlink()
                            for source_item in items:
                                self.assertEqual(sha(root / source_item['image']['path']), original_sources[source_item['profile']])
                    template.unlink()
                last_reader = reader
            with self.assertRaises(OSError):
                last_reader.pread(0, 1)  # catalog scope ended; no live Release is used afterwards


if __name__ == '__main__':
    unittest.main()
