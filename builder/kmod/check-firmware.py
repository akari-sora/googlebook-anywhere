#!/usr/bin/env python3
"""Offline MODULE_FIRMWARE gate over the trees actually shipped by vendor.sh.

Read ELF .modinfo directly (the firmware fields modinfo -F firmware prints), so
Mac fixture tests need neither a target kernel nor host kmod. Never search host
/lib/firmware or the input download cache. Numeric versions are NOT assumed to
be interchangeable: a profile must document the driver's accepted alternatives.
"""
import argparse
import fnmatch
import gzip
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import struct
import subprocess
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import devprofile

MODULE_FILE = re.compile(r"(.+)\.ko(?:\.(?:xz|gz|zst))?$")


def matches(name, pattern):
    # A filename wildcard cannot satisfy a request from another directory.
    parts, globs = name.split("/"), pattern.split("/")
    return len(parts) == len(globs) and all(fnmatch.fnmatchcase(p, g) for p, g in zip(parts, globs))


def module_token(raw):
    name = Path(raw).name
    match = MODULE_FILE.fullmatch(name)
    return (match[1] if match else name).replace("-", "_")


def parse_modules_load(text):
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        for tok in line.split():
            names.add(module_token(tok))
    return names


GLOB = re.compile(r"[*?\[\]]")

# Where each loader reads its metadata: (tree label, directory). vendor_dlkm, system_dlkm
# and the initrd have a load list; /vendor/lib/kano and /vendor/lib/dev-modules are
# only modprobed by name (--dirname) from their own modules.dep.
LOAD_LIST_DIRS = (("vendor_dlkm", "lib/modules"), ("system_dlkm", "lib/modules"), ("initrd", "lib/modules"))
DEP_DIRS = (("vendor_dlkm", "lib/modules"), ("system_dlkm", "lib/modules"), ("initrd", "lib/modules"),
            ("vendor", "lib/kano"), ("vendor", "lib/dev-modules"))
# Absolute module paths as the device sees them: /vendor/lib/modules is vendor_dlkm,
# /system/lib/modules is system_dlkm.
DEVICE_DIRS = (("/vendor/lib/modules/", "vendor_dlkm", "lib/modules"),
               ("/vendor_dlkm/lib/modules/", "vendor_dlkm", "lib/modules"),
               ("/system/lib/modules/", "system_dlkm", "lib/modules"),
               ("/system_dlkm/lib/modules/", "system_dlkm", "lib/modules"),
               ("/vendor/", "vendor", ""))
RC_MODPROBE = re.compile(r"modprobe --dirname=(/vendor/lib/(?:modules|kano|dev-modules)) ([A-Za-z0-9_-]+)")
# Any modprobe line a builder-emitted vendor script or rc file runs ("$MODPROBE" too): the
# loaders a loaded_by target must be named by.
LOADER_MODPROBE = re.compile(r'modprobe"?\s+--dirname=(/vendor/lib/(?:modules|kano|dev-modules))\s+([A-Za-z0-9_-]+)',
                             re.IGNORECASE)
# The trees whose modules.load Android init loads with a loader that honors that tree's own
# modules.blocklist. Whether lapis' second-stage loader passes the blocklist flag is not in
# this repo; it is treated as honoring it (fail-closed: a pin can only be refused, never
# vouched for, by this choice). First-stage init applies a blocklist too, the initrd's own
# lib/modules/modules.blocklist: AOSP android-15.0.0_r1 init/first_stage_init.cpp
# LoadKernelModules() builds Modprobe m({dir_path}, GetModuleLoadList(...)) with the default
# use_blocklist = true (libmodprobe/include/modprobe/modprobe.h). The checker does not read
# that file (an approximation, docs/HOW-IT-WORKS.md known gaps); a vendor_dlkm/system_dlkm
# blocklist never binds the initrd. IsBlocklisted() checks the module and every dependency
# GetDependencies() lists, as load_closure() does. The builder's own modprobe lines carry no -b.
BLOCKLIST_TREES = ("vendor_dlkm", "system_dlkm")
RC_DIRS = {"/vendor/lib/modules": ("vendor_dlkm", "lib/modules"), "/vendor/lib/kano": ("vendor", "lib/kano"),
           "/vendor/lib/dev-modules": ("vendor", "lib/dev-modules")}


def shell_words(line):
    """Split one shell line into (text, bare, operator) tuples, a small POSIX subset.

    operator is True for an unquoted run of ; & | characters (a command separator); a quoted
    or backslash-escaped ";" is plain word text (round-4 Codex M1). bare is the length of the
    word's leading unquoted, unescaped part, so "FOO=1" counts as an assignment word only when
    "FOO=" itself is unquoted. "#" starts a comment only at the start of a word (round-4 Claude
    M3: x=a#b is a word). Raises ValueError on an unterminated quote."""
    out, buf, bare, quoted, i = [], None, 0, False, 0
    def close():
        if buf is not None:
            out.append(("".join(buf), bare, False))
    while i < len(line):
        c = line[i]
        if c in " \t":
            close(); buf, bare, quoted = None, 0, False; i += 1; continue
        if c == "#" and buf is None:
            break
        if c in ";&|":
            close(); buf, bare, quoted = None, 0, False
            j = i
            while j < len(line) and line[j] in ";&|":
                j += 1
            out.append((line[i:j], 0, True)); i = j; continue
        if buf is None:
            buf = []
        if c == "'":
            j = line.find("'", i + 1)
            if j < 0:
                raise ValueError("unterminated single quote")
            buf.append(line[i + 1:j]); quoted = True; i = j + 1
        elif c == '"':
            j = i + 1
            while True:
                if j >= len(line):
                    raise ValueError("unterminated double quote")
                if line[j] == "\\" and j + 1 < len(line) and line[j + 1] in '$`"\\':
                    buf.append(line[j + 1]); j += 2
                elif line[j] == '"':
                    break
                else:
                    buf.append(line[j]); j += 1
            quoted = True; i = j + 1
        elif c == "\\":
            buf.append(line[i + 1:i + 2]); quoted = True; i += 2
        else:
            buf.append(c); bare += 0 if quoted else 1; i += 1
    close()
    return out


