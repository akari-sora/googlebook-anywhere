#!/usr/bin/env python3
"""QEMU framework acceptance follows the display configuration; other gates fail closed."""
import ast
import contextlib
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

source = (Path(__file__).resolve().parents[2] / 'builder/image.py').read_text()
tree = ast.parse(source)
names = {'serial_device_probe', 'serial_boot_completed', 'framework_log_checks',
         'qemu_framework_checks', 'record_not_run'}
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
assert {n.name for n in nodes} == names
ns = {'re': re}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'builder/image.py', 'exec'), ns)

TOKEN = 'GBOS_BOOT_123'
GOOD = f'gbos-qemu-probe: {TOKEN} boot_completed=1 system_server_pid=123\n'
BOOT = 'device getprop sys.boot_completed is 1'
STABLE = 'system_server did not restart repeatedly'
# Execute the production checksum/rename gate on tiny fixtures, without building an image or running QEMU.
gate = source[source.index('after = sha256(IMG)\nchecks["image unchanged by the QEMU runs"]'):]

class FrameworkAcceptance(unittest.TestCase):
    def finish(self, log, gop, age=5, other_checks=None):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            image, final = out / 'test.img.partial', out / 'test.img'
            image.write_bytes(b'image fixture')
            checks = dict(other_checks or {})
            # Use the actual call sites too, so the test detects bypassing framework integration.
            calls = source[source.index('framework_checks, not_run = qemu_framework_checks('):
                           source.index('checks.update(ec_selftest_checks(')]
            local = dict(ns, GBOS_OUT=out, checks=checks, log=log, probe_token=TOKEN, probe_age=age, GOP=gop)
            output = io.StringIO()
            ns['GBOS_OUT'] = out
            (out / 'build-info.txt').write_text('existing build identity\n')
            with contextlib.redirect_stdout(output):
                exec(calls, local)
                try:
                    exec(gate, dict(local, IMG=image, FINAL=final, OUT=out, before='fixture-sha',
                                    sha256=lambda p: 'fixture-sha', QMP_DIR=out / 'qmp', shutil=shutil, sys=sys))
                except SystemExit as e:
                    self.assertEqual(e.code, 1)
            renamed = final.exists()
            self.assertEqual(image.exists(), not renamed)
            self.assertEqual((out / 'test.img.sha256').exists(), renamed)
            if renamed:
                self.assertEqual((out / 'test.img.sha256').read_text(), 'fixture-sha  test.img\n')
                self.assertEqual(final.read_bytes(), b'image fixture')
            info = (out / 'build-info.txt').read_text()
            self.assertTrue(info.startswith('existing build identity\n'))
            return renamed, output.getvalue(), info

    def test_no_gpu_renames_with_explicit_non_acceptance(self):
        for prop, pid in (('', ''), ('0', '456'), ('1', '456')):
            with self.subTest(property=prop, pid=pid):
                log = GOOD + f'gbos-qemu-probe: {TOKEN} boot_completed={prop} system_server_pid={pid}\n'
                renamed, output, info = self.finish(log, False)
                self.assertTrue(renamed)
                for name in (BOOT, STABLE):
                    line = f'NOT_RUN {name} (QEMU has no GPU'
                    self.assertIn(line, output)
                    self.assertIn(line, info)
                    for status in ('PASS', 'FAIL'):
                        self.assertNotIn(f'{status} {name}', output)

    def test_gpu_requires_completion_and_stability(self):
        for log, accepted in ((GOOD, True),
                              (GOOD.replace('boot_completed=1', 'boot_completed=0'), False),
                              (GOOD.replace('boot_completed=1', 'boot_completed='), False),
                              (GOOD + GOOD.replace('pid=123', 'pid=456'), False)):
            with self.subTest(log=log):
                renamed, output, info = self.finish(log, True)
                self.assertEqual(renamed, accepted)
                self.assertNotIn('NOT_RUN', output + info)
                self.assertIn(f'{"PASS" if accepted else "FAIL"} ', output)

    def test_fatal_and_other_checks_gate_both_configurations(self):
        for gop in (False, True):
            with self.subTest(gop=gop):
                renamed, output, _ = self.finish(GOOD + 'FATAL EXCEPTION IN SYSTEM PROCESS: main\n', gop)
                self.assertFalse(renamed)
                self.assertIn('FAIL no FATAL EXCEPTION IN SYSTEM PROCESS', output)
                self.assertFalse(self.finish(GOOD, gop, other_checks={'other mandatory check': False})[0])

    def test_probe_freshness_and_parse_safety_gate_both_configurations(self):
        for gop in (False, True):
            for log, age in ((GOOD, None), (GOOD, -1), (GOOD, 16), ('', 5),
                             (GOOD.replace(TOKEN, 'wrong-nonce'), 5),
                             (GOOD + f'gbos-qemu-probe: {TOKEN} BROKEN\n', 5),
                             (GOOD + f'gbos-qemu-probe: {TOKEN} boot_completed=1', 5),
                             (GOOD + f'gbos-qemu-probe: {TOKEN} boot_completed=1 system_server_pid=', 5),
                             (GOOD.rstrip('\n'), 5),
                             (GOOD.replace('pid=123', 'pid=123 456'), 5),
                             (GOOD.replace('boot_completed=1', 'boot_completed=10'), 5)):
                with self.subTest(gop=gop, log=log, age=age):
                    renamed, output, _ = self.finish(log, gop, age)
                    self.assertFalse(renamed)
                    self.assertIn('FAIL fresh well-formed device probe', output)

    def test_profile_configuration_matches_qemu_display(self):
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / 'builder/lib'))
        import devprofile
        assignments = [n for n in tree.body if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id in {'GENERIC', 'AMD_HX370', 'GOP'} for t in n.targets)]
        for profile, gop in (('kano', False), ('generic-x86', True), ('amd-hx370', True)):
            config = {'PROF': devprofile.load(root / 'profiles' / profile)}
            exec(compile(ast.Module(assignments, []), 'builder/image.py', 'exec'), config)
            self.assertEqual(config['GOP'], gop)
            self.assertEqual(bool(ns['qemu_framework_checks'](GOOD, TOKEN, 5, config['GOP'])[1]), not gop)
        self.assertIn('"-vga", "none"', source)
        self.assertIn('*(["-device", "bochs-display,id=gfx,romfile=,bus=pcie.0,addr=0x5"] if GOP else [])', source)

    def test_build_summary_surfaces_not_run_and_alignment_stays_mandatory(self):
        build = (Path(__file__).resolve().parents[2] / 'builder/build.sh').read_text()
        self.assertIn("'^(PASS|FAIL|NOT_RUN) '", build)
        self.assertIn("'^(PASS|FAIL|SKIP|NOT_RUN) ", build)
        self.assertIn('|| { cat "$GBOS_OUT/container-alignment.log"; die "APK/APEX alignment gate failed"; }', build)
        self.assertLess(build.index('"$HERE/lib/container_alignment.py"'), build.index('python3 "$HERE/image.py"'))

    def test_device_property_response_only(self):
        check = lambda log, token: ns['serial_boot_completed'](log, token, 5)
        token = 'GBOS_BOOT_123'
        self.assertTrue(check('kernel: gbos-qemu-probe: GBOS_BOOT_123 boot_completed=1 system_server_pid=123\r\n', token))
        for sample in ('sys.boot_completed=1',
                       "printf 'GBOS_BOOT_123:%s\\n' \"$(/system/bin/getprop sys.boot_completed)\"\n",
                       'gbos-qemu-probe: GBOS_BOOT_123 boot_completed=0 system_server_pid=123\n',
                       'gbos-qemu-probe: GBOS_BOOT_123 boot_completed=10 system_server_pid=123\n',
                       'gbos-qemu-probe: GBOS_BOOT_other boot_completed=1 system_server_pid=123\n',
                       'gbos-qemu-probe: GBOS_BOOT_123 boot_completed=1 system_server_pid=\n'):
            self.assertFalse(check(sample, token), sample)
        good = 'gbos-qemu-probe: GBOS_BOOT_123 boot_completed=1 system_server_pid=123\n'
        self.assertFalse(ns['serial_boot_completed'](good, token, None))
        self.assertFalse(ns['serial_boot_completed'](good, token, 16))
        self.assertFalse(check(good + 'gbos-qemu-probe: GBOS_BOOT_123 BROKEN\n', token))
        self.assertFalse(check(good + 'gbos-qemu-probe: GBOS_BOOT_123 boot_completed=1', token))
        self.assertFalse(check(good + 'gbos-qemu-probe: GBOS_BOOT_123 boot_completed=1 system_server_pid=\n', token))
        self.assertFalse(check(good + 'gbos-qemu-probe: GBOS_BOOT_123 boot_completed=1 system_server_pid=123 456\n', token))

    def test_restarts_and_fatal_are_rejected(self):
        check = lambda log: ns['framework_log_checks'](log, 'GBOS_BOOT_123')
        one = 'servicemanager: Caller(pid=123,uid=1000,sid=u:r:system_server:s0) request\n'
        other = 'servicemanager: Caller(pid=456,uid=1000,sid=u:r:system_server:s0) request\n'
        self.assertTrue(all(check(one + one).values()))
        self.assertFalse(check(one + other)['system_server did not restart repeatedly'])
        self.assertFalse(check('gbos-qemu-probe: GBOS_BOOT_123 boot_completed=1 system_server_pid=123\n'
                               'gbos-qemu-probe: GBOS_BOOT_123 boot_completed=1 system_server_pid=456\n')
                         ['system_server did not restart repeatedly'])
        self.assertFalse(check(one + 'FATAL EXCEPTION IN SYSTEM PROCESS: main')['no FATAL EXCEPTION IN SYSTEM PROCESS'])
        deaths = ('init: Untracked process (pid: 123 name: (system_server) ppid: 1 pgrp: 1 state: Z) received SIGKILL\n'
                  'init: Untracked process (pid: 456 name: (system_server) ppid: 1 pgrp: 1 state: Z) received SIGKILL\n')
        self.assertFalse(check(deaths)['system_server did not restart repeatedly'])

if __name__ == '__main__':
    unittest.main()
