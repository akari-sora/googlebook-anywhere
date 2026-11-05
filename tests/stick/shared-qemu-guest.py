#!/usr/bin/env python3
"""Test-only guest controller and miniature fixture builder for shared-media QEMU.

This is not an installer or a physical-device authorization mechanism. The host
must construct the test VM using only disposable, file-backed virtual disks.
The guest accepts no target path and is not included in the delivered medium.
"""
import ast
import hashlib
import importlib.util
import json
import linecache
import os
from pathlib import Path
import random
import re
import select
import shutil
import stat
import struct
import subprocess
import sys
import termios
import time
import tty
import uuid
import zlib

sys.dont_write_bytecode = True
MIB = 1 << 20
BUNDLE = Path('/run/gbos-qemu-test')
TARGET = '/dev/nvme0n1'
NVME_SERIAL = 'GBOSQEMUTEST01'
PCI_DEVICES = Path('/sys/bus/pci/devices')
PROFILES = ('kano', 'amd-hx370')
SMBIOS = {
    'kano': {'sys_vendor': 'Google', 'product_name': 'Kano',
             'board_vendor': 'Google', 'board_name': 'Kano'},
    'amd-hx370': {'sys_vendor': 'ONE-NETBOOK', 'product_name': 'ONEXPLAYER X1Pro',
                  'board_vendor': 'ONE-NETBOOK', 'board_name': 'ONEXPLAYER X1Pro'},
}
HASH = re.compile(r'[0-9a-f]{64}\Z')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError('missing test module')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def prepare(repo_root, outdir):
    """Build v1/v2 synthetic catalogs and copy only guest runtime dependencies.

    Run only on an unprivileged Linux host with mtools in a new, private work
    directory. Return values belong on the independent host control channel.
    """
    root, out = Path(repo_root).resolve(strict=True), Path(outdir)
    if not out.is_absolute() or out.is_symlink() or not out.is_dir() or any(out.iterdir()):
        raise ValueError('fixture output must be an empty directory')
    st = out.stat()
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError('fixture output must be private and owned')
    if not sys.platform.startswith('linux') or os.geteuid() == 0:
        raise ValueError('fixture preparation requires an unprivileged Linux host')
    if not all(shutil.which(t) for t in ('mformat', 'mcopy', 'mmd')):
        raise ValueError('fixture preparation requires mtools')
    files = ('install/gbos-update.py', 'install/gbos-fresh.py',
             'install/stick/shared-source.py', 'install/stick/catalog.py',
             'install/stick/detect-model.py', 'profiles/kano/profile.toml',
             'profiles/amd-hx370/profile.toml', 'tests/stick/shared-qemu-guest.py')
    for relative in files:
        dst = out / 'repo' / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, dst)
    fixture = out / 'fixtures'
    fixture.mkdir(mode=0o700)
    tree = ast.parse((root / 'tests/update/selftest.py').read_text())
    names = {'rnd', 'sh', 'gpt_bytes', 'lp_super', 'newc', 'bootconfig', 'avb', 'make_release'}
    constants = {'LAYOUT', 'ESP_T', 'LIN_T', 'VBM_T', 'DYN', 'LIFT', 'OPTS'}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names or
             isinstance(node, ast.Assign) and len(node.targets) == 1 and
             isinstance(node.targets[0], ast.Name) and node.targets[0].id in constants]
    ns = {'Path': Path, 'W': fixture, 'MIB': MIB, 'os': os, 'random': random,
          'struct': struct, 'uuid': uuid, 'zlib': zlib, 'hashlib': hashlib,
          'subprocess': subprocess, 'ENV': dict(os.environ, MTOOLS_SKIP_CHECK='1')}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<fixture-definitions>', 'exec'), ns)
    gpt = load('guest_fixture_gpt', root / 'tests/stick/gptlib.py')
    result = {'smbios': SMBIOS}
    largest = 0
    for prefix, version, catalog_name, hash_key, payload_key in (
            ('fresh-', 'v1', 'fresh-catalog.json', 'fresh_catalog_sha256', 'fresh_payloads'),
            ('', 'v2', 'catalog.json', 'catalog_sha256', 'payloads')):
        items, payloads = [], {}
        for profile, model in (('kano', 'KANO'), ('amd-hx370', 'AMD')):
            source = fixture / (prefix + profile + '.img')
            ns['make_release'](source, model=model, plain=False, vendor=version)
            _, parts = gpt.read_gpt(source)
            data = next(part for part in parts.values() if part['name'] == 'userdata')
            with source.open('r+b') as image:
                image.seek(data['first'] * 512)
                image.write(b'SOURCE-USERDATA-MUST-NOT-BE-COPIED')
                image.seek(MIB - 128)
                image.write(b'SOURCE-GAP-MUST-NOT-BE-COPIED')
                image.flush()
                os.fsync(image.fileno())
            info = {'path': source.name, 'size': source.stat().st_size, 'sha256': digest(source)}
            largest = max(largest, info['size'])
            payloads[profile] = info
            items.append({'profile': profile, 'build_id': '16452207', 'image': info,
                          'boot_entries': ['lapis-debug.conf', 'lapis-quiet.conf'],
                          'hardware_validation': 'unvalidated'})
        raw = json.dumps({'schema': 1, 'payloads': items}, sort_keys=True,
                         separators=(',', ':')).encode()
        (fixture / catalog_name).write_bytes(raw)
        result[hash_key] = hashlib.sha256(raw).hexdigest()
        result[payload_key] = payloads
    for name in ('esp-build.img', 'f'):
        (fixture / name).unlink(missing_ok=True)
    for path in fixture.glob('*.sha256'):
        path.unlink()
    result['target_size'] = largest + 24 * MIB
    result['scope'] = 'SYNTHETIC_QEMU_FIXTURE_ONLY'
    return result


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate control field')
        result[key] = value
    return result


