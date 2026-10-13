#!/usr/bin/env python3
"""Device profiles: profiles/<model>/profile.toml, checked against profiles/_safety.toml (the project-wide rules).
Every value the builder needs for one model lives in its profile directory; builder/image.py, builder/vendor.sh,
builder/build.sh and builder/fetch-inputs.sh read it from here. Strict: unknown keys, missing keys and wrong types are
refused, and a profile can only add to the safety rules, never remove from them.
usage: devprofile.py check <profile dir>            validate (exit 0 = ok)
       devprofile.py get <profile dir> <a.b>        one string or boolean value, e.g. model.image_name
       devprofile.py firmware <profile dir>         "<path> <from> <src> <sha256>" per firmware file
       devprofile.py kmods <profile dir>            the extra kernel modules the profile ships, one per line
       devprofile.py json <profile dir>             the validated profile as JSON"""
import json, re, sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:   # Python < 3.11
    sys.exit("devprofile.py: Python 3.11 or newer is needed (tomllib)")

SAFETY = Path(__file__).resolve().parents[2] / "profiles" / "_safety.toml"
LSTR = ("list", str)
SCHEMA = {
    "schema": int,
    "model": {"id": str, "name": str, "status": str, "image_name": str, "dmi": ("map", str)},
    "entries": {"quiet": str, "debug": str},
    "cmdline": {"stock_drop": LSTR, "add": LSTR, "tail": LSTR, "debug_drop": LSTR, "pci_stub_ids": LSTR,
                "first_stage_drop": LSTR},
    "blacklist": {"modules": LSTR, "debug_add": LSTR, "allow": LSTR},
    "firmware_sources": {"linux_firmware_tag": str, "sof_bin_url": str, "sof_bin_sha256": str},
    "firmware": ("tables", {"path": str, "from": str, "src": str, "sha256": str}),
    "audio": {"card": str, "config_dir": str, "sources": str},
    "wifi": {"modprobe": LSTR},
    "display": {"stack": str, "hwc_backend": str, "build_prop_set": ("list", ("list", str))},
    "input": {"kbd_backlight_leds": str, "power_button": str},
    "hooks": {"vendor": str},
    "kmods": LSTR,
    "camera": {"libcamera": bool},
}
# extra kernel modules the builder can build from pinned sources (builder/kmod/build-<name>.sh) and ship in vendor
KMODS = {"ac": "ACPI AC adapter (ACPI0003): Android sees the charger; reads _PSR only, no EC host command",
         "gbos_efifb": "the UEFI GOP framebuffer as a DRM device (display.stack = gop-swiftshader), from the base's kheaders",
         "gbos_wakefilter": "drops the ACPI power button's resume KEY_WAKEUP when the button did not wake the system "
                            "(input.power_button = acpi): dark resumes stay dark"}
SAFETY_SCHEMA = {
    "schema": int,
    "cmdline": {"stock_drop": LSTR, "tail": LSTR, "forbidden_prefixes": LSTR},
    "blacklist": {"required": LSTR},
}


class ProfileError(ValueError):
    pass


def _typed(value, spec, where):
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise ProfileError(f"{where}: expected a table")
        unknown = sorted(set(value) - set(spec))
        if unknown:
            raise ProfileError(f"{where}: unknown key(s) {', '.join(unknown)}")
        for k, s in spec.items():
            if k not in value:
                raise ProfileError(f"{where}: missing key {k}")
            _typed(value[k], s, f"{where}.{k}" if where else k)
    elif isinstance(spec, tuple) and spec[0] == "list":
        if not isinstance(value, list):
            raise ProfileError(f"{where}: expected a list")
        for i, x in enumerate(value):
            _typed(x, spec[1], f"{where}[{i}]")
    elif isinstance(spec, tuple) and spec[0] == "map":
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, spec[1]) for k, v in value.items()):
            raise ProfileError(f"{where}: expected a table of {spec[1].__name__}")
    elif isinstance(spec, tuple) and spec[0] == "tables":
        if not isinstance(value, list):
            raise ProfileError(f"{where}: expected an array of tables ([[{where}]])")
        for i, x in enumerate(value):
            _typed(x, spec[1], f"{where}[{i}]")
    elif spec is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProfileError(f"{where}: expected an integer")
    elif not isinstance(value, spec):
        raise ProfileError(f"{where}: expected {spec.__name__}")


