#!/usr/bin/env python3
"""Watch the KANO AP UART during a Googlebook boot; cut power through the EC on any danger line.

usage: kano-gbos-watch.py <ap-uart.log> <seconds> <frames-dir> [--profile usb|nvme] [--ec-policy default|drain]
                          [--observe-timeout S] [--stall-timeout S] [--camera-every S] [--camera-timeout S] [--ec-timeout S]
       (kano-gbos-watch-nvme.py is this script with --profile nvme: Googlebook on the internal NVMe, so the NVMe
       controller appearing is expected and the command line keeps nvme_init allowed.)

Limits - read before relying on it:
- It only sees what the AP UART prints, so it only works with the serial-debug boot entry (earlycon +
  keep_bootcon: printk on the bootconsole, not an interactive tty). The quiet entry has no serial
  console; with it, the watcher reports "unobservable", never "no danger".
- It is a supplementary detector, not a guarantee. It polls the log once a second and reacts to lines after they
  were printed, so anything that happens before, or without, a matching line is not prevented. The protections are
  the kernel command line guards and the updaters removed from the image; this watcher checks the guards are there.

Start it right after power-on, before the kernel starts (the callers truncate the log first). Bytes already in the
log when it starts are this boot's only if they hold no kernel command line yet; otherwise only lines written from
then on count as this boot's. Danger lines are searched in the whole log.

Observability (exit 4, "unobservable"): within --observe-timeout seconds (default 120) this boot's kernel command
line must show up, and during that time the log must never stay silent for --stall-timeout seconds (default 30).
After the observe window a silent log only produces a note (Android legitimately goes quiet once it has booted).

Guards: this boot's kernel command line must carry every module of the module_blacklist of the device profile
(profiles/kano/profile.toml, the single source the builder uses; read as TOML), every initcall of its initcall_blacklist (with --profile nvme:
all but nvme_init, which must not be blacklisted) and efi=noruntime, and no spi-intel.* option. The kernel keeps the last module_blacklist= and adds up
every initcall_blacklist=. The default policy does not know GBOS_EC feature variants and cuts power if any required module is absent (fail-closed).
--ec-policy drain is the only exception: the required modules are the profile baseline without cros_ec and cros_ec_lpcs, plus every EC_EXTRA_BLOCK name parsed from builder/image.py (not a second copy of that list). Each missing name is refused. It does not ignore the EC danger regex. The LPC transport probe/registered line and the unbound platform device named cros-ec-dev are the two lines that do not cut power; the cros_ec_dev module, keyb, chardev, sysfs, debugfs, "ChromeOS EC device at" and /dev/cros_ec still do. A "Loaded kernel module .../<file>.ko" line cuts power when <file> is a forbidden EC module (consumers, other transports, or those extras); "-" and "_" in the file name are the same module. cros_ec, cros_ec_lpcs and cros_ec_proto may load. This watcher is not a host-command allowlist.

exit status: 0 window over, boot observed, no danger line; 2 danger line, EC power cut confirmed; 3 a kernel
command line guard is missing, EC power cut confirmed; 4 unobservable (also when the window ends before this boot's
command line showed up); 5 not started: invalid arguments, KANO_EC_TTY is not set (it could not cut the power) or the
guard list cannot be read from the device profile; 6 danger seen but the EC power cut could not be confirmed (cut power
by hand).
"""
import argparse, ast, importlib.util, math, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
K = HERE / "kano-ser.py"
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("kano_ser", K)
ks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ks)                     # for load_env(); kano-ser.py itself runs as a subprocess

# The EC bind regex stays in the default policy. --ec-policy drain does not delete it.
EC_BIND = r"cros-ec-dev|cros_ec_lpcs .*(probe|registered)|ChromeOS EC device at"
DRAIN_RELEASED = frozenset({"cros_ec", "cros_ec_lpcs"})
# Fatal under drain even when the line also contains an allowed transport or platform-device name.
# Bare "cros-ec-dev" is the unbound platform device the core creates without cros_ec_dev.ko, so it is not here.
DRAIN_CONSUMER = re.compile(
    r"cros_ec_dev\b|cros-ec-dev\.ko|cros_ec_keyb|cros-ec-keyb|cros_ec_chardev|cros-ec-chardev|"
    r"cros_ec_sysfs|cros-ec-sysfs|cros_ec_debugfs|cros-ec-debugfs|"
    r"ChromeOS EC device at|/dev/cros_(?:ec|fp|ish)\b")
