"""The pure checks of both delta pushes on the real lapis/KANO GPT (31 entries, no #11, 1-sector KERN-C, ROOT-C,
RWFW-A, RWFW-B; taken from the base image analysis), so a live push is not refused because the tests only ever saw a
simple layout."""
import importlib.util, os, sys, unittest
from helpers import HARNESS, NVME_SERIAL, STICK_SERIAL, gpt_bytes_lba

sys.dont_write_bytecode = True
MiB, GiB = 1 << 20, 1 << 30
REAL_SIZE = 19069493760
REAL_PARTS = [  # (number, name, start byte, size bytes)
    (1, "userdata", 14598275072, 4336885760), (2, "KERN-A", 209715200, 536870912),
    (3, "super", 746586112, 12884901888), (4, "KERN-B", 13631488000, 4194304), (5, "ROOT-B", 13635682304, 536870912),
    (6, "KERN-C", 136314880, 512), (7, "ROOT-C", 136315392, 512), (8, "OEM", 138412032, 4194304),
    (9, "ota_recovery_a", 2097152, 134217728), (10, "ota_recovery_b", 18935160832, 134217728),
    (12, "EFI-SYSTEM", 142606336, 67108864), (13, "boot_a", 14172553216, 67108864), (14, "boot_b", 14239662080, 67108864),
    (15, "vbmeta_a", 14306770944, 4194304), (16, "vbmeta_b", 14310965248, 4194304), (17, "metadata", 14315159552, 67108864),
    (18, "init_boot_a", 14382268416, 8388608), (19, "init_boot_b", 14390657024, 8388608),
    (20, "vendor_boot_a", 14399045632, 33554432), (21, "vendor_boot_b", 14432600064, 33554432),
    (22, "pvmfw_a", 14466154496, 4194304), (23, "pvmfw_b", 14470348800, 4194304), (24, "misc", 14474543104, 4194304),
    (25, "RWFW-A", 136315904, 512), (26, "RWFW-B", 136316416, 512), (27, "recovery_a", 14478737408, 33554432),
    (28, "recovery_b", 14512291840, 33554432), (29, "desktop_security_persist", 14545846272, 16777216),
    (30, "desktop_security_storage", 14562623488, 33554432), (31, "frp_persist", 14596177920, 2097152),
]
IMG = [(n, name, s, s + size) for n, name, s, size in REAL_PARTS]
XB = 37244880 * 512                                   # the stick's XBOOTLDR start (kano-nvme-install.sh END_SECT)
STICK = IMG + [(32, "XBOOTLDR", XB, XB + 512 * MiB), (33, "kano-maint", XB + 512 * MiB, XB + 512 * MiB + 62 * 128 * MiB)]
NVME = [(1, "userdata", 37246976 * 512, 128 * GiB - 34 * 512)] + IMG[1:]
WRITES = [(712 * MiB, 714 * MiB), (13644 * MiB, 13645 * MiB)]      # in super and vbmeta_a


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_")[:-3], HARNESS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dp, nv = load("kano-delta-push.py"), load("kano-delta-push-nvme.py")


def facts(disk, parts, tran, serial, size):
    name = (lambda n: f"{disk}p{n}") if disk[-1].isdigit() else (lambda n: f"{disk}{n}")
    return {"ROOTSRC": "/dev/sda33", "ROOTDISK": "sda", "ROOTTRAN": "usb", "ROOTSERIAL": STICK_SERIAL, "DISK": disk,
            "TRAN": tran, "SERIAL": serial, "SIZE": str(size), "SWAP": [], "HOLDER": [],
            "MOUNT": [("/dev/sda33", "/"), ("/dev/sda32", "/boot"), ("proc", "/proc")],
            "KPART": {n: (name(n), s, e) for n, _, s, e in parts}, "PARTS": parts}


class RealLayout(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)
        os.environ.update(KANO_STICK_SERIAL=STICK_SERIAL, KANO_NVME_SERIAL=NVME_SERIAL)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_gpt_parser_reads_the_real_table(self):
        primary, _ = gpt_bytes_lba(REAL_SIZE, [(n, name, s // 512, e // 512 - 1) for n, name, s, e in IMG])
        self.assertEqual(dp.gpt_parts(primary + bytes(MiB - len(primary))), IMG)

    def test_stick_push_accepts_the_real_layout(self):
        f = facts("sda", STICK, "usb", STICK_SERIAL, 31 * GiB)
        self.assertEqual(dp.check_stick(f, IMG, REAL_SIZE, WRITES + [(137 * MiB, 138 * MiB)]), [])

    def test_nvme_push_accepts_the_real_layout(self):
        f = facts("nvme0n1", NVME, "nvme", NVME_SERIAL + "     ", 128 * GiB)
        self.assertEqual(nv.check_nvme(f, IMG, WRITES), [])

    def test_a_partition_the_kernel_does_not_show_makes_it_refuse(self):
        # strict on purpose: the kernel must show every image partition as the GPT has it (incl. 1-sector ones)
        f = facts("sda", STICK, "usb", STICK_SERIAL, 31 * GiB)
        del f["KPART"][6]
        self.assertTrue(any("KERN-C" in e for e in dp.check_stick(f, IMG, REAL_SIZE, WRITES)))


if __name__ == "__main__":
    unittest.main()
