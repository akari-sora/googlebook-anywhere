#!/usr/bin/env python3
"""Offline checks for the AS-IS boot path. No QEMU, no root, no disk."""
import hashlib
import sys
import tempfile
import textwrap
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asis_boot  # noqa: E402
import gptlib  # noqa: E402
import produced_menu  # noqa: E402

HERE = Path(__file__).resolve().parent
QEMU = HERE / "qemu-install.py"
ASIS = HERE / "asis_boot.py"
MARKER = "# Produced stick: keep gbos-install.conf, gbos-update.conf, and loader.conf.\n"


def fail(message):
    print(f"asis-gate: {message}", file=sys.stderr)
    sys.exit(1)


def stick_body(src):
    start = src.find("def stick_drive(")
    end = src.find("\ndef ", start + 1)
    if start < 0 or end < 0:
        fail("stick_drive is missing")
    return src[start:end]


def require_snapshot(src):
    if "snapshot=on" not in stick_body(src):
        raise SystemExit("stick drive lost snapshot=on")
    if "point_copy_at" in src or "mcopy" in src or "stick_default" in src:
        raise SystemExit("asis boot rewrites a menu")


def require_split(src):
    asis_at = src.find("\nif ASIS:\n")
    serial_at = src.find('stick_default("gbos-install-nolockdown.conf")')
    if asis_at < 0 or serial_at < 0 or asis_at > serial_at:
        fail("asis exit is not before the serial scenarios")
    window = src[asis_at:serial_at]
    if "asis_boot.run(" not in window or "sys.exit(" not in window:
        fail("asis block does not stop before the serial suite")
    if src.count("produced_menu.stage_produced(") != 1:
        fail("stage_produced is no longer the one serial path")
    if src.count("produced_menu.read_delivered(") != 1:
        fail("asis does not read the delivered menu")
    arm = src.split(MARKER, 1)[1].split("else:", 1)[0]
    if "mcopy" in arm or "mdel" in arm or "stage_produced(" not in arm or "read_delivered(" not in arm:
        fail("produced arm rewrites the menu or dropped a path")
    if "boot copy sha256" not in src or "BOOT_COPY_SHA" not in src:
        fail("the boot copy itself is not hashed")


def paint(width, height, ox, oy, tw, th, bits):
    raw = bytearray(width * height * 3)
    for y in range(th):
        for x in range(tw):
            if bits[y * tw + x]:
                i = ((oy + y) * width + ox + x) * 3
                raw[i:i + 3] = b"\xff\xff\xff"
    return f"P6\n{width} {height}\n255\n".encode() + bytes(raw)


def glyph(pattern):
    return [[1 if ch == "#" else 0 for ch in row] for row in pattern]


def paint_cells(font, text, cols):
    """Paint text the way fbcon wraps: column 0 of the next row."""
    gw, gh = font["width"], font["height"]
    rows = (len(text) + cols - 1) // cols
    sw, sh = cols * gw, rows * gh
    raw = bytearray(sw * sh * 3)
    for i, ch in enumerate(text):
        bits = font["glyphs"][font["map"][ord(ch)]]
        row, col = divmod(i, cols)
        ox, oy = col * gw, row * gh
        for y in range(gh):
            for x in range(gw):
                if bits[y * gw + x]:
                    p = ((oy + y) * sw + ox + x) * 3
                    raw[p:p + 3] = b"\xff\xff\xff"
    return f"P6\n{sw} {sh}\n255\n".encode() + bytes(raw)


def grid_font(text):
    seen = list(dict.fromkeys(text))
    glyphs = []
    mapping = {}
    for i, ch in enumerate(seen):
        rows = []
        for y in range(8):
            bits = []
            for x in range(8):
                on = y == 7 or (((i + 1) >> x) & 1 and y == 0)
                bits.append("#" if on else ".")
            rows.append("".join(bits))
        glyphs.append(glyph(rows))
        mapping[ord(ch)] = i
    return asis_boot.load_font(asis_boot.pack_psf(8, 8, glyphs, mapping))


def func_body(src, name):
    token = f"def {name}("
    start = src.find("\n" + token)
    if start < 0:
        raise SystemExit(f"{name} is missing")
    start += 1
    end = start
    lines = src[start:].splitlines(keepends=True)
    for i, line in enumerate(lines):
        if i and (line.startswith("def ") or (line.strip() and not line.startswith((" ", "\t", "#")))):
            break
        end += len(line)
    return src[start:end]


def require_update_negative(src):
    """(d) must see an Install confirmation that is still on the update screen.

    `install_kind not in seen` is true whenever that phrase is never scanned.
    """
    phased = func_body(src, "_phased")
    run = func_body(src, "run")
    scan = "list(watch) + list(later) + list(record)"
    call = '["UPDATE OK", "Press Enter to reboot"], INSTALL_SEC, record=(install_kind,))'
    if scan not in phased or "attempt=2, record=record" not in phased:
        raise SystemExit("the update boot does not record the install confirmation")
    if call not in run or 'install_kind not in updated["seen"]' not in run:
        raise SystemExit("update acceptance ignores the install confirmation line")


def require_console_phrases(install_src):
    """The phrases the screen matcher waits for are printed on the console, not only into the log file."""
    log_fn = 'log() { printf \'%s\\n\' "$*"; }'
    tty = 'exec < "/dev/$TTY" > "/dev/$TTY" 2>&1'
    tee = 'exec > >(tee -a "$LOG") 2>&1'
    tty_at = install_src.find(tty)
    tee_at = install_src.find(tee)
    if log_fn not in install_src:
        raise SystemExit("installer messages are not printed to the console")
    if tty_at < 0 or tee_at < tty_at:
        raise SystemExit("installer stdout is not the console")
    for phrase in (
        "Cancelled. Nothing was written.",
        'log "INSTALL OK:',
        "to install, or just Enter to cancel",
        "to update, or just Enter to cancel",
        'read -r -p "Press Enter to $2. " _',
    ):
        if phrase not in install_src:
            raise SystemExit(f"the console phrase is gone: {phrase}")


def require_update_on_console(install_src, update_src):
    """(d) stops when UPDATE OK is on screen. The engine must be printing it, after the flush."""
    apply = 'if python3 -I "$ENGINE" apply '
    if apply not in install_src or '$(python3 -I "$ENGINE" apply' in install_src:
        raise SystemExit("the updater's output is captured off the console")
    if "def say(*a):\n    print(*a, flush=True)" not in update_src:
        raise SystemExit("UPDATE OK is not flushed to the console")
    body = func_body(update_src, "apply")
    comment = "# the ESP was written through mtools"
    ok = 'say(f"UPDATE OK:'
    comment_at = body.find(comment)
    ok_at = body.find(ok)
    window = body[max(0, comment_at - 80):comment_at]
    if comment_at < 0 or ok_at < comment_at or "disk.sync_drop()" not in window:
        raise SystemExit("UPDATE OK is printed before the disk flush")