COMMON_DANGER = [
    (r"spi-nor spi|\bmtd\d+:|mtdblock\d", "SPI flash / MTD device appeared"),
    (EC_BIND, "EC driver bound"),
    (r"tpm_tis.*2\.0 TPM|tpm tpm0:|tpm_tis_i2c_cr50 .*cr50", "TPM / Cr50 driver bound"),
    (r"gsmi: ", "gsmi active"),
    (r"(?i)vbmeta digest (error|mismatch)", "vbmeta mismatch"),
    (r"starting service 'fwupd", "fwupd started"),
    (r"kernel_max_rollforward", "crossystem rollforward"),
    (r"intel-spi \S+: |spi_intel_pci \S+: |spi-nor ", "Intel SPI driver bound"),
]
PROFILES = {
    # USB boot: the internal NVMe must stay hidden
    "usb": [(r"nvme nvme\d", "internal NVMe controller appeared")] + COMMON_DANGER,
    # NVMe boot: Googlebook lives on the internal NVMe, so the controller is expected (nvme_init not blacklisted)
    "nvme": COMMON_DANGER,
}
CMDLINE = "Kernel command line"
REPO_ROOT = HERE.parent.parent
GUARD_SOURCE = REPO_ROOT / "profiles" / "kano" / "profile.toml"
_IMAGE_EC_LISTS = None
LOADED_KO = re.compile(r"Loaded kernel module \S*/([A-Za-z0-9_.-]+)\.ko\b")


def expected_guards(profile_dir=GUARD_SOURCE.parent):
    """(modules, initcalls, plain options) every Googlebook entry carries, from the device profile the builder uses
    (profiles/kano/profile.toml with the project-wide profiles/_safety.toml, through builder/lib/devprofile.py, which
    reads TOML and checks it; the builder itself is not run): the profile's module_blacklist, and the initcall_blacklist=
    and efi= items of the tail. Raises ValueError (or OSError) when the profile is missing, invalid or implausible, so a
    changed builder fails closed."""
    lib = REPO_ROOT / "builder" / "lib" / "devprofile.py"
    spec = importlib.util.spec_from_file_location("devprofile", lib)
    if spec is None:
        raise OSError(f"no {lib}")
    dp = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(dp)
        modules, ic, plain = dp.guards(dp.load(profile_dir))
    except SystemExit as e:                      # devprofile.py refuses a Python without tomllib
        raise ValueError(str(e)) from None
    if len(ic) == 0 or not plain or len(modules) < 10:
        raise ValueError("the profile has no initcall_blacklist= / no efi= item, or its module_blacklist is implausibly short")
    return modules, ic, plain


def module_key(name):
    """Module names and .ko file names spell one driver with either '-' or '_'."""
    return name.replace("-", "_")


def image_py():
    """builder/image.py next to this checkout. image.py builds an image if it is imported, so callers parse it."""
    here = Path(__file__).resolve().parent
    for path in [here.parent.parent / "builder" / "image.py", *(p / "builder" / "image.py" for p in here.parents)]:
        if path.is_file():
            return path
    raise OSError("builder/image.py not found")


def image_ec_lists():
    """EC_EXTRA_BLOCK, EC_CORE, EC_RELEASE, EC_NEVER and EC_DRAIN from builder/image.py.

    Parsed with ast.literal_eval. This is the builder's list, not a handwritten copy.
    """
    global _IMAGE_EC_LISTS
    if _IMAGE_EC_LISTS is not None:
        return _IMAGE_EC_LISTS
    path = image_py()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    want = ("EC_EXTRA_BLOCK", "EC_CORE", "EC_RELEASE", "EC_NEVER", "EC_DRAIN")
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in want:
            found[target.id] = ast.literal_eval(node.value)
    missing = [name for name in want if name not in found]
    if missing:
        raise ValueError(f"{path} is missing {', '.join(missing)}")
    extra = found["EC_EXTRA_BLOCK"]
    if not isinstance(extra, (list, tuple)) or not extra or not all(isinstance(item, str) and item for item in extra):
        raise ValueError(f"{path} EC_EXTRA_BLOCK is not a list of module names")
    if not isinstance(found["EC_RELEASE"], dict) or not isinstance(found["EC_DRAIN"], (set, frozenset, list, tuple)):
        raise ValueError(f"{path} EC release lists have an unexpected shape")
    _IMAGE_EC_LISTS = found
    return found