def control():
    """Read one bounded control record over the VM's independent serial port."""
    fd = os.open('/dev/ttyS1', os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        print('GBOS_QEMU_READY', flush=True)
        data, deadline = bytearray(), time.monotonic() + 120
        while b'\n' not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise ValueError('control channel timed out')
            chunk = os.read(fd, min(4097 - len(data), 1024))
            if not chunk:
                raise ValueError('control channel closed')
            data.extend(chunk)
            if len(data) > 4096:
                raise ValueError('control record too large')
        if data.count(b'\n') != 1 or not data.endswith(b'\n'):
            raise ValueError('unexpected control data')
        return json.loads(data[:-1].decode('ascii'), object_pairs_hook=unique_object)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)
        os.close(fd)


def read_only_mount(path):
    result = subprocess.run(['/usr/bin/findmnt', '--mountpoint', str(path), '-n', '-o', 'OPTIONS'],
                            check=True, capture_output=True, text=True, timeout=10)
    if 'ro' not in result.stdout.strip().split(','):
        raise ValueError('test bundle and maintenance root must be read-only mounts')


def activate_virtual_nvme():
    """Activate only the one expected emulated controller for this test boot."""
    controllers = []
    for device in PCI_DEVICES.iterdir():
        pci_class = (device / 'class').read_text(encoding='ascii').strip()
        if not re.fullmatch(r'0x[0-9a-f]{6}', pci_class):
            raise ValueError('unknown PCI class')
        if pci_class == '0x010802':
            controllers.append(((device / 'vendor').read_text(encoding='ascii').strip(),
                                (device / 'device').read_text(encoding='ascii').strip()))
    if controllers != [('0x1b36', '0x0010')]:
        raise ValueError('expected exactly one QEMU NVMe PCI controller')
    for module in ('nvme_core', 'nvme'):
        subprocess.run(['/usr/bin/modprobe', '--ignore-install', module], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    subprocess.run(['/usr/bin/udevadm', 'settle', '--timeout=30'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    # Type=idle getty may create its credential mount after its start job ends.
    # This automated guest uses journal/console output and the other serial port;
    # stop interactive login before the backend takes its topology snapshot.
    subprocess.run(['/usr/bin/systemctl', '--no-ask-password', 'stop', 'serial-getty@ttyS0.service'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)


def require_virtual_target():
    """Guard this test adapter against accidental use on a physical system."""
    device = Path(TARGET)
    if not stat.S_ISBLK(device.stat().st_mode):
        raise ValueError('test target is not a block device')
    inventory = [p for p in Path('/sys/class/block').iterdir()
                 if re.fullmatch(r'nvme[0-9]+n[0-9]+', p.name)]
    if len(inventory) != 1 or inventory[0].name != 'nvme0n1':
        raise ValueError('expected one test NVMe namespace')
    controller = (inventory[0] / 'device').resolve(strict=True)
    if ((controller / 'serial').read_text().strip() != NVME_SERIAL or
            (controller / 'model').read_text().strip() != 'QEMU NVMe Ctrl'):
        raise ValueError('test NVMe identity mismatch')
    if not any((p / 'vendor').is_file() and (p / 'device').is_file()
               and (p / 'vendor').read_text().strip() == '0x1b36'
               and (p / 'device').read_text().strip() == '0x0010'
               for p in controller.parents):
        raise ValueError('test NVMe PCI identity mismatch')


def expected_backend_refusal(exc, shared, action, selected, expected):
    """Only classify the actual targeted backend rejection as a negative pass."""
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        frames.append(tb)
        tb = tb.tb_next
    names = [frame.tb_frame.f_code.co_name for frame in frames]
    origin = str(Path(shared.__file__).resolve())
    catalog_origin = str(Path(shared.catalog.__file__).resolve())
    backend = any(frame.tb_frame.f_code.co_name == 'install_observed_block'
                  and frame.tb_frame.f_code.co_filename == origin for frame in frames)
    if not backend:
        return None
    if (action == 'fresh' and selected is None and type(exc) is ValueError
            and str(exc) == 'machine profile is not uniquely and completely recognized'
            and any(frame.tb_frame.f_code.co_name == '_observed_profile'
                    and frame.tb_frame.f_code.co_filename == origin for frame in frames)):
        return 'BACKEND_MODEL_SELECTION', 'UNRECOGNIZED_DMI'
    if (action == 'bad-catalog' and type(exc) is shared.catalog.Refused
            and str(exc) == 'invalid_catalog_or_payload' and expected == '0' * 64
            and names[-1:] == ['require']):
        for frame in frames:
            local = frame.tb_frame.f_locals
            if (frame.tb_frame.f_code.co_name == 'verify_file'
                    and frame.tb_frame.f_code.co_filename == catalog_origin
                    and local.get('collect') is True and local.get('expected_hash') == expected
                    and local.get('digest') is not None
                    and local['digest'].hexdigest() != expected
                    and linecache.getline(catalog_origin, frame.tb_lineno).strip() ==
                    'require(digest.hexdigest() == expected_hash)'):
                return 'BACKEND_CATALOG_HASH', 'CATALOG_DIGEST_MISMATCH'
    return None


class TopologyObservation:
    """Passively summarize real backend return values; never change them."""
    fields = ('device_number', 'syspath', 'sysfs_dev', 'sysfs_ino', 'named_dev', 'named_ino',
              'held_dev', 'held_ino', 'size', 'related', 'source_sig', 'backing_sig', 'mountinfo', 'swaps')

    def __init__(self, shared, previous):
        self.codes = {'target_observation': shared._target_observation.__code__,
                      'block_facts': shared._block_facts.__code__}
        self.previous = previous
        self.data = {name: {'count': 0, 'first': None, 'latest': None, 'changed': set()}
                     for name in self.codes}
        self.error = None

    def __call__(self, frame, event, arg):
        try:
            if event == 'return' and arg is not None:
                for name, code in self.codes.items():
                    if frame.f_code is not code or frame.f_code.co_filename != code.co_filename:
                        continue
                    if name == 'target_observation':
                        if type(arg) is not tuple or len(arg) != len(self.fields):
                            break
                        value = arg
                    else:
                        if type(arg) is not dict:
                            break
                        value = dict(arg)
                    record = self.data[name]
                    record['count'] += 1
                    if record['first'] is None:
                        record['first'] = value
                    else:
                        first = record['first']
                        if name == 'target_observation':
                            record['changed'].update(key for key, old, new in zip(self.fields, first, value)
                                                     if old != new)
                        else:
                            record['changed'].update(key for key in set(first) | set(value)
                                                     if first.get(key) != value.get(key) or
                                                     (key in first) != (key in value))
                    record['latest'] = value
                    break
        except Exception as exc:
            self.error = type(exc).__name__
        if self.previous is not None:
            self.previous(frame, event, arg)

    def report(self):
        result = {name: {'count': record['count'], 'fields_changed': sorted(record['changed'])}
                  for name, record in self.data.items()}
        record = self.data['target_observation']
        if record['first'] is not None and 'mountinfo' in record['changed']:
            first, latest = record['first'][12], record['latest'][12]
            if (type(first) is tuple and type(latest) is tuple and len(first) == len(latest) == 2
                    and all(isinstance(item, str) for item in first + latest)):
                details, left = {}, 12
                for index, namespace in enumerate(('self', 'pid1')):
                    before, after = first[index].splitlines(), latest[index].splitlines()
                    removed, added = sorted(set(before) - set(after)), sorted(set(after) - set(before))
                    entry = {'removed_count': len(removed), 'added_count': len(added),
                             'ordering_changed': first[index] != latest[index] and not removed and not added}
                    for label, lines in (('removed', removed), ('added', added)):
                        entry[label] = [line[:400] for line in lines[:left]]
                        left -= len(entry[label])
                    details[namespace] = entry
                result['mountinfo'] = details
        if self.error is not None:
            result['observer_error'] = self.error
        return result


def observe_operation(shared, operation, *args, **kwargs):
    previous = sys.getprofile()
    observer = TopologyObservation(shared, previous)
    try:
        sys.setprofile(observer)
        try:
            return operation(*args, **kwargs)
        finally:
            sys.setprofile(previous)
    except BaseException:
        try:
            print('GBOS_QEMU_OBSERVATION ' + json.dumps(observer.report(), sort_keys=True,
                                                        separators=(',', ':')), flush=True)
        except Exception:
            pass  # Diagnostic output must not replace the backend exception.
        raise


def run(request):
    if (not isinstance(request, dict) or set(request) !=
            {'schema', 'action', 'expected_catalog_sha256', 'expected_profile', 'payloads'}
            or type(request['schema']) is not int or request['schema'] != 1
            or request['action'] not in ('fresh', 'update', 'bad-catalog')
            or request['expected_profile'] not in PROFILES
            or not isinstance(request['expected_catalog_sha256'], str)
            or not HASH.fullmatch(request['expected_catalog_sha256'])
            or not isinstance(request['payloads'], dict) or set(request['payloads']) != set(PROFILES)):
        raise ValueError('invalid test control request')
    action, expected = request['action'], request['expected_catalog_sha256']
    if action == 'bad-catalog' and expected != '0' * 64:
        raise ValueError('bad catalog scenario requires deliberately wrong digest')
    read_only_mount('/')
    read_only_mount(BUNDLE)
    shared = load('guest_shared_source', BUNDLE / 'repo/install/stick/shared-source.py')
    fixture = BUNDLE / 'fixtures'
    catalog_name = 'catalog.json' if action == 'update' else 'fresh-catalog.json'
    raw = (fixture / catalog_name).read_bytes()
    if len(raw) > shared.catalog.MAX_CATALOG:
        raise ValueError('catalog too large')
    items = shared.catalog.schema(raw)
    observed = {item['profile']: item['image'] for item in items}
    if observed != request['payloads']:
        raise ValueError('host fixture mapping differs from catalog')
    if action != 'bad-catalog':
        with shared.catalog.validate(str(fixture), catalog_name, expected):
            pass
    elif hashlib.sha256(raw).hexdigest() == expected:
        raise ValueError('bad catalog digest unexpectedly matched')
    activate_virtual_nvme()
    require_virtual_target()
    try:
        selected = shared._observed_profile(Path('/sys/class/dmi/id'))
    except ValueError:
        selected = None  # The real block backend must independently reject unknown DMI.
    if selected is not None and selected != request['expected_profile']:
        raise ValueError('recognized profile differs from host expectation')
    facts = shared.observe_block_target(TARGET)
    operation = shared.apply_observed_block if action == 'update' else shared.install_observed_block
    try:
        result = observe_operation(shared, operation, str(fixture), catalog_name, expected, TARGET,
                                   expected_target=facts)
    except (ValueError, shared.update.Refuse) as exc:
        classification = expected_backend_refusal(exc, shared, action, selected, expected)
        if classification is None:
            raise RuntimeError('unexpected backend refusal: ' + type(exc).__name__ + ': ' + str(exc)[:400]) from exc
        stage, reason_code = classification
        return {'status': 'REFUSED', 'action': action, 'stage': stage, 'reason_code': reason_code,
                'exception_class': type(exc).__name__, 'reason': str(exc),
                'scope': 'SYNTHETIC_QEMU_ONLY', 'hardware': 'NOT_VERIFIED'}
    if selected is None or action == 'bad-catalog':
        raise RuntimeError('negative scenario unexpectedly reached a successful writer')
    if result.get('profile') != request['expected_profile']:
        raise RuntimeError('backend returned an unexpected profile')
    return {'status': 'OK', 'action': action, 'profile': result['profile'], 'backend': result,
            'scope': 'SYNTHETIC_QEMU_ONLY', 'hardware': 'NOT_VERIFIED'}


def main():
    if sys.argv[1:] != ['run']:
        print('Only the test guest service may invoke run; prepare is a host Python API.', file=sys.stderr)
        return 2
    try:
        result = run(control())
    except Exception as exc:
        result = {'status': 'ERROR', 'reason': type(exc).__name__ + ': ' + str(exc)[:400],
                  'scope': 'SYNTHETIC_QEMU_ONLY', 'hardware': 'NOT_VERIFIED'}
    print('GBOS_QEMU_RESULT ' + json.dumps(result, sort_keys=True, separators=(',', ':')), flush=True)
    return 0 if result['status'] in ('OK', 'REFUSED') else 1


if __name__ == '__main__':
    sys.exit(main())
