#!/usr/bin/env python3
"""Host-side privilege drop before executing a guest preparation tool (Linux)."""
import ctypes
import os
import re
import signal
import stat
import sys

libc = ctypes.CDLL(None, use_errno=True)

def check(value):
    if value != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))

def prctl(option, arg=0):
    check(libc.prctl(option, arg, 0, 0, 0))

class Header(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]

class Data(ctypes.Structure):
    _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32),
                ('inheritable', ctypes.c_uint32)]

def main():
    root, *command = sys.argv[1:]
    if command not in (['/usr/bin/systemd-sysusers'],
                       ['/usr/bin/systemd-tmpfiles', '--create', '--boot', '--prefix=/etc'],
                       ['/usr/bin/ldconfig']):
        raise ValueError('unapproved guest command')
    root = os.path.realpath(root)
    if root == '/' or os.getpid() != 1:
        raise ValueError('owned staging root and fresh PID namespace init required')
    with open('/proc/self/mountinfo', encoding='ascii') as stream:
        for line in stream:
            point = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), line.split()[4])
            if point == root or point.startswith(root + '/'):
                raise ValueError('mount inside staged root')
    with open('/proc/sys/kernel/cap_last_cap', encoding='ascii') as stream:
        last = int(stream.read())
    if not 0 <= last < 64:
        raise ValueError('unsupported capability range')
    # The pinned tmpfiles utility requires proc. Mount a NEW proc instance for
    # this PID namespace, read-only, never a bind of the host proc filesystem.
    # Private mount propagation and the owned namespace lifecycle contain it.
    proc = root + '/proc'
    if not os.path.exists(proc):
        os.mkdir(proc, 0o755)
    if not stat.S_ISDIR(os.lstat(proc).st_mode) or os.listdir(proc):
        raise ValueError('proc mountpoint must be an empty real directory')
    libc.mount.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                          ctypes.c_ulong, ctypes.c_void_p)
    libc.mount.restype = ctypes.c_int
    check(libc.mount(b'proc', os.fsencode(proc), b'proc', 1 | 2 | 4 | 8, None))
    os.chroot(root)
    os.chdir('/')
    # CAP_SETPCAP remains available until every unwanted bounding bit is gone.
    keep = {0, 1, 3, 4}  # CHOWN, DAC_OVERRIDE, FOWNER, FSETID
    for cap in range(last + 1):
        if cap not in keep:
            prctl(24, cap)  # PR_CAPBSET_DROP
    header = Header(0x20080522, 0)
    data = (Data * 2)()
    mask = sum(1 << cap for cap in keep)
    data[0].effective = data[0].permitted = mask
    check(libc.capset(ctypes.byref(header), ctypes.byref(data)))
    prctl(38, 1)  # PR_SET_NO_NEW_PRIVS
    # exec of the unprivileged file retains only bounding bits through root's
    # normal exec rules, with no_new_privs preventing privilege elevation.
    signal.pthread_sigmask(signal.SIG_SETMASK, [])
    os.execve(command[0], command, {'PATH': '/usr/sbin:/usr/bin', 'HOME': '/',
                                     'LANG': 'C', 'SYSTEMD_OFFLINE': '1'})

if __name__ == '__main__':
    main()