def drain_forbidden_module_keys():
    """EC modules drain must not load: consumers, other transports, and every EC_EXTRA_BLOCK name.

    The transport pair and cros_ec_proto are absent from this set. Keys use '_' for both spellings.
    """
    lists = image_ec_lists()
    blocked = {module_key(name) for name in lists["EC_EXTRA_BLOCK"]}
    blocked |= {module_key(name) for name in lists["EC_CORE"] if name not in lists["EC_DRAIN"]}
    for group in lists["EC_RELEASE"].values():
        blocked |= {module_key(name) for name in group}
    blocked |= {module_key(name) for name in lists["EC_NEVER"] if "cros" in module_key(name)}
    blocked -= {module_key(name) for name in lists["EC_DRAIN"]}
    blocked.discard("cros_ec_proto")
    return blocked


def cmdline_tokens(text):
    """The tokens of this boot's kernel command line; a long one is printed in chunks, each but the last ending in
    a backslash."""
    toks = []
    for l in text.splitlines():
        i = l.find(CMDLINE)
        if i >= 0:
            part = l[i + len(CMDLINE):].lstrip(":").strip()
            toks += (part[:-1] if part.endswith("\\") else part).split()
    return toks


def missing_guards(tokens, guards, profile, ec_policy="default"):
    """What the command line `tokens` lacks of `guards` (expected_guards()), as readable items; [] = all there.

    ec_policy "drain" requires (profile modules minus cros_ec and cros_ec_lpcs) union EC_EXTRA_BLOCK
    from builder/image.py. Any other value keeps the profile list only.
    """
    modules, initcalls, plain = guards
    if ec_policy == "drain":
        modules = (set(modules) - DRAIN_RELEASED) | set(image_ec_lists()["EC_EXTRA_BLOCK"])
    mb = [t.split("=", 1)[1] for t in tokens if t.startswith("module_blacklist=")]
    have = set(mb[-1].split(",")) if mb else set()          # the kernel keeps the last module_blacklist=
    ic = set()
    for t in tokens:
        if t.startswith("initcall_blacklist="):
            ic |= set(t.split("=", 1)[1].split(","))           # every initcall_blacklist= counts
    out = [f"module_blacklist entry {m}" for m in sorted(modules - have)]
    out += [f"initcall_blacklist entry {c}" for c in initcalls
            if c not in ic and not (profile == "nvme" and c == "nvme_init")]
    if profile == "nvme" and "nvme_init" in ic:
        out.append("nvme_init is blacklisted: not an internal-NVMe entry")
    out += [p for p in plain if p not in tokens]
    out += [f"forbidden option {t}" for t in tokens if t.startswith("spi-intel.")]
    return out


def drain_transport_exception(match, line):
    """True when this EC-regex hit is one of the two drain exceptions and the line has no consumer.

    Allowed: cros_ec_lpcs probe/registered, and the bare platform device name cros-ec-dev.
    A consumer on the same line (MFD module, keyb, chardev, sysfs, debugfs, ChromeOS EC device, raw node) is not allowed.
    """
    if DRAIN_CONSUMER.search(line):
        return False
    if re.fullmatch(r"cros_ec_lpcs .*(?:probe|registered)", match):
        return True
    return match == "cros-ec-dev"


