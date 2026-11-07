import ast, hashlib, pathlib, subprocess, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import patch
SOURCE=pathlib.Path(__file__).parents[2]/'builder/image.py'
class Tests(unittest.TestCase):
 def setUp(self):
  tree=ast.parse(SOURCE.read_text()); fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='ally_storage_module')
  self.env={'Path':pathlib.Path,'hashlib':hashlib,'subprocess':subprocess,'sys':__import__('sys'),'__file__':str(SOURCE)};exec(compile(ast.Module(body=[fn],type_ignores=[]),str(SOURCE),'exec'),self.env)
  self.f=self.env['ally_storage_module']; self.cfg={'CONFIG_USB_STORAGE':'m','CONFIG_SCSI':'y','CONFIG_BLK_DEV_SD':'y'};self.prof={'model':{'id':'rog-ally'}}
 def test_real_initrd_loop_inserts_module_and_guards_duplicates(self):
  from types import SimpleNamespace
  def entry(name,data):
   name=name.encode()+b"\0"; fields=[1,0o100644,0,0,1,0,len(data),0,0,0,0,len(name),0]
   header=b"070701"+b"".join(("%08X"%x).encode() for x in fields)
   chunk=header+name;chunk+=b"\0"*(-len(chunk)%4);chunk+=data;return chunk+b"\0"*(-len(chunk)%4)
  text=SOURCE.read_text();start=text.index('out = bytearray(); off = 0;');end=text.index('(OUT / "initrd.img").write_bytes',start)
  loop=text[start:end]
  base=entry('lib/modules/modules.load',b'existing.ko\n')+entry('lib/modules/modules.dep',b'/lib/modules/existing.ko:\n')
  def run(body, storage):
   env={'body':body,'storage_ko':storage,'STUB':False,'builtin_mfd':False,'USB_SMOKE':False,'mfd_depchanged':0,'storage_load':0,'storage_dep':0,'storage_added':0,'PROF':{},'devprofile':SimpleNamespace(stage2_modules_load=lambda data,p:data)}
   exec(loop,env);return bytes(env['out'])
  def parse(blob):
   off=0;entries={}
   while off<len(blob):
    h=blob[off:off+110];self.assertEqual(h[:6],b'070701')
    ns=int(h[94:102],16);size=int(h[54:62],16)
    name=blob[off+110:off+110+ns-1].decode();start=(off+110+ns+3)&~3;end=start+size
    self.assertNotIn(name,entries);self.assertLessEqual(end,len(blob));entries[name]=blob[start:end];off=(end+3)&~3
   return entries
  entries=parse(run(base,b'ko bytes'))
  self.assertEqual(entries['lib/modules/modules.load'],b'existing.ko\nusb-storage.ko\n')
  self.assertEqual(entries['lib/modules/modules.dep'],b'/lib/modules/existing.ko:\n/lib/modules/usb-storage.ko:\n')
  self.assertEqual(entries['lib/modules/usb-storage.ko'],b'ko bytes')
  self.assertEqual(run(base,None),base)
  with self.assertRaisesRegex(ValueError,'duplicate'):run(base+entry('lib/modules/usb-storage.ko',b'x'),b'ko bytes')
 def test_other_profiles_unmodified(self): self.assertIsNone(self.f({'model':{'id':'kano'}},None,''))
 def test_missing_or_bad_config_fails(self):
  with self.assertRaises(ValueError):self.f(self.prof,None,'')
  with self.assertRaises(ValueError):self.f(self.prof,{**self.cfg,'CONFIG_USB_STORAGE':'y'},'')
 def test_module_missing_and_unpinned_refuse(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d);(p/'modules').mkdir()
   with self.assertRaises(ValueError):self.f(self.prof,self.cfg,d)
   for n,b in [('modules/usb-storage.ko',b'bad'),('modules/modules.dep',b'usb-storage.ko:\n'),('Module.symvers',b'x')]: (p/n).write_bytes(b)
   with self.assertRaisesRegex(ValueError,'pin mismatch'):self.f(self.prof,self.cfg,d)
 def test_abi_result_only_allows_exact_export_refusal(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d);(p/'modules').mkdir()
   for n,b in [('modules/usb-storage.ko',b'known'),('modules/modules.dep',b'usb-storage.ko:\n'),('Module.symvers',b'x')]: (p/n).write_bytes(b)
   fake=SimpleNamespace(hexdigest=lambda:'f429104c8803c8156f8278d37b08147d11eb3f287b58320a10fda9bf286fffd4')
   with patch.object(hashlib,'sha256',return_value=fake):
    with patch.object(subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='FAIL the module exports symbols\n',stderr='')):self.assertEqual(self.f(self.prof,self.cfg,d),b'known')
    for stdout in ('FAIL the module exports symbols\nFAIL wrong CRC\n','FAIL wrong CRC\n',''):
     with patch.object(subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout=stdout,stderr='')):
      with self.assertRaisesRegex(ValueError,'ABI'):self.f(self.prof,self.cfg,d)
    (p/'modules/modules.dep').write_text('usb-storage.ko: scsi_mod.ko\n')
    with self.assertRaisesRegex(ValueError,'dependencies'):self.f(self.prof,self.cfg,d)
if __name__=='__main__':unittest.main()
