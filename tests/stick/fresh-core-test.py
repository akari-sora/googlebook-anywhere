#!/usr/bin/env python3
"""Pure GPT/boot preparation regressions; no devices, root or external tools."""
import ast
import importlib.util
from pathlib import Path
import struct
import types
import unittest
from unittest import mock
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


u = module('fresh_test_update', ROOT / 'install/gbos-update.py')
fresh = module('fresh_test_core', ROOT / 'install/gbos-fresh.py')
fixture_ast = ast.parse((ROOT / 'tests/update/selftest.py').read_text())
fixture = {'uuid': uuid, 'struct': struct, 'zlib': zlib}
exec(compile(ast.Module(body=[n for n in fixture_ast.body if isinstance(n, ast.FunctionDef)
                             and n.name == 'gpt_bytes'], type_ignores=[]), '<existing GPT fixture>', 'exec'), fixture)


class Reader:
    path = '<memory GPT>'

    def __init__(self, parts=None, size=96 * fresh.MIB):
        self.size = size
        if parts is None:
            names = ['EFI-SYSTEM', 'super', 'userdata']
            names[2:2] = [f'{p}_{s}' for p in u.SLOT_PHYS for s in 'ab']
            parts = [(n, 2048 + i * 2048, 2048 + (i + 1) * 2048 - 1,
                      '0fc63daf-8483-4772-8e79-3d69d8477de4', str(uuid.uuid4()), 1 << (i % 8))
                     for i, n in enumerate(names)]
        self.parts = parts
        self.disk_guid = str(uuid.uuid4())
        head, tail = fixture['gpt_bytes'](parts, size, self.disk_guid)
        self.head, self.tail = bytearray(head), bytearray(tail)
        self.reads = []

    def pread(self, off, count):
        assert 0 <= off <= self.size and 0 <= count <= self.size - off
        self.reads.append((off, count))
        if off + count <= len(self.head):
            return bytes(self.head[off:off + count])
        start = self.size - len(self.tail)
        if off >= start:
            return bytes(self.tail[off - start:off - start + count])
        return bytes(count)

    def reseal(self, both=True):
        for storage, header, array in [(self.head, 512, 1024), (self.tail, len(self.tail) - 512, 0)]:
            struct.pack_into('<I', storage, header + 88, zlib.crc32(storage[array:array + 16384]))
            struct.pack_into('<I', storage, header + 16, 0)
            struct.pack_into('<I', storage, header + 16, zlib.crc32(storage[header:header + 92]))
        if both:
            self.tail[:16384] = self.head[1024:1024 + 16384]
            self.reseal(False)


