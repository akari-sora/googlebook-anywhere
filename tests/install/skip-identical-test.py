import hashlib
import types
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
source=(ROOT/'install/write-usb.sh').read_text()
begin='# >>> write-usb.py (embedded in install/write-usb.sh; tests/install/run.sh extracts the lines between these markers)'
end='# <<< write-usb.py'
assert source.count(begin)==1 and source.count(end)==1
embedded=source.split(begin,1)[1].split(end,1)[0].lstrip('\n')
w=types.ModuleType('writer')
exec(compile(embedded,str(ROOT/'install/write-usb.sh'), 'exec'),w.__dict__)


class ShellOptionTests(unittest.TestCase):
    def test_refuses_skip_without_nogrow_or_with_keep_data(self):
        script=ROOT/'install/write-usb.sh'
        for args in (['--skip-identical'],['--skip-identical','--keep-data'],['--skip-identical','--no-grow','--keep-data']):
            with self.subTest(args=args):
                result=subprocess.run(['bash',str(script),*args,'/nonexistent-image','/nonexistent-target'],capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,2)
                self.assertTrue('skip-identical' in result.stdout or '--no-grow' in result.stdout)
    def test_extracted_module_is_exactly_the_embedded_source(self):
        self.assertEqual(source.count(begin),1)
        self.assertEqual(source.count(end),1)
        self.assertTrue(callable(w.main))


class CompareWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.src=Path(self.tmp.name)/'source';self.dst=Path(self.tmp.name)/'target'
        self.src.write_bytes(b'abcdEFGHijkl');self.dst.write_bytes(b'abcdxxxxijkl')
        self.ifd=os.open(self.src,os.O_RDONLY);self.fd=os.open(self.dst,os.O_RDWR)
        self.addCleanup(os.close,self.ifd);self.addCleanup(os.close,self.fd)
        w.started=False;w.gpt_phase=False
        self.chunk=mock.patch.object(w,'CHUNK',4);self.chunk.start();self.addCleanup(self.chunk.stop)
        self.sync=mock.patch.object(w,'SYNC_EVERY',4);self.sync.start();self.addCleanup(self.sync.stop)
    def copy(self,sha=None):
        w.copy_skipping(self.ifd,self.fd,12,sha or hashlib.sha256(self.src.read_bytes()).hexdigest(),'fixture')
    def test_only_changed_chunk_is_written_and_fsynced(self):
        real=os.pwrite
        with mock.patch.object(w.os,'pwrite',wraps=real) as write, mock.patch.object(w.os,'fsync',wraps=os.fsync) as flush:
            self.copy()
        self.assertEqual(write.call_count,1);self.assertEqual(write.call_args.args[1:],(b'EFGH',4))
        self.assertEqual(self.dst.read_bytes(),self.src.read_bytes());self.assertEqual(flush.call_count,3)
    def test_all_identical_has_no_writes_but_is_flushed(self):
        self.dst.write_bytes(self.src.read_bytes())
        with mock.patch.object(w.os,'pwrite') as write, mock.patch.object(w.os,'fsync',wraps=os.fsync) as flush:
            self.copy()
        write.assert_not_called();self.assertFalse(w.started);self.assertGreater(flush.call_count,0)
    def test_short_source_or_target_reads_fail_closed(self):
        for func,returned in (('read',b'a'),('pread',b'a')):
            with self.subTest(func=func),mock.patch.object(w.os,func,return_value=returned),mock.patch.object(w.os,'pwrite') as write:
                os.lseek(self.ifd,0,os.SEEK_SET);w.started=False
                with self.assertRaises(w.Stop): self.copy()
                write.assert_not_called();self.assertFalse(w.started)
    def test_read_error_fails_without_writing(self):
        with mock.patch.object(w.os,'pread',side_effect=OSError(5,'fake I/O error')),mock.patch.object(w.os,'pwrite') as write:
            with self.assertRaises(w.Stop): self.copy()
            write.assert_not_called();self.assertFalse(w.started)
    def test_partial_write_is_failure_after_possible_modification(self):
        orig=os.pwrite
        def partial(fd,data,offset):return orig(fd,data[:1],offset)
        with mock.patch.object(w.os,'pwrite',side_effect=partial):
            with self.assertRaises(w.Stop): self.copy()
        self.assertTrue(w.started);self.assertEqual(w.status(),1)
    def test_interrupt_is_failure_after_write_may_have_started(self):
        with mock.patch.object(w.os,'pwrite',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.copy()
        self.assertTrue(w.started);self.assertEqual(w.status(),1)
    def test_wrong_source_hash_refuses_even_if_target_identical(self):
        self.dst.write_bytes(self.src.read_bytes())
        with mock.patch.object(w.os,'pwrite') as write:
            with self.assertRaises(w.Stop): self.copy('0'*64)
        write.assert_not_called();self.assertFalse(w.started)
    def test_source_changed_during_copy_fails_hash(self):
        expected=hashlib.sha256(self.src.read_bytes()).hexdigest()
        real=os.read
        calls=0
        def mutate(fd,n):
            nonlocal calls
            data=real(fd,n);calls+=1
            if calls==1:
                with self.src.open('r+b') as f:f.seek(4);f.write(b'ZZZZ')
            return data
        with mock.patch.object(w.os,'read',side_effect=mutate):
            with self.assertRaises(w.Stop): self.copy(expected)
        self.assertTrue(w.started)

    def test_verify_detects_readback_mismatch_using_owned_fd(self):
        with mock.patch.object(w,'drop_owned_cache') as drop, mock.patch.object(w,'open_uncached',side_effect=AssertionError('reopened')):
            with self.assertRaises(w.Stop): w.verify(self.ifd,self.fd,'fixture',0,12,hashlib.sha256(self.src.read_bytes()).hexdigest(),owned=True)
        drop.assert_called_once_with(self.fd,'fixture')
    def test_cache_invalidation_failure_refuses(self):
        with mock.patch.object(w.fcntl,'ioctl',side_effect=OSError(5,'fake')),mock.patch.object(w.os,'posix_fadvise',side_effect=OSError(5,'fake'),create=True):
            with self.assertRaises(w.Stop):w.drop_owned_cache(self.fd,'fixture')


class WriterFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.src=Path(self.tmp.name)/'source';self.dst=Path(self.tmp.name)/'target'
        self.src.write_bytes(b'a'*512);self.dst.write_bytes(b'b'*512)
        self.sha=hashlib.sha256(self.src.read_bytes()).hexdigest();w.started=False;w.gpt_phase=False
    def flow(self,mode='skip-identical',size='512',grow='nogrow'):
        opened=[]
        def open_dev(path,flags,rdev):
            opened.append(flags);fd=os.open(path,os.O_RDWR if flags & os.O_RDWR else os.O_WRONLY);return fd
        with mock.patch.object(w,'check_node'),mock.patch.object(w,'open_dev',side_effect=open_dev), \
             mock.patch.object(w.os.path,'realpath',return_value='fixture-sysfs'), \
             mock.patch.object(w.fcntl,'ioctl',return_value=struct.pack('i',512)), \
             mock.patch.object(w,'gpt_plan',return_value={'n':1,'img_n':1,'grow':None}), \
             mock.patch.object(w,'gpt_write') as gpt, mock.patch.object(w,'gpt_check') as check, \
             mock.patch.object(w,'drop_owned_cache') as drop, \
             mock.patch.object(w,'open_uncached',side_effect=AssertionError('target was reopened')):
            w.write(str(self.src),self.sha,str(self.dst),'1:2',size,'fixture-sysfs',grow,mode)
        return opened,gpt,check,drop
    def test_skip_uses_only_one_exclusive_read_write_fd(self):
        opened,gpt,check,drop=self.flow()
        self.assertEqual(opened,[os.O_RDWR|os.O_EXCL]);self.assertEqual(self.dst.read_bytes(),self.src.read_bytes())
        self.assertTrue(gpt.called);self.assertEqual(check.call_args.kwargs,{'strict':True});self.assertEqual(drop.call_count,2)
    def test_skip_refuses_grow_and_unequal_size_before_target_open(self):
        for size,grow in (('1024','nogrow'),('512','grow')):
            with self.subTest(size=size,grow=grow),mock.patch.object(w,'open_dev') as opened,mock.patch.object(w,'check_node') as node:
                with self.assertRaises(w.Stop):
                    w.write(str(self.src),self.sha,str(self.dst),'1:2',size,'fixture-sysfs',grow,'skip-identical')
                opened.assert_not_called();node.assert_not_called()
    def test_target_size_mismatch_refuses_before_any_write(self):
        self.dst.write_bytes(b'b'*1024)
        with mock.patch.object(w,'compare_write_exact') as write:
            with self.assertRaises(w.Stop):self.flow()
            write.assert_not_called();self.assertFalse(w.started)
    def test_target_identity_change_refuses_before_open_or_write(self):
        with mock.patch.object(w,'gpt_plan',return_value={'n':1,'img_n':1,'grow':None}), \
             mock.patch.object(w,'check_node',side_effect=w.Stop('target identity changed')), \
             mock.patch.object(w,'open_dev') as opened, mock.patch.object(w.os,'pwrite') as write:
            with self.assertRaises(w.Stop):
                w.write(str(self.src),self.sha,str(self.dst),'1:2','512','fixture-sysfs','nogrow','skip-identical')
            opened.assert_not_called();write.assert_not_called();self.assertFalse(w.started)
    def test_equal_size_gpt_step_never_writes(self):
        with mock.patch.object(w.os,'pwrite') as write:
            w.gpt_write(-1,{'n':1,'img_n':1},'fixture')
            write.assert_not_called()

    def test_default_path_keeps_write_only_and_original_helpers(self):
        fake_rfd=os.open(self.dst,os.O_RDONLY)
        try:
            with mock.patch.object(w,'check_node'),mock.patch.object(w,'open_dev',return_value=os.open(self.dst,os.O_WRONLY)) as opened, \
                 mock.patch.object(w.os.path,'realpath',return_value='fixture-sysfs'), \
                 mock.patch.object(w.fcntl,'ioctl',return_value=struct.pack('i',512)), \
                 mock.patch.object(w,'gpt_plan',return_value={'n':1,'img_n':1,'grow':None}), \
                 mock.patch.object(w,'copy') as copy,mock.patch.object(w,'copy_skipping') as skip, \
                 mock.patch.object(w,'verify') as verify,mock.patch.object(w,'gpt_write'), \
                 mock.patch.object(w,'open_uncached',return_value=fake_rfd) as reopen,mock.patch.object(w,'gpt_check'):
                w.write(str(self.src),self.sha,str(self.dst),'1:2','512','fixture-sysfs')
            self.assertEqual(opened.call_args.args[1],os.O_WRONLY|os.O_EXCL)
            copy.assert_called_once();skip.assert_not_called();verify.assert_called_once();reopen.assert_called_once()
        finally:
            try:os.close(fake_rfd)
            except OSError:pass
    def test_probe_and_check_only_gpt_do_not_open_writable_target(self):
        with mock.patch.object(w,'check_node'),mock.patch.object(w,'open_dev',return_value=os.open(self.dst,os.O_RDONLY)) as target, \
             mock.patch.object(w.os,'pwrite') as write:
            self.assertEqual(w.main(['probe',str(self.dst),'1:2']),0)
            self.assertEqual(target.call_args.args[1],os.O_RDONLY|os.O_EXCL);write.assert_not_called()
        with mock.patch.object(w,'gpt_plan',return_value={'img_n':1,'n':1,'grow':None}), \
             mock.patch.object(w,'open_dev') as target,mock.patch.object(w.os,'pwrite') as write:
            self.assertEqual(w.main(['gptcheck',str(self.src),'512','nogrow']),0)
            target.assert_not_called();write.assert_not_called()


if __name__=='__main__':unittest.main()
