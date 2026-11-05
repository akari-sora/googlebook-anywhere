#!/usr/bin/env python3
"""Shared update provenance and sealed ESP consumption on miniature images.

Requires Linux memfd, mtools and lz4. Only private regular-file fixtures are used.
"""
import ast
import errno
import fcntl
import hashlib
import importlib.util
import mmap
import os
from pathlib import Path
import random
import shutil
import struct
import sys
import time
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('shared_esp_test', ROOT / 'install/stick/shared-source.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SharedProvenance(unittest.TestCase):
    def test_sealed_fat_time_budget_excludes_idle_wait_but_still_expires(self):
        u = m.update
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryFile() as held:
            now = time.monotonic
            offset = [0.0]
            with patch.object(u.time, 'monotonic', side_effect=lambda: now() + offset[0]):
                fat = u.PreviewFat('<owned snapshot>', tmp, fd=held.fileno())
                command = [sys.executable, '-c', 'print("owned")']
                self.assertEqual(fat._run(command, 32), b'owned\n')
                first = fat.remaining_time
                offset[0] += 1000
                self.assertEqual(fat._run(command, 32), b'owned\n')
                self.assertLess(fat.remaining_time, first)
                self.assertGreater(fat.remaining_time, 100)
                fat.remaining_time = 0
                with self.assertRaisesRegex(u.PreviewFatError, 'timed out'):
                    fat._run(command, 32)

    def test_fresh_target_refuses_absent_wrong_and_closed_binding(self):
        u = m.update
        with tempfile.TemporaryDirectory(prefix='gbos-shared-provenance-') as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.write_bytes(b'verified source')
            target = root / 'target'
            target.write_bytes(b'untouched target')
            item = {'profile': 'kano', 'image': {'size': source.stat().st_size,
                    'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}}
            fd = os.open(source, os.O_RDONLY)
            disk = u.Disk(str(target), write=True)
            release = object.__new__(u.SharedRelease)
            other_release = object.__new__(u.SharedRelease)
            try:
                with patch.object(u, 'image_release_from_reader', side_effect=(release, other_release)):
                    binding = u.SharedBinding(fd, item, 'a' * 64, [fd], tmp)
                    other = u.SharedBinding(fd, item, 'a' * 64, [fd], tmp)
                try:
                    for invalid in (None, other):
                        with self.subTest(binding=invalid), self.assertRaises(u.Refuse):
                            u.apply(disk, release, 'nvme', False, None, tmp, invalid)
                        self.assertEqual(target.read_bytes(), b'untouched target')
                    binding.close()
                    with self.assertRaisesRegex(u.Refuse, 'closed'):
                        u.apply(disk, release, 'nvme', False, None, tmp, binding)
                    self.assertEqual(target.read_bytes(), b'untouched target')
                    self.assertFalse(u.WRITING)
                finally:
                    binding.close()
                    other.close()
            finally:
                os.close(disk.fd)
                os.close(fd)


@unittest.skipUnless(hasattr(os, 'memfd_create') and hasattr(os, 'O_PATH') and
                     all(shutil.which(x) for x in ('mformat', 'mmd', 'mcopy', 'mdir', 'mdel', 'minfo', 'lz4')),
                     'requires Linux memfd, O_PATH, mtools and lz4')
class SharedEspIntegration(unittest.TestCase):
    def test_sealed_esp_consumption_binding_guards_and_cleanup(self):
        u = m.update
        with tempfile.TemporaryDirectory(prefix='gbos-sealed-esp-') as tmp:
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
            item = {'profile': 'kano', 'image': {'size': source.stat().st_size,
                    'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}}
            sourcefd = os.open(source, os.O_RDONLY)
            disk = u.Disk(str(target), write=True)
            work = root / 'work'
            work.mkdir()
            binding = other = None
            try:
                binding = u.SharedBinding(sourcefd, item, 'a' * 64, [sourcefd], str(work))
                other = u.SharedBinding(sourcefd, item, 'a' * 64, [sourcefd], str(work))
                release = binding.release
                self.assertIsInstance(release, u.SharedRelease)
                fd = binding._esp_fd
                seals = (fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
                self.assertEqual(fcntl.fcntl(fd, fcntl.F_GET_SEALS) & seals, seals)
                for action in (lambda: os.pwrite(fd, b'x', 0),
                               lambda: os.ftruncate(fd, os.fstat(fd).st_size - 512),
                               lambda: os.ftruncate(fd, os.fstat(fd).st_size + 512),
                               lambda: mmap.mmap(fd, 4096, access=mmap.ACCESS_WRITE)):
                    with self.assertRaises(OSError) as failure:
                        action()
                    self.assertIn(failure.exception.errno, (errno.EPERM, errno.EACCES))
                before = hashlib.sha256(target.read_bytes()).digest()
                # Real apply, fresh target: failure must precede even state writes.
                for invalid in (None, other):
                    with self.subTest(binding=invalid), self.assertRaises(u.Refuse):
                        u.apply(disk, release, 'nvme', False, None, str(work), invalid)
                    self.assertEqual(hashlib.sha256(target.read_bytes()).digest(), before)
                    self.assertFalse(u.WRITING)
                # The names previously used for mutable exchange are unrelated to
                # the sealed source and owned bytes used by real mtools operations.
                original = (release.kernel, release.ramdisk, release.bc, release.entries,
                            release.esp.read('/kernel.efi'), release.esp.read('/initrd.img'))
                for name in ('release-esp.img', 'fat-read', 'fat-write', 'ramdisk.cpio'):
                    (work / name).write_bytes(b'unrelated replacement')
                self.assertEqual(original, (release.kernel, release.ramdisk, release.bc, release.entries,
                                            release.esp.read('/kernel.efi'), release.esp.read('/initrd.img')))
                self.assertEqual(u.bc_get(release.bc, 'androidboot.slot_suffix'), '_a')
                u.apply(disk, release, 'nvme', False, None, str(work), binding)
                installed = u.Installed(disk, str(work))
                self.assertEqual(installed.current, 'b')
                for name in ('release-esp.img', 'fat-read', 'fat-write', 'ramdisk.cpio'):
                    self.assertEqual((work / name).read_bytes(), b'unrelated replacement')
                binding.close()
                with self.assertRaises(OSError):
                    os.fstat(fd)
                # A new target image is unnecessary: refusal occurs before reading it.
                after = hashlib.sha256(target.read_bytes()).digest()
                with self.assertRaisesRegex(u.Refuse, 'closed'):
                    u.apply(disk, release, 'nvme', False, None, str(work), binding)
                self.assertEqual(hashlib.sha256(target.read_bytes()).digest(), after)
                binding.close()  # idempotent, and source still belongs to caller
                self.assertEqual(os.fstat(sourcefd).st_size, item['image']['size'])
                captured = []
                original_create = os.memfd_create
                def capture(*args, **kwargs):
                    created = original_create(*args, **kwargs)
                    captured.append(created)
                    return created
                with patch.object(u.os, 'memfd_create', side_effect=capture), \
                     patch.object(u, 'SharedRelease', side_effect=ValueError('parse failed')):
                    with self.assertRaisesRegex(ValueError, 'parse failed'):
                        u.SharedBinding(sourcefd, item, 'a' * 64, [sourcefd], str(work))
                self.assertEqual(len(captured), 1)
                with self.assertRaises(OSError):
                    os.fstat(captured[0])
            finally:
                if binding is not None:
                    binding.close()
                if other is not None:
                    other.close()
                os.close(disk.fd)
                os.close(sourcefd)
                u.WRITING.clear()
                u.WINDOW.clear()
                u.STICK.clear()


if __name__ == '__main__':
    unittest.main()