def command_words(command):
    """The words of one simple command from its command word on: shell keywords, an
    "exec"/"exec_background <context> --" prefix, an "env" prefix and NAME=value assignment
    words in front of the command (round-4 Codex M2) are dropped; quoted keywords are words.

    "env" is a command, not a keyword, so a quoted "env" is env too (round-8 Claude m1). After
    it, env_operands() drops env's options (-i, -0, -v, -u NAME, -uNAME, --unset=NAME, -C DIR,
    -S STRING split into words, any other -x/--long word, "--") and then every NAME=value word
    (env reads its own argv, so a quoted "FOO=1" is an assignment there)."""
    while command:
        word, bare = command[0]
        if word in SHELL_PREFIX and bare == len(word):
            command = command[1:]
        elif word in ("exec", "exec_background", "exec_start") and bare == len(word):
            ends = [i for i, (w, _b) in enumerate(command) if w == "--"]
            command = command[ends[0] + 1:] if ends else command[1:]
        elif word == "env":
            command = env_operands(command[1:])
        elif ASSIGNMENT.match(word) and bare >= ASSIGNMENT.match(word).end():
            command = command[1:]
        else:
            break
    return [word for word, _bare in command]


def env_operands(rest):
    """The (word, bare) list env(1) runs: rest without env's options and NAME=value words."""
    while rest:
        word = rest[0][0]
        if word == "--":
            rest = rest[1:]
            break
        if word in ("-u", "--unset", "-C", "--chdir"):
            rest = rest[2:]
        elif word in ("-S", "--split-string") and len(rest) > 1:
            try:
                split = [(w, len(w)) for w, _b, operator in shell_words(rest[1][0]) if not operator]
            except ValueError:
                split = []
            rest = split + rest[2:]
        elif word.startswith("-") and len(word) > 1:
            rest = rest[1:]
        else:
            break
    while rest and ASSIGNMENT.match(rest[0][0]):
        rest = rest[1:]
    return rest


def loader_modprobes(text):
    """(dirname, module) of every modprobe COMMAND in a builder-emitted script or rc file.

    Round-3 Codex I1 / Claude M3: a comment line, a trailing "# ..." comment and modprobe text
    inside quotes that is not the command word (an echo or log argument) name nothing. Each
    line is split by shell_words(); a line with an unterminated quote is skipped. A command is
    a word list between unquoted ; & | && || (a quoted ";" separates nothing, round-4 Codex
    M1); it counts when its command word, after command_words() drops shell keywords
    (if/then/else/do/!/{ ...), an "exec"/"exec_background <context> --" prefix, "env" and
    NAME=value assignments (round-4 Codex M2) and env's options (round-8 Claude m1), is modprobe itself ("$MODPROBE",
    /vendor/bin/modprobe, ...). Only then is LOADER_MODPROBE matched against that command.
    Not modeled (advisory only, docs/HOW-IT-WORKS.md): line continuations, here-docs and
    multi-line strings, conditions around the command, case branches, one-line functions,
    "(" or "{" glued to the command word, time/busybox/command prefixes, rc "service"
    lines, a reassigned $MODPROBE, and options other than --dirname=DIR right after modprobe.
    """
    found = []
    for line in text.splitlines():
        try:
            words = shell_words(line)
        except ValueError:
            continue
        command = []
        for word, bare, operator in words + [(";", 0, True)]:
            if not operator:
                command.append((word, bare))
                continue
            names = command_words(command)
            if names and LOADER_COMMAND.fullmatch(names[0]):
                found.extend(LOADER_MODPROBE.findall("modprobe " + " ".join(names[1:])))
            command = []
    return found


SHELL_PREFIX = ("if", "then", "else", "elif", "do", "while", "until", "!", "{", "(")
LOADER_COMMAND = re.compile(r"(?:\$\{?MODPROBE\}?|(?:\S*/)?modprobe)", re.IGNORECASE)
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def module_key(label, directory, raw):
    """The tree label and relative path ("vendor_dlkm:lib/modules/x.ko") of a load/dep entry."""
    raw = raw.strip()
    if raw.startswith("/"):
        if label == "initrd":
            rel = raw[1:]
        else:
            for prefix, tree, base in DEVICE_DIRS:
                if raw.startswith(prefix):
                    label, rel = tree, "/".join(p for p in (base, raw[len(prefix):]) if p)
                    break
            else:
                return None
    else:
        rel = f"{directory}/{raw}"
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return f"{label}:{'/'.join(parts)}"


def load_closure_inputs(profile, report, trees, initrd_meta, builder_files=()):
    """Roots with their provenance, per-file dependency edges, the kernel blacklist, the
    userspace blocklist of each honoring tree and the modules the builder's loaders name.

    Only the exact loader files count: <tree>:lib/modules/modules.load and modules.dep of
    vendor_dlkm, system_dlkm and the initrd (the initrd list as stage 2 leaves it,
    devprofile.stage2_modules_load), vendor:lib/kano/modules.dep and
    vendor:lib/dev-modules/modules.dep, <tree>:lib/modules/modules.blocklist of vendor_dlkm
    and system_dlkm, the builder's own vendor:etc/init/kano-hw.rc modprobe lines, and (for
    loaded_by only) the modprobe lines of every builder-emitted vendor script in builder_files.
    """
    def read(label, rel):
        if label == "initrd":
            data = initrd_meta.get(rel)
            return None if data is None else data.decode("utf-8", errors="replace")
        root = trees.get(label)
        if root is None:
            return None
        path = root / rel
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"loader file is not a regular file: {label}:{rel}")
        return path.read_text(errors="replace")

    files_in = {}
    for entry in report["modules"]:
        label, _, rel = entry["source"].partition(":")
        for tree, directory in DEP_DIRS:
            if label == tree and rel.startswith(directory + "/"):
                files_in.setdefault((tree, directory), {}).setdefault(entry["module"], []).append(entry["source"])
    deps = {}
    heads = {}
    for tree, directory in DEP_DIRS:
        text = read(tree, f"{directory}/modules.dep")
        if text is None:
            continue
        rows = {}
        parse_modules_dep_rows(text, rows)
        for head, tail in rows.items():
            key = module_key(tree, directory, head)
            if key is None:
                continue
            heads.setdefault((tree, directory), {}).setdefault(module_token(key), []).append(key)
            edges = deps.setdefault(key, set())
            edges.update(k for k in (module_key(tree, directory, t) for t in tail) if k)

    def lookup(tree, directory, name):
        # Approximation (Claude M3a): a name without a modules.dep head falls back to the file
        # of that name in the directory, even when the directory has a modules.dep;
        # libmodprobe InsmodWithDeps() would refuse it ("not in dependency file"). Only an
        # inconsistent image (load list naming a module depmod did not index) differs.
        # Not modeled either: softdeps and ueventd modalias loads (both only add modules).
        token = module_token(name)
        keys = heads.get((tree, directory), {}).get(token) or files_in.get((tree, directory), {}).get(token, [])
        if len(set(keys)) > 1:
            raise ValueError(f"ambiguous module {token} in {tree}:{directory}: {', '.join(sorted(set(keys)))}")
        return list(set(keys))

    roots = {}

    def add(keys, how):
        for key in keys:
            roots.setdefault(key, set()).add(how)

    for tree, directory in LOAD_LIST_DIRS:
        text = read(tree, f"{directory}/modules.load")
        if text is not None:
            if tree == "initrd":
                # The final image's first-stage list: stage 2 drops first_stage_drop and adds pci-stub.
                text = devprofile.stage2_modules_load(text, profile)
            for name in sorted(parse_modules_load(text)):
                add(lookup(tree, directory, name), tree)

    required_files = set()
    vendor_dir = {"vendor": ("vendor_dlkm", "lib/modules"), "dev": ("vendor", "lib/dev-modules")}
    for name in profile["kmods"]:
        required_files.update(lookup("vendor", "lib/kano", name))
    for name in profile["wifi"]["modprobe"]:
        required_files.update(lookup("vendor_dlkm", "lib/modules", name))
    for name in profile["modules"]["load"] + profile["modules"]["unblock"]:
        required_files.update(lookup(*vendor_dir[profile.get("_module_dirs", {}).get(name, "vendor")], name))
    if profile["cmdline"]["pci_stub_ids"]:
        required_files.update(lookup("vendor_dlkm", "lib/modules", "pci_stub"))
        required_files.update(lookup("initrd", "lib/modules", "pci_stub"))
    required_files.update(m["source"] for m in report["modules"]
                          if "builder vendor addition/replacement" in m["required_by"])
    rc = read("vendor", "etc/init/kano-hw.rc")
    for dirname, name in RC_MODPROBE.findall(rc or ""):
        required_files.update(lookup(*RC_DIRS[dirname], name))
    add(required_files, "builder")

    named = set()
    for rel in builder_files:
        for dirname, name in loader_modprobes(read("vendor", rel) or ""):
            named.update(lookup(*RC_DIRS[dirname], name))

    # The kernel refuses a module that the profile [blacklist].modules or the
    # module_blacklist= of every boot entry names, whoever asks for it.
    entries = devprofile.make_entries(profile, "")
    kernel = set(profile["blacklist"]["modules"])
    lists = [set(o.split("=", 1)[1].split(",")) for key in ("debug", "quiet")
             for o in entries[key] if o.startswith("module_blacklist=")]
    if lists:
        kernel |= set.intersection(*lists)
    kernel = {module_token(b) for b in kernel if b}
    # A tree's own modules.blocklist binds only the loader of that tree's modules.load.
    userspace = {}
    for tree in BLOCKLIST_TREES:
        for line in (read(tree, "lib/modules/modules.blocklist") or "").splitlines():
            words = line.split("#", 1)[0].split()
            if len(words) == 2 and words[0] == "blocklist":
                userspace.setdefault(tree, set()).add(module_token(words[1]))
    return roots, deps, kernel, userspace, named


