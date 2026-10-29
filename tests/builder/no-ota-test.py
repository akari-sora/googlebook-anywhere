#!/usr/bin/env python3
"""Fixture for vendor etc/init/gbos-no-ota.rc, plus synthetic QEMU log checks.

Executes the vendor.sh statements that write the rc and the statements that
build expect_new. A tree that drops the rc (the write, the mode, or the
metadata copy) fails here. builder/image.py's no_ota_log_checks is the check
the coordinator's QEMU run evaluates; this file only feeds it a
synthetic log and does not boot an image.
"""
import ast
import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder/lib"))
import devprofile

RC = "etc/init/gbos-no-ota.rc"
TRIGGER = "on property:ro.boot.slot_suffix=*"
STOP = "    stop update_engine"
LIKE = {
    "mode": 0o644,
    "uid": 0,
    "gid": 0,
    "selinux": "u:object_r:vendor_configs_file:s0",
    "caps": None,
}
PROFILES = ("kano", "generic-x86", "amd-hx370")


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


def slice_until_next(body, start, end):
    """start is unique; end is the next copy after it (the diag script is written more than once)."""
    at = one(body, start)
    nxt = body.find(end, at + len(start))
    if nxt < 0:
        fail(f"missing {end!r} after the rc writer")
    return body[at:nxt]


def load_log_checks():
    tree = ast.parse((ROOT / "builder/image.py").read_text(encoding="utf-8"))
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "no_ota_log_checks"), None)
    if fn is None:
        fail("builder/image.py has no no_ota_log_checks (coordinator QEMU checks)")
    ns = {"re": __import__("re")}
    exec(compile(ast.Module([fn], []), "image.py", "exec"), ns)
    return ns["no_ota_log_checks"]


def load_rc_rejections():
    tree = ast.parse((ROOT / "builder/image.py").read_text(encoding="utf-8"))
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "vendor_rc_rejections"), None)
    if fn is None:
        fail("builder/image.py has no vendor_rc_rejections (the generic vendor rc check)")
    ns = {"re": __import__("re")}
    exec(compile(ast.Module([fn], []), "image.py", "exec"), ns)
    return ns["vendor_rc_rejections"]


def need(label, cond):
    if not cond:
        fail(label)
    print(f"PASS {label}")


vendor = (ROOT / "builder/vendor.sh").read_text(encoding="utf-8")
docs = heredocs(vendor)
install_body = next((b for b in docs if 'etc/init/kano-bootfix.rc").chmod' in b), None)
expect_body = next((b for b in docs if "vendor uid/gid/mode/selinux/capabilities drifted" in b), None)
if install_body is None or expect_body is None:
    fail("vendor.sh heredocs missing the bootfix install or the drift check")
install_src = slice_until_next(
    install_body,
    '(tree / "etc/init/gbos-no-ota.rc").write_text(',
    '(tree / "etc/kano-diag.sh").write_text(',
)
expect_src = slice_between(
    expect_body,
    'prof = devprofile.load(os.environ["GBOS_PROFILE"])',
    "\nimport ifma\n",
)
if "stop update_engine" not in install_src or RC not in expect_src:
    fail("sliced vendor.sh text is not the no-ota rc and expect_new path")
if "override" in install_src.lower():
    fail("the rc writer contains override (init rejects it across the treble boundary)")

for profile_vendor in (ROOT / "profiles").glob("*/vendor.py"):
    if "gbos-no-ota" in profile_vendor.read_text(encoding="utf-8"):
        fail(f"{profile_vendor.relative_to(ROOT)} must not own the rc; it is generic")

saved = {k: os.environ.get(k) for k in (
    "GBOS_PROFILE", "GBOS_ADB_PUBKEY", "GBOS_EC", "GBOS_CAMERA_OVERLAY",
    "GBOS_TEST_AUTOOFF", "GBOS_DEV_KERNEL_DIR",
)}
for key in saved:
    os.environ.pop(key, None)

