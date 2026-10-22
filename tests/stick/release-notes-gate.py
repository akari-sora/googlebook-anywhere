#!/usr/bin/env python3
"""The package job refuses a tag unless the notes are released at fixed lines.

Line 1 names the tag, line 2 is empty, and line 3 is the released status.
Every line of the whole file is scanned for unpublished wording, with no code
fence exemption and no version-section boundary. The gate command is taken
from .gitlab-ci.yml and run on copies of the notes. It is not reimplemented
here. The variants are built from a synthetic template: the real notes stay
unreleased until the tag commit, and this test must keep passing on that commit.
The release job's parsed script is also exercised under sh with the available
shells; output and mutation checks cover its description (the notes file verbatim plus the history link).

The guarantee is structural: a release publishes exactly docs/RELEASE-NOTES.md (plus the
history link), and the documented procedure moves the previous version out of that file
into docs/RELEASE-HISTORY.md. The heading lint is a best-effort authoring aid that only
catches the most common slip; it is not a Markdown parser, and it must never refuse valid
current-version content: a false positive is the worse error. Its one rule, with no
fence tracking and no exemptions: a line 2..N that starts at column 0 with 1-6 '#', a
space or tab, and 'Googlebook Anywhere v<digit>' (any version, the current one too) is
refused. To keep such a line in an example, indent it by 4 spaces or put it in a list or
quote, which the lint does not look at. Known lint gap (no self-test asserts their
refusal): indented, quoted, listed, setext, emphasized, linked, code-span and table forms
of an older heading, a heading without the space or the version, 7 or more '#', and a
lowercase name or other spacing of the name.
"""
import contextlib
import difflib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

try:
    import yaml as _pyyaml
except ImportError:
    _pyyaml = None

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".gitlab-ci.yml"
NOTES = REPO / "docs/RELEASE-NOTES.md"
WORDINGS = (
    "未發布",
    "還沒有發布",
    "還沒發布",
    "待發布",
    "即將發布",
    "沒正式發布",
    "沒有正式發布",
    "未正式發布",
    "未發佈",
    "還沒有發佈",
    "還沒發佈",
    "未发布",
    "unreleased",
    "unpublished",
    "not yet released",
    "tolower",
)
RELEASED_LINE = "<!-- release-status: released -->"
UNRELEASED_LINE = "<!-- release-status: unreleased -->"
TRUTHY = {"true", "yes", "on"}
PLAIN_SCALAR_EXEMPT_STARTS = ("\"", "'", "|", ">", "[", "{", "*", "&", "!")
PIN_MISMATCH_MSG = (
    "release-path CI blocks changed: update the pinned copy in "
    "release-notes-gate.py only after review"
)
NEW_PACKAGE_GATE_LINES = r"""    # line 3 is the released status. Markdown only (no raw HTML outside code spans, char refs, escaped [ ], or non-plain
    # 1-line link targets); each line and segment (paragraph/list item/table row/heading, also in >) is scanned (2 Latin + 1 CJK views).
    - if test ! -f docs/RELEASE-NOTES.md; then echo "docs/RELEASE-NOTES.md line 1 expected '# Googlebook Anywhere $CI_COMMIT_TAG'"; echo "docs/RELEASE-NOTES.md line 2 expected empty"; echo "docs/RELEASE-NOTES.md line 3 expected '<!-- release-status:" " released -->'"; exit 1; fi; LC_ALL=C awk -v tag="$CI_COMMIT_TAG" 'function has_cjk(line) { if (index(line, "未發布") || index(line, "還沒有發布") || index(line, "還沒發布")) return 1; if (index(line, "待發布") || index(line, "即將發布") || index(line, "沒正式發布")) return 1; if (index(line, "沒有正式發布") || index(line, "未正式發布") || index(line, "未發佈")) return 1; if (index(line, "還沒有發佈") || index(line, "還沒發佈") || index(line, "未发布")) return 1; return 0; } function has_latin(low) { return index(low, "unreleased") || index(low, "unpublished") || index(low, "not yet released"); } function seg_check(    t, a, b, c) { if (seg == "") return; t = seg; gsub(/\]\([^)]*\)/, "", t); gsub(/\]\[[^]\\\n]*\]/, "]", t); gsub(/!\[/, "[", t); gsub(/[{[][+]|[+][]}]|[{]-|-[}]|\$`|`\$/, "", t); a = t; gsub(/[][*_~`\\>-]/, "", a); gsub(/[ \t\r\n]+/, " ", a); b = t; gsub(/[][*_~`\\>\/-]/, " ", b); gsub(/[ \t\r\n]+/, " ", b); c = t; gsub(/[][*_~`\\> \t\r\n-]/, "", c); if (has_latin(tolower(a)) || has_latin(tolower(b)) || has_cjk(c)) { print "docs/RELEASE-NOTES.md lines " start "-" last " expected no unpublished wording (after removing link targets, markup and line breaks)"; bad = 1; } seg = ""; } BEGIN { expect1 = "# Googlebook Anywhere " tag; released = "<!-- release-status:" " released -->"; bad = 0; seg = ""; } { if (NR == 1 && $0 != expect1) { print "docs/RELEASE-NOTES.md line 1 expected " expect1; bad = 1; } if (NR == 2 && $0 != "") { print "docs/RELEASE-NOTES.md line 2 expected empty"; bad = 1; } if (NR == 3 && $0 != released) { print "docs/RELEASE-NOTES.md line 3 expected " released; bad = 1; } if ($0 ~ /&#[0-9xX]|&[A-Za-z][A-Za-z0-9]*;/) { print "docs/RELEASE-NOTES.md line " NR " expected no character reference"; bad = 1; } h = $0; gsub(/`[^`]*`/, "", h); if (NR != 3 && h ~ /<[A-Za-z\/!?]/) { print "docs/RELEASE-NOTES.md line " NR " expected no raw HTML (Markdown only; line 3 is the status comment)"; bad = 1; } if ($0 ~ /\]\([^)]*[ \t"\047(<`]/ || $0 ~ /\]\([^)]*$/) { print "docs/RELEASE-NOTES.md line " NR " expected a link target without spaces, quotes, parentheses or a title, on one line"; bad = 1; } if ($0 ~ /\\[][()]/) { print "docs/RELEASE-NOTES.md line " NR " expected no escaped bracket or parenthesis"; bad = 1; } if (NR != 3 && $0 ~ /<!--/) { print "docs/RELEASE-NOTES.md line " NR " expected no HTML comment (line 3 is the status comment)"; bad = 1; } if (h ~ /\]\[/ || h ~ /^[ \t>]*\[[^]]*\]:/) { print "docs/RELEASE-NOTES.md line " NR " expected no reference-style link or definition (use [text](target))"; bad = 1; } if (has_latin(tolower($0)) || has_cjk($0)) { print "docs/RELEASE-NOTES.md line " NR " expected no unpublished wording"; bad = 1; } if ($0 ~ /^[ \t\r>]*$/ || $0 ~ /^[ \t>]*(=+|-+|\*+|_+)[ \t\r]*$/) { seg_check(); } else { if ($0 ~ /^[ \t>]*([-*+]|[0-9]+[.)])[ \t]/ || $0 ~ /^[ \t>]*\|/ || $0 ~ /^[ \t>]*#/) seg_check(); if (seg == "") start = NR; seg = seg "\n" $0; last = NR; } } END { seg_check(); if (NR < 1) { print "docs/RELEASE-NOTES.md line 1 expected " expect1; bad = 1; } if (NR < 2) { print "docs/RELEASE-NOTES.md line 2 expected empty"; bad = 1; } if (NR < 3) { print "docs/RELEASE-NOTES.md line 3 expected " released; bad = 1; } exit bad; }' docs/RELEASE-NOTES.md"""
CI_0394514_PACKAGE_GATE_LINES = r"""    # line 3 is the released status. Every line of the whole file is scanned for
    # unpublished wording (no fence exemption, no version-section boundary).
    - if test ! -f docs/RELEASE-NOTES.md; then echo "docs/RELEASE-NOTES.md line 1 expected '# Googlebook Anywhere $CI_COMMIT_TAG'"; echo "docs/RELEASE-NOTES.md line 2 expected empty"; echo "docs/RELEASE-NOTES.md line 3 expected '<!-- release-status:" " released -->'"; exit 1; fi; LC_ALL=C awk -v tag="$CI_COMMIT_TAG" 'function has_cjk(line) { if (index(line, "未發布") || index(line, "還沒有發布") || index(line, "還沒發布")) return 1; if (index(line, "待發布") || index(line, "即將發布") || index(line, "沒正式發布")) return 1; if (index(line, "沒有正式發布") || index(line, "未正式發布") || index(line, "未發佈")) return 1; if (index(line, "還沒有發佈") || index(line, "還沒發佈") || index(line, "未发布")) return 1; return 0; } function has_wording(line,    low) { low = tolower(line); if (index(low, "unreleased") || index(low, "unpublished") || index(low, "not yet released")) return 1; return has_cjk(line); } BEGIN { expect1 = "# Googlebook Anywhere " tag; released = "<!-- release-status:" " released -->"; bad = 0; sp = ""; cjk = ""; } { if (NR == 1 && $0 != expect1) { print "docs/RELEASE-NOTES.md line 1 expected " expect1; bad = 1; } if (NR == 2 && $0 != "") { print "docs/RELEASE-NOTES.md line 2 expected empty"; bad = 1; } if (NR == 3 && $0 != released) { print "docs/RELEASE-NOTES.md line 3 expected " released; bad = 1; } if ($0 ~ /&#([0-9]+|[xX][0-9A-Fa-f]+);|&[A-Za-z][A-Za-z0-9]+;/) { print "docs/RELEASE-NOTES.md line " NR " expected no character reference"; bad = 1; } if (has_wording($0)) { print "docs/RELEASE-NOTES.md line " NR " expected no unpublished wording"; bad = 1; } norm = $0; gsub(/<[^>]*>/, "", norm); gsub(/[*_`~\\]/, "", norm); sp = (NR == 1 ? norm : sp " " norm); cjk = cjk norm; } END { if (NR < 1) { print "docs/RELEASE-NOTES.md line 1 expected " expect1; bad = 1; } if (NR < 2) { print "docs/RELEASE-NOTES.md line 2 expected empty"; bad = 1; } if (NR < 3) { print "docs/RELEASE-NOTES.md line 3 expected " released; bad = 1; } gsub(/[ \t]+/, " ", sp); if (has_wording(sp) || has_cjk(cjk)) { print "docs/RELEASE-NOTES.md expected no unpublished wording (across lines or inside inline markup)"; bad = 1; } exit bad; }' docs/RELEASE-NOTES.md"""
PINNED_CI_BLOCKS = {
    ".deps": """.deps: &deps
  before_script:
    - apt-get update -qq
    - apt-get install -y -qq --no-install-recommends bash busybox coreutils util-linux procps python3 python3-yaml mtools zstd lz4 shellcheck git file gdisk > /dev/null
    # the runner reuses the checkout between jobs and the test jobs hand it to the tester user; root's git would refuse it
    - git config --global --add safe.directory "$CI_PROJECT_DIR"
""",
    "package": r"""package:
  stage: package
  <<: *deps
  rules:
    - if: $CI_COMMIT_TAG
  script:
    # Fixed position: line 1 is `# Googlebook Anywhere $CI_COMMIT_TAG`, line 2 is empty,
    # line 3 is the released status. Markdown only (no raw HTML outside code spans, char refs, escaped [ ], or non-plain
    # 1-line link targets); each line and segment (paragraph/list item/table row/heading, also in >) is scanned (2 Latin + 1 CJK views).
    - if test ! -f docs/RELEASE-NOTES.md; then echo "docs/RELEASE-NOTES.md line 1 expected '# Googlebook Anywhere $CI_COMMIT_TAG'"; echo "docs/RELEASE-NOTES.md line 2 expected empty"; echo "docs/RELEASE-NOTES.md line 3 expected '<!-- release-status:" " released -->'"; exit 1; fi; LC_ALL=C awk -v tag="$CI_COMMIT_TAG" 'function has_cjk(line) { if (index(line, "未發布") || index(line, "還沒有發布") || index(line, "還沒發布")) return 1; if (index(line, "待發布") || index(line, "即將發布") || index(line, "沒正式發布")) return 1; if (index(line, "沒有正式發布") || index(line, "未正式發布") || index(line, "未發佈")) return 1; if (index(line, "還沒有發佈") || index(line, "還沒發佈") || index(line, "未发布")) return 1; return 0; } function has_latin(low) { return index(low, "unreleased") || index(low, "unpublished") || index(low, "not yet released"); } function seg_check(    t, a, b, c) { if (seg == "") return; t = seg; gsub(/\]\([^)]*\)/, "", t); gsub(/\]\[[^]\\\n]*\]/, "]", t); gsub(/!\[/, "[", t); gsub(/[{[][+]|[+][]}]|[{]-|-[}]|\$`|`\$/, "", t); a = t; gsub(/[][*_~`\\>-]/, "", a); gsub(/[ \t\r\n]+/, " ", a); b = t; gsub(/[][*_~`\\>\/-]/, " ", b); gsub(/[ \t\r\n]+/, " ", b); c = t; gsub(/[][*_~`\\> \t\r\n-]/, "", c); if (has_latin(tolower(a)) || has_latin(tolower(b)) || has_cjk(c)) { print "docs/RELEASE-NOTES.md lines " start "-" last " expected no unpublished wording (after removing link targets, markup and line breaks)"; bad = 1; } seg = ""; } BEGIN { expect1 = "# Googlebook Anywhere " tag; released = "<!-- release-status:" " released -->"; bad = 0; seg = ""; } { if (NR == 1 && $0 != expect1) { print "docs/RELEASE-NOTES.md line 1 expected " expect1; bad = 1; } if (NR == 2 && $0 != "") { print "docs/RELEASE-NOTES.md line 2 expected empty"; bad = 1; } if (NR == 3 && $0 != released) { print "docs/RELEASE-NOTES.md line 3 expected " released; bad = 1; } if ($0 ~ /&#[0-9xX]|&[A-Za-z][A-Za-z0-9]*;/) { print "docs/RELEASE-NOTES.md line " NR " expected no character reference"; bad = 1; } h = $0; gsub(/`[^`]*`/, "", h); if (NR != 3 && h ~ /<[A-Za-z\/!?]/) { print "docs/RELEASE-NOTES.md line " NR " expected no raw HTML (Markdown only; line 3 is the status comment)"; bad = 1; } if ($0 ~ /\]\([^)]*[ \t"\047(<`]/ || $0 ~ /\]\([^)]*$/) { print "docs/RELEASE-NOTES.md line " NR " expected a link target without spaces, quotes, parentheses or a title, on one line"; bad = 1; } if ($0 ~ /\\[][()]/) { print "docs/RELEASE-NOTES.md line " NR " expected no escaped bracket or parenthesis"; bad = 1; } if (NR != 3 && $0 ~ /<!--/) { print "docs/RELEASE-NOTES.md line " NR " expected no HTML comment (line 3 is the status comment)"; bad = 1; } if (h ~ /\]\[/ || h ~ /^[ \t>]*\[[^]]*\]:/) { print "docs/RELEASE-NOTES.md line " NR " expected no reference-style link or definition (use [text](target))"; bad = 1; } if (has_latin(tolower($0)) || has_cjk($0)) { print "docs/RELEASE-NOTES.md line " NR " expected no unpublished wording"; bad = 1; } if ($0 ~ /^[ \t\r>]*$/ || $0 ~ /^[ \t>]*(=+|-+|\*+|_+)[ \t\r]*$/) { seg_check(); } else { if ($0 ~ /^[ \t>]*([-*+]|[0-9]+[.)])[ \t]/ || $0 ~ /^[ \t>]*\|/ || $0 ~ /^[ \t>]*#/) seg_check(); if (seg == "") start = NR; seg = seg "\n" $0; last = NR; } } END { seg_check(); if (NR < 1) { print "docs/RELEASE-NOTES.md line 1 expected " expect1; bad = 1; } if (NR < 2) { print "docs/RELEASE-NOTES.md line 2 expected empty"; bad = 1; } if (NR < 3) { print "docs/RELEASE-NOTES.md line 3 expected " released; bad = 1; } exit bad; }' docs/RELEASE-NOTES.md
    - apt-get install -y -qq --no-install-recommends curl ca-certificates > /dev/null
    - bash tools/release/make-release.sh "$CI_COMMIT_TAG" "$CI_COMMIT_SHA"
    - |
      for f in out/release/*; do
        curl --fail --silent --show-error --header "JOB-TOKEN: $CI_JOB_TOKEN" --upload-file "$f" \
          "$CI_API_V4_URL/projects/$CI_PROJECT_ID/packages/generic/installer/$CI_COMMIT_TAG/$(basename "$f")"
      done
  artifacts:
    paths: [out/release/]
    expire_in: 30 days
""",
    "release": r"""release:
  stage: release
  image: registry.gitlab.com/gitlab-org/release-cli:latest
  rules:
    - if: $CI_COMMIT_TAG
  needs: [package]
  script:
    - test "$(head -n 1 docs/RELEASE-NOTES.md)" = "# Googlebook Anywhere $CI_COMMIT_TAG" || { echo "docs/RELEASE-NOTES.md line 1 expected '# Googlebook Anywhere $CI_COMMIT_TAG'"; exit 1; }
    # docs/RELEASE-NOTES.md holds only this version (older ones are in docs/RELEASE-HISTORY.md): publish it verbatim.
    - cat docs/RELEASE-NOTES.md > release-description.md
    - printf '\n[Full release history](%s/-/blob/%s/docs/RELEASE-HISTORY.md)\n' "$CI_PROJECT_URL" "$CI_COMMIT_TAG" >> release-description.md
    - echo "release $CI_COMMIT_TAG"
  release:
    tag_name: $CI_COMMIT_TAG
    name: $CI_COMMIT_TAG
    description: ./release-description.md
    assets:
      links:
        - name: googlebook-anywhere-$CI_COMMIT_TAG.tar.gz (installer scripts; no disk/firmware images)
          url: $CI_API_V4_URL/projects/$CI_PROJECT_ID/packages/generic/installer/$CI_COMMIT_TAG/googlebook-anywhere-$CI_COMMIT_TAG.tar.gz
          link_type: package
        - name: SHA-256
          url: $CI_API_V4_URL/projects/$CI_PROJECT_ID/packages/generic/installer/$CI_COMMIT_TAG/googlebook-anywhere-$CI_COMMIT_TAG.tar.gz.sha256
          link_type: other
""",
}
OLDER = (
    "# Googlebook Anywhere v9.9.8\n"
    "\n"
    "上一版已經發布。\n"
)
OLDER_WITH_WORDING = (
    "# Googlebook Anywhere v9.9.8\n"
    "\n"
    "舊版 (未發布) 與 UNRELEASED 不在這一版。\n"
)


class Unresolved(Exception):
    pass


def fail(message):
    print(f"release-notes-gate: {message}", file=sys.stderr)
    raise SystemExit(1)


def strip_inline_comment(text):
    if " #" in text:
        return text.split(" #", 1)[0].rstrip()
    return text


def unquote_key(name):
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in ("\"", "'"):
        return name[1:-1]
    return name


def check_unresolved_scalar(raw, context):
    val = strip_inline_comment(raw).strip()
    if val.startswith("- "):
        val = val[2:].strip()
    if val and val[0] in ("*", "!", "&"):
        raise Unresolved(f"unresolved scalar in {context}: {val}")
    if ":" in val:
        _, _, rhs = val.partition(":")
        rhs = rhs.strip()
        if rhs and rhs[0] in ("*", "!", "&"):
            raise Unresolved(f"unresolved scalar in {context}: {val}")
    return val


def copy_entry(entry):
    return {"scalar": entry.get("scalar", ""), "items": list(entry.get("items", []))}


