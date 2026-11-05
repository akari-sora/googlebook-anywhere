#!/usr/bin/env python3
"""Run a test-only, rootless, instrumented shared-maintenance QEMU workflow.

All targets are fresh regular files; synthetic payloads and virtual NVMe do not
prove hardware acceptance or grant physical write authority.
"""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import gptlib  # noqa: E402

MIB = 1024 * 1024
UNIT = b'''[Unit]\nDescription=Googlebook synthetic QEMU test only\nAfter=local-fs.target systemd-udev-settle.service serial-getty@ttyS0.service systemd-sysusers.service systemd-tmpfiles-setup.service systemd-random-seed.service systemd-update-done.service ldconfig.service systemd-logind.service\nWants=systemd-udev-settle.service serial-getty@ttyS0.service systemd-sysusers.service systemd-tmpfiles-setup.service systemd-random-seed.service systemd-update-done.service ldconfig.service systemd-logind.service\nBefore=gbos-universal-diagnostic.service\n[Service]\nType=oneshot\nRuntimeDirectory=gbos-qemu-test\nExecStartPre=/usr/bin/mount -o ro,noload,nosuid,nodev LABEL=GBOS_QEMU_TEST /run/gbos-qemu-test\nExecStart=/usr/bin/python3 /run/gbos-qemu-test/repo/tests/stick/shared-qemu-guest.py run\nStandardOutput=journal+console\nStandardError=inherit\n[Install]\nWantedBy=multi-user.target\n'''


def progress(phase, **details):
    print(json.dumps({'event': 'QEMU_TEST_PROGRESS', 'phase': phase, **details}, sort_keys=True), flush=True)


