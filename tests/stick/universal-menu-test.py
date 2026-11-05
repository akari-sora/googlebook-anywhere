#!/usr/bin/env python3
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('menu', ROOT / 'install/stick/universal-menu.py')
menu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(menu)
spec = importlib.util.spec_from_file_location('diagnostic', ROOT / 'install/stick/universal-diagnostic.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class MenuTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.digest = self.root / 'digest'
        self.digest.write_text('a' * 64)
        self.block = self.root / 'block'
        self.block.mkdir()
        (self.block / 'nvme0n1').touch()
        self.match = {'recognition': 'matched', 'matches': ['kano'], 'potential_matches': [], 'unconfigured': []}
        self.detector = types.SimpleNamespace(load_rules=lambda p: [('kano', {'name': 'x'})],
            read_identity=lambda p, f: {}, detect=lambda r, i: self.match)
        self.verified = types.SimpleNamespace(payloads=[{'profile': 'kano', 'build_id': '123', 'boot_entries': []},
            {'profile': 'amd-hx370', 'build_id': '456', 'boot_entries': []}], descriptors={'kano': 1, 'amd-hx370': 2})
        self.catalog = types.SimpleNamespace(validate=lambda *a: contextlib.nullcontext(self.verified))
        self.diag = types.SimpleNamespace(health=lambda: {'result': 'HEALTHY'}, select_metadata=diag.select_metadata)
        self.facts = {'identity_kind': 'nvme-namespace', 'nbytes': 128000000000, 'device_identity': 'private'}
        self.shared = types.SimpleNamespace(observe_block_target=lambda target: dict(self.facts),
            plan_observed_block=lambda *a, **k: {'result': 'PLAN_OBSERVED', 'profile': self.match['matches'][0],
                'build_id': '123' if self.match['matches'][0] == 'kano' else '456',
                'current_slot': 'a', 'target_slot': 'b', 'write_allowed': False},
            apply=lambda *a: self.fail('write called'), install=lambda *a: self.fail('write called'),
            restore=lambda *a: self.fail('write called'))

    def run_menu(self, inputs='1\n1\n'):
        modules = {'menu_diagnostic': self.diag, 'menu_detector': self.detector,
                   'menu_catalog': self.catalog, 'menu_shared': self.shared}
        output = io.StringIO()
        with patch.object(menu, 'DIGEST', self.digest), patch.object(menu, 'BLOCK', self.block), \
             patch.object(menu, 'load', side_effect=lambda n, p: modules[n]), \
             patch('sys.stdin', io.StringIO(inputs)), contextlib.redirect_stdout(output):
            result = menu.main()
        return result, output.getvalue()

    def test_known_models(self):
        for model in ('kano', 'amd-hx370'):
            self.match['matches'] = [model]
            status, out = self.run_menu()
            self.assertEqual(status, 0)
            self.assertIn('Model: ' + model, out)
            self.assertIn('Current system slot: a', out)
            self.assertIn('Next system slot: b', out)
            self.assertNotIn('private', out)

    def test_unknown_and_ambiguous_models(self):
        for match in ([], ['kano', 'amd-hx370']):
            self.match['matches'] = match
            self.assertEqual(self.run_menu()[0], 1)
        self.match['matches'] = ['kano']
        self.match['potential_matches'] = ['amd-hx370']
        self.assertEqual(self.run_menu()[0], 1)

    def test_no_disk_and_multiple_explicit_selection(self):
        (self.block / 'nvme0n1').unlink()
        self.assertIn('no accessible', self.run_menu()[1])
        (self.block / 'nvme0n1').touch()
        (self.block / 'nvme1n1').touch()
        calls = []
        original = self.shared.plan_observed_block
        self.shared.plan_observed_block = lambda *a, **k: (calls.append(a[3]), original(*a, **k))[1]
        self.assertEqual(self.run_menu('1\n2\n')[0], 0)
        self.assertEqual(calls, ['/dev/nvme1n1'])

    def test_changed_target_or_source_after_plan(self):
        original = self.shared.plan_observed_block
        def changed(*a, **k):
            result = original(*a, **k)
            self.facts['device_identity'] = 'changed'
            return result
        self.shared.plan_observed_block = changed
        self.assertEqual(self.run_menu()[0], 1)
        self.shared.plan_observed_block = lambda *a, **k: (self.digest.write_text('b' * 64), original(*a, **k))[1]
        self.assertEqual(self.run_menu()[0], 1)

    def test_checks_again_after_plan_failure_and_never_calls_writers(self):
        health_calls = []
        self.diag.health = lambda: (health_calls.append(1), {'result': 'HEALTHY'})[1]
        self.shared.plan_observed_block = lambda *a, **k: (_ for _ in ()).throw(ValueError('unavailable'))
        self.assertEqual(self.run_menu()[0], 1)
        self.assertEqual(len(health_calls), 3)

    def test_model_and_health_change_after_plan(self):
        original = self.shared.plan_observed_block
        def changed_model(*a, **k):
            result = original(*a, **k)
            self.match['matches'] = ['amd-hx370']
            return result
        self.shared.plan_observed_block = changed_model
        self.assertEqual(self.run_menu()[0], 1)
        self.match['matches'] = ['kano']
        def changed_health(*a, **k):
            result = original(*a, **k)
            self.diag.health = lambda: {'result': 'FAILED'}
            return result
        self.shared.plan_observed_block = changed_health
        self.assertEqual(self.run_menu()[0], 1)

    def test_cancel_eof_and_long_input(self):
        for inputs in ('0\n', '', '1\n0\n', '1\n', '1\n' + '9' * 100 + '\n'):
            self.assertIn('Cancelled', self.run_menu(inputs)[1])

    def test_unhealthy_and_bad_source(self):
        self.diag.health = lambda: {'result': 'FAILED'}
        self.assertEqual(self.run_menu()[0], 1)
        self.diag.health = lambda: {'result': 'HEALTHY'}
        self.catalog.validate = lambda *a: (_ for _ in ()).throw(ValueError('bad source'))
        self.assertEqual(self.run_menu()[0], 1)


if __name__ == '__main__':
    unittest.main()
