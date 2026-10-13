#!/usr/bin/env python3
"""install/gbos-probe.sh against fake /sys and /proc trees (tests/probe/fixtures.py): what it reports for the KANO and
an AMD HX 370 mini PC, that its draft profile passes builder/lib/devprofile.py, and that it is read-only: it runs no
other program, writes only into --out, never changes the tree it reads and never copies serial numbers or MAC
addresses. No root, no hardware.
usage: tests/probe/probe-test.py"""
import hashlib, os, re, shutil, stat, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder" / "lib")); sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
import devprofile, fixtures                      # noqa: E402

PROBE = ROOT / "install" / "gbos-probe.sh"
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    print(("PASS " if ok else "FAIL ") + name + ("" if ok or not detail else f"\n     {detail}"))
    passed += bool(ok); failed += not ok


def tree_digest(d):
    h = hashlib.sha256()
    for dp, dns, fns in sorted(os.walk(d)):
        dns.sort()
        for n in sorted(dns + fns):
            p = Path(dp) / n
            st = os.lstat(p)
            h.update(f"{p.relative_to(d)} {stat.S_IFMT(st.st_mode)} {st.st_mode & 0o7777}".encode())
            if stat.S_ISLNK(st.st_mode):
                h.update(os.readlink(p).encode())
            elif stat.S_ISREG(st.st_mode):
                h.update(p.read_bytes())
    return h.hexdigest()


T = Path(tempfile.mkdtemp(prefix="gbos-probe-test-"))
# trap programs: if the probe ever starts one of these (or anything else on this PATH), the call is recorded
TRAP = T / "trap"; TRAP.mkdir(); CALLS = T / "calls"
for name in ("modprobe", "insmod", "rmmod", "mount", "umount", "dd", "flashrom", "fwupdmgr", "ectool", "efibootmgr", "nvme",
             "tpm2_getcap", "dmesg", "lspci", "lsusb", "lsblk", "udevadm", "sudo", "mkdir2"):
    (TRAP / name).write_text(f"#!/bin/sh\necho {name} \"$@\" >> {CALLS}\nexit 0\n"); (TRAP / name).chmod(0o755)
ENV = {"PATH": f"{TRAP}:/usr/bin:/bin", "LC_ALL": "C", "HOME": str(T), "PYTHONDONTWRITEBYTECODE": "1"}


def probe(root, out, *extra):
    return subprocess.run(["bash", PROBE, "--root", root, "--out", out, *extra], capture_output=True, text=True, env=ENV, timeout=120)


results = {}
for name, make in (("kano", fixtures.kano), ("hx370", fixtures.hx370)):
    root = T / f"{name}-root"; root.mkdir(); make(root)
    before = tree_digest(root)
    out = T / f"{name}-out"
    r = probe(root, out)
    check(f"{name}: the probe exits 0", r.returncode == 0, r.stderr[-500:])
    files = sorted(p.name for p in out.iterdir()) if out.is_dir() else []
    check(f"{name}: it writes exactly report.txt, coverage.txt, profile.draft.toml, probe.json",
          files == ["coverage.txt", "probe.json", "profile.draft.toml", "report.txt"], files)
    check(f"{name}: the tree it read is unchanged", tree_digest(root) == before)
    allout = "".join((out / f).read_text() for f in files) + r.stdout
    check(f"{name}: no serial number or MAC address in the output",
          fixtures.SECRET_SERIAL not in allout and fixtures.SECRET_MAC not in allout)
    results[name] = {f: (out / f).read_text() for f in files}
    results[name]["stdout"] = r.stdout
check("the probe started no other program", not CALLS.exists(), CALLS.read_text() if CALLS.exists() else "")

k, h = results["kano"], results["hx370"]
# KANO
check("kano: xe binds the ADL-P GPU and needs xe.force_probe=46a6", "8086:46a6: xe (needs xe.force_probe=46a6)" in k["report.txt"])
check("kano: Wi-Fi AX211 -> iwlwifi", re.search(r"wlan0 8086:51f0 host driver iwlwifi: lapis module: iwlwifi", k["report.txt"]) is not None)
check("kano: NVMe is built in", "0000:01:00.0  144d:a809  class 010802  host driver nvme  ->  nvme (built in)" in k["coverage.txt"])
check("kano: SOF audio controller -> the lapis SOF module", re.search(r"8086:51c8 .*lapis module: .*snd_sof_pci_intel_tgl", k["report.txt"]) is not None)
check("kano: AC adapter has no lapis driver (charging indicator)", "ACPI0003  ->  AC adapter: no lapis driver" in k["coverage.txt"])
check("kano: the ChromeOS EC and the Cr50 are reported as blocked on purpose",
      "GOOG0004  ->  ChromeOS EC: blocked on purpose" in k["coverage.txt"] and "GOOG0005  ->  Cr50 I2C TPM: blocked on purpose" in k["coverage.txt"])
