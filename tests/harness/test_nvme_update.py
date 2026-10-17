"""kano-nvme-update.sh against the fake KANO. It runs from a copy of the harness in which the sibling kano-boot.sh
(EC power cycle + boot menu over the AP UART) is a logging fake; one test also replaces the NVMe watcher."""
import hashlib, importlib.util, json, shutil, struct, unittest
from helpers import BOOT_A_GUID, HARNESS, MiB, HarnessCase, harness_copy, initrd, make_disk, make_images


def autooff_mod():
    spec = importlib.util.spec_from_file_location("kano_autooff_image", HARNESS / "kano-autooff-image.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

ARCHIVE = b"INITRD-ARCHIVE" * 64


class Update(HarnessCase):
    def setUp(self):
        super().setUp()
        fk = self.fk
        fk.kano()
        self.h = fk.tmp / "harness"
        harness_copy(self.h)
        (self.h / "kano-boot.sh").write_text('#!/usr/bin/env bash\necho "$*" >> "$FAKE_ROOT/log/boot.log"\n')
        self.work = fk.work
        old, new = make_images(fk.tmp / "img")
        for n, img, digest in ((1, old, "11"), (2, new, "22")):
            d = self.work / f"kano-final{n}"
            d.mkdir(parents=True)
            shutil.copy(img, d / f"kano-final{n}.img")
            initrd(d / "initrd.img", ARCHIVE, digest=digest)
        (self.work / "kano-final2.log").write_text("PASS boot\nkano-final2.img sha256 0123abcd\n")
        with open(fk.root / "dev" / "nvme0n1", "r+b") as f:            # the NVMe holds final1
            f.seek(MiB)
            f.write(old.read_bytes()[MiB:12 * MiB])
        (fk.root / "mnt/nesp").mkdir(parents=True)
        (fk.root / "mnt/xb/gbos-nvme").mkdir(parents=True)
        initrd(fk.root / "mnt/nesp/initrd.img", ARCHIVE, guid=BOOT_A_GUID, digest="11")
        self.runs = fk.runs
        self.out = self.runs / "nvme-update-2"
        self.runs.mkdir(parents=True)
        (self.runs / "ap-uart.log").write_text("... reboot: Power down\n")
        self.adb({"init.svc.kano_bootfix": "stopped", "sys.boot_completed": "1"})
        autooff_mod().write_vendor_fixture(self.work / "kano-final2" / "kano-final2.img", ["etc/placeholder"])

    def adb(self, props, connect=True):
        self.fk.set("adb.json", json.dumps({"connect": connect, "props": props}))

    def update(self):
        return self.fk.run(["bash", self.h / "kano-nvme-update.sh", "1", "2"], timeout=300)

    def adb_log(self):
        return self.fk.lines("adb.log")

    def assert_no_push(self, r, why):
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason(why, r.stdout + r.stderr, self.explain(r))
        self.assertNotIn("dd of=", self.fk.sent())
        self.assertEqual(self.fk.writes(), [])

    def test_stale_artifacts_and_failing_step(self):
        # review I10: a previous run of the same version left bootconfig/sha256/push results behind
        self.out.mkdir(parents=True)
        (self.out / "bootconfig.txt").write_text("androidboot.vbmeta.digest=stale\n")
        (self.out / "initrd-nvme.sha256").write_text("f" * 64 + "\n")
        (self.out / "push.txt").write_text("pushed and verified 3 MiB to the NVMe\n")
        (self.out / "run.log").write_text("pushed and verified 3 MiB to the NVMe\n")
        initrd(self.work / "kano-final2/initrd.img", b"ANOTHER ARCHIVE", digest="22")   # step 2 must fail
        r = self.update()
        self.assert_no_push(r, "initrd archives differ")
        self.reason("initrd prep failed", r.stdout)
        self.assertEqual(self.fk.ssh_calls(), [])
        self.assertEqual(self.adb_log(), [])
        self.assertFalse((self.out / "bootconfig.txt").exists())
        self.assertFalse((self.out / "initrd-nvme.sha256").exists())
        self.assertTrue((self.out / "run.log.prev").exists())

    def test_dry_run_refusal_stops_everything(self):
        img = self.work / "kano-final2/kano-final2.img"
        make_disk(img, 16 * MiB, [(2, "super", 5, 10)])
        # The scan runs before the dry run. A layout the delta tool will refuse still needs a
        # readable clean vendor, including the empty vendor_b a normal super carries.
        autooff_mod().write_vendor_fixture(img, ["etc/placeholder"])
        r = self.update()
        self.assert_no_push(r, "dry run refused")
        self.assertEqual(self.fk.ssh_calls(), [])
        self.assertEqual(self.adb_log(), [])

    def test_boot_a_guid_read_from_the_disk_must_match(self):
        self.fk.set("sgdisk/nvme0n1/i13", "Partition unique GUID: 11111111-2222-3333-4444-555555555555\n"
                                          "Partition name: 'boot_a'\n")
        r = self.update()
        self.assert_no_push(r, "NVMe boot_a GUID is '11111111-2222-3333-4444-555555555555'")
        self.assertEqual(self.fk.lines("boot.log"), ["KANO maintenance NVMe install"])
        self.assertIn("-s 192.0.2.11:5555 reboot -p", self.adb_log())

    def test_partition_13_must_be_boot_a(self):
        self.fk.set("sgdisk/nvme0n1/i13", f"Partition unique GUID: {BOOT_A_GUID}\nPartition name: 'boot_b'\n")
        self.assert_no_push(self.update(), "REFUSE: NVMe partition 13 is 'boot_b'")

    def test_adb_only_the_fixed_address_and_only_this_projects_android(self):
        self.adb({"sys.boot_completed": "1"})          # something answers at the address, but not our image
        self.fk.set("sgdisk/nvme0n1/i13", "Partition unique GUID: 00000000-0000-0000-0000-000000000000\n"
                                          "Partition name: 'boot_a'\n")
        r = self.update()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        log = self.adb_log()
        self.assertFalse([l for l in log if "reboot" in l], log)
        self.assertTrue(log)
        for line in log:
            self.assertIn("192.0.2.11:5555", line)
        self.reason("no KANO Android at 192.0.2.11:5555", r.stdout)

    def test_bootconfig_transfer_failure(self):
        self.fk.rules([{"when": "cat > /root/bootconfig-new.txt", "exit": 255}])
        r = self.update()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("bootconfig transfer failed", r.stdout)
        self.assertTrue(self.fk.writes(), "positive control: the push ran")
        self.assertFalse([c for c in self.fk.lines("calls.log") if c.startswith("mount /dev/nvme0n1p12")])

    def test_esp_rewrite_checks_the_result_and_unmounts(self):
        initrd(self.fk.root / "mnt/nesp/initrd.img", b"A DIFFERENT ARCHIVE", guid=BOOT_A_GUID)
        before = (self.fk.root / "mnt/nesp/initrd.img").read_bytes()
        r = self.update()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("REFUSE: rebuilt initrd sha256 mismatch", r.stdout)
        self.assertEqual((self.fk.root / "mnt/nesp/initrd.img").read_bytes(), before)    # nothing copied over it
        self.assertFalse((self.fk.root / "mnt/xb/gbos-nvme/initrd.img").exists())
        self.assertNotIn("nvme0n1p12", (self.fk.root / "proc/self/mounts").read_text())

    def test_watcher_failure_fails_the_run(self):
        (self.h / "kano-gbos-watch-nvme.py").write_text("import sys\nprint('UNOBSERVABLE: fake')\nsys.exit(4)\n")
        # keeps step 6 short: no KANO Android shows up, so the wait loop ends when the watcher is gone
        self.fk.rules([{"when": "echo INITRD_OK", "files": {"adb.json": json.dumps({"connect": True, "props": {}})}}])
        r = self.update()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("watcher exit 4", r.stdout)
        # everything before it worked: the ESP initrd now carries final2's bootconfig
        bc = (self.out / "bootconfig.txt").read_bytes()
        want = ARCHIVE + bc + struct.pack("<II", len(bc), sum(bc) & 0xFFFFFFFF) + b"#BOOTCONFIG\n"
        self.assertEqual((self.fk.root / "mnt/nesp/initrd.img").read_bytes(), want)
        self.assertEqual((self.fk.root / "mnt/xb/gbos-nvme/initrd.img").read_bytes(), want)
        self.assertIn(f"androidboot.boot_part_uuid={BOOT_A_GUID}".encode(), bc)
        self.assertEqual(hashlib.sha256(want).hexdigest(), (self.out / "initrd-nvme.sha256").read_text().strip())
        # no KANO Android was identified after the update boot: only the identity query went to the address
        after = self.adb_log()[[i for i, l in enumerate(self.adb_log()) if "reboot -p" in l][-1] + 1:]
        self.assertTrue(after)
        self.assertEqual([l for l in after if " shell " in l and "getprop init.svc.kano_bootfix" not in l], [], after)

    def test_the_push_is_scanned_again_immediately_before_the_writer(self):
        text = (HARNESS / "kano-nvme-update.sh").read_text()
        first = text.index('> "$OUT/autooff.txt"')
        again = text.index('> "$OUT/autooff-push.txt"')
        push = text.index('python3 "$HERE/kano-delta-push-nvme.py" "$OLDIMG" "$NEWIMG" "$KANO_IP" >')
        self.assertLess(first, again)
        self.assertLess(again, push)
        between = text[again:push]
        self.assertIn("vendor clean", between)
        self.assertNotIn("kano-ssh", between)
        self.assertNotIn("adb", between)

    def test_refuses_test_super_before_any_write(self):
        autooff_mod().write_vendor_fixture(
            self.work / "kano-final2" / "kano-final2.img",
            ["etc/kano-autooff.nonce", "etc/init/kano-autooff.rc"],
        )
        r = self.update()
        self.assert_no_push(r, "etc/kano-autooff.nonce")
        self.reason("etc/init/kano-autooff.rc", r.stdout + r.stderr)
        self.assertEqual(self.fk.ssh_calls(), [])
        self.assertEqual(self.adb_log(), [])
        self.assertFalse((self.out / "dryrun.txt").exists())

    def test_delta_writer_refuses_userdata_without_writing(self):
        # kano-delta-push-nvme.py writes super and vbmeta only. A changed userdata block is refused
        # before ssh, and this tool never creates /data/kano-autooff.nonce.
        img = self.work / "kano-final2" / "kano-final2.img"
        raw = bytearray(img.read_bytes())
        raw[12 * MiB] ^= 0x5A
        img.write_bytes(raw)
        old = self.work / "kano-final1" / "kano-final1.img"
        r = self.fk.run(
            ["python3", str(HARNESS / "kano-delta-push-nvme.py"), str(old), str(img), "192.0.2.10"],
            timeout=180,
        )
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("userdata", r.stdout + r.stderr)
        self.assertNotIn("dd of=", self.fk.sent())
        self.assertEqual(self.fk.writes(), [])

    def test_second_scan_sees_a_vendor_swapped_after_the_dry_run(self):
        new = self.work / "kano-final2" / "kano-final2.img"
        swapped = self.fk.tmp / "swapped.img"
        shutil.copy(new, swapped)
        autooff_mod().write_vendor_fixture(swapped, ["etc/kano-autooff.nonce"])
        self.fk.rules([{"when": "NVMe partition 13 is", "once": True,
                        "host_copy": {str(swapped): str(new)}}])
        r = self.update()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stdout + r.stderr)
        self.assertFalse((self.out / "push.txt").exists(), self.explain(r))
        self.assertEqual(self.fk.writes(), [])

    def test_unreadable_swaps_is_not_idle(self):
        self.fk.rules([{"when": "NVME_ID_OK", "remove": ["proc/swaps"]}])
        self.assert_no_push(self.update(), "REFUSE: cannot read")


if __name__ == "__main__":
    unittest.main()
