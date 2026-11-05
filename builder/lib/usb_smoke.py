"""Opt-in KANO USB-only backlight trial construction helpers.

This is build-time isolation only. A separate, reviewed staging/boot harness
must compare against the current internal GPT and prove the running root is
on the intended USB before any physical acceptance claim.
"""
import struct
import uuid

MAGIC = b"#BOOTCONFIG\n"
DEBUG_TITLE = "Googlebook KANO USB backlight trial serial-debug"
QUIET_TITLE = "Googlebook KANO USB backlight trial quiet"


def validate_options(profile, env):
    value = env.get("GBOS_USB_SMOKE", "")
    if value not in ("", "1"):
        raise ValueError("GBOS_USB_SMOKE must be empty or 1")
    if not value:
        return
    if (profile["model"]["id"] != "kano"
            or "gbos_kbd_backlight" not in profile["kmods"]
            or env.get("GBOS_LAPIS_BUILD") != "16658739"
            or env.get("GBOS_EC") != "drain"
            or not env.get("GBOS_ADB_PUBKEY")
            or env.get("GBOS_DEFAULT_ENTRY", "lapis-quiet.conf") != "lapis-quiet.conf"
            or env.get("GBOS_LOADER_EDITOR", "no") != "no"
            or env.get("GBOS_SKIP_SELFTEST") == "1"
            or env.get("GBOS_DEV_KERNEL_DIR")
            or env.get("GBOS_TEST_AUTOOFF")
            or env.get("GBOS_ALLOW_FIRMWARE_DRIFT") == "1"):
        raise ValueError("USB smoke requires KANO backlight profile, build 16658739, drain, ADB, "
                         "stock kernel, quiet default, editor no and mandatory self-tests")


def validate_gpt_rewrite(before, after):
    """Require new, pairwise distinct partition UUIDs and unchanged layout."""
    if not before or set(before) != set(after) or "boot_a" not in before or "userdata" not in before:
        raise ValueError("USB GPT partition names changed")
    old_guids = set()
    new_guids = set()
    for name in before:
        a, b = before[name], after[name]
        if any(a[k] != b[k] for k in ("type", "start", "end")):
            raise ValueError("USB GPT partition layout changed")
        x, y = uuid.UUID(a["guid"]), uuid.UUID(b["guid"])
        if x.int == 0 or y.int == 0 or y.version != 4:
            raise ValueError("invalid USB GPT GUID")
        old_guids.add(x)
        new_guids.add(y)
    if len(old_guids) != len(before) or len(new_guids) != len(after) or old_guids & new_guids:
        raise ValueError("USB GPT GUID collision")
    return after


def rebind_bootconfig(data, old_guid, new_guid):
    """Set the initrd boot partition UUID to its regenerated USB GPT UUID."""
    if len(data) < 20 or not data.endswith(MAGIC):
        raise ValueError("missing bootconfig footer")
    size, checksum = struct.unpack_from("<II", data, len(data) - 20)
    if not size or size > len(data) - 20:
        raise ValueError("invalid bootconfig size")
    raw = data[-20-size:-20]
    if sum(raw) & 0xffffffff != checksum:
        raise ValueError("invalid bootconfig checksum")
    old_guid, new_guid = str(uuid.UUID(old_guid)), str(uuid.UUID(new_guid))
    if old_guid == new_guid:
        raise ValueError("boot UUID was not regenerated")
    lines = raw.splitlines(keepends=True)
    prefix = b"androidboot.boot_part_uuid="
    found = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    if len(found) != 1 or lines[found[0]] != prefix + old_guid.encode("ascii") + b"\n":
        raise ValueError("bootconfig does not uniquely match original GPT")
    lines[found[0]] = prefix + new_guid.encode("ascii") + b"\n"
    result = b"".join(lines)
    return data[:-20-size] + result + struct.pack("<II", len(result), sum(result) & 0xffffffff) + MAGIC


def validate_entries(debug, quiet, extra, never, within_limit, cmdline_len):
    """Validate both emitted entries, after all profile/EC transformations."""
    pair = {"cros_ec", "cros_ec_lpcs"}
    required = set(extra) | set(never) | {"cros_ec_dev", "cros_ec_keyb", "cros_kbd_led_backlight"}
    for options in (debug, quiet):
        if len(options) != len(set(options)) or not within_limit(cmdline_len(options)):
            raise ValueError("USB smoke command line duplicate or exceeds limit")
        bl = [x for x in options if x.startswith("module_blacklist=")]
        init = [x for x in options if x.startswith("initcall_blacklist=")]
        if len(bl) != 1 or len(init) != 1:
            raise ValueError("USB smoke blacklist count")
        modules = bl[0].split("=", 1)[1].split(",")
        if (len(modules) != len(set(modules)) or not all(modules)
                or pair & set(modules) or not required <= set(modules)
                or "nvme_init" not in init[0].split("=", 1)[1].split(",")
                or "efi=noruntime" not in options):
            raise ValueError("USB smoke drain/NVMe/firmware isolation mismatch")
    if not any(o.startswith("earlycon=") for o in debug) or not {"keep_bootcon", "printk.devkmsg=on"} <= set(debug):
        raise ValueError("USB smoke debug serial protections missing")
    if any(o.startswith("earlycon=") for o in quiet):
        raise ValueError("USB smoke quiet entry must have no earlycon")
