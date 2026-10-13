"""kano-nvme-install.sh against the fake KANO: guards in the same remote invocation as the destructive commands,
decisions only from this run's output, GPT backup before the discard, stop at the first failing step; the source
(KANO_NVME_SRC) checked locally and against the stick's ESP before anything is written; a guard query that fails
refuses instead of counting as "idle"."""
import re, shutil, struct, unittest
from helpers import BOOT_A_GUID, HARNESS, MiB, HarnessCase, harness_copy, initrd, make_images

STALE_LOG = f"""[10:00:00] 1. guards
GUARDS_OK
[10:01:00] 2. copy the stick's Googlebook region to the NVMe
DD_OK
CMP_OK
UD_COPY_OK
BOOT_A_GUID={BOOT_A_GUID.upper()}
BOOTA_NAME_OK
MOUNT_OK
ESP_OK
"""


class Install(HarnessCase):
    def setUp(self):
        super().setUp()
        fk = self.fk
        fk.kano()
        old, _ = make_images(fk.tmp / "img")
        with open(fk.root / "dev" / "sda", "r+b") as f:          # the stick holds the image (MiB 1..15)
            f.seek(MiB)
            f.write(old.read_bytes()[MiB:15 * MiB])
        stick = (fk.root / "dev" / "sda").read_bytes()
        (fk.root / "dev" / "nvme0n1p1").write_bytes(stick[12 * MiB:15 * MiB])   # what p1 reads after the move
        self.nvme_before = (fk.root / "dev" / "nvme0n1").read_bytes()
        self.src = src = fk.work / "kano-final25"                # KANO_NVME_SRC of the fixtures
        src.mkdir(parents=True)
        initrd(src / "initrd.img", b"ARCHIVE" * 100)
        for name in ("lapis.conf", "lapis-debug.conf", "lapis-quiet.conf"):
            (src / name).write_text("title Googlebook KANO\nlinux /kernel.efi\ninitrd /initrd.img\n"
                                    "options initcall_blacklist=nvme_init,mei_me_driver_init efi=noruntime "
                                    "module_blacklist=cros_ec_lpcs,spi_intel_pci\n")
        for d in ("mnt/nesp/loader/entries", "mnt/xb/loader/entries", "mnt/sesp/loader/entries"):
            (fk.root / d).mkdir(parents=True)
        # the stick's ESP (read-only mount at /mnt/sesp) holds the same build
        shutil.copy(src / "initrd.img", fk.root / "mnt/sesp/initrd.img")
        for name in ("lapis.conf", "lapis-debug.conf", "lapis-quiet.conf"):
            shutil.copy(src / name, fk.root / "mnt/sesp/loader/entries" / name)
        (fk.root / "mnt/nesp/kernel.efi").write_bytes(b"KERNEL" * 1000)
        self.base = fk.runs / "nvme-install"

    def install(self, *args):
        return self.fk.run(["bash", HARNESS / "kano-nvme-install.sh", *(args or ("--go",))], timeout=300)

    def stale_previous_run(self):
        """What earlier runs left behind: the old cumulative install.log and an old run dir, all success markers."""
        self.base.mkdir(parents=True)
        (self.base / "install.log").write_text(STALE_LOG)
        old = self.base / "run-20261012-170000-1"
        old.mkdir()
        for name, marker in (("01-guards", "GUARDS_OK"), ("02-sums", "BACKUP_SUMS_OK"), ("03-copy", "DD_OK"),
                             ("04-verify", "CMP_OK"), ("05-gpt", "GPT_OK")):
            (old / f"{name}.out").write_text(STALE_LOG + marker + "\n")

    def assert_nothing_destroyed(self, r, why=None):
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        if why:
            self.reason(why, r.stdout + r.stderr, self.explain(r))
        self.assertNotIn("blkdiscard", self.fk.sent(), "the destructive step was sent")
        self.assertEqual(self.fk.writes(), [])
        self.assertEqual((self.fk.root / "dev" / "nvme0n1").read_bytes(), self.nvme_before)

    def run_dir(self):
        runs = [d for d in self.base.iterdir() if d.name.startswith("run-") and d.name != "run-20261012-170000-1"]
        self.assertEqual(len(runs), 1, runs)
        return runs[0]

    def test_happy_path(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("installed; next", r.stdout)
        w = self.fk.writes()
        self.assertEqual(w[0], "BLKDISCARD -f /dev/nvme0n1")
        self.assertTrue(w[1].startswith("WRITE nvme0n1 offset=0 "), w)
        self.assertTrue(any(x.startswith("SGDISK -e -d 32 -d 33") for x in w), w)
        run = self.run_dir()
        # the GPT backup holds the NVMe as it was before the install
        self.assertEqual((run / "02-head.bin").read_bytes(), self.nvme_before[:MiB])
        self.assertEqual((run / "02-tail.bin").read_bytes(), self.nvme_before[-MiB:])
        # guards run in the same remote script as the discard, before it
        copy = (run / "03-copy.sh").read_text()
        self.assertLess(copy.index("\nguard\n"), copy.index("blkdiscard"))
        self.assertIn("set -euo pipefail", copy.splitlines()[0])
        nesp = self.fk.root / "mnt/nesp"
        self.assertIn(f"androidboot.boot_part_uuid={BOOT_A_GUID}".encode(), (nesp / "initrd.img").read_bytes())
        self.assertIn("initcall_blacklist=mei_me_driver_init ", (nesp / "loader/entries/lapis.conf").read_text())
        self.assertEqual((self.fk.root / "mnt/xb/gbos-nvme/kernel.efi").read_bytes(), (nesp / "kernel.efi").read_bytes())

    def assert_stopped_before_any_ssh(self, r, why):
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason(why, r.stdout + r.stderr, self.explain(r))
        self.assertEqual(self.fk.ssh_calls(), [])
        self.assertEqual((self.fk.root / "dev" / "nvme0n1").read_bytes(), self.nvme_before)

    def test_source_is_a_setting(self):
        self.assertNotIn("kano-final", (HARNESS / "kano-nvme-install.sh").read_text())
        h = harness_copy(self.fk.tmp / "harness")                    # no kano-env.local.sh
        r = self.fk.run(["bash", h / "kano-nvme-install.sh", "--go"], timeout=60, KANO_NVME_SRC="")
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("KANO_NVME_SRC is not set", r.stderr)

    def test_missing_initrd(self):
        (self.src / "initrd.img").unlink()
        self.assert_stopped_before_any_ssh(self.install(), "cannot copy")

    def test_bad_bootconfig(self):
        d = (self.src / "initrd.img").read_bytes()
        n = len(d) - len(b"#BOOTCONFIG\n") - 8 - 5
        (self.src / "initrd.img").write_bytes(d[:n] + b"X" + d[n + 1:])            # checksum no longer matches
        self.assert_stopped_before_any_ssh(self.install(), "bootconfig unusable")

    def test_bootconfig_without_a_vbmeta_digest(self):
        bc = b"androidboot.boot_part_uuid=00000000-0000-0000-0000-000000000000\n"
        (self.src / "initrd.img").write_bytes(b"ARCHIVE" * 100 + bc + struct.pack("<II", len(bc), sum(bc)) +
                                              b"#BOOTCONFIG\n")
        self.assert_stopped_before_any_ssh(self.install(), "bootconfig unusable")

    def test_missing_entry(self):
        (self.src / "lapis-quiet.conf").unlink()
        self.assert_stopped_before_any_ssh(self.install(), "expected lapis-debug.conf and lapis-quiet.conf")

    def test_build_without_the_plain_entry(self):
        # builds after the plain entry was dropped: quiet + serial-debug only, on both sides
        (self.src / "lapis.conf").unlink()
        (self.fk.root / "mnt/sesp/loader/entries/lapis.conf").unlink()
        r = self.install()
        self.assertEqual(r.returncode, 0, self.explain(r))
        entries = self.fk.root / "mnt/nesp/loader/entries"
        self.assertEqual(sorted(p.name for p in entries.iterdir()), ["lapis-debug.conf", "lapis-quiet.conf"])
        self.assertIn("initcall_blacklist=mei_me_driver_init ", (entries / "lapis-quiet.conf").read_text())

    def test_stick_with_other_entries_than_the_source(self):
        # the source dropped the plain entry, the stick's ESP still has it: not the same image
        (self.src / "lapis.conf").unlink()
        self.assert_nothing_destroyed(self.install(), "does not hold the same boot entries")

    def test_entry_without_the_guards(self):
        (self.src / "lapis.conf").write_text("title x\noptions initcall_blacklist=nvme_init,mei_me_driver_init quiet\n")
        self.assert_stopped_before_any_ssh(self.install(), "module_blacklist= or efi=noruntime missing")

    def test_source_that_is_not_the_stick_image(self):
        # another build: its initrd (vbmeta digest) differs from what the stick boots
        initrd(self.src / "initrd.img", b"ARCHIVE" * 100, digest="bb")
        self.assert_nothing_destroyed(self.install(), "is not what the stick's ESP holds")
        self.assertNotIn("dd if=", self.fk.sent().split("SOURCE_OK", 1)[1] if "SOURCE_OK" in self.fk.sent() else "")

    def test_unreadable_swaps_is_not_idle(self):
        # grep exits 2 on an unreadable file; that must refuse, not count as "no swap on the NVMe"
        self.fk.rules([{"when": "GUARDS_OK", "remove": ["proc/swaps"]}])
        r = self.install()
        self.assert_nothing_destroyed(r, "REFUSE: cannot read")
        self.reason("proc/swaps: No such file", r.stdout)

    def test_unreadable_mounts_is_not_idle(self):
        self.fk.rules([{"when": "GUARDS_OK", "remove": ["proc/self/mounts"]}])
        self.assert_nothing_destroyed(self.install())

    def test_missing_holders_dir_is_not_idle(self):
        self.fk.rules([{"when": "GUARDS_OK", "remove": ["sys/block/nvme0n1/holders"]}])
        self.assert_nothing_destroyed(self.install(), "REFUSE: cannot read the holders of the NVMe")

    def test_refuses_without_go(self):
        r = self.install("--yes")
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.assertEqual(self.fk.ssh_calls(), [])

    def test_stale_success_markers_and_refusing_guard(self):
        # review C1/I1: an earlier run's GUARDS_OK/DD_OK/CMP_OK must not authorise this run
        self.stale_previous_run()
        self.fk.set("disks/nvme0n1/SERIAL", "TESTNVME9999")
        self.assert_nothing_destroyed(self.install(), "REFUSE: nvme0n1 serial is not")

    def test_guard_ssh_failure_with_stale_markers(self):
        self.stale_previous_run()
        self.fk.rules([{"when": "echo GUARDS_OK", "exit": 255}])
        self.assert_nothing_destroyed(self.install(), "01-guards: remote exit status 255")

    def test_guard_output_without_marker(self):
        self.stale_previous_run()
        self.fk.rules([{"when": "echo GUARDS_OK", "truncate": 0}])
        self.assert_nothing_destroyed(self.install(), "'GUARDS_OK' missing from this run's output")

    def test_mounted_nvme(self):
        self.fk.mount("/dev/nvme0n1p5", "/mnt/esp")
        self.assert_nothing_destroyed(self.install(), "REFUSE: something on the NVMe is mounted")

    def test_held_nvme(self):
        (self.fk.root / "sys/block/nvme0n1/nvme0n1p1/holders/dm-0").mkdir()
        self.assert_nothing_destroyed(self.install(), "REFUSE: the NVMe is held by dm-0")

    def test_wrong_pci_id(self):
        self.fk.set("sys/class/nvme/nvme0/device/device", "0xa80a\n")
        self.assert_nothing_destroyed(self.install(), "REFUSE: nvme0 is not the internal Samsung")

    def test_root_on_the_nvme(self):
        self.fk.set("proc/self/mounts", "/dev/nvme0n1p1 / f2fs rw 0 0\n")
        self.assert_nothing_destroyed(self.install(), "REFUSE: maintenance root not on sda")

    def test_wrong_entry(self):
        self.fk.set("proc/cmdline", "root=PARTUUID=x initcall_blacklist=nvme_init,mei_me_driver_init\n")
        self.assert_nothing_destroyed(self.install(), "REFUSE: not the NVMe install entry")

    def test_backup_ssh_failure(self):
        self.fk.rules([{"when": "count=1 status=none\n", "exit": 255, "once": True}])
        self.assert_nothing_destroyed(self.install(), "02-head: remote exit status 255")

    def test_backup_truncated(self):
        self.fk.rules([{"when": "count=1 status=none\n", "truncate": 4096, "once": True}])
        self.assert_nothing_destroyed(self.install(), "GPT backup head: 4096 bytes")

    def test_backup_corrupted(self):
        self.fk.rules([{"when": "iflag=skip_bytes status=none\n", "corrupt": True, "once": True}])
        self.assert_nothing_destroyed(self.install(), "GPT backup tail: sha256 differs")

    def test_target_changes_between_guards_and_discard(self):
        # state flips right before the destructive invocation (e.g. a reboot into another setup): the guard that
        # runs inside that same invocation must stop it
        self.fk.rules([{"when": "blkdiscard", "files": {"disks/nvme0n1/SERIAL": "TESTNVME9999"}}])
        r = self.install()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("03-copy: remote exit status 1", r.stdout)
        self.assertIn("blkdiscard", self.fk.sent())               # sent, but stopped by its own guard
        self.assertEqual(self.fk.writes(), [])

    def test_copy_failure_stops_before_the_gpt_rewrite(self):
        self.fk.faults({"dd_write_exit": 1})
        r = self.install()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("03-copy: remote exit status 1", r.stdout)
        w = self.fk.writes()
        self.assertEqual(w[0], "BLKDISCARD -f /dev/nvme0n1")     # positive control: the guards let it through
        self.assertFalse([x for x in w if x.startswith("SGDISK")], w)
        self.assertNotIn("sgdisk -e", self.fk.sent())

    def test_resize_failure_inside_a_pipeline_stops_the_install(self):
        self.fk.faults({"fail": ["resize.f2fs"]})
        r = self.install()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("05-gpt: remote exit status", r.stdout)
        self.assertFalse([c for c in self.fk.lines("calls.log") if re.match(r"mount /dev/nvme0n1p12", c)])


if __name__ == "__main__":
    unittest.main()
