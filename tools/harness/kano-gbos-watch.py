#!/usr/bin/env python3
"""Watch the KANO AP UART during a Googlebook boot; cut power through the EC on any danger line.

usage: kano-gbos-watch.py <ap-uart.log> <seconds> <frames-dir> [--profile usb|nvme] [--observe-timeout S]
                          [--stall-timeout S] [--camera-every S] [--camera-timeout S] [--ec-timeout S]
       (kano-gbos-watch-nvme.py is this script with --profile nvme: Googlebook on the internal NVMe, so the NVMe
       controller appearing is expected and the command line keeps nvme_init allowed.)

Limits - read before relying on it:
- It only sees what the AP UART prints, so it only works with the serial-debug boot entry (earlycon + console on the
  UART). The quiet entry has no serial console; with it, the watcher reports "unobservable", never "no danger".
- It is a supplementary detector, not a guarantee. It polls the log once a second and reacts to lines after they
  were printed, so anything that happens before, or without, a matching line is not prevented. The protections are
  the kernel command line guards and the updaters removed from the image; this watcher checks the guards are there.

Start it right after power-on, before the kernel starts (the callers truncate the log first). Bytes already in the
log when it starts are this boot's only if they hold no kernel command line yet; otherwise only lines written from
then on count as this boot's. Danger lines are searched in the whole log.

Observability (exit 4, "unobservable"): within --observe-timeout seconds (default 120) this boot's kernel command
line must show up, and during that time the log must never stay silent for --stall-timeout seconds (default 30).
After the observe window a silent log only produces a note (Android legitimately goes quiet once it has booted).

Guards: this boot's kernel command line must carry every module of the module_blacklist in builder/image.py (BL, the
single source; read with ast, not executed), every initcall of its initcall_blacklist (with --profile nvme:
all but nvme_init, which must not be blacklisted) and efi=noruntime, and no spi-intel.* option. The kernel keeps the last module_blacklist= and adds up
every initcall_blacklist=.

exit status: 0 window over, boot observed, no danger line; 2 danger line, EC power cut confirmed; 3 a kernel
command line guard is missing, EC power cut confirmed; 4 unobservable (also when the window ends before this boot's
command line showed up); 5 not started: invalid arguments, KANO_EC_TTY is not set (it could not cut the power) or the
guard list cannot be read from builder/image.py; 6 danger seen but the EC power cut could not be confirmed (cut power
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

COMMON_DANGER = [
    (r"spi-nor spi|\bmtd\d+:|mtdblock\d", "SPI flash / MTD device appeared"),
    (r"cros-ec-dev|cros_ec_lpcs .*(probe|registered)|ChromeOS EC device at", "EC driver bound"),
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
IMAGE_PY = HERE.parent.parent / "builder" / "image.py"


def expected_guards(path=IMAGE_PY):
    """(modules, initcalls, plain options) every Googlebook entry carries, from builder/image.py: BL after its
    .remove() calls, and the initcall_blacklist= and efi= items of `tail`. Parsed with ast, nothing is executed.
    Raises ValueError (or OSError) unless the file has exactly that shape, so a changed builder fails closed."""
    bl, tail, removed = None, None, []
    for node in ast.parse(Path(path).read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ("BL", "tail") for t in node.targets):
            if len(node.targets) != 1 or (bl if node.targets[0].id == "BL" else tail) is not None:
                raise ValueError(f"{node.targets[0].id} is assigned more than once")
            v = ast.literal_eval(node.value)
            if not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
                raise ValueError(f"{node.targets[0].id} is not a list of strings")
            if node.targets[0].id == "BL":
                bl = v
            else:
                tail = v
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "BL":
            raise ValueError("BL is changed with an augmented assignment")
        elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
              and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "BL"):
            if node.value.func.attr != "remove" or len(node.value.args) != 1:
                raise ValueError(f"BL.{node.value.func.attr}() is not understood")
            removed.append(ast.literal_eval(node.value.args[0]))
    if bl is None or tail is None:
        raise ValueError("no BL / tail list")
    modules = set(bl) - set(removed)
    ic = [x.split("=", 1)[1].split(",") for x in tail if x.startswith("initcall_blacklist=")]
    plain = [x for x in tail if x.startswith("efi=")]
    if len(ic) != 1 or not plain or len(modules) < 10:
        raise ValueError("tail has no single initcall_blacklist= / no efi= item, or BL is implausibly short")
    return modules, ic[0], plain


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


def missing_guards(tokens, guards, profile):
    """What the command line `tokens` lacks of `guards` (expected_guards()), as readable items; [] = all there."""
    modules, initcalls, plain = guards
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
        bad(f"cannot read the expected command line guards from {IMAGE_PY}: {e}")
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
            for pat, why in patterns:
                m = re.search(pat, body)
                if m:
                    danger(f"{why}: {m.group(0)!r}", 2, a.ec_timeout)

            cmdlines = [l for l in this.splitlines() if CMDLINE in l]
            # The command line is printed in several chunks; check it once the kernel is past 1.5 s.
            later = [float(t) for t in re.findall(r"^\S*\[\s*(\d+\.\d+)\]", this, re.M)]
            if cmdlines and later and max(later) > 1.5:
                missing = missing_guards(cmdline_tokens("\n".join(cmdlines)), guards, a.profile)
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
