"""Pinned HX370 read-only update_engine replacement in the IFMA system tree."""
import hashlib
import os
from pathlib import Path
import re
import stat

STOCK_RC_SHA256 = 'ddd600b1c1a5bc8b683d959bbe38b1978ab327929b95241641ff44e98f06e098'
STOCK_BINARY_SHA256 = 'cc9962207240038b877d71d9fed4653ad640f74a3c560e8a9ff036ed9ce235e5'
# Reviewed lifecycle shim; the build helper reproduces and verifies this digest.
SHIM_SHA256 = '449c6ed1d4060d0f01430a97a6b9ebdc7af3a820058d93c51b1b3e2315d329cf'
RC = 'system/etc/init/update_engine.rc'
BINARY = 'system/bin/update_engine'
ORIGINAL = 'service update_engine /system/bin/update_engine --logtostderr --logtofile --foreground'
REPLACEMENT = 'service update_engine /system/bin/sh -c "exit 0"'
APPEND = '''\n# Read-only service for the unsupported stock updater.\nservice gbos_update_engine_shim /system/bin/update_engine\n    class late_start\n    user root\n    group root system wakelock\n'''


def _regular(path):
    if path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f'unsafe file: {path}')
    return path.read_bytes()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def checked_shim():
    if not isinstance(SHIM_SHA256, str) or not re.fullmatch(r'[0-9a-f]{64}', SHIM_SHA256):
        raise ValueError('reviewed read-only update_engine binary is not pinned')
    shim_path = os.environ.get('GBOS_READONLY_UPDATE_ENGINE')
    if not shim_path:
        raise ValueError('GBOS_READONLY_UPDATE_ENGINE is required')
    shim = _regular(Path(shim_path))
    if _sha(shim) != SHIM_SHA256:
        raise ValueError('unexpected update_engine shim')
    return shim


def apply(profile, tree):
    if not profile.get('services', {}).get('readonly_update_engine', False):
        return {}
    if profile.get('model', {}).get('id') != 'amd-hx370' or not profile.get('cpu_compat', {}).get('patch_ifma'):
        raise ValueError('read-only update engine requires amd-hx370 and IFMA')
    shim = checked_shim()
    rc_path, bin_path = tree / RC, tree / BINARY
    rc, binary = _regular(rc_path), _regular(bin_path)
    if _sha(shim) != SHIM_SHA256 or _sha(rc) != STOCK_RC_SHA256 or _sha(binary) != STOCK_BINARY_SHA256:
        raise ValueError('unexpected update_engine shim or stock input')
    text = rc.decode('utf-8')
    if text.count(ORIGINAL) != 1 or 'gbos_update_engine_shim' in text:
        raise ValueError('unexpected update_engine init configuration')
    for path in (tree / 'system/etc/init').glob('*.rc'):
        for line in _regular(path).decode('utf-8').splitlines():
            if re.match(r'^\s*service\s+\S+\s+/system/bin/update_engine(?:_nonetd|_nostats)?(?:\s|$)', line) and not (path == rc_path and line == ORIGINAL):
                raise ValueError(f'additional updater service: {path.name}')
    updated = (text.replace(ORIGINAL, REPLACEMENT) + APPEND).encode()
    bin_path.write_bytes(shim)
    rc_path.write_bytes(updated)
    report = {RC: _sha(updated), BINARY: SHIM_SHA256}
    verify(tree, report)
    return report


def verify(tree, report):
    for rel, digest in report.items():
        if _sha(_regular(tree / rel)) != digest:
            raise ValueError(f'rebuilt update_engine mismatch: {rel}')