def load_closure(roots, deps, kernel, userspace):
    """(closure, dead) of the final image.

    dead: every file the kernel refuses, i.e. a kernel-blacklisted module and, transitively,
    every module whose modules.dep needs one (its symbols cannot resolve). A root loaded only
    by a tree's modules.load is skipped when it or any of its dependencies is on that tree's
    modules.blocklist (libmodprobe IsBlocklisted); the builder's own modprobe lines carry no
    -b, so no blocklist applies to them. First-stage init does apply the initrd's own
    lib/modules/modules.blocklist (AOSP default use_blocklist = true), but this checker does
    not read that file yet, so initrd roots are never dropped here: a known gap
    (docs/HOW-IT-WORKS.md), it can only over-include, never refuse a pin wrongly. The closure
    is every surviving root and its dependencies.
    """
    keys = set(roots) | set(deps) | {d for ds in deps.values() for d in ds}
    dead = {k for k in keys if module_token(k) in kernel}
    changed = True
    while changed:
        changed = False
        for key in sorted(keys - dead):
            if deps.get(key, set()) & dead:
                dead.add(key)
                changed = True

    def needs(key):
        seen, queue = set(), [key]
        while queue:
            for dep in deps.get(queue.pop(), ()):
                if dep not in seen:
                    seen.add(dep)
                    queue.append(dep)
        return seen

    closure = set()
    for key, how in roots.items():
        if key in dead:
            continue
        honored = [userspace.get(h, set()) for h in how if h in BLOCKLIST_TREES]
        if len(honored) == len(how) and all(
                any(module_token(k) in blocklist for k in {key} | needs(key)) for blocklist in honored):
            continue
        closure.add(key)
    queue = sorted(closure)
    while queue:
        for dep in sorted(deps.get(queue.pop(), ())):
            if dep not in closure and dep not in dead:
                closure.add(dep)
                queue.append(dep)
    return closure, dead


def builder_loader_files(vendor, base_vendor):
    """vendor-relative paths of the rc files and shell scripts under etc/ that the builder
    added or replaced (not byte-identical to the untouched vendor tree)."""
    found = []
    etc = vendor / "etc"
    if not etc.is_dir() or etc.is_symlink():
        return found
    for path in files(etc):
        rel = path.relative_to(vendor)
        if path.suffix not in (".rc", ".sh") or path.is_symlink() or not path.is_file():
            continue
        old = base_vendor / rel
        if old.is_symlink() or not old.is_file() or old.read_bytes() != path.read_bytes():
            found.append(rel.as_posix())
    return found


def unpack_entry(names, inodes, links, label, mode, ino, nlink, dev, body):
    """Apply one newc record like Linux v6.18 init/initramfs.c do_name()/maybe_link().

    names maps a path to an inode number, inodes an inode to (mode, data), links the
    hard-link table of the current archive ((devmajor, devminor), ino, file type) -> the
    first name (find_link; free_hash() clears it at each TRAILER!!!). clean_path() first
    removes a name of another file type. A regular record with nlink >= 2 whose key was
    seen becomes a link to that name's inode (opened without O_TRUNC): its data, when it has
    any, replaces the shared inode's data (vfs_truncate to the new size, then the write), a
    zero-size one keeps the data the inode already has. Any other regular record opens the
    path with O_TRUNC, so a name that is already a hard link changes every name of its inode.
    """
    fmt = stat.S_IFMT(mode)
    cur = names.get(label)
    if cur is not None and stat.S_IFMT(inodes[cur][0]) != fmt:
        del names[label]
        cur = None
    if stat.S_ISREG(mode):
        if nlink >= 2:
            key = (dev, ino, fmt)
            old = links.get(key)
            if old is None:
                links[key] = label
            else:
                names.pop(label, None)
                if old not in names:
                    return  # init_link() fails: the record is skipped
                names[label] = names[old]
                inode = names[label]
                inodes[inode] = (mode, body if body else inodes[inode][1])
                return
        if label in names:
            inodes[names[label]] = (mode, body)
            return
    elif stat.S_ISDIR(mode) and cur is not None:
        return
    else:
        names.pop(label, None)
    inode = len(inodes)
    inodes[inode] = (mode, body)
    names[label] = inode


