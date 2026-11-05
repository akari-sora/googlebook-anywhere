#!/usr/bin/env python3
"""Offline tests for the QEMU chain write evidence gate."""
import copy
import ast
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'builder' / 'lib'))
from qemu_write_trace import nvme_trace_safe, userdata_write_bytes

SIZE = 64 << 20
START, END = 16 << 20, 48 << 20
REQ = 16 << 20
DEV = {'device': 'usbdisk', 'qdev': '/machine/peripheral/device[0]/scsi.0/legacy[0]',
       'stats': {'wr_bytes': REQ, 'wr_operations': 1, 'failed_wr_operations': 0, 'invalid_wr_operations': 0}}

def request(off, size, blk='0xabc', bs='0xdef'):
    assert off % 512 == size % 512 == 0
    return (f'scsi_disk_dma_command_WRITE Write (sector {off // 512}, count {size // 512})\n'
            f'blk_co_pwritev blk {blk} bs {bs} offset {off} bytes {size} flags 0x0\n')

class TraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'trace'
        self.trace = request(START, REQ)
        self.stats = [copy.deepcopy(DEV)]
    def verify(self):
        self.path.write_text(self.trace)
        return userdata_write_bytes(self.path, self.stats, START, END, SIZE)
    def test_matches_stats_and_ignores_unrelated_pflash(self):
        self.trace = 'blk_co_pwritev blk 0x111 bs 0x222 offset 0 bytes 512 flags 0x0\n' + self.trace
        self.assertEqual(self.verify(), REQ)
    def test_end_of_disk_write_does_not_reject_real_userdata_write(self):
        self.trace += request(SIZE - 512, 512)
        self.stats[0]['stats'].update(wr_bytes=REQ + 512, wr_operations=2)
        self.assertEqual(self.verify(), REQ)
    def test_nonuserdata_bytes_cannot_satisfy_threshold(self):
        self.trace = request(END, REQ)
        self.assertEqual(self.verify(), 0)
    def test_intersection_counts_only_portion_inside_userdata(self):
        self.trace = request(START - 512, 1024)
        self.stats[0]['stats'].update(wr_bytes=1024, wr_operations=1)
        self.assertEqual(self.verify(), 512)
    def test_rejects_missing_or_truncated_trace(self):
        self.path.unlink(missing_ok=True)
        with self.assertRaises(ValueError): userdata_write_bytes(self.path, self.stats, START, END, SIZE)
        for trace in ('', self.trace[:-1], self.trace + 'blk_co_pwritev blk 0x'):
            self.trace = trace
            with self.subTest(trace=trace), self.assertRaises(ValueError): self.verify()
    def test_rejects_ambiguous_or_additional_scsi_device(self):
        self.trace += request(START, REQ, '0x123', '0x456')
        with self.assertRaises(ValueError): self.verify()
        self.trace = request(START, REQ)
        self.stats.append({'device':'other','qdev':'/machine/peripheral/x/scsi.0/legacy[0]', 'stats':{}})
        with self.assertRaises(ValueError): self.verify()
    def test_rejects_failed_invalid_and_unmatched_completed_stats(self):
        for field, value in [('wr_bytes',REQ+512), ('wr_operations',2), ('failed_wr_operations',1), ('invalid_wr_operations',1)]:
            self.stats = [copy.deepcopy(DEV)]; self.stats[0]['stats'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.verify()
    def test_rejects_duplicate_or_missing_usb_and_invalid_geometry(self):
        self.stats = [copy.deepcopy(DEV), copy.deepcopy(DEV)]
        with self.assertRaises(ValueError): self.verify()
        self.stats = []
        with self.assertRaises(ValueError): self.verify()
        self.stats = [copy.deepcopy(DEV)]
        self.path.write_text(self.trace)
        with self.assertRaises(ValueError): userdata_write_bytes(self.path, self.stats, END, START, SIZE)
    def test_rejects_unknown_event_and_out_of_bounds_io(self):
        self.trace += 'unknown_event x\n'
        with self.assertRaises(ValueError): self.verify()
        self.trace = request(SIZE - 512, 1024)
        with self.assertRaises(ValueError): self.verify()
    def test_rejects_unattributed_or_post_snapshot_scsi_request(self):
        self.trace += 'scsi_disk_dma_command_WRITE Write (sector 0, count 1)\n'
        with self.assertRaises(ValueError): self.verify()
    def test_rejects_changed_node_for_usb_block_pointer(self):
        self.trace += 'blk_co_pwritev blk 0xabc bs 0x124 offset 0 bytes 512 flags 0x0\n'
        with self.assertRaises(ValueError): self.verify()
    def test_nvme_read_and_flush_allowed_but_write_commands_rejected(self):
        for opcode in (0, 2):
            self.trace = request(START, REQ) + f"pci_nvme_io_cmd cid 1 nsid 0x1 sqid 1 opc 0x{opcode:x} opname 'test'\n"
            self.assertEqual(self.verify(), REQ)
        for opcode in (1, 8, 9, 0xff):
            self.trace = request(START, REQ) + f"pci_nvme_io_cmd cid 1 nsid 0x1 sqid 1 opc 0x{opcode:x} opname 'test'\n"
            with self.subTest(opcode=opcode), self.assertRaises(ValueError): self.verify()
    def test_nvme_admin_format_and_unknown_rejected(self):
        self.trace += "pci_nvme_admin_cmd cid 1 sqid 0 opc 0x6 opname 'identify'\n"
        self.assertEqual(self.verify(), REQ)
        for opcode in (0x80, 0x84, 0xff):
            self.trace = request(START, REQ) + f"pci_nvme_admin_cmd cid 1 sqid 0 opc 0x{opcode:x} opname 'test'\n"
            with self.subTest(opcode=opcode), self.assertRaises(ValueError): self.verify()

class ChainIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.qmp = self.out / 'qmp'; self.qmp.mkdir()
        self.image = self.out / 'input.img'
        with self.image.open('wb') as f: f.truncate(SIZE)
        self.vars = self.out / 'vars-template'; self.vars.write_bytes(b'vars')
        self.trace = request(START, REQ)
        self.stats = [copy.deepcopy(DEV), {'device':'nv','stats':{'wr_bytes':0,'wr_operations':0,
                      'failed_wr_operations':0,'invalid_wr_operations':0}}]
        self.calls = []
        self.stop_ok = True
        self.late_write = False
        self.late_other_backend = False
        self.emit_trace = True
        self.incomplete_polls = 0
        source = (Path(__file__).resolve().parents[2] / 'builder/image.py').read_text()
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'qemu')
        this = self
        class Proc:
            def __init__(self, args, env):
                self.running = True; self.returncode = None
                self.tag = 'functional' if any('functional-serial.log' in arg for arg in args) else 'chain'
                self.trace_path = this.out / f'{self.tag}-write.trace'
                if this.emit_trace: self.trace_path.write_text(this.trace)
                (this.out / f'{self.tag}-serial.log').write_text('Boot in 1s.\n')
            def poll(self): return None if self.running else self.returncode
            def terminate(self):
                if this.late_write:
                    with self.trace_path.open('a') as f: f.write(request(END,512))
                if this.late_other_backend:
                    with self.trace_path.open('a') as f:
                        f.write('blk_co_pwritev blk 0x333 bs 0x444 offset 0 bytes 512 flags 0x0\n')
                self.running = False; self.returncode = -15
            def wait(self, *args): return self.returncode
            def kill(self): self.terminate()
        def qmp(_path, *cmds):
            self.calls.extend(cmds)
            results=[]
            for c in cmds:
                if c == 'stop': results.append({'return':{}} if self.stop_ok else {'error':{}})
                else:
                    stats=copy.deepcopy(self.stats)
                    if self.incomplete_polls:
                        stats[0]['stats']['wr_bytes'] -= 512
                        self.incomplete_polls -= 1
                    results.append({'return':stats})
            return results
        self.ns = {'OUT':self.out, 'QMP_DIR':self.qmp, 'OVMF_VARS':str(self.vars), 'OVMF_CODE':'code',
                   'QEMU':'qemu', 'GOP':False, 'IMG':self.image, 'blank':self.out/'blank', 'QEMU_LIB':'',
                   'QEMU_SECONDS':0, 'USB_SMOKE':True, 'USERDATA':{'start':START,'end':END},
                   'shutil':shutil, 'os':os, 'subprocess':type('FakeSubprocess',(),{'Popen':Proc,
                       'TimeoutExpired':subprocess.TimeoutExpired}), 'time':time, 'userdata_write_bytes':userdata_write_bytes,
                   'nvme_trace_safe':nvme_trace_safe,
                   'qmp_query':qmp}
        exec(compile(ast.Module([node], []), 'builder/image.py', 'exec'), self.ns)
    def run_chain(self):
        with contextlib.redirect_stdout(io.StringIO()): return self.ns['qemu']('chain', [])
    def run_functional(self):
        with contextlib.redirect_stdout(io.StringIO()): return self.ns['qemu']('functional', [])
    def test_chain_pauses_then_reconciles_and_preserves_nvme_gate(self):
        alive, stats, log, _ = self.run_chain()
        self.assertTrue(alive)
        self.assertEqual(self.calls, ['stop','query-blockstats'])
        self.assertEqual(stats['chain_userdata_write_bytes'],REQ)
        self.assertEqual(stats['usb_smoke_nvme_wr_bytes'],0)
        self.assertIn('Boot in', log)
    def test_late_write_after_qmp_snapshot_is_refused(self):
        self.late_write=True
        _, stats, _, _ = self.run_chain()
        self.assertNotIn('chain_userdata_write_bytes',stats)
    def test_late_other_backend_write_cannot_pass_nvme_gate(self):
        self.late_other_backend = True
        _, stats, _, _ = self.run_chain()
        self.assertIsNone(stats['usb_smoke_nvme_wr_bytes'])
        self.assertNotIn('chain_userdata_write_bytes', stats)
    def test_functional_requires_complete_trace_after_qmp_stop(self):
        _, stats, _, _ = self.run_functional()
        self.assertEqual(self.calls, ['stop', 'query-blockstats'])
        self.assertEqual(stats['usb_smoke_nvme_wr_bytes'], 0)
    def test_functional_late_other_backend_write_cannot_pass_nvme_gate(self):
        self.late_other_backend = True
        _, stats, _, _ = self.run_functional()
        self.assertIsNone(stats['usb_smoke_nvme_wr_bytes'])
    def test_functional_pending_nvme_write_cannot_pass_zero_snapshot(self):
        self.trace += "pci_nvme_io_cmd cid 1 nsid 0x1 sqid 1 opc 0x1 opname 'write'\n"
        _, stats, _, _ = self.run_functional()
        self.assertIsNone(stats['usb_smoke_nvme_wr_bytes'])
    def test_functional_missing_trace_fails_closed(self):
        self.emit_trace = False
        _, stats, _, _ = self.run_functional()
        self.assertIsNone(stats)
    def test_outstanding_nvme_write_cannot_pass_zero_snapshot(self):
        self.trace += "pci_nvme_io_cmd cid 1 nsid 0x1 sqid 1 opc 0x1 opname 'write'\n"
        class Clock:
            now=0
            def monotonic(self): self.now+=1; return self.now
            def sleep(self, _): pass
        self.ns['time'] = Clock()
        _, stats, _, _ = self.run_chain()
        self.assertIsNone(stats['usb_smoke_nvme_wr_bytes'])
        self.assertNotIn('chain_userdata_write_bytes', stats)
    def test_qmp_stop_failure_cannot_report_trace_success(self):
        self.stop_ok=False
        _, stats, _, _=self.run_chain()
        self.assertIsNone(stats)
    def test_nvme_failed_write_cannot_pass_no_write_gate(self):
        self.stats[1]['stats']['failed_wr_operations']=1
        _, stats, _, _=self.run_chain()
        self.assertIsNone(stats['usb_smoke_nvme_wr_bytes'])
    def test_waits_for_inflight_io_to_match_completed_stats(self):
        self.incomplete_polls=1
        _, stats, _, _=self.run_chain()
        self.assertEqual(self.calls, ['stop','query-blockstats','query-blockstats'])
        self.assertEqual(stats['chain_userdata_write_bytes'],REQ)
    def test_missing_trace_is_not_replaced_by_whole_disk_stats(self):
        self.emit_trace=False
        class Clock:
            now=0
            def monotonic(self): self.now+=1; return self.now
            def sleep(self, _): pass
        self.ns['time']=Clock()
        _, stats, _, _=self.run_chain()
        self.assertNotIn('chain_userdata_write_bytes',stats)
    def test_production_gate_requires_verified_userdata_threshold(self):
        src=(Path(__file__).resolve().parents[2] / 'builder/image.py').read_text()
        begin=src.index('if DEFAULT_ENTRY == "lapis-quiet.conf":\n    alive, stats, clog, _probe_age = qemu("chain", [])')
        end=src.index('    template = Path(OVMF_VARS)',begin)
        for actual, expected in ((None,False),(REQ-1,False),(REQ,True)):
            checks={}
            def fake_qemu(*_): return True, {'wr_bytes':1 << 30, 'wr_highest_offset':SIZE,
                                             'chain_userdata_write_bytes':actual}, 'Boot in', None
            scope={'DEFAULT_ENTRY':'lapis-quiet.conf','qemu':fake_qemu,'checks':checks,'USB_SMOKE':False,
                   'GOP':False,'OUT':self.out,'USERDATA':{'start':START,'end':END}}
            with contextlib.redirect_stdout(io.StringIO()): exec(src[begin:end],scope)
            self.assertEqual(checks['chain: Android wrote /data (>= 16 MiB, into userdata)'],expected)

if __name__ == '__main__': unittest.main()
