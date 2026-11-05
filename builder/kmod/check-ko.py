#!/usr/bin/env python3
"""Check an out-of-tree module against the lapis GKI kernel before it goes into an image:
  - vermagic: an allocated legacy version table permits comparing the tail against the selected MODVERSIONS GKI;
  - every undefined symbol is exported by vmlinux (vmlinux.symvers of the GKI build), none comes from a GKI module
    (an unsigned module may not use those: MODULE_SIG_PROTECT), and each recorded CRC equals the exported one;
  - module_layout has its own matching vmlinux CRC, even when it is not an undefined import;
  - no symbol is exported by the module, and it carries no signature (vendor modules are loaded unsigned).
usage: check-ko.py <module.ko> <vmlinux.symvers> <expected vermagic tail> [--kano-backlight-provider <provider.ko>]
The optional pinned vendor provider is restricted to gbos_kbd_backlight and one reviewed symbol.
The CRC list is either a kbuild vmlinux.symvers (crc, name, owner, ... separated by tabs) or the "<crc> <name>" list of
the vmlinux exports that builder/kmod/prepare.py takes from the image's own kernel (every name there is vmlinux's)."""
import argparse
import hashlib
import struct
import sys

# Reviewed unsigned vendor cros-ec-proto.ko, with the single permitted export
# unprotected, non-GPL, no namespace, and owner CRC 0x53f88a75.
KANO_PROVIDER_SHA256 = "5ddce2fcfe24e9f1451783bb42d7bbdb23b99955fae211c8bc1ae8f32098ad48"
KANO_PROVIDER_SYMBOL = "cros_ec_cmd_xfer_status"
KANO_PROVIDER_CRC = 0x53f88a75

parser = argparse.ArgumentParser()
parser.add_argument("ko")
parser.add_argument("symvers")
parser.add_argument("magic_tail")
parser.add_argument("--kano-backlight-provider")
args = parser.parse_args()
ko, symvers, magic_tail = args.ko, args.symvers, args.magic_tail
data = open(ko, "rb").read()
if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1 or struct.unpack_from("<H", data, 16)[0] != 1:
    sys.exit(f"{ko}: not a 64-bit little-endian relocatable ELF")
shoff, = struct.unpack_from("<Q", data, 0x28)
shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
secs = []
for i in range(shnum):
    name, typ, flags, _addr, off, size, link, _info, _al, entsize = struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize)
    secs.append(dict(name=name, type=typ, flags=flags, off=off, size=size, link=link, entsize=entsize))
strtab = secs[shstrndx]
def cstr(off):
    return data[off:data.index(b"\0", off)].decode()
for s in secs:
    s["name"] = cstr(strtab["off"] + s["name"])
by = {s["name"]: s for s in secs}
def body(n):
    s = by.get(n)
    return data[s["off"]:s["off"] + s["size"]] if s else b""

problems = []
exported = {}
for line in open(symvers):
    if "\t" in line:
        crc, name, owner = line.split("\t")[:3]
    else:
        crc, name = line.split()
        owner = "vmlinux"
    if name in exported:
        problems.append(f"duplicate export record {name}")
    else:
        exported[name] = (int(crc, 16), owner)
# The selected CONFIG_MODVERSIONS GKI's same_magic() skips the release only when
# find_sec() finds an SHF_ALLOC __versions section, not merely extended CRCs.
info_entries = [x.split("=", 1) for x in body(".modinfo").decode(errors="replace").split("\0") if "=" in x]
info = dict(info_entries)
if args.kano_backlight_provider:
    names = [value for key, value in info_entries if key == "name"]
    if sum(s["name"] == ".modinfo" for s in secs) != 1:
        problems.append("backlight provider requires exactly one .modinfo section")
    if names != ["gbos_kbd_backlight"]:
        problems.append("backlight provider requires exact unique module name gbos_kbd_backlight")
    try:
        with open(args.kano_backlight_provider, "rb") as provider:
            provider_sha = hashlib.sha256(provider.read()).hexdigest()
    except OSError:
        provider_sha = ""
    if provider_sha != KANO_PROVIDER_SHA256:
        problems.append("unrecognized backlight provider SHA-256")
    elif names == ["gbos_kbd_backlight"]:
        if KANO_PROVIDER_SYMBOL in exported:
            problems.append(f"duplicate export record {KANO_PROVIDER_SYMBOL}")
        else:
            exported[KANO_PROVIDER_SYMBOL] = (KANO_PROVIDER_CRC, "vendor:cros-ec-proto")
