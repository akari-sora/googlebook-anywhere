#!/usr/bin/env python3
"""Fail if the public export still contains a private-patterns hit.

The scan uses tools/release/private-patterns.txt and the same excludes as
tools/release/make-public.sh. A private checkout then runs that exporter on
HEAD. Matching text is not printed. Negative scenarios append
GBOS-TEST-PRIVATE-[0-9]{6} to a throwaway copy of that pattern file and plant
a matching line there. They do not read the vendored library fixture. The
tracked-ignored NUL case stays a synthetic snapshot.

A clean personal-data scan covers the public export tree only, not the private checkout and not an unscanned installer image.

History treats tools/release/private-patterns.txt as the private path. A commit that
touches tools/harness/kano-env.local.sh has its own message. research/ is not a
history signal. GBOS_REQUIRE_PRIVATE_SCAN, set in the private project's CI/CD
variables and not in this repo, forces the scan for any non-empty value other
than 0 or false: a missing pattern file fails. Unset, empty, 0, and false still
skip a genuine public export. Do not mark that variable "Protect variable".
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PATTERNS = REPO / "tools/release/private-patterns.txt"
EXPORTER = REPO / "tools/release/make-public.sh"
EXCLUDES = (
    "research",
    "tools/harness/kano-env.local.sh",
    "tools/release/private-patterns.txt",
)
# History refuses private-patterns.txt ("history touched a private path") and
# kano-env.local.sh (its own message). research/ is not a history signal.
HISTORY_PRIVATE = "tools/release/private-patterns.txt"
HISTORY_PERSONAL = "tools/harness/kano-env.local.sh"
REQUIRE_PRIVATE_SCAN = "GBOS_REQUIRE_PRIVATE_SCAN"
MARKER_REL = "tools/release/PUBLIC-EXPORT"
# A token that cannot occur in real data. The character class is not six digits,
# so this source line is not itself a hit when the pattern is appended.
SYNTH_PATTERN = "GBOS-TEST-PRIVATE-[0-9]{6}"
SYNTH_REL = "tests/stick/export-synth-token.txt"
COMMIT_FIRST = "commit first: the export test checks HEAD, not the work tree"
EXPORT_SCOPE = (
    "A clean personal-data scan covers the public export tree only, "
    "not the private checkout and not an unscanned installer image."
)


def fail(msg):
    print(f"public-export: {msg}", file=sys.stderr)
    raise SystemExit(1)


def excluded(rel):
    for item in EXCLUDES:
        if rel == item or rel.startswith(item + "/"):
            return True
    return False


def pattern_text():
    lines = PATTERNS.read_text(encoding="utf-8").splitlines()
    if not lines or any(not line.strip() or line.lstrip().startswith("#") for line in lines):
        fail("pattern file is empty or has a comment")
    return lines


def scan_tree(root):
    rx = re.compile("|".join(f"(?:{line})" for line in pattern_text()), re.IGNORECASE)
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in (".git", "__pycache__")]
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if excluded(rel):
                continue
            if rx.search(rel):
                hits.append(rel + ":name")
            try:
                data = path.read_bytes()
            except OSError as err:
                fail(f"cannot read {rel}: {err.strerror}")
            if b"\0" in data:
                continue
            text = data.decode("utf-8", "surrogateescape")
            for number, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{number}")
    return hits


def git_env():
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "public-export-test",
        "GIT_AUTHOR_EMAIL": "public-export-test@example.invalid",
        "GIT_COMMITTER_NAME": "public-export-test",
        "GIT_COMMITTER_EMAIL": "public-export-test@example.invalid",
    })
    return env


def tracked_files(repo):
    """Paths tracked in this checkout, including files that match .gitignore."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    return [name.decode() for name in proc.stdout.split(b"\0") if name]


def materialize(src, tree, rels):
    for rel in rels:
        origin = src / rel
        if not origin.is_file():
            continue
        dest = tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, dest, follow_symlinks=True)