def parse_modules_dep_rows(text, rows):
    """modules.dep rows keyed by the raw head path; values are raw dependency paths."""
    for line in text.replace("\\\n", " ").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        head, tail = line.split(":", 1)
        if head.strip():
            rows.setdefault(head.strip(), []).extend(tail.split())


class InvalidFirmwareName(ValueError):
    """A firmware= record that is not a valid name; .name is the offending token."""

    def __init__(self, name):
        super().__init__(f"invalid firmware name {name!r}")
        self.name = name


def firmware_names(data, filename):
    """Read every firmware= record, including repeats; malformed metadata fails."""
    if filename.endswith(".xz"):
        data = lzma.decompress(data)
    elif filename.endswith(".gz"):
        data = gzip.decompress(data)
    elif filename.endswith(".zst"):
        data = subprocess.run(["zstd", "-d", "-q", "-c"], input=data,
                              capture_output=True, check=True).stdout
    if (len(data) < 64 or data[:6] != b"\x7fELF\x02\x01"
            or struct.unpack_from("<HH", data, 16) != (1, 62)):
        raise ValueError("not an x86-64 little-endian relocatable ELF module")
    shoff, = struct.unpack_from("<Q", data, 40)
    entsize, count, names_index = struct.unpack_from("<HHH", data, 58)
    if (entsize != 64 or not count or names_index >= count
            or shoff < 64 or shoff + count * entsize > len(data)):
        raise ValueError("invalid ELF section table")
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * entsize) for i in range(count)]

    def body(section):
        offset, size = section[4:6]
        if offset + size > len(data):
            raise ValueError("truncated ELF section")
        return data[offset:offset + size]

    names = body(sections[names_index])
    info = []
    for section in sections:
        start = section[0]
        end = names.find(b"\0", start)
        if start >= len(names) or end < 0:
            raise ValueError("invalid ELF section name")
        if names[start:end] == b".modinfo":
            if section[1] != 1:
                raise ValueError(".modinfo is not PROGBITS")
            info.append(body(section))
    if len(info) != 1 or not info[0] or not info[0].endswith(b"\0"):
        raise ValueError("missing or malformed .modinfo")
    firmware = set()
    for record in info[0].split(b"\0"):
        if record.startswith(b"firmware="):
            name = record[len(b"firmware="):].decode("ascii")
            if (not re.fullmatch(r"[A-Za-z0-9._+*?\[\]/-]+", name)
                    or any(part in ("", ".", "..") for part in name.split("/"))):
                raise InvalidFirmwareName(name)
            firmware.add(name)
    return sorted(firmware)


def initrd_label(name, index):
    """The logical relative path of a newc entry, validated before any other use.

    An absolute name or one with a ".." segment is an error; the message names the
    entry by its index, never by the unvalidated text. "." segments and repeated
    separators are dropped like the kernel's unpack does; "" is the archive root.
    """
    if name.startswith("/") or ".." in name.split("/"):
        raise ValueError(f"initrd entry {index} has an invalid path (absolute or with a .. segment)")
    return "/".join(part for part in name.split("/") if part not in ("", "."))


def initrd_modules(path):
    """The regular module and modules.load/modules.dep files of lapis' concatenated newc archives.

    Read in memory. Records are applied in order like the kernel's initramfs unpack
    (unpack_entry): a later record for the same path replaces the earlier one, and regular
    records with nlink >= 2 and the same device and inode share one inode, so an overwritten
    module or load list never counts and a hard-linked name reads the data the kernel leaves
    on its inode. Yields (label, bytes) of the final view.
    """
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing initrd: {path.name}")
    data = path.read_bytes()
    if data.endswith(b"#BOOTCONFIG\n"):
        if len(data) < 20:
            raise ValueError("truncated initrd bootconfig")
        size, checksum = struct.unpack_from("<II", data, len(data) - 20)
        start = len(data) - 20 - size
        if start < 0 or sum(data[start:-20]) & 0xffffffff != checksum:
            raise ValueError("invalid initrd bootconfig")
        data = data[:start]
    offset = 0
    trailers = 0
    index = 0
    names, inodes, links = {}, {}, {}
    while offset < len(data):
        if data[offset] == 0:
            offset += 1
            continue
        if data[offset:offset + 6] != b"070701" or offset + 110 > len(data):
            raise ValueError("expected an uncompressed newc initrd")
        header = data[offset:offset + 110]
        if not re.fullmatch(rb"070701[0-9A-Fa-f]{104}", header):
            raise ValueError(f"initrd entry {index} has an invalid newc header (fields must be 8 hex digits)")
        ino = int(header[6:14], 16)
        mode = int(header[14:22], 16)
        nlink = int(header[38:46], 16)
        dev = (int(header[62:70], 16), int(header[70:78], 16))
        size = int(header[54:62], 16)
        namesize = int(header[94:102], 16)
        endname = offset + 110 + namesize
        start = (endname + 3) & ~3
        end = start + size
        if namesize < 1 or endname > len(data) or data[endname - 1] != 0 or end > len(data):
            raise ValueError("truncated initrd entry")
        name = data[offset + 110:endname - 1].decode("ascii")
        if name == "TRAILER!!!":
            trailers += 1
            links.clear()
        else:
            label = initrd_label(name, index)
            if label:
                unpack_entry(names, inodes, links, label, mode, ino, nlink, dev, data[start:end])
        index += 1
        offset = (end + 3) & ~3
    if not trailers:
        raise ValueError("initrd has no newc trailer")
    for label, inode in names.items():
        mode, body = inodes[inode]
        base = PurePosixPath(label).name
        if MODULE_FILE.fullmatch(base) or base in ("modules.load", "modules.dep"):
            if not stat.S_ISREG(mode):
                raise ValueError(f"initrd {base} {label} is not a regular file")
            yield label, body


def files(root):
    # Ignore directory symlinks (e.g. /vendor/lib/modules -> /vendor_dlkm/lib/modules).
    # Those partitions are checked separately; never follow a host-absolute link.
    def fail(error):
        raise error

    for parent, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        dirs.sort()
        for name in sorted(names):
            yield Path(parent) / name


