"""kano-gbos-watch.py / kano-gbos-watch-nvme.py: "unobservable" instead of a false all-clear, the full guard set of
the device profile (profiles/kano/profile.toml) on this boot's command line, danger handling with a confirmed EC power
cut (fake pyserial), and a hung camera grab that cannot stall the watch."""
import json, shutil, subprocess, threading, time, tomllib, unittest
from helpers import HARNESS, REPO, HarnessCase, harness_copy


def image_blacklist():
    """The module_blacklist of profiles/kano/profile.toml, read here independently of the watcher's own loader."""
    with open(REPO / "profiles" / "kano" / "profile.toml", "rb") as f:
        return tomllib.load(f)["blacklist"]["modules"]


BL = image_blacklist()
LATE = "spi_intel_pci"                       # far down the list: a check of the first names only would miss it


def cmdline(initcalls="nvme_init,mei_me_driver_init", modules=None, extra=""):
    mods = ",".join(BL if modules is None else modules)
    return (f"[    0.000000] Kernel command line: initrd=\\initrd.img initcall_blacklist={initcalls} efi=noruntime "
            f"module_blacklist={mods} console=ttyS0{extra}\n")


GOOD_USB = cmdline()
GOOD_NVME = cmdline("mei_me_driver_init")
OFF = {"apshutdown": "[12.0 chipset_force_shutdown(0x4)]", "powerinfo": "[13.0 power state 0 = G3, in 0x0000]"}
STILL_ON = {"apshutdown": "[12.0 chipset_force_shutdown(0x4)]", "powerinfo": "[13.0 power state 4 = S0, in 0x7f]"}
FAST = ["--observe-timeout", "3", "--stall-timeout", "2", "--camera-every", "0"]


