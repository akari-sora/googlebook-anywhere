#!/usr/bin/env python3
"""Unit tests for builder/ifma/{patch-ifma.py,elfutil.py,manifest.json}."""
import hashlib
import importlib.util
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
IFMA_DIR = ROOT / "builder/ifma"
SPEC = importlib.util.spec_from_file_location("patch_ifma", IFMA_DIR / "patch-ifma.py")
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)


def synthetic_elf():
    data = bytearray(0x500)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into("<HHIQQQIHHHHHH", data, 16,
                     2, 62, 1, 0, 64, 0x100, 0, 64, 56, 1, 64, 2, 1)
    struct.pack_into("<IIQQQQQQ", data, 64,
                     1, 5, 0, 0x400000, 0, len(data), len(data), 0x1000)
    names = b"\0.shstrtab\0"
    data[0x300:0x300 + len(names)] = names
    struct.pack_into("<IIQQQQIIQQ", data, 0x140,
                     1, 3, 0, 0, 0x300, len(names), 0, 0, 1, 0)
    context = bytes(range(PATCHER.CONTEXT_LENGTH))
    context = (context[:PATCHER.CONTEXT_PATCH_DELTA] + PATCHER.ORIGINAL
               + context[PATCHER.CONTEXT_PATCH_DELTA + len(PATCHER.ORIGINAL):])
    data[0x200:0x200 + len(context)] = context
    table = bytes((index * 17) & 0xff for index in range(PATCHER.TABLE_LENGTH))
    data[0x380:0x400] = table
    original = bytes(data)
    patched = bytearray(original)
    patched[0x21a:0x21a + len(PATCHER.REPLACEMENT)] = PATCHER.REPLACEMENT
    patched = bytes(patched)
    row = {
        "path": "bin/target",
        "sha256": hashlib.sha256(original).hexdigest(),
        "patched_sha256": hashlib.sha256(patched).hexdigest(),
        "site_va": 0x400221,
        "file_offset": 0x221,
        "function_va": 0x400200,
        "function_size": PATCHER.CONTEXT_LENGTH,
        "size": len(original),
        "table_va": 0x400380,
        "table_file_offset": 0x380,
        "table_hex": table.hex(),
        "context_va": 0x400200,
        "context_offset": 0x200,
        "context_hex": context.hex(),
    }
    return original, patched, row


class ValidateTests(unittest.TestCase):
    def test_instruction_constants_match_manifest_and_patched_hash_logic(self):
        manifest = json.loads((IFMA_DIR / "manifest.json").read_text(encoding="utf-8"))
        manifest_original = bytes.fromhex(manifest["original_hex"])
        manifest_replacement = bytes.fromhex(manifest["replacement_hex"])
        self.assertEqual(PATCHER.ORIGINAL, manifest_original)
        self.assertEqual(PATCHER.REPLACEMENT, manifest_replacement)
        self.assertEqual(len(manifest_original), len(manifest_replacement))

        original, _, row = synthetic_elf()
        patched = bytearray(original)
        patch_offset = row["file_offset"] - PATCHER.SITE_PREFIX_LENGTH
        patched[patch_offset:patch_offset + len(manifest_replacement)] = manifest_replacement
        self.assertEqual(hashlib.sha256(patched).hexdigest(), row["patched_sha256"])
        self.assertEqual(PATCHER.validate(original, row)["patched"], bytes(patched))

    def test_original_and_patched_are_idempotent(self):
        original, patched, row = synthetic_elf()
        first = PATCHER.validate(original, row)
        self.assertEqual(first["state"], "original")
        self.assertEqual(first["patched"], patched)
        second = PATCHER.validate(patched, row)
        self.assertEqual(second["state"], "patched")
        self.assertEqual(second["patched"], patched)

    def test_unknown_hash_is_refused(self):
        original, _, row = synthetic_elf()
        tampered = bytearray(original)
        tampered[0x450] ^= 1
        with self.assertRaisesRegex(PATCHER.PatchError, "unknown full-file SHA256"):
            PATCHER.validate(bytes(tampered), row)

    def test_manifest_context_tamper_is_refused(self):
        original, _, row = synthetic_elf()
        context = bytearray.fromhex(row["context_hex"])
        context[PATCHER.CONTEXT_PATCH_DELTA] ^= 1
        row["context_hex"] = context.hex()
        with self.assertRaisesRegex(PATCHER.PatchError, "manifest context"):
            PATCHER.validate(original, row)

    def test_known_hash_cannot_hide_context_tamper(self):
        original, _, row = synthetic_elf()
        tampered = bytearray(original)
        tampered[0x205] ^= 1
        row["sha256"] = hashlib.sha256(tampered).hexdigest()
        with self.assertRaisesRegex(PATCHER.PatchError, "unexpected function context"):
            PATCHER.validate(bytes(tampered), row)

    def test_bad_mapped_offset_is_refused(self):
        original, _, row = synthetic_elf()
        row["file_offset"] += 1
        with self.assertRaisesRegex(PATCHER.PatchError, "ELF mapping"):
            PATCHER.validate(original, row)

    def test_table_tamper_is_refused_even_with_known_hash(self):
        original, _, row = synthetic_elf()
        tampered = bytearray(original)
        tampered[0x390] ^= 1
        row["sha256"] = hashlib.sha256(tampered).hexdigest()
        with self.assertRaisesRegex(PATCHER.PatchError, "constant table"):
            PATCHER.validate(bytes(tampered), row)


class BatchTests(unittest.TestCase):
    def _tmp(self):
        return tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())

    def _tree(self, directory):
        original, patched, base_row = synthetic_elf()
        root = Path(directory) / "root"
        root.mkdir()
        rows = []
        shipped = json.loads((IFMA_DIR / "manifest.json").read_text(encoding="utf-8"))["targets"]
        by_path = {r["path"]: r for r in shipped}
        for path_str in sorted(PATCHER.EXPECTED_PATHS):
            srow = by_path[path_str]
            row = dict(base_row)
            row["path"] = path_str
            for k in ("partition", "image_path", "apex_path", "payload_path", "note"):
                if k in srow:
                    row[k] = srow[k]
            path = root / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
            rows.append(row)
        manifest = Path(directory) / "manifest.json"
        manifest.write_text(json.dumps({"schema": 1, "targets": rows}), encoding="utf-8")
        return root, manifest, rows, original, patched

    def test_every_target_is_preflighted_before_first_write(self):
        with self._tmp() as directory:
            root, manifest, rows, original, _ = self._tree(directory)
            bad = root / rows[-1]["path"]
            changed = bytearray(bad.read_bytes())
            changed[0x450] ^= 1
            bad.write_bytes(changed)
            with self.assertRaisesRegex(PATCHER.PatchError, "unknown full-file SHA256"):
                PATCHER.run(root, manifest, Path(directory) / "report.json", True)
            self.assertEqual((root / rows[0]["path"]).read_bytes(), original)
            self.assertFalse((Path(directory) / "report.json").exists())

    def test_apply_then_reapply_is_idempotent(self):
        with self._tmp() as directory:
            root, manifest, rows, _, patched = self._tree(directory)
            report_path = Path(directory) / "report.json"
            first = PATCHER.run(root, manifest, report_path, True)
            self.assertEqual(len(first["changed"]), PATCHER.TARGET_COUNT)
            self.assertTrue(all((root / row["path"]).read_bytes() == patched for row in rows))
            second = PATCHER.run(root, manifest, report_path, True)
            self.assertEqual(second["changed"], [])
            self.assertTrue(all(row["before"] == "patched" for row in second["targets"]))
            first_row = second["targets"][0]
            self.assertEqual(first_row["site_offset"], 0x221)
            self.assertEqual(first_row["patch_offset"], 0x21a)
            self.assertEqual(len(first_row["original_sha256"]), 64)
            self.assertEqual(len(first_row["patched_sha256"]), 64)

    def test_target_symlink_is_refused(self):
        with self._tmp() as directory:
            root, manifest, rows, _, _ = self._tree(directory)
            target = root / rows[0]["path"]
            real = target.with_name("real")
            target.rename(real)
            target.symlink_to(real.name)
            with self.assertRaisesRegex(PATCHER.PatchError, "symlink"):
                PATCHER.run(root, manifest, Path(directory) / "report.json", False)

    def test_duplicate_target_inode_is_refused(self):
        with self._tmp() as directory:
            root, manifest, rows, _, _ = self._tree(directory)
            first = root / rows[0]["path"]
            second = root / rows[1]["path"]
            second.unlink()
            second.hardlink_to(first)
            with self.assertRaisesRegex(PATCHER.PatchError, "duplicate target inode"):
                PATCHER.run(root, manifest, Path(directory) / "report.json", False)

    def test_arbitrary_seventeen_paths_are_refused(self):
        with self._tmp() as directory:
            root, manifest, rows, _, _ = self._tree(directory)
            rows[0]["path"] = "system/bin/unexpected"
            manifest.write_text(json.dumps({"schema": 1, "targets": rows}), encoding="utf-8")
            with self.assertRaisesRegex(PATCHER.PatchError, "fixed 17-target set"):
                PATCHER.run(root, manifest, Path(directory) / "report.json", False)

    def test_report_aliasing_target_is_refused_before_writes(self):
        with self._tmp() as directory:
            root, manifest, rows, original, _ = self._tree(directory)
            report = root / rows[-1]["path"]
            with self.assertRaisesRegex(PATCHER.PatchError, "report path aliases"):
                PATCHER.run(root, manifest, report, True)
            self.assertEqual((root / rows[0]["path"]).read_bytes(), original)

    def test_report_aliasing_manifest_or_helper_is_refused_before_writes(self):
        for protected in ("manifest", "helper"):
            with self.subTest(protected=protected), self._tmp() as directory:
                root, manifest, rows, original, _ = self._tree(directory)
                report = manifest if protected == "manifest" else IFMA_DIR / "patch-ifma.py"
                with self.assertRaisesRegex(PATCHER.PatchError, "manifest or a helper"):
                    PATCHER.run(root, manifest, report, True)
                self.assertEqual((root / rows[0]["path"]).read_bytes(), original)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is not supported")
    def test_special_report_file_is_refused_before_writes(self):
        with self._tmp() as directory:
            root, manifest, rows, original, _ = self._tree(directory)
            report = Path(directory) / "report.fifo"
            os.mkfifo(report)
            with self.assertRaisesRegex(PATCHER.PatchError, "report path must be a regular file"):
                PATCHER.run(root, manifest, report, True)
            self.assertEqual((root / rows[0]["path"]).read_bytes(), original)

    def test_shipped_manifest_invariants(self):
        targets = PATCHER._load_manifest(IFMA_DIR / "manifest.json")
        self.assertEqual(len(targets), 17)
        v_plain = PATCHER._select_scope(targets, ("partition", "vendor_a"))
        s_plain = PATCHER._select_scope(targets, ("partition", "system_a"))
        a_audio = PATCHER._select_scope(targets, ("apex", "apex/com.android.hardware.audio.desktop.apex"))
        a_fp = PATCHER._select_scope(targets, ("apex", "apex/com.android.hardware.fingerprint.crosfp.apex"))
        a_virt = PATCHER._select_scope(targets, ("apex", "system/apex/com.google.android.virt.apex"))
        self.assertEqual(sorted(p for p, _ in v_plain), [
            "bin/ectool",
            "bin/hw/android.hardware.usb-service.desktop",
            "lib64/libzmij.dylib.so",
        ])
        self.assertEqual(sorted(p for p, _ in s_plain), [
            "system/bin/aisealhostservice",
            "system/bin/ectool",
            "system/bin/prefetch",
            "system/lib64/libinput.so",
            "system/lib64/libinputflinger.so",
            "system/lib64/libzmij.dylib.so",
        ])
        self.assertEqual([p for p, _ in a_audio], ["lib64/libzmij.dylib.so"])
        self.assertEqual([p for p, _ in a_fp], ["bin/hw/android.hardware.biometrics.fingerprint-service.crosfp"])
        self.assertEqual(len(a_virt), 5)
        sx_plain = PATCHER._select_scope(targets, ("partition", "system_ext_a"))
        self.assertEqual([p for p, _ in sx_plain], ["bin/trusty_security_vm_launcher"])
        self.assertEqual(len(v_plain) + len(s_plain) + len(a_audio) + len(a_fp) + len(a_virt) + len(sx_plain), 17)

    def test_scoped_partition_and_apex_modes(self):
        with self._tmp() as directory:
            _, manifest, _, original, patched = self._tree(directory)
            ven_root = Path(directory) / "ven"
            rels = ("bin/ectool", "bin/hw/android.hardware.usb-service.desktop", "lib64/libzmij.dylib.so")
            for rel in rels:
                p = ven_root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(original)
            rep = PATCHER.run(ven_root, manifest, Path(directory) / "ven.json", True, scope=("partition", "vendor_a"))
            self.assertEqual(len(rep["changed"]), 3)
            self.assertTrue(all((ven_root / rel).read_bytes() == patched for rel in rels))
            with self.assertRaisesRegex(PATCHER.PatchError, "unknown patch scope"):
                PATCHER.run(ven_root, manifest, Path(directory) / "bad.json", False, scope=("partition", "odm_a"))
            with self.assertRaisesRegex(PATCHER.PatchError, "unknown patch scope"):
                PATCHER.run(ven_root, manifest, Path(directory) / "bad.json", False, scope=("apex", "vendor/apex/other.apex"))



