#!/usr/bin/env python3
"""Offline tests of install/check-updates.py and its notices in install/update.sh (issue #7 item 2).

usage: tests/update/check-updates-test.py
Fixtures in tests/update/fixtures/updcheck/ are trimmed copies of the two public documents (production_recovery.json
and the GitLab releases list, both read on 2026-10-23) with older/newer entries. Network errors and timeouts use a
local socket. Nothing leaves this machine unless GBOS_UPDCHECK_LIVE=1 (then one extra case reads the two real URLs).
"""
import http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = Path(os.environ.get("GBOS_UPDCHECK_TOOL") or ROOT / "install/check-updates.py")
FX = ROOT / "tests/update/fixtures/updcheck"
passed = failed = skipped = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("PASS", name)
    else:
        failed += 1; print("FAIL", name); print("    " + str(detail).replace("\n", "\n    ")[:1500])


def skip(name):
    global skipped
    skipped += 1; print("SKIP", name)


def fx(name):
    return (FX / name).resolve().as_uri()


BASE_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GBOS_") and k != "CI"}
BASE_ENV["PYTHONDONTWRITEBYTECODE"] = "1"


def run(google="recovery-same.json", releases="releases.json", args=(), tool=TOOL, **env):
    e = dict(BASE_ENV, GBOS_UPDCHECK_GOOGLE_URL=google if "://" in google else fx(google),
             GBOS_UPDCHECK_RELEASES_URL=releases if "://" in releases else fx(releases))
    e.update(env)
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, str(tool), *args], env=e, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr, time.monotonic() - t0


def closed_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return f"http://127.0.0.1:{port}/x.json"


# --- Google index
rc, out, err, _ = run()
check("same lapis build: Google up to date", rc == 0 and "google: up to date (pinned 16452207, Google lapis-user 16452207)" in out, out + err)
check("... other devices' newer builds (mica 16471258) are ignored", "16471258" not in out, out)
rc, out, err, _ = run("recovery-newer.json")
check("newer lapis build: reported with the pinned one", rc == 0 and "google: newer Google build available: 16471258 (pinned 16452207)" in out, out + err)
check("... and says new pins are needed", "base/builds/16471258.sh" in out, out)
rc, out, err, _ = run("recovery-older.json")
check("older lapis build in the index: up to date", rc == 0 and "google: up to date (pinned 16452207, Google lapis-user 16400000)" in out, out + err)
rc, out, err, _ = run("recovery-nolapis.json")
check("no lapis entry: could not check, exit 0", rc == 0 and "google: could not check" in out and "no lapis-user entry" in out, out + err)
rc, out, err, _ = run("garbage.json")
check("not JSON: could not check, exit 0", rc == 0 and "google: could not check" in out, out + err)
rc, out, err, _ = run("recovery-diffwidth.json")
check("differing-width build ids: numeric max wins (100000000 newer, not lexicographic 9999999)",
      rc == 0 and "google: newer Google build available: 100000000 (pinned 16452207)" in out
      and "base/builds/100000000.sh" in out, out + err)

# --- project releases (this checkout: docs/RELEASE-NOTES.md)
rc, out, err, _ = run()
notes = (ROOT / "docs/RELEASE-NOTES.md").read_text(encoding="utf-8").splitlines()
check("releases up to v0.2.3, checkout newer: project up to date",
      rc == 0 and "project: up to date (this checkout " in out and "latest release v0.2.3" in out, out + err)
rc, out, err, _ = run(releases="releases-newer.json")
check("a newer release v9.0.0: reported", rc == 0 and "project: newer release available: v9.0.0" in out, out + err)
check("... the upcoming_release v99.0.0 is ignored", "v99" not in out, out)
rc, out, err, _ = run(releases="garbage.json")
check("malformed releases (not JSON): could not check, exit 0",
      rc == 0 and "project: could not check" in out, out + err)
rc, out, err, _ = run(releases="releases-notlist.json")
check("malformed releases (not a list): could not check, exit 0",
      rc == 0 and "project: could not check" in out and "releases is not a JSON list" in out, out + err)
rc, out, err, _ = run(releases="releases-empty.json")
check("malformed releases (empty list): could not check, exit 0",
      rc == 0 and "project: could not check" in out and "no vX.Y.Z release" in out, out + err)
rc, out, err, _ = run(releases="releases-noversion.json")
check("malformed releases (no vX.Y.Z tag): could not check, exit 0",
      rc == 0 and "project: could not check" in out and "no vX.Y.Z release" in out, out + err)