def installed_path(root, path):
    """Resolve links inside the image tree root, never against the host root."""
    pending = list(path.relative_to(root).parts)
    current = root
    links = 0
    while pending:
        part = pending.pop(0)
        if part == ".":
            continue
        if part == "..":
            if current == root:
                return None
            current = current.parent
            continue
        target = current / part
        if target.is_symlink():
            link = Path(os.readlink(target))
            links += 1
            if links > 40:
                return None
            if link.is_absolute():
                # An absolute target resolves from the device root, never against the
                # host or the tree root. Only the one mount point that really contains
                # this tree is remapped; everything else stays outside the tree.
                if root.name == "firmware":
                    if not link.is_relative_to("/vendor/firmware"):
                        return None
                    link = link.relative_to("/vendor/firmware")
                elif link.is_relative_to("/vendor"):
                    link = link.relative_to("/vendor")
                else:
                    return None
                current = root
            pending = list(link.parts) + pending
        else:
            current = target
            if pending and not current.is_dir():
                return None
    return current


def firmware_files(root):
    """Include in-image directory aliases without following escapes or cycles."""
    def walk(relative, ancestors):
        actual = installed_path(root, root / relative)
        if actual is None:
            return
        if actual.is_dir():
            if actual in ancestors:
                return
            for child in sorted(actual.iterdir()):
                yield from walk(relative / child.name, ancestors | {actual})
        elif actual.is_file() and actual.stat().st_size:
            yield relative.as_posix()

    return set(walk(Path(), set()))


def check(profile, vendor, module_trees, initrd, base_vendor, collect=None, mask=None):
    """Build the report. collect, when given, collects every firmware name and pattern
    this run may print: each module declaration, each rule's firmware pattern (used or
    not), the offending token of an invalid declaration, and for each profile pin its
    path, the module labels the reverse check prints next to it (declared_by, the
    unloaded declaring modules, the declaring module names, loaded_by) and the matched
    declaration strings; never a matched file."""
    pid = profile["model"]["id"]
    collect = set() if collect is None else collect
    report = {"profile": pid, "modules": [], "pins": [], "optional": [], "alternatives": [], "errors": [], "warnings": [], "advisories": []}
    if not base_vendor.is_dir() or base_vendor.is_symlink():
        raise ValueError(f"missing original vendor module tree: {base_vendor.name}")
    fwroot = vendor / "firmware"
    if not fwroot.is_dir() or fwroot.is_symlink():
        raise ValueError("missing final vendor firmware directory: vendor/firmware")
    available = firmware_files(fwroot)

    rules = [(kind, rule) for kind, rows in profile["firmware_check"].items() for rule in rows]
    collect.update(rule["firmware"] for _kind, rule in rules)
    found = set()
    required = {}
    for field, names in (("profile.kmods", profile["kmods"]),
                         ("profile.wifi.modprobe", profile["wifi"]["modprobe"]),
                         ("profile.modules.load", profile["modules"]["load"]),
                         ("profile.modules.unblock", profile["modules"]["unblock"])):
        for name in names:
            required.setdefault(name, []).append(field)
    if profile["cmdline"]["pci_stub_ids"]:
        # image.py adds this exact vendor_dlkm module to the initrd after stage 1.
        required.setdefault("pci_stub", []).append("builder initrd addition")

    def inspect(source, filename, read, added=False):
        module = MODULE_FILE.fullmatch(filename)[1].replace("-", "_")
        found.add(module)
        reasons = required.get(module, []) + (["builder vendor addition/replacement"] if added else [])
        entry = {"module": module, "source": source, "firmware": [],
                 "scope": "required" if reasons else "report-only", "required_by": reasons}
        report["modules"].append(entry)
        try:
            names_found = firmware_names(read(), filename)
        except (ValueError, OSError, EOFError, subprocess.SubprocessError, lzma.LZMAError, zlib.error) as exc:
            if isinstance(exc, OSError) and exc.strerror:
                # OSError.filename is the module (already named by the source label) or a
                # failing command such as zstd; name the command, never leak a host path.
                if isinstance(exc.filename, (str, os.PathLike)) and Path(exc.filename).name != filename:
                    msg = f"{exc.strerror}: {Path(exc.filename).name}"
                else:
                    msg = f"{exc.strerror}: {source}"
            else:
                msg = str(exc)
            if isinstance(exc, InvalidFirmwareName):
                collect.add(exc.name)
                # Mask the raw token first, then repr: no escaped spelling of a root is built.
                msg = f"invalid firmware name {(mask or (lambda n: n))(exc.name)!r}"
            entry["error"] = msg
            report["errors" if reasons else "warnings"].append(f"profile {pid}: module {module} ({source}): {msg}")
            return
        collect.update(names_found)
        for name in names_found:
            matching = [(kind, rule) for kind, rule in rules
                        if reasons and rule["module"] == module and matches(name, rule["firmware"])]
            result = {"firmware": name, "status": "present",
                      "matched": sorted(f for f in available if matches(f, name))}
            entry["firmware"].append(result)
            if len(matching) > 1:
                report["errors"].append(f"profile {pid}: module {module}: overlapping firmware rules for {name}")
                continue
            if matching:
                kind, rule = matching[0]
                result["reason"] = rule["reason"]
                if kind == "optional":
                    result["status"] = "optional-present" if result["matched"] else "optional-missing"
                    report["optional"].append({"module": module, "source": source, **result})
                    continue
                if not result["matched"]:
                    result["matched"] = sorted(f for f in available
                                               if any(matches(f, p) for p in rule["files"]))
                    if result["matched"]:
                        result["status"] = "alternative"
                        report["alternatives"].append({"module": module, "source": source, **result})
            if not result["matched"]:
                result["status"] = "missing"
                if reasons:
                    report["errors"].append(f"profile {pid}: module {module} ({source}): missing required firmware {name}")

    def read_module(path, source):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"module is not a regular file: {source}")
        return path.read_bytes()

    trees = {}
    for root in [vendor, *module_trees]:
        label = "vendor" if root == vendor else logical_name(root).removeprefix("firmware-")
        trees[label] = root
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"missing shipped module tree: {label}")
        for path in files(root):
            rel = path.relative_to(root)
            source = f"{label}:{rel.as_posix()}"
            # modules.load / modules.dep / modules.blocklist are read below at the exact
            # paths the loaders use, never wherever a file of that name happens to be.
            if MODULE_FILE.fullmatch(path.name):
                # Compare the final vendor readback with the untouched original.
                # Builder/kmod and hook additions in vendor are required; the
                # GBOS_DEV_KERNEL_DIR tree (lib/dev-modules) is scoped by the
                # profile like vendor_dlkm/system_dlkm.
                added = False
                if root == vendor and not rel.is_relative_to("lib/dev-modules"):
                    old = installed_path(base_vendor, base_vendor / rel)
                    added = (old is None or not old.is_file() or path.is_symlink()
                             or old.read_bytes() != path.read_bytes())
                inspect(source, path.name, lambda: read_module(path, source), added)
    initrd_meta = {}
    for name, data in initrd_modules(initrd):
        base = PurePosixPath(name).name
        if base in ("modules.load", "modules.dep"):
            initrd_meta[name] = data
        else:
            inspect(f"initrd:{name}", base, lambda data=data: data)
    required_modules = set(required) | {m["module"] for m in report["modules"] if m["scope"] == "required"}
    roots, deps, kernel, userspace, named = load_closure_inputs(
        profile, report, trees, initrd_meta, builder_loader_files(vendor, base_vendor))
    closure, dead = load_closure(roots, deps, kernel, userspace)
    by_source = {m["source"]: m for m in report["modules"]}

    for fw in profile["firmware"]:
        path = fw["path"]
        req_by = fw.get("requested_by", "")
        loaded_by = fw.get("loaded_by", "")
        declared_by = []
        declaring_modules = []
        outside_by = []
        shipped_by = []
        declarations = set()
        for entry in report["modules"]:
            hits = [decl["firmware"] for decl in entry["firmware"] if matches(path, decl["firmware"])]
            if hits:
                shipped_by.append(entry["source"])
                if entry["source"] in closure:
                    declared_by.append(entry["source"])
                    declarations.update(hits)
                    if entry["module"] not in declaring_modules:
                        declaring_modules.append(entry["module"])
                else:
                    outside_by.append(entry["source"])
        def advise(reason):
            # v0.2.3: the reverse check is advisory. A refusal is reported (ADVISORY line and
            # the report's advisories list) and never changes report["ok"] or the exit status.
            report["advisories"].append(f"profile {pid}: {path}: {reason}")

        if loaded_by:
            kind = "loaded_by"
            target = by_source.get(loaded_by)
            hits = [decl["firmware"] for decl in (target or {}).get("firmware", []) if matches(path, decl["firmware"])]
            if target is None:
                status = "undeclared"
                advise(f"loaded_by module {loaded_by} is not in the image")
            elif not hits:
                status = "undeclared"
                advise(f"not declared by its loaded_by module {loaded_by}")
            else:
                status = "undeclared"
                shadow = sorted(k for k in closure if k != loaded_by and module_token(k) == module_token(loaded_by))
                # Round-4 Claude M4: a shadowing label (closure key from modules.load/dep text)
                # is D1-checked like every other label the advisory prints.
                collect.update(shadow)
                if loaded_by in closure:
                    advise(f"loaded_by module {loaded_by} is already in the "
                        "load closure; drop loaded_by (the pin is then simply declared)")
                elif loaded_by in dead or module_token(loaded_by) in kernel:
                    advise(f"loaded_by module {loaded_by} is kernel-blacklisted "
                        "(or needs a blacklisted module) and never loads")
                elif shadow:
                    advise(f"loaded_by module {loaded_by} is shadowed by "
                        f"{', '.join(shadow)}, which the kernel loads under the same name")
                elif loaded_by not in named:
                    advise(f"loaded_by module {loaded_by} is not named by any "
                        "modprobe line the builder emits")
                else:
                    status = "conditional"
                    declarations = set(hits)
        elif declared_by:
            kind = "requested_by" if req_by else "static"
            status = "declared"
        elif req_by:
            kind = "requested_by"
            status = "runtime"
        else:
            kind = "static"
            status = "undeclared"
            if outside_by:
                advise("not declared by any loaded/required module "
                       f"(declared only by unloaded report-only {', '.join(outside_by)})")
            else:
                advise("not declared by any loaded/required module")
        if req_by and shipped_by:
            advise(f"has requested_by \"runtime: ...\" but is statically declared by "
                f"{', '.join(shipped_by)}; a conditionally loaded module needs loaded_by = \"<tree>:<module path>\"")
        pin_entry = {
            "path": path,
            "status": status,
            "kind": kind,
            "declared_by": declared_by,
            "modules": declaring_modules,
            "declarations": sorted(declarations),
            "glob": any(GLOB.search(d) for d in declarations),
        }
        if req_by:
            pin_entry["requested_by"] = req_by
        if loaded_by:
            pin_entry["loaded_by"] = loaded_by
        report["pins"].append(pin_entry)
        # Everything the reverse check prints next to this pin is a firmware name or a
        # module label: kept unchanged by the scrub, or D1 when it holds a root's text.
        collect.update(pin_texts(pin_entry, outside_by))

    for kind, rule in rules:
        if rule["module"] not in required_modules:
            report["warnings"].append(
                f"profile {pid}: unused {kind} firmware rule for report-only module "
                f"{rule['module']} ({rule['firmware']}): {rule['reason']}"
            )
    for name in sorted(required.keys() - found):
        report["errors"].append(f"profile {pid}: requested module {name} is absent from the shipped module trees/initrd")
    if not found:
        report["errors"].append(f"profile {pid}: no shipped modules found")
    report["summary"] = {}
    for scope in ("required", "report-only"):
        entries = [m for m in report["modules"] if m["scope"] == scope]
        declarations = [f for m in entries for f in m["firmware"]]
        report["summary"][scope] = {
            "modules": len(entries), "wanted": len(declarations),
            "missing": sum(f["status"] == "missing" for f in declarations),
            "metadata_errors": sum("error" in m for m in entries),
        }
    report["summary"]["pins"] = {
        "total": len(report["pins"]),
        "declared": sum(p["status"] == "declared" for p in report["pins"]),
        "runtime": sum(p["status"] == "runtime" for p in report["pins"]),
        "conditional": sum(p["status"] == "conditional" for p in report["pins"]),
        "undeclared": sum(p["status"] == "undeclared" for p in report["pins"]),
    }
    report["ok"] = not report["errors"]
    return report


