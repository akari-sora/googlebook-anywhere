#!/usr/bin/env python3
"""Read-only diagnostics for the experimental shared maintenance medium."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
BASE = Path('/opt/googlebook')
BUNDLE = Path('/run/gbos-payload')
DIGEST = Path('/etc/gbos-universal/catalog.sha256')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError('missing module')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


ONESHOTS = ('systemd-sysusers.service', 'systemd-tmpfiles-setup.service',
            'systemd-random-seed.service', 'systemd-update-done.service', 'ldconfig.service')


def health():
    def mount(path):
        result = subprocess.run(['/usr/bin/findmnt', '--mountpoint', path, '-n', '-o', 'FSTYPE,OPTIONS'],
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=10).stdout.split()
        if len(result) != 2:
            raise ValueError('missing mount')
        return result[0], result[1].split(',')
    if 'ro' not in mount('/')[1] or 'ro' not in mount(str(BUNDLE))[1]:
        raise ValueError('root or payload writable')
    var_type, var_options = mount('/var')
    if var_type != 'tmpfs' or 'rw' not in var_options:
        raise ValueError('var not writable tmpfs')
    for unit in ONESHOTS:
        result = subprocess.run(['/usr/bin/systemctl', 'show', unit, '--property=LoadState',
                                 '--property=Result', '--property=ExecMainStatus',
                                 '--property=ExecMainStartTimestampMonotonic', '--property=ConditionResult'], check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=10).stdout.splitlines()
        fields = dict(line.split('=', 1) for line in result if '=' in line)
        if (len(fields) != 5 or fields.get('LoadState') != 'loaded' or
                fields.get('Result') != 'success' or fields.get('ExecMainStatus') != '0'):
            raise ValueError('unit unsuccessful: ' + unit)
        started = fields.get('ExecMainStartTimestampMonotonic', '')
        executed = started.isdecimal() and int(started) > 0 and fields.get('ConditionResult') == 'yes'
        skipped = (unit in ('systemd-sysusers.service', 'ldconfig.service') and
                   fields.get('ConditionResult') == 'no' and started == '0')
        if not (executed or skipped):
            raise ValueError('unit neither executed nor condition-skipped: ' + unit)
    subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', 'systemd-logind.service'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    return {'result': 'HEALTHY', 'root': 'ro', 'payload': 'ro', 'var': 'rw-tmpfs',
            'logind': 'active', 'oneshots': 'success'}


def select_metadata(recognition, verified):
    """Select catalog metadata only after unambiguous, complete recognition."""
    if not isinstance(recognition, dict):
        return {'result': 'NONE', 'reason': 'recognition_unreadable', 'write_allowed': False}
    matches = recognition.get('matches')
    if (recognition.get('recognition') != 'matched' or not isinstance(matches, list)
            or len(matches) != 1 or recognition.get('potential_matches') != []
            or recognition.get('unconfigured') != []):
        return {'result': 'NONE', 'reason': 'recognition_not_unique_and_complete', 'write_allowed': False}
    chosen = [item for item in verified.payloads if item['profile'] == matches[0]]
    if len(chosen) != 1 or matches[0] not in verified.descriptors:
        raise ValueError('recognized profile missing from verified catalog')
    item = chosen[0]
    return {'result': 'CATALOG_METADATA_SELECTED', 'profile': item['profile'],
            'build_id': item['build_id'], 'expected_boot_entries': list(item['boot_entries']),
            'metadata_only': True, 'hardware_validation': 'unvalidated', 'write_allowed': False}


def main():
    print('Googlebook shared maintenance: diagnostic prototype', flush=True)
    print('Installation, updates and recovery writes are NOT ENABLED.', flush=True)
    okay = True
    result = None
    try:
        detector = module('gbos_detector', BASE / 'install/stick/detect-model.py')
        rules = detector.load_rules(BASE / 'profiles')
        fields = set().union(*(rule.keys() for _, rule in rules))
        result = detector.detect(rules, detector.read_identity(Path('/sys/class/dmi/id'), fields))
        print('Machine recognition:', json.dumps(result, sort_keys=True), flush=True)
    except (OSError, ValueError, UnicodeError) as exc:
        print('Machine recognition: UNREAD', type(exc).__name__, flush=True)
    try:
        root_options = subprocess.run(['/usr/bin/findmnt', '--mountpoint', '/', '-n', '-o', 'OPTIONS'],
                                      check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True, timeout=10).stdout.strip().split(',')
        if 'ro' not in root_options:
            raise ValueError('maintenance root is not read-only')
        # Never interpret a directory on the root filesystem as the payload.
        mounted = subprocess.run(['/usr/bin/findmnt', '--mountpoint', str(BUNDLE), '-n', '-o', 'OPTIONS'],
                                 check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, timeout=10).stdout.strip().split(',')
        if 'ro' not in mounted:
            raise ValueError('payload not mounted read-only')
        digest = DIGEST.read_text(encoding='ascii').strip()
        catalog = module('gbos_catalog', BASE / 'install/stick/catalog.py')
        with catalog.validate(str(BUNDLE), 'catalog.json', digest) as verified:
            selection = select_metadata(result, verified)
            print('Payload integrity:', json.dumps(verified.report(), sort_keys=True), flush=True)
            print('Catalog selection:', json.dumps(selection, sort_keys=True), flush=True)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        okay = False
        print('Payload integrity: UNREAD OR REFUSED', type(exc).__name__, flush=True)
        print('Catalog selection:', json.dumps({'result': 'NONE', 'reason': 'catalog_unavailable_or_inconsistent', 'write_allowed': False}, sort_keys=True), flush=True)
    try:
        status = health()
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        okay = False
        status = {'result': 'FAILED', 'reason': type(exc).__name__}
    print('System health:', json.dumps(status, sort_keys=True), flush=True)
    print('Installation: DISABLED — installer and target validation are not integrated', flush=True)
    print('Update: DISABLED — no authorized target-bound update workflow', flush=True)
    print('Recovery: DISABLED — no validated recovery workflow', flush=True)
    print('Boot policy, AVB and real hardware support remain UNVERIFIED.', flush=True)
    return 0 if okay else 1


if __name__ == '__main__':
    sys.exit(main())
