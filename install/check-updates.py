#!/usr/bin/env python3
"""Tell whether Google has a newer lapis recovery build than the one this checkout pins, and whether this project has
a newer release than this checkout. Read-only: it reads two small public JSON documents and never downloads images.

usage: install/check-updates.py [--json] [--hint]
  (default)  one line for Google and one for the project: "newer ... available", "up to date" or "could not check".
  --json     the same as one JSON object.
  --hint     for install.sh/update.sh: print only the "newer ... available" lines (nothing when up to date or when a
             check failed), and do nothing at all when CI is set.
GBOS_NO_UPDATE_CHECK=1 skips both checks (offline builds). It always exits 0, also on network errors; 2 only for a
bad argument. Requests: HTTPS GET with a 10 s timeout, a plain User-Agent, no cookies, no token, no other data.

Sources (format checked against the live documents on 2026-10-23, see docs/UPGRADE.md "怎麼知道有新版本"):
  Google: https://dl.google.com/dl/edgedl/device/recovery/production_recovery.json, the index named in
    base/builds/16452207.sh: a JSON list of entries with "target" (e.g. "lapis-user"), "channel" and
    "buildidentifier" ("16452207_CL3B.260622.271.R1"), "url" (".../lapis-user/16452207/recovery.zip").
  project: GitLab Releases API, GET /api/v4/projects/aa22396584%2Fgooglebook-anywhere/releases (public, no token):
    a JSON list with "tag_name" (e.g. "v0.2.3") and "upcoming_release".
"""
import json, os, re, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOOGLE_URL = "https://dl.google.com/dl/edgedl/device/recovery/production_recovery.json"
RELEASES_URL = "https://gitlab.com/api/v4/projects/aa22396584%2Fgooglebook-anywhere/releases?per_page=20"
USER_AGENT = "googlebook-anywhere-check-updates"
MAX_BYTES = 4 << 20


def fetch_json(url, timeout):
    # urllib keeps no cookies unless a cookie handler is installed; only the User-Agent header is set
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("response larger than 4 MB")
    return json.loads(data)


def pinned_build():
    """(target, build id) of the Google build install.sh uses (GBOS_LAPIS_BUILD, default 16452207)."""
    build = os.environ.get("GBOS_LAPIS_BUILD", "16452207")
    pins = ROOT / "base/builds" / f"{build}.sh"
    m = re.search(r"^ZIP_URL=https://dl\.google\.com/device/recovery/([a-z0-9_-]+)/([0-9]+)/", pins.read_text(), re.M)
    if not m:
        raise ValueError(f"{pins.relative_to(ROOT)}: no ZIP_URL with target and build id")
    return m.group(1), int(m.group(2))


def build_id(entry):
    m = re.match(r"([0-9]+)_", str(entry.get("buildidentifier", ""))) or re.search(r"/([0-9]+)/[^/]*$", str(entry.get("url", "")))
    return int(m.group(1)) if m else None


def newer(a, b):
    """True when a is newer than b (ints, or version tuples)."""
    return a > b


def check_google(index, target, pinned):
    if not isinstance(index, list):
        raise ValueError("index is not a JSON list")
    ids = [build_id(e) for e in index if isinstance(e, dict) and e.get("target") == target]
    ids = [i for i in ids if i is not None]
    if not ids:
        raise ValueError(f"no {target} entry in the index")
    latest = max(ids)
    if newer(latest, pinned):
        return "newer", latest, (f"newer Google build available: {latest} (pinned {pinned}); this project needs new pins "
                                 f"(base/builds/{latest}.sh) before it can build from it")
    return "up-to-date", latest, f"up to date (pinned {pinned}, Google {target} {latest})"


VER = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")


def local_version():
    lines = (ROOT / "docs/RELEASE-NOTES.md").read_text(encoding="utf-8").splitlines()
    m = VER.search(lines[0]) if lines and lines[0].startswith("# Googlebook Anywhere ") else None
    if not m:
        raise ValueError("docs/RELEASE-NOTES.md line 1 has no version")
    released = len(lines) > 2 and lines[2].strip() == "<!-- release-status: released -->"
    return tuple(map(int, m.groups())), released


def check_project(releases, local, released):
    if not isinstance(releases, list):
        raise ValueError("releases is not a JSON list")
    tags = []
    for r in releases:
        if isinstance(r, dict) and not r.get("upcoming_release"):
            m = VER.fullmatch(str(r.get("tag_name", "")))
            if m:
                tags.append(tuple(map(int, m.groups())))
    if not tags:
        raise ValueError("no vX.Y.Z release")
    latest = max(tags)
    v = lambda t: "v%d.%d.%d" % t
    here = v(local) + ("" if released else " (not released yet)")
    if newer(latest, local):
        return "newer", v(latest), f"newer release available: {v(latest)} (this checkout {here}); see docs/UPGRADE.md"
    return "up-to-date", v(latest), f"up to date (this checkout {here}, latest release {v(latest)})"


def run(timeout):
    out = {}
    for name, url, fn in (("google", os.environ.get("GBOS_UPDCHECK_GOOGLE_URL", GOOGLE_URL), "g"),
                          ("project", os.environ.get("GBOS_UPDCHECK_RELEASES_URL", RELEASES_URL), "p")):
        try:
            if fn == "g":
                target, pinned = pinned_build()
                state, latest, msg = check_google(fetch_json(url, timeout), target, pinned)
            else:
                local, released = local_version()
                state, latest, msg = check_project(fetch_json(url, timeout), local, released)
            out[name] = {"state": state, "latest": latest, "message": msg}
        except Exception as e:   # network, HTTP, JSON or format error: report it, never fail the caller
            out[name] = {"state": "error", "latest": None, "message": f"could not check ({type(e).__name__}: {e})"}
    return out


def main(argv):
    as_json, hint = "--json" in argv, "--hint" in argv
    bad = [a for a in argv if a not in ("--json", "--hint")]
    if bad or (as_json and hint):
        print("usage: install/check-updates.py [--json | --hint]", file=sys.stderr)
        return 2
    if os.environ.get("GBOS_NO_UPDATE_CHECK", "0") not in ("", "0") or (hint and os.environ.get("CI")):
        if not hint:
            print(json.dumps({"skipped": True}) if as_json else "update check skipped (GBOS_NO_UPDATE_CHECK)")
        return 0
    try:
        timeout = float(os.environ.get("GBOS_UPDCHECK_TIMEOUT", "10"))
    except ValueError:
        timeout = 10.0
    res = run(timeout)
    if as_json:
        print(json.dumps(res, indent=1))
    elif hint:
        for name, r in res.items():
            if r["state"] == "newer":
                print(f"NOTE ({name}): {r['message']}")
    else:
        for name, r in res.items():
            print(f"{name}: {r['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))