def parse_top(content):
    if ":" not in content:
        return None
    name, _, rest = content.partition(":")
    name = unquote_key(name)
    rest = rest.strip()
    if not name:
        return None
    anchor = None
    if rest.startswith("&"):
        token, _, tail = rest.partition(" ")
        anchor = token[1:]
        rest = tail.strip()
    return name, anchor, rest


def extract_raw_blocks(text):
    """Return raw text of each top-level block, keyed by unquoted block name."""
    raw_blocks = {}
    current = None
    lines = []
    pending_blanks = []
    for raw in text.splitlines():
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if not stripped:
            if current is not None:
                pending_blanks.append("")
            continue
        if indent == 0:
            if stripped.startswith("#"):
                continue
            parsed = parse_top(stripped)
            if current is not None:
                raw_blocks[current] = "\n".join(lines)
                current = None
                lines = []
                pending_blanks = []
            if parsed is not None:
                name, _anchor, _scalar = parsed
                if name in raw_blocks:
                    raise Unresolved(f"duplicate top-level block {name}")
                current = name
                lines = [raw.rstrip()]
            continue
        if current is not None:
            if pending_blanks:
                lines.extend(pending_blanks)
                pending_blanks = []
            lines.append(raw.rstrip())
    if current is not None:
        raw_blocks[current] = "\n".join(lines)
    return raw_blocks


def parse_document(text):
    """Track top-level blocks, anchors, and the key in scope. No PyYAML.

    `include:` anywhere is recorded and left unresolved. Complex keys (`? `)
    anywhere raise Unresolved. List items stay under the current key, so
    package.script is not package.before_script.
    """
    blocks = {}
    anchors = {}
    saw_include = False
    block = None
    key = None
    key_indent = None
    in_script_key = False
    for raw in text.splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()
        if content.startswith("#"):
            continue
        cleaned = strip_inline_comment(content).strip()
        if cleaned == "?" or cleaned.startswith("? ") or cleaned.startswith("?\t"):
            raise Unresolved(f"complex key (? ) is not supported: {cleaned}")
        if not in_script_key and re.search(r"(?:^|[^A-Za-z0-9_\"'./-])[\"']?include[\"']?\s*:", cleaned):
            saw_include = True
        if indent == 0:
            in_script_key = False
            parsed = parse_top(content)
            if parsed is None:
                block = None
                key = None
                continue
            name, anchor, scalar = parsed
            if name == "include":
                saw_include = True
            if name in blocks:
                raise Unresolved(f"duplicate top-level block {name}")
            if scalar and strip_inline_comment(scalar).strip().startswith("{"):
                raise Unresolved(f"top-level flow mapping is not supported: {name}")
            block = name
            blocks[name] = {}
            if anchor:
                anchors[anchor] = name
            key = None
            key_indent = None
            if scalar:
                blocks[name]["__scalar__"] = {"scalar": scalar, "items": []}
            continue
        if block is None:
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise Unresolved(f"CI jobs use 2-space keys (in {block})")
        if cleaned.startswith("{") or cleaned.startswith("["):
            if key is None or indent <= 2:
                raise Unresolved(f"flow collection at block level in {block} is not supported: {cleaned}")
        if block != "include":
            if "__scalar__" in blocks[block]:
                raise Unresolved(f"CI jobs use 2-space keys (in {block})")
            if key is None:
                if indent != 2 or content.startswith("-") or ":" not in content:
                    raise Unresolved(f"CI jobs use 2-space keys (in {block})")
            elif indent < 2 or (indent == 2 and (content.startswith("-") or ":" not in content)):
                raise Unresolved(f"CI jobs use 2-space keys (in {block})")
            elif indent > 2 and not in_script_key and not content.startswith("-") and ":" in content:
                subkey = unquote_key(content.partition(":")[0])
                if subkey == "extends" or (blocks[block][key]["scalar"] and not blocks[block][key]["items"]):
                    raise Unresolved(f"CI jobs use 2-space keys (in {block})")
        if indent == 2 and not content.startswith("-") and ":" in content:
            name, _, rest = content.partition(":")
            key = unquote_key(name)
            if any(c in key for c in "{}[],"):
                raise Unresolved(f"invalid key {key} in {block}")
            if key == "include":
                saw_include = True
            if key in blocks[block]:
                raise Unresolved(f"duplicate key {key} in {block}")
            in_script_key = key in ("script", "before_script", "after_script")
            key_indent = indent
            blocks[block][key] = {"scalar": rest.strip(), "items": []}
            continue
        if key is not None and indent > key_indent:
            if not in_script_key and re.search(r"(?:^|[^A-Za-z0-9_\"'./-])[\"']?include[\"']?\s*:", cleaned):
                saw_include = True
            blocks[block][key]["items"].append(content)
    return blocks, anchors, saw_include


def anchor_names(entry):
    refs = []
    chunks = [entry.get("scalar", "")]
    chunks.extend(entry.get("items", []))
    for chunk in chunks:
        if not chunk:
            continue
        text = strip_inline_comment(chunk).strip()
        if text.startswith("- "):
            text = text[2:].strip()
        text = text.strip("\"'")
        if text.startswith("*"):
            refs.append(text[1:].strip("\"'"))
            continue
        if text.startswith("[") and text.endswith("]"):
            for part in text[1:-1].split(","):
                part = part.strip().strip("\"'")
                if part.startswith("*"):
                    refs.append(part[1:])
    return refs


def _append_extends_piece(raw, names):
    check_unresolved_scalar(raw, "extends")
    text = strip_inline_comment(raw).strip()
    if not text:
        return
    if text.startswith("- "):
        text = text[2:].strip()
    if text.startswith("{"):
        raise Unresolved(f"flow mapping in extends is not supported: {text}")
    if text.startswith("["):
        if not text.endswith("]") or "[" in text[1:-1] or "{" in text[1:-1]:
            raise Unresolved(f"unsupported flow sequence in extends: {text}")
        inner = text[1:-1].strip()
        if not inner:
            return
        for part in inner.split(","):
            part = part.strip()
            if not part:
                continue
            check_unresolved_scalar(part, "extends")
            part = unquote_key(part)
            if not part or any(c in part for c in "[]{},:"):
                raise Unresolved(f"unsupported extends item: {part}")
            names.append(part)
        return
    text = unquote_key(text)
    if any(c in text for c in "[]{},"):
        raise Unresolved(f"unsupported extends value: {text}")
    if text:
        names.append(text)


def extends_names(entry):
    if not entry:
        return []
    names = []
    _append_extends_piece(entry.get("scalar", ""), names)
    for item in entry.get("items", []):
        _append_extends_piece(item, names)
    return names


def merge_missing(dst, src):
    for key, value in src.items():
        if key not in dst:
            dst[key] = copy_entry(value)


def deep_merge(base, override):
    result = {key: copy_entry(value) for key, value in base.items()}
    for key, value in override.items():
        result[key] = copy_entry(value)
    return result


def merged_anchor_blocks(name, blocks, anchors, seen):
    """Return top-level block names merged via `<<: *anchor` on `name`."""
    if name in seen:
        raise Unresolved(f"anchor cycle at {name}")
    block = blocks[name]
    out = []
    merge = block.get("<<")
    if merge:
        for anchor in anchor_names(merge):
            if anchor not in anchors:
                raise Unresolved(f"unresolved anchor *{anchor}")
            target = anchors[anchor]
            if target not in out:
                out.append(target)
            for sub in merged_anchor_blocks(target, blocks, anchors, seen | {name}):
                if sub not in out:
                    out.append(sub)
    return out


def resolve_merges(name, blocks, anchors, seen):
    """Apply `<<: *anchor` from this file. Keys already on the block win."""
    if name in seen:
        raise Unresolved(f"anchor cycle at {name}")
    block = blocks[name]
    result = {}
    merge = block.get("<<")
    if merge:
        for anchor in anchor_names(merge):
            if anchor not in anchors:
                raise Unresolved(f"unresolved anchor *{anchor}")
            merged = resolve_merges(anchors[anchor], blocks, anchors, seen | {name})
            merge_missing(result, merged)
    for key, value in block.items():
        if key in ("<<", "__scalar__"):
            continue
        result[key] = copy_entry(value)
    return result


def effective_job(name, blocks, anchors, defaults, stack, use_defaults=True):
    """Job after YAML merge, extends, then default. Local keys win."""
    if name in stack:
        raise Unresolved(f"extends cycle at {name}")
    if name not in blocks:
        raise Unresolved(f"extends: {name} is not in this file")
    local = resolve_merges(name, blocks, anchors, frozenset())
    merged = {}
    for parent in extends_names(local.get("extends")):
        parent_map = effective_job(
            parent, blocks, anchors, defaults, stack | {name}, use_defaults=False
        )
        merged = deep_merge(merged, parent_map)
    merged = deep_merge(merged, local)
    merged.pop("extends", None)
    merged.pop("<<", None)
    if use_defaults:
        for key, value in defaults.items():
            if key in ("__scalar__", "<<", "extends"):
                continue
            if key not in merged:
                merged[key] = copy_entry(value)
    return merged


def list_commands(entry):
    if not entry:
        return []
    return [item[2:].strip() for item in entry.get("items", []) if item.startswith("- ")]


def keyed_values(entry, key):
    """Values of `key:` on this entry, including nested rule lines and flow text."""
    if not entry:
        return []
    values = []
    chunks = []
    scalar = entry.get("scalar", "")
    if scalar:
        chunks.append(scalar)
    chunks.extend(entry.get("items", []))
    pattern = re.compile(
        r"(?:^|[^A-Za-z0-9_\"'])(?:"
        + re.escape(key)
        + '|"'
        + re.escape(key)
        + r"\"|'"
        + re.escape(key)
        + r"')\s*:(.*)",
        re.IGNORECASE,
    )
    for chunk in chunks:
        text = strip_inline_comment(chunk).strip()
        if text.startswith("- "):
            text = text[2:].strip()
        pos = 0
        while pos < len(text):
            m = pattern.search(text, pos)
            if not m:
                break
            value = m.group(1)
            for stop in (",", "}", "]"):
                cut = value.find(stop)
                if cut >= 0:
                    value = value[:cut]
            values.append(value.strip())
            pos = m.start(1)
    return values


def is_gate(cmd):
    return "LC_ALL=C awk " in cmd and "release-status" in cmd


def _check_plain_scalar_tokens(val, lineno):
    if ": " in val or ":\t" in val:
        return f"line {lineno}: plain scalar contains ': '"
    if " #" in val or "\t#" in val:
        return f"line {lineno}: plain scalar contains ' #'"
    if val.endswith(":"):
        return f"line {lineno}: plain scalar ends with ':'"
    return None


def _check_plain_scalar_value(val, lineno):
    if not val:
        return None
    if val[0] in PLAIN_SCALAR_EXEMPT_STARTS:
        return None
    return _check_plain_scalar_tokens(val, lineno)


def plain_scalar_problem(text):
    """Reject YAML 1.1/1.2 plain-scalar ambiguity patterns (': ', ' #', trailing ':'), including continuation lines."""
    block = None
    in_script_key = False
    block_scalar_indent = None
    plain_scalar_indent = None
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.rstrip("\r\n")
        if block_scalar_indent is not None:
            stripped_bs = line.strip()
            if not stripped_bs:
                continue
            indent_bs = len(line) - len(line.lstrip(" "))
            if indent_bs > block_scalar_indent:
                continue
            block_scalar_indent = None
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line[indent:]
        work = content
        if block not in (".deps", "package", "release"):
            work = re.sub(r"  +#([^\"']*)$", "", work).rstrip()
        if plain_scalar_indent is not None:
            if indent > plain_scalar_indent:
                prob = _check_plain_scalar_tokens(work, lineno)
                if prob:
                    return prob
                continue
            plain_scalar_indent = None
        if indent == 0:
            in_script_key = False
            parsed = parse_top(content)
            block = parsed[0] if parsed else None
            continue
        if indent == 2 and not content.startswith("-") and ":" in content:
            kname, _, _ = content.partition(":")
            in_script_key = unquote_key(kname) in ("script", "before_script", "after_script")
        if work.startswith("- "):
            after_dash = work[2:].strip()
            if after_dash in ("|", ">", "|-", ">-", "|+", ">+"):
                block_scalar_indent = indent
                continue
            if not in_script_key:
                m_kv = re.match(r'^(<<|[A-Za-z0-9_.-]+|"[^"]+"|\'[^\']+\')\s*:\s+(.+)$', after_dash)
                if m_kv:
                    v_str = m_kv.group(2).strip()
                    prob = _check_plain_scalar_value(v_str, lineno)
                    if prob:
                        return prob
                    if v_str and v_str[0] not in PLAIN_SCALAR_EXEMPT_STARTS:
                        plain_scalar_indent = indent + 2
                    continue
            prob = _check_plain_scalar_value(after_dash, lineno)
            if prob:
                return prob
            if after_dash and after_dash[0] not in PLAIN_SCALAR_EXEMPT_STARTS:
                plain_scalar_indent = indent
            continue
        if not work.startswith("-") and ":" in work and (indent == 2 or not in_script_key):
            m_kv = re.match(r'^(<<|[A-Za-z0-9_.-]+|"[^"]+"|\'[^\']+\')\s*:(.*)$', work)
            if m_kv:
                rest = m_kv.group(2).strip()
                if rest.startswith("&"):
                    parts = rest.split(None, 1)
                    rest = parts[1].strip() if len(parts) > 1 else ""
                if rest in ("|", ">", "|-", ">-", "|+", ">+"):
                    block_scalar_indent = indent
                    continue
                prob = _check_plain_scalar_value(rest, lineno)
                if prob:
                    return prob
                if rest and rest[0] not in PLAIN_SCALAR_EXEMPT_STARTS:
                    plain_scalar_indent = indent
                continue
        prob = _check_plain_scalar_tokens(work, lineno)
        if prob:
            return prob
    return None


def pyyaml_problem(text, expected_gate=None):
    """If PyYAML is available, safe_load the full YAML and verify package.script[0]."""
    if _pyyaml is None:
        return None
    try:
        loaded = _pyyaml.safe_load(text)
    except Exception as exc:
        return f"PyYAML safe_load failed: {exc}"
    if not isinstance(loaded, dict):
        return "PyYAML safe_load did not return a top-level mapping"
    pkg = loaded.get("package")
    if not isinstance(pkg, dict):
        return "PyYAML safe_load: package is not a mapping"
    script = pkg.get("script")
    if not isinstance(script, list) or not script or not isinstance(script[0], str):
        return "PyYAML safe_load: package.script[0] is not a string"
    if expected_gate is not None and script[0] != expected_gate:
        return "PyYAML safe_load: package.script[0] does not equal tracker gate line"
    return None


def gate_problem(command):
    if "head -n 3" in command or "/^## /{exit}" in command or "LC_ALL=C awk " not in command:
        return "unpublished gate is not the fixed-position status scan"
    prefix = command.split("LC_ALL=C awk ", 1)[0]
    if "|" in prefix:
        return "unpublished gate starts with a pipeline"
    if "IGNORECASE" in command:
        return "gate uses gawk IGNORECASE"
    if not command.strip().endswith("' docs/RELEASE-NOTES.md"):
        return "gate command does not end with ' docs/RELEASE-NOTES.md"
    for wording in WORDINGS:
        if wording not in command:
            return f"gate command is missing {wording}"
    for piece in (
        "NR == 1",
        "NR == 2",
        "NR == 3",
        '-v tag="$CI_COMMIT_TAG"',
        "line 1 expected",
        "line 2 expected empty",
        "line 3 expected",
        "expected no character reference",
        "expected no raw HTML (Markdown only; line 3 is the status comment)",
        "expected a link target without spaces, quotes, parentheses or a title, on one line",
        "expected no escaped bracket or parenthesis",
        "expected no reference-style link or definition",
        "expected no HTML comment (line 3 is the status comment)",
        'gsub(/!\\[/, "[", t); gsub(/[{[][+]|[+][]}]|[{]-|-[}]|\\$`|`\\$/, "", t);',
        "h ~ /\\]\\[/ || h ~ /^[ \\t>]*\\[[^]]*\\]:/",
        "expected no unpublished wording",
        'lines " start "-" last " expected no unpublished wording (after removing link targets, markup and line breaks)',
        "&#[0-9xX]|&[A-Za-z][A-Za-z0-9]*;",
        r'h = $0; gsub(/`[^`]*`/, "", h); if (NR != 3 && h ~ /<[A-Za-z\/!?]/)',
        r'$0 ~ /\]\([^)]*[ \t"\047(<`]/ || $0 ~ /\]\([^)]*$/',
        r'$0 ~ /\\[][()]/',
        "has_latin(tolower($0)) || has_cjk($0)",
        "seg_check()",
        'if (seg == "") return;',
        r'gsub(/\]\([^)]*\)/, "", t);',
        r'gsub(/\]\[[^]\\\n]*\]/, "]", t);',
        r'a = t; gsub(/[][*_~`\\>-]/, "", a); gsub(/[ \t\r\n]+/, " ", a);',
        r'b = t; gsub(/[][*_~`\\>\/-]/, " ", b); gsub(/[ \t\r\n]+/, " ", b);',
        r'c = t; gsub(/[][*_~`\\> \t\r\n-]/, "", c);',
        "has_latin(tolower(a)) || has_latin(tolower(b)) || has_cjk(c)",
        r'if ($0 ~ /^[ \t\r>]*$/ || $0 ~ /^[ \t>]*(=+|-+|\*+|_+)[ \t\r]*$/) { seg_check(); }',
        r'if ($0 ~ /^[ \t>]*([-*+]|[0-9]+[.)])[ \t]/ || $0 ~ /^[ \t>]*\|/ || $0 ~ /^[ \t>]*#/) seg_check();',
    ):
        if piece not in command:
            return f"gate command is missing {piece}"
    return None


def other_blocks_release_problem(blocks, anchors, raw_blocks):
    for name, bmap in blocks.items():
        local = resolve_merges(name, blocks, anchors, frozenset())
        for parent in extends_names(local.get("extends")):
            if parent in ("release", "package"):
                return f"{name} block extends {parent}"
        if name == "release":
            continue
        if "release" in bmap or "release" in local:
            return f"{name} block has a release: key"
        raw = raw_blocks.get(name, "")
        for idx, line in enumerate(raw.splitlines()):
            cleaned = strip_inline_comment(line).strip()
            if not cleaned or cleaned.startswith("#"):
                continue
            scan = cleaned.partition(":")[2] if idx == 0 else cleaned
            if re.search(r"(?:^|[^A-Za-z0-9_\"'./-])[\"']?release[\"']?\s*:", scan):
                return f"{name} block has a release: key"
            if "release-cli" in scan or "glab release" in scan or "/releases" in scan:
                return f"{name} block invokes release-cli, glab release, or /releases"
    return None


def normalize_block_text(block_text):
    return "\n".join(line.rstrip() for line in block_text.strip("\n").splitlines())


def pin_problem(text, emit_diff=True):
    """Compare package:, release:, and every anchor they merge against PINNED_CI_BLOCKS."""
    try:
        blocks, anchors, _saw_include = parse_document(text)
        raw_blocks = extract_raw_blocks(text)
    except Unresolved as err:
        return str(err)
    if "package" not in blocks or "release" not in blocks:
        return "missing package or release job"
    try:
        merged_anchors = []
        for job_name in ("package", "release"):
            for target in merged_anchor_blocks(job_name, blocks, anchors, frozenset()):
                if target not in merged_anchors:
                    merged_anchors.append(target)
    except Unresolved as err:
        return str(err)
    order = []
    for name in (*merged_anchors, ".deps", "package", "release"):
        if name not in order:
            order.append(name)
    diffs = []
    if merged_anchors != [".deps"]:
        diffs.append(
            "\n".join(
                difflib.unified_diff(
                    [".deps"],
                    merged_anchors,
                    fromfile="pinned/merged-anchors",
                    tofile=".gitlab-ci.yml/merged-anchors",
                    lineterm="",
                )
            )
        )
    for name in order:
        expected = normalize_block_text(PINNED_CI_BLOCKS.get(name, ""))
        actual = normalize_block_text(raw_blocks.get(name, ""))
        if expected != actual:
            diffs.append(
                "\n".join(
                    difflib.unified_diff(
                        expected.splitlines(),
                        actual.splitlines(),
                        fromfile=f"pinned/{name}",
                        tofile=f".gitlab-ci.yml/{name}",
                        lineterm="",
                    )
                )
            )
    if diffs:
        joined = "\n".join(diffs)
        msg = f"{PIN_MISMATCH_MSG}\n{joined}"
        if emit_diff:
            print(msg, file=sys.stderr)
        return msg
    return None


