#!/usr/bin/env python3
"""H1-H3 regression fixtures for exact pins and fail-closed nested traversal."""
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('ifma_scan_fixtures', Path(__file__).with_name('ifma-scan-tests.py'))
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)
S = f.SCANNER


class FailClosedTests(unittest.TestCase):
    category = 'synthetic'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'tree'; self.root.mkdir()
        self.prov = {'partition': 'system_a', 'container_chain': []}

    def scan(self, **kw):
        return S.scan_tree(self.root, provenance=self.prov, **kw)

    def pin(self, path, data, offset=None):
        row = dict(partition='system_a', container_chain=[{'kind':'tree_file','path':path}],
                   sha256=hashlib.sha256(data).hexdigest(), reason='synthetic reviewed fixture')
        if offset is not None:
            row.update(file_offset=hex(offset), encoding_hex=data[offset:offset+5].hex())
        return row

    def test_legacy_prefix_luq_and_huq_are_refused(self):
        for opcode in ('b4','b5'):
            for prefix in ('66','67','f2','f3','2e','3e','26','64','65','36'):
                with self.subTest(opcode=opcode,prefix=prefix):
                    data=f.elf_with_segments(bytes.fromhex(prefix+'c4e2f1'+opcode+'00c3'))
                    (self.root/'bad').write_bytes(data)
                    with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):
                        self.scan()

    def test_jmp_into_movabs_annotation_cannot_suppress_hit(self):
        data=f.elf_with_segments(bytes.fromhex('eb0248b8c4e2f1b4c3000000c3'))
        (self.root/'bad').write_bytes(data)
        output='400110: eb 02 jmp 400114\n400112: 48 b8 c4 e2 f1 b4 c3 00 00 00 movabs $0xc3b4f1e2c4,%rax\n'
        with mock.patch.object(S,'_find_disassembler',return_value=('objdump',)), mock.patch.object(S,'_objdump_output',return_value=output):
            with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):
                self.scan()

    def test_entrypoint_on_candidate_cannot_be_suppressed(self):
        data=bytearray(f.elf_with_segments(bytes.fromhex('48b8c4e2f1b4c3000000c3')))
        struct.pack_into('<Q',data,24,0x400112)
        (self.root/'entry').write_bytes(data)
        output='400110: 48 b8 c4 e2 f1 b4 c3 00 00 00 movabs $0xc3b4f1e2c4,%rax\n'
        with mock.patch.object(S,'_find_disassembler',return_value=('objdump',)), mock.patch.object(S,'_objdump_output',return_value=output):
            with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):
                self.scan()

    def test_non_instruction_pin_requires_exact_sha_offset_encoding_chain_partition(self):
        data=f.elf_with_segments(bytes.fromhex('48b8c4e2f1b4c3000000c3'))
        (self.root/'false').write_bytes(data)
        pin=self.pin('false',data,0x112)
        report=self.scan(known_non_instructions=[pin])
        self.assertEqual(len(report['accepted_non_instructions']),1)
        for field,value in [('sha256','0'*64),('file_offset','0x113'),('encoding_hex','c4e2f1b5c3'),('partition','vendor_a'),('container_chain',[{'kind':'tree_file','path':'other'}])]:
            with self.subTest(field=field):
                bad=dict(pin);bad[field]=value
                with self.assertRaises(S.ScanError):self.scan(known_non_instructions=[bad])
        changed=data[:-1]+b'1';(self.root/'false').write_bytes(changed)
        with self.assertRaisesRegex(S.ScanError,'object changed'):self.scan(known_non_instructions=[pin])

    def test_gnu_hash_candidate_requires_a_pin(self):
        data,offset=f.semantic_gnu_hash_elf()
        (self.root/'hash').write_bytes(data)
        with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):self.scan()
        self.assertEqual(len(self.scan(known_non_instructions=[self.pin('hash',data,offset)])['accepted_non_instructions']),1)

    def test_non_instruction_hash_is_checked_when_observer_is_unavailable(self):
        data=f.elf_with_segments(f.EncodingTests.LUQ)
        (self.root/'false').write_bytes(data)
        pin=self.pin('false',data,0x110)
        pin['sha256']='0'*64
        with mock.patch.object(S, '_observe_object', return_value=None):
            with self.assertRaisesRegex(S.ScanError, 'forbidden VEX IFMA'):
                self.scan(known_non_instructions=[pin])

    def test_clean_first_cpio_does_not_hide_second_plain_archive(self):
        one=f.newc_archive('clean',b'clean');two=f.newc_archive('evil',f.elf_with_segments(f.EncodingTests.LUQ))
        for padding in (b'',b'\0'*513):
            (self.root/'ramdisk.cpio').write_bytes(one+padding+two)
            with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):self.scan()

    def test_clean_first_cpio_does_not_hide_second_gzip_archive(self):
        one=f.newc_archive('clean',b'clean');two=gzip.compress(f.newc_archive('evil',f.elf_with_segments(f.EncodingTests.HUQ)))
        (self.root/'ramdisk.cpio').write_bytes(one+b'\0'*11+two)
        with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):self.scan()

    def fake_tool(self,name,payload):
        tools=self.base/'tools';bindir=tools/'bin';bindir.mkdir(parents=True,exist_ok=True)
        (bindir/(name+'-output')).write_bytes(payload)
        tool=bindir/name;tool.write_text('#!/bin/sh\ncat "$(dirname "$0")/'+name+'-output"\n');tool.chmod(0o755)
        return tools

    @staticmethod
    def lz4_frame(payload=b'fixture'):
        # Structurally valid independent-block frame; fake_tool supplies decoded bytes.
        block=(0x80000000|len(payload)).to_bytes(4,'little')+payload
        return b'\x04\x22\x4d\x18'+b'\x60\x40\0'+block+b'\0\0\0\0'

    def test_clean_first_cpio_does_not_hide_second_lz4_archive(self):
        tools=self.fake_tool('lz4',f.newc_archive('evil',f.elf_with_segments(f.EncodingTests.HUQ)))
        (self.root/'ramdisk.cpio').write_bytes(f.newc_archive('clean',b'clean')+self.lz4_frame())
        with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):self.scan(otatools=tools)

    def test_mixed_compressed_tails_consume_exactly_one_member(self):
        clean=f.newc_archive('clean',b'clean')
        evil=f.newc_archive('evil',f.elf_with_segments(f.EncodingTests.LUQ))
        tools=self.fake_tool('lz4',clean)
        sequences=[
            clean+gzip.compress(clean)+evil,
            clean+gzip.compress(clean)+gzip.compress(clean)+evil,
            clean+gzip.compress(clean)+self.lz4_frame()+evil,
            clean+lzma.compress(clean)+evil,
        ]
        with self.subTest(optional_frame='zstd'):
            if not shutil.which('zstd'):
                self.skipTest('zstd CLI unavailable for mixed-tail case')
            zstd=subprocess.run(['zstd','-q','-c'],input=clean,stdout=subprocess.PIPE,check=True).stdout
            sequences.append(clean+zstd+evil)
        for data in sequences:
            with self.subTest(length=len(data)):
                (self.root/'ramdisk.cpio').write_bytes(data)
                with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):
                    self.scan(otatools=tools)
        (self.root/'ramdisk.cpio').write_bytes(clean+gzip.compress(clean)+b'junk')
        with self.assertRaisesRegex(S.ScanError,'non-zero initramfs tail'):
            self.scan(otatools=tools)

    @unittest.skipUnless(shutil.which('lz4'),'lz4 CLI unavailable')
    def test_real_lz4_frame_then_plain_archive_is_scanned(self):
        clean=f.newc_archive('clean',b'clean')
        compressed=subprocess.run(['lz4','-z','-c'],input=clean,stdout=subprocess.PIPE,check=True).stdout
        evil=f.newc_archive('evil',f.elf_with_segments(f.EncodingTests.HUQ))
        (self.root/'ramdisk.cpio').write_bytes(clean+compressed+evil)
        with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):
            self.scan()

    def test_cpio_zero_padding_is_legal_but_junk_tail_is_refused(self):
        path=self.root/'ramdisk.cpio';archive=f.newc_archive('clean',b'clean')
        path.write_bytes(archive+b'\0'*77);self.assertEqual(self.scan()['hits'],[])
        path.write_bytes(archive+b'\0'*77+b'junk')
        with self.assertRaisesRegex(S.ScanError,'non-zero initramfs tail'):self.scan()

    def test_unknown_image_refused_and_exact_allowlist_accepted(self):
        data=b'AVB0opaque-vbmeta-fixture';(self.root/'vbmeta.img').write_bytes(data)
        with self.assertRaisesRegex(S.ScanError,'unsupported nested image'):self.scan()
        pin=self.pin('vbmeta.img',data)
        self.assertEqual(len(self.scan(unsupported_allowlist=[pin])['accepted_unsupported']),1)
        for field,value in [('sha256','0'*64),('partition','vendor_a'),('container_chain',[{'kind':'tree_file','path':'wrong.img'}])]:
            with self.subTest(field=field):
                bad=dict(pin);bad[field]=value
                with self.assertRaises(S.ScanError):self.scan(unsupported_allowlist=[bad])

    def test_missing_tool_is_fatal_unless_exact_allowlisted(self):
        data=bytearray(2048);data[1024:1028]=b'\xe2\xe1\xf5\xe0';data=bytes(data)
        (self.root/'fs.img').write_bytes(data)
        with mock.patch.object(S,'_tool',return_value=None):
            with self.assertRaisesRegex(S.ScanError,'fsck.erofs unavailable'):self.scan()
            self.assertEqual(len(self.scan(unsupported_allowlist=[self.pin('fs.img',data)])['accepted_unsupported']),1)

    def test_too_deep_image_is_fatal(self):
        data=f.elf_with_segments()
        for i in range(5):
            out=io.BytesIO()
            with zipfile.ZipFile(out,'w') as z:z.writestr('next.zip' if i else 'clean',data)
            data=out.getvalue()
        (self.root/'nested.zip').write_bytes(data)
        with self.assertRaisesRegex(S.ScanError,'nested depth limit'):self.scan(max_depth=1)

    def test_compressed_elf_modules_gzip_xz_zstd_are_scanned(self):
        elf=f.relocatable_elf(f.EncodingTests.LUQ)
        for suffix,compressed in [('gz',gzip.compress(elf)),('xz',lzma.compress(elf)),('zst',b'\x28\xb5\x2f\xfd\x60'+(len(elf)-256).to_bytes(2,'little')+((len(elf)<<3)|1).to_bytes(3,'little')+elf)]:
            with self.subTest(suffix=suffix):
                path=self.root/('module.ko.'+suffix);path.write_bytes(compressed)
                tools=self.fake_tool('zstd',elf)
                with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):self.scan(otatools=tools)
                path.unlink()

    def test_duplicate_observation_cannot_hide_missing_pin(self):
        for policy_kind in ('known_non_instructions', 'residuals', 'unsupported_allowlist'):
            with self.subTest(policy_kind=policy_kind):
                data=b'AVB0opaque' if policy_kind=='unsupported_allowlist' else f.elf_with_segments(f.EncodingTests.LUQ)
                first='a.img' if policy_kind=='unsupported_allowlist' else 'a'
                path=self.root/first;path.write_bytes(data)
                chain=[{'kind':'tree_file','path':first}]
                if policy_kind=='residuals':
                    chain[-1]['sha256']=hashlib.sha256(data).hexdigest()
                    one=dict(partition='system_a',container_chain=chain,file_offset='0x110',instruction='vpmadd52luq',reason='test residual')
                else:
                    one=self.pin(first,data,0x110 if policy_kind=='known_non_instructions' else None)
                two=copy.deepcopy(one);two['container_chain'][-1]['path']='missing'
                hook='_scan_nested_path' if policy_kind=='unsupported_allowlist' else '_merge_file_scan'
                original=getattr(S,hook)
                def duplicate(*args, **kwargs):
                    original(*args, **kwargs)
                    original(*args, **kwargs)
                with mock.patch.object(S,hook,side_effect=duplicate):
                    with self.assertRaisesRegex(S.ScanError,'missing or not uniquely observed'):
                        self.scan(**{policy_kind:[one,two]})
                path.unlink()

    def test_readable_container_pin_cannot_hide_inner_ifma(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:z.writestr('evil',f.elf_with_segments(f.EncodingTests.LUQ))
        data=out.getvalue();(self.root/'opaque.zip').write_bytes(data)
        with self.assertRaisesRegex(S.ScanError,'forbidden VEX IFMA'):
            self.scan(unsupported_allowlist=[self.pin('opaque.zip',data)])

    def test_unopenable_images_are_fatal(self):
        for name,data in [('bad.zip',b'PK\x03\x04bad'),('bad.gz',b'\x1f\x8bbad'),('bad.xz',b'\xfd7zXZ\0bad')]:
            with self.subTest(name=name):
                path=self.root/name;path.write_bytes(data)
                with self.assertRaises(S.ScanError):self.scan()
                if name.endswith('.zip'):
                    report=self.scan(unsupported_allowlist=[self.pin(name,data)])
                    self.assertEqual(len(report['accepted_unsupported']),1)
                else:
                    # J1 requires invalid framing to stop even with an outer pin.
                    with self.assertRaises(S.ScanError):
                        self.scan(unsupported_allowlist=[self.pin(name,data)])
                path.unlink()

    def test_unopenable_cpio_and_non_elf_module_are_fatal_even_with_exact_pin(self):
        # gb-ifma023e E1 spec change: a decoded non-ELF .ko.gz was exact-pinnable;
        # its payload was decoded, so it is now an unpinnable refusal.
        name='bad.cpio';data=b'070701bad';path=self.root/name;path.write_bytes(data)
        with self.assertRaises(S.ScanError):
            self.scan(unsupported_allowlist=[self.pin(name,data)])
        path.unlink()
        name='bad.ko.gz';data=gzip.compress(b'not an ELF module');path=self.root/name;path.write_bytes(data)
        with self.assertRaisesRegex(S.ScanError,'compressed kernel module payload is not ELF'):
            self.scan()
        with self.assertRaisesRegex(S.ScanError,'compressed kernel module payload is not ELF') as caught:
            self.scan(unsupported_allowlist=[self.pin(name,data)])
        self.assertNotIsInstance(caught.exception,S.ImageOpenError)


if __name__ == '__main__':
    unittest.main(verbosity=2)
