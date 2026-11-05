#!/usr/bin/env python3
"""Build-time IFMA patch integration for vendor_a, system_a/system_ext_a, and affected APEXes.

When profile `[cpu_compat] patch_ifma = true` is set:
  - Patches the 3 plain vendor_a ELFs (including vendor/bin/ectool before REMOVE unlinks it).
  - Unpacks, patches, rebuilds (EROFS), and re-signs the 2 vendor_a APEXes
    (com.android.hardware.audio.desktop.apex, com.android.hardware.fingerprint.crosfp.apex).
  - Extracts system_a from super, patches the 6 plain system_a ELFs plus the 5 ELFs inside
    system/apex/com.google.android.virt.apex, rebuilds system_a (EROFS) with preserved
    uid/gid/mode/SELinux/capabilities/UUID/pcluster, adds the system AVB hashtree footer,
    and updates vbmeta_a's system descriptor alongside vendor.

When `patch_ifma` is false, every helper is a no-op so the output image is byte-identical
to the unpatched builder path.
"""

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
import rebuild
from container_alignment import check_zip_layout

HERE = Path(__file__).resolve().parent
IFMA_DIR = HERE.parent / "ifma"
MANIFEST_PATH = IFMA_DIR / "manifest.json"

_SPEC = importlib.util.spec_from_file_location("patch_ifma", IFMA_DIR / "patch-ifma.py")
PATCH_IFMA = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PATCH_IFMA)
_SCAN_SPEC = importlib.util.spec_from_file_location("scan_ifma", IFMA_DIR / "scan-ifma.py")
SCAN_IFMA = importlib.util.module_from_spec(_SCAN_SPEC)
_SCAN_SPEC.loader.exec_module(SCAN_IFMA)

SYSTEM_UUID = "8fe02434-e0ad-5c53-9470-75a36a13aef0"
SYSTEM_SALT = "b14f17c0007cfc878a5cdac13f0514ac3b7d22996d4db3d254082387c6705124"
SYSTEM_FINGERPRINT = "Google/lapis/lapis:17/CL3B.260622.271.R1/16452207:user/release-keys"
SYSTEM_SECURITY_PATCH = "2026-09-01"
SYSTEM_OS_VERSION = "17"
SYSTEM_EXT_UUID = "5600e670-83c3-5b38-9984-89debd2d7f0b"
SYSTEM_EXT_SALT = SYSTEM_SALT
SYSTEM_EXT_FINGERPRINT = "ASUS/lapis/lapis:17/CL3B.260622.271.R1/16452207:user/release-keys"
SYSTEM_EXT_SECURITY_PATCH = "2026-09-01"
SYSTEM_EXT_OS_VERSION = "17"
SYSTEM_EXT_HASHTREE = {
    "dm_verity_version": 1,
    "data_block_size": 4096,
    "hash_block_size": 4096,
    "fec_num_roots": 2,
    "hash_algorithm": "sha256",
    "flags": 0,
}

VENDOR_SURVIVING_CHANGED = (
    "apex/com.android.hardware.audio.desktop.apex",
    "apex/com.android.hardware.fingerprint.crosfp.apex",
    "bin/hw/android.hardware.usb-service.desktop",
    "lib64/libzmij.dylib.so",
)
SYSTEM_EXPECTED_CHANGED = (
    "system/apex/com.google.android.virt.apex",
    "system/bin/aisealhostservice",
    "system/bin/ectool",
    "system/bin/prefetch",
    "system/lib64/libinput.so",
    "system/lib64/libinputflinger.so",
    "system/lib64/libzmij.dylib.so",
)
SYSTEM_EXT_EXPECTED_CHANGED = ("bin/trusty_security_vm_launcher",)

APEX_SPECS = {
    "system/apex/com.google.android.virt.apex": {
        "partition": "system_a",
        "apex_key": "com.android.virt",
        "salt": "2e5cb21d9824edaff1c06394a94446ee48b871c32868801bd4453234f4c38cf2",
        "allow_caps": True,
    },
    "apex/com.android.hardware.audio.desktop.apex": {
        "partition": "vendor_a",
        "apex_key": "com.android.hardware",
        "salt": "e389e98363f0be67dd5dae28da111ee0e464880736a2b3ee3270a0f4d43a1d9c",
        "allow_caps": False,
    },
    "apex/com.android.hardware.fingerprint.crosfp.apex": {
        "partition": "vendor_a",
        "apex_key": "com.android.hardware",
        "salt": "296b8e1881f1668f6fda928a06ade40a30516d3ac848dedf42ccbe5a60f0b3e7",
        "allow_caps": False,
    },
}