try:
    with tempfile.TemporaryDirectory(prefix="gbos-no-ota-") as tmp:
        tree = Path(tmp) / "vendor"
        (tree / "etc/init").mkdir(parents=True)
        (tree / "etc/init/fwupd.rc").write_text("service fwupd /bin/fwupd\n", encoding="utf-8")
        vmap = {"etc/init/fwupd.rc": dict(LIKE)}
        exec(install_src, {"tree": tree, "vmap": vmap})
        rc_path = tree / RC
        if not rc_path.is_file():
            fail("rc was not written")
        mode = stat.S_IMODE(rc_path.stat().st_mode)
        if mode != 0o644:
            fail(f"rc mode is {mode:o}, want 644")
        text = rc_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if TRIGGER not in lines:
            fail(f"rc missing {TRIGGER!r}")
        trig = lines.index(TRIGGER)
        if trig + 1 >= len(lines) or lines[trig + 1] != STOP:
            fail(f"line after the trigger is not {STOP!r}")
        if [ln for ln in lines if ln.startswith("on ")] != [TRIGGER]:
            fail(f"rc triggers are not exactly the slot-suffix property: {lines}")
        if text.lower().count("override") != 0:
            fail("rc contains override")
        if sum(1 for ln in lines if ln.strip() == "stop update_engine") != 1:
            fail("rc does not stop update_engine exactly once")
        copied = vmap.get(RC)
        if copied != vmap["etc/init/fwupd.rc"]:
            fail(f"rc metadata {copied!r} was not copied from fwupd.rc")
        if copied is vmap["etc/init/fwupd.rc"]:
            fail("rc metadata is the fwupd.rc dict, not a copy")
        print("PASS rc content, mode 0644, metadata copied from fwupd.rc, no override")

        for name in PROFILES:
            os.environ["GBOS_PROFILE"] = str(ROOT / "profiles" / name)
            ns = {
                "os": os,
                "importlib": importlib,
                "devprofile": devprofile,
                "Path": Path,
                "old": {},
            }
            exec(expect_src, ns)
            if RC not in ns["expect_new"]:
                fail(f"{name}: expect_new does not contain {RC}")
            print(f"PASS {name}: expect_new contains {RC}")
finally:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

checks = load_log_checks()
K_PARSE = "gbos-no-ota: rc parsed without errors"
K_ACT = "gbos-no-ota: action ran"
K_STOP = "gbos-no-ota: update_engine not running (never started, or stopped after its last start)"
K_GMS = "gbos-no-ota: no FATAL EXCEPTION in com.google.android.gms"
# Shapes copied from a real QEMU serial log (build 8) plus init's stop lines.
good = "\n".join([
    "<14>[    3.859488][    T1] init: Parsing file /system/etc/init/update_engine.rc...",
    "<14>[    4.002306][    T1] init: Parsing file /vendor/etc/init/gbos-no-ota.rc...",
    "<14>[   20.633787][    T1] init: processing action (ro.boot.slot_suffix=*) from (/system/etc/init/update_engine.rc:9)",
    "<14>[   20.784896][    T1] init: starting service 'update_engine'...",
    "<14>[   20.794773][    T1] init: ... started service 'update_engine' has pid 745",
    "<14>[   20.795000][    T1] init: processing action (ro.boot.slot_suffix=*) from (/vendor/etc/init/gbos-no-ota.rc:8)",
    "<14>[   20.795100][    T1] init: Sending signal 9 to service 'update_engine' (pid 745) process group...",
    "<14>[   20.799000][    T1] init: Service 'update_engine' (pid 745) received signal 9",
])
on = checks(good)
for k in (K_PARSE, K_ACT, K_STOP, K_GMS):
    need(k, on[k])
# The rejection QEMU build 8 showed (old trigger) must be red.
rejected = good + "\n<11>[    4.004541][    T1] init: /vendor/etc/init/gbos-no-ota.rc: 6: ParseTriggers() failed: unexported property trigger found: init.svc.update_engine"
need("rc rejection is red", checks(rejected)[K_PARSE] is False)
need("rc never parsed is red", checks(good.replace("Parsing file /vendor/etc/init/gbos-no-ota.rc", "Parsing file /vendor/etc/init/other.rc"))[K_PARSE] is False)
need("action not run is red", checks(good.replace("from (/vendor/etc/init/gbos-no-ota.rc:8)", "from (/vendor/etc/init/other.rc:8)"))[K_ACT] is False)
no_stop = "\n".join(l for l in good.splitlines() if "signal" not in l)
need("missing stop is red", checks(no_stop)[K_STOP] is False)
restarted = good + "\n<14>[   30.000000][    T1] init: starting service 'update_engine'..."
need("restart after the stop is red", checks(restarted)[K_STOP] is False)
# QEMU build 9: the vendor stop ran before init started the service, so it never started.
never_started = "\n".join(l for l in good.splitlines() if "update_engine' " not in l and "'update_engine'..." not in l and "signal" not in l)
need("never started after the action is green", checks(never_started)[K_STOP])
need("never started but the action did not run is red", checks(never_started.replace("from (/vendor/etc/init/gbos-no-ota.rc:8)", "from (/vendor/etc/init/other.rc:8)"))[K_STOP] is False)
started_late = never_started + "\n<14>[   40.000000][    T1] init: starting service 'update_engine'..."
need("a start with no later stop is red", checks(started_late)[K_STOP] is False)
# N2: the stop command failing is red, in both lifecycle paths (init/action.cpp's format).
stop_fail = "<14>[   20.795050][    T1] init: Command 'stop update_engine' action=ro.boot.slot_suffix=* (/vendor/etc/init/gbos-no-ota.rc:8) took 0ms and failed: no such service"
need("stop failed (never-started path) is red", checks(never_started + "\n" + stop_fail)[K_STOP] is False)
need("stop failed (stopped path) is red", checks(good + "\n" + stop_fail)[K_STOP] is False)
need("Unable to ... update_engine is red", checks(good + "\n<11>[   20.795060][    T1] init: Unable to stop service 'update_engine'")[K_STOP] is False)
only_recv = "\n".join(l for l in good.splitlines() if "Sending signal" not in l)
need("only a 'received signal' line (no init stop) is red", checks(only_recv)[K_STOP] is False)
other_sig = only_recv + "\n<14>[   20.795100][    T1] init: Sending signal 9 to service 'vold' (pid 300) process group..."
need("a signal to another service is red", checks(other_sig)[K_STOP] is False)
sigkill_name = good.replace("Sending signal 9 to", "Sending SIGKILL to")
need("current init wording (Sending SIGKILL) is green", checks(sigkill_name)[K_STOP])
truncated = "\n".join(good.splitlines()[:4])
need("truncated log (no vendor action line) is red", checks(truncated)[K_STOP] is False)
gms = good + "\nFATAL EXCEPTION: main\nProcess: com.google.android.gms, PID: 99\n"
need("gms fatal is red", checks(gms)[K_GMS] is False)
other = good + "\nFATAL EXCEPTION: main\nProcess: com.android.settings, PID: 7\n"
need("another process fatal is not a gms fatal", checks(other)[K_GMS])
distant = "Process: com.google.android.gms, PID: 3\n" + good + "\nFATAL EXCEPTION: main\nProcess: com.android.systemui, PID: 8\n"
need("gms earlier in the log is not this fatal", checks(distant)[K_GMS])
# N3: the generic check for every project vendor rc (Codex P2 #6).
rejects = load_rc_rejections()
for rc in ("kano-services.rc", "gbos-no-ota.rc"):
    msg = f"<11>[    4.004541][    T1] init: /vendor/etc/init/{rc}: 6: ParseTriggers() failed: unexported property trigger found: init.svc.update_engine"
    need(f"ParseTriggers() failed in {rc} is a rejection", rejects(msg))