vm = info.get("vermagic", "")
if vm.split(" ", 1)[1:] != [magic_tail]:
    problems.append(f"vermagic tail {vm.split(' ', 1)[1:]!r} != {magic_tail!r}")

# undefined symbols
sym = next(s for s in secs if s["type"] == 2)
names = secs[sym["link"]]
undef = set()
for i in range(1, sym["size"] // 24):
    st_name, st_info, _o, st_shndx, _v, _s = struct.unpack_from("<IBBHQQ", data, sym["off"] + i * 24)
    n = cstr(names["off"] + st_name)
    if st_shndx == 0 and n:
        undef.add(n)
for n in sorted(undef):
    if n not in exported:
        problems.append(f"undefined symbol {n} is not exported by the GKI")
    elif exported[n][1] != "vmlinux" and not (
        args.kano_backlight_provider and n == KANO_PROVIDER_SYMBOL
        and exported[n][1] == "vendor:cros-ec-proto"
    ):
        problems.append(f"undefined symbol {n} comes from GKI module {exported[n][1]} (protected for unsigned modules)")

# Strict version-table admission is deliberately fail-closed: the kernel has
# warn/force branches for missing CRCs, but those do not prove compatibility.
def version_body(name):
    matches = [s for s in secs if s["name"] == name]
    if not matches:
        return None
    if len(matches) != 1:
        problems.append(f"duplicate version section {name}")
    s = matches[0]
    raw = data[s["off"]:s["off"] + s["size"]]
    if not s["flags"] & 2 or len(raw) != s["size"]:  # SHF_ALLOC, as required by find_sec()
        problems.append(f"malformed or unallocated version section {name}")
        return b""
    return raw


def record_crc(table, name, crc, label):
    if not name:
        problems.append(f"malformed {label}: empty name")
    elif name in table:
        problems.append(f"duplicate {label} name {name}")
    else:
        table[name] = crc


legacy = {}
v = version_body("__versions")
if not v or len(v) % 64:
    problems.append("missing, empty or malformed __versions for tail-only vermagic")
else:
    for i in range(0, len(v), 64):
        name = v[i + 8:i + 64]
        try:
            if b"\0" not in name:
                raise ValueError()
            name = name.split(b"\0", 1)[0].decode()
        except (UnicodeDecodeError, ValueError):
            problems.append("malformed __versions name")
            continue
        # The loader compares the full unsigned long, not a masked low word.
        record_crc(legacy, name, struct.unpack_from("<Q", v, i)[0], "__versions")

extended = {}
ec, en = version_body("__version_ext_crcs"), version_body("__version_ext_names")
has_extended = ec is not None or en is not None
if has_extended:
    if not ec or not en or len(ec) % 4 or not en.endswith(b"\0"):
        problems.append("malformed extended version tables")
    else:
        # The loader consumes exactly CRC-count terminated names. Toolchains
        # may pad the section with NULs; those are not additional empty names.
        pos = 0
        for i in range(len(ec) // 4):
            end = en.find(b"\0", pos)
            if end < 0:
                problems.append("malformed extended version name/CRC count")
                break
            raw_name, pos = en[pos:end], end + 1
            try:
                name = raw_name.decode()
            except UnicodeDecodeError:
                problems.append("malformed extended version name")
                continue
            record_crc(extended, name, struct.unpack_from("<I", ec, i * 4)[0], "extended version")
        if en[pos:].strip(b"\0"):
            problems.append("malformed extended version name/CRC count or nonpadding suffix")
    for name in legacy.keys() & extended.keys():
        if legacy[name] != extended[name]:
            problems.append(f"conflicting legacy/extended CRC for {name}")

# check_version() uses extended CRCs exclusively when that representation is
# present. Never fill an omitted extended record from the legacy table.
crcs = extended if has_extended else legacy
if "module_layout" not in exported or exported["module_layout"][1] != "vmlinux":
    problems.append("module_layout is not exported by vmlinux")
for n in sorted(undef | {"module_layout"}):
    if n not in crcs:
        problems.append(f"no CRC recorded for {n}")
    elif n in exported and crcs[n] != exported[n][0]:
        problems.append(f"CRC of {n}: module {crcs[n]:#010x} != {exported[n][1]} {exported[n][0]:#010x}")
if body("__ksymtab") or body("__ksymtab_gpl"):
    problems.append("the module exports symbols")
if data.endswith(b"~Module signature appended~\n"):
    problems.append("the module is signed")
for p in problems:
    print("FAIL", p)
print(f"{ko}: {len(undef)} imports, {len(crcs)} CRCs, vermagic {vm!r}")
sys.exit(1 if problems else 0)
