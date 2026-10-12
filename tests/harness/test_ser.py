"""kano-ser.py: EC console allowlist (pure function) and the CLI around it (fake pyserial, no real port)."""
import importlib.util, json, os, sys, unittest
from helpers import HARNESS, HarnessCase

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("kano_ser", HARNESS / "kano-ser.py")
ks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ks)

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
    "gpioget": "gpioget",
    "gpioget EC_WP_L": "gpioget EC_WP_L",
}
REFUSED = {
    "flashwrite": "allowlist", "FLASHWR": "allowlist", "flashe": "allowlist", "flasherase 0 4096": "allowlist",
    "flashwp enable": "allowlist", "wp enable": "allowlist", "cbi  set 1 2 3": "allowlist",
    "cbi\tset": "control", "powerbtn\rflashwp": "control", "apshutdown\nwp enable": "control",
    "apshutdown\x00": "control", "version\x7f": "control", "i2cxfer w 0 0x10 0": "allowlist",
    "rw 0x40000000 1": "allowlist", "cutoff": "allowlist", "battcutoff": "allowlist", "gpioset EC_WP_L 0": "allowlist",
    "sysjump RW": "allowlist", "reboot ro": "allowlist", "reboot": "allowlist", "ccd reset factory": "allowlist",
    "hash": "allowlist", "": "empty", "   ": "empty", "apshutdown ": "non-ASCII", "ｖｅｒｓｉｏｎ": "non-ASCII",
    "apshutdown now": "arguments", "kblight 0": "arguments", "kblight 100": "arguments", "powerbtn -1": "arguments",
    "powerbtn 0": "arguments", "powerbtn 99999": "arguments", "powerbtn 2 3": "arguments", "kbpress 18 1": "arguments",
    "kbpress 1 8": "arguments", "kbpress 1 1 2": "arguments", "kbpress 1": "arguments", "kbpress": "arguments",
    "gpioget A B": "arguments", "gpioget A;B": "arguments", "version x": "arguments", "powerinfo 1": "arguments",
}


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


class Cli(HarnessCase):
    def ser(self, *args, replies=None):
        over = {"FAKE_SERIAL_REPLIES": json.dumps(replies or {})}
        return self.fk.run(["python3", HARNESS / "kano-ser.py", *args], timeout=30, **over)

    def serial_log(self):
        f = self.fk.root / "log" / "serial.log"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def test_refused_commands_exit_2_without_loading_pyserial(self):
        for line in ("flashwrite 0 1", "apshutdown\nwp enable", "cbi\tset", "kblight 0"):
            with self.subTest(line=line):
                r = self.ser("ec", line)
                self.assertEqual(r.returncode, 2, self.explain(r))
                self.reason("refuse", r.stderr)
                self.assertEqual(self.serial_log(), [])

    def test_allowed_command_is_sent_normalized(self):
        r = self.ser("ec", "  APSHUTDOWN ", "0.2", replies={"apshutdown": "[1.0 chipset_force_shutdown]"})
        self.assertEqual(r.returncode, 0, self.explain(r))
        writes = [e for e in self.serial_log() if e["event"] == "write"]
        self.assertEqual([bytes.fromhex(w["hex"]) for w in writes], [b"\rapshutdown\r"])
        self.assertEqual(writes[0]["dev"], str(self.fk.tmp / "no-ec-tty"))
        self.assertIn("chipset_force_shutdown", r.stdout)

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
