#!/usr/bin/env python3
"""Small serial helper for the KANO over Cr50 CCD.

  kano-ser.py ec "<command>" [seconds]      send one allowlisted EC console command, print the reply (default 1.5 s)
  kano-ser.py ap-log <file>                 append everything from the AP UART to <file> until killed
  kano-ser.py ap-send "<line>"              write one line (plus CR) to the AP UART
  kano-ser.py ap-keys <hex>                 write raw bytes (hex) to the AP UART, e.g. 1b for ESC
  kano-ser.py ap-type "<line>" [ms]         like ap-send, but one byte at a time with a pause (default 15 ms);
                                            GRUB drops input when a whole long line arrives at once
  kano-ser.py ap-spam <hex> <log> <text> [s] send <hex> every 0.25 s until <text> shows up in <log> (default 90 s)
  kano-ser.py menu <log> <title>            list the systemd-boot menu drawn in an AP UART log and the index of the
                                            entry <title> names: a substring that must match exactly one title, or
                                            "=<exact title>". Exit 4 no menu, 5 no such entry, 6 ambiguous. No port.
  kano-ser.py editor <log> <offset> <s>     wait up to <s> seconds until the log bytes after <offset> show the
                                            systemd-boot line editor: an entry's options (text with a name=value
                                            kernel parameter) drawn at column 2 of the menu's status row. Exit 0 seen,
                                            3 not seen, 4 no menu in the log. No port.

EC console: only the commands in EC_ALLOW are sent, in these exact shapes:

  apshutdown
  powerbtn
  powerbtn <ms>                    1..20000; the old rule still allows leading zeros
  kbpress <col> <row>              col 0..17, one or two digits, leading zeros allowed; row 0..7, one digit
  kbpress <col> <row> <0|1>        same col and row; state is the literal 0 or 1
  version
  battery
  powerinfo
  kblight                          read-only; any case, spaces normalized
  kblight <p>                      canonical decimal 0..100; the raw line must already be exact
  gpioget
  gpioget <name>                   1..48 characters from [A-Za-z0-9_]
  pwmduty                          the raw line must already be exact
  pwmduty 0                        the raw line must already be exact
  pwmduty 0 <d>                    d is -1 or canonical decimal 0..100; the raw line must already be exact
  gpioset EC_KB_BL_EN_L <0|1>      the raw line must already be exact; the GPIO name is case-exact
  sysinfo                          the raw line must already be exact
  cbi                              the raw line must already be exact; arguments may enter CBI write
                                   operations, so they are refused
  lidclose
  lidopen
  lidstate

This transport is a general maintenance tool. Its allowlist is a second layer that bounds which EC commands can be
sent at all. Sessions go through kano-session-lib, which is narrower (no apshutdown, powerbtn only without a
duration, and the session hardware lock). This transport does not check that lock.

The line is checked before any port is opened: control characters (CR, LF, NUL, TAB, ...), non-ASCII characters, any
command not on the allowlist and malformed arguments are refused with exit status 2. Older command names are
compared case-insensitively and their spaces are normalized. The new shapes above (pwmduty, gpioset EC_KB_BL_EN_L,
kblight <p>, sysinfo, cbi) are accepted only when the raw line is already lower-case, uses single ASCII spaces, and
has no leading or trailing space. Arguments stay case-exact. The EC itself would accept unique prefixes, so a prefix
never passes.

Keyboard-backlight writes (pwmduty 0 <d>, gpioset EC_KB_BL_EN_L, kblight <p>) change EC RAM only. An EC reset
restores them, and the EC re-drives EC_KB_BL_EN_L itself on AP suspend/resume.

The allowlist covers the EC console only. The ap-* subcommands write free text or raw bytes to the AP UART (firmware
and bootloader menus, a maintenance shell) and are not filtered: whatever you type there runs with the rights of
whatever is listening. Nothing here opens the SPI programmer.

Ports and the pyserial location come from kano-env.local.sh / kano-env.sh (KANO_EC_TTY, KANO_AP_TTY,
KANO_PYSERIAL_ROOT; empty = the system's pyserial). An unset port is refused (exit 2) before anything is sent; pyserial
is imported only when a port is actually opened.
"""
import os, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_env():
    """Fill os.environ from kano-env.local.sh (optional: the developer's own values) and then kano-env.sh, lines
    `: "${NAME:=value}"`: the first non-empty value wins, and a value already in the environment wins over both."""
    for name in ("kano-env.local.sh", "kano-env.sh"):
        if not (HERE / name).exists():
            continue
        for line in (HERE / name).read_text().splitlines():
            m = re.fullmatch(r'\s*: "\$\{(\w+):=(.*)\}"\s*(#.*)?', line)
            if m and not os.environ.get(m.group(1)):
                os.environ[m.group(1)] = os.path.expandvars(m.group(2))


