#!/usr/bin/env python3
"""AS-IS boot of a produced installer stick.

The boot copy is hashed itself and must match the produced image before QEMU.
The stick drive uses snapshot=on, so guest writes do not land in that file.
Nothing in this module rewrites an entry, a cmdline, or loader.conf.
The delivered Install entry is the default. Keys go through QMP send-key to
the ordinary console (tty0 / tty1), not through a serial-only menu entry.
"""
import gzip
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path

PSF2_MAGIC = b"\x72\xb5\x4a\x86"
IDLE_SEC = int(os.environ.get("GBOS_ASIS_IDLE_SEC", "180"))
MENU_DELAY = float(os.environ.get("GBOS_ASIS_MENU_DELAY", "2"))
PROMPT_SEC = int(os.environ.get("GBOS_ASIS_PROMPT_SEC", "1800"))
INSTALL_SEC = int(os.environ.get("GBOS_ASIS_INSTALL_SEC", "3600"))
POLL_SEC = float(os.environ.get("GBOS_ASIS_POLL_SEC", "2"))


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(16 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stick_drive(path):
    spec = f"file={path},if=none,id=stick,format=raw,snapshot=on"
    return ["-drive", spec, "-device", "usb-storage,bus=xhci.0,drive=stick,bootindex=1"]


def nvme_drive(path, serial):
    return [
        "-drive", f"file={path},if=none,id=nv,format=raw",
        "-device", f"nvme,drive=nv,serial={serial},bus=pcie.0,addr=0x2",
    ]


def _utf8(blob, i):
    b0 = blob[i]
    if b0 < 0x80:
        return b0, i + 1
    if b0 & 0xE0 == 0xC0:
        return ((b0 & 0x1F) << 6) | (blob[i + 1] & 0x3F), i + 2
    if b0 & 0xF0 == 0xE0:
        return ((b0 & 0x0F) << 12) | ((blob[i + 1] & 0x3F) << 6) | (blob[i + 2] & 0x3F), i + 3
    return (
        ((b0 & 0x07) << 18)
        | ((blob[i + 1] & 0x3F) << 12)
        | ((blob[i + 2] & 0x3F) << 6)
        | (blob[i + 3] & 0x3F),
        i + 4,
    )


def pack_psf(width, height, glyphs, unicode_map):
    """glyphs are lists of row bit-lists. unicode_map maps a codepoint to a glyph index."""
    row_bytes = (width + 7) // 8
    glyph_size = row_bytes * height
    raw_glyphs = []
    for rows in glyphs:
        buf = bytearray(glyph_size)
        for y, row in enumerate(rows):
            for x, bit in enumerate(row):
                if bit:
                    buf[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
        raw_glyphs.append(bytes(buf))
    blob = bytearray()
    by_glyph = {}
    for cp, gi in unicode_map.items():
        by_glyph.setdefault(gi, []).append(cp)
    for gi in range(len(glyphs)):
        for cp in by_glyph.get(gi, []):
            blob += chr(cp).encode("utf-8")
        blob.append(0xFF)
    header = bytearray(32)
    header[0:4] = PSF2_MAGIC
    struct.pack_into("<IIIIII", header, 8, 32, 1, len(glyphs), glyph_size, height, width)
    return bytes(header) + b"".join(raw_glyphs) + bytes(blob)


def load_font(data):
    if data[:4] != PSF2_MAGIC:
        raise ValueError("console font is not PSF2")
    header_size, flags, nglyphs, glyph_size, height, width = struct.unpack_from("<IIIIII", data, 8)
    row_bytes = (width + 7) // 8
    if row_bytes * height != glyph_size:
        raise ValueError("PSF2 glyph size does not match width and height")
    glyphs = []
    off = header_size
    for _ in range(nglyphs):
        raw = data[off:off + glyph_size]
        bits = bytearray(width * height)
        for y in range(height):
            for x in range(width):
                byte = raw[y * row_bytes + (x // 8)]
                bits[y * width + x] = 1 if byte & (0x80 >> (x % 8)) else 0
        glyphs.append(bits)
        off += glyph_size
    mapping = {}
    if flags & 1:
        blob = data[off:]
        i = 0
        for gi in range(nglyphs):
            while i < len(blob) and blob[i] != 0xFF:
                if blob[i] == 0xFE:
                    i += 1
                    while i < len(blob) and blob[i] not in (0xFE, 0xFF):
                        _cp, i = _utf8(blob, i)
                    continue
                cp, i = _utf8(blob, i)
                mapping.setdefault(cp, gi)
            if i < len(blob) and blob[i] == 0xFF:
                i += 1
    return {"width": width, "height": height, "glyphs": glyphs, "map": mapping}


def render(font, text):
    w, h = font["width"], font["height"]
    glyphs = []
    for ch in text:
        gi = font["map"].get(ord(ch))
        if gi is None:
            raise ValueError(f"font has no {ch!r}")
        glyphs.append(font["glyphs"][gi])
    tw = w * len(glyphs)
    out = bytearray(tw * h)
    for i, glyph in enumerate(glyphs):
        for y in range(h):
            out[y * tw + i * w:y * tw + (i + 1) * w] = glyph[y * w:(y + 1) * w]
    return tw, th_of(h), out


def th_of(h):
    return h


_PPM_HI = bytes(1 if b >= 128 else 0 for b in range(256))
_INV_MAP = bytes([1, 0] + [0] * 254)


def parse_ppm(data):
    if not data.startswith(b"P6"):
        raise ValueError("screendump is not a P6 PPM")
    i = 2
    tokens = []
    while len(tokens) < 3:
        while data[i] in b" \t\r\n":
            i += 1
        if data[i] == ord("#"):
            while data[i] != ord("\n"):
                i += 1
            continue
        j = i
        while data[j] not in b" \t\r\n":
            j += 1
        tokens.append(int(data[i:j]))
        i = j
    while data[i] in b" \t\r\n":
        i += 1
    width, height, _maxv = tokens
    raw = data[i:i + width * height * 3].translate(_PPM_HI)
    r, g, b = raw[0::3], raw[1::3], raw[2::3]
    if r == g == b:
        bits = bytearray(r)
    else:
        bits = bytearray(x | y | z for x, y, z in zip(r, g, b))
    return width, height, bits


def _scale(bits, tw, th, scale):
    if scale == 1:
        return tw, th, bits
    out = bytearray(tw * scale * th * scale)
    ow = tw * scale
    for y in range(th):
        for x in range(tw):
            if not bits[y * tw + x]:
                continue
            for dy in range(scale):
                row = (y * scale + dy) * ow + x * scale
                out[row:row + scale] = b"\x01" * scale
    return ow, th * scale, out


def _located(screen, sw, template, tw, th, x0, y0, x1, y1):
    ink_rows = {}
    dark_rows = {}
    key_y = 0
    key_n = -1
    for y in range(th):
        row_t = template[y * tw:(y + 1) * tw]
        xs = [x for x in range(tw) if row_t[x]]
        dxs = [x for x in range(tw) if not row_t[x]]
        ink_rows[y] = xs
        dark_rows[y] = dxs
        if len(xs) > key_n:
            key_n = len(xs)
            key_y = y
    if key_n <= 0:
        return False
    key = ink_rows[key_y]
    first = key[0]
    dark_y, dark_x = (key_y, dark_rows[key_y][0]) if dark_rows[key_y] else (None, None)
    if dark_y is None:
        for y, dxs in dark_rows.items():
            if dxs:
                dark_y, dark_x = y, dxs[0]
                break
    y_from = max(y0, 0)
    y_to = min(y1, len(screen) // sw) - th
    limit = min(x1, sw) - tw
    if limit < x0:
        return False
    for y in range(y_from, max(y_from, y_to + 1)):
        base = (y + key_y) * sw
        end_ink = base + limit + 1 + first
        pos = screen.find(1, base + x0 + first, end_ink)
        if pos < 0:
            continue
        if dark_y is not None:
            dbase = (y + dark_y) * sw
            end_dark = dbase + limit + 1 + dark_x
            if screen.find(0, dbase + x0 + dark_x, end_dark) < 0:
                continue
        x = pos - base - first
        while x <= limit:
            if not screen[base + x + first]:
                pos = screen.find(1, base + x + first, end_ink)
                if pos < 0:
                    break
                x = pos - base - first
            if dark_y is not None and screen[dbase + x + dark_x]:
                pos = screen.find(0, dbase + x + dark_x, end_dark)
                if pos < 0:
                    break
                x = pos - dbase - dark_x
                continue
            if any(not screen[base + x + kx] for kx in key):
                x += 1
                continue
            good = True
            for ty, xs in ink_rows.items():
                row = (y + ty) * sw + x
                for tx in xs:
                    if not screen[row + tx]:
                        good = False
                        break
                if not good:
                    break
                for tx in dark_rows[ty]:
                    if screen[row + tx]:
                        good = False
                        break
                if not good:
                    break
            if good:
                return True
            x += 1
    return False


def _phrase_wraps(screen, sw, sh, font, phrase, scale):
    """Match a phrase that fbcon wrapped onto column 0 of the next row.

    The installer's confirmation is one read -p line. Once the serial is in
    that line, the tail passes column 80 and column 85 (a 12px font on a
    1024-wide frame). Cells are the font grid from pixel (0, 0).
    """
    gw = font["width"] * scale
    gh = font["height"] * scale
    if gw < 1 or gh < 1 or sw < gw or sh < gh:
        return False
    cols = sw // gw
    rows = sh // gh
    glyphs = []
    for ch in phrase:
        _w, _h, bits = _scale(font["glyphs"][font["map"][ord(ch)]], font["width"], font["height"], scale)
        glyphs.append(bits)
    for row in range(rows):
        for col in range(cols):
            c, r = col, row
            ok = True
            for glyph in glyphs:
                if c >= cols:
                    c = 0
                    r += 1
                x, y = c * gw, r * gh
                if r >= rows or x + gw > sw or y + gh > sh:
                    ok = False
                    break
                for ty in range(gh):
                    src = (y + ty) * sw + x
                    base = ty * gw
                    if screen[src:src + gw] != glyph[base:base + gw]:
                        ok = False
                        break
                if not ok:
                    break
                c += 1
            if ok:
                return True
    return False


def find_on_ppm(ppm, font, phrase):
    sw, sh, screen = parse_ppm(ppm)
    tw, th, bits = render(font, phrase)
    inverted = screen.translate(_INV_MAP)
    regions = ((0, max(0, sh - 300), sw, sh), (0, 0, sw, sh))
    for scale in (1, 2):
        stw, sth, stencil = _scale(bits, tw, th, scale)
        for x0, y0, x1, y1 in regions:
            if _located(screen, sw, stencil, stw, sth, x0, y0, x1, y1):
                return True
            if _located(inverted, sw, stencil, stw, sth, x0, y0, x1, y1):
                return True
        if _phrase_wraps(screen, sw, sh, font, phrase, scale):
            return True
        if _phrase_wraps(inverted, sw, sh, font, phrase, scale):
            return True
    return any(find_on_ppm(ppm, alt, phrase) for alt in font.get("alt", ()))


def qcodes(text):
    out = []
    for ch in text:
        if ch in "\r\n":
            out.append("ret")
        elif ch == " ":
            out.append("spc")
        elif ch.isdigit() or "a" <= ch <= "z":
            out.append(ch)
        elif "A" <= ch <= "Z":
            out.append(ch.lower())
        else:
            raise ValueError(f"no qcode for {ch!r}")
    return out


class QMP:
    def __init__(self, path, proc):
        end = time.time() + 60
        while True:
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.connect(str(path))
                break
            except OSError:
                sock.close()
                if time.time() > end or proc.poll() is not None:
                    raise RuntimeError("no QMP socket")
                time.sleep(0.2)
        self.f = sock.makefile("rwb")
        self.read()
        self.cmd("qmp_capabilities")

    def read(self):
        line = self.f.readline()
        if not line:
            raise RuntimeError("QMP closed")
        return json.loads(line)

    def cmd(self, name, args=None):
        msg = {"execute": name}
        if args is not None:
            msg["arguments"] = args
        self.f.write(json.dumps(msg).encode() + b"\n")
        self.f.flush()
        while True:
            reply = self.read()
            if "return" in reply:
                return reply["return"]
            if "error" in reply:
                raise RuntimeError(reply["error"])

    def nvme(self):
        stats = self.cmd("query-blockstats")
        return next((d["stats"] for d in stats if d.get("device") == "nv"), None)

    def send(self, keys):
        for key in keys:
            self.cmd("send-key", {"keys": [{"type": "qcode", "data": key}]})
            time.sleep(0.05)

    def shot(self, path):
        self.cmd("screendump", {"filename": str(path)})
        return path.read_bytes()


def _load_console_font(work):
    path = Path(work) / "LatGrkCyr-12x22.psfu.gz"
    if not path.is_file():
        raise SystemExit(f"as-is boot needs the console font copied to {path}")
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    font = load_font(raw)
    alt = Path(work) / "cp850-8x16.psfu.gz"
    if alt.is_file():
        alt_raw = alt.read_bytes()
        if alt_raw[:2] == b"\x1f\x8b":
            alt_raw = gzip.decompress(alt_raw)
        font["alt"] = [load_font(alt_raw)]
    return font


def firmware_retry(attempt, alive, saw_load):
    """One new QEMU when OVMF never prints the boot load line and never reads the stick.

    The serial suite already treats that stall as not a scenario result. A second
    miss, or a QEMU that has already exited, is a real failure.
    """
    return attempt == 1 and alive and not saw_load


def boot_case(ctx, tag, nvme, menu_keys, need, seconds, attempt=1):
    """Boot the untouched stick. menu_keys are sent once the firmware loads the stick."""
    work = Path(ctx["work"])
    qemu = ctx["qemu"]
    code = ctx["code"]
    vars_src = ctx["vars"]
    stick = ctx["stick"]
    font = ctx["font"]
    vars_fd = work / f"{tag}-vars.fd"
    shutil.copy(vars_src, vars_fd)
    tmp = Path(tempfile.mkdtemp(prefix="gbq"))
    qmp_path = tmp / "qmp"
    serial_log = work / f"{tag}-serial.log"
    shot = work / f"{tag}.ppm"
    cmd = [
        qemu, "-machine", "q35,accel=kvm", "-cpu", "host", "-m", "8192", "-smp", "4", "-no-reboot",
        "-vga", "std", "-display", "none",
        "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,file={vars_fd}",
        "-device", "qemu-xhci,id=xhci,bus=pcie.0,addr=0x3",
        "-device", "usb-kbd,bus=xhci.0",
        "-qmp", f"unix:{qmp_path},server=on,wait=off",
        "-monitor", "none", "-nic", "none",
        "-serial", f"file:{serial_log}",
    ] + stick_drive(stick) + nvme_drive(nvme, ctx["serial"])
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    if ctx.get("qlib"):
        env["LD_LIBRARY_PATH"] = ctx["qlib"]
    print("+", " ".join(map(str, cmd)), flush=True)
    proc = subprocess.Popen(cmd, env=env)
    seen = []
    stats = {}
    loaded = False
    retry = False
    try:
        qmp = QMP(qmp_path, proc)
        deadline = time.time() + 120
        while time.time() < deadline and proc.poll() is None:
            if serial_log.is_file() and b"BdsDxe: loading" in serial_log.read_bytes():
                loaded = True
                break
            time.sleep(0.5)
        if not loaded:
            retry = firmware_retry(attempt, proc.poll() is None, loaded)
        else:
            if menu_keys:
                time.sleep(MENU_DELAY)
                qmp.send(menu_keys)
            end = time.time() + seconds
            prompted = False
            while time.time() < end and proc.poll() is None:
                try:
                    ppm = qmp.shot(shot)
                except (RuntimeError, OSError, ValueError) as err:
                    print(f"[{tag}] screendump: {err}", flush=True)
                    time.sleep(POLL_SEC)
                    continue
                for phrase in need:
                    if phrase in seen:
                        continue
                    try:
                        hit = find_on_ppm(ppm, font, phrase)
                    except ValueError as err:
                        print(f"[{tag}] font: {err}", flush=True)
                        hit = False
                    if hit:
                        seen.append(phrase)
                        print(f"[{tag}] screen {phrase!r}", flush=True)
                        if phrase.startswith("Type the last 4") and "prompt" not in stats:
                            stats["prompt"] = qmp.nvme()
                            (work / f"{tag}-prompt.ppm").write_bytes(ppm)
                            prompted = True
                if prompted and all(p in seen for p in need):
                    break
                time.sleep(POLL_SEC)
            if "prompt" in stats and IDLE_SEC and tag.endswith("-idle"):
                time.sleep(IDLE_SEC)
                try:
                    ppm = qmp.shot(shot)
                    if need[0] not in seen and find_on_ppm(ppm, font, need[0]):
                        seen.append(need[0])
                except (RuntimeError, OSError, ValueError):
                    pass
            try:
                stats["end"] = qmp.nvme()
            except (RuntimeError, OSError):
                pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    if retry:
        print(f"[{tag}] firmware produced no boot load line, retrying the boot once", flush=True)
        return boot_case(ctx, tag, nvme, menu_keys, need, seconds, attempt=2)
    if not loaded:
        return {"ok_firmware": False, "text": "", "stats": stats, "seen": seen}
    return {"ok_firmware": True, "text": "\n".join(seen), "stats": stats, "seen": seen}


def _sparse(path, size):
    path.unlink(missing_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def update_ups(names):
    """Up presses from the highlighted Install entry to Update.

    With no sort-key, systemd-boot orders entries in reverse file-name order.
    The highlighted row is the loader default, so a name that sorts below
    gbos-install.conf does not add a key, and a name that sorts above
    gbos-update.conf does not either.
    """
    ordered = sorted(set(names), reverse=True)
    try:
        install_at = ordered.index("gbos-install.conf")
        update_at = ordered.index("gbos-update.conf")
    except ValueError:
        raise SystemExit("delivered menu is missing the install or update entry")
    if install_at <= update_at:
        raise SystemExit("update entry is not above the install entry")
    return install_at - update_at


def completion_ok(result, phrases):
    """True only when every completion phrase was on screen and the boot finished."""
    if not result or not result.get("completed"):
        return False
    if result.get("timed_out") or result.get("early_exit"):
        return False
    seen = result.get("seen")
    if not isinstance(seen, (list, tuple)):
        return False
    return all(phrase in seen for phrase in phrases)


def _untouched(ctx, tag):
    now = file_sha(ctx["stick"])
    ctx["check"](f"{tag}: boot copy still matches the produced image", now == ctx["boot_sha"], now)


def run(ctx):
    stick = Path(ctx["stick"])
    now = file_sha(stick)
    if now != ctx["boot_sha"] or now != file_sha(ctx["source"]):
        raise SystemExit(f"boot copy is not the produced image ({now})")
    print(f"boot copy sha256 {now}", flush=True)
    ctx["font"] = _load_console_font(ctx["work"])
    work = Path(ctx["work"])
    serial = ctx["serial"]
    check = ctx["check"]
    blank = work / "asis-blank.img"
    _sparse(blank, ctx["nvme_bytes"])
    install_prompt = "Type the last 4 characters"
    install_kind = "to install, or just Enter to cancel"
    update_kind = "to update, or just Enter to cancel"
    idle = boot_case(ctx, "asis-idle", blank, [], [install_prompt, install_kind], PROMPT_SEC)
    check("asis-a: firmware loaded the stick", idle["ok_firmware"])
    check("asis-a: default Install prompt on the ordinary console, no key sent",
          install_prompt in idle["seen"] and install_kind in idle["seen"])
    check(f"asis-a: no NVMe write at the prompt or after {IDLE_SEC}s with no key",
          _quiet(idle["stats"].get("prompt")) and _quiet(idle["stats"].get("end")), idle["stats"])
    _untouched(ctx, "asis-a")

    wrong = _type_after_prompt(ctx, "asis-wrong", blank, [install_prompt, install_kind], "9999\r",
                               ["Cancelled. Nothing was written."], PROMPT_SEC)
    check("asis-b: wrong digits cancel on the ordinary console",
          "Cancelled. Nothing was written." in wrong["seen"] and install_kind in wrong["seen"])
    check("asis-b: no NVMe write",
          _quiet(wrong["stats"].get("prompt")) and _quiet(wrong["stats"].get("end")), wrong["stats"])
    _untouched(ctx, "asis-b")

    typed = _type_after_prompt(
        ctx, "asis-install", blank, [install_prompt, install_kind], serial[-4:] + "\r",
        ["INSTALL OK", "Press Enter to reboot"], INSTALL_SEC)
    check("asis-c: install prompt was the delivered Install entry",
          install_prompt in typed["seen"] and install_kind in typed["seen"])
    check("asis-c: install finished on the ordinary console",
          completion_ok(typed, ("INSTALL OK", "Press Enter to reboot")))
    check("asis-c: zero NVMe writes before the serial was typed",
          _quiet(typed["stats"].get("prompt")), typed["stats"].get("prompt"))
    ud = ctx["installed"]("asis-c", blank, {"text": typed["text"], "stats": typed["stats"]}, disk_only=True)
    _untouched(ctx, "asis-c")
    if not ud:
        check("asis-d: update needs the installed NVMe from asis-c", False)
        return
    with open(blank, "r+b") as f:
        f.seek(ud["first"] * 512)
        f.write(b"USER-DATA-HEAD--" * 4096)
        f.seek(ud["first"] * 512 + (64 << 20))
        f.write(b"USER-DATA-MARKER" * 4096)
    before = ctx["update_ranges"](blank, ud, "a")
    if "update_ups" not in ctx:
        raise SystemExit("as-is update menu index was not counted from the delivered entries")
    ups = ["up"] * ctx["update_ups"] + ["ret"]
    updated = _menu_then_type(
        ctx, "asis-update", blank, ups, [install_prompt, update_kind], serial[-4:] + "\r",
        ["UPDATE OK", "Press Enter to reboot"], INSTALL_SEC, record=(install_kind,))
    check("asis-d: keyboard selected the delivered Update entry",
          update_kind in updated["seen"] and install_kind not in updated["seen"])
    check("asis-d: update finished on the ordinary console",
          completion_ok(updated, ("UPDATE OK", "Press Enter to reboot")))
    check("asis-d: zero NVMe writes before the serial was typed",
          _quiet(updated["stats"].get("prompt")), updated["stats"].get("prompt"))
    ctx["updated"]("asis-d", blank, {"text": updated["text"], "stats": updated["stats"]}, before, disk_only=True)
    _untouched(ctx, "asis-d")


def _quiet(stats):
    # A missing discard counter is not zero. QMP has to report unmap_operations.
    return (
        stats is not None
        and stats.get("wr_bytes") == 0
        and stats.get("wr_operations") == 0
        and stats.get("unmap_operations") == 0
    )


def _type_after_prompt(ctx, tag, nvme, watch, reply, later, seconds):
    """Wait until the prompt is on screen, record NVMe counters, then send reply."""
    return _phased(ctx, tag, nvme, [], watch, reply, later, seconds)


def _menu_then_type(ctx, tag, nvme, menu_keys, watch, reply, later, seconds, record=()):
    return _phased(ctx, tag, nvme, menu_keys, watch, reply, later, seconds, record=record)


def _phased(ctx, tag, nvme, menu_keys, watch, reply, later, seconds, attempt=1, record=()):
    work = Path(ctx["work"])
    font = ctx["font"]
    stick = ctx["stick"]
    vars_fd = work / f"{tag}-vars.fd"
    shutil.copy(ctx["vars"], vars_fd)
    tmp = Path(tempfile.mkdtemp(prefix="gbq"))
    qmp_path = tmp / "qmp"
    serial_log = work / f"{tag}-serial.log"
    shot = work / f"{tag}.ppm"
    cmd = [
        ctx["qemu"], "-machine", "q35,accel=kvm", "-cpu", "host", "-m", "8192", "-smp", "4", "-no-reboot",
        "-vga", "std", "-display", "none",
        "-drive", f"if=pflash,format=raw,readonly=on,file={ctx['code']}",
        "-drive", f"if=pflash,format=raw,file={vars_fd}",
        "-device", "qemu-xhci,id=xhci,bus=pcie.0,addr=0x3",
        "-device", "usb-kbd,bus=xhci.0",
        "-qmp", f"unix:{qmp_path},server=on,wait=off",
        "-monitor", "none", "-nic", "none",
        "-serial", f"file:{serial_log}",
    ] + stick_drive(stick) + nvme_drive(nvme, ctx["serial"])
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    if ctx.get("qlib"):
        env["LD_LIBRARY_PATH"] = ctx["qlib"]
    print("+", " ".join(map(str, cmd)), flush=True)
    proc = subprocess.Popen(cmd, env=env)
    seen = []
    stats = {}
    loaded = False
    retry = False
    completed = False
    timed_out = False
    early_exit = False

    def absorb(qmp):
        try:
            ppm = qmp.shot(shot)
        except (RuntimeError, OSError, ValueError) as err:
            print(f"[{tag}] screendump: {err}", flush=True)
            return
        for phrase in list(watch) + list(later) + list(record):
            if phrase in seen:
                continue
            try:
                hit = find_on_ppm(ppm, font, phrase)
            except ValueError:
                hit = False
            if hit:
                seen.append(phrase)
                print(f"[{tag}] screen {phrase!r}", flush=True)
                if phrase.startswith("Type the last 4") and "prompt" not in stats:
                    stats["prompt"] = qmp.nvme()
                    (work / f"{tag}-prompt.ppm").write_bytes(ppm)

    try:
        qmp = QMP(qmp_path, proc)
        deadline = time.time() + 120
        while time.time() < deadline and proc.poll() is None:
            if serial_log.is_file() and b"BdsDxe: loading" in serial_log.read_bytes():
                loaded = True
                break
            time.sleep(0.5)
        if not loaded:
            retry = firmware_retry(attempt, proc.poll() is None, loaded)
        else:
            if menu_keys:
                time.sleep(MENU_DELAY)
                qmp.send(menu_keys)
            end = time.time() + seconds
            typed = False
            while time.time() < end and proc.poll() is None:
                absorb(qmp)
                if not typed and all(p in seen for p in watch) and "prompt" in stats:
                    qmp.send(qcodes(reply))
                    typed = True
                    print(f"[{tag}] sent {reply!r} via send-key", flush=True)
                if typed and all(p in seen for p in later):
                    completed = True
                    break
                time.sleep(POLL_SEC)
            else:
                if proc.poll() is not None:
                    early_exit = True
                else:
                    timed_out = True
            try:
                stats["end"] = qmp.nvme()
            except (RuntimeError, OSError):
                pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    if retry:
        print(f"[{tag}] firmware produced no boot load line, retrying the boot once", flush=True)
        return _phased(ctx, tag, nvme, menu_keys, watch, reply, later, seconds, attempt=2, record=record)
    if not loaded:
        return {
            "ok_firmware": False, "text": "", "stats": stats, "seen": seen,
            "completed": False, "timed_out": False, "early_exit": False,
        }
    return {
        "ok_firmware": True, "text": "\n".join(seen), "stats": stats, "seen": seen,
        "completed": completed, "timed_out": timed_out, "early_exit": early_exit,
    }