def require_keyboard(src):
    """Menu keys and the serial digits are QMP send-key. The serial port is only a firmware log."""
    send = 'self.cmd("send-key", {"keys": [{"type": "qcode", "data": key}]})'
    if src.count(send) != 1:
        raise SystemExit("keys are not QMP send-key")
    serial = '"-serial", f"file:{serial_log}"'
    for name in ("boot_case", "_phased"):
        body = func_body(src, name)
        if "qmp.send(menu_keys)" not in body or serial not in body or "mon:stdio" in body:
            raise SystemExit("the menu is not driven from the console keyboard")
    if "qmp.send(qcodes(reply))" not in func_body(src, "_phased"):
        raise SystemExit("the serial digits are not typed with send-key")


def require_idle_no_keys(src):
    """(a) is the default Install entry with no key. An empty list must stay empty, and an empty list must not send."""
    call = 'boot_case(ctx, "asis-idle", blank, [], [install_prompt, install_kind], PROMPT_SEC)'
    if call not in func_body(src, "run"):
        raise SystemExit("the idle boot sends a key")
    guard = "if menu_keys:\n                time.sleep(MENU_DELAY)\n                qmp.send(menu_keys)"
    if guard not in func_body(src, "boot_case"):
        raise SystemExit("the idle boot sends a key even when no key was asked for")


def require_menu_targets(src):
    """(b) and (c) stay on the default Install entry. (d) sends the counted Up presses."""
    typed = "return _phased(ctx, tag, nvme, [], watch, reply, later, seconds)"
    if typed not in func_body(src, "_type_after_prompt"):
        raise SystemExit("the install cases leave the default menu entry")
    run = func_body(src, "run")
    if 'ups = ["up"] * ctx["update_ups"] + ["ret"]' not in run:
        raise SystemExit("the update boot does not count its Up presses")
    if 'ctx, "asis-update", blank, ups,' not in run:
        raise SystemExit("the update boot does not send those Up presses")


def require_wrong_digits(src, serial):
    """(b) types four digits that are not this disk's last four."""
    if serial[-4:].upper() == "9999":
        raise SystemExit("the wrong digits are the real serial")
    call = '_type_after_prompt(ctx, "asis-wrong", blank, [install_prompt, install_kind], "9999\\r",'
    if call not in func_body(src, "run"):
        raise SystemExit("the wrong-digit boot does not send 9999")


def require_right_digits(src):
    """(c) and (d) type this disk's last four, not a fixed stand-in."""
    run = func_body(src, "run")
    install = (
        '_type_after_prompt(\n'
        '        ctx, "asis-install", blank, [install_prompt, install_kind], serial[-4:] + "\\r",'
    )
    update = 'ctx, "asis-update", blank, ups, [install_prompt, update_kind], serial[-4:] + "\\r",'
    if install not in run:
        raise SystemExit("the install boot does not type this disk's last 4")
    if update not in run:
        raise SystemExit("the update boot does not type this disk's last 4")


def require_nvme_counters(src):
    """No-write checks read this NVMe's blockstats. A made-up zero does not count."""
    start = src.find("\n    def nvme(self):")
    if start < 0:
        raise SystemExit("the no-write check does not query the disk")
    lines = src[start + 1:].splitlines(keepends=True)
    body = lines[0]
    for line in lines[1:]:
        if line.startswith("    def "):
            break
        body += line
    if 'self.cmd("query-blockstats")' not in body:
        raise SystemExit("the no-write check does not query the disk")
    if 'd.get("device") == "nv"' not in body:
        raise SystemExit("the no-write check does not read the NVMe device")
    for name in ("boot_case", "_phased"):
        part = func_body(src, name)
        if part.count('stats["prompt"] = qmp.nvme()') != 1:
            raise SystemExit(f"{name} does not record the NVMe counters at the prompt")
        if part.count('stats["end"] = qmp.nvme()') != 1:
            raise SystemExit(f"{name} does not record the NVMe counters at the end")


def require_idle_wait(src):
    """(a) records the end counters only after the idle interval."""
    if 'os.environ.get("GBOS_ASIS_IDLE_SEC", "180")' not in src:
        raise SystemExit("the idle wait is not 180 seconds")
    body = func_body(src, "boot_case")
    wait = (
        'if "prompt" in stats and IDLE_SEC and tag.endswith("-idle"):\n'
        "                time.sleep(IDLE_SEC)\n"
    )
    end_at = body.find('stats["end"] = qmp.nvme()')
    wait_at = body.find(wait)
    if wait_at < 0 or end_at < 0 or wait_at > end_at:
        raise SystemExit("the idle boot records the end counters without waiting")


def require_snapshot_before_keys(src):
    """The no-write snapshot is taken before the confirmation keys."""
    body = func_body(src, "_phased")
    order = (
        "                absorb(qmp)\n"
        '                if not typed and all(p in seen for p in watch) and "prompt" in stats:\n'
        "                    qmp.send(qcodes(reply))\n"
    )
    if order not in body:
        raise SystemExit("the confirmation keys are sent before the no-write snapshot")


def require_end_quiet(src):
    """(a) and (b) read the counters taken after the wait, not only the prompt snapshot."""
    body = func_body(src, "run")
    idle = '_quiet(idle["stats"].get("prompt")) and _quiet(idle["stats"].get("end"))'
    wrong = '_quiet(wrong["stats"].get("prompt")) and _quiet(wrong["stats"].get("end"))'
    if idle not in body:
        raise SystemExit("the idle boot ignores the counters after the wait")
    if wrong not in body:
        raise SystemExit("the wrong-digit boot ignores the counters after the reply")


def require_quiet_predicate(src):
    """No-write means the write and discard counters are zero. A missing sample is not quiet."""
    body = func_body(src, "_quiet")
    if 'stats.get("unmap_operations", 0)' in body or 'st.get("unmap_operations", 0)' in body:
        raise SystemExit("a missing discard counter is treated as zero")
    parts = (
        "stats is not None",
        'stats.get("wr_bytes") == 0',
        'stats.get("wr_operations") == 0',
        'stats.get("unmap_operations") == 0',
    )
    if any(part not in body for part in parts):
        raise SystemExit("the no-write check ignores a write or discard counter")
    if asis_boot._quiet({"wr_bytes": 0, "wr_operations": 0}) is not False:
        raise SystemExit("a missing discard counter counted as no discard")
    if asis_boot._quiet({"wr_bytes": 0, "wr_operations": 0, "unmap_operations": 0}) is not True:
        raise SystemExit("observed zero discard counters were rejected")
    if asis_boot._quiet({"wr_bytes": 0, "wr_operations": 0, "unmap_operations": 1}):
        raise SystemExit("a discard counted as no write")


