#!/usr/bin/env python3
"""Scan extracted image trees for VEX-encoded vpmadd52luq/vpmadd52huq.

Every raw VEX3 candidate in x86 ELF or AMD64 PE executable regions is retained.
Disassembly and GNU-hash validation are annotations, never permission to drop a
candidate. Only exact manifest identities can accept residuals/non-instructions.
Unsupported, unopenable, or too-deep nested images fail closed unless their exact
partition, typed chain and SHA-256 are in unsupported_allowlist.
"""

import argparse
import contextlib
from collections import Counter
import gzip
import hashlib
import io
import json
import lzma
import importlib.util
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from pathlib import Path

sys.dont_write_bytecode = True

PT_LOAD = 1
PT_DYNAMIC = 2
PF_X = 1
PF_R = 4
ET_REL = 1
SHT_NOBITS = 8
SHT_STRTAB = 3
SHT_DYNSYM = 11
SHT_GNU_HASH = 0x6FFFFFF6
SHF_EXECINSTR = 0x4
SHF_ALLOC = 0x2
DT_NULL = 0
DT_STRTAB = 5
DT_SYMTAB = 6
DT_STRSZ = 10
DT_SYMENT = 11
DT_GNU_HASH = 0x6FFFFEF5
EM_386 = 3
EM_X86_64 = 62
SCAN_CHUNK = 1024 * 1024
VEX_INSN_LENGTH = 5
BOOTCONFIG_MAGIC = b"#BOOTCONFIG\n"
BOOTCONFIG_FOOTER_SIZE = 20
LEGACY_LZ4_MAGIC = b"\x02\x21\x4c\x18"
LEGACY_LZ4_MAGIC_INT = 0x184C2102
LEGACY_LZ4_BLOCK_MAX_OUTPUT = 8 * 1024 * 1024
LEGACY_LZ4_COMPRESS_BOUND = (
    LEGACY_LZ4_BLOCK_MAX_OUTPUT + (LEGACY_LZ4_BLOCK_MAX_OUTPUT // 255) + 16
)
IMAGE_SUFFIXES = frozenset({".apex", ".zip", ".img", ".cpio", ".gz", ".lz4", ".xz", ".zst", ".ko"})
OBJDUMP_LINE = re.compile(
    r"^\s*([0-9a-fA-F]+):\s+((?:[0-9a-fA-F]{2}(?:\s+|$))+)(.*)$"
)


class ScanError(Exception):
    """The scan could not safely complete or found a forbidden instruction."""


class ImageOpenError(ScanError):
    """A recognized container cannot be opened at all; an exact pin may permit it.

    PINNABLE ALLOWLIST (the only cases an exact unsupported_allowlist pin may
    accept). Raise ImageOpenError / call _unsupported() ONLY when no byte of the
    object's content was ever decoded, so nothing that could be scanned is lost:
      1. unknown or unsupported image kinds identified by magic (AVB0 vbmeta,
         android-boot, bzip2/lzma-alone/lzo/gzip-old at an offset, unrecognized
         .img), the nested-depth limit, a missing erofs/sparse/ext4 tool, or a
         tool (fsck.erofs, simg2img, debugfs) that refuses the image without
         writing any output (partial output: _refuse_partial_tool_output);
      2. a gzip/xz frame found after leading NULs in an initramfs sequence
         whose decoder produced 0 bytes and is followed only by NULs (the
         direct gzip/xz branches of _scan_single_image never pin);
      3. a legacy LZ4 initramfs/nested stream (not a bzImage payload) under
         the documented 0-output per-chunk rule (lz4 CLI missing, or a
         failed call that wrote nothing);
      4. a ZIP/APEX whose central directory zipfile cannot parse
         (OSError / BadZipFile from ZipFile(); no member was opened);
      5. a bzImage whose declared payload was never decoded
         (devprofile.UndecodablePayloadError: HdrS missing or slice out of
         bounds, unsupported payload format by magic, no decoder installed).
         A legacy-LZ4 bzImage payload follows Linux unlz4() and is never
         pinnable. Any other payload failure is scanned (DecodedPayloadError)
         and refused; a gzip CRC32/ISIZE mismatch is not a failure (Linux
         __gunzip ignores it).
    Everything else - bytes decoded or partly decoded, then any parser or
    decoder error - scans what it has (forbidden hits raised first) and raises
    an unpinnable ScanError. Any exception that is not a ScanError becomes a
    ScanError naming its type (_scan_nested_path, scan_tree); it never escapes
    and never becomes ImageOpenError.
    """


class FailedFrameError(ScanError):
    """A modern zstd/lz4 frame failed to decode: never pinnable."""


class ResetStateError(ScanError):
    """A compressed initramfs member did not decode to complete newc archive(s) plus NULs."""


def _encoding_name(opcode):
    return "vpmadd52luq" if opcode == 0xB4 else "vpmadd52huq"


def _vex_hits(data, base_offset=0):
    """Return exact VEX3 IFMA encodings in *data* as (offset, mnemonic)."""
    hits = []
    index = data.find(b"\xc4")
    while index >= 0:
        if index + VEX_INSN_LENGTH > len(data):
            break
        # C4 RXB.00010 1.vvvv.L.01 B4|B5 ModRM
        if (
            (data[index + 1] & 0x1F) == 0x02
            and (data[index + 2] & 0x83) == 0x81
            and data[index + 3] in (0xB4, 0xB5)
        ):
            hits.append((base_offset + index, _encoding_name(data[index + 3])))
        index = data.find(b"\xc4", index + 1)
    return hits


def _read_exact(stream, offset, length, what):
    stream.seek(offset)
    data = stream.read(length)
    if len(data) != length:
        raise ScanError(f"{what}: truncated at file offset {offset:#x}")
    return data


def _gnu_hash(name):
    value = 5381
    for byte in name:
        value = ((value * 33) + byte) & 0xFFFFFFFF
    return value


def _unique_load_mapping(offset, address, length, loads):
    matches = [
        load for load in loads
        if load[0] <= offset and offset + length <= load[0] + load[2]
        and load[1] <= address and address + length <= load[1] + load[2]
        and offset - load[0] == address - load[1]
        and load[3] & PF_R
    ]
    return len(matches) == 1


def _validated_gnu_hash_ranges(
    stream, size, source, elf_class, shoff, shentsize, shnum,
    sh_fmt, shent_expected, loads, dynamic_segments,
):
    """Return semantically proven GNU-hash file ranges; malformed metadata proves nothing."""
    try:
        if shentsize != shent_expected or shnum in (0, 0xFFFF):
            return []
        if shoff > size or shnum > (size - shoff) // shentsize:
            return []
        sections = []
        for index in range(shnum):
            values = struct.unpack(sh_fmt, _read_exact(
                stream, shoff + index * shentsize, shentsize,
                f"{source}: section header {index}",
            ))
            name, section_type, flags, address, offset, section_size, link, _, align, entsize = values
            if section_type != SHT_NOBITS and (offset > size or section_size > size - offset):
                return []
            sections.append({
                "name": name, "type": section_type, "flags": flags,
                "address": address, "offset": offset, "size": section_size,
                "link": link, "align": align, "entsize": entsize,
            })

        if len(dynamic_segments) != 1:
            return []
        dynamic_offset, dynamic_address, dynamic_size = dynamic_segments[0]
        dynamic_entsize = 8 if elf_class == 1 else 16
        dynamic_fmt = "<II" if elf_class == 1 else "<QQ"
        if (
            not dynamic_size or dynamic_size % dynamic_entsize
            or not _unique_load_mapping(dynamic_offset, dynamic_address, dynamic_size, loads)
        ):
            return []
        tags = {}
        terminated = False
        for cursor in range(0, dynamic_size, dynamic_entsize):
            tag, value = struct.unpack(dynamic_fmt, _read_exact(
                stream, dynamic_offset + cursor, dynamic_entsize, source,
            ))
            if tag == DT_NULL:
                terminated = True
                break
            tags.setdefault(tag, []).append(value)
        if not terminated:
            return []
        required_tags = (DT_GNU_HASH, DT_SYMTAB, DT_STRTAB, DT_STRSZ, DT_SYMENT)
        if any(len(tags.get(tag, ())) != 1 for tag in required_tags):
            return []

        symbol_entsize = 16 if elf_class == 1 else 24
        word_size = 4 if elf_class == 1 else 8
        if tags[DT_SYMENT][0] != symbol_entsize:
            return []
        proven = []
        for section in sections:
            if section["type"] != SHT_GNU_HASH:
                continue
            if section["flags"] != SHF_ALLOC or section["link"] >= len(sections):
                continue
            if tags[DT_GNU_HASH][0] != section["address"]:
                continue
            if not _unique_load_mapping(
                section["offset"], section["address"], section["size"], loads,
            ):
                continue
            dynsym = sections[section["link"]]
            if (
                dynsym["type"] != SHT_DYNSYM
                or dynsym["flags"] != SHF_ALLOC
                or dynsym["entsize"] != symbol_entsize
                or not dynsym["size"]
                or dynsym["size"] % symbol_entsize
                or dynsym["link"] >= len(sections)
                or tags[DT_SYMTAB][0] != dynsym["address"]
                or not _unique_load_mapping(
                    dynsym["offset"], dynsym["address"], dynsym["size"], loads,
                )
            ):
                continue
            strtab = sections[dynsym["link"]]
            if (
                strtab["type"] != SHT_STRTAB
                or strtab["flags"] != SHF_ALLOC
                or not strtab["size"]
                or tags[DT_STRTAB][0] != strtab["address"]
                or tags[DT_STRSZ][0] != strtab["size"]
                or not _unique_load_mapping(
                    strtab["offset"], strtab["address"], strtab["size"], loads,
                )
            ):
                continue
            table = _read_exact(stream, section["offset"], section["size"], source)
            if len(table) < 16:
                continue
            nbuckets, symoffset, bloom_size, bloom_shift = struct.unpack_from("<IIII", table)
            symbol_count = dynsym["size"] // symbol_entsize
            if (
                not nbuckets or not bloom_size or bloom_size & (bloom_size - 1)
                or symoffset > symbol_count or bloom_shift >= word_size * 8
            ):
                continue
            chain_count = symbol_count - symoffset
            bloom_offset = 16
            buckets_offset = bloom_offset + bloom_size * word_size
            chains_offset = buckets_offset + nbuckets * 4
            meaningful_size = chains_offset + chain_count * 4
            if meaningful_size > len(table) or any(table[meaningful_size:]):
                continue

            string_bytes = _read_exact(stream, strtab["offset"], strtab["size"], source)
            hashes = []
            names_valid = True
            for symbol_index in range(symoffset, symbol_count):
                symbol = _read_exact(
                    stream, dynsym["offset"] + symbol_index * symbol_entsize,
                    symbol_entsize, source,
                )
                name_offset = struct.unpack_from("<I", symbol)[0]
                if name_offset >= len(string_bytes):
                    names_valid = False
                    break
                end = string_bytes.find(b"\0", name_offset)
                if end < 0:
                    names_valid = False
                    break
                hashes.append(_gnu_hash(string_bytes[name_offset:end]))
            if not names_valid:
                continue

            expected_bloom = [0] * bloom_size
            expected_buckets = [0] * nbuckets
            expected_chains = []
            bucket_members = [[] for _ in range(nbuckets)]
            word_bits = word_size * 8
            for relative, value in enumerate(hashes):
                symbol_index = symoffset + relative
                bucket = value % nbuckets
                bucket_members[bucket].append(symbol_index)
                bloom_index = (value // word_bits) & (bloom_size - 1)
                expected_bloom[bloom_index] |= (
                    (1 << (value % word_bits))
                    | (1 << ((value >> bloom_shift) % word_bits))
                )
            if any(
                members and members != list(range(members[0], members[-1] + 1))
                for members in bucket_members
            ):
                continue
            for bucket, members in enumerate(bucket_members):
                if members:
                    expected_buckets[bucket] = members[0]
            for relative, value in enumerate(hashes):
                bucket = value % nbuckets
                is_last = relative + 1 == len(hashes) or hashes[relative + 1] % nbuckets != bucket
                expected_chains.append((value & ~1) | int(is_last))

            bloom_fmt = "<" + ("I" if word_size == 4 else "Q") * bloom_size
            actual_bloom = list(struct.unpack_from(bloom_fmt, table, bloom_offset))
            actual_buckets = list(struct.unpack_from(f"<{nbuckets}I", table, buckets_offset))
            actual_chains = list(struct.unpack_from(f"<{chain_count}I", table, chains_offset))
            if (
                actual_bloom != expected_bloom
                or actual_buckets != expected_buckets
                or actual_chains != expected_chains
            ):
                continue
            proven.append((section["offset"], section["offset"] + meaningful_size))
        return proven
    except (OSError, ScanError, struct.error, ValueError):
        return []


def _elf_segments(stream, size, source, allow_truncated=False):
    ident = _read_exact(stream, 0, min(size, 20), source)
    if len(ident) < 4 or ident[:4] != b"\x7fELF":
        return None
    if len(ident) < 20:
        raise ScanError(f"{source}: truncated ELF header")
    elf_class, encoding, version = ident[4], ident[5], ident[6]
    machine_le = struct.unpack_from("<H", ident, 18)[0]
    machine_be = struct.unpack_from(">H", ident, 18)[0]
    if elf_class not in (1, 2) or encoding != 1 or version != 1:
        if EM_386 in (machine_le, machine_be) or EM_X86_64 in (machine_le, machine_be):
            raise ScanError(f"{source}: malformed x86 ELF ident")
        return {"x86": False, "segments": []}
    machine = machine_le
    if machine not in (EM_386, EM_X86_64):
        return {"x86": False, "segments": []}

    elf_type = struct.unpack_from("<H", ident, 16)[0]
    if elf_class == 1:
        header_size, phent_expected = 52, 32
        header = _read_exact(stream, 0, header_size, source)
        phoff = struct.unpack_from("<I", header, 28)[0]
        phentsize, phnum = struct.unpack_from("<HH", header, 42)
        shoff = struct.unpack_from("<I", header, 32)[0]
        shentsize, shnum = struct.unpack_from("<HH", header, 46)
        ph_fmt = "<IIIIIIII"
        sh_fmt, shent_expected = "<IIIIIIIIII", 40
    elif elf_class == 2:
        header_size, phent_expected = 64, 56
        header = _read_exact(stream, 0, header_size, source)
        phoff = struct.unpack_from("<Q", header, 32)[0]
        phentsize, phnum = struct.unpack_from("<HH", header, 54)
        shoff = struct.unpack_from("<Q", header, 40)[0]
        shentsize, shnum = struct.unpack_from("<HH", header, 58)
        ph_fmt = "<IIQQQQQQ"
        sh_fmt, shent_expected = "<IIQQQQIIQQ", 64
    else:
        raise ScanError(f"{source}: unsupported x86 ELF class {elf_class}")

    segments = []
    loads = []
    load_mappings = []
    executable_mappings = []
    dynamic_segments = []
    if phnum:
        if phentsize != phent_expected or phnum == 0xFFFF:
            raise ScanError(f"{source}: unsupported ELF program-header layout")
        if phoff > size or phnum > (size - phoff) // phentsize:
            if allow_truncated and phoff < size:
                phnum = (size - phoff) // phentsize
            else:
                raise ScanError(f"{source}: program-header table runs past end of file")
        for index in range(phnum):
            values = struct.unpack(ph_fmt, _read_exact(
                stream, phoff + index * phentsize, phentsize,
                f"{source}: program header {index}",
            ))
            if elf_class == 1:
                p_type, p_offset, p_vaddr, _, p_filesz, p_memsz, p_flags, _ = values
            else:
                p_type, p_flags, p_offset, p_vaddr, _, p_filesz, p_memsz, _ = values
            if p_offset > size or p_filesz > size - p_offset:
                if not allow_truncated:
                    raise ScanError(f"{source}: program header {index} runs past end of file")
                if p_offset >= size:
                    p_filesz = 0
                    p_memsz = 0
                else:
                    p_filesz = size - p_offset
                    p_memsz = max(p_filesz, p_memsz)
            if p_type == PT_LOAD:
                if p_filesz > p_memsz:
                    raise ScanError(f"{source}: PT_LOAD {index} has p_filesz greater than p_memsz")
                address_limit = 1 << (32 if elf_class == 1 else 64)
                if p_memsz > address_limit - p_vaddr:
                    raise ScanError(f"{source}: PT_LOAD {index} virtual mapping wraps address space")
                loads.append((p_offset, p_vaddr, p_filesz, p_flags))
                load_mappings.append((index, p_offset, p_vaddr, p_filesz, p_memsz, p_flags))
            elif p_type == PT_DYNAMIC:
                dynamic_segments.append((p_offset, p_vaddr, p_filesz))
            if p_type == PT_LOAD and p_flags & PF_X:
                if p_filesz == 0 and p_offset % 4096:
                    raise ScanError(
                        f"{source}: executable PT_LOAD {index} has p_filesz == 0 "
                        f"with non-page-aligned p_offset {p_offset:#x}"
                    )
                region = ("segment", index, p_offset, p_vaddr, p_filesz)
                if p_filesz:
                    segments.append(region)
                if p_memsz:
                    executable_mappings.append((region, p_memsz))
        mapped_loads = sorted(load_mappings, key=lambda item: (item[2], item[0]))
        furthest = None
        furthest_end = -1
        furthest_executable = None
        furthest_executable_end = -1
        for current in mapped_loads:
            if not current[4]:
                continue
            conflicting = None
            if current[5] & PF_X and current[2] < furthest_end:
                conflicting = furthest
            elif current[2] < furthest_executable_end:
                conflicting = furthest_executable
            if conflicting is not None:
                raise ScanError(
                    f"{source}: overlapping executable PT_LOAD mappings "
                    f"{conflicting[0]} and {current[0]}"
                )
            current_end = current[2] + current[4]
            if current_end > furthest_end:
                furthest = current
                furthest_end = current_end
            if current[5] & PF_X and current_end > furthest_executable_end:
                furthest_executable = current
                furthest_executable_end = current_end
    elif elf_type == ET_REL:
        if shentsize != shent_expected or shnum in (0, 0xFFFF):
            raise ScanError(f"{source}: unsupported relocatable ELF section-header layout")
        if shoff > size or shnum > (size - shoff) // shentsize:
            if allow_truncated and shoff < size:
                shnum = (size - shoff) // shentsize
            else:
                raise ScanError(f"{source}: section-header table runs past end of file")
        for index in range(shnum):
            values = struct.unpack(sh_fmt, _read_exact(
                stream, shoff + index * shentsize, shentsize,
                f"{source}: section header {index}",
            ))
            _, sh_type, sh_flags, sh_addr, sh_offset, sh_size = values[:6]
            if sh_type == SHT_NOBITS:
                continue
            if sh_offset > size or sh_size > size - sh_offset:
                if not allow_truncated:
                    raise ScanError(f"{source}: section {index} runs past end of file")
                if sh_offset >= size:
                    sh_size = 0
                else:
                    sh_size = size - sh_offset
            if sh_flags & SHF_EXECINSTR and sh_size:
                segments.append(("section", index, sh_offset, sh_addr, sh_size))
    else:
        raise ScanError(f"{source}: x86 ELF has no program headers")
    gnu_hash_ranges = []
    if elf_type in (2, 3):
        gnu_hash_ranges = _validated_gnu_hash_ranges(
            stream, size, source, elf_class, shoff, shentsize, shnum,
            sh_fmt, shent_expected, loads, dynamic_segments,
        )
    return {
        "x86": True,
        "elf_type": elf_type,
        "segments": segments,
        "executable_mappings": executable_mappings,
        "gnu_hash_ranges": gnu_hash_ranges,
    }


def _pe_segments(stream, size, source, *, effective_suffix=None):
    """Bounded EFI PE32+ AMD64 mappings, not a firmware-loader validator.

    Linux EFI stubs legitimately use zero ImageBase, short optional headers,
    32-byte alignment and virtual slack. Do not impose Windows-only alignment
    or section-name heuristics. The complete raw executable span is scanned;
    max(raw size, VirtualSize) describes its conservative loaded extent.
    """
    suffix = Path(source).suffix.lower() if effective_suffix is None else effective_suffix
    prefix = _read_exact(stream, 0, min(size, 2), source)
    if prefix != b"MZ":
        if suffix == ".efi":
            raise ScanError(f"{source}: EFI executable is missing its MZ header")
        return None
    if size < 64:
        raise ScanError(f"{source}: truncated PE DOS header")
    pe_offset = struct.unpack("<I", _read_exact(stream, 0x3c, 4, source))[0]
    # Existing non-EFI HdrS bzImages may carry MZ without a PE header. Their
    # declared payload still goes through the existing strict decoder. They
    # acquire no PE executable-coverage claim, and .efi never takes this path.
    if pe_offset == 0 and suffix != ".efi" and size >= 0x206:
        if _read_exact(stream, 0x202, 4, source) == b"HdrS":
            return None
    if pe_offset < 64 or pe_offset > size - 24:
        raise ScanError(f"{source}: invalid PE header offset")
    header = _read_exact(stream, pe_offset, 24, source)
    if header[:4] != b"PE\0\0":
        raise ScanError(f"{source}: invalid PE signature")
    machine, count, _, _, _, optional_size, characteristics = struct.unpack_from(
        "<HHIIIHH", header, 4,
    )
    if machine != 0x8664 or not characteristics & 0x0002:
        raise ScanError(f"{source}: unsupported PE machine or non-executable image")
    optional_offset = pe_offset + 24
    if not 0 < count <= 4096 or optional_size < 112 or optional_size > size - optional_offset:
        raise ScanError(f"{source}: invalid PE optional header or section count")
    optional = _read_exact(stream, optional_offset, optional_size, source)
    magic = struct.unpack_from("<H", optional)[0]
    subsystem = struct.unpack_from("<H", optional, 68)[0]
    if magic != 0x20b or subsystem not in (10, 11, 12):
        raise ScanError(f"{source}: unsupported PE optional magic or subsystem")
    directories = struct.unpack_from("<I", optional, 108)[0]
    entry = struct.unpack_from("<I", optional, 16)[0]
    image_base = struct.unpack_from("<Q", optional, 24)[0]
    image_size, headers_size = struct.unpack_from("<II", optional, 56)
    table = optional_offset + optional_size
    if (
        112 + directories * 8 > optional_size
        or count > (size - table) // 40
        or not table + count * 40 <= headers_size <= size
        or not headers_size <= image_size
        or image_base + image_size > 2**64
    ):
        raise ScanError(f"{source}: invalid PE table or image bounds")
    raw_spans, virtual_spans, segments, mappings = [], [], [], []
    for index in range(count):
        section = _read_exact(stream, table + index * 40, 40, source)
        virtual_size, rva, raw_size, raw_offset = struct.unpack_from("<IIII", section, 8)
        flags = struct.unpack_from("<I", section, 36)[0]
        mapped_size = max(virtual_size, raw_size)
        if raw_size:
            if raw_offset < headers_size or raw_offset > size or raw_size > size - raw_offset:
                raise ScanError(f"{source}: PE section {index} raw range is outside the file or headers")
            raw_spans.append((raw_offset, raw_offset + raw_size))
        if mapped_size:
            if rva < headers_size or rva > image_size or mapped_size > image_size - rva:
                raise ScanError(f"{source}: PE section {index} virtual range is outside the image or headers")
            virtual_spans.append((rva, rva + mapped_size))
        if flags & 0x20000000 and mapped_size:
            region = ("pe-section", index, raw_offset, image_base + rva, raw_size)
            mappings.append((region, mapped_size))
            if raw_size:
                segments.append(region)
    for spans in (raw_spans, virtual_spans):
        previous_end = -1
        for start, end in sorted(spans):
            if start < previous_end:
                raise ScanError(f"{source}: overlapping PE section ranges")
            previous_end = end
    if not entry or sum(
        region[3] <= image_base + entry < region[3] + region[4]
        for region in segments
    ) != 1:
        raise ScanError(f"{source}: PE entry point is not uniquely file-backed executable data")
    return {"format": "pe", "x86": True, "segments": segments,
            "executable_mappings": mappings}


def _scan_stream(stream, size, source, allow_truncated=False, *, effective_suffix=None):
    layout = _elf_segments(stream, size, source, allow_truncated=allow_truncated)
    if layout is None:
        layout = _pe_segments(stream, size, source, effective_suffix=effective_suffix)
        if layout is None:
            return {"elf": False, "x86": False, "segments": 0, "bytes": 0, "hits": []}
    elif not layout["x86"]:
        return {"elf": True, "x86": False, "segments": 0, "bytes": 0, "hits": []}
    return _scan_mapped_stream(stream, size, source, layout)


def _scan_mapped_stream(stream, size, source, layout):
    """Shared raw-candidate engine; only ELF PT_LOAD regions expand to pages."""

    found = {}
    def record_hit(file_offset, virtual_address, mnemonic, encoding, region, zero_fill=False):
        region_kind, segment_index, offset, vaddr, filesz = region
        found[(file_offset, virtual_address, encoding)] = {
            "source": source,
            "file_offset": file_offset,
            "virtual_address": virtual_address,
            "segment": segment_index,
            "instruction": mnemonic,
            "encoding_hex": encoding.hex(),
            "_region_kind": region_kind,
            "_region_file_offset": offset,
            "_region_virtual_address": vaddr,
            "_region_size": filesz,
        }
        if zero_fill and layout.get("format") == "pe":
            found[(file_offset, virtual_address, encoding)]["crosses_zero_fill"] = True

    mappings = sorted(
        layout.get("executable_mappings", ()),
        key=lambda item: (item[0][3], item[0][1]),
    )
    scan_groups = []
    # File/VA coalescing is a redundant defence: the virtual-edge scan below
    # independently checks adjacent executable mappings, including tiny spans.
    # PT_LOAD maps file pages, including bytes before/after its declared range.
    pages = []
    for region in layout["segments"]:
        kind, index, offset, vaddr, filesz = region
        if kind == "segment":
            start = offset // 4096 * 4096
            end = min(size, (offset + filesz + 4095) // 4096 * 4096)
            pages.append((kind, index, start, vaddr - (offset - start), end - start))
        else:
            scan_groups.append([region])
    prev_page = None
    prev_page_vend = -1
    for region in sorted(pages, key=lambda item: (item[3], item[1])):
        _, index, start, vstart, length = region
        vend = vstart + ((start + length + 4095) // 4096 * 4096 - start)
        delta = vstart - start
        if (
            prev_page is not None
            and vstart < prev_page_vend
            and delta != (prev_page[3] - prev_page[2])
        ):
            raise ScanError(
                f"{source}: overlapping executable PT_LOAD page mappings "
                f"{prev_page[1]} and {index} with different file deltas"
            )
        if vend > prev_page_vend:
            prev_page = region
            prev_page_vend = vend
    # Only identical file-to-VA mappings may share a scan. Overlapping rounded
    # pages with different deltas are refused above; identical pages coalesce.
    page_group_delta = None
    page_group_end = -1
    for region in sorted(pages, key=lambda item: (item[3] - item[2], item[2])):
        delta = region[3] - region[2]
        if delta == page_group_delta and region[2] <= page_group_end:
            scan_groups[-1].append(region)
        else:
            scan_groups.append([region])
            page_group_delta = delta
            page_group_end = -1
        page_group_end = max(page_group_end, region[2] + region[4])

    bytes_scanned = 0
    for members in scan_groups:
        offset = members[0][2]
        vaddr = members[0][3]
        filesz = max(member[2] + member[4] for member in members) - offset
        cursor = 0
        carry = b""
        while cursor < filesz:
            count = min(SCAN_CHUNK, filesz - cursor)
            block = _read_exact(stream, offset + cursor, count, source)
            combined = carry + block
            combined_offset = offset + cursor - len(carry)
            for file_offset, mnemonic in _vex_hits(combined, combined_offset):
                if offset <= file_offset and file_offset + VEX_INSN_LENGTH <= offset + filesz:
                    virtual_address = vaddr + file_offset - offset
                    region = next(
                        member for member in members
                        if member[3] <= virtual_address < member[3] + member[4]
                    )
                    start = file_offset - combined_offset
                    record_hit(
                        file_offset, virtual_address, mnemonic,
                        combined[start:start + VEX_INSN_LENGTH], region,
                    )
            carry = combined[-(VEX_INSN_LENGTH - 1):]
            cursor += count
            bytes_scanned += count

    overlap = VEX_INSN_LENGTH - 1

    def scan_virtual_edges(edge_mappings):
        # Keep every tail ending at an address: rounded mappings can overlap,
        # so an intervening mapping must not hide a different touching pair.
        tails = {}
        for region, memsz in sorted(edge_mappings, key=lambda item: (item[0][3], item[0][1])):
            _, _, offset, vaddr, filesz = region
            head_size = min(overlap, filesz)
            head = _read_exact(stream, offset, head_size, source) if head_size else b""
            head_mappings = [(offset + index, vaddr + index, region)
                             for index in range(head_size)]
            endings = {}
            for tail, tail_mappings in tails.get(vaddr, [(b"", [])]):
                def scan_edge(block, block_mappings):
                    boundary = tail + block
                    boundary_mappings = tail_mappings + block_mappings
                    for edge_offset, mnemonic in _vex_hits(boundary):
                        if not (edge_offset < len(tail) < edge_offset + VEX_INSN_LENGTH):
                            continue
                        file_offset, virtual_address, hit_region = boundary_mappings[edge_offset]
                        if file_offset is not None:
                            record_hit(file_offset, virtual_address, mnemonic,
                                       boundary[edge_offset:edge_offset + VEX_INSN_LENGTH], hit_region,
                                       any(item[0] is None for item in boundary_mappings[
                                           edge_offset:edge_offset + VEX_INSN_LENGTH]))

                if head_size:
                    scan_edge(head, head_mappings)
                    if filesz >= overlap:
                        tail = _read_exact(stream, offset + filesz - overlap, overlap, source)
                        tail_mappings = [
                            (offset + filesz - overlap + index,
                             vaddr + filesz - overlap + index, region)
                            for index in range(overlap)
                        ]
                    else:
                        tail = (tail + head)[-overlap:]
                        tail_mappings = (tail_mappings + head_mappings)[-overlap:]
                zero_size = memsz - filesz
                if zero_size:
                    zeros = b"\0" * min(overlap, zero_size)
                    zero_mappings = [(None, vaddr + filesz + index, region)
                                     for index in range(len(zeros))]
                    scan_edge(zeros, zero_mappings)
                    tail = (tail + zeros)[-overlap:]
                    tail_mappings = (tail_mappings + zero_mappings)[-overlap:]
                endings[(tail, tuple(tail_mappings))] = (tail, tail_mappings)
            tails.setdefault(vaddr + memsz, []).extend(endings.values())

    # Preserve original PT_LOAD/BSS edges as well as file-backed page edges.
    # They can yield different encodings at the same file offset and VA.
    scan_virtual_edges(mappings)
    page_mappings = []
    for members in scan_groups:
        first = members[0]
        if first[0] == "segment":
            length = max(member[2] + member[4] for member in members) - first[2]
            mapped_length = (first[2] + length + 4095) // 4096 * 4096 - first[2]
            region = (*first[:4], length)
            page_mappings.append((region, mapped_length))
    scan_virtual_edges(page_mappings)
    result = {
        "elf": layout.get("format") != "pe",
        "x86": True,
        "segments": len(layout["segments"]),
        "bytes": bytes_scanned,
        "hits": [found[key] for key in sorted(found)],
        "_elf_type": layout.get("elf_type"),
        "_regions": layout["segments"],
        "_gnu_hash_ranges": layout.get("gnu_hash_ranges", []),
    }
    if layout.get("format") == "pe":
        result["pe"] = True
    return result


def _find_disassembler():
    """Find optional disassemblers only after a raw candidate exists."""
    return tuple(
        tool for tool in (shutil.which("llvm-objdump"), shutil.which("objdump"))
        if tool
    )


def _objdump_output(path, region, stop_address, tool):
    """Disassemble from a known executable region start through a candidate."""
    _, _, _, start_address, _ = region
    cmd = [
        tool,
        "-d",
        "--insn-width=16",
        f"--start-address={start_address:#x}",
        f"--stop-address={stop_address:#x}",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return None
    if proc.returncode:
        return None
    return proc.stdout


def _valid_decoded_instruction(text):
    text = text.strip()
    if not text:
        return False
    lowered = text.lower()
    if "(bad)" in lowered or "<unknown>" in lowered:
        return False
    mnemonic = text.split(None, 1)[0].lower()
    if mnemonic.startswith(".") or mnemonic in ("bad", "unknown", "..."):
        return False
    return True


def _decoded_ranges(path, output, region):
    """Return byte-verified decoded instruction ranges for one mapped region."""
    _, _, region_offset, region_vaddr, region_size = region
    ranges = []
    with Path(path).open("rb") as stream:
        for line in output.splitlines():
            match = OBJDUMP_LINE.match(line)
            if not match or not _valid_decoded_instruction(match.group(3)):
                continue
            try:
                address = int(match.group(1), 16)
                encoded = bytes.fromhex(match.group(2))
            except ValueError:
                continue
            if not encoded or not (region_vaddr <= address < region_vaddr + region_size):
                continue
            file_offset = region_offset + address - region_vaddr
            if file_offset + len(encoded) > region_offset + region_size:
                continue
            stream.seek(file_offset)
            if stream.read(len(encoded)) != encoded:
                continue
            ranges.append((address, address + len(encoded)))
    return ranges


def _confirm_raw_hits(path, result):
    """Annotate candidates without removing any raw hit."""
    result["raw_candidates"] = len(result["hits"])
    result["annotated_candidates"] = []
    if not result["hits"] or result.get("_elf_type") not in (2, 3):
        return result
    tools = _find_disassembler()
    if isinstance(tools, (str, os.PathLike)):
        tools = (tools,)

    regions = result.get("_regions", ())
    gnu_hash_ranges = result.get("_gnu_hash_ranges", ())
    for hit in result["hits"]:
        metadata_matches = [
            (start, end) for start, end in gnu_hash_ranges
            if start <= hit["file_offset"]
            and hit["file_offset"] + VEX_INSN_LENGTH <= end
        ]
        if len(metadata_matches) == 1:
            start, end = metadata_matches[0]
            result["annotated_candidates"].append({
                "source": hit["source"],
                "file_offset": hit["file_offset"],
                "virtual_address": hit["virtual_address"],
                "instruction": hit["instruction"],
                "metadata_file_offset_start": start,
                "metadata_file_offset_end": end,
                "reason": "validated-gnu-hash",
            })
            continue
        if hit.get("_region_kind") != "segment":
            continue
        candidate_va = hit["virtual_address"]
        candidate_offset = hit["file_offset"]
        mappings = [
            region for region in regions
            if region[0] == "segment"
            and region[3] <= candidate_va < region[3] + region[4]
            and region[2] + candidate_va - region[3] == candidate_offset
        ]
        if len(mappings) != 1:
            continue
        region = mappings[0]
        disproved = None
        for tool in tools:
            output = _objdump_output(path, region, candidate_va + 16, tool)
            if output is None:
                continue
            for start, end in _decoded_ranges(path, output, region):
                if start < candidate_va < end:
                    disproved = (tool, start, end)
                    break
            if disproved:
                break
        if disproved:
            tool, start, end = disproved
            result["annotated_candidates"].append({
                "source": hit["source"],
                "file_offset": candidate_offset,
                "virtual_address": candidate_va,
                "instruction": hit["instruction"],
                "containing_instruction_start": start,
                "containing_instruction_end": end,
                "disassembler": Path(tool).name,
            })
            continue
    # H1: annotations never remove raw candidates.
    return result


def _public_scan_result(result):
    result.setdefault("raw_candidates", len(result["hits"]))
    result.setdefault("annotated_candidates", [])
    result.pop("_elf_type", None)
    result.pop("_regions", None)
    result.pop("_gnu_hash_ranges", None)
    for hit in result["hits"]:
        for key in tuple(hit):
            if key.startswith("_"):
                hit.pop(key)
    return result


def scan_elf_bytes(data, source="<bytes>"):
    """Scan one in-memory object; intended for tests and ZIP/CPIO members."""
    return _public_scan_result(_scan_stream(io.BytesIO(data), len(data), source))


def scan_file(path, source=None, *, allow_truncated=False, effective_suffix=None):
    """Scan one filesystem file without loading its executable segments whole."""
    path = Path(path)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ScanError(f"{path}: scanner input is not a regular non-symlink file")
    with path.open("rb") as stream:
        result = _scan_stream(
            stream, before.st_size, source or str(path),
            allow_truncated=allow_truncated, effective_suffix=effective_suffix,
        )
        after = os.fstat(stream.fileno())
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise ScanError(f"{path}: file changed while it was being scanned")
    return _public_scan_result(_confirm_raw_hits(path, result))


def _new_report(label):
    return {
        "label": label,
        "regular_files": 0,
        "elf_files": 0,
        "x86_elf_files": 0,
        "pe_files": 0,
        "x86_pe_files": 0,
        "executable_segments": 0,
        "executable_bytes": 0,
        "raw_candidates": 0,
        "annotated_candidates": [],
        "symlinks_skipped": 0,
        "nested_opened": [],
        "unsupported_images": [],
        "hits": [],
        "accepted_residuals": [],
        "accepted_non_instructions": [],
        "accepted_unsupported": [],
    }


def _merge_scan(report, result):
    report["elf_files"] += int(result["elf"])
    report["x86_elf_files"] += int(result["elf"] and result["x86"])
    report["pe_files"] += int(result.get("pe", False))
    report["x86_pe_files"] += int(result.get("pe", False) and result["x86"])
    report["executable_segments"] += result["segments"]
    report["executable_bytes"] += result["bytes"]
    report["raw_candidates"] += result["raw_candidates"]
    report["annotated_candidates"].extend(result["annotated_candidates"])
    report["hits"].extend(result["hits"])


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(SCAN_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _node_identity(node):
    return {"kind": node["kind"], "path": node["path"]}


CHAIN_KINDS = frozenset({"tree_file", "zip_member", "erofs_file", "ext4_file",
    "cpio_member", "gzip_payload", "lz4_payload", "xz_payload", "zstd_payload",
    "bzimage_payload", "sparse_payload", "initramfs_tail", "vendor_ramdisk_entry", "boot_ramdisk"})


def _valid_partition(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9_]*_[ab]", value))


def _normalize_exact_pins(rows, label, candidates=False):
    if not isinstance(rows, list):
        raise ScanError(f"{label} must be a list")
    result = []
    for row in rows:
        required = {"partition", "container_chain", "sha256", "reason"}
        if candidates:
            required |= {"file_offset", "encoding_hex"}
        if not isinstance(row, dict) or set(row) != required:
            raise ScanError(f"{label}: invalid fields")
        if not _valid_partition(row["partition"]) or not isinstance(row["reason"], str) or not row["reason"].strip():
            raise ScanError(f"{label}: invalid partition/reason")
        if not isinstance(row["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise ScanError(f"{label}: invalid sha256")
        chain = row["container_chain"]
        if not isinstance(chain, list) or not chain:
            raise ScanError(f"{label}: empty container chain")
        for node in chain:
            if (not isinstance(node, dict) or set(node) != {"kind", "path"}
                or node["kind"] not in CHAIN_KINDS or not isinstance(node["path"], str)
                or not node["path"] or node["path"].startswith("/")):
                raise ScanError(f"{label}: invalid chain node")
        clean = dict(row, container_chain=[dict(n) for n in chain])
        if candidates:
            try:
                clean["file_offset"] = int(row["file_offset"], 0)
            except (TypeError, ValueError) as exc:
                raise ScanError(f"{label}: invalid offset") from exc
            if clean["file_offset"] < 0 or not isinstance(row["encoding_hex"], str) or not re.fullmatch(r"[0-9a-f]{10}", row["encoding_hex"]):
                raise ScanError(f"{label}: invalid offset/encoding")
        if clean in result:
            raise ScanError(f"{label}: duplicate pin")
        result.append(clean)
    return result


def _unsupported(path, display, provenance, state, reason):
    digest = _sha256_file(path)
    record = {"source": display, "partition": state["partition"],
        "container_chain": _chain_identity(provenance), "sha256": digest, "reason": reason}
    state["report"]["unsupported_images"].append(record)
    matches = [p for p in state["applicable_unsupported"]
        if p["partition"] == state["partition"]
        and _chain_identity(p["container_chain"]) == _chain_identity(provenance)
        and p["sha256"] == digest]
    if len(matches) != 1:
        raise ScanError(f"{display}: unsupported nested image: {reason}; sha256={digest}")
    state["report"]["accepted_unsupported"].append(dict(matches[0]))


def _normalize_residuals(residuals):
    normalized = []
    for index, source in enumerate(residuals or ()):
        if not isinstance(source, dict):
            raise ScanError(f"residual policy {index}: row must be an object")
        required = {"partition", "container_chain", "file_offset", "instruction", "reason"}
        if set(source) != required:
            raise ScanError(
                f"residual policy {index}: fields must be exactly {sorted(required)}"
            )
        if not _valid_partition(source["partition"]):
            raise ScanError(f"residual policy {index}: invalid partition")
        if source["instruction"] not in ("vpmadd52luq", "vpmadd52huq"):
            raise ScanError(f"residual policy {index}: invalid instruction")
        if not isinstance(source["reason"], str) or not source["reason"].strip():
            raise ScanError(f"residual policy {index}: reason must be non-empty")
        try:
            file_offset = int(source["file_offset"], 0)
        except (TypeError, ValueError) as exc:
            raise ScanError(f"residual policy {index}: invalid file_offset") from exc
        chain = source["container_chain"]
        if not isinstance(chain, list) or not chain:
            raise ScanError(f"residual policy {index}: container_chain must be non-empty")
        clean_chain = []
        for node_index, node in enumerate(chain):
            if not isinstance(node, dict):
                raise ScanError(f"residual policy {index}: chain node {node_index} must be an object")
            if set(node) - {"kind", "path", "sha256"} or not {"kind", "path"} <= set(node):
                raise ScanError(f"residual policy {index}: invalid chain node {node_index}")
            if node["kind"] not in CHAIN_KINDS:
                raise ScanError(f"residual policy {index}: invalid chain kind {node['kind']!r}")
            if not isinstance(node["path"], str) or not node["path"] or node["path"].startswith("/"):
                raise ScanError(f"residual policy {index}: invalid chain path")
            clean = _node_identity(node)
            if "sha256" in node:
                value = node["sha256"]
                if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise ScanError(f"residual policy {index}: invalid sha256")
                clean["sha256"] = value
            clean_chain.append(clean)
        if "sha256" not in clean_chain[-1] or (len(clean_chain) >= 2 and clean_chain[-1]["kind"] == "erofs_file" and "sha256" not in clean_chain[-2]):
            raise ScanError(
                f"residual policy {index}: nested image and final binary must both pin sha256"
            )
        normalized.append({
            "partition": source["partition"],
            "container_chain": clean_chain,
            "file_offset": file_offset,
            "instruction": source["instruction"],
            "reason": source["reason"].strip(),
        })
    return normalized


def _chain_identity(chain):
    return [_node_identity(node) for node in chain]


def _observe_object(path, provenance, state):
    """Verify hashes as soon as a pinned object is encountered, hit or no hit."""
    identity = _chain_identity(provenance)
    digest = None
    for policy in state["object_policies"]:
        expected = policy["container_chain"]
        if len(identity) > len(expected) or identity != _chain_identity(expected[:len(identity)]):
            continue
        expected_hash = expected[len(identity) - 1].get("sha256") if identity else None
        if expected_hash:
            digest = digest or _sha256_file(path)
            if digest != expected_hash:
                raise ScanError(
                    f"pinned residual object changed at {identity[-1]['kind']}:{identity[-1]['path']}: "
                    f"expected sha256={expected_hash}, actual={digest}"
                )
    return digest


def _merge_file_scan(
    path, source, provenance, state, *, allow_truncated_elf=False, effective_suffix=None,
):
    digest = _observe_object(path, provenance, state)
    try:
        result = scan_file(path, source, effective_suffix=effective_suffix)
    except ScanError:
        if not allow_truncated_elf:
            raise
        result = scan_file(path, source, allow_truncated=True, effective_suffix=effective_suffix)
    if result["elf"]:
        _coverage_object(path, source, provenance, state,
                         "x86_elf" if result["x86"] else "non_x86_elf")
    elif result.get("pe"):
        _coverage_object(path, source, provenance, state, "x86_pe")
    if result["hits"]:
        digest = digest or _sha256_file(path)
        for hit in result["hits"]:
            hit["partition"] = state["partition"]
            hit["container_chain"] = [dict(node) for node in provenance]
            hit["sha256"] = digest
    _merge_scan(state["report"], result)


def _accepted_identity(policy):
    return {
        "partition": policy["partition"],
        "container_chain": policy["container_chain"],
        "file_offset": f"{policy['file_offset']:#x}",
        "instruction": policy["instruction"],
        "reason": policy["reason"],
    }


def _pin_counts(rows):
    return Counter(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows)


def _partition_hits(state):
    forbidden = []
    non_instructions = []
    accepted = []
    for hit in state["report"]["hits"]:
        non_matches = [p for p in state["applicable_non_instructions"]
            if hit["partition"] == p["partition"]
            and _chain_identity(hit["container_chain"]) == _chain_identity(p["container_chain"])
            and hit["sha256"] == p["sha256"]
            and hit["file_offset"] == p["file_offset"]
            and hit["encoding_hex"] == p["encoding_hex"]]
        if len(non_matches) == 1:
            non_instructions.append(non_matches[0])
            continue
        matches = [
            policy for policy in state["applicable_residuals"]
            if hit["partition"] == policy["partition"]
            and _chain_identity(hit["container_chain"]) == _chain_identity(policy["container_chain"])
            and hit["file_offset"] == policy["file_offset"]
            and hit["instruction"] == policy["instruction"]
        ]
        if len(matches) != 1:
            forbidden.append(hit)
            continue
        policy = matches[0]
        final_hash = policy["container_chain"][-1].get("sha256")
        if final_hash is None or hit["sha256"] != final_hash:
            forbidden.append(hit)
            continue
        accepted.append(_accepted_identity(policy))
    return forbidden, non_instructions, accepted


def _raise_if_forbidden_hits(state):
    if not state["report"]["hits"]:
        return
    forbidden, _, _ = _partition_hits(state)
    if forbidden:
        state["report"]["hits"] = forbidden
        first = forbidden[0]
        raise ScanError(
            f"{state['report']['label']}: found {len(forbidden)} forbidden VEX IFMA encoding(s); "
            f"first is {first['instruction']} at {first['source']}+{first['file_offset']:#x}"
        )


def _apply_residual_policy(state):
    forbidden, non_instructions, accepted = _partition_hits(state)
    if forbidden:
        state["report"]["hits"] = forbidden
        first = forbidden[0]
        raise ScanError(
            f"{state['report']['label']}: found {len(forbidden)} forbidden VEX IFMA encoding(s); "
            f"first is {first['instruction']} at {first['source']}+{first['file_offset']:#x}"
        )
    if _pin_counts(accepted) != _pin_counts([_accepted_identity(p) for p in state["applicable_residuals"]]):
        missing = [
            _accepted_identity(policy) for policy in state["applicable_residuals"]
            if _accepted_identity(policy) not in accepted
        ]
        raise ScanError(
            f"{state['report']['label']}: pinned residual missing or not uniquely observed: "
            + json.dumps(missing, sort_keys=True, separators=(",", ":"))
        )
    if _pin_counts(non_instructions) != _pin_counts(state["applicable_non_instructions"]):
        raise ScanError("known_non_instructions pin missing or not uniquely observed")
    state["report"]["hits"] = []
    state["report"]["accepted_residuals"] = accepted
    state["report"]["accepted_non_instructions"] = non_instructions


def _tool(name, otatools):
    if otatools:
        candidate = Path(otatools) / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


def _tool_env(otatools):
    env = os.environ.copy()
    if otatools:
        lib64 = str(Path(otatools) / "lib64")
        current = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = lib64 if not current else f"{lib64}:{current}"
    return env


def _image_kind_from_prefix(prefix):
    """Classify supported and explicitly unsupported container envelopes."""
    # Recognise these envelopes even under renamed files. They require an exact
    # unsupported-object pin until a complete decoder is available.
    for magic, kind in ((b"BZ", "bzip2"), (b"\x5d\x00", "lzma-alone"),
                        (b"\x89L", "lzo"), (b"\x1f\x9e", "gzip-old"),
                        (b"ANDROID!", "android-boot"),
                        (b"VNDRBOOT", "android-vendor-boot")):
        if prefix.startswith(magic):
            return kind
    if prefix[:4] == b"PK\x03\x04" or prefix[:4] == b"PK\x05\x06" or prefix[:4] == b"PK\x07\x08":
        return "zip"
    if prefix[1024:1028] == b"\xe2\xe1\xf5\xe0":
        return "erofs"
    if prefix[:4] == b"\x3a\xff\x26\xed":
        return "android-sparse"
    if prefix[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if prefix[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    if prefix[0x202:0x206] == b"HdrS":
        return "bzimage"
    if prefix[:2] == b"\x1f\x8b":
        return "gzip"
    if prefix[:6] in (b"070701", b"070702"):
        return "cpio-newc"
    if prefix[:4] in (b"\x04\x22\x4d\x18", b"\x02\x21\x4c\x18"):
        return "lz4"
    if prefix[1080:1082] == b"\x53\xef":
        return "ext4"
    if prefix[:4] == b"AVB0":
        return "avb-vbmeta"
    return None


def _image_kind(path):
    with path.open("rb") as stream:
        return _image_kind_from_prefix(stream.read(1082))


def _leading_initramfs_member(path):
    """Return (offset, kind) for a NUL-padded initramfs or opaque container."""
    with Path(path).open("rb") as stream:
        offset = 0
        while True:
            block = stream.read(SCAN_CHUNK)
            if not block:
                return None
            nonzero = len(block) - len(block.lstrip(b"\0"))
            offset += nonzero
            if nonzero != len(block):
                break
        stream.seek(offset)
        kind = _image_kind_from_prefix(stream.read(1082))
    if kind == "cpio-newc":
        return None if offset % 4 else (offset, kind)
    if kind in (
        "gzip", "xz", "zstd", "lz4",
        "bzip2", "lzma-alone", "lzo", "gzip-old", "android-boot", "android-vendor-boot",
    ):
        return offset, kind
    return None


def _safe_member(name, source):
    member = Path(name)
    if member.is_absolute() or any(part in ("", ".", "..") for part in member.parts):
        raise ScanError(f"{source}: unsafe archive member {name!r}")


def _materialize(stream, destination):
    with Path(destination).open("wb") as out:
        shutil.copyfileobj(stream, out, 1024 * 1024)


def _gzip_header_end(stream, position, limit=None):
    """Parse a gzip header like Linux lib/decompress_inflate.c:__gunzip."""
    def read_bounded(length):
        if limit is not None:
            length = max(0, min(length, limit - stream.tell()))
        return stream.read(length)

    def skip_cstring():
        while True:
            byte = read_bounded(1)
            if not byte:
                return False
            if byte == b"\0":
                return True

    stream.seek(position)
    header = read_bounded(10)
    if (
        len(header) != 10
        or header[:2] != b"\x1f\x8b"
        or header[2] != 8
        or (header[3] & 0xE0) != 0
    ):
        return None
    flags = header[3]
    if flags & 0x04:
        raw_xlen = read_bounded(2)
        if len(raw_xlen) != 2:
            return None
        xlen = int.from_bytes(raw_xlen, "little")
        if len(read_bounded(xlen)) != xlen:
            return None
    if (flags & 0x08) and not skip_cstring():
        return None
    if (flags & 0x10) and not skip_cstring():
        return None
    if (flags & 0x02) and len(read_bounded(2)) != 2:
        return None
    return stream.tell()


def _salvage_partial_decompress(
    stream, payload_start, chunk_start, bad_chunk, kind, decoded_output,
):
    """Re-decode up to the exact failing byte in *bad_chunk* to keep partial output."""
    def make_decoder():
        return (
            zlib.decompressobj(-zlib.MAX_WBITS)
            if kind == "gzip"
            else lzma.LZMADecompressor(format=lzma.FORMAT_AUTO)
        )

    def feed_piece(decoder, piece, write_out):
        if kind == "gzip":
            pending = piece
            while pending:
                data = decoder.decompress(pending, 64 * 1024)
                if write_out and data:
                    decoded_output.write(data)
                pending = decoder.unconsumed_tail
                if decoder.eof:
                    break
        else:
            data = decoder.decompress(piece, max_length=64 * 1024)
            if write_out and data:
                decoded_output.write(data)
            while not decoder.needs_input and not decoder.eof:
                data = decoder.decompress(b"", max_length=64 * 1024)
                if write_out and data:
                    decoded_output.write(data)

    def replay_prefix(decoder, write_out):
        stream.seek(payload_start)
        remaining = chunk_start - payload_start
        while remaining > 0:
            piece = stream.read(min(64 * 1024, remaining))
            if not piece:
                break
            remaining -= len(piece)
            feed_piece(decoder, piece, write_out)

    probe = make_decoder()
    try:
        replay_prefix(probe, False)
    except (OSError, EOFError, lzma.LZMAError, zlib.error):
        return
    fail_pos = 0
    step = 256
    while fail_pos < len(bad_chunk):
        piece = bad_chunk[fail_pos:fail_pos + step]
        try:
            feed_piece(probe, piece, False)
        except (OSError, EOFError, lzma.LZMAError, zlib.error):
            break
        fail_pos += len(piece)

    decoded_output.seek(0)
    decoded_output.truncate(0)
    decoder = make_decoder()
    try:
        replay_prefix(decoder, True)
        if fail_pos:
            feed_piece(decoder, bad_chunk[:fail_pos], True)
        for byte in bad_chunk[fail_pos:fail_pos + step]:
            feed_piece(decoder, bytes([byte]), True)
    except (OSError, EOFError, lzma.LZMAError, zlib.error):
        pass
    if kind == "gzip":
        try:
            flushed = decoder.flush()
            if flushed:
                decoded_output.write(flushed)
        except zlib.error:
            pass


def _compressed_member_length(
    stream, position, kind, decoded_output=None, limit=None,
):
    """Return one frame's byte length, optionally retaining gzip/xz output."""
    def read_bounded(length):
        if limit is not None:
            length = max(0, min(length, limit - stream.tell()))
        return stream.read(length)

    stream.seek(position)
    if kind in ("gzip", "xz"):
        if kind == "gzip":
            payload_start = _gzip_header_end(stream, position, limit=limit)
            if payload_start is None:
                return None
            decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        else:
            payload_start = position
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_AUTO)
        stream.seek(payload_start)
        while True:
            chunk_start = stream.tell()
            chunk = read_bounded(64 * 1024)
            if not chunk:
                if kind == "gzip":
                    try:
                        flushed = decoder.flush()
                        if decoded_output is not None and flushed:
                            decoded_output.write(flushed)
                    except zlib.error:
                        return None
                    if decoder.eof:
                        deflate_end = stream.tell() - len(decoder.unused_data)
                        return (deflate_end - position) + 8
                return None
            try:
                if kind == "gzip":
                    pending = chunk
                    while pending:
                        decoded = decoder.decompress(pending, 64 * 1024)
                        if decoded_output is not None:
                            decoded_output.write(decoded)
                        pending = decoder.unconsumed_tail
                        if decoder.eof:
                            deflate_end = stream.tell() - len(decoder.unused_data)
                            return (deflate_end - position) + 8
                else:
                    decoded = decoder.decompress(chunk, max_length=64 * 1024)
                    if decoded_output is not None:
                        decoded_output.write(decoded)
                    while not decoder.needs_input and not decoder.eof:
                        decoded = decoder.decompress(b"", max_length=64 * 1024)
                        if decoded_output is not None:
                            decoded_output.write(decoded)
                    if decoder.eof:
                        return stream.tell() - position - len(decoder.unused_data)
            except (OSError, EOFError, lzma.LZMAError, zlib.error):
                if decoded_output is not None:
                    _salvage_partial_decompress(
                        stream, payload_start, chunk_start, chunk, kind,
                        decoded_output,
                    )
                return None
    prefix = read_bounded(16)
    stream.seek(position)
    if kind == "lz4" and prefix[:4] == b"\x04\x22\x4d\x18":
        header = read_bounded(6)
        if len(header) != 6:
            return None
        flags = header[4]
        if flags >> 6 != 1 or flags & 0x02:
            return None
        extra = (8 if flags & 0x08 else 0) + (4 if flags & 0x01 else 0)
        if len(read_bounded(extra + 1)) != extra + 1:
            return None
        while True:
            raw_size = read_bounded(4)
            if len(raw_size) != 4:
                return None
            block_size = struct.unpack("<I", raw_size)[0]
            if block_size == 0:
                if flags & 0x04 and len(read_bounded(4)) != 4:
                    return None
                return stream.tell() - position
            payload_size = block_size & 0x7FFFFFFF
            if not payload_size or payload_size > 4 * 1024 * 1024:
                return None
            step = payload_size + (4 if flags & 0x10 else 0)
            if len(read_bounded(step)) != step:
                return None
    if kind == "lz4" and prefix[:4] == LEGACY_LZ4_MAGIC:
        stream.seek(position + 4)
        saw_block = False
        while True:
            boundary = stream.tell()
            raw_size = read_bounded(4)
            if len(raw_size) != 4:
                stream.seek(boundary)
                return boundary - position
            block_size = struct.unpack("<I", raw_size)[0]
            if block_size == LEGACY_LZ4_MAGIC_INT:
                continue
            if block_size == 0:
                stream.seek(boundary)
                return boundary - position
            if block_size > LEGACY_LZ4_COMPRESS_BOUND:
                if saw_block:
                    stream.seek(boundary)
                    next_prefix = read_bounded(6)
                    next_kind = _image_kind_from_prefix(next_prefix)
                    if next_kind == "cpio-newc":
                        eff_limit = limit
                        if eff_limit is None:
                            stream.seek(0, os.SEEK_END)
                            eff_limit = stream.tell()
                        try:
                            if _cpio_archive_length(
                                stream, boundary, eff_limit, "",
                            ) is not None:
                                return boundary - position
                        except ImageOpenError:
                            pass
                    elif (
                        next_kind in ("gzip", "xz", "zstd", "lz4")
                        and next_prefix[:4] != LEGACY_LZ4_MAGIC
                    ):
                        if _compressed_member_length(
                            stream, boundary, next_kind, limit=limit,
                        ) is not None:
                            return boundary - position
                return None
            if len(read_bounded(block_size)) != block_size:
                # Standard gzip headers can also encode a plausible little-
                # endian LZ4 block size (`\x1f\x8b\x08\x00` = 559,903 <=
                # compressBound). A complete declared LZ4 block wins; only a
                # truncated declared block (or a header above compressBound)
                # may instead be a validated gzip tail. Accepting that subset
                # of zero-gap legacy-LZ4->gzip transitions is an intentional
                # fail-closed superset of the kernel's unlz4.
                if saw_block and raw_size[:2] == b"\x1f\x8b":
                    stream.seek(boundary)
                    if _compressed_member_length(
                        stream, boundary, "gzip", limit=limit,
                    ) is not None:
                        return boundary - position
                return None
            saw_block = True
    if kind == "zstd" and prefix[:4] == b"\x28\xb5\x2f\xfd":
        stream.seek(position + 4)
        descriptor = read_bounded(1)
        if not descriptor:
            return None
        descriptor = descriptor[0]
        if descriptor & 0x18:
            return None
        single_segment = bool(descriptor & 0x20)
        if not single_segment and len(read_bounded(1)) != 1:
            return None
        dict_size = (0, 1, 2, 4)[descriptor & 3]
        size_flag = descriptor >> 6
        content_size = (1 if single_segment else 0, 2, 4, 8)[size_flag]
        if len(read_bounded(dict_size + content_size)) != dict_size + content_size:
            return None
        while True:
            block_header = read_bounded(3)
            if len(block_header) != 3:
                return None
            value = int.from_bytes(block_header, "little")
            last, block_type, block_size = value & 1, (value >> 1) & 3, value >> 3
            if block_type == 3:
                return None
            stored = 1 if block_type == 1 else block_size
            if len(read_bounded(stored)) != stored:
                return None
            if last:
                if descriptor & 0x04 and len(read_bounded(4)) != 4:
                    return None
                return stream.tell() - position
    return None


def _copy_stream_member(stream, destination, length):
    with Path(destination).open("wb") as output:
        remaining = length
        while remaining:
            block = stream.read(min(SCAN_CHUNK, remaining))
            if not block:
                raise ScanError("compressed CPIO tail was truncated while copying")
            output.write(block)
            remaining -= len(block)


def _cpio_archive_length(stream, position, limit, display):
    """Return one newc archive's exact byte length without copying its tail."""
    start = position
    index = 0
    while True:
        if index > 0:
            if position == limit:
                return position - start
            stream.seek(position)
            peek = stream.read(min(6, limit - position))
            if peek[:1] == b"\0" or (
                _image_kind_from_prefix(peek) in ("gzip", "xz", "zstd", "lz4")
            ):
                return position - start
        if position + 110 > limit:
            raise ImageOpenError(f"{display}: truncated newc header at member {index}")
        stream.seek(position)
        header = stream.read(110)
        if len(header) != 110 or header[:6] not in (b"070701", b"070702"):
            raise ImageOpenError(f"{display}: invalid newc header at member {index}")
        try:
            fields = [int(header[pos:pos + 8], 16) for pos in range(6, 110, 8)]
        except ValueError as exc:
            raise ImageOpenError(f"{display}: invalid newc numeric field") from exc
        filesize, namesize = fields[6], fields[11]
        if namesize < 1:
            raise ImageOpenError(f"{display}: invalid newc name length")
        name_end = position + 110 + namesize
        data_start = name_end + (-((110 + namesize) % 4)) % 4
        data_end = data_start + filesize
        next_position = data_start + filesize + (-filesize) % 4
        if next_position > limit:
            raise ImageOpenError(f"{display}: truncated newc member {index}")
        stream.seek(position + 110)
        name_raw = stream.read(namesize)
        if len(name_raw) != namesize or name_raw[-1:] != b"\0":
            raise ImageOpenError(f"{display}: truncated newc member name")
        name = name_raw[:-1].decode("utf-8", "surrogateescape")
        if name == "TRAILER!!!":
            if filesize:
                raise ImageOpenError(f"{display}: nonempty newc trailer")
            return next_position - start
        position = next_position
        index += 1


def _compressed_module_not_elf(path, display):
    """True when *display* names a .ko.gz/.ko.xz/.ko.zst whose decoded payload
    is not ELF. The caller scans the decoded bytes first and then refuses with
    an unpinnable ScanError (the payload was decoded, so it is never pinnable)."""
    if display.lower().endswith((".ko.gz", ".ko.xz", ".ko.zst")):
        with Path(path).open("rb") as stream:
            return stream.read(4) != b"\x7fELF"
    return False


def _initramfs_payload_size(path, display):
    """Return the initramfs prefix length after validating an Android bootconfig."""
    size = path.stat().st_size
    if size < len(BOOTCONFIG_MAGIC):
        return size
    with path.open("rb") as stream:
        stream.seek(size - len(BOOTCONFIG_MAGIC))
        if stream.read(len(BOOTCONFIG_MAGIC)) != BOOTCONFIG_MAGIC:
            return size
        if size < BOOTCONFIG_FOOTER_SIZE:
            raise ScanError(f"{display}: truncated Android bootconfig footer")
        footer = _read_exact(
            stream, size - BOOTCONFIG_FOOTER_SIZE,
            BOOTCONFIG_FOOTER_SIZE, display,
        )
        bootconfig_size, expected_checksum = struct.unpack_from("<II", footer)
        if bootconfig_size > size - BOOTCONFIG_FOOTER_SIZE:
            raise ScanError(f"{display}: invalid Android bootconfig size")
        payload_size = size - BOOTCONFIG_FOOTER_SIZE - bootconfig_size
        stream.seek(payload_size)
        remaining = bootconfig_size
        checksum = 0
        padding_started = False
        while remaining:
            block = stream.read(min(SCAN_CHUNK, remaining))
            if not block:
                raise ScanError(f"{display}: truncated Android bootconfig")
            # Only text followed by terminal NUL padding is metadata. A
            # checksum-valid footer must not hide a binary archive or ELF.
            for byte in block:
                if byte == 0:
                    padding_started = True
                elif padding_started or not (9 <= byte <= 13 or 32 <= byte <= 126):
                    raise ScanError(f"{display}: invalid Android bootconfig text")
            checksum = (checksum + sum(block)) & 0xFFFFFFFF
            remaining -= len(block)
        if checksum != expected_checksum:
            raise ScanError(f"{display}: invalid Android bootconfig checksum")
        return payload_size


def _copy_prefix(source, destination, length, display):
    with Path(source).open("rb") as input_stream, Path(destination).open("wb") as output:
        remaining = length
        while remaining:
            block = input_stream.read(min(SCAN_CHUNK, remaining))
            if not block:
                raise ScanError(f"{display}: truncated while copying compressed payload")
            output.write(block)
            remaining -= len(block)


def _temporary_path(state, prefix):
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=state["scratch"])
    os.close(descriptor)
    return Path(name)


def _coverage_object(path, display, provenance, state, classification, kind=None):
    """Emit optional one-off audit evidence without enlarging build reports."""
    callback = state.get("coverage_callback")
    if callback is None:
        return
    path = Path(path)
    with path.open("rb") as stream:
        prefix = stream.read(16)
    callback({
        "source": display, "partition": state["partition"],
        "container_chain": [dict(node) for node in provenance],
        "classification": classification, "kind": kind,
        "size": path.stat().st_size, "prefix_hex": prefix.hex(),
        "sha256": _sha256_file(path),
    })


def _stream_tail_is_all_nul(stream, start, limit):
    stream.seek(start)
    remaining = limit - start
    while remaining > 0:
        block = stream.read(min(SCAN_CHUNK, remaining))
        if not block:
            break
        if block.strip(b"\0"):
            return False
        remaining -= len(block)
    return True


STREAM_DECODE_BUFFER_SIZE = 128 * 1024
STREAM_DECOMPRESSOR_TIMEOUT = 30.0
STREAM_DECOMPRESSOR_TIMEOUT_BYTES_PER_SEC = 4 * 1024 * 1024
STREAM_STDERR_TAIL_LIMIT = 8 * 1024


def _stream_decompressor_timeout(input_bytes=0):
    return (
        STREAM_DECOMPRESSOR_TIMEOUT
        + max(0, int(input_bytes)) / STREAM_DECOMPRESSOR_TIMEOUT_BYTES_PER_SEC
    )


def _run_stream_decompressor(
    command, destination, *, env=None, timeout=STREAM_DECOMPRESSOR_TIMEOUT,
):
    """Stream stdout from a decompressor CLI into *destination* with bounded stderr and timeout."""
    proc = subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stderr_chunks = []
    stderr_total = 0

    def drain_stderr():
        nonlocal stderr_total
        try:
            while True:
                piece = proc.stderr.read(64 * 1024)
                if not piece:
                    break
                stderr_chunks.append(piece)
                stderr_total += len(piece)
                while stderr_total > STREAM_STDERR_TAIL_LIMIT and stderr_chunks:
                    overflow = stderr_total - STREAM_STDERR_TAIL_LIMIT
                    if len(stderr_chunks[0]) <= overflow:
                        removed = stderr_chunks.pop(0)
                        stderr_total -= len(removed)
                    else:
                        stderr_chunks[0] = stderr_chunks[0][overflow:]
                        stderr_total -= overflow
        except (OSError, ValueError):
            pass

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    timed_out = False
    deadline = (time.monotonic() + timeout) if timeout is not None else None

    def remaining_timeout():
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def kill_on_timeout():
        nonlocal timed_out
        if proc.poll() is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            return
        except OSError:
            if proc.poll() is not None:
                return
        timed_out = True

    timer = None
    if timeout is not None:
        timer = threading.Timer(timeout, kill_on_timeout)
        timer.daemon = True
        timer.start()
    try:
        with Path(destination).open("ab") as output:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        stderr_thread.join(timeout=remaining_timeout())
        if stderr_thread.is_alive():
            kill_on_timeout()
        try:
            proc.wait(timeout=remaining_timeout())
        except subprocess.TimeoutExpired:
            kill_on_timeout()
            proc.wait()
    except BaseException:
        try:
            proc.kill()
        except OSError:
            pass
        raise
    finally:
        if timer is not None:
            timer.cancel()
        if timed_out:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.stderr.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            kill_on_timeout()
            proc.wait()
        stderr_thread.join(timeout=1.0)
    if timed_out:
        raise ScanError(
            f"stream decompressor timed out after {timeout}s: {command[0]}"
        )
    return proc.returncode, b"".join(stderr_chunks)


def _decode_legacy_lz4_stream(
    stream, position, limit, lz4_tool, otatools, state, destination,
):
    """Decode a legacy LZ4 (0x184C2102) stream with Linux unlz4() continuation rules."""
    effective_limit = limit
    if effective_limit is None:
        cur = stream.tell()
        stream.seek(0, os.SEEK_END)
        effective_limit = stream.tell()
        stream.seek(cur)

    def read_bounded(length):
        remaining = max(0, effective_limit - stream.tell())
        return stream.read(min(length, remaining))

    stream.seek(position)
    if read_bounded(4) != LEGACY_LZ4_MAGIC:
        return False, "invalid legacy lz4 magic", False

    chunk_input = _temporary_path(state, "ifma-lz4-chunk-")
    batch_output = _temporary_path(state, "ifma-lz4-batch-")
    best_output = _temporary_path(state, "ifma-lz4-best-")
    env = _tool_env(otatools)
    chunks = []
    walker_error = None
    walker_unpinnable = False
    saw_block = False
    try:
        with chunk_input.open("wb") as handle:
            handle.write(LEGACY_LZ4_MAGIC)
            while True:
                chunk_offset = stream.tell()
                raw_size = read_bounded(4)
                if len(raw_size) != 4:
                    break
                block_size = struct.unpack("<I", raw_size)[0]
                if block_size == LEGACY_LZ4_MAGIC_INT:
                    continue
                if block_size == 0:
                    break
                if block_size > LEGACY_LZ4_COMPRESS_BOUND:
                    walker_error = "invalid legacy lz4 block size"
                    walker_unpinnable = True
                    break
                available = max(0, effective_limit - stream.tell())
                if available < block_size:
                    stream.seek(stream.tell() + available)
                    walker_error = "truncated legacy lz4 block"
                    walker_unpinnable = (
                        bool(chunks)
                        or available > STREAM_DECODE_BUFFER_SIZE
                        or not _stream_tail_is_all_nul(
                            stream, stream.tell(), effective_limit,
                        )
                    )
                    break
                handle.write(raw_size)
                remaining = block_size
                while remaining > 0:
                    piece = read_bounded(min(SCAN_CHUNK, remaining))
                    if not piece:
                        break
                    handle.write(piece)
                    remaining -= len(piece)
                if remaining != 0:
                    actual = block_size - remaining
                    walker_error = "truncated legacy lz4 block"
                    walker_unpinnable = (
                        bool(chunks)
                        or actual > STREAM_DECODE_BUFFER_SIZE
                        or not _stream_tail_is_all_nul(
                            stream, stream.tell(), effective_limit,
                        )
                    )
                    break
                chunks.append((chunk_offset, block_size, stream.tell()))

        if not chunks:
            if walker_error is not None:
                return False, walker_error, walker_unpinnable
            return True, "", False

        batch_output.write_bytes(b"")
        batch_in_size = chunk_input.stat().st_size
        returncode, batch_stderr = _run_stream_decompressor(
            [lz4_tool, "-dc", str(chunk_input)],
            batch_output,
            env=env,
            timeout=_stream_decompressor_timeout(batch_in_size),
        )
        batch_size = batch_output.stat().st_size if batch_output.exists() else 0
        if returncode == 0:
            if batch_size > len(chunks) * LEGACY_LZ4_BLOCK_MAX_OUTPUT:
                return False, "legacy lz4 block output exceeds 8 MiB", True
            with batch_output.open("rb") as src, Path(destination).open("ab") as dst:
                shutil.copyfileobj(src, dst, SCAN_CHUNK)
            saw_block = True
        else:
            def run_chunk_slice(start_idx, end_idx):
                batch_output.write_bytes(b"")
                with chunk_input.open("wb") as handle:
                    handle.write(LEGACY_LZ4_MAGIC)
                    for idx in range(start_idx, end_idx + 1):
                        c_offset, b_size, _ = chunks[idx]
                        stream.seek(c_offset)
                        remaining = 4 + b_size
                        while remaining > 0:
                            piece = stream.read(min(SCAN_CHUNK, remaining))
                            if not piece:
                                raise ScanError(
                                    "legacy lz4 chunk was truncated while copying"
                                )
                            handle.write(piece)
                            remaining -= len(piece)
                in_bytes = chunk_input.stat().st_size
                return _run_stream_decompressor(
                    [lz4_tool, "-dc", str(chunk_input)],
                    batch_output,
                    env=env,
                    timeout=_stream_decompressor_timeout(in_bytes),
                )

            if len(chunks) == 1:
                lo = 0
                rc, chunk_stderr = returncode, batch_stderr
            else:
                best_output.write_bytes(b"")
                lo, hi = 0, len(chunks) - 1
                while lo < hi:
                    mid = (lo + hi) // 2
                    rc, _ = run_chunk_slice(0, mid)
                    out_size = (
                        batch_output.stat().st_size if batch_output.exists() else 0
                    )
                    if (
                        rc == 0
                        and out_size <= (mid + 1) * LEGACY_LZ4_BLOCK_MAX_OUTPUT
                    ):
                        batch_output, best_output = best_output, batch_output
                        saw_block = True
                        lo = mid + 1
                    else:
                        hi = mid
                if saw_block and best_output.exists() and best_output.stat().st_size > 0:
                    with best_output.open("rb") as src, Path(destination).open("ab") as dst:
                        shutil.copyfileobj(src, dst, SCAN_CHUNK)
                rc, chunk_stderr = run_chunk_slice(lo, lo)
                out_size = batch_output.stat().st_size if batch_output.exists() else 0
                if rc == 0 and out_size > LEGACY_LZ4_BLOCK_MAX_OUTPUT:
                    return False, "legacy lz4 block output exceeds 8 MiB", True

            _, block_size, end_offset = chunks[lo]
            detail_lines = (
                chunk_stderr.decode("utf-8", "replace").strip().splitlines()
            )
            detail = str(detail_lines[-1] if detail_lines else rc)
            failed_output = (
                batch_output.stat().st_size if batch_output.exists() else 0
            )
            if failed_output > 0:
                # The failed call wrote bytes: keep them after the good chunks
                # so the caller's diagnostic scan names any IFMA in them, and
                # never let such a chunk take a pin (W5).
                with batch_output.open("rb") as src, Path(destination).open("ab") as dst:
                    shutil.copyfileobj(src, dst, SCAN_CHUNK)
                detail = f"{detail} (failed call wrote {failed_output} bytes)"
            unpinnable = (
                failed_output > 0
                or saw_block
                or lo > 0
                or len(chunks) > 1
                or walker_error is not None
                or block_size > STREAM_DECODE_BUFFER_SIZE
                or not _stream_tail_is_all_nul(
                    stream, end_offset, effective_limit,
                )
            )
            return False, detail, unpinnable
    finally:
        chunk_input.unlink(missing_ok=True)
        batch_output.unlink(missing_ok=True)
        best_output.unlink(missing_ok=True)

    if walker_error is not None:
        return False, walker_error, True
    return True, "", saw_block


def _lz4_frame_header_checksum(desc):
    prime1 = 0x9E3779B1
    prime2 = 0x85EBCA77
    prime3 = 0xC2B2AE3D
    prime4 = 0x27D4EB2F
    prime5 = 0x165667B1
    acc = (prime5 + len(desc)) & 0xFFFFFFFF
    idx = 0
    while idx + 4 <= len(desc):
        lane = int.from_bytes(desc[idx:idx + 4], "little")
        acc = (acc + (lane * prime3 & 0xFFFFFFFF)) & 0xFFFFFFFF
        acc = (((acc << 17) | (acc >> 15)) * prime4) & 0xFFFFFFFF
        idx += 4
    while idx < len(desc):
        acc = (acc + (desc[idx] * prime5 & 0xFFFFFFFF)) & 0xFFFFFFFF
        acc = (((acc << 11) | (acc >> 21)) * prime1) & 0xFFFFFFFF
        idx += 1
    acc ^= acc >> 15
    acc = (acc * prime2) & 0xFFFFFFFF
    acc ^= acc >> 13
    acc = (acc * prime3) & 0xFFFFFFFF
    acc ^= acc >> 16
    return (acc >> 8) & 0xFF


def _write_terminated_block_prefix(stream, position, prefix_end, kind, destination):
    """Write a complete, end-marked frame containing only blocks up to *prefix_end*."""
    stream.seek(position)
    prefix = stream.read(16)
    if kind == "zstd" and prefix[:4] == b"\x28\xb5\x2f\xfd" and len(prefix) >= 5:
        descriptor = prefix[4]
        single_segment = bool(descriptor & 0x20)
        win_len = 0 if single_segment else 1
        dict_size = (0, 1, 2, 4)[descriptor & 3]
        size_flag = descriptor >> 6
        content_size = (1 if single_segment else 0, 2, 4, 8)[size_flag]
        header_len = 5 + win_len + dict_size + content_size
        win_byte = b"\x88" if single_segment else prefix[5:6]
        stream.seek(position + 5 + win_len)
        dict_bytes = stream.read(dict_size)
        with Path(destination).open("wb") as output:
            output.write(b"\x28\xb5\x2f\xfd")
            output.write(bytes([descriptor & 0x03]))
            output.write(win_byte)
            output.write(dict_bytes)
            stream.seek(position + header_len)
            remaining = max(0, prefix_end - header_len)
            while remaining > 0:
                chunk = stream.read(min(SCAN_CHUNK, remaining))
                if not chunk:
                    raise ScanError("zstd block prefix was truncated while copying")
                output.write(chunk)
                remaining -= len(chunk)
            output.write(b"\x01\x00\x00")
        return
    if kind == "lz4" and prefix[:4] == b"\x04\x22\x4d\x18" and len(prefix) >= 6:
        flags = prefix[4]
        bd = prefix[5:6]
        c_size_len = 8 if (flags & 0x08) else 0
        dict_len = 4 if (flags & 0x01) else 0
        header_len = 6 + c_size_len + dict_len + 1
        stream.seek(position + 6 + c_size_len)
        dict_bytes = stream.read(dict_len)
        desc = bytes([flags & 0x71]) + bd + dict_bytes
        hc = bytes([_lz4_frame_header_checksum(desc)])
        with Path(destination).open("wb") as output:
            output.write(b"\x04\x22\x4d\x18")
            output.write(desc)
            output.write(hc)
            stream.seek(position + header_len)
            remaining = max(0, prefix_end - header_len)
            while remaining > 0:
                chunk = stream.read(min(SCAN_CHUNK, remaining))
                if not chunk:
                    raise ScanError("lz4 block prefix was truncated while copying")
                output.write(chunk)
                remaining -= len(chunk)
            output.write(b"\x00\x00\x00\x00")
        return
    stream.seek(position)
    _copy_stream_member(stream, destination, prefix_end)


def _nonfinal_block_prefix_ends(stream, position, limit, kind):
    """Return end offsets (relative to *position*) of complete non-final blocks."""
    def read_bounded(length):
        if limit is not None:
            length = max(0, min(length, limit - stream.tell()))
        return stream.read(length)

    stream.seek(position)
    prefix = read_bounded(16)
    stream.seek(position)
    if kind == "zstd" and prefix[:4] == b"\x28\xb5\x2f\xfd":
        stream.seek(position + 4)
        descriptor = read_bounded(1)
        if not descriptor:
            return []
        descriptor = descriptor[0]
        if descriptor & 0x18:
            return []
        single_segment = bool(descriptor & 0x20)
        if not single_segment and len(read_bounded(1)) != 1:
            return []
        dict_size = (0, 1, 2, 4)[descriptor & 3]
        size_flag = descriptor >> 6
        content_size = (1 if single_segment else 0, 2, 4, 8)[size_flag]
        if len(read_bounded(dict_size + content_size)) != dict_size + content_size:
            return []
        ends = []
        while True:
            block_header = read_bounded(3)
            if len(block_header) != 3:
                break
            value = int.from_bytes(block_header, "little")
            last, block_type, block_size = value & 1, (value >> 1) & 3, value >> 3
            if block_type == 3:
                break
            stored = 1 if block_type == 1 else block_size
            if len(read_bounded(stored)) != stored:
                break
            if last:
                break
            ends.append(stream.tell() - position)
        return ends
    if kind == "lz4" and prefix[:4] == b"\x04\x22\x4d\x18":
        header = read_bounded(6)
        if len(header) != 6:
            return []
        flags = header[4]
        if flags >> 6 != 1 or flags & 0x02:
            return []
        extra = (8 if flags & 0x08 else 0) + (4 if flags & 0x01 else 0)
        if len(read_bounded(extra + 1)) != extra + 1:
            return []
        ends = []
        while True:
            raw_size = read_bounded(4)
            if len(raw_size) != 4:
                break
            block_size = struct.unpack("<I", raw_size)[0]
            if block_size == 0:
                break
            payload_size = block_size & 0x7FFFFFFF
            if not payload_size or payload_size > 4 * 1024 * 1024:
                break
            step = payload_size + (4 if flags & 0x10 else 0)
            if len(read_bounded(step)) != step:
                break
            block_end = stream.tell() - position
            next_raw = read_bounded(4)
            if not next_raw:
                break
            if len(next_raw) == 4 and struct.unpack("<I", next_raw)[0] == 0:
                break
            ends.append(block_end)
            stream.seek(position + block_end)
        return ends
    return []


def _decode_compressed_cli_stream(
    input_path, destination, kind, tool, otatools, state,
):
    """Run zstd/lz4 streaming decompression and salvage the longest valid prefix on error."""
    input_path = Path(input_path)
    file_size = input_path.stat().st_size
    with input_path.open("rb") as stream:
        magic = stream.read(4)
        if kind == "lz4" and magic == LEGACY_LZ4_MAGIC:
            ok, detail, unpinnable = _decode_legacy_lz4_stream(
                stream, 0, file_size, tool, otatools, state, destination,
            )
            return (
                (0 if ok else 1),
                detail,
                unpinnable or file_size > STREAM_DECODE_BUFFER_SIZE,
            )
        prefix_ends = _nonfinal_block_prefix_ends(stream, 0, file_size, kind)
    env = _tool_env(otatools) if kind == "lz4" else None
    returncode, stderr = _run_stream_decompressor(
        [tool, "-dc", str(input_path)],
        destination,
        env=env,
        timeout=_stream_decompressor_timeout(file_size),
    )
    detail_lines = stderr.decode("utf-8", "replace").strip().splitlines()
    detail = str(detail_lines[-1] if detail_lines else returncode)
    if returncode == 0:
        return 0, "", False
    if prefix_ends:
        prefix_in = _temporary_path(state, f"ifma-{kind}-prefix-in-")
        prefix_out = _temporary_path(state, f"ifma-{kind}-prefix-out-")
        probed = {}
        try:
            def probe_prefix(prefix_end, *, terminate=True):
                key = (prefix_end, terminate)
                if key in probed:
                    return probed[key]
                prefix_out.write_bytes(b"")
                with input_path.open("rb") as stream:
                    if terminate:
                        _write_terminated_block_prefix(
                            stream, 0, prefix_end, kind, prefix_in,
                        )
                    else:
                        _copy_stream_member(stream, prefix_in, prefix_end)
                in_bytes = prefix_in.stat().st_size
                rc, _ = _run_stream_decompressor(
                    [tool, "-dc", str(prefix_in)],
                    prefix_out,
                    env=env,
                    timeout=_stream_decompressor_timeout(in_bytes),
                )
                out_size = prefix_out.stat().st_size if prefix_out.exists() else 0
                if out_size > destination.stat().st_size:
                    shutil.copyfile(prefix_out, destination)
                ok = rc == 0
                probed[key] = ok
                return ok

            lo, hi = 0, len(prefix_ends)
            while lo < hi:
                mid = (lo + hi) // 2
                if probe_prefix(prefix_ends[mid], terminate=True):
                    lo = mid + 1
                else:
                    hi = mid
            if lo > 0:
                probe_prefix(prefix_ends[lo - 1], terminate=True)
            if lo < len(prefix_ends):
                probe_prefix(prefix_ends[lo], terminate=False)
        finally:
            prefix_in.unlink(missing_ok=True)
            prefix_out.unlink(missing_ok=True)
    unpinnable_zero = bool(prefix_ends) or file_size > STREAM_DECODE_BUFFER_SIZE
    return returncode, detail, unpinnable_zero


def _scan_partial_decoded_payload(path, display, provenance, state, depth, kind):
    """Diagnostic scan of bytes written by a FAILED decode.

    Every caller raises an unpinnable ScanError afterwards; this scan only
    names IFMA hits in that error. An error that stops the traversal is
    returned as "<Type>: <message>" (never turned into acceptance) so the
    caller can name it. Any Exception counts (a corrupt member can raise
    lzma.LZMAError, zlib.error, EOFError, ...); forbidden hits found before
    the stop are raised first.
    """
    try:
        _scan_decoded_payload(
            path, display, provenance, state, depth, kind,
            allow_truncated_elf=True,
        )
    except Exception as exc:  # not BaseException: Ctrl-C still stops the build
        _raise_if_forbidden_hits(state)
        return f"{type(exc).__name__}: {exc}"
    _raise_if_forbidden_hits(state)
    return None


def _is_legacy_lz4_file(path):
    with Path(path).open("rb") as stream:
        return stream.read(4) == LEGACY_LZ4_MAGIC


def _refuse_failed_modern_frame(path, display, provenance, state, depth, kind, message):
    """A modern zstd/lz4 frame that failed to decode is ALWAYS an unpinnable
    ScanError, whatever the decoder wrote. Bytes it did write are scanned for
    the error message only; a diagnostic scan error is named, not swallowed."""
    note = None
    if path is not None and Path(path).exists() and Path(path).stat().st_size > 0:
        note = _scan_partial_decoded_payload(path, display, provenance, state, depth, kind)
    if note:
        message = f"{message} (scan of the bytes written before the failure stopped: {note})"
    raise FailedFrameError(message)


def _scan_failed_frame_partial(
    stream, position, limit, display, provenance, state, depth, kind, decoded,
):
    """Decide a failed frame; the boolean is for gzip/xz and legacy LZ4 only.

    Modern zstd/lz4: never returns - always raises the unpinnable
    FailedFrameError (decode failure, decoder missing or unable to start,
    whatever the decoder wrote; written bytes only feed the message).
    gzip/xz: True (unpinnable ScanError) when the in-process decoder produced
    > 0 bytes (scanned first), False otherwise. Legacy LZ4: a failed chunk call
    that wrote any bytes is unpinnable (bytes scanned first); a missing decoder
    returns False; otherwise, when the CLI decode returns, the
    per-chunk rule of _decode_legacy_lz4_stream() decides; when it raises
    ScanError instead (timeout, input truncated while copying a chunk), the
    legacy stream is refused through the same unpinnable
    _refuse_failed_modern_frame() path as a modern frame.
    """
    if kind in ("gzip", "xz"):
        if decoded is None or decoded.stat().st_size == 0:
            return False
        _scan_partial_decoded_payload(
            decoded, display, provenance, state, depth, kind,
        )
        return True
    if kind not in ("zstd", "lz4") or position >= limit:
        return False
    stream.seek(position)
    modern = not (kind == "lz4" and stream.read(4) == LEGACY_LZ4_MAGIC)
    tool = _tool(kind, state["otatools"])
    if not tool:
        if modern:
            raise FailedFrameError(
                f"{display}: {kind} unavailable for the frame at {position:#x}"
            )
        return False
    partial_input = _temporary_path(state, f"ifma-{kind}-partial-in-")
    partial_output = _temporary_path(state, f"ifma-{kind}-partial-out-")
    try:
        stream.seek(position)
        _copy_stream_member(stream, partial_input, limit - position)
        legacy = kind == "lz4" and _is_legacy_lz4_file(partial_input)
        try:
            _, _, unpinnable_zero = _decode_compressed_cli_stream(
                partial_input, partial_output, kind, tool, state["otatools"], state,
            )
        except OSError as exc:
            raise FailedFrameError(
                f"{display}: {kind} decoder could not start for the frame at {position:#x}: {exc}"
            ) from exc
        except ScanError as exc:
            _refuse_failed_modern_frame(
                partial_output, display, provenance, state, depth, kind, str(exc),
            )
        if legacy:
            # Legacy LZ4 keeps the output of good chunks AND whatever a
            # failed call wrote (W5, _decode_legacy_lz4_stream); any kept
            # byte is scanned here and makes the result unpinnable.
            if partial_output.stat().st_size > 0:
                _scan_partial_decoded_payload(
                    partial_output, display, provenance, state, depth, kind,
                )
            return unpinnable_zero
        # Modern zstd/lz4 frame that failed to decode: never pinnable.
        _refuse_failed_modern_frame(
            partial_output, display, provenance, state, depth, kind,
            f"{display}: invalid {kind} stream frame at {position:#x}",
        )
    finally:
        partial_input.unlink(missing_ok=True)
        partial_output.unlink(missing_ok=True)


def _is_decoded_compression_payload(provenance):
    return bool(
        provenance
        and provenance[-1].get("kind") in (
            "gzip_payload",
            "xz_payload",
            "zstd_payload",
            "lz4_payload",
            "bzimage_payload",
        )
    )


def _scan_initramfs_sequence(path, display, provenance, state, depth, suffix, kind):
    """Decode one frame at a time, then walk every following initramfs member."""
    limit = (
        path.stat().st_size
        if _is_decoded_compression_payload(provenance)
        else _initramfs_payload_size(path, display)
    )
    if limit != path.stat().st_size:
        state["report"]["nested_opened"].append({"source": display, "kind": "android-bootconfig"})
    initramfs_member = _in_initramfs_context(provenance, display, state)
    first_reset_error = None
    with path.open("rb") as stream:
        position = 0
        saw_member = False
        while position < limit:
            stream.seek(position)
            prefix = stream.read(min(1082, limit - position))
            if prefix[:1] == b"\0":
                position += len(prefix) - len(prefix.lstrip(b"\0"))
                continue
            frame_kind = _image_kind_from_prefix(prefix)
            child = provenance if position == 0 else provenance + (
                {"kind": "initramfs_tail", "path": f"offset:{position:#x}"},
            )
            source = display if position == 0 else f"{display}!/<tail:{position:#x}>"
            if frame_kind == "cpio-newc":
                tail = _temporary_path(state, "ifma-initramfs-tail-")
                try:
                    try:
                        length = _cpio_archive_length(
                            stream, position, limit, source,
                        )
                    except ImageOpenError as exc:
                        stream.seek(position)
                        _copy_stream_member(stream, tail, limit - position)
                        try:
                            _scan_cpio(tail, source, child, state, depth)
                        except (ImageOpenError, ScanError):
                            pass
                        _raise_if_forbidden_hits(state)
                        raise ScanError(str(exc)) from exc
                    stream.seek(position)
                    _copy_stream_member(stream, tail, length)
                    if position:
                        _observe_object(tail, child, state)
                    _coverage_object(tail, source, child, state, "container", frame_kind)
                    try:
                        consumed = _scan_cpio(tail, source, child, state, depth)
                    except ImageOpenError as exc:
                        _raise_if_forbidden_hits(state)
                        raise ScanError(str(exc)) from exc
                    if consumed != length:
                        _raise_if_forbidden_hits(state)
                        raise ScanError(
                            f"{source}: inconsistent newc archive length "
                            f"({consumed} != {length})"
                        )
                finally:
                    tail.unlink(missing_ok=True)
                saw_member = True
                position += length
                continue
            if frame_kind not in ("gzip", "xz", "lz4", "zstd"):
                _raise_if_forbidden_hits(state)
                raise ScanError(f"{display}: non-zero initramfs tail at {position:#x}")
            decoded = None
            try:
                if frame_kind in ("gzip", "xz"):
                    decoded = _temporary_path(state, f"ifma-{frame_kind}-decoded-")
                    with decoded.open("wb") as output:
                        length = _compressed_member_length(
                            stream, position, frame_kind, output, limit=limit,
                        )
                else:
                    length = _compressed_member_length(
                        stream, position, frame_kind, limit=limit,
                    )
                if (
                    frame_kind == "gzip"
                    and length is not None
                    and position + length > limit
                    and position + length - 8 <= limit
                ):
                    length = limit - position
                if length is None or position + length > limit:
                    if _scan_failed_frame_partial(
                        stream, position, limit, source, child, state, depth,
                        frame_kind, decoded,
                    ):
                        _raise_if_forbidden_hits(state)
                        raise ScanError(
                            f"{display}: invalid {frame_kind} stream frame at {position:#x}"
                        )
                    header_len = (
                        8
                        if prefix[:4] == LEGACY_LZ4_MAGIC
                        else (6 if frame_kind == "xz" else (2 if frame_kind == "gzip" else 4))
                    )
                    if (
                        position > 0
                        and not saw_member
                        and _stream_tail_is_all_nul(stream, position + header_len, limit)
                    ):
                        _coverage_object(
                            path, display, provenance, state, "container", frame_kind,
                        )
                        raise ImageOpenError(
                            f"{display}: invalid {frame_kind} stream frame at {position:#x}"
                        )
                    _raise_if_forbidden_hits(state)
                    raise ScanError(
                        f"{display}: invalid {frame_kind} stream frame at {position:#x}"
                    )
                frame = _temporary_path(state, "ifma-frame-")
                try:
                    stream.seek(position)
                    _copy_stream_member(stream, frame, length)
                    # The outer object was observed already. Later frames are
                    # separate typed objects; decode only their exact byte range.
                    if position:
                        _observe_object(frame, child, state)
                    _coverage_object(frame, source, child, state, "container", frame_kind)
                    try:
                        if decoded is not None:
                            _scan_decoded_payload(
                                decoded, source, child, state, depth, frame_kind,
                                initramfs_member=initramfs_member,
                            )
                        else:
                            _scan_single_image(
                                frame, source, child, state, depth,
                                suffix if position == 0 else "", frame_kind,
                                initramfs_member=initramfs_member,
                            )
                    except ResetStateError as exc:
                        if first_reset_error is None:
                            first_reset_error = exc
                    except FailedFrameError as exc:
                        # Never pinnable: no frame-level or outer pin applies.
                        _raise_if_forbidden_hits(state)
                        raise FailedFrameError(
                            f"{display}: invalid {frame_kind} stream frame at {position:#x}: {exc}"
                        ) from exc
                    except ImageOpenError as exc:
                        if (
                            prefix[:4] == LEGACY_LZ4_MAGIC
                            and position + length < limit
                            and not _stream_tail_is_all_nul(stream, position + length, limit)
                        ):
                            _raise_if_forbidden_hits(state)
                            raise ScanError(
                                f"{display}: invalid {frame_kind} stream frame at {position:#x}: {exc}"
                            ) from exc
                        if (
                            position > 0
                            and not saw_member
                            and position + length < limit
                            and _stream_tail_is_all_nul(stream, position + length, limit)
                        ):
                            limit = position + length
                        if position > 0 and not saw_member and position + length == limit:
                            raise ImageOpenError(
                                f"{display}: invalid {frame_kind} stream frame at {position:#x}: {exc}"
                            ) from exc
                        if saw_member:
                            _raise_if_forbidden_hits(state)
                            raise ScanError(
                                f"{display}: invalid {frame_kind} stream frame at {position:#x}: {exc}"
                            ) from exc
                        _unsupported(frame, source, child, state, str(exc))
                finally:
                    frame.unlink(missing_ok=True)
            finally:
                if decoded is not None:
                    decoded.unlink(missing_ok=True)
            saw_member = True
            position += length
    if first_reset_error is not None:
        _raise_if_forbidden_hits(state)
        raise ScanError(str(first_reset_error)) from first_reset_error


def _scan_partial_cpio_member(payload, source, member_provenance, state, depth, suffix):
    destination = _temporary_path(state, "ifma-cpio-partial-")
    try:
        destination.write_bytes(payload)
        try:
            _merge_file_scan(
                destination, source, member_provenance, state,
                allow_truncated_elf=True,
            )
        except (ScanError, ImageOpenError):
            pass
        try:
            _scan_nested_path(
                destination, source, member_provenance, state, depth,
                suffix=suffix,
            )
        except (ScanError, ImageOpenError):
            pass
    finally:
        destination.unlink(missing_ok=True)
    _raise_if_forbidden_hits(state)


def _scan_cpio(path, display, provenance, state, depth):
    with path.open("rb") as stream:
        index = 0
        while True:
            header = stream.read(110)
            if not header:
                if index > 0:
                    state["report"]["nested_opened"].append({"source": display, "kind": "cpio-newc"})
                    return stream.tell()
                raise ImageOpenError(f"{display}: newc archive has no TRAILER!!!")
            if len(header) != 110 or header[:6] not in (b"070701", b"070702"):
                raise ImageOpenError(f"{display}: invalid newc header at member {index}")
            try:
                fields = [int(header[pos:pos + 8], 16) for pos in range(6, 110, 8)]
            except ValueError as exc:
                raise ImageOpenError(f"{display}: invalid newc numeric field") from exc
            mode, filesize, namesize, expected_check = fields[1], fields[6], fields[11], fields[12]
            if namesize < 1:
                raise ImageOpenError(f"{display}: invalid newc name length")
            name_raw = stream.read(namesize)
            if len(name_raw) != namesize or name_raw[-1:] != b"\0":
                raise ImageOpenError(f"{display}: truncated newc member name")
            name = name_raw[:-1].decode("utf-8", "surrogateescape")
            padding = (-((110 + namesize) % 4)) % 4
            if len(stream.read(padding)) != padding:
                raise ImageOpenError(f"{display}: truncated newc name padding")
            if name == "TRAILER!!!":
                if filesize:
                    raise ImageOpenError(f"{display}: nonempty newc trailer")
                state["report"]["nested_opened"].append({"source": display, "kind": "cpio-newc"})
                return stream.tell()
            _safe_member(name, display)
            payload = stream.read(filesize)
            if len(payload) != filesize:
                if payload and stat.S_ISREG(mode):
                    source = f"{display}!/{name}"
                    member_provenance = provenance + ({"kind": "cpio_member", "path": name},)
                    _scan_partial_cpio_member(
                        payload, source, member_provenance, state, depth + 1,
                        Path(name).suffix.lower(),
                    )
                raise ImageOpenError(f"{display}: truncated newc member {name!r}")
            if header[:6] == b"070702" and (sum(payload) & 0xFFFFFFFF) != expected_check:
                if payload and stat.S_ISREG(mode):
                    source = f"{display}!/{name}"
                    member_provenance = provenance + ({"kind": "cpio_member", "path": name},)
                    _scan_partial_cpio_member(
                        payload, source, member_provenance, state, depth + 1,
                        Path(name).suffix.lower(),
                    )
                raise ImageOpenError(f"{display}: newc CRC mismatch for {name!r}")
            data_padding = (-filesize) % 4
            if len(stream.read(data_padding)) != data_padding:
                if payload and stat.S_ISREG(mode):
                    source = f"{display}!/{name}"
                    member_provenance = provenance + ({"kind": "cpio_member", "path": name},)
                    _scan_partial_cpio_member(
                        payload, source, member_provenance, state, depth + 1,
                        Path(name).suffix.lower(),
                    )
                raise ImageOpenError(f"{display}: truncated newc data padding")
            if stat.S_ISREG(mode):
                source = f"{display}!/{name}"
                member_provenance = provenance + ({"kind": "cpio_member", "path": name},)
                if payload[:4] == b"\x7fELF" or payload[:2] == b"MZ" or Path(name).suffix.lower() == ".efi":
                    destination = _temporary_path(state, "ifma-cpio-")
                    try:
                        destination.write_bytes(payload)
                        _merge_file_scan(destination, source, member_provenance, state)
                    finally:
                        destination.unlink(missing_ok=True)
                else:
                    _merge_scan(state["report"], scan_elf_bytes(payload, source))
                _scan_nested_bytes(
                    payload, source, member_provenance, state, depth + 1,
                    Path(name).suffix.lower(),
                )
            index += 1
    state["report"]["nested_opened"].append({"source": display, "kind": "cpio-newc"})


def _scan_nested_bytes(data, display, provenance, state, depth, suffix=""):
    temp = _temporary_path(state, "ifma-nested-")
    try:
        temp.write_bytes(data)
        _scan_nested_path(temp, display, provenance, state, depth, suffix=suffix)
    finally:
        temp.unlink(missing_ok=True)


# Per-member decode errors of zipfile: deflate (zlib.error), LZMA, bzip2
# (OSError), truncated data (EOFError), CRC/encryption (BadZipFile,
# RuntimeError), unknown method (NotImplementedError). Any other Exception
# (compression.zstd.ZstdError on Python 3.14, struct.error, ...) is treated
# the same way by the catch-all below: a member that cannot be read never
# hides later members, and a ZIP with any such member is never pinnable.
ZIP_MEMBER_DATA_ERRORS = (
    OSError, EOFError, RuntimeError, NotImplementedError, zipfile.BadZipFile,
    lzma.LZMAError, zlib.error,
)


def _scan_zip(path, display, provenance, state, depth):
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        # Allowlist 4: the central directory cannot be parsed; no member
        # was opened, nothing decoded.
        raise ImageOpenError(f"{display}: invalid ZIP/APEX: {exc}") from exc
    except Exception as exc:
        # UnicodeDecodeError (UTF-8 flag, bad name), NotImplementedError
        # (extract version), ...: refused, never pinnable, never a crash.
        raise ScanError(f"{display}: invalid ZIP/APEX: {type(exc).__name__}: {exc}") from exc
    try:
        with archive:
            names = set()
            damaged = []
            for info in archive.infolist():
                _safe_member(info.filename, display)
                if info.filename in names:
                    raise ScanError(f"{display}: duplicate ZIP member {info.filename!r}")
                names.add(info.filename)
                if info.is_dir():
                    continue
                source = f"{display}!/{info.filename}"
                member_provenance = provenance + ({"kind": "zip_member", "path": info.filename},)
                try:
                    _scan_zip_member(archive, info, source, member_provenance, state, depth)
                except ScanError:
                    raise
                except Exception as exc:  # ZIP_MEMBER_DATA_ERRORS and any other
                    # A damaged member cannot be read, but it never hides
                    # the scan of the members after it.
                    damaged.append(f"{info.filename}: {type(exc).__name__}: {exc}")
            if damaged:
                # Member bytes were (partly) read: never pinnable.
                _raise_if_forbidden_hits(state)
                raise ScanError(
                    f"{display}: invalid ZIP/APEX member data: " + "; ".join(damaged)
                )
    except ScanError:
        raise
    except Exception as exc:
        _raise_if_forbidden_hits(state)
        raise ScanError(
            f"{display}: invalid ZIP/APEX member data: {type(exc).__name__}: {exc}"
        ) from exc
    state["report"]["nested_opened"].append({"source": display, "kind": "zip"})


@contextlib.contextmanager
def _partial_member_scan(destination, scan):
    """A ZIP member that fails mid-copy (CRC/decode error after bytes were
    written): scan the partial bytes before they are deleted, so their IFMA
    hits are recorded (reported first by the caller's final, unpinnable
    refusal), then re-raise the copy error. The diagnostic scan's own errors
    never replace that error."""
    try:
        yield
    except Exception:
        if destination.exists() and destination.stat().st_size > 0:
            try:
                scan()
            except Exception:  # not BaseException
                pass
        raise


def _scan_zip_member(archive, info, source, member_provenance, state, depth):
    with archive.open(info, "r") as member:
        prefix = member.read(1082)
        suffix = Path(info.filename).suffix.lower()
        if prefix[:4] == b"\x7fELF" or prefix[:2] == b"MZ" or suffix == ".efi":
            destination = _temporary_path(state, "ifma-zip-")
            try:
                with _partial_member_scan(destination, lambda: _merge_file_scan(
                    destination, source, member_provenance, state, allow_truncated_elf=True,
                )):
                    with destination.open("wb") as out:
                        out.write(prefix)
                        shutil.copyfileobj(member, out, 1024 * 1024)
                _merge_file_scan(destination, source, member_provenance, state)
                if prefix[:2] == b"MZ":
                    # PE outer mappings and an HdrS payload are independent
                    # executable surfaces. A hybrid must follow both routes.
                    _scan_nested_path(
                        destination, source, member_provenance, state,
                        depth + 1, suffix=suffix,
                    )
            finally:
                destination.unlink(missing_ok=True)
            return
        is_nested = (suffix in IMAGE_SUFFIXES
                     or _image_kind_from_prefix(prefix) is not None
                     or prefix.startswith(b"\0"))
        if is_nested:
            destination = _temporary_path(state, "ifma-zip-")
            try:
                with _partial_member_scan(destination, lambda: _scan_nested_path(
                    destination, source, member_provenance, state, depth + 1, suffix=suffix,
                )):
                    with destination.open("wb") as out:
                        out.write(prefix)
                        shutil.copyfileobj(member, out, 1024 * 1024)
                _scan_nested_path(
                    destination, source, member_provenance, state,
                    depth + 1, suffix=suffix,
                )
            finally:
                destination.unlink(missing_ok=True)
            return
        # Reading to EOF makes zipfile validate the member CRC even
        # when the member is not itself a scannable object.
        callback = state.get("coverage_callback")
        digest = hashlib.sha256(prefix) if callback else None
        for block in iter(lambda: member.read(SCAN_CHUNK), b""):
            if digest is not None:
                digest.update(block)
        if callback:
            callback({"source": source, "partition": state["partition"],
                      "container_chain": [dict(n) for n in member_provenance],
                      "classification": "data", "kind": None,
                      "size": info.file_size, "prefix_hex": prefix[:16].hex(),
                      "sha256": digest.hexdigest()})


def _scan_nested_path(path, display, provenance, state, depth, suffix=None):
    kind = _image_kind(path)
    magic_offset = 0
    if kind is None:
        padded = _leading_initramfs_member(path)
        if padded is not None:
            magic_offset, kind = padded
    suffix = suffix if suffix is not None else path.suffix.lower()
    with path.open("rb") as stream:
        prefix = stream.read(4)
        if prefix == b"\x7fELF":
            return
    pe_hybrid = prefix[:2] == b"MZ" and kind == "bzimage"
    if prefix[:2] == b"MZ" and kind is None:
        # Already scanned by _merge_file_scan / scan_elf_bytes; do not falsely
        # reclassify PE executables as ordinary data or count them twice.
        return
    if kind is None and suffix not in IMAGE_SUFFIXES:
        _coverage_object(path, display, provenance, state, "data")
        return
    _observe_object(path, provenance, state)
    if kind not in ("gzip", "xz", "zstd", "lz4", "cpio-newc"):
        _coverage_object(path, display, provenance, state, "container", kind)
    if depth > state["max_depth"]:
        _unsupported(path, display, provenance, state, "nested depth limit")
        return
    try:
        _scan_nested_image(
            path, display, provenance, state, depth, suffix, kind,
            magic_offset=magic_offset,
        )
    except ImageOpenError as exc:
        # The only pinnable exit (see the ImageOpenError allowlist).
        if pe_hybrid:
            _raise_if_forbidden_hits(state)
        _unsupported(path, display, provenance, state, str(exc))
    except ScanError:
        if pe_hybrid:
            _raise_if_forbidden_hits(state)
        raise
    except Exception as exc:
        # Catch-all: an unexpected library/parser exception is refused,
        # never a crash and never a pinnable ImageOpenError.
        _raise_if_forbidden_hits(state)
        raise ScanError(f"{display}: scan failed: {type(exc).__name__}: {exc}") from exc


def _scan_nested_image(
    path, display, provenance, state, depth, suffix, kind, magic_offset=0,
):
    if kind in ("gzip", "xz", "zstd", "lz4", "cpio-newc"):
        _scan_initramfs_sequence(path, display, provenance, state, depth, suffix, kind)
    else:
        _scan_single_image(
            path, display, provenance, state, depth, suffix, kind,
            magic_offset=magic_offset,
        )


STRICT_KIND_SUFFIXES = {
    "gzip": (".gz", ".gzip"),
    "xz": (".xz",),
    "zstd": (".zst", ".zstd"),
    "lz4": (".lz4",),
}
GENERIC_COMPRESSION_SUFFIXES = (".gz", ".gzip", ".xz", ".zst", ".zstd", ".lz4")
DECODED_KIND_SUFFIXES = {
    kind: (*exts, *(ext for ext in GENERIC_COMPRESSION_SUFFIXES if ext not in exts))
    for kind, exts in STRICT_KIND_SUFFIXES.items()
}


def _decoded_inner_suffix(display):
    cleaned = re.sub(
        r"(?:!/<[^>]+>|!member\d+:[a-z0-9_-]+|[!:](?:gzip|xz|zstd|lz4|bzimage|cpio-newc))+$",
        "",
        display,
        flags=re.IGNORECASE,
    )
    markers = display[len(cleaned):]
    layers = [
        match.group(1).lower()
        for match in re.finditer(
            r"(?:!/<|!member\d+:|[!:])(gzip|xz|zstd|lz4)(?:>|$|(?=[!:]))",
            markers,
            flags=re.IGNORECASE,
        )
    ]
    if not layers:
        layers.append(None)
    leaf = cleaned.rsplit("!/", 1)[-1].rsplit("/", 1)[-1].lower()
    for layer_kind in layers:
        all_exts = STRICT_KIND_SUFFIXES.get(layer_kind, GENERIC_COMPRESSION_SUFFIXES)
        for ext in DECODED_KIND_SUFFIXES.get(layer_kind, all_exts):
            if not leaf.endswith(ext):
                continue
            leaf = leaf[:-len(ext)]
            break
    return Path(leaf).suffix.lower()


INITRAMFS_NODE_KINDS = frozenset({
    "vendor_ramdisk_entry",
    "boot_ramdisk",
})
FILE_NODE_KINDS = frozenset({
    "tree_file",
    "erofs_file",
    "ext4_file",
    "zip_member",
    "cpio_member",
})
INITRAMFS_FILE_PREFIXES = (
    "ramdisk",
    "initrd",
    "microdroid_initrd",
    "vendor_ramdisk",
)


def _is_initramfs_filename(path_text):
    leaf = str(path_text).rsplit("!/", 1)[-1].rsplit("/", 1)[-1].lower()
    while True:
        matched = False
        for ext in (*GENERIC_COMPRESSION_SUFFIXES, ".cpio", ".img"):
            if leaf.endswith(ext) and len(leaf) > len(ext):
                leaf = leaf[:-len(ext)]
                matched = True
                break
        if not matched:
            break
    return leaf in ("ramdisk", "initrd") or leaf.startswith(INITRAMFS_FILE_PREFIXES)


def _in_initramfs_context(provenance, display, state=None):
    for node in reversed(provenance):
        kind = node.get("kind")
        if kind in INITRAMFS_NODE_KINDS:
            return True
        if kind in FILE_NODE_KINDS:
            return _is_initramfs_filename(node.get("path", ""))
        if kind not in ("initramfs_tail", "sparse_payload"):
            return False
    if not provenance:
        outer_display = str(display).split("!/", 1)[0]
        return _is_initramfs_filename(outer_display)
    return False


def _decoded_is_cpio_reset_sequence(path, display):
    """Return True iff *path* ends in the Linux init/initramfs.c Reset state."""
    path = Path(path)
    limit = path.stat().st_size
    if limit <= 0:
        return limit == 0
    with path.open("rb") as stream:
        position = 0
        while position < limit:
            stream.seek(position)
            prefix = stream.read(min(SCAN_CHUNK, limit - position))
            if not prefix:
                return False
            if prefix[:1] == b"\0":
                stripped = prefix.lstrip(b"\0")
                position += len(prefix) - len(stripped)
                continue
            if position % 4 != 0 or position + 110 > limit:
                return False
            stream.seek(position)
            header = stream.read(110)
            if len(header) != 110 or header[:6] not in (b"070701", b"070702"):
                return False
            try:
                fields = [int(header[pos:pos + 8], 16) for pos in range(6, 110, 8)]
            except ValueError:
                return False
            filesize, namesize = fields[6], fields[11]
            if namesize < 1:
                return False
            name_end = position + 110 + namesize
            data_start = name_end + (-((110 + namesize) % 4)) % 4
            data_end = data_start + filesize
            next_position = data_end + (-filesize) % 4
            if next_position > limit:
                return False
            stream.seek(position + 110)
            name_raw = stream.read(namesize)
            if len(name_raw) != namesize or name_raw[-1:] != b"\0":
                return False
            name = name_raw[:-1].decode("utf-8", "surrogateescape")
            if name == "TRAILER!!!" and filesize:
                return False
            position = next_position
    return True


def _scan_decoded_payload(
    path, display, provenance, state, depth, kind, *, allow_truncated_elf=False,
    initramfs_member=None,
):
    """Scan one already-decoded compressed payload with its typed provenance.

    A .ko.gz/.ko.xz/.ko.zst whose payload is not ELF is scanned like any other
    decoded payload first, then refused with an unpinnable ScanError."""
    not_elf_module = kind in ("gzip", "xz", "zstd") and _compressed_module_not_elf(path, display)
    try:
        _scan_decoded_payload_body(
            path, display, provenance, state, depth, kind,
            allow_truncated_elf=allow_truncated_elf,
            initramfs_member=initramfs_member,
        )
    except Exception as exc:
        if not not_elf_module:
            raise
        _raise_if_forbidden_hits(state)
        raise ScanError(
            f"{display}: compressed kernel module payload is not ELF "
            f"(scan of the decoded payload stopped: {type(exc).__name__}: {exc})"
        ) from exc
    if not_elf_module:
        _raise_if_forbidden_hits(state)
        raise ScanError(f"{display}: compressed kernel module payload is not ELF")


def _scan_decoded_payload_body(
    path, display, provenance, state, depth, kind, *, allow_truncated_elf=False,
    initramfs_member=None,
):
    state["report"]["nested_opened"].append({"source": display, "kind": kind})
    member = "vmlinux" if kind == "bzimage" else f"<{kind}>"
    child = provenance + ({"kind": kind + "_payload", "path": member},)
    source = f"{display}!/{member}"
    # Diagnostic provenance markers are not a filename; retain the decoded leaf hint.
    effective_suffix = _decoded_inner_suffix(source)
    _merge_file_scan(
        path, source, child, state,
        allow_truncated_elf=allow_truncated_elf, effective_suffix=effective_suffix,
    )
    in_initramfs = (
        _in_initramfs_context(provenance, display, state)
        if initramfs_member is None
        else bool(initramfs_member)
    )
    if (
        not allow_truncated_elf
        and kind in ("gzip", "xz", "zstd", "lz4")
        and in_initramfs
    ):
        reset_ok = _decoded_is_cpio_reset_sequence(path, source)
        inner_suffix = ""
        if reset_ok and (
            _image_kind(path) is not None
            or _leading_initramfs_member(path) is not None
        ):
            inner_suffix = _decoded_inner_suffix(source)
        try:
            _scan_nested_path(
                path, source, child, state, depth + 1,
                suffix=inner_suffix,
            )
        except ScanError:
            _raise_if_forbidden_hits(state)
            if not reset_ok:
                raise ResetStateError(
                    f"{source}: unsupported non-newc initramfs member "
                    "(decoded payload does not end in cpio Reset state)"
                )
            raise
        if not reset_ok:
            _raise_if_forbidden_hits(state)
            raise ResetStateError(
                f"{source}: unsupported non-newc initramfs member "
                "(decoded payload does not end in cpio Reset state)"
            )
        return
    _scan_nested_path(
        path, source, child, state, depth + 1,
        suffix=effective_suffix,
    )


def _refuse_partial_tool_output(output, display, provenance, state, depth, kind, message):
    """A tool (fsck.erofs, simg2img, debugfs) failed but already wrote
    output. Allowlist 1 only covers a refusal with NO output: whatever it
    wrote is scanned (forbidden hits raised first), then the image is
    refused with an unpinnable ScanError."""
    output = Path(output)
    note = None
    try:
        if output.is_dir():
            member_kind = "erofs_file" if kind == "erofs" else "ext4_file"
            _walk_tree(
                output, f"{display}!/<{kind}>", state, depth + 1,
                provenance=provenance, member_kind=member_kind,
            )
        else:
            child = provenance + ({"kind": "sparse_payload", "path": "<raw>"},)
            _merge_file_scan(output, f"{display}!/<raw>", child, state, allow_truncated_elf=True)
            _scan_nested_path(output, f"{display}!/<raw>", child, state, depth + 1, suffix=".img")
    except Exception as exc:  # diagnostic scan; the image is refused anyway
        note = f"{type(exc).__name__}: {exc}"
    _raise_if_forbidden_hits(state)
    if note:
        message = f"{message} (scan of the partial output stopped: {note})"
    raise ScanError(message)


def _tool_wrote_output(output):
    output = Path(output)
    if output.is_dir():
        return any(output.iterdir())
    return output.exists() and output.stat().st_size > 0


def _is_empty_lz4_stream(path):
    with Path(path).open("rb") as stream:
        prefix = stream.read(19)
    if prefix[:4] == LEGACY_LZ4_MAGIC:
        return True
    if prefix[:4] == b"\x04\x22\x4d\x18" and len(prefix) >= 11:
        flags = prefix[4]
        extra = (8 if flags & 0x08 else 0) + (4 if flags & 0x01 else 0)
        end_mark_off = 6 + extra + 1
        if len(prefix) >= end_mark_off + 4:
            return prefix[end_mark_off:end_mark_off + 4] == b"\x00\x00\x00\x00"
    return False


def _scan_single_image(
    path, display, provenance, state, depth, suffix, kind, magic_offset=0, *,
    initramfs_member=None,
):
    """Scan one nested image by kind.

    The "gzip" and "xz" branches are an unreachable defensive refusal:
    gzip/xz members go through _scan_initramfs_sequence, which calls this
    only when nothing was decoded, and _scan_nested_image routes gzip/xz
    there too. They always raise ScanError and are not allowlist sites."""
    if kind == "zip":
        _scan_zip(path, display, provenance, state, depth)
    elif kind == "cpio-newc":
        _scan_cpio(path, display, provenance, state, depth)
    elif kind == "gzip":
        destination = _temporary_path(state, "ifma-gzip-")
        try:
            with path.open("rb") as stream, destination.open("wb") as output:
                length = _compressed_member_length(stream, 0, "gzip", output)
            if length is None:
                if destination.stat().st_size > 0:
                    _scan_partial_decoded_payload(
                        destination, display, provenance, state, depth, kind,
                    )
                    raise ScanError(f"{display}: invalid gzip stream")
                # Unreachable defensive refusal (see docstring).
                raise ScanError(f"{display}: invalid gzip stream")
            _scan_decoded_payload(
                destination, display, provenance, state, depth, kind,
                initramfs_member=initramfs_member,
            )
        finally:
            destination.unlink(missing_ok=True)
    elif kind in ("xz", "zstd", "bzimage"):
        destination = _temporary_path(state, f"ifma-{kind}-")
        try:
            if kind == "xz":
                try:
                    with path.open("rb") as stream, destination.open("wb") as output:
                        length = _compressed_member_length(stream, 0, "xz", output)
                except (OSError, EOFError, lzma.LZMAError) as exc:
                    # Unreachable defensive refusal (see docstring): written
                    # bytes are scanned, and it is always a ScanError.
                    if destination.exists() and destination.stat().st_size > 0:
                        _scan_partial_decoded_payload(
                            destination, display, provenance, state, depth, kind,
                        )
                        raise ScanError(f"{display}: invalid xz stream: {exc}") from exc
                    raise ScanError(f"{display}: invalid xz stream: {exc}") from exc
                if length is None:
                    if destination.stat().st_size > 0:
                        _scan_partial_decoded_payload(
                            destination, display, provenance, state, depth, kind,
                        )
                        raise ScanError(f"{display}: invalid xz stream")
                    raise ScanError(f"{display}: invalid xz stream")
            elif kind == "zstd":
                tool = _tool("zstd", state["otatools"])
                if not tool:
                    # A modern frame that cannot be decoded is never pinnable.
                    raise FailedFrameError(f"{display}: zstd unavailable")
                try:
                    returncode, _, unpinnable_zero = _decode_compressed_cli_stream(
                        path, destination, "zstd", tool, state["otatools"], state,
                    )
                except OSError as exc:
                    raise FailedFrameError(f"{display}: zstd decoder could not start: {exc}") from exc
                except ScanError as exc:
                    _refuse_failed_modern_frame(
                        destination, display, provenance, state, depth, kind, str(exc),
                    )
                if returncode:
                    # A failed zstd frame is never pinnable, whatever the
                    # decoder wrote; written bytes only feed the message.
                    _refuse_failed_modern_frame(
                        destination, display, provenance, state, depth, kind,
                        f"{display}: zstd decompression failed",
                    )
            else:
                spec = importlib.util.spec_from_file_location("ifma_scan_devprofile", Path(__file__).resolve().parents[1] / "lib/devprofile.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                # Only the decode call is guarded: an OSError writing the
                # decoded bytes (ENOSPC, EIO) is not a pinnable open failure.
                try:
                    vmlinux = module._extract_bzimage_payload(path)
                except module.UndecodablePayloadError as exc:
                    # Allowlist 5: the payload was never decoded (HdrS bounds,
                    # unsupported format by magic, no decoder). Legacy LZ4
                    # never lands here (devprofile._unlz4).
                    raise ImageOpenError(f"{display}: bzImage extraction failed: {exc}") from exc
                except module.DecodedPayloadError as exc:
                    # The payload WAS decoded (trailing bytes, bytes after the
                    # ELF span, ambiguity): scan it, then refuse unpinnably.
                    destination.write_bytes(exc.decoded)
                    note = _scan_partial_decoded_payload(
                        destination, display, provenance, state, depth, kind,
                    )
                    suffix_note = f" (scan of the decoded payload stopped: {note})" if note else ""
                    raise ScanError(f"{display}: bzImage extraction failed: {exc}{suffix_note}") from exc
                except Exception as exc:  # not BaseException: Ctrl-C still stops the build
                    # Any other ProfileError/OSError/...: a decoder failed with
                    # no byte kept. Never pinnable.
                    raise ScanError(
                        f"{display}: bzImage extraction failed: {type(exc).__name__}: {exc}"
                    ) from exc
                destination.write_bytes(vmlinux)
            # No handler around the zstd branch or the payload scan: nothing
            # raised after a failed (or successful) modern decode may be
            # rewritten into a pinnable ImageOpenError.
            _scan_decoded_payload(
                destination, display, provenance, state, depth, kind,
                initramfs_member=initramfs_member,
            )
        finally:
            destination.unlink(missing_ok=True)
    elif kind == "lz4":
        lz4 = _tool("lz4", state["otatools"])
        if not lz4:
            if not _is_legacy_lz4_file(path):
                # A modern frame that cannot be decoded is never pinnable.
                raise FailedFrameError(f"{display}: lz4 unavailable")
            _unsupported(path, display, provenance, state, "lz4 unavailable")
            return
        destination = _temporary_path(state, "ifma-lz4-")
        compressed = None
        try:
            payload_size = (
                path.stat().st_size
                if initramfs_member is not None or _is_decoded_compression_payload(provenance)
                else _initramfs_payload_size(path, display)
            )
            lz4_input = path
            if payload_size != path.stat().st_size:
                compressed = _temporary_path(state, "ifma-lz4-input-")
                _copy_prefix(path, compressed, payload_size, display)
                lz4_input = compressed
                state["report"]["nested_opened"].append({
                    "source": display, "kind": "android-bootconfig",
                })
            legacy = _is_legacy_lz4_file(lz4_input)
            try:
                returncode, detail, unpinnable_zero = _decode_compressed_cli_stream(
                    lz4_input, destination, "lz4", lz4, state["otatools"], state,
                )
            except OSError as exc:
                raise FailedFrameError(f"{display}: lz4 decoder could not start: {exc}") from exc
            except ScanError as exc:
                _refuse_failed_modern_frame(
                    destination, display, provenance, state, depth, kind, str(exc),
                )
            if returncode:
                message = f"{display}: lz4 decompression failed: {detail}"
                if not legacy:
                    # A failed modern lz4 frame is never pinnable, whatever
                    # the decoder wrote; written bytes only feed the message.
                    _refuse_failed_modern_frame(
                        destination, display, provenance, state, depth, kind, message,
                    )
                # Legacy LZ4: destination holds the good chunks plus whatever
                # a failed call wrote (W5); kept bytes are scanned here and
                # make the result unpinnable (unpinnable_zero is then True).
                if destination.stat().st_size > 0:
                    _scan_partial_decoded_payload(
                        destination, display, provenance, state, depth, kind,
                    )
                if unpinnable_zero:
                    raise ScanError(message)
                raise ImageOpenError(message)
            if destination.stat().st_size == 0 and not _is_empty_lz4_stream(lz4_input):
                raise ImageOpenError(f"{display}: lz4 decompression produced an empty file")
            _scan_decoded_payload(
                destination, display, provenance, state, depth, kind,
                initramfs_member=initramfs_member,
            )
        finally:
            destination.unlink(missing_ok=True)
            if compressed is not None:
                compressed.unlink(missing_ok=True)
    elif kind == "erofs":
        fsck = _tool("fsck.erofs", state["otatools"])
        if not fsck:
            _unsupported(path, display, provenance, state, "fsck.erofs unavailable")
            return
        extract = Path(tempfile.mkdtemp(prefix="ifma-erofs-", dir=state["scratch"]))
        try:
            proc = subprocess.run(
                [fsck, f"--extract={extract}", "--overwrite", str(path)],
                env=_tool_env(state["otatools"]),
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if proc.returncode:
                detail = (proc.stderr or proc.stdout).strip().splitlines()
                message = f"{display}: fsck.erofs extraction failed: {detail[-1] if detail else proc.returncode}"
                if _tool_wrote_output(extract):
                    _refuse_partial_tool_output(extract, display, provenance, state, depth, "erofs", message)
                raise ImageOpenError(message)
            state["report"]["nested_opened"].append({"source": display, "kind": "erofs"})
            _walk_tree(
                extract, f"{display}!/<erofs>", state, depth + 1,
                provenance=provenance, member_kind="erofs_file",
            )
        finally:
            shutil.rmtree(extract, ignore_errors=True)
    elif kind == "android-sparse":
        simg2img = _tool("simg2img", state["otatools"])
        if not simg2img:
            _unsupported(path, display, provenance, state, "simg2img unavailable")
            return
        raw = _temporary_path(state, "ifma-sparse-")
        try:
            proc = subprocess.run(
                [simg2img, str(path), str(raw)],
                env=_tool_env(state["otatools"]),
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if proc.returncode:
                detail = (proc.stderr or proc.stdout).strip().splitlines()
                message = f"{display}: simg2img failed: {detail[-1] if detail else proc.returncode}"
                if _tool_wrote_output(raw):
                    _refuse_partial_tool_output(raw, display, provenance, state, depth, "sparse", message)
                raise ImageOpenError(message)
            state["report"]["nested_opened"].append({"source": display, "kind": "android-sparse"})
            child = provenance + ({"kind": "sparse_payload", "path": "<raw>"},)
            _merge_file_scan(raw, f"{display}!/<raw>", child, state)
            _scan_nested_path(raw, f"{display}!/<raw>", child, state, depth + 1, suffix=".img")
        finally:
            raw.unlink(missing_ok=True)
    elif kind == "ext4":
        debugfs = _tool("debugfs", state["otatools"])
        if not debugfs:
            _unsupported(path, display, provenance, state, "debugfs unavailable")
            return
        extract = Path(tempfile.mkdtemp(prefix="ifma-ext4-", dir=state["scratch"]))
        try:
            proc = subprocess.run(
                [debugfs, "-R", f"rdump / {extract}", str(path)],
                env=_tool_env(state["otatools"]),
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            combined = "\n".join((proc.stdout, proc.stderr)).strip()
            lowered = combined.lower()
            error_markers = (
                "filesystem not open", "bad magic", "short read", "read-only file system",
                "command not found", "rdump: usage", "while opening", "error:",
            )
            extracted_any = any(extract.iterdir())
            if proc.returncode or any(marker in lowered for marker in error_markers) or not extracted_any:
                detail = combined.splitlines()
                message = (
                    f"{display}: debugfs extraction failed: "
                    f"{detail[-1] if detail else 'no files extracted'}"
                )
                if extracted_any:
                    _refuse_partial_tool_output(extract, display, provenance, state, depth, "ext4", message)
                raise ImageOpenError(message)
            state["report"]["nested_opened"].append({"source": display, "kind": "ext4"})
            _walk_tree(
                extract, f"{display}!/<ext4>", state, depth + 1,
                provenance=provenance, member_kind="ext4_file",
            )
        finally:
            shutil.rmtree(extract, ignore_errors=True)
    elif kind in (
        "bzip2", "lzma-alone", "lzo", "gzip-old", "android-boot", "android-vendor-boot",
    ):
        _unsupported(
            path, display, provenance, state,
            f"unsupported {kind} at {magic_offset:#x}",
        )
    else:
        _unsupported(path, display, provenance, state, "unrecognized or unsupported image format")


def _walk_tree(root, label, state, depth, *, provenance=(), member_kind="tree_file"):
    def walk(directory, relative):
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise ScanError(f"{label}/{relative}: cannot list directory: {exc}") from exc
        for entry in entries:
            rel = Path(relative) / entry.name
            display = f"{label}/{rel.as_posix()}"
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                state["report"]["symlinks_skipped"] += 1
            elif stat.S_ISDIR(info.st_mode):
                walk(Path(entry.path), rel)
            elif stat.S_ISREG(info.st_mode):
                state["report"]["regular_files"] += 1
                entry_provenance = provenance + ({"kind": member_kind, "path": rel.as_posix()},)
                _merge_file_scan(entry.path, display, entry_provenance, state)
                if state["nested"]:
                    _scan_nested_path(
                        Path(entry.path), display, entry_provenance, state, depth,
                        suffix=Path(entry.name).suffix.lower(),
                    )
    walk(root, Path())


def scan_tree(root, **kwargs):
    """Scan a tree and supported nested containers, raising if any IFMA hit remains.

    Unexpected exceptions such as FileNotFoundError / FileExistsError (a
    missing root, an unusable scratch directory) no longer escape: they
    become a ScanError. ImageOpenError is still raised deliberately for the
    pinnable allowlist sites and passes through unchanged."""
    try:
        return _scan_tree(root, **kwargs)
    except ScanError:
        raise
    except Exception as exc:  # not BaseException: Ctrl-C still stops the build
        raise ScanError(f"{root}: scan failed: {type(exc).__name__}: {exc}") from exc


def _scan_tree(
    root, *, label=None, otatools=None, scratch_dir=None, nested=True, max_depth=8,
    residuals=None, provenance=None, root_kind="tree_file",
    known_non_instructions=None, unsupported_allowlist=None,
    coverage_callback=None,
):
    """Scan a tree and supported nested containers, raising if any IFMA hit remains."""
    root = Path(root)
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ScanError(f"scan root is not a non-symlink directory: {root}")
    own_scratch = None
    if scratch_dir is None:
        own_scratch = tempfile.TemporaryDirectory(prefix="ifma-scan-")
        scratch = Path(own_scratch.name)
    else:
        scratch = Path(scratch_dir)
        scratch.mkdir(parents=True, exist_ok=True)
    try:
        try:
            scratch.resolve().relative_to(root.resolve())
        except ValueError:
            pass
        else:
            raise ScanError("scanner scratch directory must be outside the scanned root")
        report = _new_report(label or str(root))
        policies = _normalize_residuals(residuals)
        non_pins = _normalize_exact_pins(known_non_instructions or [], "known_non_instructions", True)
        unsupported_pins = _normalize_exact_pins(unsupported_allowlist or [], "unsupported_allowlist")
        provenance = dict(provenance or {})
        partition = provenance.get("partition")
        base_chain = provenance.get("container_chain", [])
        if partition is not None and not _valid_partition(partition):
            raise ScanError("scan provenance has invalid partition")
        if not isinstance(base_chain, list):
            raise ScanError("scan provenance container_chain must be a list")
        clean_base = []
        for node in base_chain:
            if not isinstance(node, dict) or set(node) != {"kind", "path"}:
                raise ScanError("scan provenance chain nodes require exactly kind and path")
            clean_base.append(_node_identity(node))
        applicable = [
            policy for policy in policies
            if policy["partition"] == partition
            and _chain_identity(policy["container_chain"][:len(clean_base)]) == clean_base
            and len(clean_base) <= len(policy["container_chain"])
        ]
        if any(
            any("sha256" in node for node in policy["container_chain"][:len(clean_base)])
            for policy in applicable
        ):
            raise ScanError("scan provenance may not skip verification of a sha256-pinned ancestor")
        def applicable_exact(rows):
            return [p for p in rows if p["partition"] == partition
                and _chain_identity(p["container_chain"][:len(clean_base)]) == clean_base
                and len(clean_base) <= len(p["container_chain"])]
        non_pins = applicable_exact(non_pins)
        unsupported_pins = applicable_exact(unsupported_pins)
        objects = list(applicable)
        for pin in non_pins + unsupported_pins:
            chain = [dict(n) for n in pin["container_chain"]]
            chain[-1]["sha256"] = pin["sha256"]
            objects.append(dict(pin, container_chain=chain))
        state = {
            "report": report,
            "scratch": scratch,
            "otatools": Path(otatools) if otatools else None,
            "nested": bool(nested),
            "max_depth": int(max_depth),
            "partition": partition,
            "applicable_residuals": applicable,
            "applicable_non_instructions": non_pins,
            "applicable_unsupported": unsupported_pins,
            "object_policies": objects,
            "coverage_callback": coverage_callback,
        }
        try:
            _walk_tree(
                root, report["label"], state, 0,
                provenance=tuple(clean_base), member_kind=root_kind,
            )
        except ScanError:
            raise
        except Exception as exc:
            # Catch-all: nothing escapes scan_tree as a non-ScanError crash.
            _raise_if_forbidden_hits(state)
            raise ScanError(f"{report['label']}: scan failed: {type(exc).__name__}: {exc}") from exc
        _apply_residual_policy(state)
        if _pin_counts(report["accepted_unsupported"]) != _pin_counts(unsupported_pins):
            raise ScanError("unsupported_allowlist pin missing or not uniquely observed")
        return report
    finally:
        if own_scratch is not None:
            own_scratch.cleanup()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--label")
    parser.add_argument("--otatools")
    parser.add_argument("--scratch-dir")
    parser.add_argument("--no-nested", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    try:
        report = scan_tree(
            args.root, label=args.label, otatools=args.otatools,
            scratch_dir=args.scratch_dir, nested=not args.no_nested,
        )
    except (OSError, ScanError) as exc:
        print(f"ifma-scan: refused: {exc}", file=sys.stderr)
        return 1
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"ifma-scan: clean: {report['x86_elf_files']} x86 ELFs, "
        f"{report['x86_pe_files']} AMD64 PEs, "
        f"{report['executable_segments']} executable segments, "
        f"{report['executable_bytes']} executable bytes; "
        f"raw_candidates={report['raw_candidates']}, "
        f"annotated={len(report['annotated_candidates'])}, "
        f"nested={len(report['nested_opened'])}, unsupported={len(report['unsupported_images'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