def _install_fake_otatools(ot_dir):
    """Create a self-contained otatools/bin with real-EROFS mkfs.erofs/fsck.erofs and AVB0 avbtool."""
    bin_dir = Path(ot_dir) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    lib_dir = str(ROOT / "builder/lib")

    mkfs = bin_dir / "mkfs.erofs"
    mkfs.write_text(
        f"""#!/usr/bin/env python3
import os, struct, sys, uuid
from pathlib import Path
sys.path.insert(0, {lib_dir!r})
import rebuild

args = sys.argv[1:]
build_time = 0
uuid_str = "00000000-0000-0000-0000-000000000000"
pcluster = None
mount = "/"
fs_file = None
fc_file = None
pos = []
i = 0
while i < len(args):
    a = args[i]
    if a in ("-z", "-b"):
        i += 2
    elif a == "-T":
        build_time = int(args[i + 1]); i += 2
    elif a == "-U":
        uuid_str = args[i + 1]; i += 2
    elif a == "-C":
        pcluster = int(args[i + 1]); i += 2
    elif a == "--mount-point":
        mount = args[i + 1]; i += 2
    elif a == "--fs-config-file":
        fs_file = Path(args[i + 1]); i += 2
    elif a == "--file-contexts":
        fc_file = Path(args[i + 1]); i += 2
    else:
        pos.append(a); i += 1

out_img = Path(pos[0])
src_dir = Path(pos[1])
fs_lines = [ln for ln in fs_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
fc_lines = [ln for ln in fc_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
assert len(fs_lines) == len(fc_lines), (len(fs_lines), len(fc_lines))

meta_by_rel = {{}}
prefix = mount.lstrip("/")
for fs_ln, fc_ln in zip(fs_lines, fc_lines):
    selinux = fc_ln.strip().rsplit(" ", 1)[1]
    caps_bytes = None
    if fs_ln.startswith(" "):
        rel = ""
        parts = fs_ln.strip().split()
        uid, gid, mode = int(parts[0]), int(parts[1]), int(parts[2], 8)
        extra = parts[3:]
    else:
        parts = fs_ln.strip().split()
        fs_path = parts[0]
        uid, gid, mode = int(parts[1]), int(parts[2]), int(parts[3], 8)
        extra = parts[4:]
        if prefix and fs_path == prefix:
            rel = ""
        elif prefix and fs_path.startswith(prefix + "/"):
            rel = fs_path[len(prefix) + 1:]
        else:
            rel = fs_path
    for tok in extra:
        if tok.startswith("capabilities="):
            cval = int(tok.split("=", 1)[1], 16)
            caps_bytes = struct.pack("<5I", 0x02000001, cval & 0xFFFFFFFF, 0, (cval >> 32) & 0xFFFFFFFF, 0)
    meta_by_rel[rel] = {{"uid": uid, "gid": gid, "mode": mode, "selinux": selinux, "caps": caps_bytes}}

rels = [""]
children = {{"": []}}
is_dir_map = {{"": True}}
for dirpath, dirnames, filenames in os.walk(src_dir, followlinks=False):
    dirnames.sort()
    filenames.sort()
    rdir = os.path.relpath(dirpath, src_dir)
    if rdir == ".":
        rdir = ""
    for d in dirnames:
        crel = d if rdir == "" else f"{{rdir}}/{{d}}"
        rels.append(crel)
        is_dir_map[crel] = True
        children.setdefault(crel, [])
        children.setdefault(rdir, []).append((d, crel, True))
    for fn in filenames:
        crel = fn if rdir == "" else f"{{rdir}}/{{fn}}"
        rels.append(crel)
        is_dir_map[crel] = False
        children.setdefault(rdir, []).append((fn, crel, False))

def pack_xattr(name_b, val_b):
    raw = struct.pack("<BBH", len(name_b), 6, len(val_b)) + name_b + val_b
    pad = ((len(raw) + 3) & ~3) - len(raw)
    return raw + b"\\x00" * pad

nid_of = {{}}
xattr_of = {{}}
cur_nid = 0
for rel in rels:
    m = meta_by_rel[rel]
    xe = pack_xattr(b"selinux", m["selinux"].encode("utf-8") + b"\\x00")
    if m["caps"]:
        xe += pack_xattr(b"capability", m["caps"])
    xsize = 12 + len(xe)
    xcount = 1 + len(xe) // 4
    nid_of[rel] = cur_nid
    xattr_of[rel] = (xcount, b"\\x00" * 12 + xe)
    cur_nid += (64 + xsize + 31) // 32

data_blk_start = 1 + (cur_nid * 32 + 4095) // 4096
cur_blk = data_blk_start
blk_of = {{}}
size_of = {{}}
blocks_data = {{}}

for rel in rels:
    if is_dir_map[rel]:
        parent_rel = "" if "/" not in rel else rel.rsplit("/", 1)[0]
        ents = [(".", nid_of[rel], 2), ("..", nid_of[parent_rel], 2)]
        for name, crel, cdir in sorted(children.get(rel, [])):
            ents.append((name, nid_of[crel], 2 if cdir else 1))
        noff = 12 * len(ents)
        hdr_buf = bytearray(noff)
        names_buf = bytearray()
        for idx, (name, cnid, ftype) in enumerate(ents):
            nb = name.encode("utf-8")
            struct.pack_into("<QHB", hdr_buf, idx * 12, cnid, noff + len(names_buf), ftype)
            names_buf.extend(nb)
        dblk = bytearray(4096)
        dblk[:noff + len(names_buf)] = hdr_buf + names_buf
        blk_of[rel] = cur_blk
        size_of[rel] = 4096
        blocks_data[cur_blk] = bytes(dblk)
        cur_blk += 1
    else:
        raw = (src_dir / rel).read_bytes()
        if (out_img.name == "system.erofs"
                and os.environ.get("GBOS_TEST_IFMA_MKFS_DRIFT") == "content"
                and rel == "system/etc/untouched.cfg"):
            raw += b"synthetic-content-drift"
        nblks = max(1, (len(raw) + 4095) // 4096)
        blk_of[rel] = cur_blk
        size_of[rel] = len(raw)
        padded = raw + b"\\x00" * (nblks * 4096 - len(raw))
        for b in range(nblks):
            blocks_data[cur_blk + b] = padded[b * 4096 : (b + 1) * 4096]
        cur_blk += nblks

img = bytearray(cur_blk * 4096)
struct.pack_into("<I", img, 1024 + 0, rebuild.EROFS_MAGIC)
img[1024 + 12] = 12
struct.pack_into("<H", img, 1024 + 14, 0)
struct.pack_into("<Q", img, 1024 + 24, build_time)
struct.pack_into("<I", img, 1024 + 40, 1)
struct.pack_into("<I", img, 1024 + 44, 0)
img[1024 + 48 : 1024 + 64] = uuid.UUID(uuid_str).bytes
if out_img.name == "system.erofs" and os.environ.get("GBOS_TEST_IFMA_MKFS_DRIFT") == "uuid":
    img[1024 + 48 : 1024 + 64] = uuid.UUID("11111111-2222-3333-4444-555555555555").bytes
if pcluster:
    struct.pack_into("<I", img, 1024 + 80, 0x3)
    struct.pack_into("<H", img, 1024 + 84, 0x1)
    struct.pack_into("<HHH", img, 1024 + 128, 14, 65535, pcluster // 4096)

for rel in rels:
    nid = nid_of[rel]
    off = 4096 + nid * 32
    m = meta_by_rel[rel]
    xcount, xbytes = xattr_of[rel]
    mode = m["mode"]
    if (out_img.name == "system.erofs"
            and os.environ.get("GBOS_TEST_IFMA_MKFS_DRIFT") == "metadata"
            and rel == "system/etc/untouched.cfg"):
        mode ^= 0o001
    mode_full = (0o040000 if is_dir_map[rel] else 0o100000) | (mode & 0o7777)
    struct.pack_into("<HHH", img, off, 1, xcount, mode_full)
    struct.pack_into("<Q", img, off + 8, size_of[rel])
    struct.pack_into("<I", img, off + 16, blk_of[rel])
    struct.pack_into("<II", img, off + 24, m["uid"], m["gid"])
    struct.pack_into("<I", img, off + 44, 1)
    img[off + 64 : off + 64 + len(xbytes)] = xbytes

for bidx, bbytes in blocks_data.items():
    img[bidx * 4096 : (bidx + 1) * 4096] = bbytes

out_img.write_bytes(bytes(img))
""",
        encoding="utf-8",
    )
    mkfs.chmod(0o755)

    fsck = bin_dir / "fsck.erofs"
    fsck.write_text(
        f"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
sys.path.insert(0, {lib_dir!r})
import rebuild

extract_dir = None
img_path = None
for a in sys.argv[1:]:
    if a.startswith("--extract="):
        extract_dir = Path(a.split("=", 1)[1])
    elif not a.startswith("-"):
        img_path = Path(a)

data, blksz, meta, xattr_blk, root_nid = rebuild.load_image(img_path)

def walk(nid, rel):
    r = rebuild.inode_raw(data, blksz, meta, nid)
    target = extract_dir if rel == "" else extract_dir / rel
    target.mkdir(parents=True, exist_ok=True)
    for cnid, ftype, name in rebuild.list_dir(data, blksz, meta, nid):
        crel = name if rel == "" else f"{{rel}}/{{name}}"
        if ftype == 2:
            walk(cnid, crel)
        else:
            cr = rebuild.inode_raw(data, blksz, meta, cnid)
            start = cr["iu"] * blksz
            fbytes = data[start : start + cr["size"]]
            fp = extract_dir / crel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(fbytes)
            os.chmod(fp, cr["mode"] & 0o777)
    os.chmod(target, r["mode"] & 0o777)

walk(root_nid, "")
""",
        encoding="utf-8",
    )
    fsck.chmod(0o755)

    avb = bin_dir / "avbtool"
    avb.write_text(
        """#!/usr/bin/env python3
import hashlib, json, struct, subprocess, sys
from pathlib import Path

cmd = sys.argv[1]
args = sys.argv[2:]

def pub_blob_from_key(kpath):
    raw = Path(kpath).read_bytes()
    if len(raw) == 1032 and struct.unpack_from(">I", raw, 0)[0] == 4096:
        return raw
    flag = "-pubin" if b"BEGIN PUBLIC KEY" in raw else "-pubout"
    proc = subprocess.run(["openssl", "rsa", "-in", str(kpath), flag, "-outform", "DER"],
                          capture_output=True, check=True)
    der = proc.stdout
    prefix = bytes.fromhex("30820222300d06092a864886f70d01010105000382020f003082020a0282020100")
    suffix = bytes.fromhex("0203010001")
    modulus = der[len(prefix) : len(der) - len(suffix)]
    if len(modulus) != 512:
        raise SystemExit(f"unexpected modulus length {len(modulus)}")
    return struct.pack(">II", 4096, 1) + modulus + (b"\\x00" * 512)

def parse_footer_meta(raw):
    if len(raw) < 64 or raw[-64:-60] != b"AVBf":
        raise SystemExit("missing AVBf footer")
    orig_size, vbmeta_off, hdr_len = struct.unpack_from(">QQQ", raw, len(raw) - 64 + 12)
    if raw[vbmeta_off : vbmeta_off + 4] != b"AVB0":
        raise SystemExit("missing AVB0 header at vbmeta_off")
    auth, aux = struct.unpack_from(">QQ", raw, vbmeta_off + 12)
    meta = json.loads(raw[vbmeta_off + 256 + auth : vbmeta_off + 256 + auth + aux].decode("utf-8"))
    return orig_size, vbmeta_off, meta

if cmd == "extract_public_key":
    key = args[args.index("--key") + 1]
    out = args[args.index("--output") + 1]
    Path(out).write_bytes(pub_blob_from_key(key))
elif cmd == "add_hashtree_footer":
    if "--calc_max_image_size" in args:
        ps = int(args[args.index("--partition_size") + 1])
        print(ps - 8192)
        sys.exit(0)
    img_p = Path(args[args.index("--image") + 1])
    pname = args[args.index("--partition_name") + 1]
    psize = int(args[args.index("--partition_size") + 1])
    salt = args[args.index("--salt") + 1] if "--salt" in args else ""
    key = args[args.index("--key") + 1] if "--key" in args else None
    props = []
    for idx, a in enumerate(args):
        if a == "--prop":
            props.append(args[idx + 1])
    erofs = img_p.read_bytes()
    orig_size = len(erofs)
    vbmeta_off = ((orig_size + 4095) // 4096) * 4096
    if vbmeta_off + 4096 > psize:
        raise SystemExit("partition_size too small")
    pblob = pub_blob_from_key(key) if key else b""
    meta = {
        "partition_name": pname,
        "orig_size": orig_size,
        "part_size": psize,
        "salt": salt,
        "props": props,
        "image_sha256": hashlib.sha256(erofs).hexdigest(),
        "pubkey_hex": pblob.hex(),
    }
    mbytes = json.dumps(meta, sort_keys=True).encode("utf-8")
    hdr = bytearray(256)
    hdr[0:4] = b"AVB0"
    struct.pack_into(">QQ", hdr, 12, 0, len(mbytes))
    hdr = bytes(hdr) + mbytes
    footer = bytearray(64)
    footer[0:4] = b"AVBf"
    struct.pack_into(">II", footer, 4, 1, 0)
    struct.pack_into(">QQQ", footer, 12, orig_size, vbmeta_off, len(hdr))
    out = bytearray(psize)
    out[:orig_size] = erofs
    out[vbmeta_off : vbmeta_off + len(hdr)] = hdr
    out[psize - 64 : psize] = footer
    img_p.write_bytes(bytes(out))
elif cmd == "verify_image":
    img_p = Path(args[args.index("--image") + 1])
    key = args[args.index("--key") + 1] if "--key" in args else None
    raw = img_p.read_bytes()
    if raw[:4] == b"AVB0":
        auth, aux = struct.unpack_from(">QQ", raw, 12)
        meta = json.loads(raw[256 + auth : 256 + auth + aux].decode("utf-8"))
        if key and meta.get("pubkey_hex") != pub_blob_from_key(key).hex():
            raise SystemExit("vbmeta key mismatch")
        sys.exit(0)
    orig_size, _, meta = parse_footer_meta(raw)
    if hashlib.sha256(raw[:orig_size]).hexdigest() != meta["image_sha256"]:
        raise SystemExit("AVB payload digest mismatch")
    if key:
        expected_hex = pub_blob_from_key(key).hex()
        if meta.get("pubkey_hex") != expected_hex:
            raise SystemExit("AVB public key mismatch")
elif cmd == "info_image":
    img_p = Path(args[args.index("--image") + 1])
    raw = img_p.read_bytes()
    if raw[:4] == b"AVB0":
        auth, aux = struct.unpack_from(">QQ", raw, 12)
        meta = json.loads(raw[256 + auth : 256 + auth + aux].decode("utf-8"))
        for dname, dmeta in sorted(meta.get("descriptors", {}).items()):
            print(f"    Partition Name: {dname}")
            print(f"    Salt: {dmeta.get('salt', '')}")
            print(f"    Digest: {dmeta.get('image_sha256', '')}")
            for p in dmeta.get("props", []):
                k, v = p.split(":", 1)
                print(f"    Prop: {k} -> '{v}'")
    else:
        _, _, meta = parse_footer_meta(raw)
        print(f"    Partition Name: {meta['partition_name']}")
        print(f"    Salt: {meta.get('salt', '')}")
        for p in meta.get("props", []):
            k, v = p.split(":", 1)
            print(f"    Prop: {k} -> '{v}'")
elif cmd == "update_partition_descriptor":
    in_vb = Path(args[args.index("--image") + 1])
    part_img = Path(args[args.index("--partition_image") + 1])
    key = args[args.index("--key") + 1]
    out_vb = Path(args[args.index("--output") + 1])
    raw_vb = in_vb.read_bytes()
    if raw_vb[:4] == b"AVB0":
        auth, aux = struct.unpack_from(">QQ", raw_vb, 12)
        vb_meta = json.loads(raw_vb[256 + auth : 256 + auth + aux].decode("utf-8"))
    else:
        vb_meta = {"descriptors": {}}
    _, _, pmeta = parse_footer_meta(part_img.read_bytes())
    part_name = args[args.index("--partition_name") + 1] if "--partition_name" in args else pmeta["partition_name"]
    if pmeta["partition_name"] != part_name:
        raise SystemExit(f"partition_name mismatch: {pmeta['partition_name']} vs {part_name}")
    vb_meta.setdefault("descriptors", {})[part_name] = pmeta
    vb_meta["pubkey_hex"] = pub_blob_from_key(key).hex()
    vb_meta["rollback_index"] = int(args[args.index("--rollback_index") + 1])
    mbytes = json.dumps(vb_meta, sort_keys=True).encode("utf-8")
    hdr = bytearray(256)
    hdr[0:4] = b"AVB0"
    struct.pack_into(">QQ", hdr, 12, 0, len(mbytes))
    out_vb.write_bytes(bytes(hdr) + mbytes)
elif cmd == "calculate_vbmeta_digest":
    in_vb = Path(args[args.index("--image") + 1])
    print(hashlib.sha256(in_vb.read_bytes()).hexdigest())
else:
    raise SystemExit(f"unknown avbtool command: {cmd}")
""",
        encoding="utf-8",
    )
    avb.chmod(0o755)
    return Path(ot_dir)


def _make_erofs_image(ot_dir, tree_dir, meta_map, out_erofs, uuid_str, build_time=1726000000, pcluster=32768, allow_caps=False):
    sys.path.insert(0, str(ROOT / "builder/lib"))
    import rebuild
    work = out_erofs.parent / f".mkfs-{out_erofs.name}"
    if work.exists():
        import shutil
        shutil.rmtree(work)
    work.mkdir(parents=True)
    fc = work / "fc"
    fs = work / "fs"
    rebuild.write_configs("/", tree_dir, meta_map, fc, fs, allow_caps=allow_caps)
    cmd = [
        str(Path(ot_dir) / "bin/mkfs.erofs"),
        "-z", "lz4hc,level=9",
        "-b", "4096",
        "-T", str(build_time),
        "-U", uuid_str,
    ]
    if pcluster:
        cmd += ["-C", str(pcluster)]
    cmd += ["--mount-point", "/", "--fs-config-file", str(fs), "--file-contexts", str(fc), str(out_erofs), str(tree_dir)]
    import subprocess
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _make_stock_apex(ot_dir, apex_file, apex_rel, target_rels, original_elf, keys, work_dir, with_caps=False):
    import shutil, subprocess, uuid, zipfile
    sys.path.insert(0, str(ROOT / "builder/lib"))
    import ifma
    w = Path(work_dir)
    if w.exists():
        shutil.rmtree(w)
    w.mkdir(parents=True)
    ptree = w / "ptree"
    ptree.mkdir()
    meta = {"": {"mode": 0o755, "uid": 0, "gid": 0, "selinux": "u:object_r:system_file:s0", "caps": None}}

    def ensure_dirs(rel_p):
        parts = rel_p.split("/")[:-1]
        cur = ""
        for part in parts:
            cur = part if not cur else f"{cur}/{part}"
            if cur not in meta:
                meta[cur] = {"mode": 0o755, "uid": 0, "gid": 2000, "selinux": "u:object_r:system_file:s0", "caps": None}

    for rel in target_rels:
        ensure_dirs(rel)
        fp = ptree / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(original_elf)
        os.chmod(fp, 0o755)
        meta[rel] = {"mode": 0o755, "uid": 0, "gid": 2000, "selinux": "u:object_r:system_file:s0", "caps": None}

    (ptree / "apex_manifest.pb").write_bytes(b"manifest-pb-" + apex_rel.encode("utf-8"))
    meta["apex_manifest.pb"] = {"mode": 0o644, "uid": 0, "gid": 0, "selinux": "u:object_r:system_file:s0", "caps": None}
    if with_caps:
        ensure_dirs("bin/cap_helper")
        cp = ptree / "bin/cap_helper"
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(b"cap-helper-binary")
        os.chmod(cp, 0o750)
        cap_raw = struct.pack("<5I", 0x02000001, 0x3000, 0, 0, 0)
        meta["bin/cap_helper"] = {"mode": 0o750, "uid": 0, "gid": 2000, "selinux": "u:object_r:system_file:s0", "caps": cap_raw}

    erofs_p = w / "payload.erofs"
    apex_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, apex_rel))
    _make_erofs_image(ot_dir, ptree, meta, erofs_p, apex_uuid, build_time=1726000123, pcluster=32768, allow_caps=with_caps)
    payload_img = w / "apex_payload.img"
    shutil.copyfile(erofs_p, payload_img)
    part_size = ((erofs_p.stat().st_size + 32768 + 4095) // 4096) * 4096
    spec = ifma.APEX_SPECS[apex_rel]
    pubkey_p = w / "apex_pubkey"
    subprocess.run([str(Path(ot_dir) / "bin/avbtool"), "extract_public_key", "--key", str(keys["payload_key"]), "--output", str(pubkey_p)], check=True)
    subprocess.run([
        str(Path(ot_dir) / "bin/avbtool"), "add_hashtree_footer",
        "--image", str(payload_img),
        "--partition_name", "apex_payload",
        "--partition_size", str(part_size),
        "--key", str(keys["payload_key"]),
        "--algorithm", "SHA256_RSA4096",
        "--hash_algorithm", "sha256",
        "--salt", spec["salt"],
        "--block_size", "4096",
        "--do_not_generate_fec",
        "--prop", f"apex.key:{spec['apex_key']}",
    ], check=True)
    raw_zip = w / "raw.apex"
    with zipfile.ZipFile(raw_zip, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("apex_manifest.pb", b"outer-manifest-" + apex_rel.encode("utf-8"))
        zf.writestr("AndroidManifest.xml", b"<manifest/>")
        zf.writestr("apex_pubkey", pubkey_p.read_bytes())
        zf.writestr("apex_payload.img", payload_img.read_bytes())
    unsigned_zip = w / "unsigned.apex"
    ifma.repack_apex_zip(raw_zip, unsigned_zip, payload_img.read_bytes(), pubkey_p.read_bytes())
    apex_file.parent.mkdir(parents=True, exist_ok=True)
    ifma.sign_apex_container(
        unsigned_zip,
        keys["container_key"],
        keys["container_cert_der"],
        apex_file,
        min_sdk=33,
        max_sdk=37,
    )


def _wrap_gpt_super_with_system(img_path, system_img_bytes):
    import uuid
    super_lba = 64
    sys_start_sec = 2048
    sys_sectors = len(system_img_bytes) // 512
    super_sectors = sys_start_sec + sys_sectors + 64
    total_bytes = (super_lba + super_sectors + 64) * 512
    img = bytearray(total_bytes)
    hdr = bytearray(92)
    hdr[:8] = b"EFI PART"
    struct.pack_into("<QII", hdr, 72, 2, 1, 128)
    img[512:604] = hdr
    ent = bytearray(128)
    ent[:16] = uuid.uuid4().bytes_le
    struct.pack_into("<QQ", ent, 32, super_lba, super_lba + super_sectors - 1)
    ent[56 : 56 + 10] = "super".encode("utf-16le")
    img[1024:1152] = ent
    so = super_lba * 512
    img[so + 4096 : so + 4100] = b"gDla"
    tables = bytearray(52 + 24)
    tables[:8] = b"system_a"
    struct.pack_into("<IIII", tables, 36, 0, 0, 1, 0)
    struct.pack_into("<QIQI", tables, 52, sys_sectors, 0, sys_start_sec, 0)
    mh = bytearray(256)
    mh[:4] = b"0PLA"
    struct.pack_into("<I", mh, 8, 256)
    struct.pack_into("<I", mh, 44, len(tables))
    struct.pack_into("<III", mh, 80, 0, 1, 52)
    struct.pack_into("<III", mh, 92, 52, 1, 24)
    img[so + 12288 : so + 12288 + 256] = mh
    img[so + 12288 + 256 : so + 12288 + 256 + len(tables)] = tables
    sys_off = so + sys_start_sec * 512
    img[sys_off : sys_off + len(system_img_bytes)] = system_img_bytes
    Path(img_path).write_bytes(bytes(img))
    return sys_off, len(system_img_bytes)


def run_ifma_checks(check):
    import re, shutil, subprocess, zipfile
    sys.path.insert(0, str(ROOT / "builder/lib"))
    import devprofile, ifma, rebuild

    counts = {"unit": 0, "synthetic": 0, "real-tool": 0, "oracle": 0, "skipped": 0}
    original_check = check
    category = "unit"

    def check(label, ok):
        counts[category] += 1
        original_check(f"{category}: {label}", ok)

    def skip(label, reason):
        counts["skipped"] += 1
        print(f"SKIP {label}: {reason}")

    class VisibleResult(unittest.TestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            self.report(test, True)

        def report(self, test, ok):
            nonlocal category
            category = getattr(test, "category", "unit")
            check(test.id(), ok)

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            skip(test.id(), reason)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            print(self.failures[-1][1])
            self.report(test, False)

        def addError(self, test, err):
            super().addError(test, err)
            print(self.errors[-1][1])
            self.report(test, False)

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                print(self._exc_info_to_string(err, test))
                self.report(subtest, False)

    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (ValidateTests, BatchTests))
    for filename in (
        "ifma-key-signing-tests.py",
        "ifma-metadata-tests.py",
        "ifma-path-tests.py",
        "ifma-microdroid-tests.py",
        "ifma-shell-tests.py",
        "ifma-container-tests.py",
        "ifma-scan-tests.py",
        "ifma-stream-tests.py",
        "ifma-failclosed-tests.py",
        "ifma-boundary-tests.py",
        "ifma-initramfs-r4-tests.py",
        "ifma-policy-r4-tests.py",
        "ifma-page-r4-tests.py",
        "ifma-segment-tests.py",
        "ifma-system-ext-tests.py",
        "ifma-preflight-tests.py",
    ):
        spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), Path(__file__).with_name(filename))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(mod))
    suite.run(VisibleResult())
    category = "unit"

    with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as td:
        base = Path(td)

        # 2. devprofile [cpu_compat] patch_ifma validation & defaults
        for pid in ("kano", "generic-x86", "amd-hx370"):
            p = devprofile.load(ROOT / "profiles" / pid)
            check(f"profile {pid} enables [cpu_compat] patch_ifma = true",
                  p["cpu_compat"] == {"patch_ifma": True, "patch_ally_bootstrap": False}
                  and ifma.enabled(p) is True)

        # Exercise the stock IFMA-only path with the KANO profile. HX370 now
        # explicitly opts into the separately tested read-only updater shim.
        p = devprofile.load(ROOT / "profiles/kano")
        check("IFMA-only fixture does not request the HX370 updater shim",
              p["services"]["readonly_update_engine"] is False)

        kano_toml = (ROOT / "profiles/kano/profile.toml").read_text(encoding="utf-8")
        cc_block_re = re.compile(r"(?ms)^\[cpu_compat\]\n.*?(?=^\[hooks\])")
        no_cc_dir = base / "prof-default/kano"
        shutil.copytree(ROOT / "profiles/kano", no_cc_dir)
        (no_cc_dir / "profile.toml").write_text(
            cc_block_re.sub("", kano_toml),
            encoding="utf-8",
        )
        p_def = devprofile.load(no_cc_dir)
        check("profile without [cpu_compat] defaults patch_ifma to false",
              p_def["cpu_compat"] == {"patch_ifma": False, "patch_ally_bootstrap": False}
              and ifma.enabled(p_def) is False)

        off_cc_dir = base / "prof-off/kano"
        shutil.copytree(ROOT / "profiles/kano", off_cc_dir)
        (off_cc_dir / "profile.toml").write_text(
            kano_toml.replace("patch_ifma = true", "patch_ifma = false"),
            encoding="utf-8",
        )
        p_off = devprofile.load(off_cc_dir)
        check("profile with patch_ifma = false loads as False",
              p_off["cpu_compat"] == {"patch_ifma": False, "patch_ally_bootstrap": False}
              and ifma.enabled(p_off) is False)

        for label, bad_snip, want_err in (
            ("non-bool string patch_ifma", '[cpu_compat]\npatch_ifma = "yes"\n\n', "cpu_compat.patch_ifma: expected bool"),
            ("integer patch_ifma", "[cpu_compat]\npatch_ifma = 1\n\n", "cpu_compat.patch_ifma: expected bool"),
            ("unknown key in cpu_compat", "[cpu_compat]\npatch_ifma = true\nextra = true\n\n", "cpu_compat: unknown key(s) extra"),
        ):
            bdir = base / f"prof-bad-{label.replace(' ', '_')}/kano"
            shutil.copytree(ROOT / "profiles/kano", bdir)
            (bdir / "profile.toml").write_text(
                cc_block_re.sub(bad_snip, kano_toml),
                encoding="utf-8",
            )
            try:
                devprofile.load(bdir)
                got = ""
            except (ValueError, SystemExit) as exc:
                got = str(exc)
            check(f"devprofile refuses {label} ({want_err})", want_err in got)

        bdir_not_tbl = base / "prof-bad-not-table/kano"
        shutil.copytree(ROOT / "profiles/kano", bdir_not_tbl)
        (bdir_not_tbl / "profile.toml").write_text(
            'cpu_compat = "true"\n' + cc_block_re.sub("", kano_toml),
            encoding="utf-8",
        )
        try:
            devprofile.load(bdir_not_tbl)
            got_tbl = ""
        except (ValueError, SystemExit) as exc:
            got_tbl = str(exc)
        check("devprofile refuses cpu_compat not a table (cpu_compat: expected a table)",
              "cpu_compat: expected a table" in got_tbl)



        # Synthetic integration uses fake EROFS/AVB tools; OpenSSL RSA is real.
        category = "synthetic"
        print("SYNTHETIC integration: fake otatools (mkfs.erofs, fsck.erofs, avbtool); real OpenSSL RSA")
        ot_dir = _install_fake_otatools(base / "otatools")
        original_elf, patched_elf, base_row = synthetic_elf()
        shipped_rows = json.loads((IFMA_DIR / "manifest.json").read_text(encoding="utf-8"))["targets"]
        syn_rows = []
        for srow in shipped_rows:
            r = dict(base_row)
            r["path"] = srow["path"]
            for k in ("partition", "image_path", "apex_path", "payload_path", "note"):
                if k in srow:
                    r[k] = srow[k]
            syn_rows.append(r)
        syn_manifest = base / "syn-manifest.json"
        syn_manifest.write_text(json.dumps({"schema": 1, "targets": syn_rows}), encoding="utf-8")

        stock_keys = ifma.ensure_apex_keys(base / "stock-keys")
        build_keys_dir = base / "out/keys"
        bkeys1 = ifma.ensure_apex_keys(build_keys_dir)
        bkeys2 = ifma.ensure_apex_keys(build_keys_dir)
        check("ensure_apex_keys generates RSA-4096 keys with 0600 mode and reuses them idempotently",
              bkeys1 == bkeys2
              and Path(bkeys1["payload_key"]).read_bytes() == Path(bkeys2["payload_key"]).read_bytes()
              and (Path(bkeys1["payload_key"]).stat().st_mode & 0o777) == 0o600
              and (Path(bkeys1["container_key"]).stat().st_mode & 0o777) == 0o600)

        # Prepare synthetic vendor tree
        vtree = base / "out/work/vendor-tree"
        vtree.mkdir(parents=True)
        for rel in ("bin/ectool", "bin/hw/android.hardware.usb-service.desktop", "lib64/libzmij.dylib.so"):
            fp = vtree / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(original_elf)
        (vtree / "etc").mkdir(parents=True, exist_ok=True)
        (vtree / "etc/untouched.txt").write_bytes(b"keep-me\n")
        _make_stock_apex(
            ot_dir,
            vtree / "apex/com.android.hardware.audio.desktop.apex",
            "apex/com.android.hardware.audio.desktop.apex",
            ["lib64/libzmij.dylib.so"],
            original_elf,
            stock_keys,
            base / "mk-audio",
        )
        _make_stock_apex(
            ot_dir,
            vtree / "apex/com.android.hardware.fingerprint.crosfp.apex",
            "apex/com.android.hardware.fingerprint.crosfp.apex",
            ["bin/hw/android.hardware.biometrics.fingerprint-service.crosfp"],
            original_elf,
            stock_keys,
            base / "mk-fp",
        )
        audio_stock_bytes = (vtree / "apex/com.android.hardware.audio.desktop.apex").read_bytes()
        fp_stock_bytes = (vtree / "apex/com.android.hardware.fingerprint.crosfp.apex").read_bytes()

        # Switch-off test on vendor tree first
        vtree_off = base / "out-off/work/vendor-tree"
        shutil.copytree(vtree, vtree_off)
        off_tmp = base / "out-off/work"
        off_res = ifma.patch_vendor_tree(
            p_off, vtree_off, off_tmp, otatools=ot_dir, keys_dir=base / "out-off/keys", manifest_path=syn_manifest
        )
        check("switch off: patch_vendor_tree is a no-op and leaves vendor ELFs and APEXes untouched",
              off_res is False
              and not (base / "out-off/keys").exists()
              and not (off_tmp / "ifma-vendor-plain.json").exists()
              and (vtree_off / "bin/ectool").read_bytes() == original_elf
              and (vtree_off / "lib64/libzmij.dylib.so").read_bytes() == original_elf
              and (vtree_off / "apex/com.android.hardware.audio.desktop.apex").read_bytes() == audio_stock_bytes
              and ifma.changed_vendor_paths(p_off, ["etc/selinux/vendor_file_contexts"]) == ["etc/selinux/vendor_file_contexts"]
              and ifma.verify_rebuilt_vendor_tree(p_off, vtree_off, off_tmp, otatools=ot_dir, manifest_path=syn_manifest) is False)

        # Switch-on test on vendor tree. Record the two post-sign verification
        # calls so removing verify_apex_file() from patch_apex_file() is caught.
        work_tmp = base / "out/work"
        verify_apex_calls = []
        real_verify_apex_file = ifma.verify_apex_file

        def recording_verify_apex_file(*args, **kwargs):
            verify_apex_calls.append(str(args[1]))
            return real_verify_apex_file(*args, **kwargs)

        ifma.verify_apex_file = recording_verify_apex_file
        try:
            on_res = ifma.patch_vendor_tree(
                p, vtree, work_tmp, otatools=ot_dir, keys_dir=build_keys_dir, manifest_path=syn_manifest
            )
        finally:
            ifma.verify_apex_file = real_verify_apex_file
        check("switch on: patch_vendor_tree patches all 3 plain vendor ELFs (including bin/ectool before REMOVE) and both vendor APEXes",
              on_res is True
              and (vtree / "bin/ectool").read_bytes() == patched_elf
              and (vtree / "bin/hw/android.hardware.usb-service.desktop").read_bytes() == patched_elf
              and (vtree / "lib64/libzmij.dylib.so").read_bytes() == patched_elf
              and (vtree / "etc/untouched.txt").read_bytes() == b"keep-me\n"
              and (vtree / "apex/com.android.hardware.audio.desktop.apex").read_bytes() != audio_stock_bytes
              and (vtree / "apex/com.android.hardware.fingerprint.crosfp.apex").read_bytes() != fp_stock_bytes)
        check("patch_apex_file performs post-sign verification for each rebuilt vendor APEX",
              verify_apex_calls == [
                  "apex/com.android.hardware.audio.desktop.apex",
                  "apex/com.android.hardware.fingerprint.crosfp.apex",
              ])

        check("changed_vendor_paths adds the 4 surviving vendor IFMA paths when enabled",
              ifma.changed_vendor_paths(p, ["etc/selinux/vendor_file_contexts"]) == sorted([
                  "etc/selinux/vendor_file_contexts",
                  "apex/com.android.hardware.audio.desktop.apex",
                  "apex/com.android.hardware.fingerprint.crosfp.apex",
                  "bin/hw/android.hardware.usb-service.desktop",
                  "lib64/libzmij.dylib.so",
              ]))

        # Simulate vendor.sh REMOVE unlinking bin/ectool when creating vendor-tree-new
        vtree_new = work_tmp / "vendor-tree-new"
        shutil.copytree(vtree, vtree_new)
        (vtree_new / "bin/ectool").unlink()
        v_verify = ifma.verify_rebuilt_vendor_tree(
            p, vtree_new, work_tmp, otatools=ot_dir, keys_dir=build_keys_dir, manifest_path=syn_manifest
        )
        check("verify_rebuilt_vendor_tree verifies surviving plain ELFs, pre-unlink bin/ectool report, and both rebuilt vendor APEXes",
              v_verify is True)

        # Build synthetic system_a inside GPT super image
        stree_src = base / "sys-src"
        stree_src.mkdir()
        smeta = {"": {"mode": 0o755, "uid": 0, "gid": 0, "selinux": "u:object_r:rootfs:s0", "caps": None}}

        def ensure_sys_dirs(rel_p):
            parts = rel_p.split("/")[:-1]
            cur = ""
            for part in parts:
                cur = part if not cur else f"{cur}/{part}"
                if cur not in smeta:
                    smeta[cur] = {"mode": 0o755, "uid": 0, "gid": 0, "selinux": "u:object_r:system_file:s0", "caps": None}

        for rel in (
            "system/bin/aisealhostservice",
            "system/bin/ectool",
            "system/bin/prefetch",
            "system/lib64/libinput.so",
            "system/lib64/libinputflinger.so",
            "system/lib64/libzmij.dylib.so",
        ):
            ensure_sys_dirs(rel)
            fp = stree_src / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(original_elf)
            os.chmod(fp, 0o755)
            cap_b = struct.pack("<5I", 0x02000001, 0x2000, 0, 0, 0) if rel == "system/bin/prefetch" else None
            smeta[rel] = {"mode": 0o755, "uid": 0, "gid": 2000, "selinux": "u:object_r:system_file:s0", "caps": cap_b}

        ensure_sys_dirs("system/etc/untouched.cfg")
        (stree_src / "system/etc").mkdir(parents=True, exist_ok=True)
        (stree_src / "system/etc/untouched.cfg").write_bytes(b"system-untouched\n")
        smeta["system/etc/untouched.cfg"] = {"mode": 0o644, "uid": 0, "gid": 0, "selinux": "u:object_r:system_file:s0", "caps": None}

        virt_inner = [
            r["payload_path"].lstrip("/")
            for r in syn_rows
            if r.get("apex_path") == "system/apex/com.google.android.virt.apex"
        ]
        ensure_sys_dirs("system/apex/com.google.android.virt.apex")
        _make_stock_apex(
            ot_dir,
            stree_src / "system/apex/com.google.android.virt.apex",
            "system/apex/com.google.android.virt.apex",
            virt_inner,
            original_elf,
            stock_keys,
            base / "mk-virt",
            with_caps=True,
        )
        smeta["system/apex/com.google.android.virt.apex"] = {
            "mode": 0o644, "uid": 0, "gid": 0, "selinux": "u:object_r:system_file:s0", "caps": None
        }
        sys_erofs_stock = base / "sys-stock.erofs"
        _make_erofs_image(ot_dir, stree_src, smeta, sys_erofs_stock, ifma.SYSTEM_UUID, build_time=1726000500, pcluster=32768, allow_caps=True)
        sys_img_stock = base / "sys-stock.img"
        shutil.copyfile(sys_erofs_stock, sys_img_stock)
        sys_part_size = ((sys_erofs_stock.stat().st_size + 65536 + 4095) // 4096) * 4096
        subprocess.run([
            str(ot_dir / "bin/avbtool"), "add_hashtree_footer",
            "--image", str(sys_img_stock),
            "--partition_name", "system",
            "--partition_size", str(sys_part_size),
            "--hash_algorithm", "sha256",
            "--salt", ifma.SYSTEM_SALT,
            "--block_size", "4096",
            "--fec_num_roots", "2",
            "--prop", f"com.android.build.system.os_version:{ifma.SYSTEM_OS_VERSION}",
            "--prop", f"com.android.build.system.fingerprint:{ifma.SYSTEM_FINGERPRINT}",
            "--prop", f"com.android.build.system.security_patch:{ifma.SYSTEM_SECURITY_PATCH}",
        ], check=True)
        disk_img = work_tmp / "stage-vendor.img"
        sys_off, sys_sz = _wrap_gpt_super_with_system(disk_img, sys_img_stock.read_bytes())
        (work_tmp / "offsets.env").write_text("VENDOR_OFF=4096\nVENDOR_SIZE=4096\n", encoding="utf-8")

        # Switch-off test on system partition
        check("switch off: rebuild_system_partition returns None and does not create system.img",
              ifma.rebuild_system_partition(p_off, disk_img, off_tmp, otatools=ot_dir, manifest_path=syn_manifest) is None
              and not (off_tmp / "system.img").exists())

        # Switch-on test on system partition
        sys_res = ifma.rebuild_system_partition(
            p, disk_img, work_tmp, otatools=ot_dir, keys_dir=build_keys_dir, manifest_path=syn_manifest
        )
        ifma_report_file = base / "out/logs/ifma-report.json"
        ifma_rep = json.loads(ifma_report_file.read_text(encoding="utf-8")) if ifma_report_file.is_file() else {}
        check("switch on: rebuild_system_partition patches 6 plain ELFs + 5 virt.apex ELFs, preserves capabilities/UUID, signs AVB footer, and writes logs/ifma-report.json",
              sys_res is not None
              and sys_res["system_off"] == sys_off
              and sys_res["system_size"] == sys_sz
              and (work_tmp / "system.img").stat().st_size == sys_sz
              and f"SYSTEM_OFF={sys_off}\nSYSTEM_SIZE={sys_sz}\n" in (work_tmp / "offsets.env").read_text(encoding="utf-8")
              and len(ifma_rep.get("vendor_plain", [])) == 3
              and len(ifma_rep.get("system_plain", [])) == 6
              and len(ifma_rep.get("virt_apex", [])) == 5)

        # Verify capabilities were preserved on system/bin/prefetch and inside virt.apex bin/cap_helper
        rebuilt_sys_erofs, _ = ifma._strip_avb_footer((work_tmp / "system.img").read_bytes(), "system_a")
        rebuilt_sys_erofs_p = base / "rebuilt-sys.erofs"
        rebuilt_sys_erofs_p.write_bytes(rebuilt_sys_erofs)
        rebuilt_smap = ifma._erofs_meta_map(rebuilt_sys_erofs_p)
        check("rebuild_system_partition preserves security.capability xattr on system/bin/prefetch and EROFS superblock UUID",
              rebuild.fs_uuid(rebuilt_sys_erofs_p) == ifma.SYSTEM_UUID
              and rebuilt_smap["system/bin/prefetch"]["caps"] == struct.pack("<5I", 0x02000001, 0x2000, 0, 0, 0))

        def rebuild_with_mkfs_drift(mode):
            drift_tmp = base / f"system-drift-{mode}"
            drift_tmp.mkdir()
            old_value = os.environ.get("GBOS_TEST_IFMA_MKFS_DRIFT")
            os.environ["GBOS_TEST_IFMA_MKFS_DRIFT"] = mode
            try:
                ifma.rebuild_system_partition(
                    p,
                    disk_img,
                    drift_tmp,
                    otatools=ot_dir,
                    keys_dir=build_keys_dir,
                    manifest_path=syn_manifest,
                )
                return ""
            except SystemExit as exc:
                return str(exc)
            finally:
                if old_value is None:
                    os.environ.pop("GBOS_TEST_IFMA_MKFS_DRIFT", None)
                else:
                    os.environ["GBOS_TEST_IFMA_MKFS_DRIFT"] = old_value

        metadata_drift_err = rebuild_with_mkfs_drift("metadata")
        check("rebuild_system_partition refuses metadata drift introduced by the real system_a rebuild path",
              "system_a: EROFS inode metadata drifted" in metadata_drift_err
              and "system/etc/untouched.cfg" in metadata_drift_err)

        content_drift_err = rebuild_with_mkfs_drift("content")
        check("rebuild_system_partition refuses an eighth changed file after extracting the rebuilt system_a",
              "system tree diff is not the exact CPU patch set" in content_drift_err
              and "system/etc/untouched.cfg" in content_drift_err)

        rebuilt_uuid_drift_err = rebuild_with_mkfs_drift("uuid")
        check("rebuild_system_partition refuses UUID drift introduced by the real system_a rebuild path",
              "system.erofs UUID drifted" in rebuilt_uuid_drift_err)

        # 4. Negative / fail-closed guard checks
        # 4a. Unaligned apex_payload.img in ZIP container is refused
        bad_align_zip = base / "bad-align.apex"
        with zipfile.ZipFile(bad_align_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("apex_payload.img", b"not-aligned-payload")
            zf.writestr("apex_manifest.pb", b"manifest")
            zf.writestr("apex_pubkey", b"pubkey")
        bad_align_signed = base / "bad-align-signed.apex"
        try:
            ifma.sign_apex_container(
                bad_align_zip,
                bkeys1["container_key"],
                bkeys1["container_cert_der"],
                bad_align_signed,
                min_sdk=33,
                max_sdk=37,
            )
            sign_align_err = ""
        except ValueError as exc:
            sign_align_err = str(exc)
        check("sign_apex_container refuses misalignment before writing signatures",
              "not 4096-aligned" in sign_align_err and not bad_align_signed.exists())
        try:
            ifma.verify_apex_container(bad_align_zip)
            align_err = ""
        except ValueError as exc:
            align_err = str(exc)
        check("verify_apex_container refuses unaligned apex_payload.img", "not 4096-aligned" in align_err)

        # 4b. Tampered byte in signed APEX container is refused
        good_audio = vtree_new / "apex/com.android.hardware.audio.desktop.apex"
        tampered_apex = base / "tampered.apex"
        tb = bytearray(good_audio.read_bytes())
        tb[64] ^= 0xFF
        tampered_apex.write_bytes(bytes(tb))
        try:
            ifma.verify_apex_container(tampered_apex)
            tamper_err = ""
        except ValueError as exc:
            tamper_err = str(exc)
        check("verify_apex_container refuses tampered APEX bytes (digest mismatch)", "content digest mismatch" in tamper_err)

        # 4c. Wrong outer container certificate is refused
        try:
            ifma.verify_apex_container(good_audio, expected_cert_der=Path(stock_keys["container_cert_der"]).read_bytes())
            cert_err = ""
        except ValueError as exc:
            cert_err = str(exc)
        check("verify_apex_container refuses a mismatched outer container certificate", "certificate does not match" in cert_err)

        # 4d. Mismatched apex_pubkey inside APEX (signed with build container key, but carrying old stock apex_pubkey) is refused by verify_apex_file
        mismatch_pub_unsigned = base / "mismatch-pub-unsigned.apex"
        with zipfile.ZipFile(good_audio, "r") as zf:
            good_payload_bytes = zf.read("apex_payload.img")
        stock_pubkey = base / "stock-pubkey"
        subprocess.run([str(ot_dir / "bin/avbtool"), "extract_public_key", "--key", str(stock_keys["payload_key"]), "--output", str(stock_pubkey)], check=True)
        ifma.repack_apex_zip(good_audio, mismatch_pub_unsigned, good_payload_bytes, stock_pubkey.read_bytes())
        mismatch_pub_signed = base / "mismatch-pub-signed.apex"
        ifma.sign_apex_container(
            mismatch_pub_unsigned,
            bkeys1["container_key"],
            bkeys1["container_cert_der"],
            mismatch_pub_signed,
            min_sdk=33,
            max_sdk=37,
        )
        try:
            ifma.verify_apex_file(
                mismatch_pub_signed,
                "apex/com.android.hardware.audio.desktop.apex",
                base / "v-mismatch-pub",
                ot_dir,
                expected_cert_der=Path(bkeys1["container_cert_der"]).read_bytes(),
                manifest_path=syn_manifest,
            )
            pub_err = ""
        except SystemExit as exc:
            pub_err = str(exc)
        check("verify_apex_file refuses an APEX whose embedded apex_pubkey does not verify apex_payload.img",
              "AVB public key mismatch" in pub_err)

        # 4e. Unpatched surviving vendor binary is refused by verify_rebuilt_vendor_tree
        vtree_unpatched = base / "vtree-unpatched"
        shutil.copytree(vtree_new, vtree_unpatched)
        (vtree_unpatched / "lib64/libzmij.dylib.so").write_bytes(original_elf)
        try:
            ifma.verify_rebuilt_vendor_tree(
                p, vtree_unpatched, work_tmp, otatools=ot_dir, keys_dir=build_keys_dir, manifest_path=syn_manifest
            )
            unp_err = ""
        except SystemExit as exc:
            unp_err = str(exc)
        check("verify_rebuilt_vendor_tree refuses an unpatched surviving vendor binary",
              "expected patched" in unp_err)

        # 4f. Wrong system_a EROFS UUID is refused by rebuild_system_partition
        wrong_uuid_erofs = base / "wrong-uuid.erofs"
        _make_erofs_image(ot_dir, stree_src, smeta, wrong_uuid_erofs, "11111111-2222-3333-4444-555555555555", build_time=1726000500, pcluster=32768, allow_caps=True)
        wrong_uuid_sys = base / "wrong-uuid.img"
        shutil.copyfile(wrong_uuid_erofs, wrong_uuid_sys)
        subprocess.run([
            str(ot_dir / "bin/avbtool"), "add_hashtree_footer",
            "--image", str(wrong_uuid_sys),
            "--partition_name", "system",
            "--partition_size", str(sys_part_size),
            "--hash_algorithm", "sha256",
            "--salt", ifma.SYSTEM_SALT,
        ], check=True)
        wrong_disk = base / "wrong-disk.img"
        _wrap_gpt_super_with_system(wrong_disk, wrong_uuid_sys.read_bytes())
        (base / "wrong-tmp").mkdir(parents=True, exist_ok=True)
        try:
            ifma.rebuild_system_partition(
                p, wrong_disk, base / "wrong-tmp", otatools=ot_dir, keys_dir=build_keys_dir, manifest_path=syn_manifest
            )
            uuid_err = ""
        except SystemExit as exc:
            uuid_err = str(exc)
        check("rebuild_system_partition refuses a system_a image with an unexpected EROFS UUID",
              "unexpected system_a uuid" in uuid_err)

        # 5. vendor.sh vbmeta + splicing slice (PATCH_IFMA=false vs PATCH_IFMA=true + red-when-disabled mutation)
        vendor_sh = (ROOT / "builder/vendor.sh").read_text(encoding="utf-8")
        vb_start = vendor_sh.index('echo "re-sign vbmeta"')
        vb_end = vendor_sh.index('# Check the final vendor readback', vb_start)
        vb_slice = vendor_sh[vb_start:vb_end]

        def make_bootconfig_blob(archive_bytes):
            bc_text = (
                "androidboot.vbmeta.digest=old\n"
                "androidboot.vbmeta.size=0\n"
                "androidboot.boot_part_uuid=old-uuid\n"
                "androidboot.hypervisor.protected_vm.supported=0\n"
                "androidboot.vendor.apex.com.android.hardware.keymint=com.android.hardware.keymint.rust_nonsecure\n"
                "androidboot.vendor.apex.com.android.hardware.gatekeeper=com.android.hardware.gatekeeper.nonsecure\n"
            ).encode("utf-8")
            footer = struct.pack("<II", len(bc_text), sum(bc_text) & 0xFFFFFFFF) + b"#BOOTCONFIG\n"
            return archive_bytes + bc_text + footer

        def run_vbmeta_slice(script_slice, patch_ifma_flag):
            wdir = base / f"vb-run-{patch_ifma_flag}-{abs(hash(script_slice)) % 100000}"
            if wdir.exists():
                shutil.rmtree(wdir)
            tmp_d = wdir / "tmp"
            logs_d = wdir / "logs"
            tmp_d.mkdir(parents=True)
            logs_d.mkdir(parents=True)
            ven_img = tmp_d / "vendor.img"
            shutil.copyfile(sys_erofs_stock, ven_img)
            subprocess.run([
                str(ot_dir / "bin/avbtool"), "add_hashtree_footer",
                "--image", str(ven_img),
                "--partition_name", "vendor",
                "--partition_size", str(sys_part_size),
                "--hash_algorithm", "sha256",
                "--salt", "deadbeef",
            ], check=True)
            sys_rebuilt_img = tmp_d / "system.img"
            shutil.copyfile(work_tmp / "system.img", sys_rebuilt_img)
            ext_img = tmp_d / "system_ext.img"
            shutil.copyfile(sys_erofs_stock, ext_img)
            subprocess.run([
                str(ot_dir / "bin/avbtool"), "add_hashtree_footer",
                "--image", str(ext_img), "--partition_name", "system_ext",
                "--partition_size", str(sys_part_size), "--hash_algorithm", "sha256",
                "--salt", ifma.SYSTEM_EXT_SALT,
            ], check=True)
            old_meta = json.dumps({"descriptors": {"vendor": {"image_sha256": "old_ven"}, "system": {"image_sha256": "old_sys"}, "system_ext": {"image_sha256": "old_ext"}}}).encode("utf-8")
            vb_hdr = bytearray(256)
            vb_hdr[0:4] = b"AVB0"
            struct.pack_into(">QQ", vb_hdr, 12, 0, len(old_meta))
            old_vb_bytes = bytes(vb_hdr) + old_meta
            stage_img = wdir / "stage-vendor.img"
            v_off = 4096
            vb_off = 4096 + sys_part_size
            s_off = vb_off + 65536
            sx_off = s_off + sys_part_size
            raw_stage = bytearray(sx_off + sys_part_size + 4096)
            raw_stage[vb_off : vb_off + len(old_vb_bytes)] = old_vb_bytes
            raw_stage[s_off : s_off + sys_part_size] = b"S" * sys_part_size
            raw_stage[sx_off : sx_off + sys_part_size] = b"X" * sys_part_size
            stage_img.write_bytes(bytes(raw_stage))
            (tmp_d / "offsets.env").write_text(
                f"VENDOR_OFF={v_off}\nVENDOR_SIZE={sys_part_size}\n"
                f"VBMETA_OFF={vb_off}\nBOOT_UUID=11111111-2222-3333-4444-555555555555\n"
                f"SYSTEM_OFF={s_off}\nSYSTEM_SIZE={sys_part_size}\n"
                f"SYSTEM_EXT_OFF={sx_off}\nSYSTEM_EXT_SIZE={sys_part_size}\n",
                encoding="utf-8",
            )
            standalone_in = wdir / "standalone-initrd.img"
            standalone_in.write_bytes(make_bootconfig_blob(b"cpio-archive-bytes"))
            runner = (
                f"set -euo pipefail\n"
                f"OTATOOLS={ot_dir}\n"
                f"ot() {{ LD_LIBRARY_PATH=\"$OTATOOLS/lib64\" \"$OTATOOLS/bin/$@\"; }}\n"
                f"mcopy() {{ cp \"{standalone_in}\" \"$4\"; }}\n"
                f"IMG={stage_img}\nTMP={tmp_d}\nWORK={wdir}\n"
                f"VBMETA_OFF={vb_off}\nESP_OFF=0\n"
                f"KEY={bkeys1['payload_key']}\nSTANDALONE={standalone_in}\n"
                f"PATCH_IFMA={patch_ifma_flag}\nPATCH_MODEL=false\n"
                f"VBMETA_ROLLBACK_INDEX=1339\n"  # 16452207 fixture's stock rollback index
                + script_slice
            )
            subprocess.run(["bash", "-c", runner], check=True, capture_output=True, text=True)
            vb_new = (tmp_d / "vbmeta.new.img").read_bytes()
            auth, aux = struct.unpack_from(">QQ", vb_new, 12)
            vb_dict = json.loads(vb_new[256 + auth : 256 + auth + aux].decode("utf-8"))
            final_stage = stage_img.read_bytes()
            vb_dict["_test_ext_bytes"] = final_stage[sx_off : sx_off + sys_part_size]
            vb_dict["_test_ext_expected"] = ext_img.read_bytes()
            return vb_dict, final_stage[s_off : s_off + sys_part_size], final_stage[v_off : v_off + sys_part_size], final_stage, (tmp_d / "initrd.new.img").read_bytes()

        vb_off_dict, sys_slice_off, ven_slice_off, image_off, initrd_off = run_vbmeta_slice(vb_slice, "false")
        check("switch off: vendor.sh vbmeta slice updates only vendor in vbmeta_a and leaves system_a bytes untouched in stage-vendor.img",
              vb_off_dict["descriptors"]["vendor"]["image_sha256"] != "old_ven"
              and vb_off_dict["descriptors"]["system"]["image_sha256"] == "old_sys"
              and sys_slice_off == b"S" * sys_part_size
              and vb_off_dict["descriptors"]["system_ext"]["image_sha256"] == "old_ext"
              and vb_off_dict["_test_ext_bytes"] == b"X" * sys_part_size)

        vb_on_dict, sys_slice_on, ven_slice_on, _, _ = run_vbmeta_slice(vb_slice, "true")
        check("vbmeta re-sign keeps build 16452207's rollback index with IFMA both off and on",
              vb_off_dict["rollback_index"] == 1339 and vb_on_dict["rollback_index"] == 1339)
        check("switch on: vendor.sh vbmeta slice updates vendor/system/system_ext descriptors and splices both system images into stage-vendor.img",
              vb_on_dict["descriptors"]["vendor"]["image_sha256"] != "old_ven"
              and vb_on_dict["descriptors"]["system"]["image_sha256"] != "old_sys"
              and sys_slice_on == (work_tmp / "system.img").read_bytes()
              and ven_slice_on == ven_slice_off
              and vb_on_dict["descriptors"]["system_ext"]["image_sha256"] != "old_ext"
              and vb_on_dict["_test_ext_bytes"] == vb_on_dict["_test_ext_expected"])

        # Compare the actual old/new config writers and splice shell blocks on identical inputs.
        # This is a synthetic vendor-path regression, not a complete real image A/B build.
        old_vendor = subprocess.run(["git", "show", "441223c:builder/vendor.sh"], cwd=ROOT,
                                    text=True, capture_output=True)
        old_rebuild = subprocess.run(["git", "show", "441223c:builder/lib/rebuild.py"], cwd=ROOT,
                                     text=True, capture_output=True)
        if old_vendor.returncode or old_rebuild.returncode:
            skip("pre-IFMA 441223c vendor parity", "git history for baseline 441223c unavailable")
        else:
            old_text = old_vendor.stdout
            old_slice = old_text[old_text.index('echo "re-sign vbmeta"'):old_text.index('# Check the final vendor readback')]
            _, _, _, old_image, old_initrd = run_vbmeta_slice(old_slice, "false")
            check("switch off vs 441223c: full synthetic spliced image and initrd are byte-identical",
                  old_image == image_off and old_initrd == initrd_off)
            baseline_module = base / "baseline-rebuild.py"
            baseline_module.write_text(old_rebuild.stdout)
            baseline_spec = importlib.util.spec_from_file_location("baseline_rebuild", baseline_module)
            baseline = importlib.util.module_from_spec(baseline_spec)
            baseline_spec.loader.exec_module(baseline)
            vendor_meta = {}
            for entry in [vtree_off, *sorted(vtree_off.rglob("*"))]:
                rel = "" if entry == vtree_off else entry.relative_to(vtree_off).as_posix()
                vendor_meta[rel] = {"mode": entry.lstat().st_mode & 0o7777, "uid": 1000,
                                    "gid": 2000, "selinux": "u:object_r:vendor_file:s0", "caps": None}
            for name, module in (("before", baseline), ("after", rebuild)):
                module.write_configs("/vendor", vtree_off, vendor_meta, base / (name + ".fc"), base / (name + ".fs"))
            check("switch off vs 441223c: vendor file-contexts and fs-config are byte-identical",
                  (base / "before.fc").read_bytes() == (base / "after.fc").read_bytes()
                  and (base / "before.fs").read_bytes() == (base / "after.fs").read_bytes())

        # Red-when-disabled mutations
        mut_vb_slice = vb_slice.replace('if [ "$PATCH_IFMA" = true ]; then', "if false; then", 1).replace('if patch_ifma == "true":', "if False:")
        vb_mut_dict, sys_mut_slice, _, _, _ = run_vbmeta_slice(mut_vb_slice, "true")
        check("mutation shown red: skipping PATCH_IFMA branch in vendor.sh leaves system descriptor un-updated in vbmeta_a",
              vb_mut_dict["descriptors"]["system"]["image_sha256"] == "old_sys"
              and sys_mut_slice != (work_tmp / "system.img").read_bytes())

        ext_step_start = vb_slice.index('    cp "$TMP/vbmeta.new.img" "$TMP/vbmeta.step2.img"')
        ext_step_end = vb_slice.index('\nfi', ext_step_start)
        ext_only_mutation = vb_slice[:ext_step_start] + vb_slice[ext_step_end:]
        ext_mut_dict, _, _, _, _ = run_vbmeta_slice(ext_only_mutation, "true")
        check("mutation shown red: dropping only system_ext descriptor update leaves its old descriptor while system updates",
              ext_mut_dict["descriptors"]["system_ext"]["image_sha256"] == "old_ext"
              and ext_mut_dict["descriptors"]["system"]["image_sha256"] != "old_sys")

        ifma_src = (ROOT / "builder/lib/ifma.py").read_text(encoding="utf-8")

        def load_mutated_ifma(old_s, new_s):
            assert ifma_src.count(old_s) == 1, f"anchor not unique: {old_s!r}"
            mut_text = ifma_src.replace(
                "IFMA_DIR = HERE.parent / \"ifma\"",
                f"IFMA_DIR = Path({str(IFMA_DIR)!r})",
                1,
            ).replace(old_s, new_s, 1)
            mpath = base / f"mut_ifma_{abs(hash(old_s)) % 100000}.py"
            mpath.write_text(mut_text, encoding="utf-8")
            mspec = importlib.util.spec_from_file_location(mpath.stem, mpath)
            mmod = importlib.util.module_from_spec(mspec)
            mspec.loader.exec_module(mmod)
            return mmod

        # Mutation 1: skip verify_apex_container in verify_apex_file -> tampered outer container accepted
        mut1 = load_mutated_ifma(
            '        container_info = verify_apex_container(\n'
            '            apex_file,\n'
            '            expected_cert_der=expected_cert_der,\n'
            '            stock_apex_path=stock_apex_path,\n'
            '        )\n',
            "        container_info = {}\n",
        )
        mut1_accepted = False
        try:
            mut1.verify_apex_file(
                good_audio,
                "apex/com.android.hardware.audio.desktop.apex",
                base / "mut1-work",
                ot_dir,
                expected_cert_der=Path(stock_keys["container_cert_der"]).read_bytes(),
                manifest_path=syn_manifest,
            )
            mut1_accepted = True
        except SystemExit:
            mut1_accepted = False
        check("mutation shown red: skipping verify_apex_container accepts wrong outer container cert", mut1_accepted)

        # Mutation 2: skip avbtool verify_image in verify_apex_file -> mismatched apex_pubkey accepted
        mut2 = load_mutated_ifma(
            '    _run_ot(\n        otatools,\n        "avbtool",\n        "verify_image",\n        "--image", payload_img,\n        "--key", pubkey_path,\n    )\n',
            "    pass\n",
        )
        mut2_accepted = False
        try:
            mut2.verify_apex_file(
                mismatch_pub_signed,
                "apex/com.android.hardware.audio.desktop.apex",
                base / "mut2-work",
                ot_dir,
                expected_cert_der=Path(bkeys1["container_cert_der"]).read_bytes(),
                manifest_path=syn_manifest,
            )
            mut2_accepted = True
        except SystemExit:
            mut2_accepted = False
        check("mutation shown red: skipping avbtool verify_image accepts mismatched embedded apex_pubkey", mut2_accepted)

        # Mutation 3: skip res['state'] != 'patched' check in verify_rebuilt_vendor_tree -> unpatched surviving ELF accepted
        mut3 = load_mutated_ifma(
            '        if res["state"] != "patched":\n            raise SystemExit(f"rebuilt vendor {rel} is {res[\'state\']}, expected patched")\n',
            "        pass\n",
        )
        mut3_accepted = False
        try:
            mut3.verify_rebuilt_vendor_tree(
                p, vtree_unpatched, work_tmp, otatools=ot_dir, keys_dir=build_keys_dir, manifest_path=syn_manifest
            )
            mut3_accepted = True
        except SystemExit:
            mut3_accepted = False
        check("residual scanner still refuses an unpatched vendor ELF when the direct patched-state check is removed",
              not mut3_accepted)

    print("IFMA counts: " + ", ".join(f"{name}={count}" for name, count in counts.items()))


if __name__ == "__main__":
    p_count = 0
    f_count = 0

    def _cli_check(label, cond):
        global p_count, f_count
        if cond:
            print(f"PASS {label}")
            p_count += 1
        else:
            print(f"FAIL {label}")
            f_count += 1

    run_ifma_checks(_cli_check)
    print(f"patch-ifma tests: {p_count} passed, {f_count} failed")
    sys.exit(1 if f_count else 0)
