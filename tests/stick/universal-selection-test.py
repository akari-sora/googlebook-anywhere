#!/usr/bin/env python3
"""Selection is metadata-only; tests use in-memory synthetic catalog entries."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('diagnostic', ROOT / 'install/stick/universal-diagnostic.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def recognition(profile='kano', **kwargs):
    value = {'recognition': 'matched', 'matches': [profile], 'potential_matches': [], 'unconfigured': []}
    value.update(kwargs)
    return value


class Catalog:
    def __init__(self):
        self.payloads = [{'profile': p, 'build_id': p + '-build', 'boot_entries': [p + '.conf']}
                         for p in ('kano', 'amd-hx370')]
        self.descriptors = {p['profile']: 1 for p in self.payloads}
    def report(self):
        return {'result': 'CATALOG_PAYLOADS_VERIFIED'}
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Selection(unittest.TestCase):
    def test_both_profiles_select_expected_metadata(self):
        for profile in ('kano', 'amd-hx370'):
            with self.subTest(profile=profile):
                selected = d.select_metadata(recognition(profile), Catalog())
                self.assertEqual(selected['profile'], profile)
                self.assertEqual(selected['build_id'], profile + '-build')
                self.assertEqual(selected['expected_boot_entries'], [profile + '.conf'])
                self.assertTrue(selected['metadata_only'])
                self.assertFalse(selected['write_allowed'])

    def test_no_selection_for_nonunique_unknown_or_unreadable(self):
        cases = [None, recognition(recognition='unknown', matches=[]),
                 recognition(recognition='incomplete', potential_matches=['amd-hx370']),
                 recognition(recognition='ambiguous', matches=['kano', 'amd-hx370']),
                 recognition(unconfigured=['amd-hx370']), recognition(matches=[]),
                 recognition(recognition='unreadable')]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(d.select_metadata(value, Catalog())['result'], 'NONE')

    def test_missing_selected_catalog_item_or_descriptor_refuses(self):
        catalog = Catalog()
        catalog.payloads.pop(0)
        with self.assertRaises(ValueError):
            d.select_metadata(recognition(), catalog)
        catalog = Catalog()
        del catalog.descriptors['kano']
        with self.assertRaises(ValueError):
            d.select_metadata(recognition(), catalog)

    def run_diagnostic(self, detected, catalog=None, error=None):
        detector = mock.Mock()
        detector.load_rules.return_value = [('kano', {'a': 'b'})]
        detector.detect.return_value = detected
        cat = mock.Mock()
        cat.validate.side_effect = error
        cat.validate.return_value = catalog or Catalog()
        output = io.StringIO()
        with (mock.patch.object(d, 'module', side_effect=lambda name, path: detector if name == 'gbos_detector' else cat),
              mock.patch.object(d.DIGEST.__class__, 'read_text', return_value='a' * 64),
              mock.patch.object(d, 'health', return_value={'result': 'HEALTHY'}),
              mock.patch.object(d.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='ro')),
              contextlib.redirect_stdout(output)):
            result = d.main()
        self.assertEqual(cat.validate.call_count, 1)
        return result, output.getvalue()

    def test_unknown_qemu_is_not_failure_and_operations_separately_disabled(self):
        result, output = self.run_diagnostic(recognition(recognition='unknown', matches=[]))
        self.assertEqual(result, 0)
        self.assertIn('"result": "NONE"', output)
        for operation in ('Installation', 'Update', 'Recovery'):
            self.assertIn(operation + ': DISABLED', output)

    def test_catalog_error_and_missing_selected_refuse(self):
        result, output = self.run_diagnostic(recognition(), error=ValueError('corrupt'))
        self.assertEqual(result, 1)
        self.assertIn('catalog_unavailable_or_inconsistent', output)
        catalog = Catalog()
        catalog.payloads.pop(0)
        result, output = self.run_diagnostic(recognition(), catalog=catalog)
        self.assertEqual(result, 1)
        self.assertIn('catalog_unavailable_or_inconsistent', output)


if __name__ == '__main__':
    unittest.main()
