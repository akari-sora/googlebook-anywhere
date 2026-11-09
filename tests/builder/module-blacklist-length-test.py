#!/usr/bin/env python3
"""Kernel charp value limit applies to each final boot entry."""
import ast, importlib.util, pathlib, tomllib, unittest
ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('devprofile', ROOT / 'builder/lib/devprofile.py')
devprofile = importlib.util.module_from_spec(spec); spec.loader.exec_module(devprofile)

class BlacklistLimit(unittest.TestCase):
    def test_ascii_boundaries(self):
        devprofile.validate_module_blacklist(['module_blacklist=' + 'a' * 1024], 'quiet')
        with self.assertRaisesRegex(devprofile.ProfileError, r'quiet:.*1025 bytes.*1024'):
            devprofile.validate_module_blacklist(['module_blacklist=' + 'a' * 1025], 'quiet')
    def test_utf8_bytes_not_characters(self):
        devprofile.validate_module_blacklist(['module_blacklist=' + 'é' * 512], 'debug')
        with self.assertRaises(devprofile.ProfileError):
            devprofile.validate_module_blacklist(['module_blacklist=' + 'é' * 513], 'debug')
    def test_no_missing_or_duplicate_parameters(self):
        for opts in ([], ['module_blacklist=a', 'module_blacklist=b']):
            with self.subTest(opts=opts), self.assertRaisesRegex(devprofile.ProfileError, 'exactly one'):
                devprofile.validate_module_blacklist(opts, 'debug')
    def test_real_ally06_profile_value_is_refused(self):
        profile = tomllib.loads((ROOT / 'profiles/rog-ally/profile.toml').read_text())
        value = ','.join(profile['blacklist']['modules'] + profile['blacklist']['debug_add'])
        self.assertEqual(len(value.encode()), 1039)
        with self.assertRaisesRegex(devprofile.ProfileError, r'1039 bytes.*1024'):
            devprofile.validate_module_blacklist(['module_blacklist=' + value], 'debug')
    def test_extended_only_exact_checked_ally_kernel(self):
        profile = {'model': {'id': 'rog-ally'}}
        checked = devprofile.CheckedConfig({})
        checked.bzimage_sha256 = devprofile.ALLY_EXTENDED_BLACKLIST_KERNEL_SHA256
        for n in (1024, 1025, 2048):
            devprofile.validate_module_blacklist(['module_blacklist=' + 'a'*n], 'quiet', profile=profile, checked_kernel=checked)
        with self.assertRaisesRegex(devprofile.ProfileError, '2049 bytes.*2048'):
            devprofile.validate_module_blacklist(['module_blacklist=' + 'a'*2049], 'quiet', profile=profile, checked_kernel=checked)
        for prof, kernel in (({'model': {'id': 'kano'}},checked), (profile,None), (profile,{})):
            with self.assertRaisesRegex(devprofile.ProfileError, '1025 bytes.*1024'):
                devprofile.validate_module_blacklist(['module_blacklist=' + 'a'*1025], 'quiet', profile=prof, checked_kernel=kernel)
        checked.bzimage_sha256 = '0'*64
        with self.assertRaises(devprofile.ProfileError):
            devprofile.validate_module_blacklist(['module_blacklist=' + 'a'*1025], 'quiet', profile=profile, checked_kernel=checked)
    def test_extended_rejects_duplicate_and_non_ascii_overflow(self):
        checked=devprofile.CheckedConfig({}); checked.bzimage_sha256=devprofile.ALLY_EXTENDED_BLACKLIST_KERNEL_SHA256
        kwargs={'profile':{'model':{'id':'rog-ally'}},'checked_kernel':checked}
        devprofile.validate_module_blacklist(['module_blacklist='+'é'*1024], 'debug', **kwargs)
        for opts in (['module_blacklist='+'é'*1025], ['module_blacklist=a','module_blacklist=b']):
            with self.assertRaises(devprofile.ProfileError): devprofile.validate_module_blacklist(opts,'debug',**kwargs)
    def test_image_uses_checked_kernel_and_preserves_later_binding(self):
        text=(ROOT/'builder/image.py').read_text()
        self.assertIn('profile=PROF, checked_kernel=initrd_checked_kernel',text)
        self.assertIn('installed.bzimage_sha256 != initrd_checked_kernel.bzimage_sha256',text)

    def test_kano_drain_final_value_fits(self):
        profile = tomllib.loads((ROOT / 'profiles/kano/profile.toml').read_text())
        modules = profile['blacklist']['modules'] + profile['blacklist']['debug_add']
        tree = ast.parse((ROOT / 'builder/image.py').read_text())
        names = {'EC_CORE', 'EC_RELEASE', 'EC_EXTRA_BLOCK', 'EC_DRAIN', 'DRAIN_DEBUG_DROP'}
        functions = {'ec_released', 'ec_blacklist', 'ec_variant_entries'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions or
                 isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
        ns = {}; exec(compile(ast.Module(nodes, []), 'image.py', 'exec'), ns)
        entry = ['module_blacklist=' + ','.join(modules)]
        _, debug, quiet = ns['ec_variant_entries'](entry, entry, entry, ['drain'])
        for name, opts in [('debug', debug), ('quiet', quiet)]:
            devprofile.validate_module_blacklist(opts, name)
    def test_validation_follows_ec_rewrite(self):
        tree = ast.parse((ROOT / 'builder/image.py').read_text())
        calls = [(i, n) for i, n in enumerate(tree.body) if isinstance(n, ast.Assign) and
                 isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == 'ec_variant_entries']
        self.assertEqual(len(calls), 1)
        loop = tree.body[calls[0][0] + 1]
        self.assertIsInstance(loop, ast.For)
        validators = [n for n in ast.walk(loop) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == 'validate_module_blacklist']
        self.assertEqual(len(validators), 1)
        self.assertIn(('debug', 'debug'), [(e.elts[0].value, e.elts[1].id) for e in loop.iter.elts])
        self.assertIn(('quiet', 'quiet'), [(e.elts[0].value, e.elts[1].id) for e in loop.iter.elts])

if __name__ == '__main__': unittest.main()