# Build selection happens before default manifest arguments are bound below.
# Keep the original build's reviewed constants; new builds require their own pins.
VENDOR_UUID = "7aead136-f6d4-508e-9bf2-2517134fe885"
VENDOR_FINGERPRINT = "ASUS/lapis/lapis:17/CL3B.260622.271.R1/16452207:user/release-keys"
VENDOR_SECURITY_PATCH = "2026-09-01"
VBMETA_ROLLBACK_INDEX = 1339
LAPIS_BUILD = os.environ.get("GBOS_LAPIS_BUILD", "16452207")
_BUILD_FIELDS = (
    "SYSTEM_UUID", "SYSTEM_SALT", "SYSTEM_FINGERPRINT", "SYSTEM_SECURITY_PATCH",
    "SYSTEM_OS_VERSION", "SYSTEM_EXT_UUID", "SYSTEM_EXT_SALT",
    "SYSTEM_EXT_FINGERPRINT", "SYSTEM_EXT_SECURITY_PATCH", "SYSTEM_EXT_OS_VERSION",
    "VENDOR_UUID", "VENDOR_FINGERPRINT", "VENDOR_SECURITY_PATCH", "VBMETA_ROLLBACK_INDEX",
)
if LAPIS_BUILD != "16452207":
    if LAPIS_BUILD != "16658739":
        raise SystemExit(f"no IFMA/partition pins for Google build {LAPIS_BUILD!r}")
    _pin_path = IFMA_DIR / "builds" / f"{LAPIS_BUILD}.json"
    try:
        _pins = json.loads(_pin_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot load Google build pins {_pin_path}: {exc}") from exc
    if set(_pins) != set(_BUILD_FIELDS) | {"build", "apex_salts", "manifest_sha256"} or _pins["build"] != LAPIS_BUILD:
        raise SystemExit("invalid Google build pin fields or build identity")
    for _field in _BUILD_FIELDS:
        _value = _pins[_field]
        if _field == "VBMETA_ROLLBACK_INDEX":
            if type(_value) is not int or _value < 1339:
                raise SystemExit("invalid or regressing AVB rollback index pin")
        elif not isinstance(_value, str) or not _value:
            raise SystemExit(f"invalid Google build pin {_field}")
    (SYSTEM_UUID, SYSTEM_SALT, SYSTEM_FINGERPRINT, SYSTEM_SECURITY_PATCH,
     SYSTEM_OS_VERSION, SYSTEM_EXT_UUID, SYSTEM_EXT_SALT,
     SYSTEM_EXT_FINGERPRINT, SYSTEM_EXT_SECURITY_PATCH, SYSTEM_EXT_OS_VERSION,
     VENDOR_UUID, VENDOR_FINGERPRINT, VENDOR_SECURITY_PATCH,
     VBMETA_ROLLBACK_INDEX) = (_pins[_field] for _field in _BUILD_FIELDS)
    if set(_pins["apex_salts"]) != set(APEX_SPECS):
        raise SystemExit("APEX identity pin set differs from the reviewed target set")
    for _apex, _salt in _pins["apex_salts"].items():
        if not isinstance(_salt, str) or len(_salt) != 64:
            raise SystemExit(f"invalid APEX salt pin for {_apex}")
        try:
            bytes.fromhex(_salt)
        except ValueError as exc:
            raise SystemExit(f"invalid APEX salt pin for {_apex}") from exc
        APEX_SPECS[_apex]["salt"] = _salt
    # Target ELFs are unchanged; nested guest container identities are build-specific.
    MANIFEST_PATH = IFMA_DIR / "builds" / f"{LAPIS_BUILD}.manifest.json"
    if hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest() != _pins["manifest_sha256"]:
        raise SystemExit(f"IFMA manifest differs from the reviewed Google build {LAPIS_BUILD} pin")

APK_SIG_BLOCK_MAGIC = b"APK Sig Block 42"
APK_SIG_SCHEME_V2_ID = 0x7109871A
APK_SIG_SCHEME_V3_ID = 0xF05368C0
VERITY_PADDING_BLOCK_ID = 0x42726577
SOURCE_STAMP_V2_BLOCK_ID = 0x6DFF800D
STRIPPING_PROTECTION_ATTR_ID = 0xBEEFF00D
SIG_RSA_PKCS1_V1_5_SHA256 = 0x0103
SIG_RSA_PKCS1_V1_5_SHA512 = 0x0104
ZIPALIGN_EXTRA_HEADER_ID = 0xD935
APEX_AVB_FOOTER_ARGS = (
    "--hash_algorithm", "sha256",
    "--block_size", "4096",
    "--do_not_generate_fec",
)
APEX_REQUIRED_SIGNING_BLOCK_IDS = frozenset((
    APK_SIG_SCHEME_V2_ID,
    APK_SIG_SCHEME_V3_ID,
    VERITY_PADDING_BLOCK_ID,
))
APEX_ALLOWED_SIGNING_BLOCK_IDS = APEX_REQUIRED_SIGNING_BLOCK_IDS


PASTEL_VENDOR_CHAIN = [{"kind": "tree_file", "path": "lib64/hw/vulkan.pastel.so"}]


def _vendor_has_pastel(prof):
    if not isinstance(prof, dict):
        return False
    display = prof.get("display")
    model = prof.get("model")
    return (
        (isinstance(display, dict) and display.get("stack") == "gop-swiftshader")
        or (isinstance(model, dict) and model.get("id") == "amd-hx370")
    )


def _is_pastel_vendor_pin(row):
    return (
        isinstance(row, dict)
        and row.get("partition") == "vendor_a"
        and row.get("container_chain") == PASTEL_VENDOR_CHAIN
    )


def _manifest_scan_policy(manifest_path, *, include_pastel=False):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    residuals = manifest.get("residuals", [])
    if not isinstance(residuals, list):
        raise SystemExit("IFMA manifest residuals must be a list")
    known_non_instructions = manifest.get("known_non_instructions", [])
    if not isinstance(known_non_instructions, list):
        raise SystemExit("IFMA manifest known_non_instructions must be a list")
    if not include_pastel:
        known_non_instructions = [
            row for row in known_non_instructions if not _is_pastel_vendor_pin(row)
        ]
    unsupported_allowlist = manifest.get("unsupported_allowlist", [])
    if not isinstance(unsupported_allowlist, list):
        raise SystemExit("IFMA manifest unsupported_allowlist must be a list")
    return residuals, known_non_instructions, unsupported_allowlist


def _partition_provenance(partition, chain=()):
    return {"partition": partition, "container_chain": [dict(node) for node in chain]}


def _apex_payload_provenance(apex_rel, partition):
    return _partition_provenance(partition, (
        {"kind": "tree_file", "path": apex_rel},
        {"kind": "zip_member", "path": "apex_payload.img"},
    ))


def scan_rebuilt_tree(
    tree, label, otatools=None, scratch_dir=None, *, manifest_path=MANIFEST_PATH,
    provenance=None, root_kind="tree_file", include_pastel=False,
):
    """Run the mandatory residual-IFMA scanner and print its coverage report."""
    try:
        residuals, known_non_instructions, unsupported_allowlist = _manifest_scan_policy(
            manifest_path, include_pastel=include_pastel,
        )
        report = SCAN_IFMA.scan_tree(
            tree,
            label=label,
            otatools=otatools,
            scratch_dir=scratch_dir,
            residuals=residuals,
            known_non_instructions=known_non_instructions,
            unsupported_allowlist=unsupported_allowlist,
            provenance=provenance,
            root_kind=root_kind,
        )
    except (OSError, SCAN_IFMA.ScanError) as exc:
        raise SystemExit(f"{label}: residual IFMA scan failed: {exc}") from exc
    print("ifma residual scan: " + json.dumps(report, sort_keys=True, separators=(",", ":")))
    for residual in report["accepted_residuals"]:
        print("ifma residual accepted: " + json.dumps(residual, sort_keys=True, separators=(",", ":")))
    return report


def enabled(prof):
    """True when the profile enables [cpu_compat] patch_ifma."""
    return bool(prof.get("cpu_compat", {}).get("patch_ifma", False))


def changed_vendor_paths(prof, expect_changed):
    """Return expect_changed with the 4 surviving vendor IFMA paths when patch_ifma is enabled."""
    if not enabled(prof):
        return expect_changed
    return sorted(set(list(expect_changed) + list(VENDOR_SURVIVING_CHANGED)))


def _ot_env(otatools):
    env = os.environ.copy()
    lib64 = str(Path(otatools) / "lib64")
    env["LD_LIBRARY_PATH"] = lib64 + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    env["PATH"] = str(Path(otatools) / "bin") + ":" + env.get("PATH", "")
    return env


def _run_ot(otatools, tool, *args):
    cmd = [str(Path(otatools) / "bin" / tool), *[str(a) for a in args]]
    res = subprocess.run(cmd, env=_ot_env(otatools), capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"{tool} failed ({res.returncode}):\n{res.stdout}\n{res.stderr}")
    return res.stdout


def _default_keys_dir(_tmp=None):
    """Return the persistent APEX key directory selected by the build environment."""
    if os.environ.get("GBOS_APEX_KEYS_DIR"):
        return Path(os.environ["GBOS_APEX_KEYS_DIR"])
    avb_key = os.environ.get("GBOS_AVB_KEY")
    if not avb_key:
        raise SystemExit("GBOS_AVB_KEY is required to select the persistent APEX key directory")
    return Path(avb_key).parent / "apex-keys"


def _path_within(path, parent):
    path = Path(path).expanduser().resolve(strict=False)
    parent = Path(parent).expanduser().resolve(strict=False)
    return path == parent or parent in path.parents


def _openssl_output(args, *, input_bytes=None, what):
    try:
        res = subprocess.run(
            ["openssl", *args], input=input_bytes, capture_output=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        detail = detail.decode("utf-8", "replace").strip()
        raise SystemExit(f"invalid APEX {what}" + (f": {detail}" if detail else "")) from exc
    return res.stdout


def _private_rsa4096_spki(key_path, label):
    _openssl_output(["rsa", "-in", str(key_path), "-check", "-noout"], what=label)
    details = _openssl_output(["rsa", "-in", str(key_path), "-text", "-noout"], what=label)
    if b"Private-Key: (4096 bit" not in details:
        raise SystemExit(f"invalid APEX {label}: expected RSA-4096 private key")
    return _openssl_output(
        ["pkey", "-in", str(key_path), "-pubout", "-outform", "DER"], what=label
    )


def _certificate_spki(cert_der, label="container certificate"):
    with tempfile.TemporaryDirectory(prefix="gbos-apex-cert-") as td:
        cert_path = Path(td) / "cert.der"
        cert_path.write_bytes(cert_der)
        pem = _openssl_output(
            ["x509", "-inform", "DER", "-in", str(cert_path), "-pubkey", "-noout"],
            what=label,
        )
        spki = _openssl_output(
            ["pkey", "-pubin", "-outform", "DER"], input_bytes=pem, what=f"{label} public key"
        )
        details = _openssl_output(
            ["pkey", "-pubin", "-text", "-noout"], input_bytes=pem, what=f"{label} public key"
        )
    if b"Public-Key: (4096 bit" not in details:
        raise SystemExit(f"invalid APEX {label}: expected RSA-4096 public key")
    return spki


def apex_key_fingerprints(keys):
    """Return public-only SHA-256 identities for a validated APEX keyset."""
    payload_spki = _private_rsa4096_spki(Path(keys["payload_key"]), "payload key")
    cert_der = Path(keys["container_cert_der"]).read_bytes()
    return {
        "payload_public_sha256": hashlib.sha256(payload_spki).hexdigest(),
        "container_cert_sha256": hashlib.sha256(cert_der).hexdigest(),
    }


def _validate_apex_keyset(keys_dir, keys):
    st = keys_dir.lstat()
    if keys_dir.is_symlink() or not keys_dir.is_dir():
        raise SystemExit(f"APEX keys directory is not a regular directory: {keys_dir}")
    if (st.st_mode & 0o777) != 0o700:
        raise SystemExit(f"APEX keys directory must have mode 0700: {keys_dir}")
    for label, path in keys.items():
        try:
            pst = path.lstat()
        except FileNotFoundError as exc:
            raise SystemExit(f"partial APEX keyset: missing {path.name}") from exc
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"APEX keyset entry must be a regular non-symlink file: {path}")
        if label in ("payload_key", "container_key") and (pst.st_mode & 0o777) != 0o600:
            raise SystemExit(f"APEX private key must have mode 0600: {path}")

    payload_spki = _private_rsa4096_spki(keys["payload_key"], "payload key")
    del payload_spki
    container_spki = _private_rsa4096_spki(keys["container_key"], "container key")
    cert_spki = _certificate_spki(keys["container_cert_der"].read_bytes())
    if container_spki != cert_spki:
        raise SystemExit("invalid APEX keyset: container key and certificate do not match")


def ensure_apex_keys(keys_dir=None, *, create=True):
    """Create once or strictly validate the persistent RSA-4096 APEX keyset."""
    keys_dir = Path(keys_dir) if keys_dir is not None else _default_keys_dir()
    gbos_out = os.environ.get("GBOS_OUT")
    if gbos_out and _path_within(keys_dir, gbos_out):
        raise SystemExit(f"APEX keys directory must be outside GBOS_OUT: {keys_dir}")

    names = {
        "payload_key": "apex_payload.pem",
        "container_key": "apex_container.pem",
        "container_cert_der": "apex_container.der",
    }
    keys = {name: keys_dir / filename for name, filename in names.items()}
    exists = [path.exists() or path.is_symlink() for path in keys.values()]
    if any(exists) and not all(exists):
        missing = ", ".join(path.name for path, present in zip(keys.values(), exists) if not present)
        raise SystemExit(f"partial APEX keyset in {keys_dir}: missing {missing}; refusing key rotation")

    if not all(exists):
        if not create:
            raise SystemExit(f"APEX keyset does not exist in {keys_dir}; refusing creation during verification")
        if keys_dir.exists() or keys_dir.is_symlink():
            if keys_dir.is_symlink() or not keys_dir.is_dir():
                raise SystemExit(f"APEX keys directory is not a regular directory: {keys_dir}")
            if (keys_dir.stat().st_mode & 0o777) != 0o700:
                raise SystemExit(f"APEX keys directory must have mode 0700: {keys_dir}")
        else:
            try:
                keys_dir.mkdir(parents=True, mode=0o700)
                keys_dir.chmod(0o700)
            except OSError as exc:
                raise SystemExit(f"cannot create persistent APEX keys directory {keys_dir}: {exc}") from exc
        if not os.access(keys_dir, os.W_OK):
            raise SystemExit(f"persistent APEX keys directory is not writable: {keys_dir}")
        try:
            with tempfile.TemporaryDirectory(prefix=".new-apex-keys-", dir=keys_dir) as td:
                td = Path(td)
                new_payload = td / names["payload_key"]
                new_container = td / names["container_key"]
                new_cert = td / names["container_cert_der"]
                _openssl_output(["genrsa", "-out", str(new_payload), "4096"], what="payload key generation")
                _openssl_output(
                    [
                        "req", "-x509", "-newkey", "rsa:4096", "-nodes",
                        "-keyout", str(new_container), "-out", str(new_cert),
                        "-outform", "DER", "-days", "10000",
                        "-subj", "/CN=gbos.apex.container", "-batch",
                    ],
                    what="container key generation",
                )
                new_payload.chmod(0o600)
                new_container.chmod(0o600)
                new_payload.replace(keys["payload_key"])
                new_container.replace(keys["container_key"])
                new_cert.replace(keys["container_cert_der"])
        except OSError as exc:
            raise SystemExit(f"cannot create persistent APEX keyset in {keys_dir}: {exc}") from exc

    _validate_apex_keyset(keys_dir, keys)
    fingerprints = apex_key_fingerprints(keys)
    print(f"APEX payload public key sha256: {fingerprints['payload_public_sha256']}")
    print(f"APEX container certificate sha256: {fingerprints['container_cert_sha256']}")
    return keys


def ensure_apex_keys_and_fingerprints(keys_dir=None, *, create=True):
    """Ensure the keyset and return both paths and public cache/build identities."""
    keys = ensure_apex_keys(keys_dir, create=create)
    return keys, apex_key_fingerprints(keys)


def _lp_Named_extent(img_path, super_off, target_name):
    with Path(img_path).open("rb") as f:
        f.seek(super_off + 4096)
        geo = f.read(52)
        if geo[:4] != b"gDla":
            raise SystemExit("no LP geometry")
        f.seek(super_off + 12288)
        hdr = f.read(256)
        if hdr[:4] != b"0PLA":
            raise SystemExit("no LP header")
        hsize = struct.unpack_from("<I", hdr, 8)[0]
        tables_size = struct.unpack_from("<I", hdr, 44)[0]
        p_off, p_num, p_size = struct.unpack_from("<III", hdr, 80)
        e_off, e_num, e_size = struct.unpack_from("<III", hdr, 92)
        f.seek(super_off + 12288 + hsize)
        tables = f.read(tables_size)
    for i in range(p_num):
        e = tables[p_off + i * p_size : p_off + (i + 1) * p_size]
        name = e[:36].split(b"\x00", 1)[0].decode()
        _attr, first, num, _group = struct.unpack_from("<IIII", e, 36)
        if name != target_name:
            continue
        if num != 1 or first >= e_num:
            raise SystemExit(f"{target_name} extents {num}")
        nsec, ttype, tdata, _tsource = struct.unpack_from("<QIQI", tables, e_off + first * e_size)
        if ttype != 0:
            raise SystemExit(f"{target_name} extent is not linear")
        return tdata * 512, nsec * 512
    raise SystemExit(f"{target_name} missing in super")


def _gpt_partitions(img_path):
    with Path(img_path).open("rb") as f:
        f.seek(512)
        hdr = f.read(92)
        if hdr[:8] != b"EFI PART":
            raise SystemExit("not gpt")
        ents, nent, esz = struct.unpack_from("<QII", hdr, 72)
        f.seek(ents * 512)
        data = f.read(nent * esz)
    out = {}
    for i in range(nent):
        e = data[i * esz : (i + 1) * esz]
        if e[:16] == b"\x00" * 16:
            continue
        name = e[56:128].decode("utf-16le").rstrip("\x00")
        first, last = struct.unpack_from("<QQ", e, 32)
        out[name] = {
            "name": name,
            "start": first * 512,
            "size": (last - first + 1) * 512,
        }
    return out


def _avb_footer_original_size(footer, image_size, label):
    """Validate a terminal AVB 1.x footer and return its original image size."""
    if len(footer) != 64 or footer[:4] != b"AVBf":
        raise SystemExit(f"{label} has no terminal 64-byte AVB footer")
    major, _minor, orig_size, vbmeta_offset, vbmeta_size = struct.unpack_from(">IIQQQ", footer, 4)
    footer_offset = image_size - 64
    if major != 1:
        raise SystemExit(f"{label} has unsupported AVB footer major version {major}")
    if orig_size <= 0 or orig_size > footer_offset:
        raise SystemExit(f"{label} has invalid AVB original size {orig_size}")
    if (
        vbmeta_size <= 0
        or vbmeta_offset < orig_size
        or vbmeta_offset > footer_offset
        or vbmeta_size > footer_offset - vbmeta_offset
    ):
        raise SystemExit(
            f"{label} has invalid AVB vbmeta bounds offset={vbmeta_offset} size={vbmeta_size}"
        )
    return orig_size


def _strip_avb_footer(blob, label):
    if len(blob) < 64:
        raise SystemExit(f"{label} has no terminal 64-byte AVB footer")
    orig_size = _avb_footer_original_size(blob[-64:], len(blob), label)
    return blob[:orig_size], len(blob)


def _parse_partition_avb_info(info_out, label):
    partition_names = []
    salts = []
    props = {}
    hashtree = {}
    in_hashtree = False
    hashtree_fields = {
        "Version of dm-verity": ("dm_verity_version", False),
        "Data Block Size": ("data_block_size", True),
        "Hash Block Size": ("hash_block_size", True),
        "FEC num roots": ("fec_num_roots", False),
        "Hash Algorithm": ("hash_algorithm", False),
        "Flags": ("flags", False),
    }
    for raw_line in info_out.splitlines():
        line = raw_line.strip()
        if line == "Hashtree descriptor:":
            in_hashtree = True
        elif line.startswith("Prop:"):
            in_hashtree = False
        if in_hashtree and ":" in line:
            field, value = line.split(":", 1)
            if field in hashtree_fields:
                key, has_bytes_suffix = hashtree_fields[field]
                value = value.strip()
                if has_bytes_suffix:
                    if not value.endswith(" bytes"):
                        raise SystemExit(f"{label}: stock AVB field {field!r} has invalid value {value!r}")
                    value = value[:-len(" bytes")]
                if key in hashtree:
                    raise SystemExit(f"{label}: stock AVB descriptor repeats field {field!r}")
                try:
                    hashtree[key] = value.lower() if key == "hash_algorithm" else int(value, 10)
                except ValueError as exc:
                    raise SystemExit(
                        f"{label}: stock AVB field {field!r} has invalid value {value!r}"
                    ) from exc
        if line.startswith("Partition Name:"):
            partition_names.append(line.split(":", 1)[1].strip())
        elif line.startswith("Salt:"):
            salts.append(line.split(":", 1)[1].strip().lower())
        elif line.startswith("Prop:") and " -> '" in line and line.endswith("'"):
            key, value = line[len("Prop:"):].split(" -> '", 1)
            key = key.strip()
            if key in props:
                raise SystemExit(f"{label}: stock AVB descriptor repeats property {key!r}")
            props[key] = value[:-1]
    if len(partition_names) != 1 or len(salts) != 1:
        raise SystemExit(
            f"{label}: stock image must contain exactly one partition name and salt"
        )
    try:
        salt_bytes = bytes.fromhex(salts[0])
    except ValueError as exc:
        raise SystemExit(f"{label}: stock image has invalid AVB salt {salts[0]!r}") from exc
    if not salt_bytes:
        raise SystemExit(f"{label}: stock image has an empty AVB salt")
    if set(hashtree) != set(SYSTEM_EXT_HASHTREE):
        raise SystemExit(
            f"{label}: stock image has incomplete AVB hashtree parameters: {hashtree!r}"
        )
    return {
        "partition_name": partition_names[0],
        "salt": salts[0],
        "props": props,
        "hashtree": hashtree,
    }


def _assert_system_ext_stock_avb(otatools, partition_img):
    actual = _parse_partition_avb_info(
        _run_ot(otatools, "avbtool", "info_image", "--image", partition_img),
        "system_ext_a",
    )
    expected = {
        "partition_name": "system_ext",
        "salt": SYSTEM_EXT_SALT,
        "props": {
            "com.android.build.system_ext.os_version": SYSTEM_EXT_OS_VERSION,
            "com.android.build.system_ext.fingerprint": SYSTEM_EXT_FINGERPRINT,
            "com.android.build.system_ext.security_patch": SYSTEM_EXT_SECURITY_PATCH,
        },
        "hashtree": SYSTEM_EXT_HASHTREE,
    }
    if actual != expected:
        raise SystemExit(
            f"system_ext_a stock AVB identity drifted: expected={expected!r}, actual={actual!r}"
        )
    return actual


def _index_tree(root):
    root = Path(root)
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        real_dirs = []
        for name in dirnames:
            p = Path(dirpath) / name
            rel = name if rel_dir == "" else rel_dir + "/" + name
            if p.is_symlink():
                out[rel] = ("symlink", os.readlink(p))
            else:
                real_dirs.append(name)
                out[rel] = ("dir", None)
        dirnames[:] = real_dirs
        for name in filenames:
            p = Path(dirpath) / name
            rel = name if rel_dir == "" else rel_dir + "/" + name
            if p.is_symlink():
                out[rel] = ("symlink", os.readlink(p))
            elif p.is_file():
                digest = hashlib.sha256()
                with p.open("rb") as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                out[rel] = ("file", digest.hexdigest())
            else:
                raise SystemExit("special file " + rel)
    return out


def _erofs_meta_map(erofs_path):
    data, blksz, meta_off, xattr, root = rebuild.load_image(erofs_path)
    m, hard = rebuild.walk_image(
        str(erofs_path), root, data, blksz, meta_off, xattr, strict_metadata=True,
    )
    if hard:
        raise SystemExit(f"{erofs_path} has {hard} hardlinked entries; handle before rebuild")
    return m


def _assert_same_erofs_metadata(old_erofs, new_erofs, label):
    old_sb = rebuild.superblock_params(old_erofs, strict_metadata=True)
    new_sb = rebuild.superblock_params(new_erofs, strict_metadata=True)
    superblock_fields = (
        "uuid", "build_time", "build_time_nsec", "compat", "incompat", "volume_name", "pcluster_size",
    )
    drifted_superblock = [field for field in superblock_fields if old_sb[field] != new_sb[field]]
    if drifted_superblock:
        raise SystemExit(
            f"{label}: EROFS superblock metadata drifted on {drifted_superblock}: "
            f"old={old_sb!r}, new={new_sb!r}"
        )
    oldm = _erofs_meta_map(old_erofs)
    newm = _erofs_meta_map(new_erofs)
    if set(oldm) != set(newm):
        gone = sorted(set(oldm) - set(newm))[:10]
        extra = sorted(set(newm) - set(oldm))[:10]
        raise SystemExit(f"{label}: EROFS inode paths drifted (gone={gone}, extra={extra})")
    bad = []
    fields = (
        "file_type", "mode", "uid", "gid", "nlink",
        "mtime_sec", "mtime_nsec", "xattrs",
    )
    for k in sorted(oldm):
        a, b = oldm[k], newm[k]
        drift = [field for field in fields if a.get(field) != b.get(field)]
        if drift:
            bad.append((k, drift))
    if bad:
        raise SystemExit(f"{label}: EROFS inode metadata drifted on {bad[:10]}")


def _len_prefixed(payload):
    return struct.pack("<I", len(payload)) + payload


def _apk_chunk_digests(sections, hash_name="sha256"):
    chunk_size = 1024 * 1024
    digests = []
    for sec in sections:
        mv = memoryview(sec)
        pos = 0
        n = len(mv)
        while pos < n:
            chunk = mv[pos : min(pos + chunk_size, n)]
            h = hashlib.new(hash_name)
            h.update(b"\xa5" + struct.pack("<I", len(chunk)))
            h.update(chunk)
            digests.append(h.digest())
            pos += chunk_size
    top = hashlib.new(hash_name)
    top.update(b"\x5a" + struct.pack("<I", len(digests)))
    for d in digests:
        top.update(d)
    return top.digest()


def _openssl_rsa_sign(data, key_pem_path):
    res = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key_pem_path)],
        input=data,
        capture_output=True,
        check=True,
    )
    return res.stdout


