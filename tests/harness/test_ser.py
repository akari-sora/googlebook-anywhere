"""kano-ser.py: EC console allowlist (pure function) and the CLI around it (fake pyserial, no real port)."""
import base64, hashlib, importlib.util, itertools, json, os, re, subprocess, sys, tempfile, unittest
from pathlib import Path
from helpers import HARNESS, HarnessCase

sys.dont_write_bytecode = True
# An eager `import serial` at the top of kano-ser.py (mutant ser-eager-pyserial) must load the project's fake
# pyserial, not the real one some hosts lack. The fake logs nothing unless FAKE_SERIAL_LOG is set.
_FAKE_PYSERIAL = Path(__file__).resolve().parent / "fakes" / "pyserial"
_saved_path = sys.path[:]
sys.path.insert(0, str(_FAKE_PYSERIAL))
try:
    _spec = importlib.util.spec_from_file_location("kano_ser", HARNESS / "kano-ser.py")
    ks = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ks)
finally:
    sys.path[:] = _saved_path

ALLOWED = {
    "apshutdown": "apshutdown",
    "  APSHUTDOWN  ": "apshutdown",
    "powerbtn": "powerbtn",
    "powerbtn 200": "powerbtn 200",
    "PowerBtn   300": "powerbtn 300",
    "kbpress 1 1": "kbpress 1 1",
    "kbpress 1 1 0": "kbpress 1 1 0",
    "kbpress 17 7 1": "kbpress 17 7 1",
    "version": "version",
    "battery": "battery",
    "powerinfo": "powerinfo",
    "kblight": "kblight",
    "  KBLIGHT  ": "kblight",
    "KBLIGHT": "kblight",
    "kblight 0": "kblight 0",
    "kblight 9": "kblight 9",
    "kblight 10": "kblight 10",
    "kblight 99": "kblight 99",
    "kblight 100": "kblight 100",
    "pwmduty": "pwmduty",
    "pwmduty 0": "pwmduty 0",
    "pwmduty 0 -1": "pwmduty 0 -1",
    "pwmduty 0 0": "pwmduty 0 0",
    "pwmduty 0 9": "pwmduty 0 9",
    "pwmduty 0 10": "pwmduty 0 10",
    "pwmduty 0 50": "pwmduty 0 50",
    "pwmduty 0 99": "pwmduty 0 99",
    "pwmduty 0 100": "pwmduty 0 100",
    "gpioset EC_KB_BL_EN_L 0": "gpioset EC_KB_BL_EN_L 0",
    "gpioset EC_KB_BL_EN_L 1": "gpioset EC_KB_BL_EN_L 1",
    "sysinfo": "sysinfo",
    "cbi": "cbi",
    "gpioget": "gpioget",
    "gpioget EC_WP_L": "gpioget EC_WP_L",
    "lidclose": "lidclose",
    "LIDOPEN": "lidopen",
    "lidstate": "lidstate",
}
REFUSED = {
    "flashwrite": "allowlist", "FLASHWR": "allowlist", "flashe": "allowlist", "flasherase 0 4096": "allowlist",
    "flashwp enable": "allowlist", "wp enable": "allowlist", "cbi  set 1 2 3": "arguments",
    "cbi\tset": "control", "powerbtn\rflashwp": "control", "apshutdown\nwp enable": "control",
    "apshutdown\x00": "control", "version\x7f": "control", "i2cxfer w 0 0x10 0": "allowlist",
    "rw 0x40000000 1": "allowlist", "cutoff": "allowlist", "battcutoff": "allowlist", "gpioset EC_WP_L 0": "arguments",
    "sysjump RW": "allowlist", "reboot ro": "allowlist", "reboot": "allowlist", "ccd reset factory": "allowlist",
    "hash": "allowlist", "": "empty", "   ": "empty", "apshutdown ": "non-ASCII", "ｖｅｒｓｉｏｎ": "non-ASCII",
    "apshutdown now": "arguments", "powerbtn -1": "arguments",
    "powerbtn 0": "arguments", "powerbtn 99999": "arguments", "powerbtn 2 3": "arguments", "kbpress 18 1": "arguments",
    "kbpress 1 8": "arguments", "kbpress 1 1 2": "arguments", "kbpress 1": "arguments", "kbpress": "arguments",
    "gpioget A B": "arguments", "gpioget A;B": "arguments", "version x": "arguments", "powerinfo 1": "arguments",
    "lidclose 1": "arguments", "lidopen now": "arguments", "lidstate x": "arguments", "lidangle": "allowlist",
    "tabletmode on": "allowlist",
    "pwmduty 1": "arguments", "pwmduty 1 50": "arguments", "pwmduty 00 5": "arguments",
    "pwmduty 0 101": "arguments", "pwmduty 0 -2": "arguments", "pwmduty 0 -0": "arguments",
    "pwmduty 0 -01": "arguments", "pwmduty 0 0100": "arguments", "pwmduty -0 50": "arguments",
    "pwmduty +0 50": "arguments", "pwmduty 0 +5": "arguments",
    "pwmduty 0 0x10": "arguments", "pwmduty 0 05": "arguments", "pwmduty 0 50 1": "arguments",
    "pwmduty 0 50 1 2": "arguments", "pwmduty 0 50 1 2 3": "arguments",
    "pwmduty 0 50 1 2 3 4": "arguments", "pwmduty 0 50 1 2 3 4 5": "arguments",
    "gpioset ec_kb_bl_en_l 0": "arguments", "gpioset EC_KB_BL_EN_L 2": "arguments",
    "gpioset EC_KB_BL_EN_LX 0": "arguments", "gpioset EC_KB_BL_EN_L 01": "arguments",
    "gpioset EC_FAN_EN 1": "arguments", "gpioset EC_KB_BL_EN_L": "arguments", "gpioset": "arguments",
    "gpioset EC_KB_BL_EN_L 0 1": "arguments", "gpioset EC_KB_BL_EN_L 0 1 x": "arguments",
    "gpioset EC_KB_BL_EN_L 0 1 x y": "arguments", "gpioset EC_KB_BL_EN_L 0 1 x y z": "arguments",
    "gpioset EC_KB_BL_EN_L 0 1 x y z w": "arguments",
    "kblight 101": "arguments", "kblight -1": "arguments", "kblight 007": "arguments",
    "kblight 0100": "arguments",
    "kblight 5 5": "arguments", "kblight 5 5 1": "arguments", "kblight 5 5 1 2": "arguments",
    "kblight 5 5 1 2 3": "arguments", "kblight 5 5 1 2 3 4": "arguments",
    "cbi 0": "arguments", "cbi get 1": "arguments", "cbi remove 1": "arguments",
    "cbi set 6 0": "arguments", "cbi set 6 0 4": "arguments", "cbi set 6 0 4 x": "arguments",
    "sysinfo reboot": "arguments", "sysinfo a": "arguments", "sysinfo a b": "arguments",
    "sysinfo a b c": "arguments", "sysinfo a b c d": "arguments", "sysinfo a b c d e": "arguments",
    "Kblight   50": "exact", "Kblight 100": "exact", "  PWMDuty  ": "exact", "Pwmduty   0": "exact",
    "PWMDuty 0 50": "exact", "PWMDuty   0   50": "exact", "pwmduty  0 50": "exact",
    " pwmduty 0 -1": "exact", "pwmduty 0 50 ": "exact", "GPIOSET EC_KB_BL_EN_L 0": "exact",
    "GPIOSET   EC_KB_BL_EN_L   1": "exact", "SYSINFO": "exact", "  SYSINFO  ": "exact",
    " cbi ": "exact", "  Cbi  ": "exact",
    "pwmduty\n0 50": "control", "pwmduty 0\x00-1": "control", "gpioset\tEC_KB_BL_EN_L 0": "control",
    "kblight\x7f": "control", "sysinfo\r": "control", "cbi\x1b": "control",
    "cbi\n": "control", "sysinfo\n": "control", "pwmduty 0 50\n\n": "control",
    "pwmduty 0 \u22121": "non-ASCII", "kblight\uff11": "non-ASCII",
    "gpioset EC_KB_BL_EN_L \uff10": "non-ASCII", "\uff53ysinfo": "non-ASCII", "cbi\u00a0": "non-ASCII",
}


