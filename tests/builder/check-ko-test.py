#!/usr/bin/env python3
"""Static-only synthetic ELF regressions for the selected GKI module checker."""
from pathlib import Path
import hashlib
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "builder/kmod/check-ko.py"
TAIL = "SMP preempt mod_unload modversions "
FOO = 0x11111111
LAYOUT = 0x22222222
GOOD = [("foo", FOO), ("module_layout", LAYOUT)]


def legacy(records):
    return b"".join(struct.pack("<Q", crc) + name.encode().ljust(56, b"\0") for name, crc in records)


def extended(records):
    return (b"".join(struct.pack("<I", crc) for _, crc in records),
            b"".join(name.encode() + b"\0" for name, _ in records))


def elf(versions=legacy(GOOD), ext=None, legacy_flags=2, ext_flags=2,
        imports=("foo",), tail=TAIL, extra=(), signature=False, modname=None, modinfo_extra=b""):
    strings = b"\0"
    symbols = bytes(24)
    for name in imports:
        symbols += struct.pack("<IBBHQQ", len(strings), 0x10, 0, 0, 0, 0)
        strings += name.encode() + b"\0"
    sections = [("", b"", 0, 0, 0), (".shstrtab", b"", 3, 0, 0),
                (".modinfo", b"vermagic=short-release " + tail.encode() + b"\0"
                 + (b"name=" + modname.encode() + b"\0" if modname is not None else b"") + modinfo_extra, 1, 2, 0),
                (".strtab", strings, 3, 0, 0), (".symtab", symbols, 2, 0, 3)]
    if versions is not None:
        sections.append(("__versions", versions, 1, legacy_flags, 0))
    if ext is not None:
        if ext[0] is not None:
            sections.append(("__version_ext_crcs", ext[0], 1, ext_flags, 0))
        if ext[1] is not None:
            sections.append(("__version_ext_names", ext[1], 1, ext_flags, 0))
    sections.extend(extra)
    shstrings = b"\0"
    name_offsets = []
    for name, *_ in sections:
        name_offsets.append(len(shstrings) if name else 0)
        if name:
            shstrings += name.encode() + b"\0"
    sections[1] = (".shstrtab", shstrings, 3, 0, 0)
    data = bytearray(64)
    headers = []
    for i, (_, body, kind, flags, link) in enumerate(sections):
        data.extend(bytes((-len(data)) % 8))
        offset = len(data)
        data.extend(body)
        headers.append(bytes(64) if i == 0 else struct.pack(
            "<IIQQQQIIQQ", name_offsets[i], kind, flags, 0, offset, len(body), link, 0, 8,
            24 if kind == 2 else 0))
    data.extend(bytes((-len(data)) % 8))
    shoff = len(data)
    data.extend(b"".join(headers))
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    data[:64] = struct.pack("<16sHHIQQQIHHHHHH", ident, 1, 62, 1, 0, 0, shoff, 0,
                            64, 0, 0, 64, len(sections), 1)
    if signature:
        data.extend(b"~Module signature appended~\n")
    return data


