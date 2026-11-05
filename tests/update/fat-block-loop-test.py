#!/usr/bin/env python3
"""Opt-in Linux root test of held exclusive block fd + actual mtools operations.

Creates/detaches only its own disposable loop. No device argument is accepted.
GBOS_TEST_LOOP_ROOT must name an existing root-owned private scratch directory.
GBOS_TEST_UPDATE_SHA256 pins the updater bytes actually compiled by this test.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
ACTIVE = os.environ.get('GBOS_TEST_BLOCK_LOOP') == '1' and os.geteuid() == 0
ENGINE = ROOT / 'install/gbos-update.py'
raw = ENGINE.read_bytes()
if ACTIVE and hashlib.sha256(raw).hexdigest() != os.environ.get('GBOS_TEST_UPDATE_SHA256'):
    raise SystemExit('root loop test requires the independently pinned updater SHA256')
u = types.ModuleType('block_fd_update')
u.__file__ = str(ENGINE)
exec(compile(raw, str(ENGINE), 'exec'), u.__dict__)
SIZE, OFFSET = 8 * 1024 * 1024, 1024 * 1024


def command(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=15, env=dict(os.environ, MTOOLS_SKIP_CHECK='1')).stdout


def loop_rows(*devices):
    return json.loads(command('losetup', '--list', '--json', '--output',
                             'NAME,MAJ:MIN,BACK-INO,BACK-MAJ:MIN,OFFSET,SIZELIMIT,RO',
                             *devices))['loopdevices']


def owned_loop(loop, backing, size=SIZE):
    """Use kernel loop metadata, not its printable backing pathname, as identity."""
    if not re.fullmatch(r'/dev/loop[0-9]+', loop):
        raise RuntimeError('unexpected allocated loop name')
    rows = loop_rows(loop)
    if len(rows) != 1:
        raise RuntimeError('allocated loop is not configured')
    info = rows[0]
    st, source = os.stat(loop, follow_symlinks=False), os.stat(backing, follow_symlinks=False)
    number = f'{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}'
    source_number = f'{os.major(source.st_dev)}:{os.minor(source.st_dev)}'
    node = (Path('/sys/dev/block') / number).resolve(strict=True)
    if (not stat.S_ISBLK(st.st_mode) or not stat.S_ISREG(source.st_mode) or source.st_size != size
            or info['name'] != loop or info['maj:min'] != number
            or int(info['back-ino']) != source.st_ino or info['back-maj:min'] != source_number
            or int(info['offset']) != 0 or int(info['sizelimit']) != 0 or info['ro'] is not False
            or node != Path('/sys/devices/virtual/block') / Path(loop).name
            or (node / 'dev').read_text().strip() != number or (node / 'partition').exists()):
        raise RuntimeError('allocated loop does not match the owned backing image')
    return number, str(node), source.st_dev, source.st_ino


def cleanup_owned(backing, size=SIZE):
    """Reconcile by our inode even when losetup failed before returning its name."""
    source = backing.stat(follow_symlinks=False)
    source_number = f'{os.major(source.st_dev)}:{os.minor(source.st_dev)}'
    matches = []
    for row in loop_rows():
        # Unknown kernel metadata cannot establish absence; retain the backing.
        if row['back-ino'] is None or row['back-maj:min'] is None:
            raise RuntimeError('cleanup cannot establish kernel loop identities')
        if int(row['back-ino']) == source.st_ino and row['back-maj:min'] == source_number:
            matches.append(row['name'])
    for loop in matches:
        current = owned_loop(loop, backing, size=size)
        command('losetup', '--detach', loop)
        if Path(current[1], 'loop/backing_file').exists():
            raise RuntimeError('owned loop still configured after detach')
        print(json.dumps({'event': 'OWNED_LOOP_DETACHED', 'loop': loop}), flush=True)
    for row in loop_rows():
        if row['back-ino'] is None or row['back-maj:min'] is None:
            raise RuntimeError('cleanup cannot confirm loop release')
        if int(row['back-ino']) == source.st_ino and row['back-maj:min'] == source_number:
            raise RuntimeError('owned backing is still attached')


@contextlib.contextmanager
def managed_backing(base, size=SIZE):
    # Persistent on failure: never unlink backing while an allocation is uncertain.
    tmp = Path(tempfile.mkdtemp(prefix='gbos-fat-block-', dir=base))
    backing = tmp / 'owned.img'
    with backing.open('xb') as out:
        out.truncate(size)
    print(json.dumps({'event': 'OWNED_BACKING_CREATED', 'directory': str(tmp),
                      'backing_inode': backing.stat().st_ino}), flush=True)
    primary = None
    try:
        yield tmp, backing
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            cleanup_owned(backing, size=size)
        except BaseException as cleanup_error:
            note = f'owned-loop cleanup failed; backing retained at {tmp}: {cleanup_error}'
            print(json.dumps({'event': 'CLEANUP_INCOMPLETE', 'detail': note}), flush=True)
            if primary is not None:
                primary.add_note(note)
            else:
                raise
        else:
            shutil.rmtree(tmp)


class CleanupRegression(unittest.TestCase):
    def test_failed_allocation_is_reconciled_before_removing_backing(self):
        with tempfile.TemporaryDirectory() as base:
            seen = []
            def reconcile(backing, size=SIZE):
                seen.append(backing)
                self.assertTrue(backing.exists())
            with mock.patch(__name__ + '.cleanup_owned', side_effect=reconcile):
                with self.assertRaisesRegex(TimeoutError, 'allocation interrupted'):
                    with managed_backing(base):
                        raise TimeoutError('allocation interrupted after kernel attach')
            self.assertEqual(len(seen), 1)
            self.assertEqual(list(Path(base).iterdir()), [])

    def test_failed_cleanup_retains_backing_and_original_error(self):
        for problem in (RuntimeError('identity unavailable'), RuntimeError('detach failed')):
            with self.subTest(problem=str(problem)), tempfile.TemporaryDirectory() as base:
                original = KeyboardInterrupt('allocation signal')
                with mock.patch(__name__ + '.cleanup_owned', side_effect=problem):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        with managed_backing(base) as (_, backing):
                            raise original
                self.assertIs(caught.exception, original)
                self.assertTrue(backing.exists())
                self.assertIn(str(problem), original.__notes__[0])

    def test_cleanup_error_without_body_error_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as base:
            with mock.patch(__name__ + '.cleanup_owned', side_effect=RuntimeError('detach failed')):
                with self.assertRaisesRegex(RuntimeError, 'detach failed'):
                    with managed_backing(base) as (_, backing):
                        pass
            self.assertTrue(backing.exists())

    def test_reconcile_detaches_only_matching_backing_inode(self):
        with tempfile.TemporaryDirectory() as base:
            backing = Path(base) / 'owned.img'
            backing.touch()
            st = backing.stat()
            own = {'name': '/dev/loop999', 'back-ino': st.st_ino,
                   'back-maj:min': f'{os.major(st.st_dev)}:{os.minor(st.st_dev)}'}
            other = dict(own, name='/dev/loop998', **{'back-ino': st.st_ino + 1})
            with mock.patch(__name__ + '.loop_rows', side_effect=[[other, own], [other]]), \
                 mock.patch(__name__ + '.owned_loop', return_value=('7:999', '/nonexistent', st.st_dev, st.st_ino)) as identity, \
                 mock.patch(__name__ + '.command') as run:
                cleanup_owned(backing)
            identity.assert_called_once_with('/dev/loop999', backing, size=SIZE)
            run.assert_called_once_with('losetup', '--detach', '/dev/loop999')


@unittest.skipUnless(ACTIVE and all(shutil.which(x) for x in
                     ('losetup', 'mformat', 'mcopy', 'mmd', 'mdir', 'mdel', 'minfo')),
                     'requires explicit root Linux loop test and mtools')
class ExclusiveBlockFat(unittest.TestCase):
    def test_held_exclusive_block_fd_supports_mtools_and_releases_cleanly(self):
        base = os.environ.get('GBOS_TEST_LOOP_ROOT', '')
        if not os.path.isabs(base):
            self.fail('private root-owned GBOS_TEST_LOOP_ROOT is required')
        basefd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        old_handler = signal.getsignal(signal.SIGTERM)
        def terminate(signum, frame):
            raise KeyboardInterrupt('loop test terminated; cleaning owned resources')
        signal.signal(signal.SIGTERM, terminate)
        try:
            st = os.fstat(basefd)
            self.assertEqual(st.st_uid, 0)
            self.assertEqual(stat.S_IMODE(st.st_mode) & 0o077, 0)
            with managed_backing(f'/proc/{os.getpid()}/fd/{basefd}') as (tmp, backing):
                tmp = str(tmp)
                command('mformat', '-i', f'{backing}@@{OFFSET}', '-v', 'OWNEDLOOP',
                        '-T', '14336', '-h', '2', '-s', '32', '::')
                loop = command('losetup', '--find', '--show', str(backing)).strip()
                print(json.dumps({'event': 'LOOP_ALLOCATED', 'loop': loop,
                                  'backing_inode': backing.stat().st_ino}), flush=True)
                disk = None
                identity = None
                try:
                    identity = owned_loop(loop, backing)
                    number, syspath, _, _ = identity
                    disk = u.Disk(loop, write=True, rdev=number, syspath=syspath, nbytes=SIZE)
                    def exclusive_open_refused():
                        with self.assertRaisesRegex(u.Refuse, 'exclusively'):
                            other = u.Disk(loop, write=True, rdev=number, syspath=syspath, nbytes=SIZE)
                            os.close(other.fd)
                    exclusive_open_refused()
                    before = hashlib.sha256(backing.read_bytes()).hexdigest()
                    with self.assertRaisesRegex(u.Refuse, 'bounded synthetic regular files'):
                        u.apply(disk, object(), 'nvme', False, None, tmp, object.__new__(u.SharedBinding))
                    self.assertEqual(hashlib.sha256(backing.read_bytes()).hexdigest(), before)
                    fat = u.Fat(disk.vol(OFFSET), tmp, fd=disk.fd)
                    fat.write('/owned/probe.bin', b'owned-loop\x00\xff\n')
                    self.assertEqual(fat.read('/owned/probe.bin'), b'owned-loop\x00\xff\n')
                    self.assertIn('probe.bin', fat.ls('/owned'))
                    self.assertGreater(fat.free()[0], 0)
                    disk.pwrite(512, b'HELD-BLOCK-FD')
                    disk.sync_drop()
                    self.assertEqual(disk.pread(512, 13), b'HELD-BLOCK-FD')
                    fat.delete('/owned/probe.bin')
                    self.assertFalse(fat.exists('/owned/probe.bin'))
                    disk.sync_drop()
                    exclusive_open_refused()
                    self.assertEqual(owned_loop(loop, backing), identity)
                    os.close(disk.fd)
                    disk = None
                    # Closing our original claim allows a fresh exclusive claim.
                    reopened = u.Disk(loop, write=True, rdev=number, syspath=syspath, nbytes=SIZE)
                    try:
                        self.assertEqual(reopened.pread(512, 13), b'HELD-BLOCK-FD')
                    finally:
                        os.close(reopened.fd)
                    print(json.dumps({'event': 'BLOCK_FD_MTOOLS_OK', 'shared_apply': 'REFUSED_UNCHANGED',
                                      'scope': 'DISPOSABLE_LOOP_ONLY'}), flush=True)
                finally:
                    if disk is not None:
                        os.close(disk.fd)
        finally:
            signal.signal(signal.SIGTERM, old_handler)
            os.close(basefd)


if __name__ == '__main__':
    unittest.main()
