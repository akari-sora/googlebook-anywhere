#!/usr/bin/env python3
"""Regression tests for rootless shared QEMU runner guards."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('qemu_host_under_test', HERE / 'shared-qemu-operation.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def bootconfig(slot, guid='boot-guid'):
    data = (f'androidboot.boot_part_uuid={guid}\nandroidboot.slot_suffix=_{slot}\n').encode()
    return data + struct.pack('<II', len(data), sum(data) & 0xffffffff) + b'#BOOTCONFIG\n'


def entry(title, initrd, options, kernel='/kernel.efi'):
    return f'title {title}\nlinux {kernel}\ninitrd {initrd}\noptions {options}\n'.encode()


class GuardTests(unittest.TestCase):
    def test_serial_json_waits_for_complete_nested_record(self):
        prefix = b'kernel log\r\nGBOS_QEMU_RESULT {"backend":{"copied":{"a":"hash"}}'
        self.assertIsNone(m.guest_result(prefix))
        self.assertIsNone(m.guest_result(prefix + b',"status":"OK"}'))
        self.assertEqual(m.guest_result(prefix + b',"status":"OK"}\r\r\n'),
                         {'backend': {'copied': {'a': 'hash'}}, 'status': 'OK'})
        with self.assertRaises(json.JSONDecodeError):
            m.guest_result(b'GBOS_QEMU_RESULT {"status":}\n')

    def test_target_path_swap_is_detected_and_held_fd_never_follows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, old = root / 'target', root / 'old'
            fd = m.create(path, 4096)
            try:
                m.target_named(fd, path, 4096)
                path.rename(old)
                path.write_bytes(b'other' * 819)
                with self.assertRaisesRegex(RuntimeError, 'target path changed'):
                    m.target_named(fd, path, 4096)
                os.pwrite(fd, b'held', 0)
                self.assertEqual(old.read_bytes()[:4], b'held')
                self.assertEqual(path.read_bytes()[:5], b'other')
                path.unlink()
                path.symlink_to(old)
                with self.assertRaisesRegex(RuntimeError, 'target path changed'):
                    m.target_named(fd, path, 4096)
            finally:
                os.close(fd)

    def test_firmware_rejects_symlink_fifo_directory_and_oversize(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            normal = root / 'good'
            normal.write_bytes(b'code')
            fd = m.firmware(normal)
            os.close(fd)
            link = root / 'link'
            link.symlink_to(normal)
            with self.assertRaises(OSError):
                m.firmware(link)
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            for invalid in (fifo, root):
                with self.assertRaisesRegex(RuntimeError, 'regular file'):
                    m.firmware(invalid)
            large = root / 'large'
            with large.open('wb') as out:
                out.truncate(16 * m.MIB + 1)
            with self.assertRaisesRegex(RuntimeError, 'size limit'):
                m.firmware(large)

    def test_qmp_stops_before_snapshot_and_missing_counter_is_failure(self):
        calls = []
        class FakeQMP:
            def execute(self, op):
                calls.append(op)
            def stats(self):
                calls.append('stats')
                return {'nv': {'wr_bytes': 0, 'wr_operations': 0, 'unmap_operations': 0}}
        stats = m.stopped_stats(FakeQMP())
        self.assertEqual(calls, ['stop', 'stats'])
        self.assertTrue(m.no_writes(stats['nv']))
        del stats['nv']['unmap_operations']
        self.assertFalse(m.no_writes(stats['nv']))

    def test_negative_requires_exact_backend_reason_and_zero_write(self):
        expected = ('BACKEND_MODEL_SELECTION', 'UNRECOGNIZED_DMI', 'ValueError', 'unknown DMI')
        stats = {'nv': {'wr_bytes': 0, 'wr_operations': 0, 'unmap_operations': 0}}
        result = dict(zip(('stage', 'reason_code', 'exception_class', 'reason'), expected), status='REFUSED')
        m.check_negative(result, stats, expected, 'unknown')
        for changed in (dict(result, stage='OTHER'), dict(result, reason='other'), dict(result, status='ERROR')):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                m.check_negative(changed, stats, expected, 'unknown')
        stats['nv']['wr_operations'] = 1
        with self.assertRaisesRegex(RuntimeError, 'wrote NVMe'):
            m.check_negative(result, stats, expected, 'unknown')

    def test_menu_requires_both_current_entries_and_previous_real_a(self):
        loader = b'default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n'
        source_options = 'root=/dev/ram0 bootconfig initcall_blacklist=nvme_init,mei_me_driver_init efi=noruntime'
        target_options = 'root=/dev/ram0 bootconfig initcall_blacklist=mei_me_driver_init efi=noruntime'
        current, release = {}, {'/kernel.efi': b'kernel'}
        current['/loader/loader.conf'] = loader
        current['/kernel.efi'] = b'kernel'
        current['/initrd.img'] = bootconfig('a')
        current['/gbos/b/initrd.img'] = bootconfig('b')
        for suffix in ('quiet', 'debug'):
            name = f'lapis-{suffix}.conf'
            title = f'Googlebook KANO {suffix}'
            release['/loader/entries/' + name] = entry(title, '/initrd.img', source_options)
            current['/loader/entries/' + name] = entry(title, '/initrd.img', target_options)
        def read(fd, part, path):
            return (current if fd == 10 else release)[path]
        with patch.object(m, 'fat_read', side_effect=read):
            previous = m.check_menu(10, {}, 20, {}, 'a', 'boot-guid')
            for suffix in ('quiet', 'debug'):
                name = f'lapis-{suffix}.conf'
                title = f'Googlebook KANO {suffix}'
                current['/loader/entries/' + name] = entry(title, '/gbos/b/initrd.img', target_options)
                current['/loader/entries/prev-' + name] = entry(title + ' (previous version)', '/initrd.img', target_options)
            m.check_menu(10, {}, 20, {}, 'b', 'boot-guid', previous)
            current['/loader/entries/lapis-debug.conf'] = entry('Googlebook KANO debug', '/initrd.img', target_options)
            with self.assertRaisesRegex(RuntimeError, 'current boot entry'):
                m.check_menu(10, {}, 20, {}, 'b', 'boot-guid', previous)
            current['/loader/entries/lapis-debug.conf'] = entry('Googlebook KANO debug', '/gbos/b/initrd.img', target_options)
            current['/initrd.img'] = bootconfig('b')
            with self.assertRaisesRegex(RuntimeError, 'slot or GUID'):
                m.check_menu(10, {}, 20, {}, 'b', 'boot-guid', previous)


if __name__ == '__main__':
    unittest.main()
