#!/usr/bin/env python3
"""Offline catalog and payload binding; never authorizes any write or boot."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
import re
import stat
import sys

HASH = re.compile(r'[0-9a-f]{64}\Z')
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
COMPONENT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z')
ENTRY = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _.,()+-]{0,127}\Z')
PROFILES = frozenset(('kano', 'amd-hx370'))
MAX_CATALOG = 1024 * 1024
MAX_IMAGE = 128 * 1024 ** 3
CHUNK = 1024 * 1024


class Refused(ValueError):
    pass


def require(condition):
    if not condition:
        raise Refused('invalid_catalog_or_payload')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def relative_parts(value):
    require(isinstance(value, str) and 0 < len(value) <= 512 and '\\' not in value)
    parts = value.split('/')
    require(len(parts) <= 16 and all(COMPONENT.fullmatch(part) and part not in ('.', '..') for part in parts))
    return parts


def open_relative(root_fd, path):
    """Open components without following symlinks; caller owns returned fd."""
    parts = relative_parts(path)
    directory = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = nxt
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                       dir_fd=directory)
    finally:
        os.close(directory)


def stable_signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def verify_file(root_fd, path, fd, limit, expected_hash, exact_size=None, collect=False):
    before = os.fstat(fd)
    require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= limit)
    require(exact_size is None or before.st_size == exact_size)
    digest = hashlib.sha256()
    total = 0
    data = bytearray() if collect else None
    while True:
        part = os.read(fd, min(CHUNK, limit - total + 1))
        if not part:
            break
        total += len(part)
        require(total <= limit)
        digest.update(part)
        if data is not None:
            data.extend(part)
    after = os.fstat(fd)
    require(total == before.st_size and stable_signature(before) == stable_signature(after))
    require(digest.hexdigest() == expected_hash)
    # Detect a path replaced while its original fd was being read. The fd
    # remains bound to its inode, not to immutable content or future names.
    again = open_relative(root_fd, path)
    try:
        require(stable_signature(os.fstat(again)) == stable_signature(after))
    finally:
        os.close(again)
    os.lseek(fd, 0, os.SEEK_SET)
    return bytes(data) if data is not None else None


def schema(raw):
    obj = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    require(isinstance(obj, dict) and set(obj) == {'schema', 'payloads'})
    require(type(obj['schema']) is int and obj['schema'] == 1)
    payloads = obj['payloads']
    require(isinstance(payloads, list) and 1 <= len(payloads) <= len(PROFILES))
    profiles, paths = set(), set()
    for item in payloads:
        require(isinstance(item, dict) and set(item) ==
                {'profile', 'build_id', 'image', 'boot_entries', 'hardware_validation'})
        profile = item['profile']
        require(isinstance(profile, str) and profile in PROFILES and profile not in profiles)
        profiles.add(profile)
        require(isinstance(item['build_id'], str) and TOKEN.fullmatch(item['build_id']))
        require(item['hardware_validation'] == 'unvalidated')
        image = item['image']
        require(isinstance(image, dict) and set(image) == {'path', 'size', 'sha256'})
        relative_parts(image['path'])
        require(image['path'] not in paths)
        paths.add(image['path'])
        require(type(image['size']) is int and 0 < image['size'] <= MAX_IMAGE)
        require(isinstance(image['sha256'], str) and HASH.fullmatch(image['sha256']))
        entries = item['boot_entries']
        require(isinstance(entries, list) and 1 <= len(entries) <= 16)
        require(all(isinstance(e, str) and ENTRY.fullmatch(e) for e in entries))
        require(len(set(entries)) == len(entries))
    return payloads


class VerifiedCatalog:
    """Own verified descriptors until closed; content may still change later."""
    def __init__(self, stack, payloads, descriptors):
        self._stack = stack
        self.payloads = payloads
        self.descriptors = descriptors

    def close(self):
        self._stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def report(self):
        return {'result': 'CATALOG_PAYLOADS_VERIFIED', 'profiles': sorted(p['profile'] for p in self.payloads),
                'write_allowed': False, 'boot_entries': 'EXPECTATIONS_ONLY',
                'boot_policy': 'NOT_VERIFIED', 'avb': 'NOT_VERIFIED', 'hardware': 'NOT_VERIFIED'}


def validate(root, catalog_path, expected_catalog_sha256):
    require(isinstance(expected_catalog_sha256, str) and HASH.fullmatch(expected_catalog_sha256))
    stack = ExitStack()
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, root_fd)
        cat_fd = open_relative(root_fd, catalog_path)
        stack.callback(os.close, cat_fd)
        raw = verify_file(root_fd, catalog_path, cat_fd, MAX_CATALOG, expected_catalog_sha256, collect=True)
        payloads = schema(raw)
        descriptors = {}
        for item in payloads:
            image = item['image']
            fd = open_relative(root_fd, image['path'])
            stack.callback(os.close, fd)
            verify_file(root_fd, image['path'], fd, MAX_IMAGE, image['sha256'], image['size'])
            descriptors[item['profile']] = fd
        return VerifiedCatalog(stack, payloads, descriptors)
    except BaseException:
        stack.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='trusted catalog root directory')
    parser.add_argument('--catalog', required=True, help='catalog path relative to root')
    parser.add_argument('--sha256', required=True, help='expected catalog SHA-256 from an independent source')
    args = parser.parse_args()
    try:
        with validate(args.root, args.catalog, args.sha256) as result:
            print(json.dumps(result.report(), sort_keys=True))
            return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        print(json.dumps({'result': 'REFUSED', 'write_allowed': False, 'boot_policy': 'NOT_VERIFIED',
                          'avb': 'NOT_VERIFIED', 'hardware': 'NOT_VERIFIED'}, sort_keys=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