def tracker_problem(text):
    """Semantic tracker check before comparing pinned blocks."""
    try:
        blocks, anchors, saw_include = parse_document(text)
        raw_blocks = extract_raw_blocks(text)
    except Unresolved as err:
        return str(err)
    if saw_include:
        return "include: cannot be resolved"
    if "package" not in blocks or "release" not in blocks:
        return "missing package or release job"
    try:
        other_prob = other_blocks_release_problem(blocks, anchors, raw_blocks)
    except Unresolved as err:
        return str(err)
    if other_prob:
        return other_prob
    defaults = blocks.get("default", {})
    try:
        package = effective_job("package", blocks, anchors, defaults, frozenset())
        release = effective_job("release", blocks, anchors, defaults, frozenset())
        af = package.get("allow_failure")
        if af is not None:
            scalar = check_unresolved_scalar(af.get("scalar", ""), "package.allow_failure")
            for item in af.get("items", []):
                check_unresolved_scalar(item, "package.allow_failure")
            if af.get("items") or scalar != "false":
                return "package has allow_failure"
        pkg_when = package.get("when")
        if pkg_when is not None:
            scalar = check_unresolved_scalar(pkg_when.get("scalar", ""), "package.when")
            for item in pkg_when.get("items", []):
                check_unresolved_scalar(item, "package.when")
            if pkg_when.get("items") or scalar != "on_success":
                return "package when is not on_success"
        pkg_rules = package.get("rules")
        if pkg_rules is not None:
            check_unresolved_scalar(pkg_rules.get("scalar", ""), "package.rules")
            for item in pkg_rules.get("items", []):
                check_unresolved_scalar(item, "package.rules")
            if keyed_values(pkg_rules, "<<"):
                return "package rules has merge key"
            for value in keyed_values(pkg_rules, "allow_failure"):
                val = check_unresolved_scalar(value, "package.rules.allow_failure")
                if val != "false":
                    return "package rules allow_failure"
            if keyed_values(pkg_rules, "exit_codes"):
                return "package rules allow_failure"
        for bucket in ("before_script", "after_script"):
            if any(is_gate(cmd) for cmd in list_commands(package.get(bucket))):
                return f"gate is under package.{bucket}"
        script = list_commands(package.get("script"))
        gates = [cmd for cmd in script if is_gate(cmd)]
        if len(gates) != 1:
            return "package script does not have one unpublished gate"
        command = gates[0]
        problem = gate_problem(command)
        if problem:
            return problem
        try:
            make_at = next(i for i, cmd in enumerate(script) if "make-release.sh" in cmd)
        except StopIteration:
            return "package script does not run make-release.sh"
        if script.index(command) >= make_at:
            return "unpublished gate does not run before make-release.sh"
        when_entry = release.get("when")
        if when_entry is not None:
            scalar = check_unresolved_scalar(when_entry.get("scalar", ""), "release.when")
            for item in when_entry.get("items", []):
                check_unresolved_scalar(item, "release.when")
            if when_entry.get("items") or scalar != "on_success":
                return "release when is not on_success"
        rel_rules = release.get("rules")
        if rel_rules is not None:
            check_unresolved_scalar(rel_rules.get("scalar", ""), "release.rules")
            for item in rel_rules.get("items", []):
                check_unresolved_scalar(item, "release.rules")
            if keyed_values(rel_rules, "<<"):
                return "release rules has merge key"
            if keyed_values(rel_rules, "needs"):
                return "release rules overrides needs"
            for value in keyed_values(rel_rules, "when"):
                val = check_unresolved_scalar(value, "release.rules.when")
                if val != "on_success":
                    return "release rules when is not on_success"
        rel_spec = release.get("release")
        if rel_spec is None or keyed_values(rel_spec, "description") != ["./release-description.md"]:
            return "release description is not ./release-description.md"
        needs_entry = release.get("needs")
        if needs_entry is None:
            return "release job does not need the package job"
        needs_scalar = check_unresolved_scalar(needs_entry.get("scalar", ""), "release.needs")
        needs_items = []
        for raw_item in needs_entry.get("items", []):
            check_unresolved_scalar(raw_item, "release.needs")
            cleaned = strip_inline_comment(raw_item).strip()
            if cleaned:
                needs_items.append(cleaned)
        if keyed_values(needs_entry, "optional"):
            return "release needs package is optional"
        flow_ok = needs_scalar == "[package]" and not needs_items
        block_ok = needs_scalar == "" and needs_items == ["- package"]
        if not (flow_ok or block_ok):
            return "release job does not need the package job"
    except Unresolved as err:
        return str(err)
    ps_prob = plain_scalar_problem(text)
    if ps_prob:
        return ps_prob
    yaml_prob = pyyaml_problem(text, expected_gate=command)
    if yaml_prob:
        return yaml_prob
    return None


def ci_problem(text, emit_diff=False):
    """None when the tag gate and pinned CI blocks are in force. A string when not."""
    problem = tracker_problem(text)
    if problem:
        return problem
    return pin_problem(text, emit_diff=emit_diff)


def gate_command(text):
    problem = ci_problem(text, emit_diff=True)
    if problem:
        fail(problem)
    blocks, anchors, _saw_include = parse_document(text)
    package = effective_job("package", blocks, anchors, blocks.get("default", {}), frozenset())
    return next(cmd for cmd in list_commands(package.get("script")) if is_gate(cmd))


def run_gate(command, text, tag="v9.9.9"):
    directory = Path(tempfile.mkdtemp(prefix="gbos-notes-gate-"))
    try:
        if text is not None:
            notes = directory / "docs" / "RELEASE-NOTES.md"
            notes.parent.mkdir(parents=True)
            notes.write_bytes(text.encode("utf-8"))
        env = os.environ.copy()
        env["CI_COMMIT_TAG"] = tag
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    finally:
        shutil.rmtree(directory)


def expect(command, label, text, refuse, tag="v9.9.9", needles=()):
    rc, output = run_gate(command, text, tag)
    verdict = "refuse" if rc else "pass"
    print(f"release-notes-gate: {label}: {verdict}")
    if refuse and rc == 0:
        fail(f"{label} was accepted: {output}")
    if not refuse and rc != 0:
        fail(f"{label} was refused: {output}")
    for needle in needles:
        if needle not in output:
            fail(f"{label} message missing {needle!r}: {output}")


def with_older(body):
    if not body.endswith("\n"):
        body += "\n"
    return body + OLDER


def assert_no_unpublished(text, label):
    lowered = text.lower()
    for wording in WORDINGS:
        if wording == "tolower":
            continue
        if wording.isascii():
            if wording in lowered:
                fail(f"{label} fixture still has unpublished wording")
        elif wording in text:
            fail(f"{label} fixture still has unpublished wording")


def move_gate_to_after_script(text):
    lines = text.splitlines()
    gate_idx = next(i for i, line in enumerate(lines) if "LC_ALL=C awk " in line)
    gate_line = lines.pop(gate_idx)
    script_idx = None
    in_package = False
    for index, line in enumerate(lines):
        if line == "package:":
            in_package = True
            continue
        if in_package and line and not line[0].isspace():
            break
        if in_package and line.strip() == "script:":
            script_idx = index
            break
    if script_idx is None:
        fail("cannot place after_script mutation")
    lines.insert(script_idx, "  after_script:")
    lines.insert(script_idx + 1, gate_line)
    return "\n".join(lines) + "\n"


def delete_gate(text):
    lines = [line for line in text.splitlines() if "LC_ALL=C awk " not in line]
    return "\n".join(lines) + "\n"


def weaken_gate(text):
    old = '|| index(line, "未发布")'
    if old not in text:
        fail("gate command missing 未发布; weaken mutation would not apply")
    return text.replace(old, "", 1)


def reintroduce_fence_exemption(text):
    old = "if (has_latin(tolower($0)) || has_cjk($0))"
    new = (
        'if (index($0, "```") == 1 || index($0, "~~~") == 1) { in_fence = !in_fence; next } '
        "if (in_fence) next; if (has_latin(tolower($0)) || has_cjk($0))"
    )
    if old not in text:
        fail("cannot place fence exemption mutation")
    return text.replace(old, new, 1)


def insert_under(text, job, line):
    lines = text.splitlines()
    for index, current in enumerate(lines):
        if current == f"{job}:":
            lines.insert(index + 1, line)
            return "\n".join(lines) + "\n"
    fail(f"missing job {job}")


def make_needs_optional(text):
    old = "needs: [package]"
    new = "needs:\n    - job: package\n      optional: true"
    if old in text:
        return text.replace(old, new, 1)
    return insert_under(text, "release", "  " + new.replace("\n", "\n  "))


def make_needs_comment(text):
    old = "needs: [package]"
    if old not in text:
        fail("needs line missing; comment mutation would not apply")
    return text.replace(old, "needs: [lint] # package", 1)


def make_optional_caps(text):
    old = "needs: [package]"
    new = "needs:\n    - job: package\n      optional: True"
    if old not in text:
        fail("needs line missing; optional mutation would not apply")
    return text.replace(old, new, 1)


def make_needs_flow_map_optional(text):
    old = "needs: [package]"
    if old not in text:
        fail("needs line missing; flow map optional mutation would not apply")
    return text.replace(old, "needs: [{job: package, optional: true}]", 1)


def make_needs_block_flow_map_optional(text):
    old = "needs: [package]"
    if old not in text:
        fail("needs line missing; block flow map optional mutation would not apply")
    return text.replace(old, "needs:\n    - {job: package, optional: true}", 1)


def make_needs_mapping_item(text):
    old = "needs: [package]"
    if old not in text:
        fail("needs line missing; mapping item mutation would not apply")
    return text.replace(old, "needs:\n    - job: package", 1)


def make_needs_extra_job(text):
    old = "needs: [package]"
    if old not in text:
        fail("needs line missing; extra job mutation would not apply")
    return text.replace(old, "needs: [package, lint]", 1)


def insert_package_rules_line(text, extra_line):
    lines = text.splitlines()
    in_package = False
    for index, line in enumerate(lines):
        if line == "package:":
            in_package = True
            continue
        if in_package and line and not line[0].isspace():
            break
        if in_package and line.strip() == "- if: $CI_COMMIT_TAG":
            lines.insert(index + 1, extra_line)
            return "\n".join(lines) + "\n"
    fail("package rules line missing")


def insert_package_rules_allow_failure(text):
    return insert_package_rules_line(text, "      allow_failure: true")


def insert_anchor_allow_failure(text):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(".deps:") and "&deps" in line:
            lines.insert(index + 1, "  allow_failure: true")
            return "\n".join(lines) + "\n"
    fail("deps anchor missing")


def add_extends_allow_failure(text):
    if "\npackage:\n" not in text:
        fail("missing package job")
    text = text.replace("\npackage:\n", "\n.relaxed:\n  allow_failure: true\n\npackage:\n", 1)
    return insert_under(text, "package", "  extends: .relaxed")


def extends_allow_failure_overridden(text):
    text = add_extends_allow_failure(text)
    return insert_under(text, "package", "  allow_failure: false")


def add_default_allow_failure(text):
    if "default:\n" not in text:
        fail("missing default block")
    return text.replace("default:\n", "default:\n  allow_failure: true\n", 1)


def default_allow_failure_overridden(text):
    return insert_under(add_default_allow_failure(text), "package", "  allow_failure: false")


def add_allow_failure_alias(text):
    if "default:\n" not in text:
        fail("missing default block")
    text = text.replace("default:\n", "default:\n  .yes: &yes true\n", 1)
    return insert_under(text, "package", "  allow_failure: *yes")


def add_release_when_anchor(text):
    if "\nrelease:\n" not in text:
        fail("missing release job")
    text = text.replace("\nrelease:\n", "\n.always: &always\n  when: always\n\nrelease:\n", 1)
    return insert_under(text, "release", "  <<: *always")


def insert_release_rules_line(text, extra_line):
    lines = text.splitlines()
    in_release = False
    for index, line in enumerate(lines):
        if line == "release:":
            in_release = True
            continue
        if in_release and line and not line[0].isspace():
            break
        if in_release and line.strip() == "- if: $CI_COMMIT_TAG":
            lines.insert(index + 1, extra_line)
            return "\n".join(lines) + "\n"
    fail("release rules line missing")


def insert_release_rules_when_always(text):
    return insert_release_rules_line(text, "      when: always")


def add_package_rule_merge(text):
    if "\npackage:\n" not in text:
        fail("missing package job")
    text = text.replace(
        "\npackage:\n",
        "\n.unsafe_rule: &unsafe_rule\n  allow_failure: true\n\npackage:\n",
        1,
    )
    return insert_package_rules_line(text, "      <<: *unsafe_rule")


def add_other_job_with_release_key(text):
    return (
        text
        + "\npublish:\n"
        + "  stage: test\n"
        + "  script:\n"
        + "    - echo publish\n"
        + "  release:\n"
        + "    tag_name: $CI_COMMIT_TAG\n"
        + "    description: ./docs/RELEASE-NOTES.md\n"
    )


def add_other_job_with_release_cli(text):
    return (
        text
        + "\npublish:\n"
        + "  stage: test\n"
        + "  image: registry.gitlab.com/gitlab-org/release-cli:latest\n"
        + "  script:\n"
        + "    - echo publish\n"
    )


def add_other_job_with_glab_release(text):
    return (
        text
        + "\npublish:\n"
        + "  stage: test\n"
        + "  script:\n"
        + '    - glab release create "$CI_COMMIT_TAG"\n'
    )


def add_other_job_extends_release(text):
    return (
        text
        + "\npublish:\n"
        + "  extends: release\n"
        + "  needs: []\n"
    )


def add_other_job_extends_package(text):
    return (
        text
        + "\npublish:\n"
        + "  extends: package\n"
        + "  needs: []\n"
    )


def add_other_job_flow_extends_release(text):
    return (
        text
        + "\npublish:\n"
        + "  extends: [release]\n"
        + "  stage: test\n"
        + "  needs: []\n"
    )


def add_other_job_flow_extends_package(text):
    return (
        text
        + "\npublish:\n"
        + "  extends: [ .deps , package ]\n"
        + "  stage: test\n"
        + "  needs: []\n"
    )


def add_next_line_flow_mapping_job(text):
    return (
        text
        + "\npublish:\n"
        + "  {extends: release, needs: []}\n"
    )


def add_other_job_4space_extends_release(text):
    return (
        text
        + "\npublish:\n"
        + "    extends: release\n"
        + "    stage: test\n"
        + "    needs: []\n"
    )


def add_other_job_1space_extends_release(text):
    return (
        text
        + "\npublish:\n"
        + " extends: release\n"
        + " needs: []\n"
    )


def add_other_job_4space_flow_extends_release(text):
    return (
        text
        + "\npublish:\n"
        + "    extends: [release]\n"
        + "    stage: test\n"
        + "    needs: []\n"
    )


def add_other_job_4space_flow_extends_package(text):
    return (
        text
        + "\npublish:\n"
        + "    extends: [.deps, package]\n"
        + "    stage: test\n"
        + "    needs: []\n"
    )


def add_flow_mapping_release_job(text):
    return (
        text
        + "\npublish: {stage: test, release: {tag_name: $CI_COMMIT_TAG, description: ./docs/RELEASE-NOTES.md}}\n"
    )


def add_other_job_with_releases_api(text):
    return (
        text
        + "\npublish:\n"
        + "  stage: test\n"
        + "  script:\n"
        + '    - \'curl --header "JOB-TOKEN: $CI_JOB_TOKEN" "$CI_API_V4_URL/projects/$CI_PROJECT_ID/releases"\'\n'
    )


def add_col0_comment_duplicate_package_script(text):
    marker = "\nrelease:\n"
    if marker not in text:
        fail("missing release job")
    extra = (
        "# col0 comment inside package\n"
        "  script:\n"
        "    - echo overridden\n"
        "\nrelease:\n"
    )
    return text.replace(marker, "\n" + extra, 1)


def add_col0_comment_duplicate_deps_before_script(text):
    marker = "\nlint:\n"
    if marker not in text:
        fail("missing lint job")
    extra = (
        "# col0 comment inside .deps\n"
        "  before_script:\n"
        "    - echo overridden\n"
        "\nlint:\n"
    )
    return text.replace(marker, "\n" + extra, 1)


def add_gate_or_true(text):
    old = "' docs/RELEASE-NOTES.md"
    if old not in text:
        fail("missing gate command suffix")
    return text.replace(old, "' docs/RELEASE-NOTES.md || true", 1)


def add_codex_col0_comment_before_script(text):
    marker = "\nrelease:\n"
    if marker not in text:
        fail("missing release job")
    extra = (
        "# package-local setup\n"
        "  before_script:\n"
        "    - exit 0\n"
        "\nrelease:\n"
    )
    return text.replace(marker, "\n" + extra, 1)


def add_include(text):
    return "include:\n  - local: extra.yml\n\n" + text


def move_release_job_above_package(text):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line == "release:")
    block = lines[start:]
    del lines[start:]
    dest = next(i for i, line in enumerate(lines) if line == "package:")
    lines[dest:dest] = block + [""]
    return "\n".join(lines) + "\n"


def expect_ci(text, label, red, pin_only=False):
    problem = ci_problem(text, emit_diff=False)
    if red:
        if problem is None:
            fail(f"mutation {label} still passed")
        if pin_only:
            if (
                PIN_MISMATCH_MSG not in problem
                or "--- " not in problem
                or "+++ " not in problem
                or "@@" not in problem
            ):
                fail(f"mutation {label} did not fail with pinned unified diff: {problem}")
        else:
            if tracker_problem(text) is None:
                fail(f"mutation {label} was not caught by the semantic tracker")
        first_line = problem.splitlines()[0]
        print(f"release-notes-gate: mutation {label}: red ({first_line})")
        return
    if problem is not None:
        fail(f"{label} was refused: {problem}")
    print(f"release-notes-gate: {label}: pass")


def expect_tracker_override(text, label):
    t_prob = tracker_problem(text)
    if t_prob is not None:
        fail(f"{label} was refused by tracker: {t_prob}")
    p_prob = pin_problem(text, emit_diff=False)
    if p_prob is None or PIN_MISMATCH_MSG not in p_prob:
        fail(f"{label} changed a release-path block without triggering the pin")
    print(f"release-notes-gate: {label}: pass")


def expect_fence_mutant(ci, fenced_fixture):
    mutated = reintroduce_fence_exemption(ci)
    blocks, anchors, _saw_include = parse_document(mutated)
    package = effective_job("package", blocks, anchors, blocks.get("default", {}), frozenset())
    mutant_cmd = next(cmd for cmd in list_commands(package.get("script")) if is_gate(cmd))
    rc, _out = run_gate(mutant_cmd, fenced_fixture)
    if rc != 0:
        fail("mutation fence exemption was not caught by the fenced-wording fixture")
    print("release-notes-gate: mutation fence exemption: red (fenced wording was accepted)")


