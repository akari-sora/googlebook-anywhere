import importlib.util,pathlib,unittest
P=pathlib.Path(__file__).resolve().parents[2]/'builder/lib/devprofile.py';s=importlib.util.spec_from_file_location('devprofile',P);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class MFD(unittest.TestCase):
 def test_load(self):
  self.assertEqual(m.drop_builtin_mfd_load('a.ko\nmfd-core.ko\nb.ko\n'),'a.ko\nb.ko\n')
  for x in ('a.ko\n','mfd-core.ko\nmfd-core.ko\n'):
   with self.assertRaises(m.ProfileError):m.drop_builtin_mfd_load(x)
 def test_deps(self):
  actual='/lib/modules/intel-lpss.ko: /lib/modules/mfd-core.ko\n/lib/modules/cros_ec_dev.ko: /lib/modules/mfd-core.ko /lib/modules/cros-ec-proto.ko\n/lib/modules/mfd-core.ko:\n/lib/modules/intel-lpss-pci.ko: /lib/modules/intel-lpss.ko /lib/modules/mfd-core.ko\n'
  expected='/lib/modules/intel-lpss.ko:\n/lib/modules/cros_ec_dev.ko: /lib/modules/cros-ec-proto.ko\n/lib/modules/intel-lpss-pci.ko: /lib/modules/intel-lpss.ko\n'
  self.assertEqual(m.drop_builtin_mfd_dependencies(actual),expected)
  for x in ('/lib/modules/mfd-core.ko:\n','/lib/modules/x.ko: /lib/modules/mfd-core.ko\n','/lib/modules/mfd-core.ko: /lib/modules/x.ko\n'):
   with self.assertRaises(m.ProfileError):m.drop_builtin_mfd_dependencies(x)
if __name__=='__main__':unittest.main()