def need(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def command(args, fds=(), timeout=600):
    proc = subprocess.run([str(x) for x in args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          pass_fds=tuple(fds), timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f'{args[0]} returned {proc.returncode}: {proc.stderr[-3000:].decode(errors="replace")}')
    return proc.stdout


def digest(fd):
    h = hashlib.sha256()
    offset = 0
    while data := os.pread(fd, 4 * MIB, offset):
        h.update(data)
        offset += len(data)
    return h.hexdigest()


def opened(path, write=False):
    fd = os.open(path, (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        need(stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_size > 0, 'expected nonempty regular file')
    except BaseException:
        os.close(fd)
        raise
    return fd


def create(path, size=0):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.ftruncate(fd, size)
    return fd


def fp(fd):
    return f'/proc/self/fd/{fd}'


def firmware(path):
    fd = opened(path)
    try:
        need(os.fstat(fd).st_size <= 16 * MIB, 'firmware exceeds size limit')
        return fd
    except BaseException:
        os.close(fd)
        raise


def target_named(fd, path, size):
    held, named = os.fstat(fd), os.stat(path, follow_symlinks=False)
    need(stat.S_ISREG(held.st_mode) and stat.S_ISREG(named.st_mode) and
         (held.st_dev, held.st_ino, held.st_size) == (named.st_dev, named.st_ino, size) and
         held.st_size == size and held.st_nlink == 1, 'owned target path changed')


def copy_firmware(sourcefd, destfd):
    before = os.fstat(sourcefd)
    data = os.pread(sourcefd, before.st_size, 0)
    need(len(data) == before.st_size and os.pwrite(destfd, data, 0) == len(data), 'firmware copy failed')
    after = os.fstat(sourcefd)
    need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
         (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'firmware changed')
    os.fsync(destfd)


def prepare_overlay(source, work):
    _, parts = gptlib.read_gpt(fp(source))
    matches = [p for p in parts.values() if p['name'] == 'gbos-maint']
    need(len(matches) == 1, 'missing unique maintenance partition')
    part = matches[0]
    offset = part['first'] * 512
    size = (part['last'] - part['first'] + 1) * 512
    need(size > 0 and offset + size <= os.fstat(source).st_size, 'invalid partition bounds')
    image = work / 'maint.img'
    fd = create(image, size)
    try:
        for pos in range(0, size, MIB):
            data = os.pread(source, min(MIB, size - pos), offset + pos)
            need(len(data) == min(MIB, size - pos), 'short maintenance read')
            if any(data):
                need(os.pwrite(fd, data, pos) == len(data), 'short maintenance write')
        os.fsync(fd)
    finally:
        os.close(fd)
    unitfile = work / 'service'
    unitfile.write_bytes(UNIT)
    def debugfs(query, write=False):
        return command(['debugfs', *(['-w'] if write else []), '-R', query, image], timeout=120)
    need(not debugfs('stat /etc/systemd/system/gbos-qemu-test.service'), 'test unit already present')
    debugfs(f'write {unitfile} /etc/systemd/system/gbos-qemu-test.service', True)
    debugfs('symlink /etc/systemd/system/multi-user.target.wants/gbos-qemu-test.service ../gbos-qemu-test.service', True)
    need(debugfs('cat /etc/systemd/system/gbos-qemu-test.service') == UNIT, 'unit readback mismatch')
    link = debugfs('stat /etc/systemd/system/multi-user.target.wants/gbos-qemu-test.service')
    need(b'Type: symlink' in link and b'../gbos-qemu-test.service' in link, 'unit link readback mismatch')
    overlay = work / 'usb.qcow2'
    command(['qemu-img', 'create', '-f', 'qcow2', '-F', 'raw', '-b', fp(source), overlay], (source,))
    overlayfd = opened(overlay, True)
    patch = work / 'chunk'
    patchfd = create(patch)
    changed = 0
    try:
        with image.open('rb') as modified:
            for pos in range(0, size, MIB):
                data = modified.read(min(MIB, size - pos))
                previous = os.pread(source, len(data), offset + pos)
                need(len(data) == len(previous), 'short maintenance compare')
                if data == previous:
                    continue
                os.ftruncate(patchfd, 0)
                need(os.pwrite(patchfd, data, 0) == len(data), 'short patch write')
                command(['qemu-io', '-f', 'qcow2', '-c',
                         f'write -q -s {fp(patchfd)} {offset + pos} {len(data)}', fp(overlayfd)],
                        (source, overlayfd, patchfd))
                changed += 1
        need(changed > 0, 'no test unit blocks changed')
        os.fsync(overlayfd)
    finally:
        os.close(patchfd)
        patch.unlink()
    baseline = work / 'usb-baseline.qcow2'
    with baseline.open('xb') as out, overlay.open('rb') as inp:
        shutil.copyfileobj(inp, out, 4 * MIB)
        out.flush()
        os.fsync(out.fileno())
    return overlayfd, baseline, changed


def make_bundle(work, controller=False):
    bundle = work / 'bundle'
    bundle.mkdir(mode=0o700)
    spec = importlib.util.spec_from_file_location('shared_qemu_guest', HERE / 'shared-qemu-guest.py')
    need(spec is not None and spec.loader is not None, 'guest controller missing')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    manifest = module.prepare(ROOT, bundle, controller=controller)
    (work / 'fixture-manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    total = sum(x.stat().st_size for x in bundle.rglob('*') if x.is_file())
    size = ((total + 128 * MIB + MIB - 1) // MIB) * MIB
    aux = work / 'aux.img'
    fd = create(aux, size)
    try:
        command(['mke2fs', '-q', '-F', '-t', 'ext4', '-L', 'GBOS_QEMU_TEST', '-d', bundle, fp(fd)], (fd,))
        os.fsync(fd)
    finally:
        os.close(fd)
    return manifest, bundle, aux


def connect(path, proc):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.connect(str(path))
            sock.settimeout(5)
            return sock
        except OSError:
            sock.close()
            need(proc.poll() is None, 'QEMU exited before socket creation')
            time.sleep(.1)
    raise RuntimeError('QEMU socket timeout')


class QMP:
    def __init__(self, path, proc):
        self.sock = connect(path, proc)
        self.stream = self.sock.makefile('rwb', buffering=0)
        self.read()
        self.execute('qmp_capabilities')

    def read(self):
        line = self.stream.readline()
        need(line, 'QMP disconnected')
        return json.loads(line)

    def execute(self, name):
        self.stream.write(json.dumps({'execute': name}).encode() + b'\n')
        while True:
            result = self.read()
            if 'error' in result:
                raise RuntimeError(f'QMP {name}: {result["error"]}')
            if 'return' in result:
                return result['return']

    def stats(self):
        found = {entry.get('device'): entry.get('stats') for entry in self.execute('query-blockstats')}
        for name in ('stick', 'nv'):
            need(isinstance(found.get(name), dict), f'missing QMP {name} counters')
            need(all(type(found[name].get(k)) is int for k in ('wr_bytes', 'wr_operations', 'unmap_operations')),
                 f'incomplete QMP {name} counters')
        return {name: found[name] for name in ('stick', 'nv')}

    def close(self):
        self.stream.close()
        self.sock.close()


def no_writes(stats):
    return all(stats.get(k) == 0 for k in ('wr_bytes', 'wr_operations', 'unmap_operations'))


def stopped_stats(qmp):
    qmp.execute('stop')
    return qmp.stats()


def guest_result(output):
    # A serial read can end at an inner closing brace of a larger JSON record.
    # Only parse a complete line, retaining partial records for the next read.
    match = re.search(rb'GBOS_QEMU_RESULT (\{[^\r\n]*\})\r*\n', output)
    return json.loads(match.group(1)) if match else None


def check_negative(result, stats, expected, tag):
    need(no_writes(stats['nv']), f'{tag}: refusal wrote NVMe')
    need(result.get('status') == 'REFUSED' and
         tuple(result.get(k) for k in ('stage', 'reason_code', 'exception_class', 'reason')) == expected,
         f'{tag}: unexpected refusal stage/reason: {result}')


def check_controller_result(result, stats, request, tag):
    need(no_writes(stats['stick']), f'{tag}: USB was written or unmapped')
    mode = request.get('mode')
    if mode == 'controller-cancel':
        need(no_writes(stats['nv']) and result.get('status') == 'CANCELLED'
             and result.get('controller') == {'status': 'CANCELLED', 'hardware': 'NOT_VERIFIED'},
             f'{tag}: cancellation was not read-only')
    elif mode == 'controller':
        need(stats['nv']['wr_operations'] > 0 and result.get('status') == 'COMPLETED'
             and result.get('profile') == request['expected_profile']
             and result.get('controller') == {'status': 'COMPLETED', 'hardware': 'NOT_VERIFIED'},
             f'{tag}: unexpected controller result {result}')
    else:
        raise ValueError('unknown controller mode')


def run_guest(work, tag, source, overlayfd, aux, targetfd, target, targetsize,
              code, vars_template, identity, request, negative=None):
    vars_path = work / f'{tag}-vars.fd'
    with ExitStack() as stack:
        vars_source = firmware(vars_template)
        stack.callback(os.close, vars_source)
        varsfd = create(vars_path)
        stack.callback(os.close, varsfd)
        copy_firmware(vars_source, varsfd)
        codefd = firmware(code)
        stack.callback(os.close, codefd)
        auxfd = opened(aux)
        stack.callback(os.close, auxfd)
        target_named(targetfd, target, targetsize)
        fds = (source, overlayfd, auxfd, targetfd, codefd, varsfd)
        need(len({(os.fstat(x).st_dev, os.fstat(x).st_ino) for x in fds}) == len(fds), 'disk alias')
        with tempfile.TemporaryDirectory(prefix='gbq-', dir='/tmp') as temp:
            qmp, console, control = (Path(temp) / x for x in ('qmp', 'console', 'control'))
            vendor = identity.get('sys_vendor', 'Unknown')
            product = identity.get('product_name', 'Unknown')
            board_vendor = identity.get('board_vendor', vendor)
            board_name = identity.get('board_name', product)
            need(all(re.fullmatch(r'[A-Za-z0-9 ._-]{1,64}', s) for s in
                     (vendor, product, board_vendor, board_name)), 'invalid synthetic SMBIOS')
            args = ['qemu-system-x86_64', '-machine', 'q35,accel=tcg', '-cpu', 'max', '-m', '2048', '-smp', '2',
                    '-no-reboot', '-display', 'none', '-vga', 'none', '-monitor', 'none', '-nic', 'none',
                    '-drive', f'if=pflash,format=raw,readonly=on,file={fp(codefd)}',
                    '-drive', f'if=pflash,format=raw,file={fp(varsfd)}',
                    '-device', 'qemu-xhci,id=xhci,bus=pcie.0,addr=0x3',
                    '-drive', f'file={fp(overlayfd)},if=none,id=stick,format=qcow2',
                    '-device', 'usb-storage,bus=xhci.0,drive=stick,bootindex=1',
                    '-drive', f'file={fp(auxfd)},if=none,id=aux,format=raw,readonly=on',
                    '-device', 'usb-storage,bus=xhci.0,drive=aux',
                    '-drive', f'file={fp(targetfd)},if=none,id=nv,format=raw',
                    '-device', 'nvme,id=controller,serial=GBOSQEMUTEST01,bus=pcie.0,addr=0x2',
                    '-device', 'nvme-ns,drive=nv,bus=controller,nsid=1,eui64=0x1234567890abcdef,logical_block_size=512',
                    '-smbios', f'type=1,manufacturer={vendor},product={product}',
                    '-smbios', f'type=2,manufacturer={board_vendor},product={board_name}',
                    '-qmp', f'unix:{qmp},server=on,wait=off',
                    '-serial', f'unix:{console},server=on,wait=on',
                    '-serial', f'unix:{control},server=on,wait=off']
            with (work / f'{tag}-qemu.log').open('xb') as log, (work / f'{tag}-serial.log').open('xb') as serial_log:
                proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        close_fds=True, pass_fds=fds, start_new_session=True)
                q = serial = ctl = None
                output = bytearray()
                try:
                    serial = connect(console, proc)
                    ctl = connect(control, proc)
                    q = QMP(qmp, proc)
                    serial.settimeout(1)
                    started = time.monotonic()
                    end = started + 600
                    heartbeat = started + 30
                    sent = False
                    result = None
                    while time.monotonic() < end and proc.poll() is None:
                        now = time.monotonic()
                        if now >= heartbeat:
                            progress('guest-wait', case=tag, seconds=int(now - started), ready=sent)
                            heartbeat = now + 30
                        try:
                            chunk = serial.recv(65536)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        output.extend(chunk)
                        need(len(output) <= 8 * MIB, 'serial output limit exceeded')
                        serial_log.write(chunk)
                        serial_log.flush()
                        if not sent and b'GBOS_QEMU_READY' in output:
                            ctl.sendall(json.dumps(request, separators=(',', ':')).encode() + b'\n')
                            sent = True
                        result = guest_result(output)
                        if result is not None:
                            break
                    need(sent and result is not None, f'{tag}: guest did not complete')
                    stats = stopped_stats(q)
                    with (work / f'{tag}-result.json').open('x') as evidence:
                        json.dump({'guest': result, 'qmp': stats}, evidence, sort_keys=True, indent=2)
                        evidence.write('\n')
                    need(no_writes(stats['stick']), f'{tag}: USB was written or unmapped')
                    if request.get('mode') in ('controller', 'controller-cancel'):
                        check_controller_result(result, stats, request, tag)
                    elif negative is not None:
                        check_negative(result, stats, negative, tag)
                    else:
                        need(stats['nv']['wr_operations'] > 0, f'{tag}: no actual NVMe writes')
                        expected = 'INSTALLED_OBSERVED_BLOCK' if request['action'] == 'fresh' else 'APPLIED_OBSERVED_BLOCK'
                        need(result.get('status') == 'OK' and result.get('profile') == request['expected_profile']
                             and result.get('backend', {}).get('result') == expected,
                             f'{tag}: unexpected guest result {result}')
                    return {'guest': result, 'qmp': stats}
                finally:
                    if q:
                        try:
                            q.execute('quit')
                        except (OSError, RuntimeError):
                            pass
                        q.close()
                    if serial:
                        serial.close()
                    if ctl:
                        ctl.close()
                    if proc.poll() is None:
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.terminate()
                            try:
                                proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait()
                    else:
                        proc.wait()
                    target_named(targetfd, target, targetsize)


def selected_path(bundle, item):
    raw = item['path']
    need(isinstance(raw, str) and not Path(raw).is_absolute(), 'invalid fixture path')
    root = (bundle / 'fixtures').resolve(strict=True)
    path = (root / raw).resolve(strict=True)
    need(path.is_relative_to(root) and path.is_file(), 'fixture path outside bundle')
    fd = opened(path)
    try:
        need(os.fstat(fd).st_size == item['size'] and digest(fd) == item['sha256'], 'fixture digest mismatch')
    finally:
        os.close(fd)
    return path


def part_hash(fd, part):
    return gptlib.sha(fp(fd), part['first'] * 512, (part['last'] - part['first'] + 1) * 512)


def fat_read(fd, part, path):
    return command(['mtype', '-i', f'{fp(fd)}@@{part["first"] * 512}', f'::{path}'], (fd,))


def parse_entry(data):
    fields = {}
    for line in data.decode('utf-8').splitlines():
        key, sep, value = line.partition(' ')
        need(sep and value and key in ('title', 'linux', 'initrd', 'options') and key not in fields,
             'malformed or duplicate boot entry field')
        fields[key] = value
    need(set(fields) == {'title', 'linux', 'initrd', 'options'}, 'incomplete boot entry')
    for key in ('linux', 'initrd'):
        path = fields[key]
        need(path.startswith('/') and '..' not in Path(path).parts and not any(c.isspace() for c in path),
             'invalid boot entry path')
    return fields


def lifted_options(options):
    tokens = options.split()
    old = 'initcall_blacklist=nvme_init,mei_me_driver_init'
    need(tokens.count(old) == 1 and 'efi=noruntime' in tokens, 'unexpected source boot options')
    return ' '.join('initcall_blacklist=mei_me_driver_init' if x == old else x for x in tokens)


def check_bootconfig(fd, esp, path, slot, boot_guid):
    lines = gptlib.bootconfig(fat_read(fd, esp, path)).splitlines()
    for prefix, expected in ((b'androidboot.boot_part_uuid=', boot_guid.encode()),
                             (b'androidboot.slot_suffix=', ('_' + slot).encode())):
        hits = [line for line in lines if line.startswith(prefix)]
        need(hits == [prefix + expected], 'bootconfig slot or GUID mismatch')


def check_menu(fd, esp, releasefd, release_esp, slot, boot_guid, previous=None):
    loader = b'default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n'
    need(fat_read(fd, esp, '/loader/loader.conf') == loader, 'loader default or policy differs')
    kernel = fat_read(releasefd, release_esp, '/kernel.efi')
    entries = {}
    for name in ('lapis-quiet.conf', 'lapis-debug.conf'):
        entry = parse_entry(fat_read(fd, esp, '/loader/entries/' + name))
        source = parse_entry(fat_read(releasefd, release_esp, '/loader/entries/' + name))
        expected_initrd = '/initrd.img' if slot == 'a' else '/gbos/b/initrd.img'
        need(entry['title'] == source['title'] and entry['options'] == lifted_options(source['options']) and
             entry['initrd'] == expected_initrd and
             entry['linux'] in (('/kernel.efi',) if slot == 'a' else ('/kernel.efi', '/gbos/b/kernel.efi')),
             'current boot entry differs from the expected release/slot')
        need(fat_read(fd, esp, entry['linux']) == kernel, 'boot entry kernel differs from release')
        check_bootconfig(fd, esp, entry['initrd'], slot, boot_guid)
        entries[name] = entry
        if previous is not None:
            prev = parse_entry(fat_read(fd, esp, '/loader/entries/prev-' + name))
            old = previous[name]
            need(prev == dict(old, title=old['title'] + ' (previous version)'), 'previous boot entry changed')
            need(fat_read(fd, esp, prev['linux']) == previous['_kernel'], 'previous kernel changed')
            check_bootconfig(fd, esp, prev['initrd'], 'a', boot_guid)
    entries['_kernel'] = kernel
    return entries


def verify_fresh(targetfd, source):
    releasefd = opened(source)
    try:
        original, src = gptlib.read_gpt(fp(releasefd))
        header, parts = gptlib.read_gpt(fp(targetfd))
        need(len(parts) == len(src), 'partition count changed')
        old_ids = {original['disk'], *(p['guid'] for p in src.values())}
        new_ids = {header['disk'], *(p['guid'] for p in parts.values())}
        need(len(new_ids) == len(parts) + 1 and not old_ids & new_ids, 'fresh GUIDs not new and unique')
        for n, p in parts.items():
            need(n in src and all(p[k] == src[n][k] for k in ('name', 'first', 'type', 'attrs')),
                 'fresh partition geometry differs')
            if p['name'] == 'userdata':
                need(p['last'] == header['last'] and p['last'] > src[n]['last'], 'userdata not grown')
                need(gptlib.sha(fp(targetfd), p['first'] * 512, 16 * MIB) == hashlib.sha256(bytes(16 * MIB)).hexdigest(),
                     'userdata prefix not zero')
            else:
                need(p['last'] == src[n]['last'], 'fresh non-userdata geometry changed')
                if p['name'] != 'EFI-SYSTEM':
                    need(part_hash(targetfd, p) == part_hash(releasefd, src[n]), f'fresh partition mismatch: {p["name"]}')
        names = {p['name']: p for p in parts.values()}
        srcnames = {p['name']: p for p in src.values()}
        menu = check_menu(targetfd, names['EFI-SYSTEM'], releasefd, srcnames['EFI-SYSTEM'],
                          'a', names['boot_a']['guid'])
        return (header, parts), menu
    finally:
        os.close(releasefd)


def verify_update(targetfd, before, before_hashes, release, previous):
    header, parts = gptlib.read_gpt(fp(targetfd))
    need((header, parts) == before, 'update changed GPT')
    by_name = {p['name']: p for p in parts.values()}
    for name, expected in before_hashes.items():
        need(part_hash(targetfd, by_name[name]) == expected, f'update changed preserved partition: {name}')
    releasefd = opened(release)
    try:
        _, release_parts = gptlib.read_gpt(fp(releasefd))
        release_names = {p['name']: p for p in release_parts.values()}
        for name in ('boot', 'init_boot', 'vendor_boot', 'vbmeta', 'pvmfw', 'recovery', 'ota_recovery'):
            need(part_hash(targetfd, by_name[name + '_b']) == part_hash(releasefd, release_names[name + '_a']),
                 f'updated target partition differs: {name}')
        check_menu(targetfd, by_name['EFI-SYSTEM'], releasefd, release_names['EFI-SYSTEM'],
                   'b', by_name['boot_a']['guid'], previous)
    finally:
        os.close(releasefd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'source-sha256', 'work', 'ovmf-code', 'ovmf-vars'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--controller', action='store_true', help='exercise controller against virtual NVMe')
    a = parser.parse_args()
    need(sys.platform.startswith('linux') and os.geteuid() != 0, 'requires non-root Linux host')
    need(re.fullmatch('[0-9a-f]{64}', a.source_sha256), 'invalid source digest')
    need(not any(c.isspace() for c in a.work), 'workspace path cannot contain whitespace')
    work = Path(a.work)
    work.mkdir(mode=0o700)
    need(not work.is_symlink() and stat.S_IMODE(work.stat().st_mode) == 0o700, 'workspace not private')
    source = opened(a.source)
    overlayfd = None
    try:
        progress('source-hash')
        before = os.fstat(source)
        need(digest(source) == a.source_sha256, 'source digest mismatch')
        progress('prepare-overlay')
        overlayfd, baseline, changed = prepare_overlay(source, work)
        progress('prepare-fixtures')
        manifest, bundle, aux = make_bundle(work, controller=a.controller)
        spec = importlib.util.spec_from_file_location('shared_qemu_lp', HERE / 'shared-qemu-lp.py')
        need(spec is not None and spec.loader is not None, 'independent LP checker missing')
        lp = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = lp
        spec.loader.exec_module(lp)
        checks = {}
        for profile in ('kano', 'amd-hx370'):
            target = work / f'{profile}.img'
            targetfd = create(target, manifest['target_size'])
            try:
                target_named(targetfd, target, manifest['target_size'])
                fresh_source = selected_path(bundle, manifest['fresh_payloads'][profile])
                _, source_parts = gptlib.read_gpt(fresh_source)
                data = next(p for p in source_parts.values() if p['name'] == 'userdata')
                need(os.pwrite(targetfd, b'\xa5' * (16 * MIB), data['first'] * 512) == 16 * MIB,
                     'sentinel write failed')
                os.fsync(targetfd)
                for action in ('fresh', 'update'):
                    fresh = action == 'fresh'
                    request = {'schema': 1, 'action': action, 'expected_profile': profile,
                               'expected_catalog_sha256': manifest['fresh_catalog_sha256' if fresh else 'catalog_sha256'],
                               'payloads': manifest['fresh_payloads' if fresh else 'payloads']}
                    if a.controller:
                        request['mode'] = 'controller'
                    selected_path(bundle, request['payloads'][profile])
                    tag = f'{profile}-{action}'
                    progress('guest-start', case=tag)
                    target_named(targetfd, target, manifest['target_size'])
                    checks[tag] = run_guest(work, tag, source, overlayfd, aux, targetfd, target,
                                            manifest['target_size'], a.ovmf_code, a.ovmf_vars,
                                            manifest['smbios'][profile], request)
                    target_named(targetfd, target, manifest['target_size'])
                    progress('guest-done', case=tag)
                    if fresh:
                        before_gpt, menu = verify_fresh(targetfd, fresh_source)
                        names = {p['name']: p for p in before_gpt[1].values()}
                        offset = names['userdata']['first'] * 512 + 1024
                        marker = b'GOOGLEBOOK-PRESERVE-USERDATA'
                        need(os.pwrite(targetfd, marker, offset) == len(marker), 'userdata seed write failed')
                        os.fsync(targetfd)
                        preserved = {'userdata', 'metadata', 'misc', 'frp_persist',
                                     'desktop_security_persist', 'desktop_security_storage'}
                        need(preserved <= set(names), 'required preserved partition missing')
                        before_hashes = {name: part_hash(targetfd, part) for name, part in names.items()
                                         if name.endswith('_a') or name in preserved}
                        old_lp = lp.snapshot(targetfd, names['super'])
                        release = selected_path(bundle, manifest['payloads'][profile])
                        releasefd = opened(release)
                        try:
                            _, release_parts = gptlib.read_gpt(fp(releasefd))
                            release_super = next(p for p in release_parts.values() if p['name'] == 'super')
                            release_lp = lp.snapshot(releasefd, release_super)
                        finally:
                            os.close(releasefd)
                        lp.assert_fixture_change(old_lp, release_lp)
                    else:
                        verify_update(targetfd, before_gpt, before_hashes, release, menu)
                        lp.verify_update(old_lp, lp.snapshot(targetfd, names['super']), release_lp)
                    target_named(targetfd, target, manifest['target_size'])
            finally:
                os.close(targetfd)
        if a.controller:
            target = work / 'controller-cancel.img'
            fd = create(target, manifest['target_size'])
            try:
                before_cancel = digest(fd)
                req = {'schema': 1, 'action': 'fresh', 'mode': 'controller-cancel',
                       'expected_profile': 'kano', 'expected_catalog_sha256': manifest['fresh_catalog_sha256'],
                       'payloads': manifest['fresh_payloads']}
                checks['controller-cancel'] = run_guest(work, 'controller-cancel', source, overlayfd, aux, fd,
                    target, manifest['target_size'], a.ovmf_code, a.ovmf_vars, manifest['smbios']['kano'], req)
                target_named(fd, target, manifest['target_size'])
                need(digest(fd) == before_cancel, 'cancel target changed')
            finally:
                os.close(fd)
        for tag, identity, action, negative in (() if a.controller else (
                ('bad-catalog', manifest['smbios']['kano'], 'bad-catalog',
                 ('BACKEND_CATALOG_HASH', 'CATALOG_DIGEST_MISMATCH', 'Refused', 'invalid_catalog_or_payload')),
                ('unknown', {}, 'fresh', ('BACKEND_MODEL_SELECTION', 'UNRECOGNIZED_DMI', 'ValueError',
                                         'machine profile is not uniquely and completely recognized')))):
            target = work / f'{tag}.img'
            fd = create(target, manifest['target_size'])
            try:
                req = {'schema': 1, 'action': action, 'expected_profile': 'kano',
                       'expected_catalog_sha256': '0' * 64 if action == 'bad-catalog' else manifest['fresh_catalog_sha256'],
                       'payloads': manifest['fresh_payloads']}
                progress('guest-start', case=tag)
                target_named(fd, target, manifest['target_size'])
                checks[tag] = run_guest(work, tag, source, overlayfd, aux, fd, target, manifest['target_size'],
                                        a.ovmf_code, a.ovmf_vars, identity, req, negative=negative)
                target_named(fd, target, manifest['target_size'])
                progress('guest-done', case=tag)
            finally:
                os.close(fd)
        progress('compare-media')
        command(['qemu-img', 'compare', '-f', 'qcow2', '-F', 'qcow2', fp(overlayfd), baseline],
                (source, overlayfd), timeout=1800)
        after = os.fstat(source)
        need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
             (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'source changed')
        progress('source-rehash')
        need(digest(source) == a.source_sha256, 'source content changed')
        result = {'result': 'PASS', 'scope': 'instrumented QEMU maintenance; synthetic payload and NVMe only',
                  'hardware': 'NOT_VERIFIED', 'physical_write_allowed': False, 'changed_maint_chunks': changed,
                  'scenarios': checks}
        (work / 'result.json').write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
        print(json.dumps(result, sort_keys=True))
    finally:
        if overlayfd is not None:
            os.close(overlayfd)
        os.close(source)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'result': 'FAILED', 'reason': str(exc), 'hardware': 'NOT_VERIFIED'}), file=sys.stderr)
        sys.exit(1)