def _openssl_rsa_verify_spki(data, sig, pub_key_der, hash_name="sha256"):
    with tempfile.TemporaryDirectory(prefix="gbos-apk-verify-") as td:
        td = Path(td)
        spki_f = td / "signer-spki.der"
        pub_f = td / "pub.pem"
        sig_f = td / "sig.bin"
        spki_f.write_bytes(pub_key_der)
        sig_f.write_bytes(sig)
        pub = subprocess.run(
            ["openssl", "pkey", "-pubin", "-inform", "DER", "-in", str(spki_f), "-out", str(pub_f)],
            capture_output=True,
        )
        if pub.returncode != 0:
            return False
        ver = subprocess.run(
            ["openssl", "dgst", f"-{hash_name}", "-verify", str(pub_f), "-signature", str(sig_f)],
            input=data,
            capture_output=True,
        )
        return ver.returncode == 0


def _locate_zip_sections(raw):
    eocd_off = raw.rfind(b"PK\x05\x06")
    if eocd_off < 0 or len(raw) - eocd_off < 22:
        raise SystemExit("invalid ZIP EOCD")
    com_len = struct.unpack_from("<H", raw, eocd_off + 20)[0]
    if eocd_off + 22 + com_len != len(raw):
        raise SystemExit("unexpected trailing bytes after ZIP EOCD")
    cd_size, cd_offset = struct.unpack_from("<II", raw, eocd_off + 12)
    if cd_offset + cd_size > eocd_off:
        raise SystemExit("invalid ZIP central directory bounds")
    return cd_offset, cd_size, eocd_off


def _zip_filename_bytes(info):
    encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
    try:
        return info.filename.encode(encoding)
    except UnicodeEncodeError as exc:
        raise SystemExit(f"cannot encode ZIP entry name {info.filename!r} as {encoding}") from exc


