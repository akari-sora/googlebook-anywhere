#!/usr/bin/env python3
import hashlib
from contextlib import contextmanager
import io
import importlib.util
import json
import os
import struct
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('preview', ROOT / 'install/stick/shared-source.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def opener(root, path):
    # Controlled regular files only, for portability of synthetic unit tests.
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)


class FakeESP:
    def ls(self, path):
        return ['lapis-debug.conf', 'lapis-quiet.conf']


class FakeRelease:
    build = 16658739
    model = 'AMD'
    esp = FakeESP()


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.items = []
        for profile in ('kano', 'amd-hx370'):
            content = (profile * 40).encode()
            name = profile + '.img'
            (self.root / name).write_bytes(content)
            self.items.append({'profile': profile, 'build_id': '16658739',
                               'image': {'path': name, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()},
                               'boot_entries': ['lapis-debug.conf', 'lapis-quiet.conf'], 'hardware_validation': 'unvalidated'})
        self.opener = patch.object(m.catalog, 'open_relative', opener)
        self.opener.start()
        self.addCleanup(self.opener.stop)
        self.mock = patch.object(m.update, 'image_release_from_reader', return_value=FakeRelease())
        self.parser = self.mock.start()
        self.addCleanup(self.mock.stop)
        self.save()

    def save(self):
        raw = json.dumps({'schema': 1, 'payloads': self.items}).encode()
        (self.root / 'catalog.json').write_bytes(raw)
        self.sha = hashlib.sha256(raw).hexdigest()

    def run_preview(self, profile='kano'):
        return m.preview(str(self.root), 'catalog.json', self.sha, profile)

    def test_rollback_callback_runs_at_write_boundary_and_legacy_default_is_unchanged(self):
        entry = {'title': 'Googlebook', 'linux': '/kernel', 'initrd': ['/initrd'], 'options': 'x'}
        previous = dict(entry, title='Googlebook (previous version)')
        inst = type('Installed', (), {'resume': None, 'entries': {'lapis-quiet.conf': entry,
                    'prev-lapis-quiet.conf': previous}, 'slot_of': {'prev-lapis-quiet.conf': 'a'},
                    'target': 'a', 'current': 'b', 'build': 10, 'fat': object()})()
        def fail():
            self.assertEqual(m.update.WRITING, [])
            raise ValueError('checked at boundary')
        with patch.object(m.update, 'Installed', return_value=inst), patch.object(m.update, 'slot_build', return_value=10):
            with self.assertRaisesRegex(ValueError, 'checked at boundary'):
                m.update.rollback(object(), str(self.root), pre_write_check=fail)
        self.assertEqual(m.update.WRITING, [])

    def test_synthetic_rollback_verifies_source_and_target_and_cleans_flags(self):
        target = self.root / 'target'
        target.write_bytes(b'abc')
        fd = os.open(target, os.O_RDWR)
        parent = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.addCleanup(os.close, parent)
        disk = type('Disk', (), {'fd': fd})()
        rel = type('Release', (), {'build': 12})()
        binding = object()
        checks = []
        @contextmanager
        def operation(*args):
            yield disk, rel, binding, str(self.root), 'kano', parent, target.name, lambda: checks.append('target')
        def rollback(disk, tmp, *, stick_boot, allow_build_change, pre_write_check):
            self.assertIsNone(stick_boot)
            self.assertFalse(allow_build_change)
            pre_write_check()
            m.update.WRITING.append(True)
        with patch.object(m, '_synthetic_image_operation', operation), \
             patch.object(m, 'safe_open_relative', opener), \
             patch.object(m.update.SharedBinding, 'verify', side_effect=lambda *a: checks.append('source')), \
             patch.object(m.update, 'rollback', side_effect=rollback):
            result = m.rollback_observed_image('root', 'catalog', 'digest', str(target))
        self.assertEqual(checks, ['source', 'target'])
        self.assertEqual(result['result'], 'ROLLED_BACK_SYNTHETIC_IMAGE')
        self.assertFalse(result['physical_write_allowed'])
        self.assertEqual(m.update.WRITING, [])

    def test_synthetic_rollback_write_exception_is_classified(self):
        @contextmanager
        def operation(*args):
            yield object(), object(), object(), 'tmp', 'kano', 0, 'target', lambda: None
        def fail(*args, **kwargs):
            m.update.WRITING.append(True)
            raise OSError('injected')
        with patch.object(m, '_synthetic_image_operation', operation), patch.object(m.update, 'rollback', side_effect=fail):
            with self.assertRaisesRegex(m.update.Failed, 'after entering write phase'):
                m.rollback_observed_image('root', 'catalog', 'digest', '/tmp/target')
        self.assertEqual(m.update.WRITING, [])

    def test_observed_plan_uses_selected_profile_and_nvme_engine(self):
        target = self.root / 'large-target'
        with target.open('wb') as f:
            f.truncate(129 * 1024 * 1024)
        @contextmanager
        def release(root, catalog_path, expected, profile, tmp, bounded_fat=False):
            self.assertTrue(bounded_fat)
            yield FakeRelease(), object()
        fake_inst = type('Installed', (), {'current': 'a', 'target': 'b', 'resume': None})()
        result_boot = {'fits': True, 'new_first': True, 'free': 10, 'reclaim': 1,
                       'need': 3, 'cluster': 1, 'prev': {}}
        for identity, profile in [({'sys_vendor': 'Google', 'product_name': 'Kano'}, 'kano'),
                ({'sys_vendor': 'ONE-NETBOOK', 'product_name': 'ONEXPLAYER X1Pro',
                  'board_vendor': 'ONE-NETBOOK', 'board_name': 'ONEXPLAYER X1Pro'}, 'amd-hx370')]:
            dmi = self.root / (profile + '-dmi')
            dmi.mkdir()
            for key, value in identity.items():
                (dmi / key).write_text(value + '\n')
            with patch.object(m, 'safe_open_relative', opener), \
                 patch.object(m, 'shared_release', side_effect=release) as src, \
                 patch.object(m.update.SharedBinding, 'verify'), \
                 patch.object(m.update, 'PreviewMetadataSnapshot', return_value=object()), \
                 patch.object(m.update, 'Installed', return_value=fake_inst), \
                 patch.object(m.update, 'check', return_value={'sizes': {'system_b': 1}}) as check, \
                 patch.object(m.update, 'boot_files', return_value=result_boot) as boot:
                result = m.plan_observed_image(str(self.root), 'catalog.json', self.sha, str(target), dmi)
            self.assertEqual(src.call_args.args[3], profile)
            self.assertEqual(check.call_args.args[2], 'nvme')
            self.assertEqual(boot.call_args.args[2], 'nvme')
            self.assertTrue(boot.call_args.kwargs['preview'])
            self.assertFalse(result['write_allowed'])
            self.assertEqual(target.stat().st_size, 129 * 1024 * 1024)
            self.assertIn('IN_USE_NOT_VERIFIED', result['limitations'])

    def test_observed_plan_refuses_unknown_and_incomplete_machine(self):
        dmi = self.root / 'dmi'
        dmi.mkdir()
        target = self.root / 'target'
        target.write_bytes(b'target')
        for identity in ({}, {'sys_vendor': 'Google'},
                         {'sys_vendor': 'unrecognized', 'product_name': 'unrecognized'}):
            for child in dmi.iterdir():
                child.unlink()
            for key, value in identity.items():
                (dmi / key).write_text(value + '\n')
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, 'not uniquely'):
                m.plan_observed_image(str(self.root), 'catalog.json', self.sha, str(target), dmi)

    def test_both_profiles_and_lifetime(self):
        for profile in ('kano', 'amd-hx370'):
            result = self.run_preview(profile)
            self.assertFalse(result['write_allowed'])
            reader = self.parser.call_args.args[0]
            with self.assertRaises(OSError):
                reader.pread(0, 1)

    def test_build_and_entry_mismatch(self):
        for key, value in [('build_id', '1'), ('boot_entries', ['lapis-debug.conf'])]:
            old = self.items[0][key]
            self.items[0][key] = value
            self.save()
            with self.assertRaises(ValueError):
                self.run_preview()
            self.items[0][key] = old

    def test_digest_and_mutation(self):
        with self.assertRaises(ValueError):
            m.preview(str(self.root), 'catalog.json', '0' * 64, 'kano')
        for replace in (False, True):
            def changed(reader, *args, **kwargs):
                path = self.root / 'kano.img'
                if replace:
                    other = self.root / 'new'
                    other.write_bytes(path.read_bytes())
                    os.replace(other, path)
                else:
                    path.write_bytes(b'x' * reader.size)
                return FakeRelease()
            self.parser.side_effect = changed
            with self.assertRaises(ValueError):
                self.run_preview()
            content = ('kano' * 40).encode()
            (self.root / 'kano.img').write_bytes(content)
        self.parser.side_effect = None

    def test_synthetic_target_uses_private_copy_and_existing_check(self):
        target = self.root / 'target.img'
        target.write_bytes(b'original target')
        seen = []
        def installed(disk, tmp, preview=False):
            seen.append((disk.path, disk.fd))
            self.assertNotEqual(disk.path, str(target))
            self.assertEqual(Path(disk.path).read_bytes(), b'original target')
            return object()
        with patch.object(m, 'safe_open_relative', opener), \
             patch.object(m.update, 'Installed', side_effect=installed), \
             patch.object(m.update, 'gpt_read', return_value={}), \
             patch.object(m.update, 'validate_preview_layout'), \
             patch.object(m.update, 'preview_resource_limits'), \
             patch.object(m.update, 'check') as check:
            result = m.preview(str(self.root), 'catalog.json', self.sha, 'kano', str(target))
        self.assertTrue(result['compatible_synthetic_target'])
        self.assertFalse(result['write_allowed'])
        check.assert_called_once()
        self.assertEqual(target.read_bytes(), b'original target')
        self.assertFalse(Path(seen[0][0]).exists())
        with self.assertRaises(OSError):
            os.fstat(seen[0][1])

    def test_synthetic_target_closes_disk_when_check_refuses(self):
        target = self.root / 'target.img'
        target.write_bytes(b'synthetic')
        held = []
        def installed(disk, tmp, preview=False):
            held.append(disk.fd)
            return object()
        with patch.object(m, 'safe_open_relative', opener), \
             patch.object(m.update, 'Installed', side_effect=installed), \
             patch.object(m.update, 'gpt_read', return_value={}), \
             patch.object(m.update, 'validate_preview_layout'), \
             patch.object(m.update, 'preview_resource_limits'), \
             patch.object(m.update, 'check', side_effect=m.update.Refuse('test')):
            with self.assertRaises(m.update.Refuse):
                m.preview(str(self.root), 'catalog.json', self.sha, 'kano', str(target))
        with self.assertRaises(OSError):
            os.fstat(held[0])

    def test_preview_resource_caps_before_copy_or_expansion(self):
        U = m.update
        layout = {U.ESP: {'len': U.PREVIEW_ESP_MAX + 512},
                  'vbmeta_a': {'len': 512}, 'vbmeta_b': {'len': 512}, 'super': {'start': 0, 'len': 64 << 20}}
        reader = type('R', (), {'pread': lambda self, off, n: self.fail_if_read()})()
        with self.assertRaises(U.Refuse):
            U.preview_resource_limits(reader, layout)
        layout[U.ESP]['len'] = 512
        layout['vbmeta_a']['len'] = U.PREVIEW_VBMETA_MAX + 512
        with self.assertRaises(U.Refuse):
            U.preview_resource_limits(reader, layout)
        layout['vbmeta_a']['len'] = 512
        layout['vbmeta_b']['len'] = U.PREVIEW_VBMETA_MAX + 512
        with self.assertRaises(U.Refuse):
            U.preview_resource_limits(reader, layout)
        layout['vbmeta_b']['len'] = 512
        for mx, slots in ((U.PREVIEW_LP_MAX + 1, 3), (65536, 5)):
            geo = bytearray(4096)
            struct.pack_into('<II32sIII', geo, 0, U.GEO_MAGIC, 52, bytes(32), mx, slots, 4096)
            geo[8:40] = hashlib.sha256(geo[:52]).digest()
            r = type('R', (), {'pread': lambda self, off, n: bytes(geo)})()
            with self.assertRaises(U.Refuse):
                U.preview_resource_limits(r, layout)
        # One tiny LP table claiming more referenced extents than the bound.
        geo = bytearray(4096)
        struct.pack_into('<II32sIII', geo, 0, U.GEO_MAGIC, 52, bytes(32), 65536, 1, 4096)
        geo[8:40] = hashlib.sha256(geo[:52]).digest()
        metadata = bytearray(65536)
        struct.pack_into('<I', metadata, 8, 128)
        struct.pack_into('<I', metadata, 44, 52)
        struct.pack_into('<III', metadata, 80, 0, 1, 52)
        struct.pack_into('<I', metadata, 128 + 44, U.PREVIEW_LP_REFERENCES + 1)
        r = type('R', (), {'pread': lambda self, off, n: bytes(geo) if n == 4096 else bytes(metadata)})()
        with self.assertRaises(U.Refuse):
            U.preview_resource_limits(r, layout)

    @unittest.skipUnless(shutil.which('lz4'), 'lz4 required')
    def test_preview_ramdisk_bounded_and_legacy_unmodified(self):
        U = m.update
        def compressed(data):
            proc = subprocess.run(['lz4', '-l', '-c'], input=data, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, check=True)
            return proc.stdout
        small = b'a' * 400
        oversized = b'b' * 3000
        with patch.object(U, 'PREVIEW_RAMDISK_MAX', 1024):
            self.assertEqual(U.preview_ramdisk_plain(compressed(small)), small)
            with self.assertRaises(U.Refuse):
                U.preview_ramdisk_plain(compressed(oversized))
            with self.assertRaises(U.Refuse):
                U.preview_ramdisk_plain(oversized)
            self.assertEqual(U.ramdisk_plain(compressed(oversized)), oversized)
            with self.assertRaises(U.Refuse):
                U.preview_ramdisk_plain(U.LZ4_LEGACY + b'invalid')

    def test_preview_decoder_timeout_reaps_child(self):
        U = m.update
        stub = self.root / 'lz4'
        stub.write_text('#!/bin/sh\nexec sleep 30\n')
        stub.chmod(0o755)
        processes = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            p = real_popen(*args, **kwargs)
            processes.append(p)
            return p
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ.get('PATH', '')}), \
             patch.object(U, 'PREVIEW_LZ4_TIMEOUT', 0.1), \
             patch.object(U.subprocess, 'Popen', side_effect=launch):
            with self.assertRaises(U.Refuse):
                U.preview_ramdisk_plain(U.LZ4_LEGACY + b'input')
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_shared_state_strict_and_legacy(self):
        u = m.update
        base = [('updating', 'b'), ('from', 'a'), ('build', '123'), ('fingerprint', 'fp'),
                ('key', 'abc'), ('model', 'KANO'), ('release', 'release')]
        base += [('select.' + k, 'value') for k in u.SELECT]
        base += [('shared-version', '1'), ('shared-digest', 'a' * 64)]
        text = ''.join(k + ' ' + v + '\n' for k, v in base)
        self.assertEqual(u.shared_state(text), 'a' * 64)
        self.assertIsNone(u.shared_state('updating b\nfrom a\n'))
        for bad in (text + 'shared-digest ' + 'a' * 64 + '\n',
                    text.replace('shared-version 1\n', ''),
                    text.replace('shared-digest ' + 'a' * 64, 'shared-digest bad'),
                    text.replace('from a\n', 'from b\n'), text + 'shared-unknown x\n'):
            with self.assertRaises(u.Refuse):
                u.shared_state(bad)

    def test_shared_catalog_binding_lifetime_and_mutation(self):
        with m.shared_release(str(self.root), 'catalog.json', self.sha, 'kano', str(self.root)) as (rel, binding):
            self.assertEqual(binding.verify(rel)['profile'], 'kano')
            for name in ('kano.img', 'amd-hx370.img'):
                with (self.root / name).open('rb') as source:
                    disk = type('D', (), {'fd': source.fileno()})()
                    with self.assertRaises(m.update.Refuse):
                        binding.verify(rel, disk)
            with self.assertRaises(m.update.Refuse):
                binding.verify(object())
            (self.root / 'kano.img').write_bytes(b'x' * 160)
            with self.assertRaises(m.update.Refuse):
                binding.verify(rel)
        with self.assertRaises(m.update.Refuse):
            binding.verify(rel)

    def test_legacy_apply_refuses_shared_state_before_writes(self):
        u = m.update
        disk = object()
        inst = type('I', (), {'shared_digest': 'a' * 64})()
        with patch.object(u, 'Installed', return_value=inst), patch.object(u, 'check') as check:
            with self.assertRaises(u.Refuse):
                u.apply(disk, object(), 'stick', False, None, str(self.root))
            check.assert_not_called()

    def test_fabricated_or_subclassed_binding_refused_without_target_changes(self):
        u = m.update
        target = self.root / 'fake-target'
        target.write_bytes(b'unchanged')
        disk = u.Disk(str(target), write=True)
        class Fabricated(u.SharedBinding):
            def __init__(self):
                pass
            def verify(self, *args):
                return {'profile': 'kano', 'catalog_sha256': 'a' * 64,
                        'image_sha256': 'b' * 64, 'image_size': 1}
        try:
            for binding in (object(), Fabricated()):
                with self.assertRaises(u.Refuse):
                    u.apply(disk, object(), 'stick', False, None, str(self.root), binding)
                self.assertEqual(target.read_bytes(), b'unchanged')
        finally:
            os.close(disk.fd)

    def test_shared_mode_guard_rejects_unsafe_targets_and_options(self):
        u = m.update
        target = self.root / 'guard-target'
        target.write_bytes(b'x' * 512)
        disk = u.Disk(str(target), write=True)
        binding = object.__new__(u.SharedBinding)
        try:
            with patch.object(u, 'Installed', side_effect=LookupError('passed guard')) as installed, \
                 patch.object(u, 'gpt_read', return_value={}), \
                 patch.object(u, 'validate_preview_layout'), patch.object(u, 'preview_resource_limits'), \
                 patch.object(u.SharedBinding, 'verify', return_value={}):
                for mode in ('stick', 'nvme'):
                    with self.assertRaisesRegex(LookupError, 'passed guard'):
                        u.apply(disk, object(), mode, False, None, str(self.root), binding)
                for mode, no_fallback, stick_boot, block, size in (
                    ('nvme', True, None, False, 512),
                    ('nvme', False, '/fake/stick', False, 512),
                    ('nvme', False, None, True, 512),
                    ('nvme', False, None, False, 128 * u.MIB + 1),
                    ('unknown', False, None, False, 512),
                ):
                    disk.blk, disk.size = block, size
                    with self.assertRaises(u.Refuse):
                        u.apply(disk, object(), mode, no_fallback, stick_boot, str(self.root), binding)
                self.assertEqual(installed.call_count, 2)
            self.assertEqual(target.read_bytes(), b'x' * 512)
        finally:
            os.close(disk.fd)

    def test_shared_apply_refuses_unmarked_resume_before_writes(self):
        u = m.update
        target = self.root / 'fixture'
        target.write_bytes(b'x' * 512)
        disk = u.Disk(str(target), write=True)
        inst = type('I', (), {'shared_digest': None, 'resume': 'b'})()
        binding = object.__new__(u.SharedBinding)
        try:
            with patch.object(u, 'gpt_read', return_value={}), \
                 patch.object(u, 'validate_preview_layout'), patch.object(u, 'preview_resource_limits'), \
                 patch.object(u.SharedBinding, 'verify', return_value={}), \
                 patch.object(u, 'Installed', return_value=inst), patch.object(u, 'check') as check:
                with self.assertRaises(u.Refuse):
                    u.apply(disk, object(), 'stick', False, None, str(self.root), binding)
                check.assert_not_called()
            self.assertEqual(target.read_bytes(), b'x' * 512)
        finally:
            os.close(disk.fd)

    def test_shared_identity_binds_all_operation_fields(self):
        u = m.update
        class FakeDisk:
            size = 65536
            def pread(self, off, n):
                return bytes(56) + bytes.fromhex('01' * 16) + bytes(440)
        class FakeInstalled:
            current, target = 'a', 'b'
            gpt = {'x': {'n': 1, 'start': 4096, 'len': 512, 'type': 'type', 'guid': 'guid', 'attrs': 0}}
        source = {'profile': 'kano', 'catalog_sha256': '1' * 64, 'image_sha256': '2' * 64, 'image_size': 12}
        orig = u.shared_identity(FakeDisk(), FakeInstalled(), source, 'stick', False)
        for key in source:
            changed = dict(source)
            changed[key] = 'changed' if key != 'image_size' else 13
            self.assertNotEqual(orig, u.shared_identity(FakeDisk(), FakeInstalled(), changed, 'stick', False))
        self.assertNotEqual(orig, u.shared_identity(FakeDisk(), FakeInstalled(), source, 'nvme', False))
        self.assertNotEqual(orig, u.shared_identity(FakeDisk(), FakeInstalled(), source, 'stick', True))
        FakeInstalled.gpt = {'x': dict(FakeInstalled.gpt['x'], guid='other')}
        self.assertNotEqual(orig, u.shared_identity(FakeDisk(), FakeInstalled(), source, 'stick', False))

    def test_copy_exact_rejects_growth_and_truncation(self):
        path = self.root / 'growing'
        path.write_bytes(b'abcdef')
        for expected in (5, 7):
            with path.open('rb') as source:
                dest = io.BytesIO()
                with self.assertRaises(ValueError):
                    m.copy_exact(source.fileno(), dest, expected)
                self.assertLessEqual(len(dest.getvalue()), expected)
        with path.open('rb') as source:
            dest = io.BytesIO()
            m.copy_exact(source.fileno(), dest, 6)
            self.assertEqual(dest.getvalue(), b'abcdef')

    def test_target_rejects_invalid_gpt_before_installed(self):
        target = self.root / 'target.img'
        target.write_bytes(b'synthetic')
        with patch.object(m, 'safe_open_relative', opener), \
             patch.object(m.update, 'gpt_read', return_value={'vbmeta_b': {'start': 1024, 'len': 2**50}}), \
             patch.object(m.update, 'validate_preview_layout', side_effect=m.update.Refuse('bounds')), \
             patch.object(m.update, 'Installed') as installed:
            with self.assertRaises(m.update.Refuse):
                m.preview(str(self.root), 'catalog.json', self.sha, 'kano', str(target))
            installed.assert_not_called()

    def test_reader_bounds_and_truncation(self):
        path = self.root / 'kano.img'
        with path.open('rb') as f:
            r = m.Reader(f.fileno(), 999)
            for off, n in [(-1, 1), (0, -1), (999, 1), (0, 999)]:
                with self.assertRaises(ValueError):
                    r.pread(off, n)

    def test_layout_bounds_before_release(self):
        self.mock.stop()
        header = bytearray(512)
        struct.pack_into('<QQQQ', header, 24, 1, 127, 34, 94)
        struct.pack_into('<QII', header, 72, 2, 128, 128)
        fake = type('Fake', (), {'size': 65536, 'path': 'fake', 'pread': lambda self, o, n: bytes(header)})()
        for layout in ({'x': {'start': 512, 'len': 512}},
                       {'x': {'start': 32768, 'len': 32768}},
                       {'x': {'start': 32768, 'len': 1024}, 'y': {'start': 33280, 'len': 512}}):
            with patch.object(m.update, 'gpt_read', return_value=layout):
                with self.assertRaises(m.update.Refuse):
                    m.update.image_release_from_reader(fake, str(self.root), 'test', validate_layout=True)


if __name__ == '__main__':
    unittest.main()
