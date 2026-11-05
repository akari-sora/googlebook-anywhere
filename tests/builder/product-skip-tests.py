#!/usr/bin/env python3
import copy
import contextlib
import base64
import hashlib
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'builder/lib'))
import ifma
import product_model as pm


def meta():
    return {'file_type': stat.S_IFREG, 'mode': 0o644, 'uid': 0, 'gid': 0,
            'nlink': 1, 'mtime_sec': 1230768000, 'mtime_nsec': 0,
            'selinux': 'u:object_r:system_file:s0', 'caps': None,
            'xattrs': {'security.selinux': b'u:object_r:system_file:s0'}}


class OptionalSkipTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'model': {'id': 'kano', 'android_model': pm.MODEL}}
        self.env = {'GBOS_ANDROID_MODEL': 'profile', 'GBOS_LAPIS_BUILD': pm.BUILD}

    def test_options_are_explicit_and_default_is_preserved(self):
        self.assertFalse(pm.validate_options(self.profile, {}))
        self.assertTrue(pm.validate_options(self.profile, self.env))
        self.assertTrue(pm.validate_options(self.profile, dict(self.env, GBOS_SETUP_PIN_SKIP='1')))
        for env in ({'GBOS_SETUP_PIN_SKIP': '1'}, dict(self.env, GBOS_SETUP_PIN_SKIP='0'),
                    dict(self.env, GBOS_SETUP_PIN_SKIP='yes'),
                    dict(self.env, GBOS_SETUP_PIN_SKIP='1', GBOS_LAPIS_BUILD='other')):
            with self.subTest(env=env), self.assertRaises(ValueError):
                pm.validate_options(self.profile, env)
        with mock.patch.dict(os.environ, {'GBOS_SETUP_PIN_SKIP': '1'}, clear=True), \
                mock.patch.object(ifma, '_gpt_partitions') as image_read:
            with self.assertRaises(ValueError):
                pm.rebuild_product_partition(self.profile, '/no/image', '/no/tmp')
            image_read.assert_not_called()

    def test_pin_and_in_tree_artifact_refuse_missing_tampered_or_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / 'fixture.apk.base64'
            apk.write_bytes(base64.encodebytes(b'candidate'))
            digest = hashlib.sha256(b'candidate').hexdigest()
            with mock.patch.object(pm, 'SKIP_APK', apk):
                with mock.patch.object(pm, 'SKIP_APK_SHA256', 'REPLACE_WITH_VERIFIED_SHA256'):
                    with self.assertRaisesRegex(SystemExit, 'not pinned'):
                        pm.read_skip_apk()
                with mock.patch.object(pm, 'SKIP_APK_SHA256', digest):
                    self.assertEqual(pm.read_skip_apk(), b'candidate')
                    apk.write_bytes(base64.encodebytes(b'tampered'))
                    with self.assertRaisesRegex(SystemExit, 'digest drifted'):
                        pm.read_skip_apk()
                    for invalid in (b'not base64!\n', b'', b'A' * (2 * 1024 * 1024),
                                    base64.b64encode(b'candidate') + b'\x00'):
                        apk.write_bytes(invalid)
                        with self.subTest(invalid_size=len(invalid)), self.assertRaises(SystemExit):
                            pm.read_skip_apk()
                    apk.unlink()
                    apk.symlink_to(Path(tmp) / 'elsewhere')
                    with self.assertRaisesRegex(SystemExit, 'regular'):
                        pm.read_skip_apk()

    def test_addition_requires_pinned_template_and_known_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / 'overlay').mkdir()
            index = {'overlay': ('dir', None), pm.SKIP_TEMPLATE: ('file', pm.SKIP_TEMPLATE_SHA256)}
            for index_override, meta_override in [
                ({pm.SKIP_TEMPLATE: ('file', '0' * 64)}, {}),
                ({'overlay/config/config.xml': ('file', '0' * 64)}, {}),
                ({'overlay/config': ('dir', None)}, {}),
                ({'overlay/config/other.xml': ('file', '0' * 64)}, {}),
                ({'overlay/config': ('symlink', '/elsewhere')}, {}),
                ({pm.SKIP_DEST: ('file', '0' * 64)}, {}),
                ({}, {'mode': 0o777}), ({}, {'nlink': 2}),
                ({}, {'uid': 1000}), ({}, {'caps': b'cap'}),
                ({}, {'selinux': 'u:object_r:wrong:s0'}), ({}, {'mtime_sec': 1}),
                ({}, {'xattrs': {'security.selinux': b'drift'}}),
            ]:
                proposed_index = dict(index, **index_override)
                proposed_meta = dict(meta(), **meta_override)
                with self.subTest(index=index_override, metadata=meta_override), self.assertRaises(SystemExit):
                    pm.install_skip_overlay(tree, proposed_index, {pm.SKIP_TEMPLATE: proposed_meta}, b'apk')
                self.assertFalse((tree / pm.SKIP_DEST).exists())
            metadata = {pm.SKIP_TEMPLATE: meta()}
            pm.install_skip_overlay(tree, index, metadata, b'apk')
            self.assertEqual((tree / pm.SKIP_DEST).read_bytes(), b'apk')
            self.assertEqual(metadata[pm.SKIP_DEST], metadata[pm.SKIP_TEMPLATE])
            self.assertIsNot(metadata[pm.SKIP_DEST]['xattrs'], metadata[pm.SKIP_TEMPLATE]['xattrs'])
            with self.assertRaises(SystemExit):
                pm.install_skip_overlay(tree, index, metadata, b'apk')

    def test_rebuilt_inode_metadata_has_only_exact_addition(self):
        old = {'old': meta()}
        new = {'old': meta(), pm.SKIP_DEST: meta()}
        sb = {k: 1 for k in ('uuid', 'build_time', 'build_time_nsec', 'compat', 'incompat', 'volume_name', 'pcluster_size')}
        with mock.patch.object(pm.rebuild, 'superblock_params', return_value=sb), \
                mock.patch.object(ifma, '_erofs_meta_map', side_effect=lambda p: old if p == 'old' else new):
            pm.assert_skip_metadata('old', 'new', meta())
            for bad in ('extra', 'old_mode', 'new_mtime', 'missing'):
                good = copy.deepcopy(new)
                if bad == 'extra': new['surprise'] = meta()
                elif bad == 'old_mode': new['old']['mode'] = 0o600
                elif bad == 'new_mtime': new[pm.SKIP_DEST]['mtime_sec'] = 42
                else: del new[pm.SKIP_DEST]
                with self.subTest(bad=bad), self.assertRaises(SystemExit):
                    pm.assert_skip_metadata('old', 'new', meta())
                new.clear(); new.update(good)
        with mock.patch.object(pm.rebuild, 'superblock_params', side_effect=[sb, dict(sb, uuid=2)]):
            with self.assertRaises(SystemExit):
                pm.assert_skip_metadata('old', 'new', meta())

    def test_reviewed_apk_bytes_match_pin(self):
        data = pm.read_skip_apk()
        self.assertEqual(len(data), 8405)
        self.assertEqual(hashlib.sha256(data).hexdigest(), pm.SKIP_APK_SHA256)

    def test_enabled_option_runs_complete_product_rebuild_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            image = tmp / 'image'
            image.write_bytes(bytes(32768))
            (tmp / 'offsets.env').write_text('VENDOR_OFF=0\n')
            old_prop = b'ro.product.product.model=ASUS Googlebook 14\n'
            new_prop = pm.replace_model(old_prop, pm.MODEL)
            old_apk = b'stock overlay fixture'
            new_apk = b'candidate overlay fixture'
            stock_pin = hashlib.sha256(old_apk).hexdigest()
            calls = []
            sb = {'uuid': pm.UUID, 'build_time': 123, 'volume_name': '', 'pcluster_size': None}
            def run_ot(_ot, tool, *args):
                if tool == 'avbtool' and args[0] == 'verify_image':
                    self.assertTrue(Path(args[args.index('--image') + 1]).is_file())
                if tool == 'avbtool' and args[0] == 'add_hashtree_footer':
                    p = Path(args[args.index('--image') + 1]); p.write_bytes(p.read_bytes().ljust(8192, b'\0'))
                if tool == 'fsck.erofs':
                    tree = Path(next(str(a).split('=', 1)[1] for a in args if str(a).startswith('--extract=')))
                    (tree / 'etc').mkdir(parents=True, exist_ok=True)
                    (tree / 'overlay').mkdir(parents=True, exist_ok=True)
                    (tree / 'etc/build.prop').write_bytes(old_prop if tree.name == 'product-tree' else new_prop)
                    (tree / pm.SKIP_TEMPLATE).write_bytes(old_apk)
                    if tree.name == 'product-tree-new':
                        (tree / pm.SKIP_DEST).write_bytes(new_apk)
                if tool == 'mkfs.erofs':
                    Path(args[-2]).write_bytes(b'rebuilt')
                return ''
            def copy_extent(_image, _off, _size, out, **_kw):
                Path(out).write_bytes(b'stock')
            def verify_metadata(_old, _new, expected):
                self.assertEqual(expected, meta())
                calls.append('metadata')
            def write_configs(_mount, tree, metadata, _fc, _fs, **_kwargs):
                self.assertEqual((tree / pm.SKIP_DEST).read_bytes(), new_apk)
                self.assertEqual(metadata[pm.SKIP_DEST], meta())
                calls.append('configs')
            patches = [mock.patch.dict(os.environ, dict(self.env, GBOS_SETUP_PIN_SKIP='1'), clear=True),
                       mock.patch.object(pm, 'PARTITION_SIZE', 8192), mock.patch.object(pm, 'ORIGINAL_SIZE', 5),
                       mock.patch.object(pm, 'SKIP_TEMPLATE_SHA256', stock_pin),
                       mock.patch.object(pm, 'read_skip_apk', return_value=new_apk),
                       mock.patch.object(ifma, '_gpt_partitions', return_value={'super': {'start': 4096, 'size': 16384}}),
                       mock.patch.object(ifma, '_lp_Named_extent', return_value=(4096, 8192)),
                       mock.patch.object(pm, 'assert_exclusive_extent'), mock.patch.object(pm, 'assert_stock_avb'),
                       mock.patch.object(ifma, '_copy_partition_extent', side_effect=copy_extent),
                       mock.patch.object(ifma, '_copy_erofs_extent', side_effect=copy_extent),
                       mock.patch.object(ifma, '_run_ot', side_effect=run_ot),
                       mock.patch.object(ifma, '_erofs_meta_map', return_value={pm.SKIP_TEMPLATE: meta()}),
                       mock.patch.object(pm.rebuild, 'superblock_params', return_value=sb),
                       mock.patch.object(pm.rebuild, 'fs_uuid', return_value=pm.UUID),
                       mock.patch.object(pm.rebuild, 'write_configs', side_effect=write_configs),
                       mock.patch.object(pm.rebuild, 'restore_mtimes'),
                       mock.patch.object(pm, 'assert_skip_metadata', side_effect=verify_metadata),
                       mock.patch.object(ifma, '_assert_same_erofs_metadata')]
            with contextlib.ExitStack() as stack:
                for patch in patches: stack.enter_context(patch)
                result = pm.rebuild_product_partition(self.profile, image, tmp, tmp)
                self.assertEqual(result['product_size'], 8192)
                self.assertEqual(calls, ['configs', 'metadata'])
                ifma._assert_same_erofs_metadata.assert_not_called()
                self.assertIn('PRODUCT_OFF=8192\nPRODUCT_SIZE=8192\n', (tmp / 'offsets.env').read_text())

    def test_rebuilt_tree_checks_original_files_and_new_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            rebuilt = Path(tmp)
            (rebuilt / 'etc').mkdir()
            (rebuilt / 'etc/build.prop').write_bytes(b'new prop')
            original = {'etc': ('dir', None), 'etc/build.prop': ('file', 'old'), 'unchanged': ('file', 'pin')}
            actual = {'etc': ('dir', None), 'etc/build.prop': ('file', 'new'), 'unchanged': ('file', 'pin')}
            pm.assert_product_tree(original, actual, rebuilt, b'new prop', None)
            digest = hashlib.sha256(b'apk').hexdigest()
            actual[pm.SKIP_DEST] = ('file', digest)
            pm.assert_product_tree(original, actual, rebuilt, b'new prop', b'apk')
            for altered in ({**actual, 'unexpected': ('file', 'z')},
                            {**actual, 'unchanged': ('file', 'changed')},
                            {**actual, pm.SKIP_DEST: ('file', 'wrong')}):
                with self.assertRaises(SystemExit):
                    pm.assert_product_tree(original, altered, rebuilt, b'new prop', b'apk')
            with self.assertRaises(SystemExit):
                pm.assert_product_tree(original, actual, rebuilt, b'wrong prop', b'apk')
            with self.assertRaises(SystemExit):
                pm.assert_product_tree(original, actual, rebuilt, b'new prop', None)


if __name__ == '__main__':
    unittest.main()
