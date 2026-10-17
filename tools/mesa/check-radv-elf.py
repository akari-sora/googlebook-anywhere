#!/usr/bin/env python3
"""ELF checks for the Android RADV ICD (vulkan.radeon.so).

The packaged file must be an x86-64 ET_DYN with SONAME vulkan.radeon.so, no
DT_RPATH/DT_RUNPATH, and DT_NEEDED entries only from libraries that exist in
the pinned lapis base image (/system/lib64 or /vendor/lib64). libdrm and
libdrm_amdgpu are not on that list: libdrm_amdgpu.so is absent from lapis, so
both are linked statically. The step-2 build (SONAME libvulkan_radeon.so,
RUNPATH into the WSL tree, NEEDED libdrm_amdgpu.so) fails these checks.
"""
import struct
import sys
from pathlib import Path

# Where each allowlisted SONAME was found in the lapis base (kano-real.img,
# system_a / vendor_a). libc/libm/libdl in /system/lib64 are the apex symlinks
# the linker actually opens; the file is also in the runtime apex payload.
NEEDED_ALLOW = {
    "libc.so",            # system_a/system/lib64/libc.so -> apex runtime bionic
    "libm.so",            # system_a/system/lib64/libm.so -> apex runtime bionic
    "libdl.so",           # system_a/system/lib64/libdl.so -> apex runtime bionic
    "liblog.so",          # system_a/system/lib64/liblog.so
    "libz.so",            # system_a/system/lib64/libz.so and vendor_a/lib64/libz.so
    "libcutils.so",       # vendor_a/lib64/libcutils.so and system_a/system/lib64
    "libhardware.so",     # vendor_a/lib64/libhardware.so and system_a/system/lib64
    "libnativewindow.so", # system_a/system/lib64/libnativewindow.so
    "libsync.so",         # system_a/system/lib64/libsync.so
    "libc++.so",          # vendor_a/lib64/libc++.so and system_a/system/lib64
}
SONAME = "vulkan.radeon.so"
DT_NEEDED, DT_STRTAB, DT_SYMTAB, DT_STRSZ, DT_SYMENT = 1, 5, 6, 10, 11
DT_SONAME, DT_RPATH, DT_RUNPATH, DT_NULL = 14, 15, 29, 0
PT_LOAD, PT_DYNAMIC = 1, 2
SHT_DYNSYM = 11
ET_DYN, EM_X86_64 = 3, 62


def _loads_and_dynamic(data: bytes):
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError("not ELF64 little-endian")
    e_type, e_machine = struct.unpack_from("<HH", data, 16)
    if e_type != ET_DYN or e_machine != EM_X86_64:
        raise ValueError(f"e_type={e_type} e_machine={e_machine}, want ET_DYN x86-64")
    e_phoff = struct.unpack_from("<Q", data, 32)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 54)
    loads = []
    dyn_off = dyn_sz = None
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type, _flags = struct.unpack_from("<II", data, off)
        p_offset, p_vaddr, _pa, p_filesz, _ms, _al = struct.unpack_from("<QQQQQQ", data, off + 8)
        if p_type == PT_LOAD:
            loads.append((p_offset, p_vaddr, p_filesz))
        elif p_type == PT_DYNAMIC:
            dyn_off, dyn_sz = p_offset, p_filesz
    if dyn_off is None:
        raise ValueError("no PT_DYNAMIC")
    return loads, dyn_off, dyn_sz


def _va_to_off(loads, va: int) -> int:
    for off, vaddr, filesz in loads:
        if vaddr <= va < vaddr + filesz:
            return off + (va - vaddr)
    raise ValueError(f"vaddr {va:#x} not in a PT_LOAD")


def _dynsym_blob(data: bytes, loads, symtab_va: int | None, str_off: int) -> bytes:
    e_shoff = struct.unpack_from("<Q", data, 40)[0]
    e_shentsize, e_shnum = struct.unpack_from("<HH", data, 58)
    if e_shoff and e_shentsize >= 64 and e_shnum:
        for i in range(e_shnum):
            sh = e_shoff + i * e_shentsize
            if sh + 64 > len(data):
                break
            sh_type = struct.unpack_from("<I", data, sh + 4)[0]
            if sh_type == SHT_DYNSYM:
                sh_offset, sh_size = struct.unpack_from("<QQ", data, sh + 24)
                return data[sh_offset:sh_offset + sh_size]
    if symtab_va is not None:
        sym_off = _va_to_off(loads, symtab_va)
        if sym_off < str_off:
            return data[sym_off:str_off]
    return b""


