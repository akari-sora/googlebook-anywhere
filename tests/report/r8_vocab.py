#!/usr/bin/env python3
"""Vocabulary equality and the runtime-value ban.

The independent token set is built by rendering and by the closed interface
grammar. It does not call report_vocabulary() or _word_pieces().
"""
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install" / "gbos-report.sh"
FIX = ROOT / "tests" / "report" / "fixtures" / "kano"
PROBE = ROOT / "tests" / "report" / "fixtures" / "kano-probe"
TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._+\-_][a-z0-9]+)*")
CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
PCI_RE = re.compile(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}")


def tokenize(text):
    low = str(text).lower()
    out = set()
    for m in TOKEN_RE.finditer(low):
        tok = m.group(0)
        if re.fullmatch(r"\d{1,6}", tok):
            continue
        out.add(tok)
        for part in re.split(r"[^a-z0-9]+", tok):
            if part and not re.fullmatch(r"\d{1,6}", part):
                out.add(part)
    for m in CJK_RE.finditer(str(text)):
        if m.group(0):
            out.add(m.group(0))
    return out


def load_ns():
    text = SCRIPT.read_text(encoding="utf-8")
    py = text.split("<< 'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
    py = py[: py.rfind("\nmain()")] + "\n"
    ns = {}
    exec(compile(py, str(SCRIPT), "exec"), ns)
    return ns


def main():
    ns = load_ns()
    work = pathlib.Path(tempfile.mkdtemp(prefix="gbos-r8-vocab-"))
    try:
        return run(ns, work)
    finally:
        os.environ.pop("GBOS_DEV_KERNEL_DIR", None)
        shutil.rmtree(work, ignore_errors=True)


def run(ns, work):
    dev = work / "dev"
    (dev / "include" / "config").mkdir(parents=True)
    (dev / "include" / "config" / "kernel.release").write_text(
        "6.1.75-R8RUNTIME9F3A\n", encoding="utf-8", newline="\n")
    os.environ["GBOS_DEV_KERNEL_DIR"] = str(dev)
    ns["_REPORT_WORDS"] = None
    poisoned = ns["report_vocabulary"]()
    if "r8runtime9f3a" in poisoned or "6.1.75-r8runtime9f3a" in poisoned:
        sys.stderr.write("runtime kernel release entered the vocabulary\n")
        return 1
    if ns["kernel_release_text"]() != "6.1.75-R8RUNTIME9F3A":
        sys.stderr.write("dev kernel.release was not readable\n")
        return 1
    os.environ.pop("GBOS_DEV_KERNEL_DIR", None)
    ns["_REPORT_WORDS"] = None

    raw = work / "raw"
    probe = work / "probe"
    shutil.copytree(FIX, raw)
    shutil.copytree(PROBE, probe)
    manifest = []
    saved = {}
    for src in sorted(raw.iterdir()):
        if not src.name.endswith(".txt"):
            continue
        saved[src.name] = src.read_text(encoding="utf-8")
        ident = src.name[:-4]
        (raw / (ident + ".rc")).write_text("0\n", encoding="utf-8", newline="\n")
        (raw / (ident + ".err")).write_text("", encoding="utf-8", newline="\n")
        manifest.append(ident + "\tcmd\n")
    (raw / "manifest.tsv").write_text("".join(manifest), encoding="utf-8", newline="\n")
    saved_probe = (probe / "probe.json").read_text(encoding="utf-8")
    saved_draft = (probe / "profile.draft.toml").read_text(encoding="utf-8")
    saved_report = (probe / "report.txt").read_text(encoding="utf-8")
    captured = {}

    def capture(path, body):
        captured[pathlib.Path(path).name] = body

    ns["write_excl"] = capture
    io_buf = io.StringIO()
    seen = set()

    def add_pair(txt, summary):
        seen.update(tokenize(PCI_RE.sub(" ", txt)))
        seen.update(tokenize(PCI_RE.sub(" ", summary)))
        seen.discard("20261014")
        for tok in [tok for tok in seen if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok)]:
            seen.discard(tok)

    def restore():
        for name, body in saved.items():
            (raw / name).write_text(body, encoding="utf-8", newline="\n")
        (probe / "probe.json").write_text(saved_probe, encoding="utf-8", newline="\n")
        (probe / "profile.draft.toml").write_text(saved_draft, encoding="utf-8", newline="\n")
        (probe / "report.txt").write_text(saved_report, encoding="utf-8", newline="\n")

    def render(source="fixture", use_probe=True):
        captured.clear()
        held = sys.stdout
        sys.stdout = io_buf
        try:
            ns["render"](
                str(work / "out"), "20261014", str(raw),
                str(probe) if use_probe else "", source, "")
        finally:
            sys.stdout = held
        add_pair(captured["gbos-report-20261014.txt"], captured["summary.md"])

    def with_change(mutate, source="fixture", use_probe=True):
        restore()
        mutate()
        render(source, use_probe)

    render("fixture", True)
    render("adb", True)
    render("", True)
    render("", False)

    def set_prop(key, value):
        path = raw / "getprop.txt"
        text = path.read_text(encoding="utf-8")
        line = "[%s]: [%s]" % (key, value)
        prefix = "[" + key + "]:"
        lines, found = [], False
        for old in text.splitlines():
            if old.startswith(prefix):
                lines.append(line)
                found = True
            else:
                lines.append(old)
        if not found:
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    for key in ns["OEM"]:
        with_change(lambda key=key: (raw / "cat-dmi-sys-vendor.txt").write_text(
            key + "\n", encoding="utf-8", newline="\n"))
    for key in ns["FW_VENDOR"]:
        with_change(lambda key=key: (raw / "cat-dmi-bios-vendor.txt").write_text(
            key + "\n", encoding="utf-8", newline="\n"))
    for name in sorted(ns["GPU_DRIVERS"]):
        with_change(lambda name=name: (raw / "cat-drm0-uevent.txt").write_text(
            "DRIVER=%s\nPCI_ID=8086:46A6\n" % name, encoding="utf-8", newline="\n"),
            use_probe=False)
    for name in sorted(ns["WIFI_DRIVERS"]):
        with_change(lambda name=name: (raw / "cat-wlan-uevent.txt").write_text(
            "DRIVER=%s\n" % name, encoding="utf-8", newline="\n"), use_probe=False)
    for name in sorted(ns["BT_DRIVERS"]):
        with_change(lambda name=name: (raw / "cat-bt-uevent.txt").write_text(
            "DRIVER=%s\n" % name, encoding="utf-8", newline="\n"), use_probe=False)
    for name in sorted(ns["INPUT_DRIVERS"]):
        def put_input(name=name):
            data = json.loads(saved_probe)
            data["input"][0]["driver"] = name
            (probe / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
        with_change(put_input)
    for bus, _label in ns["BUSNAME"].items():
        def put_bus(bus=bus):
            (raw / "dumpsys-input.txt").write_text(
                "1: Touchpad\n  Classes: TOUCHPAD\n  Identifier: bus=0x%s vendor=0x04f3 product=0x323c\n" % bus,
                encoding="utf-8", newline="\n")
            (raw / "cat-proc-bus-input-devices.txt").write_text("", encoding="utf-8", newline="\n")
        with_change(put_bus, use_probe=False)
    with_change(lambda: (raw / "dumpsys-input.txt").write_text(
        "1: Pen\n  Classes: EXTERNAL_STYLUS\n  Identifier: bus=0x0005 vendor=0x1234 product=0x5678\n",
        encoding="utf-8", newline="\n"))
    with_change(lambda: (raw / "cat-proc-cpuinfo.txt").write_text(
        saved["cat-proc-cpuinfo.txt"].replace("GenuineIntel", "AuthenticAMD", 1),
        encoding="utf-8", newline="\n"), use_probe=False)
    with_change(lambda: (raw / "uname-a.txt").write_text(
        "Linux synth-laptop %s #1\n" % ns["PINNED_KERNELS"][0],
        encoding="utf-8", newline="\n"), use_probe=False)

    def show_build():
        set_prop("ro.build.display.id", "")
        set_prop("ro.build.id", "16452207")
    with_change(show_build, use_probe=False)

    def draft(profile, status):
        def mutate(profile=profile, status=status):
            (probe / "profile.draft.toml").write_text(
                'schema = 1\n\n[model]\nid = "%s"\nstatus = "%s"\n' % (profile, status),
                encoding="utf-8", newline="\n")
        with_change(mutate)
    for profile in ns["PROFILES"]:
        draft(profile, "probe-only")
    for status in ns["PROFILE_STATUS"]:
        draft("kano", status)

    def wifi(body, oper=""):
        def mutate(body=body, oper=oper):
            (raw / "dumpsys-wifi.txt").write_text(body, encoding="utf-8", newline="\n")
            (raw / "cat-wlan-operstate.txt").write_text(
                (oper + "\n") if oper else "", encoding="utf-8", newline="\n")
        with_change(mutate, use_probe=False)
    wifi("Wi-Fi is enabled\ncurState=ConnectedState\n")
    wifi("Wi-Fi is disabled\n")
    wifi("Wi-Fi is enabled\ncurState=DisconnectedState\n")
    wifi("Wi-Fi is enabled\n")
    wifi("", "up")
    wifi("", "down")
    wifi("", "sideways")

    def bt(body, ls="hci0\n", prop=None):
        def mutate(body=body, ls=ls, prop=prop):
            (raw / "dumpsys-bluetooth.txt").write_text(body, encoding="utf-8", newline="\n")
            (raw / "ls-bluetooth.txt").write_text(ls, encoding="utf-8", newline="\n")
            if prop is not None:
                set_prop("init.svc.bluetooth", prop)
        with_change(mutate, use_probe=False)
    bt("Bluetooth Status\n  state: ON\n")
    bt("Bluetooth Status\n  state: OFF\n")
    bt("Bluetooth Status\n  state: TURNING_ON\n")
    bt("Bluetooth Status\n  state: TURNING_OFF\n")
    bt("", "hci0\n")
    bt("", "", "running")
    bt("", "", "stopped")
    bt("Bluetooth Status\n  enabled: true\n  state: OFF\n")

    def show_swift():
        (raw / "cat-drm0-uevent.txt").write_text(
            "DRIVER=gbos_efifb\n", encoding="utf-8", newline="\n")
        set_prop("ro.hardware.egl", "swiftshader")
        set_prop("ro.hardware.vulkan", "pastel")
    with_change(show_swift, use_probe=False)
    if "swiftshader" not in seen:
        sys.stderr.write("swift render missing label\n")
        sys.stderr.write(captured["gbos-report-20261014.txt"])
        return 1

    def unread_uevent():
        (raw / "cat-drm0-uevent.txt").write_text(
            "PCI_ID=8086:46A6\n", encoding="utf-8", newline="\n")
    with_change(unread_uevent, use_probe=False)
    if "card0 pci 8086:46a6 driver unread" not in captured["gbos-report-20261014.txt"]:
        sys.stderr.write("uevent unread missing\n")
        return 1

    def unread_sysfs():
        (raw / "cat-drm0-uevent.txt").write_text("", encoding="utf-8", newline="\n")
        (raw / "cat-drm-vendor.txt").write_text("0x8086\n", encoding="utf-8", newline="\n")
        (raw / "cat-drm-device.txt").write_text("0x46a6\n", encoding="utf-8", newline="\n")
    with_change(unread_sysfs, use_probe=False)
    if "card0 pci 8086:46a6 driver unread" not in captured["gbos-report-20261014.txt"]:
        sys.stderr.write("sysfs unread missing\n")
        return 1

    build_ac = (ROOT / "builder" / "kmod" / "build-ac.sh").read_text(encoding="utf-8")
    pinned_sha = re.search(r"(?m)^CONFIG_SHA=([0-9a-f]{64})\s*$", build_ac)
    if not pinned_sha or pinned_sha.group(1) != ns["GKI_CONFIG_SHA"]:
        sys.stderr.write("GKI config hash does not match build-ac.sh\n")
        return 1

    def kernel_line(blob):
        for line in blob.splitlines():
            if line.startswith("kernel:"):
                return line
        return ""

    def show_kernel(release, config=None, override=None):
        dev = work / "dev-gate"
        if dev.exists():
            shutil.rmtree(dev)
        cfg = dev / "include" / "config"
        cfg.mkdir(parents=True)
        (cfg / "kernel.release").write_text(release + "\n", encoding="utf-8", newline="\n")
        if config is not None:
            (dev / ".config").write_bytes(config)
        saved = ns["GKI_CONFIG_SHA"]
        os.environ["GBOS_DEV_KERNEL_DIR"] = str(dev)
        if override is not None:
            ns["GKI_CONFIG_SHA"] = override
        try:
            restore()
            (raw / "uname-a.txt").write_text(
                "Linux synth-laptop %s #1\n" % release, encoding="utf-8", newline="\n")
            render("fixture", False)
        finally:
            ns["GKI_CONFIG_SHA"] = saved
            os.environ.pop("GBOS_DEV_KERNEL_DIR", None)
        txt = captured["gbos-report-20261014.txt"]
        summary = captured["summary.md"]
        if release.lower() in txt.lower() or release.lower() in summary.lower():
            sys.stderr.write("dev release printed: %s\n" % release)
            return None
        return txt, summary

    omit = show_kernel("6.1.75-r9vocabomit")
    if omit is None or kernel_line(omit[0]) != "kernel: dev-kernel [fixture]":
        sys.stderr.write("dev-kernel without config: %s\n" % (kernel_line(omit[0]) if omit else "missing"))
        return 1
    failed = show_kernel("6.1.75-r9vocabfail", config=b"not-the-gki-config\n")
    if failed is None or kernel_line(failed[0]) != "kernel: dev-kernel, config fail [fixture]":
        sys.stderr.write("config fail: %s\n" % (kernel_line(failed[0]) if failed else "missing"))
        return 1
    gate_blob = b"r9-gate\n"
    passed = show_kernel(
        "6.1.75-r9vocabpass", config=gate_blob, override=hashlib.sha256(gate_blob).hexdigest())
    if passed is None or kernel_line(passed[0]) != "kernel: dev-kernel, config pass [fixture]":
        sys.stderr.write("config pass: %s\n" % (kernel_line(passed[0]) if passed else "missing"))
        return 1
    if ns["GKI_CONFIG_SHA"] != pinned_sha.group(1):
        sys.stderr.write("config hash override leaked\n")
        return 1

    def mismatch():
        data = json.loads(saved_probe)
        data["dmi"]["sys_vendor"] = "Dell"
        (probe / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
    with_change(mismatch)

    for i in range(10):
        seen.add("wlan%d" % i)
        seen.add("eth%d" % i)
    for i in range(100):
        seen.add("hci%d" % i)
        seen.add("card%d" % i)
    for i in range(1000):
        seen.add("renderd%d" % i)

    vocab = set(ns["report_vocabulary"]())
    missing = sorted(seen - vocab)
    extra = sorted(vocab - seen)
    if missing or extra:
        sys.stderr.write("printed-not-vocabulary %d: %s\n" % (
            len(missing), ", ".join(missing[:40])))
        sys.stderr.write("vocabulary-not-printed %d: %s\n" % (
            len(extra), ", ".join(extra[:40])))
        return 1
    # Every maximal CJK run in the Kano render is a vocabulary word.
    restore()
    render("fixture", True)
    txt = captured["gbos-report-20261014.txt"]
    summary = captured["summary.md"]
    cjk_bad = []
    for blob in (txt, summary):
        for m in CJK_RE.finditer(blob):
            if not ns["is_report_word"](m.group(0)):
                cjk_bad.append(m.group(0))
    if cjk_bad:
        sys.stderr.write("cjk not vocabulary: %s\n" % ", ".join(sorted(set(cjk_bad))[:20]))
        return 1
    print("vocabulary words %d" % len(vocab))
    print("independent tokens %d" % len(seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