def host_roots(args):
    """(path, logical name) of every build-host root this run may name, as the caller gave it."""
    roots = [(Path(__file__).resolve().parents[2], ""), (args.profile, logical_name(args.profile)),
             (args.vendor, "vendor")]
    roots += [(root, logical_name(root).removeprefix("firmware-")) for root in args.module_tree]
    roots += [(args.initrd, "initrd"), (args.report, logical_name(args.report)),
              (args.base_vendor, logical_name(args.base_vendor))]
    return roots


def logical_roots(args):
    """Absolute build-host roots this run may name, mapped to their logical names.

    Only absolute host paths enter the mapping: a caller's relative spelling (v, i, .)
    is never treated as text to rewrite. A message may echo either the caller's
    absolute spelling or its resolved form, so both are mapped; a root that names
    nothing (only slashes: "/", "//", "///", or an empty value) is skipped, since
    replacing it would rewrite every path in the message. The repo root drops to
    nothing ("<repo>/profiles/_safety.toml" -> "profiles/_safety.toml"); the profile
    directory, each module tree root, the initrd, the report and the untouched base
    vendor tree keep their logical identity. Longest path first, so a nested root is
    replaced before its parent.
    """
    roots = {}
    for path, name in host_roots(args):
        for candidate in (Path(path), Path(path).resolve()):
            if candidate.is_absolute() and candidate.name:
                roots[str(candidate)] = name
    return sorted(roots.items(), key=lambda item: len(item[0]), reverse=True)