def require(*names, serial=False):
    """Refuse (exit 2) unless every name is set (and, with serial=True, alphanumeric), before any action."""
    for n in names:
        v = os.environ.get(n, "")
        if not v or (serial and not re.fullmatch(r"[A-Za-z0-9]+", v)):
            print(f"refusing: {n} is {'not set' if not v else 'not an alphanumeric serial'}; put it in "
                  f"kano-env.local.sh or the environment (see README.md)", file=sys.stderr)
            sys.exit(2)


def _canon_0_100(t):
    """Canonical decimal 0..100: digits only, no sign, no leading zero, no hex."""
    return re.fullmatch(r"0|[1-9][0-9]{0,2}", t) is not None and int(t) <= 100


# command -> accepted argument shapes; each shape is a tuple of argument kinds (see ARG)
EC_ALLOW = {
    "apshutdown": [()],
    "powerbtn": [(), ("ms",)],
    "kbpress": [("col", "row"), ("col", "row", "state")],
    "version": [()],
    "battery": [()],
    "powerinfo": [()],
    "kblight": [(), ("pct",)],      # read (old: case and spaces normalized); a percent is an exact raw shape
    "gpioget": [(), ("gpio",)],
    # channel 0 is the keyboard backlight; channel 1 is the fan. Duty is -1 or canonical 0..100.
    # Exact raw shape: lower-case name, single ASCII spaces, no leading or trailing space.
    "pwmduty": [(), ("ch0",), ("ch0", "duty")],
    # case-exact name; the second argument is 0 or 1. No other GPIO name is accepted. Exact raw shape.
    "gpioset": [("kblgpio", "state")],
    "sysinfo": [()],                 # exact raw shape; no-argument read only
    "cbi": [()],  # exact raw shape; no-argument read only; arguments may enter CBI write operations, so they are refused
    # simulated lid close/open: the EC's debounced lid state in RAM plus the same hook and host event as the real lid
    # switch (common/lid_switch.c); no flash, CBI or power-sequencing write. Always end a session with lidopen.
    "lidclose": [()],
    "lidopen": [()],
    "lidstate": [()],
}
ARG = {
    "ms": lambda t: re.fullmatch(r"[0-9]{1,5}", t) is not None and 1 <= int(t) <= 20000,
    "col": lambda t: re.fullmatch(r"[0-9]{1,2}", t) is not None and int(t) <= 17,
    "row": lambda t: re.fullmatch(r"[0-9]", t) is not None and int(t) <= 7,
    "state": lambda t: t in ("0", "1"),
    "gpio": lambda t: re.fullmatch(r"[A-Za-z0-9_]{1,48}", t) is not None,
    "pct": lambda t: _canon_0_100(t),
    "duty": lambda t: t == "-1" or _canon_0_100(t),
    "ch0": lambda t: t == "0",       # literal channel 0; any other channel (the fan) is refused
    "kblgpio": lambda t: t == "EC_KB_BL_EN_L",
}


def _exact_raw(name, args):
    """True when the raw line must already equal its normalized form.

    New shapes from kano-session-lib v11.7 `_kano_ec` (pwmduty, gpioset EC_KB_BL_EN_L, kblight <p>, sysinfo, cbi):
    lower-case command name, single ASCII spaces, no leading or trailing space. Read-only kblight and the older
    commands keep case-folding and collapsed spaces.
    """
    return name in ("pwmduty", "gpioset", "sysinfo", "cbi") or (name == "kblight" and bool(args))


def check_ec_command(line):
    """Return (normalized_line, None) for an allowed EC console command, else (None, reason). No I/O."""
    if not isinstance(line, str):
        return None, "not a string"
    for ch in line:
        o = ord(ch)
        if o < 0x20 or o == 0x7F:
            return None, f"control character {o:#04x}"
        if o > 0x7E:
            return None, "non-ASCII character"
    tokens = [t for t in line.split(" ") if t]
    if not tokens:
        return None, "empty command"
    name, args = tokens[0].lower(), tokens[1:]
    shapes = EC_ALLOW.get(name)
    if shapes is None:
        return None, f"'{name}' is not on the EC allowlist"
    for shape in shapes:
        if len(shape) == len(args) and all(ARG[kind](a) for kind, a in zip(shape, args)):
            norm = " ".join([name] + args)
            if _exact_raw(name, args) and line != norm:
                return None, "not the exact raw shape"
            return norm, None
    return None, f"arguments not allowed for '{name}': {' '.join(args)!r}"


