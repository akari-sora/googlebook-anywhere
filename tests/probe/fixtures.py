"""Fake /sys and /proc trees for tests/probe/probe-test.py: the KANO (Acer Chromebook Spin 714, MrChromebox UEFI)
and an AMD Ryzen AI 9 HX 370 mini PC. Only the files install/gbos-probe.sh reads; values follow what those machines
report (the HX 370 one is modelled on a typical Strix Point mini PC, not taken from a real dump). Each tree also holds
files the probe must NOT copy anywhere (serial numbers, a MAC address) to check that it leaves them alone."""
import os
from pathlib import Path

SECRET_SERIAL = "SN-DO-NOT-LEAK-4711"
SECRET_MAC = "aa:bb:cc:dd:ee:42"


def _w(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if text.endswith("\n") or text == "" else text + "\n")


def _ln(root, rel, target, make_target=False):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if make_target:                                  # a directory the files below the link go into (sys/block/<disk>)
        (p.parent / target).mkdir(parents=True, exist_ok=True)
    os.symlink(target, p)


def _pci(root, slot, ven, dev, cls, driver="", sv=0, sd=0):
    b = f"sys/bus/pci/devices/{slot}"
    _w(root, f"{b}/vendor", f"0x{ven:04x}"); _w(root, f"{b}/device", f"0x{dev:04x}"); _w(root, f"{b}/class", f"0x{cls:06x}")
    _w(root, f"{b}/subsystem_vendor", f"0x{sv:04x}"); _w(root, f"{b}/subsystem_device", f"0x{sd:04x}")
    if driver:
        _ln(root, f"{b}/driver", f"../../../bus/pci/drivers/{driver}")


def _acpi(root, name, hid, status="15"):
    b = f"sys/bus/acpi/devices/{name}"
    _w(root, f"{b}/hid", hid); _w(root, f"{b}/status", status); _w(root, f"{b}/path", f"\\_SB_.{name[:4]}")
    _w(root, f"{b}/modalias", f"acpi:{hid}:")


def _usb(root, port, vid, pid, product, ifaces):
    b = f"sys/bus/usb/devices/{port}"
    _w(root, f"{b}/idVendor", vid); _w(root, f"{b}/idProduct", pid); _w(root, f"{b}/product", product)
    _w(root, f"{b}/manufacturer", "x"); _w(root, f"{b}/serial", SECRET_SERIAL)
    for i, (cls, sub, proto, drv) in enumerate(ifaces):
        ib = f"{b}/{port}:1.{i}"
        _w(root, f"{ib}/bInterfaceClass", cls)
        _w(root, f"{ib}/modalias", f"usb:v{vid.upper()}p{pid.upper()}d0001dcE0dsc01dp01ic{cls.upper()}isc{sub}ip{proto}in{i:02X}")
        if drv:
            _ln(root, f"{ib}/driver", f"../../../../../bus/usb/drivers/{drv}")


def _common(root, dmi, cpu, cards, codecs, inputs, power, mem_sleep):
    for k, v in dmi.items():
        _w(root, f"sys/class/dmi/id/{k}", v)
    _w(root, "sys/class/dmi/id/product_serial", SECRET_SERIAL)
    _w(root, "sys/class/dmi/id/board_serial", SECRET_SERIAL)
    _w(root, "sys/class/dmi/id/product_uuid", "12345678-" + SECRET_SERIAL)
    (Path(root) / "sys/firmware/efi/efivars").mkdir(parents=True, exist_ok=True)
    (Path(root) / "sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c").write_bytes(b"\x06\x00\x00\x00\x00")
    _w(root, "proc/cpuinfo", "\n".join(f"{k}\t: {v}" for k, v in cpu.items()) + "\n\nprocessor\t: 1\nvendor_id\t: SECOND\n")
    _w(root, "proc/asound/cards", cards)
    for i, c in enumerate(codecs):
        _w(root, f"proc/asound/card{i}/codec#0", f"Codec: {c}\nAddress: 0\n")
        _w(root, f"sys/class/sound/card{i}/id", c.split()[0])
    _w(root, "proc/bus/input/devices", "\n".join(
        f"I: Bus={bus} Vendor=0000 Product=0000 Version=0000\nN: Name=\"{n}\"\nP: Phys=x\nS: Sysfs=/devices/x\nH: Handlers={h}\n"
        for bus, n, h in inputs))
    for name, typ in power:
        _w(root, f"sys/class/power_supply/{name}/type", typ)
    _w(root, "sys/power/mem_sleep", mem_sleep)
    _w(root, "sys/class/net/wlan0/address", SECRET_MAC)


