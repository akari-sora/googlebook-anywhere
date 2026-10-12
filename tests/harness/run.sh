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

step "syntax: bash -n ($(bash --version | head -n 1)) and sh -n"
for f in "$H"/*.sh "$HERE"/*.sh; do
  if bash -n "$f"; then echo "ok    bash -n $f"; else echo "FAIL  bash -n $f"; fail=1; fi
done
for f in "$H/kano-ssh.sh" "$H/kano-env.sh"; do
  if sh -n "$f"; then echo "ok    sh -n $f"; else echo "FAIL  sh -n $f"; fail=1; fi
done

step "syntax: python3 -m py_compile ($(python3 --version 2>&1)), bytecode under $TMP"
for f in "$H"/*.py "$HERE"/*.py "$HERE"/fakes/remote.py "$HERE"/fakes/ssh "$HERE"/fakes/adb "$HERE"/fakes/pyserial/serial/__init__.py; do
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
fi

if [ -n "$(find "$REPO/tools" "$HERE" -name __pycache__ -o -name '*.pyc' 2> /dev/null)" ]; then
  echo "FAIL  bytecode was written into the repo"; fail=1
fi
echo
if [ $fail -eq 0 ]; then echo "ALL HARNESS TESTS PASSED"; else echo "HARNESS TESTS FAILED"; fi
exit $fail
