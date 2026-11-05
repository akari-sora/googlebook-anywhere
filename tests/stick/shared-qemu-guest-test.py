#!/usr/bin/env python3
"""Pure control-routing checks; no devices, QEMU, or physical admission."""
from contextlib import contextmanager, nullcontext, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


path = Path(__file__).with_name('shared-qemu-guest.py')
spec = importlib.util.spec_from_file_location('shared_qemu_guest', path)
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


def real_shared():
    source = path.parents[2] / 'install/stick/shared-source.py'
    module_spec = importlib.util.spec_from_file_location('real_shared', source)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


class GuestControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundle = Path(self.tmp.name)
        fixtures = self.bundle / 'fixtures'
        fixtures.mkdir()
        self.payloads = {profile: {'path': profile + '.img', 'size': 5, 'sha256': '1' * 64}
                         for profile in guest.PROFILES}
        raw = json.dumps({'payloads': [{'profile': name, 'image': info}
                                        for name, info in self.payloads.items()]}).encode()
        for name in ('catalog.json', 'fresh-catalog.json'):
            (fixtures / name).write_bytes(raw)
        self.request = {'schema': 1, 'action': 'fresh', 'expected_catalog_sha256': '2' * 64,
                        'expected_profile': 'kano', 'payloads': self.payloads}
        self.facts = {'identity': 'test-only'}
        self.catalog = types.SimpleNamespace(MAX_CATALOG=100000, schema=lambda raw: json.loads(raw)['payloads'],
                                             validate=Mock(return_value=nullcontext()),
                                             Refused=type('Refused', (ValueError,), {}), __file__=__file__)
        self.backend = types.SimpleNamespace(
            catalog=self.catalog, _observed_profile=Mock(return_value='kano'),
            observe_block_target=Mock(return_value=self.facts),
            _target_observation=lambda *_: (), _block_facts=lambda *_: {},
            install_observed_block=Mock(return_value={'profile': 'kano'}),
            apply_observed_block=Mock(return_value={'profile': 'kano'}),
            update=types.SimpleNamespace(Refuse=type('Refuse', (Exception,), {})), __file__=__file__)
        for target in (patch.object(guest, 'BUNDLE', self.bundle),
                       patch.object(guest, 'load', return_value=self.backend),
                       patch.object(guest, 'read_only_mount'),
                       patch.object(guest, 'activate_virtual_nvme'),
                       patch.object(guest, 'require_virtual_target')):
            target.start()
            self.addCleanup(target.stop)

    def test_changed_profile_binding_stops_before_target_observation(self):
        self.request['payloads'] = dict(self.payloads, kano={'path': 'wrong', 'size': 5, 'sha256': '1' * 64})
        with self.assertRaisesRegex(ValueError, 'mapping differs'):
            guest.run(self.request)
        self.backend.observe_block_target.assert_not_called()
        self.backend.install_observed_block.assert_not_called()
        guest.activate_virtual_nvme.assert_not_called()

    def test_lookalike_bad_digest_exception_is_not_a_pass(self):
        self.request.update(action='bad-catalog', expected_catalog_sha256='0' * 64)
        self.backend.install_observed_block.side_effect = ValueError('invalid_catalog_or_payload')
        with self.assertRaisesRegex(RuntimeError, 'unexpected backend refusal'):
            guest.run(self.request)
        args, kwargs = self.backend.install_observed_block.call_args
        self.assertEqual(args[1:4], ('fresh-catalog.json', '0' * 64, '/dev/nvme0n1'))
        self.assertEqual(kwargs['expected_target'], self.facts)

    def test_lookalike_unknown_dmi_exception_is_not_a_pass(self):
        self.backend._observed_profile.side_effect = ValueError('unknown')
        self.backend.install_observed_block.side_effect = ValueError('machine profile unknown')
        with self.assertRaisesRegex(RuntimeError, 'unexpected backend refusal'):
            guest.run(self.request)
        self.backend.install_observed_block.assert_called_once()

    def test_exact_real_backend_catalog_hash_failure_has_provenance(self):
        shared = real_shared()

        @contextmanager
        def resources(*_args):
            fixture = self.bundle / 'fixtures'
            rootfd = os.open(fixture, os.O_RDONLY)
            fd = os.open(fixture / 'fresh-catalog.json', os.O_RDONLY)
            try:
                shared.catalog.verify_file(rootfd, 'fresh-catalog.json', fd,
                                           shared.catalog.MAX_CATALOG, '0' * 64, collect=True)
            finally:
                os.close(fd)
                os.close(rootfd)
            yield

        with patch.object(shared, '_observed_block_resources', resources):
            try:
                shared.install_observed_block('x', 'x', '0' * 64, 'x', expected_target={})
            except shared.catalog.Refused as exc:
                self.assertEqual(guest.expected_backend_refusal(exc, shared, 'bad-catalog', 'kano', '0' * 64),
                                 ('BACKEND_CATALOG_HASH', 'CATALOG_DIGEST_MISMATCH'))
            else:
                self.fail('expected backend failure')

    def test_exact_real_backend_unknown_model_has_provenance(self):
        shared = real_shared()

        @contextmanager
        def resources(*_args):
            shared._observed_profile(self.bundle / 'missing-dmi')
            yield

        with patch.object(shared, '_observed_block_resources', resources):
            try:
                shared.install_observed_block('x', 'x', '2' * 64, 'x', expected_target={})
            except ValueError as exc:
                self.assertEqual(guest.expected_backend_refusal(exc, shared, 'fresh', None, '2' * 64),
                                 ('BACKEND_MODEL_SELECTION', 'UNRECOGNIZED_DMI'))
            else:
                self.fail('expected backend failure')

    def test_update_uses_update_catalog_and_observed_target_facts(self):
        self.request['action'] = 'update'
        result = guest.run(self.request)
        self.assertEqual(result['status'], 'OK')
        args, kwargs = self.backend.apply_observed_block.call_args
        self.assertEqual(args[1:4], ('catalog.json', '2' * 64, '/dev/nvme0n1'))
        self.assertEqual(kwargs['expected_target'], self.facts)
        self.backend.install_observed_block.assert_not_called()

    def test_control_rejects_unknown_or_missing_fields(self):
        self.request['target'] = '/dev/sda'
        with self.assertRaisesRegex(ValueError, 'invalid test control'):
            guest.run(self.request)
        self.backend.observe_block_target.assert_not_called()
        guest.activate_virtual_nvme.assert_not_called()

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            json.loads('{"schema":1,"schema":1}', object_pairs_hook=guest.unique_object)


