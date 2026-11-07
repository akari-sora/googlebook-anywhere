#!/usr/bin/env python3
"""Internal common-media controller; not connected to the diagnostic entry point.

The caller owns runtime trust, backup policy and admission. Snapshot is the
existing menu's verified (profile, build_id, catalog_sha256) observation.
This module does not itself grant permission or establish hardware acceptance.
"""
import re
from copy import deepcopy
from itertools import islice

TARGET = re.compile(r'/dev/nvme[0-9]+n[0-9]+\Z', re.ASCII)
HASH = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
TOKEN = re.compile(r'[1-9][0-9]{0,19}\Z', re.ASCII)
FACTS = {'rdev', 'syspath', 'nbytes', 'logical_sector_size', 'identity_kind', 'device_identity'}


def _source(value):
    if (type(value) is not tuple or len(value) != 3 or value[0] not in ('kano', 'amd-hx370', 'rog-ally')
            or not isinstance(value[1], str) or not TOKEN.fullmatch(value[1])
            or not isinstance(value[2], str) or not HASH.fullmatch(value[2])):
        raise ValueError('invalid source observation')
    return value


def _facts(value):
    if (type(value) is not dict or set(value) != FACTS
            or not isinstance(value['rdev'], str) or not re.fullmatch(r'[0-9]+:[0-9]+', value['rdev'])
            or not isinstance(value['syspath'], str) or not value['syspath'].startswith('/sys/devices/')
            or type(value['nbytes']) is not int or value['nbytes'] <= 0
            or type(value['logical_sector_size']) is not int or value['logical_sector_size'] != 512
            or value['identity_kind'] != 'nvme-namespace'
            or not isinstance(value['device_identity'], str) or not HASH.fullmatch(value['device_identity'])):
        raise ValueError('invalid target observation')
    return dict(value)


def _plan(shared, bundle, digest, target, dmi, source):
    plan = shared.plan_observed_block(bundle, 'catalog.json', digest, target, dmi_root=dmi)
    if (type(plan) is not dict or plan.get('result') != 'PLAN_OBSERVED'
            or plan.get('write_allowed') is not False or plan.get('profile') != source[0]
            or plan.get('build_id') != source[1] or plan.get('current_slot') not in ('a', 'b')
            or plan.get('target_slot') not in ('a', 'b') or plan['current_slot'] == plan['target_slot']):
        raise ValueError('invalid update plan')
    return deepcopy(plan)


def run(operation, *, snapshot, shared, enumerate_targets, read_line, write, bundle, dmi):
    """Confirm one operation, recheck observations and call its fixed backend once.

    read_line(limit) must return at most limit characters, including a final LF.
    Results do not imply real hardware acceptance. No retries are performed.
    """
    if operation not in ('fresh', 'update'):
        return {'status': 'REFUSED', 'hardware': 'NOT_VERIFIED'}
    try:
        source = _source(snapshot())
        targets = list(islice(enumerate_targets(), 129))
        if (not targets or len(targets) > 128 or len(set(targets)) != len(targets)
                or any(not isinstance(t, str) or not TARGET.fullmatch(t) for t in targets)):
            raise ValueError('invalid targets')
        for i, target in enumerate(targets, 1):
            write(f'{i}. {target}')
        selected = read_line(16)
        if (not isinstance(selected, str) or not re.fullmatch(r'[1-9][0-9]*\n', selected, re.ASCII)
                or int(selected) > len(targets)):
            return {'status': 'CANCELLED', 'hardware': 'NOT_VERIFIED'}
        target = targets[int(selected) - 1]
        facts = _facts(shared.observe_block_target(target))
        plan = _plan(shared, bundle, source[2], target, dmi, source) if operation == 'update' else None
        action = 'INSTALL' if operation == 'fresh' else 'UPDATE'
        purpose = 'ALL DATA WILL BE ERASED' if operation == 'fresh' else 'PRESERVE USERDATA AND CURRENT SLOT INTENDED'
        write(f'{source[0]} {target} {facts["nbytes"]} bytes: {purpose}')
        write(f'Type {action} {target} to confirm, or press Enter to cancel:')
        confirmation = read_line(128)
        if confirmation != f'{action} {target}\n':
            return {'status': 'CANCELLED', 'hardware': 'NOT_VERIFIED'}
        if _source(snapshot()) != source or _facts(shared.observe_block_target(target)) != facts:
            raise ValueError('source or target changed')
        if operation == 'update' and _plan(shared, bundle, source[2], target, dmi, source) != plan:
            raise ValueError('update plan changed')
        if _source(snapshot()) != source or _facts(shared.observe_block_target(target)) != facts:
            raise ValueError('source or target changed')
    except Exception:
        return {'status': 'REFUSED', 'hardware': 'NOT_VERIFIED'}
    try:
        backend = shared.install_observed_block if operation == 'fresh' else shared.apply_observed_block
        result = backend(bundle, 'catalog.json', source[2], target, expected_target=facts, dmi_root=dmi)
        expected = 'INSTALLED_OBSERVED_BLOCK' if operation == 'fresh' else 'APPLIED_OBSERVED_BLOCK'
        if (type(result) is not dict or result.get('result') != expected
                or result.get('profile') != source[0] or result.get('build_id') != source[1]):
            raise ValueError('invalid backend result')
        return {'status': 'COMPLETED', 'hardware': 'NOT_VERIFIED'}
    except BaseException:
        return {'status': 'UNKNOWN', 'hardware': 'NOT_VERIFIED', 'target_may_be_modified': True}
