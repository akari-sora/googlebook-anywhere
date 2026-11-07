"""Source-bound Ally Microdroid rebuild. No device or retained-VM authority.

All work is disposable until independent readback passes. Unknown filesystem
metadata is refused by the existing strict EROFS helpers, never discarded.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import copy
import weakref

_VERIFIED = {}

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "builder/ifma/microdroid-chain.json"
CONTRACT_SHA256 = "94f67c26c56cacfb139f1aa8a5d6db41a445ff729bac9e6e305736d4c7b732d7"
RUNTIME_ALIAS = "microdroid/system/apex/com.android.runtime.apex"
SYSTEM = "etc/fs/microdroid.img"
VBMETA = "etc/fs/microdroid_vbmeta.img"
KERNEL = "etc/fs/microdroid_kernel"
INITRDS = {
    "initrd_normal": "etc/microdroid_initrd_normal.img",
    "initrd_debug": "etc/microdroid_initrd_debuggable.img",
}


class Refused(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise Refused(message)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def enabled(prof):
    cpu = prof.get("cpu_compat", {})
    for flag in ("patch_ifma", "patch_ally_bootstrap"):
        _require(type(cpu.get(flag, False)) is bool, flag + " must be boolean")
    return (
        prof.get("model", {}).get("id") == "rog-ally"
        and cpu.get("patch_ifma", False)
        and cpu.get("patch_ally_bootstrap", False)
    )


def _contract():
    _require(_sha(CONTRACT) == CONTRACT_SHA256, "Microdroid contract changed")
    c = json.loads(CONTRACT.read_text())
    for rel, pin in c["sources"].items():
        _require(_sha(ROOT / rel) == pin, "Microdroid source changed: " + rel)
    return c


def source_identity(tools_dir=None):
    """No private keys, product inputs, tool execution or implicit downloads."""
    c = _contract()
    identity = {
        "contract_sha256": CONTRACT_SHA256,
        "module_sha256": _sha(__file__),
        "sources": c["sources"],
        "required_tools": c["tools"],
    }
    if tools_dir is not None:
        d = Path(tools_dir)
        identity["tools"] = {
            name: _sha(d / name) for name in ("sign_virt_apex.py", "initrd_bootconfig")
        }
        _require(
            all(identity["tools"][n] == c["tools"][n] for n in identity["tools"]),
            "Microdroid tool pin mismatch",
        )
    return identity


@dataclass(frozen=True)
class VerifiedMicrodroid:
    """Fresh artifact binding, not a serialized receipt accepted as authority."""

    payload_root: str
    artifacts: tuple
    kernel_payload_sha256: str
    source_sha256: str
    stock_payload_root: str = ""

    def assert_bound(self, payload_tree):
        entry = _VERIFIED.get(id(self))
        _require(
            entry is not None and entry() is self,
            "externally constructed verification receipt",
        )
        root = Path(payload_tree).resolve()
        _require(
            str(root) == self.payload_root, "Microdroid result belongs to another tree"
        )
        _require(
            all(_sha(root / rel) == pin for rel, pin in self.artifacts),
            "Microdroid artifacts changed after verification",
        )


def _lz4_path(directory):
    """Use a pinned private binary when supplied, otherwise the system binary."""
    private = Path(directory) / "lz4"
    if os.path.lexists(private):
        _require(not private.is_symlink() and private.is_file(), "private lz4 is not regular")
        _require(os.access(private, os.X_OK), "private lz4 is not executable")
        return private
    return Path("/usr/bin/lz4")


def _tools(tools_dir, otatools):
    _require(
        __debug__ and not sys.flags.optimize and not os.environ.get("PYTHONOPTIMIZE"),
        "optimized Python disables upstream signer checks",
    )
    raw = tools_dir or os.environ.get("GBOS_MICRODROID_TOOLS_DIR")
    _require(
        bool(raw), "GBOS_MICRODROID_TOOLS_DIR is required for enabled Ally guest chain"
    )
    d = Path(raw).resolve()
    source_identity(d)
    c = _contract()
    _require(
        _sha(Path(otatools) / "bin/avbtool") == c["tools"]["avbtool"],
        "Microdroid avbtool pin mismatch",
    )
    import ifma

    env = ifma._ot_env(otatools)
    for name in ("PYTHONOPTIMIZE", "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE"):
        env.pop(name, None)
    _require(_sha(_lz4_path(d)) == c["tools"]["lz4"], "Microdroid lz4 pin mismatch")
    # The only PATH-resolved child commands in this pinned signer are the
    # initrd utility and lz4. Do not inherit caller PATH or permit shadows.
    for name in ("avbtool", "python", "python3", "openssl"):
        _require(
            not os.path.lexists(d / name), "Microdroid tool directory shadows " + name
        )
    env["PATH"] = str(d) + os.pathsep + "/usr/bin:/bin"
    _require(
        ifma._run_ot(otatools, "avbtool", "version").strip() == "avbtool 1.4.0",
        "Microdroid AVB version mismatch",
    )
    return d, env


def _run(argv, env):
    p = subprocess.run(
        [str(x) for x in argv],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=600,
    )
    _require(p.returncode == 0, "Microdroid host command failed: " + str(argv[0]))
    return p.stdout


def _parse_info(text):
    """Parse avbtool text, not another AVB binary parser; reject duplicate fields."""
    header, descriptors, current = {}, [], None
    for line in text.splitlines():
        if not line.strip() or line.strip() in ("--", "Descriptors:"):
            continue
        if line.startswith("    ") and not line.startswith("      "):
            name, value = line.strip().split(":", 1)
            _require(
                name in ("Hash descriptor", "Hashtree descriptor", "Prop"),
                "unsupported AVB descriptor",
            )
            current = {} if name != "Prop" else value.strip()
            descriptors.append((name, current))
        elif line.startswith("      "):
            _require(isinstance(current, dict), "malformed AVB descriptor")
            key, val = line.strip().split(":", 1)
            _require(key not in current, "duplicate AVB descriptor field")
            current[key] = val.strip()
        else:
            key, val = line.split(":", 1)
            _require(key not in header, "duplicate AVB header field")
            header[key] = val.strip()
    return header, descriptors


def _info(otatools, image):
    import ifma

    return _parse_info(
        ifma._run_ot(otatools, "avbtool", "info_image", "--image", image)
    )


def _descs(pair, kind, names):
    rows = [d for k, d in pair[1] if k == kind]
    _require(
        len(rows) == len(names) and {d["Partition Name"] for d in rows} == set(names),
        "missing, duplicate or mixed AVB descriptors",
    )
    _require(all(k in (kind, "Prop") for k, d in pair[1]), "mixed AVB descriptor types")
    return {d["Partition Name"]: d for d in rows}


def _inventory(root, c, *, stock):
    import ifma

    root = Path(root)
    index = ifma._index_tree(root)
    expected = set(c["files"])
    candidates = {
        p
        for p in index
        if (
            "microdroid" in Path(p).name
            and ("kernel" in Path(p).name or Path(p).suffix == ".img")
        )
        or "service_vm" in p
        or Path(p).name == "super.img"
    }
    _require(
        candidates == expected, "unexpected kernel/initrd/legacy/service VM inventory"
    )
    for rel, row in c["files"].items():
        p = root / rel
        _require(p.is_file() and not p.is_symlink(), "guest artifact is not regular")
        if stock:
            _require(
                p.stat().st_size == row["size"] and _sha(p) == row["sha256"],
                "not exact stock guest artifact: " + rel,
            )
    return index


def _verify_stock(root, c, otatools, keys):
    import ifma

    _inventory(root, c, stock=True)
    with tempfile.TemporaryDirectory(prefix="microdroid-public-") as scratch:
        public = Path(scratch) / "owner-public"
        ifma._run_ot(
            otatools,
            "avbtool",
            "extract_public_key",
            "--key",
            keys["payload_key"],
            "--output",
            public,
        )
        owner_sha1 = hashlib.sha1(public.read_bytes()).hexdigest()
    _verify_images(root, otatools)
    for rel in (SYSTEM, VBMETA, KERNEL):
        info = _info(otatools, root / rel)
        _require(
            info == _parse_info(c["files"][rel]["info_image"]),
            "stock AVB metadata drift",
        )
        _require(
            info[0]["Public key (sha1)"] != owner_sha1,
            "already-owner-signed stock violates upstream signer precondition",
        )


def _extract(image, directory, otatools):
    import ifma

    directory.mkdir()
    ifma._run_ot(
        otatools,
        "fsck.erofs",
        "--extract=" + str(directory),
        "--overwrite",
        "--preserve-perms",
        image,
    )


def _roundtrip(image, work, tools, env):
    work.mkdir()
    body, bc, joined = (work / n for n in ("body", "bootconfig", "joined"))
    _run([tools / "initrd_bootconfig", "detach", image, body, bc], env)
    _run([tools / "initrd_bootconfig", "attach", body, bc, "--output", joined], env)
    _require(
        joined.read_bytes() == Path(image).read_bytes(),
        "initrd checksum/roundtrip mismatch",
    )
    return body.read_bytes(), bc.read_bytes()


def _bootconfig(old, new, digest, size):
    patterns = {
        "avb_version": rb'"1\.4"',
        "digest": rb'"[0-9a-f]{64}"',
        "size": rb"[0-9]+",
    }
    for field, value in patterns.items():
        pat = rb"androidboot\.vbmeta\." + field.encode() + rb" = (" + value + rb")"
        key = rb"androidboot\.vbmeta\." + field.encode() + rb"\b"
        _require(
            len(re.findall(key, old)) == len(re.findall(key, new)) == 1,
            "duplicate/malformed bootconfig key " + field,
        )
        a, b = re.findall(pat, old), re.findall(pat, new)
        _require(len(a) == len(b) == 1, "missing/duplicate bootconfig " + field)
        expected = {
            "avb_version": b'"1.4"',
            "digest": ('"' + digest + '"').encode(),
            "size": str(size).encode(),
        }[field]
        _require(b[0] == expected, "wrong bootconfig " + field)
        if field != "avb_version":
            old = re.sub(pat, lambda m: m[0].replace(a[0], expected), old)
    _require(old == new, "unapproved bootconfig text changes")


def _guest_readback(old_image, image, work, otatools, keys):
    import ifma
    import ally_adler

    work.mkdir()
    old_raw, _ = ifma._strip_avb_footer(
        Path(old_image).read_bytes(), "stock microdroid"
    )
    raw, _ = ifma._strip_avb_footer(Path(image).read_bytes(), "microdroid")
    old_erofs, new_erofs = work / "old.erofs", work / "new.erofs"
    old_erofs.write_bytes(old_raw)
    new_erofs.write_bytes(raw)
    ifma._assert_same_erofs_metadata(old_erofs, new_erofs, "microdroid")
    old_tree, new_tree = work / "old", work / "new"
    _extract(old_erofs, old_tree, otatools)
    _extract(new_erofs, new_tree, otatools)
    a, b = ifma._index_tree(old_tree), ifma._index_tree(new_tree)
    expected = set(ally_adler.expected_changed_paths("microdroid")) | {
        "system/bin/microdroid_manager",
        _contract()["runtime"]["path"],
    }
    _exact_delta(a, b, expected, "guest changed-file set is not exactly five")
    ally_adler.verify_tree("microdroid", new_tree)
    manager = ifma.PATCH_IFMA.run(
        new_tree,
        ROOT / "builder/ifma/microdroid-manager.json",
        work / "manager.json",
        False,
        scope=("microdroid", "system"),
    )
    _require(
        len(manager["targets"]) == 1 and manager["targets"][0]["after"] == "patched",
        "manager residual survives",
    )
    runtime = _contract()["runtime"]
    _require(
        _sha(old_tree / runtime["path"]) == runtime["sha256"],
        "wrong nested runtime container",
    )
    with zipfile.ZipFile(old_tree / runtime["path"]) as z:
        _require(
            hashlib.sha256(z.read("apex_payload.img")).hexdigest()
            == runtime["payload_sha256"],
            "top-level runtime substituted for nested runtime",
        )
    ifma.verify_apex_file(
        new_tree / runtime["path"],
        RUNTIME_ALIAS,
        work / "runtime-verify",
        otatools,
        expected_cert_der=Path(keys["container_cert_der"]).read_bytes(),
        stock_apex_path=old_tree / runtime["path"],
        adler_scope="microdroid_runtime",
    )
    _runtime_readback(
        old_tree / runtime["path"],
        new_tree / runtime["path"],
        work / "runtime-readback",
        otatools,
    )


def _same_header(old, new):
    mutable = {"Public key (sha1)"}
    _require(
        {k: v for k, v in old.items() if k not in mutable}
        == {k: v for k, v in new.items() if k not in mutable},
        "AVB header drift",
    )


def _exact_delta(old, new, expected, message):
    _require(
        set(old) == set(new) and {p for p in old if old[p] != new[p]} == set(expected),
        message,
    )


def _runtime_readback(old_apex, new_apex, work, otatools):
    """Independently prove complete nested metadata and linker64-only delta."""
    import ifma
    import ally_adler

    work.mkdir()
    trees = []
    images = []
    for label, apex in (("old", old_apex), ("new", new_apex)):
        with zipfile.ZipFile(apex) as z:
            raw, _ = ifma._strip_avb_footer(
                z.read("apex_payload.img"), "microdroid runtime " + label
            )
        image, tree = work / (label + ".erofs"), work / label
        image.write_bytes(raw)
        images.append(image)
        _extract(image, tree, otatools)
        trees.append(tree)
    ifma._assert_same_erofs_metadata(*images, "microdroid nested runtime")
    _exact_delta(
        ifma._index_tree(trees[0]),
        ifma._index_tree(trees[1]),
        {"bin/linker64"},
        "nested runtime changed-file set is not linker64-only",
    )
    ally_adler.verify_tree("microdroid_runtime", trees[1])


def verify_chain(
    prof, stock_payload_tree, payload_tree, work_dir, otatools, keys, *, tools_dir=None
):
    """Verify real artifacts afresh; never accept a JSON receipt or policy override."""
    _require(enabled(prof), "Microdroid chain requires explicit rog-ally opt-in")
    import ifma

    tools, env = _tools(tools_dir, otatools)
    c = _contract()
    stock, root, work = Path(stock_payload_tree), Path(payload_tree), Path(work_dir)
    _require(not work.exists(), "verification scratch must be new")
    work.mkdir(parents=True)
    _verify_stock(stock, c, otatools, keys)
    a, b = _inventory(stock, c, stock=True), _inventory(root, c, stock=False)
    _require(
        set(a) == set(b) and {p for p in a if a[p] != b[p]} == set(c["files"]),
        "outer guest-chain changed-file set is not exactly five",
    )
    _verify_images(root, otatools, keys["payload_key"])
    pairs = {}
    for rel in (SYSTEM, VBMETA, KERNEL):
        pairs[rel] = _info(otatools, root / rel)
        old = _parse_info(c["files"][rel]["info_image"])
        _same_header(old[0], pairs[rel][0])
        _require(
            (root / rel).stat().st_size == c["files"][rel]["size"],
            "signed guest artifact size drift",
        )
        _require(
            [d for k, d in old[1] if k == "Prop"]
            == [d for k, d in pairs[rel][1] if k == "Prop"],
            "AVB properties drift",
        )
    sd = _descs(pairs[SYSTEM], "Hashtree descriptor", ["system"])["system"]
    vd = _descs(pairs[VBMETA], "Hashtree descriptor", ["system"])["system"]
    _require(sd == vd, "vbmeta does not bind complete system descriptor")
    stock_sd = _descs(
        _parse_info(c["files"][SYSTEM]["info_image"]), "Hashtree descriptor", ["system"]
    )["system"]
    _require(
        {k: v for k, v in sd.items() if k != "Root Digest"}
        == {k: v for k, v in stock_sd.items() if k != "Root Digest"},
        "system geometry/salt drift",
    )
    vh = pairs[VBMETA][0]
    size = sum(
        int(vh[k].split()[0])
        for k in ("Header Block", "Authentication Block", "Auxiliary Block")
    )
    _require(
        size == c["vbmeta_meaningful_size"] and (root / VBMETA).stat().st_size == 65536,
        "vbmeta meaningful size/padding mismatch",
    )
    _require(not any((root / VBMETA).read_bytes()[size:]), "nonzero vbmeta padding")
    digest = ifma._run_ot(
        otatools,
        "avbtool",
        "calculate_vbmeta_digest",
        "--image",
        root / VBMETA,
        "--hash_algorithm",
        "sha256",
    ).strip()
    _require(
        re.fullmatch("[0-9a-f]{64}", digest) is not None, "invalid AVB digest output"
    )
    kd = _descs(pairs[KERNEL], "Hash descriptor", ["boot", *INITRDS])
    old_kd = _descs(
        _parse_info(c["files"][KERNEL]["info_image"]),
        "Hash descriptor",
        ["boot", *INITRDS],
    )
    prefix = c["kernel_original_size"]
    _require(
        (root / KERNEL).read_bytes()[:prefix] == (stock / KERNEL).read_bytes()[:prefix],
        "kernel payload changed",
    )
    for name, desc in kd.items():
        _require(
            {k: v for k, v in desc.items() if k not in ("Digest", "Image Size")}
            == {
                k: v
                for k, v in old_kd[name].items()
                if k not in ("Digest", "Image Size")
            },
            "kernel descriptor salt/flags drift",
        )
        if name == "boot":
            _require(desc == old_kd[name], "kernel boot descriptor changed")
            continue
        rel = INITRDS[name]
        data = (root / rel).read_bytes()
        _require(
            desc["Image Size"] == str(len(data)) + " bytes"
            and hashlib.sha256(bytes.fromhex(desc["Salt"]) + data).hexdigest()
            == desc["Digest"],
            "initrd salted descriptor mismatch",
        )
        obody, obc = _roundtrip(stock / rel, work / (name + "-old"), tools, env)
        body, bc = _roundtrip(root / rel, work / name, tools, env)
        _require(body == obody, "initrd body changed")
        _bootconfig(obc, bc, digest, size)
    _guest_readback(stock / SYSTEM, root / SYSTEM, work / "guest", otatools, keys)
    return _verified(
        str(root.resolve()),
        tuple(sorted((p, _sha(root / p)) for p in c["files"])),
        hashlib.sha256((root / KERNEL).read_bytes()[:prefix]).hexdigest(),
        _sha(__file__),
        str(stock.resolve()),
    )


def rebuild_chain(prof, payload_tree, work_dir, otatools, keys, *, tools_dir=None):
    """Build on a copy and sign once; publication follows fresh independent proof.

    Original payload is untouched on any failed check. Existing key material is
    mandatory; this function never creates a key or executes a guest ELF.
    """
    _require(enabled(prof), "Microdroid chain requires explicit rog-ally opt-in")
    import ifma
    import rebuild
    import ally_adler

    tools, env = _tools(tools_dir, otatools)
    c = _contract()
    root, work = Path(payload_tree), Path(work_dir)
    _require(not work.exists(), "rebuild scratch must be new")
    _require(
        not root.resolve().is_relative_to(work.resolve())
        and not work.resolve().is_relative_to(root.resolve()),
        "scratch overlaps payload",
    )
    work.mkdir(parents=True)
    # No mutation of caller's stock tree, including temporary public-key output.
    stock, candidate = work / "stock", work / "candidate"
    shutil.copytree(root, stock, symlinks=True)
    _verify_stock(stock, c, otatools, keys)
    shutil.copytree(stock, candidate, symlinks=True)
    raw, _ = ifma._strip_avb_footer((stock / SYSTEM).read_bytes(), "stock microdroid")
    _require(
        len(raw) == c["system_original_size"], "stock system footer geometry changed"
    )
    erofs = work / "stock.erofs"
    erofs.write_bytes(raw)
    sb = rebuild.superblock_params(erofs, strict_metadata=True)
    metadata = ifma._erofs_meta_map(erofs)  # refuses unknown xattrs/hardlink shapes
    tree = work / "guest-tree"
    _extract(erofs, tree, otatools)
    runtime = c["runtime"]
    _require(
        _sha(tree / runtime["path"]) == runtime["sha256"],
        "wrong nested runtime container",
    )
    with zipfile.ZipFile(tree / runtime["path"]) as z:
        _require(
            hashlib.sha256(z.read("apex_payload.img")).hexdigest()
            == runtime["payload_sha256"],
            "wrong nested runtime payload",
        )
    ally_adler.patch_tree("microdroid", tree)
    ifma.PATCH_IFMA.run(
        tree,
        ROOT / "builder/ifma/microdroid-manager.json",
        work / "manager.json",
        True,
        scope=("microdroid", "system"),
    )
    ifma.patch_apex_file(
        tree / runtime["path"],
        RUNTIME_ALIAS,
        work / "runtime",
        otatools,
        keys,
        adler_scope="microdroid_runtime",
    )
    fc, fs = work / "file-contexts", work / "fs-config"
    rebuild.write_configs("/", tree, metadata, fc, fs, allow_caps=True)
    rebuild.restore_mtimes(tree, metadata)
    rebuilt = work / "rebuilt.erofs"
    args = [
        "-z",
        "lz4hc,level=9",
        "-b",
        "4096",
        "-T",
        str(sb["build_time"]),
        "-U",
        sb["uuid"],
    ]
    if sb["volume_name"]:
        args += ["-L", sb["volume_name"]]
    if sb["pcluster_size"]:
        args += ["-C", str(sb["pcluster_size"])]
    ifma._run_ot(
        otatools,
        "mkfs.erofs",
        *args,
        "--mount-point",
        "/",
        "--fs-config-file",
        fs,
        "--file-contexts",
        fc,
        rebuilt,
        tree,
    )
    ifma._assert_same_erofs_metadata(erofs, rebuilt, "microdroid")
    new = rebuilt.read_bytes()
    span = c["system_original_size"]
    _require(len(new) <= span, "rebuilt guest exceeds original data span")
    # INVALID INTERMEDIATE: old metadata describes old bytes. This file stays
    # disposable and is never published. Official signer truncates at the old
    # original-size footer, regenerates the hashtree, then rewrites dependants.
    target = candidate / SYSTEM
    with target.open("r+b") as f:
        f.write(new)
        f.write(b"\0" * (span - len(new)))
    _require(
        target.stat().st_size == c["files"][SYSTEM]["size"], "staging geometry changed"
    )
    # Upstream must retain asserts, version validation and bootconfig update.
    _run(
        [
            sys.executable,
            "-I",
            tools / "sign_virt_apex.py",
            "--avbtool",
            Path(otatools) / "bin/avbtool",
            keys["payload_key"],
            candidate,
        ],
        env,
    )
    verified = verify_chain(
        prof, stock, candidate, work / "verify", otatools, keys, tools_dir=tools
    )
    verified.assert_bound(candidate)
    _require(
        ifma._index_tree(root) == ifma._index_tree(stock),
        "caller stock changed during rebuild",
    )
    # Caller exclusively owns a disposable extracted tree. Keep a rollback copy
    # for multi-file publication errors; never leave a half-written chain.
    published = []
    try:
        for rel in c["files"]:
            os.replace(candidate / rel, root / rel)
            published.append(rel)
    except BaseException:
        for rel in published:
            shutil.copy2(stock / rel, root / rel)
        raise
    return _verified(
        str(root.resolve()),
        verified.artifacts,
        verified.kernel_payload_sha256,
        verified.source_sha256,
        verified.stock_payload_root,
    )


def _verified(*args):
    result = VerifiedMicrodroid(*args)
    key = id(result)
    _VERIFIED[key] = weakref.ref(result, lambda _: _VERIFIED.pop(key, None))
    return result


def policy_overlay(verified, payload_tree, base_policy):
    """Only an artifact-bound, internally verified result can rebind exact rows.

    Unrelated scan policy is copied unchanged, including all unsupported entries.
    Caller must still run the ordinary full recursive scanner with this policy.
    """
    _require(
        type(verified) is VerifiedMicrodroid, "invalid Microdroid verification context"
    )
    verified.assert_bound(payload_tree)
    c = _contract()
    result = copy.deepcopy(base_policy)
    pins = dict(verified.artifacts)
    expected = c["policy_rows"]
    for key, rows in expected.items():
        actual = result[key]
        for row in rows:
            _require(
                actual.count(row) == 1,
                "Microdroid base policy row missing/duplicated/drifted",
            )
        touched = [
            row
            for row in actual
            if any(
                node.get("path") in ("system/bin/microdroid_manager", KERNEL, VBMETA)
                for node in row.get("container_chain", [])
            )
        ]
        _require(touched == rows, "unexpected Microdroid policy provenance")
    for row in expected["residuals"]:
        chain = row["container_chain"]
        if chain[-1]["path"] == "system/bin/microdroid_manager":
            result["residuals"].remove(row)
        else:
            edited = result["residuals"][result["residuals"].index(row)]
            for node in edited["container_chain"]:
                if node["path"] == KERNEL:
                    node["sha256"] = pins[KERNEL]
    for row in expected["unsupported_allowlist"]:
        result["unsupported_allowlist"][result["unsupported_allowlist"].index(row)][
            "sha256"
        ] = pins[VBMETA]
    return result


def preflight(otatools, *, tools_dir=None):
    """Enabled-path cache/tool preflight: real tool hashes and AVB version.

    Never call for disabled profiles. No signing, keys or guest inputs used.
    """
    directory, env = _tools(tools_dir, otatools)
    identity = source_identity(directory)
    identity["tools"]["avbtool"] = _sha(Path(otatools) / "bin/avbtool")
    identity["tools"]["lz4"] = _sha(_lz4_path(directory))
    identity["avb_version"] = "1.4.0"
    return identity


def _verify_images(root, otatools, key=None):
    """AVB resolves descriptor siblings by partition name, not product basename.

    Supply conventional *.img siblings so system and initrd descriptors are
    actually verified, never skipped or accidentally resolved outside scratch.
    """
    import ifma

    with tempfile.TemporaryDirectory(prefix="microdroid-avb-") as scratch:
        directory = Path(scratch)
        mapping = {"system": SYSTEM, "vbmeta": VBMETA, "boot": KERNEL, **INITRDS}
        for partition, rel in mapping.items():
            shutil.copyfile(Path(root) / rel, directory / (partition + ".img"))
        for partition in ("system", "vbmeta", "boot"):
            args = ("--key", key) if key is not None else ()
            ifma._run_ot(
                otatools,
                "avbtool",
                "verify_image",
                "--image",
                directory / (partition + ".img"),
                *args,
            )