CSI = re.compile(rb"\x1b\[([0-9;?]*)([A-Za-z])")


COUNTDOWN = re.compile(rb"Boot in [0-9]+ ?s\.")     # the status line: "Boot in 5s." / "Boot in 5 s."
OPTION = re.compile(rb"[A-Za-z0-9_.-]+=[^\s\x1b]")   # a kernel parameter name=value


def screen(seg):
    """Text per screen row of an escape-coded draw, and the row the cursor ends on. The firmware positions the cursor
    with ESC[row;colH for every line, so text is collected per screen row; colour changes do not split a row."""
    rows, row, pos = {}, 0, 0

    def put(text):
        nonlocal row
        for k, piece in enumerate(text.replace(b"\r", b"").split(b"\n")):
            if k:
                row += 1
            rows[row] = rows.get(row, b"") + piece
    for m in CSI.finditer(seg):
        put(seg[pos:m.start()])
        pos = m.end()
        if m.group(2) in (b"H", b"f"):
            row = int((m.group(1).split(b";")[0] or b"1").lstrip(b"?") or b"1")
    put(seg[pos:])
    return rows, row


def menu_draw(raw):
    """(titles top to bottom, status row) of the first systemd-boot menu draw in an AP UART log; None if there is none.

    The menu draw starts with a clear screen (ESC[2J; failing that, the first cursor positioning) and ends at the
    first countdown status line: "Boot in <n> s." alone on its screen row (padding aside). An entry title that only
    contains "Boot in" (e.g. "Boot into Linux") does not end it. Every other non-blank row is a title, whatever it
    says: no fixed list of known titles."""
    data = raw.replace(b"\0", b"")
    for m in COUNTDOWN.finditer(data):
        start = data.rfind(b"\x1b[2J", 0, m.start())
        if start < 0:
            first = re.search(rb"\x1b\[[0-9]*;?[0-9]*[Hf]", data[:m.start()])
            if not first:
                continue
            start = first.start()
        rows, status = screen(data[start:m.end()])
        if rows.get(status, b"").strip() != m.group(0):
            continue
        titles = [rows[r].decode("ascii", "replace").strip() for r in sorted(rows) if r != status]
        return [t for t in titles if t], status
    return None


def menu_titles(raw):
    """Entry titles of the first systemd-boot menu draw (see menu_draw), top to bottom; None if there is none."""
    d = menu_draw(raw)
    return d[0] if d else None


def editor_open(new, status):
    """True if `new` (AP UART bytes written after `e` was sent) holds the systemd-boot line editor: the cursor put on
    column 2 of the status row, then an entry's options, i.e. text with a name=value kernel parameter. With "editor
    no" the menu ignores `e` and draws nothing there."""
    edit = re.compile(rb"\x1b\[0*%d;0*2H(?:\x1b\[[0-9;]*m)*([^\x1b]*)" % status)
    return any(OPTION.search(m.group(1)) for m in edit.finditer(new.replace(b"\0", b"")))


def pick_entry(titles, want):
    """(index, None) of the one title `want` names, else (None, (exit status, reason)).
    "=<title>" must equal a title; anything else is a substring that must occur in exactly one title."""
    if want.startswith("="):
        hits = [i for i, t in enumerate(titles) if t == want[1:]]
    else:
        hits = [i for i, t in enumerate(titles) if want and want in t]
    if not hits:
        return None, (5, f"no menu entry matches {want!r}")
    if len(hits) > 1:
        return None, (6, f"{want!r} matches {len(hits)} entries ({'; '.join(titles[i] for i in hits)}); "
                         f"use more of the title, or '=<exact title>'")
    return hits[0], None


def number(text, what, lo, hi):
    try:
        v = float(text)
    except ValueError:
        v = None
    if v is None or not lo <= v <= hi:
        print(f"refuse: {what} must be a number in [{lo}, {hi}], got {text!r}", file=sys.stderr)
        sys.exit(2)
    return v


def hexbytes(text):
    try:
        return bytes.fromhex(text)
    except ValueError:
        print(f"refuse: not a hex byte string: {text!r}", file=sys.stderr)
        sys.exit(2)


