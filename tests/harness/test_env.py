"""Settings: kano-env.sh (generic defaults) < kano-env.local.sh (the developer's values) < the environment, read the
same way by the shell and the Python scripts; no script hardcodes hosts, serials or paths."""
import importlib.util, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from helpers import HARNESS, FakeKano, env_names, harness_copy

sys.dont_write_bytecode = True
LINE = re.compile(r'\s*: "\$\{(\w+):=(.*)\}"\s*(#.*)?')
OWNED = ["kano-ser.py", "kano-delta-push.py", "kano-delta-push-nvme.py", "kano-nvme-install.sh", "kano-nvme-update.sh",
         "kano-gbos-watch.py", "kano-gbos-watch-nvme.py", "kano-boot.sh", "kano-ssh.sh", "kano-ui.sh", "kano-tone-find.py"]
IDENTITIES = ["KANO_IP", "KANO_SSH_KEY", "KANO_ADB_ADDR", "KANO_CR50_SERIAL", "KANO_AP_TTY", "KANO_EC_TTY",
              "KANO_STICK_SERIAL", "KANO_NVME_SERIAL", "KANO_NVME_PCI", "KANO_NVME_BOOT_A_GUID"]
FAKE_LOCAL = """# test-only local file
: "${KANO_IP:=192.0.2.77}"
: "${KANO_STICK_SERIAL:=LOCALSTICK01}"
: "${KANO_CR50_SERIAL:=LOCALCR50}"
: "${KANO_EC_TTY:=/dev/fake-cr50-${KANO_CR50_SERIAL}-ec}"
: "${KANO_RUNS:=$HOME/local-runs}"
"""


def shell_values(h, env):
    script = 'HERE=$1; . "$HERE/kano-env.sh"; shift; for n in "$@"; do eval "v=\\${$n}"; printf "%s=%s\\n" "$n" "$v"; done'
    r = subprocess.run(["sh", "-c", script, "sh", str(h), *env_names(h)], env=env, capture_output=True, text=True, check=True)
    return dict(l.split("=", 1) for l in r.stdout.splitlines())


def python_values(h, script, env):
    code = ("import importlib.util, os, sys; sys.dont_write_bytecode = True\n"
            f"s = importlib.util.spec_from_file_location('m', {str(h / script)!r}); m = importlib.util.module_from_spec(s)\n"
            "s.loader.exec_module(m); m.load_env()\n"
            f"for n in {env_names(h)!r}: print(n + '=' + os.environ[n])")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    return dict(l.split("=", 1) for l in r.stdout.splitlines())


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kano-env-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def env(self, **over):
        e = {k: v for k, v in os.environ.items() if not k.startswith("KANO_")}
        e.update(HOME="/home/tester", **over)
        return e

    def values(self, local=None, **over):
        """Shell and Python values for a harness copy (with `local` as its kano-env.local.sh); both must agree."""
        h = harness_copy(self.tmp / f"h{len(list(self.tmp.iterdir()))}", local)
        want = shell_values(h, self.env(**over))
        for script in ("kano-ser.py", "kano-delta-push.py"):
            self.assertEqual(python_values(h, script, self.env(**over)), want, script)
        return want

    def test_every_line_is_parseable_and_exported(self):
        text = (HARNESS / "kano-env.sh").read_text()
        settings = [l for l in text.splitlines() if l.startswith(": ")]
        self.assertTrue(all(LINE.fullmatch(l) for l in settings), settings)
        exported = re.search(r"^export ((?:.*\\\n)*.*)$", text, re.M).group(1).replace("\\\n", " ")
        self.assertEqual(sorted(exported.split()), sorted(env_names()))

    def test_generic_defaults_without_a_local_file(self):
        v = self.values()
        for n in IDENTITIES:
            self.assertEqual(v[n], "", n)
        self.assertEqual(v["KANO_RUNS"], "/home/tester/kano-harness/runs")
        self.assertEqual(v["KANO_AP_UART_LOG"], "/home/tester/kano-harness/runs/ap-uart.log")
        self.assertEqual(v["KANO_WORK"], "/home/tester/kano-harness/work")
        self.assertEqual(v["KANO_NVME_SRC"], "")
        self.assertEqual(v["KANO_PYSERIAL_ROOT"], "")
        self.assertEqual(v["KANO_ADB"], "adb")

    def test_local_file_overrides_the_defaults(self):
        v = self.values(FAKE_LOCAL)
        self.assertEqual(v["KANO_IP"], "192.0.2.77")
        self.assertEqual(v["KANO_STICK_SERIAL"], "LOCALSTICK01")
        self.assertEqual(v["KANO_EC_TTY"], "/dev/fake-cr50-LOCALCR50-ec")          # uses a name set earlier in it
        self.assertEqual(v["KANO_AP_UART_LOG"], "/home/tester/local-runs/ap-uart.log")  # default built on its value
        self.assertEqual(v["KANO_NVME_SERIAL"], "")                                # not in it: default

    def test_environment_overrides_the_local_file_and_the_defaults(self):
        v = self.values(FAKE_LOCAL, KANO_IP="192.0.2.99", KANO_RUNS="/srv/runs", KANO_NVME_SERIAL="ENVNVME01")
        self.assertEqual(v["KANO_IP"], "192.0.2.99")
        self.assertEqual(v["KANO_AP_UART_LOG"], "/srv/runs/ap-uart.log")
        self.assertEqual(v["KANO_NVME_SERIAL"], "ENVNVME01")
        self.assertEqual(v["KANO_STICK_SERIAL"], "LOCALSTICK01")

    def test_the_developers_local_file_is_well_formed(self):
        local = HARNESS / "kano-env.local.sh"
        if not local.exists():
            self.skipTest("no kano-env.local.sh here (public export)")
        lines = [l for l in local.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
        names = [LINE.fullmatch(l).group(1) if LINE.fullmatch(l) else None for l in lines]
        self.assertNotIn(None, names, "every line must be : \"${NAME:=value}\"")
        self.assertEqual(sorted(set(names) - set(env_names())), [], "names that kano-env.sh does not know")
        v = shell_values(HARNESS, self.env())
        self.assertEqual([n for n in IDENTITIES if not v[n]], [], "identities the local file leaves empty")

    def test_test_fixtures_cover_every_setting(self):
        fk = FakeKano(self.tmp / "fk")
        self.assertEqual(sorted(fk.fixtures()), sorted(env_names()))


class NoHardcodedConstants(unittest.TestCase):
    def test_no_ip_address_literals_in_scripts(self):
        ipv4 = re.compile(r"(?<![\d.])\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])")
        for name in OWNED:
            for n, line in enumerate((HARNESS / name).read_text().splitlines(), 1):
                with self.subTest(file=name, line=n):
                    self.assertIsNone(ipv4.search(line), line)

    def test_siblings_are_called_from_the_scripts_own_directory(self):
        for name in OWNED:
            text = (HARNESS / name).read_text()
            with self.subTest(file=name):
                self.assertNotIn("~/", text)
                self.assertNotRegex(text, r"\$T/|Path\.home\(\)")

    def test_ssh_only_through_kano_ssh_with_batchmode(self):
        self.assertIn("BatchMode=yes", (HARNESS / "kano-ssh.sh").read_text())
        for name in OWNED:
            if name.endswith(".py"):
                with self.subTest(file=name):
                    self.assertNotRegex((HARNESS / name).read_text(), r'\["ssh"')


if __name__ == "__main__":
    unittest.main()
