#!/usr/bin/env bash
# Run every test of the KANO developer harness (tools/harness) and exit non-zero on any failure:
#   1. bash -n / sh -n on every shell script, python3 -m py_compile on every Python file (bytecode outside the repo)
#   2. unit tests and integration tests against a fake KANO (tests/harness/test_*.py)
#   3. the mutation check (mutate.py): every guard reverted in a temp copy must turn its tests red
# usage: tests/harness/run.sh [--no-mutation]
#
# Nothing here reaches the network, a serial port, adb or a disk: remote commands run against a fake KANO in a temp
# dir (fakes/ssh + fakes/remote.py), HOME is a temp dir, KANO_IP/KANO_ADB_ADDR are TEST-NET addresses. Works with the
# macOS /bin/bash 3.2 and with Linux bash; needs python3 (3.8+). Refuses to run as root.
set -u
exec < /dev/null
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO=$(cd "$HERE/../.." && pwd -P)
H=${KANO_HARNESS_DIR:-$REPO/tools/harness}
MUTATION=1
[ "${1:-}" = "--no-mutation" ] && MUTATION=0
[ "$(id -u)" != 0 ] || { echo "refusing to run the harness tests as root"; exit 2; }
command -v python3 > /dev/null || { echo "python3 is missing"; exit 2; }
TMP=$(mktemp -d "${TMPDIR:-/tmp}/kano-harness-run.XXXXXX") || exit 2
trap 'rm -rf "$TMP"' EXIT
export PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$TMP/pycache"
fail=0
step() { echo; echo "== $*"; }

# Paths of __pycache__ directories and *.pyc files. Recorded before this run so a
# cache left by an earlier suite is not a failure. A new path fails the check below.
# So does an existing __pycache__ or .pyc whose mtime is newer than the stamp taken
# at start (a rewritten .pyc keeps the same path).
bytecode_paths() {
  find "$@" \( -type d -name __pycache__ -o -name '*.pyc' \) -print 2>/dev/null | sort
}
bytecode_newer() {
  local stamp=$1
  shift
  find "$@" \( -type d -name __pycache__ -o -name '*.pyc' \) -newer "$stamp" -print 2>/dev/null | sort
}
: > "$TMP/pyc-stamp"
bytecode_paths "$REPO/tools" "$HERE" > "$TMP/pyc-before"

step "bytecode guard: a pre-existing __pycache__ does not fail; a new one still does"
bg="$TMP/byteguard"
mkdir -p "$bg/pre/__pycache__"
echo old > "$bg/pre/__pycache__/old.pyc"
: > "$TMP/bg-stamp"
bytecode_paths "$bg" > "$TMP/bg-before"
bytecode_paths "$bg" > "$TMP/bg-after"
if [ -n "$(comm -13 "$TMP/bg-before" "$TMP/bg-after")" ]; then
  echo "FAIL  pre-existing __pycache__ was treated as new"; fail=1
elif [ -n "$(bytecode_newer "$TMP/bg-stamp" "$bg/pre")" ]; then
  echo "FAIL  pre-existing __pycache__ was newer than the stamp"; fail=1
else
  echo "ok    pre-existing __pycache__ does not fail the guard"
fi
mkdir -p "$bg/during/__pycache__"
echo new > "$bg/during/__pycache__/new.pyc"
echo loose > "$bg/during/loose.pyc"
bytecode_paths "$bg" > "$TMP/bg-after"
new=$(comm -13 "$TMP/bg-before" "$TMP/bg-after")
case "$new" in
  *"/during/__pycache__"*"/during/__pycache__/new.pyc"*"/during/loose.pyc"*)
    echo "ok    new __pycache__ and .pyc still fail the guard" ;;
  *) echo "FAIL  new bytecode was not detected"; printf '%s\n' "$new"; fail=1 ;;
esac
# Age the existing cache so it is strictly older than the stamp. Do not move the
# stamp backward: an untouched .pyc would then already be newer, and deleting
# the rewrite below would still pass. The control must see no hit; only the
# rewrite, with an mtime strictly after the stamp, may fail the guard.
rw="$TMP/rewritecheck"
mkdir -p "$rw/__pycache__"
echo old > "$rw/__pycache__/mod.pyc"
: > "$TMP/rw-stamp"
python3 -c '
import os, sys
root, stamp = sys.argv[1], sys.argv[2]
t = os.stat(stamp).st_mtime - 1
for dirpath, dirnames, filenames in os.walk(root, topdown=False):
    for name in filenames:
        os.utime(os.path.join(dirpath, name), (t, t))
    os.utime(dirpath, (t, t))
' "$rw" "$TMP/rw-stamp" || { echo "FAIL  could not age the old .pyc"; fail=1; }
bytecode_paths "$rw" > "$TMP/rw-before"
if [ -n "$(bytecode_newer "$TMP/rw-stamp" "$rw")" ]; then
  echo "FAIL  existing .pyc not rewritten was detected"; fail=1