def logical_name(path):
    """The leaf name of a caller's path; ".", ".." or "a/.." name the directory they resolve to."""
    path = Path(path)
    if path.name and path.name != "..":
        return path.name
    return path.resolve().name or str(path)


def pin_texts(pin, outside_by=()):
    """The texts the reverse check prints for one pin: its path, its declared_by,
    loaded_by and unloaded declaring module labels, its declaring module names and the
    matched declaration strings. check() adds them to collect."""
    names = {pin["path"], *pin["declared_by"], *pin["modules"], *pin["declarations"], *outside_by}
    if pin.get("loaded_by"):
        names.add(pin["loaded_by"])
    return {n for n in names if isinstance(n, str) and n}


def local_names(names, mapping):
    """Collected firmware names and tokens that contain the text of a mapped host root
    anywhere, longest first (ties sort by text, so the order never depends on set
    iteration). Each one fails the run (D1); none is ever exempt from scrubbing."""
    roots = [raw for raw, _name in mapping]
    names = {n for n in names if isinstance(n, str) and n}
    return sorted((n for n in names if any(raw in n for raw in roots)), key=lambda n: (-len(n), n))


def masked_name(name, mapping):
    """A local firmware name with every host root text replaced by its logical name."""
    for raw, logical in mapping:
        name = name.replace(raw, logical or "<repo>")
    return name


def scrub_paths(value, mapping, local=()):
    """Replace host roots with their logical names in diagnostic text.

    A root only matches between whole path boundaries, which are an explicit delimiter
    set rather than "not a path character": a left delimiter is the string start or one
    of whitespace ' " ( [ = , : ;; a right delimiter is the string end, "/", or one of
    whitespace ' " ) ] , : ; or a "." that itself ends the text or precedes one of those
    (a sentence end, an ellipsis or punctuation such as "」", i.e. "." not followed by an
    ASCII letter, ASCII digit or "_"; "vendor.甲" therefore counts as a sentence end). So
    "<host>/vendor.old" or "<host>/repo.bak" is a sibling tree and is left intact, and
    ordinary text that merely contains a root, such as "intel+<host>/vendor+suffix" or the
    sibling tree "<host>/vendor甲", is left intact here (the output guard of main() then
    withholds such a string and fails the run).
    One left-to-right pass in a single compiled regex, with a named group per root: the
    logical name the pass just wrote is never scanned again, and the key is read from the
    match itself, so nothing has to strip a separator back off. The repo root maps to
    nothing and swallows the one separator that followed it.

    No text is exempt from this pass. local lists names that contain a host root (the
    caller fails the run on them): each is matched first and written masked
    (masked_name), wherever its root text sits.
    """
    if not mapping:
        return value
    logical = {}
    parts = []
    for index, (raw, name) in enumerate(mapping):
        group = f"root_{index}"
        token = f"(?P<{group}>{re.escape(raw)})"
        if name:
            # The separator after the root is kept; only a path boundary may follow.
            token += r"(?=(?:/|[\s'\")\],;:]|\.(?![A-Za-z0-9_])|$))"
        else:
            # The repo root drops, taking its one separator with it.
            token += r"(?:/|(?=[\s'\")\],;:]|\.(?![A-Za-z0-9_])|$))"
        logical[group] = name
        parts.append(token)
    roots = r"(?:^|(?<=[\s'\"(\[=,;:]))(?:" + "|".join(parts) + r")"
    masked = {k: masked_name(k, mapping) for k in local if k}
    if masked:
        roots = "(?P<local>" + "|".join(re.escape(k) for k in sorted(masked, key=len, reverse=True)) + ")|" + roots
    pattern = re.compile(roots)

    def replace(match):
        if match.lastgroup == "local":
            return masked[match.group(0)]
        return logical[match.lastgroup]

    def walk(node):
        if isinstance(node, str):
            return pattern.sub(replace, node)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        return node

    return walk(value)


WITHHELD = "diagnostic withheld: it contained a local build path"


def guard_texts(args):
    """Every text the output guard refuses: each host root as given (when absolute),
    resolved, and in the repr-escaped form of both; never a root that names nothing."""
    texts = set()
    for path, _name in host_roots(args):
        for candidate in (Path(path), Path(path).resolve()):
            if candidate.is_absolute() and candidate.name:
                texts.add(str(candidate))
                # F5: needed, not redundant. A root with a control character (a tab or
                # newline in a temp directory name) is spelled "\\t" by repr; removing
                # backslashes leaves "t", so only this entry catches that spelling.
                texts.add(repr(str(candidate))[1:-1])
    return sorted(texts, key=len, reverse=True)


class OutputGuard:
    """The last step before any diagnostic leaves the checker. A string that still
    contains the text of a host root anywhere (no word or path boundaries) is replaced
    by WITHHELD as a whole, and the run fails."""

    def __init__(self, texts):
        self.texts = [t for t in texts if t]
        # Every escaped spelling (repr picks its quotes per string, so a root's ' or "
        # may or may not gain a backslash): compare with all backslashes removed on both
        # sides too. Over-matching is fine; the guard fails closed.
        self.bare = sorted({t.replace("\\", "") for t in self.texts} - {""}, key=len, reverse=True)
        self.tripped = False

    def __call__(self, value):
        if isinstance(value, list):
            return [self(item) for item in value]
        bare = value.replace("\\", "")
        if any(text in value for text in self.texts) or any(text in bare for text in self.bare):
            self.tripped = True
            return WITHHELD
        return value


INTERNAL = "firmware check failed: internal error"
ARGS_FAILED = "FAIL firmware check failed: invalid arguments (see --help)"


class GuardedParser(argparse.ArgumentParser):
    """argparse that never prints an error message: its error() (which would echo the
    offending argv text) raises instead, and main() prints the one fixed ARGS_FAILED line."""

    def error(self, message):
        raise SystemExit(2)