for msg in (
    "init: /vendor/etc/init/kano-hw.rc: 3: unexported property trigger found: init.svc.x",
    "init: /vendor/etc/init/kano-services.rc: 9: service 'fwupd' overrides another service across the treble boundary.",
    "init: Command 'write /sys/x 1' action=boot (/vendor/etc/init/kano-services.rc:4) took 0ms and failed: open() failed",
    "init: /vendor/etc/init/kano-bootfix.rc: 2: Invalid section keyword found",
):
    need(f"rejection shape is caught: {msg[6:60]}", rejects(msg))
need("a rejection of a stock rc is not ours", not rejects("init: /vendor/etc/init/vpd.rc: 2: ParseTriggers() failed: x"))
good10 = (ROOT / "tests/builder/fixtures/vendor-init-lines-build10.txt").read_text(encoding="utf-8")
if len(good10.splitlines()) != 60:
    fail("build 10 vendor init fixture is not the 60 real lines")
need("all 60 real QEMU build 10 vendor init lines: no rejection", rejects(good10) == [])
need("build 10 lines plus a succeeded command: no rejection", rejects(good10 + "\ninit: Command 'stop update_engine' action=ro.boot.slot_suffix=* (/vendor/etc/init/gbos-no-ota.rc:8) took 0ms and succeeded") == [])
# N5: no fixed line window (Codex P3 #9); 15 kernel lines interleaved on the serial port.
kern = "\n".join(f"<6>[   50.{i:06d}][    C0] usb 1-1: kernel line {i}" for i in range(15))
need("gms fatal with 15 interleaved kernel lines is red", checks(good + "\nFATAL EXCEPTION: main\n" + kern + "\nProcess: com.google.android.gms.persistent, PID: 99\n")[K_GMS] is False)
need("other fatal with 15 interleaved lines is not gms", checks(good + "\nFATAL EXCEPTION: main\n" + kern + "\nProcess: com.android.settings, PID: 7\n")[K_GMS])
need("a gms Process line after the next FATAL belongs to that one", checks(good + "\nFATAL EXCEPTION: main\nProcess: com.android.settings, PID: 7\n" + kern + "\nFATAL EXCEPTION: main\nProcess: com.google.android.gms, PID: 9\n")[K_GMS] is False)
lc = "10-23 06:00:00.000  1234  1234 E AndroidRuntime: "
lc2 = "10-23 06:00:00.001  5678  5678 E AndroidRuntime: "
need("logcat pid pairing: a gms Process line of another pid is skipped", checks(good + "\n" + lc + "FATAL EXCEPTION: main\n" + lc2 + "Process: com.google.android.gms, PID: 5678\n" + kern + "\n" + lc + "Process: com.android.settings, PID: 1234\n")[K_GMS])
need("logcat pid pairing: same pid gms is red", checks(good + "\n" + lc + "FATAL EXCEPTION: main\n" + kern + "\n" + lc + "Process: com.google.android.gms, PID: 1234\n")[K_GMS] is False)
print("PASS QEMU log checks are synthetic only; the coordinator runs them")
print("NO-OTA OK")
