"""Fault injection for python3 started by the fake KANO (see fakes/ssh).

The install script classifies the nonce with os.lstat and lists a userdata root with
os.listdir. faults.json can make those raise EIO without changing the product script:

  {"eio_lstat": ["/mnt/ud/kano-autooff.nonce"]}
  {"eacces_lstat": ["/mnt/ud/kano-autooff.nonce"]}
  {"eio_lstat_nth": {"/mnt/ud/kano-autooff.nonce": 2}}
  {"eio_listdir": ["/mnt/ud"]}
  {"eio_scandir": ["/mnt/ud/kano-autooff.nonce"]}
  {"eacces_rmdir": ["/mnt/ud/kano-autooff.nonce/nested"]}
  {"eio_rmtree_after": true}
  {"rmtree_no_onexc": true}
  {"rmtree_avoids_symlink_attacks": false}
  {"rmtree_keep": true}

Paths are the logical /mnt/... names. The fake rewrites them under FAKE_ROOT before
python sees them; this module strips that prefix. remote.py is skipped so the rm/rmdir
stubs keep their own filesystem calls. The nth counter is per path and persists across
the short-lived python3 -c processes in one install.

shutil is imported before os.open / os.scandir / os.rmdir / os.unlink are wrapped.
rmtree.avoids_symlink_attacks is decided from the real functions at that import.
"""
import errno, json, os, sys

_real_lstat = os.lstat
_real_listdir = os.listdir
_root = os.environ.get("FAKE_ROOT", "")


def _skip():
    return os.path.basename(sys.argv[0]) == "remote.py"


def _faults():
    if not _root:
        return {}
    path = os.path.join(_root, "faults.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _norm(path):
    text = os.fspath(path)
    if _root and text.startswith(_root):
        text = text[len(_root):]
        if not text.startswith("/"):
            text = "/" + text
    text = text.rstrip("/")
    return text or "/"


def _bump(key):
    log = os.path.join(_root, "log")
    os.makedirs(log, exist_ok=True)
    count_path = os.path.join(log, "lstat-counts.json")
    try:
        with open(count_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = int(data.get(key, 0)) + 1
    with open(count_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return data[key]


def _lstat(path, *args, **kwargs):
    if not _skip():
        faults = _faults()
        key = _norm(path)
        nth = faults.get("eio_lstat_nth") or {}
        if isinstance(nth, dict) and key in nth and _bump(key) == int(nth[key]):
            raise OSError(errno.EIO, "Input/output error")
        listed = faults.get("eio_lstat") or []
        if key in listed:
            raise OSError(errno.EIO, "Input/output error")
        denied = faults.get("eacces_lstat") or []
        if key in denied:
            raise PermissionError(errno.EACCES, "Permission denied")
    return _real_lstat(path, *args, **kwargs)


def _listdir(path):
    if not _skip():
        faults = _faults()
        key = _norm(path)
        listed = faults.get("eio_listdir") or []
        if key in listed:
            raise OSError(errno.EIO, "Input/output error")
    return _real_listdir(path)


os.lstat = _lstat
os.listdir = _listdir

# Import shutil before replacing fd-based os functions. rmtree.avoids_symlink_attacks
# is fixed from those functions at import; wrapping them first would make it False.
import inspect
import shutil as _shutil

_real_open = os.open
_real_scandir = os.scandir
_real_rmdir = os.rmdir
_real_unlink = os.unlink
_fd_paths = {}


def _joined(path, dir_fd):
    if isinstance(path, int):
        return _fd_paths.get(path)
    name = os.fsdecode(path) if isinstance(path, (bytes, bytearray)) else os.fspath(path)
    if dir_fd is not None:
        parent = _fd_paths.get(dir_fd)
        if parent:
            return parent.rstrip("/") + "/" + name
    return _norm(name)


def _raise_listed(logical, pairs):
    if not logical:
        return
    faults = _faults()
    for key, err in pairs:
        listed = faults.get(key) or []
        if logical in listed:
            raise OSError(err, os.strerror(err))


def _open(path, flags, mode=0o777, *, dir_fd=None):
    logical = None
    if not _skip():
        logical = _joined(path, dir_fd)
        _raise_listed(logical, (("eio_open", errno.EIO), ("eacces_open", errno.EACCES)))
    if dir_fd is None:
        fd = _real_open(path, flags, mode)
    else:
        fd = _real_open(path, flags, mode, dir_fd=dir_fd)
    if not _skip() and logical and isinstance(fd, int):
        _fd_paths[fd] = logical
    return fd


def _scandir(path=None):
    if not _skip() and path is not None:
        logical = _fd_paths.get(path) if isinstance(path, int) else _norm(path)
        _raise_listed(logical, (("eio_scandir", errno.EIO), ("eacces_scandir", errno.EACCES)))
    if path is None:
        return _real_scandir()
    return _real_scandir(path)


def _rmdir(path, *, dir_fd=None):
    if not _skip():
        _raise_listed(_joined(path, dir_fd), (("eio_rmdir", errno.EIO), ("eacces_rmdir", errno.EACCES)))
    if dir_fd is None:
        return _real_rmdir(path)
    return _real_rmdir(path, dir_fd=dir_fd)


def _unlink(path, *, dir_fd=None):
    if not _skip():
        _raise_listed(_joined(path, dir_fd), (("eio_unlink", errno.EIO), ("eacces_unlink", errno.EACCES)))
    if dir_fd is None:
        return _real_unlink(path)
    return _real_unlink(path, dir_fd=dir_fd)


os.open = _open
os.scandir = _scandir
os.rmdir = _rmdir
os.unlink = _unlink


def _tune_shutil():
    if _skip():
        return
    faults = _faults()
    if not faults:
        return
    real = _shutil.rmtree
    flag = getattr(real, "avoids_symlink_attacks", False)
    if faults.get("rmtree_avoids_symlink_attacks") is False:
        flag = False
    if faults.get("rmtree_keep"):
        def keep(*args, **kwargs):
            return None
        keep.avoids_symlink_attacks = flag
        _shutil.rmtree = keep
        return
    if faults.get("rmtree_no_onexc"):
        def legacy(path, ignore_errors=False, onerror=None):
            real(path, ignore_errors=ignore_errors, onerror=onerror)
            if ignore_errors:
                return None
            err = OSError(errno.EIO, "Input/output error")
            if onerror is not None:
                onerror(os.rmdir, path, (OSError, err, None))
                return None
            raise err
        legacy.avoids_symlink_attacks = flag
        _shutil.rmtree = legacy
        return
    if faults.get("eio_rmtree_after"):
        def after(path, ignore_errors=False, onerror=None, *, onexc=None, dir_fd=None):
            params = inspect.signature(real).parameters
            kw = {}
            if "onexc" in params:
                kw["onexc"] = onexc
            if dir_fd is not None and "dir_fd" in params:
                kw["dir_fd"] = dir_fd
            real(path, ignore_errors=ignore_errors, onerror=onerror, **kw)
            if ignore_errors:
                return None
            err = OSError(errno.EIO, "Input/output error")
            if onexc is not None:
                onexc(os.rmdir, path, err)
                return None
            if onerror is not None:
                onerror(os.rmdir, path, (OSError, err, None))
                return None
            raise err
        after.avoids_symlink_attacks = flag
        _shutil.rmtree = after
        return
    if faults.get("rmtree_avoids_symlink_attacks") is False:
        real.avoids_symlink_attacks = False


_tune_shutil()