def port(dev, timeout=0.2):
    if os.environ.get("KANO_PYSERIAL_ROOT"):
        root = Path(os.environ["KANO_PYSERIAL_ROOT"])
        for p in [root / "third_party" / "pyserial", *sorted((root / "tools" / "root" / "usr" / "lib").glob("python*/site-packages"))]:
            if p.is_dir() and str(p) not in sys.path:
                sys.path.insert(0, str(p))
    import serial  # only here: refused commands never load pyserial or touch a port
    try:
        return serial.Serial(dev, 115200, timeout=timeout, exclusive=False)
    except TypeError:
        return serial.Serial(dev, 115200, timeout=timeout)


def usage():
    sys.exit(__doc__)


def main(argv):
    load_env()
    if not argv:
        usage()
    cmd = argv[0]
    if cmd.startswith("ap-"):
        require("KANO_AP_TTY")
    if cmd == "ec":
        if len(argv) not in (2, 3):
            usage()
        line, reason = check_ec_command(argv[1])
        if reason:
            print(f"refuse: EC command {argv[1]!r}: {reason}", file=sys.stderr)
            sys.exit(2)
        wait = number(argv[2], "seconds", 0, 60) if len(argv) == 3 else 1.5
        require("KANO_EC_TTY")
        s = port(os.environ["KANO_EC_TTY"])
        s.reset_input_buffer()
        s.write(b"\r" + line.encode("ascii") + b"\r")
        end, out = time.time() + wait, b""
        while time.time() < end:
            out += s.read(4096)
        print(out.decode(errors="replace"))
    elif cmd == "ap-log" and len(argv) == 2:
        s, f = port(os.environ["KANO_AP_TTY"], 0.5), open(argv[1], "ab", buffering=0)
        while True:
            b = s.read(4096)
            if b:
                f.write(b)
    elif cmd == "ap-send" and len(argv) == 2:
        port(os.environ["KANO_AP_TTY"]).write(argv[1].encode() + b"\r")
    elif cmd == "ap-type" and len(argv) in (2, 3):
        pause = (number(argv[2], "ms", 0, 1000) if len(argv) == 3 else 15) / 1000
        s = port(os.environ["KANO_AP_TTY"])
        for b in argv[1].encode() + b"\r":
            s.write(bytes([b]))
            s.flush()
            time.sleep(pause)
    elif cmd == "ap-spam" and len(argv) in (4, 5):
        data, log, want = hexbytes(argv[1]), argv[2], argv[3].encode()
        secs = number(argv[4], "seconds", 0, 3600) if len(argv) == 5 else 90
        s = port(os.environ["KANO_AP_TTY"])
        end = time.time() + secs
        while time.time() < end:
            s.write(data)
            s.flush()
            time.sleep(0.25)
            text = re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]", b"", open(log, "rb").read().replace(b"\0", b""))
            if want in text:
                print("seen")
                return
        sys.exit("timeout")
    elif cmd == "menu" and len(argv) == 3:
        titles = menu_titles(Path(argv[1]).read_bytes()) if Path(argv[1]).exists() else None
        if not titles:
            print(f"no systemd-boot menu draw in {argv[1]}", file=sys.stderr)
            sys.exit(4)
        for t in titles:
            print(f"menu: {t}")
        idx, err = pick_entry(titles, argv[2])
        if err:
            print(f"refuse: {err[1]}", file=sys.stderr)
            sys.exit(err[0])
        print(f"index {idx}")
        print(f"title {titles[idx]}")
    elif cmd == "editor" and len(argv) == 4:
        log, secs = Path(argv[1]), number(argv[3], "seconds", 0, 120)
        try:
            offset = int(argv[2].strip())
        except ValueError:
            offset = -1
        if offset < 0:
            print(f"refuse: offset must be a byte count, got {argv[2]!r}", file=sys.stderr)
            sys.exit(2)
        end = time.time() + secs
        while True:
            raw = log.read_bytes() if log.exists() else b""
            d = menu_draw(raw[:offset])
            if not d:
                print(f"no systemd-boot menu draw before byte {offset} of {log}", file=sys.stderr)
                sys.exit(4)
            if editor_open(raw[offset:], d[1]):
                print("editor open")
                return
            if time.time() >= end:
                print(f"the line editor did not show up within {secs:g} s", file=sys.stderr)
                sys.exit(3)
            time.sleep(0.2)
    elif cmd == "ap-keys" and len(argv) == 2:
        data = hexbytes(argv[1])
        port(os.environ["KANO_AP_TTY"]).write(data)
    else:
        usage()


if __name__ == "__main__":
    main(sys.argv[1:])