class Watch(HarnessCase):
    def setUp(self):
        super().setUp()
        self.log = self.fk.tmp / "ap-uart.log"
        self.stop = threading.Event()
        self.addCleanup(self.stop.set)

    def writer(self, lines, every=0.3, then_idle=True):
        """Once the watcher is up (it creates the frames dir first thing), append `lines` one by one, as a boot does
        when the watcher was started right after power-on; afterwards keep the log growing unless then_idle."""
        frames = self.fk.tmp / "frames"

        def run():
            end = time.time() + 30
            while not frames.is_dir() and time.time() < end and not self.stop.is_set():
                time.sleep(0.05)
            time.sleep(0.3)
            for l in lines:
                if self.stop.is_set():
                    return
                with open(self.log, "a") as f:
                    f.write(l)
                time.sleep(every)
            n = 0
            while not then_idle and not self.stop.is_set():
                n += 1
                with open(self.log, "a") as f:
                    f.write(f"[   {3 + n}.000000] kano-logcat: tick {n}\n")
                time.sleep(every)
        threading.Thread(target=run, daemon=True).start()

    def watch(self, seconds, *opts, script="kano-gbos-watch.py", replies=OFF, timeout=60, **env):
        t = time.time()
        r = self.fk.run(["python3", HARNESS / script, self.log, seconds, self.fk.tmp / "frames", *opts], timeout=timeout,
                        FAKE_SERIAL_REPLIES=json.dumps(replies), **env)
        r.elapsed = time.time() - t
        return r

    def ec_writes(self):
        f = self.fk.root / "log" / "serial.log"
        lines = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
        return [bytes.fromhex(e["hex"]).strip(b"\r").decode() for e in lines if e["event"] == "write"]

    def test_log_without_this_boots_command_line_is_unobservable(self):
        self.log.write_text("coreboot-4.20 ...\n")
        self.writer(["edk2 ...\n", "Boot in 3 s\n", "Boot in 2 s\n"], then_idle=False)
        r = self.watch(30, *FAST)
        self.assertEqual(r.returncode, 4, self.explain(r))
        self.reason("UNOBSERVABLE: no kernel command line", r.stdout)
        self.assertLess(r.elapsed, 10)
        self.assertEqual(self.ec_writes(), [])

    def test_missing_log_is_unobservable(self):
        r = self.watch(30, *FAST)
        self.assertEqual(r.returncode, 4, self.explain(r))

    def test_an_older_boots_command_line_does_not_count(self):
        self.log.write_text(GOOD_USB + "[    2.000000] earlier boot\n")
        self.writer(["firmware banner\n"], then_idle=False)
        r = self.watch(30, *FAST)
        self.assertEqual(r.returncode, 4, self.explain(r))
        self.reason("already holds a kernel command line", r.stdout)

    def test_log_that_stops_growing_is_unobservable(self):
        self.writer([GOOD_USB, "[    2.000000] x\n"])
        r = self.watch(30, "--observe-timeout", "20", "--stall-timeout", "2", "--camera-every", "0")
        self.assertEqual(r.returncode, 4, self.explain(r))
        self.reason("has not grown", r.stdout)

    def test_observed_boot_without_danger(self):
        self.writer([GOOD_USB, "[    2.000000] init: starting\n"], then_idle=False)
        r = self.watch(5, *FAST)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.reason("this boot observed, no danger line seen", r.stdout)

    def test_quiet_after_the_observe_window_is_only_a_note(self):
        self.writer([GOOD_USB, "[    2.000000] init: starting\n"] + ["[    2.5] more\n"] * 12, every=0.3)
        r = self.watch(11, "--observe-timeout", "3", "--stall-timeout", "2", "--camera-every", "0")
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.reason("log silent since", r.stdout)

    def test_missing_guard_cuts_power(self):
        self.writer([GOOD_USB.replace("efi=noruntime ", ""), "[    2.000000] x\n"], then_idle=False)
        r = self.watch(30, *FAST)
        self.assertEqual(r.returncode, 3, self.explain(r))
        self.reason("guard missing from this boot's kernel command line: efi=noruntime", r.stdout)
        self.assertEqual(self.ec_writes()[:2], ["apshutdown", "powerinfo"])
        self.reason("EC confirms the AP is off", r.stdout)

    def test_danger_line_cuts_power(self):
        self.writer([GOOD_USB, "[    2.000000] x\n", "[    3.100000] nvme nvme0: pci function 0000:01:00.0\n"],
                    then_idle=False)
        r = self.watch(30, *FAST)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("internal NVMe controller appeared", r.stdout)

    def test_unconfirmed_power_cut_has_its_own_exit_status(self):
        self.writer([GOOD_USB, "[    2.000000] x\n", "[    3.000000] tpm tpm0: starting up\n"], then_idle=False)
        r = self.watch(30, *FAST, replies=STILL_ON, timeout=90)
        self.assertEqual(r.returncode, 6, self.explain(r))
        self.reason("EC POWER CUT NOT CONFIRMED", r.stdout)
        self.assertEqual(self.ec_writes().count("apshutdown"), 2)

    def test_the_guard_list_is_the_builders(self):
        self.assertGreater(len(BL), 40)
        for m in ("cros_ec_lpcs", "cros_ec_chardev", "tpm", "spi_intel_pci", "gsmi", "nvram", LATE):
            self.assertIn(m, BL)
        self.assertNotIn("cros_ec_proto", BL)                     # BL.remove()d: cros_kbd_led_backlight needs it
        self.assertGreater(BL.index(LATE), 2)

    def test_a_later_blacklist_entry_missing_cuts_power(self):
        self.writer([cmdline(modules=[m for m in BL if m != LATE]), "[    2.000000] x\n"], then_idle=False)
        r = self.watch(30, *FAST)
        self.assertEqual(r.returncode, 3, self.explain(r))
        self.reason(f"module_blacklist entry {LATE}", r.stdout)

    def test_only_the_last_module_blacklist_counts(self):
        # the kernel keeps the last module_blacklist=; a short one after the full one replaces it
        self.writer([cmdline(extra=" module_blacklist=cros_ec_lpcs"), "[    2.000000] x\n"], then_idle=False)
        r = self.watch(30, *FAST)
        self.assertEqual(r.returncode, 3, self.explain(r))

    def test_spi_intel_option_cuts_power(self):
        self.writer([cmdline(extra=" spi-intel.writeable=1"), "[    2.000000] x\n"], then_idle=False)
        r = self.watch(30, *FAST)
        self.assertEqual(r.returncode, 3, self.explain(r))
        self.reason("forbidden option spi-intel.writeable=1", r.stdout)

    def test_command_line_printed_in_chunks(self):
        whole = GOOD_USB.split("Kernel command line: ", 1)[1].split()
        half = len(whole) // 2
        chunks = [f"[    0.000000] Kernel command line: {' '.join(whole[:half])} \\\n",
                  f"[    0.010000] Kernel command line: {' '.join(whole[half:])}\n"]
        self.writer(chunks + ["[    2.000000] init: starting\n"], then_idle=False)
        r = self.watch(5, *FAST)
        self.assertEqual(r.returncode, 0, self.explain(r))

    def test_window_shorter_than_the_observe_timeout_is_unobservable(self):
        # the log keeps growing but never shows this boot's command line; the window ends first
        self.writer(["firmware banner\n"], then_idle=False)
        r = self.watch(3, "--observe-timeout", "60", "--stall-timeout", "30", "--camera-every", "0")
        self.assertEqual(r.returncode, 4, self.explain(r))
        self.reason("window ended before this boot's kernel command line", r.stdout)
        self.assertNotIn("no danger line seen", r.stdout)

    def test_invalid_durations_do_not_start(self):
        for argv in (["0"], ["nan"], ["-5"], ["inf"], ["5", "--observe-timeout", "0"],
                     ["5", "--stall-timeout", "nan"], ["5", "--ec-timeout", "-1"], ["5", "--camera-every", "-1"]):
            with self.subTest(argv=argv):
                try:
                    r = self.fk.run(["python3", HARNESS / "kano-gbos-watch.py", self.log, argv[0], self.fk.tmp / "frames",
                                     *argv[1:]], timeout=30)
                except subprocess.TimeoutExpired as e:
                    self.fail(f"watcher did not refuse an invalid duration: {e}")
                self.assertEqual(r.returncode, 5, self.explain(r))
                self.assertFalse((self.fk.tmp / "frames").exists())
        self.assertEqual(self.ec_writes(), [])

    def test_unreadable_guard_source_does_not_start(self):
        h = harness_copy(self.fk.tmp / "elsewhere" / "tools" / "harness")      # no builder/ next to it
        r = self.fk.run(["python3", h / "kano-gbos-watch.py", self.log, "5", self.fk.tmp / "frames"], timeout=30)
        self.assertEqual(r.returncode, 5, self.explain(r))
        self.reason("cannot read the expected command line guards", r.stderr)
        self.assertFalse((self.fk.tmp / "frames").exists())

    def test_nvme_profile(self):
        # on the NVMe the controller is expected; the command line keeps nvme_init allowed
        self.writer([GOOD_NVME, "[    2.000000] x\n", "[    3.100000] nvme nvme0: pci function 0000:01:00.0\n"],
                    then_idle=False)
        r = self.watch(5, *FAST, script="kano-gbos-watch-nvme.py")
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.stop.set()
        time.sleep(0.5)
        self.stop.clear()
        self.log.write_text("")
        shutil.rmtree(self.fk.tmp / "frames")
        self.writer([GOOD_USB, "[    2.000000] x\n"], then_idle=False)
        r = self.watch(10, *FAST, script="kano-gbos-watch-nvme.py")
        self.assertEqual(r.returncode, 3, self.explain(r))

    def test_hung_camera_grab_does_not_stall_the_watch(self):
        ff = self.fk.tmp / "ffbin"
        ff.mkdir()
        pidfile = self.fk.tmp / "ffmpeg.pid"
        (ff / "ffmpeg").write_text(f"#!/bin/sh\necho $$ >> '{pidfile}'\nexec sleep 60\n")
        (ff / "ffmpeg").chmod(0o755)
        self.writer([GOOD_USB, "[    2.000000] x\n", "[    2.5] y\n", "[    2.6] z\n", "[    2.7] z\n",
                     "[    2.8] z\n", "[    3.000000] gsmi: Registered\n"], every=0.5, then_idle=False)
        t = time.time()
        r = self.fk.run(["python3", HARNESS / "kano-gbos-watch.py", self.log, 60, self.fk.tmp / "frames",
                         "--observe-timeout", "10", "--stall-timeout", "5", "--camera-every", "0.5",
                         "--camera-timeout", "1"], timeout=60, PATH=f"{ff}:{self.fk.env()['PATH']}",
                        FAKE_SERIAL_REPLIES=json.dumps(OFF))
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("gsmi active", r.stdout)
        self.reason("camera: ffmpeg killed", r.stdout)
        self.assertLess(time.time() - t, 20)
        pids = pidfile.read_text().split()
        self.assertGreater(len(pids), 1, "a hung grab must be killed so that the next one can start")
        alive = [p for p in pids if subprocess.run(["kill", "-0", p], capture_output=True).returncode == 0]
        self.assertEqual(alive, [], "ffmpeg left running")

    def test_hung_ec_port_times_out(self):
        self.writer([GOOD_USB, "[    2.000000] x\n", "[    3.000000] tpm tpm0: starting up\n"], then_idle=False)
        try:
            r = self.watch(60, *FAST, "--ec-timeout", "2", replies=OFF, timeout=80, FAKE_SERIAL_HANG="1")
        except subprocess.TimeoutExpired as e:
            self.fail(f"the hung EC port did not time out within the test limit: {e}")
        self.assertEqual(r.returncode, 6, self.explain(r))
        self.reason("timed out", r.stdout)
        self.assertLess(r.elapsed, 40)


if __name__ == "__main__":
    unittest.main()