check("kano: a disabled ACPI device (MSFT0101, status 0) is left out", "MSFT0101" not in k["coverage.txt"])
check("kano: Bluetooth on USB class e0 -> btusb", re.search(r"usb  3-10:1.0  8087:0033  class e0 .*lapis module: btusb", k["coverage.txt"]) is not None)
check("kano: touchpad, touchscreen stylus, keyboard, power button and lid found",
      all(x in k["report.txt"] for x in ("touchpad: ELAN0000:00 04F3:3240 Touchpad (i2c)", "stylus: ELAN9008:00 04F3:2C82 Stylus (i2c)",
                                         "keyboard: AT Translated Set 2 keyboard (i8042)", "power button: Power Button (host)",
                                         "lid switch: Lid Switch (host)")))
check("kano: firmware line says coreboot, UEFI, Secure Boot off",
      "coreboot yes" in k["report.txt"] and "UEFI yes" in k["report.txt"] and "Secure Boot off" in k["report.txt"])
check("kano: warns about the ChromeOS EC and the AC adapter", "ChromeOS EC present" in k["report.txt"] and "no charging indicator" in k["report.txt"])
check("kano: the USB stick is not listed as an internal disk", "sda" not in k["report.txt"].split("internal disk:")[1].split("wifi:")[0])
check("kano: the draft names the keyboard backlight LED path",
      "/sys/devices/platform/GOOG0004:00/GOOG0002:00/leds" in k["profile.draft.toml"])
# HX 370
check("hx370: no lapis driver for the Radeon 890M -> framebuffer fallback", "1002:150e: NO lapis GPU driver" in h["report.txt"])
check("hx370: MT7925 Wi-Fi has no lapis driver", re.search(r"wlp2s0 14c3:7925 host driver mt7925e: no lapis driver", h["report.txt"]) is not None)
check("hx370: RTL8125 Ethernet -> r8169", "10ec:8125  class 020000  host driver r8169  ->  lapis module: r8169" in h["coverage.txt"])
check("hx370: HD audio 1022:15e3 -> snd_hda_intel, ACP 1022:15e2 has no lapis driver",
      re.search(r"1022:15e3 host driver snd_hda_intel: lapis module: .*snd_hda_intel", h["report.txt"]) is not None
      and re.search(r"1022:15e2 host driver snd_acp_pci: no lapis driver", h["report.txt"]) is not None)
check("hx370: SATA controller and SATA disk are unsupported, the NVMe is fine",
      "SATA/AHCI: NOT SUPPORTED" in h["coverage.txt"] and "sdb sata" in h["report.txt"] and "NOT usable: no SATA in lapis" in h["report.txt"]
      and "nvme0n1 nvme" in h["report.txt"])
check("hx370: the fTPM is reported as blocked on purpose", "MSFT0101  ->  TPM 2.0: blocked on purpose" in h["coverage.txt"])
check("hx370: warns that there is no GPU driver", "no lapis GPU driver for this machine's GPU" in h["report.txt"])
check("hx370: CPU line", "AuthenticAMD family 26 model 36" in h["report.txt"])

# draft profiles pass the builder's loader (in a profiles/<id>/ directory next to a _safety.toml)
for name, did in (("kano", "kano"), ("hx370", "hx370-mini")):
    d = T / f"draft-{name}" / did; d.mkdir(parents=True)
    (d / "profile.toml").write_text(results[name]["profile.draft.toml"])
    try:
        p = devprofile.load(d)
        ok = p["model"]["id"] == did and p["model"]["status"] == "probe-only"
        check(f"{name}: profile.draft.toml loads with builder/lib/devprofile.py (id {did}, status probe-only)", ok)
    except Exception as e:                                  # noqa: BLE001 (report any loader refusal)
        check(f"{name}: profile.draft.toml loads with builder/lib/devprofile.py", False, repr(e))
