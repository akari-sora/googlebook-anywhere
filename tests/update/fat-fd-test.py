#!/usr/bin/env python3
"""FAT operations keep their target disk descriptor across mtools subprocesses."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('gbos_update_fd_test', ROOT / 'install/gbos-update.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class DescriptorTests(unittest.TestCase):
    def test_each_mtools_operation_passes_only_its_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'disk'
            image.write_bytes(bytes(4096))
            disk = u.Disk(str(image), write=True)
            fat = u.Fat(disk.vol(512), tmp, fd=disk.fd)
            calls = []
            def fake_run(cmd, **kwargs):
                calls.append((cmd, kwargs))
                if cmd[0] == 'mdir' and '-b' in cmd:
                    return subprocess.CompletedProcess(cmd, 0, '', '')
                if cmd[0] == 'minfo':
                    output = 'cluster size: 1 sectors\nsector size: 512\n'
                elif cmd[0] == 'mdir':
                    output = '1024 bytes free\n'
                else:
                    output = ''
                if cmd[0] == 'mcopy' and '-n' in cmd:
                    output = 'x'
                return subprocess.CompletedProcess(cmd, 0, output if kwargs.get('text') else output.encode(), '')
            try:
                with patch.object(u.subprocess, 'run', side_effect=fake_run):
                    fat.ls('/')
                    fat.read('/file')
                    fat.write('/dir/file', b'x')
                    with patch.object(fat, 'exists', return_value=True):
                        fat.delete('/file')
                    fat.free()
                self.assertEqual({c[0][0] for c in calls}, {'mdir', 'mcopy', 'mmd', 'mdel', 'minfo'})
                self.assertTrue(all(c[1]['pass_fds'] == (disk.fd,) for c in calls))
                self.assertTrue(all(f'/proc/self/fd/{disk.fd}@@512' in c[0] for c in calls))
                read = [c for c in calls if c[0][0] == 'mcopy' and '-n' in c[0]]
                write = [c for c in calls if c[0][0] == 'mcopy' and '-o' in c[0]]
                self.assertTrue(all(c[0][-1] == '-' for c in read))
                self.assertEqual(write[0][0][-2], '-')
                self.assertEqual(write[0][1]['input'], b'x')
            finally:
                os.close(disk.fd)


@unittest.skipUnless(os.path.isdir('/proc/self/fd') and all(shutil.which(x) for x in
    ('mformat', 'mcopy', 'mdir', 'mmd', 'mdel', 'minfo')), 'requires Linux /proc and mtools')
class RealMtoolsTests(unittest.TestCase):
    def test_replaced_path_keeps_reads_writes_and_space_on_pinned_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, original, replacement = (root / x for x in ('target', 'original', 'replacement'))
            offset = 1048576
            for image, label in ((path, 'ORIGINAL'), (replacement, 'REPLACED')):
                with image.open('wb') as f:
                    f.truncate(8 * 1024 * 1024)
                subprocess.run(['mformat', '-i', f'{image}@@{offset}', '-v', label,
                    '-T', '14336', '-h', '2', '-s', '32', '::'], check=True)
            disk = u.Disk(str(path), write=True)
            fat = u.Fat(disk.vol(offset), tmp, fd=disk.fd)
            try:
                os.rename(path, original)
                os.rename(replacement, path)
                before = path.read_bytes()
                fat.write('/dir/example.txt', b'pinned')
                self.assertIn('example.txt', fat.ls('/dir'))
                self.assertEqual(fat.read('/dir/example.txt'), b'pinned')
                self.assertGreater(fat.free()[0], 0)
                fat.delete('/dir/example.txt')
                self.assertNotIn('example.txt', fat.ls('/dir'))
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(os.fstat(disk.fd).st_ino, original.stat().st_ino)
            finally:
                os.close(disk.fd)
            with self.assertRaisesRegex(RuntimeError, r'mcopy.*failed'):
                fat.read('/dir/example.txt')


if __name__ == '__main__':
    unittest.main()