# Codex I1: raw lines the transport must accept or refuse exactly as the lib does, for the new shapes.
CODEX_I1 = (
    "PWMDuty 0 50",
    "pwmduty  0 50",
    " pwmduty 0 -1",
    "pwmduty 0 50 ",
    "Kblight 100",
    "GPIOSET EC_KB_BL_EN_L 0",
    "SYSINFO",
    " cbi ",
)

_NEW_NAMES = ("pwmduty", "gpioset", "sysinfo", "cbi", "kblight")


def _new_command_row(line):
    """A new-shape row: pwmduty, gpioset, sysinfo, cbi, or kblight with an argument.

    Read-only kblight (the name alone, any case, any spaces) stays on the old normalization rules, so it is not
    compared with the lib.
    """
    parts = [t for t in line.split(" ") if t]
    if not parts:
        return False
    low = parts[0].lower()
    name = next((n for n in _NEW_NAMES if low == n or low.startswith(n)), None)
    if name is None:
        return False
    if name == "kblight" and low == "kblight" and len(parts) == 1:
        return False
    return True


# kano-session-lib v11.7 with its example LAN prefix replaced by 192.0.2. (TEST-NET-1); see tools/harness/README.md.
# The test reads only this path and pins this sha256.
_LIB_SHA256 = "5b75e850ecc78cd5d742cadbde8c195cf1974eaa1422e64dfcd531e8db92f93c"
_LIB_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "kano-session-lib-v117.sh"


