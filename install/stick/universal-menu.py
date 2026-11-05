#!/usr/bin/env python3
"""Interactive read-only update preview for the shared maintenance medium."""
import importlib.util
from pathlib import Path
import re
import sys

BASE = Path('/opt/googlebook')
BUNDLE = Path('/run/gbos-payload')
DIGEST = Path('/etc/gbos-universal/catalog.sha256')
DMI = Path('/sys/class/dmi/id')
BLOCK = Path('/sys/class/block')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Unavailable(Exception):
    pass


def snapshot(diag, detector, catalog):
    try:
        if diag.health().get('result') != 'HEALTHY':
            raise ValueError('health unavailable')
    except Exception as exc:
        raise Unavailable('system health could not be checked') from exc
    try:
        rules = detector.load_rules(BASE / 'profiles')
        fields = set().union(*(rule.keys() for _, rule in rules))
        recognition = detector.detect(rules, detector.read_identity(DMI, fields))
        if (recognition.get('recognition') != 'matched' or len(recognition.get('matches', [])) != 1
                or recognition.get('potential_matches') != [] or recognition.get('unconfigured') != []):
            raise ValueError('model unavailable')
    except Exception as exc:
        raise Unavailable('machine model could not be identified') from exc
    try:
        digest = DIGEST.read_text(encoding='ascii').strip()
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('catalog digest unavailable')
        with catalog.validate(str(BUNDLE), 'catalog.json', digest) as verified:
            selection = diag.select_metadata(recognition, verified)
        if selection.get('result') != 'CATALOG_METADATA_SELECTED' or selection.get('write_allowed') is not False:
            raise ValueError('model source unavailable')
        return (selection['profile'], selection['build_id'], digest)
    except Exception as exc:
        raise Unavailable('installation files could not be checked for this model') from exc


def choice():
    line = sys.stdin.readline(65)
    if not line or not line.endswith('\n'):
        return None
    return line.strip()


def main():
    print('Googlebook maintenance — update preview only')
    print('No changes will be made to your disk.')
    print('1. Preview update while keeping data\n0. Cancel')
    if choice() != '1':
        print('Cancelled.')
        return 0
    try:
        diag = load('menu_diagnostic', BASE / 'install/stick/universal-diagnostic.py')
        detector = load('menu_detector', BASE / 'install/stick/detect-model.py')
        catalog = load('menu_catalog', BASE / 'install/stick/catalog.py')
        shared = load('menu_shared', BASE / 'install/stick/shared-source.py')
    except Exception:
        print('Preview unavailable: maintenance tools could not be loaded.')
        return 1
    try:
        before = snapshot(diag, detector, catalog)
    except Unavailable as exc:
        print(f'Preview unavailable: {exc}.')
        return 1
    print('Model:', before[0])
    targets = []
    try:
        names = sorted(p.name for p in BLOCK.iterdir() if re.fullmatch(r'nvme[0-9]+n[0-9]+', p.name))
        for name in names:
            target = '/dev/' + name
            facts = shared.observe_block_target(target)
            if facts.get('identity_kind') == 'nvme-namespace' and type(facts.get('nbytes')) is int and facts['nbytes'] > 0:
                targets.append((target, facts))
    except Exception:
        print('Preview unavailable: an internal disk could not be checked.')
        return 1
    if not targets:
        print('Preview unavailable: no accessible internal NVMe disk was found.')
        return 1
    print('Choose an internal disk:')
    for index, (target, facts) in enumerate(targets, 1):
        print(f"{index}. {target} — {facts['nbytes'] / 1000000000:.1f} GB")
    print('0. Cancel')
    selected = choice()
    if selected is None or not selected.isascii() or not selected.isdecimal() or not 1 <= int(selected) <= len(targets):
        print('Cancelled.')
        return 0
    target, facts = targets[int(selected) - 1]
    try:
        if snapshot(diag, detector, catalog) != before:
            raise ValueError('system or installation files changed')
        if shared.observe_block_target(target) != facts:
            raise ValueError('target changed')
        try:
            plan = shared.plan_observed_block(str(BUNDLE), 'catalog.json', before[2], target, dmi_root=DMI)
        finally:
            after = snapshot(diag, detector, catalog)
            after_target = shared.observe_block_target(target)
        if (after != before or after_target != facts
                or plan.get('result') != 'PLAN_OBSERVED' or plan.get('write_allowed') is not False
                or plan.get('profile') != before[0] or plan.get('build_id') != before[1]
                or plan.get('current_slot') not in ('a', 'b')
                or plan.get('target_slot') not in ('a', 'b')
                or plan['current_slot'] == plan['target_slot']):
            raise ValueError('preview changed')
    except Exception:
        print('Preview unavailable: the disk or installation files could not be checked, or changed during the check.')
        return 1
    print('Current system slot:', plan['current_slot'])
    print('Next system slot:', plan['target_slot'])
    print('Preview complete. No update was installed; installation and recovery are not available yet.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
