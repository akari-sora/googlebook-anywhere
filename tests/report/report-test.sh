#!/usr/bin/env bash
# install/gbos-report.sh: field validators, the read-only allowlist (including refusals), and a
# full report from saved adb output plus a gbos-probe directory. No adb server, no device, no personal data.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
R="$ROOT/install/gbos-report.sh"
FIX="$ROOT/tests/report/fixtures"
EXP="$ROOT/tests/report/expected"
T=$(mktemp -d "${TMPDIR:-/tmp}/gbos-report-test.XXXXXX")
trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok() { echo "PASS $1"; pass=$((pass + 1)); }
bad() { echo "FAIL $1"; fail=$((fail + 1)); }

out=$(bash "$R" --scrub all --fixture "$FIX/kano" --out "$T/scrubmode" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'unknown argument' && [ ! -e "$T/scrubmode" ] \
    && ok "--scrub is refused" || bad "--scrub is refused (rc=$rc)"
out=$(bash "$R" --include-raw --fixture "$FIX/kano" --out "$T/rawmode" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'unknown argument' && [ ! -e "$T/rawmode" ] \
    && ok "--include-raw is refused" || bad "--include-raw is refused (rc=$rc)"

list=$(bash "$R" --list) || { echo "FAIL --list"; exit 1; }
while IFS= read -r cmd; do
    [ -n "$cmd" ] || continue
    out=$(bash "$R" --classify "$cmd" 2> "$T/cl.err"); rc=$?
    if [ "$rc" = 0 ] && [ "$out" = allow ]; then ok "allow: $cmd"
    else bad "allow: $cmd (rc=$rc out=$out)"; fi
done << EOF
$list
EOF

refuse() { # refuse <name> <command>
    local name=$1 cmd=$2 out rc=0
    out=$(bash "$R" --classify "$cmd" 2> "$T/cl.err") || rc=$?
    if [ "$rc" = 2 ] && [ "$out" = refused ]; then ok "refused: $name"
    else bad "refused: $name (rc=$rc out=$out)"; fi
}
refuse "adb root" "adb root"
refuse "adb remount" "adb remount"
refuse "adb reboot" "adb reboot"
refuse "adb install" "adb install app.apk"
refuse "adb push" "adb push local.apk /data/local/tmp/app.apk"
refuse "adb pull" "adb pull /data/local/tmp/x ."
refuse "adb disable-verity" "adb disable-verity"
refuse "settings put" "settings put global adb_enabled 1"
refuse "adb shell settings" "adb shell settings put system screen_off_timeout 60000"
refuse "setprop" "setprop persist.sys.usb.config adb"
refuse "rm" "rm -rf /data"
refuse "svc" "svc wifi disable"
refuse "input tap" "input tap 1 1"
refuse "getprop one key" "getprop ro.serialno"
refuse "getprop with a semicolon" "getprop; id"
refuse "dumpsys alone" "dumpsys"
refuse "dumpsys package" "dumpsys package"
refuse "dumpsys wifi with an extra argument" "dumpsys wifi set-wifi-enabled disabled"
refuse "unfiltered logcat" "logcat -d"
refuse "logcat without a tag filter" "logcat -d -b crash,main"
refuse "logcat wildcard tag" "logcat -d -b crash,main -s *:V"
refuse "cat of a MAC address file" "cat /sys/class/net/wlan0/address"
refuse "cat of two files" "cat /proc/cpuinfo /sys/class/net/wlan0/address"
refuse "cat of product serial" "cat /sys/class/dmi/id/product_serial"
refuse "cat of proc net" "cat /proc/net/arp"
refuse "ls of /data" "ls /data"
refuse "empty command" ""

while IFS= read -r cmd; do
    [ -n "$cmd" ] || continue
    case "$cmd" in
        *';'*|*'|'*|*'&'*|*'<'*|*'>'*|*'$'*) bad "allowlist command has a metacharacter: $cmd" ;;
    esac
    case "$cmd" in
        cat\ *)
            path=${cmd#cat }
            case "$path" in
                *[[:space:]]*|*'..'*|*address*|*serial*|*uuid*|*wpa*|*pstore*|*WifiConfig*|/proc/net*)
                    bad "cat path is too wide: $path" ;;
                /proc/*|/sys/*) ok "cat path: $path" ;;
                *) bad "cat path is not /proc or /sys: $path" ;;
            esac
            ;;
        logcat\ *)
            case "$cmd" in
                *'*:'*) bad "logcat filter has a wildcard" ;;
                "logcat -d -b crash,main -s "*) ok "logcat is a filtered dump" ;;
                *) bad "logcat shape: $cmd" ;;
            esac
            ;;
    esac
done << EOF
$list
EOF

# the shell part (before the embedded Python) must not name a device subcommand we refuse
bash_part=$(sed '/^PY$/q' "$R")
if printf '%s\n' "$bash_part" | grep -nE 'adb[[:space:]]+(root|remount|reboot|install|push|pull|disable-verity)|settings[[:space:]]+put|[[:space:]]setprop[[:space:]]'; then
    bad "the script text contains a forbidden device command"
else
    ok "the script text has no forbidden device command"
fi
if printf '%s\n' "$bash_part" | grep -nE 'declare[[:space:]]+-A|mapfile|readarray|\$\{[A-Za-z_][A-Za-z0-9_]*,,\}'; then
    bad "bash 3.2 syntax"
else
    ok "bash 3.2 syntax"
fi

out=$(bash "$R" --help 2>&1); rc=$?
[ "$rc" = 0 ] && printf '%s\n' "$out" | grep -q 'allowlist' && ok "--help" || bad "--help (rc=$rc)"
out=$(bash "$R" --bogus 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'unknown argument' && ok "unknown argument" || bad "unknown argument (rc=$rc)"
out=$(bash "$R" --classify 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q -- '--classify needs' && ok "--classify needs a command" || bad "--classify needs a command"
out=$(bash "$R" --date 2026-10-14 --fixture "$FIX/kano" --out "$T/baddate" 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'YYYYMMDD' && [ ! -e "$T/baddate" ] && ok "bad date" || bad "bad date (rc=$rc)"
out=$(bash "$R" --fixture "$FIX/kano" --serial emulator-5554 --out "$T/mix" 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'does not use adb' && [ ! -e "$T/mix" ] && ok "fixture plus serial" || bad "fixture plus serial"
out=$(bash "$R" --serial 'bad serial' --out "$T/badserial" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'serial' && [ ! -e "$T/badserial" ] && ok "serial with a space" || bad "serial with a space"
out=$(bash "$R" --serial --help-me --out "$T/dashserial" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -e "$T/dashserial" ] && ok "serial starting with a dash" || bad "serial starting with a dash (rc=$rc out=$out)"
mkdir -p "$T/empty-probe"
out=$(bash "$R" --probe "$T/empty-probe" --out "$T/noprobe" 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'probe.json' && [ ! -e "$T/noprobe" ] && ok "probe without probe.json" || bad "probe without probe.json"
mkdir -p "$T/busy"; printf 'keep\n' > "$T/busy/keep"
out=$(bash "$R" --fixture "$FIX/swiftshader" --out "$T/busy" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ "$(cat "$T/busy/keep")" = keep ] && [ ! -e "$T/busy/summary.md" ] && ok "non-empty --out" || bad "non-empty --out (rc=$rc)"
mkdir -p "$T/linktarget"
ln -s "$T/linktarget" "$T/outlink"
out=$(bash "$R" --fixture "$FIX/swiftshader" --out "$T/outlink" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ -z "$(ls -A "$T/linktarget")" ] && ok "symlink --out" || bad "symlink --out (rc=$rc)"
# a symlink fixture file is refused; use a copy of the directory so the real fixtures stay untouched
mkdir -p "$T/fx"; cp -a "$FIX/kano/." "$T/fx/"; ln -sf "$T/fx/getprop.txt" "$T/fx/uname-a.txt"
out=$(bash "$R" --fixture "$T/fx" --out "$T/symfix" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'symlink fixture' && [ ! -e "$T/symfix" ] && ok "symlink fixture file" || bad "symlink fixture file (rc=$rc out=$out)"

mkdir -p "$T/nopath"
for tool in python3 date mkdir mktemp ls cat; do
    src=$(command -v "$tool") || { bad "host has no $tool"; continue; }
    ln -s "$src" "$T/nopath/$tool"
done
# a slash in the bash path keeps this shell's PATH from hiding bash itself; the child only sees $T/nopath
out=$(PATH="$T/nopath" "$(command -v bash)" "$R" --out "$T/noadb" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'adb not found' && [ ! -e "$T/noadb" ] && ok "adb not found" || bad "adb not found (rc=$rc out=$out)"

# fake adb records every argument. The script may pass only "shell" plus one allowlisted command.
cat > "$T/adb" << 'EOF'
#!/bin/sh
{
    printf '%s' "$1"
    shift
    for a in "$@"; do
        printf '\t%s' "$a"
    done
    printf '\n'
} >> "$GBOS_FAKE_ADB_LOG"
printf 'ok\n'
EOF
chmod +x "$T/adb"
export GBOS_FAKE_ADB_LOG="$T/adb.log"
: > "$T/adb.log"
if PATH="$T:$PATH" bash "$R" --out "$T/fake" --date 20261014 > "$T/fake.out" 2> "$T/fake.err"; then
    : > "$T/expect.log"
    while IFS= read -r cmd; do
        [ -n "$cmd" ] || continue
        printf 'shell\t%s\n' "$cmd" >> "$T/expect.log"
    done << EOF
$list
EOF
    if diff -q "$T/expect.log" "$T/adb.log" > /dev/null; then ok "fake adb saw only allowlisted shell commands"
    else bad "fake adb saw only allowlisted shell commands"; diff -u "$T/expect.log" "$T/adb.log" | head -n 40; fi
    grep -q 'allowlisted fields' "$T/fake/gbos-report-20261014.txt" && ok "fake adb run still writes a privacy header" || bad "fake adb run still writes a privacy header"
else
    bad "fake adb run (rc)"
    cat "$T/fake.err"
fi
: > "$T/adb.log"
if PATH="$T:$PATH" bash "$R" --serial emulator-5554 --out "$T/fake2" --date 20261014 > /dev/null 2> "$T/fake2.err"; then
    : > "$T/expect2.log"
    while IFS= read -r cmd; do
        [ -n "$cmd" ] || continue
        printf -- '-s\temulator-5554\tshell\t%s\n' "$cmd" >> "$T/expect2.log"
    done << EOF
$list
EOF
    if diff -q "$T/expect2.log" "$T/adb.log" > /dev/null; then ok "fake adb -s gets the serial and the same commands"
    else bad "fake adb -s gets the serial and the same commands"; diff -u "$T/expect2.log" "$T/adb.log" | head -n 20; fi
else
    bad "fake adb -s run"; cat "$T/fake2.err"
fi
: > "$T/adb.log"
if PATH="$T:$PATH" bash "$R" --probe "$FIX/kano-probe" --out "$T/probe" --date 20261014 > /dev/null; then
    if [ ! -s "$T/adb.log" ]; then ok "probe-only does not call adb"
    else bad "probe-only does not call adb"; cat "$T/adb.log"; fi
    grep -q '僅 probe' "$T/probe/summary.md" && grep -q '8086:46a6' "$T/probe/summary.md" \
        && grep -q '探測到 wlan0' "$T/probe/summary.md" && grep -q '| 建置版本 | 未知 |' "$T/probe/summary.md" \
        && ok "probe-only summary" || bad "probe-only summary"
    if grep -F -q '02:11:22:33:44:55' "$T/probe/gbos-report-20261014.txt" || grep -F -q 'synthuser' "$T/probe/gbos-report-20261014.txt"; then
        bad "probe-only report still has a planted identifier"
    else
        ok "probe-only report scrubbed the planted identifier"
    fi
else
    bad "probe-only run"
fi
: > "$T/adb.log"
if PATH="$T:$PATH" bash "$R" --fixture "$FIX/swiftshader" --out "$T/swift" --date 20261014 > /dev/null; then
    [ ! -s "$T/adb.log" ] && ok "fixture mode does not call adb" || bad "fixture mode does not call adb"
    grep -F -q '沒有原生 GPU 驅動（SwiftShader, vkms, gbos_efifb）' "$T/swift/summary.md" \
        && grep -F -q 'no native GPU driver (SwiftShader, vkms, gbos_efifb)' "$T/swift/gbos-report-20261014.txt" \
        && ! grep -F -q '已綁定' "$T/swift/summary.md" \
        && grep -F -q 'SYNTHSERIAL002' "$FIX/swiftshader/getprop.txt" \
        && ! grep -F -q 'SYNTHSERIAL002' "$T/swift/gbos-report-20261014.txt" \
        && ! grep -F -q 'SYNTHSERIAL002' "$T/swift/summary.md" \
        && ! grep -F -q 'lapis' "$T/swift/summary.md" \
        && ok "swiftshader fixture is not bound" || bad "swiftshader fixture is not bound"
else
    bad "swiftshader fixture run"
fi

if bash "$R" --fixture "$FIX/kano" --probe "$FIX/kano-probe" --out "$T/full" --date 20261014 > /dev/null; then
    if diff -q "$EXP/gbos-report-20261014.txt" "$T/full/gbos-report-20261014.txt" > /dev/null \
        && diff -q "$EXP/summary.md" "$T/full/summary.md" > /dev/null; then
        ok "fixture report matches the expected output"
    else
        bad "fixture report matches the expected output"
        diff -u "$EXP/gbos-report-20261014.txt" "$T/full/gbos-report-20261014.txt" | head -n 60
        diff -u "$EXP/summary.md" "$T/full/summary.md" | head -n 40
    fi
    txt="$T/full/gbos-report-20261014.txt"
    sum="$T/full/summary.md"
    secrets='SYNTHSERIAL001 SYNTHSERIAL002 ARMSERIALSHOULDGO BOOTSERIAL999 BOOTSERIAL888 02:00:00:00:00:01 02-00-00-00-00-02 02:11:22:33:44:55 490154203237518 A1000000000001 SyntheticCafe ada.lovelace@example.com Lovelace synth-laptop synth-build synthuser a1b2c3d4e5f67890 203.0.113.10 2001:db8::10'
    leaked=0
    # shellcheck disable=SC2086
    for s in $secrets; do
        if grep -F -q -- "$s" "$txt" || grep -F -q -- "$s" "$sum"; then
            bad "secret still present: $s"
            leaked=1
        fi
    done
    [ "$leaked" = 0 ] && ok "no synthetic identifier remains in the report"
    grep -F -q '## getprop' "$txt" && bad "default report contains raw getprop" || ok "default report has no raw dumps"
    grep -F -q '8086:46a6' "$txt" && grep -F -q 'CL3B.260622.271.R1' "$sum" \
        && grep -F -q '已連線，驅動 iwlwifi' "$sum" && grep -F -q '已綁定（xe）' "$sum" && grep -F -q '成功 4、失敗 1' "$sum" \
        && grep -F -q '| 韌體 | coreboot |' "$sum" \
        && grep -F -q '| 韌體旗標 | UEFI yes, Secure Boot off |' "$sum" \
        && grep -F -q '| 機型 | Google |' "$sum" && grep -F -q '| 板名 | Google |' "$sum" \
        && grep -F -q '| CPU | GenuineIntel family 6 model 154 stepping 3 |' "$sum" \
        && grep -F -q '| 音效卡 | 未知 |' "$sum" && grep -F -q '未知（沒有映像標記）' "$sum" \
        && grep -F -q 'kano（probe-only）' "$sum" && grep -F -q '一致：DMI、CPU、PCI' "$sum" \
        && grep -F -q '觸控螢幕：04f3:2d5b（i2c）' "$sum" && grep -F -q '觸控板：04f3:323c（i2c）' "$sum" \
        && ! grep -F -q 'ELAN' "$sum" && ! grep -F -q '123456789012345' "$txt" \
        && ! grep -F -q '8086:51f0' "$sum" && ! grep -F -q '8086:51F0' "$txt" \
        && ! grep -F -q 'lapis' "$sum" && ! grep -F -q 'ASUS' "$txt" \
        && ! grep -F -q 'Kano' "$sum" && ! grep -F -q 'MrChromebox' "$sum" && ! grep -F -q 'i7-1260P' "$txt" \
        && ! grep -F -q 'sof-nau8825' "$sum" && ! grep -F -q 'sofna8825' "$txt" \
        && ok "summary keeps the hardware facts" || bad "summary keeps the hardware facts"
    grep -F -q 'allowlisted fields' "$txt" && grep -F -q '不含 dumpsys／logcat 原文' "$sum" \
        && ok "header says the default report is allowlisted" || bad "header says the default report is allowlisted"
else
    bad "full fixture report"
fi

! grep -F -q '## getprop' "$T/full/gbos-report-20261014.txt" && ! grep -F -q 'privacy counts:' "$T/full/gbos-report-20261014.txt" \
    && grep -F -q 'does not attach raw logs' "$T/full/gbos-report-20261014.txt" \
    && grep -F -q '不附原始 log' "$T/full/summary.md" \
    && ok "report has no raw section and says to ask for logs" || bad "report has no raw section and says to ask for logs"

one() { # one <name> <dir> ; then the caller greps "$T/case/summary.md" and the txt
    local name=$1 dir=$2
    if bash "$R" --fixture "$dir" --out "$T/case-$name" --date 20261014 > /dev/null; then
        ok "ran $name"
    else
        bad "ran $name"
    fi
}
one wifi-history "$FIX/wifi-history"
grep -F -q '| Wi-Fi | 已關閉 |' "$T/case-wifi-history/summary.md" && ! grep -F -q '已連線' "$T/case-wifi-history/summary.md" \
    && ! grep -F -q 'HistoryCafe9f3a' "$T/case-wifi-history/summary.md" && ! grep -F -q 'HistoryCafe9f3a' "$T/case-wifi-history/gbos-report-20261014.txt" \
    && ok "disabled Wi-Fi stays disabled when history says COMPLETED" || bad "disabled Wi-Fi stays disabled when history says COMPLETED"
one drm-unreadable "$FIX/drm-unreadable"
grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/case-drm-unreadable/summary.md" && grep -F -q '| GPU PCI | 未知 |' "$T/case-drm-unreadable/summary.md" \
    && ! grep -F -q '未綁定' "$T/case-drm-unreadable/summary.md" && ! grep -F -q '沒有原生' "$T/case-drm-unreadable/summary.md" \
    && ok "unreadable DRM is unknown" || bad "unreadable DRM is unknown"
one input-real "$FIX/input-real"
grep -F -q '觸控螢幕：04f3:2d5b（i2c）' "$T/case-input-real/summary.md" \
    && grep -F -q '觸控板：06cb:ce92（i2c）' "$T/case-input-real/summary.md" \
    && grep -F -q '觸控板：04f3:1234（i2c）' "$T/case-input-real/summary.md" \
    && ! grep -F -q 'LeakNick9f3a' "$T/case-input-real/summary.md" && ! grep -F -q 'LEAKDESC9F3A2C' "$T/case-input-real/gbos-report-20261014.txt" \
    && ! grep -F -q 'LEAKUNIQ9F3A2C' "$T/case-input-real/gbos-report-20261014.txt" \
    && ok "input parser keeps kind, bus, and hardware id" || bad "input parser keeps kind, bus, and hardware id"
one dmi-not-lapis "$FIX/dmi-not-lapis"
grep -F -q '| 機型 | Framework |' "$T/case-dmi-not-lapis/summary.md" \
    && ! grep -F -q 'Laptop16' "$T/case-dmi-not-lapis/summary.md" \
    && ! grep -F -q 'lapis' "$T/case-dmi-not-lapis/summary.md" && ! grep -F -q 'ASUS' "$T/case-dmi-not-lapis/gbos-report-20261014.txt" \
    && ok "model comes from DMI, not ro.product.model" || bad "model comes from DMI, not ro.product.model"
one lapis-only "$FIX/lapis-only"
grep -F -q '| 機型 | 未知 |' "$T/case-lapis-only/summary.md" \
    && ! grep -F -q 'lapis' "$T/case-lapis-only/summary.md" && ! grep -F -q 'lapis' "$T/case-lapis-only/gbos-report-20261014.txt" \
    && ! grep -F -q 'ASUS' "$T/case-lapis-only/gbos-report-20261014.txt" \
    && ok "image model is not the machine when DMI is missing" || bad "image model is not the machine when DMI is missing"
one bt-adapter-off "$FIX/bt-adapter-off"
grep -F -q '| 藍牙 | 已關閉，驅動 btusb |' "$T/case-bt-adapter-off/summary.md" && ! grep -F -q '已開啟' "$T/case-bt-adapter-off/summary.md" \
    && ! grep -F -q 'LeakNickBT9f3a' "$T/case-bt-adapter-off/gbos-report-20261014.txt" \
    && ! grep -F -q '02:99:88:77:66:55' "$T/case-bt-adapter-off/gbos-report-20261014.txt" \
    && ok "bluetooth power comes from the adapter, not the service" || bad "bluetooth power comes from the adapter, not the service"
one bt-service-only "$FIX/bt-service-only"
grep -F -q '服務執行中，開關未確認' "$T/case-bt-service-only/summary.md" \
    && ! grep -F -q '已開啟' "$T/case-bt-service-only/summary.md" && ! grep -F -q '已關閉' "$T/case-bt-service-only/summary.md" \
    && ok "bluetooth service alone is not power on" || bad "bluetooth service alone is not power on"
one bt-hal-order "$FIX/bt-hal-order"
grep -F -q '服務執行中，開關未確認' "$T/case-bt-hal-order/summary.md" && ! grep -F -q '已關閉' "$T/case-bt-hal-order/summary.md" \
    && ok "bluetooth_hal stopped does not hide a running service" || bad "bluetooth_hal stopped does not hide a running service"
one gpu-mixed "$FIX/gpu-mixed"
grep -F -q '| 顯示／GPU 驅動 | 已綁定（i915） |' "$T/case-gpu-mixed/summary.md" \
    && grep -F -q '| GPU PCI | 1234:5678, 1234:1111, 8086:9a49 |' "$T/case-gpu-mixed/summary.md" \
    && grep -F -q 'card0 driver vkms pci 1234:5678 shim' "$T/case-gpu-mixed/summary.md" \
    && ! grep -F -q '已綁定（vkms）' "$T/case-gpu-mixed/summary.md" \
    && ok "a real GPU driver on a later card is bound" || bad "a real GPU driver on a later card is bound"
if bash "$R" --fixture "$FIX/probe-mismatch" --probe "$FIX/probe-mismatch-probe" --out "$T/mismatch" --date 20261014 > /dev/null; then
    ms=$T/mismatch/summary.md
    mt=$T/mismatch/gbos-report-20261014.txt
    grep -F -q '未知（沒有映像標記）' "$ms" && grep -F -q '| 機型 | Google |' "$ms" \
        && grep -F -q '| 音效卡 | 未知 |' "$ms" \
        && ! grep -F -q 'other-laptop' "$ms" && ! grep -F -q 'other-laptop' "$mt" \
        && ! grep -F -q 'googlebook-sound' "$ms" && ! grep -F -q 'googlebook-sound' "$mt" \
        && ! grep -F -q 'live-linux-only-sound' "$ms" && ! grep -F -q 'live-linux-only-sound' "$mt" \
        && grep -F -q '已省略（probe 與這台裝置不是同一台）' "$ms" \
        && ! grep -F -q 'Secure Boot' "$ms" && ! grep -F -q 'UEFI' "$ms" \
        && ! grep -F -q 'Other Laptop' "$ms" && ! grep -F -q '1002:1638' "$mt" \
        && ! grep -F -q 'lapis' "$ms" && ! grep -F -q '02:11:22:33:44:55' "$mt" && ! grep -F -q 'synthuser' "$mt" \
        && ok "a different machine's probe facts are omitted" || bad "a different machine's probe facts are omitted"
else
    bad "probe mismatch run"
fi

leaks='LEAKSERIAL9F3A LEAKBOOT9F3A LEAKRIL9F3A LEAKPERSIST9F3A LEAKUSBPROP9F3A LEAKUSB9F3A LEAKDESC9F3A2C LEAKUNIQ9F3A2C LEAKEDID9F3A LEAKARMSERIAL9F3A LeakHomeWifi9f3a LeakScanWifi9f3a LEAKPSK9F3A2C LeakAccount9f3a LEAKEAP9F3A leak.person@example.test leakuser9f3a leakhost9f3a deadbeefcafebabe 029988776655 001122334455 02:99:88:77:66:55 2001:db8:9:8:7:6:5:4 203.0.113.99 001a7dda7113 local:4619827259835644999 /home/Leak Person leakuser9f3a/tax.pdf'
nicknames='lapis ASUS LeakNick9f3a LeakNickBT9f3a'
grep -F -q 'LEAKSERIAL9F3A' "$FIX/leaks/getprop.txt" && grep -F -q 'LeakScanWifi9f3a' "$FIX/leaks/dumpsys-wifi.txt" \
    && grep -F -q 'LeakNickBT9f3a' "$FIX/leaks/cat-bt-name.txt" && ok "leak fixture actually contains the identifiers" || bad "leak fixture actually contains the identifiers"
if bash "$R" --fixture "$FIX/leaks" --out "$T/leaks" --date 20261014 > /dev/null; then
    leaked=0
    # shellcheck disable=SC2086
    for s in $leaks; do
        if grep -F -q -- "$s" "$T/leaks/gbos-report-20261014.txt" || grep -F -q -- "$s" "$T/leaks/summary.md"; then
            bad "leak fixture still shows $s"
            leaked=1
        fi
    done
    [ "$leaked" = 0 ] && ok "leak fixture identifiers are absent" || true
    leaked=0
    # shellcheck disable=SC2086
    for s in $nicknames; do
        if grep -F -q -- "$s" "$T/leaks/gbos-report-20261014.txt" || grep -F -q -- "$s" "$T/leaks/summary.md"; then
            bad "default report still shows $s"
            leaked=1
        fi
    done
    [ "$leaked" = 0 ] && ok "default report drops image names and nicknames" || true
    grep -F -q '| Wi-Fi | 已關閉 |' "$T/leaks/summary.md" && ! grep -F -q '## getprop' "$T/leaks/gbos-report-20261014.txt" \
        && ok "leak fixture is structured fields only" || bad "leak fixture is structured fields only"
else
    bad "leak fixture run"
fi

# Each schema field's source carries its own canary. The canary is in the parsed value, not appended after it.
shaped='CANARYSERIAL9F3A canary.model@example.test deadbeefcafebabe /home/canaryuser9f3a canary.bios@example.test 203.0.113.77 canary.cpu@example.test canary.sound@example.test CanarySsid9f3a Cafe, Home 02:aa:bb:cc:dd:11 001122334455 CanaryNick9f3a canary-2.4-host 2.4-host canaryhost9f3a canaryuser9f3a canary-laptop-9f3a lapis ASUS 490154203237518 canary.probe@example.test CANARYTAX9F3A leak.person@example.test 02:99:88:77:66:55'
if bash "$R" --fixture "$FIX/r2/canary" --probe "$FIX/r2/canary-probe" --out "$T/canary" --date 20261014 > /dev/null; then
    leaked=0
    # shellcheck disable=SC2086
    for s in $shaped; do
        if grep -R -F -q -- "$s" "$T/canary"; then
            bad "canary still present: $s"
            leaked=1
        fi
    done
    [ "$leaked" = 0 ] && ok "canary identifiers are absent from every output file" || true
    grep -F -q '04f3:2d5b' "$T/canary/summary.md" \
        && grep -F -q 'family 6' "$T/canary/summary.md" && grep -F -q '| Wi-Fi | 已連線 |' "$T/canary/summary.md" \
        && ! grep -F -q '6.6.30-ok' "$T/canary/summary.md" \
        && ! grep -F -q 'canary.cpu@example.test' "$T/canary/gbos-report-20261014.txt" \
        && ! grep -R -F -q -- 'canary.sound' "$T/canary" \
        && ! grep -R -F -q -- 'canary.cpu' "$T/canary" \
        && ok "canary fixture keeps the checked hardware values" || bad "canary fixture keeps the checked hardware values"
else
    bad "canary run"
fi

out=$(bash "$R" --fixture "$FIX/kano" --adb --out "$T/fxadb" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'does not use adb' && [ ! -e "$T/fxadb" ] && ok "--fixture --adb is refused" || bad "--fixture --adb is refused (rc=$rc)"
mkdir -p "$T/linktarget2"
ln -s "$T/linktarget2" "$T/outlink2"
out=$(bash "$R" --fixture "$FIX/swiftshader" --out "$T/outlink2/" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ -z "$(ls -A "$T/linktarget2")" ] && ok "trailing-slash symlink --out" || bad "trailing-slash symlink --out (rc=$rc out=$out)"
mkdir -p "$T/badprobe" "$T/keep-empty"
printf '[]\n' > "$T/badprobe/probe.json"
out=$(bash "$R" --probe "$T/badprobe" --out "$T/keep-empty" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'must be a JSON object' && ! printf '%s\n' "$out" | grep -q 'Traceback' \
    && [ -d "$T/keep-empty" ] && [ -z "$(ls -A "$T/keep-empty")" ] && ok "bad probe.json keeps a pre-existing empty directory" || bad "bad probe.json keeps a pre-existing empty directory (rc=$rc)"
out=$(bash "$R" --probe "$T/badprobe" --out "$T/created-bad" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -e "$T/created-bad" ] && ok "failed run removes the directory it created" || bad "failed run removes the directory it created (rc=$rc)"
mkdir -p "$T/probesym"
printf '%s\n' '{"dmi":{"sys_vendor":"Google","product_name":"Kano"}}' > "$T/probesym/probe.json"
ln -s "$T/badprobe/probe.json" "$T/probesym/report.txt"
out=$(bash "$R" --probe "$T/probesym" --out "$T/repsym" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'refusing symlink' && [ ! -e "$T/repsym" ] && ok "probe report.txt symlink is refused" || bad "probe report.txt symlink is refused (rc=$rc out=$out)"
rm "$T/probesym/report.txt"
ln -s "$T/badprobe/probe.json" "$T/probesym/profile.draft.toml"
out=$(bash "$R" --probe "$T/probesym" --out "$T/draftsym" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'refusing symlink' && [ ! -e "$T/draftsym" ] && ok "probe draft symlink is refused" || bad "probe draft symlink is refused (rc=$rc out=$out)"

cat > "$T/slowadb" << 'EOF'
#!/bin/sh
cmd=$(printf '%s' "$*")
case "$cmd" in
    *getprop*)
        sleep 5
        printf 'SLOWPROP-SHOULD-NOT-APPEAR\n'
        ;;
    *)
        printf 'ok\n'
        ;;
esac
EOF
chmod +x "$T/slowadb"
mkdir -p "$T/slowbin"
ln -s "$T/slowadb" "$T/slowbin/adb"
start=$(date +%s)
if GBOS_ADB_TIMEOUT=1 PATH="$T/slowbin:$PATH" bash "$R" --out "$T/slow" --date 20261014 > /dev/null 2> "$T/slow.err"; then
    end=$(date +%s)
    elapsed=$((end - start))
    if [ "$elapsed" -lt 20 ] && ! grep -F -q 'SLOWPROP-SHOULD-NOT-APPEAR' "$T/slow/gbos-report-20261014.txt" \
        && grep -q 'timed out' "$T/slow.err"; then
        ok "adb command timeout kills only that command"
    else
        bad "adb command timeout kills only that command (elapsed=$elapsed)"
    fi
else
    bad "adb command timeout run"
    cat "$T/slow.err"
fi

# Round-2 reproductions. Each fixture failed on the previous script and must pass here.
if bash "$R" --fixture "$FIX/r2/raw-shapes" --out "$T/raw-shapes" --date 20261014 > /dev/null; then
    rs="$T/raw-shapes"
    ! grep -R -F -q -- 'RawWifiOwner777' "$rs" && ! grep -R -F -q -- 'RawWifiScan777' "$rs" \
        && ! grep -R -F -q -- 'RawWifiLast777' "$rs" && ! grep -R -F -q -- 'RawBattery777' "$rs" \
        && ! grep -R -F -q -- 'RawOwner777' "$rs" \
        && ok "raw-only shapes are not fields" || bad "raw-only shapes are not fields"
else
    bad "raw-only shapes are not fields"
fi
out=$(bash "$R" --include-raw --fixture "$FIX/r2/raw-shapes" --out "$T/raw-shapes-flag" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -e "$T/raw-shapes-flag" ] && ok "raw-shapes fixture refuses --include-raw" \
    || bad "raw-shapes fixture refuses --include-raw (rc=$rc)"
one cpu-at "$FIX/r2/cpu-at"
grep -F -q 'family 6' "$T/case-cpu-at/summary.md" && grep -F -q 'model 154' "$T/case-cpu-at/summary.md" \
    && grep -F -q 'stepping 3' "$T/case-cpu-at/summary.md" \
    && ! grep -F -q 'i7-1260P' "$T/case-cpu-at/summary.md" && ! grep -F -q '@ 2.10GHz' "$T/case-cpu-at/summary.md" \
    && ! grep -F -q 'i7-1260P' "$T/case-cpu-at/gbos-report-20261014.txt" \
    && ! grep -F -q '沒有收集到' "$T/case-cpu-at/summary.md" && ! grep -F -q 'not collected' "$T/case-cpu-at/gbos-report-20261014.txt" \
    && ok "CPU row is family model stepping, not the marketing name" || bad "CPU row is family model stepping, not the marketing name"
one bios-quad "$FIX/r2/bios-quad"
! grep -F -q '1.14.0.0' "$T/case-bios-quad/summary.md" && ! grep -F -q '1.14.0.0' "$T/case-bios-quad/gbos-report-20261014.txt" \
    && grep -F -q '| 機型 | Google |' "$T/case-bios-quad/summary.md" && grep -F -q '| 韌體 | coreboot |' "$T/case-bios-quad/summary.md" \
    && ok "BIOS version text is absent and the vendor enum remains" || bad "BIOS version text is absent and the vendor enum remains"
one bios-date "$FIX/r2/bios-date"
! grep -F -q '10/01/2026' "$T/case-bios-date/summary.md" && ! grep -F -q '14505.722.0' "$T/case-bios-date/summary.md" \
    && ! grep -F -q '10/01/2026' "$T/case-bios-date/gbos-report-20261014.txt" \
    && grep -F -q '| 韌體 | coreboot |' "$T/case-bios-date/summary.md" \
    && ok "SMBIOS date and dotted BIOS version are absent" || bad "SMBIOS date and dotted BIOS version are absent"
one swift-timeout "$FIX/r2/swift-timeout"
grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/case-swift-timeout/summary.md" \
    && ! grep -F -q '沒有原生' "$T/case-swift-timeout/summary.md" \
    && ok "SwiftShader plus an unread DRM card is unknown" || bad "SwiftShader plus an unread DRM card is unknown"
one swift-denied "$FIX/r2/swift-denied"
grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/case-swift-denied/summary.md" \
    && ! grep -F -q '沒有原生' "$T/case-swift-denied/summary.md" \
    && ok "SwiftShader plus permission denied is unknown" || bad "SwiftShader plus permission denied is unknown"
one gpu-partial "$FIX/r2/gpu-partial"
grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/case-gpu-partial/summary.md" \
    && ! grep -F -q '沒有原生' "$T/case-gpu-partial/summary.md" \
    && ok "a shim plus an unread card is unknown" || bad "a shim plus an unread card is unknown"
one gpu-real-unread "$FIX/r2/gpu-real-unread"
grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/case-gpu-real-unread/summary.md" \
    && ! grep -F -q '已綁定' "$T/case-gpu-real-unread/summary.md" \
    && ! grep -F -q '沒有原生' "$T/case-gpu-real-unread/summary.md" \
    && ok "a real driver plus an unread card is unknown" || bad "a real driver plus an unread card is unknown"
one gpu-transport "$FIX/r2/gpu-transport"
grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/case-gpu-transport/summary.md" \
    && ! grep -F -q '沒有原生' "$T/case-gpu-transport/summary.md" \
    && ! grep -F -q 'REVIEWUSB777' "$T/case-gpu-transport/gbos-report-20261014.txt" \
    && ok "a transport error is unknown, not no native driver" || bad "a transport error is unknown, not no native driver"
one wifi-now "$FIX/r2/wifi-now"
grep -F -q '| Wi-Fi | 已連線 |' "$T/case-wifi-now/summary.md" && ! grep -F -q '已關閉' "$T/case-wifi-now/summary.md" \
    && ok "current COMPLETED is not cleared by a later Wi-Fi is disabled" || bad "current COMPLETED is not cleared by a later Wi-Fi is disabled"
one wifi-history-last "$FIX/r2/wifi-history-last"
grep -F -q '| Wi-Fi | 已開啟、未連線 |' "$T/case-wifi-history-last/summary.md" \
    && ! grep -F -q '已連線' "$T/case-wifi-history-last/summary.md" && ! grep -F -q 'OldCafe9f3a' "$T/case-wifi-history-last/summary.md" \
    && ok "a historical mWifiInfo does not mean connected" || bad "a historical mWifiInfo does not mean connected"
one wifi-enabled-only "$FIX/r2/wifi-enabled-only"
grep -F -q '已開啟，連線狀態未知' "$T/case-wifi-enabled-only/summary.md" \
    && ! grep -F -q '未連線' "$T/case-wifi-enabled-only/summary.md" \
    && ok "enabled alone does not claim the link is down" || bad "enabled alone does not claim the link is down"
one empty-board "$FIX/r2/empty-board"
grep -F -q '| 板名 | 未知 |' "$T/case-empty-board/summary.md" && grep -F -q '| 機型 | 未知 |' "$T/case-empty-board/summary.md" \
    && ! grep -F -q 'lapis' "$T/case-empty-board/summary.md" && ! grep -F -q 'ASUS' "$T/case-empty-board/gbos-report-20261014.txt" \
    && ok "empty DMI board does not fall back to ro.product.board" || bad "empty DMI board does not fall back to ro.product.board"
one uname-host "$FIX/r2/uname-host"
grep -F -q '| 核心 | other |' "$T/case-uname-host/summary.md" \
    && ! grep -F -q '6.6.30-lapis' "$T/case-uname-host/summary.md" \
    && ! grep -F -q 'alice-2.4-laptop' "$T/case-uname-host/summary.md" && ! grep -F -q '2.4-laptop' "$T/case-uname-host/gbos-report-20261014.txt" \
    && ok "a hostname is not the kernel, and a foreign release is other" || bad "a hostname is not the kernel, and a foreign release is other"
one serial-build "$FIX/r2/serial-build"
! grep -F -q 'KANO987654321' "$T/case-serial-build/summary.md" && ! grep -F -q 'KANO987654321' "$T/case-serial-build/gbos-report-20261014.txt" \
    && grep -F -q '| 建置版本 | 未知 |' "$T/case-serial-build/summary.md" \
    && ok "a getprop serial inside the build id is unknown" || bad "a getprop serial inside the build id is unknown"
one serial-dmi "$FIX/r2/serial-dmi"
! grep -F -q 'REVIEWUSB777' "$T/case-serial-dmi/summary.md" && ! grep -F -q 'REVIEWUSB777' "$T/case-serial-dmi/gbos-report-20261014.txt" \
    && ok "a known serial is rejected in the DMI model" || bad "a known serial is rejected in the DMI model"
one nick-dumpsys "$FIX/r2/nick-dumpsys"
grep -F -q '觸控板：04f3:2d5b（i2c）' "$T/case-nick-dumpsys/summary.md" && ! grep -F -q 'Alice' "$T/case-nick-dumpsys/summary.md" \
    && ! grep -F -q 'Alice' "$T/case-nick-dumpsys/gbos-report-20261014.txt" \
    && ok "a nickname that contains a vid:pid is not copied" || bad "a nickname that contains a vid:pid is not copied"
one nick-proc "$FIX/r2/nick-proc"
grep -F -q '觸控板：04f3:1234（bluetooth）' "$T/case-nick-proc/summary.md" && ! grep -F -q 'ReviewOwner777' "$T/case-nick-proc/gbos-report-20261014.txt" \
    && ok "a proc nickname that contains a vid:pid is not copied" || bad "a proc nickname that contains a vid:pid is not copied"
if bash "$R" --probe "$FIX/r2/probe-email" --out "$T/probe-email" --date 20261014 > /dev/null; then
    pe="$T/probe-email"
    ! grep -R -F -q -- 'leak.person@example.test' "$pe" && ! grep -R -F -q -- '02:99:88:77:66:55' "$pe" \
        && ! grep -R -F -q -- 'review.owner@example.test' "$pe" && ! grep -R -F -q -- 'REVIEW-TAX-777' "$pe" \
        && ! grep -R -F -q -- '/sdcard' "$pe" && grep -F -q '觸控板：unknown（usb）' "$pe/summary.md" \
        && ok "probe vendor and product must be 4 hex digits" || bad "probe vendor and product must be 4 hex digits"
else
    bad "probe vendor and product must be 4 hex digits"
fi
one bt-profile "$FIX/r2/bt-profile"
grep -F -q '| 藍牙 | 已關閉 |' "$T/case-bt-profile/summary.md" && ! grep -F -q '已開啟' "$T/case-bt-profile/summary.md" \
    && ok "bluetooth power comes from the Bluetooth Status block" || bad "bluetooth power comes from the Bluetooth Status block"
one bt-stopped "$FIX/r2/bt-stopped"
grep -F -q '服務已停止，開關未確認' "$T/case-bt-stopped/summary.md" && ! grep -F -q '服務執行中' "$T/case-bt-stopped/summary.md" \
    && ok "a stopped bluetooth service is not reported as running" || bad "a stopped bluetooth service is not reported as running"
one sleep-fail "$FIX/r2/sleep-fail"
grep -F -q '| 休眠／喚醒 | 未知 |' "$T/case-sleep-fail/summary.md" && ! grep -F -q '失敗 9' "$T/case-sleep-fail/summary.md" \
    && ! grep -F -q 'fail 9' "$T/case-sleep-fail/gbos-report-20261014.txt" \
    && ok "a failed suspend counter is unknown" || bad "a failed suspend counter is unknown"
one ver-placeholder "$FIX/r2/ver-placeholder"
[ -f "$T/case-ver-placeholder/summary.md" ] && ! grep -F -q '[[ver0]]' "$T/case-ver-placeholder/summary.md" \
    && ! grep -F -q 'Traceback' "$T/case-ver-placeholder/gbos-report-20261014.txt" \
    && ok "a placeholder token does not crash the report" || bad "a placeholder token does not crash the report"
if bash "$R" --probe "$FIX/r2/audio-type" --out "$T/audio-type" --date 20261014 > "$T/audio-type.out" 2> "$T/audio-type.err"; then
    ! grep -q 'Traceback' "$T/audio-type.err" && ! grep -F -q '| 音效卡 | 42 |' "$T/audio-type/summary.md" \
        && ok "a non-list card_ids value does not crash" || bad "a non-list card_ids value does not crash"
else
    bad "a non-list card_ids value does not crash"
    cat "$T/audio-type.err"
fi
mkdir -p "$T/real-target"
ln -s "$T/real-target" "$T/link-parent"
out=$(bash "$R" --fixture "$FIX/swiftshader" --out "$T/link-parent/newreport" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -e "$T/real-target/newreport" ] && ok "--out parent symlink is refused" || bad "--out parent symlink is refused (rc=$rc)"
mkdir -p "$T/realprobe"
printf '%s\n' '{"dmi":{"sys_vendor":"Google","product_name":"Kano"}}' > "$T/realprobe/probe.json"
ln -s "$T/realprobe" "$T/probelink"
out=$(bash "$R" --probe "$T/probelink/" --out "$T/probeslash" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'symlink' && [ ! -e "$T/probeslash" ] \
    && ok "trailing-slash --probe symlink is refused" || bad "trailing-slash --probe symlink is refused (rc=$rc)"
ln -s "$FIX/kano" "$T/fixlink"
out=$(bash "$R" --fixture "$T/fixlink/" --out "$T/fixslash" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && printf '%s\n' "$out" | grep -q 'symlink' && [ ! -e "$T/fixslash" ] \
    && ok "trailing-slash --fixture symlink is refused" || bad "trailing-slash --fixture symlink is refused (rc=$rc)"

# Round-3 reproductions. Each one failed on the previous script.
one driver-secret "$FIX/r3/driver-secret"
! grep -R -F -q -- 'r3owner7f3a' "$T/case-driver-secret" \
    && grep -F -q '8086:46a6' "$T/case-driver-secret/summary.md" \
    && ok "an unknown GPU driver that is a serial is not printed" || bad "an unknown GPU driver that is a serial is not printed"
one driver-other "$FIX/r3/driver-other"
! grep -R -F -q -- 'notadriver' "$T/case-driver-other" \
    && grep -F -q 'driver other' "$T/case-driver-other/gbos-report-20261014.txt" \
    && grep -F -q 'unknown (8086:46a6, other)' "$T/case-driver-other/gbos-report-20261014.txt" \
    && grep -F -q '8086:46a6' "$T/case-driver-other/summary.md" \
    && ! grep -F -q 'driver unread' "$T/case-driver-other/gbos-report-20261014.txt" \
    && ok "a GPU driver outside the enumeration is other" || bad "a GPU driver outside the enumeration is other"
one sound-other "$FIX/r3/sound-other"
! grep -R -F -q -- 'WeirdCodec' "$T/case-sound-other" \
    && grep -F -q '| 音效卡 | 未知 |' "$T/case-sound-other/summary.md" \
    && ! grep -F -q '| 音效卡 | other |' "$T/case-sound-other/summary.md" \
    && ok "a sound card name is not reported" || bad "a sound card name is not reported"
one dri-empty "$FIX/r3/dri-empty"
grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/case-dri-empty/summary.md" \
    && ! grep -F -q '沒有原生' "$T/case-dri-empty/summary.md" \
    && ok "an empty DRM listing is unknown" || bad "an empty DRM listing is unknown"
one sound-email "$FIX/r3/sound-email"
! grep -R -F -q -- 'canary.sound' "$T/case-sound-email" \
    && ! grep -R -F -q -- 'example.test' "$T/case-sound-email" \
    && ok "a sound card email is not truncated into the report" || bad "a sound card email is not truncated into the report"
one kernel-email "$FIX/r3/kernel-email"
! grep -R -F -q -- 'leak.person' "$T/case-kernel-email" \
    && ! grep -R -F -q -- 'example.test' "$T/case-kernel-email" \
    && ok "a kernel token is validated whole" || bad "a kernel token is validated whole"
one embed-imei "$FIX/r3/embed-imei"
! grep -R -F -q -- '490154203237518' "$T/case-embed-imei" \
    && grep -F -q '| 建置版本 | 未知 |' "$T/case-embed-imei/summary.md" \
    && ok "an IMEI embedded in the build id is unknown" || bad "an IMEI embedded in the build id is unknown"
one case-serial "$FIX/r3/case-serial"
! grep -R -F -i -q -- 'r3desk9f3a' "$T/case-case-serial" \
    && ok "a serial matches regardless of case" || bad "a serial matches regardless of case"
one card4-shim "$FIX/r3/card4-shim"
grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/case-card4-shim/summary.md" \
    && ! grep -F -q '沒有原生' "$T/case-card4-shim/summary.md" \
    && ok "card4 unread plus a shim is unknown" || bad "card4 unread plus a shim is unknown"
one card4-real "$FIX/r3/card4-real"
grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/case-card4-real/summary.md" \
    && grep -F -q '| GPU PCI | 未知 |' "$T/case-card4-real/summary.md" \
    && ! grep -F -q '已綁定' "$T/case-card4-real/summary.md" \
    && ! grep -F -q 'real-gpu' "$T/case-card4-real/summary.md" \
    && ok "card4 unread plus a real GPU is unknown" || bad "card4 unread plus a real GPU is unknown"
one wifi-history-only "$FIX/r3/wifi-history-only"
grep -F -q '連線狀態未知' "$T/case-wifi-history-only/summary.md" \
    && ! grep -F -q '| Wi-Fi | 已連線' "$T/case-wifi-history-only/summary.md" \
    && ! grep -R -F -q -- 'R3OldCafe9F3A' "$T/case-wifi-history-only" \
    && ok "history alone is not a current connection" || bad "history alone is not a current connection"
one wifi-contradict "$FIX/r3/wifi-contradict"
grep -F -q '| Wi-Fi | 未知 |' "$T/case-wifi-contradict/summary.md" \
    && ! grep -F -q '已連線' "$T/case-wifi-contradict/summary.md" \
    && ok "supplicant DISCONNECTED contradicts curState ConnectedState" || bad "supplicant DISCONNECTED contradicts curState ConnectedState"
one wifi-contradict-rev "$FIX/r3/wifi-contradict-rev"
grep -F -q '| Wi-Fi | 未知 |' "$T/case-wifi-contradict-rev/summary.md" \
    && ! grep -F -q '已連線' "$T/case-wifi-contradict-rev/summary.md" \
    && ok "supplicant COMPLETED contradicts curState DisconnectedState" || bad "supplicant COMPLETED contradicts curState DisconnectedState"
one wifi-radio-both "$FIX/r3/wifi-radio-both"
grep -F -q '| Wi-Fi | 未知 |' "$T/case-wifi-radio-both/summary.md" \
    && ok "enabled and disabled in the current window are unknown" || bad "enabled and disabled in the current window are unknown"
if bash "$R" --fixture "$FIX/r3/same-dmi" --probe "$FIX/r3/same-dmi-probe" --out "$T/same-dmi" --date 20261014 > /dev/null; then
    sd="$T/same-dmi"
    grep -F -q '已省略（probe 與這台裝置不是同一台）' "$sd/summary.md" \
        && grep -F -q '| 機型 | Google |' "$sd/summary.md" \
        && grep -F -q '8086:46a6' "$sd/summary.md" \
        && ! grep -R -F -q -- '1002:150e' "$sd" \
        && ! grep -R -F -q -- 'AuthenticAMD' "$sd" \
        && ! grep -F -q 'Secure Boot' "$sd/summary.md" \
        && ! grep -F -q 'UEFI' "$sd/summary.md" \
        && ! grep -F -q 'Kano' "$sd/summary.md" \
        && ok "same DMI with a different CPU or GPU omits probe facts" || bad "same DMI with a different CPU or GPU omits probe facts"
else
    bad "same DMI with a different CPU or GPU omits probe facts"
fi
if bash "$R" --probe "$FIX/r3/dmi-space" --out "$T/dmi-space" --date 20261014 > /dev/null; then
    grep -F -q '| 機型 | Dell |' "$T/dmi-space/summary.md" \
        && grep -F -q '| 板名 | other |' "$T/dmi-space/summary.md" \
        && grep -F -q '| 韌體 | AMI |' "$T/dmi-space/summary.md" \
        && grep -F -q 'AuthenticAMD family 26 model 96 stepping 0' "$T/dmi-space/summary.md" \
        && ! grep -F -q 'XPS' "$T/dmi-space/summary.md" \
        && ! grep -F -q 'MiniPC' "$T/dmi-space/summary.md" \
        && ! grep -F -q 'American Megatrends' "$T/dmi-space/summary.md" \
        && ! grep -F -q 'Radeon' "$T/dmi-space/summary.md" \
        && ! grep -F -q '1.05' "$T/dmi-space/summary.md" \
        && ! grep -F -q '06/01/2026' "$T/dmi-space/summary.md" \
        && ! grep -F -q 'Dell Inc.' "$T/dmi-space/gbos-report-20261014.txt" \
        && ok "DMI vendors and CPU numbers stay, and the free-text names do not" || bad "DMI vendors and CPU numbers stay, and the free-text names do not"
else
    bad "DMI names may contain spaces and commas, and a CPU may contain w/"
fi
if bash "$R" --probe "$FIX/r3/input-dual" --out "$T/input-dual" --date 20261014 > /dev/null; then
    grep -F -q '觸控螢幕：04f3:2c82' "$T/input-dual/summary.md" \
        && grep -F -q '觸控筆：04f3:2c82' "$T/input-dual/summary.md" \
        && grep -F -q '觸控板：04f3:2c82（usb）' "$T/input-dual/summary.md" \
        && grep -F -q '觸控板：04f3:2c82（i2c）' "$T/input-dual/summary.md" \
        && ok "touchscreen and stylus with one vid:pid are both kept" || bad "touchscreen and stylus with one vid:pid are both kept"
else
    bad "touchscreen and stylus with one vid:pid are both kept"
fi
if bash "$R" --probe "$FIX/r3/json-type" --out "$T/json-type" --date 20261014 > /dev/null; then
    ! grep -R -F -q -- 'True' "$T/json-type" && ! grep -R -F -q -- '123456' "$T/json-type" \
        && grep -F -q 'model: unknown [probe]' "$T/json-type/gbos-report-20261014.txt" \
        && grep -F -q 'board: unknown [probe]' "$T/json-type/gbos-report-20261014.txt" \
        && grep -F -q 'firmware: unknown [probe]' "$T/json-type/gbos-report-20261014.txt" \
        && grep -F -q 'cpu: unknown [probe]' "$T/json-type/gbos-report-20261014.txt" \
        && ok "a non-string DMI value is unknown" || bad "a non-string DMI value is unknown"
else
    bad "a non-string DMI value is unknown"
fi
one short-user "$FIX/r3/short-user"
! grep -R -F -q -- 'alice' "$T/case-short-user" \
    && ok "a short user name is not copied into the build id" || bad "a short user name is not copied into the build id"
one dri-imei "$FIX/r3/dri-imei"
! grep -R -F -q -- '490154203237518' "$T/case-dri-imei" \
    && grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/case-dri-imei/summary.md" \
    && ok "a DRM node that embeds an IMEI is not printed" || bad "a DRM node that embeds an IMEI is not printed"
one sleep-imei "$FIX/r3/sleep-imei"
! grep -R -F -q -- '490154203237518' "$T/case-sleep-imei" \
    && grep -F -q '| 休眠／喚醒 | 未知 |' "$T/case-sleep-imei/summary.md" \
    && ok "a suspend counter that is an IMEI is unknown" || bad "a suspend counter that is an IMEI is unknown"
if bash "$R" --probe "$FIX/r3/iface-mac" --out "$T/iface-mac" --date 20261014 > /dev/null; then
    ! grep -R -F -q -- '001122334455' "$T/iface-mac" && ! grep -R -F -q -- 'wlx001122334455' "$T/iface-mac" \
        && ok "a wlx MAC interface name is not a Wi-Fi field" || bad "a wlx MAC interface name is not a Wi-Fi field"
else
    bad "a wlx MAC interface name is not a Wi-Fi field"
fi
if bash "$R" --probe "$FIX/r3/ipv6-input" --out "$T/ipv6-input" --date 20261014 > /dev/null; then
    ! grep -R -F -q -- '2001:0db8' "$T/ipv6-input" \
        && ok "an IPv6 address is not parsed as a vid:pid" || bad "an IPv6 address is not parsed as a vid:pid"
else
    bad "an IPv6 address is not parsed as a vid:pid"
fi
one cisco-bios "$FIX/r3/cisco-bios"
! grep -R -F -q -- '0299.8877.6655' "$T/case-cisco-bios" \
    && ok "a Cisco MAC is not a BIOS version" || bad "a Cisco MAC is not a BIOS version"
one dotmac-bios "$FIX/r3/dotmac-bios"
! grep -R -F -q -- '02.99.88.77.66.55' "$T/case-dotmac-bios" \
    && ok "a dotted MAC is not a BIOS version" || bad "a dotted MAC is not a BIOS version"
one pad-ip "$FIX/r3/pad-ip"
! grep -R -F -q -- '192.168.001.001' "$T/case-pad-ip" \
    && ok "a zero-padded IPv4 address is not a model name" || bad "a zero-padded IPv4 address is not a model name"
one aid-cpu "$FIX/r3/aid-cpu"
! grep -R -F -q -- 'deadbeefcafebabe' "$T/case-aid-cpu" \
    && ok "an Android id embedded in a CPU name is unknown" || bad "an Android id embedded in a CPU name is unknown"
one bt-nested "$FIX/r3/bt-nested"
grep -F -q '| 藍牙 | 已關閉 |' "$T/case-bt-nested/summary.md" \
    && ! grep -F -q '已開啟' "$T/case-bt-nested/summary.md" \
    && ok "a nested profile enabled line does not turn Bluetooth on" || bad "a nested profile enabled line does not turn Bluetooth on"
one bt-turning "$FIX/r3/bt-turning"
grep -F -q '開啟中' "$T/case-bt-turning/summary.md" \
    && ! grep -F -q '| 藍牙 | 已開啟 |' "$T/case-bt-turning/summary.md" \
    && ! grep -F -q '| 藍牙 | 已關閉 |' "$T/case-bt-turning/summary.md" \
    && ok "TURNING_ON is not reported as on" || bad "TURNING_ON is not reported as on"
one bt-turning-off "$FIX/r3/bt-turning-off"
grep -F -q '關閉中' "$T/case-bt-turning-off/summary.md" \
    && ! grep -F -q '| 藍牙 | 已開啟 |' "$T/case-bt-turning-off/summary.md" \
    && ! grep -F -q '| 藍牙 | 已關閉 |' "$T/case-bt-turning-off/summary.md" \
    && ok "TURNING_OFF is not reported as off" || bad "TURNING_OFF is not reported as off"
if bash "$R" --fixture "$FIX/r3/draft-lower" --probe "$FIX/r3/draft-lower-probe" --out "$T/draft-lower" --date 20261014 > /dev/null; then
    ! grep -R -F -i -q -- 'reviewusb777' "$T/draft-lower" \
        && ok "a lowercased draft id does not keep an uppercase serial" || bad "a lowercased draft id does not keep an uppercase serial"
else
    bad "a lowercased draft id does not keep an uppercase serial"
fi

# Explicit offline sources must not fall through to adb.
: > "$T/adb.log"
out=$(PATH="$T:$PATH" bash "$R" --probe / --out "$T/proot" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -s "$T/adb.log" ] && [ ! -e "$T/proot" ] \
    && ok "--probe / does not call adb" || bad "--probe / does not call adb (rc=$rc)"
: > "$T/adb.log"
out=$(PATH="$T:$PATH" bash "$R" --fixture / --out "$T/fixroot" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -s "$T/adb.log" ] && [ ! -e "$T/fixroot" ] \
    && ok "--fixture / does not call adb" || bad "--fixture / does not call adb (rc=$rc)"
: > "$T/adb.log"
out=$(PATH="$T:$PATH" bash "$R" --probe "" --out "$T/pempty" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -s "$T/adb.log" ] && [ ! -e "$T/pempty" ] \
    && ok "--probe empty does not call adb" || bad "--probe empty does not call adb (rc=$rc)"
: > "$T/adb.log"
out=$(PATH="$T:$PATH" bash "$R" --fixture "" --out "$T/fempty" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -s "$T/adb.log" ] && [ ! -e "$T/fempty" ] \
    && ok "--fixture empty does not call adb" || bad "--fixture empty does not call adb (rc=$rc)"
mkdir -p "$T/dot-real"
printf '%s\n' '{"dmi":{"sys_vendor":"Google","product_name":"Kano"}}' > "$T/dot-real/probe.json"
ln -s "$T/dot-real" "$T/dot-link"
out=$(bash "$R" --probe "$T/dot-link/." --out "$T/dotprobe" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -e "$T/dotprobe" ] && ok "--probe link/. is refused" || bad "--probe link/. is refused (rc=$rc)"
out=$(bash "$R" --fixture "$FIX/kano" --out "$T/dot-link/./written" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -e "$T/dot-real/written" ] && ok "--out link/./written is refused" || bad "--out link/./written is refused (rc=$rc)"

mkdir -p "$T/trapbin"
cat > "$T/trapbin/adb" << 'EOF'
#!/bin/sh
nfile=${GBOS_TRAP_COUNT:?}
n=0
if [ -f "$nfile" ]; then
    n=$(cat "$nfile" 2>/dev/null || printf '0')
fi
case "$n" in
    ''|*[!0-9]*) n=0 ;;
esac
n=$((n + 1))
printf '%s\n' "$n" > "$nfile"
if [ "$n" = 1 ]; then
    trap '' TERM
    sleep 30
fi
printf 'ok\n'
EOF
chmod +x "$T/trapbin/adb"
start=$(date +%s)
if GBOS_ADB_TIMEOUT=1 GBOS_TRAP_COUNT="$T/trapcount" PATH="$T/trapbin:$PATH" bash "$R" --out "$T/trapout" --date 20261014 > /dev/null 2> "$T/trap.err"; then
    end=$(date +%s)
    elapsed=$((end - start))
    if [ "$elapsed" -lt 20 ] && grep -q 'timed out' "$T/trap.err"; then
        ok "a command that ignores TERM is still killed"
    else
        bad "a command that ignores TERM is still killed (elapsed=$elapsed)"
    fi
else
    bad "a command that ignores TERM is still killed"
    cat "$T/trap.err"
fi

for exe in install/gbos-report.sh tests/report/report-test.sh
do
    if [ -x "$ROOT/$exe" ]; then ok "executable $exe"
    else bad "executable $exe"; fi
done

# Producer keeps Vendor and Product, and the report prints the id rather than the name.
mkdir -p "$T/stylus-root/proc/bus/input" "$T/stylus-root/sys/class/dmi/id" "$T/stylus-root/proc"
printf '%s\n' 'I: Bus=0018 Vendor=056a Product=1234 Version=0001' 'N: Name="Wayacom I2C Stylus"' 'H: Handlers=event9' '' > "$T/stylus-root/proc/bus/input/devices"
printf '%s\n' 'Google' > "$T/stylus-root/sys/class/dmi/id/sys_vendor"
printf '%s\n' 'Kano' > "$T/stylus-root/sys/class/dmi/id/product_name"
if bash "$ROOT/install/gbos-probe.sh" --root "$T/stylus-root" --out "$T/stylus-probe" > /dev/null; then
    grep -F -q '"vendor": "056a"' "$T/stylus-probe/probe.json" && grep -F -q '"product": "1234"' "$T/stylus-probe/probe.json" \
        && ok "the probe stores input vendor and product" || bad "the probe stores input vendor and product"
    if bash "$R" --probe "$T/stylus-probe" --out "$T/stylus-report" --date 20261014 > /dev/null; then
        grep -F -q '觸控筆：056a:1234' "$T/stylus-report/summary.md" \
            && ! grep -F -q 'Wayacom' "$T/stylus-report/summary.md" \
            && ok "a stylus id survives the probe to the report" || bad "a stylus id survives the probe to the report"
    else
        bad "a stylus id survives the probe to the report"
    fi
else
    bad "the probe stores input vendor and product"
fi

# Round-4 reproductions. Each one leaked or merged on the previous script.
r4() {
    if bash "$R" --fixture "$FIX/r4/$1" --out "$T/r4-$1" --date 20261014 > /dev/null; then
        ok "ran r4 $1"
    else
        bad "ran r4 $1"
    fi
}
r4 mac-underscore
! grep -R -F -q -- '02_aa_bb_cc_dd_11' "$T/r4-mac-underscore" \
    && ! grep -R -F -q -- '02_aa_bb' "$T/r4-mac-underscore" \
    && grep -F -q '| 機型 | Google |' "$T/r4-mac-underscore/summary.md" \
    && grep -F -q 'redactions: 0' "$T/r4-mac-underscore/gbos-report-20261014.txt" \
    && ok "an underscore MAC is not a model" || bad "an underscore MAC is not a model"
r4 mac-space
! grep -R -F -q -- '02 aa bb cc dd 11' "$T/r4-mac-space" \
    && ! grep -R -F -q -- '02 aa bb' "$T/r4-mac-space" \
    && grep -F -q '| 機型 | Google |' "$T/r4-mac-space/summary.md" \
    && ok "a space-separated MAC is not a model" || bad "a space-separated MAC is not a model"
r4 mac-comma
! grep -R -F -q -- '02,aa,bb,cc,dd,11' "$T/r4-mac-comma" \
    && ! grep -R -F -q -- '02,aa,bb' "$T/r4-mac-comma" \
    && ok "a comma MAC is not a model" || bad "a comma MAC is not a model"
r4 cisco-space
! grep -R -F -q -- '02de adbe ef11' "$T/r4-cisco-space" \
    && ! grep -R -F -q -- '02de adbe' "$T/r4-cisco-space" \
    && ok "a Cisco MAC with spaces is not a model" || bad "a Cisco MAC with spaces is not a model"
r4 mac-plus
! grep -R -F -q -- '02+aa+bb+cc+dd+11' "$T/r4-mac-plus" \
    && ! grep -R -F -q -- '02+aa+bb' "$T/r4-mac-plus" \
    && grep -F -q '8086:46a6' "$T/r4-mac-plus/summary.md" \
    && grep -F -q 'CL3B.260622.271.R1' "$T/r4-mac-plus/summary.md" \
    && grep -F -q 'redactions: 0' "$T/r4-mac-plus/gbos-report-20261014.txt" \
    && ok "a plus MAC is dropped and PCI and the build id stay" || bad "a plus MAC is dropped and PCI and the build id stay"
r4 mac-plus-build
! grep -R -F -q -- '02+aa+bb+cc+dd+11' "$T/r4-mac-plus-build" \
    && grep -F -q '| 建置版本 | 未知 |' "$T/r4-mac-plus-build/summary.md" \
    && ok "a plus MAC is not a build id" || bad "a plus MAC is not a build id"
r4 at-email
! grep -R -F -q -- 'alice at example.com' "$T/r4-at-email" \
    && ! grep -R -F -q -- 'example.com' "$T/r4-at-email" \
    && ! grep -R -F -q -- 'alice at' "$T/r4-at-email" \
    && grep -F -q '| 機型 | Google |' "$T/r4-at-email/summary.md" \
    && ok "an at-email is not a model" || bad "an at-email is not a model"
r4 at-split
! grep -R -F -q -- 'alice at' "$T/r4-at-split" \
    && ! grep -R -F -q -- 'example.com' "$T/r4-at-split" \
    && grep -F -q '| 機型 | other |' "$T/r4-at-split/summary.md" \
    && ok "an email split across vendor and product is not reassembled" || bad "an email split across vendor and product is not reassembled"
r4 cpu-at-email
grep -F -q 'GenuineIntel family 6 model 154 stepping 3' "$T/r4-cpu-at-email/summary.md" \
    && ! grep -R -F -q -- '(at)' "$T/r4-cpu-at-email" \
    && ! grep -R -F -q -- 'example.com' "$T/r4-cpu-at-email" \
    && ok "a CPU (at) email is dropped and the numbers stay" || bad "a CPU (at) email is dropped and the numbers stay"
r4 grouped-imei
! grep -R -F -q -- '4901542' "$T/r4-grouped-imei" \
    && ! grep -R -F -q -- '0323751' "$T/r4-grouped-imei" \
    && grep -F -q '| 機型 | Google |' "$T/r4-grouped-imei/summary.md" \
    && ok "a grouped IMEI is not a model" || bad "a grouped IMEI is not a model"
r4 aid-space
! grep -R -F -q -- 'dead beef' "$T/r4-aid-space" \
    && ! grep -R -F -q -- 'cafe babe' "$T/r4-aid-space" \
    && ok "a split Android id is not a model" || bad "a split Android id is not a model"
r4 aid-half
! grep -R -F -q -- 'deadbeef' "$T/r4-aid-half" \
    && ! grep -R -F -q -- 'cafebabe' "$T/r4-aid-half" \
    && ok "a half Android id is not a model" || bad "a half Android id is not a model"
r4 multiline-dmi
! grep -R -F -q -- 'alice' "$T/r4-multiline-dmi" \
    && ! grep -R -F -q -- 'example.test' "$T/r4-multiline-dmi" \
    && grep -F -q '| 機型 | Google |' "$T/r4-multiline-dmi/summary.md" \
    && ok "a multiline DMI value is not the first line" || bad "a multiline DMI value is not the first line"
r4 hex-email
! grep -R -F -q -- '61 6c 69 63 65 40' "$T/r4-hex-email" \
    && grep -F -q '| 機型 | Google |' "$T/r4-hex-email/summary.md" \
    && ok "a spaced hex email is not a model" || bad "a spaced hex email is not a model"
r4 parens-name
! grep -R -F -q -- 'Latitude' "$T/r4-parens-name" \
    && ! grep -R -F -q -- '(Dell)' "$T/r4-parens-name" \
    && grep -F -q '| 機型 | Dell |' "$T/r4-parens-name/summary.md" \
    && ok "a parenthesized product name is not the model" || bad "a parenthesized product name is not the model"
r4 sleep-email
grep -F -q '| 休眠／喚醒 | 未知 |' "$T/r4-sleep-email/summary.md" \
    && ! grep -R -F -q -- '12345' "$T/r4-sleep-email" \
    && ! grep -R -F -q -- 'example.test' "$T/r4-sleep-email" \
    && ! grep -F -q '成功 12345' "$T/r4-sleep-email/summary.md" \
    && ok "a suspend counter keeps no digit prefix" || bad "a suspend counter keeps no digit prefix"
r4 bios-ip
! grep -R -F -q -- '203.0.113.44' "$T/r4-bios-ip" \
    && grep -F -q '| 韌體 | coreboot |' "$T/r4-bios-ip/summary.md" \
    && grep -F -q 'redactions: 0' "$T/r4-bios-ip/gbos-report-20261014.txt" \
    && ok "a BIOS version that is an IP address is absent" || bad "a BIOS version that is an IP address is absent"
r4 bios-pad-ip
! grep -R -F -q -- '192.168.001.001' "$T/r4-bios-pad-ip" \
    && grep -F -q '| 韌體 | coreboot |' "$T/r4-bios-pad-ip/summary.md" \
    && ok "a zero-padded IPv4 BIOS version is absent" || bad "a zero-padded IPv4 BIOS version is absent"
if bash "$R" --probe "$FIX/r4/probe-bios-ip" --out "$T/r4-probe-bios-ip" --date 20261014 > /dev/null; then
    ! grep -R -F -q -- '203.0.113.44' "$T/r4-probe-bios-ip" \
        && grep -F -q 'firmware: coreboot [probe]' "$T/r4-probe-bios-ip/gbos-report-20261014.txt" \
        && ok "a probe BIOS version that is an IP address is absent" || bad "a probe BIOS version that is an IP address is absent"
else
    bad "a probe BIOS version that is an IP address is absent"
fi
r4 wifi-disabledstate
grep -F -q '| Wi-Fi | 未知 |' "$T/r4-wifi-disabledstate/summary.md" \
    && ! grep -F -q '已關閉' "$T/r4-wifi-disabledstate/summary.md" \
    && ! grep -F -q '已連線' "$T/r4-wifi-disabledstate/summary.md" \
    && ok "enabled plus DisabledState is unknown" || bad "enabled plus DisabledState is unknown"
r4 wifi-two-cur
grep -F -q '| Wi-Fi | 未知 |' "$T/r4-wifi-two-cur/summary.md" \
    && ! grep -F -q '已連線' "$T/r4-wifi-two-cur/summary.md" \
    && ok "two curState values are unknown" || bad "two curState values are unknown"
r4 wifi-two-supp
grep -F -q '| Wi-Fi | 未知 |' "$T/r4-wifi-two-supp/summary.md" \
    && ! grep -F -q '已連線' "$T/r4-wifi-two-supp/summary.md" \
    && ok "two supplicant states are unknown" || bad "two supplicant states are unknown"
r4 wifi-far
grep -F -q '| Wi-Fi | 未知 |' "$T/r4-wifi-far/summary.md" \
    && ! grep -F -q '已連線' "$T/r4-wifi-far/summary.md" \
    && ok "a later supplicant state is still in the current window" || bad "a later supplicant state is still in the current window"
r4 wifi-op-down
grep -F -q '| Wi-Fi | 未知 |' "$T/r4-wifi-op-down/summary.md" \
    && ! grep -F -q '已連線' "$T/r4-wifi-op-down/summary.md" \
    && ok "operstate down contradicts a link-up snapshot" || bad "operstate down contradicts a link-up snapshot"
r4 wifi-op-up
grep -F -q '| Wi-Fi | 未知 |' "$T/r4-wifi-op-up/summary.md" \
    && ! grep -F -q '已關閉' "$T/r4-wifi-op-up/summary.md" \
    && ok "operstate up contradicts a hard-down snapshot" || bad "operstate up contradicts a hard-down snapshot"
r4 bt-turn-enabled
grep -F -q '關閉中' "$T/r4-bt-turn-enabled/summary.md" \
    && ! grep -F -q '| 藍牙 | 已開啟 |' "$T/r4-bt-turn-enabled/summary.md" \
    && ok "TURNING_OFF wins over enabled true" || bad "TURNING_OFF wins over enabled true"
r4 bt-turn-reverse
grep -F -q '開啟中' "$T/r4-bt-turn-reverse/summary.md" \
    && ! grep -F -q '| 藍牙 | 已關閉 |' "$T/r4-bt-turn-reverse/summary.md" \
    && ok "TURNING_ON wins over enabled false" || bad "TURNING_ON wins over enabled false"
r4 bt-stable-off
grep -F -q '| 藍牙 | 未知 |' "$T/r4-bt-stable-off/summary.md" \
    && ! grep -F -q '已開啟' "$T/r4-bt-stable-off/summary.md" \
    && ok "enabled true plus state OFF is unknown" || bad "enabled true plus state OFF is unknown"
r4 model-code
grep -F -q '| 機型 | Lenovo |' "$T/r4-model-code/summary.md" \
    && grep -F -q '| 韌體 | AMI |' "$T/r4-model-code/summary.md" \
    && ! grep -R -F -q -- '20XW0011TW' "$T/r4-model-code" \
    && ! grep -R -F -q -- 'N32ET84W' "$T/r4-model-code" \
    && ! grep -R -F -q -- '192-168-1-50' "$T/r4-model-code" \
    && grep -F -q 'redactions: 0' "$T/r4-model-code/gbos-report-20261014.txt" \
    && ok "public model codes are not emitted" || bad "public model codes are not emitted"
r4 dash-ip
! grep -R -F -q -- '192-168-1-50' "$T/r4-dash-ip" \
    && grep -F -q '| 建置版本 | 未知 |' "$T/r4-dash-ip/summary.md" \
    && grep -F -q '| 核心 | other |' "$T/r4-dash-ip/summary.md" \
    && ok "a dashed IPv4 address is not a build id or a kernel" || bad "a dashed IPv4 address is not a build id or a kernel"
r4 kernel-g12
grep -F -q '| 核心 | other |' "$T/r4-kernel-g12/summary.md" \
    && ! grep -R -F -q -- '0123456789ab' "$T/r4-kernel-g12" \
    && ! grep -R -F -q -- 'g0123456789ab' "$T/r4-kernel-g12" \
    && ok "a 12-hex localversion is other, not a free grammar" || bad "a 12-hex localversion is other, not a free grammar"
r4 kernel-g8
grep -F -q '| 核心 | other |' "$T/r4-kernel-g8/summary.md" \
    && ! grep -R -F -q -- 'g1234abcd' "$T/r4-kernel-g8" \
    && ok "a short localversion is not printed" || bad "a short localversion is not printed"

# Same numbers, different marketing names, still one machine. Vendor-only CPU and an unread card are not.
rm -rf "$T/names-fix" "$T/names-probe" "$T/vendor-fix" "$T/vendor-probe" "$T/step-fix" "$T/step-probe" "$T/c4-fix" "$T/c4-probe" "$T/draft-fix" "$T/draft-probe"
cp -a "$FIX/kano" "$T/names-fix"
cp -a "$FIX/kano-probe" "$T/names-probe"
cp -a "$FIX/kano" "$T/vendor-fix"
cp -a "$FIX/kano-probe" "$T/vendor-probe"
cp -a "$FIX/kano" "$T/step-fix"
cp -a "$FIX/kano-probe" "$T/step-probe"
cp -a "$FIX/kano" "$T/c4-fix"
cp -a "$FIX/kano-probe" "$T/c4-probe"
cp -a "$FIX/kano" "$T/draft-fix"
cp -a "$FIX/kano-probe" "$T/draft-probe"
python3 - "$T/names-fix/cat-proc-cpuinfo.txt" "$T/names-probe/probe.json" "$T/vendor-fix/cat-proc-cpuinfo.txt" "$T/vendor-probe/probe.json" "$T/step-probe/probe.json" << 'PY'
import json, pathlib, sys
names_cpu, names_probe, ven_cpu, ven_probe, step_probe = sys.argv[1:]
cpu = pathlib.Path(names_cpu).read_text(encoding="utf-8")
cpu = cpu.replace("12th Gen Intel(R) Core(TM) i7-1260P", "Chip AAA", 1)
pathlib.Path(names_cpu).write_text(cpu, encoding="utf-8", newline="\n")
probe = pathlib.Path(names_probe).read_text(encoding="utf-8")
probe = probe.replace("12th Gen Intel(R) Core(TM) i7-1260P", "Chip BBB", 1)
pathlib.Path(names_probe).write_text(probe, encoding="utf-8", newline="\n")
pathlib.Path(ven_cpu).write_text("processor: 0\nvendor_id: GenuineIntel\n", encoding="utf-8", newline="\n")
data = json.loads(pathlib.Path(ven_probe).read_text(encoding="utf-8"))
data["cpu"] = {"vendor_id": "GenuineIntel"}
pathlib.Path(ven_probe).write_text(json.dumps(data), encoding="utf-8", newline="\n")
data = json.loads(pathlib.Path(step_probe).read_text(encoding="utf-8"))
data["cpu"]["stepping"] = "9"
pathlib.Path(step_probe).write_text(json.dumps(data), encoding="utf-8", newline="\n")
PY
cp "$FIX/r4/draft-uscore/profile.draft.toml" "$T/draft-probe/profile.draft.toml"
printf '\ncard4\n' >> "$T/c4-fix/ls-dev-dri.txt"
if bash "$R" --fixture "$T/names-fix" --probe "$T/names-probe" --out "$T/names" --date 20261014 > /dev/null; then
    grep -F -q '一致：DMI、CPU、PCI' "$T/names/summary.md" \
        && grep -F -q 'UEFI yes, Secure Boot off' "$T/names/summary.md" \
        && grep -F -q 'family 6' "$T/names/summary.md" \
        && ! grep -R -F -q -- 'Chip AAA' "$T/names" \
        && ! grep -R -F -q -- 'Chip BBB' "$T/names" \
        && ! grep -R -F -q -- 'i7-1260P' "$T/names" \
        && ok "same CPU numbers with different model names merge" || bad "same CPU numbers with different model names merge"
else
    bad "same CPU numbers with different model names merge"
fi
if bash "$R" --fixture "$T/vendor-fix" --probe "$T/vendor-probe" --out "$T/vendor-only" --date 20261014 > /dev/null; then
    grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/vendor-only/summary.md" \
        && ! grep -F -q '一致：DMI' "$T/vendor-only/summary.md" \
        && ! grep -F -q 'UEFI' "$T/vendor-only/summary.md" \
        && ok "vendor-only CPU does not merge" || bad "vendor-only CPU does not merge"
else
    bad "vendor-only CPU does not merge"
fi
if bash "$R" --fixture "$T/step-fix" --probe "$T/step-probe" --out "$T/step" --date 20261014 > /dev/null; then
    grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/step/summary.md" \
        && ! grep -F -q '一致：DMI' "$T/step/summary.md" \
        && ! grep -F -q 'UEFI' "$T/step/summary.md" \
        && ok "a different stepping does not merge" || bad "a different stepping does not merge"
else
    bad "a different stepping does not merge"
fi
if bash "$R" --fixture "$T/c4-fix" --probe "$T/c4-probe" --out "$T/c4" --date 20261014 > /dev/null; then
    grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/c4/summary.md" \
        && grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/c4/summary.md" \
        && ! grep -F -q '一致：DMI' "$T/c4/summary.md" \
        && ! grep -F -q 'UEFI yes' "$T/c4/summary.md" \
        && ! grep -F -q 'real-gpu' "$T/c4/summary.md" \
        && ok "an unread card4 does not merge" || bad "an unread card4 does not merge"
else
    bad "an unread card4 does not merge"
fi
if bash "$R" --fixture "$T/draft-fix" --probe "$T/draft-probe" --out "$T/draft-mac" --date 20261014 > /dev/null; then
    grep -F -q '一致：DMI、CPU、PCI' "$T/draft-mac/summary.md" \
        && grep -F -q '| profile 草稿 | 未知 |' "$T/draft-mac/summary.md" \
        && ! grep -R -F -q -- '02_de_ad_be_ef_11' "$T/draft-mac" \
        && ! grep -R -F -q -- '02_de_ad' "$T/draft-mac" \
        && ! grep -F -q '已省略' "$T/draft-mac/summary.md" \
        && ok "a draft id that is an underscore MAC is unknown on this machine" || bad "a draft id that is an underscore MAC is unknown on this machine"
else
    bad "a draft id that is an underscore MAC is unknown on this machine"
fi

# ro.build.user is not an identifier. Fragments of it must not eat the kernel or the title.
fr_user() {
    local user=$1 name=$2
    rm -rf "$T/fr-$name-fix" "$T/fr-$name-probe" "$T/fr-$name"
    cp -a "$FIX/kano" "$T/fr-$name-fix"
    cp -a "$FIX/kano-probe" "$T/fr-$name-probe"
    printf '%s\n' "[ro.build.user]: [$user]" >> "$T/fr-$name-fix/getprop.txt"
    bash "$R" --fixture "$T/fr-$name-fix" --probe "$T/fr-$name-probe" --out "$T/fr-$name" --date 20261014 > /dev/null
}
if fr_user android-build android \
    && grep -F -q '| 核心 | other |' "$T/fr-android/summary.md" \
    && grep -F -q 'Googlebook' "$T/fr-android/summary.md" \
    && grep -F -q '| 機型 | Google |' "$T/fr-android/summary.md" \
    && grep -F -q 'CL3B.260622.271.R1' "$T/fr-android/summary.md" \
    && grep -F -q '8086:46a6' "$T/fr-android/summary.md" \
    && grep -F -q 'redactions: 0' "$T/fr-android/gbos-report-20261014.txt" \
    && ! grep -R -F -q -- 'android-build' "$T/fr-android" \
    && fr_user google google \
    && grep -F -q '| 核心 | other |' "$T/fr-google/summary.md" \
    && grep -F -q 'Googlebook' "$T/fr-google/summary.md" \
    && grep -F -q '| 機型 | Google |' "$T/fr-google/summary.md" \
    && grep -F -q 'CL3B.260622.271.R1' "$T/fr-google/summary.md" \
    && grep -F -q '8086:46a6' "$T/fr-google/summary.md" \
    && grep -F -q 'redactions: 0' "$T/fr-google/gbos-report-20261014.txt"; then
    ok "a build user does not redact the kernel, title, vendor, build id, or PCI"
else
    bad "a build user does not redact the kernel, title, vendor, build id, or PCI"
fi

# The output firewall still removes a canary when the vendor check is mutated off.
if python3 - "$R" "$T/mutated-report.sh" << 'PY'
import pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
old = '''def vendor_enum(value, banned):
    """Known OEM, or other. The product string is never this function's input."""
    raw = raw_scalar(value)'''
new = '''def vendor_enum(value, banned):
    """Known OEM, or other. The product string is never this function's input."""
    return raw_scalar(value) or ""
    raw = raw_scalar(value)'''
if old not in src:
    sys.stderr.write("mutation anchor missing\n")
    raise SystemExit(1)
pathlib.Path(sys.argv[2]).write_text(src.replace(old, new, 1), encoding="utf-8", newline="\n")
PY
then
    mkdir -p "$T/mut-mail" "$T/mut-mac"
    printf '%s\n' 'canary.model@example.test' > "$T/mut-mail/cat-dmi-sys-vendor.txt"
    printf '%s\n' '02_aa_bb_cc_dd_11' > "$T/mut-mac/cat-dmi-sys-vendor.txt"
    if bash "$R" --fixture "$T/mut-mail" --out "$T/mut-mail-off" --date 20261014 > /dev/null \
        && ! grep -R -F -q -- 'canary.model' "$T/mut-mail-off" \
        && ! grep -R -F -q -- 'example.test' "$T/mut-mail-off" \
        && grep -F -q 'redactions: 0' "$T/mut-mail-off/gbos-report-20261014.txt" \
        && bash "$R" --fixture "$T/mut-mac" --out "$T/mut-mac-off" --date 20261014 > /dev/null \
        && ! grep -R -F -q -- '02_aa_bb' "$T/mut-mac-off" \
        && grep -F -q 'redactions: 0' "$T/mut-mac-off/gbos-report-20261014.txt" \
        && bash "$T/mutated-report.sh" --fixture "$T/mut-mail" --out "$T/mut-mail-on" --date 20261014 > /dev/null \
        && ! grep -R -F -q -- 'canary.model' "$T/mut-mail-on" \
        && ! grep -R -F -q -- 'example.test' "$T/mut-mail-on" \
        && grep -E -q 'redactions: [1-9]' "$T/mut-mail-on/gbos-report-20261014.txt" \
        && bash "$T/mutated-report.sh" --fixture "$T/mut-mac" --out "$T/mut-mac-on" --date 20261014 > /dev/null \
        && ! grep -R -F -q -- '02_aa_bb' "$T/mut-mac-on" \
        && grep -E -q 'redactions: [1-9]' "$T/mut-mac-on/gbos-report-20261014.txt"; then
        ok "the output firewall stops a canary when the validator is disabled"
    else
        bad "the output firewall stops a canary when the validator is disabled"
    fi
else
    bad "the output firewall stops a canary when the validator is disabled"
fi

# Round 5: kernel pin, firewall tokens, PCI-bus identity, raw values, GPU inventory.
python3 - "$FIX/kano" "$T" << 'PY'
import json, pathlib, shutil, sys
fix, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

def copy(name):
    dest = root / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(fix, dest)
    return dest

def prop(path, key, value):
    text = path.read_text(encoding="utf-8")
    line = "[%s]: [%s]" % (key, value)
    pat = "[" + key + "]:"
    lines = []
    found = False
    for old in text.splitlines():
        if old.startswith(pat):
            lines.append(line)
            found = True
        else:
            lines.append(old)
    if not found:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

leak = copy("r5-kern")
prop(leak / "getprop.txt", "ro.serialno", "02aabbccddee")
prop(leak / "getprop.txt", "persist.sys.wifimac", "02:aa:bb:cc:dd:ee")
(leak / "uname-a.txt").write_text(
    "Linux synth-laptop 6.1.75-android14-11-g02aabbccddee #1\n", encoding="utf-8", newline="\n")

pin = copy("r5-pin")
(pin / "uname-a.txt").write_text(
    "Linux synth-laptop 6.18.21-android17-5-g3938c8d97142-ab16302624 #1\n", encoding="utf-8", newline="\n")

dev = copy("r5-dev")
(dev / "uname-a.txt").write_text(
    "Linux synth-laptop 6.1.75-android14-11-gbosdev #1\n", encoding="utf-8", newline="\n")

host = copy("r5-host")
prop(host / "getprop.txt", "net.hostname", "model")
prop(host / "getprop.txt", "ro.serialno", "4")
ver = (host / "cat-proc-version.txt").read_text(encoding="utf-8")
ver = ver.replace("(synthuser@synth-build)", "(gbos@gbos)", 1)
(host / "cat-proc-version.txt").write_text(ver, encoding="utf-8", newline="\n")

def host_case(name, value):
    dest = copy(name)
    prop(dest / "getprop.txt", "net.hostname", value)
    return dest

for value, name in (("gbos", "r5-gbos"), ("googlebook", "r5-gbook"), ("kano", "r5-kano"), ("google", "r5-google")):
    host_case(name, value)

wifi = copy("r5-wifi")
(wifi / "cat-wlan-uevent.txt").write_text("DRIVER=notadriver\nPCI_ID=8086:51F0\n", encoding="utf-8", newline="\n")
bt = copy("r5-bt")
(bt / "cat-bt-uevent.txt").write_text("DRIVER=notabtdrv\nPCI_ID=8086:51F1\n", encoding="utf-8", newline="\n")

raw = copy("r5-raw")
(raw / "cat-drm0-uevent.txt").write_text("DRIVER= xe \nPCI_ID= 8086:46a6 \n", encoding="utf-8", newline="\n")
(raw / "cat-drm-vendor.txt").write_text("zz\n", encoding="utf-8", newline="\n")
(raw / "cat-drm-device.txt").write_text("zz\n", encoding="utf-8", newline="\n")
(raw / "dumpsys-input.txt").write_text(
    "1: Alice Keyboard\n  Classes: TOUCHPAD\n"
    "  Identifier: bus=0x0018 vendor=0xdead@example.test product=0xbeef@example.test\n",
    encoding="utf-8", newline="\n")
(raw / "cat-proc-bus-input-devices.txt").write_text("", encoding="utf-8", newline="\n")
(raw / "dumpsys-wifi.txt").write_text(
    "Wi-Fi is enabled\ncurState=ConnectedState@example.test\nSupplicant state: COMPLETED@example.test\n",
    encoding="utf-8", newline="\n")
(raw / "dumpsys-bluetooth.txt").write_text(
    "Bluetooth Status\n  enabled: true@example.test\n  state: ON@example.test\n",
    encoding="utf-8", newline="\n")

blank = copy("r5-blank")
prop(blank / "getprop.txt", "ro.build.display.id", "NOTABUILD")
prop(blank / "getprop.txt", "ro.build.id", "NOTABUILD")
(blank / "uname-a.txt").write_text("Linux synth-laptop\n", encoding="utf-8", newline="\n")
(blank / "cat-proc-version.txt").write_text("no version here\n", encoding="utf-8", newline="\n")
(blank / "cat-drm0-uevent.txt").write_text("DRIVER=xe\nPCI_ID=not-an-id\n", encoding="utf-8", newline="\n")
(blank / "cat-drm-vendor.txt").write_text("zz\n", encoding="utf-8", newline="\n")
(blank / "cat-drm-device.txt").write_text("zz\n", encoding="utf-8", newline="\n")

empty = copy("r5-emptygpu")
prop(empty / "getprop.txt", "ro.hardware.egl", "swiftshader")
prop(empty / "getprop.txt", "ro.hardware.vulkan", "pastel")
(empty / "ls-dev-dri.txt").write_text("", encoding="utf-8", newline="\n")
for name in ("cat-drm0-uevent.txt", "cat-drm-vendor.txt", "cat-drm-device.txt"):
    (empty / name).write_text("", encoding="utf-8", newline="\n")

shim = copy("r5-shim")
prop(shim / "getprop.txt", "ro.hardware.egl", "swiftshader")
(shim / "cat-drm0-uevent.txt").write_text("DRIVER=gbos_efifb\n", encoding="utf-8", newline="\n")
(shim / "cat-drm-vendor.txt").write_text("", encoding="utf-8", newline="\n")
(shim / "cat-drm-device.txt").write_text("", encoding="utf-8", newline="\n")
(shim / "ls-dev-dri.txt").write_text("card0\n", encoding="utf-8", newline="\n")

tail = copy("r5-swifttail")
prop(tail / "getprop.txt", "ro.hardware.egl", "swiftshader@example.test")
prop(tail / "getprop.txt", "ro.hardware.vulkan", "pastel@example.test")
(tail / "cat-drm0-uevent.txt").write_text("DRIVER=gbos_efifb\n", encoding="utf-8", newline="\n")
(tail / "cat-drm-vendor.txt").write_text("", encoding="utf-8", newline="\n")
(tail / "cat-drm-device.txt").write_text("", encoding="utf-8", newline="\n")
(tail / "ls-dev-dri.txt").write_text("card0\n", encoding="utf-8", newline="\n")

flag = root / "r5-flag-probe"
shutil.copytree(pathlib.Path(sys.argv[1]).parents[0] / "kano-probe", flag)
data = json.loads((flag / "probe.json").read_text(encoding="utf-8"))
data["firmware"] = {}
(flag / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
(flag / "report.txt").write_text(
    "firmware: UEFI yes@example.test, Secure Boot off@example.test\n",
    encoding="utf-8", newline="\n")

iface = root / "r5-iface-probe"
shutil.copytree(pathlib.Path(sys.argv[1]).parents[0] / "kano-probe", iface)
data = json.loads((iface / "probe.json").read_text(encoding="utf-8"))
data["net"] = [{"name": "wlalice1", "wireless": True}, {"name": "wlsecret1", "wireless": True}]
(iface / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")

unread = root / "r5-unread-probe"
shutil.copytree(pathlib.Path(sys.argv[1]).parents[0] / "kano-probe", unread)
data = json.loads((unread / "probe.json").read_text(encoding="utf-8"))
data["pci"].append({"class": "030000"})
(unread / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
unread2 = root / "r5-unread2-probe"
shutil.copytree(unread, unread2)
data = json.loads((unread2 / "probe.json").read_text(encoding="utf-8"))
data["pci"] = [item for item in data["pci"] if "id" in item]
data["pci"].append({"class": "030000", "id": "not-a-pci", "driver": ""})
(unread2 / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")

dup = copy("r5-dup")
(dup / "cat-proc-bus-pci-devices.txt").write_text("0000\t808646a6\t0\n0001\t808646a6\t0\n", encoding="utf-8", newline="\n")
dup_probe = root / "r5-dup-probe"
shutil.copytree(pathlib.Path(sys.argv[1]).parents[0] / "kano-probe", dup_probe)
data = json.loads((dup_probe / "probe.json").read_text(encoding="utf-8"))
data["pci"] = [
    {"class": "030000", "id": "8086:46a6", "driver": "xe"},
    {"class": "030000", "id": "8086:46a6", "driver": "xe"},
]
(dup_probe / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
one = root / "r5-one-probe"
shutil.copytree(dup_probe, one)
data["pci"] = [{"class": "030000", "id": "8086:46a6", "driver": "xe"}]
(one / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
PY
[ "$?" = 0 ] && ok "round 5 fixtures were built" || bad "round 5 fixtures were built"
if bash "$R" --fixture "$T/r5-kern" --out "$T/r5-kern-out" --date 20261014 > /dev/null \
    && ! grep -R -F -q -- '02aabbccddee' "$T/r5-kern-out" \
    && ! grep -R -F -q -- 'g02aabbccddee' "$T/r5-kern-out" \
    && ! grep -R -F -q -- '02:aa:bb:cc:dd:ee' "$T/r5-kern-out" \
    && ! grep -R -F -q -- '6.1.75-android14-11-g02aabbccddee' "$T/r5-kern-out" \
    && grep -F -q '8086:46a6' "$T/r5-kern-out/summary.md" \
    && grep -F -q '| 機型 | Google |' "$T/r5-kern-out/summary.md" \
    && grep -F -q '| 核心 | other |' "$T/r5-kern-out/summary.md" \
    && grep -F -q 'redactions: 0' "$T/r5-kern-out/gbos-report-20261014.txt"; then
    ok "a kernel 12-hex tail equal to a serial or MAC is not printed"
else
    bad "a kernel 12-hex tail equal to a serial or MAC is not printed"
fi
if bash "$R" --fixture "$T/r5-pin" --out "$T/r5-pin-out" --date 20261014 > /dev/null \
    && grep -F -q '| 核心 | 6.18.21-android17-5-g3938c8d97142-ab16302624 |' "$T/r5-pin-out/summary.md" \
    && grep -F -q 'redactions: 0' "$T/r5-pin-out/gbos-report-20261014.txt"; then
    ok "the pinned image kernel is printed exactly"
else
    bad "the pinned image kernel is printed exactly"
fi
rel="$T/r5-krel/include/config"
mkdir -p "$rel"
printf '%s\n' '6.1.75-android14-11-gbosdev' > "$rel/kernel.release"
if GBOS_DEV_KERNEL_DIR="$T/r5-krel" bash "$R" --fixture "$T/r5-dev" --out "$T/r5-dev-out" --date 20261014 > /dev/null \
    && grep -F -q '| 核心 | dev-kernel |' "$T/r5-dev-out/summary.md" \
    && ! grep -R -F -q -- '6.1.75-android14-11-gbosdev' "$T/r5-dev-out" \
    && ! grep -R -F -q -- 'gbosdev' "$T/r5-dev-out" \
    && bash "$R" --fixture "$T/r5-dev" --out "$T/r5-dev-off" --date 20261014 > /dev/null \
    && grep -F -q '| 核心 | other |' "$T/r5-dev-off/summary.md" \
    && ! grep -R -F -q -- 'gbosdev' "$T/r5-dev-off"; then
    ok "a dev kernel is dev-kernel, and the release stays absent when the directory is unset"
else
    bad "a dev kernel is dev-kernel, and the release stays absent when the directory is unset"
fi
host_ok=1
for pair in "r5-host model" "r5-gbos gbos" "r5-gbook googlebook" "r5-kano kano" "r5-google google"; do
    name=${pair%% *}
    word=${pair#* }
    if ! bash "$R" --fixture "$T/$name" --probe "$FIX/kano-probe" --out "$T/$name-out" --date 20261014 > /dev/null \
        || ! grep -F -q 'redactions: 0' "$T/$name-out/gbos-report-20261014.txt" \
        || ! grep -F -q '| 機型 | Google |' "$T/$name-out/summary.md" \
        || ! grep -F -q 'model 154' "$T/$name-out/summary.md" \
        || ! grep -F -q 'Googlebook' "$T/$name-out/summary.md" \
        || ! grep -F -q '| 項目 |' "$T/$name-out/summary.md" \
        || ! grep -F -q '成功 4、失敗 1' "$T/$name-out/summary.md" \
        || ! grep -F -q 'CL3B.260622.271.R1' "$T/$name-out/summary.md" \
        || ! grep -F -q '8086:46a6' "$T/$name-out/summary.md" \
        || ! grep -F -q '一致：DMI、CPU、PCI' "$T/$name-out/summary.md" \
        || grep -F -q "net.hostname" "$T/$name-out/summary.md"; then
        host_ok=0
        bad "hostname $word leaves the header and the fixture facts"
    fi
done
[ "$host_ok" = 1 ] && ok "default and label hostnames do not redact headers or fixture facts"
if bash "$R" --fixture "$T/r5-wifi" --out "$T/r5-wifi-out" --date 20261014 > /dev/null \
    && grep -F -q '8086:51f0' "$T/r5-wifi-out/summary.md" \
    && ! grep -R -F -q -- 'notadriver' "$T/r5-wifi-out" \
    && bash "$R" --fixture "$T/r5-bt" --out "$T/r5-bt-out" --date 20261014 > /dev/null \
    && grep -F -q '8086:51f1' "$T/r5-bt-out/summary.md" \
    && ! grep -R -F -q -- 'notabtdrv' "$T/r5-bt-out"; then
    ok "an unknown Wi-Fi or Bluetooth driver keeps the PCI id"
else
    bad "an unknown Wi-Fi or Bluetooth driver keeps the PCI id"
fi
if bash "$R" --probe "$T/r5-iface-probe" --out "$T/r5-iface-out" --date 20261014 > /dev/null \
    && ! grep -R -F -q -- 'wlalice1' "$T/r5-iface-out" \
    && ! grep -R -F -q -- 'wlsecret1' "$T/r5-iface-out"; then
    ok "only wlan0-wlan9 and eth0-eth9 are interface names"
else
    bad "only wlan0-wlan9 and eth0-eth9 are interface names"
fi
if bash "$R" --fixture "$T/r5-emptygpu" --probe "$FIX/kano-probe" --out "$T/r5-emptygpu-out" --date 20261014 > /dev/null \
    && grep -F -q '| 顯示／GPU 驅動 | 未知 |' "$T/r5-emptygpu-out/summary.md" \
    && ! grep -F -q '沒有原生' "$T/r5-emptygpu-out/summary.md" \
    && grep -F -q '一致：DMI、CPU、PCI' "$T/r5-emptygpu-out/summary.md" \
    && bash "$R" --fixture "$T/r5-shim" --probe "$FIX/kano-probe" --out "$T/r5-shim-out" --date 20261014 > /dev/null \
    && grep -F -q '沒有原生 GPU 驅動（SwiftShader, gbos_efifb）' "$T/r5-shim-out/summary.md" \
    && grep -F -q '一致：DMI、CPU、PCI' "$T/r5-shim-out/summary.md"; then
    ok "an empty DRM list is unknown, and a platform shim can still be the same machine"
else
    bad "an empty DRM list is unknown, and a platform shim can still be the same machine"
fi
if bash "$R" --fixture "$T/r5-swifttail" --out "$T/r5-swifttail-out" --date 20261014 > /dev/null \
    && grep -F -q '沒有原生 GPU 驅動（gbos_efifb）' "$T/r5-swifttail-out/summary.md" \
    && ! grep -F -q 'SwiftShader' "$T/r5-swifttail-out/summary.md" \
    && ! grep -R -F -q -- 'example.test' "$T/r5-swifttail-out" \
    && bash "$R" --probe "$T/r5-flag-probe" --out "$T/r5-flag-out" --date 20261014 > /dev/null \
    && ! grep -F -q 'UEFI yes' "$T/r5-flag-out/summary.md" \
    && ! grep -F -q 'Secure Boot' "$T/r5-flag-out/summary.md" \
    && ! grep -R -F -q -- 'example.test' "$T/r5-flag-out"; then
    ok "a suffixed SwiftShader token or firmware flag is not a complete value"
else
    bad "a suffixed SwiftShader token or firmware flag is not a complete value"
fi
if bash "$R" --fixture "$FIX/kano" --probe "$T/r5-unread-probe" --out "$T/r5-unread-out" --date 20261014 > /dev/null \
    && grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/r5-unread-out/summary.md" \
    && ! grep -F -q '一致：DMI' "$T/r5-unread-out/summary.md" \
    && bash "$R" --fixture "$FIX/kano" --probe "$T/r5-unread2-probe" --out "$T/r5-unread2-out" --date 20261014 > /dev/null \
    && grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/r5-unread2-out/summary.md" \
    && ! grep -F -q '一致：DMI' "$T/r5-unread2-out/summary.md" \
    && ! grep -R -F -q -- 'not-a-pci' "$T/r5-unread2-out"; then
    ok "an unread probe PCI device is not the same machine"
else
    bad "an unread probe PCI device is not the same machine"
fi
if bash "$R" --fixture "$T/r5-dup" --probe "$T/r5-dup-probe" --out "$T/r5-dup-out" --date 20261014 > /dev/null \
    && grep -F -q '一致：DMI、CPU、PCI' "$T/r5-dup-out/summary.md" \
    && bash "$R" --fixture "$T/r5-dup" --probe "$T/r5-one-probe" --out "$T/r5-one-out" --date 20261014 > /dev/null \
    && grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/r5-one-out/summary.md" \
    && ! grep -F -q '一致：DMI' "$T/r5-one-out/summary.md"; then
    ok "two PCI devices with one id merge only with two on the other side"
else
    bad "two PCI devices with one id merge only with two on the other side"
fi
if bash "$R" --fixture "$T/r5-raw" --out "$T/r5-raw-out" --date 20261014 > /dev/null \
    && ! grep -R -F -q -- ' xe ' "$T/r5-raw-out" \
    && ! grep -F -q '已綁定（xe）' "$T/r5-raw-out/summary.md" \
    && ! grep -F -q '8086:46a6' "$T/r5-raw-out/summary.md" \
    && ! grep -R -F -q -- 'dead:beef' "$T/r5-raw-out" \
    && ! grep -R -F -q -- 'example.test' "$T/r5-raw-out" \
    && ! grep -F -q '已連線' "$T/r5-raw-out/summary.md" \
    && ! grep -F -q '| 藍牙 | 已開啟 |' "$T/r5-raw-out/summary.md"; then
    ok "a spaced or suffixed raw token is not accepted"
else
    bad "a spaced or suffixed raw token is not accepted"
fi
if bash "$R" --fixture "$T/r5-blank" --out "$T/r5-blank-out" --date 20261014 > /dev/null \
    && grep -F -q '| 建置版本 | 未知 |' "$T/r5-blank-out/summary.md" \
    && grep -F -q '| 核心 | 未知 |' "$T/r5-blank-out/summary.md" \
    && grep -F -q '| GPU PCI | 未知 |' "$T/r5-blank-out/summary.md" \
    && ! grep -R -F -q -- 'NOTABUILD' "$T/r5-blank-out" \
    && ! grep -R -F -q -- 'not-an-id' "$T/r5-blank-out"; then
    ok "build, kernel, and PCI stay unknown when every candidate is invalid"
else
    bad "build, kernel, and PCI stay unknown when every candidate is invalid"
fi

# The token list is whatever a device-free render can print, not report_vocabulary() checking itself.
if python3 "$ROOT/tests/report/r8_vocab.py"; then
    ok "printed tokens with no device data equal the vocabulary"
else
    bad "printed tokens with no device data equal the vocabulary"
fi

# Round 7: a banned identifier that is a word this report prints must not change the body.
# PCI/USB ids stay one token. A missing PCI list is not an empty bus. Input suffixes are not types.
if python3 - "$R" "$FIX/kano" "$FIX/kano-probe" "$T" << 'PY'
import io, json, pathlib, re, shutil, subprocess, sys
script, fix_s, probe_s, root_s = sys.argv[1:]
root = pathlib.Path(root_s)
fix = pathlib.Path(fix_s)
probe_src = pathlib.Path(probe_s)
text = pathlib.Path(script).read_text(encoding="utf-8")
py = text.split("<< 'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
py = py[:py.rfind("\nmain()")] + "\n"
ns = {}
exec(compile(py, script, "exec"), ns)
real_out = sys.stdout
sys.stdout = io.StringIO()
captured = {}

def capture(path, body):
    captured[pathlib.Path(path).name] = body

ns["write_excl"] = capture
raw = root / "r7-raw"
if raw.exists():
    shutil.rmtree(raw)
raw.mkdir()
manifest = []
for src in sorted(fix.iterdir()):
    if src.name.endswith(".txt"):
        (raw / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        ident = src.name[:-4]
        (raw / (ident + ".rc")).write_text("0\n", encoding="utf-8", newline="\n")
        (raw / (ident + ".err")).write_text("", encoding="utf-8", newline="\n")
        manifest.append(ident + "\tcmd\n")
(raw / "manifest.tsv").write_text("".join(manifest), encoding="utf-8", newline="\n")
orig_prop = (raw / "getprop.txt").read_text(encoding="utf-8")
orig_uname = (raw / "uname-a.txt").read_text(encoding="utf-8")
orig_ver = (raw / "cat-proc-version.txt").read_text(encoding="utf-8")

def set_prop(body, key, value):
    line = "[%s]: [%s]" % (key, value)
    prefix = "[" + key + "]:"
    lines, found = [], False
    for old in body.splitlines():
        if old.startswith(prefix):
            lines.append(line)
            found = True
        else:
            lines.append(old)
    if not found:
        lines.append(line)
    return "\n".join(lines) + "\n"

def render_once():
    captured.clear()
    ns["render"](str(root / "r7-mem"), "20261014", str(raw), str(probe_src), "fixture", "")
    return captured["gbos-report-20261014.txt"], captured["summary.md"]

base_txt, base_sum = render_once()
words = set(ns["report_vocabulary"]())
for extra in ("renderD0", "renderD128", "renderD999"):
    words.add(extra)
bad = []
for word in sorted(words):
    prop = set_prop(set_prop(orig_prop, "ro.serialno", word), "net.hostname", word)
    (raw / "getprop.txt").write_text(prop, encoding="utf-8", newline="\n")
    uname, ver = orig_uname, orig_ver
    if re.fullmatch(r"[A-Za-z0-9._+-]+", word):
        uname = orig_uname.replace("synth-laptop", word, 1)
        ver = orig_ver.replace("synthuser@synth-build", "synthuser@" + word, 1)
    (raw / "uname-a.txt").write_text(uname, encoding="utf-8", newline="\n")
    (raw / "cat-proc-version.txt").write_text(ver, encoding="utf-8", newline="\n")
    try:
        txt, summary = render_once()
    except SystemExit as exc:
        bad.append("%s exit %s" % (word, exc))
        continue
    if txt != base_txt or summary != base_sum:
        bad.append(word)
    if len(bad) >= 8:
        break
(raw / "getprop.txt").write_text(orig_prop, encoding="utf-8", newline="\n")
(raw / "uname-a.txt").write_text(orig_uname, encoding="utf-8", newline="\n")
(raw / "cat-proc-version.txt").write_text(orig_ver, encoding="utf-8", newline="\n")
sys.stdout = real_out
if bad:
    sys.stderr.write("vocabulary redacted: %s\n" % ", ".join(bad))
    raise SystemExit(1)
print("vocabulary words %d" % len(words))
PY
then
    ok "every report vocabulary word used as hostname and serial leaves the body unchanged"
else
    bad "every report vocabulary word used as hostname and serial leaves the body unchanged"
fi

agree_files() { # agree_files <out-dir>
    python3 - "$1" << 'PY'
import pathlib, re, sys
out = pathlib.Path(sys.argv[1])
txt = (out / "gbos-report-20261014.txt").read_text(encoding="utf-8")
summary = (out / "summary.md").read_text(encoding="utf-8")

def die(msg):
    sys.stderr.write(msg + "\n")
    raise SystemExit(1)

en, zh = [], []
for line in txt.splitlines():
    m = re.match(r"^([^:]+): (.*) \[([^\]]*)\]$", line)
    if m:
        en.append((m.group(2), m.group(3)))
for line in summary.splitlines():
    if not line.startswith("| ") or line.startswith("| 項目") or line.startswith("|---"):
        continue
    parts = [p.strip() for p in line.strip().strip("|").split("|")]
    if len(parts) >= 3:
        zh.append((parts[1], parts[2]))
if len(en) != len(zh):
    die("row count %d %d" % (len(en), len(zh)))
pairs = (
    ("沒有映像標記", "no image marker"),
    ("沒有連線狀態", "no link state"),
    ("沒有開關狀態", "no power state"),
    ("已省略（probe 與這台裝置不是同一台）", "omitted (probe is not the same machine)"),
    ("不是 Googlebook 狀態", "not Googlebook state"),
    ("沒有原生 GPU 驅動", "no native GPU driver"),
    ("已開啟、未連線", "enabled, not connected"),
    ("已開啟，連線狀態未知", "enabled, connection unknown"),
    ("介面存在，開關未知", "adapter present, power unknown"),
    ("服務執行中，開關未確認", "service running, power unknown"),
    ("服務已停止，開關未確認", "service stopped, power unknown"),
    ("介面已起來", "interface up"),
    ("介面已關閉", "interface down"),
    ("僅 probe", "probe only"),
    ("探測到", "probe saw"),
    ("已連線", "connected"),
    ("已關閉", "disabled"),
    ("已開啟", "on"),
    ("開啟中", "turning on"),
    ("關閉中", "turning off"),
    ("已綁定", "bound"),
    ("觸控螢幕", "touchscreen"),
    ("觸控板", "touchpad"),
    ("觸控筆", "stylus"),
    ("未知", "unknown"),
    ("驅動", "driver"),
    ("成功", "success"),
    ("失敗", "fail"),
    ("一致", "agree"),
)
pairs = tuple(sorted(pairs, key=lambda item: len(item[0]), reverse=True))
origins = {
    "fixture": "fixture",
    "adb": "adb",
    "probe": "probe",
    "—": "—",
    "fixture+probe": "fixture、probe",
    "adb+probe": "adb、probe",
}

def words(text):
    return re.findall(r"[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*", text)

def norm_zh(en, zh):
    text = zh
    en_words = set(words(en))
    for src, dst in pairs:
        if src == "已關閉" and dst == "disabled":
            if "off" in en_words and "disabled" not in en_words:
                text = text.replace(src, "off")
                continue
        text = text.replace(src, dst)
    return text

for (a, oa), (b, ob) in zip(en, zh):
    if (a == "unknown") != (b == "未知"):
        die("disagree %s / %s" % (a, b))
    if words(a) != words(norm_zh(a, b)):
        die("row words %s / %s" % (a, b))
    nums_a = re.findall(r"\d+", a)
    nums_b = re.findall(r"\d+", b)
    if nums_a != nums_b:
        die("row numbers %s / %s" % (nums_a, nums_b))
    ids_a = re.findall(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", a.lower())
    ids_b = re.findall(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", b.lower())
    if ids_a != ids_b:
        die("row pci %s / %s" % (ids_a, ids_b))
    if origins.get(oa, oa) != ob:
        die("origin %s / %s" % (oa, ob))
nt = re.search(r"redactions: (\d+)", txt)
ns_ = re.search(r"遮罩 (\d+) 行", summary)
if not nt or not ns_ or nt.group(1) != ns_.group(1):
    die("redaction counts")
PY
}

if python3 - "$R" "$FIX/kano" "$FIX/kano-probe" "$T" << 'PY'
import json, pathlib, shutil, subprocess, sys
script, fix_s, probe_s, root_s = sys.argv[1:]
R = script
fix = pathlib.Path(fix_s)
probe_src = pathlib.Path(probe_s)
root = pathlib.Path(root_s)
PIN = "6.18.21-android17-5-g3938c8d97142-ab16302624"

def copy_fix(name):
    dest = root / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(fix, dest)
    return dest

def copy_probe(name):
    dest = root / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(probe_src, dest)
    return dest

def set_prop(path, key, value):
    text = path.read_text(encoding="utf-8")
    line = "[%s]: [%s]" % (key, value)
    prefix = "[" + key + "]:"
    lines, found = [], False
    for old in text.splitlines():
        if old.startswith(prefix):
            lines.append(line)
            found = True
        else:
            lines.append(old)
    if not found:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

def host_serial(dest, word, serial=None):
    set_prop(dest / "getprop.txt", "net.hostname", word)
    set_prop(dest / "getprop.txt", "ro.serialno", serial if serial is not None else word)
    uname = (dest / "uname-a.txt").read_text(encoding="utf-8")
    if " " not in word:
        uname = uname.replace("synth-laptop", word, 1)
        (dest / "uname-a.txt").write_text(uname, encoding="utf-8", newline="\n")
        ver = (dest / "cat-proc-version.txt").read_text(encoding="utf-8")
        ver = ver.replace("synthuser@synth-build", "synthuser@" + word, 1)
        (dest / "cat-proc-version.txt").write_text(ver, encoding="utf-8", newline="\n")

def pin_kernel(dest):
    (dest / "uname-a.txt").write_text("Linux synth-laptop %s #1\n" % PIN, encoding="utf-8", newline="\n")

made = []
for word in ("card0", "hci0", "renderD128", "CL3B", "R1", "real-gpu", "probe-only", "marker", "state", "image"):
    dest = copy_fix("r7-word-" + word.replace(" ", "_"))
    host_serial(dest, word)
    made.append(dest.name)
serials = copy_fix("r7-serial-ids")
set_prop(serials / "getprop.txt", "ro.serialno", "46a6")
for frag in ("04f3", "323c", "2d5b"):
    dest = copy_fix("r7-frag-" + frag)
    set_prop(dest / "getprop.txt", "ro.serialno", frag)
hid = copy_fix("r7-hid")
hp = copy_probe("r7-hid-probe")
set_prop(hid / "getprop.txt", "ro.serialno", "i2c_hid")
data = json.loads((hp / "probe.json").read_text(encoding="utf-8"))
data["input"][0]["driver"] = "i2c_hid"
(hp / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
kern = copy_fix("r7-kern")
pin_kernel(kern)
set_prop(kern / "getprop.txt", "ro.serialno", "3938c8d97142")
set_prop(kern / "getprop.txt", "net.hostname", "android17")
# fragments that are not the whole release still have to keep the pinned string
for word, name in (("g3938c8d97142", "r7-ghash"), ("ab16302624", "r7-ab")):
    dest = copy_fix(name)
    pin_kernel(dest)
    host_serial(dest, word)
# PCI completeness
def pci_case(name, pci_value, empty_adb, drop_file=False):
    dest = copy_fix(name)
    pr = copy_probe(name + "-probe")
    data = json.loads((pr / "probe.json").read_text(encoding="utf-8"))
    if pci_value == "missing":
        data.pop("pci", None)
    else:
        data["pci"] = pci_value
    (pr / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
    pci_path = dest / "cat-proc-bus-pci-devices.txt"
    if drop_file:
        pci_path.unlink()
    elif empty_adb:
        pci_path.write_text("", encoding="utf-8", newline="\n")
    return dest.name, pr.name

pci_case("r7-nokey-empty", "missing", True)
pci_case("r7-nokey-devs", "missing", False)
pci_case("r7-null", None, True)
pci_case("r7-both-empty", [], True)
pci_case("r7-missing-file", [], False, drop_file=True)
# input raw tokens
def input_case(name, dumpsys):
    dest = copy_fix(name)
    (dest / "dumpsys-input.txt").write_text(dumpsys, encoding="utf-8", newline="\n")
    (dest / "cat-proc-bus-input-devices.txt").write_text("", encoding="utf-8", newline="\n")
    return dest.name

input_case("r7-class-ok", "1: Generic Device\n  Classes: 0x00100000\n  Identifier: bus=0x0018 vendor=0xdead product=0xbeef\n")
input_case("r7-class-sym", "1: Generic Device\n  Classes: TOUCHPAD\n  Identifier: bus=0x0018 vendor=0xdead product=0xbeef\n")
input_case("r7-class-suffix", "1: Generic Device\n  Classes: 0x00100000@example.test\n  Identifier: bus=0x0018 vendor=0xdead product=0xbeef\n")
input_case("r7-class-space", "1: Generic Device\n  Classes:  0x00100000\n  Identifier: bus=0x0018 vendor=0xdead product=0xbeef\n")
input_case("r7-class-tail", "1: Generic Device\n  Classes: 0x00100000 \n  Identifier: bus=0x0018 vendor=0xdead product=0xbeef\n")
input_case("r7-src-ok", "1: Generic Device\n  Sources: 0x00001002\n  Identifier: bus=0x0018 vendor=0xdead product=0xbeef\n")
input_case("r7-src-suffix", "1: Generic Device\n  Sources: 0x00001002@example.test\n  Identifier: bus=0x0018 vendor=0xdead product=0xbeef\n")
input_case("r7-sym-suffix", "1: Generic Device\n  Classes: TOUCHPAD@example.test\n  Identifier: bus=0x0018 vendor=0xdead product=0xbeef\n")
input_case("r7-class-sib", "1: Generic Device\n  Classes: 0x00100000\n  Classes: 0x00000014@example.test\n  Identifier: bus=0x0018 vendor=0xdead product=0xbeef\n")
input_case("r7-src-sib", "1: Generic Device\n  Sources: 0x00001002\n  Sources: 0x00001002@example.test\n  Identifier: bus=0x0018 vendor=0xdead product=0xbeef\n")
kind = copy_probe("r7-kind-probe")
data = json.loads((kind / "probe.json").read_text(encoding="utf-8"))
data["input"] = [{"kind": "touchpad (i2c, by handler)", "vendor": "04f3", "product": "323c", "bus": "0018"}]
(kind / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
badkind = copy_probe("r7-badkind-probe")
data["input"] = [{"kind": "touchpad@example.test", "vendor": "04f3", "product": "323c", "bus": "0018"}]
(badkind / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
padhex = copy_probe("r7-padhex-probe")
data["input"] = [{"kind": "touchpad02aabbccddee", "vendor": "04f3", "product": "323c", "bus": "0018"}]
(padhex / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
# radio facts survive an unknown state
wifi = copy_fix("r7-wifi-usb")
(wifi / "dumpsys-wifi.txt").unlink()
(wifi / "cat-wlan-operstate.txt").unlink()
(wifi / "cat-wlan-uevent.txt").write_text("DRIVER=notadriver\nPRODUCT=bda/b00b/001\n", encoding="utf-8", newline="\n")
wifip = copy_fix("r7-wifi-pci")
(wifip / "dumpsys-wifi.txt").unlink()
(wifip / "cat-wlan-operstate.txt").unlink()
(wifip / "cat-wlan-uevent.txt").write_text("DRIVER=notadriver\nPCI_ID=8086:51F0\n", encoding="utf-8", newline="\n")
bt = copy_fix("r7-bt-usb")
(bt / "dumpsys-bluetooth.txt").unlink()
(bt / "ls-bluetooth.txt").unlink()
(bt / "cat-bt-uevent.txt").write_text("DRIVER=notabtdrv\nPRODUCT=bda/b00c/001\n", encoding="utf-8", newline="\n")
btturn = copy_fix("r7-bt-turn")
(btturn / "dumpsys-bluetooth.txt").write_text("Bluetooth Status\n  state: TURNING_ON\n", encoding="utf-8", newline="\n")
(btturn / "cat-bt-uevent.txt").write_text("DRIVER=notabtdrv\nPRODUCT=bda/b00c/001\n", encoding="utf-8", newline="\n")
btmiss = copy_fix("r7-bt-miss")
(btmiss / "dumpsys-bluetooth.txt").unlink()
(btmiss / "ls-bluetooth.txt").unlink()
(btmiss / "cat-bt-uevent.txt").write_text("DRIVER=notabtdrv\nPCI_ID=8086:51F1\n", encoding="utf-8", newline="\n")
gp = (btmiss / "getprop.txt").read_text(encoding="utf-8")
gp = "\n".join(line for line in gp.splitlines() if not line.startswith("[init.svc.bluetooth]:")) + "\n"
(btmiss / "getprop.txt").write_text(gp, encoding="utf-8", newline="\n")
usbcan = copy_fix("r7-usbcan")
(usbcan / "cat-wlan-uevent.txt").write_text("DRIVER=notadriver\nPRODUCT=r6usbowner9f3a\n", encoding="utf-8", newline="\n")
mut = root / "r7-mutated-report.sh"
src = pathlib.Path(script).read_text(encoding="utf-8")
old = '    prod = ue.get("PRODUCT") or ""\n'
new = '    prod = ue.get("PRODUCT") or ""\n    if prod:\n        return "usb " + prod\n'
if old not in src:
    sys.stderr.write("usb mutation anchor missing\n")
    raise SystemExit(1)
mut.write_text(src.replace(old, new, 1), encoding="utf-8", newline="\n")
print("r7 fixtures ready")
PY
then
    ok "round 7 fixtures were built"
else
    bad "round 7 fixtures were built"
fi

word_ok=1
for word in card0 hci0 renderD128 CL3B R1 real-gpu probe-only marker state image; do
    if ! bash "$R" --fixture "$T/r7-word-$word" --probe "$FIX/kano-probe" --out "$T/r7-word-$word-out" --date 20261014 > /dev/null \
        || ! agree_files "$T/r7-word-$word-out" \
        || ! grep -F -q 'redactions: 0' "$T/r7-word-$word-out/gbos-report-20261014.txt" \
        || ! grep -F -q 'CL3B.260622.271.R1' "$T/r7-word-$word-out/summary.md" \
        || ! grep -F -q '8086:46a6' "$T/r7-word-$word-out/summary.md" \
        || ! grep -F -q 'card0' "$T/r7-word-$word-out/gbos-report-20261014.txt" \
        || ! grep -F -q '未知（沒有映像標記）' "$T/r7-word-$word-out/summary.md" \
        || ! grep -F -q 'unknown (no image marker)' "$T/r7-word-$word-out/gbos-report-20261014.txt"; then
        word_ok=0
        bad "hostname or serial $word keeps the printed facts"
    fi
done
[ "$word_ok" = 1 ] && ok "hostname or serial equal to a printed word keeps both files"
if bash "$R" --fixture "$T/r7-serial-ids" --out "$T/r7-serial-ids-out" --date 20261014 > /dev/null \
    && agree_files "$T/r7-serial-ids-out" \
    && grep -F -q '8086:46a6' "$T/r7-serial-ids-out/summary.md" \
    && grep -F -q '8086:46a6' "$T/r7-serial-ids-out/gbos-report-20261014.txt" \
    && grep -F -q '04f3:323c' "$T/r7-serial-ids-out/summary.md" \
    && grep -F -q '04f3:2d5b' "$T/r7-serial-ids-out/summary.md" \
    && grep -F -q 'redactions: 0' "$T/r7-serial-ids-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r7-frag-04f3" --out "$T/r7-frag-04f3-out" --date 20261014 > /dev/null \
    && grep -F -q '04f3:323c' "$T/r7-frag-04f3-out/summary.md" \
    && grep -F -q '04f3:2d5b' "$T/r7-frag-04f3-out/summary.md" \
    && grep -F -q 'redactions: 0' "$T/r7-frag-04f3-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r7-frag-323c" --out "$T/r7-frag-323c-out" --date 20261014 > /dev/null \
    && grep -F -q '04f3:323c' "$T/r7-frag-323c-out/summary.md" \
    && bash "$R" --fixture "$T/r7-frag-2d5b" --out "$T/r7-frag-2d5b-out" --date 20261014 > /dev/null \
    && grep -F -q '04f3:2d5b' "$T/r7-frag-2d5b-out/summary.md" \
    && bash "$R" --fixture "$T/r7-hid" --probe "$T/r7-hid-probe" --out "$T/r7-hid-out" --date 20261014 > /dev/null \
    && grep -F -q 'driver i2c_hid' "$T/r7-hid-out/gbos-report-20261014.txt" \
    && grep -F -q '驅動 i2c_hid' "$T/r7-hid-out/summary.md" \
    && grep -F -q 'redactions: 0' "$T/r7-hid-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r7-kern" --out "$T/r7-kern-out" --date 20261014 > /dev/null \
    && grep -F -q '6.18.21-android17-5-g3938c8d97142-ab16302624' "$T/r7-kern-out/summary.md" \
    && grep -F -q 'redactions: 0' "$T/r7-kern-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r7-ghash" --out "$T/r7-ghash-out" --date 20261014 > /dev/null \
    && grep -F -q '6.18.21-android17-5-g3938c8d97142-ab16302624' "$T/r7-ghash-out/summary.md" \
    && bash "$R" --fixture "$T/r7-ab" --out "$T/r7-ab-out" --date 20261014 > /dev/null \
    && grep -F -q '6.18.21-android17-5-g3938c8d97142-ab16302624' "$T/r7-ab-out/summary.md"; then
    ok "a PCI fragment, a closed driver, or a shipped kernel hash is not deleted"
else
    bad "a PCI fragment, a closed driver, or a shipped kernel hash is not deleted"
fi
if bash "$R" --fixture "$T/r7-nokey-empty" --probe "$T/r7-nokey-empty-probe" --out "$T/r7-nokey-empty-out" --date 20261014 > /dev/null \
    && grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/r7-nokey-empty-out/summary.md" \
    && ! grep -F -q '一致：DMI' "$T/r7-nokey-empty-out/summary.md" \
    && ! grep -F -q 'UEFI yes' "$T/r7-nokey-empty-out/summary.md" \
    && bash "$R" --fixture "$T/r7-nokey-devs" --probe "$T/r7-nokey-devs-probe" --out "$T/r7-nokey-devs-out" --date 20261014 > /dev/null \
    && grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/r7-nokey-devs-out/summary.md" \
    && ! grep -F -q '一致：DMI' "$T/r7-nokey-devs-out/summary.md" \
    && bash "$R" --fixture "$T/r7-null" --probe "$T/r7-null-probe" --out "$T/r7-null-out" --date 20261014 > /dev/null \
    && grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/r7-null-out/summary.md" \
    && ! grep -F -q 'UEFI yes' "$T/r7-null-out/summary.md" \
    && bash "$R" --fixture "$T/r7-missing-file" --probe "$T/r7-missing-file-probe" --out "$T/r7-missing-file-out" --date 20261014 > /dev/null \
    && grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/r7-missing-file-out/summary.md" \
    && ! grep -F -q '一致：DMI' "$T/r7-missing-file-out/summary.md" \
    && bash "$R" --fixture "$T/r7-both-empty" --probe "$T/r7-both-empty-probe" --out "$T/r7-both-empty-out" --date 20261014 > /dev/null \
    && grep -F -q '一致：DMI、CPU、PCI' "$T/r7-both-empty-out/summary.md" \
    && grep -F -q 'UEFI yes, Secure Boot off' "$T/r7-both-empty-out/summary.md"; then
    ok "a missing PCI list does not merge, and a read empty list can"
else
    bad "a missing PCI list does not merge, and a read empty list can"
fi
if bash "$R" --fixture "$T/r7-class-ok" --out "$T/r7-class-ok-out" --date 20261014 > /dev/null \
    && grep -F -q 'touchpad: dead:beef (i2c)' "$T/r7-class-ok-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r7-class-sib" --out "$T/r7-class-sib-out" --date 20261014 > /dev/null \
    && grep -F -q 'touchpad: dead:beef (i2c)' "$T/r7-class-sib-out/gbos-report-20261014.txt" \
    && ! grep -R -F -q -- 'example.test' "$T/r7-class-sib-out" \
    && bash "$R" --fixture "$T/r7-src-sib" --out "$T/r7-src-sib-out" --date 20261014 > /dev/null \
    && grep -F -q 'touchscreen: dead:beef (i2c)' "$T/r7-src-sib-out/gbos-report-20261014.txt" \
    && ! grep -R -F -q -- 'example.test' "$T/r7-src-sib-out" \
    && bash "$R" --fixture "$T/r7-class-sym" --out "$T/r7-class-sym-out" --date 20261014 > /dev/null \
    && grep -F -q 'touchpad: dead:beef (i2c)' "$T/r7-class-sym-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r7-src-ok" --out "$T/r7-src-ok-out" --date 20261014 > /dev/null \
    && grep -F -q 'touchscreen: dead:beef (i2c)' "$T/r7-src-ok-out/gbos-report-20261014.txt" \
    && bash "$R" --probe "$T/r7-kind-probe" --out "$T/r7-kind-out" --date 20261014 > /dev/null \
    && grep -F -q 'touchpad: 04f3:323c (i2c)' "$T/r7-kind-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r7-class-suffix" --out "$T/r7-class-suffix-out" --date 20261014 > /dev/null \
    && ! grep -F -q 'dead:beef' "$T/r7-class-suffix-out/summary.md" \
    && ! grep -R -F -q -- 'example.test' "$T/r7-class-suffix-out" \
    && bash "$R" --fixture "$T/r7-class-space" --out "$T/r7-class-space-out" --date 20261014 > /dev/null \
    && ! grep -F -q 'dead:beef' "$T/r7-class-space-out/summary.md" \
    && bash "$R" --fixture "$T/r7-class-tail" --out "$T/r7-class-tail-out" --date 20261014 > /dev/null \
    && ! grep -F -q 'dead:beef' "$T/r7-class-tail-out/summary.md" \
    && bash "$R" --fixture "$T/r7-src-suffix" --out "$T/r7-src-suffix-out" --date 20261014 > /dev/null \
    && ! grep -F -q 'dead:beef' "$T/r7-src-suffix-out/summary.md" \
    && ! grep -R -F -q -- 'example.test' "$T/r7-src-suffix-out" \
    && bash "$R" --fixture "$T/r7-sym-suffix" --out "$T/r7-sym-suffix-out" --date 20261014 > /dev/null \
    && ! grep -F -q 'dead:beef' "$T/r7-sym-suffix-out/summary.md" \
    && bash "$R" --probe "$T/r7-badkind-probe" --out "$T/r7-badkind-out" --date 20261014 > /dev/null \
    && ! grep -F -q '04f3:323c' "$T/r7-badkind-out/summary.md" \
    && ! grep -R -F -q -- 'example.test' "$T/r7-badkind-out" \
    && bash "$R" --probe "$T/r7-padhex-probe" --out "$T/r7-padhex-out" --date 20261014 > /dev/null \
    && ! grep -F -q '04f3:323c' "$T/r7-padhex-out/summary.md" \
    && ! grep -R -F -q -- '02aabbccddee' "$T/r7-padhex-out"; then
    ok "input kind and capability bits reject a suffix and accept a whole token"
else
    bad "input kind and capability bits reject a suffix and accept a whole token"
fi
if bash "$R" --fixture "$T/r7-wifi-usb" --out "$T/r7-wifi-usb-out" --date 20261014 > /dev/null \
    && grep -F -q 'unknown, driver other, usb 0bda:b00b' "$T/r7-wifi-usb-out/gbos-report-20261014.txt" \
    && grep -F -q '未知，驅動 other, usb 0bda:b00b' "$T/r7-wifi-usb-out/summary.md" \
    && ! grep -F -q '已連線' "$T/r7-wifi-usb-out/summary.md" \
    && bash "$R" --fixture "$T/r7-wifi-pci" --out "$T/r7-wifi-pci-out" --date 20261014 > /dev/null \
    && grep -F -q 'unknown, driver other, pci 8086:51f0' "$T/r7-wifi-pci-out/gbos-report-20261014.txt" \
    && grep -F -q '未知，驅動 other, pci 8086:51f0' "$T/r7-wifi-pci-out/summary.md" \
    && bash "$R" --fixture "$T/r7-bt-usb" --out "$T/r7-bt-usb-out" --date 20261014 > /dev/null \
    && grep -F -q 'service running, power unknown, driver other, usb 0bda:b00c' "$T/r7-bt-usb-out/gbos-report-20261014.txt" \
    && grep -F -q '服務執行中，開關未確認，驅動 other, usb 0bda:b00c' "$T/r7-bt-usb-out/summary.md" \
    && bash "$R" --fixture "$T/r7-bt-turn" --out "$T/r7-bt-turn-out" --date 20261014 > /dev/null \
    && grep -F -q 'turning on, driver other, usb 0bda:b00c' "$T/r7-bt-turn-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r7-bt-miss" --out "$T/r7-bt-miss-out" --date 20261014 > /dev/null \
    && grep -F -q 'unknown, driver other, pci 8086:51f1' "$T/r7-bt-miss-out/gbos-report-20261014.txt" \
    && grep -F -q '未知，驅動 other, pci 8086:51f1' "$T/r7-bt-miss-out/summary.md" \
    && ! grep -F -q '| 藍牙 | 已開啟 |' "$T/r7-bt-miss-out/summary.md"; then
    ok "an unknown radio state still reports the driver and the chip id"
else
    bad "an unknown radio state still reports the driver and the chip id"
fi
if bash "$R" --fixture "$T/r7-usbcan" --out "$T/r7-usbcan-out" --date 20261014 > /dev/null \
    && ! grep -R -F -q -- 'r6usbowner9f3a' "$T/r7-usbcan-out" \
    && bash "$T/r7-mutated-report.sh" --fixture "$T/r7-usbcan" --out "$T/r7-usbcan-mut" --date 20261014 > /dev/null \
    && grep -R -F -q -- 'r6usbowner9f3a' "$T/r7-usbcan-mut"; then
    ok "a raw USB PRODUCT mutation is visible to the canary and absent before it"
else
    bad "a raw USB PRODUCT mutation is visible to the canary and absent before it"
fi

# Round 8: a dev release is not vocabulary, printed words stay, a missing capture cannot merge.
if python3 - "$R" "$FIX/kano" "$FIX/kano-probe" "$T" << 'PY'
import json, pathlib, shutil, sys
script, fix_s, probe_s, root_s = sys.argv[1:]
root = pathlib.Path(root_s)
fix = pathlib.Path(fix_s)
probe_src = pathlib.Path(probe_s)
exp = fix.parent.parent / "expected"

def copy_fix(name):
    dest = root / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(fix, dest)
    return dest

def copy_probe(name):
    dest = root / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(probe_src, dest)
    return dest

def set_prop(path, key, value):
    text = path.read_text(encoding="utf-8")
    line = "[%s]: [%s]" % (key, value)
    prefix = "[" + key + "]:"
    lines, found = [], False
    for old in text.splitlines():
        if old.startswith(prefix):
            lines.append(line)
            found = True
        else:
            lines.append(old)
    if not found:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

def host_serial(dest, word, serial=None):
    set_prop(dest / "getprop.txt", "net.hostname", word)
    set_prop(dest / "getprop.txt", "ro.serialno", serial if serial is not None else word)
    if " " not in word:
        uname = (dest / "uname-a.txt").read_text(encoding="utf-8")
        uname = uname.replace("synth-laptop", word, 1)
        (dest / "uname-a.txt").write_text(uname, encoding="utf-8", newline="\n")
        ver = (dest / "cat-proc-version.txt").read_text(encoding="utf-8")
        ver = ver.replace("synthuser@synth-build", "synthuser@" + word, 1)
        (dest / "cat-proc-version.txt").write_text(ver, encoding="utf-8", newline="\n")

def set_release(dest, release, host=None):
    uname = (dest / "uname-a.txt").read_text(encoding="utf-8")
    if host is not None:
        uname = uname.replace("synth-laptop", host, 1)
    uname = uname.replace("6.1.75-android14", release, 1)
    (dest / "uname-a.txt").write_text(uname, encoding="utf-8", newline="\n")

def krel(name, text):
    dest = root / name / "include" / "config"
    dest.mkdir(parents=True)
    (dest / "kernel.release").write_text(text + "\n", encoding="utf-8", newline="\n")

def empty_pci_probe(name):
    dest = copy_fix(name)
    pr = copy_probe(name + "-probe")
    data = json.loads((pr / "probe.json").read_text(encoding="utf-8"))
    data["pci"] = []
    (pr / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
    return dest

host = copy_fix("r8-dev-host")
set_prop(host / "getprop.txt", "net.hostname", "alicehost")
set_release(host, "6.1.75-alicehost", "alicehost")
ver = (host / "cat-proc-version.txt").read_text(encoding="utf-8")
ver = ver.replace("synthuser@synth-build", "synthuser@alicehost", 1)
(host / "cat-proc-version.txt").write_text(ver, encoding="utf-8", newline="\n")
krel("r8-krel-host", "6.1.75-alicehost")

serial = copy_fix("r8-dev-serial")
set_release(serial, "6.1.75-SYNTHSERIAL001")
krel("r8-krel-serial", "6.1.75-SYNTHSERIAL001")

hx = copy_fix("r8-dev-hex")
set_prop(hx / "getprop.txt", "ro.serialno", "02aabbccddee")
set_release(hx, "6.1.75-g02aabbccddee")
krel("r8-krel-hex", "6.1.75-g02aabbccddee")

clean = copy_fix("r8-dev-clean")
set_release(clean, "6.1.75-r8devlocal")
krel("r8-krel-clean", "6.1.75-r8devlocal")

host_other = copy_fix("r8-host-other")
host_serial(host_other, "other", serial="SYNTHSERIAL001")
serial_other = copy_fix("r8-serial-other")
set_prop(serial_other / "getprop.txt", "ro.serialno", "other")

wifi = copy_fix("r8-wifi-other")
set_prop(wifi / "getprop.txt", "ro.serialno", "other")
(wifi / "dumpsys-wifi.txt").unlink()
(wifi / "cat-wlan-operstate.txt").unlink()
(wifi / "cat-wlan-uevent.txt").write_text(
    "DRIVER=notadriver\nPRODUCT=bda/b00b/001\n", encoding="utf-8", newline="\n")

gen = copy_fix("r8-generic-other")
set_prop(gen / "getprop.txt", "ro.serialno", "other")
(gen / "cat-dmi-sys-vendor.txt").write_text("AcmeCo\n", encoding="utf-8", newline="\n")
(gen / "cat-dmi-board-vendor.txt").write_text("AcmeCo\n", encoding="utf-8", newline="\n")
(gen / "cat-dmi-bios-vendor.txt").write_text("AcmeBios\n", encoding="utf-8", newline="\n")
cpu = (gen / "cat-proc-cpuinfo.txt").read_text(encoding="utf-8")
cpu = cpu.replace("vendor_id: GenuineIntel", "vendor_id: HygonGenuine", 1)
(gen / "cat-proc-cpuinfo.txt").write_text(cpu, encoding="utf-8", newline="\n")
(gen / "cat-drm0-uevent.txt").write_text(
    "DRIVER=strangegpu\nPCI_ID=1234:5678\n", encoding="utf-8", newline="\n")
(gen / "cat-wlan-uevent.txt").write_text(
    "DRIVER=notadriver\nPCI_ID=8086:51F0\n", encoding="utf-8", newline="\n")
(gen / "cat-bt-uevent.txt").write_text(
    "DRIVER=notabtdrv\nPRODUCT=bda/b00c/001\n", encoding="utf-8", newline="\n")

for name, word in (
    ("r8-cjk-mei", "沒有"),
    ("r8-cjk-qu", "驅"),
    ("r8-cjk-zhi", "知"),
    ("r8-cjk-agree", "一致"),
):
    dest = copy_fix(name)
    host_serial(dest, word)

stale = empty_pci_probe("r8-stale-rc0")
(stale / "cat-proc-bus-pci-devices.txt").unlink()
(stale / "cat-proc-bus-pci-devices.rc").write_text("0\n", encoding="utf-8", newline="\n")
failrc = empty_pci_probe("r8-stale-rc1")
(failrc / "cat-proc-bus-pci-devices.txt").unlink()
(failrc / "cat-proc-bus-pci-devices.rc").write_text("1\n", encoding="utf-8", newline="\n")
empty = empty_pci_probe("r8-empty-pci")
(empty / "cat-proc-bus-pci-devices.txt").write_text("", encoding="utf-8", newline="\n")
(empty / "cat-proc-bus-pci-devices.rc").write_text("0\n", encoding="utf-8", newline="\n")

def input_case(name, dumpsys):
    dest = copy_fix(name)
    (dest / "dumpsys-input.txt").write_text(dumpsys, encoding="utf-8", newline="\n")
    (dest / "cat-proc-bus-input-devices.txt").write_text("", encoding="utf-8", newline="\n")
    return dest

input_case(
    "r8-stylus",
    "1: Generic Device\n  Classes: EXTERNAL_STYLUS\n"
    "  Identifier: bus=0x0005 vendor=0x1234 product=0x5678\n")
input_case(
    "r8-stylus-suffix",
    "1: Generic Device\n  Classes: EXTERNAL_STYLUS@example.test\n"
    "  Identifier: bus=0x0005 vendor=0x1234 product=0x5678\n")
input_case(
    "r8-id-ok",
    "1: Touchpad\n  Classes: TOUCHPAD\n"
    "  Identifier: bus=0x0018 vendor=0x04f3 product=0x323c\n")
input_case(
    "r8-id-canary",
    "1: Touchpad\n  Classes: TOUCHPAD\n"
    "  Identifier: bus=0x0018 vendor=0xr8idowner9f3a product=0x323c\n")

pci_serial = copy_fix("r8-pci-serial")
set_prop(pci_serial / "getprop.txt", "ro.serialno", "8086:46a6")

def copy_expected(name):
    dest = root / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir()
    shutil.copy(exp / "gbos-report-20261014.txt", dest / "gbos-report-20261014.txt")
    shutil.copy(exp / "summary.md", dest / "summary.md")
    return dest

copy_expected("r8-agree-base")
num = copy_expected("r8-agree-num")
text = (num / "summary.md").read_text(encoding="utf-8")
old = "成功 4、失敗 1"
if old not in text:
    sys.stderr.write("agree number anchor missing\n")
    raise SystemExit(1)
(num / "summary.md").write_text(text.replace(old, "成功 999、失敗 1", 1), encoding="utf-8", newline="\n")
wifi_md = copy_expected("r8-agree-wifi")
text = (wifi_md / "summary.md").read_text(encoding="utf-8")
old = "已連線，驅動 iwlwifi"
if old not in text:
    sys.stderr.write("agree wifi anchor missing\n")
    raise SystemExit(1)
(wifi_md / "summary.md").write_text(text.replace(old, "已關閉，驅動 iwlwifi", 1), encoding="utf-8", newline="\n")
pci_md = copy_expected("r8-agree-pci")
text = (pci_md / "summary.md").read_text(encoding="utf-8")
old_gpu = "| GPU PCI | 8086:46a6 | fixture |"
old_wifi = "| Wi-Fi | 已連線，驅動 iwlwifi | fixture |"
if old_gpu not in text or old_wifi not in text:
    sys.stderr.write("agree pci anchor missing\n")
    raise SystemExit(1)
text = text.replace(old_gpu, "| GPU PCI | none | fixture |", 1)
text = text.replace(old_wifi, "| Wi-Fi | 已連線，驅動 iwlwifi 8086:46a6 | fixture |", 1)
(pci_md / "summary.md").write_text(text, encoding="utf-8", newline="\n")

src = pathlib.Path(script).read_text(encoding="utf-8")
repls = (
    ("vendor=0x([0-9a-f]{1,4})", "vendor=0x([^,\\s]+)"),
    (
        '    elif not re.fullmatch(r"[0-9a-fA-F]{1,4}", raw):\n        return ""',
        '    elif not re.fullmatch(r"[0-9a-fA-F]{1,4}", raw):\n        return raw.lower()',
    ),
    (
        "or ident_exact(label, banned) or not PCI_RE.fullmatch(label)):",
        "or ident_exact(label, banned)):",
    ),
)
for old, new in repls:
    if old not in src:
        sys.stderr.write("identifier mutation anchor missing\n")
        raise SystemExit(1)
    src = src.replace(old, new, 1)
(root / "r8-idmut.sh").write_text(src, encoding="utf-8", newline="\n")
print("r8 fixtures ready")
PY
then
    ok "round 8 fixtures were built"
else
    bad "round 8 fixtures were built"
fi

if GBOS_DEV_KERNEL_DIR="$T/r8-krel-host" bash "$R" --fixture "$T/r8-dev-host" --out "$T/r8-dev-host-out" --date 20261014 > /dev/null \
    && grep -F -q 'kernel: dev-kernel [fixture]' "$T/r8-dev-host-out/gbos-report-20261014.txt" \
    && grep -F -q 'redactions: 0' "$T/r8-dev-host-out/gbos-report-20261014.txt" \
    && ! grep -R -F -q -- 'alicehost' "$T/r8-dev-host-out" \
    && ! grep -R -F -q -- '6.1.75-alicehost' "$T/r8-dev-host-out" \
    && GBOS_DEV_KERNEL_DIR="$T/r8-krel-serial" bash "$R" --fixture "$T/r8-dev-serial" --out "$T/r8-dev-serial-out" --date 20261014 > /dev/null \
    && grep -F -q 'kernel: dev-kernel [fixture]' "$T/r8-dev-serial-out/gbos-report-20261014.txt" \
    && grep -F -q 'redactions: 0' "$T/r8-dev-serial-out/gbos-report-20261014.txt" \
    && ! grep -R -F -q -- 'SYNTHSERIAL001' "$T/r8-dev-serial-out" \
    && ! grep -R -F -q -- '6.1.75-SYNTHSERIAL001' "$T/r8-dev-serial-out" \
    && GBOS_DEV_KERNEL_DIR="$T/r8-krel-hex" bash "$R" --fixture "$T/r8-dev-hex" --out "$T/r8-dev-hex-out" --date 20261014 > /dev/null \
    && grep -F -q 'kernel: dev-kernel [fixture]' "$T/r8-dev-hex-out/gbos-report-20261014.txt" \
    && grep -F -q 'redactions: 0' "$T/r8-dev-hex-out/gbos-report-20261014.txt" \
    && ! grep -R -F -q -- '02aabbccddee' "$T/r8-dev-hex-out" \
    && ! grep -R -F -q -- '6.1.75-g02aabbccddee' "$T/r8-dev-hex-out" \
    && GBOS_DEV_KERNEL_DIR="$T/r8-krel-clean" bash "$R" --fixture "$T/r8-dev-clean" --out "$T/r8-dev-clean-out" --date 20261014 > /dev/null \
    && grep -F -q 'kernel: dev-kernel [fixture]' "$T/r8-dev-clean-out/gbos-report-20261014.txt" \
    && grep -F -q '| 核心 | dev-kernel |' "$T/r8-dev-clean-out/summary.md" \
    && ! grep -R -F -q -- '6.1.75-r8devlocal' "$T/r8-dev-clean-out" \
    && grep -F -q 'redactions: 0' "$T/r8-dev-clean-out/gbos-report-20261014.txt" \
    && agree_files "$T/r8-dev-clean-out"; then
    ok "a dev kernel release is never printed"
else
    bad "a dev kernel release is never printed"
fi

if bash "$R" --fixture "$FIX/kano" --probe "$FIX/kano-probe" --out "$T/r8-base" --date 20261014 > /dev/null \
    && bash "$R" --fixture "$T/r8-host-other" --probe "$FIX/kano-probe" --out "$T/r8-host-other-out" --date 20261014 > /dev/null \
    && cmp -s "$T/r8-base/gbos-report-20261014.txt" "$T/r8-host-other-out/gbos-report-20261014.txt" \
    && cmp -s "$T/r8-base/summary.md" "$T/r8-host-other-out/summary.md" \
    && bash "$R" --fixture "$T/r8-serial-other" --probe "$FIX/kano-probe" --out "$T/r8-serial-other-out" --date 20261014 > /dev/null \
    && cmp -s "$T/r8-base/gbos-report-20261014.txt" "$T/r8-serial-other-out/gbos-report-20261014.txt" \
    && cmp -s "$T/r8-base/summary.md" "$T/r8-serial-other-out/summary.md" \
    && grep -F -q 'kernel: other' "$T/r8-serial-other-out/gbos-report-20261014.txt" \
    && grep -F -q 'redactions: 0' "$T/r8-serial-other-out/gbos-report-20261014.txt"; then
    ok "hostname or serial other keeps kernel other and the rest of the body"
else
    bad "hostname or serial other keeps kernel other and the rest of the body"
fi

if bash "$R" --fixture "$T/r8-wifi-other" --out "$T/r8-wifi-other-out" --date 20261014 > /dev/null \
    && grep -F -q 'unknown, driver other, usb 0bda:b00b' "$T/r8-wifi-other-out/gbos-report-20261014.txt" \
    && grep -F -q '未知，驅動 other, usb 0bda:b00b' "$T/r8-wifi-other-out/summary.md" \
    && grep -F -q 'kernel: other' "$T/r8-wifi-other-out/gbos-report-20261014.txt" \
    && grep -F -q 'redactions: 0' "$T/r8-wifi-other-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r8-generic-other" --out "$T/r8-generic-other-out" --date 20261014 > /dev/null \
    && agree_files "$T/r8-generic-other-out" \
    && grep -F -q 'model: other' "$T/r8-generic-other-out/gbos-report-20261014.txt" \
    && grep -F -q 'board: other' "$T/r8-generic-other-out/gbos-report-20261014.txt" \
    && grep -F -q 'firmware: other' "$T/r8-generic-other-out/gbos-report-20261014.txt" \
    && grep -F -q 'kernel: other' "$T/r8-generic-other-out/gbos-report-20261014.txt" \
    && grep -F -q 'display/gpu: unknown (1234:5678, other)' "$T/r8-generic-other-out/gbos-report-20261014.txt" \
    && grep -F -q 'drm cards: card0 driver other pci 1234:5678' "$T/r8-generic-other-out/gbos-report-20261014.txt" \
    && grep -F -q 'cpu: other family 6 model 154 stepping 3' "$T/r8-generic-other-out/gbos-report-20261014.txt" \
    && grep -F -q 'wifi: connected, driver other, pci 8086:51f0' "$T/r8-generic-other-out/gbos-report-20261014.txt" \
    && grep -F -q 'bluetooth: on, driver other, usb 0bda:b00c' "$T/r8-generic-other-out/gbos-report-20261014.txt" \
    && grep -F -q '未知（1234:5678, other）' "$T/r8-generic-other-out/summary.md" \
    && grep -F -q '已連線，驅動 other, pci 8086:51f0' "$T/r8-generic-other-out/summary.md" \
    && grep -F -q '已開啟，驅動 other, usb 0bda:b00c' "$T/r8-generic-other-out/summary.md" \
    && grep -F -q 'redactions: 0' "$T/r8-generic-other-out/gbos-report-20261014.txt"; then
    ok "unknown hardware printed as other stays when the serial is other"
else
    bad "unknown hardware printed as other stays when the serial is other"
fi

cjk_ok=1
for name in r8-cjk-mei r8-cjk-qu r8-cjk-zhi r8-cjk-agree; do
    if ! bash "$R" --fixture "$T/$name" --probe "$FIX/kano-probe" --out "$T/$name-out" --date 20261014 > /dev/null \
        || ! cmp -s "$T/r8-base/gbos-report-20261014.txt" "$T/$name-out/gbos-report-20261014.txt" \
        || ! cmp -s "$T/r8-base/summary.md" "$T/$name-out/summary.md" \
        || ! grep -F -q 'redactions: 0' "$T/$name-out/gbos-report-20261014.txt"; then
        cjk_ok=0
        bad "serial or hostname $name keeps the body"
    fi
done
[ "$cjk_ok" = 1 ] && ok "a CJK fragment equal to part of a printed phrase keeps the body"

if bash "$R" --fixture "$T/r8-stale-rc0" --probe "$T/r8-stale-rc0-probe" --out "$T/r8-stale-rc0-out" --date 20261014 > /dev/null \
    && grep -F -q 'omitted (probe is not the same machine)' "$T/r8-stale-rc0-out/gbos-report-20261014.txt" \
    && grep -F -q '已省略（probe 與這台裝置不是同一台）' "$T/r8-stale-rc0-out/summary.md" \
    && ! grep -F -q 'agree: DMI' "$T/r8-stale-rc0-out/gbos-report-20261014.txt" \
    && ! grep -F -q '一致：DMI' "$T/r8-stale-rc0-out/summary.md" \
    && ! grep -F -q 'UEFI yes' "$T/r8-stale-rc0-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r8-stale-rc1" --probe "$T/r8-stale-rc1-probe" --out "$T/r8-stale-rc1-out" --date 20261014 > /dev/null \
    && grep -F -q 'omitted (probe is not the same machine)' "$T/r8-stale-rc1-out/gbos-report-20261014.txt" \
    && ! grep -F -q 'agree: DMI' "$T/r8-stale-rc1-out/gbos-report-20261014.txt" \
    && ! grep -F -q 'UEFI yes' "$T/r8-stale-rc1-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r8-empty-pci" --probe "$T/r8-empty-pci-probe" --out "$T/r8-empty-pci-out" --date 20261014 > /dev/null \
    && grep -F -q 'agree: DMI, CPU, PCI' "$T/r8-empty-pci-out/gbos-report-20261014.txt" \
    && grep -F -q '一致：DMI、CPU、PCI' "$T/r8-empty-pci-out/summary.md" \
    && grep -F -q 'UEFI yes, Secure Boot off' "$T/r8-empty-pci-out/gbos-report-20261014.txt"; then
    ok "a missing PCI capture stays unread even when a success rc is left behind"
else
    bad "a missing PCI capture stays unread even when a success rc is left behind"
fi

if bash "$R" --fixture "$T/r8-stylus" --out "$T/r8-stylus-out" --date 20261014 > /dev/null \
    && grep -F -q 'stylus: 1234:5678 (bluetooth)' "$T/r8-stylus-out/gbos-report-20261014.txt" \
    && grep -F -q '觸控筆：1234:5678（bluetooth）' "$T/r8-stylus-out/summary.md" \
    && bash "$R" --fixture "$T/r8-stylus-suffix" --out "$T/r8-stylus-suffix-out" --date 20261014 > /dev/null \
    && ! grep -F -q '1234:5678' "$T/r8-stylus-suffix-out/gbos-report-20261014.txt" \
    && ! grep -R -F -q -- 'example.test' "$T/r8-stylus-suffix-out"; then
    ok "EXTERNAL_STYLUS is a stylus and a suffix is not"
else
    bad "EXTERNAL_STYLUS is a stylus and a suffix is not"
fi

if bash "$R" --fixture "$T/r8-pci-serial" --probe "$FIX/kano-probe" --out "$T/r8-pci-serial-out" --date 20261014 > /dev/null \
    && agree_files "$T/r8-pci-serial-out" \
    && grep -F -q 'redactions: 3' "$T/r8-pci-serial-out/gbos-report-20261014.txt" \
    && grep -F -q '遮罩 3 行' "$T/r8-pci-serial-out/summary.md" \
    && grep -F -q 'gpu pci: unknown' "$T/r8-pci-serial-out/gbos-report-20261014.txt" \
    && grep -F -q 'drm cards: unknown' "$T/r8-pci-serial-out/gbos-report-20261014.txt" \
    && grep -F -q 'live linux gpu: unknown' "$T/r8-pci-serial-out/gbos-report-20261014.txt" \
    && ! grep -R -F -q -- '8086:46a6' "$T/r8-pci-serial-out"; then
    ok "a serial equal to a whole PCI id still redacts the rows that print that id"
else
    bad "a serial equal to a whole PCI id still redacts the rows that print that id"
fi

if agree_files "$T/r8-agree-base" \
    && ! agree_files "$T/r8-agree-num" \
    && ! agree_files "$T/r8-agree-wifi" \
    && ! agree_files "$T/r8-agree-pci"; then
    ok "the two files must agree on each row's numbers, words, and PCI ids"
else
    bad "the two files must agree on each row's numbers, words, and PCI ids"
fi

if bash "$R" --fixture "$T/r8-id-ok" --out "$T/r8-id-ok-out" --date 20261014 > /dev/null \
    && grep -F -q 'touchpad: 04f3:323c (i2c)' "$T/r8-id-ok-out/gbos-report-20261014.txt" \
    && bash "$R" --fixture "$T/r8-id-canary" --out "$T/r8-id-canary-out" --date 20261014 > /dev/null \
    && ! grep -R -F -q -- 'r8idowner9f3a' "$T/r8-id-canary-out" \
    && bash "$T/r8-idmut.sh" --fixture "$T/r8-id-canary" --out "$T/r8-id-canary-mut" --date 20261014 > /dev/null \
    && grep -R -F -q -- 'r8idowner9f3a' "$T/r8-id-canary-mut"; then
    ok "an Identifier vendor mutation is visible only after the canary can reach that field"
else
    bad "an Identifier vendor mutation is visible only after the canary can reach that field"
fi

# Round 9: dev-kernel never prints the release, a missing DRM capture does not merge,
# driver unread stays a report word, and the shipped script has no input dump.
if ! grep -F -q 'GBOS_REPORT_INPUTS' "$R" \
    && ! grep -F -q 'flush_consumed' "$R" \
    && ! grep -F -q 'def consume' "$R" \
    && ! grep -F -q 'write_text' "$R" \
    && ! grep -F -q 'def shipped_kernels' "$R" \
    && ! grep -F -q 'def release_embeds_banned' "$R"; then
    ok "the shipped report has no test hook and no dead kernel helper"
else
    bad "the shipped report has no test hook and no dead kernel helper"
fi
mkdir -p "$T/r9-keep"
printf '%s\n' 'KEEP' > "$T/r9-keep/sentinel"
ln -s "$T/r9-keep/sentinel" "$T/r9-inputs-link"
if GBOS_REPORT_INPUTS="$T/r9-inputs-link" bash "$R" --fixture "$FIX/kano" --probe "$FIX/kano-probe" --out "$T/r9-hook-out" --date 20261014 > /dev/null \
    && [ "$(cat "$T/r9-keep/sentinel")" = KEEP ] \
    && [ ! -e "$T/r9-inputs-link-written" ] \
    && ! find "$T/r9-hook-out" -type f ! -name 'gbos-report-20261014.txt' ! -name 'summary.md' | grep -q .; then
    ok "GBOS_REPORT_INPUTS does not write or follow a symlink"
else
    bad "GBOS_REPORT_INPUTS does not write or follow a symlink"
fi
if python3 - "$R" > "$T/hook-check.txt" << 'PY'
import ast, pathlib, re, sys
script = sys.argv[1]
text = pathlib.Path(script).read_text(encoding="utf-8")
head, rest = text.split("<< 'PY'\n", 1)
py, tail = rest.rsplit("\nPY\n", 1)
bash = head + "\n" + tail
allowed_env = {"GBOS_DEV_KERNEL_DIR", "ANDROID_SERIAL"}
write_flag_names = {"O_WRONLY", "O_CREAT", "O_APPEND", "O_RDWR", "O_TRUNC"}
write_modes = {"w", "wb", "a", "ab", "w+", "a+", "r+", "rb+", "x", "xb"}
os_write_names = {
    "replace", "rename", "link", "symlink", "unlink", "remove",
    "system", "popen", "makedirs", "truncate", "renames", "chmod",
}
os_call_ok = {
    "open", "close", "fstat", "lstat", "readlink", "dup", "mkdir", "rmdir",
    "listdir", "fdopen", "read", "getenv",
}
path_writes = {"touch", "symlink_to", "unlink", "rename", "write_text", "write_bytes", "hardlink_to", "link_to"}

def literal_name(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None

def os_aliases_of(tree):
    names = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    names.add(alias.asname or alias.name)
    return names

def is_os_environ(node, aliases):
    return (
        isinstance(node, ast.Attribute) and node.attr in ("environ", "environb")
        and isinstance(node.value, ast.Name) and node.value.id in aliases
    )

def expr_has_int(node):
    return any(isinstance(n, ast.Constant) and isinstance(n.value, int) and not isinstance(n.value, bool) for n in ast.walk(node))

def expr_has_write(node, func, seen=None):
    if node is None:
        return False
    seen = seen or set()
    if isinstance(node, ast.Name):
        if node.id in seen:
            return False
        assigned = None
        for stmt in ast.walk(func):
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == node.id:
                        assigned = stmt.value
        return expr_has_write(assigned, func, seen | {node.id})
    if isinstance(node, ast.Attribute) and node.attr in write_flag_names:
        return True
    if isinstance(node, ast.Constant) and node.value in write_modes:
        return True
    for child in ast.iter_child_nodes(node):
        if expr_has_write(child, func, seen):
            return True
    return False

def check(bash_text, py_text):
    bad = []
    try:
        tree = ast.parse(py_text)
    except SyntaxError as exc:
        return ["syntax " + str(exc)]
    aliases = os_aliases_of(tree)
    open_names = {"open"}

    def is_open_expr(node):
        if isinstance(node, ast.Name) and node.id in open_names:
            return True
        if isinstance(node, ast.Attribute) and node.attr == "open":
            return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound == "os" and alias.name != "os":
                    bad.append("os rebound")
        if isinstance(node, ast.ImportFrom) and node.module in ("os", "io", "builtins", "pathlib", "_io"):
            for alias in node.names:
                if alias.name == "open":
                    open_names.add(alias.asname or alias.name)
                    bad.append("import open")
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and is_open_expr(node.value) and target.id not in open_names:
                open_names.add(target.id)
                changed = True
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("os", "subprocess", "shutil"):
            for alias in node.names:
                if node.module != "os" or alias.name in ("environ", "environb", "getenv", "system", "popen"):
                    bad.append("import " + alias.name)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("shutil", "subprocess"):
                    bad.append("import " + alias.name)
        if not is_os_environ(node, aliases):
            continue
        parent = parents.get(node)
        call = parents.get(parent) if isinstance(parent, ast.Attribute) else None
        if isinstance(parent, ast.Attribute) and parent.attr == "get" and isinstance(call, ast.Call) and call.func is parent:
            if not call.args:
                bad.append("environ read without a name")
            else:
                name = literal_name(call.args[0])
                if name is None:
                    bad.append("non-literal environ read")
                elif name not in allowed_env:
                    bad.append("environ " + name)
        else:
            bad.append("environ use is not os.environ.get")
    write_opens = []
    fdopens = []

    class BodyWalk(ast.NodeVisitor):
        def __init__(self):
            self.func = "<module>"
            self.func_node = None

        def visit_FunctionDef(self, node):
            prev, prev_node = self.func, self.func_node
            self.func, self.func_node = node.name, node
            self.generic_visit(node)
            self.func, self.func_node = prev, prev_node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                bad.append("builtin open in " + self.func)
            elif isinstance(func, ast.Name) and func.id in open_names:
                bad.append("open alias in " + self.func)
            if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 1:
                if isinstance(node.args[0], ast.Name) and node.args[0].id in aliases:
                    bad.append("getattr os in " + self.func)
            if isinstance(func, ast.Attribute):
                attr = func.attr
                if attr in path_writes and not (isinstance(func.value, ast.Name) and func.value.id in aliases):
                    bad.append(attr + " in " + self.func)
                if attr == "open" and not (isinstance(func.value, ast.Name) and func.value.id in aliases):
                    bad.append("non-os open in " + self.func)
                if isinstance(func.value, ast.Name) and func.value.id in aliases:
                    if attr == "getenv":
                        if not node.args or literal_name(node.args[0]) not in allowed_env:
                            bad.append("getenv unlisted in " + self.func)
                    elif attr in os_write_names:
                        bad.append("os.%s in %s" % (attr, self.func))
                    elif attr == "mkdir" and self.func != "open_out_dir":
                        bad.append("os.mkdir in " + self.func)
                    elif attr == "rmdir" and self.func != "_release_created":
                        bad.append("os.rmdir in " + self.func)
                    elif attr == "open":
                        flags = node.args[1] if len(node.args) > 1 else None
                        for kw in node.keywords:
                            if kw.arg == "flags":
                                flags = kw.value
                        if flags is not None and expr_has_int(flags):
                            bad.append("numeric open flags in " + self.func)
                        if expr_has_write(flags, self.func_node):
                            write_opens.append(self.func)
                            target = node.args[0] if node.args else None
                            held = False
                            for kw in node.keywords:
                                val = kw.value
                                if kw.arg == "dir_fd" and isinstance(val, ast.Subscript) and isinstance(val.value, ast.Name) and val.value.id == "_HELD_OUT":
                                    held = True
                            if not (isinstance(target, ast.Name) and target.id == "name"):
                                bad.append("write open target in " + self.func)
                            if not held:
                                bad.append("write open dir_fd in " + self.func)
                    elif attr == "fdopen":
                        fdopens.append(self.func)
                        mode = node.args[1] if len(node.args) > 1 else None
                        if not (isinstance(mode, ast.Constant) and mode.value == "w" and self.func == "write_excl"):
                            bad.append("os.fdopen mode in " + self.func)
                    elif attr not in os_call_ok:
                        bad.append("os.%s in %s" % (attr, self.func))
                if isinstance(func.value, ast.Name) and func.value.id == "shutil":
                    bad.append("shutil in " + self.func)
            self.generic_visit(node)

    BodyWalk().visit(tree)
    if write_opens != ["write_excl"]:
        bad.append("write opens %s" % write_opens)
    if fdopens != ["write_excl"]:
        bad.append("fdopen %s" % fdopens)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "write_excl":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr in write_flag_names:
                    bad.append("write flag in " + node.name)
                if isinstance(inner, ast.Constant) and inner.value in write_modes:
                    bad.append("write mode in " + node.name)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr in write_flag_names:
                    bad.append("module write flag")
                if isinstance(inner, ast.Constant) and inner.value in write_modes:
                    bad.append("module write mode")
    assigned = set(re.findall(r"(?m)^[ \t]*([A-Za-z_][A-Za-z0-9_]*)=", bash_text))
    for match in re.finditer(r"(?m)^[ \t]*local[ \t]+([^\n#]+)", bash_text):
        assigned.update(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)=", match.group(1)))
    for match in re.finditer(r"(?:^|[;&|])\s*([A-Za-z_][A-Za-z0-9_]*)=", bash_text, re.M):
        assigned.add(match.group(1))
    for match in re.finditer(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", bash_text):
        assigned.add(match.group(1))
    for match in re.finditer(r"\bread\b[^;\n]*", bash_text):
        for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", match.group(0)):
            if name not in ("read", "r", "IFS", "d"):
                assigned.add(name)
    expanded = set(re.findall(r"\$\{!?([A-Za-z_][A-Za-z0-9_]*)", bash_text))
    expanded.update(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)\b", bash_text))
    external = expanded - assigned
    if not external <= {"TMPDIR", "GBOS_ADB_TIMEOUT"}:
        bad.append("bash env " + ",".join(sorted(external - {"TMPDIR", "GBOS_ADB_TIMEOUT"})))

    def redir_ok(target):
        raw = target.strip().strip("\"'")
        if raw in ("/dev/null", "&1", "&2", "1", "2"):
            return True
        return any(part in target for part in ("$RAW", "${RAW", "$_out", "$_err", "$_rcf"))

    for lineno, line in enumerate(bash_text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if "*'>'*" in line or '*">"*' in line:
            continue
        for match in re.finditer(r"(?:^|[\s;|&])(\d*)(>>|>&|>)(?![>|=])\s*(\"[^\"]*\"|'[^']*'|[^\s;&|)]+)", line):
            if not redir_ok(match.group(3).rstrip(";")):
                bad.append("redirect line %d %s" % (lineno, match.group(3)))
        if re.search(r"(?:^|[\s;|&])\d*<>\s*\S", line) or re.search(r"(?:^|[\s;|&])\d*>\|", line):
            bad.append("redirect line %d" % lineno)
        if re.search(r"(?:^|[;&|(`]|then\s+|else\s+|do\s+|command\s+)(?:tee|cp|mv|ln|dd|mkdir|install|rsync|tar|ditto|touch)\b", line):
            bad.append("shell write line %d" % lineno)
        if re.search(r"\badb\s+pull\b", line) or re.search(r"trap\s+['\"].*\b(?:cp|mv|tee|install|touch)\b", line):
            bad.append("shell write line %d" % lineno)
    return bad

base = check(bash, py)
if base:
    sys.stderr.write("\n".join(base) + "\n")
    raise SystemExit(1)
print("hook allowlist ok")
shapes = {
    "getenv-unlisted": (bash, py + "\nos.getenv('GBOS_UNLISTED')\n"),
    "open-fn": (bash, py + "\ndef hook_shape_open():\n    open('/tmp/hook-shape', 'w')\n"),
    "bash-tee-home": (bash + "\ntee \"$HOME\"\n", py),
    "environ-copy-get": (bash, py + "\nos.environ.copy().get('GBOS_DEV_KERNEL_DIR')\n"),
    "environ-membership": (bash, py + "\n_hook_in = 'GBOS_DEV_KERNEL_DIR' in os.environ\n"),
    "environ-items": (bash, py + "\nlist(os.environ.items())\n"),
    "open-numeric": (bash, py + "\nos.open('/tmp/hook-shape', 0o1101)\n"),
    "os-replace": (bash, py + "\nos.replace('/tmp/a', '/tmp/b')\n"),
    "module-open": (bash, py + "\nopen('/tmp/hook-shape', 'a')\n"),
    "bash-cp-lower": (bash + "\ncp -R \"$RAW\" \"${gbos_dump_dir:-/tmp/hook}\"\n", py),
    "module-open-write": (bash, py + "\nopen('/tmp/hook-shape', 'w')\n"),
    "write-excl-extra": (bash, py.replace(
        "def write_excl(path, text):\n",
        "def write_excl(path, text):\n    open('/tmp/hook-shape', 'w')\n", 1)),
    "environ-alias": (bash, py + "\nenv = os.environ\nenv.get('GBOS_UNLISTED')\n"),
    "environ-get-unlisted": (bash, py + "\nos.environ.get('GBOS_UNLISTED')\n"),
    "os-system": (bash, py + "\nos.system('true')\n"),
    "os-popen": (bash, py + "\nos.popen('true')\n"),
    "import-subprocess": (bash, py + "\nimport subprocess\n"),
    "os-environb": (bash, py + "\nos.environb.get(b'HOME')\n"),
    "getattr-environ": (bash, py + "\ngetattr(os, 'environ')\n"),
    "os-alias-environ": (bash, py + "\nimport os as _o\n_o.environ\n"),
    "path-touch": (bash, py + "\nPath('/tmp/hook-shape').touch()\n"),
    "path-rename": (bash, py + "\nPath('/tmp/hook-shape').rename('/tmp/hook-shape-2')\n"),
    "path-unlink": (bash, py + "\nPath('/tmp/hook-shape').unlink()\n"),
    "path-symlink": (bash, py + "\nPath('/tmp/hook-shape').symlink_to('/tmp/other')\n"),
    "os-makedirs": (bash, py + "\nos.makedirs('/tmp/hook-shape')\n"),
    "os-truncate": (bash, py + "\nos.truncate('/tmp/hook-shape', 0)\n"),
    "os-renames": (bash, py + "\nos.renames('/tmp/a', '/tmp/b')\n"),
    "os-chmod": (bash, py + "\nos.chmod('/tmp/hook-shape', 0o644)\n"),
    "bash-diamond": (bash + "\n1<>/tmp/hook-shape\n", py),
    "bash-clobber": (bash + "\n>| /tmp/hook-shape\n", py),
    "bash-then-cp": (bash + "\nif true; then cp \"$RAW\" \"$HOME/x\"; fi\n", py),
    "bash-trap-cp": (bash + "\ntrap 'cp \"$RAW\" \"$HOME/x\"' EXIT\n", py),
    "bash-command-cp": (bash + "\ncommand cp \"$RAW\" \"$HOME/x\"\n", py),
    "bash-install": (bash + "\ninstall \"$RAW\" \"$HOME/x\"\n", py),
    "bash-rsync": (bash + "\nrsync -a \"$RAW\" \"$HOME/x\"\n", py),
    "bash-tar": (bash + "\ntar -cf \"$HOME/x\" \"$RAW\"\n", py),
    "bash-ditto": (bash + "\nditto \"$RAW\" \"$HOME/x\"\n", py),
    "bash-adb-pull": (bash + "\nadb pull /sdcard/x \"$HOME/x\"\n", py),
    "bash-touch": (bash + "\ntouch \"$HOME/x\"\n", py),
    "path-open-excl": (bash, py.replace(
        "def write_excl(path, text):\n",
        "def write_excl(path, text):\n    Path('/tmp/hook-shape').open('w').write(text)\n", 1)),
    "os-open-kw-flags": (bash, py.replace(
        "def write_excl(path, text):\n",
        "def write_excl(path, text):\n    os.open('/tmp/hook-shape', flags=os.O_WRONLY | os.O_CREAT)\n", 1)),
    "os-open-no-dirfd": (bash, py.replace(
        "fd = os.open(name, flags, 0o644, dir_fd=_HELD_OUT[1])\n",
        "fd = os.open('/tmp/hook-shape', flags, 0o644)\n", 1)),
    "io-open": (bash, py + "\nimport io\nio.open('/tmp/hook-shape')\n"),
    "os-open-alias": (bash, py + "\noo = os.open\noo('/tmp/hook-shape', 0)\n"),
    "open-assign": (bash, py + "\nf = open\nf('/tmp/hook-shape')\n"),
    "from-os-open": (bash, py + "\nfrom os import open as oo\noo('/tmp/hook-shape', 0)\n"),
    "io-as-os": (bash, py + "\nimport io as os\nos.open('/tmp/hook-shape')\n"),
}
for shape, (shape_bash, shape_py) in shapes.items():
    found = check(shape_bash, shape_py)
    if not found:
        sys.stderr.write("hook shape %s was accepted\n" % shape)
        raise SystemExit(1)
    print("hook shape %s rejected" % shape)
if check(bash, py + "\nos.environ.get('ANDROID_SERIAL')\n"):
    sys.stderr.write("listed environ get was rejected\n")
    raise SystemExit(1)
print("hook listed get still allowed")
PY
then
    ok "env reads and file writes stay on the allowlist"
else
    bad "env reads and file writes stay on the allowlist"
    cat "$T/hook-check.txt" >&2
fi
for shape in getenv-unlisted open-fn bash-tee-home environ-copy-get environ-membership environ-items \
    open-numeric os-replace module-open bash-cp-lower module-open-write write-excl-extra \
    environ-alias environ-get-unlisted \
    os-system os-popen import-subprocess os-environb getattr-environ os-alias-environ \
    path-touch path-rename path-unlink path-symlink os-makedirs os-truncate os-renames os-chmod \
    bash-diamond bash-clobber bash-then-cp bash-trap-cp bash-command-cp bash-install bash-rsync \
    bash-tar bash-ditto bash-adb-pull bash-touch path-open-excl os-open-kw-flags os-open-no-dirfd \
    io-open os-open-alias open-assign from-os-open io-as-os; do
    if [ -f "$T/hook-check.txt" ] && grep -F -q "hook shape $shape rejected" "$T/hook-check.txt"; then
        ok "hook shape $shape is rejected"
    else
        bad "hook shape $shape is rejected"
    fi
done
if agree_files "$T/case-bt-adapter-off"; then
    ok "bluetooth off agrees with the Chinese row"
else
    bad "bluetooth off agrees with the Chinese row"
fi
if python3 - "$R" "$FIX/kano" "$FIX/kano-probe" "$T" << 'PY'
import json, os, pathlib, shutil, subprocess, sys
script, fix_s, probe_s, root_s = sys.argv[1:]
root = pathlib.Path(root_s)
fix = pathlib.Path(fix_s)
probe_src = pathlib.Path(probe_s)
fails = []

def copy_fix(name):
    dest = root / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(fix, dest)
    return dest

def copy_probe(name):
    dest = root / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(probe_src, dest)
    return dest

def set_prop(path, key, value):
    text = path.read_text(encoding="utf-8")
    line = "[%s]: [%s]" % (key, value)
    prefix = "[" + key + "]:"
    lines, found = [], False
    for old in text.splitlines():
        if old.startswith(prefix):
            lines.append(line)
            found = True
        else:
            lines.append(old)
    if not found:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

def set_release(dest, release, host=None):
    uname = (dest / "uname-a.txt").read_text(encoding="utf-8")
    if host is not None:
        uname = uname.replace("synth-laptop", host, 1)
    uname = uname.replace("6.1.75-android14", release, 1)
    (dest / "uname-a.txt").write_text(uname, encoding="utf-8", newline="\n")

def krel(name, text, config=None, link_config=False):
    dest = root / name
    cfg = dest / "include" / "config"
    cfg.mkdir(parents=True)
    (cfg / "kernel.release").write_text(text + "\n", encoding="utf-8", newline="\n")
    if link_config:
        target = dest / "config-target"
        target.write_text("not-the-config\n", encoding="utf-8", newline="\n")
        (dest / ".config").symlink_to(target)
    elif config is not None:
        (dest / ".config").write_bytes(config)

def run(name, fixture, kdir=None, probe=None):
    out = root / (name + "-out")
    if out.exists():
        shutil.rmtree(out)
    env = os.environ.copy()
    if kdir:
        env["GBOS_DEV_KERNEL_DIR"] = str(root / kdir)
    else:
        env.pop("GBOS_DEV_KERNEL_DIR", None)
    cmd = ["bash", script, "--fixture", str(fixture), "--out", str(out), "--date", "20261014"]
    if probe:
        cmd[4:4] = ["--probe", str(probe)]
    proc = subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        fails.append("%s rc %s %s" % (name, proc.returncode, proc.stderr[-200:]))
        return None
    txt = (out / "gbos-report-20261014.txt").read_text(encoding="utf-8")
    summary = (out / "summary.md").read_text(encoding="utf-8")
    return txt, summary

def kernel_line(txt):
    for line in txt.splitlines():
        if line.startswith("kernel:"):
            return line
    return ""

def expect_dev(name, fixture, kdir, release, line, banned):
    got = run(name, fixture, kdir)
    if not got:
        return
    txt, summary = got
    blob = txt + "\n" + summary
    if kernel_line(txt) != line:
        fails.append("%s kernel %s" % (name, kernel_line(txt)))
    if "redactions: 0" not in txt:
        fails.append("%s redactions" % name)
    for word in banned:
        if word and word in blob:
            fails.append("%s printed %s" % (name, word))

cases = [
    ("r9-shift", "02aabbccddee", None, "6.1.75-ga02aabbccddee", "kernel: dev-kernel [fixture]"),
    ("r9-concat-host", "SYNTHSERIAL001", "alicehost", "6.1.75-galicehost", "kernel: dev-kernel [fixture]"),
    ("r9-concat-serial", "SYNTHSERIAL001", None, "6.1.75-buildSYNTHSERIAL001", "kernel: dev-kernel [fixture]"),
    ("r9-nvidia", "SYNTHSERIAL001", None, "6.1.75-nvidia-drm", "kernel: dev-kernel [fixture]"),
    ("r9-nvidia-extra", "SYNTHSERIAL001", None, "6.1.75-nvidia-drm-extra", "kernel: dev-kernel [fixture]"),
    ("r9-hid", "SYNTHSERIAL001", None, "6.1.75-hid-multitouch", "kernel: dev-kernel [fixture]"),
    ("r9-i2c", "SYNTHSERIAL001", None, "6.1.75-i2c_hid", "kernel: dev-kernel [fixture]"),
    ("r9-plus", "SYNTHSERIAL001", None, "6.1.75-fixture+probe", "kernel: dev-kernel [fixture]"),
    ("r9-dotted", "SYNTHSERIAL001", None, "6.1.75-CL3B.260622.271.R1", "kernel: dev-kernel [fixture]"),
    ("r9-realgpu", "SYNTHSERIAL001", None, "6.1.75-real-gpu", "kernel: dev-kernel [fixture]"),
    ("r9-exact", "SYNTHSERIAL001", None, "nvidia-drm", "kernel: dev-kernel [fixture]"),
    ("r9-synth", "SYNTHSERIAL001", "synth-laptop", "6.1.75-synth-laptop", "kernel: dev-kernel [fixture]"),
    ("r9-sep-host", "SYNTHSERIAL001", "alice-laptop", "6.1.75-alice.laptop", "kernel: dev-kernel [fixture]"),
    ("r9-sep-fqdn", "SYNTHSERIAL001", "alicehost.lan", "6.1.75-alicehost", "kernel: dev-kernel [fixture]"),
    ("r9-sep-serial", "AB12-CD34EF56", None, "6.1.75-ab12cd34ef56", "kernel: dev-kernel [fixture]"),
]
for name, serial, host, release, line in cases:
    dest = copy_fix(name)
    set_prop(dest / "getprop.txt", "ro.serialno", serial)
    if host:
        set_prop(dest / "getprop.txt", "net.hostname", host)
    set_release(dest, release, host)
    krel(name + "-krel", release)
    banned = [release]
    if serial != "SYNTHSERIAL001":
        banned.append(serial)
    if host:
        banned.append(host)
    expect_dev(name, dest, name + "-krel", release, line, banned)

bare = copy_fix("r9-noconfig")
set_release(bare, "6.1.75-r9noconfig")
krel("r9-noconfig-krel", "6.1.75-r9noconfig")
expect_dev("r9-noconfig", bare, "r9-noconfig-krel", "6.1.75-r9noconfig", "kernel: dev-kernel [fixture]", ["6.1.75-r9noconfig"])

badcfg = copy_fix("r9-badcfg")
set_release(badcfg, "6.1.75-r9badcfg")
krel("r9-badcfg-krel", "6.1.75-r9badcfg", config=b"not the gki config\n")
expect_dev("r9-badcfg", badcfg, "r9-badcfg-krel", "6.1.75-r9badcfg", "kernel: dev-kernel, config fail [fixture]", ["6.1.75-r9badcfg"])

linkcfg = copy_fix("r9-linkcfg")
set_release(linkcfg, "6.1.75-r9linkcfg")
krel("r9-linkcfg-krel", "6.1.75-r9linkcfg", link_config=True)
expect_dev("r9-linkcfg", linkcfg, "r9-linkcfg-krel", "6.1.75-r9linkcfg", "kernel: dev-kernel [fixture]", ["6.1.75-r9linkcfg"])

def display_line(txt):
    for line in txt.splitlines():
        if line.startswith("display/gpu:"):
            return line
    return ""

def gpu_cell(summary):
    for line in summary.splitlines():
        if "顯示／GPU" in line:
            return line
    return ""

def assert_gpu_unknown(name, txt, summary, xe_row=False):
    disp = display_line(txt)
    cell = gpu_cell(summary)
    if disp != "display/gpu: unknown [fixture]":
        fails.append("%s display %s" % (name, disp))
    if "| 顯示／GPU 驅動 | 未知 |" not in cell:
        fails.append("%s gpu cell %s" % (name, cell))
    if "已綁定" in summary or "沒有原生 GPU 驅動" in summary or "bound (" in txt or "no native GPU driver" in txt:
        fails.append("%s claimed a gpu" % name)
    if "real-gpu" in txt:
        fails.append("%s real-gpu" % name)
    if xe_row and "driver xe" not in txt:
        fails.append("%s xe row missing" % name)

def expect_omit(name, drop, xe_row=False):
    dest = copy_fix(name)
    probe = copy_probe(name + "-probe")
    for rel in drop:
        path = dest / rel
        if path.exists():
            path.unlink()
    got = run(name, dest, probe=probe)
    if not got:
        return
    txt, summary = got
    if "omitted (probe is not the same machine)" not in txt or "agree: DMI" in txt or "UEFI yes" in txt:
        fails.append("%s merged %s" % (name, kernel_line(txt)))
    if "已省略" not in summary:
        fails.append("%s summary" % name)
    assert_gpu_unknown(name, txt, summary, xe_row)

expect_omit("r9-dri-missing", ["ls-dev-dri.txt"])
expect_omit("r9-dri-all", [
    "ls-dev-dri.txt", "cat-drm-vendor.txt", "cat-drm-device.txt", "cat-drm0-uevent.txt",
])
enoent = copy_fix("r10-dri-enoent")
enoent_probe = copy_probe("r10-dri-enoent-probe")
(enoent / "ls-dev-dri.txt").write_text("card0\nrenderD128\n", encoding="utf-8", newline="\n")
(enoent / "ls-dev-dri.rc").write_text("1\n", encoding="utf-8", newline="\n")
(enoent / "ls-dev-dri.err").write_text("No such file or directory\n", encoding="utf-8", newline="\n")
got = run("r10-dri-enoent", enoent, probe=enoent_probe)
if got:
    if "omitted (probe is not the same machine)" not in got[0] or "agree: DMI" in got[0] or "UEFI yes" in got[0]:
        fails.append("r10-dri-enoent merged")
    assert_gpu_unknown("r10-dri-enoent", got[0], got[1])
    if "driver xe" in got[0] or "8086:46a6" in got[0] or "8086:46a6" in got[1]:
        fails.append("r10-dri-enoent kept a card row")
denied = copy_fix("r9-dri-denied")
probe = copy_probe("r9-dri-denied-probe")
(denied / "ls-dev-dri.txt").write_text("card0\n", encoding="utf-8", newline="\n")
(denied / "ls-dev-dri.rc").write_text("1\n", encoding="utf-8", newline="\n")
(denied / "ls-dev-dri.err").write_text("permission denied\n", encoding="utf-8", newline="\n")
got = run("r9-dri-denied", denied, probe=probe)
if got:
    if "agree: DMI" in got[0] or "UEFI yes" in got[0] or "omitted (probe is not the same machine)" not in got[0]:
        fails.append("r9-dri-denied merged")
    assert_gpu_unknown("r9-dri-denied", got[0], got[1])
    if "driver xe" in got[0] or "8086:46a6" in got[0] or "8086:46a6" in got[1]:
        fails.append("r9-dri-denied kept a card row")
listed = copy_fix("r10-dri-xe")
listed_probe = copy_probe("r10-dri-xe-probe")
got = run("r10-dri-xe", listed, probe=listed_probe)
if got:
    if display_line(got[0]) != "display/gpu: bound (xe) [fixture]":
        fails.append("r10-dri-xe display %s" % display_line(got[0]))
    if "已綁定（xe）" not in gpu_cell(got[1]):
        fails.append("r10-dri-xe cell %s" % gpu_cell(got[1]))
    if "agree: DMI" not in got[0] or "UEFI yes" not in got[0]:
        fails.append("r10-dri-xe did not merge")
shim = copy_fix("r10-dri-shim")
shim_probe = copy_probe("r10-dri-shim-probe")
(shim / "cat-drm0-uevent.txt").write_text("DRIVER=gbos_efifb\n", encoding="utf-8", newline="\n")
(shim / "cat-drm-vendor.txt").unlink()
(shim / "cat-drm-device.txt").unlink()
(shim / "ls-dev-dri.txt").write_text("card0\n", encoding="utf-8", newline="\n")
got = run("r10-dri-shim", shim, probe=shim_probe)
if got:
    if "no native GPU driver" not in display_line(got[0]):
        fails.append("r10-dri-shim display %s" % display_line(got[0]))
    if "沒有原生 GPU 驅動" not in gpu_cell(got[1]):
        fails.append("r10-dri-shim cell %s" % gpu_cell(got[1]))
    if "agree: DMI" not in got[0] or "UEFI yes" not in got[0]:
        fails.append("r10-dri-shim did not merge")
    if "bound (" in got[0]:
        fails.append("r10-dri-shim bound")
empty = copy_fix("r10-dri-empty")
empty_probe = copy_probe("r10-dri-empty-probe")
(empty / "ls-dev-dri.txt").write_text("", encoding="utf-8", newline="\n")
for name in ("cat-drm0-uevent.txt", "cat-drm-vendor.txt", "cat-drm-device.txt"):
    (empty / name).write_text("", encoding="utf-8", newline="\n")
got = run("r10-dri-empty", empty, probe=empty_probe)
if got:
    assert_gpu_unknown("r10-dri-empty", got[0], got[1])
    if "agree: DMI" not in got[0] or "UEFI yes" not in got[0]:
        fails.append("r10-dri-empty did not merge")

unread = copy_fix("r9-unread")
(unread / "cat-drm0-uevent.txt").write_text("PCI_ID=8086:46A6\n", encoding="utf-8", newline="\n")
base = run("r9-unread", unread)
unread_id = copy_fix("r9-unread-id")
(unread_id / "cat-drm0-uevent.txt").write_text("PCI_ID=8086:46A6\n", encoding="utf-8", newline="\n")
set_prop(unread_id / "getprop.txt", "ro.serialno", "unread")
set_prop(unread_id / "getprop.txt", "net.hostname", "unread")
same = run("r9-unread-id", unread_id)
if base and same:
    if "card0 pci 8086:46a6 driver unread" not in base[0] or "redactions: 0" not in base[0]:
        fails.append("unread line missing")
    if base[0] != same[0] or base[1] != same[1]:
        fails.append("serial unread redacted the driver line")
sysfs = copy_fix("r9-unread-sysfs")
(sysfs / "cat-drm0-uevent.txt").write_text("", encoding="utf-8", newline="\n")
got = run("r9-unread-sysfs", sysfs)
if got and "card0 pci 8086:46a6 driver unread" not in got[0]:
    fails.append("sysfs unread line missing")
sysfs_probe = copy_fix("r10-uevent-empty")
sysfs_probe_dir = copy_probe("r10-uevent-empty-probe")
(sysfs_probe / "cat-drm0-uevent.txt").write_text("", encoding="utf-8", newline="\n")
got = run("r10-uevent-empty", sysfs_probe, probe=sysfs_probe_dir)
if got:
    if "card0 pci 8086:46a6 driver unread" not in got[0]:
        fails.append("empty uevent line missing")
    if "agree: DMI" not in got[0] or "UEFI yes" not in got[0]:
        fails.append("empty uevent did not merge")
miss_ue = copy_fix("r10-uevent-missing")
miss_probe = copy_probe("r10-uevent-missing-probe")
(miss_ue / "cat-drm0-uevent.txt").unlink()
got = run("r10-uevent-missing", miss_ue, probe=miss_probe)
if got:
    if "omitted (probe is not the same machine)" not in got[0] or "agree: DMI" in got[0] or "UEFI yes" in got[0]:
        fails.append("missing uevent merged")
    if "card0 pci 8086:46a6 driver unread" not in got[0]:
        fails.append("missing uevent row")
    if "driver xe" in got[0]:
        fails.append("missing uevent kept xe")
deny_ue = copy_fix("r10-uevent-denied")
deny_probe = copy_probe("r10-uevent-denied-probe")
(deny_ue / "cat-drm0-uevent.rc").write_text("1\n", encoding="utf-8", newline="\n")
(deny_ue / "cat-drm0-uevent.err").write_text("permission denied\n", encoding="utf-8", newline="\n")
got = run("r10-uevent-denied", deny_ue, probe=deny_probe)
if got:
    if "omitted (probe is not the same machine)" not in got[0] or "agree: DMI" in got[0] or "UEFI yes" in got[0]:
        fails.append("denied uevent merged")
    if "card0 pci 8086:46a6 driver unread" not in got[0]:
        fails.append("denied uevent row")
shim_only = copy_fix("r10-uevent-shim")
shim_only_probe = copy_probe("r10-uevent-shim-probe")
(shim_only / "cat-drm0-uevent.txt").write_text("DRIVER=gbos_efifb\n", encoding="utf-8", newline="\n")
(shim_only / "cat-drm-vendor.txt").unlink()
(shim_only / "cat-drm-device.txt").unlink()
got = run("r10-uevent-shim", shim_only, probe=shim_only_probe)
if got:
    if "agree: DMI" not in got[0] or "UEFI yes" not in got[0]:
        fails.append("uevent-only shim did not merge")
    if "gbos_efifb" not in got[0]:
        fails.append("uevent-only shim driver")

def reject_node(name, token, dri=None, serial=None, bt_name=None, dri_unknown=False):
    dest = copy_fix(name)
    probe = None
    if dri is not None:
        (dest / "ls-dev-dri.txt").write_text(dri, encoding="utf-8", newline="\n")
    if bt_name is not None:
        probe = copy_probe(name + "-probe")
        data = json.loads((probe / "probe.json").read_text(encoding="utf-8"))
        data["bluetooth"] = [{"name": bt_name}]
        (probe / "probe.json").write_text(json.dumps(data) + "\n", encoding="utf-8", newline="\n")
    if serial:
        set_prop(dest / "getprop.txt", "ro.serialno", serial)
        set_prop(dest / "getprop.txt", "net.hostname", serial)
    got = run(name, dest, probe=probe)
    if not got:
        return
    txt, summary = got
    if token in txt or token in summary:
        fails.append("%s printed %s" % (name, token))
    if "redactions: 0" not in txt:
        fails.append("%s redactions %s" % (name, txt.split("redactions:", 1)[-1][:20]))
    if dri_unknown:
        nodes = ""
        for line in txt.splitlines():
            if line.startswith("dri nodes:"):
                nodes = line
        if nodes != "dri nodes: unknown [fixture]":
            fails.append("%s nodes %s" % (name, nodes))

reject_node("r10-card00", "card00", dri="card00\n", dri_unknown=True)
reject_node("r10-card00-id", "card00", dri="card00\n", serial="card00", dri_unknown=True)
reject_node("r10-renderD001", "renderD001", dri="card0\nrenderD001\n")
reject_node("r10-renderD001-id", "renderD001", dri="card0\nrenderD001\n", serial="renderD001")
reject_node("r10-hci00", "hci00", bt_name="hci00")
reject_node("r10-hci00-id", "hci00", bt_name="hci00", serial="hci00")

py_body = pathlib.Path(script).read_text(encoding="utf-8").split("<< 'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
py_body = py_body[:py_body.rfind("\nmain()")] + "\n"
word_ns = {}
exec(compile(py_body, script, "exec"), word_ns)
for word, want in (("renderD128", True), ("card0", True), ("renderD001", False), ("card00", False)):
    if word_ns["is_report_word"](word) != want:
        fails.append("is_report_word %s" % word)

import time
def fifo_case(name, which):
    dest = copy_fix(name)
    release = "6.1.75-" + name
    set_release(dest, release)
    kdir = root / (name + "-k")
    cfg = kdir / "include" / "config"
    cfg.mkdir(parents=True)
    if which == "config":
        (cfg / "kernel.release").write_text(release + "\n", encoding="utf-8", newline="\n")
        os.mkfifo(str(kdir / ".config"))
    else:
        os.mkfifo(str(cfg / "kernel.release"))
    out = root / (name + "-out")
    env = os.environ.copy()
    env["GBOS_DEV_KERNEL_DIR"] = str(kdir)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["bash", script, "--fixture", str(dest), "--out", str(out), "--date", "20261014"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        fails.append("%s hung" % name)
        return
    if time.monotonic() - started >= 5:
        fails.append("%s slow" % name)
    if proc.returncode != 0:
        fails.append("%s rc %s %s" % (name, proc.returncode, proc.stderr[-200:]))
        return
    txt = (out / "gbos-report-20261014.txt").read_text(encoding="utf-8")
    if release in txt:
        fails.append("%s printed release" % name)
    if which == "config":
        if kernel_line(txt) != "kernel: dev-kernel [fixture]":
            fails.append("%s kernel %s" % (name, kernel_line(txt)))
        if "config pass" in kernel_line(txt) or "config fail" in kernel_line(txt):
            fails.append("%s config word" % name)
    elif kernel_line(txt) != "kernel: other [fixture]":
        fails.append("%s kernel %s" % (name, kernel_line(txt)))

fifo_case("r10-fifo-cfg", "config")
fifo_case("r10-fifo-rel", "release")
link_rel = copy_fix("r10-link-rel")
link_release = "6.1.75-r10linkrel"
set_release(link_rel, link_release)
kdir = root / "r10-link-rel-k"
cfg = kdir / "include" / "config"
cfg.mkdir(parents=True)
target = kdir / "release-target"
target.write_text(link_release + "\n", encoding="utf-8", newline="\n")
(cfg / "kernel.release").symlink_to(target)
got = run("r10-link-rel", link_rel, kdir="r10-link-rel-k")
if got:
    if kernel_line(got[0]) != "kernel: other [fixture]":
        fails.append("symlink kernel.release %s" % kernel_line(got[0]))
    if link_release in got[0] or link_release in got[1]:
        fails.append("symlink kernel.release printed")

def input_case(name, dumpsys):
    dest = copy_fix(name)
    (dest / "dumpsys-input.txt").write_text(dumpsys, encoding="utf-8", newline="\n")
    (dest / "cat-proc-bus-input-devices.txt").write_text("", encoding="utf-8", newline="\n")
    return dest

bus_i2c = input_case("r9-bus-i2c", "1: Touchpad\n  Classes: TOUCHPAD\n  Identifier: bus=0x0018 vendor=0x04f3 product=0x323c\n")
bus_usb = input_case("r9-bus-usb", "1: Touchpad\n  Classes: TOUCHPAD\n  Identifier: bus=0x0003 vendor=0x04f3 product=0x323c\n")
a = run("r9-bus-i2c", bus_i2c)
b = run("r9-bus-usb", bus_usb)
if a and b:
    if "touchpad: 04f3:323c (i2c)" not in a[0] or "(usb)" in a[0]:
        fails.append("bus 0018")
    if "touchpad: 04f3:323c (usb)" not in b[0] or "(i2c)" in b[0]:
        fails.append("bus 0003")
prod_a = input_case("r9-prod-a", "1: Touchpad\n  Classes: TOUCHPAD\n  Identifier: bus=0x0018 vendor=0x04f3 product=0x323c\n")
prod_b = input_case("r9-prod-b", "1: Touchpad\n  Classes: TOUCHPAD\n  Identifier: bus=0x0018 vendor=0x04f3 product=0xabcd\n")
a = run("r9-prod-a", prod_a)
b = run("r9-prod-b", prod_b)
if a and b:
    if "touchpad: 04f3:323c (i2c)" not in a[0]:
        fails.append("product 323c")
    if "touchpad: 04f3:abcd (i2c)" not in b[0] or "323c" in b[0]:
        fails.append("product abcd")

def merged(name, txt):
    if "agree: DMI" not in txt or "UEFI yes" not in txt:
        fails.append("%s did not merge" % name)

def omitted(name, txt, summary):
    if "omitted (probe is not the same machine)" not in txt or "agree: DMI" in txt or "UEFI yes" in txt:
        fails.append("%s merged" % name)
    if "已省略" not in summary:
        fails.append("%s summary" % name)

empty_xe = copy_fix("r11-dri-empty-xe")
empty_xe_probe = copy_probe("r11-dri-empty-xe-probe")
(empty_xe / "ls-dev-dri.txt").write_text("", encoding="utf-8", newline="\n")
got = run("r11-dri-empty-xe", empty_xe, probe=empty_xe_probe)
if got:
    assert_gpu_unknown("r11-dri-empty-xe", got[0], got[1])
    if "driver xe" in got[0] or "已綁定" in got[1]:
        fails.append("r11-dri-empty-xe kept a driver")
    merged("r11-dri-empty-xe", got[0])

empty_shim = copy_fix("r11-dri-empty-shim")
empty_shim_probe = copy_probe("r11-dri-empty-shim-probe")
(empty_shim / "ls-dev-dri.txt").write_text("", encoding="utf-8", newline="\n")
(empty_shim / "cat-drm0-uevent.txt").write_text("DRIVER=gbos_efifb\n", encoding="utf-8", newline="\n")
(empty_shim / "cat-drm-vendor.txt").unlink()
(empty_shim / "cat-drm-device.txt").unlink()
got = run("r11-dri-empty-shim", empty_shim, probe=empty_shim_probe)
if got:
    assert_gpu_unknown("r11-dri-empty-shim", got[0], got[1])
    if "gbos_efifb" in got[0] or "gbos_efifb" in got[1] or "no native GPU driver" in got[0]:
        fails.append("r11-dri-empty-shim claimed the unlisted shim")
    merged("r11-dri-empty-shim", got[0])

card1 = copy_fix("r11-dri-card1")
card1_probe = copy_probe("r11-dri-card1-probe")
(card1 / "ls-dev-dri.txt").write_text("card1\n", encoding="utf-8", newline="\n")
got = run("r11-dri-card1", card1, probe=card1_probe)
if got:
    assert_gpu_unknown("r11-dri-card1", got[0], got[1])
    omitted("r11-dri-card1", got[0], got[1])
    if "driver xe" in got[0]:
        fails.append("r11-dri-card1 used card0")
    nodes = ""
    for line in got[0].splitlines():
        if line.startswith("dri nodes:"):
            nodes = line
        if line.startswith("drm cards:") and "card0" in line:
            fails.append("r11-dri-card1 drm row %s" % line)
    if nodes != "dri nodes: card1 [fixture]":
        fails.append("r11-dri-card1 nodes %s" % nodes)

render_only = copy_fix("r11-dri-render-only")
render_probe = copy_probe("r11-dri-render-only-probe")
(render_only / "ls-dev-dri.txt").write_text("renderD128\n", encoding="utf-8", newline="\n")
got = run("r11-dri-render-only", render_only, probe=render_probe)
if got:
    assert_gpu_unknown("r11-dri-render-only", got[0], got[1])
    if "driver xe" in got[0]:
        fails.append("r11-dri-render-only kept xe")
    if "dri nodes: renderD128 [" not in got[0]:
        fails.append("r11-dri-render-only nodes")
    merged("r11-dri-render-only", got[0])

shim_miss = copy_fix("r11-dri-shim-missing")
shim_miss_probe = copy_probe("r11-dri-shim-missing-probe")
(shim_miss / "ls-dev-dri.txt").unlink()
(shim_miss / "cat-drm0-uevent.txt").write_text("DRIVER=gbos_efifb\n", encoding="utf-8", newline="\n")
(shim_miss / "cat-drm-vendor.txt").unlink()
(shim_miss / "cat-drm-device.txt").unlink()
got = run("r11-dri-shim-missing", shim_miss, probe=shim_miss_probe)
if got:
    assert_gpu_unknown("r11-dri-shim-missing", got[0], got[1])
    omitted("r11-dri-shim-missing", got[0], got[1])
    if "no native GPU driver" in got[0] or "沒有原生 GPU 驅動" in got[1]:
        fails.append("r11-dri-shim-missing said no native")
    if "gbos_efifb" in got[0] or "gbos_efifb" in got[1]:
        fails.append("r11-dri-shim-missing built a card row without a list")

hide = copy_fix("r11-dri-shim-hides-xe")
hide_probe = copy_probe("r11-dri-shim-hides-xe-probe")
(hide / "ls-dev-dri.txt").write_text("card0\n", encoding="utf-8", newline="\n")
(hide / "cat-drm0-uevent.txt").write_text("DRIVER=gbos_efifb\n", encoding="utf-8", newline="\n")
(hide / "cat-drm-vendor.txt").unlink()
(hide / "cat-drm-device.txt").unlink()
(hide / "cat-drm1-uevent.txt").write_text("DRIVER=xe\nPCI_ID=8086:46A6\n", encoding="utf-8", newline="\n")
(hide / "cat-drm1-vendor.txt").write_text("0x8086\n", encoding="utf-8", newline="\n")
(hide / "cat-drm1-device.txt").write_text("0x46a6\n", encoding="utf-8", newline="\n")
got = run("r11-dri-shim-hides-xe", hide, probe=hide_probe)
if got:
    if "no native GPU driver" not in display_line(got[0]) or "bound (" in got[0]:
        fails.append("r11-dri-shim-hides-xe display %s" % display_line(got[0]))
    if "driver xe" in got[0]:
        fails.append("r11-dri-shim-hides-xe counted unlisted xe")
    if "gbos_efifb" not in got[0]:
        fails.append("r11-dri-shim-hides-xe missed the listed shim")
    merged("r11-dri-shim-hides-xe", got[0])

def list_fail(name, stdout, rc, err, shim=False):
    dest = copy_fix(name)
    probe = copy_probe(name + "-probe")
    dri = dest / "ls-dev-dri.txt"
    if stdout is None:
        if dri.exists():
            dri.unlink()
    else:
        dri.write_text(stdout, encoding="utf-8", newline="\n")
    if rc is not None:
        (dest / "ls-dev-dri.rc").write_text("%s\n" % rc, encoding="utf-8", newline="\n")
    if err is not None:
        (dest / "ls-dev-dri.err").write_text(err, encoding="utf-8", newline="\n")
    if shim:
        (dest / "cat-drm0-uevent.txt").write_text("DRIVER=gbos_efifb\n", encoding="utf-8", newline="\n")
        for rel in ("cat-drm-vendor.txt", "cat-drm-device.txt"):
            path = dest / rel
            if path.exists():
                path.unlink()
    got = run(name, dest, probe=probe)
    if not got:
        return
    txt, summary = got
    assert_gpu_unknown(name, txt, summary)
    omitted(name, txt, summary)
    blob = txt + "\n" + summary
    banned = ("gbos_efifb",) if shim else ("driver xe", "8086:46a6")
    for word in banned:
        if word in blob:
            fails.append("%s printed %s" % (name, word))
    cards = ""
    for line in txt.splitlines():
        if line.startswith("drm cards:"):
            cards = line
    if cards != "drm cards: unknown [fixture]":
        fails.append("%s cards %s" % (name, cards))

# A failed DRM list names no card. Card files are present and must not become a row.
shapes = (
    ("missing", None, None, None),
    ("leftover-rc", None, 0, ""),
    ("enoent", "card0\n", 1, "No such file or directory\n"),
    ("denied", "card0\n", 1, "permission denied\n"),
    ("not-captured", "card0\n", 1, "not captured\n"),
    ("timeout", "card0\n", 1, "timed out\n"),
    ("offline", "card0\n", 1, "device offline\n"),
    ("rc", "card0\n", 9, "capture failed\n"),
)
for shape, stdout, rc, err in shapes:
    list_fail("r12-list-" + shape, stdout, rc, err, shim=False)
    list_fail("r12-list-" + shape + "-shim", stdout, rc, err, shim=True)

bcd = copy_fix("r12-bcd-empty")
(bcd / "cat-wlan-uevent.txt").write_text("DRIVER=notadriver\nPRODUCT=1234/5678/\n", encoding="utf-8", newline="\n")
(bcd / "cat-bt-uevent.txt").write_text("DRIVER=notabtdrv\nPRODUCT=1234/5678/ab cd\n", encoding="utf-8", newline="\n")
got = run("r12-bcd-empty", bcd)
if got:
    blob = got[0] + "\n" + got[1]
    if blob.count("usb 1234:5678") < 2 or "ab cd" in blob:
        fails.append("r12-bcd-empty %s" % blob[blob.find("wifi:"):blob.find("wifi:") + 180])
two = copy_fix("r12-bcd-two")
(two / "cat-wlan-uevent.txt").write_text("DRIVER=notadriver\nPRODUCT=1234/5678\n", encoding="utf-8", newline="\n")
(two / "cat-bt-uevent.txt").write_text("DRIVER=notabtdrv\nPRODUCT=1234/5678\n", encoding="utf-8", newline="\n")
got = run("r12-bcd-two", two)
if got:
    blob = got[0] + "\n" + got[1]
    if blob.count("usb 1234:5678") < 2 or "notadriver" in blob or "notabtdrv" in blob or "1234/5678" in blob:
        fails.append("r12-bcd-two %s" % blob[blob.find("wifi:"):blob.find("wifi:") + 180])
if fails:
    sys.stderr.write("\n".join(fails) + "\n")
    raise SystemExit(1)
print("round 9 fixtures ok")
PY
then
    ok "dev-kernel, missing DRM, unread, and input ids"
else
    bad "dev-kernel, missing DRM, unread, and input ids"
fi

mkdir -p "$T/r10-grand/victim/child"
ln -s "$T/r10-grand/victim" "$T/r10-grand/alias"
out=$(bash "$R" --fixture "$FIX/kano" --out "$T/r10-grand/alias/child/new" --date 20261014 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -e "$T/r10-grand/victim/child/new" ] && printf '%s\n' "$out" | grep -q 'symlink' \
    && ok "--out grandparent symlink is refused" || bad "--out grandparent symlink is refused (rc=$rc)"
mkdir -p "$T/r10-swap-held" "$T/r10-swap-victim"
cat > "$T/r10-swap.env" << 'EOF'
gbos_swap_debug() {
    case "$BASH_COMMAND" in
        python3*) ;;
        *) return 0 ;;
    esac
    [ "${GBOS_SWAP_OUT:-1}" = 1 ] || return 0
    GBOS_SWAP_OUT=0
    mv "$GBOS_SWAP_HELD" "$GBOS_SWAP_ASIDE" || return 0
    ln -s "$GBOS_SWAP_VICTIM" "$GBOS_SWAP_HELD" || return 0
    return 0
}
trap gbos_swap_debug DEBUG
EOF
out=$(BASH_ENV="$T/r10-swap.env" \
    GBOS_SWAP_HELD="$T/r10-swap-held" \
    GBOS_SWAP_ASIDE="$T/r10-swap-aside" \
    GBOS_SWAP_VICTIM="$T/r10-swap-victim" \
    bash "$R" --fixture "$FIX/kano" --out "$T/r10-swap-held" --date 20261014 2>&1); rc=$?
if [ "$rc" = 2 ] \
    && [ ! -e "$T/r10-swap-victim/summary.md" ] \
    && [ ! -e "$T/r10-swap-victim/gbos-report-20261014.txt" ] \
    && [ ! -e "$T/r10-swap-aside/summary.md" ] \
    && [ ! -e "$T/r10-swap-aside/gbos-report-20261014.txt" ] \
    && printf '%s\n' "$out" | grep -q 'symlink'; then
    ok "a directory swapped for a symlink after the check is refused"
else
    bad "a directory swapped for a symlink after the check is refused (rc=$rc)"
    printf '%s\n' "$out" >&2
fi

rm -rf "$T/r11-mk-held" "$T/r11-mk-aside" "$T/r11-mk-victim"
mkdir -p "$T/r11-mk-held" "$T/r11-mk-victim"
printf 'sentinel\n' > "$T/r11-mk-victim/sentinel"
cat > "$T/r11-mk.env" << 'EOF'
gbos_mkdir_debug() {
    case "$BASH_COMMAND" in
        python3*|mkdir*) ;;
        *) return 0 ;;
    esac
    [ "${GBOS_MKDIR_SWAP:-1}" = 1 ] || return 0
    GBOS_MKDIR_SWAP=0
    mv "$GBOS_MKDIR_HELD" "$GBOS_MKDIR_ASIDE" || return 0
    ln -s "$GBOS_MKDIR_VICTIM" "$GBOS_MKDIR_HELD" || return 0
    return 0
}
trap gbos_mkdir_debug DEBUG
EOF
out=$(BASH_ENV="$T/r11-mk.env" \
    GBOS_MKDIR_HELD="$T/r11-mk-held" \
    GBOS_MKDIR_ASIDE="$T/r11-mk-aside" \
    GBOS_MKDIR_VICTIM="$T/r11-mk-victim" \
    bash "$R" --fixture "$FIX/kano" --out "$T/r11-mk-held/new/leaf" --date 20261014 2>&1); rc=$?
if [ "$rc" = 2 ] \
    && [ -L "$T/r11-mk-held" ] \
    && [ ! -e "$T/r11-mk-victim/new" ] \
    && [ ! -e "$T/r11-mk-aside/new" ] \
    && [ "$(cat "$T/r11-mk-victim/sentinel")" = sentinel ] \
    && [ -z "$(find "$T/r11-mk-victim" -mindepth 1 ! -name sentinel -print)" ] \
    && printf '%s\n' "$out" | grep -q 'symlink'; then
    ok "an ancestor swapped for a symlink before mkdir leaves the victim unchanged"
else
    bad "an ancestor swapped for a symlink before mkdir leaves the victim unchanged (rc=$rc)"
    printf '%s\n' "$out" >&2
fi

rm -rf "$T/r11-fifo-fix" "$T/r11-fifo-out"
mkdir -p "$T/r11-fifo-fix"
mkfifo "$T/r11-fifo-fix/getprop.txt"
out=$(timeout 5 bash "$R" --fixture "$T/r11-fifo-fix" --out "$T/r11-fifo-out" --date 20261014 2>&1); rc=$?
printf '%s\n' "$out" | grep -q 'not a regular file' \
    && ! printf '%s\n' "$out" | grep -q 'refusing symlink' \
    && [ "$rc" = 2 ] \
    && ok "a fifo fixture says it is not a regular file" \
    || bad "a fifo fixture says it is not a regular file (rc=$rc)"

rm -rf "$T/r11-pyi"
mkdir -p "$T/r11-pyi/cwd"
printf '%s\n' 'import sys; sys.stderr.write("cwd-json-shadow\n"); raise SystemExit(9)' > "$T/r11-pyi/cwd/json.py"
out=$(cd "$T/r11-pyi/cwd" && bash "$R" --fixture "$FIX/kano" --probe "$FIX/kano-probe" --out "$T/r11-pyi/out" --date 20261014 2>&1); rc=$?
if grep -F -q 'python3 -I - "$@"' "$R" \
    && [ "$rc" = 0 ] \
    && [ -f "$T/r11-pyi/out/summary.md" ] \
    && ! printf '%s\n' "$out" | grep -q 'cwd-json-shadow'; then
    ok "python3 -I does not load a module from the working directory"
else
    bad "python3 -I does not load a module from the working directory (rc=$rc)"
    printf '%s\n' "$out" >&2
fi

# Schema property: SOURCE_MAP leaves, not one token stuffed into every field at once.
if python3 - "$R" << 'PY'
import pathlib, stat, sys, types
script = sys.argv[1]
text = pathlib.Path(script).read_text(encoding="utf-8")
start = text.index("def _mode_private")
end = text.index("def _resolve_alias")
ns = {"stat": stat, "sys": types.SimpleNamespace(platform="linux")}
exec(text[start:end], ns)

class St:
    def __init__(self, uid, mode):
        self.st_uid = uid
        self.st_mode = mode

def check(link_uid, link_mode, parent_uid, parent_mode, platform, expect):
    ns["sys"].platform = platform
    ns["os"] = types.SimpleNamespace(fstat=lambda fd: St(parent_uid, stat.S_IFDIR | parent_mode))
    got = ns["_system_alias"](3, St(link_uid, link_mode))
    if got != expect:
        sys.stderr.write("alias %s/%s parent %s/%s %s -> %s\n" % (
            link_uid, oct(link_mode), parent_uid, oct(parent_mode), platform, got))
        raise SystemExit(1)

check(0, 0o755, 0, 0o755, "linux", True)
check(0, 0o777, 0, 0o755, "linux", True)
check(0, 0o777, 0, 0o755, "darwin", False)
check(1000, 0o755, 0, 0o755, "linux", False)
check(0, 0o755, 0, 0o775, "linux", False)
check(0, 0o755, 0, 0o707, "linux", False)
check(0, 0o755, 1000, 0o755, "linux", False)
print("alias branches ok")
PY
then
    ok "system-alias negative branches are refused"
else
    bad "system-alias negative branches are refused"
fi
bash_alias=$(sed -n '/^mode_is_private()/,/^}/p; /^meta_is_system()/,/^}/p' "$R")
if printf '%s\n' "$bash_alias" | bash -c '
    eval "$(cat)"
    fail=0
    meta_is_system 0 755 link || fail=1
    if [ "$(uname -s)" = Linux ]; then
        meta_is_system 0 777 link || fail=1
    else
        meta_is_system 0 777 link && fail=1
    fi
    meta_is_system 1000 755 link && fail=1
    meta_is_system 0 775 dir && fail=1
    meta_is_system 0 707 dir && fail=1
    meta_is_system 1000 755 dir && fail=1
    exit "$fail"
'; then
    ok "bash system-alias mode rejects a writable parent"
else
    bad "bash system-alias mode rejects a writable parent"
fi
if [ "$(uname -s)" = Linux ] && [ -L /bin ] && [ ! -w /bin ] && [ ! -w /usr/bin ]; then
    bin_meta=$(stat -c '%u %a' /bin 2>/dev/null || true)
    root_meta=$(stat -c '%u %a' / 2>/dev/null || true)
    if [ "$bin_meta" = "0 777" ] && [ "$root_meta" = "0 755" ]; then
        live=$(bash "$R" --out /bin/gbos-r12-no-write --date 20261014 2>&1 || true)
        if printf '%s\n' "$live" | grep -F -q 'must not follow a symlink'; then
            bad "root-owned Linux /bin symlink is a system alias"
        else
            ok "root-owned Linux /bin symlink is a system alias"
        fi
    fi
fi

# Schema property: SOURCE_MAP leaves, not one token stuffed into every field at once.
if python3 - "$R" "$T/full/gbos-report-20261014.txt" "$T/schema-root" > "$T/schema-precheck.txt" << 'PY'
import copy, json, os, pathlib, re, shutil, subprocess, sys, threading
from concurrent.futures import ThreadPoolExecutor
script, report, root = sys.argv[1:]
text = pathlib.Path(script).read_text(encoding="utf-8")
body = re.search(r"SCHEMA = \((.*?)\)\n", text, re.S)
src_body = re.search(r'SOURCE_MAP_TEXT = """(.*?)"""', text, re.S)
if not body or not src_body:
    sys.stderr.write("SCHEMA or SOURCE_MAP_TEXT missing\n")
    raise SystemExit(1)
keys = re.findall(r'"([^"]+)"', body.group(1))
adds = re.findall(r'add\("([^"]+)"', text)
if keys != list(dict.fromkeys(keys)) or set(keys) != set(adds):
    sys.stderr.write("schema %s\nadds %s\n" % (keys, adds))
    raise SystemExit(1)
report_text = pathlib.Path(report).read_text(encoding="utf-8")
missing = [k for k in keys if (k + ":") not in report_text]
if missing:
    sys.stderr.write("missing fields %s\n" % missing)
    raise SystemExit(1)
rows = []
for line in src_body.group(1).strip().splitlines():
    field, leaf = line.split("\t")
    rows.append((field, leaf))
if {field for field, _leaf in rows} != set(keys):
    sys.stderr.write("source map fields %s\n" % sorted({field for field, _leaf in rows}))
    raise SystemExit(1)
classes = [
    ("r4.mail@example.test", ["r4.mail", "example.test"]),
    ("02:de:ad:be:ef:11", ["02:de:ad"]),
    ("02-de-ad-be-ef-11", ["02-de-ad"]),
    ("02.de.ad.be.ef.11", ["02.de.ad"]),
    ("02de.adbe.ef11", ["02de.adbe"]),
    ("02_de_ad_be_ef_11", ["02_de_ad"]),
    ("02 de ad be ef 11", ["02 de ad"]),
    ("02+de+ad+be+ef+11", ["02+de+ad"]),
    ("02,de,ad,be,ef,11", ["02,de,ad"]),
    ("203.0.113.44", ["203.0.113"]),
    ("2001:db8:abcd::11", ["db8:abcd"]),
    ("490154203237518", ["490154"]),
    ("4901542 0323751 8", ["4901542", "0323751"]),
    ("deadbeefcafebabe", ["deadbeef", "cafebabe"]),
    ("dead beef cafe babe", ["dead beef", "cafe babe"]),
    ("deadbeef cafebabe", ["deadbeef", "cafebabe"]),
    ("R4SERIAL9F3A", ["R4SERIAL"]),
    ("/home/r4user/secret", ["r4user"]),
    ("r4host9f3a", ["r4host"]),
    ("alice at example.com", ["example.com", "alice at"]),
    ("alice (at) example.com", ["example.com", "(at)"]),
    ("6.1.75-android14-11-g02aabbccddee", ["02aabbccddee", "g02aabbccddee"]),
    ("wlalice1", ["wlalice1"]),
    ("wlsecret1", ["wlsecret1"]),
]
GETPROP = (
    "[ro.serialno]: [SYNTHFIXED001]\n"
    "[ro.build.display.id]: [CL3B.260622.271.R1]\n"
    "[ro.build.id]: [16452207]\n"
    "[ro.build.version.incremental]: [123456789012345]\n"
    "[ro.product.model]: [Kano]\n"
    "[ro.product.device]: [kano]\n"
    "[ro.product.name]: [kano]\n"
    "[ro.product.manufacturer]: [Google]\n"
    "[ro.hardware]: [kano]\n"
    "[ro.hardware.egl]: [mesa]\n"
    "[ro.hardware.vulkan]: [intel]\n"
    "[ro.boot.serialno]: [BOOTSERIAL999]\n"
    "[ro.boot.android_id]: [a1b2c3d4e5f67890]\n"
    "[net.hostname]: [synth-laptop]\n"
    "[init.svc.bluetooth]: [running]\n"
    "[wifi.interface]: [wlan0]\n"
    "[gsm.imei]: [490154203237518]\n"
    "[dhcp.wlan0.hostname]: [dhcp-host]\n"
    "[dhcp.wlan0.ipaddress]: [192.0.2.10]\n"
    "[gsm.serial]: [GSMSERIAL001]\n"
    "[init.svc.bluetooth_hal]: [running]\n"
    "[init.svc.vendor.bluetooth-1-0]: [running]\n"
    "[persist.radio.meid]: [MEID001]\n"
    "[persist.radio.serialno]: [RADIOSERIAL001]\n"
    "[persist.sys.android_id]: [persistandroid]\n"
    "[ril.serialnumber]: [RILSERIAL001]\n"
    "[ro.boot.btmacaddr]: [02:00:00:00:00:01]\n"
    "[ro.boot.wifimacaddr]: [02:00:00:00:00:02]\n"
    "[ro.build.host]: [buildhost]\n"
    "[ro.build.user]: [builder]\n"
    "[ro.product.board]: [kano]\n"
    "[vendor.usb.serialno]: [USBSERIAL001]\n"
)
CPU = (
    "processor: 0\n"
    "vendor_id: GenuineIntel\n"
    "cpu family: 6\n"
    "model: 154\n"
    "model name: 12th Gen Intel(R) Core(TM) i7-1260P\n"
    "stepping: 3\n"
)
BASE_PROBE = {
    "dmi": {
        "sys_vendor": "Google", "product_name": "Kano", "board_vendor": "Google",
        "board_name": "Kano", "bios_vendor": "coreboot", "bios_version": "MrChromebox-4.0",
        "bios_date": "2026-01-01",
    },
    "cpu": {
        "vendor_id": "GenuineIntel", "cpu family": "6", "model": "154",
        "stepping": "3", "model name": "12th Gen Intel(R) Core(TM) i7-1260P",
    },
    "audio": {"card_ids": ["sof-nau8825"]},
    "net": [{"name": "wlan0", "wireless": True}],
    "bluetooth": [{"name": "hci0"}],
    "pci": [{"class": "030000", "id": "8086:46a6", "driver": "xe"}],
    "input": [{"kind": "touchpad", "vendor": "04f3", "product": "323c", "name": "Touchpad", "bus": "0018"}],
    "firmware": {"uefi": True, "secure_boot": "off"},
}
DRAFT = 'schema = 1\n\n[model]\nid = "kano"\nstatus = "probe-only"\n'
REPORT = "firmware: UEFI yes, Secure Boot off\n"
MAY = {
    "dmi-sys-vendor", "probe-dmi-sys-vendor",
    "cpuinfo", "cpuinfo-family", "cpuinfo-model", "cpuinfo-stepping",
    "probe-cpu", "probe-cpu-family", "probe-cpu-model", "probe-cpu-stepping",
    "probe-pci-id", "probe-pci-class", "pci-bus",
    "drm-pci", "drm-uevent", "drm-sysfs-vendor", "drm-sysfs-device",
}

def base_files():
    return {
        "getprop.txt": GETPROP,
        "cat-dmi-sys-vendor.txt": "Google\n",
        "cat-dmi-product-name.txt": "Kano\n",
        "cat-dmi-board-vendor.txt": "Google\n",
        "cat-dmi-board-name.txt": "Kano\n",
        "cat-dmi-bios-vendor.txt": "coreboot\n",
        "cat-dmi-bios-version.txt": "MrChromebox-4.0\n",
        "cat-dmi-bios-date.txt": "2026-01-01\n",
        "uname-a.txt": "Linux synth-laptop 6.1.75-android14 #1 SMP\n",
        "cat-proc-version.txt": "Linux version 6.1.75-android14 (builder@buildhost) #1\n",
        "cat-proc-cpuinfo.txt": CPU,
        "cat-proc-asound-cards.txt": " 0 [sofnau8825]: sof-nau8825 - sof-nau8825\n",
        "cat-drm0-uevent.txt": "DRIVER=xe\nPCI_ID=8086:46a6\n",
        "cat-proc-bus-pci-devices.txt": "0000\t808646a6\t0\n",
        "ls-dev-dri.txt": "card0\nrenderD128\n",
        "dumpsys-wifi.txt": 'Wi-Fi is enabled\ncurState=ConnectedState\nmWifiInfo SSID: "SyntheticCafe", Supplicant state: COMPLETED\n',
        "cat-wlan-uevent.txt": "DRIVER=iwlwifi\n",
        "cat-wlan-operstate.txt": "up\n",
        "dumpsys-bluetooth.txt": "Bluetooth Status\n  enabled: true\n  state: ON\n",
        "cat-bt-uevent.txt": "DRIVER=btusb\n",
        "dumpsys-input.txt": "1: Touchpad\n  Classes: TOUCHPAD\n",
        "cat-proc-bus-input-devices.txt": 'I: Bus=0018 Vendor=04f3 Product=323c Version=0000\nN: Name="Touchpad"\nH: Handlers=event1\n',
        "dumpsys-power.txt": "Suspend Success Count: 4\nSuspend Failure Count: 1\n",
        "cat-suspend-success.txt": "4\n",
        "cat-suspend-fail.txt": "1\n",
        "ls-bluetooth.txt": "hci0\n",
        "logcat.txt": "no crash\n",
        "dumpsys-display.txt": "display dump\n",
        "dumpsys-audio.txt": "audio dump\n",
        "dumpsys-battery.txt": "battery dump\n",
        "cat-dmi-product-version.txt": "1.0\n",
    }

def set_prop(text, key, value):
    pat = re.compile(r"(?m)^\[" + re.escape(key) + r"\]: \[.*\]\s*$")
    line = "[%s]: [%s]" % (key, value)
    if not pat.search(text):
        raise SystemExit("missing prop " + key)
    return pat.sub(line, text, count=1)

PROBE_EXTRA = {}
RC_OVERRIDE = {}

def plant(leaf, token, files, data):
    global PROBE_EXTRA, RC_OVERRIDE
    PROBE_EXTRA = {}
    RC_OVERRIDE = {}
    draft, report = DRAFT, REPORT
    if leaf == "dmi-sys-vendor":
        files["cat-dmi-sys-vendor.txt"] = token + "\n"
    elif leaf == "probe-dmi-sys-vendor":
        data["dmi"]["sys_vendor"] = token
    elif leaf == "ignored-dmi-product":
        files["cat-dmi-product-name.txt"] = token + "\n"
    elif leaf == "dmi-board-vendor":
        files["cat-dmi-board-vendor.txt"] = token + "\n"
    elif leaf == "probe-dmi-board-vendor":
        data["dmi"]["board_vendor"] = token
    elif leaf == "ignored-dmi-board-name":
        files["cat-dmi-board-name.txt"] = token + "\n"
    elif leaf == "dmi-bios-vendor":
        files["cat-dmi-bios-vendor.txt"] = token + "\n"
    elif leaf == "probe-dmi-bios-vendor":
        data["dmi"]["bios_vendor"] = token
    elif leaf == "ignored-bios-version":
        files["cat-dmi-bios-version.txt"] = token + "\n"
    elif leaf == "ignored-bios-date":
        files["cat-dmi-bios-date.txt"] = token + "\n"
    elif leaf == "probe-firmware":
        data["firmware"]["secure_boot"] = token
        report = ""
    elif leaf == "probe-firmware-uefi":
        # A/B pass a real bool. The canary and the matrix pass the raw string.
        data["firmware"]["uefi"] = token
        report = ""
    elif leaf == "probe-report":
        data["firmware"] = {}
        report = "firmware: %s\n" % token
    elif leaf == "prop-display-id":
        files["getprop.txt"] = set_prop(files["getprop.txt"], "ro.build.display.id", token)
    elif leaf == "prop-build-id":
        files["getprop.txt"] = set_prop(files["getprop.txt"], "ro.build.display.id", "")
        files["getprop.txt"] = set_prop(files["getprop.txt"], "ro.build.id", token)
    elif leaf == "uname":
        files["uname-a.txt"] = "Linux synth-laptop %s #1\n" % token
    elif leaf == "proc-version":
        files["uname-a.txt"] = "Linux synth-laptop\n"
        files["cat-proc-version.txt"] = "Linux version %s (builder@buildhost) #1\n" % token
    elif leaf == "drm-driver":
        files["cat-drm0-uevent.txt"] = "DRIVER=%s\nPCI_ID=8086:46a6\n" % token
    elif leaf == "egl":
        # Shim path: a real GPU returns before egl is read.
        files["cat-drm0-uevent.txt"] = "DRIVER=gbos_efifb\n"
        files["getprop.txt"] = set_prop(files["getprop.txt"], "ro.hardware.egl", token)
    elif leaf == "vulkan":
        files["cat-drm0-uevent.txt"] = "DRIVER=gbos_efifb\n"
        files["getprop.txt"] = set_prop(files["getprop.txt"], "ro.hardware.egl", "")
        files["getprop.txt"] = set_prop(files["getprop.txt"], "ro.hardware.vulkan", token)
    elif leaf == "drm-pci":
        files["cat-drm0-uevent.txt"] = "DRIVER=xe\nPCI_ID=%s\n" % token
    elif leaf == "drm-uevent":
        files["cat-drm0-uevent.txt"] = "DRIVER=%s\nPCI_ID=%s\n" % (token, token)
    elif leaf == "cpuinfo":
        files["cat-proc-cpuinfo.txt"] = CPU.replace("vendor_id: GenuineIntel", "vendor_id: " + token, 1)
    elif leaf == "probe-cpu":
        data["cpu"]["vendor_id"] = token
    elif leaf == "ignored-cpu-model":
        files["cat-proc-cpuinfo.txt"] = CPU.replace(
            "model name: 12th Gen Intel(R) Core(TM) i7-1260P", "model name: " + token, 1)
    elif leaf == "ignored-asound":
        files["cat-proc-asound-cards.txt"] = " 0 [card ]: %s - x\n" % token
    elif leaf == "ignored-probe-audio":
        data["audio"] = {"card_ids": [token]}
    elif leaf == "dumpsys-wifi":
        files["dumpsys-wifi.txt"] = (
            "Wi-Fi is enabled\ncurState=ConnectedState\n"
            'mWifiInfo SSID: "%s", Supplicant state: COMPLETED\n' % token)
    elif leaf == "wlan-uevent":
        files["cat-wlan-uevent.txt"] = "DRIVER=%s\n" % token
    elif leaf == "wlan-operstate":
        files["cat-wlan-operstate.txt"] = token + "\n"
    elif leaf == "dumpsys-bluetooth":
        files["dumpsys-bluetooth.txt"] = "Bluetooth Status\n  enabled: true\n  state: ON\n  name: %s\n" % token
    elif leaf == "bt-uevent":
        files["cat-bt-uevent.txt"] = "DRIVER=%s\n" % token
    elif leaf == "dumpsys-input":
        files["dumpsys-input.txt"] = "1: %s\n  Classes: TOUCHPAD\n" % token
    elif leaf == "proc-input":
        files["cat-proc-bus-input-devices.txt"] = (
            'I: Bus=0018 Vendor=04f3 Product=323c Version=0000\nN: Name="%s"\nH: Handlers=event1\n' % token)
    elif leaf == "ignored-probe-input":
        data["input"] = [{"kind": "touchpad", "vendor": "04f3", "product": "323c", "name": token, "bus": "0018"}]
    elif leaf == "dumpsys-power":
        files["dumpsys-power.txt"] = "Suspend Success Count: %s\nSuspend Failure Count: %s\n" % (token, token)
        RC_OVERRIDE["cat-suspend-success"] = "1\n"
        RC_OVERRIDE["cat-suspend-fail"] = "1\n"
    elif leaf == "suspend-sysfs":
        files["dumpsys-power.txt"] = "no counts\n"
        files["cat-suspend-success.txt"] = token + "\n"
        files["cat-suspend-fail.txt"] = token + "\n"
    elif leaf == "ls-dri":
        files["ls-dev-dri.txt"] = "card0\nrenderD128\n%s\n" % token
    elif leaf == "probe-pci":
        data["pci"] = [{"class": "030000", "id": "8086:46a6", "driver": token}]
    elif leaf == "probe-net":
        data["net"] = [{"name": token, "wireless": True}]
    elif leaf == "probe-bt":
        data["bluetooth"] = [{"name": token}]
    elif leaf == "drm-sysfs-vendor":
        files["cat-drm0-uevent.txt"] = "DRIVER=xe\n"
        files["cat-drm-vendor.txt"] = token + "\n"
        files["cat-drm-device.txt"] = "0x46a6\n"
    elif leaf == "drm-sysfs-device":
        files["cat-drm0-uevent.txt"] = "DRIVER=xe\n"
        files["cat-drm-vendor.txt"] = "0x8086\n"
        files["cat-drm-device.txt"] = token + "\n"
    elif leaf == "pci-bus":
        files["cat-proc-bus-pci-devices.txt"] = token + "\n"
    elif leaf == "cpuinfo-family":
        files["cat-proc-cpuinfo.txt"] = CPU.replace("cpu family: 6", "cpu family: " + token, 1)
    elif leaf == "cpuinfo-model":
        files["cat-proc-cpuinfo.txt"] = CPU.replace("\nmodel: 154\n", "\nmodel: " + token + "\n", 1)
    elif leaf == "cpuinfo-stepping":
        files["cat-proc-cpuinfo.txt"] = CPU.replace("stepping: 3", "stepping: " + token, 1)
    elif leaf == "probe-cpu-family":
        data["cpu"]["cpu family"] = token
    elif leaf == "probe-cpu-model":
        data["cpu"]["model"] = token
    elif leaf == "probe-cpu-stepping":
        data["cpu"]["stepping"] = token
    elif leaf == "wlan-pci":
        files["cat-wlan-uevent.txt"] = "DRIVER=notadriver\nPCI_ID=%s\n" % token
    elif leaf == "wlan-usb":
        files["cat-wlan-uevent.txt"] = "DRIVER=notadriver\nPRODUCT=%s\n" % token
    elif leaf == "wlan-usb-vid":
        files["cat-wlan-uevent.txt"] = "DRIVER=notadriver\nPRODUCT=%s/5678/0001\n" % token
    elif leaf == "wlan-usb-pid":
        files["cat-wlan-uevent.txt"] = "DRIVER=notadriver\nPRODUCT=1234/%s/0001\n" % token
    elif leaf == "wlan-usb-bcd":
        files["cat-wlan-uevent.txt"] = "DRIVER=notadriver\nPRODUCT=1234/5678/%s\n" % token
    elif leaf == "bt-pci":
        files["cat-bt-uevent.txt"] = "DRIVER=notabtdrv\nPCI_ID=%s\n" % token
    elif leaf == "bt-usb":
        files["cat-bt-uevent.txt"] = "DRIVER=notabtdrv\nPRODUCT=%s\n" % token
    elif leaf == "bt-usb-vid":
        files["cat-bt-uevent.txt"] = "DRIVER=notabtdrv\nPRODUCT=%s/5678/0001\n" % token
    elif leaf == "bt-usb-pid":
        files["cat-bt-uevent.txt"] = "DRIVER=notabtdrv\nPRODUCT=1234/%s/0001\n" % token
    elif leaf == "bt-usb-bcd":
        files["cat-bt-uevent.txt"] = "DRIVER=notabtdrv\nPRODUCT=1234/5678/%s\n" % token
    elif leaf == "wifi-curstate":
        files["dumpsys-wifi.txt"] = "Wi-Fi is enabled\ncurState=%s\n" % token
    elif leaf == "wifi-supplicant":
        files["dumpsys-wifi.txt"] = "Wi-Fi is enabled\nSupplicant state: %s\n" % token
    elif leaf == "bt-enabled":
        files["dumpsys-bluetooth.txt"] = "Bluetooth Status\n  enabled: %s\n" % token
    elif leaf == "bt-radio-state":
        files["dumpsys-bluetooth.txt"] = "Bluetooth Status\n  state: %s\n" % token
    elif leaf == "dumpsys-input-id":
        files["dumpsys-input.txt"] = (
            "1: Touchpad\n  Classes: TOUCHPAD\n"
            "  Identifier: bus=0x0018 vendor=0x%s product=0x323c\n" % token)
        files["cat-proc-bus-input-devices.txt"] = ""
    elif leaf == "dumpsys-input-bus":
        files["dumpsys-input.txt"] = (
            "1: Touchpad\n  Classes: TOUCHPAD\n"
            "  Identifier: bus=0x%s vendor=0x04f3 product=0x323c\n" % token)
        files["cat-proc-bus-input-devices.txt"] = ""
    elif leaf == "dumpsys-input-product":
        files["dumpsys-input.txt"] = (
            "1: Touchpad\n  Classes: TOUCHPAD\n"
            "  Identifier: bus=0x0018 vendor=0x04f3 product=0x%s\n" % token)
        files["cat-proc-bus-input-devices.txt"] = ""
    elif leaf == "dumpsys-input-classes":
        files["dumpsys-input.txt"] = "1: Generic Device\n  Classes: %s\n" % token
        files["cat-proc-bus-input-devices.txt"] = ""
    elif leaf == "dumpsys-input-sources":
        files["dumpsys-input.txt"] = "1: Generic Device\n  Sources: %s\n" % token
        files["cat-proc-bus-input-devices.txt"] = ""
    elif leaf == "probe-input-kind":
        data["input"] = [{"kind": token, "vendor": "04f3", "product": "323c", "name": "Touchpad", "bus": "0018"}]
    elif leaf == "probe-input-busname":
        data["input"] = [{"kind": "touchpad", "vendor": "04f3", "product": "323c", "name": "Touchpad", "bus": "", "busname": token}]
    elif leaf == "power-success":
        files["dumpsys-power.txt"] = "Suspend Success Count: %s\nSuspend Failure Count: 1\n" % token
        files["cat-suspend-success.txt"] = ""
        files["cat-suspend-fail.txt"] = ""
    elif leaf == "power-fail":
        files["dumpsys-power.txt"] = "Suspend Success Count: 4\nSuspend Failure Count: %s\n" % token
        files["cat-suspend-success.txt"] = ""
        files["cat-suspend-fail.txt"] = ""
    elif leaf == "suspend-success":
        files["dumpsys-power.txt"] = "no counts\n"
        files["cat-suspend-success.txt"] = token + "\n"
        files["cat-suspend-fail.txt"] = "1\n"
    elif leaf == "suspend-fail":
        files["dumpsys-power.txt"] = "no counts\n"
        files["cat-suspend-success.txt"] = "4\n"
        files["cat-suspend-fail.txt"] = token + "\n"
    elif leaf == "proc-input-vendor":
        files["cat-proc-bus-input-devices.txt"] = (
            'I: Bus=0018 Vendor=%s Product=323c Version=0000\nN: Name="Touchpad"\nH: Handlers=event1\n' % token)
    elif leaf == "proc-input-product":
        files["cat-proc-bus-input-devices.txt"] = (
            'I: Bus=0018 Vendor=04f3 Product=%s Version=0000\nN: Name="Touchpad"\nH: Handlers=event1\n' % token)
    elif leaf == "proc-input-bus":
        files["cat-proc-bus-input-devices.txt"] = (
            'I: Bus=%s Vendor=04f3 Product=323c Version=0000\nN: Name="Touchpad"\nH: Handlers=event1\n' % token)
    elif leaf == "probe-input-vendor":
        data["input"] = [{"kind": "touchpad", "vendor": token, "product": "323c", "name": "Touchpad", "bus": "0018"}]
    elif leaf == "probe-input-product":
        data["input"] = [{"kind": "touchpad", "vendor": "04f3", "product": token, "name": "Touchpad", "bus": "0018"}]
    elif leaf == "probe-input-bus":
        data["input"] = [{"kind": "touchpad", "vendor": "04f3", "product": "323c", "name": "Touchpad", "bus": token}]
    elif leaf == "probe-input-driver":
        data["input"] = [{"kind": "touchpad", "vendor": "04f3", "product": "323c", "name": "Touchpad", "bus": "0018", "driver": token}]
    elif leaf == "probe-pci-id":
        data["pci"] = [{"class": "030000", "id": token, "driver": "xe"}]
    elif leaf == "probe-draft-id":
        draft = 'schema = 1\n\n[model]\nid = "%s"\nstatus = "probe-only"\n' % token
    elif leaf == "probe-draft-status":
        draft = 'schema = 1\n\n[model]\nid = "kano"\nstatus = "%s"\n' % token
    elif leaf == "ls-bluetooth":
        files["dumpsys-bluetooth.txt"] = "no status here\n"
        files["getprop.txt"] = set_prop(files["getprop.txt"], "init.svc.bluetooth", "")
        files["ls-bluetooth.txt"] = token + "\n"
    elif leaf == "prop-bt-service":
        files["dumpsys-bluetooth.txt"] = "no status here\n"
        files["ls-bluetooth.txt"] = "\n"
        files["getprop.txt"] = set_prop(files["getprop.txt"], "init.svc.bluetooth", token)
    elif leaf == "probe-net-wireless":
        data["net"] = [{"name": "wlan0", "wireless": token}]
    elif leaf == "drm-card1":
        files["ls-dev-dri.txt"] = "card1\n"
        files["cat-drm1-uevent.txt"] = "DRIVER=%s\nPCI_ID=8086:46a6\n" % token
        files["cat-drm1-vendor.txt"] = "0x8086\n"
        files["cat-drm1-device.txt"] = "0x46a6\n"
    elif leaf == "drm-card2":
        files["ls-dev-dri.txt"] = "card2\n"
        files["cat-drm2-uevent.txt"] = "DRIVER=%s\nPCI_ID=8086:46a6\n" % token
        files["cat-drm2-vendor.txt"] = "0x8086\n"
        files["cat-drm2-device.txt"] = "0x46a6\n"
    elif leaf == "drm-card3":
        files["ls-dev-dri.txt"] = "card3\n"
        files["cat-drm3-uevent.txt"] = "DRIVER=%s\nPCI_ID=8086:46a6\n" % token
        files["cat-drm3-vendor.txt"] = "0x8086\n"
        files["cat-drm3-device.txt"] = "0x46a6\n"
    elif leaf == "ignored-probe-net-address":
        data["net"] = [{"name": "wlan0", "wireless": True, "address": token}]
    elif leaf == "probe-pci-class":
        data["pci"] = [{"class": token, "id": "8086:46a6", "driver": "xe"}]
    elif leaf == "probe-pci-unread":
        data["pci"] = [{"class": "030000", "id": "8086:46a6", "driver": "xe", "unread": token}]
    elif leaf == "ignored-logcat":
        files["logcat.txt"] = token + "\n"
    elif leaf == "ignored-dumpsys-display":
        files["dumpsys-display.txt"] = token + "\n"
    elif leaf == "ignored-dumpsys-audio":
        files["dumpsys-audio.txt"] = token + "\n"
    elif leaf == "ignored-dumpsys-battery":
        files["dumpsys-battery.txt"] = token + "\n"
    elif leaf == "ignored-dmi-product-version":
        files["cat-dmi-product-version.txt"] = token + "\n"
    elif leaf == "ignored-getprop-rest":
        for key in (
            "ro.serialno", "ro.build.version.incremental", "ro.product.model", "ro.product.device",
            "ro.product.name", "ro.product.manufacturer", "ro.hardware",
            "wifi.interface", "ro.boot.serialno", "ro.boot.android_id", "net.hostname",
            "gsm.imei", "dhcp.wlan0.hostname", "dhcp.wlan0.ipaddress", "gsm.serial",
            "init.svc.bluetooth_hal", "init.svc.vendor.bluetooth-1-0",
            "persist.radio.meid", "persist.radio.serialno", "persist.sys.android_id",
            "ril.serialnumber", "ro.boot.btmacaddr", "ro.boot.wifimacaddr",
            "ro.build.host", "ro.build.user", "ro.product.board", "vendor.usb.serialno",
        ):
            files["getprop.txt"] = set_prop(files["getprop.txt"], key, token)
    elif leaf == "ignored-probe-dmi-rest":
        for key in ("product_name", "board_name", "bios_version", "bios_date", "product_version"):
            data["dmi"][key] = token
    elif leaf == "ignored-probe-cpu-model":
        data["cpu"]["model name"] = token
    elif leaf == "ignored-probe-bt-device":
        data["bluetooth"] = [{"name": "hci0", "device": token}]
    elif leaf == "ignored-probe-pci-slot":
        data["pci"] = [{"class": "030000", "id": "8086:46a6", "driver": "xe", "slot": token}]
    elif leaf == "ignored-draft-rest":
        draft = 'schema = "%s"\n\n[model]\nid = "kano"\nname = "%s"\nstatus = "probe-only"\n' % (token, token)
    elif leaf == "ignored-probe-coverage":
        PROBE_EXTRA["coverage.txt"] = token + "\n"
    else:
        raise SystemExit("no planter for " + leaf)
    return draft, report

leaves = []
seen = set()
for _field, leaf in rows:
    if leaf in ("constant", "derived") or leaf in seen:
        continue
    seen.add(leaf)
    leaves.append(leaf)
planted = {
    "dmi-sys-vendor", "probe-dmi-sys-vendor", "ignored-dmi-product", "dmi-board-vendor",
    "probe-dmi-board-vendor", "ignored-dmi-board-name", "dmi-bios-vendor", "probe-dmi-bios-vendor",
    "ignored-bios-version", "ignored-bios-date", "probe-firmware", "probe-report",
    "prop-display-id", "prop-build-id", "uname", "proc-version", "drm-driver", "egl",
    "drm-pci", "drm-uevent", "cpuinfo", "probe-cpu", "ignored-cpu-model", "ignored-asound",
    "ignored-probe-audio", "dumpsys-wifi", "wlan-uevent", "wlan-operstate", "dumpsys-bluetooth",
    "bt-uevent", "dumpsys-input", "proc-input", "ignored-probe-input", "dumpsys-power",
    "suspend-sysfs", "ls-dri", "probe-pci", "probe-pci-id", "probe-net", "probe-bt",
    "probe-draft-id", "probe-draft-status",
    "probe-firmware-uefi", "drm-sysfs-vendor", "drm-sysfs-device", "pci-bus",
    "cpuinfo-family", "cpuinfo-model", "cpuinfo-stepping",
    "probe-cpu-family", "probe-cpu-model", "probe-cpu-stepping",
    "wlan-pci", "bt-pci", "vulkan",
    "wlan-usb", "wlan-usb-vid", "wlan-usb-pid", "wlan-usb-bcd",
    "bt-usb", "bt-usb-vid", "bt-usb-pid", "bt-usb-bcd",
    "wifi-curstate", "wifi-supplicant", "bt-enabled", "bt-radio-state",
    "dumpsys-input-id", "dumpsys-input-bus", "dumpsys-input-product",
    "dumpsys-input-classes", "dumpsys-input-sources",
    "probe-input-kind", "probe-input-busname",
    "power-success", "power-fail", "suspend-success", "suspend-fail",
    "proc-input-vendor", "proc-input-product", "proc-input-bus",
    "probe-input-vendor", "probe-input-product", "probe-input-bus", "probe-input-driver",
    "ls-bluetooth", "prop-bt-service", "probe-net-wireless", "probe-pci-class",
    "drm-card1", "drm-card2", "drm-card3", "ignored-probe-net-address",
    "probe-pci-unread",
    "ignored-logcat", "ignored-dumpsys-display", "ignored-dumpsys-audio",
    "ignored-dumpsys-battery", "ignored-dmi-product-version", "ignored-getprop-rest",
    "ignored-probe-dmi-rest", "ignored-probe-cpu-model", "ignored-probe-bt-device",
    "ignored-probe-pci-slot", "ignored-draft-rest", "ignored-probe-coverage",
}
if set(leaves) != planted:
    sys.stderr.write("map %s\nplanter %s\n" % (sorted(set(leaves) - planted), sorted(planted - set(leaves))))
    raise SystemExit(1)
root = pathlib.Path(root)
# Reachability is what the shipped CLI prints. There is no frame tracer.
# A leaf is PRINTED or IGNORED, never both. constant/derived are not leaves.
IGNORED_UNNAMED = {
    "dumpsys-wifi", "dumpsys-bluetooth",
    "wlan-usb-bcd", "bt-usb-bcd",
}
IGNORED = {leaf for leaf in leaves if leaf.startswith("ignored-") or leaf in IGNORED_UNNAMED}
PRINTED = [leaf for leaf in leaves if leaf not in IGNORED]
if set(PRINTED) & IGNORED or set(PRINTED) | IGNORED != set(leaves):
    sys.stderr.write("class overlap printed %s ignored %s\n" % (
        sorted(set(PRINTED) & IGNORED), sorted(set(leaves) - set(PRINTED) - IGNORED)))
    raise SystemExit(1)
field_of = {}
for _field, _leaf in rows:
    if _leaf in ("constant", "derived"):
        continue
    field_of.setdefault(_leaf, _field)
label_block = re.search(r"ROW_LABELS = \((.*?)\)\n", text, re.S)
zh_of = dict(re.findall(r'\("([^"]+)", "([^"]+)"\)', label_block.group(1) if label_block else ""))
repo = pathlib.Path(script).resolve().parents[1]
fix_root = repo / "tests" / "report" / "fixtures"
id_block = re.search(r"done << 'EOF'\n(.*?\n)EOF\n", text, re.S)
allow_ids = set()
for line in (id_block.group(1) if id_block else "").splitlines():
    if "|" in line and not line.startswith("#"):
        allow_ids.add(line.split("|", 1)[0].strip())
# Not an allowlist capture. Sidecars (.rc/.err) and the scrub corpus are test controls.
EXCLUDE_FILES = {
    "cat-bt-name.txt": "not an allowlist id",
}
scanned = set()
unknown_files = []
for path in fix_root.rglob("*"):
    if not path.is_file():
        continue
    if path.suffix in (".rc", ".err", ".in", ".out"):
        continue
    if "scrub" in path.parts:
        continue
    if path.name in EXCLUDE_FILES:
        continue
    if path.name == "probe.json":
        walk_target = json.loads(path.read_text(encoding="utf-8"))

        def walk_keys(obj, prefix, _scanned=scanned):
            if isinstance(obj, dict):
                for key, val in obj.items():
                    kid = prefix + "." + key if prefix else key
                    _scanned.add("key:" + kid)
                    walk_keys(val, kid)
            elif isinstance(obj, list):
                for item in obj:
                    walk_keys(item, prefix)

        walk_keys(walk_target, "")
        continue
    if path.name == "profile.draft.toml":
        scanned.add("file:profile.draft.toml")
        for match in re.finditer(r'(?m)^(?:([A-Za-z0-9_.]+) =|\[([A-Za-z0-9_.]+)\]\s*$)', path.read_text(encoding="utf-8")):
            name = match.group(1) or match.group(2)
            if name:
                scanned.add("draft:" + name)
        continue
    if path.name in ("report.txt", "coverage.txt"):
        scanned.add("file:" + path.name)
        continue
    if path.name == "getprop.txt":
        scanned.add("file:getprop.txt")
        for match in re.finditer(r"(?m)^\[([^\]]+)\]:", path.read_text(encoding="utf-8")):
            scanned.add("prop:" + match.group(1))
        continue
    if path.suffix == ".txt":
        stem = path.name[:-4]
        if stem in allow_ids:
            scanned.add("file:" + path.name)
        else:
            unknown_files.append(str(path.relative_to(fix_root)))
if unknown_files:
    sys.stderr.write("unclassified fixture file: %s\n" % unknown_files)
    raise SystemExit(1)
for required in (
    "key:net.address", "key:input.bus", "key:firmware.uefi", "key:firmware.secure_boot",
    "file:cat-drm1-uevent.txt", "file:cat-drm2-uevent.txt", "prop:ro.build.host",
    "prop:ro.boot.wifimacaddr",
):
    if required not in scanned:
        sys.stderr.write("completeness missed %s\n" % required)
        raise SystemExit(1)
COVER = {
    "file:getprop.txt": "prop-display-id",
    "file:dumpsys-input.txt": "dumpsys-input",
    "file:dumpsys-display.txt": "ignored-dumpsys-display",
    "file:dumpsys-audio.txt": "ignored-dumpsys-audio",
    "file:dumpsys-power.txt": "dumpsys-power",
    "file:dumpsys-battery.txt": "ignored-dumpsys-battery",
    "file:dumpsys-wifi.txt": "dumpsys-wifi",
    "file:dumpsys-bluetooth.txt": "dumpsys-bluetooth",
    "file:ls-dev-dri.txt": "ls-dri",
    "file:ls-bluetooth.txt": "ls-bluetooth",
    "file:uname-a.txt": "uname",
    "file:logcat.txt": "ignored-logcat",
    "file:cat-proc-cpuinfo.txt": "cpuinfo",
    "file:cat-proc-version.txt": "proc-version",
    "file:cat-proc-asound-cards.txt": "ignored-asound",
    "file:cat-proc-bus-input-devices.txt": "proc-input",
    "file:cat-dmi-sys-vendor.txt": "dmi-sys-vendor",
    "file:cat-dmi-product-name.txt": "ignored-dmi-product",
    "file:cat-dmi-product-version.txt": "ignored-dmi-product-version",
    "file:cat-dmi-board-vendor.txt": "dmi-board-vendor",
    "file:cat-dmi-board-name.txt": "ignored-dmi-board-name",
    "file:cat-dmi-bios-vendor.txt": "dmi-bios-vendor",
    "file:cat-dmi-bios-version.txt": "ignored-bios-version",
    "file:cat-dmi-bios-date.txt": "ignored-bios-date",
    "file:cat-drm-vendor.txt": "drm-sysfs-vendor",
    "file:cat-drm-device.txt": "drm-sysfs-device",
    "file:cat-drm0-uevent.txt": "drm-driver",
    "file:cat-drm1-uevent.txt": "drm-card1",
    "file:cat-drm2-uevent.txt": "drm-card2",
    "file:cat-suspend-success.txt": "suspend-success",
    "file:cat-suspend-fail.txt": "suspend-fail",
    "file:cat-proc-bus-pci-devices.txt": "pci-bus",
    "file:cat-wlan-operstate.txt": "wlan-operstate",
    "file:cat-wlan-uevent.txt": "wlan-uevent",
    "file:cat-bt-uevent.txt": "bt-uevent",
    "prop:ro.build.display.id": "prop-display-id",
    "prop:ro.build.id": "prop-build-id",
    "prop:ro.hardware.egl": "egl",
    "prop:ro.hardware.vulkan": "vulkan",
    "prop:init.svc.bluetooth": "prop-bt-service",
    "prop:ro.build.version.incremental": "ignored-getprop-rest",
    "prop:ro.product.model": "ignored-getprop-rest",
    "prop:ro.product.device": "ignored-getprop-rest",
    "prop:ro.product.name": "ignored-getprop-rest",
    "prop:ro.product.manufacturer": "ignored-getprop-rest",
    "prop:ro.hardware": "ignored-getprop-rest",
    "prop:ro.serialno": "ignored-getprop-rest",
    "prop:ro.boot.serialno": "ignored-getprop-rest",
    "prop:ro.boot.android_id": "ignored-getprop-rest",
    "prop:net.hostname": "ignored-getprop-rest",
    "prop:wifi.interface": "ignored-getprop-rest",
    "prop:gsm.imei": "ignored-getprop-rest",
    "prop:dhcp.wlan0.hostname": "ignored-getprop-rest",
    "prop:dhcp.wlan0.ipaddress": "ignored-getprop-rest",
    "prop:gsm.serial": "ignored-getprop-rest",
    "prop:init.svc.bluetooth_hal": "ignored-getprop-rest",
    "prop:init.svc.vendor.bluetooth-1-0": "ignored-getprop-rest",
    "prop:persist.radio.meid": "ignored-getprop-rest",
    "prop:persist.radio.serialno": "ignored-getprop-rest",
    "prop:persist.sys.android_id": "ignored-getprop-rest",
    "prop:ril.serialnumber": "ignored-getprop-rest",
    "prop:ro.boot.btmacaddr": "ignored-getprop-rest",
    "prop:ro.boot.wifimacaddr": "ignored-getprop-rest",
    "prop:ro.build.host": "ignored-getprop-rest",
    "prop:ro.build.user": "ignored-getprop-rest",
    "prop:ro.product.board": "ignored-getprop-rest",
    "prop:vendor.usb.serialno": "ignored-getprop-rest",
    "key:audio": "ignored-probe-audio",
    "key:audio.card_ids": "ignored-probe-audio",
    "key:bluetooth": "probe-bt",
    "key:bluetooth.device": "ignored-probe-bt-device",
    "key:bluetooth.name": "probe-bt",
    "key:cpu": "probe-cpu",
    "key:dmi": "probe-dmi-sys-vendor",
    "key:input": "probe-input-kind",
    "key:net": "probe-net",
    "key:pci": "probe-pci",
    "key:cpu.cpu family": "probe-cpu-family",
    "key:cpu.model": "probe-cpu-model",
    "key:cpu.model name": "ignored-probe-cpu-model",
    "key:cpu.stepping": "probe-cpu-stepping",
    "key:cpu.vendor_id": "probe-cpu",
    "key:dmi.bios_date": "ignored-probe-dmi-rest",
    "key:dmi.bios_vendor": "probe-dmi-bios-vendor",
    "key:dmi.bios_version": "ignored-probe-dmi-rest",
    "key:dmi.board_name": "ignored-probe-dmi-rest",
    "key:dmi.board_vendor": "probe-dmi-board-vendor",
    "key:dmi.product_name": "ignored-probe-dmi-rest",
    "key:dmi.product_version": "ignored-probe-dmi-rest",
    "key:dmi.sys_vendor": "probe-dmi-sys-vendor",
    "key:input.kind": "probe-input-kind",
    "key:input.name": "ignored-probe-input",
    "key:input.vendor": "probe-input-vendor",
    "key:input.product": "probe-input-product",
    "key:input.bus": "probe-input-bus",
    "key:net.name": "probe-net",
    "key:net.wireless": "probe-net-wireless",
    "key:net.address": "ignored-probe-net-address",
    "key:firmware": "probe-firmware",
    "key:firmware.uefi": "probe-firmware-uefi",
    "key:firmware.secure_boot": "probe-firmware",
    "key:pci.class": "probe-pci-class",
    "key:pci.driver": "probe-pci",
    "key:pci.id": "probe-pci-id",
    "key:pci.slot": "ignored-probe-pci-slot",
    "file:profile.draft.toml": "probe-draft-id",
    "file:report.txt": "probe-report",
    "file:coverage.txt": "ignored-probe-coverage",
    "draft:schema": "ignored-draft-rest",
    "draft:model": "ignored-draft-rest",
    "draft:id": "probe-draft-id",
    "draft:name": "ignored-draft-rest",
    "draft:status": "probe-draft-status",
    "file:cat-drm1-vendor.txt": "drm-card1",
    "file:cat-drm1-device.txt": "drm-card1",
    "file:cat-drm2-vendor.txt": "drm-card2",
    "file:cat-drm2-device.txt": "drm-card2",
    "file:cat-drm3-vendor.txt": "drm-card3",
    "file:cat-drm3-device.txt": "drm-card3",
    "file:cat-drm3-uevent.txt": "drm-card3",
    "key:pci.unread": "probe-pci-unread",
}
for cid in (
    "cat-drm1-vendor", "cat-drm1-device",
    "cat-drm2-vendor", "cat-drm2-device",
    "cat-drm3-vendor", "cat-drm3-device", "cat-drm3-uevent",
):
    if cid not in allow_ids:
        sys.stderr.write("product allowlist lost %s\n" % cid)
        raise SystemExit(1)
    scanned.add("file:" + cid + ".txt")
scanned.add("key:pci.unread")
missing_src = sorted(scanned - set(COVER))
if missing_src:
    sys.stderr.write("fixture source not classified: %s\n" % missing_src)
    raise SystemExit(1)
bad_cover = sorted(set(COVER.values()) - set(leaves))
if bad_cover:
    sys.stderr.write("cover leaf missing from map: %s\n" % bad_cover)
    raise SystemExit(1)
PINNED = "6.18.21-android17-5-g3938c8d97142-ab16302624"
# token A, token B, mark that must be only in A's row, mark only in B's row.
# Feeds lists every row the two valid values are allowed to change.
PAIRS = {
    "dmi-sys-vendor": ("Google", "Dell", "model: Google ", "model: Dell "),
    "probe-dmi-sys-vendor": ("Google", "Dell", "model: Google ", "model: Dell "),
    "dmi-board-vendor": ("Google", "Dell", "board: Google ", "board: Dell "),
    "probe-dmi-board-vendor": ("Google", "Dell", "board: Google ", "board: Dell "),
    "dmi-bios-vendor": ("coreboot", "AMI", "firmware: coreboot ", "firmware: AMI "),
    "probe-dmi-bios-vendor": ("coreboot", "AMI", "firmware: coreboot ", "firmware: AMI "),
    "probe-firmware": ("off", "on", "Secure Boot off", "Secure Boot on"),
    "probe-firmware-uefi": ("yes", "no", "UEFI yes", "UEFI no"),
    "probe-report": ("UEFI yes, Secure Boot off", "UEFI no, Secure Boot on", "UEFI yes", "UEFI no"),
    "prop-display-id": ("CL3B.260622.271.R1", "16452207", "CL3B.260622.271.R1", "16452207"),
    "prop-build-id": ("16452207", "CL3B.260622.271.R1", "build: 16452207 ", "build: CL3B.260622.271.R1 "),
    "uname": (PINNED, "6.1.75-android14", PINNED, "kernel: other "),
    "proc-version": (PINNED, "6.1.75-android14", PINNED, "kernel: other "),
    "drm-driver": ("xe", "i915", "bound (xe)", "bound (i915)"),
    "drm-uevent": ("xe", "i915", "bound (xe)", "bound (i915)"),
    "egl": ("swiftshader", "mesa", "SwiftShader", "no native GPU driver (gbos_efifb)"),
    "vulkan": ("pastel", "intel", "SwiftShader", "no native GPU driver (gbos_efifb)"),
    "drm-pci": ("8086:46a6", "8086:1234", "8086:46a6", "8086:1234"),
    "drm-sysfs-vendor": ("0x8086", "0x1002", "8086:46a6", "1002:46a6"),
    "drm-sysfs-device": ("0x46a6", "0x1234", "8086:46a6", "8086:1234"),
    "pci-bus": ("0000\t808646a6\t0", "0000\t808651f0\t0", "agree: DMI, CPU, PCI", "omitted (probe is not the same machine)"),
    "cpuinfo": ("GenuineIntel", "AuthenticAMD", "GenuineIntel", "AuthenticAMD"),
    "cpuinfo-family": ("6", "15", "family 6", "family 15"),
    "cpuinfo-model": ("154", "15", "model 154", "model 15 stepping"),
    "cpuinfo-stepping": ("3", "4", "stepping 3", "stepping 4"),
    "probe-cpu": ("GenuineIntel", "AuthenticAMD", "GenuineIntel", "AuthenticAMD"),
    "probe-cpu-family": ("6", "15", "family 6", "family 15"),
    "probe-cpu-model": ("154", "15", "model 154", "model 15 stepping"),
    "probe-cpu-stepping": ("3", "4", "stepping 3", "stepping 4"),
    "wlan-uevent": ("iwlwifi", "ath9k", "iwlwifi", "ath9k"),
    "wlan-operstate": ("up", "down", "wifi: connected,", "wifi: unknown,"),
    "wlan-pci": ("8086:46a6", "8086:51f0", "8086:46a6", "8086:51f0"),
    "wlan-usb": ("1234/5678/0001", "9abc/5678/0001", "1234:5678", "9abc:5678"),
    "wlan-usb-vid": ("1234", "9abc", "1234:5678", "9abc:5678"),
    "wlan-usb-pid": ("5678", "9abc", "1234:5678", "1234:9abc"),
    "bt-uevent": ("btusb", "btintel", "btusb", "btintel"),
    "bt-pci": ("8086:46a6", "8086:51f0", "8086:46a6", "8086:51f0"),
    "bt-usb": ("1234/5678/0001", "9abc/5678/0001", "1234:5678", "9abc:5678"),
    "bt-usb-vid": ("1234", "9abc", "1234:5678", "9abc:5678"),
    "bt-usb-pid": ("5678", "9abc", "1234:5678", "1234:9abc"),
    "wifi-curstate": ("ConnectedState", "DisconnectedState", "wifi: connected,", "enabled, not connected"),
    "wifi-supplicant": ("COMPLETED", "DISCONNECTED", "wifi: connected,", "enabled, not connected"),
    "bt-enabled": ("true", "false", "bluetooth: on", "bluetooth: off"),
    "bt-radio-state": ("ON", "OFF", "bluetooth: on", "bluetooth: off"),
    "ls-bluetooth": ("hci0", "none", "adapter present", "bluetooth: unknown,"),
    "prop-bt-service": ("running", "stopped", "service running", "service stopped"),
    "dumpsys-input-id": ("04f3", "abcd", "04f3:323c", "abcd:323c"),
    "dumpsys-input-bus": ("0018", "0003", "(i2c)", "(usb)"),
    "dumpsys-input-product": ("323c", "abcd", "04f3:323c", "04f3:abcd"),
    "dumpsys-input-classes": ("TOUCHPAD", "TOUCH", "touchpad:", "touchscreen:"),
    "dumpsys-input-sources": ("0x00001002", "0x00100008", "touchscreen:", "touchpad:"),
    "proc-input-vendor": ("04f3", "abcd", "04f3:323c", "abcd:323c"),
    "proc-input-product": ("323c", "abcd", "04f3:323c", "04f3:abcd"),
    "proc-input-bus": ("0018", "0003", "(i2c)", "(usb)"),
    "probe-input-vendor": ("04f3", "abcd", "04f3:323c", "abcd:323c"),
    "probe-input-product": ("323c", "abcd", "04f3:323c", "04f3:abcd"),
    "probe-input-bus": ("0018", "0003", "(i2c)", "(usb)"),
    "probe-input-busname": ("i2c", "usb", "(i2c)", "(usb)"),
    "probe-input-driver": ("elan_i2c", "usbhid", "elan_i2c", "usbhid"),
    "probe-input-kind": ("touchpad", "touchscreen", "touchpad:", "touchscreen:"),
    "dumpsys-input": ("Touchpad", "Trackpad", "input: touchpad: 04f3:323c (i2c) [", "touchpad: unknown"),
    "proc-input": ("Touchpad", "Trackpad", "input: touchpad: 04f3:323c (i2c) [", "touchpad: unknown"),
    "power-success": ("4", "9", "success 4", "success 9"),
    "power-fail": ("1", "8", "fail 1", "fail 8"),
    "suspend-success": ("4", "9", "success 4", "success 9"),
    "suspend-fail": ("1", "8", "fail 1", "fail 8"),
    "dumpsys-power": ("4", "9", "success 4", "success 9"),
    "suspend-sysfs": ("4", "9", "success 4", "success 9"),
    "ls-dri": ("renderD1", "renderD2", "renderD1 [", "renderD2 ["),
    "probe-pci": ("xe", "i915", "xe", "i915"),
    "probe-pci-id": ("8086:46a6", "8086:1234", "8086:46a6", "8086:1234"),
    "probe-pci-class": ("030000", "028000", "8086:46a6", "live linux gpu: unknown "),
    "probe-net": ("wlan0", "wlan1", "wlan0", "wlan1"),
    "probe-net-wireless": ("yes", "no", "wlan0", "live linux wifi: unknown "),
    "drm-card1": ("xe", "i915", "bound (xe)", "bound (i915)"),
    "drm-card2": ("xe", "i915", "bound (xe)", "bound (i915)"),
    "drm-card3": ("xe", "i915", "bound (xe)", "bound (i915)"),
    "probe-pci-unread": ("no", "yes", "agree: DMI, CPU, PCI", "omitted (probe is not the same machine)"),
    "probe-bt": ("hci0", "hci1", "hci0", "hci1"),
    "probe-draft-id": ("kano", "generic-x86", "kano (probe-only)", "generic-x86 (probe-only)"),
    "probe-draft-status": ("probe-only", "verified", "kano (probe-only)", "kano (verified)"),
}
FEEDS = {
    "drm-driver": ["display/gpu", "drm cards"],
    "drm-card1": ["display/gpu", "drm cards"],
    "drm-card2": ["display/gpu", "drm cards"],
    "drm-card3": ["display/gpu", "drm cards"],
    "drm-uevent": ["display/gpu", "drm cards"],
    "drm-pci": ["gpu pci", "drm cards"],
    "drm-sysfs-vendor": ["gpu pci", "drm cards"],
    "drm-sysfs-device": ["gpu pci", "drm cards"],
    "pci-bus": ["compare", "firmware flags", "draft profile", "live linux gpu", "live linux sound", "live linux wifi", "live linux bluetooth", "live linux input"],
    "probe-pci-unread": ["compare", "firmware flags", "draft profile", "live linux gpu", "live linux sound", "live linux wifi", "live linux bluetooth", "live linux input"],
    "dumpsys-power": ["sleep/resume"],
    "suspend-sysfs": ["sleep/resume"],
    "probe-input-vendor": ["live linux input"],
    "probe-input-product": ["live linux input"],
    "probe-input-bus": ["live linux input"],
    "probe-input-driver": ["live linux input"],
    "probe-input-kind": ["live linux input"],
    "probe-input-busname": ["live linux input"],
}
MODE_OF = {"pci-bus": "both", "probe-pci-unread": "both"}
missing_pairs = sorted(set(PRINTED) - set(PAIRS))
if missing_pairs:
    sys.stderr.write("printed leaf has no value pair: %s\n" % missing_pairs)
    raise SystemExit(1)
CANARY = "bad@leaf"
# IGNORED proofs use a token the @ / MAC / IP firewall does not mask.
IGNORED_CANARY = "R4CANARY9Q"
PLANT_LOCK = threading.Lock()
miss = []
official = {}

def mode_of(leaf):
    if leaf in MODE_OF:
        return MODE_OF[leaf]
    if leaf.startswith("probe-") or leaf.startswith("ignored-probe") or leaf == "ignored-draft-rest":
        return "probe"
    return "fixture"

# Fields a planter is allowed to change between A, B, and the canary.
# Setup that is identical across the three runs is not a field of the leaf.
LEAF_FIELDS = {
    "dmi-sys-vendor": ["file:cat-dmi-sys-vendor.txt"],
    "probe-dmi-sys-vendor": ["key:dmi.sys_vendor"],
    "ignored-dmi-product": ["file:cat-dmi-product-name.txt"],
    "dmi-board-vendor": ["file:cat-dmi-board-vendor.txt"],
    "probe-dmi-board-vendor": ["key:dmi.board_vendor"],
    "ignored-dmi-board-name": ["file:cat-dmi-board-name.txt"],
    "dmi-bios-vendor": ["file:cat-dmi-bios-vendor.txt"],
    "probe-dmi-bios-vendor": ["key:dmi.bios_vendor"],
    "ignored-bios-version": ["file:cat-dmi-bios-version.txt"],
    "ignored-bios-date": ["file:cat-dmi-bios-date.txt"],
    "probe-firmware": ["key:firmware.secure_boot"],
    "probe-firmware-uefi": ["key:firmware.uefi"],
    "probe-report": ["file:report.txt"],
    "prop-display-id": ["prop:ro.build.display.id"],
    "prop-build-id": ["prop:ro.build.id"],
    "uname": ["file:uname-a.txt"],
    "proc-version": ["file:cat-proc-version.txt"],
    "drm-driver": ["uevent:cat-drm0-uevent.txt:DRIVER"],
    "egl": ["prop:ro.hardware.egl"],
    "vulkan": ["prop:ro.hardware.vulkan"],
    "drm-pci": ["uevent:cat-drm0-uevent.txt:PCI_ID"],
    "drm-uevent": ["uevent:cat-drm0-uevent.txt:DRIVER", "uevent:cat-drm0-uevent.txt:PCI_ID"],
    "cpuinfo": ["cpuinfo:vendor_id"],
    "probe-cpu": ["key:cpu.vendor_id"],
    "ignored-cpu-model": ["cpuinfo:model name"],
    "ignored-asound": ["file:cat-proc-asound-cards.txt"],
    "ignored-probe-audio": ["key:audio.card_ids.0"],
    "dumpsys-wifi": ["file:dumpsys-wifi.txt"],
    "wlan-uevent": ["uevent:cat-wlan-uevent.txt:DRIVER"],
    "wlan-operstate": ["file:cat-wlan-operstate.txt"],
    "dumpsys-bluetooth": ["file:dumpsys-bluetooth.txt"],
    "bt-uevent": ["uevent:cat-bt-uevent.txt:DRIVER"],
    "dumpsys-input": ["file:dumpsys-input.txt"],
    "proc-input": ["file:cat-proc-bus-input-devices.txt"],
    "ignored-probe-input": ["key:input.0.name"],
    "dumpsys-power": ["file:dumpsys-power.txt"],
    "suspend-sysfs": ["file:cat-suspend-success.txt", "file:cat-suspend-fail.txt"],
    "ls-dri": ["file:ls-dev-dri.txt"],
    "probe-pci": ["key:pci.0.driver"],
    "probe-net": ["key:net.0.name"],
    "probe-bt": ["key:bluetooth.0.name"],
    "drm-sysfs-vendor": ["file:cat-drm-vendor.txt"],
    "drm-sysfs-device": ["file:cat-drm-device.txt"],
    "pci-bus": ["file:cat-proc-bus-pci-devices.txt"],
    "cpuinfo-family": ["cpuinfo:cpu family"],
    "cpuinfo-model": ["cpuinfo:model"],
    "cpuinfo-stepping": ["cpuinfo:stepping"],
    "probe-cpu-family": ["key:cpu.cpu family"],
    "probe-cpu-model": ["key:cpu.model"],
    "probe-cpu-stepping": ["key:cpu.stepping"],
    "wlan-pci": ["uevent:cat-wlan-uevent.txt:PCI_ID"],
    "wlan-usb": ["uevent:cat-wlan-uevent.txt:PRODUCT"],
    "wlan-usb-vid": ["uevent:cat-wlan-uevent.txt:PRODUCT"],
    "wlan-usb-pid": ["uevent:cat-wlan-uevent.txt:PRODUCT"],
    "wlan-usb-bcd": ["uevent:cat-wlan-uevent.txt:PRODUCT"],
    "bt-pci": ["uevent:cat-bt-uevent.txt:PCI_ID"],
    "bt-usb": ["uevent:cat-bt-uevent.txt:PRODUCT"],
    "bt-usb-vid": ["uevent:cat-bt-uevent.txt:PRODUCT"],
    "bt-usb-pid": ["uevent:cat-bt-uevent.txt:PRODUCT"],
    "bt-usb-bcd": ["uevent:cat-bt-uevent.txt:PRODUCT"],
    "wifi-curstate": ["file:dumpsys-wifi.txt"],
    "wifi-supplicant": ["file:dumpsys-wifi.txt"],
    "bt-enabled": ["file:dumpsys-bluetooth.txt"],
    "bt-radio-state": ["file:dumpsys-bluetooth.txt"],
    "dumpsys-input-id": ["file:dumpsys-input.txt"],
    "dumpsys-input-bus": ["file:dumpsys-input.txt"],
    "dumpsys-input-product": ["file:dumpsys-input.txt"],
    "dumpsys-input-classes": ["file:dumpsys-input.txt"],
    "dumpsys-input-sources": ["file:dumpsys-input.txt"],
    "probe-input-kind": ["key:input.0.kind"],
    "probe-input-busname": ["key:input.0.busname"],
    "power-success": ["file:dumpsys-power.txt"],
    "power-fail": ["file:dumpsys-power.txt"],
    "suspend-success": ["file:cat-suspend-success.txt"],
    "suspend-fail": ["file:cat-suspend-fail.txt"],
    "proc-input-vendor": ["file:cat-proc-bus-input-devices.txt"],
    "proc-input-product": ["file:cat-proc-bus-input-devices.txt"],
    "proc-input-bus": ["file:cat-proc-bus-input-devices.txt"],
    "probe-input-vendor": ["key:input.0.vendor"],
    "probe-input-product": ["key:input.0.product"],
    "probe-input-bus": ["key:input.0.bus"],
    "probe-input-driver": ["key:input.0.driver"],
    "probe-pci-id": ["key:pci.0.id"],
    "probe-draft-id": ["draft:id"],
    "probe-draft-status": ["draft:status"],
    "ls-bluetooth": ["file:ls-bluetooth.txt"],
    "prop-bt-service": ["prop:init.svc.bluetooth"],
    "probe-net-wireless": ["key:net.0.wireless"],
    "drm-card1": ["uevent:cat-drm1-uevent.txt:DRIVER"],
    "drm-card2": ["uevent:cat-drm2-uevent.txt:DRIVER"],
    "drm-card3": ["uevent:cat-drm3-uevent.txt:DRIVER"],
    "ignored-probe-net-address": ["key:net.0.address"],
    "probe-pci-class": ["key:pci.0.class"],
    "probe-pci-unread": ["key:pci.0.unread"],
    "ignored-logcat": ["file:logcat.txt"],
    "ignored-dumpsys-display": ["file:dumpsys-display.txt"],
    "ignored-dumpsys-audio": ["file:dumpsys-audio.txt"],
    "ignored-dumpsys-battery": ["file:dumpsys-battery.txt"],
    "ignored-dmi-product-version": ["file:cat-dmi-product-version.txt"],
    "ignored-getprop-rest": [
        "prop:ro.serialno",
        "prop:ro.build.version.incremental", "prop:ro.product.model", "prop:ro.product.device",
        "prop:ro.product.name", "prop:ro.product.manufacturer", "prop:ro.hardware",
        "prop:wifi.interface", "prop:ro.boot.serialno", "prop:ro.boot.android_id", "prop:net.hostname",
        "prop:gsm.imei", "prop:dhcp.wlan0.hostname", "prop:dhcp.wlan0.ipaddress", "prop:gsm.serial",
        "prop:init.svc.bluetooth_hal", "prop:init.svc.vendor.bluetooth-1-0",
        "prop:persist.radio.meid", "prop:persist.radio.serialno", "prop:persist.sys.android_id",
        "prop:ril.serialnumber", "prop:ro.boot.btmacaddr", "prop:ro.boot.wifimacaddr",
        "prop:ro.build.host", "prop:ro.build.user", "prop:ro.product.board", "prop:vendor.usb.serialno",
    ],
    "ignored-probe-dmi-rest": [
        "key:dmi.product_name", "key:dmi.board_name", "key:dmi.bios_version",
        "key:dmi.bios_date", "key:dmi.product_version",
    ],
    "ignored-probe-cpu-model": ["key:cpu.model name"],
    "ignored-probe-bt-device": ["key:bluetooth.0.device"],
    "ignored-probe-pci-slot": ["key:pci.0.slot"],
    "ignored-draft-rest": ["draft:schema", "draft:name"],
    "ignored-probe-coverage": ["extra:coverage.txt"],
}
if set(LEAF_FIELDS) != set(leaves):
    sys.stderr.write("leaf fields %s\n" % sorted(set(leaves) ^ set(LEAF_FIELDS)))
    raise SystemExit(1)
BOOL_VALUE = {
    "probe-firmware-uefi": (True, False),
    "probe-net-wireless": (True, False),
    "probe-pci-unread": (False, True),
}
# A rejected canary may keep a sibling fact. These rows must still drop the canary and the A mark.
CANARY_KEEP = {
    "probe-firmware": "UEFI yes",
    "probe-firmware-uefi": "Secure Boot off",
    "egl": "gbos_efifb",
    "vulkan": "gbos_efifb",
}
# The planted token is the whole fact. A rejected canary has no sibling value to keep.
MUST_FOLD = {
    "dmi-sys-vendor", "probe-dmi-sys-vendor",
    "dmi-board-vendor", "probe-dmi-board-vendor",
    "dmi-bios-vendor", "probe-dmi-bios-vendor",
    "prop-build-id", "proc-version", "probe-report",
    "probe-draft-id", "probe-draft-status", "drm-uevent",
    "wlan-operstate", "wifi-curstate", "wifi-supplicant",
    "bt-enabled", "bt-radio-state", "ls-bluetooth", "prop-bt-service",
    "dumpsys-power", "suspend-sysfs",
    "power-success", "power-fail", "suspend-success", "suspend-fail",
    "probe-net-wireless", "probe-pci-class",
}
# A non-boolean unread value is not unread, so the canary keeps A's compare row.
CANARY_HOLDS = {"probe-pci-unread"}

def prepare(leaf, token):
    with PLANT_LOCK:
        files = base_files()
        data = copy.deepcopy(BASE_PROBE)
        draft, report_line = plant(leaf, token, files, data)
        extra = dict(PROBE_EXTRA)
        rc = dict(RC_OVERRIDE)
    return files, data, draft, report_line, extra, rc

def scalar_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value)

def input_fields(files, data, draft, report_line, extra, rc):
    snap = {}
    for name, content in files.items():
        if name == "getprop.txt":
            for match in re.finditer(r"(?m)^\[([^\]]+)\]: \[(.*)\]\s*$", content):
                snap["prop:" + match.group(1)] = match.group(2)
        elif name == "cat-proc-cpuinfo.txt":
            for line in content.splitlines():
                if ": " in line:
                    key, val = line.split(": ", 1)
                    snap["cpuinfo:" + key] = val
        elif name.endswith("-uevent.txt"):
            for line in content.splitlines():
                if "=" in line:
                    key, val = line.split("=", 1)
                    snap["uevent:%s:%s" % (name, key)] = val
        else:
            snap["file:" + name] = content

    def walk(obj, prefix):
        if isinstance(obj, dict):
            for key, val in obj.items():
                kid = prefix + "." + str(key) if prefix else str(key)
                walk(val, kid)
            return
        if isinstance(obj, list):
            for i, item in enumerate(obj):
                kid = "%s.%d" % (prefix, i) if prefix else str(i)
                walk(item, kid)
            return
        if prefix:
            snap["key:" + prefix] = scalar_text(obj)

    walk(data, "")
    for line in (draft or "").splitlines():
        match = re.match(r'^([A-Za-z0-9_.]+) = "?([^"]*)"?\s*$', line)
        if match:
            snap["draft:" + match.group(1)] = match.group(2)
    snap["file:report.txt"] = report_line or ""
    for name, body in extra.items():
        snap["extra:" + name] = body
    for ident, body in rc.items():
        snap["rc:" + ident] = body
    return snap

def changed_keys(snaps):
    keys = set()
    for snap in snaps:
        keys.update(snap)
    changed = []
    for key in sorted(keys):
        vals = [snap.get(key, None) for snap in snaps]
        if any(val != vals[0] for val in vals[1:]):
            changed.append(key)
    return changed

def isolation_misses(leaf, snaps, canary_token):
    owned = LEAF_FIELDS[leaf]
    changed = changed_keys(snaps)
    local = []
    extra = [key for key in changed if key not in owned]
    if extra:
        local.append("%s isolation extra %s" % (leaf, extra))
    if not any(key in changed for key in owned):
        local.append("%s isolation owned field did not change" % leaf)
    canary_snap = snaps[-1]
    for key in owned:
        blob = canary_snap.get(key, "")
        if str(canary_token) not in blob:
            local.append("%s canary missing from %s" % (leaf, key))
    return local

FIXED_FIELDS = {
    "drm-card1": {"file:cat-drm1-vendor.txt": "0x8086\n", "file:cat-drm1-device.txt": "0x46a6\n"},
    "drm-card2": {"file:cat-drm2-vendor.txt": "0x8086\n", "file:cat-drm2-device.txt": "0x46a6\n"},
    "drm-card3": {"file:cat-drm3-vendor.txt": "0x8086\n", "file:cat-drm3-device.txt": "0x46a6\n"},
}
SECTION_SOURCES = {"draft:model"}
FIELD_ALIAS = {"file:coverage.txt": "extra:coverage.txt"}

def norm_field(key):
    return re.sub(r"\.\d+(?=\.|$)", "", key)

def part_prefix(src):
    if src == "file:cat-proc-cpuinfo.txt":
        return "cpuinfo:"
    if src.startswith("file:") and src.endswith("-uevent.txt"):
        return "uevent:" + src[len("file:"):] + ":"
    return ""

def is_parent_source(src, cover):
    dot = src + "."
    return any(other.startswith(dot) for other in cover)

def owned_keys_for(src, leaf):
    owned = LEAF_FIELDS[leaf]
    prefix = part_prefix(src)
    if prefix:
        hit = [key for key in owned if key.startswith(prefix)]
        if hit:
            return hit
    want = norm_field(FIELD_ALIAS.get(src, src))
    hit = [key for key in owned if norm_field(key) == want]
    if hit:
        return hit
    if src == "file:getprop.txt":
        hit = [key for key in owned if key.startswith("prop:")]
        if hit:
            return hit
    if src == "file:profile.draft.toml":
        hit = [key for key in owned if key.startswith("draft:")]
        if hit:
            return hit
    return []

def cover_plant_gaps(cover):
    gaps = []
    cache = {}

    def snaps_of(leaf):
        if leaf not in cache:
            got = []
            for token in ("alpha-leaf", "beta-leaf", "R4CANARY9Q"):
                files, data, draft, report_line, extra, rc = prepare(leaf, token)
                got.append(input_fields(files, data, draft, report_line, extra, rc))
            cache[leaf] = got
        return cache[leaf]

    for src, leaf in sorted(cover.items()):
        if src in SECTION_SOURCES or is_parent_source(src, cover):
            continue
        if leaf not in LEAF_FIELDS:
            gaps.append("%s -> missing leaf %s" % (src, leaf))
            continue
        snaps = snaps_of(leaf)
        fixed = FIXED_FIELDS.get(leaf, {}).get(src)
        if fixed is not None:
            if any(snap.get(src) != fixed for snap in snaps):
                gaps.append("%s fixed bytes mismatch on %s" % (src, leaf))
            continue
        keys = owned_keys_for(src, leaf)
        if not keys:
            gaps.append("%s not planted by %s" % (src, leaf))
            continue
        for key in keys:
            vals = [snap.get(key) for snap in snaps]
            if len(set(vals)) < 2:
                gaps.append("%s field %s did not vary on %s" % (src, key, leaf))
            blob = "" if vals[-1] is None else str(vals[-1])
            if "R4CANARY9Q" not in blob:
                gaps.append("%s canary missing from %s on %s" % (src, key, leaf))
    return gaps

cover_gaps = cover_plant_gaps(COVER)
if cover_gaps:
    sys.stderr.write("cover vs planter: %s\n" % " | ".join(cover_gaps[:12]))
    raise SystemExit(1)
bad_cover_map = dict(COVER)
bad_cover_map["key:net.name"] = "ignored-probe-net-address"
bad_cover_gaps = cover_plant_gaps(bad_cover_map)
if not any(item.startswith("key:net.name ") for item in bad_cover_gaps):
    sys.stderr.write("cover cross-check missed key:net.name: %s\n" % bad_cover_gaps[:8])
    raise SystemExit(1)
sib = []
for token in ("6", "15", CANARY):
    files, data, draft, report_line, extra, rc = prepare("cpuinfo-family", token)
    files["cat-proc-cpuinfo.txt"] = files["cat-proc-cpuinfo.txt"].replace("stepping: 3", "stepping: " + str(token), 1)
    sib.append(input_fields(files, data, draft, report_line, extra, rc))
sib_miss = isolation_misses("cpuinfo-family", sib, CANARY)
if not any("isolation extra" in item for item in sib_miss):
    sys.stderr.write("cpuinfo sibling field was not isolated: %s\n" % sib_miss)
    raise SystemExit(1)
ue_sib = []
for token in ("xe", "i915", CANARY):
    files, data, draft, report_line, extra, rc = prepare("drm-driver", token)
    files["cat-drm0-uevent.txt"] = "DRIVER=%s\nPCI_ID=%s\n" % (token, token)
    ue_sib.append(input_fields(files, data, draft, report_line, extra, rc))
ue_miss = isolation_misses("drm-driver", ue_sib, CANARY)
if not any("PCI_ID" in item for item in ue_miss):
    sys.stderr.write("uevent sibling field was not isolated: %s\n" % ue_miss)
    raise SystemExit(1)

def run_case(tag, files, data, draft, report_line, extra, rc, mode, script_path=None):
    job = root / "bh" / tag
    if job.exists():
        shutil.rmtree(job)
    fix = job / "fix"
    probe = job / "probe"
    out = job / "out"
    fix.mkdir(parents=True)
    probe.mkdir(parents=True)
    rc_map = rc or {}
    for name, content in files.items():
        (fix / name).write_text(content, encoding="utf-8", newline="\n")
        if name.endswith(".txt"):
            ident = name[:-4]
            (fix / (ident + ".rc")).write_text(rc_map.get(ident, "0\n"), encoding="utf-8", newline="\n")
            (fix / (ident + ".err")).write_text("", encoding="utf-8", newline="\n")
    (probe / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
    (probe / "profile.draft.toml").write_text(draft, encoding="utf-8", newline="\n")
    (probe / "report.txt").write_text(report_line, encoding="utf-8", newline="\n")
    for extra_name, extra_body in extra.items():
        (probe / extra_name).write_text(extra_body, encoding="utf-8", newline="\n")
    cmd = ["bash", script_path or script, "--out", str(out), "--date", "20261014"]
    if mode == "probe":
        cmd[2:2] = ["--probe", str(probe)]
    elif mode == "both":
        cmd[2:2] = ["--fixture", str(fix), "--probe", str(probe)]
    else:
        cmd[2:2] = ["--fixture", str(fix)]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return "", "", proc.stderr[-300:]
    txt_path = out / "gbos-report-20261014.txt"
    md_path = out / "summary.md"
    if not txt_path.is_file() or not md_path.is_file():
        return "", "", "no report"
    return txt_path.read_text(encoding="utf-8"), md_path.read_text(encoding="utf-8"), ""

def txt_rows(blob):
    found = {}
    for line in blob.splitlines():
        match = re.match(r"^([^:]+): (.*) \[[^\]]*\]$", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found

def changed_fields(left, right):
    keys = set(txt_rows(left)) | set(txt_rows(right))
    return {key for key in keys if txt_rows(left).get(key) != txt_rows(right).get(key)}

def feed_text(blob, fields):
    lines = []
    for line in blob.splitlines():
        if any(line.startswith(field + ":") for field in fields):
            lines.append(line)
        elif any(line.startswith("| " + zh_of.get(field, "\x00") + " |") for field in fields):
            lines.append(line)
    return "\n".join(lines)

def strip_feed(blob, fields):
    prefixes = []
    for field in fields:
        prefixes.append(field + ":")
        zh = zh_of.get(field, "")
        if zh:
            prefixes.append("| " + zh + " |")
    kept = []
    for line in blob.splitlines(True):
        if any(line.startswith(prefix) for prefix in prefixes):
            continue
        kept.append(line)
    return "".join(kept)

def pair_tokens(leaf):
    if leaf in BOOL_VALUE:
        return BOOL_VALUE[leaf]
    if leaf in PAIRS:
        return PAIRS[leaf][0], PAIRS[leaf][1]
    if leaf in ("wlan-usb-bcd", "bt-usb-bcd"):
        return "0001", "0002"
    return "alpha-leaf", "beta-leaf"

jobs = []
for leaf in PRINTED:
    token_a, token_b = pair_tokens(leaf)
    mode = mode_of(leaf)
    for label, token in (("a", token_a), ("b", token_b), ("c", CANARY)):
        files, data, draft, report_line, extra, rc = prepare(leaf, token)
        jobs.append((leaf, label, files, data, draft, report_line, extra, rc, mode))
for leaf in sorted(IGNORED):
    token_a, token_b = pair_tokens(leaf)
    mode = mode_of(leaf)
    for label, token in (("a", token_a), ("b", token_b), ("c", IGNORED_CANARY)):
        files, data, draft, report_line, extra, rc = prepare(leaf, token)
        jobs.append((leaf, label, files, data, draft, report_line, extra, rc, mode))
base_fix = base_files()
base_data = copy.deepcopy(BASE_PROBE)
base_jobs = []
for mode in ("fixture", "probe", "both"):
    base_jobs.append(("baseline", mode, base_fix, base_data, DRAFT, REPORT, {}, {}, mode))

def exec_job(item):
    kind = item[0]
    if kind == "baseline":
        _k, mode, files, data, draft, report_line, extra, rc, _m = item
        txt, md, err = run_case("baseline-" + mode, files, data, draft, report_line, extra, rc, mode)
        return ("baseline", mode, txt, md, err)
    leaf, label, files, data, draft, report_line, extra, rc, mode = item
    tag = "%s-%s" % (leaf.replace("/", "-"), label)
    txt, md, err = run_case(tag, files, data, draft, report_line, extra, rc, mode)
    snap = input_fields(files, data, draft, report_line, extra, rc)
    return (leaf, label, txt, md, err, snap)

results = {}
baselines = {}
with ThreadPoolExecutor(max_workers=4) as pool:
    for item in pool.map(exec_job, base_jobs + jobs):
        if item[0] == "baseline":
            baselines[item[1]] = item
        else:
            results[(item[0], item[1])] = item

def baseline_for(mode):
    return baselines[mode]

def row_of(blob, field):
    for line in blob.splitlines():
        if line.startswith(field + ":"):
            return line
    return ""

def judge_printed(leaf, got_a, got_b, got_c, canary_token):
    local = []
    _ta, _tb, mark_a, mark_b = PAIRS[leaf]
    feeds = FEEDS.get(leaf, [field_of[leaf]])
    if got_a[4] or got_b[4] or got_c[4]:
        local.append("%s run: %s" % (leaf, got_a[4] or got_b[4] or got_c[4]))
        return local
    txt_a, md_a = got_a[2], got_a[3]
    txt_b, md_b = got_b[2], got_b[3]
    txt_c, md_c = got_c[2], got_c[3]
    local.extend(isolation_misses(leaf, [got_a[5], got_b[5], got_c[5]], canary_token))
    changed = changed_fields(txt_a, txt_b)
    if field_of[leaf] not in changed and not (set(feeds) & changed):
        local.append("%s values did not change a fed row (%s)" % (leaf, sorted(changed)))
    extra = changed - set(feeds)
    if extra:
        local.append("%s changed extra rows %s" % (leaf, sorted(extra)))
    if not changed:
        local.append("%s valid values printed the same report" % leaf)
    if strip_feed(txt_a, feeds) != strip_feed(txt_b, feeds) or strip_feed(md_a, feeds) != strip_feed(md_b, feeds):
        local.append("%s differed outside its row" % leaf)
    for field in changed:
        zh = zh_of.get(field, "")
        prefix = "| " + zh + " |"
        row_md_a = next((line for line in md_a.splitlines() if line.startswith(prefix)), "")
        row_md_b = next((line for line in md_b.splitlines() if line.startswith(prefix)), "")
        if row_md_a == row_md_b:
            local.append("%s markdown %s unchanged" % (leaf, field))
    fed_a = feed_text(txt_a, feeds)
    fed_b = feed_text(txt_b, feeds)
    fed_c = feed_text(txt_c, feeds)
    if mark_a not in fed_a or mark_b not in fed_b or mark_a in fed_b or mark_b in fed_a:
        local.append("%s marks %s || %s" % (leaf, fed_a[:220], fed_b[:220]))
    if canary_token in txt_c or canary_token in md_c:
        local.append("%s canary leaked" % leaf)
    if leaf in CANARY_HOLDS:
        if fed_c != fed_a or feed_text(md_c, feeds) != feed_text(md_a, feeds):
            local.append("%s canary moved a held row: %s" % (leaf, fed_c[:220]))
    elif mark_a in fed_c:
        local.append("%s canary kept the valid token" % leaf)
    row_c = row_of(txt_c, field_of[leaf])
    base_row = row_of(baseline_for(mode_of(leaf))[2], field_of[leaf])
    if leaf == "ls-dri":
        if row_c != base_row or canary_token in row_c:
            local.append("%s dri nodes changed: %s" % (leaf, row_c))
    elif leaf == "prop-display-id":
        if "16452207" not in row_c or "CL3B" in row_c or canary_token in row_c:
            local.append("%s display fallback: %s" % (leaf, row_c))
    elif leaf == "uname":
        if canary_token in row_c or PINNED in row_c or ("unknown" not in row_c and "other" not in row_c):
            local.append("%s kernel canary: %s" % (leaf, row_c))
    elif leaf == "pci-bus":
        compare = row_of(txt_c, "compare")
        if "omitted" not in compare or canary_token in compare:
            local.append("%s compare canary: %s" % (leaf, compare))
    elif leaf in CANARY_KEEP:
        if CANARY_KEEP[leaf] not in row_c or canary_token in row_c:
            local.append("%s canary row: %s" % (leaf, row_c))
    elif leaf in MUST_FOLD:
        if "unknown" not in row_c and "omitted" not in row_c:
            local.append("%s canary row not unknown: %s" % (leaf, row_c))
    return local

def ignored_report_misses(leaf, got_a, got_b, got_c, canary_token, base_txt, base_md, base_err):
    local = []
    if got_a[4] or got_b[4] or got_c[4] or base_err:
        local.append("%s ignored run: %s" % (leaf, got_a[4] or got_b[4] or got_c[4] or base_err))
        return local
    local.extend(isolation_misses(leaf, [got_a[5], got_b[5], got_c[5]], canary_token))
    if leaf in ("wlan-usb-bcd", "bt-usb-bcd"):
        if got_c[2] != got_a[2] or got_c[3] != got_a[3] or got_a[2] != got_b[2] or got_a[3] != got_b[3]:
            local.append("%s bcd changed the report" % leaf)
        if "1234:5678" not in got_a[2] or "1234:5678" not in got_c[2]:
            local.append("%s bcd hid vid:pid" % leaf)
    else:
        for label, got in (("a", got_a), ("b", got_b), ("c", got_c)):
            if got[2] != base_txt or got[3] != base_md:
                local.append("%s ignored %s output changed" % (leaf, label))
    if canary_token in (got_c[2] + got_c[3]):
        local.append("%s ignored canary leaked" % leaf)
    return local

for leaf in sorted(IGNORED):
    mode = mode_of(leaf)
    _b, _m, base_txt, base_md, base_err = baseline_for(mode)
    miss.extend(ignored_report_misses(
        leaf, results[(leaf, "a")], results[(leaf, "b")], results[(leaf, "c")],
        IGNORED_CANARY, base_txt, base_md, base_err))

for leaf in PRINTED:
    miss.extend(judge_printed(leaf, results[(leaf, "a")], results[(leaf, "b")], results[(leaf, "c")], CANARY))

def mutation_case(name, token):
    files = base_files()
    data = copy.deepcopy(BASE_PROBE)
    draft, report_line = DRAFT, REPORT
    extra = {}
    if name == "p-display":
        text_prop = files["getprop.txt"]
        if not text_prop.endswith("\n"):
            text_prop += "\n"
        files["getprop.txt"] = text_prop + "[ro.vendor.unused]: [%s]\n" % token
    elif name == "p-probecpu":
        data["cpu"]["unused_key"] = token
    elif name == "p-probedmi":
        data["dmi"]["unused_vendor"] = token
    elif name == "p-cpuinfo":
        files["cat-proc-cpuinfo.txt"] = files["cat-proc-cpuinfo.txt"] + "flags: %s\n" % token
    elif name == "m-vendor-sibling":
        data["dmi"]["board_vendor"] = token
    elif name == "m-cpufam-cross":
        files["cat-proc-cpuinfo.txt"] = files["cat-proc-cpuinfo.txt"].replace("cpu family: 6", "cpu family: " + token, 1)
    elif name == "m-kernel-cross":
        files["uname-a.txt"] = "Linux synth-laptop %s #1\n" % token
    elif name == "m-driver-cross":
        files["cat-wlan-uevent.txt"] = "DRIVER=%s\n" % token
    elif name == "m-pci-cross":
        files["cat-drm0-uevent.txt"] = "DRIVER=xe\nPCI_ID=%s\n" % token
    elif name == "m-wifi-ssid-orig":
        files["dumpsys-wifi.txt"] = (
            "Wi-Fi is enabled\ncurState=ConnectedState\n"
            'mWifiInfo SSID: "%s", Supplicant state: COMPLETED\n' % token)
    elif name == "m-wifi-history":
        files["dumpsys-wifi.txt"] = "Wi-Fi is enabled\ncurState=ConnectedState\nHistory:\ncurState=%s\n" % token
    elif name == "m-bt-getprop":
        text_prop = files["getprop.txt"]
        if not text_prop.endswith("\n"):
            text_prop += "\n"
        files["getprop.txt"] = text_prop + "[ro.vendor.unused]: [%s]\n" % token
    elif name == "m-bt-outside":
        files["dumpsys-bluetooth.txt"] = "Unused: %s\nBluetooth Status\n  enabled: true\n  state: ON\n" % token
    elif name == "m-input-otherline":
        files["dumpsys-input.txt"] = "1: Touchpad\n  Classes: TOUCHPAD\n  Location: %s\n" % token
        files["cat-proc-bus-input-devices.txt"] = ""
    elif name == "m-input-preheader":
        files["dumpsys-input.txt"] = "%s\n1: Touchpad\n  Classes: TOUCHPAD\n" % token
    elif name == "m-proc-phys":
        files["cat-proc-bus-input-devices.txt"] = (
            'I: Bus=0018 Vendor=04f3 Product=323c Version=0000\nN: Name="Touchpad"\nP: Phys=%s\n' % token)
    elif name == "m-report-otherline":
        data["firmware"] = {}
        report_line = "cpu: %s\nfirmware: UEFI yes, Secure Boot off\n" % token
    elif name == "m-firmware-report":
        report_line = "firmware: %s\n" % token
    elif name == "m-input-descriptor":
        files["dumpsys-input.txt"] = "1: Touchpad\n  Classes: TOUCHPAD\n  Descriptor: %s\n" % token
        files["cat-proc-bus-input-devices.txt"] = ""
    elif name == "m-proc-unused":
        files["cat-proc-bus-input-devices.txt"] = (
            'I: Bus=0018 Vendor=04f3 Product=323c Version=0000\nN: Name="Touchpad"\nS: Unused=%s\n' % token)
    elif name == "m-cpu-sibling":
        files["cat-proc-cpuinfo.txt"] = files["cat-proc-cpuinfo.txt"].replace(
            "\nmodel: 154\n", "\nmodel: " + token + "\n", 1)
    elif name == "m-input-name-id":
        files["dumpsys-input.txt"] = "1: %s\n  Classes: TOUCHPAD\n" % token
        files["cat-proc-bus-input-devices.txt"] = ""
    else:
        raise SystemExit("unknown mutation " + name)
    return files, data, draft, report_line, extra

MUTATIONS = (
    ("p-display", "prop-display-id"),
    ("p-probecpu", "probe-cpu"),
    ("p-probedmi", "probe-dmi-sys-vendor"),
    ("p-cpuinfo", "cpuinfo"),
    ("m-vendor-sibling", "probe-dmi-sys-vendor"),
    ("m-cpufam-cross", "probe-cpu-family"),
    ("m-kernel-cross", "proc-version"),
    ("m-driver-cross", "bt-uevent"),
    ("m-pci-cross", "probe-pci-id"),
    ("m-wifi-ssid-orig", "wifi-curstate"),
    ("m-wifi-history", "wifi-curstate"),
    ("m-bt-getprop", "bt-enabled"),
    ("m-bt-outside", "bt-enabled"),
    ("m-input-otherline", "dumpsys-input-id"),
    ("m-input-preheader", "dumpsys-input"),
    ("m-proc-phys", "proc-input"),
    ("m-report-otherline", "probe-report"),
    ("m-firmware-report", "probe-firmware"),
    ("m-input-descriptor", "dumpsys-input-id"),
    ("m-proc-unused", "proc-input-vendor"),
    ("m-cpu-sibling", "cpuinfo-family"),
    ("m-input-name-id", "dumpsys-input-id"),
)

MUT_MODE = {
    "m-cpufam-cross": "both",
    "m-kernel-cross": "both",
    "m-pci-cross": "both",
}

def exec_mut(item):
    name, leaf, label, token, mode = item
    files, data, draft, report_line, extra = mutation_case(name, token)
    txt, md, err = run_case(
        "mut-%s-%s" % (name, label), files, data, draft, report_line, extra, {}, mode)
    snap = input_fields(files, data, draft, report_line, extra, {})
    return name, leaf, label, txt, md, err, snap

mut_items = []
for name, leaf in MUTATIONS:
    mode = MUT_MODE.get(name, mode_of(leaf) if leaf in PRINTED or leaf in IGNORED else "fixture")
    token_a, token_b = pair_tokens(leaf)
    for label, token in (("a", token_a), ("b", token_b), ("c", CANARY)):
        mut_items.append((name, leaf, label, token, mode))

with ThreadPoolExecutor(max_workers=4) as pool:
    mut_results = list(pool.map(exec_mut, mut_items))

grouped = {}
for name, leaf, label, txt, md, err, snap in mut_results:
    grouped.setdefault(name, {})[label] = (leaf, label, txt, md, err, snap)

for name, leaf in MUTATIONS:
    got = grouped[name]

    def pack(label, got=got):
        return got[label]

    if leaf in PRINTED:
        found = judge_printed(leaf, pack("a"), pack("b"), pack("c"), CANARY)
    else:
        ga, gb, gc = pack("a"), pack("b"), pack("c")
        _b, _m, base_txt, base_md, base_err = baseline_for(mode_of(leaf))
        found = ignored_report_misses(leaf, ga, gb, gc, CANARY, base_txt, base_md, base_err)
    code = 1 if found else 0
    print("planter mutation %s: precheck exit %d" % (name, code))
    sys.stderr.write("planter mutation %s: precheck exit %d\n" % (name, code))
    if code == 0:
        miss.append("mutation %s was not caught" % name)

RAW = "R4SERIAL9F3A"
product_src = pathlib.Path(script).read_text(encoding="utf-8")
PRODUCT_PATCH = (
    ("uefi-raw", "probe-firmware-uefi",
     "        if isinstance(uefi, bool):\n            parts.append(\"UEFI yes\" if uefi else \"UEFI no\")\n",
     "        if isinstance(uefi, str):\n            parts.append(\"UEFI \" + uefi)\n        elif isinstance(uefi, bool):\n            parts.append(\"UEFI yes\" if uefi else \"UEFI no\")\n"),
    ("wireless-raw", "probe-net-wireless",
     "        if n.get(\"wireless\") is True:\n            name = valid_iface(json_text(n.get(\"name\")), banned)\n",
     "        if n.get(\"wireless\") is True or isinstance(n.get(\"wireless\"), str):\n            name = n.get(\"wireless\") if isinstance(n.get(\"wireless\"), str) else valid_iface(json_text(n.get(\"name\")), banned)\n"),
    ("wlan-bcd-raw", "wlan-usb-bcd",
     "    return \"usb \" + vid + \":\" + pid\n",
     "    return \"usb \" + vid + \":\" + pid + \" bcd \" + m.group(3)\n"),
    ("bt-bcd-raw", "bt-usb-bcd",
     "    return \"usb \" + vid + \":\" + pid\n",
     "    return \"usb \" + vid + \":\" + pid + \" bcd \" + m.group(3)\n"),
)
for tag, leaf, old, new in PRODUCT_PATCH:
    if old not in product_src:
        print("product mutation %s: precheck exit 2" % tag)
        sys.stderr.write("product mutation %s: patch missing\n" % tag)
        miss.append("product patch missing " + tag)
        continue
    path = root / ("mutprod-%s.sh" % tag)
    path.write_text(product_src.replace(old, new, 1), encoding="utf-8", newline="\n")
    gots = {}
    token_a, token_b = pair_tokens(leaf)
    for label, token in (("a", token_a), ("b", token_b), ("c", RAW)):
        files, data, draft, report_line, extra, rc = prepare(leaf, token)
        txt, md, err = run_case(
            "prod-%s-%s" % (tag, label), files, data, draft, report_line, extra, rc,
            mode_of(leaf), script_path=str(path))
        gots[label] = (leaf, label, txt, md, err, input_fields(files, data, draft, report_line, extra, rc))
    if leaf in PRINTED:
        found = judge_printed(leaf, gots["a"], gots["b"], gots["c"], RAW)
    else:
        found = []
        ga, gb, gc = gots["a"], gots["b"], gots["c"]
        if ga[4] or gb[4] or gc[4]:
            found.append("run")
        else:
            found.extend(isolation_misses(leaf, [ga[5], gb[5], gc[5]], RAW))
            if RAW in (gc[2] + gc[3]) or gc[2] != ga[2] or gc[3] != ga[3]:
                found.append("printed raw bcd")
    code = 1 if found else 0
    print("product mutation %s: precheck exit %d" % (tag, code))
    sys.stderr.write("product mutation %s: precheck exit %d\n" % (tag, code))
    if code == 0:
        miss.append("product mutation %s was not caught" % tag)

def priority_edit(tag, edit, field, present, absent=(), rc=None):
    files = base_files()
    edit(files)
    txt, _md, err = run_case(
        "prio-" + tag, files, copy.deepcopy(BASE_PROBE), DRAFT, REPORT, {}, rc or {}, "fixture")
    row = row_of(txt, field)
    if err or any(part not in row for part in present) or any(part in row for part in absent):
        miss.append("%s priority %s row %s" % (tag, err, row))

priority_edit(
    "uname-wins",
    lambda files: files.__setitem__("uname-a.txt", "Linux synth-laptop %s #1 SMP\n" % PINNED),
    "kernel", (PINNED,), ("6.1.75-android14",))
priority_edit(
    "proc-wins",
    lambda files: (
        files.__setitem__("uname-a.txt", "Linux synth-laptop\n"),
        files.__setitem__("cat-proc-version.txt", "Linux version %s (builder@buildhost) #1\n" % PINNED)),
    "kernel", (PINNED,), ())
priority_edit(
    "display-wins",
    lambda files: files.__setitem__("getprop.txt", set_prop(
        set_prop(files["getprop.txt"], "ro.build.display.id", "CL3B.260622.271.R1"),
        "ro.build.id", "16452207")),
    "build", ("CL3B.260622.271.R1",), ())
priority_edit(
    "build-fallback",
    lambda files: files.__setitem__("getprop.txt", set_prop(files["getprop.txt"], "ro.build.display.id", "")),
    "build", ("16452207",), ("CL3B",))
priority_edit(
    "power-disagree",
    lambda files: (
        files.__setitem__("dumpsys-power.txt", "Suspend Success Count: 4\nSuspend Failure Count: 1\n"),
        files.__setitem__("cat-suspend-success.txt", "9\n"),
        files.__setitem__("cat-suspend-fail.txt", "1\n")),
    "sleep/resume", ("unknown",), ("success 4", "success 9"))
priority_edit(
    "dumpsys-wins",
    lambda files: (
        files.__setitem__("dumpsys-power.txt", "Suspend Success Count: 4\nSuspend Failure Count: 1\n"),
        files.__setitem__("cat-suspend-success.txt", "9\n"),
        files.__setitem__("cat-suspend-fail.txt", "1\n")),
    "sleep/resume", ("success 4",), ("success 9", "unknown"),
    {"cat-suspend-success": "1\n", "cat-suspend-fail": "1\n"})
priority_edit(
    "sysfs-wins",
    lambda files: (
        files.__setitem__("dumpsys-power.txt", "no counts\n"),
        files.__setitem__("cat-suspend-success.txt", "9\n"),
        files.__setitem__("cat-suspend-fail.txt", "1\n")),
    "sleep/resume", ("success 9",), ("unknown",))
if miss:
    sys.stderr.write("behaviour gaps (%d): %s\n" % (len(miss), " | ".join(miss[:40])))
    raise SystemExit(1)
print("behaviour leaves printed %d ignored %d" % (len(PRINTED), len(IGNORED)))

n = 0
for leaf in leaves:
    for token, frags in classes:
        modes = ("probe", "both") if leaf.startswith("probe-") or leaf.startswith("ignored-probe-") else ("fixture", "both")
        for mode in modes:
            n += 1
            job = root / ("j%04d" % n)
            fix = job / "fix"
            probe = job / "probe"
            fix.mkdir(parents=True)
            probe.mkdir(parents=True)
            files = base_files()
            data = copy.deepcopy(BASE_PROBE)
            draft, report_line = plant(leaf, token, files, data)
            planted_blob = "\n".join([
                json.dumps(data), draft, report_line, "\n".join(files.values()),
                "\n".join(PROBE_EXTRA.values()),
            ])
            if str(token) not in planted_blob:
                raise SystemExit("token not in %s input" % leaf)
            for name, content in files.items():
                (fix / name).write_text(content, encoding="utf-8", newline="\n")
            if leaf == "ignored-getprop-rest":
                if ("[ro.serialno]: [%s]" % token) not in files["getprop.txt"]:
                    raise SystemExit("serial not planted")
            elif "[ro.serialno]: [SYNTHFIXED001]" not in files["getprop.txt"]:
                raise SystemExit("canary pre-registered as serial")
            (probe / "probe.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
            (probe / "profile.draft.toml").write_text(draft, encoding="utf-8", newline="\n")
            (probe / "report.txt").write_text(report_line, encoding="utf-8", newline="\n")
            for extra_name, extra_body in PROBE_EXTRA.items():
                (probe / extra_name).write_text(extra_body, encoding="utf-8", newline="\n")
            absent = [token]
            for frag in frags:
                if frag not in absent:
                    absent.append(frag)
            (job / "absent.txt").write_text("\n".join(absent) + "\n", encoding="utf-8", newline="\n")
            (job / "leaf").write_text(leaf + "\n" + token + "\n" + mode + "\n", encoding="utf-8", newline="\n")
            (job / "mode").write_text(mode + "\n", encoding="utf-8", newline="\n")
            if mode == "both" and leaf in MAY:
                (job / "may-omit").write_text("yes\n", encoding="utf-8", newline="\n")
print("schema-planted %d" % n)
PY
then
    ok "schema lists every output field"
else
    bad "schema lists every output field"
    cat "$T/schema-precheck.txt" >&2
fi
for mut in p-display p-probecpu p-probedmi p-cpuinfo \
    m-vendor-sibling m-cpufam-cross m-kernel-cross m-driver-cross m-pci-cross \
    m-wifi-ssid-orig m-wifi-history m-bt-getprop m-bt-outside \
    m-input-otherline m-input-preheader m-proc-phys m-report-otherline \
    m-firmware-report m-input-descriptor m-proc-unused m-cpu-sibling m-input-name-id; do
    if [ -f "$T/schema-precheck.txt" ] && grep -F -q "planter mutation $mut: precheck exit 1" "$T/schema-precheck.txt"; then
        ok "planter mutation $mut makes the precheck exit non-zero"
    else
        bad "planter mutation $mut makes the precheck exit non-zero"
    fi
done
for mut in uefi-raw wireless-raw wlan-bcd-raw bt-bcd-raw; do
    if [ -f "$T/schema-precheck.txt" ] && grep -F -q "product mutation $mut: precheck exit 1" "$T/schema-precheck.txt"; then
        ok "product mutation $mut makes the precheck exit non-zero"
    else
        bad "product mutation $mut makes the precheck exit non-zero"
    fi
done
cat > "$T/schema-run.sh" << 'EOF'
#!/bin/bash
d=$1
mode=$(head -n 1 "$d/mode")
if [ "$mode" = probe ]; then
    bash "$GBOS_R" --probe "$d/probe" --out "$d/out" --date 20261014 > "$d/run.err" 2>&1 || {
        printf 'run failed\n' > "$d/result"
        exit 0
    }
elif [ "$mode" = fixture ]; then
    bash "$GBOS_R" --fixture "$d/fix" --out "$d/out" --date 20261014 > "$d/run.err" 2>&1 || {
        printf 'run failed\n' > "$d/result"
        exit 0
    }
else
    bash "$GBOS_R" --fixture "$d/fix" --probe "$d/probe" --out "$d/out" --date 20261014 > "$d/run.err" 2>&1 || {
        printf 'run failed\n' > "$d/result"
        exit 0
    }
fi
while IFS= read -r tok || [ -n "$tok" ]; do
    [ -n "$tok" ] || continue
    if grep -R -F -q -- "$tok" "$d/out"; then
        printf 'present %s\n' "$tok" > "$d/result"
        exit 0
    fi
done < "$d/absent.txt"
if [ ! -f "$d/may-omit" ]; then
    if grep -F -q 'omitted (probe is not the same machine)' "$d/out/gbos-report-20261014.txt"; then
        printf 'masked by omit\n' > "$d/result"
        exit 0
    fi
fi
printf 'ok\n' > "$d/result"
EOF
chmod +x "$T/schema-run.sh"
export GBOS_R=$R
find "$T/schema-root" -mindepth 1 -maxdepth 1 -type d -name 'j*' -print0 | xargs -0 -P 4 -n 1 "$T/schema-run.sh"
schema_bad=0
schema_dirs=0
for d in "$T/schema-root"/j*; do
    [ -d "$d" ] || continue
    schema_dirs=$((schema_dirs + 1))
    if [ ! -f "$d/result" ]; then
        schema_bad=$((schema_bad + 1))
        [ "$schema_bad" -le 8 ] && bad "schema $(head -n 1 "$d/leaf") missing result"
        continue
    fi
    line=$(cat "$d/result")
    if [ "$line" != ok ]; then
        schema_bad=$((schema_bad + 1))
        if [ "$schema_bad" -le 8 ]; then
            bad "schema $(head -n 1 "$d/leaf") $(sed -n '2p' "$d/leaf"): $line"
        fi
    fi
done
if [ "$schema_dirs" -gt 0 ] && [ "$schema_bad" = 0 ]; then
    ok "every schema field source rejects every canary class ($schema_dirs)"
else
    bad "every schema field source rejects every canary class ($schema_bad of $schema_dirs)"
fi

echo "report tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