# a copy of the tool in a tree whose RELEASE-NOTES is a released v0.2.2 / v0.2.3
with tempfile.TemporaryDirectory() as td:
    t = Path(td)
    (t / "install").mkdir(); (t / "docs").mkdir(); shutil.copytree(ROOT / "base/builds", t / "base/builds")
    shutil.copy(TOOL, t / "install/check-updates.py")
    for ver, want in (("v0.2.2", "project: newer release available: v0.2.3 (this checkout v0.2.2)"),
                      ("v0.2.3", "project: up to date (this checkout v0.2.3, latest release v0.2.3)")):
        (t / "docs/RELEASE-NOTES.md").write_text(f"# Googlebook Anywhere {ver}\n\n<!-- release-status: released -->\n\nx\n")
        rc, out, err, _ = run(tool=t / "install/check-updates.py")
        check(f"released checkout {ver}: {want.split(':')[1].split('(')[0].strip()}", rc == 0 and want in out, out + err)

# numeric (not lexicographic) version comparison: v0.10.0 > v0.9.0 numerically,
# but "v0.10.0" < "v0.9.0" as strings, so a string comparison would miss it
with tempfile.TemporaryDirectory() as td:
    t = Path(td)
    (t / "install").mkdir(); (t / "docs").mkdir(); shutil.copytree(ROOT / "base/builds", t / "base/builds")
    shutil.copy(TOOL, t / "install/check-updates.py")
    (t / "docs/RELEASE-NOTES.md").write_text("# Googlebook Anywhere v0.9.0\n\n<!-- release-status: released -->\n\nx\n")
    rc, out, err, _ = run(tool=t / "install/check-updates.py", releases="releases-double-digit.json")
    check("double-digit minor: v0.10.0 newer than checked-out v0.9.0 (numeric, not lexicographic)",
          rc == 0 and "project: newer release available: v0.10.0 (this checkout v0.9.0)" in out, out + err)

# --- network errors, timeout, request contents
rc, out, err, dt = run(closed_port(), closed_port())
check("connection refused: both could not check, exit 0, no traceback",
      rc == 0 and out.count("could not check") == 2 and "Traceback" not in err, out + err)
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(4)
silent = f"http://127.0.0.1:{srv.getsockname()[1]}/x.json"
rc, out, err, dt = run(silent, silent, GBOS_UPDCHECK_TIMEOUT="1")
srv.close()
check("a server that never answers: timeout, could not check, exit 0", rc == 0 and out.count("could not check") == 2 and dt < 15, f"{dt:.1f}s\n{out}{err}")
check("the default timeout is 10 s", 'os.environ.get("GBOS_UPDCHECK_TIMEOUT", "10")' in TOOL.read_text(), "")

seen = []
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        seen.append((self.command, self.path, dict(self.headers)))
        body = (FX / ("recovery-same.json" if "rec" in self.path else "releases.json")).read_bytes()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.send_header("Set-Cookie", "a=b")
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
hs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=hs.serve_forever, daemon=True).start()
u = f"http://127.0.0.1:{hs.server_address[1]}"
rc, out, err, _ = run(u + "/rec.json", u + "/rel.json")
hs.shutdown()
names = {k.lower() for _, _, h in seen for k in h}
check("over HTTP: both answered up to date", rc == 0 and out.count("up to date") == 2, out + err)
check("two GET requests only", [c for c, _, _ in seen] == ["GET", "GET"], seen)
check("headers: plain User-Agent, no cookie or token", names <= {"host", "user-agent", "accept", "accept-encoding", "connection"}
      and all(h.get("User-Agent") == "googlebook-anywhere-check-updates" for _, _, h in seen), seen)

# --- modes
rc, out, err, _ = run(args=["--json"], google="recovery-newer.json")
try:
    j = json.loads(out)
    check("--json: states and latest values", j["google"]["state"] == "newer" and j["google"]["latest"] == 16471258
          and j["project"]["state"] == "up-to-date" and j["project"]["latest"] == "v0.2.3", out)
except Exception as e:
    check("--json: parses", False, f"{e}\n{out}{err}")
