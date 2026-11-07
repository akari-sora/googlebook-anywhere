#!/usr/bin/env python3
"""Tests for the diagnostic-only model detector (no hardware or disk writes)."""
import sys
# Disable bytecode before imports to avoid creating .pyc files.
sys.dont_write_bytecode = True

import contextlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install/stick/detect-model.py"
spec = importlib.util.spec_from_file_location("detect_model", SCRIPT)
detector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detector)


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.rules = detector.load_rules(ROOT / "profiles")

    def check_result(self, rules, identity, recognition, matches=(), potential=(), unconfigured=()):
        result = detector.detect(rules, identity)
        self.assertEqual(result, {"recognition": recognition, "matches": list(matches),
                                  "potential_matches": list(potential), "unconfigured": list(unconfigured),
                                  "hardware_validation": "unvalidated", "write_allowed": False})

    def test_real_kano_family_only(self):
        self.check_result(self.rules, {"sys_vendor": "Google", "product_name": "Kano"}, "matched",
                          matches=("kano",))

    def test_hx370_windows_smbios_family_fields_only(self):
        # These values were observed via Windows SMBIOS. A matching Linux
        # sysfs identity and a usable common maintenance boot remain unverified.
        hx = {"sys_vendor": "ONE-NETBOOK", "product_name": "ONEXPLAYER X1Pro",
              "board_vendor": "ONE-NETBOOK", "board_name": "ONEXPLAYER X1Pro"}
        self.check_result(self.rules, hx, "matched", matches=("amd-hx370",))
        for field in hx:
            with self.subTest(missing=field):
                identity = dict(hx)
                del identity[field]
                self.check_result(self.rules, identity, "incomplete", potential=("amd-hx370",))
            with self.subTest(mismatch=field):
                identity = dict(hx, **{field: "Other"})
                self.check_result(self.rules, identity, "unknown")
        self.check_result(self.rules, {"cpu_name": "AMD Ryzen AI 9 HX 370"}, "incomplete",
                          potential=("amd-hx370", "kano"))
        self.check_result(self.rules, {"sys_vendor": "Synthetic HX", "product_name": "Synthetic"}, "unknown")

    def test_optional_ally_rule_matches_and_remains_fail_closed(self):
        # Synthetic fixture, not a real device identity.
        with tempfile.TemporaryDirectory() as temp:
            profiles = Path(temp)
            for name in detector.PROFILE_IDS:
                (profiles / name).mkdir()
                (profiles / name / 'profile.toml').write_bytes((ROOT / 'profiles' / name / 'profile.toml').read_bytes())
            self.assertEqual({p for p, _ in detector.load_rules(profiles)}, set(detector.PROFILE_IDS))
            ally = profiles / 'rog-ally'
            ally.mkdir()
            rule_file = ally / 'profile.toml'
            rule_file.write_text('[model]\nid = "rog-ally"\ndmi = {sys_vendor = "Fixture", product_name = "Fixture Ally"}\n')
            rules = detector.load_rules(profiles)
            self.check_result(rules, {'sys_vendor': 'Fixture', 'product_name': 'Fixture Ally'},
                              'matched', matches=('rog-ally',))
            self.check_result(rules, {'sys_vendor': 'Fixture'}, 'incomplete', potential=('rog-ally',))
            self.check_result(rules, {'sys_vendor': 'Fixture', 'product_name': 'Other'}, 'unknown')
            rule_file.write_text('[model]\nid = "rog-ally"\ndmi = {}\n')
            rules = detector.load_rules(profiles)
            self.check_result(rules, {'sys_vendor': 'Google', 'product_name': 'Kano'},
                              'matched', matches=('kano',), unconfigured=('rog-ally',))
            rule_file.unlink()
            with self.assertRaises(OSError):
                detector.load_rules(profiles)

    def test_empty_map_is_explicitly_unconfigured(self):
        rules = [("amd-hx370", {}), ("kano", {"sys_vendor": "Google", "product_name": "Kano"})]
        self.check_result(rules, {"sys_vendor": "Google", "product_name": "Kano"}, "matched",
                          matches=("kano",), unconfigured=("amd-hx370",))
        self.check_result([("amd-hx370", {})], {}, "unknown", unconfigured=("amd-hx370",))

    def test_hx370_dmi_cli_diagnostics_and_missing_board_field(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = {"sys_vendor": "ONE-NETBOOK", "product_name": "ONEXPLAYER X1Pro",
                        "board_vendor": "ONE-NETBOOK", "board_name": "ONEXPLAYER X1Pro"}
            for name, value in identity.items():
                (root / name).write_text(value + "\n")
            args = [sys.executable, str(SCRIPT), "--dmi-root", str(root)]
            result = subprocess.run(args, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            self.assertEqual(data["recognition"], "matched")
            self.assertEqual(data["matches"], ["amd-hx370"])
            self.assertEqual(data["hardware_validation"], "unvalidated")
            self.assertFalse(data["write_allowed"])
            (root / "board_name").unlink()
            result = subprocess.run(args, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            self.assertEqual(data["recognition"], "incomplete")
            self.assertEqual(data["potential_matches"], ["amd-hx370"])
            self.assertFalse(data["write_allowed"])

    def test_case_sensitive_and_no_partial_match(self):
        self.check_result(self.rules, {"sys_vendor": "google", "product_name": "Kano"}, "unknown")
        self.check_result(self.rules, {"sys_vendor": "Google"}, "incomplete", potential=("kano",))

    def test_empty_invalid_or_missing_field_is_incomplete(self):
        for value in (None, "", " \n", 3):
            with self.subTest(value=value):
                self.check_result(self.rules, {"sys_vendor": "Google", "product_name": value}, "incomplete",
                                  potential=("kano",))

    def test_unknown_if_an_available_field_proves_mismatch(self):
        self.check_result(self.rules, {"sys_vendor": "Other"}, "unknown")

    def test_complete_collision_and_duplicate_profile_id(self):
        rule = {"sys_vendor": "Synthetic", "product_name": "Model"}
        self.check_result([("one", rule), ("two", rule)], rule, "ambiguous", matches=("one", "two"))
        with self.assertRaises(detector.InvalidCatalog):
            detector.detect([("one", rule), ("one", rule)], rule)

    def test_complete_match_plus_incomplete_competitor(self):
        rules = [("one", {"sys_vendor": "Synthetic"}),
                 ("two", {"sys_vendor": "Synthetic", "product_name": "Model"})]
        self.check_result(rules, {"sys_vendor": "Synthetic"}, "incomplete", matches=("one",), potential=("two",))

    def test_synthetic_hx_rule_only(self):
        # Test fixture is not real HX370 machine identity and must not enter a shipped profile.
        rules = [("amd-hx370", {"sys_vendor": "Synthetic HX", "product_name": "Synthetic device"}),
                 ("kano", {"sys_vendor": "Google", "product_name": "Kano"})]
        self.check_result(rules, {"sys_vendor": "Synthetic HX", "product_name": "Synthetic device"}, "matched",
                          matches=("amd-hx370",))

    def test_invalid_rule_fields_and_values_refused(self):
        for rule in ({"product_serial": "secret"}, {"../sys_vendor": "Google"}, {"sys_vendor": ""},
                     {"sys_vendor": " Google"}, {"sys_vendor": 3}, {"sys_vendor": "bad\nline"}, []):
            with self.subTest(rule=rule), self.assertRaises(detector.InvalidCatalog):
                detector.detect([("kano", rule)], {})

    def test_read_only_cli_missing_empty_and_unreadable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "sys_vendor").write_text("Google\n")
            for mode in ("missing", "empty", "unreadable"):
                if mode == "empty":
                    (root / "product_name").write_text("")
                elif mode == "unreadable":
                    (root / "product_name").unlink()
                    (root / "product_name").mkdir()  # reliably unreadable as a regular file, also as root
                before = {p.name: p.stat().st_mtime_ns for p in root.iterdir()}
                result = subprocess.run([sys.executable, str(SCRIPT), "--dmi-root", str(root)],
                                        capture_output=True, text=True, check=True)
                after = {p.name: p.stat().st_mtime_ns for p in root.iterdir()}
                self.assertEqual(before, after)
                self.assertEqual(json.loads(result.stdout)["recognition"], "incomplete")
                self.assertFalse(json.loads(result.stdout)["write_allowed"])

    def test_noncanonical_and_oversized_dmi_are_not_matched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "product_name").write_text("Kano\n")
            for text in (" Google\n", "Google \n", "Google" + "x" * 300):
                (root / "sys_vendor").write_text(text)
                identity = detector.read_identity(root, {"sys_vendor", "product_name"})
                self.assertNotIn("sys_vendor", identity)
                self.assertEqual(detector.detect(self.rules, identity)["recognition"], "incomplete")

    def test_bad_catalog_fails_with_write_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            old_root = detector.ROOT
            detector.ROOT = Path(temp)
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output):
                    code = detector.main(["--dmi-root", temp])
            finally:
                detector.ROOT = old_root
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue()), {"error": "invalid_input_or_catalog",
                             "hardware_validation": "unvalidated", "write_allowed": False})

    def test_cli_real_rules_and_error_are_deterministic_and_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "sys_vendor").write_text("Google\n")
            (root / "product_name").write_text("Kano\n")
            args = [sys.executable, str(SCRIPT), "--dmi-root", str(root)]
            first = subprocess.run(args, capture_output=True, text=True, check=True)
            second = subprocess.run(args, capture_output=True, text=True, check=True)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(json.loads(first.stdout)["matches"], ["kano"])
            error = subprocess.run([sys.executable, str(SCRIPT), "--bad-option"], capture_output=True, text=True)
            self.assertEqual(error.returncode, 2)
            self.assertFalse(json.loads(error.stdout)["write_allowed"])
            self.assertEqual(error.stderr, "")

    def test_malformed_profile_catalog_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for profile_id in detector.PROFILE_IDS:
                (root / profile_id).mkdir()
                (root / profile_id / "profile.toml").write_text(
                    f'[model]\nid = "{profile_id}"\ndmi = {{ product_serial = "secret" }}\n')
            with self.assertRaises(detector.InvalidCatalog):
                detector.load_rules(root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