def _lib_shape_accepts(line):
    """Python transliteration of kano-session-lib v11.7 `_kano_ec` lines 103-117, pinned to the real `_kano_ec` on every bash row.

    nocasematch is off and the hardware lock is held, so only the case arm and the regexes decide. The lock call
    and the transport call are not part of this decision. Bash cannot hold NUL; a NUL cannot match these patterns.
    """
    if line in ("powerinfo", "version", "battery", "lidstate", "kblight", "gpioget", "pwmduty", "pwmduty 0",
                "sysinfo", "cbi", "lidclose", "lidopen", "powerbtn"):
        return True
    if re.fullmatch(r"gpioget [A-Za-z][A-Za-z0-9_]*", line):
        return True
    if re.fullmatch(r"gpioset EC_KB_BL_EN_L [01]", line):
        return True
    if re.fullmatch(r"pwmduty 0 (-1|[0-9]|[1-9][0-9]|100)", line):
        return True
    if re.fullmatch(r"kblight ([0-9]|[1-9][0-9]|100)", line):
        return True
    m = re.fullmatch(r"kbpress ([0-9]{1,2}) ([0-9])", line)
    return bool(m and int(m.group(1)) <= 17 and int(m.group(2)) <= 7)


def _lib_copy():
    """Return the vendored fixture. A missing or changed file is an infrastructure error, not an assertion failure."""
    if not _LIB_FIXTURE.is_file():
        raise RuntimeError(f"missing vendored lib fixture: {_LIB_FIXTURE}")
    digest = hashlib.sha256(_LIB_FIXTURE.read_bytes()).hexdigest()
    if digest != _LIB_SHA256:
        raise RuntimeError(f"vendored lib fixture sha256 {digest} != pinned {_LIB_SHA256}")
    return _LIB_FIXTURE


def _bash_kano_ec(lines):
    """Source the vendored lib in a temp dir and return {line: accepted} from the real `_kano_ec`.

    The transport is a stub that exits 0, and the hardware lock is held, so a 0 exit is the shape check accepting.
    Lines that contain NUL are not passed to bash (a bash variable cannot hold NUL). Trailing LFs are kept: the
    decode appends a sentinel and the parameter expansion removes only that sentinel.
    """
    script = r"""#!/bin/bash
set +e
OUT=$1
KANO_HARNESS=$2
LIB=$3
export OUT KANO_HARNESS
export KANO_IP=192.0.2.10 KANO_ADB=adb KANO_ADB_ADDR=192.0.2.10:5555
export KANO_ANDROID_ID_PROP=init.svc.kano_bootfix
export KANO_EC_TTY=/tmp/no-ec KANO_AP_TTY=/tmp/no-ap
export KANO_AP_UART_LOG="$OUT/ap-uart.log"
export KANO_STICK_SERIAL=TESTSTICK0001
export KANO_NVME_BOOT_A_GUID=00000000-0000-4000-8000-000000000099
export LC_ALL=C
# shellcheck disable=SC1090
source "$LIB"
if [ $? -ne 0 ]; then
  echo "source failed" >&2
  exit 3
fi
KANO_LOCKED=1
shopt -u nocasematch
while IFS= read -r b64; do
  # Command substitution drops trailing LFs. The sentinel is appended after decode and then removed.
  cmd=$(printf '%s' "$b64" | base64 -d; printf .)
  cmd=${cmd%.}
  _kano_ec "$cmd" >/dev/null 2>&1
  printf '%s\n' "$?"
done
"""
    with tempfile.TemporaryDirectory(prefix="kano-ec-shape-") as tmp:
        root = Path(tmp)
        harness = root / "harness"
        harness.mkdir()
        (harness / "kano-env.sh").write_text(
            'kano_require() {\n'
            '  for _kano_n in "$@"; do\n'
            '    eval "_kano_v=\\${$_kano_n:-}"\n'
            '    if [ -z "$_kano_v" ]; then echo "refusing: $_kano_n is not set" >&2; exit 2; fi\n'
            '  done\n'
            '}\n',
            newline="\n")
        (harness / "kano-ser.py").write_text("import sys\nsys.exit(0)\n", newline="\n")
        lib = root / "kano-session-lib.sh"
        lib.write_bytes(_lib_copy().read_bytes())
        (root / "out").mkdir()
        path = root / "probe.sh"
        path.write_text(script, newline="\n")
        payload = "".join(base64.b64encode(line.encode("utf-8")).decode("ascii") + "\n" for line in lines)
        proc = subprocess.run(["bash", str(path), str(root / "out"), str(harness), str(lib)],
                              input=payload, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"lib shape probe exit {proc.returncode}\n{proc.stderr}\n{proc.stdout}")
    got = [ln for ln in proc.stdout.splitlines() if ln != ""]
    if len(got) != len(lines):
        raise RuntimeError(f"lib shape probe returned {len(got)} rows for {len(lines)}\n{proc.stderr}")
    return {line: rc == "0" for line, rc in zip(lines, got)}


