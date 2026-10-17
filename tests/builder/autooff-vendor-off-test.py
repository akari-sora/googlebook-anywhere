#!/usr/bin/env python3
"""Run the vendor.sh switch-off path.

vendor.sh exits at the audio pin check before kano_autooff.install, so this test executes the
heredoc text that actually installs the files and the text that accepts or rejects the vendor
diff. The switch is unset and empty. Both must leave the autooff script, init rc, and nonce
out of the tree, out of vmap, and out of the drift allow-list, and must not write
image/autooff.nonce. The switch-on path writes one nonce into the vendor tree and the sidecar.
"""
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import devprofile
import kano_autooff

KEYS = ("etc/kano-autooff.sh", "etc/init/kano-autooff.rc", "etc/kano-autooff.nonce")
LIKE = {"mode": 0o644, "uid": 0, "gid": 0, "selinux": "u:object_r:vendor_configs_file:s0", "caps": 0}
NONCE_META = dict(LIKE)
NONCE_META["mode"] = 0o444


def fail(msg):
    raise SystemExit(f"FAIL {msg}")


def heredocs(text):
    bodies = []
    for part in text.split("<< 'PY'\n")[1:]:
        body, sep, _rest = part.partition("\nPY\n")
        if not sep:
            fail("unclosed vendor.sh python heredoc")
        bodies.append(body)
    return bodies


def one(body, needle):
    if body.count(needle) != 1:
        fail(f"expected one {needle!r}, found {body.count(needle)}")
    return body.index(needle)


def slice_between(body, start, end):
    return body[one(body, start):one(body, end)]


def prepare(tree):
    (tree / "etc/init").mkdir(parents=True)
    (tree / "etc/init/fwupd.rc").write_text("service fwupd /bin/fwupd\n")
    return {"etc/init/fwupd.rc": dict(LIKE)}


def run_install(tree, vmap):
    ns = {"os": os, "kano_autooff": kano_autooff, "tree": tree, "vmap": vmap}
    exec(install_src, ns)


def absent(tree, vmap, label):
    for key in KEYS:
        if (tree / key).exists():
            fail(f"{label}: {key} was written")
        if key in vmap:
            fail(f"{label}: vmap has {key}")
    print(f"PASS {label}: no autooff files or vmap keys")


def expect_lists():
    ns = {
        "os": os,
        "importlib": importlib,
        "devprofile": devprofile,
        "Path": Path,
        "old": {},
    }
    exec(expect_src, ns)
    return ns


vendor = (ROOT / "builder/vendor.sh").read_text()
docs = heredocs(vendor)
first = next((b for b in docs if '_autooff = os.environ.get("GBOS_TEST_AUTOOFF", "")' in b), None)
second = next((b for b in docs if "vendor uid/gid/mode/selinux/capabilities drifted" in b), None)
if first is None or second is None:
    fail("vendor.sh heredocs missing the autooff install or the drift check")
install_src = slice_between(first, '_autooff = os.environ.get("GBOS_TEST_AUTOOFF", "")', "# adb over TCP")
expect_src = slice_between(second, 'prof = devprofile.load(os.environ["GBOS_PROFILE"])', "if only_old != expect_old")
tree_diff_src = slice_between(second, "if only_old != expect_old", "\nblock =")
drift_src = slice_between(second, "if gone != expect_old", 'print("vendor tree ok")')
if "kano_autooff.install" not in install_src or "etc/kano-autooff.sh" not in expect_src:
    fail("sliced vendor.sh text is not the install and expect_new path")

os.environ["GBOS_PROFILE"] = str(ROOT / "profiles/kano")
os.environ.pop("GBOS_ADB_PUBKEY", None)
os.environ.pop("GBOS_EC", None)
os.environ.pop("GBOS_CAMERA_OVERLAY", None)
os.environ.pop("GBOS_TEST_AUTOOFF", None)