kd = devprofile.load(T / "draft-kano" / "kano")
check("kano draft: cmdline.add = [xe.force_probe=46a6] (as in profiles/kano)", kd["cmdline"]["add"] == ["xe.force_probe=46a6"])
hd = devprofile.load(T / "draft-hx370" / "hx370-mini")
check("hx370 draft: no xe option", hd["cmdline"]["add"] == [])
check("kano draft: the native display stack (xe binds), no extra kernel module",
      kd["display"]["stack"] == "native" and kd["kmods"] == [])
check("hx370 draft: the GOP framebuffer + SwiftShader stack (no lapis GPU driver), with gbos_efifb",
      hd["display"]["stack"] == "gop-swiftshader" and hd["kmods"] == ["gbos_efifb"])

# refusals: --out must be new or empty, the root must look like one, unknown arguments
busy = T / "busy"; busy.mkdir(); (busy / "keep").write_text("x")
r = probe(T / "kano-root", busy)
check("refuses a non-empty --out and leaves it alone", r.returncode == 2 and sorted(p.name for p in busy.iterdir()) == ["keep"], r.stderr)
r = probe(T / "nothing-here", T / "o2")
check("refuses a --root without sys/ and proc/ (and creates no --out)", r.returncode == 2 and not (T / "o2").exists(), r.stderr)
r = subprocess.run(["bash", PROBE, "--bogus"], capture_output=True, text=True, env=ENV)
check("refuses an unknown argument", r.returncode == 2 and "unknown argument" in r.stderr)
os.symlink(T / "elsewhere", T / "outlink")
r = probe(T / "kano-root", T / "outlink")
check("refuses an --out that is a symlink", r.returncode == 2 and not (T / "elsewhere").exists(), r.stderr)

# the probe's Python is read-only by construction: no subprocess, no os.system/exec, writes only through write()
src = PROBE.read_text()
py = src.split("<< 'PY'", 1)[1]
check("the probe's Python starts no program (no subprocess/os.system/exec*/popen)",
      not re.search(r"\bsubprocess\b|os\.system|os\.exec|os\.popen|os\.spawn|\bPopen\b", py))
check("the probe's Python opens files for writing only in write() (O_EXCL under --out)",
      py.count("os.open(") == 1 and "O_EXCL" in py and py.count("os.fdopen(") == 1
      and not re.search(r"(?<![a-z])open\([^)]*['\"][wax]b?\+?['\"]", py) and ".write_text(" not in py and ".write_bytes(" not in py)
bash_part = src.split("<< 'PY'", 1)[0]
bad = re.findall(r"\b(modprobe|insmod|rmmod|mount|umount|dd|tee|rm|cp|mv|chmod|chown|ln|touch|efibootmgr|flashrom|sudo|curl|wget)\b", bash_part)
check("the bash part starts nothing but python3 (plus mkdir for --out, ls and sed for checks/help)",
      not bad and bash_part.count("exec ") == 1 and "exec python3 - " in bash_part, bad)

# the module list: names and patterns only, every pattern a known bus prefix
mods = (ROOT / "install/probe/lapis-modules.tsv").read_text().splitlines()
rows = [l.split("\t") for l in mods if not l.startswith("#")]
check("lapis-modules.tsv: every row is <module>\\t<pattern or ->",
      all(len(r) == 2 and re.fullmatch(r"[a-z0-9_]+", r[0]) and (r[1] == "-" or r[1].startswith(("pci:", "usb:", "acpi", "hid:", "i2c:", "sdio:")))
          for r in rows) and len({r[0] for r in rows}) > 500)
r = subprocess.run([sys.executable, ROOT / "install/probe/gen-module-list.py", T / "x.tsv", ROOT / "install"], capture_output=True, text=True)
check("gen-module-list.py: a directory without .ko files gives an empty list, not an error", r.returncode == 0 and "0 modules" in r.stdout, r.stderr)
(T / "kos").mkdir(); (T / "kos" / "fake.ko").write_bytes(b"not an elf")
r = subprocess.run([sys.executable, ROOT / "install/probe/gen-module-list.py", T / "y.tsv", T / "kos"], capture_output=True, text=True)
check("gen-module-list.py refuses a .ko that is not ELF64", r.returncode != 0 and "not an ELF64" in r.stderr + r.stdout)

shutil.rmtree(T, ignore_errors=True)
print(f"probe tests: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