def expect_gate_ci_mutant(ci, old_piece, new_piece, label, fixtures, expect_pass_to_fail=False):
    if old_piece not in ci or old_piece not in PINNED_CI_BLOCKS["package"]:
        fail(f"cannot build gate mutant {label}: missing {old_piece!r}")
    mutated_ci = ci.replace(old_piece, new_piece, 1)
    saved_pkg = PINNED_CI_BLOCKS["package"]
    try:
        PINNED_CI_BLOCKS["package"] = saved_pkg.replace(old_piece, new_piece, 1)
        p_prob = pin_problem(mutated_ci, emit_diff=False)
        if p_prob is not None:
            fail(f"mutant {label} unexpectedly failed pin_problem: {p_prob}")
        t_prob = tracker_problem(mutated_ci)
        if t_prob is None:
            fail(f"tracker_problem did not reject mutant {label}")
        c_prob = ci_problem(mutated_ci, emit_diff=False)
        if c_prob is None:
            fail(f"ci_problem did not reject mutant {label}")
    finally:
        PINNED_CI_BLOCKS["package"] = saved_pkg
    blocks, anchors, _saw_include = parse_document(mutated_ci)
    package = effective_job("package", blocks, anchors, blocks.get("default", {}), frozenset())
    mutant_cmd = next(cmd for cmd in list_commands(package.get("script")) if is_gate(cmd))
    g_prob = gate_problem(mutant_cmd)
    if g_prob is None:
        fail(f"gate_problem did not reject mutant {label}")
    for fix_label, fix_text in fixtures:
        rc, _out = run_gate(mutant_cmd, fix_text)
        if expect_pass_to_fail:
            if rc == 0:
                fail(f"mutant {label} still accepted must-pass case {fix_label}")
        elif rc != 0:
            fail(f"mutant {label} still refused {fix_label}")
    print(f"release-notes-gate: mutation {label}: red ({t_prob})")


def expect_all_refuse(command, cases, tag="v9.9.9"):
    accepted = []
    for label, text, needles in cases:
        rc, output = run_gate(command, text, tag=tag)
        verdict = "refuse" if rc else "pass"
        print(f"release-notes-gate: {label}: {verdict}")
        if rc == 0:
            accepted.append(label)
            continue
        for needle in needles:
            if needle not in output:
                fail(f"{label} message missing {needle!r}: {output}")
    if accepted:
        fail(f"{', '.join(accepted)} accepted unpublished wording")


def current_tag(text):
    lines = text.splitlines()
    if not lines:
        return None
    prefix = "# Googlebook Anywhere "
    line = lines[0]
    if not line.startswith(prefix):
        return None
    token = line[len(prefix):].split(" ", 1)[0]
    if re.fullmatch(r"v[0-9]+(?:\.[0-9]+)*", token):
        return token
    return None


LINE_BREAK_CHARS = {
    "\r": "U+000D (carriage return)",
    "\v": "U+000B (vertical tab)",
    "\f": "U+000C (form feed)",
    "\x1c": "U+001C (file separator)",
    "\x1d": "U+001D (group separator)",
    "\x1e": "U+001E (record separator)",
    "\x85": "U+0085 (next line)",
    "\u2028": "U+2028 (line separator)",
    "\u2029": "U+2029 (paragraph separator)",
}


HISTORY = REPO / "docs/RELEASE-HISTORY.md"
HISTORY_TITLE = "# Googlebook Anywhere release history"
# The whole heading lint (round 7): a line 2..N that starts at column 0 with 1-6 '#', a
# space or tab, and 'Googlebook Anywhere v<digit>'. No fence tracking, no exemptions.
OLDER_HEADING = re.compile(r"#{1,6}[ \t]+Googlebook Anywhere v[0-9]")
HEADING_WORKAROUND = (
    "to keep such a line in an example, indent it by 4 spaces, or put the whole fenced code "
    "block inside a list item or a quote (the lint does not look there)"
)


def older_heading_line(text):
    """The line number of the first column-0 '# Googlebook Anywhere v<digit>' heading on lines 2..N, else None."""
    for lineno, line in enumerate(text.split("\n"), 1):
        if lineno > 1 and OLDER_HEADING.match(line):
            return lineno
    return None


STATUS_PREFIX = "<!-- release-status"


def version_heading_problem(text):
    """Refuse notes that are not one plain current-version file.

    The notes are expected as raw bytes for this check: a "\\r" or any other
    character that str.splitlines() breaks on but awk does not (LF is the only
    awk line break) refuses the file, and so does any Unicode format character
    (category Cf: zero-width space/joiner/non-joiner, word joiner, a BOM after the
    start, bidi controls, soft hyphen), which no reader can see. A line 2..N may not
    start at column 0 with 1-6 '#', a space or tab, and 'Googlebook Anywhere v<digit>'
    (older_heading_line; no fence tracking): the file holds only the current version,
    which the release job publishes verbatim, and older versions live in
    docs/RELEASE-HISTORY.md.
    """
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    for lineno, line in enumerate(text.split("\n"), 1):
        for column, char in enumerate(line):
            if char in LINE_BREAK_CHARS:
                return (
                    f"docs/RELEASE-NOTES.md line {lineno}: expected only '\\n' as a line break; "
                    f"found {LINE_BREAK_CHARS[char]}"
                )
            if unicodedata.category(char) == "Cf" and not (lineno == 1 and column == 0 and char == "\ufeff"):
                return (
                    f"docs/RELEASE-NOTES.md line {lineno}: expected no invisible format character; "
                    f"found U+{ord(char):04X} ({unicodedata.name(char, 'format character').lower()})"
                )
    lineno = older_heading_line(text)
    if lineno is not None:
        return (
            f"docs/RELEASE-NOTES.md line {lineno}: expected only the current version; a column-0 "
            "'# Googlebook Anywhere v...' heading after line 1 (any version, the current one too) is "
            "refused: the file holds one section, and an earlier section belongs in "
            f"docs/RELEASE-HISTORY.md; {HEADING_WORKAROUND}"
        )
    lines = text.split("\n")
    if not text.endswith("\n"):
        return (
            f"docs/RELEASE-NOTES.md line {len(lines)}: expected the file to end with exactly one newline; "
            "found none (the release job then joins the history link to the last line)"
        )
    if text.endswith("\n\n"):
        last = len(lines) - 1
        while last > 1 and lines[last - 1] == "":
            last -= 1
        return (
            f"docs/RELEASE-NOTES.md line {last + 1}: expected the file to end with exactly one newline; "
            "found empty lines at the end"
        )
    return None


def notes_heading_problem(path):
    """Run version_heading_problem() on the raw bytes of a notes file.

    A text read would turn a lone "\\r" into "\\n" before the check could see it.
    """
    return version_heading_problem(Path(path).read_bytes())


