#!/usr/bin/env python3
"""Run kano_autooff.script() on a fake root.

sleep and reboot are PATH stubs. cat and wc are the host binaries. The host
reboot is not on PATH. A symlink to the right nonce stays on: the bytes have
to be a regular file on /data, not a pointer at the vendor copy or another
disk. A hard link of a regular file is accepted because a hard link cannot
cross the vendor erofs and the userdata f2fs.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder" / "lib"))
import kano_autooff  # noqa: E402

NONCE = b"0123456789abcdef0123456789abcdef\n"
OTHER = b"fedcba9876543210fedcba9876543210\n"
UPPER = b"abcdefabcdefabcdefabcdefabcdefab\n".upper()
HEX31 = b"0123456789abcdef0123456789abcde\n"
HEX33 = b"abcdefabcdefabcdefabcdefabcdefabc\n"
CRLF = NONCE[:-1] + b"\r\n"
TRAIL = NONCE + b"x"
NO_LF = NONCE[:-1]
NUL = NONCE[:10] + b"\x00" + NONCE[11:]
BEFORE = b"10.50 1.00\n"
AFTER = b"70.50 1.00\n"
EXPECTED = ["cat", "wc", "cat", "wc", "cat", "wc", "sleep", "cat", "wc", "cat", "wc", "cat", "wc"]

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"PASS {name}")
        return
    FAILS.append(name)
    print(f"FAIL {name}: {detail}")


def require_tools() -> dict[str, str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SystemExit("refusing to run as root (mode 000 would still be readable)")
    found = {}
    for name in ("cat", "wc", "rm", "ln", "chmod"):
        path = shutil.which(name)
        if not path:
            raise SystemExit(f"need {name} on PATH")
        found[name] = path
    shell = "/bin/sh" if Path("/bin/sh").is_file() else "sh"
    proc = subprocess.run([shell, "-c", "command -V printf"], capture_output=True, text=True)
    if proc.returncode != 0 or "builtin" not in proc.stdout:
        raise SystemExit(f"printf is not a shell builtin: {proc.stdout!r} {proc.stderr!r}")
    found["shell"] = shell
    return found


def write_stubs(bindir: Path, tools: dict[str, str]) -> Path:
    bindir.mkdir(parents=True)
    sleep = f"""#!/bin/sh
printf 'sleep %s\\n' "$*" >> "$KANO_CMDLOG"
if [ "${{KANO_SLEEP_RC:-0}}" != 0 ]; then
  exit "$KANO_SLEEP_RC"
fi
if [ -n "${{KANO_UPTIME_AFTER:-}}" ]; then
  {tools["cat"]} "$KANO_UPTIME_AFTER" > "$KANO_UPTIME_PATH"
fi
case "${{KANO_FLIP:-}}" in
  "") ;;
  replace)
    {tools["cat"]} "$KANO_FLIP_SRC" > "$KANO_FLIP_DEST"
    ;;
  delete)
    {tools["rm"]} -f "$KANO_FLIP_DEST"
    ;;
  symlink)
    {tools["rm"]} -f "$KANO_FLIP_DEST"
    {tools["ln"]} -s "$KANO_FLIP_SRC" "$KANO_FLIP_DEST"
    ;;
  *)
    printf 'bad flip\\n' >&2
    exit 1
    ;;
esac
exit 0
"""
    reboot = """#!/bin/sh
printf 'reboot %s\\n' "$*" >> "$KANO_CMDLOG"
exit 0
"""
    (bindir / "sleep").write_text(sleep, newline="\n")
    (bindir / "reboot").write_text(reboot, newline="\n")
    os.chmod(bindir / "sleep", 0o755)
    os.chmod(bindir / "reboot", 0o755)
    sweep = bindir / "sweep"
    sweep.mkdir()
    template = """#!/bin/sh
if [ -n "${KANO_FAULT_COUNTER:-}" ]; then
  n=0
  if [ -f "$KANO_FAULT_COUNTER" ]; then
    IFS= read -r n < "$KANO_FAULT_COUNTER" || n=0
  fi
  case "$n" in
    ""|*[!0-9]*) n=0 ;;
  esac
  n=$((n + 1))
  printf '%s\\n' "$n" > "$KANO_FAULT_COUNTER"
  if [ -n "${KANO_FAULT_TRACE:-}" ]; then
    printf '%s __TOOL__\\n' "$n" >> "$KANO_FAULT_TRACE"
  fi
  if [ "${KANO_FAULT_AT:-0}" -gt 0 ] && [ "$n" -eq "${KANO_FAULT_AT:-0}" ]; then
    case "${KANO_FAULT_MODE:-}" in
      exit1)
        __REAL__ "$@"
        exit 1
        ;;
      empty)
        exit 0
        ;;
      garbage)
        printf 'GARBAGE\\n'
        exit 0
        ;;
    esac
  fi