def kano(root):
    """Acer Chromebook Spin 714 (KANO) on MrChromebox UEFI, booted into a live Linux."""
    _common(root, {"sys_vendor": "Google", "product_name": "Kano", "product_version": "rev3", "product_family": "Google_Brya",
                   "board_vendor": "Google", "board_name": "Kano", "bios_vendor": "coreboot",
                   "bios_version": "MrChromebox-2409.1", "bios_date": "10/01/2026"},
            {"processor": "0", "vendor_id": "GenuineIntel", "cpu family": "6", "model": "154", "stepping": "3",
             "model name": "12th Gen Intel(R) Core(TM) i7-1260P", "microcode": "0x434"},
            " 0 [sofnau8825     ]: sof-nau8825 - sof-nau8825\n", ["Nuvoton NAU8825"],
            [("0011", "AT Translated Set 2 keyboard", "sysrq kbd event0"), ("0018", "ELAN0000:00 04F3:3240 Touchpad", "mouse0 event1"),
             ("0018", "ELAN9008:00 04F3:2C82", "event2"), ("0018", "ELAN9008:00 04F3:2C82 Stylus", "event3"),
             ("0019", "Power Button", "kbd event4"), ("0019", "Lid Switch", "event5")],
            [("BAT0", "Battery")], "s2idle [deep]")
    _pci(root, "0000:00:02.0", 0x8086, 0x46A6, 0x030000, "xe")
    _pci(root, "0000:00:14.0", 0x8086, 0x51ED, 0x0C0330, "xhci_hcd")
    _pci(root, "0000:00:14.3", 0x8086, 0x51F0, 0x028000, "iwlwifi")
    _pci(root, "0000:00:15.0", 0x8086, 0x51E8, 0x0C8000, "intel-lpss")
    _pci(root, "0000:00:1e.0", 0x8086, 0x51A8, 0x078000, "intel-lpss")
    _pci(root, "0000:00:1f.3", 0x8086, 0x51C8, 0x040100, "sof-audio-pci-intel-tgl")
    _pci(root, "0000:01:00.0", 0x144D, 0xA809, 0x010802, "nvme")
    for name, hid in (("GOOG0004:00", "GOOG0004"), ("GOOG0002:00", "GOOG0002"), ("GOOG0005:00", "GOOG0005"),
                      ("PNP0C0D:00", "PNP0C0D"), ("PNP0C0C:00", "PNP0C0C"), ("PNP0C0A:00", "PNP0C0A"), ("ACPI0003:00", "ACPI0003"),
                      ("PNP0303:00", "PNP0303"), ("INTC1055:00", "INTC1055")):
        _acpi(root, name, hid)
    _acpi(root, "MSFT0101:00", "MSFT0101", status="0")           # present in the tables, disabled
    _usb(root, "3-10", "8087", "0033", "AX211 Bluetooth", [("e0", "01", "01", "btusb")])
    _ln(root, "sys/class/net/wlan0/device", "../../../0000:00:14.3")
    (Path(root) / "sys/class/net/wlan0/wireless").mkdir(parents=True)
    _ln(root, "sys/class/bluetooth/hci0/device", "../../../3-10:1.0")
    _ln(root, "sys/block/nvme0n1", "../devices/pci0000:00/0000:00:1d.0/0000:01:00.0/nvme/nvme0/nvme0n1", make_target=True)
    _w(root, "sys/block/nvme0n1/size", "1000215216"); _w(root, "sys/block/nvme0n1/removable", "0")
    _ln(root, "sys/block/sda", "../devices/pci0000:00/0000:00:14.0/usb3/3-1/3-1:1.0/host0/target0:0:0/0:0:0:0/block/sda", make_target=True)
    _w(root, "sys/block/sda/size", "62521344"); _w(root, "sys/block/sda/removable", "1")
    _w(root, "sys/block/loop0/size", "8")
    _ln(root, "sys/class/backlight/intel_backlight", "../../devices/pci0000:00/0000:00:02.0/drm/card0/card0-eDP-1/intel_backlight")
    _ln(root, "sys/class/leds/chromeos::kbd_backlight", "../../devices/platform/GOOG0004:00/GOOG0002:00/leds/chromeos::kbd_backlight")