def history_problem(path):
    """docs/RELEASE-HISTORY.md must exist and start with its title line."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return "docs/RELEASE-HISTORY.md is missing (older release sections live there)"
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except OSError as exc:
        return f"docs/RELEASE-HISTORY.md cannot be read ({type(exc).__name__})"
    except UnicodeDecodeError as exc:
        line = data[:exc.start].count(b"\n") + 1
        return f"docs/RELEASE-HISTORY.md line {line}: expected UTF-8 text; found byte 0x{data[exc.start]:02x}"
    first = data.split(b"\n", 1)[0]
    if first != HISTORY_TITLE.encode("utf-8"):
        return f"docs/RELEASE-HISTORY.md line 1 expected {HISTORY_TITLE!r}"
    for lineno, line in enumerate(text.split("\n"), 1):
        if STATUS_PREFIX in line:
            return (
                f"docs/RELEASE-HISTORY.md line {lineno}: expected no release-status line (drop line 3 of a "
                "section when it moves here)"
            )
    return None


def run_main_on(notes, history=HISTORY, notes_only=True):
    captured = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            main(notes=notes, notes_only=notes_only, history=history)
    except SystemExit as exc:
        code = exc.code
    return code, captured.getvalue()


def notes_lint_checks(real, tag):
    """The notes hold only the current version (round 3): the heading lint, the history
    file, the raw-bytes / lone-CR and Unicode Cf (D3) refusals through the real main()."""
    for label, notes, needle in (
        ("older top-level heading", real + "\n# Googlebook Anywhere v0.2.1\n", "older top-level heading"),
        ("older sub-heading", "# Googlebook Anywhere v9.9.9\n\nx\n## Googlebook Anywhere v0.2.1\n", "md line 4:"),
        ("six-hash heading", "# Googlebook Anywhere v9.9.9\n\n###### Googlebook Anywhere v0.2.1\n", "md line 3:"),
    ):
        problem = version_heading_problem(notes)
        if problem is None:
            fail(f"heading lint: {label} was not refused")
        if needle != label and needle not in problem:
            fail(f"heading lint: {label} message missing {needle!r}: {problem}")
        if "belongs in docs/RELEASE-HISTORY.md" not in problem:
            fail(f"heading lint: {label} message does not name the history file: {problem}")
        print(f"release-notes-gate: heading lint {label}: refuse")
    for label, notes in (
        ("the real notes", real),
        ("prose mention of an older version", "# Googlebook Anywhere v9.9.9\n\nsee Googlebook Anywhere v0.2.0 notes\n"),
        ("sub-heading about something else", "# Googlebook Anywhere v9.9.9\n\n## Googlebook hardware\n"),
    ):
        problem = version_heading_problem(notes)
        if problem is not None:
            fail(f"heading lint: {label} was refused: {problem}")
        print(f"release-notes-gate: heading lint {label}: pass")
    # D3: every Unicode format character is refused with its line, wherever it is.
    for char in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u202e", "\u2066", "\u200e", "\u00ad"):
        problem = version_heading_problem(f"plain text\n\nordinary{char}prose\n")
        needle = f"md line 3: expected no invisible format character; found U+{ord(char):04X}"
        if problem is None or needle not in problem:
            fail(f"format character U+{ord(char):04X} was not refused at line 3: {problem}")
    if version_heading_problem("\ufeffplain text\n") is not None:
        fail("a BOM at the very start was refused")
    print("release-notes-gate: Unicode format characters (Cf) refused with their line; leading BOM allowed: pass")
    for label, notes, needle in (
        ("raw CR inside line (bytes)", b"## old\r# Googlebook Anywhere v0.2.2", "line 1: expected only '\\n'"),
        ("CRLF line (bytes)", b"```text\nfence example\n```\r\n", "line 3: expected only '\\n'"),
        ("U+2028 inside line", "## \u2028# Googlebook Anywhere v0.2.1\n", "U+2028"),
    ):
        problem = version_heading_problem(notes)
        if problem is None or needle not in problem:
            fail(f"{label} was not refused with {needle!r}: {problem}")
        print(f"release-notes-gate: {label}: refuse")
    # The real main() reads the notes as raw bytes and checks the history file.
    with tempfile.TemporaryDirectory(prefix="gbos-notes-main-") as tmp_main:
        tmp_main = Path(tmp_main)
        lone_cr = tmp_main / "RELEASE-NOTES.md"
        lone_cr.write_bytes(
            b"# Googlebook Anywhere v9.9.9\n\n" + RELEASED_LINE.encode() + b"\n\n## old\r# Googlebook Anywhere v0.2.2\n"
        )
        code, output = run_main_on(lone_cr)
        if code != 1 or "md line 5: expected only '\\n' as a line break; found U+000D" not in output:
            fail(f"main() on a notes file with a lone CR did not refuse it from the raw bytes: rc={code} {output!r}")
        print("release-notes-gate: main() reads the notes file as raw bytes (lone CR): refuse")
        clean = tmp_main / "clean.md"
        clean.write_text("# Googlebook Anywhere v9.9.9\n\n" + RELEASED_LINE + "\n\nx\n", encoding="utf-8")
        older = tmp_main / "older.md"
        older.write_text(clean.read_text(encoding="utf-8") + "\n# Googlebook Anywhere v9.9.8\n\nold\n", encoding="utf-8")
        code, output = run_main_on(older)
        if code != 1 or "md line 7: expected only the current version" not in output:
            fail(f"main() accepted notes with an older version heading: rc={code} {output!r}")
        print("release-notes-gate: main() on notes with an older version heading: refuse")
        wrong_title = tmp_main / "wrong-history.md"
        wrong_title.write_text("# Release history\n\n# Googlebook Anywhere v9.9.8\n", encoding="utf-8")
        for label, history, needle in (
            ("missing history file", tmp_main / "absent.md", "docs/RELEASE-HISTORY.md is missing"),
            ("history file without its title line", wrong_title, "docs/RELEASE-HISTORY.md line 1 expected"),
        ):
            code, output = run_main_on(clean, history=history)
            if code != 1 or needle not in output:
                fail(f"main() did not refuse a {label}: rc={code} {output!r}")
            print(f"release-notes-gate: main() with a {label}: refuse")
        code, output = run_main_on(clean)
        if code not in (0, None):
            fail(f"main() refused clean notes with the real history file: rc={code} {output!r}")
        print("release-notes-gate: main() on clean notes with the real history file: pass")


SIM_ENV = "GBOS_NOTES_GATE_NO_SIM"


def next_tag(tag):
    parts = tag[1:].split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return "v" + ".".join(parts)


def move_current_to_history(real, history, layout="documented"):
    """The README procedure, literally: delete line 3 (the status comment) of the released
    notes, then insert the remaining section directly below line 1 (the title) of the
    history. The documented layout is: line 1 the title, line 2 empty, the moved section,
    one empty line, then the older sections as they were (any empty lines that followed the
    title are not repeated). layout="pasted" is the bare paste with no empty lines added;
    the history check accepts both, and whatever the history had on line 2."""
    lines = real.split("\n")
    if len(lines) < 3 or not lines[2].startswith(STATUS_PREFIX):
        fail("docs/RELEASE-NOTES.md line 3 must be the release-status line")
    moved = "\n".join(lines[:2] + lines[3:])
    if not moved.endswith("\n"):
        moved += "\n"
    title, _sep, rest = history.partition("\n")
    if title != HISTORY_TITLE:
        fail(f"docs/RELEASE-HISTORY.md line 1 expected {HISTORY_TITLE!r}")
    if layout == "pasted":
        return title + "\n" + moved + rest
    return title + "\n\n" + moved + "\n" + rest.lstrip("\n")


def simulation_tree(tmp):
    """A copy of the tracked files (or of the tree when there is no git index)."""
    dest = Path(tmp) / "tree"
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True)
    if listed.returncode == 0 and listed.stdout:
        for rel in listed.stdout.decode("utf-8").split("\0"):
            if not rel or not (REPO / rel).is_file():
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, target, follow_symlinks=False)
    else:
        shutil.copytree(REPO, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    return dest


SIM_RAN = []


def next_release_simulation(real, history, dry=False):
    """N1: the real notes must not be constrained by the gate's self-tests. Follow the
    documented procedure for the next release (and two legal edits of today's notes) on a
    copy of the tree; the gate must pass on each. Round 8 decision: the gate never runs
    tests/stick/docs-story-test.py (CI runs it on the real tree), so a simulation checks
    only the gate's own rules. dry=True walks every history-dependent step in-process
    without starting a gate (the NM3 check on unusual history layouts)."""
    if os.environ.get(SIM_ENV) and not dry:
        print("release-notes-gate: next-release simulation: skipped inside a simulation")
        return
    tag = current_tag(real)
    new = next_tag(tag)
    # The section that moves is the released one (as on the tag commit).
    released = flip_real(real, tag)
    moved_history = move_current_to_history(released, history)
    head = f"# Googlebook Anywhere {new}\n\n{UNRELEASED_LINE}\n\n本版延續上一版。\n\n"
    reused = next((l for l in real.split("\n") if l.startswith("- ")), "- 沿用的已知問題。")
    # L4: the repo's own development-period format, and the same notes flipped for the tag.
    dev = (f"# Googlebook Anywhere {new} (未發布)\n\n{UNRELEASED_LINE}\n\n這一版還沒有發布。"
           "本版延續上一版。\n\n## 驗證紀錄\n\n- 尚待補上。\n")
    flipped_next = flip_real(dev, new)
    title_only = HISTORY_TITLE + "\n" + history.partition("\n")[2].lstrip("\n")
    # README: drop line 3 (the status comment), move the rest unchanged below the title.
    lines = released.split("\n")
    del lines[2]
    h_title, _sep, h_rest = history.partition("\n")
    literal = h_title + "\n\n" + "\n".join(lines).rstrip("\n") + "\n\n" + h_rest.lstrip("\n")
    literal_next = (f"# Googlebook Anywhere {new}\n\n{RELEASED_LINE}\n\n本版延續上一版。\n\n"
                    "## 新版驗證\n\n- 新版內容。\n")
    literal_dev = (f"# Googlebook Anywhere {new} (未發布)\n\n{UNRELEASED_LINE}\n\n這一版還沒有發布。"
                   "本版延續上一版。\n\n## 新版驗證\n\n- 新版內容。\n")
    variants = (
        ("next release with a 驗證紀錄 section", head + "## 驗證紀錄\n\n- 尚待補上。\n", moved_history, True),
        ("next release without a 驗證紀錄 section", head + "## 變更\n\n- 新項目。\n", moved_history, True),
        ("next release reusing a known issue line", head + "## 已知問題與限制\n\n" + reused + "\n", moved_history, True),
        ("codex legal edit: one more item", real + "- 其他見 README「現況」與 docs/STATUS-KANO.md。\n", history, True),
        ("codex legal edit: renamed sub-heading",
         re.sub(r"^## .*$", "## 測試紀錄", real, count=1, flags=re.M), history, True),
        # L3: the literal procedure on a history whose title has no empty line after it,
        # and the bare paste right below line 1.
        ("codex I2 history title directly followed by a section", head + "## 變更\n\n- 新項目。\n",
         move_current_to_history(released, title_only), True),
        ("claude I-1 section pasted right below the title", head + "## 變更\n\n- 新項目。\n",
         move_current_to_history(released, history, layout="pasted"), True),
        ("today's history without the empty line 2", real, title_only, True),
        # Codex r6 I2: history layouts the README procedure allows; the gate never indexes them.
        ("codex r6 I2 history with only its title", real, HISTORY_TITLE + "\n", True),
        ("codex r6 I2 version heading with no empty line after it", real,
         HISTORY_TITLE + "\n# Googlebook Anywhere v0.2.1\nOld notes.\n", True),
        ("codex r6 I2 history with prose and no version heading", real,
         HISTORY_TITLE + "\n\n較早版本的說明。\n", True),
        # Round 8 G2 (claude): the README move done literally on the REAL notes, new notes
        # without the report-only sentence; the gate alone must accept it.
        ("README-literal move of the real notes, new released notes, CI=1", literal_next, literal, True),
        ("README-literal move of the real notes, new development notes, no CI", literal_dev, literal, False),
        # New notes without the report-only sentence on a history that has none either: the
        # gate's rules hold; only docs-story (not run here) would object.
        ("new notes without the report-only sentence on a title-only history, CI=1", literal_next,
         HISTORY_TITLE + "\n", True),
        ("claude m-2 development format (未發布), gate without CI=1", dev, moved_history, False),
        ("claude m-2 development format flipped for the tag, CI=1", flipped_next, moved_history, True),
    )
    run = subprocess.run
    if dry:
        def run(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, "", "")
    with tempfile.TemporaryDirectory(prefix="gbos-next-release-") as tmp:
        tree = simulation_tree(tmp)
        for label, notes, hist, with_ci in variants:
            env = dict(os.environ, **{SIM_ENV: "1"})
            if with_ci:
                env["CI"] = "1"
            else:
                env.pop("CI", None)
            (tree / "docs/RELEASE-NOTES.md").write_text(notes, encoding="utf-8")
            (tree / "docs/RELEASE-HISTORY.md").write_text(hist, encoding="utf-8")
            proc = run([sys.executable, "-I", "tests/stick/release-notes-gate.py"], cwd=tree,
                       env=env, capture_output=True, text=True, timeout=120)
            if dry:
                continue
            if proc.returncode != 0:
                tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
                fail(f"next-release simulation {label}: gate rc={proc.returncode}: {tail}")
            SIM_RAN.append(label)
            print(f"release-notes-gate: next-release simulation {label}: gate pass")
        # K3 / K4: the full gate entry on a broken history: one clean failure line, no
        # traceback; and the history scan is really called from main().
        (tree / "docs/RELEASE-NOTES.md").write_text(real, encoding="utf-8")
        # F2: a synthetic history, never an index into the real file; F3 (codex r5 M2): the
        # wording sits at the tail of a long history, so a truncated scan misses it.
        wording = (HISTORY_TITLE + "\n\n# Googlebook Anywhere v0.2.1\n\n"
                   + "".join(f"- 舊版項目 {i}。\n" for i in range(400))
                   + "\n# Googlebook Anywhere v0.1.0\n\nunreleased build\n")
        env = dict(os.environ, CI="1", **{SIM_ENV: "1"})
        for label, data, needle in (
            ("codex r5 M3 / r6 I2 wording at the tail of a synthetic history", wording.encode("utf-8"),
             "docs/RELEASE-HISTORY.md lines 2..N fixture still has unpublished wording"),
            ("codex r5 M2 non-UTF-8 history", (HISTORY_TITLE + "\n\nok \xff\n").encode("latin-1"),
             "docs/RELEASE-HISTORY.md line 3: expected UTF-8 text; found byte 0xff"),
            ("missing history", None, "docs/RELEASE-HISTORY.md is missing (older release sections live there)"),
        ):
            target = tree / "docs/RELEASE-HISTORY.md"
            target.unlink(missing_ok=True)
            if data is not None:
                target.write_bytes(data)
            proc = run([sys.executable, "-I", "tests/stick/release-notes-gate.py"], cwd=tree,
                       env=env, capture_output=True, text=True, timeout=120)
            if dry:
                continue
            errors = [l for l in proc.stderr.splitlines() if l.strip()]
            if (proc.returncode == 0 or "Traceback" in proc.stdout + proc.stderr
                    or errors != [f"release-notes-gate: {needle}"]):
                fail(f"full gate entry, {label}: expected rc 1 and the one line {needle!r}; "
                     f"got rc={proc.returncode}, stderr={errors[-3:]}")
            print(f"release-notes-gate: full gate entry {label}: one clean failure line")


def round4_checks(real, history):
    """Review round 3: an indented code line is not checked, one final newline, the D3
    category and BOM boundaries, no status line in the history, the next release."""
    code_block = "# Googlebook Anywhere v9.9.9\n\n    # Googlebook Anywhere v0.2.1 (indented code)\n"
    if version_heading_problem(code_block) is not None:
        fail(f"heading lint: a 4-space indented code line was refused: {version_heading_problem(code_block)}")
    print("release-notes-gate: heading lint 4-space indented code line: pass")
    # N3: exactly one final newline, so the release job always leaves an empty line.
    for label, notes, needle in (
        ("codex r3 M3 no final newline", "# Googlebook Anywhere v9.9.9\n\nlast line", "md line 3: expected the file to end with exactly one newline"),
        ("trailing empty lines", "# Googlebook Anywhere v9.9.9\n\nlast line\n\n\n", "md line 4: expected the file to end with exactly one newline"),
    ):
        problem = version_heading_problem(notes)
        if problem is None or needle not in problem:
            fail(f"final newline lint: {label} was not refused with {needle!r}: {problem}")
        print(f"release-notes-gate: final newline lint {label}: refuse")
    if version_heading_problem("# Googlebook Anywhere v9.9.9\n\nlast line\n") is not None:
        fail("final newline lint: a file ending with one newline was refused")
    print("release-notes-gate: final newline lint exactly one newline: pass")
    # N5: D3 refuses exactly Cf; a BOM is allowed only at the very start of line 1.
    for char in ("\u0301", "\u034f", "\ufe0f", "\u00a0"):
        problem = version_heading_problem(f"# Googlebook Anywhere v9.9.9\n\nCafe{char} notes\n")
        if problem is not None:
            fail(f"D3: U+{ord(char):04X} ({unicodedata.category(char)}) is not Cf but was refused: {problem}")
    print("release-notes-gate: D3 non-Cf marks (U+0301, U+034F, U+FE0F, U+00A0): pass")
    for notes, needle in (
        ("# Googlebook Anywhere v9.9.9\n\ufeff# Googlebook Anywhere v0.2.1\n", "md line 2: expected no invisible format character; found U+FEFF"),
        ("# Googlebook Anywhere v9.9.9\n\n\ufeffprose\n", "md line 3: expected no invisible format character; found U+FEFF"),
        ("x\ufeff\n", "md line 1: expected no invisible format character; found U+FEFF"),
    ):
        problem = version_heading_problem(notes)
        if problem is None or needle not in problem:
            fail(f"D3: a BOM outside line 1 column 0 was not refused with {needle!r}: {problem}")
    print("release-notes-gate: D3 BOM after line 1 column 0: refuse")
    # N2: a moved section drops its status line; the history has none.
    with tempfile.TemporaryDirectory(prefix="gbos-history-") as tmp:
        bad = Path(tmp) / "RELEASE-HISTORY.md"
        bad.write_text(HISTORY_TITLE + "\n\n# Googlebook Anywhere v9.9.8\n\n" + RELEASED_LINE + "\n\nold\n", encoding="utf-8")
        problem = history_problem(bad)
        if problem is None or "line 5: expected no release-status line" not in problem:
            fail(f"a history file with a status line was not refused: {problem}")
        print("release-notes-gate: history file with a release-status line: refuse")
        good = Path(tmp) / "moved.md"
        good.write_text(move_current_to_history(real, history), encoding="utf-8")
        if history_problem(good) is not None:
            fail(f"the documented move left a history the gate refuses: {history_problem(good)}")
    print("release-notes-gate: documented move of the current section: history accepted")
    next_release_simulation(real, history)
    # Round 8 G2 (NM3): every history-dependent step of the simulation on history layouts
    # the README allows, in-process; an index into the real history raises here.
    for label, layout in (
        ("a history with only its title", HISTORY_TITLE + "\n"),
        ("a version heading with no empty line after it", HISTORY_TITLE + "\n# Googlebook Anywhere v0.2.1\nOld notes.\n"),
        ("prose and no version heading", HISTORY_TITLE + "\n\n較早版本的說明。\n"),
    ):
        try:
            next_release_simulation(real, layout, dry=True)
        except Exception as exc:  # noqa: BLE001 - the point of the test
            fail(f"next-release simulation on {label}: {type(exc).__name__}: {exc}")
        print(f"release-notes-gate: next-release simulation steps on {label}: pass")


def history_unpublished_check(path):
    """L6: every line after the title of the history, whatever versions it holds, carries
    no unpublished wording (each moved section passed the package gate). Never an
    exception: a missing or unreadable file is a gate failure."""
    try:
        text = Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        fail(f"docs/RELEASE-HISTORY.md cannot be read: {type(exc).__name__}")
    assert_no_unpublished(text.partition("\n")[2], "docs/RELEASE-HISTORY.md lines 2..N")


def round5_checks(real):
    """Review round 4: quoted indented code, setext, pseudo headings and prose mentions
    pass (the round-7 lint only looks at column-0 ATX lines); the literal move
    procedure's output is accepted by the history check (L3); the history scan covers the
    whole file and never crashes (L6). The simulation variants (L3, L4) run in main."""
    top = "# Googlebook Anywhere v9.9.9\n\n" + RELEASED_LINE + "\n\n本版內容。\n\n"
    must_pass = (
        ("codex r4 I1 quoted 4-space code", top + "> Markdown 範例：\n>\n>     # Googlebook Anywhere v0.2.3\n"),
        ("quoted code with a tab-free 5-space body", top + ">      ## Googlebook Anywhere v0.2.1\n"),
        ("nested quoted 4-space code", top + "> > 範例\n> >\n> >     # Googlebook Anywhere v0.2.1\n"),
        ("unquoted 4-space code", top + "    # Googlebook Anywhere v0.2.1\n"),
        ("prose mention in a sentence", top + "這一版修正了 Googlebook Anywhere v0.2.1 的安裝碟問題。\n"),
        ("list item mentioning a version mid-sentence", top + "- 從 Googlebook Anywhere v0.2.1 升級時保留資料。\n"),
        ("bold mention mid-sentence", top + "請先升級到 **Googlebook Anywhere v0.2.1** 再更新。\n"),
        ("list item then a thematic break", top + "- 見 Googlebook Anywhere v0.2.1 的說明\n---\n"),
        ("setext heading about something else", top + "相容性\n======\n"),
        ("pseudo heading naming the current version", top + "**Googlebook Anywhere v9.9.9** 的重點：\n"),
        ("current-version line in the development format",
         "# Googlebook Anywhere v9.9.9 (未發布)\n\n" + UNRELEASED_LINE + "\n\n- Googlebook Anywhere v9.9.9 起改用新流程。\n"),
    )
    for label, notes in must_pass:
        problem = version_heading_problem(notes)
        if problem is not None:
            fail(f"heading lint: {label} was refused: {problem}")
        print(f"release-notes-gate: heading lint {label}: pass")
    must_fail = (
        ("column-0 older heading", top + "# Googlebook Anywhere v0.2.1\n", "md line 7:"),
        ("column-0 level-6 older heading with a tab", top + "######\tGooglebook Anywhere v0.1.0 舊版\n", "md line 7:"),
    )
    for label, notes, needle in must_fail:
        problem = version_heading_problem(notes)
        if problem is None or needle not in problem or "belongs in docs/RELEASE-HISTORY.md" not in problem:
            fail(f"heading lint: {label} was not refused at {needle!r}: {problem}")
        print(f"release-notes-gate: heading lint {label}: refuse")
    # The real notes path: the full main() refuses the notes with a column-0 older heading.
    if not real.endswith("\n"):
        fail("docs/RELEASE-NOTES.md must end with a newline")
    mixed = real + "\n# Googlebook Anywhere v0.2.1\n\n舊版正文。\n"
    with tempfile.TemporaryDirectory(prefix="gbos-mixed-setext-") as tmp:
        path = Path(tmp) / "RELEASE-NOTES.md"
        path.write_text(mixed, encoding="utf-8")
        code, output = run_main_on(path)
        needle = f"docs/RELEASE-NOTES.md line {real.count(chr(10)) + 2}: expected only the current version"
        if code == 0 or needle not in output or "belongs in docs/RELEASE-HISTORY.md" not in output:
            fail(f"the real notes with a column-0 older heading were not refused by the heading lint in main(): rc={code}: {output.strip().splitlines()[-2:]}")
    print("release-notes-gate: real notes with a column-0 older section heading: refuse")
    round6_checks(real)
    round7_checks(real)
    # L3: the literal procedure's output passes history_problem in both layouts and on a
    # history without the empty line 2.
    released = flip_real(real, current_tag(real))
    history = HISTORY.read_text(encoding="utf-8")
    title_only = HISTORY_TITLE + "\n" + history.partition("\n")[2].lstrip("\n")
    with tempfile.TemporaryDirectory(prefix="gbos-history-") as tmp:
        for label, text in (
            ("documented layout", move_current_to_history(released, history)),
            ("documented layout on a title-only line 2", move_current_to_history(released, title_only)),
            ("bare paste below the title", move_current_to_history(released, history, layout="pasted")),
        ):
            path = Path(tmp) / "RELEASE-HISTORY.md"
            path.write_text(text, encoding="utf-8")
            if history_problem(path) is not None:
                fail(f"literal move ({label}): the history check refused it: {history_problem(path)}")
            lines = text.split("\n")
            if lines[0] != HISTORY_TITLE or STATUS_PREFIX in text:
                fail(f"literal move ({label}): wrong layout")
            print(f"release-notes-gate: literal move {label}: history accepted")
        documented = move_current_to_history(released, title_only).split("\n")
        if documented[1] != "" or documented[2] != released.split("\n")[0]:
            fail("literal move: the documented layout must be title, empty line, moved heading")
    # L6: the history scan covers every section and never raises.
    with tempfile.TemporaryDirectory(prefix="gbos-history-scan-") as tmp:
        for label, text, expect_fail in (
            ("wording in the newest section", HISTORY_TITLE + "\n\n# Googlebook Anywhere v0.2.1\n\n這一版還沒有發布。\n", True),
            ("wording in the last section", HISTORY_TITLE + "\n\n# Googlebook Anywhere v0.2.1\n\nok\n\n# Googlebook Anywhere v0.0.1\n\nunreleased build\n", True),
            ("no v0.2.0 section at all", HISTORY_TITLE + "\n\n# Googlebook Anywhere v0.3.0\n\nok\n", False),
            ("title only, no newline", HISTORY_TITLE, False),
            ("not UTF-8", None, True),
            ("missing file", "MISSING", True),
        ):
            path = Path(tmp) / f"h{abs(hash(label))}.md"
            if text is None:
                path.write_bytes(b"\xff\xfe")
            elif text != "MISSING":
                path.write_text(text, encoding="utf-8")
            captured = io.StringIO()
            code = 0
            try:
                with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(captured):
                    history_unpublished_check(path)
            except SystemExit as exc:
                code = exc.code
            except Exception as exc:  # noqa: BLE001 - the point of the test
                fail(f"history scan ({label}) raised {type(exc).__name__}")
            if bool(code) != expect_fail:
                fail(f"history scan ({label}): expected {'refuse' if expect_fail else 'pass'}, rc={code}")
            print(f"release-notes-gate: history scan {label}: {'refuse' if expect_fail else 'pass'}")


def round6_checks(real):
    """Review round 5: the lint is minimal (one column-0 ATX rule). Every false-positive
    shape the reviewers reported is valid current-version content and must pass through the
    real main() (notes_only), and an unreadable history fails main() with one clean line."""
    top = "# Googlebook Anywhere v9.9.9\n\n" + RELEASED_LINE + "\n\n本版內容。\n\n"
    must_pass = (
        ("codex r5 I1 closed fence in a list item", "- ```text\n  # Googlebook Anywhere v0.2.3\n  ```\n"),
        ("codex r5 I1 ordered list ~~~ fence", "1. ~~~text\n   # Googlebook Anywhere v0.2.1\n   ~~~\n"),
        ("codex r5 I2 tab-indented code", "\tGooglebook Anywhere v0.2.1 安裝範例。\n"),
        ("codex r5 I2 quoted code with two tabs", "> \t\tGooglebook Anywhere v0.2.1\n"),
        ("codex r5 I3 wrapped sentence", "本版修正從\nGooglebook Anywhere v0.2.1 升級時的安裝問題。\n"),
        ("codex r5 I3 list continuation", "- 本版修正從\n  Googlebook Anywhere v0.2.1 升級時的問題。\n"),
        ("codex r5 I3 table without outer pipes", "Googlebook Anywhere v0.2.1 | 本版\n--- | ---\n舊行為 | 新行為\n"),
        ("codex r5 I3 current version in a code span", "`Googlebook Anywhere v9.9.9`USB 安裝流程已修正。\n"),
        ("codex r5 I4 sentence then a quoted rule", "相容於 Googlebook Anywhere v9.9.9。\n> ---\n"),
        ("codex r5 I4 current-version setext", "Googlebook Anywhere v9.9.9 相容性\n===\n"),
        ("claude r5 I-1 list sentence starting with the name", "- Googlebook Anywhere v0.2.1 的使用者可以直接升級，不用重灌。\n"),
        ("claude r5 I-1 tab code example", "範例輸出：\n\n\tGooglebook Anywhere v0.2.1 (build 1)\n"),
        ("claude r5 I-1 hard-wrapped continuation", "這一版修正了上一版\nGooglebook Anywhere v0.2.2 的安裝碟問題。\n"),
        ("claude r5 I-1 paragraph starting with the name (zh)", "Googlebook Anywhere v0.2.2 的使用者可以直接升級。\n"),
        ("claude r5 I-1 paragraph starting with the name (en)", "Googlebook Anywhere v0.2.2 users can upgrade in place.\n"),
        ("claude r5 I-1 range sentence", "Googlebook Anywhere v0.2.2 到 v0.2.3 之間沒有格式變更。\n"),
        ("claude r5 I-1 table row without a leading pipe", "Googlebook Anywhere v0.2.2 | 舊版\n"),
        ("claude r5 I-1 linked list item", "- [Googlebook Anywhere v0.2.2 的發行說明](docs/RELEASE-HISTORY.md)\n"),
        ("claude r5 I-1 list item starting with a code span", "- `Googlebook Anywhere v0.2.2` 的映像不再支援。\n"),
        ("claude r5 I-1 fence opened on a list-marker line", "1. ```\n   Googlebook Anywhere v0.2.1\n   ```\n"),
        ("claude r5 I-2 current-version setext sub-heading", "Googlebook Anywhere v9.9.9 的重點\n---\n"),
        ("claude r5 I-2 ATX heading then a rule", "## 從 Googlebook Anywhere v0.2.1 升級\n---\n"),
        ("claude r5 I-2 quote then an unquoted rule", "> 從 Googlebook Anywhere v0.2.2 升級時請先備份。\n---\n"),
        ("claude r5 I-2 table row then a rule", "| Googlebook Anywhere v0.2.2 | 舊 |\n---\n"),
        ("claude r5 I-2 upgrade sub-heading in setext form", "從 Googlebook Anywhere v0.2.2 升級\n---\n"),
        ("deepseek r5 I-1 tab-indented heading", "\t# Googlebook Anywhere v0.2.1\n"),
        ("deepseek r5 I-1 tab-indented sample line", "\tGooglebook Anywhere v0.2.1 的輸出範例\n"),
        ("deepseek r5 I-1 quoted tab-indented heading", "> \t# Googlebook Anywhere v0.2.1\n"),
        ("deepseek r5 I-1 quoted tab-indented sample", ">\t\tGooglebook Anywhere v0.2.1 範例\n"),
    )
    with tempfile.TemporaryDirectory(prefix="gbos-round6-") as tmp:
        path = Path(tmp) / "RELEASE-NOTES.md"
        for label, snippet in must_pass:
            path.write_text(top + snippet, encoding="utf-8")
            try:
                code, output = run_main_on(path)
            except Exception as exc:  # noqa: BLE001 - a traceback is a failure here
                fail(f"heading lint: {label} raised {type(exc).__name__} in main()")
            if code != 0:
                fail(f"heading lint: {label} was refused by main(): {output.strip().splitlines()[-1:]}")
            print(f"release-notes-gate: heading lint {label}: pass")
        # K3 through the real main(): an unreadable history is one clean failure line.
        path.write_text(top.rstrip("\n") + "\n", encoding="utf-8")
        hist = Path(tmp) / "RELEASE-HISTORY.md"
        cases = [
            ("non-UTF-8 history", (HISTORY_TITLE + "\n\nok \xff\n").encode("latin-1"),
             "release-notes-gate: docs/RELEASE-HISTORY.md line 3: expected UTF-8 text; found byte 0xff"),
            ("non-UTF-8 title line", b"\xfe# x\n", "release-notes-gate: docs/RELEASE-HISTORY.md line 1: expected UTF-8 text; found byte 0xfe"),
            ("missing history", None, "release-notes-gate: docs/RELEASE-HISTORY.md is missing (older release sections live there)"),
        ]
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            cases.append(("unreadable history", "chmod", "release-notes-gate: docs/RELEASE-HISTORY.md cannot be read (PermissionError)"))
        else:
            print("SKIP heading lint unreadable history (running as root)")
        for label, data, line in cases:
            hist.unlink(missing_ok=True)
            if data == "chmod":
                hist.write_text(HISTORY_TITLE + "\n", encoding="utf-8")
                hist.chmod(0)
            elif data is not None:
                hist.write_bytes(data)
            try:
                code, output = run_main_on(path, history=hist)
            except Exception as exc:  # noqa: BLE001 - the point of the test
                fail(f"main() with a {label} raised {type(exc).__name__}")
            finally:
                if hist.exists():
                    hist.chmod(0o600)
            if code == 0 or output.strip().splitlines() != [line]:
                fail(f"main() with a {label}: expected rc 1 and the one line {line!r}; got rc={code}: {output!r}")
            print(f"release-notes-gate: main() with a {label}: one clean failure line")


def round7_checks(real):
    """Review round 6: no fence logic. Shapes without a column-0 heading line pass through
    the real main(); a column-0 heading inside a top-level fence is refused with the
    workaround (spec change); legal gap shapes pass; read errors are one clean line."""
    top = "# Googlebook Anywhere v9.9.9\n\n" + RELEASED_LINE + "\n\n本版內容。\n\n"
    list_fence = "- ```text\n  sample\n  ```\n\n"
    must_pass = (
        ("codex r6 I1 list fence then a top-level fence, example indented",
         list_fence + "```\n    # Googlebook Anywhere v0.2.2\n```\n"),
        ("codex r6 I1 list fence then a top-level fence without a heading",
         list_fence + "```\nGooglebook Anywhere v0.2.2 sample\n```\n"),
        # Claude r7 M-1: a heading line itself in a list item renders as a real heading; it
        # passes only as a known lint gap, it is NOT the workaround.
        ("known lint gap (not the workaround): a heading line in a list item", "- ```bash\n  make\n  ```\n\n範例：\n\n- # Googlebook Anywhere v0.2.1\n"),
        ("claude r6 I-1 B list fence then ~~~ with an indented example", "- ```bash\n  make\n  ```\n~~~\n    # Googlebook Anywhere v0.2.1\n~~~\n"),
        ("claude r6 I-1 D ordered ~~~ list fence then a quoted example", "1. ~~~text\n   x\n   ~~~\n\n> ## Googlebook Anywhere v0.2.2\n"),
        ("claude r6 I-1 E line-start code span", "```diff``` 區塊現在會上色。\n\n```\n範例\n```\n"),
        ("claude r6 M-1 heading without a version", "## Googlebook Anywhere 安裝碟\n"),
        ("claude r6 M-1 seven hashes", "####### Googlebook Anywhere v0.2.1\n"),
        ("claude r6 M-1 no space after the hashes", "#Googlebook Anywhere v0.2.1\n"),
        ("claude r6 M-1 lowercase name", "## googlebook anywhere v0.2.1\n"),
        ("deepseek r6 double space in the name", "## Googlebook  Anywhere v0.2.1\n"),
        ("workaround: the whole fenced block inside a list item",
         "- 範例：\n\n  ```markdown\n  # Googlebook Anywhere v0.2.1\n  ```\n"),
        ("workaround: the whole fenced block inside a quote",
         "> ```markdown\n> # Googlebook Anywhere v0.2.1\n> ```\n"),
        ("fence info line inside a fence", "````\n```python\nprint(1)\n```\n````\n"),
    )
    must_fail = (
        ("codex r6 I1 exact: top-level fenced column-0 heading", list_fence + "```\n# Googlebook Anywhere v0.2.2\n```\n", 12),
        ("claude r6 I-1 A exact", "- ```bash\n  make\n  ```\n\n範例：\n\n```\n# Googlebook Anywhere v0.2.1\n```\n", 14),
        ("spec change: top-level fenced example", "```markdown\n# Googlebook Anywhere v0.2.1\n```\n", 8),
        ("spec change: ~~~ fenced level-2 example", "~~~\n## Googlebook Anywhere v0.2.1\n~~~\n", 8),
        ("claude r6 M-2 the current version repeated", "## Googlebook Anywhere v9.9.9 重點\n", 7),
        ("tab after the hashes", "###\tGooglebook Anywhere v0.1.0\n", 7),
    )
    with tempfile.TemporaryDirectory(prefix="gbos-round7-") as tmp:
        path = Path(tmp) / "RELEASE-NOTES.md"
        for label, snippet in must_pass:
            path.write_text(top + snippet, encoding="utf-8")
            code, output = run_main_on(path)
            if code != 0:
                fail(f"heading lint: {label} was refused by main(): {output.strip().splitlines()[-1:]}")
            print(f"release-notes-gate: heading lint {label}: pass")
        for label, snippet, lineno in must_fail:
            path.write_text(top + snippet, encoding="utf-8")
            code, output = run_main_on(path)
            needle = f"docs/RELEASE-NOTES.md line {lineno}: expected only the current version"
            # Codex r7 M1: literal text, so a degraded HEADING_WORKAROUND goes red.
            if (code == 0 or needle not in output
                    or "indent it by 4 spaces, or put the whole fenced code block inside a list item or a quote" not in output):
                fail(f"heading lint: {label} was not refused at line {lineno} with the workaround: rc={code}: {output.strip()[-300:]}")
            print(f"release-notes-gate: heading lint {label}: refuse (with the workaround)")
        # The workaround the message gives really passes.
        path.write_text(top + "```markdown\n    # Googlebook Anywhere v0.2.1\n```\n", encoding="utf-8")
        code, output = run_main_on(path)
        if code != 0:
            fail(f"heading lint: the documented workaround (indent by 4 spaces) was refused: {output.strip()[-200:]}")
        print("release-notes-gate: heading lint documented workaround: pass")
        # Claude/DeepSeek r6 M-4: the OSError branch of history_problem without chmod (also as root).
        path.write_text(top.rstrip("\n") + "\n", encoding="utf-8")
        hist = Path(tmp) / "RELEASE-HISTORY.md"
        hist.write_text(HISTORY_TITLE + "\n", encoding="utf-8")
        original = Path.read_bytes

        def refuse_history(self):
            if self == hist:
                raise PermissionError(13, "Permission denied")
            return original(self)

        Path.read_bytes = refuse_history
        try:
            code, output = run_main_on(path, history=hist)
        finally:
            Path.read_bytes = original
        line = "release-notes-gate: docs/RELEASE-HISTORY.md cannot be read (PermissionError)"
        if code == 0 or output.strip().splitlines() != [line]:
            fail(f"main() with a history that raises PermissionError: expected the one line {line!r}; got rc={code}: {output!r}")
        print("release-notes-gate: main() with a history read error: one clean failure line")
        # Claude r7 M-3: the notes OSError branch of main(), without chmod (also as root).
        def refuse_notes(self):
            if self == path:
                raise PermissionError(13, "Permission denied")
            return original(self)

        Path.read_bytes = refuse_notes
        try:
            code, output = run_main_on(path, history=hist)
        finally:
            Path.read_bytes = original
        line = "release-notes-gate: docs/RELEASE-NOTES.md cannot be read (PermissionError)"
        if code == 0 or output.strip().splitlines() != [line]:
            fail(f"main() with notes that raise PermissionError: expected the one line {line!r}; got rc={code}: {output!r}")
        print("release-notes-gate: main() with a notes read error: one clean failure line")
        # Claude r6 M-6: a non-UTF-8 notes file.
        path.write_bytes(top.rstrip("\n").encode("utf-8") + b"\nok \xff\n")
        try:
            code, output = run_main_on(path)
        except Exception as exc:  # noqa: BLE001 - the point of the test
            fail(f"main() with non-UTF-8 notes raised {type(exc).__name__}")
        line = "release-notes-gate: docs/RELEASE-NOTES.md line 6: expected UTF-8 text; found byte 0xff"
        if code == 0 or output.strip().splitlines() != [line]:
            fail(f"main() with non-UTF-8 notes: expected the one line {line!r}; got rc={code}: {output!r}")
        print("release-notes-gate: main() with non-UTF-8 notes: one clean failure line")


def flip_real(text, tag):
    lines = text.splitlines()
    if len(lines) < 3 or lines[1] != "":
        fail("docs/RELEASE-NOTES.md must keep an empty line 2 so the status stays on line 3")
    if lines[2] not in (RELEASED_LINE, UNRELEASED_LINE):
        fail("docs/RELEASE-NOTES.md line 3 must be the release-status line")
    lines[0] = f"# Googlebook Anywhere {tag}"
    lines[2] = RELEASED_LINE
    body = "\n".join(lines) + "\n"
    return body.replace("這一版還沒有發布。", "", 1)


def release_description_runtimes():
    """Every POSIX shell available here: /bin/sh, dash, bash --posix, busybox sh, mksh, ksh, yash, posh."""
    runtimes = [("sh", ["/bin/sh"])]
    found = {}
    for name in ("dash", "bash", "busybox", "mksh", "ksh", "yash", "posh"):
        found[name] = shutil.which(name)
    if found["dash"]:
        runtimes.append(("dash", [found["dash"]]))
    if found["bash"]:
        runtimes.append(("bash --posix", [found["bash"], "--posix"]))
    if found["busybox"]:
        runtimes.append(("busybox sh", [found["busybox"], "sh"]))
    for name in ("mksh", "ksh", "yash", "posh"):
        if found[name]:
            runtimes.append((name, [found[name]]))
    for name in ("dash", "bash", "busybox", "mksh", "ksh", "yash", "posh"):
        if not found[name]:
            print(f"SKIP release description {name} ({name} not installed)")
    return runtimes


def history_link(project, tag):
    return f"[Full release history]({project}/-/blob/{tag}/docs/RELEASE-HISTORY.md)"


def release_description(ci, notes, tag, project, shell, refuse=False):
    """Execute the actual parsed release script in a disposable checkout, never release-cli.

    Returns (problem, description bytes or None)."""
    with tempfile.TemporaryDirectory(prefix="gbos-release-description-") as tmp:
        directory = Path(tmp)
        ci_file = directory / ".gitlab-ci.yml"
        ci_file.write_text(ci, encoding="utf-8")
        job = _pyyaml.safe_load(ci_file.read_text(encoding="utf-8"))["release"]
        commands = job["script"]
        if not isinstance(commands, list) or not all(isinstance(cmd, str) for cmd in commands):
            return "release.script must be a list of shell strings", None
        source = directory / "docs" / "RELEASE-NOTES.md"
        source.parent.mkdir()
        source.write_text(notes, encoding="utf-8")
        env = os.environ.copy()
        env.update(CI_COMMIT_TAG=tag, CI_PROJECT_URL=project)
        proc = subprocess.run(
            [*shell, "-eu", "-c", "\n".join(commands)],
            cwd=directory, env=env, capture_output=True, text=True, timeout=10,
        )
        description = directory / job["release"]["description"]
        if refuse:
            if proc.returncode == 0 or description.exists():
                return "wrong tag was not refused before writing the description", None
            if "line 1 expected" not in proc.stdout + proc.stderr:
                return "wrong tag did not report the existing line-1 diagnostic", None
            return None, None
        if proc.returncode:
            return f"release script exited {proc.returncode}: {proc.stdout}{proc.stderr}", None
        if not description.is_file():
            return "release.description does not name the generated file", None
        if source.read_text(encoding="utf-8") != notes:
            return "release script changed the notes file", None
        return None, description.read_bytes()


def release_description_problem(ci, notes, tag, project, shell):
    """None when the description is the notes file verbatim, an empty line and the history link."""
    problem, got = release_description(ci, notes, tag, project, shell)
    if problem:
        return problem
    expected = notes + "\n" + history_link(project, tag) + "\n"
    if got != expected.encode("utf-8"):
        return "description differs from the notes file plus an empty line and the history link"
    return None


def expect_release_descriptions(ci, flipped, tag, history):
    """The release description is docs/RELEASE-NOTES.md verbatim plus the history link.

    The reviewers' leak shapes (fences in list continuations, lazy continuations,
    quoted and split headings) cannot pull another version's text: that text is not in
    the notes file, and nothing is cut. An older top-level heading (which the heading
    lint refuses) still cannot shorten the description."""
    if _pyyaml is None:
        print("SKIP parsed release description tests (PyYAML not installed)")
        return  # main already requires PyYAML when CI is set
    top = "# Googlebook Anywhere v9.9.9\n\n" + RELEASED_LINE + "\n\n本版內容。\n\n"
    shapes = (
        ("codex r2 I1 lazy quote continuation",
         "> Googlebook\nAnywhere\n> v0.2.2\n> ===\n\n本版其餘內容。\n"),
        ("codex r2 I2 list continuation fences",
         "- first item\n\n  ```\n  example\n## Googlebook Anywhere v0.2.2\n\n前一段文字。\n\n"
         "- second item\n\n  ```\n  example\n    ```\n"),
        ("claude r2 I-1 closer three columns deeper",
         "- 範例：\n\n  ```\n  make\n     ```\n\n## Googlebook Anywhere v0.2.2\n文字\n\n"
         "- 範例二：\n\n  ```\n  x\n     ```\n"),
        ("round 1 list fences", "- ```text\n  first container\n  ```\n## Googlebook Anywhere v0.2.2\n"),
        ("quoted split heading", "> Googlebook Anywhere\n> v0.2.2\n> ===\n"),
        ("split setext heading", "Googlebook Anywhere\nv0.2.2\n===\n"),
        ("inline diff sub-heading", "## Googlebook Anywhere {+v0.2.2+}\n"),
        ("deepseek 4-column open, column-0 close", "- item\n\n    ```\n    x\n```\n## Googlebook Anywhere v0.2.2\n"),
    )
    cases = [(f"real {tag} flipped", flipped, tag)]
    cases += [(label, top + shape, "v9.9.9") for label, shape in shapes]
    cases.append(("only section, no final newline", top.rstrip("\n"), "v9.9.9"))
    cut_case = ("older top-level heading (lint refuses; the job must not cut)",
                top + "# Googlebook Anywhere v9.9.8\n\n舊版內容。\n", "v9.9.9")
    cases.append(cut_case)
    release = extract_raw_blocks(ci)["release"]
    cat = "    - cat docs/RELEASE-NOTES.md > release-description.md\n"
    mutants = (
        ("cut at the next version heading", cat,
         "    - LC_ALL=C awk 'NR > 1 && /^# Googlebook Anywhere /{exit} {print}' docs/RELEASE-NOTES.md > release-description.md\n",
         [cut_case]),
        ("omit notes", cat, "    - true > release-description.md\n", cases[:1]),
        ("omit history link", "printf '\\n[Full release history]", "printf '\\n[Full history]", cases[:1]),
        ("history link to the notes file", "/docs/RELEASE-HISTORY.md)", "/docs/RELEASE-NOTES.md)", cases[:1]),
        ("no empty line before the link", "printf '\\n[Full", "printf '[Full", cases[:1]),
    )
    for runtime, shell in release_description_runtimes():
        for index, (label, notes, case_tag) in enumerate(cases):
            project = f"https://gitlab.example.invalid/group-{index}/project"
            problem = release_description_problem(ci, notes, case_tag, project, shell)
            if problem:
                fail(f"release description {runtime} {label}: {problem}")
            print(f"release-notes-gate: release description {runtime} {label}: notes file verbatim + history link")
        problem, _got = release_description(
            ci, top, "v0.0.0", "https://gitlab.example.invalid/wrong/tag", shell, refuse=True,
        )
        if problem:
            fail(f"release description {runtime}: {problem}")
        print(f"release-notes-gate: release description {runtime} wrong tag: refuse")
        for mutation, old, new, affected in mutants:
            if release.count(old) != 1:
                fail(f"cannot place release description mutation {mutation}")
            mutant = ci.replace(release, release.replace(old, new, 1), 1)
            for label, notes, case_tag in affected:
                project = "https://gitlab.example.invalid/mutation/project"
                problem = release_description_problem(mutant, notes, case_tag, project, shell)
                if problem is None or not problem.startswith("description differs"):
                    fail(f"release description mutant {mutation} {runtime} {label}: expected output mismatch, got {problem}")
                print(f"release-notes-gate: release description mutation {mutation} {runtime} {label}: red")


def synthetic_flipped(tag, snippet):
    """Synthetic released notes for tag with snippet in the body; never the real notes."""
    return (
        f"# Googlebook Anywhere {tag}\n\n{RELEASED_LINE}\n\n本版內容。\n\n"
        + snippet + "\n\n## 驗證紀錄\n\n- 測試項目。\n"
    )


def planted(phrase):
    return with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        + phrase
        + "\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )


def load_0394514_ci(current_ci):
    if NEW_PACKAGE_GATE_LINES not in current_ci:
        fail("live .gitlab-ci.yml does not contain NEW_PACKAGE_GATE_LINES")
    return current_ci.replace(NEW_PACKAGE_GATE_LINES, CI_0394514_PACKAGE_GATE_LINES, 1)


GATE11_REFUSE_CASES = {
    "c02_list": "- 這一版還沒\n  有發布。",
    "c03_quote": "> 這一版還沒\n> 有發布。",
    "c08_trail": "這一版還沒 \n有發布。",
    "c04_link": "這一版還沒有[發布](https://x.invalid/s)。",
    "c12_ol": "1. 這一版還沒\n   有發布。",
    "e03_quote": "> This version is not yet\n> released.",
    "e04_link": "not yet [released](https://x.invalid)",
    "e05_link": "[not yet](https://x.invalid) released",
    "e06_ref": "not [yet][s] released\n\n[s]: https://x.invalid",
    "e07_br": "not yet<br>released",
    "codex_link": "This version is not [yet](https://example.invalid/status) released.",
    "codex_cjk_link": "這一版未[發布](https://example.invalid/status)。",
    "codex_em_ml": 'This version is un<em\nclass="status">r</em>eleased.',
    "codex_attr": 'un<span title=">">r</span>eleased',
    "e13_noscolon": "<div>\nThis version is un&#114eleased.\n</div>",
    "c10_noscolon": "<div>\n這一版&#26410發布。\n</div>",
    "softbreak": "This version is not yet\nreleased.",
    "entity": "un&#114;eleased",
    "dblspace": "not  yet released",
    "markup": "un**r**eleased",
    "cjk_tag": "未<b></b>發布",
    "code": "`unreleased`",
    "hardwrap": "這一版還沒\n有發布。",
    "named": "a &amp; b",
    "r9_codex_paren_url": "This version is not [yet](https://example.invalid/wiki/Release_(software)/status) released.",
    "r9_codex_title": 'This version is not [yet](https://example.invalid/status "Release (current) notes") released.',
    "r9_codex_cjk_paren": "這一版[未](https://example.invalid/wiki/Release_(software)/status)發布。",
    "r9_codex_comment_gt": "<div>\nThis version is not <!-- status > check -->yet released.\n</div>",
    "r9_claude_lone_lt": "a < b and not yet\nreleased here > c",
    "r9_claude_lone_lt_cjk": "a < b 這一版還沒\n有發布 > c",
    "r9_multiline_link": "not [yet](https://example.invalid\n) released",
    "r9_autolink": "see <https://example.invalid> not yet released",
    "r9_quote_list": "> - 這一版還沒\n>   有發布。",
    "r9_hyphen": "un-released build",
    "r10_claude_hyphen": "This is not-yet-released.",
    "r10_underscore": "not_yet_released build",
    "r10_slash": "not/yet/released",
    "r10_codex_escaped_ref": "This version is not [yet][status\\]current] released.\n\n[status\\]current]: https://example.invalid/status",
    "r10_codex_escaped_ref_cjk": "這一版[未][狀態\\]現行]發布。\n\n[狀態\\]現行]: https://example.invalid/status",
    "r11_n_paren_target": "這一版還沒[](docs/x\\)y)有發布。",
    "r11_n_paren_target_en": "This version is not yet [](docs/x\\)y)released.",
    "r11_n_tick_target_comment": "This version is not [yet](u`) <!-- x` --> released.",
    "r11_n_comment_split": "This version is not yet<!-- note -->\nreleased.",
    "r11_n_img_cjk_alt": "這一版還沒![](x.png)有發布。",
    "r11_n_img_latin": "This version is not ![yet](x.png) released.",
    "r11_n_idiff_latin": "{+not yet+} released",
    "r11_n_idiff_cjk_sq": "這一版還沒[+有+]發布。",
    "r11_n_idiff_del": "un{-r-}eleased",
    "r11_n_math": "un$`r`$eleased",
    "r11_codex_ref_wrap": "This version is not [yet][release\nstatus] released.\n\n[release status]: https://example.invalid/status",
    "r11_codex_ref_wrap_cjk": "這一版[未][release\nstatus]發布。\n\n[release status]: https://example.invalid/status",
    "r11_codex_ref_escape": "This version is not [yet][release\\_status] released.\n\n[release\\_status]: https://example.invalid/status",
    "r11_codex_ref_escape_cjk": "這一版[未][release\\_status]發布。\n\n[release\\_status]: https://example.invalid/status",
    "r11_ref_definition_only": "[status]: https://example.invalid/status",
    "r10_strong_cjk": "這一版未**發布**。",
    "r10_quote_wrap": "> This version is not yet\n> released.",
    "r10_claude_multiline_ref_cjk": "- [實機測試][這一版還沒\n  有發布]",
    "r10_claude_multiline_ref": "See [HX 370][not yet\nreleased] for details.",
}
GATE10_REFUSE_CASES = GATE11_REFUSE_CASES

GATE11_PASS_CASES = {
    "r11_plain_bang_plus": "Wow! 2+2 is 4; {x} costs $5.",
    "r11_image_ok": "![KANO](docs/images/kano-welcome.png)",
    "r11_code_span_brackets": "- 用 `a[i][j]` 取值。",
    "p04_para": "功能 A 未\n\n發布流程改了。",
    "p07_para": "Some items are not yet\n\nReleased builds are signed.",
    "r9_run_released": "Run released scripts from this directory.",
    "r9_run_published": "We run published tests before packaging.",
    "r9_lone_lt_ok": "a < b and b > c are fine",
    "r9_adjacent_items": "- 功能 A 未\n- 發布流程改了。",
    "r9_table": "| 功能 | 狀態 |\n|---|---|\n| A 未 | x |\n| 發布 | y |",
    "r9_simple_link": "See [the docs](https://example.invalid/docs) for details.",
    "p03_items": "- Some items are not yet\n- Released builds are signed.",
    "f1": "Hardware validation on the Spin 714: not yet. Released images are QEMU-tested only.",
    "f2": "Is the HX 370 image signed? Not yet; released images will be.",
    "f3": "> - 功能 A 未\n> - 發布流程改了。",
    "f4": "> - Hardware test: not yet\n> - Released builds are signed.",
    "f13": "> - 實機還未\n> 1. 發布方式改用 GitLab release",
    "f5": "實機驗證：未\n---\n發布方式改成 GitLab release。",
    "f14": "實機驗證：未\n  ## 發布",
    "f11": "- 實機：未; 發布：GitLab release",
    "f8": "- `install/gbos-report.sh --out <dir>` 把報告寫到 `<dir>`。",
    "f10": "- Profiles live in `profiles/<model>/profile.toml`.",
    "r10_codex_quote_para": "> Some items are not yet\n>\n> Released builds are signed.",
    "r10_quote_list": "> - Some items are not yet\n> - Released builds are signed.",
    "f12": "- Spin 714 的音訊要等待 SOF v2.13 發布。",
    "f6": "功能 | 狀態\n--- | ---\nA 未 | x\n發布 | y",
    "f7": "| 功能 | 狀態 | 備註 |\n|---|---|---|\n| HX 370 | 未 | 發布前只做 QEMU |",
}
GATE10_PASS_CASES = GATE11_PASS_CASES


def main(notes=NOTES, notes_only=False, history=HISTORY):
    """Run the gate. notes, notes_only and history let a self-test drive this real entry on temp files."""
    if not CI.is_file() or not Path(notes).is_file():
        fail("missing .gitlab-ci.yml or docs/RELEASE-NOTES.md")
    notes = Path(notes)
    # Claude r6 M-6: an unreadable or non-UTF-8 notes file is one clean failure line.
    try:
        data = notes.read_bytes()
        data.decode("utf-8")
    except OSError as exc:
        fail(f"docs/RELEASE-NOTES.md cannot be read ({type(exc).__name__})")
    except UnicodeDecodeError as exc:
        bad_line = data[:exc.start].count(b"\n") + 1
        fail(f"docs/RELEASE-NOTES.md line {bad_line}: expected UTF-8 text; "
             f"found byte 0x{data[exc.start]:02x}")
    real = notes.read_text(encoding="utf-8")
    tag = current_tag(real)
    if tag is None:
        fail("docs/RELEASE-NOTES.md line 1 does not name a version")
    h_prob = notes_heading_problem(notes)
    if h_prob is not None:
        fail(h_prob)
    hist_prob = history_problem(history)
    if hist_prob is not None:
        fail(hist_prob)
    if notes_only:
        return
    ci = CI.read_text(encoding="utf-8")
    command = gate_command(ci)
    print("release-notes-gate: gate under package.script before make-release.sh: yes")
    expect_ci(ci, "live ci", False)
    if plain_scalar_problem(ci) is not None:
        fail(f"live ci failed plain_scalar_problem: {plain_scalar_problem(ci)}")
    print("release-notes-gate: live ci plain-scalar lint: pass")
    if _pyyaml is None:
        if os.environ.get("CI"):
            fail("PyYAML (python3-yaml) is required when CI is set")
        print("SKIP PyYAML safe_load (python3-yaml not installed)")
    else:
        y_prob = pyyaml_problem(ci, expected_gate=command)
        if y_prob is not None:
            fail(f"live ci failed pyyaml_problem: {y_prob}")
        print("release-notes-gate: live ci PyYAML safe_load + package.script[0] match: pass")
    ci_0394514 = load_0394514_ci(ci)
    ps_0394514 = plain_scalar_problem(ci_0394514)
    if ps_0394514 is None or "plain scalar contains ': '" not in ps_0394514:
        fail(f"0394514 .gitlab-ci.yml was not refused by plain_scalar_problem: {ps_0394514}")
    print(f"release-notes-gate: 0394514 .gitlab-ci.yml plain-scalar lint: red ({ps_0394514})")
    if _pyyaml is not None:
        yp_0394514 = pyyaml_problem(ci_0394514)
        if yp_0394514 is None or "PyYAML safe_load failed" not in yp_0394514:
            fail(f"0394514 .gitlab-ci.yml was not refused by PyYAML safe_load: {yp_0394514}")
        first_yp = yp_0394514.splitlines()[0]
        print(f"release-notes-gate: 0394514 .gitlab-ci.yml PyYAML safe_load: red ({first_yp})")
    verbose_cont_ci = ci.replace(
        "- bash tests/report/report-test.sh",
        "- bash tests/report/report-test.sh\n      --verbose: yes",
        1,
    )
    ps_cont = plain_scalar_problem(verbose_cont_ci)
    if ps_cont is None or "plain scalar contains ': '" not in ps_cont:
        fail(f"plain-scalar continuation --verbose: yes was not refused by plain_scalar_problem: {ps_cont}")
    expect_ci(verbose_cont_ci, "claude R9-M2 plain scalar continuation --verbose: yes", True)
    expect_ci(
        ci.replace("expire_in: 30 days", "expire_in: 30 days # inline comment", 1),
        "plain scalar with space-hash",
        True,
    )
    expect_ci(
        ci.replace("expire_in: 30 days", "expire_in: 30 days:", 1),
        "plain scalar ending with colon",
        True,
    )
    expect_ci(delete_gate(ci), "gate deleted", True)
    expect_ci(weaken_gate(ci), "gate weakened", True)
    expect_ci(move_gate_to_after_script(ci), "after_script", True)
    expect_ci(insert_under(ci, "package", "  allow_failure: true"), "allow_failure", True)
    expect_ci(
        insert_under(ci, "package", "  allow_failure:\n    exit_codes: 1"),
        "allow_failure exit_codes scalar",
        True,
    )
    expect_ci(
        insert_under(ci, "package", "  allow_failure:\n    exit_codes: [1]"),
        "allow_failure exit_codes list",
        True,
    )
    expect_ci(
        insert_under(ci, "package", "  allow_failure: {exit_codes: [1]}"),
        "allow_failure exit_codes flow list",
        True,
    )
    expect_ci(
        insert_under(ci, "package", "  allow_failure: {exit_codes: 1}"),
        "allow_failure exit_codes flow scalar",
        True,
    )
    expect_ci(
        insert_under(ci, "package", '  "allow_failure": true'),
        "quoted allow_failure",
        True,
    )
    expect_ci(add_allow_failure_alias(ci), "allow_failure alias", True)
    expect_ci(
        insert_under(ci, "package", "  allow_failure: !!bool true"),
        "allow_failure bool tag",
        True,
    )
    expect_ci(insert_package_rules_allow_failure(ci), "package rules allow_failure", True)
    expect_ci(insert_anchor_allow_failure(ci), "anchor allow_failure", True)
    expect_ci(add_extends_allow_failure(ci), "extends allow_failure", True)
    expect_ci(add_default_allow_failure(ci), "default allow_failure", True)
    expect_ci(add_release_when_anchor(ci), "merged when always", True)
    expect_ci(insert_release_rules_when_always(ci), "release rules when always", True)
    expect_ci(
        insert_under(ci, "release", '  "when": always'),
        "quoted release when",
        True,
    )
    include_problem = ci_problem(add_include(ci))
    if include_problem is None or "include:" not in include_problem or "cannot be resolved" not in include_problem:
        fail(f"mutation include was not refused clearly: {include_problem}")
    print(f"release-notes-gate: mutation include: red ({include_problem})")
    expect_ci(insert_under(ci, "package", "  include: extra.yml"), "nested include", True)
    expect_ci(make_needs_comment(ci), "needs comment", True)
    expect_ci(make_optional_caps(ci), "optional True", True)
    expect_ci(insert_under(ci, "release", "  when: always"), "when always", True)
    expect_ci(make_needs_optional(ci), "needs optional", True)
    expect_ci(make_needs_flow_map_optional(ci), "needs flow map optional", True)
    expect_ci(make_needs_block_flow_map_optional(ci), "needs block flow map optional", True)
    expect_ci(make_needs_mapping_item(ci), "needs mapping item", True)
    expect_ci(make_needs_extra_job(ci), "needs extra job", True)
    # Codex R7-M1 four YAML mutants
    expect_ci(
        insert_package_rules_line(ci, '      "allow_failure": true'),
        "codex package rules quoted allow_failure",
        True,
    )
    expect_ci(
        insert_release_rules_line(ci, '      "when": always'),
        "codex release rules quoted when always",
        True,
    )
    expect_ci(
        insert_release_rules_line(
            insert_package_rules_line(ci, '      "allow_failure": true'),
            '      "when": always',
        ),
        "codex rules quoted allow_failure and when always",
        True,
    )
    expect_ci(add_package_rule_merge(ci), "codex package rule merge", True)
    # Claude R7-M4 survivors n1-n5, n7, plus n6 (red via pinned block check)
    expect_ci(
        insert_under(ci, "package", "  ? allow_failure\n  : true"),
        "claude n1 package complex key allow_failure",
        True,
    )
    expect_ci(
        insert_release_rules_line(ci, "      needs: []"),
        "claude n2 release rules needs empty",
        True,
    )
    expect_ci(
        add_other_job_with_release_key(ci),
        "claude n3 other job with release key",
        True,
    )
    expect_ci(
        add_other_job_with_release_cli(ci),
        "other job with release-cli",
        True,
    )
    expect_ci(
        add_other_job_with_glab_release(ci),
        "other job with glab release",
        True,
    )
    expect_ci(
        ci.replace("description: ./release-description.md", "description: ./docs/RELEASE-NOTES.en.md", 1),
        "claude n4 release description changed",
        True,
    )
    expect_ci(
        insert_under(ci, "package", "  when: manual"),
        "claude n5 package when manual",
        True,
    )
    expect_ci(
        insert_release_rules_line(ci, "      needs: [lint]"),
        "claude n7 release rules needs lint",
        True,
    )
    expect_ci(
        insert_package_rules_line(ci, "      when: manual"),
        "claude n6 package rules when manual (pinned)",
        True,
        pin_only=True,
    )
    # Round-8 tracker / pin hardening (Claude y1-y4, y6 + Codex R8-M1)
    expect_ci(
        add_other_job_extends_release(ci),
        "claude y1 other job extends release",
        True,
    )
    expect_ci(
        add_other_job_extends_package(ci),
        "other job extends package",
        True,
    )
    expect_ci(
        add_flow_mapping_release_job(ci),
        "claude y2 flow-mapping job with release",
        True,
    )
    expect_ci(
        add_other_job_with_releases_api(ci),
        "claude y3 other job with /releases API",
        True,
    )
    expect_ci(
        add_col0_comment_duplicate_package_script(ci),
        "claude y4 col-0 comment + duplicate package script",
        True,
    )
    expect_ci(
        add_gate_or_true(ci),
        "claude y4 gate command with || true suffix",
        True,
    )
    expect_ci(
        add_col0_comment_duplicate_deps_before_script(ci),
        "claude y6 col-0 comment + duplicate .deps before_script",
        True,
    )
    expect_ci(
        add_codex_col0_comment_before_script(ci),
        "codex R8-M1 col-0 comment before_script override (pinned)",
        True,
        pin_only=True,
    )
    # Round-9 tracker hardening (Claude M1, Codex R9-M2)
    expect_ci(
        add_other_job_flow_extends_release(ci),
        "claude R9-M1 / codex R9-M2 other job extends [release]",
        True,
    )
    expect_ci(
        add_other_job_flow_extends_package(ci),
        "codex R9-M2 other job extends [.deps, package]",
        True,
    )
    expect_ci(
        add_next_line_flow_mapping_job(ci),
        "claude R9-M1 next-line flow mapping job",
        True,
    )
    # Round-10 tracker indentation hardening (Codex R10-M2, Claude M2)
    for indent_label, indent_mutant in (
        ("claude R10-M2 4-space extends: release + needs: []", add_other_job_4space_extends_release(ci)),
        ("claude R10-M2 1-space extends: release + needs: []", add_other_job_1space_extends_release(ci)),
        ("codex R10-M2 4-space extends: [release]", add_other_job_4space_flow_extends_release(ci)),
        ("codex R10-M2 4-space extends: [.deps, package]", add_other_job_4space_flow_extends_package(ci)),
    ):
        t_indent = tracker_problem(indent_mutant)
        if t_indent is None or "CI jobs use 2-space keys" not in t_indent:
            fail(f"{indent_label} did not fail with 'CI jobs use 2-space keys': {t_indent}")
        expect_ci(indent_mutant, indent_label, True)
    expect_tracker_override(extends_allow_failure_overridden(ci), "extends allow_failure overridden")
    expect_tracker_override(default_allow_failure_overridden(ci), "default allow_failure overridden")
    expect_tracker_override(insert_under(ci, "release", "  when: on_success"), "when on_success")
    expect_tracker_override(
        ci.replace("needs: [package]", "needs:\n    - package", 1),
        "needs block list package",
    )
    expect_ci(move_release_job_above_package(ci), "job order ignored", False)

    notes_lint_checks(real, tag)
    round4_checks(real, HISTORY.read_text(encoding="utf-8"))
    round5_checks(real)
    history_unpublished_check(Path(history))
    print("release-notes-gate: docs/RELEASE-HISTORY.md lines 2..N have no unpublished wording: yes")
    rc, _output = run_gate(command, real, tag)
    print(f"release-notes-gate: docs/RELEASE-NOTES.md today: {'refuse' if rc else 'pass'} (informational)")
    if real.splitlines()[2] == UNRELEASED_LINE and rc == 0:
        fail("today's unreleased notes were accepted")
    flipped = flip_real(real, tag)
    if flipped.splitlines()[2] != RELEASED_LINE:
        fail("flipped notes did not put the released status on line 3")
    _unpublished_sentence = "這一版還沒有發布。"
    if _unpublished_sentence not in real:
        if flipped.splitlines()[3:] != real.splitlines()[3:]:
            fail("flipped notes changed a line other than lines 1 and 3")
    else:
        _real_tail = "\n".join(real.splitlines()[3:]).replace(_unpublished_sentence, "", 1)
        _flipped_tail = "\n".join(flipped.splitlines()[3:])
        if _flipped_tail != _real_tail:
            fail("flipped notes changed more than lines 1 and 3 and the removed sentence")
    # The flipped notes are run through the real gate below; any unpublished wording left in them fails there.
    expect_release_descriptions(ci, flipped, tag, history)

    unreleased_status = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + UNRELEASED_LINE
        + "\n"
        "這一版已經寫好。這段是測試用的範本。\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    missing_status = (
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        "這一版已經寫好。這段是測試用的範本。\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
        "\n"
        "# Googlebook Anywhere v9.9.8\n"
        "\n"
        "上一版已經發布。\n"
    )
    assert_no_unpublished(missing_status, "missing released status")
    released_unreleased = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "UNRELEASED\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    released_mixed = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "Unreleased\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    released_cjk = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "這一版還沒發布。\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    early_sub = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "## Unreleased\n"
        "\n"
        "這一版還沒有發布。\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    if early_sub.find("## Unreleased") > early_sub.find("還沒有發布"):
        fail("early sub-heading fixture does not put ## before the unpublished sentence")
    first_sub = with_older(
        "## Unreleased\n"
        "\n"
        + UNRELEASED_LINE
        + "\n"
        "這一版還沒有發布。\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    nonempty_line2 = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        " \n"
        + RELEASED_LINE
        + "\n"
        "這一版已經寫好。\n"
    )
    title_only = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + UNRELEASED_LINE
        + "\n"
        "這一版還沒有發布。這段是測試用的範本。\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    if "未發布" in title_only.splitlines()[0]:
        fail("title-only fixture still has unpublished wording in the title")
    inexact = (
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        "<!-- release-status: Released -->\n"
        "\n"
        "這一版已經寫好。這段是測試用的範本。\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    assert_no_unpublished(inexact, "inexact status")
    released_clean = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "這一版已經寫好。這段是測試用的範本。\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    clean_lines = released_clean.splitlines()
    if clean_lines[1] != "" or clean_lines[2] != RELEASED_LINE:
        fail("clean fixture status is not on line 3")
    boundary = released_clean.index("\n# Googlebook Anywhere v9.9.8\n")
    current = released_clean[:boundary]
    if RELEASED_LINE not in current.splitlines()[:3]:
        fail("clean fixture has no released status on line 3")
    assert_no_unpublished(released_clean, "released clean")
    fenced = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "## 文件範例\n"
        "\n"
        "```markdown\n"
        "# Googlebook Anywhere v9.9.8\n"
        "```\n"
        "\n"
        "## Unreleased\n"
        "\n"
        "這一版還沒有發布。\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    tilde = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "~~~text\n"
        "# Googlebook Anywhere v0.1.0\n"
        "~~~\n"
        "\n"
        "unpublished\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    fenced_wording = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "範例裡的字一樣要擋。\n"
        "\n"
        "```markdown\n"
        "# Googlebook Anywhere v9.9.8\n"
        "unreleased\n"
        "```\n"
        "\n"
        "~~~text\n"
        "Not Yet Released\n"
        "~~~\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
    )
    backtick_fenced_wording = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "```text\n"
        "這一版還沒有發布。\n"
        "```\n"
    )
    tilde_fenced_wording = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "~~~text\n"
        "UNRELEASED\n"
        "~~~\n"
    )
    older_section_wording = (
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "這一版已經寫好。\n"
        "\n"
        + OLDER_WITH_WORDING
    )
    indented_code_wording = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "    這一版還沒有發布。\n"
    )
    false_fence_codex = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "```markdown`example\n"
        "\n"
        "這一版還沒有發布。\n"
        "\n"
        "```\n"
        "```markdown`example\n"
        "```\n"
        "\n"
        "## 新工具\n"
    )
    false_fence_claude14 = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "```gbos-update.py --check``` 會先檢查。\n"
        "\n"
        "這一版還沒有發布。\n"
        "\n"
        "```sh\n"
        "echo ok\n"
        "```\n"
    )
    false_fence_claude19 = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "<!--\n"
        "```\n"
        "-->\n"
        "\n"
        "這一版還沒有發布。\n"
        "\n"
        "```\n"
    )
    status_in_fence = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        "```markdown\n"
        + RELEASED_LINE
        + "\n"
        "```\n"
    )
    second_section_above = (
        "# Googlebook Anywhere v9.9.10\n"
        "\n"
        "下一版草稿。\n"
        "\n"
        + released_clean
    )
    elsewhere = (
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        "這一版已經寫好。這段是測試用的範本。\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "\n"
        "# Googlebook Anywhere v9.9.8\n"
        "\n"
        "上一版已經發布。\n"
    )
    assert_no_unpublished(elsewhere, "released line elsewhere")
    old_released = (
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        "這一版已經寫好。這段是測試用的範本。\n"
        "\n"
        "## 變更\n"
        "\n"
        "- 範本項目\n"
        "\n"
        "# Googlebook Anywhere v9.9.8\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "上一版已經發布。\n"
    )
    assert_no_unpublished(old_released.split("# Googlebook Anywhere v9.9.8")[0], "old-version current section")
    repeated = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        "UNRELEASED\n"
    )
    policy = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "# Googlebook Anywhere version policy\n"
        "\n"
        "這一版還沒有發布。\n"
    )
    suffix = with_older(
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        "# Googlebook Anywhere v9.9.8 摘要\n"
        "\n"
        "未发布\n"
    )
    bom = "\ufeff" + released_clean
    crlf = released_clean.replace("\n", "\r\n")
    seg_msg = "expected no unpublished wording (after removing link targets, markup and line breaks)"
    raw_html_msg = "expected no raw HTML (Markdown only; line 3 is the status comment)"
    link_target_msg = "expected a link target without spaces, quotes, parentheses or a title, on one line"
    escaped_bracket_msg = "expected no escaped bracket or parenthesis"
    codex_softbreak = planted("This version is not yet\nreleased.")
    codex_entity = planted("This version is un&#114;eleased.")
    named_entity = planted("This version has &amp; in it.")
    claude_18_hardwrap = planted("這一版還沒\n有發布。")
    double_space = planted("This version is not  yet released.")
    inline_bold = planted("un**r**eleased")
    inline_html = planted("未<b></b>發布")
    inline_code = planted("`unreleased`")

    status_needles = ("line 3 expected", RELEASED_LINE)
    expect(command, "unreleased status", unreleased_status, True, needles=status_needles)
    expect(command, "missing released status", missing_status, True, needles=status_needles)
    expect(command, "released + UNRELEASED", released_unreleased, True, needles=("expected no unpublished wording",))
    expect(command, "released + Unreleased", released_mixed, True, needles=("expected no unpublished wording",))
    expect(command, "released + 還沒發布", released_cjk, True, needles=("expected no unpublished wording",))
    expect(command, "released + early sub-heading", early_sub, True)
    expect(command, "sub-heading first line", first_sub, True, needles=("line 1 expected",))
    expect(command, "non-empty line 2", nonempty_line2, True, needles=("line 2 expected empty",))
    expect(command, "title only", title_only, True)
    expect(command, "inexact status", inexact, True, needles=status_needles)
    expect(command, "status on line 3 released + clean", released_clean, False)
    expect_all_refuse(
        command,
        (
            ("false-fence backtick in info string", false_fence_codex, ("expected no unpublished wording",)),
            ("false-fence inline code at line start", false_fence_claude14, ("expected no unpublished wording",)),
            ("false-fence in html comment", false_fence_claude19, ("expected no unpublished wording",)),
            (
                "codex softbreak (This version is not yet\\nreleased.)",
                codex_softbreak,
                ("lines 3-5 " + seg_msg,),
            ),
            ("codex entity (un&#114;eleased)", codex_entity, ("line 4 expected no character reference",)),
            ("named entity (&amp;)", named_entity, ("line 4 expected no character reference",)),
            (
                "claude 18 hardwrap (這一版還沒\\n有發布。)",
                claude_18_hardwrap,
                ("lines 3-5 " + seg_msg,),
            ),
            (
                "double space (not  yet released)",
                double_space,
                ("lines 3-4 " + seg_msg,),
            ),
            (
                "inline markup (un**r**eleased)",
                inline_bold,
                ("lines 3-4 " + seg_msg,),
            ),
            (
                "inline markup (未<b></b>發布)",
                inline_html,
                ("line 4 " + raw_html_msg,),
            ),
            (
                "inline markup (`unreleased`)",
                inline_code,
                ("line 4 expected no unpublished wording", "lines 3-4 " + seg_msg),
            ),
        ),
    )
    # gate11 cases (inputs/gate11/cases.json) plus the round-11 reference-link cases (gate12), planted and in the flipped real notes
    gate11_planted_refuse = []
    gate11_flipped_refuse = []
    for case_name, snippet in GATE11_REFUSE_CASES.items():
        if case_name in ("e13_noscolon", "c10_noscolon"):
            needles = (raw_html_msg, "expected no character reference")
        elif case_name in ("entity", "named"):
            needles = ("expected no character reference",)
        elif case_name in ("e07_br", "codex_em_ml", "codex_attr", "r9_codex_comment_gt", "cjk_tag"):
            needles = (raw_html_msg,)
        elif case_name == "r9_autolink":
            needles = (raw_html_msg, "expected no unpublished wording", seg_msg)
        elif case_name in ("r9_codex_paren_url", "r9_codex_title", "r9_codex_cjk_paren", "r9_multiline_link"):
            needles = (link_target_msg,)
        elif case_name in ("r10_codex_escaped_ref", "r10_codex_escaped_ref_cjk"):
            needles = (escaped_bracket_msg,)
        elif case_name.startswith("r11_codex_ref") or case_name == "r11_ref_definition_only":
            needles = ("expected no reference-style link or definition",)
        elif case_name.startswith("r11_n_paren_target"):
            needles = (escaped_bracket_msg,)
        elif case_name == "r11_n_tick_target_comment":
            needles = (link_target_msg, "expected no HTML comment")
        elif case_name == "r11_n_comment_split":
            needles = ("expected no HTML comment",)
        elif case_name == "code":
            needles = ("expected no unpublished wording", seg_msg)
        else:
            needles = (seg_msg,)
        gate11_planted_refuse.append((f"gate11 {case_name} (planted)", planted(snippet), needles))
        gate11_flipped_refuse.append(
            (
                f"gate11 {case_name} (flipped)",
                synthetic_flipped(tag, snippet),
                needles,
            )
        )
    expect_all_refuse(command, gate11_planted_refuse)
    expect_all_refuse(command, gate11_flipped_refuse, tag=tag)
    for case_name, snippet in GATE11_PASS_CASES.items():
        expect(command, f"gate11 {case_name} (planted)", planted(snippet), False)
        expect(
            command,
            f"gate11 {case_name} (flipped)",
            synthetic_flipped(tag, snippet),
            False,
            tag=tag,
        )
    expect(command, "fenced-heading", fenced, True, needles=("expected no unpublished wording",))
    expect(command, "tilde-fence heading", tilde, True, needles=("expected no unpublished wording",))
    expect(command, "fenced wording refused", fenced_wording, True, needles=("expected no unpublished wording",))
    expect(command, "backtick-fenced wording", backtick_fenced_wording, True, needles=("expected no unpublished wording",))
    expect(command, "tilde-fenced wording", tilde_fenced_wording, True, needles=("expected no unpublished wording",))
    expect(command, "older-version section wording", older_section_wording, True, needles=("expected no unpublished wording",))
    expect(command, "indented code block wording", indented_code_wording, True, needles=("expected no unpublished wording",))
    expect(command, "status in fence", status_in_fence, True, needles=status_needles)
    expect(command, "second version section above", second_section_above, True, needles=("line 1 expected",))
    expect(command, "released line elsewhere", elsewhere, True, needles=status_needles)
    expect(command, "old-version released line", old_released, True, needles=status_needles)
    expect(command, "repeated current heading", repeated, True, needles=("expected no unpublished wording",))
    expect(command, "version policy heading", policy, True, needles=("expected no unpublished wording",))
    expect(command, "heading with extra text", suffix, True, needles=("expected no unpublished wording",))
    expect(command, "bom line 1", bom, True, needles=("line 1 expected",))
    expect(command, "crlf line endings", crlf, True, needles=("line 1 expected",))
    expect(
        command,
        "missing file",
        None,
        True,
        needles=(
            "line 1 expected '# Googlebook Anywhere v9.9.9'",
            "line 2 expected empty",
            "line 3 expected '<!-- release-status:  released -->'",
        ),
    )
    for wording in WORDINGS:
        if wording == "tolower":
            continue
        sample = wording if not wording.isascii() else wording.upper()
        expect(command, f"wording {wording}", planted(sample), True, needles=("expected no unpublished wording",))
    expect(command, "flipped real notes", flipped, False, tag=tag)
    # Wording deep in a long file: a gate that stops scanning after some line must turn red.
    expect(
        command,
        "flipped real notes + wording on the last line",
        flipped + "這一版還沒有發布。\n",
        True,
        tag=tag,
        needles=(f"line {len(flipped.splitlines()) + 1} expected no unpublished wording",),
    )
    long_tail = (
        "# Googlebook Anywhere v9.9.9\n"
        "\n"
        + RELEASED_LINE
        + "\n"
        + "".join(f"- 範本項目 {n}\n" for n in range(5000))
        + "not yet released\n"
    )
    expect(command, "wording after 5000 lines", long_tail, True, needles=("line 5004 expected no unpublished wording",))
    expect_fence_mutant(ci, backtick_fenced_wording)
    expect_gate_ci_mutant(
        ci,
        'if (seg == "") return;',
        "if (1) return;",
        "segment check never true",
        (
            ("codex softbreak", codex_softbreak),
            ("claude 18 hardwrap", claude_18_hardwrap),
            ("c02_list", planted(GATE11_REFUSE_CASES["c02_list"])),
            ("inline bold", inline_bold),
            ("r9_claude_lone_lt", planted(GATE11_REFUSE_CASES["r9_claude_lone_lt"])),
            ("r9_quote_list", planted(GATE11_REFUSE_CASES["r9_quote_list"])),
            ("r9_hyphen", planted(GATE11_REFUSE_CASES["r9_hyphen"])),
            ("r10_claude_hyphen", planted(GATE11_REFUSE_CASES["r10_claude_hyphen"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'gsub(/[][*_~`\\>-]/, "", a); ',
        "",
        "no Latin view-a markup deletion",
        (
            ("inline bold", inline_bold),
            ("r9_hyphen", planted(GATE11_REFUSE_CASES["r9_hyphen"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'gsub(/[][*_~`\\>-]/, "", a); gsub(/[ \t\r\n]+/, " ", a); b = t; gsub(/[][*_~`\\>\/-]/, " ", b); ',
        r'gsub(/[ \t\r\n]+/, " ", a); b = t; ',
        "no ASCII punctuation removal",
        (
            ("inline bold", inline_bold),
            ("r9_hyphen", planted(GATE11_REFUSE_CASES["r9_hyphen"])),
            ("e03_quote", planted(GATE11_REFUSE_CASES["e03_quote"])),
            ("e04_link", planted(GATE11_REFUSE_CASES["e04_link"])),
            ("codex_link", planted(GATE11_REFUSE_CASES["codex_link"])),
            ("r10_claude_hyphen", planted(GATE11_REFUSE_CASES["r10_claude_hyphen"])),
            ("r10_underscore", planted(GATE11_REFUSE_CASES["r10_underscore"])),
            ("r10_slash", planted(GATE11_REFUSE_CASES["r10_slash"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'b = t; gsub(/[][*_~`\\>\/-]/, " ", b); gsub(/[ \t\r\n]+/, " ", b); ',
        "",
        "drop second Latin view (b)",
        (
            ("r10_claude_hyphen", planted(GATE11_REFUSE_CASES["r10_claude_hyphen"])),
            ("r10_underscore", planted(GATE11_REFUSE_CASES["r10_underscore"])),
            ("r10_slash", planted(GATE11_REFUSE_CASES["r10_slash"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'gsub(/[ \t\r\n]+/, " ", a); b = t; gsub(/[][*_~`\\>\/-]/, " ", b); gsub(/[ \t\r\n]+/, " ", b); ',
        r'b = t; gsub(/[][*_~`\\>\/-]/, " ", b); ',
        "whitespace not collapsed (keep \\n)",
        (
            ("codex softbreak", codex_softbreak),
            ("double space", double_space),
            ("r9_claude_lone_lt", planted(GATE11_REFUSE_CASES["r9_claude_lone_lt"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'gsub(/[][*_~`\\> \t\r\n-]/, "", c); ',
        "",
        "no CJK markup/newline removal",
        (
            ("claude 18 hardwrap", claude_18_hardwrap),
            ("c02_list", planted(GATE11_REFUSE_CASES["c02_list"])),
            ("c03_quote", planted(GATE11_REFUSE_CASES["c03_quote"])),
            ("c04_link", planted(GATE11_REFUSE_CASES["c04_link"])),
            ("codex_cjk_link", planted(GATE11_REFUSE_CASES["codex_cjk_link"])),
            ("r9_claude_lone_lt_cjk", planted(GATE11_REFUSE_CASES["r9_claude_lone_lt_cjk"])),
            ("r9_quote_list", planted(GATE11_REFUSE_CASES["r9_quote_list"])),
            ("r10_strong_cjk", planted(GATE11_REFUSE_CASES["r10_strong_cjk"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'gsub(/[][*_~`\\> \t\r\n-]/, "", c);',
        r'gsub(/[ -~\t\r\n]/, "", c);',
        "CJK view deletes all ASCII again",
        (
            ("f11", planted(GATE11_PASS_CASES["f11"])),
            ("f12", planted(GATE11_PASS_CASES["f12"])),
            ("f7", planted(GATE11_PASS_CASES["f7"])),
        ),
        expect_pass_to_fail=True,
    )
    expect_gate_ci_mutant(
        ci,
        r'h = $0; gsub(/`[^`]*`/, "", h); if (NR != 3 && h ~ /<[A-Za-z\/!?]/) { print "docs/RELEASE-NOTES.md line " NR " expected no raw HTML (Markdown only; line 3 is the status comment)"; bad = 1; } ',
        "",
        "no raw-HTML check",
        (
            ("e07_br", planted(GATE11_REFUSE_CASES["e07_br"])),
            ("codex_em_ml", planted(GATE11_REFUSE_CASES["codex_em_ml"])),
            ("codex_attr", planted(GATE11_REFUSE_CASES["codex_attr"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'h = $0; gsub(/`[^`]*`/, "", h); if (NR != 3 && h ~ /<[A-Za-z\/!?]/)',
        r'if (NR != 3 && $0 ~ /<[A-Za-z\/!?]/)',
        "raw-HTML check without code-span exemption",
        (
            ("f8", planted(GATE11_PASS_CASES["f8"])),
            ("f10", planted(GATE11_PASS_CASES["f10"])),
        ),
        expect_pass_to_fail=True,
    )
    expect_gate_ci_mutant(
        ci,
        r'if ($0 ~ /\]\([^)]*[ \t"\047(<`]/ || $0 ~ /\]\([^)]*$/) ',
        r'if ($0 ~ /\]\([^)]*[<]/ || $0 ~ /\]\([^)]*$/) ',
        "link check accepts a title",
        (
            ("r9_codex_paren_url", planted(GATE11_REFUSE_CASES["r9_codex_paren_url"])),
            ("r9_codex_title", planted(GATE11_REFUSE_CASES["r9_codex_title"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'if ($0 ~ /\\[][()]/) { print "docs/RELEASE-NOTES.md line " NR " expected no escaped bracket or parenthesis"; bad = 1; } ',
        "",
        "no escaped-bracket check",
        # Reference links are refused outright since gate12, so no case is caught by this check alone; the
        # structural requirement in gate_problem() turns this mutant red.
        (),
    )
    expect_gate_ci_mutant(
        ci,
        r'if ($0 ~ /^[ \t\r>]*$/',
        r'if ($0 ~ /^[ \t\r]*$/',
        ">-only lines not a boundary",
        (
            ("r10_codex_quote_para", planted(GATE11_PASS_CASES["r10_codex_quote_para"])),
        ),
        expect_pass_to_fail=True,
    )
    expect_gate_ci_mutant(
        ci,
        r'if ($0 ~ /^[ \t>]*([-*+]|[0-9]+[.)])[ \t]/ || $0 ~ /^[ \t>]*\|/ || $0 ~ /^[ \t>]*#/) seg_check(); ',
        r'if ($0 ~ /^[ \t>]*\|/ || $0 ~ /^[ \t>]*#/) seg_check(); ',
        "list items do not start a new segment",
        (
            ("r9_adjacent_items", planted(GATE11_PASS_CASES["r9_adjacent_items"])),
            ("p03_items", planted(GATE11_PASS_CASES["p03_items"])),
            ("f3", planted(GATE11_PASS_CASES["f3"])),
            ("f4", planted(GATE11_PASS_CASES["f4"])),
            ("r10_quote_list", planted(GATE11_PASS_CASES["r10_quote_list"])),
        ),
        expect_pass_to_fail=True,
    )
    expect_gate_ci_mutant(
        ci,
        r'gsub(/\]\([^)]*\)/, "", t); ',
        "",
        "no link-target removal",
        (
            ("e05_link", planted(GATE11_REFUSE_CASES["e05_link"])),
            ("codex_link", planted(GATE11_REFUSE_CASES["codex_link"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'if (h ~ /\]\[/ || h ~ /^[ \t>]*\[[^]]*\]:/) { print "docs/RELEASE-NOTES.md line " NR " expected no reference-style link or definition (use [text](target))"; bad = 1; } ',
        "",
        "no reference-link refusal",
        (
            ("r11_codex_ref_escape", planted(GATE11_REFUSE_CASES["r11_codex_ref_escape"])),
            ("r11_codex_ref_wrap", planted(GATE11_REFUSE_CASES["r11_codex_ref_wrap"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'gsub(/!\[/, "[", t); gsub(/[{[][+]|[+][]}]|[{]-|-[}]|\$`|`\$/, "", t); ',
        "",
        "no image / inline-diff / inline-math token removal",
        (
            ("r11_n_img_cjk_alt", planted(GATE11_REFUSE_CASES["r11_n_img_cjk_alt"])),
            ("r11_n_idiff_latin", planted(GATE11_REFUSE_CASES["r11_n_idiff_latin"])),
            ("r11_n_idiff_cjk_sq", planted(GATE11_REFUSE_CASES["r11_n_idiff_cjk_sq"])),
        ),
    )
    expect_gate_ci_mutant(
        ci,
        r'gsub(/\]\[[^]\\\n]*\]/, "]", t);',
        r'gsub(/\]\[[^]\\]*\]/, "]", t);',
        "reference-label removal allowed across lines",
        (),  # reference links are refused outright since gate12; gate_problem() makes this red
    )
    expect_gate_ci_mutant(
        ci,
        "&#[0-9xX]|&[A-Za-z][A-Za-z0-9]*;",
        "&#([0-9]+|[xX][0-9A-Fa-f]+);|&[A-Za-z][A-Za-z0-9]+;",
        "character reference requires ;",
        (
            ("e13_noscolon (no div)", planted("This version is un&#114eleased.")),
            ("c10_noscolon (no div)", planted("這一版&#26410發布。")),
        ),
    )
    # Round 8: the next-release simulation really ran (outside a simulation).
    if not os.environ.get(SIM_ENV) and not notes_only and not SIM_RAN:
        fail("the next-release simulation did not run")
    print("release-notes-gate: clean")


if __name__ == "__main__":
    main()