rc, out, err, _ = run(args=["--hint"])
check("--hint, all up to date: prints nothing", rc == 0 and out == "", out + err)
rc, out, err, _ = run(closed_port(), closed_port(), args=["--hint"])
check("--hint, network error: prints nothing, exit 0", rc == 0 and out == "", out + err)
rc, out, err, _ = run("recovery-newer.json", "releases-newer.json", args=["--hint"])
check("--hint, both newer: two NOTE lines", rc == 0 and out.count("NOTE (") == 2 and "16471258" in out and "v9.0.0" in out, out + err)
rc, out, err, _ = run("recovery-newer.json", "releases-newer.json", args=["--hint"], CI="true")
check("--hint with CI set: silent", rc == 0 and out == "", out + err)
rc, out, err, dt = run(closed_port(), closed_port(), GBOS_NO_UPDATE_CHECK="1")
check("GBOS_NO_UPDATE_CHECK=1: skipped, no request", rc == 0 and out.strip() == "update check skipped (GBOS_NO_UPDATE_CHECK)", out + err)
rc, out, err, _ = run(args=["--bogus"])
check("unknown argument: exit 2", rc == 2 and "usage" in err, out + err)

# --- install/update.sh: the notice before install.sh, never blocking
# update.sh refuses root (`[ "$(id -u)" -ne 0 ]`), which made these three cases
# always SKIP in CI (root before `useradd`). Shadow `id` with a fake that reports
# a normal user so the notice path is exercised even as root; never SKIP.
with tempfile.TemporaryDirectory() as td:
    t = Path(td)
    (t / "install").mkdir(); (t / "docs").mkdir(); (t / "work/keys").mkdir(parents=True)
    (t / "work/keys/vbmeta.pem").write_text("k\n")
    shutil.copytree(ROOT / "base/builds", t / "base/builds")
    shutil.copy(ROOT / "install/update.sh", t / "install/update.sh")
    shutil.copy(TOOL, t / "install/check-updates.py")
    shutil.copy(ROOT / "docs/RELEASE-NOTES.md", t / "docs/RELEASE-NOTES.md")
    (t / "install/install.sh").write_text('#!/bin/bash\necho "STUB install.sh checked=${GBOS_UPDATE_CHECKED:-} args=$*"\nexit 7\n')
    fakebin = t / "fakebin"
    fakebin.mkdir()
    (fakebin / "id").write_text('#!/bin/sh\necho 1000\n')
    (fakebin / "id").chmod(0o755)
    def upd(*args, **env):
        e = dict(BASE_ENV, GBOS_UPDCHECK_GOOGLE_URL=fx("recovery-newer.json"), GBOS_UPDCHECK_RELEASES_URL=fx("releases-newer.json"))
        e.update(env)
        e["PATH"] = str(fakebin) + os.pathsep + e.get("PATH", "/usr/bin:/bin")
        p = subprocess.run(["bash", str(t / "install/update.sh"), "--work", str(t / "work"), *args], env=e,
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr
    rc, out = upd()
    check("update.sh: notice printed before install.sh runs, install.sh told it was checked",
          rc == 7 and "NOTE (google)" in out and "NOTE (project)" in out
          and out.find("NOTE (") < out.find("STUB install.sh checked=1"), out)
    rc, out = upd("--no-update-check")
    check("update.sh --no-update-check: no notice, flag not passed on, install.sh still runs",
          rc == 7 and "NOTE" not in out and "STUB install.sh" in out and "--no-update-check" not in out, out)
    rc, out = upd(GBOS_UPDCHECK_GOOGLE_URL=closed_port(), GBOS_UPDCHECK_RELEASES_URL=closed_port())
    check("update.sh network error: no notice, install.sh still runs", rc == 7 and "NOTE" not in out and "STUB install.sh" in out, out)

# --- install.sh: the hook in the final summary (its full runs are in tests/install/install-sh-test.sh)
src = (ROOT / "install/install.sh").read_text()
check("install.sh: --no-update-check sets GBOS_NO_UPDATE_CHECK", "--no-update-check) export GBOS_NO_UPDATE_CHECK=1" in src, "")
check("install.sh: the --hint call is non-blocking and skipped after update.sh",
      '[ "${GBOS_UPDATE_CHECKED:-0}" = 1 ] || python3 "$ROOT/install/check-updates.py" --hint || true' in src, "")

# --- opt-in: the real documents
if os.environ.get("GBOS_UPDCHECK_LIVE") == "1":
    p = subprocess.run([sys.executable, str(TOOL)], env=BASE_ENV, capture_output=True, text=True, timeout=60)
    check("live: both documents read and understood", p.returncode == 0 and "could not check" not in p.stdout, p.stdout + p.stderr)
else:
    skip("live check of the two public URLs (set GBOS_UPDCHECK_LIVE=1)")

print(f"check-updates tests: {passed} passed, {failed} failed, {skipped} skipped")
sys.exit(1 if failed else 0)