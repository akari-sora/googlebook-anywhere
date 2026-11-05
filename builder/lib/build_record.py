"""Bounded builder self-report; neither authentication nor write authorization."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MAX_RECORD = 65536
HASH = re.compile(r'[0-9a-f]{64}\Z')
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')


def need(ok):
    if not ok:
        raise ValueError('invalid build record')


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result)
        result[key] = value
    return result


def schema(raw):
    need(0 < len(raw) <= MAX_RECORD)
    obj = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
    need(type(obj) is dict and set(obj) == {'schema', 'profile', 'profile_sha256', 'declared_build_id',
         'build_id_basis', 'image', 'selftest', 'not_run', 'options', 'pristine', 'input_coverage', 'authority'})
    need(type(obj['schema']) is int and obj['schema'] == 1)
    for k in ('profile', 'declared_build_id'):
        need(type(obj[k]) is str and TOKEN.fullmatch(obj[k]))
    need(type(obj['profile_sha256']) is str and HASH.fullmatch(obj['profile_sha256']))
    need(obj['build_id_basis'] == 'selected_pins' and obj['pristine'] == 'NOT_PROVEN' and
         obj['input_coverage'] == 'INCOMPLETE' and obj['authority'] == 'UNSIGNED_BUILDER_SELF_REPORT')
    image = obj['image']
    need(type(image) is dict and set(image) == {'basename', 'size', 'sha256'})
    need(type(image['basename']) is str and TOKEN.fullmatch(image['basename']) and image['basename'].endswith('.img'))
    need(type(image['size']) is int and 0 < image['size'] <= 128 * 1024**3)
    need(type(image['sha256']) is str and HASH.fullmatch(image['sha256']))
    need(obj['selftest'] in ('passed', 'skipped'))
    need(type(obj['not_run']) is list and len(obj['not_run']) <= 32 and
         all(type(x) is str and 0 < len(x) <= 160 for x in obj['not_run']) and
         len(set(obj['not_run'])) == len(obj['not_run']))
    opts = obj['options']
    option_keys = {'entry', 'editor', 'ec', 'adb', 'usb_smoke', 'model_override'}
    need(type(opts) is dict and set(opts) in (option_keys, option_keys | {'setup_pin_skip'}))
    need(opts['entry'] in ('lapis-quiet.conf', 'lapis-debug.conf') and opts['editor'] in ('yes', 'no'))
    need(type(opts['ec']) is list and len(opts['ec']) <= 3 and
         all(x in ('drain', 'sensors', 'typec', 'buttons') for x in opts['ec']))
    need(all(type(opts[x]) is bool for x in ('adb', 'usb_smoke', 'model_override')))
    if 'setup_pin_skip' in opts:
        need(type(opts['setup_pin_skip']) is bool)
    return obj


def encode(obj):
    raw = (json.dumps(obj, sort_keys=True, separators=(',', ':')) + '\n').encode()
    schema(raw)
    return raw


def read_pinned(path, pin):
    need(type(pin) is str and HASH.fullmatch(pin))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= MAX_RECORD)
        raw = bytearray()
        while len(raw) <= MAX_RECORD:
            part = os.read(fd, MAX_RECORD + 1 - len(raw))
            if not part:
                break
            raw.extend(part)
        after = os.fstat(fd)
        need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
             (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        raw = bytes(raw)
        need(len(raw) == before.st_size and hashlib.sha256(raw).hexdigest() == pin)
        return raw, schema(raw)
    finally:
        os.close(fd)


def publish(img, final, digest, record, passed):
    """Publish sidecars exclusively and image last; failed gate never publishes."""
    need(passed and HASH.fullmatch(digest) and record['image']['sha256'] == digest)
    raw = encode(record)
    img, final = Path(img), Path(final)
    sidecars = [(Path(str(final) + '.sha256'), (digest + '  ' + final.name + '\n').encode()),
                (Path(str(final) + '.build-record.json'), raw)]
    if record['selftest'] == 'skipped':
        sidecars.append((Path(str(final) + '.not-self-tested'),
                         b'built with GBOS_SKIP_SELFTEST=1: never booted in QEMU before release\n'))
    paths = [final, Path(str(final) + '.not-self-tested')] + [p for p, _ in sidecars]
    need(not any(os.path.lexists(p) for p in paths))
    need(not any(os.path.lexists(str(p) + '.partial') for p in paths if p != final))
    need(img.parent == final.parent)
    image_fd = os.open(img, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(image_fd)
        need(stat.S_ISREG(info.st_mode) and info.st_size == record['image']['size'])
        os.fsync(image_fd)
    finally:
        os.close(image_fd)
    dir_fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    created = []
    try:
        for target, data in sidecars:
            tmp = Path(str(target) + '.partial')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            created.append(tmp)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.link(tmp, target)
            created.append(target)
            tmp.unlink()
        os.fsync(dir_fd)
        os.link(img, final)
        created.append(final)
        img.unlink()
        os.fsync(dir_fd)
    except BaseException:
        # Sidecars were made durable before the final link. Once that link may
        # exist, keep the complete set and report failure; removing sidecars
        # without durable removal of the image could expose an incomplete image
        # after a crash. Checking the path also covers interruption immediately
        # after link(), before the Python bookkeeping is updated.
        if os.path.lexists(final):
            raise
        for path in reversed(created):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(dir_fd)