def _raw_compressed_zip_entry(raw, info, src_apex_path):
    """Return the source entry's compressed byte stream without a zlib round-trip."""
    off = info.header_offset
    if off < 0 or off + 30 > len(raw) or raw[off : off + 4] != b"PK\x03\x04":
        raise SystemExit(f"{src_apex_path}: invalid local header for {info.filename!r}")
    flags, method = struct.unpack_from("<HH", raw, off + 6)
    fn_len, ex_len = struct.unpack_from("<HH", raw, off + 26)
    data_off = off + 30 + fn_len + ex_len
    data_end = data_off + info.compress_size
    if flags & 0x1 or method != info.compress_type or data_end > len(raw):
        raise SystemExit(f"{src_apex_path}: unsupported local entry layout for {info.filename!r}")
    if raw[off + 30 : off + 30 + fn_len] != _zip_filename_bytes(info):
        raise SystemExit(f"{src_apex_path}: local/central ZIP name mismatch for {info.filename!r}")
    return raw[data_off:data_end]


def _alignment_extra(current_offset, name_length, alignment):
    """Return an Android 0xd935 local extra field whose payload records alignment."""
    if alignment <= 1:
        return b""
    base_data_offset = current_offset + 30 + name_length
    total_size = (-base_data_offset) % alignment
    if total_size == 0:
        return b""
    # The header itself is six bytes. For alignment=4 and residue=1,
    # adding four just once gives five, while pack() still emits six.
    while total_size < 6:
        total_size += alignment
    return (
        struct.pack("<HHH", ZIPALIGN_EXTRA_HEADER_ID, total_size - 4, alignment)
        + b"\x00" * (total_size - 6)
    )


