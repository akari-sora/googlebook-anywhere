"""No file under tools/harness or tests/harness except kano-env.local.sh may contain the developer's real values.

The values are not in this file in any form (no literal, no hash): the test reads the extended regexes of
tools/release/private-patterns.txt, the same list tools/release/make-public.sh scans the public export with, and runs
the same `grep -E` over every file. That list is private (never exported), so in the public export the test is
skipped; the synthetic test checks the scanner itself everywhere.
"""
import os, subprocess, tempfile, unittest
from pathlib import Path
from helpers import HARNESS, REPO, TESTS

PATTERNS = REPO / "tools" / "release" / "private-patterns.txt"
ALLOWED = {"kano-env.local.sh"}       # the private repo's own values; the public export drops it


def scan(paths, patterns):
    """(file, line number) of every line matching a pattern (grep -iE -f: case-insensitive, like make-public.sh).
    A grep error (exit status above 1: unreadable file, bad pattern) raises instead of passing as "clean"."""
    paths = [str(p) for p in paths]
    if not paths:
        return []
    r = subprocess.run(["grep", "-inHE", "-f", str(patterns), "--", *paths], capture_output=True, text=True,
                       errors="replace")
    if r.returncode > 1:
        raise RuntimeError(f"grep failed (exit {r.returncode}): {r.stderr.strip()}")
    return [tuple(l.split(":", 2)[:2]) for l in r.stdout.splitlines()]


def files():
    for root in (HARNESS, TESTS):
        for dirpath, dirnames, names in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in names:
                if name not in ALLOWED and not name.endswith(".pyc"):
                    yield Path(dirpath) / name


class NoPersonalData(unittest.TestCase):
    def test_scanner_finds_planted_values_and_fails_on_errors(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "patterns").write_text("TESTSTICK0001\n192\\.0\\.2\\.\n")
            (d / "a.txt").write_text("a\nserial TESTSTICK0001 here\nip=192.0.2.10\n")
            (d / "b.txt").write_text("nothing here\n")
            self.assertEqual(scan([d / "a.txt", d / "b.txt"], d / "patterns"),
                             [(str(d / "a.txt"), "2"), (str(d / "a.txt"), "3")])
            self.assertEqual(scan([d / "b.txt"], d / "patterns"), [])
            with self.assertRaises(RuntimeError):
                scan([d / "missing.txt"], d / "patterns")
            (d / "bad").write_text("a[\n")
            with self.assertRaises(RuntimeError):
                scan([d / "b.txt"], d / "bad")

    def test_no_real_values_outside_the_local_file(self):
        if not PATTERNS.exists():
            self.skipTest(f"no {PATTERNS.relative_to(REPO)} here (public export)")
        self.assertTrue(PATTERNS.read_text().strip(), "the pattern list is empty")
        paths = list(files())
        self.assertGreater(len(paths), 20)
        found = scan(paths, PATTERNS)
        self.assertEqual(found, [], f"personal values in: {sorted(set(found))}")

    def test_this_file_holds_no_digest(self):
        import re
        self.assertIsNone(re.search(r"\b[0-9a-f]{40,}\b", Path(__file__).read_text()))


if __name__ == "__main__":
    unittest.main()