def commit_paths(tree, rels, env):
    init = subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
    )
    if init.returncode != 0:
        fail("git init failed: " + (init.stderr or init.stdout).strip())
    present = [rel for rel in rels if (tree / rel).is_file()]
    if not present:
        fail("snapshot has no tracked files")
    for start in range(0, len(present), 80):
        chunk = present[start:start + 80]
        add = subprocess.run(
            ["git", "add", "-f", "--"] + chunk,
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
        )
        if add.returncode != 0:
            fail("git add failed: " + (add.stderr or add.stdout).strip())
    commit = subprocess.run(
        ["git", "commit", "-m", "public export snapshot"],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        fail("git commit failed: " + (commit.stderr or commit.stdout).strip())
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return head.stdout.strip()


def run_exporter(tree, head, dest, env):
    return subprocess.run(
        ["bash", str(tree / "tools/release/make-public.sh"), head, str(dest)],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
    )


def git_checkout(root):
    """True when root itself is a git work tree. A parent repository does not count."""
    return (root / ".git").exists()


def pattern_tracked(root):
    """git ls-files --error-unmatch succeeds when the pattern file is in the index."""
    if not git_checkout(root):
        return False
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", "tools/release/private-patterns.txt"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def has_head_commit(root):
    """True when `git rev-parse --verify -q HEAD` resolves in root."""
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def private_path_present(root, rel):
    """True when rel is part of this checkout.

    A git checkout decides by the index (`git ls-files`), so an untracked
    kano-env.local.sh or research/ directory does not count. When HEAD is
    still unborn, a path in the work tree also counts. A directory that
    is not a git checkout has no index; a path in the work tree counts.
    """
    if git_checkout(root):
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", rel],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return True
        if not has_head_commit(root):
            return (root / rel).exists()
        return False
    return (root / rel).exists()


def excludes_absent(root):
    return all(not private_path_present(root, item) for item in EXCLUDES)


def marker_sha(text):
    """The marker body, or None. The exporter writes one SHA and a newline."""
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\n", text):
        return text[:-1]
    return None


def history_has_path(root, path):
    """True when any commit in `--all` refs touches path.

    `git rev-list` only sees commits this clone actually has. A shallow clone
    drops history beyond its depth, so an older add or delete of path can be
    invisible. Depth has to reach the parent of the commit that dropped the
    path, which is two commits deeper than that commit's distance from HEAD.
    """
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-list", "-n1", "--all", "--", path],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        fail("git rev-list failed: " + (proc.stderr or proc.stdout).strip())
    return bool(proc.stdout.strip())


def history_touched_private(root):
    """True when history touches tools/release/private-patterns.txt.

    That file is never legitimate in a public repo. research/ is not checked.
    tools/harness/kano-env.local.sh is reported on its own.
    """
    return history_has_path(root, HISTORY_PRIVATE)


def private_scan_required():
    """True when the private CI variable forces a scan. Never a repo file.

    Unset, empty, 0, and false (any case; surrounding whitespace ignored) do
    not force it. Any other non-empty value does, including 1 and true.
    """
    raw = os.environ.get(REQUIRE_PRIVATE_SCAN)
    if raw is None:
        return False
    value = raw.strip()
    if value == "" or value == "0" or value.lower() == "false":
        return False
    return True


def marker_names_commit(root, sha):
    """True when `git cat-file -e <sha>^{commit}` succeeds in this clone.

    The same depth limit as history_touched_private applies: a marker SHA that
    is not in the shallow clone does not resolve.
    """
    proc = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def is_public_export_tree(root=REPO):
    """SKIP only for a tree that carries the exporter's marker.

    The marker file must full-match one 40- or 64-hex SHA and a newline.
    The three excluded paths must be absent from the index in a git checkout,
    or from the work tree when there is no index. A directory that is not a git
    checkout has no index and no history; the marker text is the whole check.

    A git checkout must also have the pattern file out of the index. Do not
    SKIP when `git rev-list -n1 --all -- tools/release/private-patterns.txt`
    prints a commit (history touched a private path), when history touches
    tools/harness/kano-env.local.sh (personal settings file in history; remove
    it before merging), or when `git cat-file -e <marker>^{commit}` succeeds
    (the marker names a commit in this clone). research/ is not a history
    signal. A real export's marker is the private source SHA, which is not an
    object in the public clone, and that clone's history never contained the
    pattern file.

    Shallow clones only contain commits within their depth. Both git commands
    miss a private-path commit or a marker SHA beyond that depth, so a shallow
    checkout can look like a genuine export even when a full clone would fail.
    Any non-empty GBOS_REQUIRE_PRIVATE_SCAN other than 0 or false closes that
    on the private CI: main() then fails a missing pattern file instead of
    skipping. This SKIP is not a scan.
    """
    path = root / MARKER_REL
    if not path.is_file():
        return False
    if not excludes_absent(root):
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError:
        text = ""
    reasons = []
    sha = marker_sha(text)
    if sha is None:
        reasons.append("PUBLIC-EXPORT marker is not a 40- or 64-hex SHA")
    if git_checkout(root):
        if pattern_tracked(root):
            return False
        if history_touched_private(root):
            reasons.append("history touched a private path")
        if history_has_path(root, HISTORY_PERSONAL):
            reasons.append("personal settings file in history; remove it before merging")
        if sha is not None and marker_names_commit(root, sha):
            reasons.append("PUBLIC-EXPORT marker names a commit in this repo")
    if reasons:
        fail("; ".join(reasons))
    return True


def snapshot_export():
    """Export HEAD of this repo with make-public.sh.

    The exporter reads that commit with git ls-tree and git archive. It does not
    modify the repository. A dirty index or work tree (tracked changes only) is
    refused: this test checks HEAD, not the work tree.
    """
    if not git_checkout(REPO) or not has_head_commit(REPO):
        fail("no commit yet (commit first)")
    status = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        fail("git status failed: " + (status.stderr or status.stdout).strip())
    if status.stdout.strip():
        fail(COMMIT_FIRST)
    before = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="gbos-public-snapshot-"))
    try:
        dest = tmp / "export"
        proc = subprocess.run(
            ["bash", "tools/release/make-public.sh", head, str(dest)],
            cwd=str(REPO),
            capture_output=True,
            text=True,
        )
        after = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        if before.stdout != after.stdout:
            fail("make-public.sh modified the repo")
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            fail(f"make-public.sh exited {proc.returncode}")
        if "personal-data scan clean" not in proc.stdout:
            fail("make-public.sh did not report a clean scan")
        if not dest.is_dir():
            fail("export directory was not published")
        marker = (dest / MARKER_REL).read_text(encoding="utf-8").strip()
        if marker != head:
            fail("PUBLIC-EXPORT marker is not the source commit")
    finally:
        shutil.rmtree(tmp)


