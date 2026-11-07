#!/usr/bin/env python3
"""Build a QEMU-only shared maintenance diagnostic image; never open devices.

This is not an installer. All input digests need independently reviewed values.
The output and its mandatory .not-self-tested marker must not already exist.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import ctypes
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
PROFILES_ROOT = HERE.parents[1] / 'profiles'
CHUNK = 4 * 1024 * 1024
MIB = 1024 * 1024
ALIGN = 128 * MIB
ESP_SIZE = 512 * MIB
LINUX_TYPE = '0fc63daf-8483-4772-8e79-3d69d8477de4'
EFI_TYPE = 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError('module unavailable')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CAT = load('gbos_catalog', HERE / 'catalog.py')
STICK = load('gbos_maint_stick', HERE / 'maint-stick.py')
RECORD = load('gbos_build_record', HERE.parents[1] / 'builder/lib/build_record.py')
DETECT = load('gbos_detector', HERE / 'detect-model.py')
BASE_PROFILES = ('kano', 'amd-hx370')
PROFILE_ARGS = {'kano': 'kano', 'amd-hx370': 'hx370', 'rog-ally': 'rog_ally'}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def rounded(n, alignment=ALIGN):
    need(type(n) is int and 0 <= n <= 512 * 1024 ** 3, 'invalid size')
    return ((n + alignment - 1) // alignment) * alignment


def capacity(root_bytes, payload_bytes):
    need(root_bytes > 0 and payload_bytes > 0, 'empty input')
    root_size = rounded(max(1024 * MIB, root_bytes + root_bytes // 2 + 512 * MIB))
    data_size = rounded(max(512 * MIB, payload_bytes + payload_bytes // 10 + 256 * MIB))
    # 1 MiB leading alignment, three aligned partitions, plus backup GPT.
    return root_size, data_size, MIB + ESP_SIZE + root_size + data_size + MIB


def hash_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_size > 0, 'input must be a regular file')
        digest = hashlib.sha256()
        while True:
            b = os.read(fd, CHUNK)
            if not b:
                break
            digest.update(b)
        need(CAT.stable_signature(before) == CAT.stable_signature(os.fstat(fd)), 'input changed')
        return digest.hexdigest()
    finally:
        os.close(fd)


def tree_bytes(root):
    need(root.is_dir() and not root.is_symlink(), 'root must be a real directory')
    total = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            elif not stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                raise ValueError('special file in maintenance root')
    return total


def safe_dir(root, relative):
    path = root
    for part in relative.split('/'):
        path /= part
        if path.exists() or path.is_symlink():
            need(path.is_dir() and not path.is_symlink(), 'unsafe staged directory')
        else:
            path.mkdir(mode=0o755)
    return path


def write_new(path, data, mode=0o644):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)


def copy_checked(fd, dest, expected_hash, expected_size):
    before = os.fstat(fd)
    need(stat.S_ISREG(before.st_mode) and before.st_size == expected_size, 'bad payload fd')
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    outfd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(outfd, 'wb') as out:
        while True:
            b = os.read(fd, CHUNK)
            if not b:
                break
            total += len(b)
            need(total <= expected_size, 'payload grew')
            digest.update(b)
            out.write(b)
    need(total == expected_size and digest.hexdigest() == expected_hash and
         CAT.stable_signature(before) == CAT.stable_signature(os.fstat(fd)), 'payload changed')


def commandline(root_guid):
    template = (HERE / 'gbos-install.entry.template').read_text(encoding='utf-8')
    lines = [line for line in template.splitlines() if line.startswith('options ')]
    need(len(lines) == 1, 'unexpected maintenance options')
    tokens = lines[0].split()[1:]
    kept = [t for t in tokens if not t.startswith(('root=', 'console=', 'xe.', 'gbos.'))
            and t not in ('rw', 'rootwait', 'quiet', 'loglevel=3')]
    names = []
    for raw in (HERE / 'builtin-initcalls.txt').read_text(encoding='utf-8').splitlines():
        fields = raw.split('#', 1)[0].split()
        if len(fields) == 2:
            names.append(fields[1])
    need(names and '@INITCALL_BLACKLIST@' in ' '.join(kept), 'initcall map missing')
    kept = [t.replace('@INITCALL_BLACKLIST@', ','.join(names)) for t in kept]
    opts = [f'root=PARTUUID={root_guid}', 'ro', 'rootflags=noload', 'rootwait', 'fsck.mode=skip', 'systemd.gpt_auto=0', 'console=tty0',
            'console=ttyS0,115200'] + kept
    need(all('@' not in t and not t.startswith(('xe.', 'gbos.install', 'gbos.update')) for t in opts),
         'unsafe diagnostic options')
    return ' '.join(opts)


def make_entry(root_guid):
    return ('title Googlebook shared maintenance diagnostic (QEMU only)\n'
            'linux /vmlinuz-linux\ninitrd /initramfs-linux.img\noptions ' +
            commandline(root_guid) + '\n').encode()


def setup_root(root, catalog_sha, data_guid, records=None, profiles=BASE_PROFILES):
    # Unselected optional profiles must not leak from the supplied maintenance root:
    # the detector reads every present optional profile, including stale rules.
    for profile in DETECT.OPTIONAL_PROFILE_IDS:
        if profile not in profiles:
            need(not os.path.lexists(root / 'opt/googlebook/profiles' / profile),
                 'root contains unselected optional profile')
    # An assembled, unmodified maintenance root must not already have installer payloads.
    for rel in ('usr/local/sbin/gbos-install', 'usr/lib/systemd/system/gbos-installer.service',
                'etc/systemd/system/gbos-installer.service', 'usr/local/sbin/gbos-update.py'):
        need(not os.path.lexists(root / rel), 'root contains installer or update payload')
    app = safe_dir(root, 'opt/googlebook/install/stick')
    for name in ('catalog.py', 'detect-model.py', 'universal-diagnostic.py', 'shared-source.py',
                 'universal-menu.py', 'universal-controller.py'):
        write_new(app / name, (HERE / name).read_bytes())
    for name in ('gbos-update.py', 'gbos-fresh.py'):
        write_new(app.parent / name, (HERE.parent / name).read_bytes())
    for profile in profiles:
        target = safe_dir(root, 'opt/googlebook/profiles/' + profile)
        write_new(target / 'profile.toml', (PROFILES_ROOT / profile / 'profile.toml').read_bytes())
    config = safe_dir(root, 'etc/gbos-universal')
    write_new(config / 'catalog.sha256', (catalog_sha + '\n').encode())
    if records:
        record_dir = safe_dir(root, 'etc/gbos-universal/build-records')
        for profile, (raw, pin) in records.items():
            write_new(record_dir / (profile + '.json'), raw)
            write_new(record_dir / (profile + '.sha256'), (pin + '\n').encode())
    safe_dir(root, 'run/gbos-payload')
    etc = safe_dir(root, 'etc')
    fstab = etc / 'fstab'
    if os.path.lexists(fstab):
        need(not fstab.is_symlink() and fstab.is_file(), 'unsafe fstab')
        fstab.unlink()
    # A caller-provided root must not carry an inherited mount instruction for
    # a different machine or disk. The diagnostic owns this entire fstab.
    write_new(fstab, (f'PARTUUID={data_guid} /run/gbos-payload ext4 ro,noload,nosuid,nodev,noexec,nofail 0 0\n'
                      'tmpfs /var tmpfs rw,nodev,nosuid,mode=0755,size=256M 0 0\n').encode())
    safe_dir(root, 'var')
    # Override precisely the pinned root chmod rule, preserving all other rules.
    source = root / 'usr/lib/tmpfiles.d/root.conf'
    need(source.is_file() and not source.is_symlink(), 'missing pinned root tmpfiles rule')
    lines = source.read_text(encoding='utf-8').splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.split() == ['z', '/', '555', '-', '-', '-']]
    need(len(matches) == 1, 'unexpected pinned root tmpfiles rule')
    del lines[matches[0]]
    write_new(safe_dir(root, 'etc/tmpfiles.d') / 'root.conf', ''.join(lines).encode())
    system = safe_dir(root, 'etc/systemd/system')
    unit = system / 'gbos-universal-diagnostic.service'
    write_new(unit, (HERE / 'gbos-universal-diagnostic.service').read_bytes())
    wants = safe_dir(root, 'etc/systemd/system/multi-user.target.wants')
    (wants / unit.name).symlink_to('../' + unit.name)
    unit = system / 'gbos-universal-menu.service'
    write_new(unit, (HERE / unit.name).read_bytes())
    (wants / unit.name).symlink_to('../' + unit.name)


def run_owned(args, root, timeout=120):
    # Keep the child waitable (including after exit) until after signalling its
    # process group. This reserves its PID/PGID and prevents reuse by another job.
    need(signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL,
         'SIGCHLD must have its default handler; no competing child reaper')
    signals = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    handlers = {}
    proc = None
    subreaper = None
    def interrupted(signum, frame):
        raise InterruptedError('preparation interrupted by signal %d' % signum)
    try:
        if sys.platform.startswith('linux'):
            # This synchronous builder owns no other child or thread. Adopting
            # descendants makes waitpid(ECHILD) a completion proof for this job,
            # including namespace processes still exiting after unshare dies.
            need(len(list(Path('/proc/self/task').iterdir())) == 1,
                 'guest preparation requires a single-threaded owner')
            need(not Path(f'/proc/self/task/{os.getpid()}/children').read_text().strip(),
                 'guest preparation requires an owner with no existing children')
            libc = ctypes.CDLL(None, use_errno=True)
            old_subreaper = ctypes.c_int()
            need(libc.prctl(37, ctypes.byref(old_subreaper), 0, 0, 0) == 0,
                 'cannot read child subreaper state')
            need(libc.prctl(36, 1, 0, 0, 0) == 0, 'cannot adopt owned descendants')
            subreaper = (libc, old_subreaper.value)
        for sig in signals:
            handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, interrupted)
        proc = subprocess.Popen(args, cwd=root, close_fds=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                start_new_session=True, env={'PATH': '/usr/sbin:/usr/bin', 'LANG': 'C'})
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            deadline = time.monotonic() + timeout
            while True:
                # WNOWAIT deliberately observes without releasing the leader PID.
                result = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if result is not None:
                    break
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(args, timeout)
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            if result.si_code != os.CLD_EXITED or result.si_status != 0:
                status = result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
                raise subprocess.CalledProcessError(status, args)
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, signals)
            # A waitable child remains reserved until wait(); if unexpectedly
            # reaped by another thread, never signal a possibly reused group.
            try:
                exited = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                raise RuntimeError('owned child was reaped externally; refusing group signal')
            if exited is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait()
            if subreaper is not None:
                # Do not clean the staging root while any owned descendant is
                # still exiting. Signals stay blocked through this final reap.
                while True:
                    try:
                        os.waitpid(-1, 0)
                    except InterruptedError:
                        continue
                    except ChildProcessError:
                        break
    finally:
        if subreaper is not None:
            libc, original_subreaper = subreaper
            need(libc.prctl(36, original_subreaper, 0, 0, 0) == 0,
                 'cannot restore child subreaper state')
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def prepare_guest(root):
    """Prepare the staged guest using the owned namespace and capability boundary."""
    need(sys.platform.startswith('linux') and os.geteuid() == 0, 'Linux root required')
    unshare = shutil.which('unshare')
    need(unshare and os.path.isabs(unshare) and os.access(unshare, os.X_OK),
         'absolute unshare executable required')
    need(os.path.isabs(sys.executable), 'absolute host Python required')
    prefix = [unshare, '--mount', '--pid', '--net', '--fork', '--kill-child=SIGKILL',
              '--propagation', 'private', sys.executable, '-I',
              str(HERE / 'universal-guest-helper.py'), str(root)]
    for command in (['/usr/bin/systemd-sysusers'],
                    ['/usr/bin/systemd-tmpfiles', '--create', '--boot', '--prefix=/etc'],
                    ['/usr/bin/ldconfig']):
        run_owned(prefix + command, root)
    # Mark /etc prepared only after every command has succeeded. /var is tmpfs.
    marker = root / 'etc/.updated'
    need(not marker.is_symlink(), 'unsafe .updated marker')
    marker.touch(exist_ok=True)
    ns = max(time.time_ns(), (root / 'usr').stat().st_mtime_ns + 1_000_000_000)
    os.utime(marker, ns=(ns, ns), follow_symlinks=False)


def run(args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, **kwargs)


def verify_debugfs(tool, image, path, expected_size, expected_sha):
    proc = subprocess.Popen([tool, '-R', 'cat /' + path, str(image)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    digest = hashlib.sha256()
    size = 0
    try:
        for block in iter(lambda: proc.stdout.read(CHUNK), b''):
            size += len(block)
            need(size <= expected_size, 'filesystem member too large')
            digest.update(block)
        need(proc.wait() == 0 and size == expected_size and digest.hexdigest() == expected_sha,
             'filesystem member readback mismatch')
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def write_all(fd, data, offset):
    while data:
        n = os.pwrite(fd, data, offset)
        need(n > 0, 'short write')
        offset += n
        data = data[n:]


def copy_part(src, dest_fd, offset):
    digest = hashlib.sha256()
    off = 0
    with open(src, 'rb') as f:
        while b := f.read(CHUNK):
            digest.update(b)
            if b != bytes(len(b)):
                write_all(dest_fd, b, offset + off)
            off += len(b)
    # Re-read the entire output extent; sparse holes read as zero.
    actual = hashlib.sha256()
    i = 0
    while i < off:
        b = os.pread(dest_fd, min(CHUNK, off - i), offset + i)
        need(bool(b), 'short partition readback')
        actual.update(b)
        i += len(b)
    need(digest.digest() == actual.digest(), 'partition readback mismatch')


def build(args):
    record_args = {profile: (getattr(args, arg + '_build_record', None),
                             getattr(args, arg + '_build_record_sha256', None))
                   for profile, arg in PROFILE_ARGS.items()}
    need(not any(x for pair in record_args.values() for x in pair) or
         all(all(record_args[p]) for p in BASE_PROFILES),
         'both base build record paths and pins are required')
    need(all(all(pair) or not any(pair) for pair in record_args.values()),
         'each build record requires its independent pin')
    need(os.name == 'posix' and sys.platform.startswith('linux'), 'Linux required')
    need(os.geteuid() == 0, 'root required to pack root filesystem owners')
    out = Path(args.out).absolute()
    need(not str(out).startswith('/dev/') and not out.parent.is_symlink(), 'unsafe output')
    need(not os.path.lexists(out) and not os.path.lexists(str(out) + '.not-self-tested'), 'output exists')
    need(out.parent.is_dir(), 'output parent missing')
    tools = {name: shutil.which(name) for name in ('mke2fs', 'mkfs.vfat', 'mcopy', 'mmd', 'mtype', 'debugfs')}
    need(all(tools.values()), 'missing filesystem tools')
    root = Path(args.root).absolute()
    xboot = Path(args.xbootldr).absolute()
    resolved_out = out.resolve()
    for source_dir in (root, xboot, Path(args.bundle).absolute()):
        source = source_dir.resolve()
        need(resolved_out != source and source not in resolved_out.parents,
             'output cannot be inside an input directory')
    need(xboot.is_dir() and not xboot.is_symlink(), 'xbootldr must be a real directory')
    inputs = [(Path(args.efi), args.efi_sha256), (xboot / 'vmlinuz-linux', args.kernel_sha256),
              (xboot / 'initramfs-linux.img', args.initrd_sha256)]
    for path, expected in inputs:
        need(CAT.HASH.fullmatch(expected) and hash_file(path) == expected, 'boot input hash mismatch')
    # The boot copy and root's own kernel/initramfs must describe the same assembled root.
    need(hash_file(root / 'boot/vmlinuz-linux') == args.kernel_sha256 and
         hash_file(root / 'boot/initramfs-linux.img') == args.initrd_sha256, 'root and boot inputs differ')
    root_bytes = tree_bytes(root)
    with CAT.validate(args.bundle, 'catalog.json', args.catalog_sha256) as verified:
        profiles = tuple(p for p in (*BASE_PROFILES, 'rog-ally') if p in verified.descriptors)
        need(set(BASE_PROFILES).issubset(profiles), 'both base profiles required')
        need(not any(record_args['rog-ally']) or 'rog-ally' in profiles,
             'Ally record requires Ally payload')
        if 'rog-ally' in profiles:
            need(all(all(record_args[p]) for p in profiles), 'Ally requires all build records and pins')
            rules = DETECT.load_rules(PROFILES_ROOT)
            need({p for p, _ in rules} == set(profiles) and all(rule for _, rule in rules),
                 'Ally requires configured model rules for every profile')
        records = {}
        if all(record_args['kano']):
            for item in verified.payloads:
                path, pin = record_args[item['profile']]
                raw, record = RECORD.read_pinned(path, pin)
                image = item['image']
                profile = item['profile']
                profile_raw = (PROFILES_ROOT / profile / 'profile.toml').read_bytes()
                need(record['selftest'] == 'passed' and record['profile'] == profile and
                     record['declared_build_id'] == item['build_id'] and
                     record['profile_sha256'] == hashlib.sha256(profile_raw).hexdigest() and
                     record['image'] == {'basename': Path(image['path']).name, 'size': image['size'],
                                         'sha256': image['sha256']}, 'build record mismatch')
                records[profile] = (raw, pin)
        payload_bytes = sum(x['image']['size'] for x in verified.payloads) + (Path(args.bundle) / 'catalog.json').stat().st_size
        root_size, payload_size, total = capacity(root_bytes, payload_bytes)
        if args.max_bytes is not None:
            need(args.max_bytes >= total, 'output capacity too small')
        with tempfile.TemporaryDirectory(prefix='.gbos-universal-', dir=out.parent) as tmp:
            tmp = Path(tmp)
            stage = tmp / 'root'
            shutil.copytree(root, stage, symlinks=True)
            data_dir = tmp / 'payload'
            data_dir.mkdir()
            # Re-read the catalog under no-follow semantics, matching independently supplied hash.
            root_fd = os.open(args.bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                cat_fd = CAT.open_relative(root_fd, 'catalog.json')
                try:
                    cat_raw = CAT.verify_file(root_fd, 'catalog.json', cat_fd, CAT.MAX_CATALOG,
                                              args.catalog_sha256, collect=True)
                finally:
                    os.close(cat_fd)
            finally:
                os.close(root_fd)
            write_new(data_dir / 'catalog.json', cat_raw)
            for item in verified.payloads:
                image = item['image']
                parts = CAT.relative_parts(image['path'])
                parent = safe_dir(data_dir, '/'.join(parts[:-1])) if len(parts) > 1 else data_dir
                copy_checked(verified.descriptors[item['profile']], parent / parts[-1], image['sha256'], image['size'])
            with CAT.validate(str(data_dir), 'catalog.json', args.catalog_sha256):
                pass
            rg, dg, eg, disk_guid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            setup_root(stage, args.catalog_sha256, dg, records, profiles)
            if 'rog-ally' in profiles:
                rules = DETECT.load_rules(stage / 'opt/googlebook/profiles')
                need({p for p, _ in rules} == set(profiles) and all(rule for _, rule in rules),
                     'staged model rules are incomplete')
            for profile, (raw, _) in records.items():
                staged = stage / 'opt/googlebook/profiles' / profile / 'profile.toml'
                need(hashlib.sha256(staged.read_bytes()).hexdigest() ==
                     RECORD.schema(raw)['profile_sha256'], 'staged profile changed')
            prepare_guest(stage)
            esp, root_img, data_img = tmp / 'esp.img', tmp / 'root.img', tmp / 'data.img'
            for path, size in ((esp, ESP_SIZE), (root_img, root_size), (data_img, payload_size)):
                with path.open('xb') as f:
                    f.truncate(size)
            run([tools['mkfs.vfat'], '-F', '32', '-n', 'GBOS-BOOT', esp])
            for directory in ('EFI', 'EFI/BOOT', 'loader', 'loader/entries'):
                run([tools['mmd'], '-i', esp, '::/' + directory])
            entry = tmp / 'gbos-universal.conf'
            entry.write_bytes(make_entry(rg))
            loader = tmp / 'loader.conf'
            loader.write_text('default gbos-universal.conf\ntimeout 0\neditor no\nauto-entries no\nauto-firmware no\n')
            for source, dest, expected in ((Path(args.efi), 'EFI/BOOT/BOOTX64.EFI', args.efi_sha256),
                                 (xboot / 'vmlinuz-linux', 'vmlinuz-linux', args.kernel_sha256),
                                 (xboot / 'initramfs-linux.img', 'initramfs-linux.img', args.initrd_sha256),
                                 (entry, 'loader/entries/gbos-universal.conf', hash_file(entry)),
                                 (loader, 'loader/loader.conf', hash_file(loader))):
                run([tools['mcopy'], '-i', esp, source, '::/' + dest])
                actual = subprocess.run([tools['mtype'], '-i', str(esp), '::/' + dest], check=True,
                                        stdout=subprocess.PIPE).stdout
                need(hashlib.sha256(actual).hexdigest() == expected, 'ESP readback mismatch')
            for path, label, source in ((root_img, 'gbos-maint', stage), (data_img, 'gbos-payload', data_dir)):
                run([tools['mke2fs'], '-F', '-q', '-t', 'ext4', '-b', '4096', '-m', '0', '-L', label,
                     '-d', source, path, str(path.stat().st_size // 4096)])
            for item in verified.payloads:
                image = item['image']
                verify_debugfs(tools['debugfs'], data_img, image['path'], image['size'], image['sha256'])
            verify_debugfs(tools['debugfs'], data_img, 'catalog.json', len(cat_raw), args.catalog_sha256)
            root_members = ['etc/gbos-universal/catalog.sha256', 'etc/fstab',
                            'etc/systemd/system/gbos-universal-diagnostic.service',
                            'etc/systemd/system/gbos-universal-menu.service',
                            'etc/tmpfiles.d/root.conf', 'etc/.updated', 'etc/locale.conf', 'etc/vconsole.conf']
            root_members += ['opt/googlebook/install/stick/' + name for name in
                             ('catalog.py', 'detect-model.py', 'universal-diagnostic.py', 'shared-source.py',
                              'universal-menu.py', 'universal-controller.py')]
            root_members += ['opt/googlebook/install/' + name for name in ('gbos-update.py', 'gbos-fresh.py')]
            root_members += ['opt/googlebook/profiles/' + name + '/profile.toml'
                             for name in profiles]
            for profile in records:
                root_members += ['etc/gbos-universal/build-records/' + profile + suffix
                                 for suffix in ('.json', '.sha256')]
            for member in root_members:
                data = (stage / member).read_bytes()
                verify_debugfs(tools['debugfs'], root_img, member, len(data), hashlib.sha256(data).hexdigest())
            sectors = total // 512
            ef = MIB // 512
            el = ef + ESP_SIZE // 512 - 1
            rf = el + 1
            rl = rf + root_size // 512 - 1
            df = rl + 1
            dl = df + payload_size // 512 - 1
            entries = (STICK.partition_entry(EFI_TYPE, eg, ef, el, 'GBOS-BOOT') +
                       STICK.partition_entry(LINUX_TYPE, rg, rf, rl, 'gbos-maint') +
                       STICK.partition_entry(LINUX_TYPE, dg, df, dl, 'gbos-payload')).ljust(128 * 128, b'\0')
            primary = STICK.gpt_header(1, sectors - 1, 2, disk_guid.bytes_le, sectors, entries)
            backup = STICK.gpt_header(sectors - 1, 1, sectors - 33, disk_guid.bytes_le, sectors, entries)
            marker = str(out) + '.not-self-tested'
            marker_fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
            marker_identity = os.fstat(marker_fd).st_ino, os.fstat(marker_fd).st_dev
            with os.fdopen(marker_fd, 'wb') as f:
                f.write(b'NOT_SELF_TESTED: QEMU-only diagnostic; hardware boot and installation not validated.\n')
                f.flush()
                os.fsync(f.fileno())
            dir_fd = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            fd = None
            output_identity = None
            try:
                fd = os.open(out, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                output_identity = os.fstat(fd).st_ino, os.fstat(fd).st_dev
                os.ftruncate(fd, total)
                write_all(fd, STICK.protective_mbr(sectors), 0)
                write_all(fd, primary, 512)
                write_all(fd, entries, 1024)
                write_all(fd, entries, (sectors - 33) * 512)
                write_all(fd, backup, (sectors - 1) * 512)
                for part, first in ((esp, ef), (root_img, rf), (data_img, df)):
                    copy_part(part, fd, first * 512)
                os.fsync(fd)
                need(os.pread(fd, 512, 512) == primary and
                     os.pread(fd, len(entries), 1024) == entries and
                     os.pread(fd, len(entries), (sectors - 33) * 512) == entries and
                     os.pread(fd, 512, (sectors - 1) * 512) == backup, 'GPT readback mismatch')
                os.close(fd)
                fd = None
                STICK.LAYOUT.load(str(out))
            except BaseException:
                if fd is not None:
                    os.close(fd)
                for path, identity in ((out, output_identity), (Path(marker), marker_identity)):
                    try:
                        st = path.lstat()
                        if identity is not None and (st.st_ino, st.st_dev) == identity:
                            path.unlink()
                    except FileNotFoundError:
                        pass
                raise
    print(json.dumps({'result': 'DIAGNOSTIC_IMAGE_NOT_SELF_TESTED', 'bytes': total,
                      'root_bytes': root_size, 'payload_partition_bytes': payload_size,
                      'sha256': hash_file(out), 'mode': 'recorded' if records else 'legacy',
                      'pristine': 'NOT_PROVEN', 'write_allowed': False, 'hardware': 'NOT_VERIFIED'}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('root', 'xbootldr', 'efi', 'efi-sha256', 'kernel-sha256', 'initrd-sha256',
                'bundle', 'catalog-sha256', 'out'):
        parser.add_argument('--' + arg, required=True)
    parser.add_argument('--max-bytes', type=int)
    for profile in ('kano', 'hx370', 'rog-ally'):
        parser.add_argument('--' + profile + '-build-record')
        parser.add_argument('--' + profile + '-build-record-sha256')
    args = parser.parse_args()
    try:
        build(args)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('shared maintenance build refused:', str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
