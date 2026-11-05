#!/usr/bin/env python3
"""Offline fixture tests for the opt-in, static USB evidence publisher."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'builder/lib'))
import kano_usb_diag as diag  # noqa: E402 - import the repository module after adding its path


class VendorInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = Path(self.tmp.name)
        (self.tree / 'etc/init').mkdir(parents=True)
        (self.tree / 'etc/selinux').mkdir()
        (self.tree / 'etc/selinux/vendor_file_contexts').write_text('/vendor/.* u:object_r:vendor_file:s0\n')
        (self.tree / 'etc/selinux/vendor_sepolicy.cil').write_text('; existing\n')
        self.meta = {'etc/init/fwupd.rc': {'mode': 0o644, 'uid': 0, 'gid': 0}}

    def test_vendor_actual_switch_off_and_on(self):
        vendor = (ROOT / 'builder/vendor.sh').read_text()
        start = vendor.index('_usb_smoke = os.environ.get("GBOS_USB_SMOKE", "")')
        end = vendor.index('# adb over TCP is opt-in:', start)
        code = vendor[start:end]
        ns = {'os': os, 'prof': {'model': {'id': 'kano'}}, 'tree': self.tree,
              'vmap': self.meta, 'Path': Path, 'kano_usb_diag': diag,
              'sys': SimpleNamespace(argv=['x', 'x', str(ROOT / 'builder/lib')])}
        original_context = (self.tree / 'etc/selinux/vendor_file_contexts').read_bytes()
        original_policy = (self.tree / 'etc/selinux/vendor_sepolicy.cil').read_bytes()
        with mock.patch.dict(os.environ, {}, clear=True):
            exec(code, ns)
        self.assertTrue(all(not (self.tree / p).exists() and p not in self.meta for p in diag.FILES))
        self.assertEqual((self.tree / 'etc/selinux/vendor_file_contexts').read_bytes(), original_context)
        self.assertEqual((self.tree / 'etc/selinux/vendor_sepolicy.cil').read_bytes(), original_policy)
        with mock.patch.dict(os.environ, {'GBOS_USB_SMOKE': '1'}, clear=True):
            exec(code, ns)
        self.assertTrue(all((self.tree / p).is_file() for p in diag.FILES))

    def test_switch_install_and_runtime_policy(self):
        # The vendor heredoc has an explicit conditional; install is never called in the unset case.
        vendor = (ROOT / 'builder/vendor.sh').read_text()
        self.assertIn('if _usb_smoke:', vendor)
        self.assertIn('kano_usb_diag.install(', vendor)
        self.assertTrue(all(not (self.tree / p).exists() for p in diag.FILES))
        diag.install(self.tree, self.meta, ROOT / 'builder/usb-smoke/collect-runtime.sh')
        self.assertTrue(all((self.tree / p).is_file() and p in self.meta for p in diag.FILES))
        self.assertIn(diag.CONTEXT, (self.tree / 'etc/selinux/vendor_file_contexts').read_text())
        cil = (self.tree / 'etc/selinux/vendor_sepolicy.cil').read_text()
        self.assertIn('(allow shell_202604 kano_usb_evidence (file (open read getattr)))', cil)
        self.assertIn('(allow shell_202604 kano_usb_evidence (dir (search getattr)))', cil)
        self.assertNotIn('typepermissive', cil)
        self.assertNotIn('shell_202604 kano_usb_evidence (file (write', cil)
        self.assertIn('on property:sys.boot_completed=1', diag.RC)
        self.assertIn('group shell', diag.RC)
        self.assertIn('mv "$T" "$D/runtime.txt"', diag.WRAPPER)
        self.assertNotIn('/data/', diag.WRAPPER)


class CollectorFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.sys = self.root / 'sys'
        self.blocks = self.sys / 'class/block'
        self.blocks.mkdir(parents=True)
        self.device = self.sys / 'devices/pci/usb1/1-1/block/sda'
        self.device.mkdir(parents=True)
        (self.device / 'dev').write_text('8:0\n')
        (self.device / 'slaves').mkdir()
        (self.device.parents[1] / 'serial').write_text('testserial\n')
        (self.blocks / 'sda').symlink_to(self.device)
        part = self.device / 'sda1'
        part.mkdir()
        (part / 'dev').write_text('8:1\n')
        (part / 'partition').write_text('1\n')
        (self.blocks / 'sda1').symlink_to(part)
        (self.root / 'boot_id').write_text('11111111-1111-4111-8111-111111111111\n')
        (self.root / 'mountinfo').write_text('1 0 8:1 / / rw - ext4 /dev/block/sda1 rw\n')
        binary = self.root / 'bin'
        binary.mkdir()
        getprop = binary / 'getprop'
        getprop.write_text('#!/bin/sh\ncase "$1" in ro.boot.boot_part_uuid) echo 22222222-2222-4222-8222-222222222222;; sys.boot_completed) echo 1;; esac\n')
        getprop.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'])
        script = (ROOT / 'builder/usb-smoke/collect-runtime.sh').read_text()
        script = script.replace('/proc/sys/kernel/random/boot_id', str(self.root / 'boot_id'))
        script = script.replace('/proc/1/mountinfo', str(self.root / 'mountinfo'))
        script = script.replace('/sys/class/block/', str(self.blocks) + '/')
        script = script.replace('/sys/devices/*)', str(self.sys / 'devices') + '/*)')
        script = script.replace('!= /sys ]', '!= ' + str(self.sys) + ' ]')
        self.script = self.root / 'collector.sh'
        self.script.write_text(script)

    def run_collector(self):
        return subprocess.run(['/bin/sh', str(self.script)], env=self.env, text=True, capture_output=True)

    def test_complete_topology_and_framing(self):
        result = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('KANO_USB_RUNTIME_V1\nBOOT_GUID '))
        self.assertIn('BEGIN_MOUNTINFO\n', result.stdout)
        self.assertIn('\nEND_MOUNTINFO\nBEGIN_BLOCKS\n', result.stdout)
        self.assertIn('NODE sda 8:0 ', result.stdout)
        self.assertIn('NODE sda1 8:1 ', result.stdout)
        self.assertTrue(result.stdout.endswith('END_BLOCKS\nCOMPLETE\n'))

    def assert_unread(self, reason):
        result = self.run_collector()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UNREAD ' + reason + '\n', result.stdout)
        self.assertNotIn('\nCOMPLETE\n', result.stdout)

    def test_partition_without_slaves_is_bound_to_parent(self):
        part = self.device / 'sda1'
        self.assertFalse((part / 'slaves').exists())
        result = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'NODE sda1 8:1 {part} sda - usb testserial\n', result.stdout)
        (part / 'slaves').mkdir()
        self.assertEqual(self.run_collector().returncode, 0)
        (part / 'slaves' / 'unexpected').touch()
        self.assert_unread('slaves')

    def test_partition_number_must_be_readable_positive_decimal(self):
        part = self.device / 'sda1'
        for value in ('', '0', '00', '-1', '1a', '1 2', '1\n2'):
            with self.subTest(value=value):
                (part / 'partition').write_text(value + '\n')
                self.assert_unread('parent')
        (part / 'partition').unlink()
        (part / 'partition').symlink_to(self.root / 'missing')
        self.assert_unread('parent')
        (part / 'partition').unlink()
        (part / 'partition').symlink_to(self.root / 'boot_id')
        self.assert_unread('parent')

    def test_unreadable_partition_fails_closed(self):
        cat = self.root / 'bin/cat'
        cat.write_text('#!/bin/sh\ncase "$1" in */sda1/partition) exit 1;; esac\nexec /bin/cat "$@"\n')
        cat.chmod(0o755)
        self.assert_unread('parent')

    def test_whole_device_slaves_are_still_emitted(self):
        virtual = self.sys / 'devices/virtual/block/dm-0'
        virtual.mkdir(parents=True)
        (virtual / 'dev').write_text('253:0\n')
        (virtual / 'slaves').mkdir()
        (virtual / 'slaves/sda').symlink_to(self.device)
        (self.blocks / 'dm-0').symlink_to(virtual)
        result = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'NODE dm-0 253:0 {virtual} - sda other -\n', result.stdout)

    def test_partition_parent_class_entry_must_exist_and_resolve_correctly(self):
        parent = self.blocks / 'sda'
        parent.unlink()
        self.assert_unread('parent')
        other = self.sys / 'devices/pci/sda'
        other.mkdir(parents=True)
        (other / 'dev').write_text('8:0\n')
        (other / 'slaves').mkdir()
        parent.symlink_to(other)
        self.assert_unread('parent')
        parent.unlink()
        parent.symlink_to(self.root / 'missing')
        self.assert_unread('block_entry')

    def test_partition_parent_must_be_whole_and_have_valid_distinct_dev(self):
        parent_dev = self.device / 'dev'
        for value in ('', 'bad', '8:1'):
            with self.subTest(value=value):
                parent_dev.write_text(value + '\n')
                result = self.run_collector()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('\nCOMPLETE\n', result.stdout)
        parent_dev.write_text('8:0\n')
        (self.device / 'partition').write_text('1\n')
        self.assert_unread('parent')

    def test_whole_device_still_requires_readable_slaves_directory(self):
        (self.device / 'slaves').rmdir()
        self.assert_unread('slaves')
        (self.device / 'slaves').symlink_to(self.root / 'missing')
        self.assert_unread('slaves')

    def test_partition_rejects_dangling_or_non_directory_slaves(self):
        slaves = self.device / 'sda1/slaves'
        slaves.symlink_to(self.root / 'missing')
        self.assert_unread('slaves')
        slaves.unlink()
        slaves.write_text('')
        self.assert_unread('slaves')

    def test_missing_or_malformed_node_fails_without_complete(self):
        for val in ('', 'garbage'):
            (self.device / 'dev').write_text(val)
            result = self.run_collector()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('UNREAD dev', result.stdout)
            self.assertNotIn('\nCOMPLETE\n', result.stdout)
        (self.device / 'dev').unlink()
        result = self.run_collector()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('\nCOMPLETE\n', result.stdout)

    def test_bad_boot_id_and_empty_mountinfo_fail(self):
        (self.root / 'boot_id').write_text('bad\n')
        self.assertNotEqual(self.run_collector().returncode, 0)
        (self.root / 'boot_id').write_text('11111111-1111-4111-8111-111111111111\n')
        (self.root / 'mountinfo').write_text('')
        result = self.run_collector()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UNREAD mountinfo', result.stdout)


class PublisherFixture(unittest.TestCase):
    SAMPLE = ('KANO_USB_RUNTIME_V1\nBOOT_GUID 22222222-2222-4222-8222-222222222222\n'
              'BOOT_ID 11111111-1111-4111-8111-111111111111\nBOOT_COMPLETED 1\n'
              'BEGIN_MOUNTINFO\n1 0 8:1 / / rw - ext4 /dev/sda1 rw\nEND_MOUNTINFO\n'
              'BEGIN_BLOCKS\nNODE sda 8:0 /sys/devices/usb1/1-1/sda - - usb testserial\n'
              'END_BLOCKS\nCOMPLETE\n')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.dest = self.root / 'evidence'
        self.dest.mkdir()
        self.bindir = self.root / 'bin'
        self.bindir.mkdir()
        for name in ('chown', 'restorecon'):
            self.stub(name, 'exit 0')
        self.collector = self.root / 'collector.sh'
        self.fixture = self.root / 'sample.txt'
        self.env = dict(os.environ, PATH=str(self.bindir) + os.pathsep + os.environ['PATH'])
        wrapper = diag.WRAPPER.replace('/dev/kano-usb-evidence', str(self.dest))
        wrapper = wrapper.replace('/system/bin/sh /vendor/etc/kano-usb-collect.sh',
                                  '/bin/sh ' + str(self.collector))
        self.wrapper = self.root / 'wrapper.sh'
        self.wrapper.write_text(wrapper)

    def stub(self, name, body):
        tool = self.bindir / name
        tool.write_text('#!/bin/sh\n' + body + '\n')
        tool.chmod(0o755)

    def run_publisher(self, content, status=0):
        self.fixture.write_text(content)
        self.collector.write_text('#!/bin/sh\ncat ' + str(self.fixture) + f'\nexit {status}\n')
        return subprocess.run(['/bin/sh', str(self.wrapper)], env=self.env, capture_output=True)

    def test_complete_output_is_published_once(self):
        result = self.run_publisher(self.SAMPLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dest / 'runtime.txt').read_text(), self.SAMPLE)
        self.assertEqual((self.dest / 'runtime.txt').stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.dest / 'failure.txt').exists())
        self.assertFalse((self.dest / '.runtime.tmp').exists())
        second = self.run_publisher('different output', 2)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual((self.dest / 'runtime.txt').read_text(), self.SAMPLE)
        self.assertFalse((self.dest / 'failure.txt').exists())

    def test_collector_failures_publish_only_allowlisted_categories(self):
        # Full stdout can include sensitive mount and topology data; none may
        # appear in the marker, including for unknown or injected reasons.
        sensitive = 'KANO_USB_RUNTIME_V1\nNODE secret-serial private-path\n'
        reasons = ('boot_guid', 'boot_id', 'boot_completed', 'properties', 'mountinfo',
                   'blocks', 'block_entry', 'name', 'dev', 'realpath', 'topology',
                   'parent', 'slaves', 'serial')
        for reason in (*reasons, 'secret-serial', 'dev extra', ''):
            with self.subTest(reason=reason):
                if (self.dest / '.started').exists():
                    (self.dest / '.started').rmdir()
                (self.dest / 'failure.txt').unlink(missing_ok=True)
                output = sensitive + 'UNREAD ' + reason + '\n'
                result = self.run_publisher(output, 2)
                expected = 'UNREAD collector_' + reason if reason in reasons else 'UNREAD collector'
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.dest / 'failure.txt').read_text(), expected + '\n')
                self.assertEqual((self.dest / 'failure.txt').stat().st_mode & 0o777, 0o640)
                self.assertFalse((self.dest / 'runtime.txt').exists())
                self.assertFalse((self.dest / '.runtime.tmp').exists())
                self.assertFalse((self.dest / '.failure.tmp').exists())

    def test_success_exit_with_incomplete_or_extra_framing_is_refused(self):
        for content in (self.SAMPLE[:-9], self.SAMPLE + 'extra\n'):
            with self.subTest(content=content[-20:]):
                if (self.dest / '.started').exists():
                    (self.dest / '.started').rmdir()
                (self.dest / 'failure.txt').unlink(missing_ok=True)
                result = self.run_publisher(content)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.dest / 'failure.txt').read_text(), 'UNREAD framing\n')
                self.assertFalse((self.dest / 'runtime.txt').exists())
                self.assertFalse((self.dest / '.runtime.tmp').exists())

    def test_failed_first_snapshot_cannot_be_replaced(self):
        first = self.run_publisher('KANO_USB_RUNTIME_V1\nUNREAD dev\n', 2)
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual((self.dest / 'failure.txt').read_text(), 'UNREAD collector_dev\n')
        second = self.run_publisher(self.SAMPLE)
        self.assertNotEqual(second.returncode, 0)
        self.assertFalse((self.dest / 'runtime.txt').exists())
        self.assertEqual((self.dest / 'failure.txt').read_text(), 'UNREAD collector_dev\n')

    def test_nonzero_collector_refuses_even_complete_framing(self):
        result = self.run_publisher(self.SAMPLE, 2)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dest / 'runtime.txt').exists())
        self.assertEqual((self.dest / 'failure.txt').read_text(), 'UNREAD collector\n')

    def test_failed_marker_publication_still_refuses_runtime(self):
        self.stub('restorecon', 'exit 1')
        result = self.run_publisher('KANO_USB_RUNTIME_V1\nUNREAD mountinfo\n', 2)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dest / 'failure.txt').exists())
        self.assertFalse((self.dest / 'runtime.txt').exists())
        self.assertFalse((self.dest / '.runtime.tmp').exists())
        self.assertEqual((self.dest / '.failure.tmp').read_text(), 'UNREAD collector_mountinfo\n')

    def test_failed_runtime_publication_is_not_success(self):
        self.stub('restorecon', 'exit 1')
        result = self.run_publisher(self.SAMPLE)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dest / 'runtime.txt').exists())
        self.assertFalse((self.dest / 'failure.txt').exists())



if __name__ == '__main__':
    unittest.main()