def require_user_data(qemu_src, asis_src):
    """(d) plants non-zero data in the userdata head and at +64 MiB, and catches a head re-zero."""
    run = func_body(asis_src, "run")
    installed_call = 'ud = ctx["installed"]("asis-c", blank,'
    guard = (
        "    if not ud:\n"
        '        check("asis-d: update needs the installed NVMe from asis-c", False)\n'
        "        return\n"
    )
    baseline = '    before = ctx["update_ranges"](blank, ud, "a")\n'
    installed_at = run.find(installed_call)
    guard_at = run.find(guard)
    base_at = run.find(baseline)
    if installed_at < 0 or guard_at < 0 or base_at < 0 or not (installed_at < guard_at < base_at):
        raise SystemExit("userdata plant is not between asis-c read-back and asis-d baseline hashes")
    body = func_body(qemu_src, "preserved_ranges")
    head = '"userdata-head": (ud_first * 512, 16 << 20)'
    window = '"userdata-marker": (ud_first * 512 + (64 << 20), 1 << 20)'
    if head not in body:
        raise SystemExit("the update check does not hash the first 16 MiB of userdata")
    if window not in body:
        raise SystemExit("the update check does not hash the user-data window")
    ns = {}
    exec(body, ns)
    ud_first = 37244928
    nvme_bytes = 27927765504
    got = ns["preserved_ranges"](ud_first, nvme_bytes)
    if got.get("userdata-head") != (ud_first * 512, 16 << 20):
        raise SystemExit("userdata head window is not the installer's 16 MiB wipe")
    if got.get("userdata-marker") != (ud_first * 512 + (64 << 20), 1 << 20):
        raise SystemExit("userdata marker window moved")
    ranges = func_body(qemu_src, "update_ranges")
    if 'preserved_ranges(ud["first"], NVME_BYTES)' not in ranges:
        raise SystemExit("the update check does not use the preserved ranges")
    updated = func_body(qemu_src, "updated")
    if 'after == before["sha"]' not in updated:
        raise SystemExit("the update check does not compare the saved ranges")
    prep_code = textwrap.dedent(run[guard_at + len(guard):base_at])
    with tempfile.TemporaryDirectory(prefix="asis-ud-") as tmp:
        blank = Path(tmp) / "nvme.img"
        with open(blank, "wb") as f:
            f.truncate(nvme_bytes)
        with open(blank, "rb") as f:
            f.seek(ud_first * 512)
            if f.read(16 << 20) != bytes(16 << 20):
                raise SystemExit("freshly installed userdata head is not zero before planting")
        exec(prep_code, {"blank": blank, "ud": {"first": ud_first}, "open": open})
        before = {"ranges": got, "sha": {k: gptlib.sha(blank, a, n) for k, (a, n) in got.items()}}
        kept = {k: gptlib.sha(blank, a, n) for k, (a, n) in before["ranges"].items()}
        if kept != before["sha"]:
            raise SystemExit("unchanged userdata failed the preserved-range comparison")
        with open(blank, "r+b") as f:
            f.seek(ud_first * 512)
            f.write(bytes(16 << 20))
        after_head = {k: gptlib.sha(blank, a, n) for k, (a, n) in before["ranges"].items()}
        changed_head = [k for k in after_head if after_head[k] != before["sha"][k]]
        if after_head == before["sha"] or changed_head != ["userdata-head"]:
            raise SystemExit("a fake update that re-zeroes the userdata head still passed the preserved-range comparison")
        with open(blank, "r+b") as f:
            f.seek(ud_first * 512 + (64 << 20))
            f.write(bytes(1 << 20))
        after_marker = {k: gptlib.sha(blank, a, n) for k, (a, n) in before["ranges"].items()}
        changed_both = [k for k in after_marker if after_marker[k] != before["sha"][k]]
        if after_marker == before["sha"] or "userdata-marker" not in changed_both:
            raise SystemExit("a fake update that zeroes the userdata marker still passed the preserved-range comparison")
    plant = (
        '    with open(blank, "r+b") as f:\n'
        '        f.seek(ud["first"] * 512)\n'
        '        f.write(b"USER-DATA-HEAD--" * 4096)\n'
        '        f.seek(ud["first"] * 512 + (64 << 20))\n'
        '        f.write(b"USER-DATA-MARKER" * 4096)\n'
        '    before = ctx["update_ranges"](blank, ud, "a")\n'
    )
    if plant not in run:
        raise SystemExit("the update disk has no user-data head and marker plant")


def require_completion(src):
    """(c) and (d) fail unless the completion line and the reboot prompt were both on screen."""
    run = func_body(src, "run")
    phased = func_body(src, "_phased")
    calls = (
        'completion_ok(typed, ("INSTALL OK", "Press Enter to reboot"))',
        'completion_ok(updated, ("UPDATE OK", "Press Enter to reboot"))',
    )
    if any(call not in run for call in calls):
        raise SystemExit("as-is success does not require the completion message")
    marker = "if typed and all(p in seen for p in later):\n                    completed = True"
    if marker not in phased or "if typed and any(p in seen for p in later)" in phased:
        raise SystemExit("the boot stops before every completion line is on screen")
    finished = {
        "completed": True,
        "timed_out": False,
        "early_exit": False,
        "seen": ["INSTALL OK", "Press Enter to reboot"],
    }
    if asis_boot.completion_ok(finished, ("INSTALL OK", "Press Enter to reboot")) is not True:
        raise SystemExit("a finished install screen was rejected")
    stalled = {
        "completed": False,
        "timed_out": True,
        "early_exit": False,
        "seen": ["Type the last 4 characters", "to install, or just Enter to cancel"],
    }
    if asis_boot.completion_ok(stalled, ("INSTALL OK", "Press Enter to reboot")):
        raise SystemExit("a prompt without INSTALL OK still counted as finished")
    written = {
        "completed": True,
        "timed_out": False,
        "early_exit": False,
        "seen": ["Type the last 4 characters"],
    }
    if asis_boot.completion_ok(written, ("INSTALL OK", "Press Enter to reboot")):
        raise SystemExit("a written disk without the completion line still counted as finished")
    expired = {
        "completed": True,
        "timed_out": True,
        "early_exit": False,
        "seen": ["UPDATE OK", "Press Enter to reboot"],
    }
    if asis_boot.completion_ok(expired, ("UPDATE OK", "Press Enter to reboot")):
        raise SystemExit("a timed-out boot still counted as finished")
    early = {
        "completed": True,
        "timed_out": False,
        "early_exit": True,
        "seen": ["UPDATE OK", "Press Enter to reboot"],
    }
    if asis_boot.completion_ok(early, ("UPDATE OK", "Press Enter to reboot")):
        raise SystemExit("an early exit still counted as finished")


