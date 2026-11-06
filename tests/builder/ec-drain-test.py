#!/usr/bin/env python3
"""GBOS_EC=drain: KANO-only experimental variant. No hardware, no image build.

The profile blacklist is not edited. A KANO build with GBOS_EC unset is typec.
GBOS_EC=none and an explicit empty value are the opt-out (every entry unchanged).
Later sections lock the parsers, the typec default, the two-module drain release,
the vendor no-op, the QEMU assertions and the watcher policy.
"""
import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder" / "lib"))
import devprofile  # noqa: E402 - the repository-local import requires sys.path above.

pass_n = 0
fail_n = 0


def check(name, cond):
    global pass_n, fail_n
    if cond:
        print(f"PASS {name}")
        pass_n += 1
    else:
        print(f"FAIL {name}")
        fail_n += 1


# Item 1: the KANO profile blacklist still lists the EC transport/core and every consumer.
# The image builder, not the profile, releases the typec set when GBOS_EC is unset.
# GBOS_EC is not a profile key; power_button stays the ACPI button.
kano = devprofile.load(ROOT / "profiles" / "kano")
safety = devprofile.load_safety()
mods = list(kano["blacklist"]["modules"])
required = list(safety["blacklist"]["required"])
STILL_BLOCKED = (
    "cros_ec", "cros_ec_lpcs", "cros_ec_dev", "cros_ec_keyb", "cros_ec_chardev",
    "cros_ec_sysfs", "cros_ec_debugfs",
)
check("KANO profile blacklist still lists transport, MFD, keyb and userspace EC drivers",
      all(m in mods for m in STILL_BLOCKED))
check("cros_ec_proto stays allowed (not on the KANO blacklist)", "cros_ec_proto" not in mods)
check("safety required still blocks cros_ec and cros_ec_lpcs",
      "cros_ec" in required and "cros_ec_lpcs" in required and "cros_ec_dev" in required)
check("KANO power_button stays acpi", kano["input"]["power_button"] == "acpi")
check("profile.toml does not set GBOS_EC", "GBOS_EC" not in (ROOT / "profiles" / "kano" / "profile.toml").read_text())
check("KANO early-boot modprobe list does not name an EC module",
      devprofile.module_boot_lines(kano) == "" and not kano["modules"]["load"])


def run(cmd, env):
    return subprocess.run(cmd, env=env, text=True, capture_output=True)