class DeviceActivation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.devices = Path(self.tmp.name)
        pin = patch.object(guest, 'PCI_DEVICES', self.devices)
        pin.start()
        self.addCleanup(pin.stop)
        mock = patch.object(guest.subprocess, 'run')
        self.commands = mock.start()
        self.addCleanup(mock.stop)

    def pci(self, name, device_class, vendor='0x1b36', device='0x0010'):
        directory = self.devices / name
        directory.mkdir()
        for field, value in (('class', device_class), ('vendor', vendor), ('device', device)):
            (directory / field).write_text(value + '\n')

    def test_one_expected_controller_runs_fixed_commands_only(self):
        self.pci('0000:00:01.0', '0x010802')
        self.pci('0000:00:02.0', '0x030000')
        guest.activate_virtual_nvme()
        self.assertEqual(self.commands.call_args_list, [
            unittest.mock.call(['/usr/bin/modprobe', '--ignore-install', 'nvme_core'], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30),
            unittest.mock.call(['/usr/bin/modprobe', '--ignore-install', 'nvme'], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30),
            unittest.mock.call(['/usr/bin/udevadm', 'settle', '--timeout=30'], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30),
            unittest.mock.call(['/usr/bin/systemctl', '--no-ask-password', 'stop',
                                'serial-getty@ttyS0.service'], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30),
        ])

    def test_unknown_controller_and_unknown_class_refuse_before_modprobe(self):
        self.pci('0000:00:01.0', '0x010802', vendor='0x1234')
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            guest.activate_virtual_nvme()
        self.commands.assert_not_called()
        (self.devices / '0000:00:01.0' / 'class').write_text('unknown\n')
        with self.assertRaisesRegex(ValueError, 'unknown PCI class'):
            guest.activate_virtual_nvme()
        self.commands.assert_not_called()

    def test_additional_nvme_refuses_before_modprobe(self):
        self.pci('0000:00:01.0', '0x010802')
        self.pci('0000:00:02.0', '0x010802')
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            guest.activate_virtual_nvme()
        self.commands.assert_not_called()

    def test_modprobe_failure_stops_before_next_command(self):
        self.pci('0000:00:01.0', '0x010802')
        self.commands.side_effect = subprocess.CalledProcessError(1, 'modprobe')
        with self.assertRaises(subprocess.CalledProcessError):
            guest.activate_virtual_nvme()
        self.assertEqual(self.commands.call_count, 1)


class PassiveObservation(unittest.TestCase):
    def setUp(self):
        def target(value):
            return value

        def facts(value):
            return value

        self.target, self.facts = target, facts
        self.shared = types.SimpleNamespace(_target_observation=target, _block_facts=facts)

    def test_success_preserves_return_and_existing_profiler(self):
        prior = sys.getprofile()
        seen = []

        def original(frame, event, _arg):
            if frame.f_code is self.target.__code__:
                seen.append(event)

        value = tuple(range(14))
        sys.setprofile(original)
        try:
            returned = guest.observe_operation(self.shared, self.target, value)
            self.assertIs(returned, value)
            self.assertIs(sys.getprofile(), original)
            self.assertIn('return', seen)
        finally:
            sys.setprofile(prior)

    def test_exception_reports_changed_fields_and_bounded_mount_lines(self):
        first = list(range(14))
        first[12] = ('first\n' + '\n'.join('old' + str(i) for i in range(20)), 'pid1-before')
        latest = list(first)
        latest[12] = ('last\n' + '\n'.join('new' + str(i) for i in range(20)), 'pid1-after')

        def failing():
            self.target(tuple(first))
            self.facts({'size': 1, 'kind': 'nvme'})
            self.target(tuple(latest))
            self.facts({'size': 2, 'kind': 'nvme'})
            raise ValueError('backend refused')

        prior = sys.getprofile()
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(ValueError, 'backend refused'):
            guest.observe_operation(self.shared, failing)
        self.assertIs(sys.getprofile(), prior)
        prefix, raw = output.getvalue().strip().split(' ', 1)
        self.assertEqual(prefix, 'GBOS_QEMU_OBSERVATION')
        result = json.loads(raw)
        self.assertEqual(result['target_observation'], {'count': 2, 'fields_changed': ['mountinfo']})
        self.assertEqual(result['block_facts'], {'count': 2, 'fields_changed': ['size']})
        details = result['mountinfo']
        self.assertGreater(details['self']['added_count'], 12)
        self.assertLessEqual(sum(len(detail[key]) for detail in details.values()
                                 for key in ('added', 'removed')), 12)
        self.assertEqual(details['pid1']['removed_count'], 1)


if __name__ == '__main__':
    unittest.main()