else
  echo "ok    existing .pyc not rewritten passes the guard"
fi
cp "$rw/__pycache__/mod.pyc" "$TMP/mod.pyc.old"
python3 -c '
import os, sys
path, stamp = sys.argv[1], sys.argv[2]
with open(path, "w", encoding="ascii") as out:
    out.write("rewritten\n")
stamp_m = os.stat(stamp).st_mtime
os.utime(path, (stamp_m + 1, stamp_m + 1))
' "$rw/__pycache__/mod.pyc" "$TMP/rw-stamp" || { echo "FAIL  could not rewrite .pyc"; fail=1; }
bytecode_paths "$rw" > "$TMP/rw-after"
if cmp -s "$TMP/mod.pyc.old" "$rw/__pycache__/mod.pyc"; then
  echo "FAIL  existing .pyc bytes were not rewritten"; fail=1
elif [ -n "$(comm -13 "$TMP/rw-before" "$TMP/rw-after")" ]; then
  echo "FAIL  rewritten .pyc changed the path set"; fail=1
elif [ -z "$(bytecode_newer "$TMP/rw-stamp" "$rw")" ]; then
  echo "FAIL  existing .pyc rewritten was not detected"; fail=1
else
  echo "ok    existing .pyc rewritten fails the guard"
fi

step "syntax: bash -n ($(bash --version | head -n 1)) and sh -n"
for f in "$H"/*.sh "$HERE"/*.sh; do
  if bash -n "$f"; then echo "ok    bash -n $f"; else echo "FAIL  bash -n $f"; fail=1; fi
done
for f in "$H/kano-ssh.sh" "$H/kano-env.sh"; do
  if sh -n "$f"; then echo "ok    sh -n $f"; else echo "FAIL  sh -n $f"; fail=1; fi
done

step "syntax: python3 -m py_compile ($(python3 --version 2>&1)), bytecode under $TMP"
for f in "$H"/*.py "$HERE"/*.py "$HERE"/fakes/remote.py "$HERE"/fakes/ssh "$HERE"/fakes/adb "$HERE"/fakes/pyfault/sitecustomize.py "$HERE"/fakes/pyserial/serial/__init__.py; do
  if python3 -m py_compile "$f"; then echo "ok    $f"; else echo "FAIL  $f"; fail=1; fi
done

step "unit and fake-KANO integration tests"
if (cd "$HERE" && python3 -m unittest -v test_ser test_env test_no_personal_data test_unconfigured test_boot_menu test_real_layout test_delta_push test_nvme_install test_nvme_update test_watch test_ui); then
  echo "ok    unit and integration tests"
else
  echo "FAIL  unit and integration tests"; fail=1
fi

if [ $MUTATION -eq 1 ]; then
  step "mutation check: each guard reverted in a temp copy of the harness must turn its tests red"
  if python3 "$HERE/mutate.py"; then echo "ok    mutation check"; else echo "FAIL  mutation check"; fail=1; fi

  step "mutation check: a mutant id that names no mutation is refused before anything runs"
  # $1 = label, $2 = id: mutate.py must print "unknown mutant id: <repr(id)>" on stderr and exit 2, nothing run.
  check_unknown() {
    local rc=0 out
    out=$(python3 "$HERE/mutate.py" "$2" 2>&1) || rc=$?
    case "$out" in
      *"unknown mutant id: "*)
        if [ "$rc" -eq 2 ]; then echo "ok    unknown id $1 refused (exit 2)"
        else echo "FAIL  unknown id $1: exit $rc, wanted 2"; fail=1; fi ;;
      *) echo "FAIL  unknown id $1: no 'unknown mutant id' message: $out"; fail=1 ;;
    esac
  }
  check_unknown "no-such-mutant" "no-such-mutant"
  check_unknown "trailing carriage return" "$(printf 'ser-pwm-channel\r')"
  if python3 "$HERE/mutate.py" ser-pwm-channel > /dev/null 2>&1; then
    echo "ok    existing id ser-pwm-channel still accepted"
  else
    echo "FAIL  existing id ser-pwm-channel was refused"; fail=1
  fi
fi

bytecode_paths "$REPO/tools" "$HERE" > "$TMP/pyc-after"
new=$(comm -13 "$TMP/pyc-before" "$TMP/pyc-after" || true)
newer=$(bytecode_newer "$TMP/pyc-stamp" "$REPO/tools" "$HERE" || true)
if [ -n "$new" ] || [ -n "$newer" ]; then
  echo "FAIL  bytecode was written into the repo"
  [ -z "$new" ] || printf '%s\n' "$new"
  [ -z "$newer" ] || printf '%s\n' "$newer"
  fail=1
fi
echo
if [ $fail -eq 0 ]; then echo "ALL HARNESS TESTS PASSED"; else echo "HARNESS TESTS FAILED"; fi
exit $fail