fi
exec __REAL__ "$@"
"""
    reals = {"cat": tools["cat"], "wc": tools["wc"], "sleep": str(bindir / "sleep")}
    for name, real in reals.items():
        text = template.replace("__TOOL__", name).replace("__REAL__", real)
        path = sweep / name
        path.write_text(text, newline="\n")
        os.chmod(path, 0o755)
    return sweep


def assert_commands(path: str, bindir: Path, reboot_stub: bool) -> None:
    shell = "/bin/sh" if Path("/bin/sh").is_file() else "sh"
    proc = subprocess.run(
        [shell, "-c", "command -v reboot; command -v sleep"],
        env={"PATH": path, "LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=False,
    )
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or len(lines) != 2:
        raise SystemExit(f"command -v failed: {proc.stdout!r} {proc.stderr!r}")
    if reboot_stub and lines[0] != str(bindir / "reboot"):
        raise SystemExit(f"reboot is not the stub: {lines[0]}")
    if "reboot" in lines[0] and "/sbin/" in lines[0]:
        raise SystemExit(f"refusing real reboot on PATH: {lines[0]}")


def prepare(root: Path, vendor: bytes | None, data: bytes | None, uptime: bytes | None) -> tuple[Path, Path]:
    (root / "vendor" / "etc").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "proc").mkdir()
    (root / "dev").mkdir()
    (root / "dev" / "kmsg").write_bytes(b"")
    vpath = root / "vendor" / "etc" / "kano-autooff.nonce"
    dpath = root / "data" / "kano-autooff.nonce"
    if vendor is not None:
        vpath.write_bytes(vendor)
    if data is not None:
        dpath.write_bytes(data)
    if uptime is not None:
        (root / "proc" / "uptime").write_bytes(uptime)
    return vpath, dpath


def run_script(shell: str, script: Path, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shell, str(script)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def outcome(proc: subprocess.CompletedProcess[str], root: Path, cmdlog: Path) -> dict:
    kmsg_path = root / "dev" / "kmsg"
    kmsg = kmsg_path.read_text(errors="replace") if kmsg_path.is_file() else ""
    log = cmdlog.read_text(errors="replace").splitlines() if cmdlog.is_file() else []
    return {
        "rc": proc.returncode,
        "kmsg": kmsg,
        "log": log,
        "err": proc.stderr,
        "out": proc.stdout,
    }


def stayed(got: dict) -> bool:
    reboots = [ln for ln in got["log"] if ln.startswith("reboot")]
    return (
        got["rc"] == 0
        and reboots == []
        and "powering off" not in got["kmsg"]
        and "staying on" in got["kmsg"]
        and got["err"] == ""
    )


def powered(got: dict) -> bool:
    return (
        got["rc"] == 0
        and got["log"] == ["sleep 60", "reboot -p"]
        and "KANO-AUTOOFF: armed" in got["kmsg"]
        and "KANO-AUTOOFF: powering off" in got["kmsg"]
        and "staying on" not in got["kmsg"]
        and got["err"] == ""
        and got["out"] == ""
    )


def detail(got: dict) -> str:
    return f"rc={got['rc']} log={got['log']} kmsg={got['kmsg']!r} err={got['err']!r} out={got['out']!r}"


def base_env(path: str, root: Path, cmdlog: Path, after: Path) -> dict[str, str]:
    return {
        "PATH": path,
        "LC_ALL": "C",
        "KANO_AUTOOFF_ROOT": str(root),
        "KANO_CMDLOG": str(cmdlog),
        "KANO_UPTIME_PATH": str(root / "proc" / "uptime"),
        "KANO_UPTIME_AFTER": str(after),
    }


def write_after(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


def check_static(text: str, shell: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as handle:
        handle.write(text)
        name = handle.name
    try:
        proc = subprocess.run([shell, "-n", name], capture_output=True, text=True)
    finally:
        os.unlink(name)
    check("sh -n generated script", proc.returncode == 0, proc.stderr.strip())
    code = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    reboots = [ln.strip() for ln in code if "reboot" in ln.split()[:1] or ln.strip().startswith("reboot")]
    check("one reboot -p", reboots == ["reboot -p"], str(reboots))
    body = "\n".join(code)
    for bad in ("readlink", "getprop", "setprop", "uevent", "/sys/", "awk", "eval", "`", "stat ", " ls", "head ", " od ", " dd ", "cmp "):
        check(f"no {bad.strip()}", bad not in body, "present")
    check("wait literal once", text.count("_kano_wait=60\n") == 1, text)


def check_install() -> None:
    like = {"mode": 0o644, "uid": 0, "gid": 0, "selinux": "u:object_r:vendor_configs_file:s0", "caps": 0}
    sentinel = "caller-should-return"
    previous = os.environ.get("GBOS_OUT")
    os.environ["GBOS_OUT"] = sentinel
    try:
        _check_install_work(like)
    finally:
        restored = os.environ.get("GBOS_OUT")
        if previous is None:
            os.environ.pop("GBOS_OUT", None)
        else:
            os.environ["GBOS_OUT"] = previous
    check("GBOS_OUT restored", restored == sentinel, repr(restored))


def _check_install_work(like: dict) -> None:
    old_gbos_out = os.environ.get("GBOS_OUT")
    try:
        _check_install_body(like)
    finally:
        if old_gbos_out is None:
            os.environ.pop("GBOS_OUT", None)
        else:
            os.environ["GBOS_OUT"] = old_gbos_out


def _check_install_body(like: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="kano-autooff-install-") as tmp:
        base = Path(tmp)
        nonces = []
        scripts = []
        for index in (1, 2):
            tree = base / f"tree{index}"
            (tree / "etc" / "init").mkdir(parents=True)
            vmap = {"etc/init/fwupd.rc": dict(like)}
            os.environ["GBOS_OUT"] = str(base / f"out{index}")
            kano_autooff.install(tree, vmap, 60)
            vendor = (tree / "etc" / "kano-autooff.nonce").read_bytes()
            side = (base / f"out{index}" / "image" / "autooff.nonce").read_bytes()
            nonces.append(vendor)
            scripts.append((tree / "etc" / "kano-autooff.sh").read_bytes())
            mode = (tree / "etc" / "kano-autooff.nonce").stat().st_mode & 0o777
            check(
                f"build {index} nonce format and sidecar",
                vendor == side and re.fullmatch(rb"[0-9a-f]{32}\n", vendor) is not None and mode == 0o444,
                f"mode={oct(mode)} vendor={vendor!r} side={side!r}",
            )
            check(
                f"build {index} vmap nonce is 0444",
                vmap["etc/kano-autooff.nonce"]["mode"] == 0o444
                and vmap["etc/kano-autooff.sh"]["mode"] == 0o644,
                str(vmap["etc/kano-autooff.nonce"]),
            )
        check("two builds differ", nonces[0] != nonces[1] and scripts[0] == scripts[1], nonces[0].hex())
        os.environ.pop("GBOS_OUT", None)
        tree = base / "bare"
        (tree / "etc" / "init").mkdir(parents=True)
        try:
            kano_autooff.install(tree, {"etc/init/fwupd.rc": dict(like)}, 60)
            check("absent without GBOS_OUT", False, "install returned")
        except ValueError:
            check("absent without GBOS_OUT", not (tree / "etc" / "kano-autooff.nonce").exists(), "nonce written")


def one_case(shell: str, script: Path, bindir_path: str, spec: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="kano-autooff-case-") as tmp:
        work = Path(tmp)
        root = work / "root"
        root.mkdir()
        vendor = spec.get("vendor", NONCE)
        data = spec.get("data", NONCE)
        uptime = spec.get("uptime", BEFORE)
        vpath, dpath = prepare(root, vendor, data, uptime)
        if spec.get("drop_vendor"):
            vpath.unlink()
        if spec.get("drop_data"):
            if dpath.exists():
                dpath.unlink()
        if spec.get("symlink_data"):
            if dpath.exists():
                dpath.unlink()
            os.symlink(vpath, dpath)
        if spec.get("symlink_vendor"):
            hold = root / "hold"
            hold.mkdir()
            real = hold / "nonce"
            real.write_bytes(NONCE)
            if vpath.exists() or vpath.is_symlink():
                vpath.unlink()
            os.symlink(real, vpath)
            dpath.write_bytes(NONCE)
        if spec.get("hardlink_data"):
            hold = root / "data" / "inode"
            hold.write_bytes(NONCE)
            if dpath.exists():
                dpath.unlink()
            os.link(hold, dpath)
        if spec.get("data_dir"):
            if dpath.exists():
                dpath.unlink()
            dpath.mkdir()
        if spec.get("data_fifo"):
            if dpath.exists():
                dpath.unlink()
            os.mkfifo(dpath)
        locked = spec.get("lock")
        if locked == "data":
            os.chmod(dpath, 0)
            if os.access(dpath, os.R_OK):
                os.chmod(dpath, 0o644)
                check(spec["name"], False, "chmod 000 did not remove read permission")
                return
        if locked == "vendor":
            os.chmod(vpath, 0)
            if os.access(vpath, os.R_OK):
                os.chmod(vpath, 0o644)
                check(spec["name"], False, "chmod 000 did not remove read permission")
                return
        after = work / "after"
        write_after(after, spec.get("after", AFTER))
        cmdlog = work / "cmdlog"
        env = base_env(bindir_path, root, cmdlog, after)
        if spec.get("sleep_rc"):
            env["KANO_SLEEP_RC"] = str(spec["sleep_rc"])
        if spec.get("flip"):
            env["KANO_FLIP"] = spec["flip"]
            env["KANO_FLIP_DEST"] = str(dpath)
            if spec["flip"] == "replace":
                src = work / "flip"
                src.write_bytes(OTHER)
                env["KANO_FLIP_SRC"] = str(src)
            elif spec["flip"] == "symlink":
                env["KANO_FLIP_SRC"] = str(vpath)
        if spec.get("relative"):
            env["KANO_AUTOOFF_ROOT"] = "not-absolute"
        try:
            try:
                proc = run_script(shell, script, work, env)
            except subprocess.TimeoutExpired:
                check(spec["name"], False, "timed out")
                return
        finally:
            if locked == "data" and dpath.exists() and not dpath.is_symlink():
                os.chmod(dpath, 0o644)
            if locked == "vendor" and vpath.exists() and not vpath.is_symlink():
                os.chmod(vpath, 0o644)
        got = outcome(proc, root, cmdlog)
        if spec.get("relative"):
            ok = (
                got["rc"] == 0
                and got["log"] == []
                and "staying on" in got["err"]
                and "powering off" not in got["kmsg"]
            )
        elif spec.get("power"):
            ok = powered(got)
        else:
            ok = stayed(got)
        check(spec["name"], ok, detail(got))


def cases() -> list[dict]:
    return [
        {"name": "positive identical nonce", "power": True},
        {"name": "hardlink on data powers off", "power": True, "hardlink_data": True},
        {"name": "centiseconds exactly N powers off", "power": True, "uptime": b"10.90 0.00\n", "after": b"70.90 0.00\n"},
        {"name": "missing vendor", "drop_vendor": True},
        {"name": "missing data", "drop_data": True},
        {"name": "different nonce", "data": OTHER},
        {"name": "both uppercase", "vendor": UPPER, "data": UPPER},
        {"name": "data uppercase", "data": UPPER},
        {"name": "both 31 hex", "vendor": HEX31, "data": HEX31},
        {"name": "both 33 hex", "vendor": HEX33, "data": HEX33},
        {"name": "both CRLF", "vendor": CRLF, "data": CRLF},
        {"name": "both trailing data", "vendor": TRAIL, "data": TRAIL},
        {"name": "both no LF", "vendor": NO_LF, "data": NO_LF},
        {"name": "both NUL", "vendor": NUL, "data": NUL},
        {"name": "data NUL", "data": NUL},
        {"name": "symlink to vendor nonce", "symlink_data": True},
        {"name": "vendor symlink to the right bytes", "symlink_vendor": True},
        {"name": "unreadable data", "lock": "data"},
        {"name": "unreadable vendor", "lock": "vendor"},
        {"name": "data is a directory", "data_dir": True, "drop_data": True},
        {"name": "data is a fifo", "data_fifo": True, "drop_data": True},
        {"name": "nonce changes before power-off", "flip": "replace"},
        {"name": "nonce deleted before power-off", "flip": "delete"},
        {"name": "nonce becomes a symlink before power-off", "flip": "symlink"},
        {"name": "short uptime", "after": b"70.49 1.00\n"},
        {"name": "centiseconds short by one", "uptime": b"10.90 0.00\n", "after": b"70.89 0.00\n"},
        {"name": "sleep fails", "sleep_rc": 1},
        {"name": "uptime missing", "uptime": None},
        {"name": "uptime NUL", "uptime": b"10.50\x00 1.00\n"},
        {"name": "uptime leading zero", "uptime": b"08.00 1.00\n"},
        {"name": "relative root", "relative": True},
    ]


def fault_sweep(shell: str, script: Path, tools: dict[str, str], bindir: Path, sweep: Path, sweep_path: str) -> None:
    with tempfile.TemporaryDirectory(prefix="kano-autooff-sweep-") as tmp:
        work = Path(tmp)
        root = work / "base"
        root.mkdir()
        prepare(root, NONCE, NONCE, BEFORE)
        after = work / "after"
        write_after(after, AFTER)
        counter = work / "counter"
        trace = work / "trace"
        cmdlog = work / "cmdlog"
        env = base_env(sweep_path, root, cmdlog, after)
        env["KANO_FAULT_COUNTER"] = str(counter)
        env["KANO_FAULT_TRACE"] = str(trace)
        env["KANO_FAULT_AT"] = "0"
        proc = run_script(shell, script, work, env)
        got = outcome(proc, root, cmdlog)
        parsed = []
        if trace.is_file():
            for line in trace.read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0].isdigit():
                    parsed.append(parts[1])
        check(
            "fault-sweep baseline positive run",
            powered(got) and parsed == EXPECTED,
            f"trace={parsed} {detail(got)}",
        )
        if parsed != EXPECTED:
            return
        runs = 0
        poweroffs = 0
        for index, tool in enumerate(EXPECTED, start=1):
            for mode in ("exit1", "empty", "garbage"):
                runs += 1
                slot = work / f"s{index}-{mode}"
                sroot = slot / "root"
                sroot.mkdir(parents=True)
                prepare(sroot, NONCE, NONCE, BEFORE)
                scmd = slot / "cmdlog"
                scounter = slot / "counter"
                env = base_env(sweep_path, sroot, scmd, after)
                env["KANO_FAULT_COUNTER"] = str(scounter)
                env["KANO_FAULT_AT"] = str(index)
                env["KANO_FAULT_MODE"] = mode
                label = f"fault k={index}/{len(EXPECTED)} {tool} mode={mode}"
                try:
                    sproc = run_script(shell, script, slot, env)
                except subprocess.TimeoutExpired:
                    check(label, False, "timed out")
                    poweroffs += 1
                    continue
                sgot = outcome(sproc, sroot, scmd)
                reboots = [ln for ln in sgot["log"] if ln.startswith("reboot")]
                ok = stayed(sgot)
                if reboots:
                    poweroffs += len(reboots)
                check(label, ok, detail(sgot))
        check(
            f"fault-injection sweep ({len(EXPECTED)} invocations x 3 modes = {runs} runs)",
            poweroffs == 0 and not any(name.startswith("fault k=") for name in FAILS),
            f"poweroffs={poweroffs}",
        )
        print(
            f"FAULT INJECTION SWEEP: K={len(EXPECTED)} invocations x 3 modes = {runs} runs, "
            f"{poweroffs} poweroffs"
        )


def main() -> None:
    lengths = {
        "NONCE": (NONCE, 33),
        "OTHER": (OTHER, 33),
        "UPPER": (UPPER, 33),
        "HEX31": (HEX31, 32),
        "HEX33": (HEX33, 34),
        "CRLF": (CRLF, 34),
        "TRAIL": (TRAIL, 34),
        "NO_LF": (NO_LF, 32),
        "NUL": (NUL, 33),
    }
    for name, (payload, size) in lengths.items():
        if len(payload) != size:
            raise SystemExit(f"{name} length {len(payload)} != {size}")
    if UPPER == NONCE or UPPER != UPPER.lower().upper():
        raise SystemExit("UPPER fixture is not uppercase hex")
    tools = require_tools()
    text = kano_autooff.script(60)
    check_static(text, tools["shell"])
    for bad in (59, 7201, True, 60.0, "2400"):
        try:
            kano_autooff.script(bad)  # type: ignore[arg-type]
        except ValueError:
            check(f"script rejects {bad!r}", True)
        else:
            check(f"script rejects {bad!r}", False, "accepted")
    check_install()
    with tempfile.TemporaryDirectory(prefix="kano-autooff-bin-") as tmp:
        bindir = Path(tmp) / "bin"
        sweep = write_stubs(bindir, tools)
        host_bin = os.path.dirname(tools["cat"])
        wc_bin = os.path.dirname(tools["wc"])
        plain = f"{bindir}{os.pathsep}{host_bin}{os.pathsep}{wc_bin}"
        sweep_path = f"{sweep}{os.pathsep}{plain}"
        assert_commands(plain, bindir, True)
        assert_commands(sweep_path, bindir, True)
        script = Path(tmp) / "kano-autooff.sh"
        script.write_text(text, newline="\n")
        for spec in cases():
            one_case(tools["shell"], script, plain, spec)
        fault_sweep(tools["shell"], script, tools, bindir, sweep, sweep_path)
    if FAILS:
        print(f"{len(FAILS)} failed")
        raise SystemExit(1)
    print("AUTOOFF SCRIPT OK")


if __name__ == "__main__":
    main()
