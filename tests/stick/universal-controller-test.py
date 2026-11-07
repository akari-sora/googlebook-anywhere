import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

path = Path(__file__).resolve().parents[2] / 'install/stick/universal-controller.py'
spec = importlib.util.spec_from_file_location('controller', path)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.source = ('kano', '16658739', 'a' * 64)
        self.facts = {'rdev': '259:0', 'syspath': '/sys/devices/pci/nvme0/nvme0n1',
                      'nbytes': 1000000000, 'logical_sector_size': 512,
                      'identity_kind': 'nvme-namespace', 'device_identity': 'b' * 64}
        self.plan = {'result': 'PLAN_OBSERVED', 'write_allowed': False, 'profile': 'kano',
                     'build_id': '16658739', 'current_slot': 'a', 'target_slot': 'b'}
        self.snapshot = Mock(side_effect=lambda: self.source)
        self.shared = Mock()
        self.shared.observe_block_target.side_effect = lambda _: dict(self.facts)
        self.shared.plan_observed_block.side_effect = lambda *a, **k: dict(self.plan)
        self.shared.install_observed_block.side_effect = lambda *a, **k: {
            'result': 'INSTALLED_OBSERVED_BLOCK', 'profile': self.source[0], 'build_id': self.source[1]}
        self.shared.apply_observed_block.side_effect = lambda *a, **k: {
            'result': 'APPLIED_OBSERVED_BLOCK', 'profile': self.source[0], 'build_id': self.source[1]}
        self.lines = iter(['1\n', 'INSTALL /dev/nvme0n1\n'])
        self.read = lambda limit: next(self.lines, '')[:limit]
        self.output = []
        self.targets = ['/dev/nvme0n1']

    def run_op(self, op='fresh'):
        return c.run(op, snapshot=self.snapshot, shared=self.shared,
                     enumerate_targets=lambda: self.targets, read_line=self.read,
                     write=self.output.append, bundle='/bundle', dmi='/dmi')

    def no_write(self):
        self.shared.install_observed_block.assert_not_called()
        self.shared.apply_observed_block.assert_not_called()

    def test_profiles_and_operations(self):
        for profile in ('kano', 'amd-hx370', 'rog-ally'):
            for op in ('fresh', 'update'):
                with self.subTest(profile=profile, op=op):
                    self.setUp()
                    self.source = (profile, self.source[1], self.source[2])
                    self.plan['profile'] = profile
                    self.lines = iter(['1\n', f'{"INSTALL" if op == "fresh" else "UPDATE"} /dev/nvme0n1\n'])
                    self.assertEqual(self.run_op(op)['status'], 'COMPLETED')
                    backend = self.shared.install_observed_block if op == 'fresh' else self.shared.apply_observed_block
                    backend.assert_called_once_with('/bundle', 'catalog.json', 'a' * 64, '/dev/nvme0n1',
                                                    expected_target=self.facts, dmi_root='/dmi')
                    self.assertEqual(self.shared.plan_observed_block.call_count, 2 if op == 'update' else 0)
                    self.assertIn(f'Type {"INSTALL" if op == "fresh" else "UPDATE"} /dev/nvme0n1 to confirm, or press Enter to cancel:', self.output)

    def test_ally_refuses_changed_or_invalid_identity_before_backend(self):
        for failure in ('missing', 'invalid_hash', 'changed_source', 'wrong_plan', 'changed_target', 'sd_card'):
            with self.subTest(failure=failure):
                self.setUp()
                self.source = ('rog-ally', self.source[1], self.source[2])
                self.plan['profile'] = 'rog-ally'
                self.lines = iter(['1\n', 'UPDATE /dev/nvme0n1\n'])
                if failure == 'missing':
                    self.snapshot.side_effect = [None]
                elif failure == 'invalid_hash':
                    self.source = ('rog-ally', self.source[1], 'bad')
                elif failure == 'changed_source':
                    self.snapshot.side_effect = [self.source, ('amd-hx370', self.source[1], self.source[2])]
                elif failure == 'wrong_plan':
                    self.plan['profile'] = 'kano'
                elif failure == 'changed_target':
                    self.shared.observe_block_target.side_effect = [self.facts, dict(self.facts, nbytes=42)]
                elif failure == 'sd_card':
                    self.targets = ['/dev/mmcblk0']
                self.assertEqual(self.run_op('update')['status'], 'REFUSED')
                self.no_write()

    def test_ally_backend_profile_mismatch_is_unknown_after_dispatch(self):
        self.source = ('rog-ally', self.source[1], self.source[2])
        self.shared.install_observed_block.side_effect = lambda *a, **k: {
            'result': 'INSTALLED_OBSERVED_BLOCK', 'profile': 'kano', 'build_id': self.source[1]}
        self.assertEqual(self.run_op(), {'status': 'UNKNOWN', 'hardware': 'NOT_VERIFIED',
                                         'target_may_be_modified': True})
        self.shared.install_observed_block.assert_called_once()

    def test_cancel_invalid_confirmation_and_selection(self):
        for lines in ([], ['1\n'], ['1\n', 'INSTALL /dev/nvme0n1' + 'x' * 200 + '\n'],
                      ['1\n', 'UPDATE /dev/nvme0n1\n'], ['0\n'], ['01\n']):
            with self.subTest(lines=lines):
                self.setUp()
                self.lines = iter(lines)
                self.assertEqual(self.run_op()['status'], 'CANCELLED')
                self.no_write()

    def test_multiple_targets_explicit(self):
        self.targets.append('/dev/nvme1n1')
        self.lines = iter(['2\n', 'INSTALL /dev/nvme1n1\n'])
        self.assertEqual(self.run_op()['status'], 'COMPLETED')
        self.assertEqual(self.shared.install_observed_block.call_args.args[3], '/dev/nvme1n1')

    def test_changed_source_target_or_plan_refuses(self):
        for changed in ('source', 'target', 'plan'):
            with self.subTest(changed=changed):
                self.setUp()
                self.lines = iter(['1\n', 'UPDATE /dev/nvme0n1\n'])
                if changed == 'source':
                    self.snapshot.side_effect = [self.source, ('amd-hx370', self.source[1], self.source[2])]
                if changed == 'target':
                    self.shared.observe_block_target.side_effect = [self.facts, dict(self.facts, nbytes=42)]
                if changed == 'plan':
                    self.shared.plan_observed_block.side_effect = [self.plan, dict(self.plan, target_slot='a')]
                self.assertEqual(self.run_op('update')['status'], 'REFUSED')
                self.no_write()

    def test_plan_failure_and_invalid_inputs(self):
        self.shared.plan_observed_block.side_effect = ValueError('not installed')
        self.assertEqual(self.run_op('update')['status'], 'REFUSED')
        self.no_write()
        for field, value in (('nbytes', True), ('logical_sector_size', True), ('identity_kind', 'loop-backing'),
                             ('rdev', 'bad'), ('device_identity', 'x')):
            with self.subTest(field=field):
                self.setUp()
                self.facts[field] = value
                self.assertEqual(self.run_op()['status'], 'REFUSED')
                self.no_write()
        self.setUp()
        self.targets = ['/dev/sda']
        self.assertEqual(self.run_op()['status'], 'REFUSED')
        self.no_write()

    def test_malformed_source_and_bounded_enumeration(self):
        for build in ('0', '01', '-1', '1' * 21, '１２', 'x'):
            with self.subTest(build=build):
                self.setUp()
                self.source = ('kano', build, 'a' * 64)
                self.assertEqual(self.run_op()['status'], 'REFUSED')
                self.no_write()
        self.setUp()
        seen = []
        def endless():
            n = 0
            while True:
                seen.append(n)
                yield f'/dev/nvme{n}n1'
                n += 1
        result = c.run('fresh', snapshot=self.snapshot, shared=self.shared,
                       enumerate_targets=endless, read_line=self.read, write=self.output.append,
                       bundle='/bundle', dmi='/dmi')
        self.assertEqual(result['status'], 'REFUSED')
        self.assertEqual(len(seen), 129)
        self.no_write()

    def test_mutable_plan_changed_during_confirmation(self):
        self.lines = iter(['1\n', 'UPDATE /dev/nvme0n1\n'])
        self.plan['boot_space'] = {'free': 10}
        self.shared.plan_observed_block.side_effect = lambda *a, **kw: self.plan
        def read_and_change(limit):
            line = next(self.lines, '')[:limit]
            if line.startswith('UPDATE'):
                self.plan['boot_space']['free'] = 0
            return line
        self.read = read_and_change
        self.assertEqual(self.run_op('update')['status'], 'REFUSED')
        self.no_write()

    def test_invalid_result_and_exception_are_unknown_once(self):
        for result in ('bad', 'exception', 'interrupt'):
            with self.subTest(result=result):
                self.setUp()
                if result == 'bad':
                    self.shared.install_observed_block.side_effect = lambda *a, **k: {}
                else:
                    self.shared.install_observed_block.side_effect = RuntimeError() if result == 'exception' else KeyboardInterrupt()
                self.assertEqual(self.run_op()['status'], 'UNKNOWN')
                self.shared.install_observed_block.assert_called_once()


if __name__ == '__main__':
    unittest.main()