class GptTests(unittest.TestCase):
    def test_generate_preserves_extents_numbers_types_attrs_and_changes_all_guids(self):
        src = Reader()
        plan = fresh.prepare_gpt(u, src, src.size + 24 * fresh.MIB)
        self.assertEqual(plan['layout']['userdata']['start'], src.parts[-1][1] * 512)
        self.assertEqual(plan['layout']['userdata']['start'] + plan['layout']['userdata']['len'],
                         (plan['last_lba'] - 32) * 512)
        old = fresh.read_gpt(u, src)
        previous = {old['disk_guid']} | {p['guid'] for p in old['layout'].values()}
        generated = {plan['disk_guid']} | {p['guid'] for p in plan['layout'].values()}
        self.assertEqual(len(generated), len(plan['layout']) + 1)
        self.assertFalse(previous & generated)
        for name, p in old['layout'].items():
            for key in ('n', 'start', 'type', 'attrs') + (() if name == 'userdata' else ('len',)):
                self.assertEqual(p[key], plan['layout'][name][key])
        # Validate generated primary and backup using the real reader/parser too.
        dst = Reader(size=plan['size'])
        dst.head = bytearray(plan['pmbr'] + plan['primary'] + plan['table'])
        dst.tail = bytearray(plan['table'] + plan['backup'])
        self.assertEqual(fresh.read_gpt(u, dst)['layout'], plan['layout'])

    def test_smaller_than_raw_is_allowed_when_payload_and_userdata_fit(self):
        src = Reader()
        self.assertLess(fresh.prepare_gpt(u, src, 48 * fresh.MIB)['size'], src.size)

    def test_target_too_small_and_nonsector_size(self):
        for size in (32 * fresh.MIB, 48 * fresh.MIB + 1):
            with self.subTest(size=size), self.assertRaises(u.Refuse):
                fresh.prepare_gpt(u, Reader(), size)

    def test_backup_header_crc(self):
        src = Reader()
        src.tail[-512 + 16] ^= 1
        with self.assertRaisesRegex(u.Refuse, 'header CRC'):
            fresh.read_gpt(u, src)

    def test_backup_array_crc(self):
        src = Reader()
        src.tail[56] ^= 1
        with self.assertRaisesRegex(u.Refuse, 'array CRC'):
            fresh.read_gpt(u, src)

    def test_backup_valid_array_disagreement(self):
        src = Reader()
        src.tail[48] ^= 1
        src.reseal(False)
        with self.assertRaisesRegex(u.Refuse, 'copies disagree'):
            fresh.read_gpt(u, src)

    def test_overlap_and_outside_usable(self):
        for first, last in ((2048, 4095), (src_last := 96 * fresh.MIB // 512 - 16, src_last + 5)):
            src = Reader()
            struct.pack_into('<QQ', src.head, 1024 + 128 + 32, first, last)
            src.reseal()
            with self.subTest(first=first), self.assertRaises(u.Refuse):
                fresh.read_gpt(u, src)

    def test_duplicate_or_zero_guid(self):
        for guid in (bytes(16), None):
            src = Reader()
            src.head[1024 + 128 + 16:1024 + 128 + 32] = guid if guid is not None else src.head[1040:1056]
            src.reseal()
            with self.subTest(guid=guid), self.assertRaisesRegex(u.Refuse, 'GUID'):
                fresh.read_gpt(u, src)

    def test_unsupported_table_header_and_reciprocal(self):
        for offset, fmt, value in ((80, '<I', 127), (84, '<I', 256), (8, '<I', 0x20000),
                                   (20, '<I', 1), (32, '<Q', 42), (72, '<Q', 3), (48, '<Q', 999999)):
            src = Reader()
            struct.pack_into(fmt, src.head, 512 + offset, value)
            src.reseal()
            with self.subTest(offset=offset), self.assertRaises(u.Refuse):
                fresh.read_gpt(u, src)

    def test_userdata_not_last(self):
        src = Reader()
        parts = list(src.parts)
        a, b = parts[-2], parts[-1]
        parts[-2] = (a[0], b[1], b[2], *a[3:])
        parts[-1] = (b[0], a[1], a[2], *b[3:])
        with self.assertRaisesRegex(u.Refuse, 'physically last'):
            fresh.read_gpt(u, Reader(parts))

    def test_collision_generation_is_bounded(self):
        src = Reader()
        with mock.patch.object(fresh.uuid, 'uuid4', return_value=uuid.UUID(src.disk_guid)) as generator:
            with self.assertRaisesRegex(u.Refuse, 'distinct GUIDs'):
                fresh.prepare_gpt(u, src, src.size)
            self.assertEqual(generator.call_count, 16)

    def test_zero_generation_is_bounded(self):
        src = Reader()
        with mock.patch.object(fresh.uuid, 'uuid4', return_value=uuid.UUID(int=0)) as generator:
            with self.assertRaises(u.Refuse):
                fresh.prepare_gpt(u, src, 96 * fresh.MIB)
            self.assertEqual(generator.call_count, 16)


class FakeFat:
    def __init__(self, files):
        self.files = files
        self.space = (1 << 20, 512)

    def ls(self, path):
        prefix = path.rstrip('/') + '/'
        return sorted({p[len(prefix):].split('/')[0] for p in self.files if p.startswith(prefix)})

    def read(self, path):
        return self.files[path]

    def free(self):
        return self.space


def release():
    kernel, ramdisk = b'kernel stays byte-identical', b'\x02\x21\x4c\x18compressed unchanged'
    bc = b'androidboot.slot_suffix=_a\nandroidboot.boot_part_uuid=old\n'
    files = {'/kernel.efi': kernel, '/initrd.img': ramdisk + u.bc_blob(bc),
             '/loader/loader.conf': b'default lapis-debug.conf\n'}
    options = 'root=/dev/ram0 bootconfig module_blacklist=cros_ec ' + u.LIFT + ' efi=noruntime'
    entries = {}
    for name in u.REQUIRED_ENTRIES:
        text = u.entry_text('Googlebook kano fake', '/kernel.efi', ['/initrd.img'], options)
        files['/loader/entries/' + name] = text.encode()
        entries[name] = u.entry_parse(text)
    return types.SimpleNamespace(esp=FakeFat(files), kernel=kernel, ramdisk=ramdisk, bc=bc, entries=entries,
                                 layout={'boot_a': {'guid': 'old'}})


class BootTests(unittest.TestCase):
    def test_prepare_preserves_compressed_ramdisk_changes_only_boot_guid(self):
        rel = release()
        guid = str(uuid.uuid4())
        boot = fresh.prepare_boot(u, rel, guid)
        ramdisk, bc = u.split_initrd(boot['initrd'])
        self.assertEqual(ramdisk, rel.ramdisk)
        self.assertEqual(u.bc_get(bc, 'androidboot.slot_suffix'), '_a')
        self.assertEqual(u.bc_get(bc, 'androidboot.boot_part_uuid'), guid)
        self.assertEqual(boot['kernel'], rel.kernel)
        self.assertEqual(boot['loader'], b'default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n')
        for text in boot['entries'].values():
            self.assertIn(b'initcall_blacklist=mei_me_driver_init', text)
            self.assertIn(b'module_blacklist=cros_ec', text)
            self.assertIn(b'efi=noruntime', text)

    def test_reject_unknown_previous_and_update_artifacts(self):
        for path in ('/loader/entries/prev-lapis-quiet.conf', '/loader/entries/unknown.conf',
                     '/gbos/update-state.txt', '/gbos-nvme/a/kernel.efi'):
            rel = release()
            rel.esp.files[path] = b'old'
            with self.subTest(path=path), self.assertRaises(u.Refuse):
                fresh.prepare_boot(u, rel, str(uuid.uuid4()))

    def test_insufficient_fat_replacement_space(self):
        rel = release()
        rel.esp.space = (0, 512)
        with self.assertRaisesRegex(u.Refuse, 'replacement space'):
            fresh.prepare_boot(u, rel, str(uuid.uuid4()))

    def test_source_boot_bytes_differ(self):
        for path in ('/kernel.efi', '/initrd.img', '/loader/entries/lapis-quiet.conf'):
            rel = release()
            rel.esp.files[path] += b'mutation'
            with self.subTest(path=path), self.assertRaises(u.Refuse):
                fresh.prepare_boot(u, rel, str(uuid.uuid4()))

    def test_source_boot_guid_must_match_source_gpt(self):
        rel = release()
        rel.layout['boot_a']['guid'] = str(uuid.uuid4())
        with self.assertRaises(u.Refuse):
            fresh.prepare_boot(u, rel, str(uuid.uuid4()))

    def test_slot_b_and_per_slot_paths_refused(self):
        rel = release()
        rel.entries['lapis-quiet.conf']['linux'] = '/gbos/a/kernel.efi'
        with self.assertRaises(u.Refuse):
            fresh.prepare_boot(u, rel, str(uuid.uuid4()))
        rel = release()
        rel.bc = u.bc_set(rel.bc, 'androidboot.slot_suffix', '_b')
        rel.esp.files['/initrd.img'] = rel.ramdisk + u.bc_blob(rel.bc)
        with self.assertRaises(u.Refuse):
            fresh.prepare_boot(u, rel, str(uuid.uuid4()))

    def test_test_environment_refused_before_source_or_target(self):
        with mock.patch.dict(fresh.os.environ, {'GBOS_UPDATE_TEST_ANY': ''}):
            with self.assertRaisesRegex(u.Refuse, 'test environment'):
                fresh.install(u, None, None, None, lambda: None, after_write_check=lambda: None)


if __name__ == '__main__':
    unittest.main()
