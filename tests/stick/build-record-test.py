#!/usr/bin/env python3
import ast
import hashlib
import importlib.util
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('record', Path(__file__).resolve().parents[2] / 'builder/lib/build_record.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

def sha(data): return hashlib.sha256(data).hexdigest()

def record(data, status='passed'):
    return {'schema': 1, 'profile': 'kano', 'profile_sha256': 'a' * 64,
            'declared_build_id': '16452207', 'build_id_basis': 'selected_pins',
            'image': {'basename': 'test.img', 'size': len(data), 'sha256': sha(data)},
            'selftest': status, 'not_run': [],
            'options': {'entry': 'lapis-quiet.conf', 'editor': 'no', 'ec': [], 'adb': False,
                        'usb_smoke': False, 'model_override': False},
            'pristine': 'NOT_PROVEN', 'input_coverage': 'INCOMPLETE',
            'authority': 'UNSIGNED_BUILDER_SELF_REPORT'}

class RecordTest(unittest.TestCase):
    def test_actual_producer_keeps_default_options_compatible(self):
        source = Path(__file__).resolve().parents[2] / 'builder/image.py'
        function = next(node for node in ast.parse(source.read_text()).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'publish_record')
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'image.img'
            image.write_bytes(b'image')
            publisher = mock.Mock()
            namespace = dict(os=os, hashlib=hashlib, PROF={'model': {'id': 'kano'}},
                             PROFILE_BYTES=b'profile', FINAL=image, IMG=image,
                             DEFAULT_ENTRY='lapis-quiet.conf', EDITOR='no',
                             EC_FEATURES=[], ADB=False, USB_SMOKE=False, build_record=publisher)
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
            legacy_options = set(record(b'image')['options'])
            for option in ('', '1'):
                with mock.patch.dict(os.environ, {'GBOS_SETUP_PIN_SKIP': option}, clear=True):
                    namespace['publish_record'](sha(b'image'), 'passed', [])
                produced = publisher.publish.call_args.args[3]
                parsed = r.schema(r.encode(produced))
                if option:
                    self.assertEqual(set(parsed['options']), legacy_options | {'setup_pin_skip'})
                    self.assertIs(parsed['options']['setup_pin_skip'], True)
                else:
                    self.assertEqual(set(parsed['options']), legacy_options)

    def test_optional_setup_skip_keeps_legacy_records_and_strict_types(self):
        legacy = r.schema(r.encode(record(b'image')))
        self.assertNotIn('setup_pin_skip', legacy['options'])
        for value in (False, True):
            current = record(b'image')
            current['options']['setup_pin_skip'] = value
            self.assertIs(r.schema(r.encode(current))['options']['setup_pin_skip'], value)
        for value in (0, 1, 'true', None):
            current = record(b'image')
            current['options']['setup_pin_skip'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                r.encode(current)
        current = record(b'image')
        current['options']['unexpected'] = False
        with self.assertRaises(ValueError):
            r.encode(current)

    def test_publication_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / 'test.img'
            img = Path(str(final) + '.partial')
            data = b'image'
            for status in ('passed', 'skipped'):
                img.write_bytes(data)
                digest, item = sha(data), record(data, status)
                with self.assertRaises(ValueError): r.publish(img, final, digest, item, False)
                with mock.patch.object(r.hashlib, 'sha256', side_effect=AssertionError('unexpected hash')):
                    r.publish(img, final, digest, item, True)
                self.assertEqual(final.read_bytes(), data)
                self.assertEqual(Path(str(final) + '.not-self-tested').exists(), status == 'skipped')
                with self.assertRaises(ValueError): r.publish(img, final, digest, item, True)
                for path in Path(tmp).iterdir(): path.unlink()
            img.write_bytes(data)
            real_open = r.os.open
            def fail(path, *args, **kwargs):
                if str(path).endswith('.build-record.json.partial'): raise OSError('injected')
                return real_open(path, *args, **kwargs)
            with mock.patch.object(r.os, 'open', side_effect=fail), self.assertRaises(OSError):
                r.publish(img, final, sha(data), record(data), True)
            self.assertFalse(final.exists())
            self.assertFalse(Path(str(final) + '.sha256').exists())

    def test_durable_publication_order_and_directory_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / 'test.img'
            img = Path(str(final) + '.partial')
            data = b'image'
            img.write_bytes(data)
            events = []
            real_fsync, real_link, real_unlink = os.fsync, os.link, Path.unlink
            def fsync(fd):
                info = os.fstat(fd)
                kind = 'dir' if stat.S_ISDIR(info.st_mode) else ('image' if info.st_ino == img_inode else 'sidecar')
                events.append(('fsync', kind))
                return real_fsync(fd)
            def link(src, dst):
                events.append(('link', Path(dst).name))
                return real_link(src, dst)
            def unlink(path, *args, **kwargs):
                events.append(('unlink', path.name))
                return real_unlink(path, *args, **kwargs)
            img_inode = img.stat().st_ino
            with mock.patch.object(r.os, 'fsync', side_effect=fsync), mock.patch.object(r.os, 'link', side_effect=link), \
                 mock.patch.object(Path, 'unlink', unlink):
                r.publish(img, final, sha(data), record(data), True)
            final_link = events.index(('link', final.name))
            self.assertLess(events.index(('fsync', 'image')), final_link)
            dirs = [i for i, event in enumerate(events) if event == ('fsync', 'dir')]
            self.assertEqual(len(dirs), 2)
            self.assertLess(events.index(('unlink', final.name + '.build-record.json.partial')), dirs[0])
            self.assertLess(dirs[0], final_link)
            self.assertLess(events.index(('unlink', img.name)), dirs[1])
            for path in Path(tmp).iterdir(): path.unlink()
            img.write_bytes(data)
            def fail_dir(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError('injected directory fsync failure')
                return real_fsync(fd)
            with mock.patch.object(r.os, 'fsync', side_effect=fail_dir), self.assertRaises(OSError):
                r.publish(img, final, sha(data), record(data), True)
            self.assertTrue(img.exists())
            self.assertFalse(final.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [img])
            dir_count = 0
            def fail_second_dir(fd):
                nonlocal dir_count
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    dir_count += 1
                    if dir_count == 2: raise OSError('injected final directory fsync failure')
                return real_fsync(fd)
            with mock.patch.object(r.os, 'fsync', side_effect=fail_second_dir), self.assertRaises(OSError):
                r.publish(img, final, sha(data), record(data), True)
            self.assertFalse(img.exists())
            self.assertEqual(final.read_bytes(), data)
            self.assertEqual(r.schema(Path(str(final) + '.build-record.json').read_bytes())['selftest'], 'passed')
            self.assertTrue(Path(str(final) + '.sha256').is_file())

    def test_post_link_errors_retain_complete_outputs(self):
        for status in ('passed', 'skipped'):
            for failure in ('directory-sync', 'candidate-unlink', 'link-return'):
                with self.subTest(status=status, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    final = Path(tmp) / 'test.img'
                    img = Path(str(final) + '.partial')
                    data = b'image'
                    img.write_bytes(data)
                    real_fsync, real_link, real_unlink = os.fsync, os.link, Path.unlink
                    directories = 0
                    final_linked = False
                    post_link_unlinks = []
                    def fsync(fd):
                        nonlocal directories
                        if stat.S_ISDIR(os.fstat(fd).st_mode):
                            directories += 1
                            if failure == 'directory-sync' and directories == 2:
                                raise OSError('injected final directory sync')
                        return real_fsync(fd)
                    def link(src, dst):
                        nonlocal final_linked
                        result = real_link(src, dst)
                        if Path(dst) == final:
                            final_linked = True
                            if failure == 'link-return':
                                raise OSError('injected after final link')
                        return result
                    def unlink(path, *args, **kwargs):
                        if final_linked:
                            post_link_unlinks.append(path)
                        if path == img and failure == 'candidate-unlink':
                            raise OSError('injected candidate unlink')
                        return real_unlink(path, *args, **kwargs)
                    with mock.patch.object(r.os, 'fsync', side_effect=fsync), \
                         mock.patch.object(r.os, 'link', side_effect=link), \
                         mock.patch.object(Path, 'unlink', unlink), self.assertRaises(OSError):
                        r.publish(img, final, sha(data), record(data, status), True)
                    self.assertTrue(final_linked)
                    self.assertEqual(final.read_bytes(), data)
                    self.assertEqual(Path(str(final) + '.sha256').read_text(), sha(data) + '  test.img\n')
                    self.assertEqual(r.schema(Path(str(final) + '.build-record.json').read_bytes())['selftest'], status)
                    self.assertEqual(Path(str(final) + '.not-self-tested').exists(), status == 'skipped')
                    self.assertTrue(all(path == img for path in post_link_unlinks))
                    self.assertEqual(img.exists(), failure != 'directory-sync')

    def test_reader_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'record'
            raw = r.encode(record(b'image'))
            for bad in (b'{}', raw[:-2] + b',"schema":1}', b'x' * (r.MAX_RECORD + 1), raw[:-2] + b',"extra":1}'):
                path.write_bytes(bad)
                with self.assertRaises(ValueError): r.read_pinned(path, sha(bad))
            bad_type = raw.replace(b'"schema":1', b'"schema":true')
            path.write_bytes(bad_type)
            with self.assertRaises(ValueError): r.read_pinned(path, sha(bad_type))
            path.write_bytes(raw)
            with self.assertRaises(ValueError): r.read_pinned(path, '0' * 64)
            path.unlink()
            path.symlink_to(Path(tmp) / 'missing')
            with self.assertRaises(OSError): r.read_pinned(path, sha(raw))
            path.unlink()
            os.mkfifo(path)
            with self.assertRaises(ValueError): r.read_pinned(path, sha(raw))

if __name__ == '__main__': unittest.main()
