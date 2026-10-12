"""kano-delta-push.py (stick) and kano-delta-push-nvme.py (internal NVMe) against the fake KANO.

Refusals must happen before any write command is even sent; the guard that rides along with every dd must stop a
write when the target changed after the checks (identity, GPT, kernel table, mounted/swap/held), and a query that
fails there must refuse too."""
import unittest
from helpers import (CHANGED_MIB, HARNESS, IMAGE_PARTS, MiB, NVME_PARTS, NVME_SIZE, STICK_PARTS, STICK_SIZE,
                     HarnessCase, make_disk, make_images)


class Base(HarnessCase):
    script = None

    def setUp(self):
        super().setUp()
        self.fk.kano()
        self.old, self.new = make_images(self.fk.tmp / "img")
        self.copy_old_to("sda")
        self.copy_old_to("nvme0n1")

    def copy_old_to(self, disk):
        """The target already holds the old image's partition contents (MiB 1..15)."""
        data = self.old.read_bytes()
        with open(self.fk.root / "dev" / disk, "r+b") as f:
            f.seek(MiB)
            f.write(data[MiB:12 * MiB])          # everything but userdata, which moved on the NVMe

    def push(self, *extra):
        return self.fk.run(["python3", HARNESS / self.script, self.old, self.new, "192.0.2.10", *extra], timeout=180)

    def disk_mib(self, disk, m):
        with open(self.fk.root / "dev" / disk, "rb") as f:
            f.seek(m * MiB)
            return f.read(MiB)

    def new_mib(self, m):
        return self.new.read_bytes()[m * MiB:(m + 1) * MiB]

    def assert_refused_at_the_write(self, r, why):
        """Checks passed and the write command went out, but its guard stopped it on the KANO."""
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("identity check", r.stderr, self.explain(r))
        self.reason(why, r.stderr, self.explain(r))
        self.assertIn("dd of=", self.fk.sent())
        self.assertEqual(self.fk.writes(), [])

    def assert_refused_before_writing(self, r, why):
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason(why, r.stderr + r.stdout, self.explain(r))
        self.assertNotIn("dd of=", self.fk.sent(), "a write command was sent")
        self.assertEqual(self.fk.writes(), [])


class NoAsserts(unittest.TestCase):
    def test_guards_are_explicit_exits_not_asserts(self):
        # `python3 -O` strips assert statements; no guard may depend on one
        import re
        for name in ("kano-delta-push.py", "kano-delta-push-nvme.py"):
            with self.subTest(file=name):
                self.assertIsNone(re.search(r"^\s*assert\b", (HARNESS / name).read_text(), re.M))


