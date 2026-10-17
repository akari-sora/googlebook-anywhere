"""kano-nvme-install.sh against the fake KANO: guards in the same remote invocation as the destructive commands,
decisions only from this run's output, GPT backup before the discard, stop at the first failing step; the source
(KANO_NVME_SRC) checked locally and against the stick's ESP before anything is written; a guard query that fails
refuses instead of counting as "idle". A TEST autooff vendor in the stick region that would be copied is refused
before blkdiscard; a nonce left on the copied userdata is deleted before umount.
Only an lstat of ENOENT counts as absent. A lookup, directory read, delete, confirming lstat,
remount, recheck, or unmount that fails stops before resize.f2fs, fsck.f2fs, and the ESP.
A nonce directory is removed inside python3; filenames are not passed back to the shell."""
import hashlib, importlib.util, io, os, re, shlex, shutil, struct, unittest, zlib
from contextlib import redirect_stderr, redirect_stdout
from helpers import (BOOT_A_GUID, HARNESS, IMAGE_PARTS, IMAGE_SIZE, MiB, REPO, HarnessCase, harness_copy, initrd,
                     make_disk, make_images)
import lapis_lp


def autooff_mod():
    spec = importlib.util.spec_from_file_location("kano_autooff_image", HARNESS / "kano-autooff-image.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

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


def sector_pad(blob):
    nsec = (len(blob) + 511) // 512
    return blob + bytes(nsec * 512 - len(blob)), nsec


def gpt_bounds(mod, disk):
    reader = mod.FileReader(str(disk), None)
    try:
        return mod.find_supers(reader)[0]
    finally:
        reader.close()


def every_slot(slots, primary, backup=None):
    backup = primary if backup is None else backup
    return {(slot, flag): (backup if flag else primary) for slot in range(slots) for flag in (False, True)}


def plant_blobs(disk, mod, blobs, images, mx, slots):
    super_off, super_end = gpt_bounds(mod, disk)
    geo = mod._geometry_slot(mx, slots, 4096)
    with open(disk, "r+b") as handle:
        handle.seek(super_off + mod.LP_PRIMARY_GEOMETRY)
        handle.write(geo)
        handle.seek(super_off + mod.LP_BACKUP_GEOMETRY)
        handle.write(geo)
        for (slot, backup), blob in blobs.items():
            if len(blob) > mx:
                raise AssertionError("metadata blob %d does not fit in %d" % (len(blob), mx))
            handle.seek(super_off + mod._metadata_offset(mx, slots, slot, backup))
            handle.write(blob + bytes(mx - len(blob)))
        for rel, payload in images:
            handle.seek(super_off + rel)
            handle.write(payload)
    return super_off, super_end


def pad_lp_tables(blob, tsize):
    raw = bytearray(blob)
    hsz = struct.unpack_from("<I", raw, 8)[0]
    tables = bytes(raw[hsz:])
    if tsize < len(tables):
        raise AssertionError("tables pad is smaller than the table")
    raw = raw[:hsz] + tables + bytes(tsize - len(tables))
    struct.pack_into("<I", raw, 44, tsize)
    raw[48:80] = hashlib.sha256(raw[hsz:hsz + tsize]).digest()
    raw[12:44] = bytes(32)
    raw[12:44] = hashlib.sha256(raw[:hsz]).digest()
    return bytes(raw)


def resign_header(data, at):
    hsize = struct.unpack_from("<I", data, at + 12)[0]
    hdr = bytearray(data[at:at + hsize])
    hdr[16:20] = b"\x00" * 4
    struct.pack_into("<I", hdr, 16, zlib.crc32(bytes(hdr)) & 0xFFFFFFFF)
    data[at:at + hsize] = hdr


def resign_primary_gpt(data):
    ents = bytes(data[1024:1024 + 128 * 128])
    struct.pack_into("<I", data, 512 + 88, zlib.crc32(ents) & 0xFFFFFFFF)
    resign_header(data, 512)


def resign_backup_gpt(data):
    backup_at = len(data) - (128 * 128 + 512)
    ents = bytes(data[backup_at:backup_at + 128 * 128])
    struct.pack_into("<I", data, len(data) - 512 + 88, zlib.crc32(ents) & 0xFFFFFFFF)
    resign_header(data, len(data) - 512)


def kill_backup_gpt(data):
    data[len(data) - 512:len(data) - 512 + 8] = b"XXXXXXXX"


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
        # daily vendor: LP + EROFS with no autooff paths, so the scan runs and allows the copy
        autooff_mod().write_vendor_fixture(fk.root / "dev" / "sda", ["etc/placeholder"])
        # sgdisk -i 1 starts at sector 24576, size 6144. The rewrite command updates
        # that answer to sector 30720, size 8192, and does not touch sysfs. 8192 is
        # at least the old 6144, so the happy path's new p1 can hold the userdata
        # copy. Sysfs here is the post-reread node: OLD_START != NEW_START, and the
        # length check compares the kernel size to the new partition size.
        fk.set("sys/block/nvme0n1/nvme0n1p1/start", "30720\n")
        fk.set("sys/block/nvme0n1/nvme0n1p1/size", "8192\n")

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
        self.assertIn("vendor clean", r.stdout)
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
        self.assertLess(copy.index("\nguard\n"), copy.index("kano-autooff-image.py"))
        self.assertLess(copy.index("kano-autooff-image.py"), copy.index("blkdiscard"))
        self.assertLess(copy.index("sda1 is not userdata"), copy.index("blkdiscard"))
        gpt = (run / "05-gpt.sh").read_text()
        self.assertLess(gpt.index("echo UD_COPY_OK"), gpt.index("strip_nonce /mnt/ud/kano-autooff.nonce"))
        self.assertLess(gpt.index("strip_nonce /mnt/ud/kano-autooff.nonce"), gpt.index("\numount /mnt/ud"))
        self.assertLess(gpt.index("\numount /mnt/ud"), gpt.index("mount -o ro -t f2fs /dev/nvme0n1p1 /mnt/ud"))
        self.assertLess(gpt.index("mount -o ro -t f2fs /dev/nvme0n1p1 /mnt/ud"),
                        gpt.index("still present after unmount"))
        self.assertLess(gpt.index("still present after unmount"), gpt.index("\nresize.f2fs"))
        self.assertLess(gpt.index("\nresize.f2fs"), gpt.index("\nfsck.f2fs"))
        self.assertLess(gpt.index("kernel p1 start is not the new userdata start"),
                        gpt.index("mount -t f2fs /dev/nvme0n1p1 /mnt/ud"))
        self.assertLess(gpt.index("new p1 is shorter than the userdata copy"),
                        gpt.index('dd if="$D" of="$D"'))
        self.assertLess(gpt.index("kernel p1 length is not the new userdata length"),
                        gpt.index('dd if="$D" of="$D"'))
        self.assertLess(gpt.index("/sys/block/nvme0n1/nvme0n1p1/start"), gpt.index("echo UD_COPY_OK"))
        self.assertLess(gpt.index("/sys/block/nvme0n1/nvme0n1p1/size"), gpt.index("echo UD_COPY_OK"))
        moved = (self.fk.root / "sgdisk/nvme0n1/i1").read_text()
        self.assertIn("First sector: 30720", moved)
        self.assertIn("Partition size: 8192 sectors", moved)
        self.assertNotIn("First sector: 24576", moved)
        calls = self.fk.lines("calls.log")
        ro = [i for i, c in enumerate(calls) if c.startswith("mount ") and " -o ro " in c and "f2fs" in c]
        resize = [i for i, c in enumerate(calls) if c.startswith("resize.f2fs")]
        self.assertEqual(len(ro), 1, calls)
        self.assertEqual(len(resize), 1, calls)
        self.assertLess(ro[0], resize[0])
        self.assertEqual(len(self._copied_umounts()), 2, calls)
        early = (run / "01-autooff.sh").read_text()
        self.assertIn("kano-autooff-image.py", early)
        self.assertNotIn("blkdiscard", early)
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

    def test_copy_scans_again_if_the_stick_changes_before_discard(self):
        # 01-autooff sees the clean vendor from setUp. The stick becomes a TEST image only when the
        # copy script (the one that contains blkdiscard) is sent, so only the second scan can refuse it.
        src = self.fk.root / "stick-test.img"
        shutil.copy(self.fk.root / "dev" / "sda", src)
        autooff_mod().write_vendor_fixture(src, ["etc/kano-autooff.nonce"])
        self.fk.rules([{"when": "blkdiscard", "overwrite": {"stick-test.img": "dev/sda"}}])
        r = self.install()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stdout + r.stderr, self.explain(r))
        self.reason("03-copy: remote exit status 1", r.stdout)
        self.assertEqual(self.fk.writes(), [])
        self.assertEqual((self.fk.root / "dev" / "nvme0n1").read_bytes(), self.nvme_before)

    def test_refuses_vendor_nonce_before_any_write(self):
        autooff_mod().write_vendor_fixture(self.fk.root / "dev" / "sda", ["etc/kano-autooff.nonce"])
        r = self.install()
        self.assert_nothing_destroyed(r, "etc/kano-autooff.nonce")
        self.assertFalse((self.run_dir() / "03-copy.sh").exists())
        self.assertNotIn("blkdiscard", (self.run_dir() / "01-autooff.sh").read_text())

    def test_refuses_vendor_rc_before_any_write(self):
        autooff_mod().write_vendor_fixture(self.fk.root / "dev" / "sda", ["etc/init/kano-autooff.rc"])
        r = self.install()
        self.assert_nothing_destroyed(r, "etc/init/kano-autooff.rc")
        self.assertEqual(self.fk.writes(), [])

    def test_userdata_nonce_is_removed_before_umount(self):
        nonce = self.fk.root / "mnt" / "ud" / "kano-autooff.nonce"
        self.fk.rules([{"when": "mount -t f2fs /dev/nvme0n1p1 /mnt/ud",
                        "files": {"mnt/ud/kano-autooff.nonce": "0123456789abcdef0123456789abcdef\n"}}])
        r = self.install()
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertFalse(nonce.exists())
        text = (self.run_dir() / "05-gpt.sh").read_text()
        mount_at = text.index("mount -t f2fs /dev/nvme0n1p1 /mnt/ud")
        rm_at = text.index("strip_nonce /mnt/ud/kano-autooff.nonce")
        umount_at = text.index("umount /mnt/ud")
        self.assertLess(mount_at, rm_at)
        self.assertLess(rm_at, umount_at)
        self.assertNotIn("nvme0n1p1", (self.fk.root / "proc/self/mounts").read_text())

    def test_erofs_names_match_the_builder_walker(self):
        import sys
        sys.path.insert(0, str(REPO / "builder" / "lib"))
        import rebuild
        mod = autooff_mod()
        names = ["etc/placeholder", "etc/kano-autooff.nonce", "etc/init/kano-autooff.rc"]
        blob = mod.build_erofs(names)
        self.assertEqual(mod.erofs_paths(blob), {
            "etc", "etc/placeholder", "etc/kano-autooff.nonce", "etc/init", "etc/init/kano-autooff.rc",
        })
        path = self.fk.tmp / "vendor.erofs"
        path.write_bytes(blob)
        data, blksz, meta, xattr, root = rebuild.load_image(path)
        vmap, _hard = rebuild.walk_image(str(path), root, data, blksz, meta, xattr)
        self.assertEqual(mod.erofs_paths(blob), {key for key in vmap if key})

    def test_vendor_b_and_broken_lp_and_plain_disk(self):
        mod = autooff_mod()
        both = self.fk.tmp / "both.img"
        make_disk(both, IMAGE_SIZE, IMAGE_PARTS)
        mod.write_vendor_fixture(both, ["etc/placeholder"], b_paths=["etc/init/kano-autooff.rc"])
        r = self.fk.run(["python3", HARNESS / "kano-autooff-image.py", "--file", str(both)], timeout=60)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/init/kano-autooff.rc", r.stderr)
        broken = self.fk.tmp / "broken.img"
        make_disk(broken, IMAGE_SIZE, IMAGE_PARTS)
        mod.write_broken_lp(broken)
        r = self.fk.run(["python3", HARNESS / "kano-autooff-image.py", "--file", str(broken)], timeout=60)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("cannot list vendor", r.stderr)
        plain = self.fk.tmp / "plain.img"
        make_disk(plain, IMAGE_SIZE, IMAGE_PARTS)
        r = self.fk.run(["python3", HARNESS / "kano-autooff-image.py", "--file", str(plain)], timeout=60)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("no valid LP geometry", r.stderr)

    def _check(self, disk):
        return self.fk.run(["python3", str(HARNESS / "kano-autooff-image.py"), "--file", str(disk)], timeout=120)

    def test_empty_vendor_b_backup_lp_and_resolved_erofs(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "edges.img"

        def plant(paths, b_paths=None, links=None):
            make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
            mod.write_vendor_fixture(disk, paths, b_paths=b_paths, links=links)

        plant(["etc/placeholder"])
        r = self._check(disk)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)

        plant(["etc/kano-autooff.nonce"])
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("no extents", r.stderr)

        plant(["etc/placeholder"], b_paths=["etc/init/kano-autooff.rc"])
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/init/kano-autooff.rc", r.stderr)

        # Primary geometry checksum fails; the backup copy still describes the TEST vendor.
        plant(["etc/kano-autooff.nonce"])
        reader = mod.FileReader(str(disk), None)
        try:
            super_off, _super_end = mod.find_supers(reader)[0]
        finally:
            reader.close()
        with open(disk, "r+b") as handle:
            handle.seek(super_off + mod.LP_PRIMARY_GEOMETRY)
            handle.write(b"XXXX")
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)

        # Primary and backup slot 0 are both this TEST table. Flipping one primary byte
        # breaks that copy's checksum; the backup is still the same TEST table, so the
        # nonce is refused either way. This does not prove a clean primary is skipped.
        plant(["etc/kano-autooff.nonce"])
        with open(disk, "r+b") as handle:
            handle.seek(super_off + mod.LP_METADATA_BASE + 20)
            byte = handle.read(1)
            handle.seek(super_off + mod.LP_METADATA_BASE + 20)
            handle.write(bytes([byte[0] ^ 0xFF]))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)

        # Primary names the clean vendor at 1 MiB. Slot 0's backup names a different TEST extent at 2 MiB.
        plant(["etc/placeholder"])
        test_img = mod.build_erofs(["etc/kano-autooff.nonce"])
        nsec = (len(test_img) + 511) // 512
        test_slot = mod._metadata_blob(
            [("vendor_a", [(nsec, (2 << 20) // 512)]), ("vendor_b", [])],
            _super_end - super_off, 2048)
        test_slot += bytes(mod.LP_METADATA_MAX - len(test_slot))
        backup_at = super_off + mod._metadata_offset(mod.LP_METADATA_MAX, mod.LP_SLOTS, 0, True)
        with open(disk, "r+b") as handle:
            handle.seek(super_off + (2 << 20))
            handle.write(test_img)
            handle.seek(backup_at)
            handle.write(test_slot)
        r = self._check(disk)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)
        primary_at = super_off + mod._metadata_offset(mod.LP_METADATA_MAX, mod.LP_SLOTS, 0, False)
        with open(disk, "r+b") as handle:
            handle.seek(primary_at)
            handle.write(test_slot)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)

        with open(disk, "r+b") as handle:
            handle.seek(super_off + mod.LP_PRIMARY_GEOMETRY)
            handle.write(b"XXXX")
            handle.seek(super_off + mod.LP_BACKUP_GEOMETRY)
            handle.write(b"XXXX")
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("no valid LP geometry", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        # Dirent file type is not what the kernel uses to enter a directory.
        blob = bytearray(mod.build_erofs(["etc/kano-autooff.nonce"]))
        self.assertEqual(blob[2 * 4096 + 2 * 12 + 10], 2)
        blob[2 * 4096 + 2 * 12 + 10] = 0
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        mod.write_vendor_bytes(disk, bytes(blob), None)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)

        plant(["realetc/kano-autooff.nonce", "realetc/init/kano-autooff.rc"], links={"etc": "realetc"})
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.reason("etc/init/kano-autooff.rc", r.stderr)

        plant(["realetc/payload"], links={"etc": "etc"})
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("symlink loop", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

    def test_real_lapis_super_accepts_empty_vendor_b(self):
        """LP metadata bytes from lapis super.orig.img. vendor_b has no extents; vendor_a is a small clean EROFS."""
        mod = autooff_mod()
        fixture = REPO / "tests" / "harness" / "fixtures" / "lapis-super-lp.hex"
        meta = lapis_lp.decode(fixture)
        self.assertEqual(len(meta), 405504)
        self.assertEqual(hashlib.sha256(meta).hexdigest(), "9b41c3813c74a5e0fe914b75f843451669c2e19e9216b636bc64a48d9e839033")
        parts, _first, bsize = mod._parse_metadata(meta[mod.LP_METADATA_BASE:mod.LP_METADATA_BASE + mod.LP_METADATA_MAX])
        self.assertEqual(parts["vendor_b"], [])
        self.assertEqual(len(parts["vendor_a"]), 1)
        _nsec, _kind, target, _src = parts["vendor_a"][0]
        super_bytes = 12884901888
        self.assertEqual(bsize, super_bytes)
        disk = self.fk.tmp / "lapis-super.img"
        make_disk(disk, MiB + super_bytes + MiB, [(1, "super", 1, 1 + super_bytes // MiB)])
        with open(disk, "r+b") as handle:
            handle.seek(MiB)
            handle.write(meta)
            handle.seek(MiB + target * 512)
            handle.write(mod.build_erofs(["etc/placeholder"]))
        r = self._check(disk)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)
        with open(disk, "r+b") as handle:
            handle.seek(MiB + target * 512)
            handle.write(mod.build_erofs(["etc/kano-autooff.nonce", "etc/init/kano-autooff.rc"]))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.reason("etc/init/kano-autooff.rc", r.stderr)
        self.assertEqual(self.fk.writes(), [])

    def test_replaced_checker_is_refused_before_any_write(self):
        stub = self.fk.root / "stub-checker.py"
        stub.write_text("import sys\nprint('vendor clean')\nsys.exit(0)\n")
        self.fk.rules([{"when": "autooff_scan", "overwrite": {"stub-checker.py": "root/kano-autooff-image.py"}}])
        r = self.install()
        self.assert_nothing_destroyed(r, "autooff checker sha256 mismatch")
        self.assertFalse((self.run_dir() / "03-copy.sh").exists())

    def test_super_only_writers_do_not_copy_userdata(self):
        delta = (HARNESS / "kano-delta-push-nvme.py").read_text()
        self.assertIn('ALLOW = ("super", "vbmeta_a", "vbmeta_b")', delta)
        self.assertIn("userdata", delta)
        update = (REPO / "install" / "gbos-update.py").read_text()
        self.assertIn('NEVER = ("userdata", "metadata", "misc", "frp_persist", '
                      '"desktop_security_persist", "desktop_security_storage")', update)
        stick = (REPO / "install" / "stick" / "gbos-install.sh").read_text()
        self.assertIn('dd if=/dev/zero of="$(pdev "$T" "$UN")" bs=1M count=16', stick)
        self.assertIn('--super "$(pdev "$SRC" "$SN")" --mode nvme', stick)
        self.assertNotIn("--userdata", stick)
        usb = (HARNESS / "kano-delta-push.py").read_text()
        self.assertNotIn("nvme0n1", usb)
        # NEVER is a comment list. apply() refuses a write that overlaps guard_ranges, which includes userdata.
        self.assertIn("def guard_ranges", update)
        self.assertIn('allowed = {"super"}', update)
        rollback = update[update.index("def rollback("):update.index("\ndef main")]
        self.assertIn('sb.write("/loader/entries/gbos-nvme.conf"', rollback)
        self.assertNotIn("disk.pwrite", rollback)
        upd = (REPO / "install" / "update.sh").read_text()
        self.assertIn('bash "$ROOT/install/install.sh"', upd)
        self.assertIn('exec sudo "$ROOT/install/write-usb.sh" --keep-data', upd)

    def _bounds(self, mod, disk):
        reader = mod.FileReader(str(disk), None)
        try:
            return mod.find_supers(reader)[0]
        finally:
            reader.close()

    def _resign(self, raw):
        raw = bytearray(raw)
        hsz = struct.unpack_from("<I", raw, 8)[0]
        tsize = struct.unpack_from("<I", raw, 44)[0]
        raw[48:80] = hashlib.sha256(raw[hsz:hsz + tsize]).digest()
        raw[12:44] = bytes(32)
        raw[12:44] = hashlib.sha256(raw[:hsz]).digest()
        return bytes(raw)

    def test_invalid_primary_header_reads_the_backup(self):
        """A checksum-valid primary liblp would reject must not hide a TEST vendor in the backup."""
        mod = autooff_mod()
        disk = self.fk.tmp / "version.img"

        def setup():
            make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
            mod.write_vendor_fixture(disk, ["etc/placeholder"])
            test_img = mod.build_erofs(["etc/kano-autooff.nonce"])
            nsec = (len(test_img) + 511) // 512
            super_off, super_end = self._bounds(mod, disk)
            blob = mod._metadata_blob(
                [("vendor_a", [(nsec, (2 << 20) // 512)]), ("vendor_b", [])],
                super_end - super_off, 2048)
            blob += bytes(mod.LP_METADATA_MAX - len(blob))
            with open(disk, "r+b") as handle:
                handle.seek(super_off + (2 << 20))
                handle.write(test_img)
                handle.seek(super_off + mod._metadata_offset(mod.LP_METADATA_MAX, mod.LP_SLOTS, 0, True))
                handle.write(blob)
            return super_off

        def patch_primary(mut):
            super_off = setup()
            at = super_off + mod._metadata_offset(mod.LP_METADATA_MAX, mod.LP_SLOTS, 0, False)
            with open(disk, "r+b") as handle:
                handle.seek(at)
                raw = bytearray(handle.read(mod.LP_METADATA_MAX))
                mut(raw)
                handle.seek(at)
                handle.write(self._resign(raw))
            r = self._check(disk)
            self.assertEqual(r.returncode, 2, self.explain(r))
            self.reason("etc/kano-autooff.nonce", r.stderr)
            self.assertNotIn("vendor clean", r.stdout)

        def major_11(raw):
            struct.pack_into("<H", raw, 4, 11)

        def minor_3(raw):
            struct.pack_into("<H", raw, 6, 3)

        def minor_0_wide_header(raw):
            struct.pack_into("<H", raw, 6, 0)

        def first_sector_zero(raw):
            hsz = struct.unpack_from("<I", raw, 8)[0]
            off, _num, _esz = struct.unpack_from("<III", raw, 116)
            struct.pack_into("<Q", raw, hsz + off, 0)

        def bad_attribute(raw):
            hsz = struct.unpack_from("<I", raw, 8)[0]
            struct.pack_into("<I", raw, hsz + 36, 0x10)

        for mutate in (major_11, minor_3, minor_0_wide_header, first_sector_zero, bad_attribute):
            patch_primary(mutate)

        super_off = setup()
        with open(disk, "r+b") as handle:
            for slot in range(mod.LP_SLOTS):
                for backup in (False, True):
                    at = super_off + mod._metadata_offset(mod.LP_METADATA_MAX, mod.LP_SLOTS, slot, backup)
                    handle.seek(at + 4)
                    handle.write(struct.pack("<H", 11))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("damaged in both copies", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

    def test_other_metadata_slot_cannot_hide_a_test_vendor(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "slot1.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        mod.write_vendor_fixture(disk, ["etc/placeholder"])
        test_img = mod.build_erofs(["etc/kano-autooff.nonce"])
        nsec = (len(test_img) + 511) // 512
        super_off, super_end = self._bounds(mod, disk)
        blob = mod._metadata_blob(
            [("vendor_a", [(nsec, (2 << 20) // 512)]), ("vendor_b", [])],
            super_end - super_off, 2048)
        blob += bytes(mod.LP_METADATA_MAX - len(blob))
        with open(disk, "r+b") as handle:
            handle.seek(super_off + (2 << 20))
            handle.write(test_img)
            for backup in (False, True):
                handle.seek(super_off + mod._metadata_offset(mod.LP_METADATA_MAX, mod.LP_SLOTS, 1, backup))
                handle.write(blob)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertEqual(self.fk.writes(), [])

    def test_struct_size_geometry_cannot_select_the_wrong_backup(self):
        """Primary geometry struct_size 56, hashed the way liblp hashes the first 52 bytes, must not win.

        The backup geometry is metadata_max_size 32768. Its slot-0 backup is a TEST vendor. A parser that
        kept the 56-byte geometry would read the clean table stored at the 65536-byte backup offset.
        """
        mod = autooff_mod()
        disk = self.fk.tmp / "geo56.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        super_off, super_end = self._bounds(mod, disk)
        super_size = super_end - super_off
        clean = mod.build_erofs(["etc/placeholder"])
        test = mod.build_erofs(["etc/kano-autooff.nonce"])
        clean_nsec = (len(clean) + 511) // 512
        test_nsec = (len(test) + 511) // 512
        clean_meta = mod._metadata_blob(
            [("vendor_a", [(clean_nsec, (1 << 20) // 512)]), ("vendor_b", [])], super_size, 2048)
        test_meta = mod._metadata_blob(
            [("vendor_a", [(test_nsec, (2 << 20) // 512)]), ("vendor_b", [])], super_size, 2048)
        mx = 32768
        with open(disk, "r+b") as handle:
            handle.seek(super_off + mod.LP_PRIMARY_GEOMETRY)
            loose = bytearray(mod.LP_GEOMETRY_SIZE)
            struct.pack_into("<II", loose, 0, mod.GEO_MAGIC, 56)
            struct.pack_into("<III", loose, 40, mod.LP_METADATA_MAX, mod.LP_SLOTS, 4096)
            # Checksum covers the 52 bytes liblp hashes. struct_size stays 56, so only that
            # field check can reject this primary. Hashing 56 bytes would fail the checksum first.
            body = bytearray(loose[:52])
            body[8:40] = bytes(32)
            loose[8:40] = hashlib.sha256(body).digest()
            handle.write(bytes(loose))
            handle.seek(super_off + mod.LP_BACKUP_GEOMETRY)
            handle.write(mod._geometry_slot(mx, mod.LP_SLOTS, 4096))
            handle.seek(super_off + (1 << 20))
            handle.write(clean)
            handle.seek(super_off + (2 << 20))
            handle.write(test)

            def put(slot, backup, meta, width):
                handle.seek(super_off + mod._metadata_offset(width, mod.LP_SLOTS, slot, backup))
                handle.write(meta + bytes(width - len(meta)))

            put(0, False, b"\x00" * 64, mx)
            put(0, True, test_meta, mx)
            for slot in (1, 2):
                for backup in (False, True):
                    put(slot, backup, clean_meta, mx)
            put(0, True, clean_meta, mod.LP_METADATA_MAX)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

    def test_erofs_48bit_metabox_and_unknown_features(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "erofs.img"

        def plant(image):
            make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
            mod.write_vendor_bytes(disk, image)

        def relocate(blob, new_nid=40, decoy=7):
            image = bytearray(blob)
            image[4096 + new_nid * 32:4096 + new_nid * 32 + 32] = image[4096:4096 + 32]
            struct.pack_into("<H", image, 1024 + 14, decoy)
            struct.pack_into("<Q", image, 1024 + 112, new_nid)
            incompat = struct.unpack_from("<I", image, 1024 + 80)[0] | mod.EROFS_FEATURE_INCOMPAT_48BIT
            struct.pack_into("<I", image, 1024 + 80, incompat)
            return bytes(image)

        plant(relocate(mod.build_erofs(["etc/placeholder"])))
        r = self._check(disk)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)

        plant(relocate(mod.build_erofs(["etc/kano-autooff.nonce"])))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)

        image = bytearray(mod.build_erofs(["etc/kano-autooff.nonce"]))
        struct.pack_into("<I", image, 1024 + 80, mod.EROFS_FEATURE_INCOMPAT_48BIT)
        plant(bytes(image))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)

        image = bytearray(mod.build_erofs(["etc/placeholder"]))
        struct.pack_into("<I", image, 1024 + 80, 0x2)
        plant(bytes(image))
        r = self._check(disk)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)

        for bit in (0x200, 0x400):
            image = bytearray(mod.build_erofs(["etc/placeholder"]))
            struct.pack_into("<I", image, 1024 + 80, bit)
            plant(bytes(image))
            r = self._check(disk)
            self.assertEqual(r.returncode, 2, self.explain(r))
            self.reason("unidentified incompatible feature", r.stderr)
            self.reason(hex(bit), r.stderr)
            self.assertNotIn("vendor clean", r.stdout)

        def metabox(paths):
            blob = bytearray(mod.build_erofs(paths))
            base = 2 * 4096
            nid, nameoff, ftype = struct.unpack_from("<QHB", blob, base + 24)
            self.assertEqual((nid, ftype), (1, 2))
            self.assertTrue(bytes(blob[base + nameoff:base + nameoff + 3]).startswith(b"etc"))
            struct.pack_into("<Q", blob, base + 24, (1 << 63) | 0)
            etc_inode = bytes(blob[4096 + 32:4096 + 64])
            box = len(blob) // 4096
            blob.extend(bytes(4096))
            blob[box * 4096:box * 4096 + 32] = etc_inode
            off = 4096 + 3 * 32
            struct.pack_into("<HHHH", blob, off, 0, 0, 0o100644, 1)
            struct.pack_into("<I", blob, off + 8, 32)
            struct.pack_into("<I", blob, off + 16, box)
            blob[1024 + 13] = 1
            struct.pack_into("<I", blob, 1024 + 80, mod.EROFS_FEATURE_INCOMPAT_METABOX)
            struct.pack_into("<Q", blob, 1024 + 128, 3)
            return bytes(blob)

        plant(metabox(["etc/placeholder"]))
        r = self._check(disk)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)
        plant(metabox(["etc/kano-autooff.nonce"]))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)

        image = bytearray(metabox(["etc/placeholder"]))
        struct.pack_into("<Q", image, 1024 + 128, (1 << 63) | 3)
        plant(bytes(image))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("metabox nid loops", r.stderr)

        image = bytearray(metabox(["etc/placeholder"]))
        struct.pack_into("<H", image, 4096 + 3 * 32, 2)  # datalayout 1, not flat
        plant(bytes(image))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("metabox inode layout is not supported", r.stderr)

        image = bytearray(mod.build_erofs(["etc/placeholder"]))
        image[1024 + 90] = 1
        plant(bytes(image))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("dirblkbits", r.stderr)

        image = bytearray(mod.build_erofs(["etc/placeholder"]))
        struct.pack_into("<H", image, 1024 + 86, 1)
        plant(bytes(image))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("extra devices", r.stderr)

    def test_symlink_dotdot_and_duplicate_dirents(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "link.img"

        def plant(paths, links=None):
            make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
            mod.write_vendor_fixture(disk, paths, links=links)

        plant(["realetc/kano-autooff.nonce"], links={"etc": "../realetc"})
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("vendor symlink leaves the image", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        plant(["realetc/kano-autooff.nonce"], links={"etc": "/vendor/../realetc"})
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("vendor symlink leaves the image", r.stderr)

        plant(["a/decoy/placeholder", "a/real/kano-autooff.nonce"], links={"etc": "a/decoy/../real"})
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)

        blob = bytearray(mod.build_erofs(["etc/placeholder"]))
        blob[2 * 4096:(3 * 4096)] = mod._dir_block([(".", 0, 2), (".", 0, 2)])
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        mod.write_vendor_bytes(disk, bytes(blob))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("duplicate name", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

    def test_primary_gpt_crc_failure_uses_the_backup(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "gpt.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        mod.write_vendor_fixture(disk, ["etc/placeholder"])
        # Partition 2 is super. Flip one byte of its primary name so the entry CRC no longer matches.
        with open(disk, "r+b") as handle:
            handle.seek(1024 + 128 + 56)
            handle.write(b"X")
        r = self._check(disk)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)
        backup_entries = IMAGE_SIZE - (128 * 128 + 512)
        with open(disk, "r+b") as handle:
            handle.seek(backup_entries + 128 + 56)
            handle.write(b"X")
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("no GPT", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

    def test_initrd_keeps_slot_suffix_b(self):
        text = (HARNESS / "kano-nvme-install.sh").read_text()
        start = text.index("initrd_nvme() {")
        end = text.index("\n}\n\nlog ", start)
        script = self.fk.tmp / "initrd-fn.sh"
        script.write_text(text[start:end + 2] + "\n")
        src, dst = self.fk.tmp / "in.img", self.fk.tmp / "out.img"
        bc = b"androidboot.slot_suffix=_b\nandroidboot.boot_part_uuid=old\nandroidboot.vbmeta.digest=aa\n"
        src.write_bytes(b"ARCH" + bc + struct.pack("<II", len(bc), sum(bc) & 0xFFFFFFFF) + b"#BOOTCONFIG\n")
        r = self.fk.run(["bash", "-c", 'source "$1"; initrd_nvme "$2" "$3" "$4"',
                         "bash", str(script), str(src), str(dst), BOOT_A_GUID], timeout=30)
        self.assertEqual(r.returncode, 0, self.explain(r))
        out = dst.read_bytes()
        self.assertIn(b"androidboot.slot_suffix=_b\n", out)
        self.assertIn(b"androidboot.boot_part_uuid=" + BOOT_A_GUID.encode(), out)
        self.assertNotIn(b"androidboot.boot_part_uuid=old", out)

    def test_stick_userdata_nonce_refuses_before_discard(self):
        nonce = self.fk.root / "mnt" / "srcud" / "kano-autooff.nonce"
        nonce.parent.mkdir(parents=True)
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        r = self.install()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("stick userdata has kano-autooff.nonce", r.stdout + r.stderr)
        self.assertEqual(self.fk.writes(), [])
        self.assertEqual((self.fk.root / "dev" / "nvme0n1").read_bytes(), self.nvme_before)
        self.assertIn("blkdiscard", (self.run_dir() / "03-copy.sh").read_text())

    def test_scan_refuses_when_the_checker_does_not_print_vendor_clean(self):
        text = (HARNESS / "kano-nvme-install.sh").read_text()
        start = text.index("autooff_scan() {")
        end = text.index("\n}\nEOF", start)
        checker = self.fk.tmp / "checker.py"
        shutil.copy(HARNESS / "kano-autooff-image.py", checker)
        fn = text[start:end + 2].replace("/root/kano-autooff-image.py", shlex.quote(str(checker)))
        bindir = self.fk.tmp / "stubbin"
        bindir.mkdir()
        stub = bindir / "python3"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        digest = hashlib.sha256(checker.read_bytes()).hexdigest()
        script = self.fk.tmp / "scan.sh"
        script.write_text(
            "set -euo pipefail\n"
            "refuse() { echo \"REFUSE: $*\" >&2; exit 1; }\n"
            f"AUTOOFF_SHA={digest}\nBYTES=1\n{fn}\nautooff_scan\n")
        r = self.fk.run(["bash", str(script)], timeout=30, PATH=f"{bindir}:{os.environ['PATH']}")
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("autooff scan did not report vendor clean", r.stderr)

    def test_resize_failure_inside_a_pipeline_stops_the_install(self):
        self.fk.faults({"fail": ["resize.f2fs"]})
        r = self.install()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("05-gpt: remote exit status", r.stdout)
        self.assertFalse([c for c in self.fk.lines("calls.log") if re.match(r"mount /dev/nvme0n1p12", c)])

    def _copied_umounts(self):
        hits = []
        for line in self.fk.lines("calls.log"):
            parts = line.split()
            if parts and parts[0] == "umount" and any(part.rstrip("/") == "/mnt/ud" for part in parts[1:]):
                hits.append(line)
        return hits

    def _no_copied_umount(self):
        hits = self._copied_umounts()
        self.assertEqual(hits, [], hits)

    def _stopped_before_resize(self, r, why):
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason(why, r.stdout + r.stderr, self.explain(r))
        calls = self.fk.lines("calls.log")
        self.assertFalse([c for c in calls if c.startswith("resize.f2fs")], calls)
        self.assertFalse([c for c in calls if c.startswith("fsck.f2fs")], calls)
        self.assertFalse([c for c in calls if re.match(r"mount /dev/nvme0n1p12", c)], calls)

    def _pair(self, mod):
        clean, clean_nsec = sector_pad(mod.build_erofs(["etc/placeholder"]))
        test, test_nsec = sector_pad(mod.build_erofs(["etc/kano-autooff.nonce"]))
        return clean, clean_nsec, test, test_nsec

    def test_entry_cap_does_not_skip_a_valid_primary(self):
        """A checksum-valid primary with more than 4096 entries refuses the image.

        The same layout with 3 groups keeps that primary. A checksum-bad 4097-entry
        primary still falls through to the backup.
        """
        mod = autooff_mod()
        disk = self.fk.tmp / "cap.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        clean, clean_nsec, test, test_nsec = self._pair(mod)
        _off, super_end = gpt_bounds(mod, disk)
        size = super_end - _off
        mx, slots, first = 262144, 3, 4096
        groups = [(f"g{i:04d}", 0, 0) for i in range(4097)]
        wide = mod._metadata_blob(
            [("vendor_a", [(test_nsec, (3 << 20) // 512)], 1), ("vendor_b", [])],
            size, first, groups=groups, limit=mx)
        test_blob = mod._metadata_blob(
            [("vendor_a", [(test_nsec, (3 << 20) // 512)], 1), ("vendor_b", [])],
            size, first, limit=mx)
        clean_blob = mod._metadata_blob(
            [("vendor_a", [(clean_nsec, (2 << 20) // 512)], 1), ("vendor_b", [])],
            size, first, limit=mx)
        images = [((2 << 20), clean), ((3 << 20), test)]
        plant_blobs(disk, mod, every_slot(slots, wide, clean_blob), images, mx, slots)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("more entries than this checker reads", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)
        self.assertNotIn("etc/kano-autooff.nonce", r.stderr)

        plant_blobs(disk, mod, every_slot(slots, test_blob, clean_blob), images, mx, slots)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("more entries", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        bad = bytearray(wide)
        bad[12] ^= 0xFF
        plant_blobs(disk, mod, every_slot(slots, bytes(bad), test_blob), images, mx, slots)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("more entries", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        stick = self.fk.root / "dev" / "sda"
        soff, send = gpt_bounds(mod, stick)
        self.assertEqual(send - soff, size)
        plant_blobs(stick, mod, every_slot(slots, wide, clean_blob), images, mx, slots)
        r = self.install()
        self.assert_nothing_destroyed(r, "more entries than this checker reads")
        self.assertNotIn("vendor clean", r.stdout + r.stderr)

    def test_tables_cap_does_not_skip_a_valid_primary(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "tsize.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        clean, clean_nsec, test, test_nsec = self._pair(mod)
        _off, super_end = gpt_bounds(mod, disk)
        size = super_end - _off
        mx, slots, first = 2 << 20, 1, 9216
        clean_rel, test_rel = first * 512, 10752 * 512
        primary = pad_lp_tables(mod._metadata_blob(
            [("vendor_a", [(clean_nsec, first)], 1), ("vendor_b", [])],
            size, first, limit=mx), (1 << 20) + 4096)
        backup = mod._metadata_blob(
            [("vendor_a", [(test_nsec, test_rel // 512)], 1), ("vendor_b", [])],
            size, first, limit=mx)
        plant_blobs(disk, mod, every_slot(slots, primary, backup),
                    [(clean_rel, clean), (test_rel, test)], mx, slots)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("LP tables exceed the checker limit", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)
        self.assertNotIn("etc/kano-autooff.nonce", r.stderr)

    def test_slot_suffix_names_the_vendor_android_mounts(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "suffix.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        clean, clean_nsec, test, test_nsec = self._pair(mod)
        _off, super_end = gpt_bounds(mod, disk)
        size = super_end - _off
        mx, first = mod.LP_METADATA_MAX, 2048
        images = [((1 << 20), clean), ((2 << 20), test)]
        at_clean = (clean_nsec, (1 << 20) // 512)
        at_test = (test_nsec, (2 << 20) // 512)

        def check(specs, slots, backup_specs=None):
            primary = mod._metadata_blob(specs, size, first, limit=mx)
            backup = primary if backup_specs is None else mod._metadata_blob(backup_specs, size, first, limit=mx)
            plant_blobs(disk, mod, every_slot(slots, primary, backup), images, mx, slots)
            return self._check(disk)

        r = check([
            ("vendor", [at_test], 3),
            ("vendor_a", [at_clean], 3),
        ], 2)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        r = check([
            ("vendor", [at_test], 3),
            ("vendor_a", [at_clean], 1),
            ("vendor_b", [], 1),
        ], 2)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        r = check([
            ("vendor_a", [at_test], 1),
            ("vendor_a_a", [at_clean], 1),
        ], 2)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        r = check([("vendor", [at_clean], 3)], 2)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)

        r = check(
            [("v" * 35, [at_test], 3)], 2,
            [("vendor_a", [at_clean], 1), ("vendor_b", [])])
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("LP partition name too long", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        primary = mod._metadata_blob([
            ("vendor", [at_test], 3),
            ("vendor_a", [at_clean], 1),
        ], size, first, limit=mx)
        backup = mod._metadata_blob([
            ("vendor_a", [at_clean], 1),
            ("vendor_b", []),
        ], size, first, limit=mx)
        plain = mod._metadata_blob([
            ("vendor_a", [at_clean], 1),
            ("vendor_b", []),
        ], size, first, limit=mx)
        blobs = every_slot(3, plain)
        blobs[(2, False)] = primary
        blobs[(2, True)] = backup
        plant_blobs(disk, mod, blobs, images, mx, 3)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("slot 2 has slot-suffixed names", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        stick = self.fk.root / "dev" / "sda"
        soff, send = gpt_bounds(mod, stick)
        self.assertEqual(send - soff, size)
        hide = mod._metadata_blob([
            ("vendor", [at_test], 3),
            ("vendor_a", [at_clean], 1),
            ("vendor_b", [], 1),
        ], size, first, limit=mx)
        plant_blobs(stick, mod, every_slot(2, hide), images, mx, 2)
        r = self.install()
        self.assert_nothing_destroyed(r, "etc/kano-autooff.nonce")
        self.assertNotIn("vendor clean", r.stdout + r.stderr)

    def test_disabled_vendor_is_skipped_and_updated_is_not_a_refusal(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "disabled.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        clean, clean_nsec, test, test_nsec = self._pair(mod)
        _off, super_end = gpt_bounds(mod, disk)
        size = super_end - _off
        mx, first = mod.LP_METADATA_MAX, 2048
        images = [((1 << 20), clean), ((2 << 20), test)]

        def plant(specs):
            blob = mod._metadata_blob(specs, size, first, limit=mx)
            plant_blobs(disk, mod, every_slot(2, blob), images, mx, 2)

        plant([
            ("vendor_a", [(test_nsec, (2 << 20) // 512)], 1 | mod.LP_PARTITION_ATTR_DISABLED),
            ("vendor_a", [(clean_nsec, (1 << 20) // 512)], 1),
        ])
        r = self._check(disk)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)

        plant([("vendor_a", [(test_nsec, (2 << 20) // 512)], 1 | mod.LP_PARTITION_ATTR_DISABLED)])
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.assertNotIn("vendor clean", r.stdout)

        plant([("vendor_a", [(clean_nsec, (1 << 20) // 512)], 1 | mod.LP_PARTITION_ATTR_UPDATED)])
        r = self._check(disk)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)

    def test_lp_checksum_failure_reads_the_backup(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "csum.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        clean, clean_nsec, test, test_nsec = self._pair(mod)
        _off, super_end = gpt_bounds(mod, disk)
        size = super_end - _off
        mx, first = mod.LP_METADATA_MAX, 2048
        clean_blob = mod._metadata_blob(
            [("vendor_a", [(clean_nsec, (1 << 20) // 512)], 1), ("vendor_b", [])], size, first)
        test_blob = mod._metadata_blob(
            [("vendor_a", [(test_nsec, (2 << 20) // 512)], 1), ("vendor_b", [])], size, first)
        images = [((1 << 20), clean), ((2 << 20), test)]

        def plant(primary):
            blobs = every_slot(mod.LP_SLOTS, clean_blob)
            blobs[(0, False)] = primary
            blobs[(0, True)] = test_blob
            plant_blobs(disk, mod, blobs, images, mx, mod.LP_SLOTS)

        header_bad = bytearray(clean_blob)
        header_bad[12] ^= 0xFF
        plant(bytes(header_bad))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        tables_bad = bytearray(clean_blob)
        tables_bad[48] ^= 0xFF
        hsz = struct.unpack_from("<I", tables_bad, 8)[0]
        tables_bad[12:44] = bytes(32)
        tables_bad[12:44] = hashlib.sha256(tables_bad[:hsz]).digest()
        plant(bytes(tables_bad))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

    def test_lp_block_device_size_does_not_select_the_backup(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "bsize.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        clean, clean_nsec, test, test_nsec = self._pair(mod)
        super_off, super_end = gpt_bounds(mod, disk)
        size = super_end - super_off
        mx, first = mod.LP_METADATA_MAX, 2048
        clean_blob = bytearray(mod._metadata_blob(
            [("vendor_a", [(clean_nsec, (1 << 20) // 512)], 1), ("vendor_b", [])], size, first))
        hsz = struct.unpack_from("<I", clean_blob, 8)[0]
        desc_off, _num, _esz = struct.unpack_from("<III", clean_blob, 116)
        struct.pack_into("<Q", clean_blob, hsz + desc_off + 16, size + 4096)
        clean_blob = self._resign(clean_blob)
        test_blob = mod._metadata_blob(
            [("vendor_a", [(test_nsec, (2 << 20) // 512)], 1), ("vendor_b", [])], size, first)
        blobs = every_slot(mod.LP_SLOTS, test_blob)
        blobs[(0, False)] = clean_blob
        plant_blobs(disk, mod, blobs, [((1 << 20), clean), ((2 << 20), test)], mx, mod.LP_SLOTS)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("LP block device size does not match super", r.stderr)
        self.assertNotIn("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

    def test_gpt_kernel_name_my_lba_and_entry_size(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "gptname.img"

        def fresh():
            make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
            mod.write_vendor_fixture(disk, ["etc/placeholder"])
            return bytearray(disk.read_bytes())

        data = fresh()
        struct.pack_into("<H", data, 1024 + 128 + 56, 0x00F3)
        resign_primary_gpt(data)
        kill_backup_gpt(data)
        disk.write_bytes(data)
        r = self._check(disk)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("vendor clean", r.stdout)

        data = fresh()
        data[1024 + 4 * 128:1024 + 5 * 128] = data[1024 + 128:1024 + 256]
        resign_primary_gpt(data)
        disk.write_bytes(data)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("more than one GPT partition is named super", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        data = fresh()
        struct.pack_into("<Q", data, 512 + 24, 99)
        resign_primary_gpt(data)
        kill_backup_gpt(data)
        disk.write_bytes(data)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("no GPT", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

        data = fresh()
        entry = bytearray(256)
        entry[:32] = data[1024 + 128:1024 + 160]
        first, last = struct.unpack_from("<QQ", data, 1024 + 128 + 32)
        struct.pack_into("<QQ", entry, 32, first, last)
        name = "super".encode("utf-16le")
        entry[56:56 + len(name)] = name
        data[1024:1024 + 256] = entry
        struct.pack_into("<II", data, 512 + 80, 1, 256)
        struct.pack_into("<I", data, 512 + 88, zlib.crc32(bytes(entry)) & 0xFFFFFFFF)
        resign_header(data, 512)
        kill_backup_gpt(data)
        disk.write_bytes(data)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("no GPT", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

    def test_gpt_header_crc_selects_the_backup_super(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "gpthdr.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        mod.write_vendor_fixture(disk, ["etc/placeholder"])
        small = self.fk.tmp / "gptsmall.img"
        make_disk(small, 8 * MiB, [(1, "super", 1, 4)])
        mod.write_vendor_fixture(small, ["etc/kano-autooff.nonce"])
        data = bytearray(disk.read_bytes())
        data[12 * MiB:15 * MiB] = small.read_bytes()[MiB:4 * MiB]
        ent = len(data) - (128 * 128 + 512) + 128
        struct.pack_into("<QQ", data, ent + 32, (12 * MiB) // 512, (15 * MiB) // 512 - 1)
        resign_backup_gpt(data)
        data[512 + 16] ^= 0xFF
        disk.write_bytes(data)
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("etc/kano-autooff.nonce", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

    def test_erofs_format_bit_outside_the_mask_is_refused(self):
        mod = autooff_mod()
        disk = self.fk.tmp / "fmt.img"
        image = bytearray(mod.build_erofs(["etc/placeholder"]))
        struct.pack_into("<H", image, 4096, 0x20)
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        mod.write_vendor_bytes(disk, bytes(image))
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.reason("unsupported format", r.stderr)
        self.assertNotIn("vendor clean", r.stdout)

    def test_copied_nonce_symlink_is_removed(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        link = ud / "kano-autooff.nonce"
        link.symlink_to("nowhere")
        r = self.install()
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertFalse(link.exists())
        self.assertFalse(link.is_symlink())
        self.assertTrue(any(c.startswith("rm ") and "kano-autooff.nonce" in c for c in self.fk.lines("calls.log")))

    def test_copied_nonce_directory_is_removed(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        directory = ud / "kano-autooff.nonce"
        directory.mkdir()
        (directory / "child").write_text("x")
        r = self.install()
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertFalse(directory.exists())

    def test_copied_nonce_delete_failure_stops(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        nonce = ud / "kano-autooff.nonce"
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        self.fk.faults({"rm_exit": 1})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertTrue(nonce.is_file())
        self._no_copied_umount()

    def test_copied_userdata_mount_failure_stops(self):
        self.fk.faults({"fail_mount": ["f2fs"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot mount copied userdata")

    def test_copied_userdata_unreadable_root_stops(self):
        mp = self.fk.root / "mnt" / "ud"
        self.addCleanup(lambda: os.chmod(mp, 0o755) if mp.is_dir() else None)
        self.fk.faults({"unreadable_mount": ["f2fs"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot read copied userdata")

    def test_stick_partlabel_must_be_userdata_before_the_ro_mount(self):
        self.fk.set("disks/sda1/PARTLABEL", "NOTUSERDATA")
        r = self.install()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("sda1 is not userdata", r.stdout + r.stderr)
        self.assertEqual(self.fk.writes(), [])
        self.assertEqual((self.fk.root / "dev" / "nvme0n1").read_bytes(), self.nvme_before)
        self.assertIn("blkdiscard", (self.run_dir() / "03-copy.sh").read_text())

    def test_stick_userdata_nonce_directory_refuses_before_discard(self):
        directory = self.fk.root / "mnt" / "srcud" / "kano-autooff.nonce"
        directory.mkdir(parents=True)
        (directory / "child").write_text("x")
        r = self.install()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("stick userdata has kano-autooff.nonce", r.stdout + r.stderr)
        self.assertEqual(self.fk.writes(), [])
        self.assertEqual((self.fk.root / "dev" / "nvme0n1").read_bytes(), self.nvme_before)

    def test_stick_userdata_nonce_symlink_refuses_before_discard(self):
        nonce = self.fk.root / "mnt" / "srcud" / "kano-autooff.nonce"
        nonce.parent.mkdir(parents=True)
        nonce.symlink_to("missing")
        r = self.install()
        self.assertNotEqual(r.returncode, 0, self.explain(r))
        self.reason("stick userdata has kano-autooff.nonce", r.stdout + r.stderr)
        self.assertEqual(self.fk.writes(), [])
        self.assertTrue(nonce.is_symlink())
        self._no_resize_fsck_esp()

    def test_stick_nonce_lookup_eio_stops_before_discard(self):
        nonce = self.fk.root / "mnt" / "srcud" / "kano-autooff.nonce"
        nonce.parent.mkdir(parents=True)
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        self.fk.faults({"eio_lstat": ["/mnt/srcud/kano-autooff.nonce"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot stat stick userdata")
        self.assertEqual(self.fk.writes(), [])
        self.assertEqual((self.fk.root / "dev" / "nvme0n1").read_bytes(), self.nvme_before)
        self.assertTrue(nonce.is_file())

    def test_stick_root_list_eio_stops_before_discard(self):
        src = self.fk.root / "mnt" / "srcud"
        src.mkdir(parents=True)
        self.fk.faults({"eio_listdir": ["/mnt/srcud"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot read stick userdata")
        self.assertEqual(self.fk.writes(), [])
        self.assertEqual((self.fk.root / "dev" / "nvme0n1").read_bytes(), self.nvme_before)

    def _no_resize_fsck_esp(self):
        calls = self.fk.lines("calls.log")
        self.assertFalse([c for c in calls if c.startswith("resize.f2fs")], calls)
        self.assertFalse([c for c in calls if c.startswith("fsck.f2fs")], calls)
        self.assertFalse([c for c in calls if re.match(r"mount /dev/nvme0n1p12", c)], calls)

    def test_copied_nonce_symlink_to_a_directory_is_not_followed(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        target = ud / "real-dir"
        target.mkdir()
        (target / "keep").write_text("safe")
        link = ud / "kano-autooff.nonce"
        link.symlink_to("real-dir")
        r = self.install()
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertFalse(link.is_symlink())
        self.assertTrue((target / "keep").is_file())
        calls = self.fk.lines("calls.log")
        self.assertTrue(any(c.startswith("rm -f ") and c.endswith("kano-autooff.nonce") for c in calls), calls)
        self.assertFalse(any(c.startswith("rmdir ") and c.endswith("kano-autooff.nonce") for c in calls), calls)
        self.assertFalse(any(c.startswith("rm -rf ") for c in calls), calls)

    def test_copied_nonce_directory_does_not_follow_a_symlink(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        outside = self.fk.root / "mnt" / "outside-keep"
        outside.mkdir()
        (outside / "keep").write_text("safe")
        directory = ud / "kano-autooff.nonce"
        directory.mkdir()
        (directory / "child").write_text("x")
        (directory / "link").symlink_to(outside)
        r = self.install()
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertFalse(directory.exists())
        self.assertTrue((outside / "keep").is_file())
        calls = self.fk.lines("calls.log")
        self.assertFalse(any(c.startswith("rm -rf ") or c.startswith("rm -r ") for c in calls), calls)
        self.assertFalse(any((c.startswith("rm ") or c.startswith("rmdir ")) and "outside-keep" in c for c in calls),
                         calls)

    def test_copied_nonce_lookup_eio_stops(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        nonce = ud / "kano-autooff.nonce"
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        self.fk.faults({"eio_lstat": ["/mnt/ud/kano-autooff.nonce"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertTrue(nonce.is_file())
        calls = self.fk.lines("calls.log")
        self.assertFalse([c for c in calls if c.startswith("rm ") and "kano-autooff.nonce" in c], calls)
        self._no_copied_umount()

    def test_copied_userdata_root_list_eio_stops(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        nonce = ud / "kano-autooff.nonce"
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        self.fk.faults({"eio_listdir": ["/mnt/ud"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot read copied userdata")
        self.assertTrue(nonce.is_file())
        calls = self.fk.lines("calls.log")
        self.assertFalse([c for c in calls if c.startswith("rm ") and "kano-autooff.nonce" in c], calls)

    def test_copied_nonce_rm_eio_stops(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        nonce = ud / "kano-autooff.nonce"
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        self.fk.faults({"rm_eio": True})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertTrue(nonce.is_file())
        self._no_copied_umount()

    def test_copied_nonce_confirm_lstat_eio_stops(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        nonce = ud / "kano-autooff.nonce"
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        self.fk.faults({"eio_lstat_nth": {"/mnt/ud/kano-autooff.nonce": 2}})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        calls = self.fk.lines("calls.log")
        self.assertTrue(any(c.startswith("rm -f ") and c.endswith("kano-autooff.nonce") for c in calls), calls)
        self._no_copied_umount()

    def test_copied_nonce_still_present_when_rm_returns_0_stops(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        nonce = ud / "kano-autooff.nonce"
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        self.fk.faults({"rm_keep": True})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertTrue(nonce.is_file())
        self._no_copied_umount()

    def _nonce_dir(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        directory = ud / "kano-autooff.nonce"
        directory.mkdir()
        return ud, directory

    def _plant(self, directory, name, data=b"x"):
        path = os.path.join(os.fspath(directory), name)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def test_copied_nonce_directory_odd_names_do_not_escape(self):
        """Names with LF, TAB, a leading dash, and dot-dot fragments stay inside the nonce directory.

        The sandboxed tree is the fake KANO root. A sibling of the nonce directory, and the cwd file
        and empty directory named by an LF fragment, must still exist afterwards.
        """
        ud, directory = self._nonce_dir()
        nested = directory / "nested"
        nested.mkdir()
        self._plant(nested, "inner", b"n")
        self._plant(directory, "tab\tname", b"t")
        self._plant(directory, "-leading", b"d")
        self._plant(directory, "...", b"p")
        self._plant(directory, "..x", b"p")
        # LF is legal; "/" is not. The old text protocol splits x<LF>f<TAB>victim into rm -f victim
        # in the remote cwd, and y<LF>d<TAB>emptydir into rmdir emptydir there.
        self._plant(directory, "x\nf\tvictim", b"lf")
        self._plant(nested, "y\nd\temptydir", b"dot")
        cwd = self.fk.root / "root"
        cwd_victim = cwd / "victim"
        cwd_victim.write_text("do-not-delete")
        empty = cwd / "emptydir"
        empty.mkdir()
        sibling = ud / "victim"
        sibling.write_text("sibling")
        dash = cwd / "-leading"
        dash.write_text("dash")
        outside = self.fk.root / "mnt" / "outside-keep"
        outside.mkdir()
        (outside / "keep").write_text("safe")
        (directory / "link").symlink_to(outside)
        r = self.install()
        # Outside-tree sentinels are asserted before the exit status, so a mutant that
        # deletes them is red even when a later status check would also fail.
        self.assertTrue(cwd_victim.is_file())
        self.assertEqual(cwd_victim.read_text(), "do-not-delete")
        self.assertTrue(empty.is_dir())
        self.assertTrue(sibling.is_file())
        self.assertEqual(sibling.read_text(), "sibling")
        self.assertTrue(dash.is_file())
        self.assertEqual(dash.read_text(), "dash")
        self.assertEqual((outside / "keep").read_text(), "safe")
        calls = self.fk.lines("calls.log")
        self.assertFalse(any(c.startswith("rm -rf ") or c.startswith("rm -r ") for c in calls), calls)
        self.assertFalse(any(c.startswith("rm ") and c.split()[-1] in ("victim", "../victim", "-leading")
                             for c in calls), calls)
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertFalse(directory.exists())

    def _reader_calls(self, mod, disk):
        reads = []
        original = mod.FileReader.read

        def wrapped(reader, offset, length):
            reads.append((offset, length))
            return original(reader, offset, length)

        mod.FileReader.read = wrapped
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                mod.main(["--file", str(disk)])
        finally:
            mod.FileReader.read = original
        return reads

    def test_lp_name_with_interior_nul_refuses_without_reading_the_backup(self):
        """Interior NUL is a checker limit: exit 2, the other metadata copy is not read, nothing is written.

        Device-mapper would cut the 36-byte name to vendor_a and mount the TEST extent.
        The same primary also has a clean vendor_b with extents. Without the limit that
        vendor_b is scanned and the image is accepted. Slot 0's backup names the TEST
        nonce, so falling through to it would report that path.
        """
        mod = autooff_mod()
        disk = self.fk.tmp / "nulname.img"
        make_disk(disk, IMAGE_SIZE, IMAGE_PARTS)
        clean, clean_nsec, test, test_nsec = self._pair(mod)
        super_off, super_end = gpt_bounds(mod, disk)
        size = super_end - super_off
        mx, first = mod.LP_METADATA_MAX, 2048
        primary = bytearray(mod._metadata_blob(
            [("vendor_a", [(test_nsec, (2 << 20) // 512)], 1),
             ("vendor_b", [(clean_nsec, (1 << 20) // 512)], 1)],
            size, first))
        hsz = struct.unpack_from("<I", primary, 8)[0]
        self.assertTrue(bytes(primary[hsz:hsz + 8]).startswith(b"vendor_a"), bytes(primary[hsz:hsz + 36]))
        primary[hsz + 9] = ord("X")
        primary[hsz + 35] = ord("Z")
        primary = self._resign(primary)
        clean_slot = mod._metadata_blob(
            [("vendor_a", [(clean_nsec, (1 << 20) // 512)], 1), ("vendor_b", [])],
            size, first)
        test_backup = mod._metadata_blob(
            [("vendor_a", [(test_nsec, (2 << 20) // 512)], 1), ("vendor_b", [])],
            size, first)
        blobs = every_slot(mod.LP_SLOTS, clean_slot)
        blobs[(0, False)] = primary
        blobs[(0, True)] = test_backup
        plant_blobs(disk, mod, blobs, [((1 << 20), clean), ((2 << 20), test)], mx, mod.LP_SLOTS)
        before = disk.read_bytes()
        r = self._check(disk)
        self.assertEqual(r.returncode, 2, self.explain(r))
        self.assertNotIn("vendor clean", r.stdout)
        self.assertNotIn("etc/kano-autooff.nonce", r.stderr)
        self.assertEqual(disk.read_bytes(), before)
        reads = self._reader_calls(mod, disk)
        primary_at = super_off + mod._metadata_offset(mx, mod.LP_SLOTS, 0, False)
        backup_at = super_off + mod._metadata_offset(mx, mod.LP_SLOTS, 0, True)

        def hits(start):
            return any(off < start + mx and off + length > start for off, length in reads)

        self.assertTrue(hits(primary_at), reads)
        self.assertFalse(hits(backup_at), reads)
        self.reason("interior NUL", r.stderr)

    def test_copied_nonce_directory_read_eio_stops(self):
        _ud, directory = self._nonce_dir()
        self._plant(directory, "child")
        nested = directory / "nested"
        nested.mkdir()
        self._plant(nested, "inner", b"y")
        self.fk.faults({"eio_scandir": ["/mnt/ud/kano-autooff.nonce"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertTrue((directory / "child").is_file())
        self.assertTrue((nested / "inner").is_file())
        self._no_copied_umount()

    def test_copied_nonce_directory_rmdir_eacces_stops(self):
        _ud, directory = self._nonce_dir()
        self._plant(directory, "child")
        nested = directory / "nested"
        nested.mkdir()
        self._plant(nested, "inner", b"y")
        self.fk.faults({"eacces_rmdir": ["/mnt/ud/kano-autooff.nonce/nested"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertTrue(nested.is_dir())
        self.assertTrue(directory.is_dir())
        self._no_copied_umount()

    def test_copied_nonce_rmtree_reported_error_stops(self):
        _ud, directory = self._nonce_dir()
        self._plant(directory, "child")
        self.fk.faults({"eio_rmtree_after": True})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertFalse(directory.exists())
        self._no_copied_umount()

    def test_copied_nonce_rmtree_requires_avoids_symlink_attacks(self):
        _ud, directory = self._nonce_dir()
        self._plant(directory, "child")
        self.fk.faults({"rmtree_avoids_symlink_attacks": False})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertTrue((directory / "child").is_file())
        self._no_copied_umount()

    def test_copied_nonce_rmtree_keep_stops_on_confirm(self):
        _ud, directory = self._nonce_dir()
        self._plant(directory, "child")
        self.fk.faults({"rmtree_keep": True})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertTrue((directory / "child").is_file())
        self._no_copied_umount()

    def test_copied_nonce_symlink_left_behind_stops(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        link = ud / "kano-autooff.nonce"
        link.symlink_to("nowhere")
        self.fk.faults({"rm_keep": True})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertTrue(link.is_symlink())
        self._no_copied_umount()

    def test_copied_userdata_umount_failure_stops(self):
        self.fk.faults({"fail_umount": ["/mnt/ud"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot unmount copied userdata")
        calls = self.fk.lines("calls.log")
        self.assertFalse(any(c.startswith("mount ") and " -o ro " in c and "f2fs" in c for c in calls), calls)

    def test_copied_userdata_recheck_umount_failure_stops(self):
        self.fk.faults({"fail_umount_nth": {"/mnt/ud": 2}})
        r = self.install()
        self._stopped_before_resize(r, "cannot unmount copied userdata after recheck")
        calls = self.fk.lines("calls.log")
        self.assertTrue(any(c.startswith("mount ") and " -o ro " in c and "f2fs" in c for c in calls), calls)
        self.assertEqual(len(self._copied_umounts()), 2, calls)

    def test_copied_userdata_remount_failure_stops(self):
        self.fk.faults({"fail_mount": ["-o ro -t f2fs"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot remount copied userdata")
        self.assertEqual(len(self._copied_umounts()), 1)

    def test_copied_nonce_recheck_lstat_eio_stops(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        nonce = ud / "kano-autooff.nonce"
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        self.fk.faults({"eio_lstat_nth": {"/mnt/ud/kano-autooff.nonce": 3}})
        r = self.install()
        self._stopped_before_resize(r, "cannot recheck copied kano-autooff.nonce")
        self.assertFalse(nonce.exists())
        calls = self.fk.lines("calls.log")
        self.assertTrue(any(c.startswith("mount ") and " -o ro " in c and "f2fs" in c for c in calls), calls)

    def test_copied_nonce_recheck_sees_a_name_planted_on_remount(self):
        self.fk.faults({"plant_nonce_on_ro_remount": True})
        r = self.install()
        self._stopped_before_resize(r, "still present after unmount")
        nonce = self.fk.root / "mnt" / "ud" / "kano-autooff.nonce"
        self.assertTrue(nonce.is_file())
        calls = self.fk.lines("calls.log")
        self.assertFalse(any(c.startswith("resize.f2fs") for c in calls), calls)

    def test_kernel_p1_start_mismatch_stops_before_filesystem(self):
        """sysfs start != NEW_START (30720) stops before the userdata filesystem is used.

        24576 is the start sgdisk reported before the rewrite. Leaving the kernel
        node there must not pass: the check compares to the new start.
        """
        self.fk.set("sys/block/nvme0n1/nvme0n1p1/start", "24576\n")
        r = self.install()
        self._stopped_before_resize(r, "kernel p1 start is not the new userdata start")
        calls = self.fk.lines("calls.log")
        self.assertFalse(any(c.startswith("mount ") and "f2fs" in c and "/mnt/ud" in c for c in calls), calls)
        self.assertFalse(any(c.startswith("dd ") and "if=/dev/nvme0n1" in c and "of=/dev/nvme0n1" in c
                             for c in calls), calls)

    def test_kernel_p1_length_mismatch_stops_before_filesystem(self):
        """sysfs size != NEW_SECT stops before the userdata copy. A missing size refuses too."""
        self.fk.set("sys/block/nvme0n1/nvme0n1p1/size", "6144\n")
        r = self.install()
        self._stopped_before_resize(r, "kernel p1 length is not the new userdata length")
        calls = self.fk.lines("calls.log")
        self.assertFalse(any(c.startswith("mount ") and "f2fs" in c and "/mnt/ud" in c for c in calls), calls)
        self.assertFalse(any(c.startswith("dd ") and "if=/dev/nvme0n1" in c and "of=/dev/nvme0n1" in c
                             for c in calls), calls)

    def test_kernel_p1_shorter_than_new_length_stops_before_userdata_dd(self):
        """sysfs size smaller than the new length (2048 vs 8192) still refuses.

        The product compare stays '='. A '-le' at the kernel length check accepts
        2048 and reaches the userdata dd, so this case turns red.
        """
        self.fk.set("sys/block/nvme0n1/nvme0n1p1/size", "2048\n")
        r = self.install()
        self._stopped_before_resize(r, "kernel p1 length is not the new userdata length")
        calls = self.fk.lines("calls.log")
        self.assertFalse(any(c.startswith("mount ") and "f2fs" in c and "/mnt/ud" in c for c in calls), calls)
        self.assertFalse(any(c.startswith("dd ") and "if=/dev/nvme0n1" in c and "of=/dev/nvme0n1" in c
                             for c in calls), calls)

    def test_new_p1_shorter_than_userdata_copy_stops_before_dd(self):
        """NEW_SECT < OLD_SECT (2048 < 6144) refuses before the userdata dd.

        Kernel sysfs start and size stay equal to that new p1 (30720 and 2048),
        so the kernel checks would pass. Only the new-length guard stops the copy.
        """
        self.fk.faults({"nvme_p1_new_sectors": 2048})
        self.fk.set("sys/block/nvme0n1/nvme0n1p1/start", "30720\n")
        self.fk.set("sys/block/nvme0n1/nvme0n1p1/size", "2048\n")
        r = self.install()
        self._stopped_before_resize(r, "new p1 is shorter than the userdata copy")
        calls = self.fk.lines("calls.log")
        self.assertFalse(any(c.startswith("dd ") and "if=/dev/nvme0n1" in c and "of=/dev/nvme0n1" in c
                             for c in calls), calls)
        self.assertFalse(any(c.startswith("resize.f2fs") for c in calls), calls)
        self.assertFalse(any(c.startswith("fsck.f2fs") for c in calls), calls)
        self.assertFalse(any(re.match(r"mount /dev/nvme0n1p12", c) for c in calls), calls)

    def test_new_p1_equal_to_userdata_copy_installs(self):
        """NEW_SECT == OLD_SECT (6144) installs.

        Sysfs start and size match the new p1. '-ge' allows this equal length;
        '-gt' refuses it.
        """
        self.fk.faults({"nvme_p1_new_sectors": 6144})
        self.fk.set("sys/block/nvme0n1/nvme0n1p1/start", "30720\n")
        self.fk.set("sys/block/nvme0n1/nvme0n1p1/size", "6144\n")
        r = self.install()
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("installed; next", r.stdout)
        calls = self.fk.lines("calls.log")
        self.assertTrue(any(c.startswith("dd ") and "if=/dev/nvme0n1" in c and "of=/dev/nvme0n1" in c
                            for c in calls), calls)
        moved = (self.fk.root / "sgdisk/nvme0n1/i1").read_text()
        self.assertIn("Partition size: 6144 sectors", moved)

    def test_kernel_p1_length_unreadable_stops_before_filesystem(self):
        (self.fk.root / "sys/block/nvme0n1/nvme0n1p1/size").unlink()
        r = self.install()
        self._stopped_before_resize(r, "cannot read the kernel length of p1")
        calls = self.fk.lines("calls.log")
        self.assertFalse(any(c.startswith("dd ") and "if=/dev/nvme0n1" in c and "of=/dev/nvme0n1" in c
                             for c in calls), calls)

    def test_copied_nonce_lookup_eacces_stops(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        nonce = ud / "kano-autooff.nonce"
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        self.fk.faults({"eacces_lstat": ["/mnt/ud/kano-autooff.nonce"]})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertTrue(nonce.is_file())
        calls = self.fk.lines("calls.log")
        self.assertFalse([c for c in calls if c.startswith("rm ") and "kano-autooff.nonce" in c], calls)
        self._no_copied_umount()

    def test_copied_nonce_rm_error_after_delete_stops(self):
        ud = self.fk.root / "mnt" / "ud"
        ud.mkdir(parents=True)
        nonce = ud / "kano-autooff.nonce"
        nonce.write_text("0123456789abcdef0123456789abcdef\n")
        self.fk.faults({"rm_error_after": True})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertFalse(nonce.exists())
        self._no_copied_umount()

    def test_copied_nonce_rmtree_onerror_reports_and_stops(self):
        _ud, directory = self._nonce_dir()
        self._plant(directory, "child")
        self.fk.faults({"rmtree_no_onexc": True})
        r = self.install()
        self._stopped_before_resize(r, "cannot delete copied kano-autooff.nonce")
        self.assertFalse(directory.exists())
        self._no_copied_umount()


if __name__ == "__main__":
    unittest.main()
