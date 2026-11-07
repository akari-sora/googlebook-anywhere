#!/usr/bin/env python3
import ast, pathlib, sys, unittest
root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / 'builder/lib'))
import devprofile
source = (root / 'builder/image.py').read_text()
tree = ast.parse(source)
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'functional_cmdline')
ns = {'devprofile': devprofile}
exec(compile(ast.Module(body=[node], type_ignores=[]), 'image.py', 'exec'), ns)
functional_cmdline = ns['functional_cmdline']
EARLY = 'earlycon=uart8250,io,0x3f8,115200n8'
TOKEN = 'a' * 32
class FunctionalCommandLine(unittest.TestCase):
 def test_fits_unchanged(self):
  opts = ['module_blacklist=a,b', 'console=ttyS0,115200n8', EARLY]
  self.assertEqual(functional_cmdline(opts, TOKEN, ''), opts + ['androidboot.gbos_qemu_nonce='+TOKEN])
 def test_2005_bytes_remove_only_earlycon(self):
  prefix = ['module_blacklist=', 'initcall_blacklist=nvme_init,mei_me_driver_init', 'keep_bootcon', 'printk.devkmsg=on', 'console=ttyS0,115200n8', EARLY]
  suffix = 'androidboot.gbos_qemu_nonce='+TOKEN
  n = 2005-len(' '.join(prefix+[suffix]).encode())
  prefix[0] += 'x'*n
  self.assertEqual(len(' '.join(prefix+[suffix]).encode()), 2005)
  out = functional_cmdline(prefix, TOKEN, devprofile.KERNEL_CONFIG_CMDLINE)
  self.assertEqual(out, [x for x in prefix if x != EARLY]+[suffix])
  self.assertEqual(len(' '.join(out).encode()),1969)
  self.assertTrue(devprofile.x86_cmdline_fits(len(' '.join(out).encode()),devprofile.KERNEL_CONFIG_CMDLINE))
 def test_unfixable_command_fails(self):
  with self.assertRaisesRegex(ValueError,'exceeds'):
   functional_cmdline(['module_blacklist='+'x'*2048,EARLY],TOKEN,devprofile.KERNEL_CONFIG_CMDLINE)
 def test_actual_checked_builtin_controls_budget(self):
  opts=['module_blacklist='+'x'*1900, EARLY]
  self.assertIn(EARLY,functional_cmdline(opts,TOKEN,''))
  with self.assertRaisesRegex(ValueError,'exceeds'):
   functional_cmdline(opts,TOKEN,'x'*200)
  self.assertIn('devprofile.builtin_cmdline_text(initrd_checked_kernel)',source)
  self.assertIn('functional_cmdline(q, probe_token, qemu_builtin)',source)
if __name__ == '__main__': unittest.main()