def require_untouched(src):
    """Each case re-hashes the boot copy and compares it with the produced image."""
    body = func_body(src, "_untouched")
    if 'now = file_sha(ctx["stick"])' not in body or 'now == ctx["boot_sha"]' not in body:
        raise SystemExit("the boot copy is not re-hashed after a case")
    run = func_body(src, "run")
    for tag in ("asis-a", "asis-b", "asis-c", "asis-d"):
        if run.count(f'_untouched(ctx, "{tag}")') != 1:
            raise SystemExit(f"the boot copy is not checked after {tag}")


def require_installed(src):
    """(c) reads the NVMe before it reports an install. Each disk check is the whole condition."""
    body = func_body(src, "installed")
    returns = tuple(line.strip() for line in body.splitlines() if "return " in line)
    expect = (
        "if nh is None: return None",
        "if not ud: return None",
        "return ud",
    )
    if returns != expect:
        raise SystemExit("the install check returns without the NVMe")
    read_at = body.find("gptlib.read_gpt(nv, backup=True)")
    none_at = body.find("if nh is None: return None")
    parts = "not bad and sorted(ng) == sorted(mparts)"
    head = "udhead == bytes(16 << 20)"
    parts_at = body.find(parts)
    head_at = body.find(head)
    if not (0 <= read_at < none_at < parts_at < head_at):
        raise SystemExit("the install check does not read the NVMe")
    for at, expr in ((parts_at, parts), (head_at, head)):
        before = body[:at].rstrip()
        after = body[at + len(expr):].lstrip()
        if before.endswith(" or") or after[:1] not in ",)":
            raise SystemExit("the install check is not the whole condition")


def require_updated_compares(src):
    """(d) compares the hashes of the saved ranges. The compare is the whole check, not one side of or."""
    body = func_body(src, "updated")
    for line in body.splitlines():
        code = line.split("#", 1)[0].strip()
        if code == "return" or code.startswith("return ") or code.startswith("return("):
            raise SystemExit("the update check returns before comparing the disk")
    assignment = 'after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}'
    hashed = body.find(assignment)
    compared = body.find('after == before["sha"]')
    if not (0 <= hashed < compared):
        raise SystemExit("the update check does not hash the saved ranges")
    for line in body[hashed + len(assignment):compared].splitlines():
        code = line.split("#", 1)[0].strip()
        rest = code[len("after"):].lstrip() if code.startswith("after") else ""
        if rest.startswith("=") and not rest.startswith("=="):
            raise SystemExit("the update check replaces the hashes before comparing")
        if rest.startswith(":"):
            eq = 0
            while True:
                found = rest.find("=", eq)
                if found < 0:
                    break
                if not rest.startswith("==", found):
                    raise SystemExit("the update check replaces the hashes before comparing")
                eq = found + 2
        for needle in ('["after"]', "['after']"):
            at = code.find(needle)
            if at < 0:
                continue
            tail = code[at + len(needle):].lstrip()
            if tail.startswith("=") and not tail.startswith("=="):
                raise SystemExit("the update check replaces the hashes before comparing")
    if not body[compared + len('after == before["sha"]'):].lstrip().startswith(","):
        raise SystemExit("the update compare is not the whole check")


def require_pre_qemu_hash(src):
    """The digest QEMU opens is the copy's own bytes, printed before the first boot."""
    body = func_body(src, "run")
    marks = (
        "now = file_sha(stick)",
        'now != ctx["boot_sha"] or now != file_sha(ctx["source"])',
        'print(f"boot copy sha256 {now}", flush=True)',
        "boot_case(",
    )
    at = [body.find(mark) for mark in marks]
    if any(i < 0 for i in at) or at != sorted(at) or len(set(at)) != len(at):
        raise SystemExit("the boot copy is not hashed and printed before QEMU")


def require_host_stick_skipped(qemu_src, asis_src):
    """AS-IS snapshot never writes the host stick. Post-install stick reads stay on the raw serial path."""
    for name, call, phrase in (
        ("installed", "stick_after_install(", "stick menu starts the internal disk by default"),
        ("updated", "stick_after_update(", "the stick's menu starts the internal disk by default"),
    ):
        body = func_body(qemu_src, name)
        helper = func_body(qemu_src, call.split("(", 1)[0])
        needle = "if not disk_only:\n        " + call
        if needle not in body or body.count(call) != 1 or phrase not in helper:
            raise SystemExit(f"{name} still reads the host stick when the boot drive is a snapshot")
        if "mtype" not in helper or "STICK" not in helper:
            raise SystemExit(f"{call} no longer reads the stick")
    if asis_src.count("disk_only=True") != 2 or "disk_only=False" in asis_src:
        raise SystemExit("asis post-install checks are not disk_only")


