import hashlib,importlib.util,json,os,pathlib,unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[2];p=ROOT/'builder/lib/ally_microdroid.py';spec=importlib.util.spec_from_file_location('candidate',p);m=importlib.util.module_from_spec(spec);import sys;sys.modules[spec.name]=m;spec.loader.exec_module(m)
class ContractVersions(unittest.TestCase):
 def test_both_versions_and_identity(self):
  for build in ('16452207','16658739'):
   with patch.dict(os.environ,{'GBOS_LAPIS_BUILD':build}):
    c=m._contract();path,pin=m.CONTRACTS[build];self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),pin);self.assertEqual(m.source_identity()['contract_sha256'],pin);self.assertEqual(len(c['files']),5)
 def test_unknown_refused(self):
  with patch.dict(os.environ,{'GBOS_LAPIS_BUILD':'99999999'}):
   with self.assertRaises(m.Refused):m._contract()
 def test_cross_version_and_mutated_pin_rejected(self):
  old=m.CONTRACTS['16452207'];new=m.CONTRACTS['16658739']
  with patch.dict(os.environ,{'GBOS_LAPIS_BUILD':'16658739'}),patch.dict(m.CONTRACTS,{'16658739':(new[0],old[1])}):
   with self.assertRaises(m.Refused):m._contract()
 def test_166_expected_files(self):
  c=json.loads(m.CONTRACTS['16658739'][0].read_text())
  expected={
   'etc/fs/microdroid.img':(28815360,'4bd5c1c8833f9076af702e675dc1c6e58c78b8a68590b4f9544a1b29482640e4'),
   'etc/fs/microdroid_vbmeta.img':(65536,'5315ebd6901df35604e25c7cd88679f1c76899f360a375bc5e1888c4fbf9ea50'),
   'etc/fs/microdroid_kernel':(10047488,'ab11a15a034c43cfc204169bfd27b46723d65260b0171f5db8e15184d1866557'),
   'etc/microdroid_initrd_normal.img':(837752,'f9cc0c9db4b90579ddb27387cba3219a426cc60ba3ee3364a2f4bb6f49ddaa48'),
   'etc/microdroid_initrd_debuggable.img':(837916,'5a687b7fe0041378643a5ff1acc23b81c91a2679acb082f76553bd90a637a35f'),
  }
  self.assertEqual(set(c['files']),set(expected))
  for name,values in expected.items():self.assertEqual((c['files'][name]['size'],c['files'][name]['sha256']),values)
  for name in ('etc/fs/microdroid.img','etc/fs/microdroid_vbmeta.img'):
   info=c['files'][name]['info_image'];self.assertIn('16658739:user/dev-keys',info);self.assertIn('2026-10-01',info)
 def test_kernel_descriptor_equivalence(self):
  import re
  with patch.dict(os.environ,{'GBOS_LAPIS_BUILD':'16452207'}): old=m._contract()
  with patch.dict(os.environ,{'GBOS_LAPIS_BUILD':'16658739'}): new=m._contract()
  a=old['files']['etc/fs/microdroid_kernel']['info_image'];b=new['files']['etc/fs/microdroid_kernel']['info_image']
  def boot(t):
   block=t.split('    Hash descriptor:',1)[1].split('    Prop:',1)[0]
   return tuple(re.search(r'^      '+field+r':\s*(.+)$',block,re.M).group(1) for field in ('Image Size','Hash Algorithm','Partition Name','Salt','Digest'))
  self.assertEqual(boot(a),boot(b));self.assertEqual(boot(a)[:3],('9974784 bytes','sha256','boot'))
 def test_residuals_match_166_reviewed_policy(self):
  c=json.loads(m.CONTRACTS['16658739'][0].read_text())
  manifest=json.loads((ROOT/'builder/ifma/builds/16658739.manifest.json').read_text())
  for kind,rows in c['policy_rows'].items():
   for row in rows:self.assertIn(row,manifest[kind])
if __name__=='__main__':unittest.main()
