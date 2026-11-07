#!/usr/bin/env python3
import ast, pathlib, unittest, os, json, re, tempfile, types, contextlib, io
root=pathlib.Path(__file__).resolve().parents[2]
tree=ast.parse((root/'builder/image.py').read_text())
node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='functional_wake_check')
ns={'qmp_query':None};exec(compile(ast.Module(body=[node],type_ignores=[]),'image.py','exec'),ns);check=ns['functional_wake_check']
class Wake(unittest.TestCase):
 def invoke(self,replies):
  calls=[]
  def query(path,cmd,**kwargs):
   calls.append((cmd,kwargs));r=replies.pop(0)
   if isinstance(r,Exception):raise r
   return [r]
  result=check('/socket',query);return result,calls
 def test_running_no_wake(self):
  r,c=self.invoke([{'return':{'status':'running','running':True}}]);self.assertEqual(r,(True,False));self.assertEqual([x[0] for x in c],['query-status'])
 def test_suspended_wakes(self):
  r,c=self.invoke([{'return':{'status':'suspended','running':False}},{'return':{'wakeup-suspend-support':True}},{'return':{}}]);self.assertEqual(r,(True,True));self.assertEqual([x[0] for x in c],['query-status','query-current-machine','system_wakeup']);self.assertTrue(all(x[1]=={'timeout':2} for x in c))
 def test_refuse_ambiguous_and_failed(self):
  suspended={'return':{'status':'suspended','running':False}}
  for replies in ([{'return':{'status':'paused','running':False}}],[{'return':{'status':'running','running':False}}],[{'error':{}}],[OSError('disconnect')],[suspended,{'return':{'wakeup-suspend-support':False}}],[suspended,{'return':{'wakeup-suspend-support':True}},{'error':{}}]):
   with self.subTest(replies=replies):self.assertEqual(self.invoke(list(replies))[0],(False,False))
 def test_qmp_query_closes_on_failure(self):
  fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='qmp_query')
  class Stream:
   closed=False
   def readline(self): raise OSError('disconnected')
   def close(self): self.closed=True
  class Sock:
   closed=False
   def __init__(self):self.stream=Stream()
   def settimeout(self,x):pass
   def connect(self,x):pass
   def makefile(self,x):return self.stream
   def close(self):self.closed=True
  sock=Sock(); env={'socket':types.SimpleNamespace(socket=lambda *a:sock,AF_UNIX=1),'json':json}
  exec(compile(ast.Module(body=[fn],type_ignores=[]),'image.py','exec'),env)
  with self.assertRaises(OSError):env['qmp_query']('/socket','query-status')
  self.assertTrue(sock.closed);self.assertTrue(sock.stream.closed)
 def test_functional_loop_latches_failure_and_chain_never_wakes(self):
  names={'qemu','serial_device_probe','serial_boot_completed','framework_log_checks','qemu_framework_checks'}
  nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
  class Clock:
   now=0
   def monotonic(self):return self.now
   def sleep(self,n):self.now+=n
  class Process:
   returncode=0
   def poll(self):return None
   def terminate(self):pass
   def wait(self,*a):return 0
  for outcomes in ([(False,False),(True,False),(True,False)],[(True,False),(False,False),(True,False)]):
   with self.subTest(outcomes=outcomes),tempfile.TemporaryDirectory() as t:
    out=pathlib.Path(t); clock=Clock();calls=[];remaining=list(outcomes);token='ABC'
    def wake(path):calls.append(path);return remaining.pop(0)
    def popen(*args,**kwargs):
     (out/'functional-serial.log').write_text('gbos-qemu-probe: ABC boot_completed=1 system_server_pid=123\n')
     return Process()
    def query(*args,**kwargs):raise OSError('end readback unavailable')
    env={'OUT':out,'QMP_DIR':out,'shutil':types.SimpleNamespace(copy=lambda *a:None),'OVMF_VARS':'vars','OVMF_CODE':'code','QEMU':'qemu','GOP':True,'IMG':out/'image','blank':out/'nvme','USB_SMOKE':False,'os':os,'QEMU_LIB':None,'subprocess':types.SimpleNamespace(Popen=popen,TimeoutExpired=TimeoutError),'time':clock,'QEMU_SECONDS':6,'functional_wake_check':wake,'qmp_query':query,'re':re}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'image.py','exec'),env)
    with contextlib.redirect_stdout(io.StringIO()):alive,_,log,age=env['qemu']('functional',[],token)
    self.assertTrue(alive);self.assertIs(env['FUNCTIONAL_QMP_OK'],False);self.assertEqual(len(calls),3)
    checks,_=env['qemu_framework_checks'](log,token,age,True)
    self.assertTrue(checks['fresh well-formed device probe']);self.assertTrue(checks['device getprop sys.boot_completed is 1'])
    self.assertFalse(env['serial_boot_completed'](log,token,16))
    clock.now=0;calls.clear()
    with contextlib.redirect_stdout(io.StringIO()):env['qemu']('chain',[])
    self.assertEqual(calls,[])
if __name__=='__main__':unittest.main()
