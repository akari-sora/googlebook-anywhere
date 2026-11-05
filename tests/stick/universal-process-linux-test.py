#!/usr/bin/env python3
"""Opt-in Linux root proof: PID namespace timeout/interruption leave no live children.

Runs only synthetic Python processes, never guest binaries or hardware.
"""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('stick', ROOT / 'install/stick/universal-stick.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)

@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0 and
                     os.environ.get('GBOS_TEST_PIDNS') == '1', 'opt-in Linux root PID namespace proof')
class NamespaceCleanup(unittest.TestCase):
    def test_timeout_and_interruption(self):
        child = '''import os,sys,time,subprocess
p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
with open(sys.argv[1]+'.tmp', 'w') as f:
 f.write(open('/proc/self/status').read().split('Pid:\\t')[1].split('\\n')[0]+' '+str(p.pid))
os.replace(sys.argv[1]+'.tmp', sys.argv[1])
time.sleep(60)
'''
        # Read host-visible descendants directly from /proc; guest PID values differ.
        for mode in ('timeout', 'interrupt'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                pidfile = Path(tmp) / 'ready'
                runner = '''import importlib.util,sys
s=importlib.util.spec_from_file_location('u',sys.argv[1]);u=importlib.util.module_from_spec(s);s.loader.exec_module(u)
u.run_owned(['unshare','--pid','--fork','--kill-child=SIGKILL',sys.executable,'-c',sys.argv[3],sys.argv[4]],sys.argv[2],timeout=float(sys.argv[5]))
'''
                p = subprocess.Popen([sys.executable, '-c', runner, str(ROOT / 'install/stick/universal-stick.py'), tmp,
                                      child, str(pidfile), '1' if mode == 'timeout' else '30'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 5
                    while not pidfile.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(pidfile.exists())
                    initpid = int(pidfile.read_text().split()[0])
                    children_path = Path(f'/proc/{initpid}/task/{initpid}/children')
                    children = [int(x) for x in children_path.read_text().split()]
                    self.assertTrue(children)
                    if mode == 'interrupt':
                        os.kill(p.pid, signal.SIGTERM)
                    p.wait(timeout=5)
                    # Product run_owned must return only after every descendant
                    # has been reaped; a test-side grace period hides the bug.
                    self.assertFalse(any(Path(f'/proc/{x}').exists() for x in [initpid] + children))
                finally:
                    if p.poll() is None:
                        p.kill()
                        p.wait()

if __name__ == '__main__':
    unittest.main()