class Allowlist(unittest.TestCase):
    def test_allowed(self):
        for line, want in ALLOWED.items():
            with self.subTest(line=line):
                self.assertEqual(ks.check_ec_command(line), (want, None))

    def test_refused_with_the_right_reason(self):
        for line, why in REFUSED.items():
            with self.subTest(line=line):
                norm, reason = ks.check_ec_command(line)
                self.assertIsNone(norm)
                if not os.environ.get("HARNESS_BEHAVIOUR_ONLY"):
                    self.assertIn(why, reason)

    def test_no_prefix_or_abbreviation_passes(self):
        # the EC accepts unique prefixes; every proper prefix of an allowed name must be refused
        for name in ks.EC_ALLOW:
            for k in range(1, len(name)):
                with self.subTest(prefix=name[:k]):
                    self.assertIsNone(ks.check_ec_command(name[:k])[0])

    def test_new_commands_match_lib_shapes(self):
        # Same raw strings through the lib's `_kano_ec` (sourced copy, stub transport, lock held) and check_ec_command.
        # Equality is required only for the new shapes. Old commands stay as they are, including apshutdown.
        table = list(dict.fromkeys((*CODEX_I1, *ALLOWED, *REFUSED)))
        for line in CODEX_I1:
            self.assertTrue(_new_command_row(line), line)
        new_rows = [line for line in table if _new_command_row(line)]
        bash_rows = [line for line in new_rows if "\x00" not in line]
        bash_rows += [line for line in ("apshutdown", "powerbtn 200", "kbpress 1 1 0", "  KBLIGHT  ")
                      if line not in bash_rows]
        lib = _bash_kano_ec(bash_rows)
        for line in bash_rows:
            if "\x00" not in line:
                self.assertEqual(_lib_shape_accepts(line), lib[line], f"verbatim regex drifted: {line!r}")
        for line in new_rows:
            with self.subTest(line=line):
                transport = ks.check_ec_command(line)[0] is not None
                if "\x00" in line:
                    self.assertFalse(_lib_shape_accepts(line), line)
                    self.assertFalse(transport, line)
                else:
                    self.assertEqual(transport, lib[line], line)
        for line in ("apshutdown", "powerbtn 200", "kbpress 1 1 0", "  KBLIGHT  "):
            self.assertFalse(_new_command_row(line), line)
            self.assertIsNotNone(ks.check_ec_command(line)[0], line)
            self.assertFalse(lib[line], line)
        # Trailing LFs must reach `_kano_ec`. Stripping them would make these look like the accepted lines above.
        self.assertTrue(lib["cbi"])
        self.assertTrue(lib["sysinfo"])
        self.assertTrue(lib["pwmduty 0 50"])
        for line in ("cbi\n", "sysinfo\n", "pwmduty 0 50\n\n"):
            self.assertIn(line, lib)
            self.assertFalse(lib[line], repr(line))
            self.assertIsNone(ks.check_ec_command(line)[0])

    def test_new_shape_sweep_matches_lib_copy(self):
        # Generated rows, not a hand table. _lib_shape_accepts is pinned to the real `_kano_ec` above.
        _lib_copy()
        pool = ["0", "1", "-1", "-0", "+0", "-01", "00", "01", "05", "08", "007", "0100", "101", "+1", "0x10", "1e2",
                "EC_KB_BL_EN_L", "ec_kb_bl_en_l", "EC_KB_BL_EN_LX", "EC_KB_BL_EN", "EC_FAN_EN", "set", "remove", "x"]
        # Inclusive -2..130, for the numeric argument of pwmduty 0 and kblight.
        rows = {f"{n} {p}" for n in ("pwmduty 0", "kblight") for p in map(str, range(-2, 131))}
        for name in ("pwmduty", "gpioset", "sysinfo", "cbi", "kblight"):
            for k in range(4):  # arity 0..3
                for args in itertools.product(pool, repeat=k):
                    rows.add(" ".join((name,) + args))
        for line in [r for r in rows if r.count(" ") <= 2]:
            w = line.split(" ")
            for v in ([w[0].upper()] + w[1:], [w[0].title()] + w[1:]):
                rows.add(" ".join(v))
            rows |= {"  ".join(w), " " + line, line + " "}
        bad = sorted(r for r in rows if _new_command_row(r)
                     and (ks.check_ec_command(r)[0] is not None) != _lib_shape_accepts(r))
        self.assertEqual(bad, [])
        sent = sorted(r for r in rows if _new_command_row(r) and ks.check_ec_command(r)[0] not in (None, r))
        self.assertEqual(sent, [])