with tempfile.TemporaryDirectory(prefix="kano-autooff-vendor-") as tmp:
    base = Path(tmp)
    for label, value in (("unset", None), ("empty", "")):
        if value is None:
            os.environ.pop("GBOS_TEST_AUTOOFF", None)
        else:
            os.environ["GBOS_TEST_AUTOOFF"] = value
        os.environ["GBOS_OUT"] = str(base / f"out-{label}")
        tree = base / label
        vmap = prepare(tree)
        run_install(tree, vmap)
        absent(tree, vmap, label)
        side = Path(os.environ["GBOS_OUT"]) / "image" / "autooff.nonce"
        if side.exists():
            fail(f"{label}: image/autooff.nonce was written")
        lists = expect_lists()
        for key in KEYS:
            if key in lists["expect_new"]:
                fail(f"{label}: drift allow-list contains {key}")
        print(f"PASS {label}: drift allow-list omits the autooff keys")

    os.environ["GBOS_TEST_AUTOOFF"] = "60"
    os.environ["GBOS_OUT"] = str(base / "out-on")
    on_tree = base / "on"
    on_vmap = prepare(on_tree)
    run_install(on_tree, on_vmap)
    for key in KEYS:
        want = NONCE_META if key.endswith(".nonce") else LIKE
        if not (on_tree / key).is_file() or on_vmap.get(key) != want:
            fail(f"on: {key} was not installed into the tree and vmap")
    vendor_nonce = (on_tree / "etc/kano-autooff.nonce").read_bytes()
    side = Path(os.environ["GBOS_OUT"]) / "image" / "autooff.nonce"
    if side.read_bytes() != vendor_nonce or not re.fullmatch(rb"[0-9a-f]{32}\n", vendor_nonce):
        fail(f"on: sidecar does not match the vendor nonce {vendor_nonce!r}")
    if (on_tree / "etc/kano-autooff.nonce").stat().st_mode & 0o777 != 0o444:
        fail("on: vendor nonce is not mode 0444")
    print("PASS on: vendor nonce matches image/autooff.nonce")
    on_lists = expect_lists()
    if any(key not in on_lists["expect_new"] for key in KEYS):
        fail("on: drift allow-list dropped the autooff keys")
    print("PASS on: the same text installs the autooff files when the switch is 60")

    os.environ.pop("GBOS_TEST_AUTOOFF", None)
    off_lists = expect_lists()
    leaked = sorted(set(off_lists["expect_new"]) | set(KEYS))
    ns = dict(off_lists)
    ns["expect_old"] = ["sentinel-old"]
    ns["only_old"] = ["sentinel-old"]
    ns["only_new"] = leaked
    ns["changed"] = list(off_lists["expect_changed"])
    try:
        exec(tree_diff_src, ns)
    except SystemExit as exc:
        if "not the expected set" not in str(exc):
            fail(f"tree diff raised the wrong error: {exc}")
    else:
        fail("switch off accepted a tree that contains the autooff files")
    print("PASS switch off rejects a tree diff that adds the autooff files")

    ns["only_new"] = list(on_lists["expect_new"])
    ns["changed"] = list(on_lists["expect_changed"])
    ns["expect_new"] = list(on_lists["expect_new"])
    ns["expect_changed"] = list(on_lists["expect_changed"])
    try:
        exec(tree_diff_src, ns)
    except SystemExit as exc:
        fail(f"switch on rejected its own allow-list: {exc}")
    print("PASS switch on accepts a tree diff that adds the autooff files")

    ns = dict(off_lists)
    ns["expect_old"] = ["sentinel-old"]
    ns["gone"] = ["sentinel-old"]
    ns["extra"] = sorted(set(off_lists["expect_new"]) | {"etc/kano-autooff.nonce"})
    ns["bad"] = []
    try:
        exec(drift_src, ns)
    except SystemExit as exc:
        if str(exc) != "vendor uid/gid/mode/selinux/capabilities drifted":
            fail(f"metadata drift raised the wrong error: {exc}")
    else:
        fail("switch off accepted metadata that adds etc/kano-autooff.sh")
    ns["extra"] = list(off_lists["expect_new"])
    ns["bad"] = ["etc/init/kano-autooff.rc"]
    try:
        exec(drift_src, ns)
    except SystemExit as exc:
        if str(exc) != "vendor uid/gid/mode/selinux/capabilities drifted":
            fail(f"metadata drift raised the wrong error: {exc}")
    else:
        fail("switch off accepted a metadata change of etc/init/kano-autooff.rc")
    print("PASS switch off rejects autooff metadata drift")

print("VENDOR OFF OK")
