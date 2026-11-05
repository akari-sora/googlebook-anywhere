#!/usr/bin/env python3
"""Build selection must retain the reviewed IFMA scope and reject identity drift."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
passed = 0

def run(lib, build=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("GBOS_LAPIS_BUILD", None)
    if build is not None:
        env["GBOS_LAPIS_BUILD"] = build
    code = """import sys,json;sys.path.insert(0,sys.argv[1]);import ifma
print(json.dumps(dict(build=ifma.LAPIS_BUILD,uuid=ifma.VENDOR_UUID,rollback=ifma.VBMETA_ROLLBACK_INDEX,fingerprint=ifma.SYSTEM_FINGERPRINT,targets=len(ifma.PATCH_IFMA._load_manifest(ifma.MANIFEST_PATH)),default=str(ifma.scan_rebuilt_tree.__kwdefaults__["manifest_path"]),manifest=str(ifma.MANIFEST_PATH))))
"""
    return subprocess.run([sys.executable, "-I", "-B", "-c", code, str(lib)],
                          env=env, capture_output=True, text=True)

def check(cond, name):
    global passed
    if not cond:
        raise SystemExit("FAIL " + name)
    passed += 1
    print("PASS " + name)

old = run(ROOT / "builder/lib")
check(old.returncode == 0 and json.loads(old.stdout)["rollback"] == 1339, "default retains old build and rollback")
new = run(ROOT / "builder/lib", "16658739")
check(new.returncode == 0, "new build imports")
data = json.loads(new.stdout)
check(data["build"] == "16658739" and data["rollback"] == 1369, "new build preserves measured rollback")
check(data["uuid"] == "4d286de7-7133-55bf-8283-b414f127b88a", "new vendor identity selected")
check(data["fingerprint"] == "Android/desktop_system/desktop:17/CL3B.260622.276.R4/16658739:user/release-keys", "measured system footer fingerprint selected")
check(data["targets"] == 17 and data["default"] == data["manifest"], "all17 reviewed targets and captured scanner defaults retained")
check(Path(data["manifest"]).name == "16658739.manifest.json", "new build selects its complete residual manifest")
check(Path(json.loads(old.stdout)["manifest"]).name == "manifest.json", "old build retains original residual manifest")
old_policy = json.loads((ROOT / "builder/ifma/manifest.json").read_text())
new_policy = json.loads(Path(data["manifest"]).read_text())
# Only the four reviewed container SHA fields may differ. Exact executable pins,
# paths, offsets, contexts and acceptance reasons must remain unchanged.
old_policy["residuals"][0]["container_chain"][2]["sha256"] = "4bd5c1c8833f9076af702e675dc1c6e58c78b8a68590b4f9544a1b29482640e4"
for row in (old_policy["residuals"][7], old_policy["residuals"][8]):
    row["container_chain"][2]["sha256"] = "ab11a15a034c43cfc204169bfd27b46723d65260b0171f5db8e15184d1866557"
old_policy["unsupported_allowlist"][0]["sha256"] = "5315ebd6901df35604e25c7cd88679f1c76899f360a375bc5e1888c4fbf9ea50"
check(new_policy == old_policy, "only four reviewed nested-container hashes differ; executable and acceptance scope unchanged")
for bad in ("", "99999999", "../16658739", "016658739"):
    result = run(ROOT / "builder/lib", bad)
    check(result.returncode != 0 and "no IFMA/partition pins" in result.stderr, "unsupported build rejected " + repr(bad))

with tempfile.TemporaryDirectory(prefix="lapis-ifma-pins-") as td:
    tree = Path(td)
    shutil.copytree(ROOT / "builder/lib", tree / "lib")
    shutil.copytree(ROOT / "builder/ifma", tree / "ifma")
    pin = tree / "ifma/builds/16658739.json"
    original = json.loads(pin.read_text())
    for label, mutate in (
        ("wrong build identity", lambda p: p.update(build="16452207")),
        ("unknown field", lambda p: p.update(extra="ignored")),
        ("boolean rollback", lambda p: p.update(VBMETA_ROLLBACK_INDEX=True)),
        ("regressing rollback", lambda p: p.update(VBMETA_ROLLBACK_INDEX=1338)),
        ("changed reviewed manifest", lambda p: p.update(manifest_sha256="0" * 64)),
        ("missing APEX salt", lambda p: p["apex_salts"].pop(next(iter(p["apex_salts"])))),
    ):
        altered = json.loads(json.dumps(original))
        mutate(altered)
        pin.write_text(json.dumps(altered))
        result = run(tree / "lib", "16658739")
        check(result.returncode != 0, label + " rejected")
    pin.write_text(json.dumps(original))
    manifest = tree / "ifma/builds/16658739.manifest.json"
    manifest.write_text(manifest.read_text() + "\n")
    result = run(tree / "lib", "16658739")
    check(result.returncode != 0 and "manifest differs" in result.stderr, "modified selected manifest rejected before use")
    pin.unlink()
    result = run(tree / "lib", "16658739")
    check(result.returncode != 0 and "cannot load Google build pins" in result.stderr, "missing build pins rejected")

env = dict(os.environ, GBOS_LAPIS_BUILD="16658739", PYTHONDONTWRITEBYTECODE="1")
code = """import sys;sys.path.insert(0,sys.argv[1]);import ifma
actual={"partition_name":"system_ext","salt":ifma.SYSTEM_EXT_SALT,"props":{"com.android.build.system_ext.os_version":ifma.SYSTEM_EXT_OS_VERSION,"com.android.build.system_ext.fingerprint":ifma.SYSTEM_EXT_FINGERPRINT,"com.android.build.system_ext.security_patch":ifma.SYSTEM_EXT_SECURITY_PATCH},"hashtree":dict(ifma.SYSTEM_EXT_HASHTREE)}
ifma._run_ot=lambda *args:""
ifma._parse_partition_avb_info=lambda *args:actual
ifma._assert_system_ext_stock_avb(None,None)
actual["props"]["com.android.build.system_ext.fingerprint"]="wrong-base"
try:ifma._assert_system_ext_stock_avb(None,None)
except SystemExit as exc:
 assert "identity drifted" in str(exc)