def negative_tracked_ignored_nul():
    """A tracked file that matches an ignore rule and contains NUL must be refused.

    make-public.sh's binary rule is unchanged. The snapshot adds the file with -f,
    the same way it keeps other tracked-but-ignored paths, and the export must
    still refuse it.
    """
    tmp = Path(tempfile.mkdtemp(prefix="gbos-public-nul-"))
    try:
        dest = tmp / "export"
        tree = tmp / "tree"
        tree.mkdir()
        rels = tracked_files(REPO)
        materialize(REPO, tree, rels)
        ensure_synth_pattern(tree)
        write_synth_file(tree)
        rel = "tests/harness/fixtures/tracked-ignored-nul.bin"
        probe = tree / rel
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"\0")
        env = git_env()
        head = commit_paths(tree, rels + [rel, SYNTH_REL], env)
        listed = subprocess.run(
            ["git", "-C", str(tree), "ls-files", "--", rel],
            capture_output=True,
            text=True,
            check=True,
        )
        if listed.stdout.strip() != rel:
            fail("NUL probe was not tracked in the snapshot")
        ignored = subprocess.run(
            ["git", "-C", str(tree), "check-ignore", "--no-index", "-v", "--", rel],
            capture_output=True,
            text=True,
        )
        if ignored.returncode != 0 or "*.bin" not in ignored.stdout:
            fail("NUL probe does not match an ignore rule")
        proc = run_exporter(tree, head, dest, env)
        text = proc.stdout + proc.stderr
        if proc.returncode == 0 or "refusing: binary file (contains NUL bytes)" not in text:
            fail("tracked ignored NUL file did not make the export refuse: " + text.strip())
        if rel not in text:
            fail("export refusal did not name the tracked NUL file")
        print("public-export: tracked ignored NUL refused")
    finally:
        shutil.rmtree(tmp)


def require_scope():
    for rel in ("tools/release/make-public.sh", "tools/harness/README.md"):
        text = (REPO / rel).read_text(encoding="utf-8")
        if EXPORT_SCOPE not in text:
            fail(f"{rel} treats a clean scan as the whole checkout or an unscanned image")
    exporter = (REPO / "tools/release/make-public.sh").read_text(encoding="utf-8")
    if "tools/harness/kano-env.local.sh" not in exporter:
        fail("the exporter no longer drops the private settings file")


def proc_tail(proc):
    lines = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
    return "\n".join(lines[-40:])


def use_running_script(root):
    """Run this process's test file inside a throwaway copy.

    git clone copies HEAD. An uncommitted edit of this file would otherwise
    leave the nested run on the old script. Commit the edit in the copy when
    the bytes differ so a scenario that must be a clean tree stays clean.
    """
    script = root / "tests" / "stick" / "public-export-test.py"
    current = Path(__file__).read_bytes()
    if script.is_file() and script.read_bytes() == current:
        return
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(current)
    if not git_checkout(root) or not has_head_commit(root):
        return
    git_ok(["git", "add", "--", "tests/stick/public-export-test.py"], root)
    staged = git_ok(
        ["git", "status", "--porcelain", "--", "tests/stick/public-export-test.py"],
        root,
    )
    if staged.stdout.strip():
        git_ok(["git", "commit", "-q", "-m", "use the running export test"], root)


def run_nested(root, extra_env=None):
    """Run this test in root.

    GBOS_REQUIRE_PRIVATE_SCAN is dropped unless extra_env sets it. Nested
    public-tree checks have to skip even when private CI exports the variable.
    """
    use_running_script(root)
    env = git_env()
    env.pop(REQUIRE_PRIVATE_SCAN, None)
    env["GBOS_PUBLIC_EXPORT_NESTED"] = "1"
    if extra_env:
        env.update(extra_env)
    script = root / "tests" / "stick" / "public-export-test.py"
    return subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )


def shallow_clone(src, dest, depth):
    """A real shallow clone. A local --local clone would copy every object."""
    url = Path(src).resolve().as_uri()
    proc = subprocess.run(
        ["git", "clone", "--depth", str(depth), "--no-local", "--quiet", url, str(dest)],
        env=git_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        fail("git clone failed: " + (proc.stderr or proc.stdout).strip())


def clone_repo(src, dest):
    proc = subprocess.run(
        # --no-hardlinks: the checkout and $TMPDIR are often different filesystems (tmpfs /tmp), and
        # current git dies on a cross-device hardlink instead of copying.
        ["git", "clone", "--local", "--no-hardlinks", "--quiet", str(src), str(dest)],
        env=git_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        fail("git clone failed: " + (proc.stderr or proc.stdout).strip())


def git_ok(args, root):
    proc = subprocess.run(args, cwd=str(root), env=git_env(), capture_output=True, text=True)
    if proc.returncode != 0:
        fail(" ".join(args[:3]) + " failed: " + (proc.stderr or proc.stdout).strip())
    return proc


def remove_excludes(root):
    for item in EXCLUDES:
        path = root / item
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()


def synth_body():
    """Two matching lines. Joined at runtime so this source is not itself a hit."""
    prefix = "GBOS-TEST-PRIVATE-"
    return "".join(prefix + f"{number:06d}\n" for number in (1, 2))


def synth_line_numbers(text):
    rx = re.compile(SYNTH_PATTERN)
    return [number for number, line in enumerate(text.splitlines(), 1) if rx.search(line)]


def ensure_synth_pattern(root):
    """Append the synthetic pattern to this copy. Does not open the vendored fixture."""
    path = root / "tools/release/private-patterns.txt"
    if not path.is_file():
        fail("throwaway copy has no pattern file to extend")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if SYNTH_PATTERN not in lines:
        if text and not text.endswith("\n"):
            text += "\n"
        text = text + SYNTH_PATTERN + "\n"
        path.write_text(text, encoding="utf-8")
        lines = text.splitlines()
    if not lines or lines[-1] != SYNTH_PATTERN:
        fail("synthetic pattern was not appended")


def write_synth_file(root):
    dest = root / SYNTH_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(synth_body(), encoding="utf-8")


def redact_synth_file(root):
    (root / SYNTH_REL).write_text("removed\n", encoding="utf-8")


def commit_rel(root, rels, message):
    git_ok(["git", "add", "--"] + list(rels), root)
    git_ok(["git", "commit", "-q", "-m", message], root)


def install_synth(root, commit=False, redact=False, commit_redaction=False):
    """Plant a private-pattern hit that exists only in this throwaway copy.

    commit puts the pattern line and the planted file on HEAD, for a scenario
    whose export of HEAD must still see the hit. redact removes the hit from
    the work tree. commit_redaction records that removal, for a scenario whose
    dirty state is a different file (deleted NUL, or a tree that must be clean
    before a symlink commit).
    """
    ensure_synth_pattern(root)
    write_synth_file(root)
    if commit:
        commit_rel(
            root,
            ["tools/release/private-patterns.txt", SYNTH_REL],
            "plant synthetic private token",
        )
    if redact:
        redact_synth_file(root)
        if synth_line_numbers((root / SYNTH_REL).read_text(encoding="utf-8")):
            fail("synthetic token is still in the work tree")
        if commit_redaction:
            commit_rel(root, [SYNTH_REL], "redact synthetic private token")


def export_head(root, dest):
    """Run make-public.sh on HEAD and return (sha, status before, status after, proc)."""
    sha = git_ok(["git", "rev-parse", "HEAD"], root).stdout.strip()
    before = git_ok(["git", "status", "--porcelain"], root).stdout
    proc = subprocess.run(
        ["bash", "tools/release/make-public.sh", sha, str(dest)],
        cwd=str(root),
        env=git_env(),
        capture_output=True,
        text=True,
    )
    after = git_ok(["git", "status", "--porcelain"], root).stdout
    return sha, before, after, proc


def prove_positive_export(tmp):
    """A real make-public.sh export carries the marker, and that tree SKIPs."""
    synth = tmp / "synth"
    (synth / "tools" / "release").mkdir(parents=True)
    for name in ("make-public.sh", "png-ok.py", "private-patterns.txt"):
        shutil.copy(REPO / "tools" / "release" / name, synth / "tools" / "release" / name)
    (synth / "README.md").write_text("public export fixture\n", encoding="utf-8")
    git_ok(["git", "init", "-q", "-b", "main"], synth)
    git_ok(
        ["git", "add", "--", "README.md", "tools/release/make-public.sh", "tools/release/png-ok.py"],
        synth,
    )
    git_ok(["git", "commit", "-q", "-m", "synthetic public source"], synth)
    dest = tmp / "positive-export"
    sha, before, after, proc = export_head(synth, dest)
    if before != after:
        fail("make-public.sh modified the synthetic repo")
    text = proc.stdout + proc.stderr
    if proc.returncode != 0 or "personal-data scan clean" not in proc.stdout:
        fail("positive export did not succeed: " + proc_tail(proc))
    marker = (dest / MARKER_REL).read_text(encoding="utf-8").strip()
    if marker != sha:
        fail("positive export marker is not the source commit")
    for item in EXCLUDES:
        if (dest / item).exists():
            fail(f"positive export still has {item}")
    script = dest / "tests" / "stick" / "public-export-test.py"
    script.parent.mkdir(parents=True)
    shutil.copy(Path(__file__), script)
    skipped = run_nested(dest)
    if skipped.returncode != 0 or "SKIP public-export: PUBLIC-EXPORT marker (public tree)" not in skipped.stdout:
        fail("positive export did not skip: " + proc_tail(skipped))
    pub = tmp / "pubgit"
    shutil.copytree(dest, pub)
    git_ok(["git", "init", "-q", "-b", "main"], pub)
    if has_head_commit(pub):
        fail("freshly initialized public repo unexpectedly has a HEAD commit")
    skipped_unborn = run_nested(pub)
    if skipped_unborn.returncode != 0 or "SKIP public-export: PUBLIC-EXPORT marker (public tree)" not in skipped_unborn.stdout:
        fail("public git init with unborn HEAD did not skip: " + proc_tail(skipped_unborn))
    git_ok(["git", "add", "-A", "--"], pub)
    skipped_staged_unborn = run_nested(pub)
    if skipped_staged_unborn.returncode != 0 or "SKIP public-export: PUBLIC-EXPORT marker (public tree)" not in skipped_staged_unborn.stdout:
        fail("public git init with staged files and unborn HEAD did not skip: " + proc_tail(skipped_staged_unborn))
    print("public-export: unborn HEAD in public export tree skips")
    git_ok(["git", "commit", "-q", "-m", "public checkout"], pub)
    skipped_git = run_nested(pub)
    if skipped_git.returncode != 0 or "SKIP public-export:" not in skipped_git.stdout:
        fail("public git checkout did not skip: " + proc_tail(skipped_git))
    prove_public_history_and_scan_switch(tmp, pub, dest)
    tracked = tmp / "tracked-pattern"
    shutil.copytree(pub, tracked)
    shutil.copy(REPO / "tools/release/private-patterns.txt", tracked / "tools/release/private-patterns.txt")
    ensure_synth_pattern(tracked)
    write_synth_file(tracked)
    git_ok(["git", "add", "-f", "--", "tools/release/private-patterns.txt", SYNTH_REL], tracked)
    (tracked / "tools/release/private-patterns.txt").unlink()
    refused = run_nested(tracked)
    if refused.returncode == 0 or "SKIP" in refused.stdout or "still tracked" not in refused.stderr:
        fail("tracked pattern file in a public-looking tree did not fail: " + proc_tail(refused))
    (synth / MARKER_REL).write_text(sha + "\n", encoding="utf-8")
    git_ok(["git", "add", "--", MARKER_REL], synth)
    git_ok(["git", "commit", "-q", "-m", "commit the export marker"], synth)
    again = export_head(synth, tmp / "reexport")[3]
    again_text = again.stdout + again.stderr
    if again.returncode == 0 or "already contains tools/release/PUBLIC-EXPORT" not in again_text:
        fail("re-export of a marked commit was not refused: " + proc_tail(again))
    print("public-export: positive export has marker and skips")


def prove_public_history_and_scan_switch(tmp, pub, dest):
    """research/ history still skips. Personal settings history does not.

    GBOS_REQUIRE_PRIVATE_SCAN never skips when it is any non-empty value other
    than 0 or false. Unset, 0, and false still skip a genuine public export,
    including a shallow clone of one. An untracked personal settings file or
    research/ directory does not make this git checkout look private.
    """
    off = run_nested(pub, {REQUIRE_PRIVATE_SCAN: "0"})
    if off.returncode != 0 or "SKIP public-export:" not in off.stdout:
        fail("GBOS_REQUIRE_PRIVATE_SCAN=0 did not skip a public export: " + proc_tail(off))
    print("public-export: private scan not required skips a public tree")

    forced = run_nested(pub, {REQUIRE_PRIVATE_SCAN: "1"})
    if (
        forced.returncode == 0
        or "SKIP" in forced.stdout
        or "missing tools/release/private-patterns.txt" not in forced.stderr
    ):
        fail("GBOS_REQUIRE_PRIVATE_SCAN=1 skipped a public export: " + proc_tail(forced))
    print("public-export: private scan required does not skip a public tree")

    forced_tree = run_nested(dest, {REQUIRE_PRIVATE_SCAN: "1"})
    if (
        forced_tree.returncode == 0
        or "SKIP" in forced_tree.stdout
        or "missing tools/release/private-patterns.txt" not in forced_tree.stderr
    ):
        fail("GBOS_REQUIRE_PRIVATE_SCAN=1 skipped a public export tree: " + proc_tail(forced_tree))
    print("public-export: private scan required does not skip a marker tree")

    def expect_skip(proc, label):
        if proc.returncode != 0 or "SKIP public-export:" not in proc.stdout:
            fail(label + " did not skip: " + proc_tail(proc))

    def expect_force(proc, label):
        if (
            proc.returncode == 0
            or "SKIP" in proc.stdout
            or "missing tools/release/private-patterns.txt" not in proc.stderr
        ):
            fail(label + " did not force the scan: " + proc_tail(proc))

    expect_skip(run_nested(pub, {REQUIRE_PRIVATE_SCAN: "false"}), "GBOS_REQUIRE_PRIVATE_SCAN=false")
    print("public-export: private scan value false skips")
    expect_skip(run_nested(pub, {REQUIRE_PRIVATE_SCAN: "FALSE"}), "GBOS_REQUIRE_PRIVATE_SCAN=FALSE")
    print("public-export: private scan value FALSE skips")
    expect_skip(run_nested(pub, {REQUIRE_PRIVATE_SCAN: ""}), "GBOS_REQUIRE_PRIVATE_SCAN empty")
    print("public-export: private scan value empty skips")
    expect_force(run_nested(pub, {REQUIRE_PRIVATE_SCAN: "true"}), "GBOS_REQUIRE_PRIVATE_SCAN=true")
    print("public-export: mutation private scan true: red")
    expect_force(run_nested(pub, {REQUIRE_PRIVATE_SCAN: "yes"}), "GBOS_REQUIRE_PRIVATE_SCAN=yes")
    print("public-export: mutation private scan yes: red")
    expect_force(run_nested(pub, {REQUIRE_PRIVATE_SCAN: " 1"}), "GBOS_REQUIRE_PRIVATE_SCAN spaced 1")
    print("public-export: mutation private scan spaced 1: red")
    expect_force(run_nested(pub, {REQUIRE_PRIVATE_SCAN: "2"}), "GBOS_REQUIRE_PRIVATE_SCAN=2")
    print("public-export: mutation private scan 2: red")

    personal = pub / HISTORY_PERSONAL
    personal.parent.mkdir(parents=True, exist_ok=True)
    personal.write_text("local-settings-placeholder\n", encoding="utf-8")
    listed = subprocess.run(
        ["git", "-C", str(pub), "ls-files", "--error-unmatch", "--", HISTORY_PERSONAL],
        capture_output=True,
        text=True,
    )
    if listed.returncode == 0:
        fail("untracked personal settings file is in the index")
    untracked_personal = run_nested(pub)
    personal.unlink()
    if (
        untracked_personal.returncode != 0
        or "SKIP public-export:" not in untracked_personal.stdout
        or "private checkout" in untracked_personal.stderr
    ):
        fail("untracked kano-env.local.sh made a public checkout look private: " + proc_tail(untracked_personal))
    print("public-export: untracked kano-env.local.sh stays a public checkout")

    research = pub / "research" / "x.md"
    research.parent.mkdir(parents=True, exist_ok=True)
    research.write_text("public notes\n", encoding="utf-8")
    listed = subprocess.run(
        ["git", "-C", str(pub), "ls-files", "--error-unmatch", "--", "research"],
        capture_output=True,
        text=True,
    )
    if listed.returncode == 0:
        fail("untracked research is in the index")
    untracked_research = run_nested(pub)
    shutil.rmtree(pub / "research")
    if (
        untracked_research.returncode != 0
        or "SKIP public-export:" not in untracked_research.stdout
        or "private checkout" in untracked_research.stderr
    ):
        fail("untracked research made a public checkout look private: " + proc_tail(untracked_research))
    print("public-export: untracked research stays a public checkout")

    worktree_research = dest / "research"
    worktree_research.mkdir()
    (worktree_research / "x.md").write_text("x\n", encoding="utf-8")
    non_git = run_nested(dest)
    shutil.rmtree(worktree_research)
    if non_git.returncode == 0 or "private checkout" not in non_git.stderr:
        fail("non-git work tree research was not treated as private: " + proc_tail(non_git))
    print("public-export: non-git research path still looks private")

    shallow_pub = tmp / "pub-shallow"
    shallow_clone(pub, shallow_pub, 1)
    shallow_skip = run_nested(shallow_pub)
    if shallow_skip.returncode != 0 or "SKIP public-export:" not in shallow_skip.stdout:
        fail("shallow public export did not skip: " + proc_tail(shallow_skip))
    print("public-export: shallow public export skips")

    research_src = tmp / "research-src"
    clone_repo(pub, research_src)
    (research_src / "research").mkdir()
    (research_src / "research" / "README.md").write_text("public notes\n", encoding="utf-8")
    git_ok(["git", "add", "--", "research/README.md"], research_src)
    git_ok(["git", "commit", "-q", "-m", "add public research notes"], research_src)
    git_ok(["git", "rm", "-q", "--", "research/README.md"], research_src)
    git_ok(["git", "commit", "-q", "-m", "remove public research notes"], research_src)
    research_clone = tmp / "research-clone"
    clone_repo(research_src, research_clone)
    if (research_clone / "research").exists():
        fail("fresh clone still has research/")
    skipped_research = run_nested(research_clone)
    if skipped_research.returncode != 0 or "SKIP public-export:" not in skipped_research.stdout:
        fail("public repo that added and removed research/ did not skip: " + proc_tail(skipped_research))
    if "history touched a private path" in skipped_research.stderr:
        fail("research/ history was reported as a private path")
    print("public-export: research added and removed still skips")

    personal_src = tmp / "personal-src"
    clone_repo(pub, personal_src)
    personal_path = personal_src / HISTORY_PERSONAL
    personal_path.parent.mkdir(parents=True, exist_ok=True)
    personal_path.write_text("personal-settings-placeholder\n", encoding="utf-8")
    git_ok(["git", "add", "-f", "--", HISTORY_PERSONAL], personal_src)
    git_ok(["git", "commit", "-q", "-m", "add personal settings"], personal_src)
    git_ok(["git", "rm", "-q", "--", HISTORY_PERSONAL], personal_src)
    git_ok(["git", "commit", "-q", "-m", "remove personal settings"], personal_src)
    personal_clone = tmp / "personal-clone"
    clone_repo(personal_src, personal_clone)
    if (personal_clone / HISTORY_PERSONAL).exists():
        fail("fresh clone still has the personal settings file")
    refused = run_nested(personal_clone)
    if refused.returncode == 0 or "SKIP" in refused.stdout:
        fail("personal settings history did not fail: " + proc_tail(refused))
    if "personal settings file in history; remove it before merging" not in refused.stderr:
        fail("personal settings history missing its message: " + proc_tail(refused))
    if "history touched a private path" in refused.stderr:
        fail("personal settings history was reported as a private path: " + proc_tail(refused))
    print("public-export: personal settings file in history refused")


def plant_forged_marker(root, marker_text):
    """Delete the three excluded paths and commit marker_text on top."""
    git_ok(["git", "rm", "-r", "-q", "--", *EXCLUDES], root)
    marker = root / MARKER_REL
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(marker_text, encoding="utf-8")
    git_ok(["git", "add", "--", MARKER_REL], root)
    git_ok(["git", "commit", "-q", "-m", "forge a PUBLIC-EXPORT marker"], root)


def refuse_forged_clone(cloned, label, needles):
    proc = run_nested(cloned)
    if proc.returncode == 0 or "SKIP" in proc.stdout:
        fail(label + " did not fail: " + proc_tail(proc))
    for needle in needles:
        if needle not in proc.stderr:
            fail(label + " missing " + needle + ": " + proc_tail(proc))
    print("public-export: " + label)


def prove_skip_signal():
    """Private trees must not take the public SKIP, including a real export."""
    tmp = Path(tempfile.mkdtemp(prefix="gbos-public-skip-"))
    try:
        deleted = tmp / "deleted"
        clone_repo(REPO, deleted)
        install_synth(deleted, commit=True)
        remove_excludes(deleted)
        proc = run_nested(deleted)
        if proc.returncode == 0 or "SKIP" in proc.stdout or "still tracked" not in proc.stderr:
            fail("work-tree delete of excludes did not fail: " + proc_tail(proc))
        print("public-export: work-tree excludes deleted refused")

        src = tmp / "rmsrc"
        clone_repo(REPO, src)
        install_synth(src, commit=True)
        git_ok(["git", "rm", "-r", "-q", "--", *EXCLUDES], src)
        git_ok(["git", "commit", "-q", "-m", "drop private excludes"], src)
        cloned = tmp / "rmclone"
        clone_repo(src, cloned)
        proc = run_nested(cloned)
        if proc.returncode == 0 or "SKIP" in proc.stdout or "missing tools/release/private-patterns.txt" not in proc.stderr:
            fail("git-rm clone did not fail closed: " + proc_tail(proc))
        print("public-export: private commit without excludes refused")

        marked = tmp / "marked"
        clone_repo(REPO, marked)
        install_synth(marked, commit=True)
        (marked / MARKER_REL).parent.mkdir(parents=True, exist_ok=True)
        (marked / MARKER_REL).write_text("not-a-public-export\n", encoding="utf-8")
        proc = run_nested(marked)
        if proc.returncode == 0 or "SKIP" in proc.stdout or "in a private checkout" not in proc.stderr:
            fail("marker in a private tree did not fail: " + proc_tail(proc))
        print("public-export: marker in private tree refused")

        arbitrary = tmp / "forge-text"
        clone_repo(REPO, arbitrary)
        install_synth(arbitrary, commit=True)
        plant_forged_marker(arbitrary, "forged-not-a-sha\n")
        arbitrary_clone = tmp / "forge-text-clone"
        clone_repo(arbitrary, arbitrary_clone)
        if not history_has_path(arbitrary_clone, HISTORY_PRIVATE):
            fail("arbitrary-marker clone history did not touch a private path")
        refuse_forged_clone(
            arbitrary_clone,
            "forged marker text refused",
            (
                "PUBLIC-EXPORT marker is not a 40- or 64-hex SHA",
                "history touched a private path",
            ),
        )

        ancestor_src = tmp / "forge-ancestor"
        clone_repo(REPO, ancestor_src)
        install_synth(ancestor_src, commit=True)
        ancestor = git_ok(["git", "rev-parse", "HEAD"], ancestor_src).stdout.strip()
        if marker_sha(ancestor + "\n") != ancestor:
            fail("ancestor id is not a 40- or 64-hex SHA")
        plant_forged_marker(ancestor_src, ancestor + "\n")
        ancestor_clone = tmp / "forge-ancestor-clone"
        clone_repo(ancestor_src, ancestor_clone)
        head = git_ok(["git", "rev-parse", "HEAD"], ancestor_clone).stdout.strip()
        if ancestor == head:
            fail("ancestor marker is the clone HEAD, not an ancestor")
        is_ancestor = subprocess.run(
            ["git", "-C", str(ancestor_clone), "merge-base", "--is-ancestor", ancestor, "HEAD"],
            capture_output=True,
            text=True,
        )
        if is_ancestor.returncode != 0:
            fail("planted marker does not name an ancestor")
        named = subprocess.run(
            ["git", "-C", str(ancestor_clone), "cat-file", "-e", f"{ancestor}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if named.returncode != 0:
            fail("ancestor marker does not resolve in the clone")
        if not history_has_path(ancestor_clone, HISTORY_PRIVATE):
            fail("ancestor-marker clone history did not touch a private path")
        refuse_forged_clone(
            ancestor_clone,
            "forged ancestor marker refused",
            (
                "history touched a private path",
                "PUBLIC-EXPORT marker names a commit in this repo",
            ),
        )

        shallow = tmp / "forge-ancestor-shallow"
        shallow_clone(ancestor_src, shallow, 1)
        if not (shallow / ".git" / "shallow").is_file():
            fail("forged marker depth clone was not shallow")
        if history_has_path(shallow, HISTORY_PRIVATE):
            fail("shallow forged clone can still see private-patterns history")
        named = subprocess.run(
            ["git", "-C", str(shallow), "cat-file", "-e", f"{ancestor}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if named.returncode == 0:
            fail("shallow clone resolved the forged marker commit")
        forced = run_nested(shallow, {REQUIRE_PRIVATE_SCAN: "1"})
        if forced.returncode == 0 or "SKIP" in forced.stdout:
            fail("shallow forged marker with GBOS_REQUIRE_PRIVATE_SCAN=1 did not fail: " + proc_tail(forced))
        if "missing tools/release/private-patterns.txt" not in forced.stderr:
            fail(
                "shallow forged marker with GBOS_REQUIRE_PRIVATE_SCAN=1 missing pattern failure: "
                + proc_tail(forced)
            )
        print("public-export: shallow forged marker with private scan required refused")

        orphan = tmp / "forge-orphan"
        clone_repo(REPO, orphan)
        install_synth(orphan, commit=True)
        # Keep private history reachable even from a tagless, detached clone.
        git_ok(["git", "branch", "private-history", "HEAD"], orphan)
        git_ok(["git", "checkout", "-q", "--orphan", "clean"], orphan)
        git_ok(["git", "rm", "-r", "-f", "-q", "--", *EXCLUDES], orphan)
        (orphan / MARKER_REL).parent.mkdir(parents=True, exist_ok=True)
        (orphan / MARKER_REL).write_text("1" * 40 + "\n", encoding="utf-8")
        if has_head_commit(orphan):
            fail("unborn orphan branch unexpectedly has a HEAD commit")
        if not history_has_path(orphan, HISTORY_PRIVATE):
            fail("unborn orphan clone did not see private-patterns history across --all")
        refuse_forged_clone(
            orphan,
            "orphan unborn branch with forged marker refused",
            ("history touched a private path",),
        )
        git_ok(["git", "add", "--", MARKER_REL], orphan)
        git_ok(["git", "commit", "-q", "-m", "orphan commit with forged marker"], orphan)
        if not has_head_commit(orphan):
            fail("committed orphan branch has no HEAD commit")
        if not history_has_path(orphan, HISTORY_PRIVATE):
            fail("committed orphan clone did not see private-patterns history across --all")
        refuse_forged_clone(
            orphan,
            "orphan committed branch with forged marker refused",
            ("history touched a private path",),
        )

        prove_positive_export(tmp)
    finally:
        shutil.rmtree(tmp)


def prove_head_export_cases():
    """Dirty fix, deleted NUL, a tracked symlink, and a planted scan hit must fail."""
    tmp = Path(tempfile.mkdtemp(prefix="gbos-public-head-"))
    try:
        dirty = tmp / "dirty"
        clone_repo(REPO, dirty)
        install_synth(dirty, commit=True, redact=True)
        proc = run_nested(dirty)
        if proc.returncode == 0 or COMMIT_FIRST not in proc.stderr or "public-export: clean" in proc.stdout:
            fail("dirty fix did not fail the export test: " + proc_tail(proc))
        _sha, before, after, exported = export_head(dirty, tmp / "dirty-out")
        if before != after:
            fail("make-public.sh modified the dirty repo")
        exported_text = exported.stdout + exported.stderr
        if (
            exported.returncode == 0
            or "personal data found in the export" not in exported_text
            or SYNTH_REL + ":1" not in exported_text
            or SYNTH_REL + ":2" not in exported_text
        ):
            fail("dirty fix: HEAD export did not still report the synthetic hits: " + proc_tail(exported))
        print("public-export: dirty fix refused")

        nul = tmp / "nul"
        clone_repo(REPO, nul)
        install_synth(nul, commit=True, redact=True, commit_redaction=True)
        rel = "tests/harness/fixtures/review-committed-nul.bin"
        (nul / rel).parent.mkdir(parents=True, exist_ok=True)
        (nul / rel).write_bytes(b"\0")
        git_ok(["git", "add", "-f", "--", rel], nul)
        git_ok(["git", "commit", "-q", "-m", "track a NUL file"], nul)
        (nul / rel).unlink()
        proc = run_nested(nul)
        if proc.returncode == 0 or COMMIT_FIRST not in proc.stderr:
            fail("deleted NUL file did not fail the export test: " + proc_tail(proc))
        _sha, before, after, exported = export_head(nul, tmp / "nul-out")
        if before != after:
            fail("make-public.sh modified the NUL repo")
        exported_text = exported.stdout + exported.stderr
        if exported.returncode == 0 or "refusing: binary file (contains NUL bytes)" not in exported_text or rel not in exported_text:
            fail("deleted NUL file: HEAD export did not refuse the blob: " + proc_tail(exported))
        print("public-export: deleted NUL file refused")

        linked = tmp / "link"
        clone_repo(REPO, linked)
        install_synth(linked, commit=True, redact=True, commit_redaction=True)
        link = linked / "docs" / "README-link.md"
        link.symlink_to("../README.md")
        git_ok(["git", "add", "--", "docs/README-link.md"], linked)
        git_ok(["git", "commit", "-q", "-m", "track a symlink"], linked)
        clean = git_ok(["git", "status", "--porcelain", "--untracked-files=no"], linked).stdout
        if clean.strip():
            fail("symlink case was not a clean commit")
        proc = run_nested(linked)
        text = proc.stdout + proc.stderr
        if proc.returncode == 0 or COMMIT_FIRST in text or "not a regular file" not in text or "120000" not in text:
            fail("tracked symlink did not fail the export test: " + proc_tail(proc))
        if "public-export: clean" in proc.stdout:
            fail("tracked symlink reported a clean export")
        print("public-export: tracked symlink refused")

        planted = tmp / "planted"
        clone_repo(REPO, planted)
        install_synth(planted, commit=True)
        clean = git_ok(["git", "status", "--porcelain", "--untracked-files=no"], planted).stdout
        if clean.strip():
            fail("planted scan_tree case was not a clean tree")
        proc = run_nested(planted)
        blob = proc.stdout + proc.stderr
        if proc.returncode == 0 or "public-export: clean" in proc.stdout or COMMIT_FIRST in blob:
            fail("scan_tree did not fail the planted tree: " + proc_tail(proc))
        if "private-pattern hit" not in proc.stderr or SYNTH_REL + ":1" not in proc.stderr:
            fail("scan_tree did not report the planted synthetic file: " + proc_tail(proc))
        if SYNTH_REL + ":2" not in proc.stderr:
            fail("scan_tree did not report both planted lines: " + proc_tail(proc))
        print("public-export: scan_tree reports a planted hit")

        unborn_priv = tmp / "unborn-private"
        unborn_priv.mkdir()
        materialize(
            REPO,
            unborn_priv,
            (
                "tools/release/private-patterns.txt",
                "tools/release/make-public.sh",
                "tools/harness/README.md",
                "tests/stick/public-export-test.py",
            ),
        )
        git_ok(["git", "init", "-q", "-b", "main"], unborn_priv)
        if has_head_commit(unborn_priv):
            fail("unborn private repo unexpectedly has a HEAD commit")
        proc = run_nested(unborn_priv)
        if proc.returncode == 0 or "SKIP" in proc.stdout or "public-export: no commit yet (commit first)" not in proc.stderr:
            fail("unborn HEAD in private checkout did not fail with no commit yet: " + proc_tail(proc))
        git_ok(["git", "add", "-A", "--"], unborn_priv)
        proc_staged = run_nested(unborn_priv)
        if proc_staged.returncode == 0 or "SKIP" in proc_staged.stdout or "public-export: no commit yet (commit first)" not in proc_staged.stderr:
            fail("staged unborn HEAD in private checkout did not fail with no commit yet: " + proc_tail(proc_staged))
        print("public-export: unborn HEAD in private checkout refused")
    finally:
        shutil.rmtree(tmp)


def main():
    nested = os.environ.get("GBOS_PUBLIC_EXPORT_NESTED") == "1"
    # A pattern file removed from the work tree but still in the index has its
    # own message. Check that before the broader "private checkout" refusal,
    # which also treats tracked paths as present.
    if not PATTERNS.is_file() and git_checkout(REPO) and pattern_tracked(REPO):
        fail("tools/release/private-patterns.txt is still tracked")
    if (REPO / MARKER_REL).is_file() and any(private_path_present(REPO, item) for item in EXCLUDES):
        fail("tools/release/PUBLIC-EXPORT is in a private checkout")
    if not PATTERNS.is_file():
        if private_scan_required():
            fail("missing tools/release/private-patterns.txt")
        if is_public_export_tree():
            print("SKIP public-export: PUBLIC-EXPORT marker (public tree)")
            return
        fail("missing tools/release/private-patterns.txt")
    if not git_checkout(REPO) or not has_head_commit(REPO):
        fail("no commit yet (commit first)")
    if not nested:
        prove_skip_signal()
        prove_head_export_cases()
    require_scope()
    if not nested:
        negative_tracked_ignored_nul()
    hits = scan_tree(REPO)
    if nested:
        # Each nested scenario plants SYNTH_REL and then asserts its own gate
        # (dirty HEAD, deleted NUL, symlink, missing pattern file). Pre-existing
        # hits must not decide that gate. A synthetic hit still fails. The
        # top-level run reports every hit.
        hits = [hit for hit in hits if hit.startswith(SYNTH_REL + ":")]
    if hits:
        print("private-pattern hit:", file=sys.stderr)
        for hit in hits:
            print(hit, file=sys.stderr)
        fail(f"{len(hits)} private-pattern hit(s)")
    snapshot_export()
    print("public-export: clean")


if __name__ == "__main__":
    main()