TOKEN = re.compile(r"[\x21-\x7e]+")
MODULE = re.compile(r"[a-z0-9_]+")


def _tokens(lst, where):
    for t in lst:
        if not TOKEN.fullmatch(t):
            raise ProfileError(f"{where}: {t!r} is not one kernel command line option")
    if len(set(lst)) != len(lst):
        raise ProfileError(f"{where}: duplicate items")


def load_safety(path=SAFETY):
    with open(path, "rb") as f:
        s = tomllib.load(f)
    _typed(s, SAFETY_SCHEMA, "")
    if s["schema"] != 1:
        raise ProfileError(f"{path}: schema {s['schema']} is not supported")
    for k in ("stock_drop", "tail"):
        _tokens(s["cmdline"][k], f"_safety cmdline.{k}")
    if "efi=noruntime" not in s["cmdline"]["tail"] or not any(t.startswith("initcall_blacklist=") for t in s["cmdline"]["tail"]):
        raise ProfileError("_safety cmdline.tail must carry efi=noruntime and an initcall_blacklist=")
    if not all(MODULE.fullmatch(m) for m in s["blacklist"]["required"]):
        raise ProfileError("_safety blacklist.required: not module names")
    return s


def load(profile_dir, safety_path=SAFETY):
    d = Path(profile_dir)
    with open(d / "profile.toml", "rb") as f:
        p = tomllib.load(f)
    p.setdefault("firmware", [])       # [[firmware]] tables: a profile without device firmware simply has none
    p.setdefault("kmods", [])          # extra kernel modules (KMODS): none unless the profile lists them
    p.setdefault("camera", {"libcamera": False})   # the libcamera camera HAL overlay (hooks.vendor camera()): off
    _typed(p, SCHEMA, "")
    s = load_safety(safety_path)
    if p["schema"] != 1:
        raise ProfileError(f"schema {p['schema']} is not supported")
    m = p["model"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", m["id"]) or m["id"] != d.resolve().name:
        raise ProfileError(f"model.id {m['id']!r} must be lower-case and equal to the directory name {d.resolve().name!r}")
    if m["status"] not in ("verified", "experimental", "probe-only"):
        raise ProfileError("model.status must be verified, experimental or probe-only")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.img", m["image_name"]):
        raise ProfileError("model.image_name must be a plain file name ending in .img")
    for k, t in p["entries"].items():
        if not t or not t.isprintable():
            raise ProfileError(f"entries.{k}: empty or not printable")
    c = p["cmdline"]
    for k in ("stock_drop", "add", "tail", "debug_drop"):
        _tokens(c[k], f"cmdline.{k}")
    # the builder (module_blacklist=, the safety tail's initcall_blacklist= and efi=) owns these; a profile option could
    # override them later on the line, so no profile option may start with one of them
    for k in ("add", "tail"):
        for t in c[k]:
            for pre in s["cmdline"]["forbidden_prefixes"]:
                if t.startswith(pre):
                    raise ProfileError(f"cmdline.{k}: {t} is not allowed in a profile (prefix {pre})")
    if set(c["stock_drop"]) & set(s["cmdline"]["tail"]):
        raise ProfileError("cmdline.stock_drop must not remove a safety option")
    if not all(re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", x) for x in c["pci_stub_ids"]):
        raise ProfileError("cmdline.pci_stub_ids: expected vvvv:dddd (lower-case hex)")
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+\.ko", x) for x in c["first_stage_drop"]):
        raise ProfileError("cmdline.first_stage_drop: expected module file names (name.ko)")
    b = p["blacklist"]
    for k in ("modules", "debug_add", "allow"):
        if not all(MODULE.fullmatch(x) for x in b[k]) or len(set(b[k])) != len(b[k]):
            raise ProfileError(f"blacklist.{k}: expected distinct module names")
    missing = [x for x in s["blacklist"]["required"] if x not in b["modules"]]
    if missing:
        raise ProfileError(f"blacklist.modules lacks required module(s): {', '.join(missing)}")
    if set(b["allow"]) & (set(b["modules"]) | set(b["debug_add"]) | set(s["blacklist"]["required"])):
        raise ProfileError("blacklist.allow overlaps the blacklist or the required modules")
    fs = p["firmware_sources"]
    paths = set()
    for i, fw in enumerate(p["firmware"]):
        if fw["from"] not in ("linux-firmware", "sof-bin"):
            raise ProfileError(f"firmware[{i}].from must be linux-firmware or sof-bin")
        for k in ("path", "src"):
            v = fw[k]
            if not re.fullmatch(r"[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*", v) or ".." in v.split("/"):
                raise ProfileError(f"firmware[{i}].{k}: not a plain relative path")
        if not re.fullmatch(r"[0-9a-f]{64}", fw["sha256"]):
            raise ProfileError(f"firmware[{i}].sha256: expected 64 lower-case hex digits")
        if fw["path"] in paths:
            raise ProfileError(f"firmware: {fw['path']} listed twice")
        paths.add(fw["path"])
    if any(fw["from"] == "linux-firmware" for fw in p["firmware"]) and not re.fullmatch(r"[0-9]{8}", fs["linux_firmware_tag"]):
        raise ProfileError("firmware_sources.linux_firmware_tag: expected a linux-firmware release tag (YYYYMMDD)")
    if any(fw["from"] == "sof-bin" for fw in p["firmware"]) and not (
            fs["sof_bin_url"].startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", fs["sof_bin_sha256"])):
        raise ProfileError("firmware_sources: sof-bin files need an https sof_bin_url and its sof_bin_sha256")
    a = p["audio"]
    for k in ("card", "config_dir"):
        if a[k] and not re.fullmatch(r"[A-Za-z0-9._-]+", a[k]):
            raise ProfileError(f"audio.{k}: not a plain name")
    if a["sources"] and not (d / a["sources"]).is_file():
        raise ProfileError(f"audio.sources: {a['sources']} is not a file in {d}")
    if not all(MODULE.fullmatch(x) for x in p["wifi"]["modprobe"]):
        raise ProfileError("wifi.modprobe: expected module names")
    disp = p["display"]
    # native: the image's own GPU stack (KANO: xe + Mesa ANV); gop-swiftshader: machines whose GPU has no driver in this
    # kernel (AMD, older Intel, QEMU): the firmware's GOP framebuffer (gbos_efifb.ko) and SwiftShader from Cuttlefish
    if disp["stack"] not in ("native", "gop-swiftshader"):
        raise ProfileError("display.stack must be native or gop-swiftshader")
    if (disp["stack"] == "gop-swiftshader") != ("gbos_efifb" in p["kmods"]):
        raise ProfileError("display.stack = gop-swiftshader and kmods = [\"gbos_efifb\"] go together")
    if disp["hwc_backend"] not in ("", "client"):
        raise ProfileError("display.hwc_backend must be empty or client")
    for i, kv in enumerate(disp["build_prop_set"]):
        if len(kv) != 3 or not re.fullmatch(r"[A-Za-z0-9_.]+", kv[0]) or kv[0].startswith(("ro.adb.", "service.adb.", "persist.adb.", "ro.debuggable")):
            raise ProfileError(f"display.build_prop_set[{i}]: expected [property, old value, new value] (not an adb/debuggable property)")
    inp = p["input"]
    if inp["kbd_backlight_leds"] and not re.fullmatch(r"/sys/devices/[A-Za-z0-9:._/-]+/leds", inp["kbd_backlight_leds"]):
        raise ProfileError("input.kbd_backlight_leds: expected empty or /sys/devices/.../leds")
    if inp["power_button"] not in ("acpi", "stock"):
        raise ProfileError("input.power_button must be acpi or stock")
    if "gbos_wakefilter" in p["kmods"] and inp["power_button"] != "acpi":
        raise ProfileError("kmods gbos_wakefilter filters the ACPI power button: it needs input.power_button = acpi")
    unknown = [k for k in p["kmods"] if k not in KMODS]
    if unknown or len(set(p["kmods"])) != len(p["kmods"]):
        raise ProfileError(f"kmods: expected distinct names from {', '.join(sorted(KMODS))}")
    h = p["hooks"]["vendor"]
    if h and not (re.fullmatch(r"[a-z0-9_]+\.py", h) and (d / h).is_file()):
        raise ProfileError(f"hooks.vendor: {h} is not a Python file in {d}")
    needs_hook = [k for k, v in (("audio.config_dir", a["config_dir"]), ("wifi.modprobe", p["wifi"]["modprobe"]),
                                 ("display.hwc_backend", disp["hwc_backend"]), ("display.build_prop_set", disp["build_prop_set"]),
                                 ("input.kbd_backlight_leds", inp["kbd_backlight_leds"]),
                                 ("input.power_button = acpi", inp["power_button"] == "acpi"),
                                 ("camera.libcamera", p["camera"]["libcamera"])) if v]
    if needs_hook and not h:
        raise ProfileError(f"{', '.join(needs_hook)} need a hooks.vendor file (see profiles/kano/vendor.py)")
    p["_dir"] = str(d.resolve())
    p["_safety"] = s
    return p


def stock_tokens(vendor_cmdline, prof):
    """The stock vendor_boot options that every entry starts from: minus the safety and profile drop lists and the stock
    module_blacklist=/initcall_blacklist= items, which the builder replaces."""
    drop = set(prof["_safety"]["cmdline"]["stock_drop"]) | set(prof["cmdline"]["stock_drop"])
    return [o for o in vendor_cmdline.split() if o not in drop and not o.startswith(("module_blacklist=", "initcall_blacklist="))]


SERIAL = ("console=", "earlycon=")
DEBUG_ONLY = ("keep_bootcon", "printk.devkmsg=on")


def make_entries(prof, vendor_cmdline):
    """The options of the serial-debug and quiet entries, and the "normal" options (no pci-stub and no serial-debug
    extras; no entry uses them since the plain entry was dropped, guards() reads its module_blacklist), as token lists."""
    c, b = prof["cmdline"], prof["blacklist"]
    opts = stock_tokens(vendor_cmdline, prof) + c["add"]
    tail = prof["_safety"]["cmdline"]["tail"] + c["tail"]
    normal = opts + ["module_blacklist=" + ",".join(b["modules"])] + tail
    debug = [o for o in opts if o not in c["debug_drop"]] + \
        ["module_blacklist=" + ",".join(b["modules"] + b["debug_add"])] + tail + list(DEBUG_ONLY) + \
        (["pci-stub.ids=" + ",".join(c["pci_stub_ids"])] if c["pci_stub_ids"] else [])
    # quiet: the debug entry's hardware set without any serial console (the 115200-baud console is synchronous)
    quiet = [o for o in debug if not o.startswith(SERIAL) and o not in DEBUG_ONLY + ("ignore_loglevel", "loglevel=8")] + ["loglevel=4"]
    return {"normal": normal, "debug": debug, "quiet": quiet}


def guards(prof):
    """(modules, initcalls, plain options) that every entry of this profile carries: the normal module_blacklist, the
    initcall_blacklist= and the efi= items of the tail."""
    tail = prof["_safety"]["cmdline"]["tail"] + prof["cmdline"]["tail"]
    ic = [x.split("=", 1)[1].split(",") for x in tail if x.startswith("initcall_blacklist=")]
    if len(ic) != 1:
        raise ProfileError("the tail must have exactly one initcall_blacklist=")
    return set(prof["blacklist"]["modules"]), ic[0], [x for x in tail if x.startswith("efi=")]


def _main(argv):
    if len(argv) < 3 or argv[1] not in ("check", "get", "firmware", "kmods", "json"):
        sys.exit(__doc__)
    try:
        p = load(argv[2])
    except (OSError, tomllib.TOMLDecodeError, ProfileError) as e:
        sys.exit(f"profile {argv[2]}: {e}")
    if argv[1] == "check":
        print(f"profile {p['model']['id']} ok")
    elif argv[1] == "get":
        v = p
        for k in argv[3].split("."):
            v = v[k]
        if isinstance(v, bool):
            v = "true" if v else "false"
        if not isinstance(v, str):
            sys.exit(f"{argv[3]} is not a string or a boolean")
        print(v)
    elif argv[1] == "firmware":
        for fw in p["firmware"]:
            print(fw["path"], fw["from"], fw["src"], fw["sha256"])
    elif argv[1] == "kmods":
        for k in p["kmods"]:
            print(k)
    else:
        print(json.dumps({k: v for k, v in p.items()}, indent=1, sort_keys=True))


if __name__ == "__main__":
    _main(sys.argv)