def base_env(tmp, profile):
    env = os.environ.copy()
    env.update({
        "GBOS_BASE": str(tmp / "base.img"),
        "GBOS_INITRD": str(tmp / "initrd.img"),
        "GBOS_OTATOOLS": str(tmp / "ota"),
        "GBOS_AVB_KEY": str(tmp / "key.pem"),
        "GBOS_INPUTS": str(tmp / "in"),
        "GBOS_OUT": str(tmp / "out"),
        "GBOS_PROFILE": str(ROOT / "profiles" / profile),
        "GBOS_FIRMWARE": str(tmp / "in"),
        "GBOS_AUDIO_SRC": str(tmp / "in"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(tmp / "pyc"),
    })
    env.pop("GBOS_EC", None)
    return env


def parser_refuses(script, ec, profile="kano", needle="GBOS_EC must be"):
    """True when the preflight refuses this GBOS_EC before building."""
    with tempfile.TemporaryDirectory(prefix="gbos-ec-") as raw:
        tmp = Path(raw)
        (tmp / "base.img").write_bytes(b"x")
        (tmp / "initrd.img").write_bytes(b"x")
        (tmp / "key.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
        (tmp / "ota").mkdir()
        (tmp / "in").mkdir()
        env = base_env(tmp, profile)
        if ec is not None:
            env["GBOS_EC"] = ec
        r = run(["bash", str(script)], env)
        text = r.stdout + r.stderr
        return r.returncode == 2 and needle in text


ACCEPT = ("", "none", "drain", "sensors", "typec", "buttons", "sensors,typec", "typec,buttons",
          "buttons,sensors,typec")
REFUSE = ("drain,sensors", "sensors,drain", "buttons,drain", "drain,buttons", "drain,drain",
          "DRAIN", "drain ", " sensors", "sensors,sensors", "sensors,", "sensors,,typec",
          ",sensors", "sensors,chardev", "sensors,typec,sensors", "drain,buttons,sensors")

check("build.sh accepts empty, drain and the original feature lists",
      all(not parser_refuses(ROOT / "builder" / "build.sh", ec) for ec in ACCEPT))
check("build.sh refuses mixed drain, duplicates, empty items and unknown items",
      all(parser_refuses(ROOT / "builder" / "build.sh", ec) for ec in REFUSE))
check("vendor.sh accepts empty, drain and the original feature lists",
      all(not parser_refuses(ROOT / "builder" / "vendor.sh", ec) for ec in ACCEPT))
check("vendor.sh refuses mixed drain, duplicates, empty items and unknown items",
      all(parser_refuses(ROOT / "builder" / "vendor.sh", ec) for ec in REFUSE))
check("build.sh keeps GBOS_EC KANO-only",
      parser_refuses(ROOT / "builder" / "build.sh", "drain", profile="generic-x86",
                     needle="GBOS_EC is audited for the KANO only")
      and parser_refuses(ROOT / "builder" / "build.sh", "sensors", profile="generic-x86",
                         needle="GBOS_EC is audited for the KANO only"))
check("vendor.sh keeps GBOS_EC KANO-only",
      parser_refuses(ROOT / "builder" / "vendor.sh", "drain", profile="generic-x86",
                     needle="GBOS_EC is audited for the KANO only")
      and parser_refuses(ROOT / "builder" / "vendor.sh", "buttons,typec", profile="generic-x86",
                         needle="GBOS_EC is audited for the KANO only")
      and parser_refuses(ROOT / "builder" / "build.sh", "typec", profile="generic-x86",
                         needle="GBOS_EC is audited for the KANO only")
      and parser_refuses(ROOT / "builder" / "vendor.sh", "typec", profile="amd-hx370",
                         needle="GBOS_EC is audited for the KANO only"))


def parser_text(script, ec, profile="kano"):
    """(exit, output) of a preflight. ec is None when GBOS_EC is unset."""
    with tempfile.TemporaryDirectory(prefix="gbos-ec-") as raw:
        tmp = Path(raw)
        (tmp / "base.img").write_bytes(b"x")
        (tmp / "initrd.img").write_bytes(b"x")
        (tmp / "key.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
        (tmp / "ota").mkdir()
        (tmp / "in").mkdir()
        env = base_env(tmp, profile)
        if ec is not None:
            env["GBOS_EC"] = ec
        r = run(["bash", str(script)], env)
        return r.returncode, r.stdout + r.stderr


def resolved_note(script, ec, profile="kano"):
    _rc, text = parser_text(script, ec, profile)
    return text


for label, path in (("build.sh", ROOT / "builder" / "build.sh"),
                    ("vendor.sh", ROOT / "builder" / "vendor.sh")):
    unset_kano = resolved_note(path, None, "kano")
    empty_kano = resolved_note(path, "", "kano")
    none_kano = resolved_note(path, "none", "kano")
    unset_generic = resolved_note(path, None, "generic-x86")
    check(f"{label} unset on KANO resolves to typec",
          "NOTE: GBOS_EC=typec: this image lets audited EC drivers talk to the EC" in unset_kano
          and "GBOS_EC must be" not in unset_kano)
    check(f"{label} explicit empty and none on KANO opt out",
          "NOTE: GBOS_EC=none: the EC stays unreachable" in empty_kano
          and "NOTE: GBOS_EC=none: the EC stays unreachable" in none_kano
          and "lets audited EC drivers" not in empty_kano
          and "lets audited EC drivers" not in none_kano)
    check(f"{label} unset on generic-x86 stays none and does not apply typec",
          "NOTE: GBOS_EC=none: the EC stays unreachable" in unset_generic
          and "GBOS_EC=typec" not in unset_generic
          and "GBOS_EC is audited for the KANO only" not in unset_generic)


def shell_accepts_drain_buttons(script_text):
    """A mutant that treats any drain* value as drain. True when drain,buttons is no longer refused."""
    old = '[ "$ec" = "drain" ]'
    new = '[[ "$ec" == drain* ]]'
    if script_text.count(old) != 1:
        return None
    mutated = script_text.replace(old, new, 1)
    with tempfile.TemporaryDirectory(prefix="gbos-ec-mut-") as raw:
        tmp = Path(raw)
        (tmp / "base.img").write_bytes(b"x")
        (tmp / "initrd.img").write_bytes(b"x")
        (tmp / "key.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
        (tmp / "ota").mkdir()
        (tmp / "in").mkdir()
        script = tmp / "parser.sh"
        script.write_text(mutated)
        script.chmod(0o755)
        env = base_env(tmp, "kano")
        env["GBOS_EC"] = "drain,buttons"
        # The mutated build.sh/vendor.sh still refer to $HERE as their own directory.
        # Run them from a copy that sits next to the real builder/lib via the original path:
        # replace only the predicate, but execute the original file's directory by overlaying
        # the function. Easiest: run bash on the mutated copy; HERE is the copy's dir, so
        # devprofile.py will be missing and a KANO check would fail closed. The refuse path
        # for drain,buttons returns before any profile read when the predicate rejects it.
        r = run(["bash", str(script)], env)
        text = r.stdout + r.stderr
        refused = r.returncode == 2 and "GBOS_EC must be" in text
        return not refused


for label, path in (("build.sh", ROOT / "builder" / "build.sh"),
                    ("vendor.sh", ROOT / "builder" / "vendor.sh")):
    accepted = shell_accepts_drain_buttons(path.read_text())
    if accepted is None:
        check(f"mutation anchor once: {label} accepts drain,buttons", False)
    elif accepted:
        print(f"RED {label} accepts drain,buttons")
        check(f"mutation shown red: {label} accepts drain,buttons", True)
    else:
        check(f"mutation still green: {label} accepts drain,buttons", False)


def load_image_ec(text):
    """Exec the EC parser and blacklist helpers (and their constants) from image.py source."""
    tree = ast.parse(text)
    keep_fn = {"parse_gbos_ec", "ec_released", "ec_blacklist", "ec_variant_entries"}
    keep_asg = {"EC_CORE", "EC_RELEASE", "EC_EXTRA_BLOCK", "EC_NEVER", "EC_DRAIN", "DRAIN_DEBUG_DROP"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in keep_fn:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            ids = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(i in keep_asg for i in ids):
                nodes.append(node)
    missing = keep_fn - {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    if missing:
        raise SystemExit(f"image.py EC helpers missing: {sorted(missing)}")
    ns = {"re": re}
    exec(compile(ast.Module(nodes, []), "image.py", "exec"), ns)
    return ns


def load_real_vcmd():
    tree = ast.parse((ROOT / "tests" / "builder" / "profile-test.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "REAL_VCMD" for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit("REAL_VCMD not found")


IMAGE_NS = load_image_ec((ROOT / "builder" / "image.py").read_text(encoding="utf-8"))
REAL_VCMD = load_real_vcmd()
ENTRIES = devprofile.make_entries(kano, REAL_VCMD)
PAIR = {"cros_ec", "cros_ec_lpcs"}
EXTRA = ("gpio_cros_ec", "cros_ec_hwmon", "cros_ec_wdt", "rtc_cros_ec", "cros_ec_uart", "cros_ec_rpmsg")
STILL = {"cros_ec_dev", "cros_ec_keyb"} | set(IMAGE_NS["EC_NEVER"])


def parse_refuses(ns, raw, model="kano"):
    try:
        ns["parse_gbos_ec"](raw, model)
    except SystemExit as e:
        return "GBOS_EC must be" in str(e)
    return False


def _kano_only(ns, raw):
    try:
        ns["parse_gbos_ec"](raw, "generic-x86")
    except SystemExit as e:
        return "GBOS_EC is audited for the KANO only" in str(e)
    return False


check("image.py accepts empty, none, drain and the original feature lists",
      IMAGE_NS["parse_gbos_ec"]("", "kano") == []
      and IMAGE_NS["parse_gbos_ec"]("none", "kano") == []
      and IMAGE_NS["parse_gbos_ec"]("none", "generic-x86") == []
      and IMAGE_NS["parse_gbos_ec"](None, "generic-x86") == []
      and IMAGE_NS["parse_gbos_ec"]("drain", "kano") == ["drain"]
      and all(IMAGE_NS["parse_gbos_ec"](ec, "kano") == ec.split(",")
               for ec in ACCEPT if ec not in ("", "none", "drain")))
check("unset GBOS_EC on KANO is typec",
      IMAGE_NS["parse_gbos_ec"](None, "kano") == ["typec"]
      and IMAGE_NS["parse_gbos_ec"]("typec", "kano") == ["typec"])
check("image.py refuses mixed drain, duplicates, empty items and unknown items",
      all(parse_refuses(IMAGE_NS, ec) for ec in REFUSE))
check("image.py keeps GBOS_EC KANO-only",
      all(_kano_only(IMAGE_NS, raw) for raw in ("drain", "sensors", "typec", "typec,buttons"))
      and not _kano_only(IMAGE_NS, "none")
      and not _kano_only(IMAGE_NS, ""))


def bl_of(opts):
    toks = [o for o in opts if o.startswith("module_blacklist=")]
    return toks, (toks[0].split("=", 1)[1].split(",") if len(toks) == 1 else None)


def drain_entry_ok(ns, base):
    """True when this entry releases exactly the two transport modules and nothing else."""
    out = ns["ec_blacklist"](base, ["drain"])
    toks, bls = bl_of(out)
    base_toks, base_bl = bl_of(base)
    if len(toks) != 1 or bls is None or base_bl is None:
        return False
    if set(bls) != (set(base_bl) - PAIR) | set(EXTRA):
        return False
    removed = [m for m in base_bl if m not in set(bls)]
    if set(removed) != PAIR or len(removed) != 2:
        return False
    if not STILL <= set(bls) or PAIR & set(bls) or not set(EXTRA) <= set(bls):
        return False
    if any(o.startswith("spi-intel.") for o in out) or "efi=noruntime" not in out:
        return False
    if [o for o in out if o.startswith("efi=")] != ["efi=noruntime"]:
        return False
    # install/update derivative: only nvme_init is lifted; the blacklist token stays
    lifted = " ".join(
        "initcall_blacklist=mei_me_driver_init" if t == "initcall_blacklist=nvme_init,mei_me_driver_init" else t
        for t in out)
    if "module_blacklist=" + ",".join(bls) not in lifted.split():
        return False
    return True


check("GBOS_EC=none leaves every KANO entry unchanged",
      all(IMAGE_NS["ec_blacklist"](ENTRIES[name], []) == list(ENTRIES[name]) for name in ("normal", "debug", "quiet"))
      and IMAGE_NS["ec_released"]([]) == set())
TYPEC_RELEASED = set(IMAGE_NS["EC_CORE"]) | set(IMAGE_NS["EC_RELEASE"]["typec"])
STAY_BLOCKED = ("thunderbolt", "cros_typec_switch", "cros_ec_ucsi", "cros_usbpd_charger")


def typec_entry_ok(ns, base):
    out = ns["ec_blacklist"](base, ["typec"])
    _toks, bls = bl_of(out)
    _base_toks, base_bl = bl_of(base)
    if bls is None or base_bl is None:
        return False
    if set(bls) != (set(base_bl) - TYPEC_RELEASED) | set(EXTRA):
        return False
    if TYPEC_RELEASED & set(bls) or not set(EXTRA) <= set(bls):
        return False
    if any(name not in bls for name in STAY_BLOCKED):
        return False
    return "efi=noruntime" in out and not any(o.startswith("spi-intel.") for o in out)


typec_normal, typec_debug, typec_quiet = IMAGE_NS["ec_variant_entries"](
    ENTRIES["normal"], ENTRIES["debug"], ENTRIES["quiet"], ["typec"])
check("KANO default typec releases EC_CORE plus the typec drivers and keeps the writers blocked",
      all(typec_entry_ok(IMAGE_NS, ENTRIES[name]) for name in ("normal", "debug", "quiet"))
      and IMAGE_NS["ec_released"](["typec"]) == TYPEC_RELEASED
      and TYPEC_RELEASED == {"cros_ec", "cros_ec_lpcs", "cros_ec_dev", "cros_ec_typec", "cros_usbpd_notify"})
check("KANO default typec cmdline stays inside the documented limit",
      devprofile.boot_cmdline_len(typec_debug) == 1892
      and devprofile.boot_cmdline_len(typec_quiet) == 1800
      and devprofile.cmdline_within_limit(devprofile.boot_cmdline_len(typec_debug))
      and devprofile.cmdline_within_limit(devprofile.boot_cmdline_len(typec_quiet))
      and "ignore_loglevel" in typec_debug and "loglevel=8" in typec_debug
      and "earlycon=uart8250,mmio32,0xfe03e000,115200n8" in typec_debug
      and "keep_bootcon" in typec_debug and "printk.devkmsg=on" in typec_debug
      and "loglevel=4" in typec_quiet and "ignore_loglevel" not in typec_quiet)
check("drain releases exactly cros_ec and cros_ec_lpcs on normal, quiet and serial-debug",
      all(drain_entry_ok(IMAGE_NS, ENTRIES[name]) for name in ("normal", "debug", "quiet")))
drain_normal, drain_debug, drain_quiet = IMAGE_NS["ec_variant_entries"](
    ENTRIES["normal"], ENTRIES["debug"], ENTRIES["quiet"], ["drain"])
check("drain cmdline stays inside the documented limit",
      devprofile.cmdline_within_limit(devprofile.boot_cmdline_len(drain_debug))
      and devprofile.cmdline_within_limit(devprofile.boot_cmdline_len(drain_quiet))
      and "ignore_loglevel" not in drain_debug and "loglevel=8" not in drain_debug
      and "earlycon=uart8250,mmio32,0xfe03e000,115200n8" in drain_debug
      and "keep_bootcon" in drain_debug and "printk.devkmsg=on" in drain_debug
      and "loglevel=4" in drain_quiet and "ignore_loglevel" not in drain_quiet)
sensors_normal, sensors_debug, sensors_quiet = IMAGE_NS["ec_variant_entries"](
    ENTRIES["normal"], ENTRIES["debug"], ENTRIES["quiet"], ["sensors"])
check("feature variants keep serial-debug verbosity tokens",
      "ignore_loglevel" in sensors_debug and "loglevel=8" in sensors_debug)
sensors_rel = IMAGE_NS["ec_released"](["sensors", "typec", "buttons"])
check("feature variants still release EC_CORE (including cros_ec_dev) plus their drivers",
      sensors_rel == set(IMAGE_NS["EC_CORE"]) | set(IMAGE_NS["EC_RELEASE"]["sensors"])
      | set(IMAGE_NS["EC_RELEASE"]["typec"]) | set(IMAGE_NS["EC_RELEASE"]["buttons"])
      and "cros_ec_dev" in sensors_rel and "cros_ec_keyb" in sensors_rel
      and "cros_ec_chardev" not in sensors_rel)


def image_mutant(old, new):
    text = (ROOT / "builder" / "image.py").read_text(encoding="utf-8")
    if text.count(old) != 1:
        return None
    return load_image_ec(text.replace(old, new, 1))


drain_dev = image_mutant(
    'EC_DRAIN = {"cros_ec", "cros_ec_lpcs"}',
    'EC_DRAIN = {"cros_ec", "cros_ec_lpcs", "cros_ec_dev"}')
if drain_dev is None:
    check("mutation anchor once: drain also releases cros_ec_dev", False)
elif any(drain_entry_ok(drain_dev, ENTRIES[name]) for name in ("debug", "quiet")):
    check("mutation still green: drain also releases cros_ec_dev", False)
else:
    print("RED drain also releases cros_ec_dev")
    check("mutation shown red: drain also releases cros_ec_dev", True)

drain_mix = image_mutant(
    'elif raw == "drain":\n        features = ["drain"]',
    'elif raw == "drain" or (raw.startswith("drain,") and raw.count(",") == 1):\n'
    '        features = ["drain"] if raw == "drain" else raw.split(",")')
if drain_mix is None:
    check("mutation anchor once: image.py accepts drain,buttons", False)
elif parse_refuses(drain_mix, "drain,buttons"):
    check("mutation still green: image.py accepts drain,buttons", False)
else:
    print("RED image.py accepts drain,buttons")
    check("mutation shown red: image.py accepts drain,buttons", True)

def load_vendor(path):
    spec = importlib.util.spec_from_file_location("kano_vendor_ec", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VENDOR = load_vendor(ROOT / "profiles" / "kano" / "vendor.py")
check("drain and the opt-out change no vendor file",
      VENDOR.ec_vendor_features("drain") == []
      and VENDOR.ec_vendor_features("") == []
      and VENDOR.ec_vendor_features("none") == []
      and VENDOR.ec_expected(["drain"]) == ([], [])
      and VENDOR.ec_vendor_features("typec") == ["typec"]
      and VENDOR.ec_expected(["typec"]) == ([], []))
check("feature lists still name their vendor files",
      VENDOR.ec_vendor_features("sensors,typec") == ["sensors", "typec"]
      and VENDOR.ec_vendor_features("buttons") == ["buttons"]
      and VENDOR.ec_expected(["sensors"]) == ([], ["overlay/framework-res__lapis__auto_generated_rro_vendor.apk"])
      and VENDOR.ec_expected(["buttons"]) == (["usr/keylayout", "usr/keylayout/cros_ec_buttons.kl"], [])
      and VENDOR.ec_expected(["typec"]) == ([], []))


def vendor_refuses_mixed(fn):
    try:
        fn("drain,buttons") if fn == VENDOR.ec_vendor_features else fn(["drain", "buttons"])
    except SystemExit as e:
        return "GBOS_EC=drain is exclusive" in str(e)
    return False


check("vendor hooks refuse mixed drain",
      vendor_refuses_mixed(VENDOR.ec_vendor_features)
      and vendor_refuses_mixed(VENDOR.ec_expected))


class _Ctx:
    def __init__(self, tree):
        self.tree = tree
        self.vmap = {"usr/idc": {"mode": 0o755}, "usr/idc/Elan_Touchpad.idc": {"mode": 0o644}}


def tree_paths(tree):
    return sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*"))


with tempfile.TemporaryDirectory(prefix="gbos-ec-vendor-") as raw:
    tree = Path(raw) / "drain"
    tree.mkdir()
    VENDOR.ec(_Ctx(tree), ["drain"])
    drain_paths = tree_paths(tree)
    mixed = Path(raw) / "mixed"
    mixed.mkdir()
    try:
        VENDOR.ec(_Ctx(mixed), ["drain", "buttons"])
        mixed_refused = False
    except SystemExit as e:
        mixed_refused = "GBOS_EC=drain is exclusive" in str(e)
    mixed_paths = tree_paths(mixed)
    buttons = Path(raw) / "buttons"
    (buttons / "usr").mkdir(parents=True)
    VENDOR.ec(_Ctx(buttons), ["buttons"])
    button_paths = tree_paths(buttons)
check("drain writes no overlay, IIO label, or keylayout", drain_paths == [] and mixed_refused and mixed_paths == [])
check("buttons still writes only the volume keylayout",
      button_paths == ["usr", "usr/keylayout", "usr/keylayout/cros_ec_buttons.kl"])
vsh = (ROOT / "builder" / "vendor.sh").read_text()
check("vendor.sh skips the vendor hook for drain",
      vsh.count("hooks.ec_vendor_features(") == 2
      and 'EC = [f for f in os.environ.get("GBOS_EC", "").split(",") if f]' not in vsh)


def vendor_mutant_writes_keylayout():
    """Drop the exclusive-drain refuse inside ec(). True when drain,buttons then writes the keylayout."""
    text = (ROOT / "profiles" / "kano" / "vendor.py").read_text(encoding="utf-8")
    old = '    if "drain" in features:\n        raise SystemExit("GBOS_EC=drain is exclusive")\n    import struct, zipfile, zlib'
    if text.count(old) != 1:
        return None
    mutated = text.replace(old, "    import struct, zipfile, zlib", 1)
    with tempfile.TemporaryDirectory(prefix="gbos-ec-vmut-") as raw:
        path = Path(raw) / "vendor.py"
        path.write_text(mutated)
        mod = load_vendor(path)
        tree = Path(raw) / "tree"
        (tree / "usr").mkdir(parents=True)
        try:
            mod.ec(_Ctx(tree), ["drain", "buttons"])
        except SystemExit:
            return False
        return (tree / "usr/keylayout/cros_ec_buttons.kl").is_file()


wrote = vendor_mutant_writes_keylayout()
if wrote is None:
    check("mutation anchor once: vendor ec() accepts drain,buttons", False)
elif not wrote:
    check("mutation still green: vendor ec() accepts drain,buttons", False)
else:
    print("RED vendor ec() accepts drain,buttons")
    check("mutation shown red: vendor ec() accepts drain,buttons", True)

def load_ec_selftest(text=None):
    """Exec ec_selftest_checks plus the EC lists it reads. image.py itself is not imported."""
    if text is None:
        text = (ROOT / "builder" / "image.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    keep_fn = {"ec_selftest_checks"}
    keep_asg = {"EC_CORE", "EC_RELEASE", "EC_EXTRA_BLOCK", "EC_NEVER", "EC_DRAIN"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in keep_fn:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            ids = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(i in keep_asg for i in ids):
                nodes.append(node)
    ns = {"re": re}
    exec(compile(ast.Module(nodes, []), "image.py", "exec"), ns)
    return ns["ec_selftest_checks"]


EC_LOG = load_ec_selftest()
PROBE = "cros_ec_lpcs GOOG0004:00: EC ID not detected\n"
UNBOUND = "platform cros-ec-dev: unbound platform device cros-ec-dev.0\n"


def ko(stem):
    return f"Loaded kernel module /vendor/lib/modules/{stem}.ko\n"


drain_log = ko("cros_ec") + ko("cros_ec_lpcs") + ko("iwlwifi") + ko("cros_ec_proto") + PROBE + UNBOUND
drain_checks = EC_LOG(drain_log, ["drain"])
check("drain QEMU log accepts the transport pair, the probe give-up, and an unbound cros-ec-dev device",
      all(drain_checks.values())
      and "not a HOST_SLEEP pass" in " ".join(drain_checks)
      and "HOST_SLEEP passed" not in " ".join(drain_checks)
      and "drain passed" not in " ".join(drain_checks))
check("drain QEMU log rejects MFD, keyb and userspace drivers, a missing probe, and /dev/cros_ec",
      all(not all(EC_LOG(drain_log + ko(stem), ["drain"]).values())
          for stem in ("cros_ec_dev", "cros-ec-dev", "cros_ec_keyb", "cros-ec-keyb",
                       "cros_ec_chardev", "cros_ec_sysfs", "cros_ec_debugfs"))
      and not EC_LOG(ko("cros_ec") + ko("cros_ec_lpcs") + UNBOUND, ["drain"])[
          "EC variant: cros_ec_lpcs loaded, probed GOOG0004, gave up (no EC in QEMU)"]
      and not EC_LOG(drain_log + "/dev/cros_ec\n", ["drain"])["EC variant: no /dev/cros_ec"])
check("drain QEMU log rejects Type-C, sensorhub, an extra transport, and each EC_EXTRA_BLOCK name",
      all(not all(EC_LOG(drain_log + ko(stem), ["drain"]).values())
          for stem in ("cros-ec-typec", "cros_ec_typec", "cros_ec_sensorhub", "cros-ec-sensorhub",
                       "cros_ec_uart", "cros-ec-uart", "cros_ec_spi", "cros-ec-usb"))
      and all(not all(EC_LOG(drain_log + ko(name), ["drain"]).values())
              and not all(EC_LOG(drain_log + ko(name.replace("_", "-")), ["drain"]).values())
              for name in EXTRA))
check("opt-out and feature QEMU checks stay on Loaded-module lines",
      all(EC_LOG(UNBOUND, []).values())
      and not all(EC_LOG(ko("cros_ec"), []).values())
      and all(EC_LOG(ko("cros_ec") + ko("cros_ec_lpcs") + ko("cros_ec_dev") + PROBE, ["sensors"]).values())
      and "drain:" not in " ".join(EC_LOG(PROBE, ["sensors"]))
      and not EC_LOG(PROBE, ["typec"])["EC variant: cros-ec-typec loaded, waits for the EC"]
      and EC_LOG(PROBE + "cros-ec-typec GOOG0014:00: couldn't find parent EC device\n", ["typec"])[
          "EC variant: cros-ec-typec loaded, waits for the EC"])
img_text = (ROOT / "builder" / "image.py").read_text(encoding="utf-8")
check("drain reuses the GOOG0004 SSDT and adds no early-boot modprobe",
      'if EC_FEATURES:\n    (OUT / "ec-ssdt.aml").write_bytes(ec_ssdt())' in img_text
      and "modprobe cros_ec" not in img_text)


def qemu_mutant_accepts_other_ec():
    """Shrink the drain block list back to MFD/keyb/userspace. True when Type-C, sensorhub and uart then pass."""
    old = "return blocked  # drain-blocked EC module keys"
    new = ('return {mod_key(n) for n in ("cros_ec_dev", "cros_ec_keyb", "cros_ec_chardev", '
           '"cros_ec_sysfs", "cros_ec_debugfs")}')
    if img_text.count(old) != 1:
        return None
    fn = load_ec_selftest(img_text.replace(old, new, 1))
    stems = ("cros-ec-typec", "cros_ec_sensorhub", "cros_ec_uart")
    return all(all(fn(drain_log + ko(stem), ["drain"]).values()) for stem in stems)


accepted = qemu_mutant_accepts_other_ec()
if accepted is None:
    check("mutation anchor once: drain QEMU check ignores Type-C, sensorhub and an extra transport", False)
elif not accepted:
    check("mutation still green: drain QEMU check ignores Type-C, sensorhub and an extra transport", False)
else:
    print("RED drain QEMU check ignores Type-C, sensorhub and an extra transport")
    check("mutation shown red: drain QEMU check ignores Type-C, sensorhub and an extra transport", True)


def run_watch_policy():
    """Watcher checks. The release archive ships this test and not tools/harness, so the caller skips there."""

    def load_watch(path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    watch_path = ROOT / "tools" / "harness" / "kano-gbos-watch.py"
    watch = load_watch(watch_path, "kano_gbos_watch_drain")
    guard_modules, guard_ic, guard_plain = watch.expected_guards()
    guards = (guard_modules, guard_ic, guard_plain)
    # Hardcoded. A mutant that grows DRAIN_RELEASED must still be caught against this pair.
    transport_pair = {"cros_ec", "cros_ec_lpcs"}

    def guard_tokens(drop):
        mods = [m for m in guard_modules if m not in drop]
        return [f"module_blacklist={','.join(mods)}",
                "initcall_blacklist=" + ",".join(guard_ic), *guard_plain]

    def drain_tokens(drop):
        mods = [m for m in guard_modules if m not in drop]
        for name in EXTRA:
            if name not in drop and name not in mods:
                mods.append(name)
        return [f"module_blacklist={','.join(mods)}",
                "initcall_blacklist=" + ",".join(guard_ic), *guard_plain]

    def typec_tokens(extra_drop=()):
        drop = set(TYPEC_RELEASED) | set(extra_drop)
        mods = [m for m in guard_modules if m not in drop]
        for name in EXTRA:
            if name not in drop and name not in mods:
                mods.append(name)
        return [f"module_blacklist={','.join(mods)}",
                "initcall_blacklist=" + ",".join(guard_ic), *guard_plain]

    pair_gone = watch.missing_guards(drain_tokens(transport_pair), guards, "usb", "drain")
    third_gone = watch.missing_guards(drain_tokens(transport_pair | {"cros_ec_dev"}), guards, "usb", "drain")
    tpm_gone = watch.missing_guards(drain_tokens(transport_pair | {"tpm"}), guards, "usb", "drain")
    check("drain watcher policy allows only cros_ec and cros_ec_lpcs to be absent",
          pair_gone == []
          and third_gone == ["module_blacklist entry cros_ec_dev"]
          and "module_blacklist entry tpm" in tpm_gone
          and "module_blacklist entry cros_ec" not in tpm_gone
          and "module_blacklist entry cros_ec_lpcs" not in tpm_gone)
    bare = watch.missing_guards(guard_tokens(transport_pair), guards, "usb", "drain")
    old_full = watch.missing_guards(guard_tokens(set()), guards, "usb", "default")
    stay_blocked = ("thunderbolt", "cros_typec_switch", "cros_ec_ucsi", "cros_usbpd_charger")
    check("drain watcher policy refuses a blacklist that omits every EC_EXTRA_BLOCK name",
          tuple(IMAGE_NS["EC_EXTRA_BLOCK"]) == EXTRA
          and all(f"module_blacklist entry {name}" in bare for name in EXTRA))
    check("default watcher policy accepts the typec image and fails closed otherwise",
          watch.missing_guards(typec_tokens(), guards, "usb", "default") == []
          and all(f"module_blacklist entry {name}" in old_full for name in EXTRA)
          and "module_blacklist entry cros_ec" not in watch.missing_guards(
              guard_tokens(transport_pair), guards, "usb", "default")
          and all(
              f"module_blacklist entry {name}" in watch.missing_guards(
                  typec_tokens({name}), guards, "usb", "default")
              for name in stay_blocked + EXTRA))
    check("drain watcher policy refuses each missing EC_EXTRA_BLOCK name",
          all(
              f"module_blacklist entry {name}" in watch.missing_guards(
                  drain_tokens(transport_pair | {name}), guards, "usb", "drain")
              and all(
                  f"module_blacklist entry {other}" not in watch.missing_guards(
                      drain_tokens(transport_pair | {name}), guards, "usb", "drain")
                  for other in EXTRA if other != name)
              for name in EXTRA))
    spi_gone = watch.missing_guards(
        drain_tokens(transport_pair) + ["spi-intel.writeable=1"], guards, "usb", "drain")
    nvme_ic = [c for c in guard_ic if c != "nvme_init"]
    nvme_tokens = [
        ("initcall_blacklist=" + ",".join(nvme_ic)) if t.startswith("initcall_blacklist=") else t
        for t in drain_tokens(transport_pair)]
    check("drain watcher policy still refuses spi-intel and a blacklisted nvme_init",
          any(x == "forbidden option spi-intel.writeable=1" for x in spi_gone)
          and watch.missing_guards(nvme_tokens, guards, "nvme", "drain") == []
          and any("nvme_init" in x for x in watch.missing_guards(
              drain_tokens(transport_pair), guards, "nvme", "drain")))
    ok_body = "cros_ec_lpcs GOOG0004:00: probe\ncros-ec-dev cros-ec-dev.0: no EC behind it\n"
    typec_body = ok_body + "ChromeOS EC device at 0\ncros-ec-typec GOOG0014:00: waiting\n"
    check("drain watcher still uses the EC regex and only skips the two transport lines",
          watch.first_danger(ok_body, watch.PROFILES["usb"], "drain") is None
          and watch.first_danger("cros_ec_lpcs GOOG0004:00: probe cros_ec_keyb\n", watch.PROFILES["usb"], "drain") is not None
          and all(watch.first_danger(ok_body + extra + "\n", watch.PROFILES["usb"], "drain") is not None
                   for extra in ("cros_ec_dev", "cros-ec-dev.ko", "cros_ec_keyb", "cros-ec-keyb",
                                 "cros_ec_chardev", "cros_ec_sysfs", "cros_ec_debugfs",
                                 "ChromeOS EC device at 0", "/dev/cros_ec")))
    check("default watcher accepts typec EC lines and still cuts on consumers",
          watch.first_danger(typec_body, watch.PROFILES["usb"], "default") is None
          and all(watch.first_danger(typec_body + extra + "\n", watch.PROFILES["usb"], "default") is not None
                  for extra in ("cros_ec_keyb", "cros_ec_chardev", "cros_ec_sysfs", "cros_ec_debugfs",
                                "/dev/cros_ec", "cros_usbpd_charger", "cros_typec_switch", "cros_ec_ucsi")))

    def loaded(stem):
        return f"init: Loaded kernel module /vendor/lib/modules/{stem}.ko\n"

    allowed_loads = "".join(loaded(stem) for stem in (
        "cros_ec", "cros_ec_lpcs", "cros_ec_proto", "cros-ec-proto", "cros_kbd_led_backlight", "iwlwifi"))
    check("drain watcher treats a Loaded line for each forbidden EC module as danger",
          all(watch.first_danger(loaded(name), watch.PROFILES["usb"], "drain") is not None
              and watch.first_danger(loaded(name.replace("_", "-")), watch.PROFILES["usb"], "drain") is not None
              for name in EXTRA)
          and all(watch.first_danger(loaded(stem), watch.PROFILES["usb"], "drain") is not None
                  for stem in ("cros-ec-typec", "cros_ec_sensorhub", "cros_ec_spi", "cros-ec-usb", "cros_ec_dev"))
          and watch.first_danger(ok_body + allowed_loads, watch.PROFILES["usb"], "drain") is None
          and watch.first_danger(loaded("cros_ec"), watch.PROFILES["usb"], "default") is None
          and watch.first_danger(loaded("cros_ec_lpcs"), watch.PROFILES["usb"], "default") is None
          and watch.first_danger(loaded("cros_ec_dev"), watch.PROFILES["usb"], "default") is None
          and watch.first_danger(loaded("cros-ec-typec"), watch.PROFILES["usb"], "default") is None
          and watch.first_danger(loaded("cros_usbpd_notify"), watch.PROFILES["usb"], "default") is None
          and watch.first_danger(loaded("cros_ec_proto"), watch.PROFILES["usb"], "default") is None
          and all(watch.first_danger(loaded(stem), watch.PROFILES["usb"], "default") is not None
                  for stem in ("cros_ec_uart", "thunderbolt", "cros_usbpd_charger",
                               "cros_typec_switch", "cros_ec_ucsi", "cros_ec_keyb", "gpio_cros_ec")))
    watch_src = watch_path.read_text(encoding="utf-8")
    check("watcher parses EC_EXTRA_BLOCK from builder/image.py",
          "gpio_cros_ec" not in watch_src and "image_ec_lists" in watch_src)

    def with_mutant(text, name, fn):
        with tempfile.TemporaryDirectory(prefix="gbos-ec-watch-") as raw:
            root = Path(raw)
            harness = root / "tools" / "harness"
            harness.mkdir(parents=True)
            (harness / "kano-ser.py").write_text("def load_env():\n    pass\n")
            path = harness / "kano-gbos-watch.py"
            path.write_text(text)
            os.symlink(ROOT / "builder", root / "builder")
            mod = load_watch(path, name)
            return fn(mod)

    def watch_mutant_allows_third():
        """Grow the drain release set by cros_ec_dev. True when a cmdline missing that module is then accepted."""
        text = watch_path.read_text(encoding="utf-8")
        old = 'DRAIN_RELEASED = frozenset({"cros_ec", "cros_ec_lpcs"})'
        new = 'DRAIN_RELEASED = frozenset({"cros_ec", "cros_ec_lpcs", "cros_ec_dev"})'
        if text.count(old) != 1:
            return None
        return with_mutant(text.replace(old, new, 1), "kano_gbos_watch_mut", lambda mod: (
            "module_blacklist entry cros_ec_dev" not in mod.missing_guards(
                drain_tokens(transport_pair | {"cros_ec_dev"}), guards, "usb", "drain")))

    def watch_mutant_drops_extras():
        """Drop the EC_EXTRA_BLOCK union. True when a blacklist without those names is then accepted."""
        text = watch_path.read_text(encoding="utf-8")
        old = 'modules = (set(modules) - DRAIN_RELEASED) | set(image_ec_lists()["EC_EXTRA_BLOCK"])'
        new = "modules = set(modules) - DRAIN_RELEASED"
        if text.count(old) != 1:
            return None
        return with_mutant(text.replace(old, new, 1), "kano_gbos_watch_extra_mut", lambda mod: (
            "module_blacklist entry gpio_cros_ec" not in mod.missing_guards(
                guard_tokens(transport_pair), guards, "usb", "drain")))

    def watch_mutant_ignores_loaded_extra():
        """Skip the Loaded-module danger return. True when cros_ec_uart.ko no longer cuts power."""
        text = watch_path.read_text(encoding="utf-8")
        old = 'return "forbidden EC module loaded", hit.group(0)'
        new = "continue"
        if text.count(old) != 1:
            return None
        return with_mutant(text.replace(old, new, 1), "kano_gbos_watch_loaded_mut", lambda mod: (
            mod.first_danger(loaded("cros_ec_uart"), mod.PROFILES["usb"], "drain") is None))

    def show_mutant(label, result):
        if result is None:
            check(f"mutation anchor once: {label}", False)
        elif not result:
            check(f"mutation still green: {label}", False)
        else:
            print(f"RED {label}")
            check(f"mutation shown red: {label}", True)

    show_mutant("watcher drain policy allows a third module", watch_mutant_allows_third())
    show_mutant("watcher drain policy drops EC_EXTRA_BLOCK", watch_mutant_drops_extras())
    show_mutant("watcher drain policy ignores a loaded extra", watch_mutant_ignores_loaded_extra())


# tools/harness is a developer tree. make-release.sh does not put it in the installer archive,
# and that archive still runs this file. A git checkout must have the watcher; a release tree skips.
if (ROOT / "tools" / "harness" / "kano-gbos-watch.py").is_file():
    run_watch_policy()
elif (ROOT / ".git").exists():
    check("this checkout has tools/harness/kano-gbos-watch.py", False)
else:
    print("SKIP watcher policy (this tree has no tools/harness)")

print(f"ec-drain tests: {pass_n} passed, {fail_n} failed")
sys.exit(0 if fail_n == 0 else 1)