class StickPush(Base):
    script = "kano-delta-push.py"

    def test_happy_path_writes_the_changed_blocks(self):
        r = self.push()
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("pushed and verified 4 MiB", r.stdout)
        self.assertEqual(len(self.fk.writes()), 3)               # runs: MiB 2, MiB 5-6, MiB 10
        for m in CHANGED_MIB:
            self.assertEqual(self.disk_mib("sda", m), self.new_mib(m), m)

    def test_dry_run_sends_nothing(self):
        r = self.push("--dry-run")
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertEqual(self.fk.ssh_calls(), [])

    def test_wrong_serial(self):
        self.fk.set("disks/sda/SERIAL", "0123456789ABCDEF")
        self.assert_refused_before_writing(self.push(), "is not the Kingston stick")

    def test_not_a_usb_disk(self):
        self.fk.set("disks/sda/TRAN", "sata")
        self.assert_refused_before_writing(self.push(), "is not a USB disk")

    def test_gpt_mismatch(self):
        parts = [(2, "super", 5, 10) if p[0] == 2 else p for p in STICK_PARTS]
        make_disk(self.fk.root / "dev" / "sda", STICK_SIZE, parts)
        self.assert_refused_before_writing(self.push(), "partition 2 'super': image")

    def test_kernel_table_differs_from_gpt(self):
        self.fk.set("sys/block/sda/sda2/start", "10240\n")
        self.assert_refused_before_writing(self.push(), "kernel's table")

    def test_write_into_a_mounted_partition(self):
        self.fk.mount("/dev/sda2", "/mnt/super")
        self.assert_refused_before_writing(self.push(), "sda2, which is mounted")

    def test_write_into_a_held_partition(self):
        (self.fk.root / "sys/block/sda/sda5/holders/dm-0").mkdir()
        self.assert_refused_before_writing(self.push(), "sda5, which is held by dm-0")

    def test_write_overlapping_a_partition_that_is_not_in_the_image(self):
        make_disk(self.fk.root / "dev" / "sda", STICK_SIZE, STICK_PARTS + [(6, "rogue", 5, 7)])
        self.assert_refused_before_writing(self.push(), "overlaps partition 6 'rogue'")

    def test_facts_call_fails(self):
        self.fk.rules([{"when": "FACTS_OK", "exit": 255}])
        self.assert_refused_before_writing(self.push(), "could not read the target")

    def test_identity_rechecked_in_the_same_command_as_each_write(self):
        # the stick is swapped after the checks passed: the dd must not run
        self.fk.rules([{"when": "dd of=", "files": {"disks/sda/SERIAL": "SOMEOTHERSTICK"}}])
        r = self.push()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("identity check", r.stderr)
        self.assertIn("dd of=", self.fk.sent())                  # the write command went out ...
        self.assertEqual(self.fk.writes(), [])                   # ... and was refused on the KANO


    def test_partition_mounted_after_the_checks(self):
        # the first run (MiB 2) lies in sda5; it gets mounted between the facts and the write
        self.fk.rules([{"when": "dd of=", "files": {"proc/self/mounts": "/dev/sda33 / ext4 rw 0 0\n"
                                                                          "/dev/sda5 /mnt/esp vfat rw 0 0\n"}}])
        self.assert_refused_at_the_write(self.push(), "sda5 is mounted")

    def test_partition_held_after_the_checks(self):
        self.fk.rules([{"when": "dd of=", "files": {"sys/block/sda/sda5/holders/dm-1": ""}}])
        self.assert_refused_at_the_write(self.push(), "sda5 is held by dm-1")

    def test_swap_after_the_checks(self):
        self.fk.rules([{"when": "dd of=", "files": {"proc/swaps": "Filename Type Size Used Priority\n"
                                                                    "/dev/sda5 partition 1 0 -2\n"}}])
        self.assert_refused_at_the_write(self.push(), "sda5 is used as swap")

    def test_gpt_rewritten_after_the_checks(self):
        # once the checks passed, the stick's partition table is rewritten (super moved): its first MiB differs
        make_disk(self.fk.tmp / "other.img", STICK_SIZE, [(2, "super", 5, 10) if p[0] == 2 else p for p in STICK_PARTS])
        (self.fk.root / "gpt-new.bin").write_bytes((self.fk.tmp / "other.img").read_bytes()[:MiB])
        self.fk.rules([{"when": "dd of=", "overwrite": {"gpt-new.bin": "dev/sda"}}])
        self.assert_refused_at_the_write(self.push(), "the partition table changed")

    def test_kernel_table_moved_after_the_checks(self):
        self.fk.rules([{"when": "dd of=", "files": {"sys/block/sda/sda5/start": "4096\n"}}])
        self.assert_refused_at_the_write(self.push(), "sda5 is not where the checks saw it")

    def test_unreadable_swaps_at_the_write(self):
        self.fk.rules([{"when": "dd of=", "remove": ["proc/swaps"]}])
        self.assert_refused_at_the_write(self.push(), "proc/swaps: No such file")

    def test_unreadable_holders_at_the_write(self):
        self.fk.rules([{"when": "dd of=", "remove": ["sys/block/sda/sda5/holders"]}])
        self.assert_refused_at_the_write(self.push(), "cannot read the holders of sda5")

    def test_facts_without_a_holders_dir(self):
        self.fk.rules([{"when": "FACTS_OK", "remove": ["sys/block/sda/holders"]}])
        self.assert_refused_before_writing(self.push(), "could not read the target")

    def test_facts_ssh_exit_2(self):
        self.fk.rules([{"when": "FACTS_OK", "exit": 2}])
        self.assert_refused_before_writing(self.push(), "ssh exit 2")


