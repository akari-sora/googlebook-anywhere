#!/usr/bin/env bash
# install/kano-check.sh against fake machines (a fake /proc + /sys tree and a fake crossystem); nothing real is read
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
T=$(mktemp -d "${TMPDIR:-/tmp}/kano-check.XXXXXX"); trap 'rm -rf "$T"' EXIT
pass=0; fail=0
mkdir -p "$T/cwd"; touch "$T/cwd/abc"
machine() {  # machine <dir> <hwid> <stepping> <bios_version> <devsw> <wpsw> <battery> <ac>
    local d=$1; mkdir -p "$d/proc" "$d/sys/class/dmi/id" "$d/sys/class/power_supply/BAT0" "$d/sys/class/power_supply/AC" "$d/etc" "$d/bin"
    printf 'processor\t: 0\ncpu family\t: 6\nmodel\t\t: 154\nmodel name\t: 12th Gen Intel(R) Core(TM) i7-1260P\nstepping\t: %s\n' "$3" > "$d/proc/cpuinfo"
    echo Kano > "$d/sys/class/dmi/id/product_name"; echo "$4" > "$d/sys/class/dmi/id/bios_version"; echo coreboot > "$d/sys/class/dmi/id/bios_vendor"
    echo "$7" > "$d/sys/class/power_supply/BAT0/capacity"; echo "$8" > "$d/sys/class/power_supply/AC/online"
    echo CHROMEOS_RELEASE_BOARD=brya > "$d/etc/lsb-release"
    if [ "$4" = stock ]; then
        printf '#!/bin/sh\ncase "$1" in hwid) echo "%s";; fwid) echo Google_Kano.14505.575.0;; devsw_boot) echo %s;; wpsw_cur) echo %s;; esac\n' "$2" "$5" "$6" > "$d/bin/crossystem"
    else
        printf '#!/bin/sh\nexit 1\n' > "$d/bin/crossystem"
    fi
    chmod +x "$d/bin/crossystem"
}
expect() {  # expect <name> <exit> <machine dir> <regex>...: every regex must match some line of the output
    local name=$1 code=$2 dir=$3 re miss='' out rc; shift 3
    # run from a directory with a three-letter file name in it: an unquoted ??? label would turn into "abc"
    out=$(cd "$T/cwd" && PATH="$dir/bin:$PATH" KANO_CHECK_ROOT="$dir" bash "$ROOT/install/kano-check.sh" 2>&1); rc=$?
    for re in "$@"; do printf '%s\n' "$out" | grep -Eq -- "$re" || miss="$miss [$re]"; done
    if [ "$rc" = "$code" ] && [ -z "$miss" ]; then echo "PASS $name"; pass=$((pass + 1))
    else echo "FAIL $name (rc=$rc, missing:${miss:- none})"; printf '%s\n' "$out" | sed 's/^/    /'; fail=$((fail + 1)); fi
}
machine "$T/retail" "KANO-ABCD E2B-C3D-E4F" 3 stock 1 0 80 1
expect "retail KANO (stepping 3) on stock firmware, ready: GO" 0 "$T/retail" "^OK +CPU .*06-9a-03" "^RESULT: GO with 0 warning"
machine "$T/retail4" "KANO-ABCD E2B-C3D-E4F" 4 stock 1 0 80 1
expect "retail KANO (stepping 4): GO" 0 "$T/retail4" "06-9a-04, production stepping" "^RESULT: GO with 0 warning"
machine "$T/k0" "KANO-ABCD E2B-C3D-E4F" 2 stock 1 0 80 1
expect "K0 engineering sample: STOP" 1 "$T/k0" "^STOP +CPU .*engineering sample" "^RESULT: STOP"
machine "$T/test" "KANO TEST 2090" 4 stock 1 0 80 1
expect "TEST HWID: STOP" 1 "$T/test" "engineering/test unit" "^RESULT: STOP"
machine "$T/other" "VOLMAR-ABCD" 4 stock 1 0 80 1
sed -i.bak 's/Kano/Volmar/' "$T/other/sys/class/dmi/id/product_name"
expect "another model: STOP" 1 "$T/other" "not a KANO" "^RESULT: STOP"
machine "$T/wp" "KANO-ABCD" 4 stock 1 1 80 1
expect "write protection on: GO with a warning" 0 "$T/wp" "^WARN +hardware write protection is on" "^RESULT: GO with 1 warning"
machine "$T/nodev" "KANO-ABCD" 4 stock 0 0 80 1
expect "developer mode off: STOP" 1 "$T/nodev" "developer mode is off" "^RESULT: STOP"
machine "$T/mrc" "" 4 "MrChromebox-2603.0" 0 1 80 1
expect "already MrChromebox: GO" 0 "$T/mrc" "already on MrChromebox" "^RESULT: GO with 0 warning"
machine "$T/low" "KANO-ABCD" 4 stock 1 0 20 0
expect "low battery, no charger: GO with 2 warnings" 0 "$T/low" "battery 20 %" "charger not detected" "^RESULT: GO with 2 warning"
machine "$T/step5" "KANO-ABCD" 5 stock 1 0 80 1
expect "unknown stepping: UNKNOWN, not GO" 2 "$T/step5" "^\?\?\? +CPU .*stepping '5', which this check does not know" "^RESULT: UNKNOWN"
machine "$T/stepx" "KANO-ABCD" "?" stock 1 0 80 1
expect "unreadable stepping: UNKNOWN, not STOP or GO" 2 "$T/stepx" "stepping '\?'" "^RESULT: UNKNOWN"
machine "$T/model" "KANO-ABCD" 4 stock 1 0 80 1
sed -i.bak 's/^model\t\t: 154/model\t\t: 140/' "$T/model/proc/cpuinfo"
expect "another CPU model: UNKNOWN" 2 "$T/model" "family '6' model '140'" "^RESULT: UNKNOWN"
machine "$T/nocpu" "KANO-ABCD" 4 stock 1 0 80 1
rm "$T/nocpu/proc/cpuinfo"
expect "no /proc/cpuinfo: UNKNOWN" 2 "$T/nocpu" "family '\?' model '\?'" "^RESULT: UNKNOWN"
machine "$T/nofw" "" 4 "SomeVendor 1.0" 0 1 80 1
expect "firmware not identified: UNKNOWN" 2 "$T/nofw" "firmware not identified" "^RESULT: UNKNOWN"
machine "$T/both" "KANO-ABCD" 2 "SomeVendor 1.0" 0 1 80 1
expect "a blocker wins over an unknown item: STOP" 1 "$T/both" "engineering sample" "firmware not identified" "^RESULT: STOP"
echo "kano-check tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
