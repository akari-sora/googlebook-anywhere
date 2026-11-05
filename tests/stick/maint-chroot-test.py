#!/usr/bin/env python3
"""The host chroot tool is resolved before initramfs construction."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('maint_assemble', ROOT / 'install/stick/maint-assemble.py')
maint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maint)


class HostChrootTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chroot test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = self.root / 'chroot tool'
        self.tool.write_text('#!/bin/sh\nexit 0\n')
        self.tool.chmod(0o755)

    def test_absolute_executable_with_spaces_is_returned(self):
        with mock.patch.object(maint.shutil, 'which', return_value=str(self.tool)):
            self.assertEqual(maint.host_chroot_tool(), str(self.tool))

    def test_missing_relative_nonexecutable_or_directory_refused(self):
        nonexecutable = self.root / 'not-executable'
        nonexecutable.write_text('x')
        nonexecutable.chmod(0o644)
        for value in (None, 'bin/chroot', str(nonexecutable), str(self.root), str(self.root / 'missing')):
            with self.subTest(value=value), mock.patch.object(maint.shutil, 'which', return_value=value):
                with self.assertRaises(SystemExit):
                    maint.host_chroot_tool()

    def test_helper_rejects_bad_host_chroot_before_any_mount(self):
        helper = self.root / 'helper.sh'
        helper.write_text(maint.INITRAMFS_HELPER)
        for bad in ('relative/chroot', str(self.root / 'missing'), str(self.root)):
            with self.subTest(value=bad):
                result = subprocess.run(['/bin/sh', str(helper), str(self.root), '7.2.3', '/usr/bin/mkinitcpio', bad],
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('host chroot', result.stderr)
                self.assertNotIn('mount:', result.stderr)

    def test_helper_preserves_space_in_host_executable_path(self):
        root = self.root / 'image root'
        for part in ('proc', 'sys', 'dev', 'run', 'tmp', 'usr/bin'):
            (root / part).mkdir(parents=True, exist_ok=True)
        (root / 'usr/bin/depmod').write_text('#!/bin/sh\n')
        (root / 'usr/bin/depmod').chmod(0o755)
        work = self.root / 'work'
        work.mkdir()
        (work / 'dev-zero').write_bytes(b'0')
        (work / 'dev-urandom').write_bytes(b'R')
        self.tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$HOST_CHROOT_LOG"\n')
        self.tool.chmod(0o755)
        log = self.root / 'invocations'
        helper = self.root / 'helper.sh'
        self.assertGreater(maint.INITRAMFS_HELPER.count('/usr/bin/mount'), 1)
        # Neutralize every mount command; all other operations touch only the
        # disposable fixture, while the actual chroot invocations stay intact.
        helper.write_text(maint.INITRAMFS_HELPER.replace('/usr/bin/mount', ':').replace('/usr/bin/cp', '/bin/cp'))
        result = subprocess.run(['/bin/sh', str(helper), str(root), '7.2.3', '/usr/bin/mkinitcpio', str(self.tool)],
                                env={**os.environ, 'HOST_CHROOT_LOG': str(log)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().splitlines(), [str(root), '/usr/bin/depmod', '-a', '7.2.3',
                         str(root), '/usr/bin/mkinitcpio', '-k', '7.2.3', '-g', '/boot/initramfs-linux.img',
                         '-c', '/etc/mkinitcpio-gbos.conf'])

    def test_existing_initramfs_does_not_need_host_chroot(self):
        boot = self.root / 'boot'
        boot.mkdir()
        (boot / 'initramfs-linux.img').write_bytes(b'existing')
        with mock.patch.object(maint, 'host_chroot_tool', side_effect=AssertionError('unexpected lookup')):
            maint.build_initramfs(str(self.root))


if __name__ == '__main__':
    unittest.main()