class NvmePush(Base):
    script = "kano-delta-push-nvme.py"

    def test_happy_path_writes_only_super_and_vbmeta(self):
        r = self.push()
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("pushed and verified 3 MiB to the NVMe", r.stdout)
        self.assertTrue(all(w.startswith("WRITE nvme0n1 ") for w in self.fk.writes()), self.fk.writes())
        for m in (5, 6, 10):
            self.assertEqual(self.disk_mib("nvme0n1", m), self.new_mib(m), m)
        self.assertNotEqual(self.disk_mib("nvme0n1", 2), self.new_mib(2))      # ESP block skipped

    def test_dry_run_sends_nothing(self):
        r = self.push("--dry-run")
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertEqual(self.fk.ssh_calls(), [])

    def test_wrong_serial(self):
        self.fk.set("disks/nvme0n1/SERIAL", "TESTNVME9999")
        self.assert_refused_before_writing(self.push(), "is not the internal NVMe")

    def test_gpt_mismatch(self):
        parts = [(2, "super", 4, 9) if p[0] == 2 else p for p in NVME_PARTS]
        make_disk(self.fk.root / "dev" / "nvme0n1", NVME_SIZE, parts)
        self.assert_refused_before_writing(self.push(), "'super': image")

    def test_nvme_is_the_root_disk(self):
        self.fk.set("proc/self/mounts", "/dev/nvme0n1p1 / f2fs rw 0 0\n")
        self.assert_refused_before_writing(self.push(), "is on the NVMe")

    def test_nvme_partition_mounted(self):
        self.fk.mount("/dev/nvme0n1p5", "/mnt/nesp")
        self.assert_refused_before_writing(self.push(), "nvme0n1p5 is in use: mounted")

    def test_nvme_used_as_swap(self):
        self.fk.set("proc/swaps", "Filename Type Size Used Priority\n/dev/nvme0n1p1 partition 1 0 -2\n")
        self.assert_refused_before_writing(self.push(), "nvme0n1p1 is in use: used as swap")

    def test_nvme_held(self):
        (self.fk.root / "sys/block/nvme0n1/nvme0n1p1/holders/dm-3").mkdir()
        self.assert_refused_before_writing(self.push(), "nvme0n1p1 is in use: held by dm-3")

    def test_not_the_maintenance_os(self):
        # / still on sda33, but sda is not the Kingston stick
        self.fk.set("disks/sda/SERIAL", "SOMEOTHERDISK")
        self.assert_refused_before_writing(self.push(), "not the maintenance OS")

    def test_kernel_table_differs_from_gpt(self):
        self.fk.set("sys/block/nvme0n1/nvme0n1p2/start", "10240\n")
        self.assert_refused_before_writing(self.push(), "kernel's table")

    def test_write_overlapping_another_nvme_partition(self):
        make_disk(self.fk.root / "dev" / "nvme0n1", NVME_SIZE, NVME_PARTS + [(9, "rogue", 9, 11)])
        self.assert_refused_before_writing(self.push(), "overlaps partition 9 'rogue'")

    def test_identity_rechecked_in_the_same_command_as_each_write(self):
        self.fk.rules([{"when": "dd of=", "files": {"proc/self/mounts": "/dev/sda33 / ext4 rw 0 0\n"
                                                                          "/dev/nvme0n1p2 /mnt ext4 rw 0 0\n"}}])
        self.assert_refused_at_the_write(self.push(), "nvme0n1p2 is mounted")

    def test_wrong_serial_at_the_write(self):
        self.fk.rules([{"when": "dd of=", "files": {"disks/nvme0n1/SERIAL": "TESTNVME9999"}}])
        self.assert_refused_at_the_write(self.push(), "not the internal NVMe")

    def test_root_disk_unknown_at_the_write(self):
        # lsblk prints nothing for the root's parent disk: "" != nvme0n1 must not count as "root not on the NVMe"
        self.fk.rules([{"when": "dd of=", "remove": ["disks/sda33/PKNAME"]}])
        self.assert_refused_at_the_write(self.push(), "not the internal NVMe")

    def test_swap_on_the_nvme_after_the_checks(self):
        self.fk.rules([{"when": "dd of=", "files": {"proc/swaps": "Filename Type Size Used Priority\n"
                                                                    "/dev/nvme0n1 partition 1 0 -2\n"}}])
        self.assert_refused_at_the_write(self.push(), "nvme0n1 is used as swap")


if __name__ == "__main__":
    unittest.main()