def main():
    payload = b"boot-copy-bytes\n"
    with tempfile.TemporaryDirectory(prefix="asis-gate-") as tmp:
        root = Path(tmp)
        source = root / "produced.img"
        copy = root / "boot.img"
        source.write_bytes(payload)
        copy.write_bytes(payload)
        if asis_boot.file_sha(copy) != hashlib.sha256(payload).hexdigest():
            fail("boot copy hash is not the copy's own bytes")
        if asis_boot.file_sha(copy) != asis_boot.file_sha(source):
            fail("identical copy did not match the produced image")
        flipped = bytearray(payload)
        flipped[0] ^= 0xFF
        copy.write_bytes(flipped)
        if asis_boot.file_sha(copy) == asis_boot.file_sha(source):
            fail("a changed boot copy still matched the produced image")
    drive = " ".join(asis_boot.stick_drive("/tmp/stick.img"))
    if "snapshot=on" not in drive or "id=stick" not in drive:
        fail("live stick drive is not a snapshot")
    qemu_src = QEMU.read_text()
    asis_src = ASIS.read_text()
    require_split(qemu_src)
    try:
        require_snapshot(asis_src)
    except SystemExit as err:
        fail(str(err))
    body = stick_body(asis_src)
    mutant = asis_src.replace(body, body.replace("snapshot=on", "snapshot=off", 1), 1)
    try:
        require_snapshot(mutant)
    except SystemExit:
        pass
    else:
        fail("removing snapshot=on did not fail the boot-drive check")
    try:
        require_host_stick_skipped(qemu_src, asis_src)
    except SystemExit as err:
        fail(str(err))
    unguarded = qemu_src.replace(
        "if not disk_only:\n        stick_after_install(",
        "stick_after_install(",
        1,
    )
    try:
        require_host_stick_skipped(unguarded, asis_src)
    except SystemExit:
        pass
    else:
        fail("an unguarded stick_after_install still passed")
    host_stick = asis_src.replace("disk_only=True", "disk_only=False", 1)
    try:
        require_host_stick_skipped(qemu_src, host_stick)
    except SystemExit:
        pass
    else:
        fail("disk_only=False still passed")
    visible = [
        "gbos-install.conf",
        "gbos-update.conf",
        "lapis-debug.conf",
        "lapis-quiet.conf",
    ]
    if asis_boot.update_ups(visible) != 1:
        fail("the visible delivered install/update menu is not 1 Up")
    menu = [
        "gbos-install.conf",
        "gbos-nvme-debug.conf",
        "gbos-nvme.conf",
        "gbos-update.conf",
        "lapis-debug.conf",
        "lapis-quiet.conf",
    ]
    if asis_boot.update_ups(menu) != 3:
        fail("the 6-entry install/update menu is not 3 Ups")
    if asis_boot.update_ups(menu + ["aaa.conf"]) != 3:
        fail("an entry below install changed the Up count")
    if asis_boot.update_ups(menu + ["gbos-kept.conf"]) != 4:
        fail("an entry between install and update did not add an Up")
    mut_src = asis_src.replace("return install_at - update_at", "return 3", 1)
    ns = {}
    exec(func_body(mut_src, "update_ups"), ns)
    if ns["update_ups"](menu + ["gbos-kept.conf"]) == 4:
        fail("hardcoding 3 still counted the extra entry")
    if '"update_ups": update_ups' not in qemu_src or "visible_entries(STICK" not in qemu_src:
        fail("asis does not pass the counted Up presses")
    unwired = qemu_src.replace('        "update_ups": update_ups,\n', "", 1)
    if '"update_ups": update_ups' in unwired:
        fail("an as-is boot without the counted index still passed")
    if "write" in produced_menu.read_delivered.__code__.co_names:
        fail("read_delivered writes")
    if "write" in produced_menu._delivered.__code__.co_names:
        fail("_delivered writes")
    if "write" not in produced_menu.stage_produced.__code__.co_names:
        fail("stage_produced no longer writes the extra serial names")
    letter_a = glyph([
        "...#....",
        "..#.#...",
        ".#...#..",
        ".#####..",
        ".#...#..",
        ".#...#..",
        ".#...#..",
        "........",
    ])
    letter_b = glyph([
        ".####...",
        ".#...#..",
        ".####...",
        ".#...#..",
        ".#...#..",
        ".####...",
        "........",
        "........",
    ])
    space = glyph(["........"] * 8)
    font = asis_boot.load_font(asis_boot.pack_psf(8, 8, [space, letter_a, letter_b], {32: 0, 65: 1, 66: 2}))
    tw, th, bits = asis_boot.render(font, "AB")
    ppm = paint(48, 32, 5, 7, tw, th, bits)
    if not asis_boot.find_on_ppm(ppm, font, "AB"):
        fail("ordinary-console text was not found")
    if asis_boot.find_on_ppm(ppm, font, "BA"):
        fail("a different phrase matched the screen")
    if asis_boot.find_on_ppm(paint(48, 32, 0, 0, 1, 1, bytearray(b"\x00")), font, "AB"):
        fail("a blank screen matched the prompt")
    wrapped_ab = paint_cells(font, "AB", 1)
    if not asis_boot.find_on_ppm(wrapped_ab, font, "AB"):
        fail("a prompt wrapped onto the next row was missed")
    if asis_boot.find_on_ppm(wrapped_ab, font, "BA"):
        fail("a wrapped row matched the wrong phrase")
    serial_at = qemu_src.find('NVME_SERIAL = "')
    if serial_at < 0:
        fail("harness serial is missing")
    serial = qemu_src[serial_at + len('NVME_SERIAL = "'):qemu_src.find('"', serial_at + len('NVME_SERIAL = "'))]
    line = f"  Type the last 4 characters of the serial ({serial}) to install, or just Enter to cancel: "
    kind = "to install, or just Enter to cancel"
    other = "to update, or just Enter to cancel"
    head = "Type the last 4 characters"
    tail_at = line.find(kind)
    if tail_at < 0 or tail_at + len(kind) <= 85:
        fail("the delivered install prompt does not wrap at 85 columns")
    prompt_font = grid_font(line + other)
    for cols in (80, 85):
        screen = paint_cells(prompt_font, line, cols)
        if not asis_boot.find_on_ppm(screen, prompt_font, head):
            fail("wrapped install prompt hid the opening words")
        if not asis_boot.find_on_ppm(screen, prompt_font, kind):
            fail("wrapped install prompt hid the install tail")
        if asis_boot.find_on_ppm(screen, prompt_font, other):
            fail("a wrapped install prompt matched the update tail")
    body = func_body(asis_src, "find_on_ppm")
    needle = (
        "        if _phrase_wraps(screen, sw, sh, font, phrase, scale):\n"
        "            return True\n"
        "        if _phrase_wraps(inverted, sw, sh, font, phrase, scale):\n"
        "            return True\n"
    )
    if needle not in body:
        fail("find_on_ppm does not search a wrapped console line")
    env = dict(asis_boot.__dict__)
    env["__builtins__"] = __builtins__
    exec(body.replace(needle, "", 1), env)
    if env["find_on_ppm"](paint_cells(prompt_font, line, 80), prompt_font, kind):
        fail("a one-row stencil still matched the wrapped install tail")
    if not asis_boot.firmware_retry(1, True, False):
        fail("a live firmware stall is recorded as the scenario")
    if (
        asis_boot.firmware_retry(2, True, False)
        or asis_boot.firmware_retry(1, False, False)
        or asis_boot.firmware_retry(1, True, True)
    ):
        fail("firmware retry is not limited to one live stall")
    call = "firmware_retry(attempt, proc.poll() is None, loaded)"

    def both_boots_retry(src):
        if src.count(call) != 2 or "return attempt == 1 and alive and not saw_load" not in src:
            raise SystemExit("both as-is boots do not retry a firmware stall")

    try:
        both_boots_retry(asis_src)
    except SystemExit as err:
        fail(str(err))
    neutered = asis_src.replace(call, "firmware_retry(9, False, True)", 1)
    try:
        both_boots_retry(neutered)
    except SystemExit:
        pass
    else:
        fail("an as-is boot without the firmware retry still passed")
    mut = func_body(asis_src, "firmware_retry").replace(
        "return attempt == 1 and alive and not saw_load",
        "return False",
        1,
    )
    ns = {}
    exec(mut, ns)
    if ns["firmware_retry"](1, True, False):
        fail("dropping the firmware retry still started another QEMU")
    try:
        require_update_negative(asis_src)
    except SystemExit as err:
        fail(str(err))
    dropped_scan = asis_src.replace(" + list(record)", "", 1)
    try:
        require_update_negative(dropped_scan)
    except SystemExit:
        pass
    else:
        fail("the update screen can pass while the install prompt is showing")
    dropped_record = asis_src.replace(", record=(install_kind,)", "", 1)
    try:
        require_update_negative(dropped_record)
    except SystemExit:
        pass
    else:
        fail("an update boot that never looks for the install line still passed")
    try:
        require_pre_qemu_hash(asis_src)
    except SystemExit as err:
        fail(str(err))
    dropped_compare = asis_src.replace(
        'now != ctx["boot_sha"] or now != file_sha(ctx["source"])',
        "False",
        1,
    )
    try:
        require_pre_qemu_hash(dropped_compare)
    except SystemExit:
        pass
    else:
        fail("a changed boot copy still reached QEMU")
    dropped_print = asis_src.replace(
        'print(f"boot copy sha256 {now}", flush=True)\n',
        "",
        1,
    )
    try:
        require_pre_qemu_hash(dropped_print)
    except SystemExit:
        pass
    else:
        fail("QEMU can start without printing the boot copy's own hash")
    install_src = (HERE.parents[1] / "install/stick/gbos-install.sh").read_text(encoding="utf-8")
    try:
        require_console_phrases(install_src)
    except SystemExit as err:
        fail(str(err))
    file_only = install_src.replace(
        '''log() { printf '%s\\n' "$*"; }''',
        '''log() { printf '%s\\n' "$*" >> "$LOG"; }''',
        1,
    )
    try:
        require_console_phrases(file_only)
    except SystemExit:
        pass
    else:
        fail("a log that skips the console still counted as on screen")
    no_tty = install_src.replace('exec < "/dev/$TTY" > "/dev/$TTY" 2>&1\n', "", 1)
    try:
        require_console_phrases(no_tty)
    except SystemExit:
        pass
    else:
        fail("installer text still counted as on screen without the tty")
    update_src = (HERE.parents[1] / "install/gbos-update.py").read_text(encoding="utf-8")
    try:
        require_update_on_console(install_src, update_src)
    except SystemExit as err:
        fail(str(err))
    captured = install_src.replace(
        'if python3 -I "$ENGINE" apply ',
        'if out=$(python3 -I "$ENGINE" apply ',
        1,
    )
    try:
        require_update_on_console(captured, update_src)
    except SystemExit:
        pass
    else:
        fail("a captured updater still counted as on screen")
    unflushed = update_src.replace("print(*a, flush=True)", "print(*a)", 1)
    try:
        require_update_on_console(install_src, unflushed)
    except SystemExit:
        pass
    else:
        fail("an unflushed UPDATE OK still counted as on screen")
    early = update_src.replace("# the ESP was written through mtools", "# flush removed", 1)
    try:
        require_update_on_console(install_src, early)
    except SystemExit:
        pass
    else:
        fail("UPDATE OK before the disk flush still stopped the update boot")
    try:
        require_keyboard(asis_src)
    except SystemExit as err:
        fail(str(err))
    no_key = asis_src.replace(
        'self.cmd("send-key", {"keys": [{"type": "qcode", "data": key}]})',
        'self.cmd("screendump", {"filename": "keys"})',
        1,
    )
    try:
        require_keyboard(no_key)
    except SystemExit:
        pass
    else:
        fail("typed digits still counted without send-key")
    serial_keys = asis_src.replace("qmp.send(qcodes(reply))", "serial_log.write_text(reply)", 1)
    try:
        require_keyboard(serial_keys)
    except SystemExit:
        pass
    else:
        fail("a serial write still counted as the console keyboard")
    serial_console = asis_src.replace('"-serial", f"file:{serial_log}"', '"-serial", "mon:stdio"', 1)
    try:
        require_keyboard(serial_console)
    except SystemExit:
        pass
    else:
        fail("a serial console still counted as the keyboard")
    try:
        require_idle_no_keys(asis_src)
    except SystemExit as err:
        fail(str(err))
    keyed = asis_src.replace('asis-idle", blank, []', 'asis-idle", blank, ["ret"]', 1)
    try:
        require_idle_no_keys(keyed)
    except SystemExit:
        pass
    else:
        fail("the idle boot still passed after a key was sent")
    always = asis_src.replace(
        "if menu_keys:\n                time.sleep(MENU_DELAY)\n                qmp.send(menu_keys)",
        "if True:\n                time.sleep(MENU_DELAY)\n                qmp.send(menu_keys)",
        1,
    )
    try:
        require_idle_no_keys(always)
    except SystemExit:
        pass
    else:
        fail("an idle boot that always sends a key still passed")
    try:
        require_menu_targets(asis_src)
    except SystemExit as err:
        fail(str(err))
    left = asis_src.replace(
        "return _phased(ctx, tag, nvme, [], watch, reply, later, seconds)",
        'return _phased(ctx, tag, nvme, ["down"], watch, reply, later, seconds)',
        1,
    )
    try:
        require_menu_targets(left)
    except SystemExit:
        pass
    else:
        fail("the install cases still passed after leaving the default entry")
    no_up = asis_src.replace('ctx, "asis-update", blank, ups,', 'ctx, "asis-update", blank, [],', 1)
    try:
        require_menu_targets(no_up)
    except SystemExit:
        pass
    else:
        fail("the update boot still passed without its Up presses")
    try:
        require_wrong_digits(asis_src, serial)
    except SystemExit as err:
        fail(str(err))
    right = asis_src.replace('"9999\\r"', 'serial[-4:] + "\\r"', 1)
    try:
        require_wrong_digits(right, serial)
    except SystemExit:
        pass
    else:
        fail("the right serial still counted as a wrong digit")
    own = asis_src.replace('"9999\\r"', '"' + serial[-4:] + '\\r"', 1)
    try:
        require_wrong_digits(own, serial)
    except SystemExit:
        pass
    else:
        fail("the disk's own last 4 still counted as a wrong digit")
    try:
        require_right_digits(asis_src)
    except SystemExit as err:
        fail(str(err))
    fixed = asis_src.replace(
        'ctx, "asis-install", blank, [install_prompt, install_kind], serial[-4:] + "\\r",',
        'ctx, "asis-install", blank, [install_prompt, install_kind], "9999\\r",',
        1,
    )
    try:
        require_right_digits(fixed)
    except SystemExit:
        pass
    else:
        fail("a fixed 9999 still counted as this disk's install serial")
    kept = asis_src.replace(
        'ctx, "asis-update", blank, ups, [install_prompt, update_kind], serial[-4:] + "\\r",',
        'ctx, "asis-update", blank, ups, [install_prompt, update_kind], "0000\\r",',
        1,
    )
    try:
        require_right_digits(kept)
    except SystemExit:
        pass
    else:
        fail("a fixed 0000 still counted as this disk's update serial")
    try:
        require_nvme_counters(asis_src)
    except SystemExit as err:
        fail(str(err))
    invented = asis_src.replace(
        'return next((d["stats"] for d in stats if d.get("device") == "nv"), None)',
        'return {"wr_bytes": 0, "wr_operations": 0, "unmap_operations": 0}',
        1,
    )
    try:
        require_nvme_counters(invented)
    except SystemExit:
        pass
    else:
        fail("a zero counter still counted as the NVMe")
    made_up = asis_src.replace(
        'stats["prompt"] = qmp.nvme()',
        'stats["prompt"] = {"wr_bytes": 0, "wr_operations": 0}',
        1,
    )
    try:
        require_nvme_counters(made_up)
    except SystemExit:
        pass
    else:
        fail("a made-up prompt counter still counted as the disk")
    try:
        require_idle_wait(asis_src)
    except SystemExit as err:
        fail(str(err))
    skipped = asis_src.replace("                time.sleep(IDLE_SEC)\n", "", 1)
    try:
        require_idle_wait(skipped)
    except SystemExit:
        pass
    else:
        fail("an idle boot that skips the wait still passed")
    zero = asis_src.replace(
        'os.environ.get("GBOS_ASIS_IDLE_SEC", "180")',
        'os.environ.get("GBOS_ASIS_IDLE_SEC", "0")',
        1,
    )
    try:
        require_idle_wait(zero)
    except SystemExit:
        pass
    else:
        fail("a zero idle wait still counted as the no-write interval")
    try:
        require_snapshot_before_keys(asis_src)
    except SystemExit as err:
        fail(str(err))
    early = (
        "                absorb(qmp)\n"
        '                if not typed and all(p in seen for p in watch) and "prompt" in stats:\n'
        "                    qmp.send(qcodes(reply))\n"
    )
    late = (
        '                if not typed and all(p in seen for p in watch) and "prompt" in stats:\n'
        "                    qmp.send(qcodes(reply))\n"
        "                absorb(qmp)\n"
    )
    swapped = asis_src.replace(early, late, 1)
    try:
        require_snapshot_before_keys(swapped)
    except SystemExit:
        pass
    else:
        fail("keys sent before the snapshot still counted as no write yet")
    try:
        require_end_quiet(asis_src)
    except SystemExit as err:
        fail(str(err))
    idle_only = asis_src.replace(
        ' and _quiet(idle["stats"].get("end"))',
        "",
        1,
    )
    try:
        require_end_quiet(idle_only)
    except SystemExit:
        pass
    else:
        fail("an idle boot that skips the end counters still passed")
    wrong_only = asis_src.replace(
        ' and _quiet(wrong["stats"].get("end"))',
        "",
        1,
    )
    try:
        require_end_quiet(wrong_only)
    except SystemExit:
        pass
    else:
        fail("a wrong-digit boot that skips the end counters still passed")
    try:
        require_quiet_predicate(asis_src)
    except SystemExit as err:
        fail(str(err))
    defaulted = asis_src.replace(
        'stats.get("unmap_operations") == 0',
        'stats.get("unmap_operations", 0) == 0',
        1,
    )
    try:
        require_quiet_predicate(defaulted)
    except SystemExit:
        pass
    else:
        fail("a missing discard counter still counted as no discard")
    serial_quiet = func_body(qemu_src, "no_writes")
    if 'st.get("unmap_operations", 0)' in serial_quiet or 'st.get("unmap_operations") == 0' not in serial_quiet:
        fail("the serial suite treats a missing discard counter as no discard")
    ns = {}
    exec(serial_quiet, ns)
    if ns["no_writes"]({"wr_bytes": 0, "wr_operations": 0}) is not False:
        fail("the serial suite treats a missing discard counter as no discard")
    if ns["no_writes"]({"wr_bytes": 0, "wr_operations": 0, "unmap_operations": 0}) is not True:
        fail("the serial suite rejected observed zero discard counters")
    for old, message in (
        ('stats.get("wr_bytes") == 0', "a non-zero write byte count still counted as no write"),
        ('stats.get("wr_operations") == 0', "a non-zero write operation count still counted as no write"),
        ('stats.get("unmap_operations") == 0', "a discard still counted as no write"),
    ):
        mutant = asis_src.replace(old, "True", 1)
        try:
            require_quiet_predicate(mutant)
        except SystemExit:
            pass
        else:
            fail(message)
    try:
        require_completion(asis_src)
    except SystemExit as err:
        fail(str(err))
    dropped_finish = asis_src.replace(
        'completion_ok(typed, ("INSTALL OK", "Press Enter to reboot"))',
        "True",
        1,
    )
    try:
        require_completion(dropped_finish)
    except SystemExit:
        pass
    else:
        fail("an install without INSTALL OK still passed")
    dropped_update = asis_src.replace(
        'completion_ok(updated, ("UPDATE OK", "Press Enter to reboot"))',
        "True",
        1,
    )
    try:
        require_completion(dropped_update)
    except SystemExit:
        pass
    else:
        fail("an update without UPDATE OK still passed")
    any_later = asis_src.replace(
        "if typed and all(p in seen for p in later):\n                    completed = True",
        "if typed and any(p in seen for p in later):\n                    completed = True",
        1,
    )
    try:
        require_completion(any_later)
    except SystemExit:
        pass
    else:
        fail("stopping on the first completion line still passed")
    try:
        require_user_data(qemu_src, asis_src)
    except SystemExit as err:
        fail(str(err))
    no_head_plant = asis_src.replace(
        '        f.seek(ud["first"] * 512)\n        f.write(b"USER-DATA-HEAD--" * 4096)\n',
        "",
        1,
    )
    try:
        require_user_data(qemu_src, no_head_plant)
    except SystemExit as err:
        if "re-zeroes the userdata head" not in str(err):
            fail(f"missing head plant failed for the wrong reason: {err}")
    else:
        fail("an update disk without a non-zero userdata head plant still passed")
    zero_head_plant = asis_src.replace(
        'b"USER-DATA-HEAD--" * 4096',
        'b"\\0" * 65536',
        1,
    )
    try:
        require_user_data(qemu_src, zero_head_plant)
    except SystemExit as err:
        if "re-zeroes the userdata head" not in str(err):
            fail(f"zero head plant failed for the wrong reason: {err}")
    else:
        fail("a zero userdata head plant still counted as non-zero user data")
    blank_data = asis_src.replace(
        'b"USER-DATA-MARKER" * 4096',
        'b"\\0" * 65536',
        1,
    )
    try:
        require_user_data(qemu_src, blank_data)
    except SystemExit:
        pass
    else:
        fail("a blank userdata window still counted as kept user data")
    skipped = qemu_src.replace(
        '"userdata-marker": (ud_first * 512 + (64 << 20), 1 << 20)',
        '"head": (0, 1 << 20)',
        1,
    )
    try:
        require_user_data(skipped, asis_src)
    except SystemExit:
        pass
    else:
        fail("an update that skips the user-data window still passed")
    dropped_head = qemu_src.replace(
        '"userdata-head": (ud_first * 512, 16 << 20),\n',
        "",
        1,
    )
    try:
        require_user_data(dropped_head, asis_src)
    except SystemExit:
        pass
    else:
        fail("an update that skips the userdata head still passed")
    ignored = qemu_src.replace('after == before["sha"]', "True", 1)
    try:
        require_user_data(ignored, asis_src)
    except SystemExit:
        pass
    else:
        fail("a changed user-data window still counted as kept")
    try:
        require_untouched(asis_src)
    except SystemExit as err:
        fail(str(err))
    for tag in ("asis-a", "asis-b", "asis-c", "asis-d"):
        dropped = asis_src.replace(f'_untouched(ctx, "{tag}")', "None", 1)
        try:
            require_untouched(dropped)
        except SystemExit:
            pass
        else:
            fail(f"skipping the boot-copy check after {tag} still passed")
    ignored = asis_src.replace('now == ctx["boot_sha"]', "True", 1)
    try:
        require_untouched(ignored)
    except SystemExit:
        pass
    else:
        fail("a rewritten boot copy still counted as untouched")
    try:
        require_installed(qemu_src)
    except SystemExit as err:
        fail(str(err))
    missing = qemu_src.replace(
        "if nh is None: return None",
        'if nh is None: return {"first": 0}',
        1,
    )
    try:
        require_installed(missing)
    except SystemExit:
        pass
    else:
        fail("a missing GPT still counted as installed")
    early = qemu_src.replace(
        "def installed(tag, nv, r, disk_only=False):\n",
        "def installed(tag, nv, r, disk_only=False):\n    return {\"first\": 0}\n",
        1,
    )
    try:
        require_installed(early)
    except SystemExit:
        pass
    else:
        fail("an install check that returns before the NVMe still passed")
    loose = qemu_src.replace("not bad and sorted(ng) == sorted(mparts)", "True", 1)
    try:
        require_installed(loose)
    except SystemExit:
        pass
    else:
        fail("a partition mismatch still counted as installed")
    full = qemu_src.replace("udhead == bytes(16 << 20)", "True", 1)
    try:
        require_installed(full)
    except SystemExit:
        pass
    else:
        fail("a userdata head that was not wiped still counted as installed")
    for label, old, new in (
        ("a partition check joined with True or still passed",
         "not bad and sorted(ng) == sorted(mparts)",
         "True or not bad and sorted(ng) == sorted(mparts)"),
        ("a userdata-head check joined with True or still passed",
         "udhead == bytes(16 << 20)",
         "True or udhead == bytes(16 << 20)"),
        ("a partition check joined with or True still passed",
         "not bad and sorted(ng) == sorted(mparts)",
         "not bad and sorted(ng) == sorted(mparts) or True"),
        ("a userdata-head check joined with or True still passed",
         "udhead == bytes(16 << 20)",
         "udhead == bytes(16 << 20) or True"),
    ):
        joined = qemu_src.replace(old, new, 1)
        try:
            require_installed(joined)
        except SystemExit:
            pass
        else:
            fail(label)
    try:
        require_updated_compares(qemu_src)
    except SystemExit as err:
        fail(str(err))
    early_update = qemu_src.replace(
        'def updated(tag, nv, r, before, t="b", c="a", c_initrd="/initrd.img", disk_only=False):\n',
        'def updated(tag, nv, r, before, t="b", c="a", c_initrd="/initrd.img", disk_only=False):\n    return\n',
        1,
    )
    try:
        require_updated_compares(early_update)
    except SystemExit:
        pass
    else:
        fail("an update check that returns before the disk still passed")
    copied = qemu_src.replace(
        'after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}',
        'after = before["sha"]',
        1,
    )
    try:
        require_updated_compares(copied)
    except SystemExit:
        pass
    else:
        fail("comparing the saved hashes to themselves still passed")
    stacked = qemu_src.replace(
        'after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}\n',
        'after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}\n    after = before["sha"]\n',
        1,
    )
    try:
        require_updated_compares(stacked)
    except SystemExit:
        pass
    else:
        fail("hashes computed and then discarded still passed")
    tight = qemu_src.replace(
        'after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}\n',
        'after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}\n    after=before["sha"]\n',
        1,
    )
    try:
        require_updated_compares(tight)
    except SystemExit:
        pass
    else:
        fail("an assignment without a space still replaced the hashes")
    joined = qemu_src.replace(
        'after == before["sha"]',
        'after == before["sha"] or True',
        1,
    )
    try:
        require_updated_compares(joined)
    except SystemExit:
        pass
    else:
        fail("a compare joined with or True still passed")
    noted = qemu_src.replace(
        'after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}\n',
        'after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}\n    after: dict = before["sha"]\n',
        1,
    )
    try:
        require_updated_compares(noted)
    except SystemExit:
        pass
    else:
        fail("an annotated assignment still replaced the hashes")
    indirect = qemu_src.replace(
        'after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}\n',
        'after = {k: gptlib.sha(nv, a, n) for k, (a, n) in before["ranges"].items()}\n    globals()["after"] = before["sha"]\n',
        1,
    )
    try:
        require_updated_compares(indirect)
    except SystemExit:
        pass
    else:
        fail("a subscript assignment still replaced the hashes")
    print("asis-gate: clean")


if __name__ == "__main__":
    main()
