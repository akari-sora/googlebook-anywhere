#!/usr/bin/env python3
"""Public CLI lifecycle regressions; no images, keys, devices, or network.

The pinned pre-fix source (7d82e910...) was reproduced launching a new process
session and leaving its child alive after a Thread.start BaseException. The
fixture owner killed/reaped it. These tests lock the same-PGID replacement and
its immediate-child cleanup. Non-child zombies are never claimed reaped.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "builder/lib/devprofile.py"
SPEC = importlib.util.spec_from_file_location("decoder_lifecycle_devprofile", SOURCE)
DEV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEV)


class InjectedAbort(BaseException):
    pass


class DecoderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.children = []
        self.real_popen = subprocess.Popen
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()

    def record_popen(self, *args, **kwargs):
        child = self.real_popen(*args, **kwargs)
        self.children.append(child)
        return child

    def run_cli(self, script, data=b"", **kwargs):
        with mock.patch.object(DEV.subprocess, "Popen", side_effect=self.record_popen):
            return DEV._run_bounded([sys.executable, "-B", "-c", script], data, **kwargs)

    def assert_closed_reaped(self):
        self.assertTrue(self.children)
        for child in self.children:
            self.assertIsNotNone(child.poll())
            self.assertTrue(child.stdin.closed)
            self.assertTrue(child.stdout.closed)
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)

    def test_success_exact_bytes_and_stderr_discard(self):
        result = self.run_cli(
            "import sys;sys.stderr.write('discard');"
            "sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])", b"public\x00fixture",
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, b"public\x00fixture"[::-1], b""))
        self.assert_closed_reaped()

    def test_nonzero_preserves_output_and_exit(self):
        result = self.run_cli("import sys;sys.stdout.write('partial');sys.exit(7)")
        self.assertEqual((result.returncode, result.stdout), (7, b"partial"))
        self.assert_closed_reaped()

    def test_actual_cli_inherits_process_group(self):
        result = self.run_cli("import os;print(os.getpgrp())")
        self.assertEqual(int(result.stdout), os.getpgrp())
        self.assert_closed_reaped()

    def test_short_writes_and_transient_eagain(self):
        real_write, real_read = os.write, os.read
        counts = {"write": 0, "read": 0}

        def short_write(fd, data):
            counts["write"] += 1
            if counts["write"] == 1:
                raise BlockingIOError()
            return real_write(fd, data[:137])

        def transient_read(fd, length):
            # Do not inject into Popen's own pre-return exec-error pipe.
            if self.children and fd == self.children[-1].stdout.fileno():
                counts["read"] += 1
                if counts["read"] == 1:
                    raise BlockingIOError()
            return real_read(fd, length)

        data = b"public-partial-write" * 4096
        with mock.patch.object(DEV.os, "write", side_effect=short_write), \
                mock.patch.object(DEV.os, "read", side_effect=transient_read):
            result = self.run_cli("import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())", data)
        self.assertEqual(result.stdout, data)
        self.assertGreater(counts["write"], 2)
        self.assert_closed_reaped()

    def test_broken_stdin_pipe_still_drains_output(self):
        result = self.run_cli("import os;os.close(0);os.write(1,b'completed')", b"x" * 1048576)
        self.assertEqual((result.returncode, result.stdout), (0, b"completed"))
        self.assert_closed_reaped()

    def test_timeout_retains_prefix_and_reaps_child(self):
        with mock.patch.object(DEV, "_decoder_timeout", return_value=1):
            with self.assertRaisesRegex(DEV.DecoderLimitError, "timed out") as caught:
                self.run_cli("import os,time;os.write(1,b'prefix');time.sleep(30)")
        self.assertEqual(caught.exception.decoded, b"prefix")
        self.assert_closed_reaped()

    def test_cap_plus_one_keeps_exact_cap(self):
        with mock.patch.object(DEV, "DECODER_OUTPUT_CAP", 64):
            with self.assertRaisesRegex(DEV.DecoderLimitError, "exceeds 64") as caught:
                self.run_cli("import os;os.write(1,b'x'*65)")
        self.assertEqual(caught.exception.decoded, b"x" * 64)
        self.assert_closed_reaped()

    def test_stdout_closed_but_child_alive_shares_deadline(self):
        started = time.monotonic()
        with mock.patch.object(DEV, "_decoder_timeout", return_value=1):
            with self.assertRaisesRegex(DEV.DecoderLimitError, "timed out") as caught:
                self.run_cli("import os,time;os.close(1);time.sleep(30)")
        self.assertEqual(caught.exception.decoded, b"")
        self.assertLess(time.monotonic() - started, 6)
        self.assert_closed_reaped()

    def test_drains_stdout_after_direct_child_exit(self):
        result = self.run_cli(
            "import os,time\n"
            "if os.fork()==0:\n time.sleep(.1);os.write(1,b'late');os._exit(0)\n"
            "os._exit(0)"
        )
        self.assertEqual((result.returncode, result.stdout), (0, b"late"))
        self.assert_closed_reaped()

    def test_selector_constructor_abort_reaps_before_return(self):
        with mock.patch.object(DEV.selectors, "DefaultSelector", side_effect=InjectedAbort):
            with self.assertRaises(InjectedAbort):
                self.run_cli("import time;time.sleep(30)")
        self.assert_closed_reaped()

    def test_registration_and_select_aborts_close_selector_and_child(self):
        real_selector = DEV.selectors.DefaultSelector
        for phase in ("register", "select"):
            with self.subTest(phase=phase):
                selectors = []

                class AbortingSelector:
                    def __init__(self):
                        self.inner = real_selector()
                        self.closed = False
                        self.registrations = 0
                        selectors.append(self)

                    def register(self, *args):
                        self.registrations += 1
                        if phase == "register" and self.registrations == 2:
                            raise InjectedAbort()
                        return self.inner.register(*args)

                    def select(self, *args):
                        raise InjectedAbort()

                    def close(self):
                        self.closed = True
                        self.inner.close()

                    def __getattr__(self, name):
                        return getattr(self.inner, name)

                with mock.patch.object(DEV.selectors, "DefaultSelector", AbortingSelector):
                    with self.assertRaises(InjectedAbort):
                        self.run_cli("import time;time.sleep(30)", b"input")
                self.assertTrue(all(selector.closed for selector in selectors))
                self.assert_closed_reaped()

    def test_read_failure_is_not_success(self):
        real_read = os.read

        def read_error(fd, length):
            # Popen also reads its exec-error pipe before registering stdout.
            if self.children and fd == self.children[-1].stdout.fileno():
                raise OSError("public injected read failure")
            return real_read(fd, length)

        with mock.patch.object(DEV.os, "read", side_effect=read_error):
            with self.assertRaisesRegex(OSError, "public injected read failure"):
                self.run_cli("import os,time;os.write(1,b'prefix');time.sleep(30)")
        self.assert_closed_reaped()

    def test_lookup_race_during_cleanup_does_not_skip_reap(self):
        def record_racy(*args, **kwargs):
            child = self.record_popen(*args, **kwargs)
            real_kill = child.kill

            def exited_before_kill():
                real_kill()
                child.wait(timeout=5)
                raise ProcessLookupError()

            child.kill = exited_before_kill
            return child

        with mock.patch.object(DEV.subprocess, "Popen", side_effect=record_racy), \
                mock.patch.object(DEV.selectors, "DefaultSelector", side_effect=InjectedAbort):
            with self.assertRaises(InjectedAbort):
                DEV._run_bounded([sys.executable, "-c", "import time;time.sleep(30)"], b"")
        self.assert_closed_reaped()

    def test_actual_benign_cli_under_session_escape_guard(self):
        denied = []

        def guarded(*args, **kwargs):
            if kwargs.get("start_new_session") or kwargs.get("process_group") not in (None, -1):
                denied.append(True)
                raise SystemExit("public guard refusal")
            return self.record_popen(*args, **kwargs)

        with mock.patch.object(DEV.subprocess, "Popen", side_effect=guarded):
            result = DEV._run_bounded([sys.executable, "-c", "print('allowed')"], b"")
        self.assertEqual((result.returncode, result.stdout, denied), (0, b"allowed\n", []))
        self.assert_closed_reaped()

    def test_outer_owned_group_term_contains_decoder_descendant(self):
        record = Path(self.temp.name) / "group.json"
        decoder = (
            "import json,os,subprocess,sys,time\n"
            "kid=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
            f"open({str(record)!r},'w').write(json.dumps(dict(pid=os.getpid(),pgid=os.getpgrp(),kid=kid.pid)))\n"
            "time.sleep(30)"
        )
        worker = (
            "import importlib.util,sys\n"
            f"s=importlib.util.spec_from_file_location('d',{str(SOURCE)!r});"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
            f"m._run_bounded([sys.executable,'-c',{decoder!r}],b'')"
        )
        leader = self.real_popen([sys.executable, "-B", "-c", worker], start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        row = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if record.exists():
                    try:
                        row = json.loads(record.read_text())
                        break
                    except json.JSONDecodeError:
                        pass
                time.sleep(.02)
            self.assertIsNotNone(row, "public fixture did not start")
            self.assertEqual(row["pgid"], leader.pid)
            self.assertEqual(os.getpgid(row["kid"]), leader.pid)
            os.killpg(leader.pid, signal.SIGTERM)
            leader.wait(timeout=5)
            for pid in (row["pid"], row["kid"]):
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                       capture_output=True, timeout=5).stdout.strip()
                self.assertTrue(not state or state.startswith(b"Z"), "live owned group member")
        finally:
            groups = {leader.pid}
            if row is not None:
                groups.add(row["pgid"])
            for group in groups:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if leader.poll() is None:
                leader.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