def inspect(data: bytes):
    """Return (soname, needed list, rpath tags present, has_hmi_export)."""
    loads, dyn_off, dyn_sz = _loads_and_dynamic(data)
    strtab_va = strsz = symtab_va = None
    ents = []
    for i in range(0, dyn_sz, 16):
        tag, val = struct.unpack_from("<qQ", data, dyn_off + i)
        if tag == DT_NULL:
            break
        ents.append((tag, val))
        if tag == DT_STRTAB:
            strtab_va = val
        elif tag == DT_STRSZ:
            strsz = val
        elif tag == DT_SYMTAB:
            symtab_va = val
    if strtab_va is None or not strsz:
        raise ValueError("dynamic section has no STRTAB")
    str_off = _va_to_off(loads, strtab_va)
    blob = data[str_off:str_off + strsz]

    def s(n: int) -> str:
        end = blob.index(b"\0", n)
        return blob[n:end].decode("utf-8", "replace")

    soname = None
    needed = []
    rpaths = []
    for tag, val in ents:
        if tag == DT_SONAME:
            soname = s(val)
        elif tag == DT_NEEDED:
            needed.append(s(val))
        elif tag in (DT_RPATH, DT_RUNPATH):
            rpaths.append((tag, s(val)))

    has_hmi = False
    dynsym = _dynsym_blob(data, loads, symtab_va, str_off)
    for off in range(0, len(dynsym) - (len(dynsym) % 24), 24):
        st_name, st_info, st_other, st_shndx, _st_val, _st_sz = struct.unpack_from("<IBBHQQ", dynsym, off)
        bind = st_info >> 4
        vis = st_other & 0x3
        if st_shndx != 0 and bind == 1 and vis == 0 and st_name < len(blob) and s(st_name) == "HMI":
            has_hmi = True
            break
    return soname, needed, rpaths, has_hmi


def problems(path: Path) -> list[str]:
    data = path.read_bytes()
    try:
        soname, needed, rpaths, has_hmi = inspect(data)
    except ValueError as e:
        return [str(e)]
    bad = []
    if soname != SONAME:
        bad.append(f"SONAME {soname!r} != {SONAME!r}")
    if rpaths:
        bad.append("RUNPATH/RPATH present: " + ", ".join(f"{tag}:{text}" for tag, text in rpaths))
    extra = [n for n in needed if n not in NEEDED_ALLOW]
    if extra:
        bad.append("NEEDED not in lapis allowlist: " + ", ".join(extra))
    if "libc.so" not in needed:
        bad.append("NEEDED libc.so missing")
    if any(n in ("libdrm.so", "libdrm_amdgpu.so") for n in needed):
        bad.append("libdrm/libdrm_amdgpu must be static, not DT_NEEDED")
    if not has_hmi:
        bad.append("missing GLOBAL/DEFAULT HMI export in .dynsym")
    return bad


def remove_rpath(path: Path) -> int:
    """Drop DT_RPATH and DT_RUNPATH entries in place. Returns how many were removed."""
    data = bytearray(path.read_bytes())
    _loads, dyn_off, dyn_sz = _loads_and_dynamic(data)
    slots = []
    for i in range(0, dyn_sz, 16):
        tag, val = struct.unpack_from("<qQ", data, dyn_off + i)
        slots.append((tag, val))
        if tag == DT_NULL:
            break
    keep = [(t, v) for t, v in slots if t not in (DT_RPATH, DT_RUNPATH)]
    removed = len(slots) - len(keep)
    if not removed:
        return 0
    if keep[-1][0] != DT_NULL:
        keep.append((DT_NULL, 0))
    while len(keep) < len(slots):
        keep.append((DT_NULL, 0))
    for i, (tag, val) in enumerate(keep):
        struct.pack_into("<qQ", data, dyn_off + i * 16, tag, val)
    path.write_bytes(data)
    return removed


