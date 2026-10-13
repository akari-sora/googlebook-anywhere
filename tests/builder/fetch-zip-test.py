#!/usr/bin/env python3
"""base/gbos_base.py fetch-zip against a local server that behaves like ci.android.com: the build URL redirects to a
"signed" URL, and the signature is refused after a few requests (the real one expires after 300 s; counting requests
keeps the test fast and deterministic). Each case starts a fresh server. No network: 127.0.0.1 only.
Cases: expiry during the fetch (re-resolve and retry once); the timed refresh (GBOS_FETCH_REFRESH=0) whose re-resolve
fails keeps the still-valid signed URL; a size change between resolves stops; 404/500 are not retried; a signature
that is refused for good stops after one re-resolve; a stored member larger than one 32 MiB range survives an expiry
in its second range."""
import hashlib
import http.server
import io
import os
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print("PASS " + name)
    else:
        failed += 1
        print("FAIL " + name + (": " + detail if detail else ""))


def make_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, (name, data) in enumerate(members.items()):
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED if i % 2 else zipfile.ZIP_STORED)
    return buf.getvalue(), {n: hashlib.sha256(d).hexdigest() for n, d in members.items()}


class Server:
    """mode: uses = requests one signature is good for; resolve_fail_after = resolves after which the build URL answers
    500; grow_after = resolves after which the object is one byte longer; data_status = HTTP status for every data read;
    refuse_all = every signed request after the first resolve is refused."""

    def __init__(self, blob, **mode):
        self.blob, self.mode = blob, mode
        self.token = self.left = self.refused = self.resolves = self.data_reads = 0
        self.log = []      # (token, first byte, last byte, status) of every signed request
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.handle(self)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/build/otatools.zip"

    def handle(self, h):
        m = self.mode
        if h.path == "/build/otatools.zip":
            self.resolves += 1
            if m.get("resolve_fail_after") is not None and self.resolves > m["resolve_fail_after"]:
                h.send_error(500, "backend error")
                return
            self.token += 1
            self.left = m.get("uses", 1000)
            h.send_response(307)
            h.send_header("Location", f"/signed/otatools.zip?Expires=t{self.token}")
            h.end_headers()
            return
        if not h.path.startswith("/signed/otatools.zip?Expires=t"):
            h.send_error(404)
            return
        tok = int(h.path.rsplit("t", 1)[1])
        rng = h.headers.get("Range", "")
        a, b = (int(x) for x in rng.split("=", 1)[1].split("-"))
        is_probe = (a, b) == (0, 0)
        if not is_probe and m.get("refuse_all") and self.resolves >= 1:
            self.refused += 1
            self.log.append((tok, a, b, 400))
            h.send_error(400, "ExpiredToken")
            return
        valid_old = m.get("old_stays_valid") and tok < self.token
        if not valid_old and (tok != self.token or self.left <= 0):
            self.refused += 1
            self.log.append((tok, a, b, 400))
            h.send_error(400, "ExpiredToken")
            return
        if not valid_old:
            self.left -= 1
        if not is_probe:
            self.data_reads += 1
            if m.get("data_status"):
                self.log.append((tok, a, b, m["data_status"]))
                h.send_error(m["data_status"])
                return
        blob = self.blob + (b"\0" if m.get("grow_after") is not None and self.resolves > m["grow_after"] else b"")
        body = blob[a:b + 1]
        self.log.append((tok, a, b, 206))
        h.send_response(206)
        h.send_header("Content-Range", f"bytes {a}-{a + len(body) - 1}/{len(blob)}")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def close(self):
        self.srv.shutdown()


def fetch(srv, sums, env=None):
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out"
        r = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "base/gbos_base.py"), "fetch-zip", srv.url, str(out),
                            *[f"{n}={s}" for n, s in sums.items()]],
                           capture_output=True, text=True, timeout=300, env={**os.environ, **(env or {})})
        good = all((out / os.path.basename(n)).is_file()
                   and hashlib.sha256((out / os.path.basename(n)).read_bytes()).hexdigest() == s for n, s in sums.items())
        return r, good


small = {"bin/avbtool": os.urandom(70_000), "bin/lpmake": os.urandom(150_000), "lib64/libc++.so": b"x" * 90_000, "etc/empty": b""}
blob, sums = make_zip(small)

s = Server(blob, uses=3)
r, good = fetch(s, sums)
check("expiry during the fetch: every member arrives with its SHA-256", r.returncode == 0 and good, r.stderr.strip()[-200:])
check("expiry during the fetch: the server really refused and the URL was re-resolved", s.refused >= 1 and s.resolves >= 2,
      f"refused={s.refused} resolves={s.resolves}")
s.close()

s = Server(blob, resolve_fail_after=1, old_stays_valid=True)
r, good = fetch(s, sums, {"GBOS_FETCH_REFRESH": "0"})
check("timed refresh whose re-resolve fails keeps the still-valid signed URL", r.returncode == 0 and good and s.resolves >= 2,
      f"rc={r.returncode} resolves={s.resolves} {r.stderr.strip()[-200:]}")
s.close()

s = Server(blob, uses=3, grow_after=1)
r, good = fetch(s, sums)
check("a size change between resolves stops the fetch", r.returncode != 0 and "changed size" in r.stderr, r.stderr.strip()[-200:])
s.close()

for status in (404, 500):
    s = Server(blob, data_status=status)
    r, good = fetch(s, sums)
    check(f"HTTP {status} on data is not retried", r.returncode != 0 and s.resolves == 1, f"rc={r.returncode} resolves={s.resolves}")
    s.close()

s = Server(blob, refuse_all=True)
r, good = fetch(s, sums)
check("a signature refused for good stops after one re-resolve", r.returncode != 0 and s.resolves == 2, f"rc={r.returncode} resolves={s.resolves}")
s.close()

big = {"bin/big-stored": os.urandom((32 << 20) + 300_000)}
bblob, bsums = make_zip(big)
s = Server(bblob, uses=5)      # resolve probe, tail, central dir, local header, 1st range -> the 2nd range is refused
r, good = fetch(s, bsums)
refused = [(a, b) for t, a, b, c in s.log if c == 400]
first = [(a, b) for t, a, b, c in s.log if c == 206 and t == 1 and b - a + 1 == 32 << 20]
check("a stored member larger than one 32 MiB range survives an expiry in its second range",
      r.returncode == 0 and good and s.resolves == 2 and len(first) == 1 and len(refused) == 1
      and refused[0][0] == first[0][1] + 1 and (2, *refused[0], 206) in s.log,
      f"rc={r.returncode} resolves={s.resolves} log={[(t, a, b, c) for t, a, b, c in s.log if b - a > 0]} {r.stderr.strip()[-200:]}")
s.close()

print(f"fetch-zip tests: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
