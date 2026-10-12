"""kano-boot.sh / kano-ser.py menu: every title the systemd-boot menu draws counts (no fixed pattern), in screen
order; a substring must name exactly one entry, "=<title>" names it exactly; only the real countdown status line ends
the menu, not a title containing "Boot in". An append is typed only once the line editor is seen open. The fake menu is
drawn the way the edk2 serial terminal does it: clear screen, then ESC[row;colH + padded text per line, colour codes
for the highlight; the countdown on row 25, column 1; the line editor at row 25, column 2."""
import importlib.util, json, sys, unittest
from helpers import HARNESS, HarnessCase, harness_copy

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("kano_ser_menu", HARNESS / "kano-ser.py")
ks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ks)

TITLES = [
    "Googlebook KANO safe serial-debug",
    "Googlebook KANO safe",
    "Googlebook KANO plain (no pci-stub, for comparison)",
    "Googlebook (internal disk)",
    "Googlebook (internal disk) serial-debug",
    "Install Googlebook to the internal disk",
    "KANO maintenance Linux",
    "KANO maintenance NVMe install",
    "Reboot Into Firmware Interface",
]
NORMAL, HIGHLIGHT = b"\x1b[0m\x1b[37m\x1b[40m", b"\x1b[0m\x1b[30m\x1b[47m"
LINUX_TITLES = ["Googlebook KANO quiet", "Boot into Linux", "Googlebook KANO quiet (internal)"]
# what the menu draws after `e` with "editor yes": the status row cleared, then the start of the entry's options
EDITOR = (b"\x1b[25;02H" + HIGHLIGHT + b" " * 77 + b"\x1b[25;02H\x1b[1m" + HIGHLIGHT +
          b"stack_depot_disable=on cgroup_disable=pressure bootconfig initrd=\\initrd.img intel_iommu=igfx_off")


def firmware_menu(titles=TITLES, selected=0, clear=True, width=80):
    """AP UART bytes: firmware chatter (it even names an entry), then the menu draw, then countdown redraws."""
    out = (b"coreboot-4.22 Mon Oct  6 bootblock starting...\r\n"
           b"BdsDxe: loading Boot0002 \"UEFI Googlebook KANO safe\" from PciRoot(0x0)/USB(0x0,0x0)\r\n"
           b"BdsDxe: starting Boot0002\r\n")
    out += NORMAL + (b"\x1b[2J" if clear else b"") + b"\x1b[01;01H"
    for i, t in enumerate(titles):
        out += b"\x1b[%02d;%02dH" % (3 + i, 2) + (HIGHLIGHT if i == selected else NORMAL) + t.center(width - 2).encode()
    out += NORMAL + b"\x1b[%02d;%02dH" % (3 + len(titles), 2) + b" " * (width - 2)          # blank row
    out += b"\x1b[25;01H" + "Boot in 5 s.".center(width).encode()
    out += b"\x1b[25;01H" + "Boot in 4 s.".center(width).encode()
    out += b"\x1b[04;02H" + HIGHLIGHT + "Some later redraw".center(width - 2).encode()       # after the first draw
    return out


class Parse(unittest.TestCase):
    def test_every_drawn_title_in_screen_order(self):
        self.assertEqual(ks.menu_titles(firmware_menu()), TITLES)

    def test_highlight_position_does_not_matter(self):
        self.assertEqual(ks.menu_titles(firmware_menu(selected=4)), TITLES)

    def test_without_clear_screen_the_first_cursor_positioning_starts_the_menu(self):
        self.assertEqual(ks.menu_titles(firmware_menu(clear=False)), TITLES)

    def test_a_title_containing_boot_in_does_not_end_the_menu(self):
        self.assertEqual(ks.menu_titles(firmware_menu(LINUX_TITLES)), LINUX_TITLES)
        self.assertEqual(ks.pick_entry(LINUX_TITLES, "quiet")[1][0], 6)

    def test_countdown_status_row(self):
        self.assertEqual(ks.menu_draw(firmware_menu())[1], 25)

    def test_editor_detection(self):
        self.assertTrue(ks.editor_open(EDITOR, 25))
        self.assertFalse(ks.editor_open(EDITOR, 24))                          # another row
        self.assertFalse(ks.editor_open(EDITOR.replace(b"=", b" "), 25))      # no options drawn
        self.assertFalse(ks.editor_open(b"\x1b[25;02H" + HIGHLIGHT + b" " * 77, 25))   # only the cleared line
        # a redraw of the menu entries (what Up/Down cause) is not the editor
        self.assertFalse(ks.editor_open(b"\x1b[04;02H" + HIGHLIGHT + "Googlebook KANO safe".center(78).encode(), 25))

    def test_no_menu(self):
        self.assertIsNone(ks.menu_titles(b"coreboot\r\nno boot loader\r\n"))
        self.assertIsNone(ks.menu_titles(b"plain text only\r\nBoot in 3 s.\r\n"))

    def test_pick(self):
        cases = {
            "=Googlebook (internal disk)": (3, None),
            "=Googlebook (internal disk) serial-debug": (4, None),
            "Install Googlebook": (5, None),
            "plain": (2, None),
            "KANO maintenance NVMe install": (7, None),
            "Reboot Into Firmware": (8, None),
        }
        for want, result in cases.items():
            with self.subTest(want=want):
                self.assertEqual(ks.pick_entry(TITLES, want), result)
        for want, status in {"Googlebook (internal disk)": 6, "serial-debug": 6, "KANO maintenance": 6,
                             "Googlebook KANO safe": 6, "=googlebook (internal disk)": 5, "=Googlebook": 5,
                             "nothing like it": 5, "": 5}.items():
            with self.subTest(want=want):
                idx, err = ks.pick_entry(TITLES, want)
                self.assertIsNone(idx)
                self.assertEqual(err[0], status)


