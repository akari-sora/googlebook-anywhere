"""Without kano-env.local.sh and with no identities in the environment (the public export as cloned), every script
that would ssh, adb, open a serial port or write a disk refuses with a clear message before doing any of it."""
import json, unittest
from helpers import HarnessCase, harness_copy, make_images


class Unconfigured(HarnessCase):
    def setUp(self):
        super().setUp()
        self.h = harness_copy(self.fk.tmp / "harness")          # no kano-env.local.sh
        self.old, self.new = make_images(self.fk.tmp / "img")

    def run_bare(self, argv, **over):
        return self.fk.run([str(a).replace("{H}", str(self.h)) for a in argv], timeout=60, **{**self.bare_overrides(), **over})

    def bare_overrides(self):
        # fk.run() starts from the full fixtures; blank every KANO_* so kano-env.sh's generic defaults apply
        return {k: "" for k in self.fk.fixtures()}

    def serial_events(self):
        f = self.fk.root / "log" / "serial.log"
        return [json.loads(l)["event"] for l in f.read_text().splitlines()] if f.exists() else []

    def assert_refused(self, r, name):
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason(f"{name} is not set", r.stdout + r.stderr, self.explain(r))
        self.assertEqual(self.fk.ssh_calls(), [], "ssh was used")
        self.assertEqual(self.fk.lines("adb.log"), [], "adb was used")
        self.assertNotIn("open", self.serial_events(), "a serial port was opened")
        self.assertEqual(self.fk.writes(), [])

    def test_kano_ssh(self):
        self.assert_refused(self.run_bare(["sh", "{H}/kano-ssh.sh", "true"]), "KANO_IP")

    def test_kano_ssh_without_key(self):
        self.assert_refused(self.run_bare(["sh", "{H}/kano-ssh.sh", "true"], KANO_IP="192.0.2.10"), "KANO_SSH_KEY")

    def test_delta_push_stick(self):
        self.assert_refused(self.run_bare(["python3", "{H}/kano-delta-push.py", self.old, self.new, "192.0.2.10"]),
                            "KANO_SSH_KEY")

    def test_delta_push_nvme(self):
        self.assert_refused(self.run_bare(["python3", "{H}/kano-delta-push-nvme.py", self.old, self.new, "192.0.2.10"]),
                            "KANO_SSH_KEY")

    def test_dry_runs_need_no_identity(self):
        for script in ("kano-delta-push.py", "kano-delta-push-nvme.py"):
            r = self.run_bare(["python3", f"{{H}}/{script}", self.old, self.new, "", "--dry-run"])
            self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertEqual(self.fk.ssh_calls(), [])

    def test_empty_stick_serial_never_matches_a_disk_without_serial(self):
        # ssh works (key set), the stick reports no serial and KANO_STICK_SERIAL is empty: "" == "" must not pass
        self.fk.kano()
        self.fk.set("disks/sda/SERIAL", "")
        r = self.run_bare(["python3", "{H}/kano-delta-push.py", self.old, self.new, "192.0.2.10"],
                          KANO_SSH_KEY=str(self.fk.tmp / "id_test"))
        self.assert_refused(r, "KANO_STICK_SERIAL")

    def test_empty_nvme_serial_never_matches_a_disk_without_serial(self):
        self.fk.kano()
        self.fk.set("disks/nvme0n1/SERIAL", "")
        self.fk.set("disks/sda/SERIAL", "")
        r = self.run_bare(["python3", "{H}/kano-delta-push-nvme.py", self.old, self.new, "192.0.2.10"],
                          KANO_SSH_KEY=str(self.fk.tmp / "id_test"))
        self.assert_refused(r, "KANO_NVME_SERIAL")

    def test_nvme_install(self):
        self.assert_refused(self.run_bare(["bash", "{H}/kano-nvme-install.sh", "--go"]), "KANO_IP")

    def test_nvme_update(self):
        self.assert_refused(self.run_bare(["bash", "{H}/kano-nvme-update.sh", "1", "2"]), "KANO_IP")

    def test_kano_ser(self):
        self.assert_refused(self.run_bare(["python3", "{H}/kano-ser.py", "ec", "version"]), "KANO_EC_TTY")
        self.assert_refused(self.run_bare(["python3", "{H}/kano-ser.py", "ap-keys", "1b"]), "KANO_AP_TTY")

    def test_kano_boot(self):
        self.assert_refused(self.run_bare(["bash", "{H}/kano-boot.sh", "KANO maintenance"]), "KANO_EC_TTY")

    def test_kano_ui(self):
        self.assert_refused(self.run_bare(["bash", "{H}/kano-ui.sh", "list"]), "KANO_ADB_ADDR")

    def test_watchers_do_not_start_without_a_way_to_cut_power(self):
        for script in ("kano-gbos-watch.py", "kano-gbos-watch-nvme.py"):
            r = self.run_bare(["python3", f"{{H}}/{script}", self.fk.tmp / "uart.log", "5", self.fk.tmp / "frames"])
            self.assertEqual(r.returncode, 5, self.explain(r))
            self.reason("KANO_EC_TTY is not set", r.stderr)
            self.assertFalse((self.fk.tmp / "frames").exists())


if __name__ == "__main__":
    unittest.main()