def repack_apex_zip(src_apex_path, out_unsigned_zip, payload_bytes, pubkey_bytes):
    """Repack an APEX while byte-copying unchanged compressed entry streams."""
    raw_src = Path(src_apex_path).read_bytes()
    with zipfile.ZipFile(src_apex_path, "r") as zin:
        infos = zin.infolist()
        names = [info.filename for info in infos]
        if "apex_payload.img" not in names or "apex_pubkey" not in names:
            raise SystemExit(f"{src_apex_path}: missing apex_payload.img or apex_pubkey")
        if len(set(names)) != len(names):
            raise SystemExit(f"{src_apex_path}: duplicate ZIP entries")
        entries = []
        for info in infos:
            if info.filename.startswith("/") or ".." in Path(info.filename).parts:
                raise SystemExit(f"{src_apex_path}: unsafe ZIP entry {info.filename!r}")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise SystemExit(f"unsupported ZIP compression {info.compress_type} on {info.filename}")
            if info.filename == "apex_payload.img":
                comp_data = payload_bytes
                crc = zipfile.crc32(payload_bytes) & 0xFFFFFFFF
                file_size = len(payload_bytes)
                compress_type = zipfile.ZIP_STORED
            elif info.filename == "apex_pubkey":
                comp_data = pubkey_bytes
                crc = zipfile.crc32(pubkey_bytes) & 0xFFFFFFFF
                file_size = len(pubkey_bytes)
                compress_type = zipfile.ZIP_STORED
            else:
                comp_data = _raw_compressed_zip_entry(raw_src, info, src_apex_path)
                crc = info.CRC
                file_size = info.file_size
                compress_type = info.compress_type
            entries.append((info, comp_data, crc, file_size, compress_type))

    out = bytearray()
    cd_records = []
    for info, comp_data, crc, file_size, compress_type in entries:
        fn_bytes = _zip_filename_bytes(info)
        align = (16384 if info.filename == "apex_payload.img" or info.filename.endswith(".so")
                 else 4) if compress_type == zipfile.ZIP_STORED else 1
        extra = _alignment_extra(len(out), len(fn_bytes), align)
        lh_offset = len(out)
        dos_date = ((info.date_time[0] - 1980) << 9) | (info.date_time[1] << 5) | info.date_time[2]
        dos_time = (info.date_time[3] << 11) | (info.date_time[4] << 5) | (info.date_time[5] // 2)
        lh = struct.pack(
            "<4sHHHHHIIIHH",
            b"PK\x03\x04",
            info.extract_version,
            info.flag_bits & 0x800,
            compress_type,
            dos_time,
            dos_date,
            crc,
            len(comp_data),
            file_size,
            len(fn_bytes),
            len(extra),
        )
        out += lh + fn_bytes + extra
        if info.filename == "apex_payload.img" and (len(out) % 16384) != 0:
            raise SystemExit(f"apex_payload.img data offset {len(out)} is not 16384-byte aligned")
        out += comp_data
        cd_rec = struct.pack(
            "<4sHHHHHHIIIHHHHHII",
            b"PK\x01\x02",
            info.create_version,
            info.extract_version,
            info.flag_bits & 0x800,
            compress_type,
            dos_time,
            dos_date,
            crc,
            len(comp_data),
            file_size,
            len(fn_bytes),
            0,
            0,
            0,
            info.internal_attr,
            info.external_attr,
            lh_offset,
        ) + fn_bytes
        cd_records.append(cd_rec)

    cd_offset = len(out)
    cd_blob = b"".join(cd_records)
    out += cd_blob
    eocd = struct.pack(
        "<4sHHHHIIH",
        b"PK\x05\x06",
        0,
        0,
        len(cd_records),
        len(cd_records),
        len(cd_blob),
        cd_offset,
        0,
    )
    out += eocd
    Path(out_unsigned_zip).write_bytes(bytes(out))


def sign_apex_container(
    unsigned_zip_path,
    key_pem_path,
    cert_der_path,
    out_apex_path,
    min_sdk=None,
    max_sdk=None,
):
    """Sign an unsigned APEX ZIP with APK Signature Scheme v2 and v3 (RSA-4096 + SHA-256)."""
    if (
        not isinstance(min_sdk, int)
        or not isinstance(max_sdk, int)
        or not (0 <= min_sdk <= max_sdk <= 0xFFFFFFFF)
    ):
        raise ValueError("explicit stock v3 minSdk/maxSdk values are required")
    # Alignment must precede v2/v3 signing; moving local entries afterwards
    # would invalidate the content signatures.
    check_zip_layout(unsigned_zip_path)
    raw = Path(unsigned_zip_path).read_bytes()
    cert_der = Path(cert_der_path).read_bytes()
    cd_offset, cd_size, eocd_off = _locate_zip_sections(raw)
    before_cd = raw[:cd_offset]
    cd = raw[cd_offset : cd_offset + cd_size]
    eocd = bytearray(raw[eocd_off:])

    # EOCD used in digest calculation must have cd_offset set to the start of the APK Signing Block (= cd_offset).
    eocd_for_digest = bytearray(eocd)
    struct.pack_into("<I", eocd_for_digest, 16, cd_offset)
    top_digest = _apk_chunk_digests((before_cd, cd, bytes(eocd_for_digest)))

    digest_entry = _len_prefixed(struct.pack("<I", SIG_RSA_PKCS1_V1_5_SHA256) + _len_prefixed(top_digest))
    digests_seq = _len_prefixed(digest_entry)
    certs_seq = _len_prefixed(_len_prefixed(cert_der))

    # V2 signed data carries the v3 stripping protection attribute (0xBEEFF00D -> 3).
    v2_attr = _len_prefixed(struct.pack("<II", STRIPPING_PROTECTION_ATTR_ID, 3))
    v2_signed_data = digests_seq + certs_seq + _len_prefixed(v2_attr)
    v2_sig = _openssl_rsa_sign(v2_signed_data, key_pem_path)
    v2_sigs_seq = _len_prefixed(_len_prefixed(struct.pack("<I", SIG_RSA_PKCS1_V1_5_SHA256) + _len_prefixed(v2_sig)))
    pub_der = subprocess.run(
        ["openssl", "pkey", "-in", str(key_pem_path), "-pubout", "-outform", "DER"],
        capture_output=True,
        check=True,
    ).stdout
    v2_signer = _len_prefixed(
        _len_prefixed(v2_signed_data) + v2_sigs_seq + _len_prefixed(pub_der)
    )
    v2_block_value = _len_prefixed(v2_signer)

    # V3 signed data carries minSdk and maxSdk.
    v3_signed_data = (
        digests_seq
        + certs_seq
        + struct.pack("<II", min_sdk, max_sdk)
        + _len_prefixed(b"")
    )
    v3_sig = _openssl_rsa_sign(v3_signed_data, key_pem_path)
    v3_sigs_seq = _len_prefixed(_len_prefixed(struct.pack("<I", SIG_RSA_PKCS1_V1_5_SHA256) + _len_prefixed(v3_sig)))
    v3_signer = _len_prefixed(
        _len_prefixed(v3_signed_data)
        + struct.pack("<II", min_sdk, max_sdk)
        + v3_sigs_seq
        + _len_prefixed(pub_der)
    )
    v3_block_value = _len_prefixed(v3_signer)

    def kv_pair(block_id, val):
        return struct.pack("<QI", 4 + len(val), block_id) + val

    pairs = kv_pair(APK_SIG_SCHEME_V2_ID, v2_block_value) + kv_pair(APK_SIG_SCHEME_V3_ID, v3_block_value)
    base_total = 8 + len(pairs) + 8 + 16
    rem = base_total % 4096
    if rem != 0:
        pad_needed = (4096 - rem) % 4096
        if pad_needed < 12:
            pad_needed += 4096
        pairs += kv_pair(VERITY_PADDING_BLOCK_ID, b"\x00" * (pad_needed - 12))
    size_of_block = len(pairs) + 8 + 16
    sig_block = (
        struct.pack("<Q", size_of_block)
        + pairs
        + struct.pack("<Q", size_of_block)
        + APK_SIG_BLOCK_MAGIC
    )
    if len(sig_block) % 4096 != 0:
        raise SystemExit(f"APK signing block size {len(sig_block)} is not a multiple of 4096")

    new_cd_offset = cd_offset + len(sig_block)
    struct.pack_into("<I", eocd, 16, new_cd_offset)
    Path(out_apex_path).write_bytes(before_cd + sig_block + cd + bytes(eocd))


def _read_len_prefixed(buf, off=0):
    if off + 4 > len(buf):
        raise ValueError("truncated length-prefixed field")
    n = struct.unpack_from("<I", buf, off)[0]
    if off + 4 + n > len(buf):
        raise ValueError("length-prefixed field exceeds buffer")
    return buf[off + 4 : off + 4 + n], off + 4 + n


def _read_only_len_prefixed(buf, label):
    value, end = _read_len_prefixed(buf, 0)
    if end != len(buf):
        raise ValueError(f"unsupported {label}: expected exactly one length-prefixed value")
    return value


def _zip_unchanged_entry_contract(apex_path):
    """Return the complete ordered ZIP contract, masking mutable entry contents."""
    with zipfile.ZipFile(apex_path, "r") as zf:
        infos = zf.infolist()
        names = [info.filename for info in infos]
        for required in ("apex_payload.img", "apex_pubkey"):
            if names.count(required) != 1:
                raise ValueError(f"{apex_path}: expected exactly one {required} ZIP entry")
        if len(set(names)) != len(names):
            raise ValueError(f"{apex_path}: duplicate ZIP entry names")
        return tuple(
            (info.filename, None, None)
            if info.filename in ("apex_payload.img", "apex_pubkey")
            else (info.filename, info.CRC, info.file_size)
            for info in infos
        )


def verify_apex_container(apex_path, expected_cert_der=None, stock_apex_path=None, *, allow_stock_source_stamp=False):
    """Verify an APEX container's ZIP alignment and APK Signature Scheme v2 + v3 signatures."""
    if allow_stock_source_stamp and (expected_cert_der is not None or stock_apex_path is not None):
        raise ValueError("SourceStamp allowance is only for stock input")
    check_zip_layout(apex_path)
    raw = Path(apex_path).read_bytes()
    cd_offset, cd_size, eocd_off = _locate_zip_sections(raw)

    if stock_apex_path is not None:
        stock_contract = _zip_unchanged_entry_contract(stock_apex_path)
        actual_contract = _zip_unchanged_entry_contract(apex_path)
        if actual_contract != stock_contract:
            raise ValueError(
                "unchanged ZIP entry name/CRC/uncompressed-size/order drifted: "
                f"stock={stock_contract!r}, actual={actual_contract!r}"
            )

    # Check apex_payload.img alignment in the ZIP local file headers.
    with zipfile.ZipFile(apex_path, "r") as zf:
        info = zf.getinfo("apex_payload.img")
        if info.compress_type != zipfile.ZIP_STORED:
            raise ValueError("apex_payload.img is not stored uncompressed")
        off = info.header_offset
        fn_len, ex_len = struct.unpack_from("<HH", raw, off + 26)
        data_off = off + 30 + fn_len + ex_len
        if data_off % 4096 != 0:
            raise ValueError(f"apex_payload.img data offset {data_off} is not 4096-byte aligned")

    if cd_offset < 32 or raw[cd_offset - 16 : cd_offset] != APK_SIG_BLOCK_MAGIC:
        raise ValueError("missing APK Signing Block magic before central directory")
    size_footer = struct.unpack_from("<Q", raw, cd_offset - 24)[0]
    sig_block_start = cd_offset - 8 - size_footer
    if sig_block_start < 0:
        raise ValueError("invalid APK Signing Block start")
    size_header = struct.unpack_from("<Q", raw, sig_block_start)[0]
    if size_header != size_footer:
        raise ValueError("APK Signing Block header/footer size mismatch")

    pairs_buf = raw[sig_block_start + 8 : cd_offset - 24]
    pos = 0
    blocks = {}
    while pos < len(pairs_buf):
        if pos + 12 > len(pairs_buf):
            raise ValueError("truncated APK Signing Block pair")
        pair_len = struct.unpack_from("<Q", pairs_buf, pos)[0]
        if pair_len < 4 or pos + 8 + pair_len > len(pairs_buf):
            raise ValueError("invalid APK Signing Block pair length")
        block_id = struct.unpack_from("<I", pairs_buf, pos + 8)[0]
        if block_id in blocks:
            raise ValueError(f"duplicate APK Signing Block ID {block_id:#x}")
        blocks[block_id] = pairs_buf[pos + 12 : pos + 8 + pair_len]
        pos += 8 + pair_len

    block_ids = set(blocks)
    # Stock input may carry an ancillary SourceStamp. We still verify its
    # v2/v3 content signatures. Repacking drops the old signing block, and
    # rebuilt output must pass the default policy without a stale stamp block.
    # The ZIP companion entry remains byte-for-byte under the unchanged-entry
    # contract; it is metadata, not an asserted or verified SourceStamp.
    allowed_block_ids = APEX_ALLOWED_SIGNING_BLOCK_IDS | (
        {SOURCE_STAMP_V2_BLOCK_ID} if allow_stock_source_stamp else set()
    )
    if (
        not APEX_REQUIRED_SIGNING_BLOCK_IDS.issubset(block_ids)
        or not block_ids.issubset(allowed_block_ids)
    ):
        raise ValueError(
            "unexpected APK Signing Block ID set: "
            f"required={sorted(APEX_REQUIRED_SIGNING_BLOCK_IDS)!r}, "
            f"allowed={sorted(allowed_block_ids)!r}, actual={sorted(blocks)!r}"
        )
    if not blocks[VERITY_PADDING_BLOCK_ID] or any(blocks[VERITY_PADDING_BLOCK_ID]):
        raise ValueError("invalid APK Signing Block verity padding pair")
    before_sig = raw[:sig_block_start]
    cd = raw[cd_offset : cd_offset + cd_size]
    eocd = bytearray(raw[eocd_off:])
    struct.pack_into("<I", eocd, 16, sig_block_start)
    expected_top_digests = {
        SIG_RSA_PKCS1_V1_5_SHA256: _apk_chunk_digests((before_sig, cd, bytes(eocd)), "sha256"),
        SIG_RSA_PKCS1_V1_5_SHA512: _apk_chunk_digests((before_sig, cd, bytes(eocd)), "sha512"),
    }

    certs = {}
    for scheme_id, scheme_name in ((APK_SIG_SCHEME_V2_ID, "v2"), (APK_SIG_SCHEME_V3_ID, "v3")):
        signers_seq = _read_only_len_prefixed(blocks[scheme_id], f"{scheme_name} signer block")
        signer = _read_only_len_prefixed(signers_seq, f"{scheme_name} signer sequence")
        signed_data, p = _read_len_prefixed(signer, 0)
        if scheme_id == APK_SIG_SCHEME_V3_ID:
            if p + 8 > len(signer):
                raise ValueError("truncated v3 signer SDK range")
            signer_min_sdk, signer_max_sdk = struct.unpack_from("<II", signer, p)
            if signer_min_sdk > signer_max_sdk:
                raise ValueError("v3 signer minSdk exceeds maxSdk")
            p += 8
        sigs_seq, p = _read_len_prefixed(signer, p)
        pub_key_der, p = _read_len_prefixed(signer, p)
        if p != len(signer):
            raise ValueError(f"unsupported {scheme_name} signer trailing data")

        digests_seq, sd_p = _read_len_prefixed(signed_data, 0)
        certs_seq, sd_p = _read_len_prefixed(signed_data, sd_p)
        if scheme_id == APK_SIG_SCHEME_V3_ID:
            if sd_p + 8 > len(signed_data):
                raise ValueError("truncated v3 signed-data SDK range")
            sd_min_sdk, sd_max_sdk = struct.unpack_from("<II", signed_data, sd_p)
            sd_p += 8
            if (sd_min_sdk, sd_max_sdk) != (signer_min_sdk, signer_max_sdk):
                raise ValueError("v3 minSdk/maxSdk mismatch between signer and signed_data")
        attrs_seq, sd_p = _read_len_prefixed(signed_data, sd_p)
        if scheme_id == APK_SIG_SCHEME_V2_ID:
            # Current apksig appends one empty length-prefixed reserved field; older valid
            # v2 signers end immediately after the attributes sequence.
            if signed_data[sd_p:] not in (b"", b"\x00\x00\x00\x00"):
                raise ValueError("unsupported v2 signed-data trailing fields")
            attr = _read_only_len_prefixed(attrs_seq, "v2 additional attributes")
            if len(attr) != 8:
                raise ValueError("unsupported v2 stripping protection attribute structure")
            attr_id, attr_val = struct.unpack("<II", attr)
            if attr_id != STRIPPING_PROTECTION_ATTR_ID or attr_val != 3:
                raise ValueError("v2 missing v3 stripping protection attribute")
        else:
            if sd_p != len(signed_data):
                raise ValueError("unsupported v3 signed-data trailing fields")
            if attrs_seq:
                raise ValueError("unsupported v3 additional attributes")

        digest_rec = _read_only_len_prefixed(digests_seq, f"{scheme_name} digest sequence")
        if len(digest_rec) < 8:
            raise ValueError(f"truncated {scheme_name} digest record")
        d_alg = struct.unpack_from("<I", digest_rec, 0)[0]
        d_bytes, d_end = _read_len_prefixed(digest_rec, 4)
        if d_end != len(digest_rec):
            raise ValueError(f"unsupported {scheme_name} digest record")
        if d_alg not in expected_top_digests or d_bytes != expected_top_digests[d_alg]:
            raise ValueError(f"{scheme_name} content digest mismatch")

        cert_der = _read_only_len_prefixed(certs_seq, f"{scheme_name} certificate sequence")
        if expected_cert_der is not None and cert_der != expected_cert_der:
            raise ValueError(f"{scheme_name} certificate does not match expected container certificate")
        try:
            cert_spki = _certificate_spki(cert_der, f"{scheme_name} certificate")
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
        if pub_key_der != cert_spki:
            raise ValueError(f"{scheme_name} signer public key does not match certificate public key")

        sig_rec = _read_only_len_prefixed(sigs_seq, f"{scheme_name} signature sequence")
        if len(sig_rec) < 8:
            raise ValueError(f"truncated {scheme_name} signature record")
        s_alg = struct.unpack_from("<I", sig_rec, 0)[0]
        sig_bytes, s_end = _read_len_prefixed(sig_rec, 4)
        if s_end != len(sig_rec):
            raise ValueError(f"unsupported {scheme_name} signature record")
        if s_alg != d_alg:
            raise ValueError(f"unexpected {scheme_name} signature algorithm {s_alg:#x}")
        hash_name = "sha256" if s_alg == SIG_RSA_PKCS1_V1_5_SHA256 else "sha512"
        if not _openssl_rsa_verify_spki(signed_data, sig_bytes, pub_key_der, hash_name):
            raise ValueError(f"{scheme_name} RSA signature verification failed")
        certs[scheme_name] = hashlib.sha256(cert_der).hexdigest()

    if certs["v2"] != certs["v3"]:
        raise ValueError("v2/v3 container signer certificates differ")

    return {
        "v2_verified": True,
        "v3_verified": True,
        "source_stamp_present": SOURCE_STAMP_V2_BLOCK_ID in blocks,
        "payload_data_offset": data_off,
        "cert_sha256": certs["v3"],
        "v3_min_sdk": signer_min_sdk,
        "v3_max_sdk": signer_max_sdk,
    }


def _parse_apex_avb_info(info_out, apex_rel):
    salts = [line.split(":", 1)[1].strip().lower() for line in info_out.splitlines() if line.strip().startswith("Salt:")]
    key_prefix = "Prop: apex.key -> '"
    apex_keys = [
        line.strip()[len(key_prefix):-1]
        for line in info_out.splitlines()
        if line.strip().startswith(key_prefix) and line.strip().endswith("'")
    ]
    if len(salts) != 1 or len(apex_keys) != 1:
        raise SystemExit(
            f"{apex_rel}: stock payload must contain exactly one salt and one apex.key property"
        )
    try:
        bytes.fromhex(salts[0])
    except ValueError as exc:
        raise SystemExit(f"{apex_rel}: stock payload has invalid AVB salt {salts[0]!r}") from exc
    return {"salt": salts[0], "apex_key": apex_keys[0]}


def _stock_apex_avb_identity(otatools, payload_img, spec, apex_rel):
    identity = _parse_apex_avb_info(
        _run_ot(otatools, "avbtool", "info_image", "--image", payload_img),
        apex_rel,
    )
    expected = {
        "salt": str(spec["salt"]).lower(),
        "apex_key": str(spec["apex_key"]),
    }
    if identity != expected:
        raise SystemExit(
            f"{apex_rel}: stock payload AVB identity drifted: expected={expected!r}, actual={identity!r}"
        )
    return identity


def _apex_hashtree_args(partition_size, key, salt, apex_key):
    """Arguments shared by APEX footer sizing and the real footer operation."""
    return (
        "--partition_name", "apex_payload",
        "--partition_size", str(partition_size),
        "--key", str(key),
        "--algorithm", "SHA256_RSA4096",
        *APEX_AVB_FOOTER_ARGS,
        "--salt", salt,
        "--prop", f"apex.key:{apex_key}",
    )


def _apex_max_image_size(otatools, partition_size, key, salt, apex_key):
    return int(
        _run_ot(
            otatools,
            "avbtool",
            "add_hashtree_footer",
            *_apex_hashtree_args(partition_size, key, salt, apex_key),
            "--calc_max_image_size",
        ).strip()
    )


def _add_apex_hashtree_footer(otatools, image, partition_size, key, salt, apex_key):
    return _run_ot(
        otatools,
        "avbtool",
        "add_hashtree_footer",
        "--image", image,
        *_apex_hashtree_args(partition_size, key, salt, apex_key),
    )


def _apex_partition_size(otatools, original_size, erofs_size, key, salt, apex_key):
    """Keep the stock payload size when possible, otherwise grow until AVB metadata fits."""
    part_size = original_size
    max_args = (key, salt, apex_key)
    if erofs_size > _apex_max_image_size(otatools, part_size, *max_args):
        part_size = (
            (max(part_size, erofs_size + erofs_size // 64 + 65536) + 4095) // 4096
        ) * 4096
        while erofs_size > _apex_max_image_size(otatools, part_size, *max_args):
            part_size += 65536
    return part_size


def patch_apex_file(
    apex_file,
    apex_rel,
    work_dir,
    otatools,
    keys,
    *,
    manifest_path=MANIFEST_PATH,
    spec=None,
    scope=None,
):
    """Unpack one APEX, patch its payload ELFs, rebuild/sign apex_payload.img, update apex_pubkey, and sign container."""
    apex_file = Path(apex_file)
    work_dir = Path(work_dir)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    spec = dict(APEX_SPECS[apex_rel] if spec is None else spec)
    scope = ("apex", apex_rel) if scope is None else scope

    try:
        stock_container = verify_apex_container(apex_file, allow_stock_source_stamp=True)
    except ValueError as exc:
        raise SystemExit(f"{apex_rel}: stock container signature verification failed: {exc}") from exc

    with zipfile.ZipFile(apex_file, "r") as zf:
        raw_payload = zf.read("apex_payload.img")

    stock_payload = work_dir / "stock-apex_payload.img"
    stock_payload.write_bytes(raw_payload)
    stock_identity = _stock_apex_avb_identity(otatools, stock_payload, spec, apex_rel)

    erofs_bytes, orig_payload_size = _strip_avb_footer(raw_payload, apex_rel)
    del raw_payload
    src_erofs = work_dir / "payload.erofs.src"
    src_erofs.write_bytes(erofs_bytes)
    del erofs_bytes

    sb = rebuild.superblock_params(src_erofs, strict_metadata=True)
    old_meta = _erofs_meta_map(src_erofs)
    payload_tree = work_dir / "payload-tree"
    payload_tree.mkdir()
    _run_ot(
        otatools,
        "fsck.erofs",
        f"--extract={payload_tree}",
        "--overwrite",
        "--preserve-perms",
        src_erofs,
    )

    old_index = _index_tree(payload_tree)
    report_path = work_dir / "patch-report.json"
    try:
        report = PATCH_IFMA.run(payload_tree, manifest_path, report_path, True, scope=scope)
    except (OSError, PATCH_IFMA.PatchError) as exc:
        raise SystemExit(f"IFMA patch refused on {apex_rel}: {exc}") from exc
    scan_rebuilt_tree(
        payload_tree,
        f"{apex_rel}:patched-payload",
        otatools,
        work_dir / "scan-patched",
        manifest_path=manifest_path,
        provenance=_apex_payload_provenance(apex_rel, spec["partition"]),
        root_kind="erofs_file",
    )

    fc_file = work_dir / "payload_file_contexts"
    fs_file = work_dir / "payload_fs_config"
    rebuild.write_configs(
        "/",
        payload_tree,
        old_meta,
        fc_file,
        fs_file,
        allow_caps=bool(spec.get("allow_caps", False)),
    )

    new_erofs = work_dir / "payload.erofs"
    mkfs_args = [
        "-z", "lz4hc,level=9",
        "-b", "4096",
        "-T", str(sb["build_time"]),
        "-U", sb["uuid"],
    ]
    if sb["volume_name"]:
        mkfs_args += ["-L", sb["volume_name"]]
    if sb["pcluster_size"]:
        mkfs_args += ["-C", str(sb["pcluster_size"])]
    rebuild.restore_mtimes(payload_tree, old_meta)
    mkfs_args += [
        "--mount-point", "/",
        "--fs-config-file", str(fs_file),
        "--file-contexts", str(fc_file),
        str(new_erofs),
        str(payload_tree),
    ]
    _run_ot(otatools, "mkfs.erofs", *mkfs_args)

    if rebuild.fs_uuid(new_erofs) != sb["uuid"]:
        raise SystemExit(f"{apex_rel}: rebuilt payload EROFS UUID drifted")
    _assert_same_erofs_metadata(src_erofs, new_erofs, apex_rel)

    check_tree = work_dir / "payload-tree-new"
    check_tree.mkdir()
    _run_ot(
        otatools,
        "fsck.erofs",
        f"--extract={check_tree}",
        "--overwrite",
        "--preserve-perms",
        new_erofs,
    )
    new_index = _index_tree(check_tree)
    if set(old_index) != set(new_index):
        raise SystemExit(f"{apex_rel}: payload file list changed")
    expected_rel = {t["rel_path"] for t in report["targets"]}
    actual_changed = {k for k in old_index if old_index[k] != new_index[k]}
    if actual_changed != expected_rel:
        raise SystemExit(f"{apex_rel}: unexpected payload file changes: {sorted(actual_changed)} vs {sorted(expected_rel)}")
    scan_rebuilt_tree(
        check_tree,
        f"{apex_rel}:rebuilt-payload",
        otatools,
        work_dir / "scan-rebuilt",
        manifest_path=manifest_path,
        provenance=_apex_payload_provenance(apex_rel, spec["partition"]),
        root_kind="erofs_file",
    )

    new_payload = work_dir / "apex_payload.img"
    shutil.copyfile(new_erofs, new_payload)
    pubkey_path = work_dir / "apex_pubkey"
    _run_ot(
        otatools,
        "avbtool",
        "extract_public_key",
        "--key", keys["payload_key"],
        "--output", pubkey_path,
    )

    # Keep the stock partition size if sufficient; size it using the exact real-footer arguments.
    erofs_sz = new_erofs.stat().st_size
    part_size = _apex_partition_size(
        otatools,
        orig_payload_size,
        erofs_sz,
        keys["payload_key"],
        stock_identity["salt"],
        stock_identity["apex_key"],
    )

    _add_apex_hashtree_footer(
        otatools,
        new_payload,
        part_size,
        keys["payload_key"],
        stock_identity["salt"],
        stock_identity["apex_key"],
    )
    _run_ot(
        otatools,
        "avbtool",
        "verify_image",
        "--image", new_payload,
        "--key", keys["payload_key"],
    )

    unsigned_zip = work_dir / "unsigned.apex"
    repack_apex_zip(apex_file, unsigned_zip, new_payload.read_bytes(), pubkey_path.read_bytes())
    signed_apex = work_dir / "signed.apex"
    sign_apex_container(
        unsigned_zip,
        keys["container_key"],
        keys["container_cert_der"],
        signed_apex,
        min_sdk=stock_container["v3_min_sdk"],
        max_sdk=stock_container["v3_max_sdk"],
    )
    verify_apex_file(
        signed_apex,
        apex_rel,
        work_dir / "verify",
        otatools,
        expected_cert_der=Path(keys["container_cert_der"]).read_bytes(),
        stock_apex_path=apex_file,
        manifest_path=manifest_path,
        spec=spec,
        scope=scope,
    )
    os.replace(signed_apex, apex_file)
    shutil.rmtree(payload_tree, ignore_errors=True)
    shutil.rmtree(check_tree, ignore_errors=True)
    return report


_RSA4096_SPKI_PREFIX = bytes.fromhex("30820222300d06092a864886f70d01010105000382020f003082020a0282020100")
_RSA4096_SPKI_SUFFIX = bytes.fromhex("0203010001")


def _avbpubkey_to_pem(pubkey_bytes, apex_rel):
    """Convert a 1032-byte RSA-4096 AvbRSAPublicKeyHeader blob (apex_pubkey) into a SubjectPublicKeyInfo PEM."""
    if len(pubkey_bytes) != 1032 or struct.unpack_from(">I", pubkey_bytes, 0)[0] != 4096:
        raise SystemExit(f"{apex_rel}: invalid RSA-4096 apex_pubkey header (len={len(pubkey_bytes)})")
    modulus = pubkey_bytes[8:520]
    spki_der = _RSA4096_SPKI_PREFIX + modulus + _RSA4096_SPKI_SUFFIX
    b64 = base64.b64encode(spki_der).decode("ascii")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return ("-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n").encode("ascii")


def verify_apex_file(
    apex_file,
    apex_rel,
    work_dir,
    otatools,
    *,
    expected_cert_der=None,
    stock_apex_path=None,
    manifest_path=MANIFEST_PATH,
    spec=None,
    scope=None,
):
    """Verify an APEX's outer APK v2/v3 signature, inner payload AVB signature against embedded apex_pubkey, and patched ELFs."""
    apex_file = Path(apex_file)
    work_dir = Path(work_dir)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    spec = dict(APEX_SPECS[apex_rel] if spec is None else spec)
    scope = ("apex", apex_rel) if scope is None else scope

    try:
        container_info = verify_apex_container(
            apex_file,
            expected_cert_der=expected_cert_der,
            stock_apex_path=stock_apex_path,
        )
    except ValueError as exc:
        raise SystemExit(f"{apex_rel}: container signature verification failed: {exc}") from exc

    with zipfile.ZipFile(apex_file, "r") as zf:
        payload_bytes = zf.read("apex_payload.img")
        pubkey_bytes = zf.read("apex_pubkey")

    payload_img = work_dir / "apex_payload.img"
    pubkey_path = work_dir / "apex_pubkey.pem"
    payload_img.write_bytes(payload_bytes)
    pubkey_path.write_bytes(_avbpubkey_to_pem(pubkey_bytes, apex_rel))

    reextracted_pub = work_dir / "apex_pubkey.reextracted"
    _run_ot(
        otatools,
        "avbtool",
        "extract_public_key",
        "--key", pubkey_path,
        "--output", reextracted_pub,
    )
    if reextracted_pub.read_bytes() != pubkey_bytes:
        raise SystemExit(f"{apex_rel}: embedded apex_pubkey failed AvbRSAPublicKeyHeader round-trip check")

    _run_ot(
        otatools,
        "avbtool",
        "verify_image",
        "--image", payload_img,
        "--key", pubkey_path,
    )
    info_out = _run_ot(otatools, "avbtool", "info_image", "--image", payload_img)
    identity = _parse_apex_avb_info(info_out, apex_rel)
    expected_identity = {
        "salt": str(spec["salt"]).lower(),
        "apex_key": str(spec["apex_key"]),
    }
    if identity != expected_identity:
        raise SystemExit(
            f"{apex_rel}: payload AVB identity drifted: expected={expected_identity!r}, actual={identity!r}"
        )

    erofs_bytes, _ = _strip_avb_footer(payload_bytes, apex_rel)
    erofs_path = work_dir / "payload.erofs"
    erofs_path.write_bytes(erofs_bytes)
    extract_dir = work_dir / "tree"
    extract_dir.mkdir()
    _run_ot(otatools, "fsck.erofs", f"--extract={extract_dir}", "--overwrite", erofs_path)

    report_path = work_dir / "verify-report.json"
    try:
        report = PATCH_IFMA.run(extract_dir, manifest_path, report_path, False, scope=scope)
    except (OSError, PATCH_IFMA.PatchError) as exc:
        raise SystemExit(f"{apex_rel}: post-sign IFMA verification failed: {exc}") from exc
    if not all(t["before"] == "patched" and t["after"] == "patched" for t in report["targets"]):
        raise SystemExit(f"{apex_rel}: payload target not in patched state")
    scan_rebuilt_tree(
        extract_dir,
        f"{apex_rel}:verified-payload",
        otatools,
        work_dir / "scan-verified",
        manifest_path=manifest_path,
        provenance=_apex_payload_provenance(apex_rel, spec["partition"]),
        root_kind="erofs_file",
    )
    shutil.rmtree(extract_dir, ignore_errors=True)
    return {
        "apex": apex_rel,
        "container": container_info,
        "targets": report["targets"],
    }


def patch_vendor_tree(prof, tree, tmp, otatools=None, keys_dir=None, *, manifest_path=MANIFEST_PATH):
    """Patch vendor_a plain ELFs (before REMOVE unlinks bin/ectool) and the 2 vendor_a APEXes."""
    if not enabled(prof):
        return False
    tree = Path(tree)
    tmp = Path(tmp)
    otatools = Path(otatools or os.environ["GBOS_OTATOOLS"])
    keys = ensure_apex_keys(keys_dir or _default_keys_dir(tmp), create=False)

    plain_report_path = tmp / "ifma-vendor-plain.json"
    try:
        plain_report = PATCH_IFMA.run(
            tree,
            manifest_path,
            plain_report_path,
            True,
            scope=("partition", "vendor_a"),
        )
    except (OSError, PATCH_IFMA.PatchError) as exc:
        raise SystemExit(f"IFMA vendor plain patch refused: {exc}") from exc

    for apex_rel, slug in (
        ("apex/com.android.hardware.audio.desktop.apex", "audio"),
        ("apex/com.android.hardware.fingerprint.crosfp.apex", "crosfp"),
    ):
        patch_apex_file(
            tree / apex_rel,
            apex_rel,
            tmp / f"ifma-apex-{slug}",
            otatools,
            keys,
            manifest_path=manifest_path,
        )
    scan_rebuilt_tree(
        tree, "vendor_a patched", otatools, tmp / "ifma-scan-vendor",
        manifest_path=manifest_path,
        provenance=_partition_provenance("vendor_a"),
    )
    print(f"ifma vendor patched: {len(plain_report['targets'])} plain targets + 2 APEXes")
    return True


def verify_rebuilt_vendor_tree(prof, rebuilt_tree, tmp, otatools=None, keys_dir=None, *, manifest_path=MANIFEST_PATH):
    """Verify the rebuilt vendor tree's surviving plain IFMA files, pre-removal ectool report, and 2 APEXes."""
    if not enabled(prof):
        return False
    rebuilt_tree = Path(rebuilt_tree)
    tmp = Path(tmp)
    otatools = Path(otatools or os.environ["GBOS_OTATOOLS"])
    keys = ensure_apex_keys(keys_dir or _default_keys_dir(tmp), create=False)
    cert_der = Path(keys["container_cert_der"]).read_bytes()

    plain_report_path = tmp / "ifma-vendor-plain.json"
    if not plain_report_path.is_file():
        raise SystemExit("missing ifma-vendor-plain.json report")
    plain_report = json.loads(plain_report_path.read_text(encoding="utf-8"))
    by_path = {t["path"]: t for t in plain_report.get("targets", [])}
    if set(by_path) != {
        "vendor/lib64/libzmij.dylib.so",
        "vendor/bin/ectool",
        "vendor/bin/hw/android.hardware.usb-service.desktop",
    }:
        raise SystemExit("ifma-vendor-plain.json target set mismatch")
    for p, row in by_path.items():
        if row.get("after") != "patched":
            raise SystemExit(f"ifma-vendor-plain.json: {p} was not patched")

    rows = {r["path"]: r for r in PATCH_IFMA._load_manifest(manifest_path)}
    for manifest_key, rel in (
        ("vendor/lib64/libzmij.dylib.so", "lib64/libzmij.dylib.so"),
        ("vendor/bin/hw/android.hardware.usb-service.desktop", "bin/hw/android.hardware.usb-service.desktop"),
    ):
        target = rebuilt_tree / rel
        if not target.is_file() or target.is_symlink():
            raise SystemExit(f"rebuilt vendor missing {rel}")
        res = PATCH_IFMA.validate(target.read_bytes(), rows[manifest_key])
        if res["state"] != "patched":
            raise SystemExit(f"rebuilt vendor {rel} is {res['state']}, expected patched")

    for apex_rel, slug in (
        ("apex/com.android.hardware.audio.desktop.apex", "audio"),
        ("apex/com.android.hardware.fingerprint.crosfp.apex", "crosfp"),
    ):
        verify_apex_file(
            rebuilt_tree / apex_rel,
            apex_rel,
            tmp / f"ifma-verify-{slug}",
            otatools,
            expected_cert_der=cert_der,
            manifest_path=manifest_path,
        )
    scan_rebuilt_tree(
        rebuilt_tree, "vendor_a rebuilt", otatools, tmp / "ifma-scan-vendor-readback",
        manifest_path=manifest_path,
        provenance=_partition_provenance("vendor_a"),
        include_pastel=_vendor_has_pastel(prof),
    )
    print("ifma vendor readback ok")
    return True


def _copy_erofs_extent(img_path, offset, size, destination, *, partition_name="system_a"):
    """Copy only the AVB original-image extent, using a bounded stream buffer."""
    if offset < 0 or size < 64 or offset + size > Path(img_path).stat().st_size:
        raise SystemExit(f"{partition_name} extent is truncated or outside the image")
    with Path(img_path).open("rb") as source:
        source.seek(offset + size - 64)
        original_size = _avb_footer_original_size(source.read(64), size, partition_name)
        source.seek(offset)

        class ExtentReader:
            remaining = original_size

            def read(self, count):
                if not self.remaining:
                    return b""
                data = source.read(min(count, self.remaining))
                if not data:
                    raise SystemExit(f"{partition_name} EROFS extent is truncated")
                self.remaining -= len(data)
                return data

        with Path(destination).open("wb") as target:
            shutil.copyfileobj(ExtentReader(), target, length=1024 * 1024)


def _copy_partition_extent(img_path, offset, size, destination, *, partition_name):
    """Copy one complete logical-partition extent with strict source bounds."""
    if offset < 0 or size <= 0 or offset + size > Path(img_path).stat().st_size:
        raise SystemExit(f"{partition_name} extent is truncated or outside the image")
    remaining = size
    with Path(img_path).open("rb") as source, Path(destination).open("wb") as target:
        source.seek(offset)
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise SystemExit(f"{partition_name} extent is truncated")
            target.write(chunk)
            remaining -= len(chunk)


def rebuild_system_partition(prof, img_path, tmp, otatools=None, keys_dir=None, *, manifest_path=MANIFEST_PATH):
    """Extract system_a from super, patch 6 plain ELFs + virt.apex (5 ELFs), rebuild EROFS, add AVB footer, and return extent."""
    if not enabled(prof):
        return None
    img_path = Path(img_path)
    tmp = Path(tmp)
    otatools = Path(otatools or os.environ["GBOS_OTATOOLS"])
    keys = ensure_apex_keys(keys_dir or _default_keys_dir(tmp), create=False)
    cert_der = Path(keys["container_cert_der"]).read_bytes()

    parts = _gpt_partitions(img_path)
    super_part = parts["super"]
    rel_off, system_size = _lp_Named_extent(img_path, super_part["start"], "system_a")
    if rel_off + system_size > super_part["size"]:
        raise SystemExit("system_a extent runs past the super partition")
    system_off = super_part["start"] + rel_off

    src_erofs = tmp / "system.erofs.src"
    _copy_erofs_extent(img_path, system_off, system_size, src_erofs)

    sb = rebuild.superblock_params(src_erofs, strict_metadata=True)
    if sb["uuid"] != SYSTEM_UUID:
        raise SystemExit(
            f"unexpected system_a uuid {sb['uuid']}: selected Google build {LAPIS_BUILD}"
        )

    smap = _erofs_meta_map(src_erofs)
    sys_tree = tmp / "system-tree"
    if sys_tree.exists():
        shutil.rmtree(sys_tree)
    sys_tree.mkdir(parents=True)
    _run_ot(
        otatools,
        "fsck.erofs",
        f"--extract={sys_tree}",
        "--overwrite",
        "--preserve-perms",
        src_erofs,
    )
    old_index = _index_tree(sys_tree)

    plain_report_path = tmp / "ifma-system-plain.json"
    try:
        plain_report = PATCH_IFMA.run(
            sys_tree,
            manifest_path,
            plain_report_path,
            True,
            scope=("partition", "system_a"),
        )
    except (OSError, PATCH_IFMA.PatchError) as exc:
        raise SystemExit(f"IFMA system plain patch refused: {exc}") from exc

    virt_rel = "system/apex/com.google.android.virt.apex"
    virt_report = patch_apex_file(
        sys_tree / virt_rel,
        virt_rel,
        tmp / "ifma-apex-virt",
        otatools,
        keys,
        manifest_path=manifest_path,
    )
    scan_rebuilt_tree(
        sys_tree, "system_a patched", otatools, tmp / "ifma-scan-system",
        manifest_path=manifest_path,
        provenance=_partition_provenance("system_a"),
    )

    fc_file = tmp / "system_file_contexts"
    fs_file = tmp / "system_fs_config"
    rebuild.write_configs("/", sys_tree, smap, fc_file, fs_file, allow_caps=True)

    new_erofs = tmp / "system.erofs"
    if new_erofs.exists():
        new_erofs.unlink()
    mkfs_args = [
        "-z", "lz4hc,level=9",
        "-b", "4096",
        "-T", str(sb["build_time"]),
        "-U", sb["uuid"],
    ]
    if sb["volume_name"]:
        mkfs_args += ["-L", sb["volume_name"]]
    if sb["pcluster_size"]:
        mkfs_args += ["-C", str(sb["pcluster_size"])]
    rebuild.restore_mtimes(sys_tree, smap)
    mkfs_args += [
        "--mount-point", "/",
        "--fs-config-file", str(fs_file),
        "--file-contexts", str(fc_file),
        str(new_erofs),
        str(sys_tree),
    ]
    _run_ot(otatools, "mkfs.erofs", *mkfs_args)

    if rebuild.fs_uuid(new_erofs) != SYSTEM_UUID:
        raise SystemExit("system.erofs UUID drifted")
    _assert_same_erofs_metadata(src_erofs, new_erofs, "system_a")

    shutil.rmtree(sys_tree)
    new_tree = tmp / "system-tree-new"
    if new_tree.exists():
        shutil.rmtree(new_tree)
    new_tree.mkdir(parents=True)
    _run_ot(
        otatools,
        "fsck.erofs",
        f"--extract={new_tree}",
        "--overwrite",
        "--preserve-perms",
        new_erofs,
    )
    new_index = _index_tree(new_tree)
    only_old = sorted(set(old_index) - set(new_index))
    only_new = sorted(set(new_index) - set(old_index))
    changed = sorted(k for k in set(old_index) & set(new_index) if old_index[k] != new_index[k])
    if only_old or only_new or changed != list(SYSTEM_EXPECTED_CHANGED):
        raise SystemExit(
            f"system tree diff is not the expected 7 IFMA files: only_old={only_old}, only_new={only_new}, changed={changed}"
        )

    # Re-verify the 6 plain system ELFs and com.google.android.virt.apex from the freshly extracted system.erofs.
    verify_plain = PATCH_IFMA.run(
        new_tree,
        manifest_path,
        tmp / "ifma-system-verify.json",
        False,
        scope=("partition", "system_a"),
    )
    if not all(t["before"] == "patched" for t in verify_plain["targets"]):
        raise SystemExit("rebuilt system_a plain target not in patched state")
    verify_apex_file(
        new_tree / virt_rel,
        virt_rel,
        tmp / "ifma-verify-virt",
        otatools,
        expected_cert_der=cert_der,
        manifest_path=manifest_path,
    )
    scan_rebuilt_tree(
        new_tree, "system_a rebuilt", otatools, tmp / "ifma-scan-system-readback",
        manifest_path=manifest_path,
        provenance=_partition_provenance("system_a"),
    )
    shutil.rmtree(new_tree, ignore_errors=True)
    src_erofs.unlink(missing_ok=True)

    system_img = tmp / "system.img"
    shutil.copyfile(new_erofs, system_img)
    new_erofs.unlink(missing_ok=True)
    _run_ot(
        otatools,
        "avbtool",
        "add_hashtree_footer",
        "--image", system_img,
        "--partition_name", "system",
        "--partition_size", str(system_size),
        "--hash_algorithm", "sha256",
        "--salt", SYSTEM_SALT,
        "--block_size", "4096",
        "--fec_num_roots", "2",
        "--prop", f"com.android.build.system.os_version:{SYSTEM_OS_VERSION}",
        "--prop", f"com.android.build.system.fingerprint:{SYSTEM_FINGERPRINT}",
        "--prop", f"com.android.build.system.security_patch:{SYSTEM_SECURITY_PATCH}",
    )
    if system_img.stat().st_size != system_size:
        raise SystemExit(f"system.img size {system_img.stat().st_size} != {system_size}")

    offsets_env = tmp / "offsets.env"
    if offsets_env.is_file():
        offsets_env.write_text(
            offsets_env.read_text(encoding="utf-8")
            + f"SYSTEM_OFF={system_off}\nSYSTEM_SIZE={system_size}\n",
            encoding="utf-8",
        )

    logs_dir = tmp.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ven_plain_file = tmp / "ifma-vendor-plain.json"
    summary = {
        "schema": 1,
        "system_off": system_off,
        "system_size": system_size,
        "system_uuid": SYSTEM_UUID,
        "vendor_plain": json.loads(ven_plain_file.read_text(encoding="utf-8"))["targets"] if ven_plain_file.is_file() else [],
        "system_plain": plain_report["targets"],
        "virt_apex": virt_report["targets"],
    }
    (logs_dir / "ifma-report.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"system tree ok (offset={system_off}, size={system_size})")
    return {"system_off": system_off, "system_size": system_size, "system_img": system_img}


def rebuild_system_ext_partition(prof, img_path, tmp, otatools=None, *, manifest_path=MANIFEST_PATH):
    """Patch the one approved system_ext_a ELF and rebuild its EROFS/AVB image fail-closed."""
    if not enabled(prof):
        return None
    img_path = Path(img_path)
    tmp = Path(tmp)
    otatools = Path(otatools or os.environ["GBOS_OTATOOLS"])

    parts = _gpt_partitions(img_path)
    super_part = parts["super"]
    rel_off, partition_size = _lp_Named_extent(img_path, super_part["start"], "system_ext_a")
    if rel_off + partition_size > super_part["size"]:
        raise SystemExit("system_ext_a extent runs past the super partition")
    partition_off = super_part["start"] + rel_off

    stock_partition_img = tmp / "system_ext.stock.img"
    _copy_partition_extent(
        img_path, partition_off, partition_size, stock_partition_img,
        partition_name="system_ext_a",
    )
    try:
        _assert_system_ext_stock_avb(otatools, stock_partition_img)
    finally:
        stock_partition_img.unlink(missing_ok=True)

    src_erofs = tmp / "system_ext.erofs.src"
    _copy_erofs_extent(
        img_path, partition_off, partition_size, src_erofs, partition_name="system_ext_a",
    )
    sb = rebuild.superblock_params(src_erofs, strict_metadata=True)
    if sb["uuid"] != SYSTEM_EXT_UUID:
        raise SystemExit(
            f"unexpected system_ext_a uuid {sb['uuid']}: selected Google build {LAPIS_BUILD}"
        )

    stock_meta = _erofs_meta_map(src_erofs)
    stock_tree = tmp / "system-ext-tree"
    if stock_tree.exists():
        shutil.rmtree(stock_tree)
    stock_tree.mkdir(parents=True)
    _run_ot(
        otatools, "fsck.erofs", f"--extract={stock_tree}", "--overwrite", "--preserve-perms", src_erofs,
    )
    stock_index = _index_tree(stock_tree)

    report_path = tmp / "ifma-system-ext-plain.json"
    try:
        patch_report = PATCH_IFMA.run(
            stock_tree,
            manifest_path,
            report_path,
            True,
            scope=("partition", "system_ext_a"),
        )
    except (OSError, PATCH_IFMA.PatchError) as exc:
        raise SystemExit(f"IFMA system_ext plain patch refused: {exc}") from exc
    scan_rebuilt_tree(
        stock_tree,
        "system_ext_a patched",
        otatools,
        tmp / "ifma-scan-system-ext",
        manifest_path=manifest_path,
        provenance=_partition_provenance("system_ext_a"),
    )

    fc_file = tmp / "system_ext_file_contexts"
    fs_file = tmp / "system_ext_fs_config"
    rebuild.write_configs("/system_ext", stock_tree, stock_meta, fc_file, fs_file, allow_caps=True)
    new_erofs = tmp / "system_ext.erofs"
    new_erofs.unlink(missing_ok=True)
    mkfs_args = [
        "-z", "lz4hc,level=9",
        "-b", "4096",
        "-T", str(sb["build_time"]),
        "-U", sb["uuid"],
    ]
    if sb["volume_name"]:
        mkfs_args += ["-L", sb["volume_name"]]
    if sb["pcluster_size"]:
        mkfs_args += ["-C", str(sb["pcluster_size"])]
    rebuild.restore_mtimes(stock_tree, stock_meta)
    mkfs_args += [
        "--mount-point", "/system_ext",
        "--fs-config-file", str(fs_file),
        "--file-contexts", str(fc_file),
        str(new_erofs),
        str(stock_tree),
    ]
    _run_ot(otatools, "mkfs.erofs", *mkfs_args)
    if rebuild.fs_uuid(new_erofs) != SYSTEM_EXT_UUID:
        raise SystemExit("system_ext.erofs UUID drifted")
    _assert_same_erofs_metadata(src_erofs, new_erofs, "system_ext_a")

    shutil.rmtree(stock_tree)
    rebuilt_tree = tmp / "system-ext-tree-new"
    if rebuilt_tree.exists():
        shutil.rmtree(rebuilt_tree)
    rebuilt_tree.mkdir(parents=True)
    _run_ot(
        otatools, "fsck.erofs", f"--extract={rebuilt_tree}", "--overwrite", "--preserve-perms", new_erofs,
    )
    rebuilt_index = _index_tree(rebuilt_tree)
    only_old = sorted(set(stock_index) - set(rebuilt_index))
    only_new = sorted(set(rebuilt_index) - set(stock_index))
    changed = sorted(
        path for path in set(stock_index) & set(rebuilt_index)
        if stock_index[path] != rebuilt_index[path]
    )
    if only_old or only_new or changed != list(SYSTEM_EXT_EXPECTED_CHANGED):
        raise SystemExit(
            "system_ext tree diff is not the expected 1 IFMA file: "
            f"only_old={only_old}, only_new={only_new}, changed={changed}"
        )

    verify_report = PATCH_IFMA.run(
        rebuilt_tree,
        manifest_path,
        tmp / "ifma-system-ext-verify.json",
        False,
        scope=("partition", "system_ext_a"),
    )
    if len(verify_report["targets"]) != 1 or verify_report["targets"][0]["before"] != "patched":
        raise SystemExit("rebuilt system_ext_a target not in patched state")
    scan_rebuilt_tree(
        rebuilt_tree,
        "system_ext_a rebuilt",
        otatools,
        tmp / "ifma-scan-system-ext-readback",
        manifest_path=manifest_path,
        provenance=_partition_provenance("system_ext_a"),
    )
    shutil.rmtree(rebuilt_tree, ignore_errors=True)
    src_erofs.unlink(missing_ok=True)

    partition_img = tmp / "system_ext.img"
    shutil.copyfile(new_erofs, partition_img)
    new_erofs.unlink(missing_ok=True)
    _run_ot(
        otatools,
        "avbtool",
        "add_hashtree_footer",
        "--image", partition_img,
        "--partition_name", "system_ext",
        "--partition_size", str(partition_size),
        "--hash_algorithm", "sha256",
        "--salt", SYSTEM_EXT_SALT,
        "--block_size", "4096",
        "--fec_num_roots", "2",
        "--prop", f"com.android.build.system_ext.os_version:{SYSTEM_EXT_OS_VERSION}",
        "--prop", f"com.android.build.system_ext.fingerprint:{SYSTEM_EXT_FINGERPRINT}",
        "--prop", f"com.android.build.system_ext.security_patch:{SYSTEM_EXT_SECURITY_PATCH}",
    )
    if partition_img.stat().st_size != partition_size:
        raise SystemExit(f"system_ext.img size {partition_img.stat().st_size} != {partition_size}")

    offsets_env = tmp / "offsets.env"
    if offsets_env.is_file():
        offsets_env.write_text(
            offsets_env.read_text(encoding="utf-8")
            + f"SYSTEM_EXT_OFF={partition_off}\nSYSTEM_EXT_SIZE={partition_size}\n",
            encoding="utf-8",
        )

    logs_dir = tmp.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    summary_path = logs_dir / "ifma-report.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {"schema": 1}
    summary.update({
        "system_ext_off": partition_off,
        "system_ext_size": partition_size,
        "system_ext_uuid": SYSTEM_EXT_UUID,
        "system_ext_plain": patch_report["targets"],
    })
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"system_ext tree ok (offset={partition_off}, size={partition_size})")
    return {
        "system_ext_off": partition_off,
        "system_ext_size": partition_size,
        "system_ext_img": partition_img,
    }