def first_danger(body, patterns, ec_policy):
    """The first danger hit as (why, text), or None.

    drain does not drop the EC regex. It skips only an allowed transport hit, then keeps looking.
    Consumers are fatal under drain even when that regex did not name them.
    """
    for pat, why in patterns:
        if ec_policy == "drain" and pat == EC_BIND:
            found = None
            for m in re.finditer(pat, body):
                line = body.splitlines()[body.count("\n", 0, m.start())]
                if drain_transport_exception(m.group(0), line):
                    continue
                found = m
                break
            if found:
                return why, found.group(0)
            continue
        m = re.search(pat, body)
        if m:
            return why, m.group(0)
    if ec_policy == "drain":
        m = DRAIN_CONSUMER.search(body)
        if m:
            return "EC consumer driver bound", m.group(0)
        forbidden = drain_forbidden_module_keys()
        for hit in LOADED_KO.finditer(body):
            if module_key(hit.group(1)) in forbidden:
                return "forbidden EC module loaded", hit.group(0)
    return None


def clean(b):
    return re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]", b"", b.replace(b"\0", b"")).decode(errors="replace")


def complete(text):
    """Only complete lines: the last one may still be arriving over the UART."""
    return text[: text.rfind("\n") + 1]


def ec(cmd, timeout):
    try:
        r = subprocess.run([sys.executable, str(K), "ec", cmd, "2"], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"EC '{cmd}' timed out after {timeout} s"
    return r.returncode, r.stdout + r.stderr


def cut_power(timeout):
    """EC apshutdown, then confirm with powerinfo that the AP is in G3/S5. Returns True when confirmed."""
    for attempt in (1, 2):
        rc, out = ec("apshutdown", timeout)
        print(out.strip(), flush=True)
        if rc != 0:
            print(f"EC apshutdown attempt {attempt} failed (exit {rc})", flush=True)
        for _ in range(3):
            rc, out = ec("powerinfo", timeout)
            if rc == 0 and re.search(r"power state \d+ = (G3|S5)\b", out):
                print(f"EC confirms the AP is off: {out.strip()}", flush=True)
                return True
        print(f"EC power state not confirmed off (last reply {out.strip()!r})", flush=True)
    print("EC POWER CUT NOT CONFIRMED - cut the power by hand", flush=True)
    return False


def danger(why, code, timeout):
    print(f"DANGER {why} -> EC apshutdown", flush=True)
    sys.exit(code if cut_power(timeout) else 6)


def main(argv):
    ap = argparse.ArgumentParser(description="KANO AP UART danger watcher")
    ap.add_argument("log", type=Path)
    ap.add_argument("seconds", type=float)
    ap.add_argument("frames", type=Path)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="usb")
    ap.add_argument("--ec-policy", choices=("default", "drain"), default="default")
    ap.add_argument("--observe-timeout", type=float, default=120)
    ap.add_argument("--stall-timeout", type=float, default=30)
    ap.add_argument("--camera-every", type=float, default=30, help="seconds between camera frames, 0 = off")
    ap.add_argument("--camera-timeout", type=float, default=15)
    ap.add_argument("--ec-timeout", type=float, default=15)

    def bad(msg):
        print(f"refusing: {msg}", file=sys.stderr)
        sys.exit(5)
    ap.error = bad
    a = ap.parse_args(argv)
    for name in ("seconds", "observe_timeout", "stall_timeout", "camera_timeout", "ec_timeout", "camera_every"):
        v = getattr(a, name)
        if not math.isfinite(v) or v < 0 or (v == 0 and name != "camera_every"):
            bad(f"{name.replace('_', '-')} must be a finite number above 0{' (or 0 = off)' if name == 'camera_every' else ''}, got {v}")
    ks.load_env()
    if not os.environ.get("KANO_EC_TTY"):
        bad("KANO_EC_TTY is not set, so a danger line could not cut the power; put it in kano-env.local.sh "
            "or the environment (see README.md)")
    try:
        guards = expected_guards()
    except (OSError, SyntaxError, ValueError) as e:
        bad(f"cannot read the expected command line guards from {GUARD_SOURCE}: {e}")
    if a.ec_policy == "drain":
        try:
            image_ec_lists()
        except (OSError, SyntaxError, ValueError, TypeError) as e:
            bad(f"cannot read the drain EC block list from builder/image.py: {e}")
    patterns = PROFILES[a.profile]
    a.frames.mkdir(parents=True, exist_ok=True)

    start = time.time()
    raw = a.log.read_bytes() if a.log.exists() else b""
    base = len(raw) if CMDLINE in clean(raw) else 0
    if base:
        print(f"note: {a.log} already holds a kernel command line; only lines written from now on count as this boot's",
              flush=True)
    last_size, last_growth, silent_noted = len(raw), start, False
    verified, cam, cam_start, last_frame = False, None, 0.0, 0.0
    try:
        while time.time() - start < a.seconds:
            now = time.time()
            raw = a.log.read_bytes() if a.log.exists() else b""
            if len(raw) < base:                       # truncated: everything from here on is this boot
                base = 0
            if len(raw) != last_size:
                last_size, last_growth, silent_noted = len(raw), now, False
            whole, this = complete(clean(raw)), complete(clean(raw[base:]))

            # "Module X is blacklisted" is the guard working, not the driver binding.
            body = "\n".join(l for l in whole.splitlines() if "command line" not in l.lower() and "blacklisted" not in l)
            hit = first_danger(body, patterns, a.ec_policy)
            if hit:
                why, text = hit
                danger(f"{why}: {text!r}", 2, a.ec_timeout)

            cmdlines = [l for l in this.splitlines() if CMDLINE in l]
            # The command line is printed in several chunks; check it once the kernel is past 1.5 s.
            later = [float(t) for t in re.findall(r"^\S*\[\s*(\d+\.\d+)\]", this, re.M)]
            if cmdlines and later and max(later) > 1.5:
                missing = missing_guards(cmdline_tokens("\n".join(cmdlines)), guards, a.profile, a.ec_policy)
                if missing:
                    danger(f"guard missing from this boot's kernel command line: {', '.join(missing)}", 3, a.ec_timeout)
                if not verified:
                    verified = True
                    print(f"this boot's kernel command line has all guards (+{now - start:.0f} s)", flush=True)

            in_window = now - start < a.observe_timeout
            if not verified and not in_window:
                print(f"UNOBSERVABLE: no kernel command line from this boot within {a.observe_timeout:.0f} s "
                      f"(log {a.log}, {len(raw)} bytes) - quiet entry, logger not running, or watcher started late",
                      flush=True)
                sys.exit(4)
            if now - last_growth >= a.stall_timeout:
                if in_window:
                    print(f"UNOBSERVABLE: the log has not grown for {a.stall_timeout:.0f} s "
                          f"(+{now - start:.0f} s, {len(raw)} bytes)", flush=True)
                    sys.exit(4)
                if not silent_noted:
                    silent_noted = True
                    print(f"note: log silent since +{last_growth - start:.0f} s; lines after that cannot be checked",
                          flush=True)

            if cam is not None and cam.poll() is None and now - cam_start > a.camera_timeout:
                cam.kill()
                cam.wait()
                print(f"camera: ffmpeg killed after {a.camera_timeout:.0f} s", flush=True)
            if a.camera_every > 0 and now - last_frame > a.camera_every and (cam is None or cam.poll() is not None):
                last_frame = cam_start = now
                try:
                    cam = subprocess.Popen(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-input_format", "mjpeg",
                         "-video_size", "1920x1080", "-framerate", "30", "-i", "/dev/video0",
                         "-vf", "select=gte(n\\,45)", "-frames:v", "1", "-y",
                         str(a.frames / f"f{int(now - start):04d}.jpg")], stdin=subprocess.DEVNULL)
                except OSError as e:
                    print(f"camera: {e}", flush=True)
                    cam = None
            time.sleep(1)
    finally:
        if cam is not None and cam.poll() is None:
            cam.kill()
    if not verified:
        print(f"UNOBSERVABLE: the {a.seconds:g} s window ended before this boot's kernel command line showed up "
              f"(log {a.log}, {len(raw)} bytes)", flush=True)
        sys.exit(4)
    tail = f"; log silent since +{last_growth - start:.0f} s" if silent_noted else ""
    print(f"watch window over, this boot observed, no danger line seen{tail}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
