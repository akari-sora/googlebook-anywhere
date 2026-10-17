#!/usr/bin/env python3
"""Read a produced installer stick's menu and refuse to rewrite it.

qemu-install.py --produced calls stage_produced before any boot. Serial,
nolockdown, noinitcall, and nomodule entries are extra filenames derived from
the bytes just read. The delivered gbos-install.conf, gbos-update.conf, and
ESP loader.conf stay as the builder wrote them until that check has passed.
After the check, point_copy_at may aim the *copy's* ESP default at an extra
serial filename so QEMU's serial port is the installer terminal. The source
stick file is not this module's to open.
"""
import hashlib
import importlib.util
import re
from pathlib import Path

SERIAL_OPTS = (
    " console=ttyS0,115200 gbos.install.tty=ttyS0"
    " systemd.mask=serial-getty@ttyS0.service"
)
DELIVERED = ("gbos-install.conf", "gbos-update.conf")
PLAIN_NAME = "maintenance.conf"
PROTECTION = (
    "lockdown=integrity",
    "efi=noruntime",
    "module_blacklist=",
    "initcall_blacklist=",
)
_PAYLOAD = None


class ProducedMenuError(Exception):
    pass


def load_payload():
    global _PAYLOAD
    if _PAYLOAD is not None:
        return _PAYLOAD
    path = Path(__file__).resolve().parents[2] / "install/stick/stick-payload.py"
    spec = importlib.util.spec_from_file_location("stick_payload", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _PAYLOAD = module
    return module


def _ascii(data, label):
    try:
        return data.decode("ascii")
    except UnicodeError as err:
        raise ProducedMenuError(f"produced {label} is not ASCII") from err


def _options(text, label):
    lines = [line for line in text.splitlines() if line.startswith("options ")]
    if len(lines) != 1:
        raise ProducedMenuError(f"{label} needs exactly one options line")
    return lines[0]


def _require_protections(text, label):
    options = _options(text, label)
    missing = [token for token in PROTECTION if token not in options]
    if missing:
        raise ProducedMenuError(f"{label} boots without " + " ".join(missing))
    if "title Maintenance" in text.splitlines():
        raise ProducedMenuError("plain Maintenance entry is on the produced stick")


def derive_serial(install, update, initcalls):
    """Extra filenames only. The delivered texts are not returned for writing."""
    if f" initcall_blacklist={initcalls} " not in install:
        raise ProducedMenuError(
            "produced install entry has no initcall_blacklist from builtin-initcalls.txt"
        )
    if not _options(install, "gbos-install.conf").endswith(" gbos.install=1"):
        raise ProducedMenuError("produced install entry does not end with gbos.install=1")
    if not _options(update, "gbos-update.conf").endswith(" gbos.install=1 gbos.update=1"):
        raise ProducedMenuError("produced update entry lost gbos.update=1")

    def retitle(text, title):
        return re.sub(r"(?m)^title .*$", f"title {title}", text)

    def add_serial(text):
        return re.sub(r"(?m)^(options .*)$", lambda m: m.group(1) + SERIAL_OPTS, text)

    test_entry = add_serial(retitle(install, "gbos-install-test"))
    nold = retitle(test_entry, "gbos-install-nolockdown").replace(" lockdown=integrity", "")
    noic = retitle(test_entry, "gbos-install-noinitcall").replace(
        f" initcall_blacklist={initcalls} ", " initcall_blacklist=mei_me_driver_init "
    )
    if noic.count("initcall_blacklist=mei_me_driver_init ") != 1:
        raise ProducedMenuError("cannot build the noinitcall entry")
    nomod = retitle(test_entry, "gbos-install-nomodule").replace(",spi_intel_pci,", ",")
    if test_entry.count(",spi_intel_pci,") != 1 or "spi_intel_pci" in nomod:
        raise ProducedMenuError("cannot build the nomodule entry")
    upd = add_serial(retitle(update, "gbos-update-test"))
    if "gbos.update=1" not in upd.split():
        raise ProducedMenuError("serial update entry lost gbos.update=1")
    extras = (
        ("gbos-install-test.conf", test_entry),
        ("gbos-install-nolockdown.conf", nold),
        ("gbos-install-noinitcall.conf", noic),
        ("gbos-install-nomodule.conf", nomod),
        ("gbos-update-test.conf", upd),
    )
    for name, _text in extras:
        if name in DELIVERED or name == PLAIN_NAME:
            raise ProducedMenuError("refusing to rewrite a delivered entry")
    return extras


def _delivered(store, template, partuuid, initcalls):
    """Read the delivered menu and compare it to the builder. Write nothing."""
    payload = load_payload()
    try:
        names = list(store.names())
    except FileNotFoundError as err:
        raise ProducedMenuError("produced stick has no loader entries") from err
    texts = {}
    for name in names:
        texts[name] = _ascii(store.read(f"entries/{name}"), name)
    for name, text in texts.items():
        if name == PLAIN_NAME or "title Maintenance" in text.splitlines():
            raise ProducedMenuError("plain Maintenance entry is on the produced stick")
    for name in DELIVERED:
        if name not in texts:
            raise ProducedMenuError(f"produced stick has no {name}")
    install = texts["gbos-install.conf"]
    update = texts["gbos-update.conf"]
    loader = _ascii(store.read("esp/loader.conf"), "loader.conf")
    snapshot = dict(texts)
    snapshot["loader.conf"] = loader
    expected_install, expected_update = payload.render_pair(template, partuuid, initcalls)
    if install != expected_install:
        raise ProducedMenuError("produced gbos-install.conf is not the builder render")
    if update != expected_update:
        raise ProducedMenuError("produced gbos-update.conf is not the builder render")
    if loader != payload.esp_loader_text():
        raise ProducedMenuError("produced loader default is not gbos-install.conf")
    _require_protections(install, "gbos-install.conf")
    _require_protections(update, "gbos-update.conf")
    return install, snapshot


def read_delivered(store, template, partuuid, initcalls):
    """AS-IS boot: the delivered menu must match the builder, and nothing is written."""
    install, snapshot = _delivered(store, template, partuuid, initcalls)
    for name, text in snapshot.items():
        key = "esp/loader.conf" if name == "loader.conf" else f"entries/{name}"
        if _ascii(store.read(key), name) != text:
            raise ProducedMenuError(f"delivered {name} changed")
    return install


def stage_produced(store, template, partuuid, initcalls):
    """Read the delivered menu, compare it to the builder, write only extra names.

    Returns the produced gbos-install.conf text. That text is what static
    checks must use. A bad delivered file raises and is not replaced.
    """
    install, snapshot = _delivered(store, template, partuuid, initcalls)
    for name, text in derive_serial(install, snapshot["gbos-update.conf"], initcalls):
        store.write(f"entries/{name}", text.encode("ascii"))
    for name, text in snapshot.items():
        key = "esp/loader.conf" if name == "loader.conf" else f"entries/{name}"
        if _ascii(store.read(key), name) != text:
            raise ProducedMenuError(f"delivered {name} changed")
    store.produced_menu_ok = True
    return install


def point_copy_at(store, name):
    """Point the test copy's ESP default at an extra serial entry.

    The delivered default was already compared to esp_loader_text(). This
    write is how QEMU selects the serial session. It does not write
    gbos-install.conf or gbos-update.conf.
    """
    if not getattr(store, "produced_menu_ok", False):
        raise ProducedMenuError(
            "refusing to change the loader default before the produced menu check"
        )
    if name in DELIVERED or name == PLAIN_NAME:
        raise ProducedMenuError(
            "refusing to boot a delivered entry in place of an extra serial entry"
        )
    key = f"entries/{name}"
    try:
        text = _ascii(store.read(key), name)
    except FileNotFoundError as err:
        raise ProducedMenuError(f"missing extra entry {name}") from err
    if "console=ttyS0,115200" not in text or "systemd.mask=serial-getty@ttyS0.service" not in text:
        raise ProducedMenuError("extra entry has no serial-only tokens")
    install = _ascii(store.read("entries/gbos-install.conf"), "gbos-install.conf")
    roots = [tok for tok in install.split() if tok.startswith("root=PARTUUID=")]
    if len(roots) != 1 or roots[0] not in text.split():
        raise ProducedMenuError("extra entry lost the produced root")
    if name.startswith("gbos-update") and "gbos.update=1" not in text.split():
        raise ProducedMenuError("extra update entry lost gbos.update=1")
    store.write(
        "esp/loader.conf",
        f"default {name}\ntimeout 1\neditor no\n".encode("ascii"),
    )


class MemStick:
    """In-memory stand-in for the XBOOTLDR entries and the ESP loader.conf."""

    def __init__(self, files):
        self.files = dict(files)
        self.produced_menu_ok = False

    def read(self, key):
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write(self, key, data):
        self.files[key] = data

    def names(self):
        prefix = "entries/"
        return sorted(key[len(prefix):] for key in self.files if key.startswith(prefix))


def _good_files(install, update, loader):
    nvme = (
        "title Googlebook (internal disk)\n"
        "linux /gbos-nvme/kernel.efi\n"
        "initrd /gbos-nvme/initrd.img\n"
        "options console=tty0\n"
    )
    return {
        "entries/gbos-install.conf": install.encode("ascii"),
        "entries/gbos-update.conf": update.encode("ascii"),
        "entries/gbos-nvme.conf": nvme.encode("ascii"),
        "esp/loader.conf": loader.encode("ascii"),
    }


def replay(qemu_path):
    """Offline mutation of the produced-menu check. No QEMU, no mtools, no image."""
    failed = []

    def ok(name):
        print(f"PASS {name}", flush=True)

    def bad(name):
        failed.append(name)
        print(f"FAIL {name}", flush=True)

    payload = load_payload()
    repo = Path(__file__).resolve().parents[2]
    template = (repo / "install/stick/gbos-install.entry.template").read_text()
    initcalls, _covered = payload.parse_map(
        (repo / "install/stick/builtin-initcalls.txt").read_text()
    )
    partuuid = "00112233-4455-6677-8899-aabbccddeeff"
    install, update = payload.render_pair(template, partuuid, initcalls)
    loader = payload.esp_loader_text()

    def reject(files, label):
        store = MemStick(files)
        snap = dict(store.files)
        try:
            stage_produced(store, template, partuuid, initcalls)
        except ProducedMenuError:
            if store.files != snap:
                bad(f"{label} rewrote the delivered menu")
            else:
                ok(label)
            return
        bad(f"{label} was accepted")

    good = MemStick(_good_files(install, update, loader))
    try:
        got = stage_produced(good, template, partuuid, initcalls)
    except ProducedMenuError as err:
        bad(f"good produced menu was rejected ({err})")
    else:
        kept = (
            got == install
            and good.read("entries/gbos-install.conf") == install.encode("ascii")
            and good.read("entries/gbos-update.conf") == update.encode("ascii")
            and good.read("esp/loader.conf") == loader.encode("ascii")
            and good.read("entries/gbos-nvme.conf").startswith(b"title Googlebook")
            and "entries/gbos-install-test.conf" in good.files
            and "entries/maintenance.conf" not in good.files
        )
        if kept:
            ok("produced install, update, loader, and nvme entries stay byte for byte")
        else:
            bad("produced install, update, loader, and nvme entries stay byte for byte")
        serial = good.read("entries/gbos-install-test.conf").decode("ascii")
        upd = good.read("entries/gbos-update-test.conf").decode("ascii")
        noic = good.read("entries/gbos-install-noinitcall.conf").decode("ascii")
        nold = good.read("entries/gbos-install-nolockdown.conf").decode("ascii")
        nomod = good.read("entries/gbos-install-nomodule.conf").decode("ascii")
        delivered = install + "\n" + update
        serial_ok = (
            f"root=PARTUUID={partuuid}" in serial
            and "lockdown=integrity" in serial
            and "console=ttyS0,115200" in serial
            and "systemd.mask=serial-getty@ttyS0.service" in serial
            and "gbos.update=1" in upd.split()
            and f"root=PARTUUID={partuuid}" in upd
            and "lockdown=integrity" not in nold.split()
            and "initcall_blacklist=mei_me_driver_init" in noic.split()
            and initcalls not in noic
            and "spi_intel_pci" not in nomod
            and "console=ttyS0" not in delivered
            and "systemd.mask=serial-getty@ttyS0.service" not in delivered
        )
        if serial_ok:
            ok("serial extras keep the produced root and protections, and are not written back")
        else:
            bad("serial extras keep the produced root and protections, and are not written back")

    broken = _good_files(install, update, loader)
    broken["entries/gbos-install.conf"] = install.replace(
        " lockdown=integrity", "", 1
    ).encode("ascii")
    reject(broken, "a produced install entry without lockdown is rejected and not repaired")

    wrong_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    store = MemStick(_good_files(install, update, loader))
    snap = dict(store.files)
    try:
        stage_produced(store, template, wrong_id, initcalls)
    except ProducedMenuError:
        if store.files == snap:
            ok("a produced install entry with the wrong PARTUUID is rejected and not repaired")
        else:
            bad("a wrong PARTUUID rewrote the delivered menu")
    else:
        bad("a produced install entry with the wrong PARTUUID was accepted")

    no_flag = _good_files(install, update, loader)
    no_flag["entries/gbos-update.conf"] = update.replace(" gbos.update=1", "").encode("ascii")
    reject(no_flag, "a produced update entry without gbos.update=1 is rejected and not repaired")

    wrong_loader = _good_files(install, update, loader)
    wrong_loader["esp/loader.conf"] = (
        "default gbos-nvme.conf\ntimeout 5\neditor no\nauto-firmware no\n"
    ).encode("ascii")
    reject(wrong_loader, "a produced loader default other than gbos-install.conf is rejected")

    planted = _good_files(install, update, loader)
    planted["entries/maintenance.conf"] = (
        "title Maintenance\n"
        "linux /vmlinuz-linux\n"
        "initrd /initramfs-linux.img\n"
        "options root=PARTUUID=" + partuuid + " rw\n"
    ).encode("ascii")
    reject(planted, "a planted maintenance.conf is rejected and not repaired")

    missing = _good_files(install, update, loader)
    del missing["entries/gbos-install.conf"]
    reject(missing, "a produced stick with no gbos-install.conf is rejected")

    hashed = MemStick(broken)
    digest = hashlib.sha256(b"".join(hashed.files[k] for k in sorted(hashed.files))).hexdigest()
    # Publishing a checksum of the bad bytes does not enter the check.
    if digest and len(digest) == 64:
        reject(broken, "a checksum of the bad install entry does not make that menu pass")
    else:
        bad("a checksum of the bad install entry does not make that menu pass")

    fresh = MemStick(_good_files(install, update, loader))
    try:
        point_copy_at(fresh, "gbos-install-test.conf")
    except ProducedMenuError as err:
        early_ok = (
            "before the produced menu check" in str(err)
            and fresh.read("esp/loader.conf") == loader.encode("ascii")
        )
    else:
        early_ok = False
    if not early_ok:
        bad("the copy default is not changed before the produced menu check")
    else:
        stage_produced(fresh, template, partuuid, initcalls)
        before_i = fresh.read("entries/gbos-install.conf")
        before_u = fresh.read("entries/gbos-update.conf")
        point_copy_at(fresh, "gbos-install-test.conf")
        aimed = fresh.read("esp/loader.conf") == (
            b"default gbos-install-test.conf\ntimeout 1\neditor no\n"
        )
        try:
            point_copy_at(fresh, "gbos-install.conf")
        except ProducedMenuError:
            refused_delivered = True
        else:
            refused_delivered = False
        if (
            aimed
            and refused_delivered
            and fresh.read("entries/gbos-install.conf") == before_i
            and fresh.read("entries/gbos-update.conf") == before_u
        ):
            ok("after the check, only the copy default aims at the extra serial entry")
        else:
            bad("after the check, only the copy default aims at the extra serial entry")

    src = Path(qemu_path).read_text()
    marker = "# Produced stick: keep gbos-install.conf, gbos-update.conf, and loader.conf.\n"
    wiring = None
    if src.count(marker) != 1 or src.count("produced_menu.stage_produced(") != 1:
        wiring = "stage_produced is not the one produced path"
    else:
        arm = src.split(marker, 1)[1].split("else:", 1)[0]
        rest = src.split(marker, 1)[1].split("else:", 1)[1].split("def stick_default", 1)[0]
        install_copies = [
            line.strip() for line in src.splitlines()
            if "mcopy" in line and "::/loader/entries/gbos-install.conf" in line
        ]
        update_copies = [
            line.strip() for line in src.splitlines()
            if "mcopy" in line and "::/loader/entries/gbos-update.conf" in line
        ]
        if "mcopy" in arm or "mdel" in arm or "stage_produced(" not in arm:
            wiring = "produced arm rewrites the menu or does not stage it"
        elif install_copies != [
            'mtools("mcopy", STICK, XBOFF, "-o", W / "gbos-install.conf", "::/loader/entries/gbos-install.conf")'
        ]:
            wiring = "non-produced install mcopy changed"
        elif update_copies:
            wiring = "gbos-update.conf is an mcopy destination"
        elif "::/loader/entries/gbos-install.conf" not in rest:
            wiring = "non-produced path no longer plants the installer entry"
        elif "produced_menu.point_copy_at(produced_store, name)" not in src:
            wiring = "the copy default is not the point_copy_at path"
    if wiring:
        bad(f"qemu-install.py keeps the produced menu ({wiring})")
    else:
        ok("qemu-install.py reads the produced menu and does not mcopy it back")

    return 1 if failed else 0