else:raise AssertionError("wrong base accepted")
"""
result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "builder/lib")],
                        env=env, capture_output=True, text=True)
check(result.returncode == 0, "selected AVB identity accepts exact source and rejects wrong base")

pastel_code = """import sys;sys.path.insert(0,sys.argv[1]);import devprofile,ifma
_,non_default,_=ifma._manifest_scan_policy(ifma.MANIFEST_PATH)
_,non_pastel,_=ifma._manifest_scan_policy(ifma.MANIFEST_PATH,include_pastel=True)
assert not any(ifma._is_pastel_vendor_pin(r) for r in non_default)
pastel_pins=[r for r in non_pastel if ifma._is_pastel_vendor_pin(r)]
assert len(pastel_pins)==1 and pastel_pins[0]["file_offset"]=="0x17a50ef" and pastel_pins[0]["encoding_hex"]=="c40289b508"
assert not ifma._vendor_has_pastel(devprofile.load(sys.argv[2]))
assert ifma._vendor_has_pastel(devprofile.load(sys.argv[3]))
assert ifma._vendor_has_pastel(devprofile.load(sys.argv[4]))
assert not ifma._vendor_has_pastel({"display": None, "model": None})
"""
result = subprocess.run(
    [
        sys.executable, "-I", "-B", "-c", pastel_code,
        str(ROOT / "builder/lib"),
        str(ROOT / "profiles/kano"),
        str(ROOT / "profiles/amd-hx370"),
        str(ROOT / "profiles/generic-x86"),
    ],
    env=env, capture_output=True, text=True,
)
check(result.returncode == 0, "vulkan.pastel.so vendor_a non-instruction pin scoped to pastel-bearing rebuilt vendor trees")
print(f"lapis IFMA pins: {passed} passed, 0 failed")