class Cli(HarnessCase):
    def ser(self, *args, replies=None):
        over = {"FAKE_SERIAL_REPLIES": json.dumps(replies or {})}
        return self.fk.run(["python3", HARNESS / "kano-ser.py", *args], timeout=30, **over)

    def serial_log(self):
        f = self.fk.root / "log" / "serial.log"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def test_refused_commands_exit_2_without_loading_pyserial(self):
        for line in ("flashwrite 0 1", "apshutdown\nwp enable", "cbi\tset", "kblight 101",
                     "pwmduty 1", "gpioset EC_FAN_EN 1", "cbi set 6 0 4", "sysinfo reboot"):
            with self.subTest(line=line):
                r = self.ser("ec", line)
                self.assertEqual(r.returncode, 2, self.explain(r))
                self.reason("refuse", r.stderr)
                log = self.serial_log()
                self.assertEqual([e for e in log if e["event"] == "import"], [],
                                 "pyserial was imported eagerly; the refused path must not load it")
                self.assertEqual(log, [])

    def test_allowed_command_is_sent_normalized(self):
        r = self.ser("ec", "  APSHUTDOWN ", "0.2", replies={"apshutdown": "[1.0 chipset_force_shutdown]"})
        self.assertEqual(r.returncode, 0, self.explain(r))
        writes = [e for e in self.serial_log() if e["event"] == "write"]
        self.assertEqual([bytes.fromhex(w["hex"]) for w in writes], [b"\rapshutdown\r"])
        self.assertEqual(writes[0]["dev"], str(self.fk.tmp / "no-ec-tty"))
        self.assertIn("chipset_force_shutdown", r.stdout)

    def test_keyboard_backlight_command_is_sent_with_case_exact_arguments(self):
        refused = self.ser("ec", "  GPIOSET   EC_KB_BL_EN_L   0 ")
        self.assertEqual(refused.returncode, 2, self.explain(refused))
        self.assertEqual([e for e in self.serial_log() if e["event"] == "write"], [])
        r = self.ser("ec", "gpioset EC_KB_BL_EN_L 0", "0.2")
        self.assertEqual(r.returncode, 0, self.explain(r))
        writes = [bytes.fromhex(e["hex"]) for e in self.serial_log() if e["event"] == "write"]
        self.assertEqual(writes, [b"\rgpioset EC_KB_BL_EN_L 0\r"])

    def test_existing_caller_form_ec_powerbtn_seconds(self):
        r = self.ser("ec", "powerbtn", "2")
        self.assertEqual(r.returncode, 0, self.explain(r))
        writes = [bytes.fromhex(e["hex"]) for e in self.serial_log() if e["event"] == "write"]
        self.assertEqual(writes, [b"\rpowerbtn\r"])

    def test_bad_seconds_and_hex_are_refused_before_any_port(self):
        for args in (("ec", "version", "-1"), ("ec", "version", "abc"), ("ec", "version", "999"), ("ap-keys", "zz")):
            with self.subTest(args=args):
                r = self.ser(*args)
                self.assertEqual(r.returncode, 2, self.explain(r))
                self.assertEqual([e for e in self.serial_log() if e["event"] != "import"], [])

    def test_ap_keys_still_work(self):
        r = self.ser("ap-keys", "1b5b41")
        self.assertEqual(r.returncode, 0, self.explain(r))
        writes = [e for e in self.serial_log() if e["event"] == "write"]
        self.assertEqual([(w["dev"], w["hex"]) for w in writes], [(str(self.fk.tmp / "no-ap-tty"), "1b5b41")])


if __name__ == "__main__":
    unittest.main()