def _synth(soname: str, needed: list[str], runpath: str | None, export_hmi: bool = True, hmi_bind: int = 1) -> bytes:
    """Minimal ET_DYN whose dynamic section and .dynsym the checker can read."""
    blobs = ["HMI", soname, *needed] + ([runpath] if runpath else [])
    strtab = bytearray(b"\0")
    off = {}
    for b in blobs:
        off[b] = len(strtab)
        strtab += b.encode() + b"\0"
    sym_undef = struct.pack("<IBBHQQ", 0, 0, 0, 0, 0, 0)
    st_info = ((hmi_bind << 4) | 1) if export_hmi else 0x01
    sym_hmi = struct.pack("<IBBHQQ", off["HMI"], st_info, 0, 1 if export_hmi else 0, 0x100, 248)
    symtab = sym_undef + sym_hmi
    ehdr = 64
    phdrs = 56 * 2
    dyn_off = ehdr + phdrs
    ents = [(DT_STRTAB, 0), (DT_STRSZ, len(strtab)), (DT_SYMTAB, 0), (DT_SYMENT, 24), (DT_SONAME, off[soname])]
    for n in needed:
        ents.append((DT_NEEDED, off[n]))
    if runpath:
        ents.append((DT_RUNPATH, off[runpath]))
    ents.append((DT_NULL, 0))
    dyn_len = len(ents) * 16
    sym_off = dyn_off + dyn_len
    str_off = sym_off + len(symtab)
    total = str_off + len(strtab)
    ents[0] = (DT_STRTAB, str_off)
    ents[2] = (DT_SYMTAB, sym_off)
    dyn = b"".join(struct.pack("<qQ", t, v) for t, v in ents)
    buf = bytearray(total)
    buf[0:4] = b"\x7fELF"
    buf[4] = 2
    buf[5] = 1
    buf[6] = 1
    struct.pack_into("<HHI", buf, 16, ET_DYN, EM_X86_64, 1)
    struct.pack_into("<Q", buf, 32, ehdr)
    struct.pack_into("<HHH", buf, 54, 56, 2, 0)
    struct.pack_into("<IIQQQQQQ", buf, ehdr, PT_LOAD, 5, 0, 0, 0, total, total, 0x1000)
    struct.pack_into("<IIQQQQQQ", buf, ehdr + 56, PT_DYNAMIC, 6, dyn_off, dyn_off, 0, len(dyn), len(dyn), 8)
    buf[dyn_off:dyn_off + len(dyn)] = dyn
    buf[sym_off:sym_off + len(symtab)] = symtab
    buf[str_off:str_off + len(strtab)] = strtab
    return bytes(buf)


def self_test() -> None:
    import tempfile
    good = _synth(SONAME, ["libc.so", "libm.so", "libdl.so", "liblog.so"], None)
    bad_soname = _synth("libvulkan_radeon.so", ["libc.so"], None)
    bad_rpath = _synth(SONAME, ["libc.so"], "$ORIGIN/../../../subprojects/libdrm-2.4.123")
    bad_needed = _synth(SONAME, ["libc.so", "libdrm_amdgpu.so", "libdrm.so"], None)
    bad_hmi = _synth(SONAME, ["libc.so"], None, export_hmi=False)
    weak_hmi = _synth(SONAME, ["libc.so"], None, export_hmi=True, hmi_bind=2)
    cases = {
        "good": (good, True),
        "step2-soname": (bad_soname, False),
        "step2-runpath": (bad_rpath, False),
        "step2-needed": (bad_needed, False),
        "missing-hmi": (bad_hmi, False),
        "weak-hmi": (weak_hmi, False),
    }
    with tempfile.TemporaryDirectory(prefix="radv-elf-") as td:
        tdir = Path(td)
        for name, (blob, ok) in cases.items():
            p = tdir / f"{name}.so"
            p.write_bytes(blob)
            got = problems(p)
            if ok and got:
                raise SystemExit(f"self-test {name} should pass: {got}")
            if not ok and not got:
                raise SystemExit(f"self-test {name} should fail")
        rp = tdir / "strip-rpath.so"
        rp.write_bytes(bad_rpath)
        if remove_rpath(rp) != 1 or problems(rp):
            raise SystemExit(f"self-test remove_rpath failed: {problems(rp)}")
    print("elf self-test ok")


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "--self-test":
        self_test()
        return 0
    if len(argv) == 3 and argv[1] == "--strip-rpath":
        n = remove_rpath(Path(argv[2]))
        print(f"removed {n} RPATH/RUNPATH entries")
        return 0
    if len(argv) != 2:
        print("usage: check-radv-elf.py <file.so> | --self-test | --strip-rpath <file.so>", file=sys.stderr)
        return 2
    bad = problems(Path(argv[1]))
    if bad:
        print("\n".join(bad))
        return 1
    print(f"ok {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

