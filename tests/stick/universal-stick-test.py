#!/usr/bin/env python3
"""Synthetic file-only checks for the shared maintenance prototype."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('universal_stick', ROOT / 'install/stick/universal-stick.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SharedMediumTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'root'
        self.boot = self.base / 'boot'
        self.bundle = self.base / 'bundle'
        for directory in (self.root / 'boot', self.boot, self.bundle / 'payloads'):
            directory.mkdir(parents=True)
        rules = self.root / 'usr/lib/tmpfiles.d'
        rules.mkdir(parents=True)
        (rules / 'root.conf').write_text('# pinned\nz / 555 - - -\nd /keep 0755 - - -\n')
        self.kernel = b'kernel example'
        self.initrd = b'initrd example'
        for name, data in (('vmlinuz-linux', self.kernel), ('initramfs-linux.img', self.initrd)):
            (self.root / 'boot' / name).write_bytes(data)
            (self.boot / name).write_bytes(data)
        self.efi = self.base / 'bootx64.efi'
        self.efi.write_bytes(b'UEFI test bootloader')
        payloads = []
        for profile, raw in (('kano', b'KANO payload'), ('amd-hx370', b'HX370 payload')):
            path = 'payloads/' + profile + '.img'
            (self.bundle / path).write_bytes(raw)
            payloads.append({'profile': profile, 'build_id': 'synthetic',
                             'image': {'path': path, 'size': len(raw), 'sha256': sha(raw)},
                             'boot_entries': ['expected entry'], 'hardware_validation': 'unvalidated'})
        self.raw = json.dumps({'schema': 1, 'payloads': payloads}).encode()
        (self.bundle / 'catalog.json').write_bytes(self.raw)
        self.args = type('Args', (), {'root': str(self.root), 'xbootldr': str(self.boot),
            'efi': str(self.efi), 'efi_sha256': sha(self.efi.read_bytes()),
            'kernel_sha256': sha(self.kernel), 'initrd_sha256': sha(self.initrd),
            'bundle': str(self.bundle), 'catalog_sha256': sha(self.raw),
            'out': str(self.base / 'out.img'), 'max_bytes': None})()

    def test_capacity_is_based_on_both_inputs(self):
        root, data, total = u.capacity(1_000_000_000, 38_138_987_520)
        self.assertGreater(data, 38_138_987_520)
        self.assertGreater(total, 32_000_000_000)
        self.assertEqual(total, u.MIB + u.ESP_SIZE + root + data + u.MIB)
        for values in ((0, 1), (1, 0), (-1, 1), (1, 2**50)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                u.capacity(*values)

    def test_boot_entry_is_readonly_and_diagnostic_only(self):
        entry = u.make_entry('00000000-0000-0000-0000-000000000001').decode()
        self.assertIn('root=PARTUUID=00000000-0000-0000-0000-000000000001 ro rootflags=noload', entry)
        self.assertIn('lockdown=integrity', entry)
        self.assertIn('efi=noruntime', entry)
        self.assertIn('fsck.mode=skip', entry)
        self.assertIn('systemd.gpt_auto=0', entry)
        self.assertIn('initcall_blacklist=', entry)
        for forbidden in ('gbos.install', 'gbos.update', 'gbos-nvme', 'xe.force_probe', '@'):
            self.assertNotIn(forbidden, entry)

    def test_overlay_contains_diagnostic_and_menu_with_planner(self):
        digest = 'e' * 64
        u.setup_root(self.root, digest, '00000000-0000-0000-0000-000000000002')
        self.assertEqual((self.root / 'etc/gbos-universal/catalog.sha256').read_text(), digest + '\n')
        fstab = (self.root / 'etc/fstab').read_text()
        self.assertIn('ro,noload,nosuid,nodev,noexec,nofail', fstab)
        self.assertTrue((self.root / 'etc/systemd/system/multi-user.target.wants/gbos-universal-diagnostic.service').is_symlink())
        unit = (self.root / 'etc/systemd/system/gbos-universal-diagnostic.service').read_text()
        self.assertIn('RequiresMountsFor=/run/gbos-payload', unit)
        self.assertIn('After=local-fs.target', unit)
        self.assertTrue((self.root / 'etc/systemd/system/multi-user.target.wants/gbos-universal-menu.service').is_symlink())
        menu_unit = (self.root / 'etc/systemd/system/gbos-universal-menu.service').read_text()
        self.assertIn('Requires=gbos-universal-diagnostic.service', menu_unit)
        self.assertIn('After=gbos-universal-diagnostic.service', menu_unit)
        self.assertIn('StandardInput=tty', menu_unit)
        self.assertIn('TTYPath=/dev/tty1', menu_unit)
        for name in ('catalog.py', 'detect-model.py', 'universal-diagnostic.py', 'shared-source.py', 'universal-menu.py'):
            self.assertTrue((self.root / 'opt/googlebook/install/stick' / name).is_file())
        for name in ('gbos-update.py', 'gbos-fresh.py'):
            self.assertTrue((self.root / 'opt/googlebook/install' / name).is_file())
        self.assertFalse((self.root / 'usr/local/sbin/gbos-install').exists())
        self.assertTrue((self.root / 'opt/googlebook/profiles/kano/profile.toml').exists())

    def test_inherited_writable_fstab_mount_is_removed(self):
        etc = self.root / 'etc'
        etc.mkdir()
        (etc / 'fstab').write_text('/dev/nvme0n1p1 /srv/target ext4 rw 0 2\n')
        u.setup_root(self.root, 'e' * 64, '00000000-0000-0000-0000-000000000002')
        active = [line for line in (etc / 'fstab').read_text().splitlines() if line and not line.startswith('#')]
        self.assertEqual(active, ['PARTUUID=00000000-0000-0000-0000-000000000002 /run/gbos-payload '
                                  'ext4 ro,noload,nosuid,nodev,noexec,nofail 0 0',
                                  'tmpfs /var tmpfs rw,nodev,nosuid,mode=0755,size=256M 0 0'])

    def test_overlay_rejects_existing_installer_and_symlinked_directory(self):
        target = self.root / 'usr/local/sbin'
        target.mkdir(parents=True)
        (target / 'gbos-install').write_text('bad')
        with self.assertRaises(ValueError):
            u.setup_root(self.root, 'e' * 64, 'guid')
        (target / 'gbos-install').unlink()
        (self.root / 'etc').symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            u.setup_root(self.root, 'e' * 64, 'guid')

    def test_tmpfiles_override_preserves_unrelated_rules(self):
        u.setup_root(self.root, 'e' * 64, 'guid')
        self.assertEqual((self.root / 'etc/tmpfiles.d/root.conf').read_text(),
                         '# pinned\nd /keep 0755 - - -\n')

    def test_confined_preparation_commands_and_marker_order(self):
        (self.root / 'etc').mkdir()
        calls = []
        def run(argv, root):
            self.assertFalse((self.root / 'etc/.updated').exists())
            calls.append((argv, root))
        with (mock.patch.object(u.sys, 'platform', 'linux'),
              mock.patch.object(u.os, 'geteuid', return_value=0),
              mock.patch.object(u.shutil, 'which', side_effect=lambda n: '/usr/bin/' + n),
              mock.patch.object(u.os, 'access', return_value=True),
              mock.patch.object(u, 'run_owned', side_effect=run)):
            u.prepare_guest(self.root)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1][0][-4:], ['/usr/bin/systemd-tmpfiles', '--create', '--boot', '--prefix=/etc'])
        self.assertTrue((self.root / 'etc/.updated').exists())
        for args, root in calls:
            self.assertIn('--net', args)
            self.assertIn('--pid', args)
            self.assertIn('--propagation', args)
            self.assertIn('--kill-child=SIGKILL', args)
            self.assertIn(str(u.HERE / 'universal-guest-helper.py'), args)
            self.assertEqual(root, self.root)

    def test_guest_helper_allows_only_boot_scoped_tmpfiles(self):
        spec = importlib.util.spec_from_file_location('guest_helper_test',
                   ROOT / 'install/stick/universal-guest-helper.py')
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        good = ['/usr/bin/systemd-tmpfiles', '--create', '--boot', '--prefix=/etc']
        with mock.patch.object(sys, 'argv', ['helper', '/', *good]):
            with self.assertRaisesRegex(ValueError, 'owned staging root'):
                helper.main()
        for command in (good[:2] + good[3:], good + ['--remove'], good[:-1]):
            with self.subTest(command=command), mock.patch.object(sys, 'argv', ['helper', '/', *command]):
                with self.assertRaisesRegex(ValueError, 'unapproved guest command'):
                    helper.main()

    def test_preparation_failure_does_not_mark_updated(self):
        (self.root / 'etc').mkdir()
        calls = []
        def fail_tmpfiles(argv, root):
            calls.append(argv)
            if argv[-4:] == ['/usr/bin/systemd-tmpfiles', '--create', '--boot', '--prefix=/etc']:
                raise subprocess.CalledProcessError(1, 'guest')
        with (mock.patch.object(u.sys, 'platform', 'linux'),
              mock.patch.object(u.os, 'geteuid', return_value=0),
              mock.patch.object(u.shutil, 'which', side_effect=lambda n: '/usr/bin/' + n),
              mock.patch.object(u.os, 'access', return_value=True),
              mock.patch.object(u, 'run_owned', side_effect=fail_tmpfiles)):
            with self.assertRaises(subprocess.CalledProcessError):
                u.prepare_guest(self.root)
        self.assertFalse((self.root / 'etc/.updated').exists())
        self.assertEqual(len(calls), 2)

    def test_owned_exited_leader_does_not_signal_reaped_group(self):
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
        with mock.patch.object(u.os, 'killpg', wraps=os.killpg) as kill:
            u.run_owned([sys.executable, '-c', 'pass'], self.base, timeout=5)
        kill.assert_not_called()
        self.assertEqual(before, {sig: signal.getsignal(sig) for sig in before})

    def test_owned_timeout_reaps_child(self):
        pidfile = self.base / 'child.pid'
        code = "import os,time,sys; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
        with self.assertRaises(subprocess.TimeoutExpired):
            u.run_owned([sys.executable, '-c', code, str(pidfile)], self.base, timeout=0.5)
        pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_owned_interruption_reaps_child(self):
        pidfile = self.base / 'interrupt.pid'
        code = "import os,time,sys; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
        runner = ("import importlib.util,sys; spec=importlib.util.spec_from_file_location('u',sys.argv[1]); "
                  "u=importlib.util.module_from_spec(spec); spec.loader.exec_module(u); "
                  "u.run_owned([sys.executable,'-c',sys.argv[3],sys.argv[4]],sys.argv[2])")
        proc = subprocess.Popen([sys.executable, '-c', runner, str(ROOT / 'install/stick/universal-stick.py'),
                                 str(self.base), code, str(pidfile)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pidfile.exists())
            pid = int(pidfile.read_text())
            os.kill(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_fd_copy_enforces_hash_and_size(self):
        source = self.bundle / 'payloads/kano.img'
        fd = os.open(source, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        u.copy_checked(fd, self.base / 'good', sha(source.read_bytes()), source.stat().st_size)
        with self.assertRaises(ValueError):
            u.copy_checked(fd, self.base / 'bad', '0' * 64, source.stat().st_size)

    def test_corrupt_or_swapped_payload_refused(self):
        path = self.bundle / 'payloads/kano.img'
        path.write_bytes((self.bundle / 'payloads/amd-hx370.img').read_bytes())
        with self.assertRaises((ValueError, OSError)):
            with u.CAT.validate(str(self.bundle), 'catalog.json', sha(self.raw)):
                pass

    def test_runtime_diagnostics_refuse_writable_mount_and_detect_valid_catalog(self):
        u.setup_root(self.root, sha(self.raw), '00000000-0000-0000-0000-000000000002')
        diagnostic_path = self.root / 'opt/googlebook/install/stick/universal-diagnostic.py'
        spec = importlib.util.spec_from_file_location('runtime_diagnostic', diagnostic_path)
        diagnostic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diagnostic)
        output = io.StringIO()
        with (mock.patch.object(diagnostic, 'BASE', self.root / 'opt/googlebook'),
              mock.patch.object(diagnostic, 'BUNDLE', self.bundle),
              mock.patch.object(diagnostic, 'DIGEST', self.root / 'etc/gbos-universal/catalog.sha256'),
              mock.patch.object(diagnostic, 'health', return_value={'result': 'HEALTHY'}),
              mock.patch.object(diagnostic.subprocess, 'run',
                                return_value=subprocess.CompletedProcess([], 0, stdout='ro,noload')),
              redirect_stdout(output)):
            self.assertEqual(diagnostic.main(), 0)
        self.assertIn('CATALOG_PAYLOADS_VERIFIED', output.getvalue())
        self.assertIn('"write_allowed": false', output.getvalue())
        output = io.StringIO()
        with (mock.patch.object(diagnostic.subprocess, 'run',
                                return_value=subprocess.CompletedProcess([], 0, stdout='rw,relatime')),
              redirect_stdout(output)):
            self.assertEqual(diagnostic.main(), 1)
        self.assertIn('Payload integrity: UNREAD OR REFUSED', output.getvalue())

    def test_health_checks_all_mounts_and_failed_units(self):
        path = ROOT / 'install/stick/universal-diagnostic.py'
        spec = importlib.util.spec_from_file_location('health_test', path)
        d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(d)
        def reply(args, **kwargs):
            if 'findmnt' in args[0]:
                mount = args[args.index('--mountpoint') + 1]
                value = 'tmpfs rw,nodev,nosuid' if mount == '/var' else 'ext4 ro,noload'
            elif 'show' in args:
                value = 'LoadState=loaded\nResult=success\nExecMainStatus=0\nExecMainStartTimestampMonotonic=123\nConditionResult=yes\n'
            else:
                value = ''
            return subprocess.CompletedProcess(args, 0, stdout=value)
        with mock.patch.object(d.subprocess, 'run', side_effect=reply):
            self.assertEqual(d.health()['result'], 'HEALTHY')
        def failed(args, **kwargs):
            result = reply(args, **kwargs)
            if 'systemd-tmpfiles-setup.service' in args:
                result.stdout = 'LoadState=loaded\nResult=success\nExecMainStatus=73\nExecMainStartTimestampMonotonic=123\nConditionResult=yes\n'
            return result
        with mock.patch.object(d.subprocess, 'run', side_effect=failed):
            with self.assertRaises(ValueError):
                d.health()
        def never_started(args, **kwargs):
            result = reply(args, **kwargs)
            if 'systemd-tmpfiles-setup.service' in args:
                result.stdout = result.stdout.replace('Monotonic=123', 'Monotonic=0')
            return result
        with mock.patch.object(d.subprocess, 'run', side_effect=never_started):
            with self.assertRaises(ValueError):
                d.health()
        def legitimately_skipped(args, **kwargs):
            result = reply(args, **kwargs)
            if 'systemd-sysusers.service' in args:
                result.stdout = result.stdout.replace('Monotonic=123', 'Monotonic=0').replace('ConditionResult=yes', 'ConditionResult=no')
            return result
        with mock.patch.object(d.subprocess, 'run', side_effect=legitimately_skipped):
            self.assertEqual(d.health()['result'], 'HEALTHY')
        def forbidden_skip(args, **kwargs):
            result = reply(args, **kwargs)
            if 'systemd-tmpfiles-setup.service' in args:
                result.stdout = result.stdout.replace('Monotonic=123', 'Monotonic=0').replace('ConditionResult=yes', 'ConditionResult=no')
            return result
        with mock.patch.object(d.subprocess, 'run', side_effect=forbidden_skip):
            with self.assertRaises(ValueError):
                d.health()
        def bad_var(args, **kwargs):
            result = reply(args, **kwargs)
            if '/var' in args:
                result.stdout = 'ext4 rw'
            return result
        with mock.patch.object(d.subprocess, 'run', side_effect=bad_var):
            with self.assertRaises(ValueError):
                d.health()

    def make_records(self):
        payloads = json.loads(self.raw)['payloads']
        result = {}
        for item in payloads:
            profile, image = item['profile'], item['image']
            record = {'schema': 1, 'profile': profile,
                      'profile_sha256': sha((ROOT / 'profiles' / profile / 'profile.toml').read_bytes()),
                      'declared_build_id': item['build_id'], 'build_id_basis': 'selected_pins',
                      'image': {'basename': Path(image['path']).name, 'size': image['size'], 'sha256': image['sha256']},
                      'selftest': 'passed', 'not_run': [],
                      'options': {'entry': 'lapis-quiet.conf', 'editor': 'no', 'ec': [], 'adb': False,
                                  'usb_smoke': False, 'model_override': False},
                      'pristine': 'NOT_PROVEN', 'input_coverage': 'INCOMPLETE',
                      'authority': 'UNSIGNED_BUILDER_SELF_REPORT'}
            raw = u.RECORD.encode(record)
            path = self.base / (profile + '.json')
            path.write_bytes(raw)
            arg = 'kano' if profile == 'kano' else 'hx370'
            setattr(self.args, arg + '_build_record', str(path))
            setattr(self.args, arg + '_build_record_sha256', sha(raw))
            result[profile] = (path, raw, record)
        return result

    def test_recorded_assembly(self):
        self.record_fixture = self.make_records()
        self.test_regular_file_assembly_with_simulated_fs_tools()

    def test_record_options_and_binding_refused_before_staging(self):
        fixtures = self.make_records()
        with (mock.patch.object(u.sys, 'platform', 'linux'), mock.patch.object(u.os, 'geteuid', return_value=0),
              mock.patch.object(u.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name),
              mock.patch.object(u.shutil, 'copytree') as copy):
            self.args.hx370_build_record_sha256 = None
            with self.assertRaises(ValueError): u.build(self.args)
            self.args.hx370_build_record_sha256 = sha(fixtures['amd-hx370'][1])
            path, raw, record = fixtures['kano']
            for key, value in [('profile', 'amd-hx370'), ('declared_build_id', 'wrong'),
                               ('profile_sha256', '0' * 64), ('selftest', 'skipped')]:
                bad = dict(record, **{key: value})
                data = u.RECORD.encode(bad)
                path.write_bytes(data)
                self.args.kano_build_record_sha256 = sha(data)
                with self.subTest(key=key), self.assertRaises(ValueError): u.build(self.args)
            for key, value in [('basename', 'wrong.img'), ('size', 999), ('sha256', '0' * 64)]:
                bad = dict(record, image=dict(record['image'], **{key: value}))
                data = u.RECORD.encode(bad)
                path.write_bytes(data)
                self.args.kano_build_record_sha256 = sha(data)
                with self.subTest(image=key), self.assertRaises(ValueError): u.build(self.args)
            copy.assert_not_called()

    def test_regular_file_assembly_with_simulated_fs_tools(self):
        mtools_files = {}
        mkfs_contents = {}
        staged_records = {}
        real_run = subprocess.run
        def fake_run(args, **kwargs):
            argv = list(map(str, args))
            tool = Path(argv[0]).name
            if tool == 'mcopy':
                mtools_files[argv[-1]] = Path(argv[-2]).read_bytes()
            if tool == 'mtype':
                return subprocess.CompletedProcess(argv, 0, stdout=mtools_files[argv[-1]])
            if tool == 'mke2fs':
                root = Path(argv[argv.index('-d') + 1])
                image = Path(argv[-2])
                if image.name == 'root.img' and hasattr(self, 'record_fixture'):
                    for profile in self.record_fixture:
                        staged_records[profile] = (root / 'etc/gbos-universal/build-records' / (profile + '.json')).read_bytes()
                mkfs_contents[image.name] = sorted(str(p.relative_to(root)) for p in root.rglob('*'))
                with image.open('r+b') as f:
                    f.write(b'fake ext4 image')
            return subprocess.CompletedProcess(argv, 0, stdout=b'')
        def fake_prepare(root):
            if hasattr(self, 'record_fixture'):
                for path, _, _ in self.record_fixture.values():
                    path.unlink()
                    path.write_bytes(b'replaced after validation')
            for name in ('.updated', 'locale.conf', 'vconsole.conf'):
                (root / 'etc' / name).write_text('fixture')
        with (mock.patch.object(u.sys, 'platform', 'linux'),
              mock.patch.object(u.os, 'geteuid', return_value=0),
              mock.patch.object(u.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name),
              mock.patch.object(u.subprocess, 'run', side_effect=fake_run),
              mock.patch.object(u, 'prepare_guest', side_effect=fake_prepare),
              mock.patch.object(u, 'verify_debugfs') as readback,
              mock.patch.object(u, 'ESP_SIZE', 8192),
              mock.patch.object(u, 'capacity', return_value=(8192, 8192, 2 * u.MIB + 3 * 8192))):
            u.build(self.args)
        image = Path(self.args.out)
        self.assertTrue(image.is_file())
        self.assertTrue(Path(str(image) + '.not-self-tested').is_file())
        self.assertEqual(u.STICK.LAYOUT.load(str(image))[0], image.stat().st_size // 512)
        self.assertEqual(readback.call_count, 24 if hasattr(self, 'record_fixture') else 20)
        if hasattr(self, 'record_fixture'):
            self.assertEqual(staged_records, {p: values[1] for p, values in self.record_fixture.items()})
        expected_readback = {'etc/systemd/system/gbos-universal-menu.service',
                             'opt/googlebook/install/gbos-update.py', 'opt/googlebook/install/gbos-fresh.py'}
        expected_readback.update('opt/googlebook/install/stick/' + name for name in
                                 ('catalog.py', 'detect-model.py', 'universal-diagnostic.py', 'shared-source.py', 'universal-menu.py'))
        self.assertTrue(expected_readback.issubset({call.args[2] for call in readback.call_args_list}))
        self.assertTrue({'etc/locale.conf', 'etc/vconsole.conf'}.issubset(
            {call.args[2] for call in readback.call_args_list}))
        self.assertEqual(mtools_files['::/EFI/BOOT/BOOTX64.EFI'], self.efi.read_bytes())
        self.assertIn(b'auto-entries no', mtools_files['::/loader/loader.conf'])
        self.assertNotIn(b'gbos.install', mtools_files['::/loader/entries/gbos-universal.conf'])
        self.assertIn('payloads/kano.img', mkfs_contents['data.img'])
        self.assertIn('payloads/amd-hx370.img', mkfs_contents['data.img'])
        self.assertIn('etc/gbos-universal/catalog.sha256', mkfs_contents['root.img'])
        self.assertNotIn('usr/local/sbin/gbos-install', mkfs_contents['root.img'])
        del real_run

    def test_output_existing_rejected_without_touching_it(self):
        Path(self.args.out).write_bytes(b'keep me')
        with (mock.patch.object(u.sys, 'platform', 'linux'), mock.patch.object(u.os, 'geteuid', return_value=0)):
            with self.assertRaises(ValueError):
                u.build(self.args)
        self.assertEqual(Path(self.args.out).read_bytes(), b'keep me')

    def test_capacity_refused_before_output_created(self):
        self.args.max_bytes = 1
        with (mock.patch.object(u.sys, 'platform', 'linux'), mock.patch.object(u.os, 'geteuid', return_value=0),
              mock.patch.object(u.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name)):
            with self.assertRaises(ValueError):
                u.build(self.args)
        self.assertFalse(Path(self.args.out).exists())

    def test_output_inside_input_rejected(self):
        self.args.out = str(self.root / 'out.img')
        with (mock.patch.object(u.sys, 'platform', 'linux'), mock.patch.object(u.os, 'geteuid', return_value=0),
              mock.patch.object(u.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name)):
            with self.assertRaises(ValueError):
                u.build(self.args)
        self.assertFalse(Path(self.args.out).exists())


if __name__ == '__main__':
    unittest.main()
