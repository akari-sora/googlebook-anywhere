#!/usr/bin/env python3
"""Read-only catalog release and optional synthetic-image compatibility preview."""
import argparse
from contextlib import ExitStack, contextmanager
import importlib.util
import json
import os
import re
import secrets
from pathlib import Path
import stat
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


catalog = load('shared_catalog', HERE / 'catalog.py')
update = load('shared_update', HERE.parent / 'gbos-update.py')
detector = load('shared_detector', HERE / 'detect-model.py')
fresh = load('shared_fresh', HERE.parent / 'gbos-fresh.py')


def safe_open_relative(root_fd, path):
    """Linux O_PATH pins and classifies an inode before opening it for data."""
    if not hasattr(os, 'O_PATH') or not os.path.isdir('/proc/self/fd'):
        raise ValueError('Linux O_PATH and /proc/self/fd are required')
    parts = catalog.relative_parts(path)
    directory = os.dup(root_fd)
    probe = None
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = nxt
        probe = os.open(parts[-1], os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        st = os.fstat(probe)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError('input is not a regular file')
        fd = os.open(f'/proc/self/fd/{probe}', os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        if catalog.stable_signature(os.fstat(fd)) != catalog.stable_signature(st):
            os.close(fd)
            raise ValueError('input changed while opening')
        return fd
    finally:
        if probe is not None:
            os.close(probe)
        os.close(directory)


# Scoped to this imported module; the standalone catalog API is unchanged.
catalog.open_relative = safe_open_relative


class Reader:
    def __init__(self, fd, size):
        self.fd, self.size, self.path = fd, size, '<verified catalog image>'

    def pread(self, off, n):
        if type(off) is not int or type(n) is not int or off < 0 or n < 0 or off > self.size or n > self.size - off:
            raise ValueError('image read out of bounds')
        result = bytearray()
        while len(result) < n:
            part = os.pread(self.fd, n - len(result), off + len(result))
            if not part:
                raise ValueError('short image read')
            result.extend(part)
        return bytes(result)



def copy_exact(fd, out, size):
    """Copy at most the measured size, rejecting short or grown input."""
    remaining = size
    while remaining:
        data = os.read(fd, min(1024 * 1024, remaining))
        if not data:
            raise ValueError('target shortened while copying')
        out.write(data)
        remaining -= len(data)
    if os.read(fd, 1):
        raise ValueError('target grew while copying')



@contextmanager
def shared_release(root, catalog_path, expected, profile, tmp, bounded_fat=False):
    """Keep the verified catalog and selected release alive together; no CLI writer."""
    with catalog.validate(root, catalog_path, expected) as verified:
        matches = [p for p in verified.payloads if p['profile'] == profile]
        if len(matches) != 1:
            raise ValueError('profile absent')
        item = matches[0]
        fd = verified.descriptors[profile]
        binding = update.SharedBinding(fd, item, expected, verified.descriptors.values(), tmp,
                                       **({"bounded_fat": True} if bounded_fat else {}))
        try:
            rel = binding.release
            if str(rel.build) != item['build_id']:
                raise ValueError('catalog build differs from parsed release')
            entries = [name for name in rel.esp.ls('/loader/entries') if name.endswith('.conf')]
            if len(entries) != len(set(entries)) or sorted(entries) != sorted(item['boot_entries']):
                raise ValueError('catalog boot entry inventory differs from ESP')
            update.SharedBinding.verify(binding, rel)
            yield rel, binding
            if bounded_fat:
                rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    info = item['image']
                    catalog.verify_file(rootfd, info['path'], fd, catalog.MAX_IMAGE, info['sha256'], info['size'])
                finally:
                    os.close(rootfd)
        finally:
            binding.close()


def _preview_verified(root, verified, profile, target=None):
    """Preview against descriptors owned by the caller's validated catalog."""
    with tempfile.TemporaryDirectory(prefix='gbos-preview-') as tmp:
        items = [p for p in verified.payloads if p['profile'] == profile]
        if len(items) != 1:
            raise ValueError('profile absent')
        item = items[0]
        info = item['image']
        fd = verified.descriptors[profile]
        before = catalog.stable_signature(os.fstat(fd))
        reader = Reader(fd, info['size'])
        release = update.image_release_from_reader(reader, tmp, 'catalog preview', validate_layout=True)
        if str(release.build) != item['build_id']:
            raise ValueError('catalog build differs from parsed release')
        names = release.esp.ls('/loader/entries')
        entries = sorted(name for name in names if name.endswith('.conf'))
        if len(set(entries)) != len(entries) or entries != sorted(item['boot_entries']):
            raise ValueError('catalog boot entry inventory differs from ESP')
        compatible = None
        if target is not None:
            # mtools receives only a private regular-file snapshot, never target pathname.
            target = Path(target)
            if not target.is_absolute():
                raise ValueError('target path must be absolute')
            parent = os.open(str(target.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                tfd = safe_open_relative(parent, target.name)
                try:
                    st = os.fstat(tfd)
                    if not 0 < st.st_size <= 128 * 1024 ** 2:
                        raise ValueError('synthetic target size outside bounds')
                    dest = os.path.join(tmp, 'installed.img')
                    with open(dest, 'xb') as out:
                        copy_exact(tfd, out, st.st_size)
                    if catalog.stable_signature(os.fstat(tfd)) != catalog.stable_signature(st):
                        raise ValueError('target changed while copying')
                    disk = update.Disk(dest)
                    try:
                        # Bound installed LP parsing too; target is a synthetic snapshot.
                        bounded = Reader(disk.fd, disk.size)
                        layout = update.gpt_read(bounded)
                        update.validate_preview_layout(bounded, layout)
                        update.preview_resource_limits(bounded, layout)
                        inst = update.Installed(disk, tmp, preview=True)
                        update.check(inst, release, 'stick')
                        compatible = True
                    finally:
                        os.close(disk.fd)
                finally:
                    os.close(tfd)
            finally:
                os.close(parent)
        rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if catalog.stable_signature(os.fstat(fd)) != before:
                raise ValueError('payload changed during preview')
            os.lseek(fd, 0, os.SEEK_SET)
            catalog.verify_file(rootfd, info['path'], fd, catalog.MAX_IMAGE, info['sha256'], info['size'])
        finally:
            os.close(rootfd)
        return {'result': 'PREVIEW_OK', 'profile': profile, 'build_id': str(release.build),
                'release_model_title_token': release.model, 'boot_entries': entries,
                'compatible_synthetic_target': compatible, 'write_allowed': False,
                'hardware': 'NOT_VERIFIED', 'boot_policy': 'NOT_VERIFIED', 'avb': 'NOT_VERIFIED',
                'source_immutable': False}



def preview(root, catalog_path, expected, profile, target=None):
    """Return plain facts only; no reader or Release outlives catalog ownership."""
    with catalog.validate(root, catalog_path, expected) as verified:
        return _preview_verified(root, verified, profile, target)


def preview_all(root, catalog_path, expected):
    """Preview each profile under one validated catalog; no partial result."""
    with catalog.validate(root, catalog_path, expected) as verified:
        return [_preview_verified(root, verified, profile) for profile in sorted(verified.descriptors)]


def _observed_profile(dmi_root):
    rules = detector.load_rules(HERE.parent.parent / 'profiles')
    fields = set().union(*(rule.keys() for _, rule in rules))
    match = detector.detect(rules, detector.read_identity(Path(dmi_root), fields))
    if (match['recognition'] != 'matched' or len(match['matches']) != 1
            or match['potential_matches'] or match['unconfigured']):
        raise ValueError('machine profile is not uniquely and completely recognized')
    return match['matches'][0]


def plan_observed_image(root, catalog_path, expected, target, dmi_root=Path('/sys/class/dmi/id')):
    """Plan against an observed regular image; never authorizes or applies writes."""
    profile = _observed_profile(dmi_root)
    target = Path(target)
    if not target.is_absolute():
        raise ValueError('target path must be absolute')
    parent = os.open(str(target.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        tfd = safe_open_relative(parent, target.name)
        try:
            before = catalog.stable_signature(os.fstat(tfd))
            if before[2] <= 0:
                raise ValueError('empty target')
            disk = Reader(tfd, before[2])
            disk.path = '<observed regular target>'
            with tempfile.TemporaryDirectory(prefix='gbos-observed-plan-') as tmp:
                with shared_release(root, catalog_path, expected, profile, tmp, bounded_fat=True) as (rel, binding):
                    update.SharedBinding.verify(binding, rel, disk)
                    snapshot = update.PreviewMetadataSnapshot(disk, tmp)
                    inst = update.Installed(disk, tmp, preview=True, snapshot=snapshot)
                    if inst.resume:
                        raise ValueError('target has an interrupted update')
                    chk = update.check(inst, rel, 'nvme')
                    bf = update.boot_files(inst, rel, 'nvme', tmp, preview=True)
                    update.SharedBinding.verify(binding, rel, disk)
                    again = safe_open_relative(parent, target.name)
                    try:
                        if (catalog.stable_signature(os.fstat(tfd)) != before or
                                catalog.stable_signature(os.fstat(again)) != before):
                            raise ValueError('target changed during planning')
                    finally:
                        os.close(again)
                    return {'result': 'PLAN_OBSERVED', 'profile': profile,
                            'build_id': str(rel.build), 'current_slot': inst.current,
                            'target_slot': inst.target, 'slot_sizes': chk['sizes'],
                            'boot_space': {k: bf[k] for k in ('fits', 'new_first', 'free', 'reclaim', 'need', 'cluster')},
                            'previous_entry_available': bool(bf['prev']),
                            'retention_intent': 'PRESERVE_USERDATA_AND_CURRENT_SLOT', 'write_allowed': False,
                            'limitations': ['REGULAR_IMAGE_ONLY', 'NOT_ATOMIC_SNAPSHOT',
                                            'IN_USE_NOT_VERIFIED', 'HARDWARE_NOT_VERIFIED',
                                            'BOOT_POLICY_NOT_VERIFIED', 'NO_WRITE_AUTHORIZATION']}
        finally:
            os.close(tfd)
    finally:
        os.close(parent)


@contextmanager
def _synthetic_image_operation(root, catalog_path, expected, target, dmi_root):
    """Own one synthetic update's source, scratch and observed target descriptors."""
    profile = _observed_profile(dmi_root)
    target = Path(target)
    if not target.is_absolute():
        raise ValueError('target path must be absolute')
    parent = os.open(str(target.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        observed = safe_open_relative(parent, target.name)
        try:
            before = catalog.stable_signature(os.fstat(observed))
            if not 0 < before[2] <= 128 * update.MIB:
                raise ValueError('synthetic target size outside bounds')
            scratch = os.environ.get('TMPDIR') or '/tmp'
            if not os.path.isabs(scratch):
                raise ValueError('scratch directory must be absolute')
            scratchfd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                with _pinned_scratch(scratchfd) as tmp:
                    with shared_release(root, catalog_path, expected, profile, tmp, bounded_fat=True) as (rel, binding):
                        # Reject aliases before opening a target for writes.
                        update.SharedBinding.verify(binding, rel, Reader(observed, before[2]))
                        named = f'/proc/{os.getpid()}/fd/{parent}/{target.name}'
                        disk = update.Disk(named, write=True, nbytes=before[2])
                        try:
                            if disk.blk or not stat.S_ISREG(os.fstat(disk.fd).st_mode):
                                raise ValueError('synthetic target must be a regular file')
                            def check_target():
                                again = safe_open_relative(parent, target.name)
                                try:
                                    if any(catalog.stable_signature(os.fstat(fd)) != before
                                           for fd in (observed, disk.fd, again)):
                                        raise ValueError('target changed before update')
                                finally:
                                    os.close(again)
                            check_target()
                            update.SharedBinding.verify(binding, rel, disk)
                            yield disk, rel, binding, tmp, profile, parent, target.name, check_target
                        finally:
                            os.close(disk.fd)
            finally:
                os.close(scratchfd)
        finally:
            os.close(observed)
    finally:
        os.close(parent)


def apply_observed_image(root, catalog_path, expected, target, dmi_root=Path('/sys/class/dmi/id')):
    """Internal file-only integration API; no CLI or physical write authorization.

    Unlike the read-only plan API, this updates the explicitly supplied <=128 MiB
    synthetic regular image. Callers supply independently trusted catalog facts.
    The operation is serial: updater process flags are restored on every exit.
    """
    flags = [(flag, len(flag)) for flag in (update.WRITING, update.WINDOW, update.STICK)]
    try:
        with _synthetic_image_operation(root, catalog_path, expected, target, dmi_root) as operation:
            disk, rel, binding, tmp, profile, parent, name, check_target = operation
            update.apply(disk, rel, 'nvme', False, None, tmp, shared_binding=binding, pre_write_check=check_target)
            again = safe_open_relative(parent, name)
            try:
                held = os.fstat(disk.fd)
                named = os.fstat(again)
                if (held.st_dev, held.st_ino, held.st_size) != (named.st_dev, named.st_ino, named.st_size):
                    raise ValueError('target path changed after update; opened image may already be modified')
            finally:
                os.close(again)
            result = {'result': 'APPLIED_SYNTHETIC_IMAGE', 'profile': profile, 'build_id': str(rel.build),
                      'physical_write_allowed': False, 'hardware': 'NOT_VERIFIED',
                      'retention_intent': 'PRESERVE_USERDATA_AND_CURRENT_SLOT'}
        return result
    except Exception as exc:
        if len(update.WRITING) > flags[0][1] and not isinstance(exc, update.Failed):
            raise update.Failed(f'synthetic update failed after entering write phase: {exc}') from exc
        raise
    finally:
        for flag, count in flags:
            del flag[count:]



def rollback_observed_image(root, catalog_path, expected, target, dmi_root=Path('/sys/class/dmi/id')):
    """Switch slots on a synthetic regular image; no block-device interface."""
    flags = [(flag, len(flag)) for flag in (update.WRITING, update.WINDOW, update.STICK)]
    try:
        with _synthetic_image_operation(root, catalog_path, expected, target, dmi_root) as operation:
            disk, rel, binding, tmp, profile, parent, name, check_target = operation
            def pre_write_check():
                update.SharedBinding.verify(binding, rel, disk)
                check_target()
            update.rollback(disk, tmp, stick_boot=None, allow_build_change=False, pre_write_check=pre_write_check)
            again = safe_open_relative(parent, name)
            try:
                held, named = os.fstat(disk.fd), os.fstat(again)
                if (held.st_dev, held.st_ino, held.st_size) != (named.st_dev, named.st_ino, named.st_size):
                    raise ValueError('target path changed after rollback; opened image may already be modified')
            finally:
                os.close(again)
            result = {'result': 'ROLLED_BACK_SYNTHETIC_IMAGE', 'profile': profile,
                      'build_id': str(rel.build), 'physical_write_allowed': False,
                      'hardware': 'NOT_VERIFIED'}
        return result
    except Exception as exc:
        if len(update.WRITING) > flags[0][1] and not isinstance(exc, update.Failed):
            raise update.Failed(f'synthetic rollback failed after entering write phase: {exc}') from exc
        raise
    finally:
        for flag, count in flags:
            del flag[count:]


_DEVNO = re.compile(r'[0-9]+:[0-9]+\Z')


def _sys_device(number):
    if not _DEVNO.fullmatch(number):
        raise ValueError('invalid block device number')
    link = Path('/sys/dev/block') / number
    path = link.resolve(strict=True)
    if not path.is_dir() or not str(path).startswith('/sys/devices/'):
        raise ValueError('unresolved block topology')
    if (path / 'dev').read_text().strip() != number:
        raise ValueError('block topology device mismatch')
    return path


def _devno(device):
    return f'{os.major(device)}:{os.minor(device)}'


def _ancestors(number, seen=None):
    """Resolve block parents/slaves and loop backing filesystem; fail on unknown."""
    if seen is None:
        seen = set()
    if number in seen or len(seen) >= 256:
        raise ValueError('cyclic or excessive block topology')
    seen.add(number)
    node = _sys_device(number)
    result = {number}
    if (node / 'partition').exists():
        partition = (node / 'partition').read_text().strip()
        if not partition.isdecimal() or int(partition) <= 0:
            raise ValueError('invalid partition number')
        parent = node.parent
        parent_no = (parent / 'dev').read_text().strip()
        if _sys_device(parent_no) != parent:
            raise ValueError('invalid partition parent')
        result |= _ancestors(parent_no, seen)
    else:
        slaves = node / 'slaves'
        if not slaves.is_dir():
            raise ValueError('unknown block dependencies')
        links = list(slaves.iterdir())
        if len(links) > 256:
            raise ValueError('excessive block dependencies')
        for link in links:
            dep = link.resolve(strict=True)
            dep_no = (dep / 'dev').read_text().strip()
            if _sys_device(dep_no) != dep:
                raise ValueError('invalid block dependency')
            result |= _ancestors(dep_no, seen.copy())
        backing = node / 'loop/backing_file'
        if (node / 'loop').exists() and not backing.is_file():
            raise ValueError('unknown loop backing')
        if backing.exists():
            raw = backing.read_text().strip()
            if not raw or '\\' in raw or raw.endswith(' (deleted)'):
                raise ValueError('unknown loop backing')
            path = Path(raw if raw.startswith('/') else '/' + raw)
            st = path.stat()
            if not stat.S_ISREG(st.st_mode):
                raise ValueError('invalid loop backing')
            result |= _ancestors(_devno(st.st_dev), seen.copy())
    return result




def _read_bounded(path, limit):
    with open(path, 'r', encoding='utf-8') as stream:
        text = stream.read(limit + 1)
    if not text or len(text) > limit:
        raise ValueError('missing or excessive topology data')
    return text

def _filesystem_ancestors(number):
    """Known memory-only filesystems have no block parent (swap checked separately)."""
    matches = []
    for line in _read_bounded('/proc/self/mountinfo', 16 * 1024 * 1024).splitlines():
        fields = line.split()
        if len(fields) < 10 or '-' not in fields:
            raise ValueError('malformed mount state')
        if fields[2] == number:
            pos = fields.index('-')
            if pos + 1 >= len(fields):
                raise ValueError('malformed mount state')
            matches.append(fields[pos + 1])
    if matches and all(kind in ('tmpfs', 'ramfs') for kind in matches):
        return set()
    return _ancestors(number)

def _target_observation(disk, path, source_fds):
    """One fail-closed observation, not a global exclusivity guarantee."""
    st = os.fstat(disk.fd)
    if not stat.S_ISBLK(st.st_mode):
        raise ValueError('target must be a block device')
    number = _devno(st.st_rdev)
    node = _sys_device(number)
    if (node / 'partition').exists():
        raise ValueError('target must be a whole device')
    if not (node / 'slaves').is_dir() or any((node / 'slaves').iterdir()):
        raise ValueError('target has unsupported or unknown dependencies')
    if (int((node / 'size').read_text().strip()) * 512 != disk.size or disk.size <= 0
            or os.lseek(disk.fd, 0, os.SEEK_END) != disk.size):
        raise ValueError('target size changed')
    named = os.stat(path)
    if not stat.S_ISBLK(named.st_mode) or named.st_rdev != st.st_rdev:
        raise ValueError('target path changed')
    # Resolve any loop backing; it must exist and have known underlying topology.
    _ancestors(number)
    backing = node / 'loop/backing_file'
    backing_sig = None
    if backing.exists():
        text = backing.read_text().strip()
        bp = Path(text if text.startswith('/') else '/' + text)
        bs = bp.stat()
        backing_sig = (bs.st_dev, bs.st_ino)
    related = {number}
    classdir = Path('/sys/class/block')
    entries = list(classdir.iterdir())
    if not entries or len(entries) > 4096:
        raise ValueError('unknown block inventory')
    nodes = [node]
    for entry in entries:
        resolved = entry.resolve(strict=True)
        if (resolved / 'partition').exists() and resolved.parent == node:
            partno = (resolved / 'dev').read_text().strip()
            if (_sys_device(partno) != resolved or
                    not re.fullmatch(r'[1-9][0-9]*', (resolved / 'partition').read_text().strip())):
                raise ValueError('invalid target partition')
            related.add(partno)
            nodes.append(resolved)
    for target_node in nodes:
        holders = target_node / 'holders'
        if not holders.is_dir() or any(holders.iterdir()):
            raise ValueError('target has holders or unknown holder state')
    source_sig = []
    for fd in source_fds:
        source = os.fstat(fd)
        if not (stat.S_ISREG(source.st_mode) or stat.S_ISDIR(source.st_mode)):
            raise ValueError('invalid source backing')
        if backing_sig == (source.st_dev, source.st_ino):
            raise ValueError('source aliases loop backing file')
        ancestry = _filesystem_ancestors(_devno(source.st_dev))
        if ancestry & related:
            raise ValueError('source is on target')
        source_sig.append((source.st_dev, source.st_ino, tuple(sorted(ancestry))))
    mounts = []
    for mountfile in ('/proc/self/mountinfo', '/proc/1/mountinfo'):
        data = _read_bounded(mountfile, 16 * 1024 * 1024)
        for line in data.splitlines():
            fields = line.split()
            if len(fields) < 10 or '-' not in fields or not _DEVNO.fullmatch(fields[2]):
                raise ValueError('malformed mount state')
            if fields[2] in related:
                raise ValueError('target is mounted')
        mounts.append(data)
    swaps = _read_bounded('/proc/swaps', 1024 * 1024)
    lines = swaps.splitlines()
    if not lines or not lines[0].startswith('Filename'):
        raise ValueError('unknown swap state')
    for line in lines[1:]:
        fields = line.split()
        if len(fields) != 5 or '\\' in fields[0]:
            raise ValueError('unknown swap entry')
        sw = os.stat(fields[0])
        dev = sw.st_rdev if stat.S_ISBLK(sw.st_mode) else sw.st_dev
        if _ancestors(_devno(dev)) & related:
            raise ValueError('target is used for swap')
    node_st = node.stat()
    return (number, str(node), node_st.st_dev, node_st.st_ino, named.st_dev, named.st_ino,
            st.st_dev, st.st_ino, disk.size, tuple(sorted(related)), tuple(source_sig), backing_sig, tuple(mounts), swaps)


@contextmanager
def _pinned_scratch(parent_fd):
    """Create and use a private directory under the already observed directory fd."""
    for _ in range(10):
        name = 'gbos-block-plan-' + secrets.token_hex(16)
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            break
        except FileExistsError:
            continue
    else:
        raise ValueError('cannot allocate private scratch directory')
    fd = None
    trusted = False
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        st = os.fstat(fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        path = f'/proc/{os.getpid()}/fd/{fd}'
        proc = os.stat(path)
        if (not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o700
                or st.st_uid != os.geteuid() or st.st_dev != os.fstat(parent_fd).st_dev
                or (st.st_dev, st.st_ino) != (named.st_dev, named.st_ino)
                or (st.st_dev, st.st_ino) != (proc.st_dev, proc.st_ino)):
            raise ValueError('cannot bind private scratch directory')
        trusted = True
        yield path
    finally:
        if fd is not None:
            try:
                if trusted:
                    for entry in os.scandir(fd):
                        os.unlink(entry.name, dir_fd=fd)
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                st = os.fstat(fd)
                if (named.st_dev, named.st_ino) != (st.st_dev, st.st_ino):
                    raise ValueError('private scratch directory changed')
                os.rmdir(name, dir_fd=parent_fd)
            finally:
                os.close(fd)
        else:
            os.rmdir(name, dir_fd=parent_fd)


def plan_observed_block(root, catalog_path, expected, target, dmi_root=Path('/sys/class/dmi/id')):
    """Read-only local observation of a whole block target; never authorizes writes."""
    profile = _observed_profile(dmi_root)
    if not Path(target).is_absolute():
        raise ValueError('target path must be absolute')
    disk = update.Disk(str(target), write=False)
    try:
        if not disk.blk:
            raise ValueError('target must be a block device')
        rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            catfd = safe_open_relative(rootfd, catalog_path)
            try:
                # Do not call tempfile.gettempdir(): its first lookup probes by writing.
                scratch = os.environ.get('TMPDIR') or '/tmp'
                if not os.path.isabs(scratch):
                    raise ValueError('scratch directory must be absolute')
                scratchfd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    with catalog.validate(root, catalog_path, expected) as verified:
                        root_before = catalog.stable_signature(os.fstat(rootfd))
                        if catalog.stable_signature(os.stat(root)) != root_before:
                            raise ValueError('catalog root changed')
                        catalog.verify_file(rootfd, catalog_path, catfd, catalog.MAX_CATALOG, expected)
                        items = [item for item in verified.payloads if item['profile'] == profile]
                        if len(items) != 1:
                            raise ValueError('profile absent')
                        sources = (rootfd, catfd, scratchfd, *verified.descriptors.values())
                        before = _target_observation(disk, str(target), sources)
                        # No scratch path is created until all backing devices are checked.
                        with _pinned_scratch(scratchfd) as tmp:
                            item = items[0]
                            fd = verified.descriptors[profile]
                            binding = update.SharedBinding(fd, item, expected, verified.descriptors.values(), tmp, bounded_fat=True, preview_tmp=tmp)
                            try:
                                rel = binding.release
                                if str(rel.build) != item['build_id']:
                                    raise ValueError('catalog build differs from parsed release')
                                entries = [n for n in rel.esp.ls('/loader/entries') if n.endswith('.conf')]
                                if len(entries) != len(set(entries)) or sorted(entries) != sorted(item['boot_entries']):
                                    raise ValueError('catalog boot entry inventory differs from ESP')
                                update.SharedBinding.verify(binding, rel, disk)
                                snapshot = update.PreviewMetadataSnapshot(disk, tmp)
                                inst = update.Installed(disk, tmp, preview=True, snapshot=snapshot, preview_tmp=tmp)
                                if inst.resume:
                                    raise ValueError('target has an interrupted update')
                                chk = update.check(inst, rel, 'nvme')
                                bf = update.boot_files(inst, rel, 'nvme', tmp, preview=True, preview_tmp=tmp)
                                update.SharedBinding.verify(binding, rel, disk)
                                if _target_observation(disk, str(target), sources) != before:
                                    raise ValueError('target or topology changed during planning')
                                info = item['image']
                                os.lseek(fd, 0, os.SEEK_SET)
                                catalog.verify_file(rootfd, info['path'], fd, catalog.MAX_IMAGE, info['sha256'], info['size'])
                                os.lseek(catfd, 0, os.SEEK_SET)
                                catalog.verify_file(rootfd, catalog_path, catfd, catalog.MAX_CATALOG, expected)
                                if catalog.stable_signature(os.stat(root)) != root_before:
                                    raise ValueError('catalog root changed')
                                return {'result': 'PLAN_OBSERVED', 'profile': profile,
                                        'build_id': str(rel.build), 'current_slot': inst.current,
                                        'target_slot': inst.target, 'slot_sizes': chk['sizes'],
                                        'boot_space': {k: bf[k] for k in ('fits', 'new_first', 'free', 'reclaim', 'need', 'cluster')},
                                        'previous_entry_available': bool(bf['prev']),
                                        'retention_intent': 'PRESERVE_USERDATA_AND_CURRENT_SLOT', 'write_allowed': False,
                                        'limitations': ['NOT_ATOMIC_SNAPSHOT', 'NOT_GLOBALLY_EXCLUSIVE',
                                                        'MOUNT_CHECK_SELF_AND_PID1_ONLY', 'HARDWARE_NOT_VERIFIED',
                                                        'BOOT_POLICY_NOT_VERIFIED', 'NO_WRITE_AUTHORIZATION']}
                            finally:
                                binding.close()
                finally:
                    os.close(scratchfd)
            finally:
                os.close(catfd)
        finally:
            os.close(rootfd)
    finally:
        os.close(disk.fd)


def _block_facts(disk, target):
    """Observed consistency facts, never proof of caller or hardware authority."""
    import fcntl
    import hashlib
    import struct
    st = os.fstat(disk.fd)
    if not stat.S_ISBLK(st.st_mode):
        raise ValueError('target must be a block device')
    number = _devno(st.st_rdev)
    node = _sys_device(number)
    if (node / 'partition').exists():
        raise ValueError('target must be a whole device')
    sector = bytearray(4)
    fcntl.ioctl(disk.fd, 0x1268, sector, True)  # BLKSSZGET, Linux UAPI fs.h
    logical = struct.unpack('=I', sector)[0]
    if logical != 512 or (node / 'queue/logical_block_size').read_text().strip() != '512':
        raise ValueError('shared block update requires 512-byte logical sectors')

    def field(path):
        value = _read_bounded(path, 512).strip()
        if not value or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError('missing or invalid stable device identity')
        return value

    if re.fullmatch('loop[0-9]+', node.name) and node == Path('/sys/devices/virtual/block') / node.name:
        backing = field(node / 'loop/backing_file')
        if '\\' in backing or backing.endswith(' (deleted)'):
            raise ValueError('unknown loop backing identity')
        backing = Path(backing if backing.startswith('/') else '/' + backing)
        source = backing.stat()
        status = bytearray(232)
        fcntl.ioctl(disk.fd, 0x4C05, status, True)  # LOOP_GET_STATUS64
        dev, ino, _, offset, limit = struct.unpack_from('=QQQQQ', status)
        loop_number, _, _, flags = struct.unpack_from('=IIII', status, 40)
        major, minor = os.major(source.st_dev), os.minor(source.st_dev)
        encoded = (major << 8) | (minor & 255) | ((minor & ~255) << 12)
        if (not stat.S_ISREG(source.st_mode) or not 0 <= major < 4096 or not 0 <= minor < 2 ** 20
                or (dev, ino) != (encoded, source.st_ino) or offset or limit or flags & 1
                or loop_number != int(node.name[4:]) or source.st_size != disk.size):
            raise ValueError('loop kernel backing identity differs')
        kind = 'loop-backing'
        identity = {'kind': kind, 'filesystem': _devno(source.st_dev), 'inode': source.st_ino}
    elif re.fullmatch('nvme[0-9]+n[0-9]+', node.name):
        controller = (node / 'device').resolve(strict=True)
        if (controller != node.parent or not re.fullmatch('nvme[0-9]+', controller.name)
                or (controller / 'subsystem').resolve(strict=True) != Path('/sys/class/nvme')):
            raise ValueError('unsupported NVMe controller topology')
        wwid, serial, model, nsid = (field(p) for p in
                                  (node / 'wwid', controller / 'serial', controller / 'model', node / 'nsid'))
        if (not wwid.startswith(('eui.', 'uuid.', 'nvme.'))
                or wwid.startswith(('eui.', 'uuid.')) and not any(c in '123456789abcdefABCDEF' for c in wwid.split('.', 1)[1])
                or not nsid.isdecimal()
                or not 0 < int(nsid) < 2 ** 32):
            raise ValueError('unsupported NVMe namespace identity')
        inventory = list(Path('/sys/class/block').glob('nvme*'))
        if not inventory or len(inventory) > 4096:
            raise ValueError('unknown NVMe inventory')
        for other in inventory:
            other = other.resolve(strict=True)
            if other == node or (other / 'partition').exists():
                continue
            if field(other / 'wwid') == wwid:
                raise ValueError('duplicate NVMe namespace identity')
        kind = 'nvme-namespace'
        identity = {'kind': kind, 'wwid': wwid, 'serial': serial, 'model': model, 'nsid': int(nsid)}
    else:
        raise ValueError('shared block update requires a known NVMe namespace or loop backing')
    again = os.fstat(disk.fd)
    named = os.stat(target)
    if ((st.st_dev, st.st_ino, st.st_rdev) != (again.st_dev, again.st_ino, again.st_rdev)
            or not stat.S_ISBLK(named.st_mode) or named.st_rdev != st.st_rdev
            or os.lseek(disk.fd, 0, os.SEEK_END) != disk.size
            or int((node / 'size').read_text().strip()) * 512 != disk.size):
        raise ValueError('target changed during identity observation')
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'rdev': number, 'syspath': str(node), 'nbytes': disk.size,
            'logical_sector_size': logical, 'identity_kind': kind, 'device_identity': digest}


def observe_block_target(target):
    """Read-only target facts for an external controller; does not confirm writes."""
    if not Path(target).is_absolute():
        raise ValueError('target path must be absolute')
    disk = update.Disk(str(target))
    try:
        return _block_facts(disk, str(target))
    finally:
        os.close(disk.fd)


@contextmanager
def _observed_block_resources(root, catalog_path, expected, target, expected_target, dmi_root):
    """Hold catalog, source, scratch and exclusive target without parsing its layout."""
    if any(name.startswith('GBOS_UPDATE_TEST') for name in os.environ):
        raise ValueError('test environment is not admitted by the block update API')
    keys = {'rdev', 'syspath', 'nbytes', 'logical_sector_size', 'identity_kind', 'device_identity'}
    if (type(expected_target) is not dict or set(expected_target) != keys
            or not isinstance(expected_target['rdev'], str) or not _DEVNO.fullmatch(expected_target['rdev'])
            or not isinstance(expected_target['syspath'], str) or not expected_target['syspath'].startswith('/sys/devices/')
            or type(expected_target['nbytes']) is not int or expected_target['nbytes'] <= 0
            or type(expected_target['logical_sector_size']) is not int or expected_target['logical_sector_size'] != 512
            or expected_target['identity_kind'] not in ('nvme-namespace', 'loop-backing')
            or not isinstance(expected_target['device_identity'], str)
            or not re.fullmatch('[0-9a-f]{64}', expected_target['device_identity'])):
        raise ValueError('complete independently observed target facts are required')
    expected_target = dict(expected_target)
    profile = _observed_profile(dmi_root)
    if not Path(target).is_absolute():
        raise ValueError('target path must be absolute')
    target = str(target)
    with ExitStack() as stack:
        observed = update.Disk(target)
        stack.callback(os.close, observed.fd)
        if _block_facts(observed, target) != expected_target:
            raise ValueError('target differs from expected device identity')
        rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, rootfd)
        root_before = catalog.stable_signature(os.fstat(rootfd))
        catfd = safe_open_relative(rootfd, catalog_path)
        stack.callback(os.close, catfd)
        scratch = os.environ.get('TMPDIR') or '/tmp'
        if not os.path.isabs(scratch):
            raise ValueError('scratch directory must be absolute')
        scratchfd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, scratchfd)
        verified = stack.enter_context(catalog.validate(root, catalog_path, expected))
        catalog.verify_file(rootfd, catalog_path, catfd, catalog.MAX_CATALOG, expected)
        if catalog.stable_signature(os.stat(root)) != root_before:
            raise ValueError('catalog root changed')
        items = [item for item in verified.payloads if item['profile'] == profile]
        if len(items) != 1:
            raise ValueError('profile absent')
        sources = (rootfd, catfd, scratchfd, *verified.descriptors.values())
        before = _target_observation(observed, target, sources)
        disk = update.Disk(target, write=True, rdev=expected_target['rdev'],
                           syspath=expected_target['syspath'], nbytes=expected_target['nbytes'])
        stack.callback(os.close, disk.fd)

        def check_target():
            if (_block_facts(disk, target) != expected_target
                    or _target_observation(disk, target, sources) != before):
                raise ValueError('target identity or topology changed during block update')
            os.lseek(catfd, 0, os.SEEK_SET)
            catalog.verify_file(rootfd, catalog_path, catfd, catalog.MAX_CATALOG, expected)
            if (catalog.stable_signature(os.fstat(rootfd)) != root_before
                    or catalog.stable_signature(os.stat(root)) != root_before):
                raise ValueError('catalog root changed')

        check_target()
        tmp = stack.enter_context(_pinned_scratch(scratchfd))
        item = items[0]
        binding = update.SharedBinding(verified.descriptors[profile], item, expected,
                                       verified.descriptors.values(), tmp, bounded_fat=True, preview_tmp=tmp)
        stack.callback(binding.close)
        rel = binding.release
        entries = [name for name in rel.esp.ls('/loader/entries') if name.endswith('.conf')]
        if (str(rel.build) != item['build_id'] or len(entries) != len(set(entries))
                or sorted(entries) != sorted(item['boot_entries'])):
            raise ValueError('catalog release facts differ from parsed source')
        yield disk, rel, binding, tmp, profile, check_target
        check_target()


@contextmanager
def _observed_block_operation(root, catalog_path, expected, target, expected_target, dmi_root):
    """Add installed-target snapshots only for the keep-data updater."""
    with _observed_block_resources(root, catalog_path, expected, target, expected_target, dmi_root) as values:
        disk, rel, binding, tmp, profile, check_target = values
        operation = update.SharedBlockOperation(disk, rel, binding, tmp,
                                                expected_target['device_identity'], check_target)
        try:
            yield disk, rel, binding, tmp, profile, operation
        finally:
            operation.close()


def apply_observed_block(root, catalog_path, expected, target, *, expected_target,
                         dmi_root=Path('/sys/class/dmi/id')):
    """Internal keep-data backend, no CLI or self-issued physical authorization.

    Caller provides independently trusted catalog/profile facts and externally
    confirmed target facts. Observation is not authorization. This does not admit
    arbitrary public media, fresh installation, or full backup restoration.
    """
    flags = [(flag, len(flag)) for flag in (update.WRITING, update.WINDOW, update.STICK)]
    try:
        with _observed_block_operation(root, catalog_path, expected, target, expected_target, dmi_root) as values:
            disk, rel, binding, tmp, profile, operation = values
            update.apply(disk, rel, 'nvme', False, None, tmp, shared_binding=binding, block_operation=operation)
            return {'result': 'APPLIED_OBSERVED_BLOCK', 'profile': profile, 'build_id': str(rel.build),
                    'device_identity': expected_target['device_identity'], 'hardware': 'NOT_VERIFIED',
                    'retention_intent': 'PRESERVE_USERDATA_AND_CURRENT_SLOT',
                    'limitations': ['NOT_GLOBALLY_EXCLUSIVE', 'MOUNT_CHECK_SELF_AND_PID1_ONLY',
                                    'CALLER_TRUST_AND_CONFIRMATION_REQUIRED', 'BOOT_POLICY_NOT_VERIFIED']}
    except Exception as exc:
        if len(update.WRITING) > flags[0][1] and not isinstance(exc, update.Failed):
            raise update.Failed(f'block update failed after entering write phase; target may be modified: {exc}') from exc
        raise
    finally:
        for flag, count in flags:
            del flag[count:]


@contextmanager
def _fresh_flags():
    """Classify partial fresh writes and restore serial-operation flags on exit."""
    if any(name.startswith('GBOS_UPDATE_TEST') for name in os.environ):
        raise ValueError('test environment is not admitted by the fresh install API')
    flags = [(flag, len(flag)) for flag in (update.WRITING, update.WINDOW, update.STICK)]
    try:
        yield
    except Exception as exc:
        if len(update.WRITING) > flags[0][1] and not isinstance(exc, update.Failed):
            raise update.Failed(f'fresh install failed after entering write phase; target may be modified: {exc}') from exc
        raise
    finally:
        for flag, count in flags:
            del flag[count:]


def install_observed_image(root, catalog_path, expected, target, dmi_root=Path('/sys/class/dmi/id')):
    """Fresh install on an explicit <=128 MiB synthetic regular image, no CLI.

    Caller must independently trust the source as a pristine build of the chosen
    profile; the supplied catalog digest alone does not establish that trust.
    """
    with _fresh_flags():
        with _synthetic_image_operation(root, catalog_path, expected, target, dmi_root) as values:
            disk, rel, binding, tmp, profile, parent, name, check_target = values
            def after_write_check():
                again = safe_open_relative(parent, name)
                try:
                    held, named = os.fstat(disk.fd), os.fstat(again)
                    if ((held.st_dev, held.st_ino, held.st_size) != (named.st_dev, named.st_ino, named.st_size)
                            or held.st_size != disk.size):
                        raise ValueError('target path changed after fresh install; opened image may already be modified')
                finally:
                    os.close(again)
            facts = fresh.install(update, disk, binding, tmp, check_target,
                                  after_write_check=after_write_check)
            after_write_check()
            result = dict(facts, result='INSTALLED_SYNTHETIC_IMAGE', profile=profile,
                          build_id=str(rel.build), physical_write_allowed=False, hardware='NOT_VERIFIED')
        return result


def install_observed_block(root, catalog_path, expected, target, *, expected_target,
                           dmi_root=Path('/sys/class/dmi/id')):
    """Internal fresh backend; caller owns pristine trust and destructive admission.

    Observed facts are not authorization. No public media writer or controlled
    maintenance confirmation and backup workflow is connected to this backend.
    """
    with _fresh_flags():
        with _observed_block_resources(root, catalog_path, expected, target, expected_target, dmi_root) as values:
            disk, rel, binding, tmp, profile, check_target = values
            facts = fresh.install(update, disk, binding, tmp, check_target,
                                  after_write_check=check_target)
            result = dict(facts, result='INSTALLED_OBSERVED_BLOCK', profile=profile,
                          build_id=str(rel.build), device_identity=expected_target['device_identity'],
                          hardware='NOT_VERIFIED')
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True)
    p.add_argument('--catalog', required=True)
    p.add_argument('--sha256', required=True)
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument('--profile', choices=sorted(catalog.PROFILES))
    selection.add_argument('--all-profiles', action='store_true')
    p.add_argument('--synthetic-installed-image', help='optional regular file, max 128 MiB; no device')
    a = p.parse_args()
    if a.all_profiles and a.synthetic_installed_image:
        p.error('--synthetic-installed-image requires --profile')
    try:
        result = preview_all(a.root, a.catalog, a.sha256) if a.all_profiles else preview(
            a.root, a.catalog, a.sha256, a.profile, a.synthetic_installed_image)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RuntimeError, update.Refuse) as exc:
        print(json.dumps({'result': 'REFUSED', 'reason': str(exc), 'write_allowed': False}, sort_keys=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