class Boot(HarnessCase):
    """kano-boot.sh end to end: fake EC power button makes the fake firmware draw the menu on the AP UART log."""

    def setUp(self):
        super().setUp()
        self.h = harness_copy(self.fk.tmp / "harness")
        self.uart = self.fk.tmp / "uart.log"
        self.menu(firmware_menu())
        self.env = dict(KANO_AP_UART_LOG=str(self.uart), FAKE_SERIAL_BOOT_MENU=str(self.fk.tmp / "menu.bin"),
                        FAKE_SERIAL_BOOT_LOG=str(self.uart))

    def menu(self, draw):
        (self.fk.tmp / "menu.bin").write_bytes(draw)

    def editor_yes(self):
        (self.fk.tmp / "editor.bin").write_bytes(EDITOR)
        self.env["FAKE_SERIAL_EDITOR"] = str(self.fk.tmp / "editor.bin")

    def boot(self, *args):
        return self.fk.run(["bash", self.h / "kano-boot.sh", *args], timeout=90, **self.env)

    def ap_keys(self):
        f = self.fk.root / "log" / "serial.log"
        ev = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
        return [e["hex"] for e in ev if e["event"] == "write" and e["dev"] == self.fk.fixtures()["KANO_AP_TTY"]]

    def test_exact_title_selects_that_entry(self):
        r = self.boot("=Googlebook (internal disk)")
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertEqual(self.ap_keys(), ["1b5b41", "1b5b48"] + ["1b5b42"] * 3 + ["0d"])
        self.reason("selected #3: Googlebook (internal disk)", r.stdout)

    def test_ambiguous_title_selects_nothing(self):
        r = self.boot("Googlebook (internal disk)")
        self.assertEqual(r.returncode, 6, self.explain(r))
        self.assertEqual(self.ap_keys(), ["1b5b41"])            # countdown stopped, nothing chosen
        self.reason("matches 2 entries", r.stderr)

    def test_a_title_containing_boot_in_cannot_hide_an_ambiguity(self):
        self.menu(firmware_menu(LINUX_TITLES))
        r = self.boot("quiet")
        self.assertEqual(r.returncode, 6, self.explain(r))
        self.assertEqual(self.ap_keys(), ["1b5b41"])            # countdown stopped, nothing chosen
        self.reason("matches 2 entries", r.stderr)

    def test_append_is_typed_once_the_editor_is_open(self):
        self.editor_yes()
        r = self.boot("=Googlebook (internal disk)", "loglevel=8")
        self.assertEqual(r.returncode, 0, self.explain(r))
        typed = [bytes.fromhex(k).decode() for k in self.ap_keys()[7:-2]]
        self.assertEqual(self.ap_keys()[:7], ["1b5b41", "1b5b48"] + ["1b5b42"] * 3 + ["65", "1b5b46"])
        self.assertEqual("".join(typed), " loglevel=8")
        self.assertEqual(self.ap_keys()[-2:], ["0d", "0d"])     # ap-type's CR, then Enter

    def test_append_without_an_open_editor_types_nothing(self):
        # "editor no" (the image default): `e` is ignored, so the append's characters would be menu hotkeys
        r = self.boot("=Googlebook (internal disk)", "nvme_core.default_ps_max_latency_us=0")
        self.assertEqual(r.returncode, 3, self.explain(r))
        self.assertEqual(self.ap_keys(), ["1b5b41", "1b5b48"] + ["1b5b42"] * 3 + ["65"])   # nothing after `e`
        self.reason("nothing typed", r.stdout)

    def test_unwritable_log_stops_before_power_on(self):
        self.uart.write_text("an old menu draw would be parsed from here\n")
        self.uart.chmod(0o444)
        r = self.boot("=Googlebook (internal disk)")
        self.assertEqual(r.returncode, 3, self.explain(r))
        ev = [json.loads(l) for l in (self.fk.root / "log" / "serial.log").read_text().splitlines()]
        sent = [bytes.fromhex(e["hex"]).strip(b"\r").decode() for e in ev if e["event"] == "write"]
        self.assertEqual(sent, ["apshutdown"])                  # no powerbtn, no menu keys
        self.reason("cannot empty the AP UART log", r.stdout)

    def test_titles_outside_any_fixed_pattern_count(self):
        # "Install ..." sits between entries an old fixed pattern did not know; Down presses must include them
        r = self.boot("Install Googlebook")
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertEqual(self.ap_keys(), ["1b5b41", "1b5b48"] + ["1b5b42"] * 5 + ["0d"])


if __name__ == "__main__":
    unittest.main()