def main():
    """Parse the arguments, then run; any exception becomes one fixed, guarded failure
    line (never a traceback), rc 1, and a JSON report that carries that error."""
    try:
        # F4: argument parsing is inside the boundary. An argparse error (unknown or
        # missing argument, bad value) would echo argv, which may hold a local root.
        args = parse_args()
    except SystemExit as exc:
        if exc.code in (0, None):
            raise  # --help: argparse's own usage text, no argv echoed
        print(ARGS_FAILED, file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001
        print(ARGS_FAILED, file=sys.stderr)
        return 2
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 - the boundary of everything the checker prints
        pname = "<profile>"
        try:
            pname = logical_name(args.profile)
        except Exception:  # noqa: BLE001
            pass
        guard = OutputGuard(safe_guard_texts(args))
        error = guard(f"profile {pname}: {INTERNAL} ({type(exc).__name__})")
        try:
            args.report.write_text(json.dumps({"profile": guard(pname), "ok": False, "errors": [error]},
                                              indent=2, sort_keys=True) + "\n")
        except Exception:  # noqa: BLE001
            pass
        print(guard("FAIL " + error), file=sys.stderr)
        return 1


def safe_guard_texts(args):
    try:
        return guard_texts(args)
    except Exception:  # noqa: BLE001
        return [str(Path(__file__).resolve().parents[2])]


def parse_args():
    parser = GuardedParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--vendor", required=True, type=Path)
    parser.add_argument("--base-vendor", required=True, type=Path,
                        help="untouched vendor extraction, to identify builder additions/replacements")
    parser.add_argument("--module-tree", required=True, action="append", type=Path)
    parser.add_argument("--initrd", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def run(args):
    pname = logical_name(args.profile)
    mapping = logical_roots(args)
    names = set()
    try:
        # Resolve the profile path: an error from the loader then carries an absolute
        # path the scrub can map, while a relative "--profile sub/generic-x86" still
        # reports the logical name "generic-x86".
        profile = devprofile.load(args.profile.resolve())
        report = check(profile, args.vendor, args.module_tree, args.initrd, args.base_vendor, names,
                       mask=lambda n: masked_name(n, mapping))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        if isinstance(exc, OSError) and exc.filename and isinstance(exc.filename, (str, os.PathLike)):
            msg = f"{exc.strerror}: {Path(exc.filename).name}"
        else:
            msg = str(exc)
        report = {"profile": pname, "ok": False,
                  "errors": [f"profile {pname}: firmware check failed: {msg}"]}
    # Host build directories are replaced by their logical names in the errors, the
    # warnings, each module's error and everything printed below. Data fields (module,
    # source, firmware, matched, reason, declared_by, loaded_by, requested_by, declarations,
    # path, profile) are never rewritten, but each passes the output guard below. A firmware name, pattern,
    # invalid token, pinned firmware path, pin module label or matched declaration that
    # contains the text of a host root fails the run (D1); its D1 line prints the masked
    # name with repr. Then every diagnostic, the reverse check's PINNED lines included,
    # passes the output guard: one that still holds a host root in any spelling is
    # withheld and the run fails.
    local = local_names(names, mapping)
    for name in local:
        report.setdefault("errors", []).append(
            f"profile {report['profile']}: firmware name contains a local build path: {masked_name(name, mapping)!r}")
        report["ok"] = False
    guard = OutputGuard(guard_texts(args))

    def clean(value):
        return guard(scrub_paths(value, mapping, local))

    report["errors"] = clean(report.get("errors", []))
    # Reverse-check refusals (advisory in v0.2.3): scrubbed and guarded like an error, but
    # they never touch report["ok"]; only a guard hit while printing one fails the run.
    advisories = report.get("advisories", [])
    if "advisories" in report:
        report["advisories"] = clean(advisories)
    if "warnings" in report:
        report["warnings"] = clean(report["warnings"])
    for entry in report.get("modules", []):
        if "error" in entry:
            entry["error"] = clean(entry["error"])
    lines = []
    for item in report.get("optional", []):
        lines.append((sys.stdout, clean(f"OPTIONAL firmware: profile {report['profile']}: module {item['module']}: "
                                        f"{item['firmware']} ({item['status']}): {item['reason']}")))
    for pin in report.get("pins", []):
        if pin["status"] == "declared":
            lines.append((sys.stdout, clean(f"PINNED firmware: profile {report['profile']}: {pin['path']}: "
                                            f"declared by {', '.join(pin['declared_by'])}")))
        elif pin["status"] == "runtime":
            lines.append((sys.stdout, clean(
                f"PINNED firmware: profile {report['profile']}: {pin['path']}: {pin['requested_by']}")))
        elif pin["status"] == "conditional":
            lines.append((sys.stdout, clean(f"PINNED firmware: profile {report['profile']}: {pin['path']}: "
                                            f"conditional, declared by loaded_by {pin['loaded_by']}")))
    for advisory in advisories:
        lines.append((sys.stdout, clean(f"ADVISORY firmware pin: {advisory}")))
    for warning in report.get("warnings", []):
        if ": unused " in warning:
            lines.append((sys.stdout, clean(f"UNUSED firmware rule: WARNING: {warning}")))
    # No local path in any output, the JSON report included: every other string field
    # (module, source, firmware, matched, reason, every pin field with requested_by and
    # loaded_by, profile) passes the output guard as data (never rewritten); one that holds
    # a root's text in any spelling becomes WITHHELD and the run fails.
    def guard_data(node):
        if isinstance(node, str):
            return guard(node)
        if isinstance(node, list):
            return [guard_data(item) for item in node]
        if isinstance(node, dict):
            return {key: guard_data(item) for key, item in node.items()}
        return node

    for key in list(report):
        if key not in ("errors", "warnings", "advisories"):
            report[key] = guard_data(report[key])
    if guard.tripped:
        report["errors"].append(clean(f"profile {report['profile']}: firmware check failed: "
                                      "a diagnostic contained a local build path and was withheld"))
        report["ok"] = False
    for error in report["errors"]:
        lines.append((sys.stderr, clean("FAIL " + error)))
    if "summary" in report:
        stock = report["summary"]["report-only"]
        lines.append((sys.stdout, clean(
            f"firmware report-only: {stock['modules']} stock modules; "
            f"{stock['missing']} missing firmware declarations; {stock['metadata_errors']} metadata errors")))
    req = report.get("summary", {}).get("required", {"modules": 0, "wanted": 0})
    pins = report.get("summary", {}).get("pins", {"total": 0, "declared": 0, "runtime": 0, "conditional": 0})
    lines.append((sys.stdout, clean(
        f"firmware check: {'PASS' if report['ok'] else 'FAIL'}; "
        f"required: {req['modules']} modules, {req['wanted']} firmware declarations; "
        f"{len(report.get('modules', []))} modules; report: {args.report.name}; "
        f"pins: {pins['total']}, declared: {pins['declared']}, runtime: {pins['runtime']}, "
        f"conditional: {pins['conditional']}, advisory: {len(report.get('advisories', []))}")))
    if guard.tripped and report["ok"]:
        raise AssertionError("output guard tripped on a passing run")
    try:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        # Report the logical name, never the missing host directory or a traceback.
        print(clean(f"firmware check: cannot write report {args.report.name}: {exc.strerror or exc}"),
              file=sys.stderr)
        return 1
    for stream, line in lines:
        print(line, file=stream)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
