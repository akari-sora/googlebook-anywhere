#!/usr/bin/env python3
"""Focused constructor tests; block device stats are simulated on a regular file."""
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('update_disk_binding', ROOT / 'install/gbos-update.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class DiskOpenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / 'target.img'
        self.path.write_bytes(b'original target')
        self.real_open, self.real_close = os.open, os.close
        self.real_stat, self.real_fstat, self.real_lseek = os.stat, os.fstat, os.lseek
        self.opened = []
        self.closed = []
        self.flags = []
        self.addCleanup(self.close_survivors)

    def close_survivors(self):
        for fd in self.opened:
            try:
                self.real_close(fd)
            except OSError:
                pass

    def opened_fd(self, path, flags, *args, **kwargs):
        fd = self.real_open(path, flags, *args, **kwargs)
        self.opened.append(fd)
        self.flags.append(flags)
        return fd

    def closed_fd(self, fd):
        self.closed.append(fd)
        self.real_close(fd)

    def assert_closed_once(self):
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.closed.count(self.opened[0]), 1)
        with self.assertRaises(OSError):
            self.real_fstat(self.opened[0])

    def fake_block(self):
        original = self.real_stat(self.path)
        return SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_dev=original.st_dev,
                               st_ino=original.st_ino, st_rdev=os.makedev(8, 0))

    def block_stat(self, fake):
        def wrapped(path, *args, **kwargs):
            if os.fspath(path) == str(self.path):
                return fake
            return self.real_stat(path, *args, **kwargs)
        return wrapped

    def test_path_replaced_before_open_refuses_and_closes_without_writes(self):
        alternate = self.path.with_name('replacement.img')
        alternate.write_bytes(b'replacement target')
        def replace_and_open(path, flags, *args, **kwargs):
            os.replace(alternate, self.path)
            return self.opened_fd(path, flags, *args, **kwargs)
        with patch.object(u.os, 'open', side_effect=replace_and_open), \
             patch.object(u.os, 'close', side_effect=self.closed_fd):
            with self.assertRaises(u.Refuse):
                u.Disk(str(self.path), write=True)
        self.assertEqual(self.path.read_bytes(), b'replacement target')
        self.assert_closed_once()

    def test_read_open_is_nonblocking_until_fifo_substitution_refused(self):
        real = self.real_stat(self.path)
        fifo = SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_dev=real.st_dev,
                               st_ino=real.st_ino, st_rdev=real.st_rdev)
        def changed_fstat(fd):
            return fifo if fd in self.opened else self.real_fstat(fd)
        with patch.object(u.os, 'open', side_effect=self.opened_fd), \
             patch.object(u.os, 'close', side_effect=self.closed_fd), \
             patch.object(u.os, 'fstat', side_effect=changed_fstat):
            with self.assertRaises(u.Refuse):
                u.Disk(str(self.path))
        self.assertTrue(self.flags[-1] & os.O_NONBLOCK)
        self.assert_closed_once()

    def test_opened_type_change_refuses_and_closes(self):
        fake = self.fake_block()
        def changed_fstat(fd):
            return fake if fd in self.opened else self.real_fstat(fd)
        with patch.object(u.os, 'open', side_effect=self.opened_fd), \
             patch.object(u.os, 'close', side_effect=self.closed_fd), \
             patch.object(u.os, 'fstat', side_effect=changed_fstat):
            with self.assertRaises(u.Refuse):
                u.Disk(str(self.path), write=True)
        self.assertEqual(self.path.read_bytes(), b'original target')
        self.assert_closed_once()

    def test_each_failure_after_open_closes_descriptor(self):
        fake = self.fake_block()
        cases = [
            ('wrong-rdev', {'rdev': '8:1'}, None, u.Refuse),
            ('missing-rdev', {}, None, u.Refuse),
            ('wrong-syspath', {'rdev': '8:0', 'syspath': '/wrong'}, None, u.Refuse),
            ('wrong-size', {'rdev': '8:0', 'nbytes': 999}, None, u.Refuse),
            ('invalid-size', {'rdev': '8:0', 'nbytes': 'not-a-number'}, None, ValueError),
            ('lseek-error', {'rdev': '8:0'}, OSError('lseek failed'), OSError),
        ]
        for name, kwargs, lseek_error, exception in cases:
            with self.subTest(name=name):
                self.opened.clear()
                self.closed.clear()
                def fake_fstat(fd):
                    return fake if fd in self.opened else self.real_fstat(fd)
                def maybe_lseek(fd, *args):
                    if fd in self.opened and lseek_error:
                        raise lseek_error
                    return self.real_lseek(fd, *args)
                with patch.object(u.os, 'stat', side_effect=self.block_stat(fake)), \
                     patch.object(u.os, 'open', side_effect=self.opened_fd), \
                     patch.object(u.os, 'close', side_effect=self.closed_fd), \
                     patch.object(u.os, 'fstat', side_effect=fake_fstat), \
                     patch.object(u.os, 'lseek', side_effect=maybe_lseek):
                    with self.assertRaises(exception):
                        u.Disk(str(self.path), write=True, **kwargs)
                self.assert_closed_once()
                self.assertTrue(self.flags[-1] & os.O_EXCL)
                self.assertEqual(self.path.read_bytes(), b'original target')

    def test_set_blocking_failure_closes_descriptor(self):
        with patch.object(u.os, 'open', side_effect=self.opened_fd), \
             patch.object(u.os, 'close', side_effect=self.closed_fd), \
             patch.object(u.os, 'set_blocking', side_effect=OSError('set blocking failed')):
            with self.assertRaisesRegex(OSError, 'set blocking failed'):
                u.Disk(str(self.path), write=True)
        self.assert_closed_once()

    def test_close_error_does_not_mask_original_refusal(self):
        def close_and_fail(fd):
            self.closed_fd(fd)
            raise OSError('simulated close error')
        with patch.object(u.os, 'open', side_effect=self.opened_fd), \
             patch.object(u.os, 'close', side_effect=close_and_fail):
            with self.assertRaisesRegex(u.Refuse, 'not the confirmed'):
                u.Disk(str(self.path), write=True, nbytes=999)
        self.assert_closed_once()

    def test_fstat_failure_closes_descriptor(self):
        def broken_fstat(fd):
            if fd in self.opened:
                raise OSError('fstat failed')
            return self.real_fstat(fd)
        with patch.object(u.os, 'open', side_effect=self.opened_fd), \
             patch.object(u.os, 'close', side_effect=self.closed_fd), \
             patch.object(u.os, 'fstat', side_effect=broken_fstat):
            with self.assertRaises(OSError):
                u.Disk(str(self.path), write=True)
        self.assert_closed_once()

    def test_successful_regular_symlink_alias_retains_descriptor(self):
        alias = self.path.with_name('alias.img')
        alias.symlink_to(self.path)
        with patch.object(u.os, 'open', side_effect=self.opened_fd), \
             patch.object(u.os, 'close', side_effect=self.closed_fd):
            disk = u.Disk(str(alias), write=True, nbytes=self.path.stat().st_size)
            self.assertFalse(disk.blk)
            self.assertEqual(disk.pread(0, 8), b'original')
            self.assertTrue(os.get_blocking(disk.fd))
            self.assertEqual(self.closed, [])
            self.assertFalse(self.flags[-1] & os.O_EXCL)
            self.real_fstat(disk.fd)
            os.close(disk.fd)
        self.assert_closed_once()

    def test_simulated_block_opens_exclusively_and_retains_descriptor(self):
        fake = self.fake_block()
        def fake_fstat(fd):
            return fake if fd in self.opened else self.real_fstat(fd)
        with patch.object(u.os, 'stat', side_effect=self.block_stat(fake)), \
             patch.object(u.os, 'open', side_effect=self.opened_fd), \
             patch.object(u.os, 'close', side_effect=self.closed_fd), \
             patch.object(u.os, 'fstat', side_effect=fake_fstat):
            disk = u.Disk(str(self.path), write=True, rdev='8:0')
            self.assertTrue(disk.blk)
            self.assertTrue(os.get_blocking(disk.fd))
            self.assertTrue(self.flags[-1] & os.O_EXCL)
            self.assertEqual(self.closed, [])
            self.real_fstat(disk.fd)
            os.close(disk.fd)
        self.assert_closed_once()


if __name__ == '__main__':
    unittest.main()
