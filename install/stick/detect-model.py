#!/usr/bin/env python3
"""Read-only model-family diagnostics; this does not authorize installation."""
import sys
# Disable bytecode before imports to avoid creating .pyc files.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import tomllib  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PROFILE_IDS = ("amd-hx370", "kano")
OPTIONAL_PROFILE_IDS = ("rog-ally",)
# Only non-unique DMI fields. Do not read product/board serials, UUIDs, or asset tags.
DMI_FIELDS = frozenset({"sys_vendor", "product_name", "product_version",
                        "board_vendor", "board_name", "board_version"})


class InvalidCatalog(ValueError):
    pass


def valid_value(value):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= 256 and all(c.isprintable() for c in value))


def validate_rules(rules):
    """Validate all rules before reading machine identity; an empty map is unconfigured."""
    seen = set()
    for profile_id, rule in rules:
        if (not isinstance(profile_id, str) or not profile_id or profile_id in seen
                or not isinstance(rule, dict)):
            raise InvalidCatalog("invalid profile")
        seen.add(profile_id)
        if any(key not in DMI_FIELDS or not valid_value(value) for key, value in rule.items()):
            raise InvalidCatalog("invalid rule")


def load_rules(profiles_dir):
    """Read only the model identity rules from the supported profile files."""
    rules = []
    profile_ids = PROFILE_IDS + tuple(
        profile_id for profile_id in OPTIONAL_PROFILE_IDS
        if os.path.lexists(profiles_dir / profile_id)
    )
    for profile_id in profile_ids:
        with (profiles_dir / profile_id / "profile.toml").open("rb") as f:
            profile = tomllib.load(f)
        model = profile.get("model")
        if not isinstance(model, dict) or model.get("id") != profile_id or "dmi" not in model:
            raise InvalidCatalog("invalid profile identity")
        rules.append((profile_id, model["dmi"]))
    validate_rules(rules)
    return rules


def read_identity(dmi_root, fields):
    identity = {}
    for field in sorted(fields):
        try:
            # A fixed upper bound prevents malformed test or sysfs content becoming unbounded.
            with (dmi_root / field).open("r", encoding="utf-8") as f:
                value = f.read(258)
            # sysfs adds one newline; preserve all other characters for exact matching.
            if value.endswith("\n"):
                value = value[:-1]
            if valid_value(value):
                identity[field] = value
        except (OSError, UnicodeError):
            pass  # Missing and unreadable fields are both unknown, never an empty match.
    return identity


def detect(rules, identity):
    """Classify complete matches and rules that could match if missing data were available."""
    validate_rules(rules)
    identity = {key: value for key, value in identity.items()
                if key in DMI_FIELDS and valid_value(value)}
    matches, potential, unconfigured = [], [], []
    for profile_id, rule in rules:
        if not rule:
            unconfigured.append(profile_id)
        elif any(field in identity and identity[field] != expected for field, expected in rule.items()):
            continue
        elif all(field in identity and valid_value(identity[field]) for field in rule):
            matches.append(profile_id)
        else:
            potential.append(profile_id)
    if len(matches) > 1:
        recognition = "ambiguous"
    elif potential:
        recognition = "incomplete"
    elif matches:
        recognition = "matched"
    else:
        recognition = "unknown"
    return {"recognition": recognition, "matches": sorted(matches), "potential_matches": sorted(potential),
            "unconfigured": sorted(unconfigured), "hardware_validation": "unvalidated", "write_allowed": False}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid arguments")


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--dmi-root", type=Path, default=Path("/sys/class/dmi/id"),
                        help="directory containing non-secret DMI fields (default: /sys/class/dmi/id)")
    try:
        args = parser.parse_args(argv)
        rules = load_rules(ROOT / "profiles")
        fields = set().union(*(rule.keys() for _, rule in rules))
        result = detect(rules, read_identity(args.dmi_root, fields))
        code = 0
    except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
        result = {"error": "invalid_input_or_catalog", "hardware_validation": "unvalidated", "write_allowed": False}
        code = 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