def hx370(root):
    """An AMD Ryzen AI 9 HX 370 (Strix Point) mini PC with Windows-style firmware, booted into a live Linux."""
    _common(root, {"sys_vendor": "MiniPC Works", "product_name": "HX370 Mini", "product_version": "1.0",
                   "product_family": "Mini PC", "board_vendor": "MiniPC Works", "board_name": "HX370-MB",
                   "bios_vendor": "American Megatrends International, LLC.", "bios_version": "1.05", "bios_date": "06/01/2026"},
            {"processor": "0", "vendor_id": "AuthenticAMD", "cpu family": "26", "model": "36", "stepping": "0",
             "model name": "AMD Ryzen AI 9 HX 370 w/ Radeon 890M", "microcode": "0xb204032"},
            " 0 [Generic        ]: HDA-Intel - HD-Audio Generic\n 1 [Generic_1      ]: HDA-Intel - HD-Audio Generic\n",
            ["ATI R6xx HDMI", "Realtek ALC897"],
            [("0003", "USB Keyboard", "sysrq kbd event0"), ("0019", "Power Button", "kbd event1")],
            [], "s2idle")
    _pci(root, "0000:c4:00.0", 0x1002, 0x150E, 0x030000, "amdgpu")
    _pci(root, "0000:c4:00.1", 0x1002, 0x1640, 0x040300, "snd_hda_intel")
    _pci(root, "0000:c4:00.5", 0x1022, 0x15E2, 0x048000, "snd_acp_pci")
    _pci(root, "0000:c4:00.6", 0x1022, 0x15E3, 0x040300, "snd_hda_intel")
    _pci(root, "0000:c4:00.3", 0x1022, 0x151E, 0x0C0330, "xhci_hcd")
    _pci(root, "0000:01:00.0", 0x10EC, 0x8125, 0x020000, "r8169")
    _pci(root, "0000:02:00.0", 0x14C3, 0x7925, 0x028000, "mt7925e")
    _pci(root, "0000:03:00.0", 0x1E0F, 0x0009, 0x010802, "nvme")
    _pci(root, "0000:04:00.0", 0x1022, 0x43F6, 0x010601, "ahci")
    for name, hid in (("PNP0C0C:00", "PNP0C0C"), ("MSFT0101:00", "MSFT0101"), ("AMDI0030:00", "AMDI0030"), ("PNP0C09:00", "PNP0C09")):
        _acpi(root, name, hid)
    _usb(root, "1-4", "0e8d", "0717", "Wireless_Device", [("e0", "01", "01", "btusb")])
    _ln(root, "sys/class/net/wlp2s0/device", "../../../0000:02:00.0")
    (Path(root) / "sys/class/net/wlp2s0/wireless").mkdir(parents=True)
    _ln(root, "sys/class/net/enp1s0/device", "../../../0000:01:00.0")
    _ln(root, "sys/block/nvme0n1", "../devices/pci0000:00/0000:00:02.4/0000:03:00.0/nvme/nvme0/nvme0n1", make_target=True)
    _w(root, "sys/block/nvme0n1/size", "2000409264"); _w(root, "sys/block/nvme0n1/removable", "0")
    _ln(root, "sys/block/sdb", "../devices/pci0000:00/0000:04:00.0/ata1/host1/target1:0:0/1:0:0:0/block/sdb", make_target=True)
    _w(root, "sys/block/sdb/size", "1953525168"); _w(root, "sys/block/sdb/removable", "0"); _w(root, "sys/block/sdb/device/model", "SATA SSD 1TB")
    _w(root, "sys/class/tpm/tpm0/tpm_version_major", "2")