passed = failed = 0
with tempfile.TemporaryDirectory(prefix="gbos-check-ko-") as td:
    root = Path(td)

    def expect(name, code, why="", symvers=None, provider=None, **kwargs):
        global passed, failed
        module = root / "module.ko"
        module.write_bytes(elf(**kwargs))
        exports = root / "symvers"
        exports.write_text(symvers if symvers is not None else
                           "0x11111111 foo\n0x22222222 module_layout\n")
        optimize = ["-O"] if sys.flags.optimize else []
        check = CHECK
        options = []
        if provider is not None:
            # Test-only copy pins synthetic bytes; production has no override.
            good_provider = b"synthetic-reviewed-provider"
            fake_sha = hashlib.sha256(good_provider).hexdigest()
            check = root / "check-ko-test-copy.py"
            source = CHECK.read_text().replace(
                "5ddce2fcfe24e9f1451783bb42d7bbdb23b99955fae211c8bc1ae8f32098ad48", fake_sha)
            check.write_text(source)
            provider_file = root / "provider.ko"
            provider_file.write_bytes(good_provider if provider == "good" else b"wrong-provider")
            options = ["--kano-backlight-provider", str(provider_file)]
        result = subprocess.run([sys.executable, *optimize, "-I", "-B", str(check), str(module), str(exports), TAIL, *options],
                                capture_output=True, text=True)
        good = result.returncode == code and (not why or why in result.stdout)
        print(("PASS " if good else "FAIL ") + name + ("" if good else
              f": rc={result.returncode} stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"))
        passed += good
        failed += not good

    provider_symbol = "cros_ec_cmd_xfer_status"
    provider_crc = 0x53f88a75
    provider_versions = legacy([(provider_symbol, provider_crc), ("module_layout", LAYOUT)])
    expect("vendor import accepted only with vetted provider", 0, provider="good", modname="gbos_kbd_backlight",
           imports=(provider_symbol,), versions=provider_versions)
    expect("vendor import denied without opt-in", 1, "not exported by the GKI", modname="gbos_kbd_backlight",
           imports=(provider_symbol,), versions=provider_versions)
    expect("unrecognized provider rejected", 1, "unrecognized backlight provider", provider="wrong",
           modname="gbos_kbd_backlight", imports=(provider_symbol,), versions=provider_versions)
    expect("wrong module rejected", 1, "requires exact unique module name", provider="good", modname="other",
           imports=(provider_symbol,), versions=provider_versions)
    expect("duplicate module names rejected", 1, "requires exact unique module name", provider="good",
           modname="gbos_kbd_backlight", modinfo_extra=b"name=gbos_kbd_backlight\0",
           imports=(provider_symbol,), versions=provider_versions)
    expect("duplicate modinfo sections rejected", 1, "exactly one .modinfo section", provider="good",
           modname="gbos_kbd_backlight", imports=(provider_symbol,), versions=provider_versions,
           extra=((".modinfo", b"name=gbos_kbd_backlight\0", 1, 2, 0),))
    expect("vendor import CRC must match", 1, "CRC of cros_ec_cmd_xfer_status", provider="good",
           modname="gbos_kbd_backlight", imports=(provider_symbol,),
           versions=legacy([(provider_symbol, 3), ("module_layout", LAYOUT)]))
    expect("protected import still rejected", 1, "comes from GKI module", provider="good",
           modname="gbos_kbd_backlight", imports=(provider_symbol, "foo"),
           versions=legacy([(provider_symbol, provider_crc), ("foo", FOO), ("module_layout", LAYOUT)]),
           symvers="0x11111111\tfoo\tprotected_module\tEXPORT_SYMBOL\n0x22222222\tmodule_layout\tvmlinux\tEXPORT_SYMBOL\n")
    expect("other unknown vendor import still rejected", 1, "not exported by the GKI", provider="good",
           modname="gbos_kbd_backlight", imports=(provider_symbol, "unknown"),
           versions=legacy([(provider_symbol, provider_crc), ("unknown", 1), ("module_layout", LAYOUT)]))
    expect("provider symbol collision rejected", 1, "duplicate export record", provider="good",
           modname="gbos_kbd_backlight", imports=(provider_symbol,), versions=provider_versions,
           symvers="0x53f88a75 cros_ec_cmd_xfer_status\n0x22222222 module_layout\n")
    expect("duplicate symvers records rejected", 1, "duplicate export record foo",
           symvers="0x11111111 foo\n0x11111111 foo\n0x22222222 module_layout\n")
    expect("duplicate provider CRC records rejected", 1, "duplicate", provider="good",
           modname="gbos_kbd_backlight", imports=(provider_symbol,),
           versions=legacy([(provider_symbol, provider_crc), (provider_symbol, provider_crc), ("module_layout", LAYOUT)]))
    expect("valid legacy versions with non-imported module_layout", 0)
    expect("module_layout CRC mismatch is checked separately", 1, "CRC of module_layout",
           versions=legacy([("foo", FOO), ("module_layout", 0x99999999)]))
    expect("missing module_layout fails closed", 1, "no CRC recorded for module_layout",
           versions=legacy([("foo", FOO)]))
    expect("module_layout must be exported by vmlinux", 1, "module_layout is not exported by vmlinux",
           symvers="0x11111111\tfoo\tvmlinux\tEXPORT_SYMBOL\n0x22222222\tmodule_layout\tother\tEXPORT_SYMBOL\n")
    expect("module_layout export must exist", 1, "module_layout is not exported by vmlinux",
           symvers="0x11111111 foo\n")
    expect("zero imports still need version proof", 1, "__versions", versions=None, imports=())
    expect("extended-only tables do not permit tail-only vermagic", 1, "__versions",
           versions=None, ext=extended(GOOD))
    expect("legacy versions must not be empty", 1, "__versions", versions=b"")
    expect("legacy versions must be allocated", 1, "__versions", legacy_flags=0)
    expect("legacy record must be whole", 1, "__versions", versions=legacy(GOOD) + b"x")
    expect("legacy CRC high word cannot be masked away", 1, "CRC of module_layout",
           versions=legacy([("foo", FOO), ("module_layout", (1 << 32) | LAYOUT)]))
    expect("duplicate legacy names are ambiguous even with equal CRC", 1, "duplicate",
           versions=legacy(GOOD + [("foo", FOO)]))
    expect("duplicate legacy sections are ambiguous", 1, "duplicate",
           extra=(("__versions", legacy(GOOD), 1, 2, 0),))
    expect("legacy name must terminate", 1, "__versions", versions=struct.pack("<Q", FOO) + b"f" * 56)
    expect("legacy name must not be empty", 1, "__versions", versions=struct.pack("<Q", FOO) + bytes(56))
    expect("valid extended table takes precedence with allocated legacy marker", 0,
           versions=legacy([("marker", 1)]), ext=extended(GOOD))
    expect("consistent legacy and extended tables are accepted", 0, ext=extended(GOOD))
    expect("extended names allow one trailing NUL of real toolchain padding", 0,
           ext=(extended(GOOD)[0], extended(GOOD)[1] + b"\0"))
    expect("extended names allow all-NUL alignment padding", 0,
           ext=(extended(GOOD)[0], extended(GOOD)[1] + bytes(15)))
    expect("extended table cannot borrow missing import from legacy", 1, "no CRC recorded for foo",
           ext=extended([("module_layout", LAYOUT)]))
    expect("extended table cannot borrow missing module_layout from legacy", 1, "no CRC recorded for module_layout",
           ext=extended([("foo", FOO)]))
    expect("conflicting legacy and extended CRCs fail closed", 1, "conflicting",
           ext=extended([("foo", 0x99999999), ("module_layout", LAYOUT)]))
    expect("extended CRCs need matching names section", 1, "extended", ext=(extended(GOOD)[0], None))
    expect("extended names need matching CRCs section", 1, "extended", ext=(None, extended(GOOD)[1]))
    expect("extended pair must not be empty", 1, "extended", ext=(b"", b""))
    expect("extended sections must be allocated", 1, "extended", ext=extended(GOOD), ext_flags=0)
    expect("extended CRCs must be whole", 1, "extended", ext=(extended(GOOD)[0] + b"x", extended(GOOD)[1]))
    expect("extended names must terminate", 1, "extended", ext=(extended(GOOD)[0], extended(GOOD)[1][:-1]))
    expect("extended names must not be empty", 1, "extended", ext=(struct.pack("<I", FOO), b"\0"))
    expect("extended name and CRC counts must match", 1, "extended", ext=(struct.pack("<I", FOO), extended(GOOD)[1]))
    expect("extended CRC count cannot outlast terminated names", 1, "extended",
           ext=(extended(GOOD)[0] + struct.pack("<I", 3), extended(GOOD)[1]))
    expect("extended trailing nonpadding bytes are refused", 1, "extended",
           ext=(extended(GOOD)[0], extended(GOOD)[1] + b"extra\0"))
    expect("duplicate extended names are ambiguous", 1, "duplicate", ext=extended(GOOD + [("foo", FOO)]))
    expect("duplicate extended sections are ambiguous", 1, "duplicate", ext=extended(GOOD),
           extra=(("__version_ext_crcs", extended(GOOD)[0], 1, 2, 0),))
    expect("existing import CRC mismatch guard", 1, "CRC of foo", versions=legacy([("foo", 3), ("module_layout", LAYOUT)]))
    expect("existing missing import CRC guard", 1, "no CRC recorded for foo", versions=legacy([("module_layout", LAYOUT)]))
    expect("existing unknown import guard", 1, "not exported by the GKI", imports=("unknown",))
    expect("existing protected module import guard", 1, "comes from GKI module",
           symvers="0x11111111\tfoo\tother\tEXPORT_SYMBOL\n0x22222222\tmodule_layout\tvmlinux\tEXPORT_SYMBOL\n")
    expect("existing vermagic tail guard", 1, "vermagic tail", tail="SMP wrong ")
    expect("existing module export guard", 1, "module exports symbols", extra=(("__ksymtab", b"x", 1, 2, 0),))
    expect("existing GPL module export guard", 1, "module exports symbols", extra=(("__ksymtab_gpl", b"x", 1, 2, 0),))
    expect("existing signature guard", 1, "module is signed", signature=True)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
