#!/usr/bin/env bash
# KANO Android UI helper over adb: kano-ui.sh list | tap "<exact text>" | shot <file>
# Talks only to the fixed address $KANO_ADB_ADDR (kano-env.sh), and only once that device has shown it runs this
# project's vendor image (KANO_ANDROID_ID_PROP set there): nothing is dumped, tapped or captured on another device.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
. "$HERE/kano-env.sh"
kano_require KANO_ADB KANO_ADB_ADDR KANO_ANDROID_ID_PROP
ADB=$KANO_ADB; S="-s $KANO_ADB_ADDR"
[ -n "$(timeout 30 $ADB $S shell getprop "$KANO_ANDROID_ID_PROP" 2> /dev/null | tr -d '\r\n')" ] ||
  { echo "refusing: $KANO_ADB_ADDR is not this project's Android ($KANO_ANDROID_ID_PROP not set there)" >&2; exit 3; }
dump() { timeout 60 $ADB $S shell uiautomator dump /sdcard/kano-ui.xml >/dev/null 2>&1; timeout 30 $ADB $S exec-out cat /sdcard/kano-ui.xml; }
case "$1" in
  list) dump | python3 -c "
import sys, re
x = sys.stdin.read()
for m in re.finditer(r\"<node [^>]*>\", x):
    n = m.group(0); t = re.search(r\" text=\\\"([^\\\"]*)\\\"\", n).group(1); d = re.search(r\"content-desc=\\\"([^\\\"]*)\\\"\", n).group(1)
    c = re.search(r\"clickable=\\\"(true|false)\\\"\", n).group(1); b = re.search(r\"bounds=\\\"([^\\\"]*)\\\"\", n).group(1)
    if t or d: print((t or d)[:70], \"|\", \"click\" if c == \"true\" else \"\", b)
";;
  tap) dump | python3 -c "
import sys, re
want = sys.argv[1]; x = sys.stdin.read()
for m in re.finditer(r\"<node [^>]*>\", x):
    n = m.group(0); t = re.search(r\" text=\\\"([^\\\"]*)\\\"\", n).group(1); d = re.search(r\"content-desc=\\\"([^\\\"]*)\\\"\", n).group(1)
    if want in (t, d):
        a, b, c, e = map(int, re.findall(r\"\\d+\", re.search(r\"bounds=\\\"([^\\\"]*)\\\"\", n).group(1)))
        print((a + c) // 2, (b + e) // 2); break
" "$2" | { read X Y; [ -n "$X" ] && timeout 30 $ADB $S shell input tap $X $Y && echo "tapped $2 at $X,$Y" || echo "NOT FOUND: $2"; };;
  shot) timeout 30 $ADB $S exec-out screencap -p > "$2" && echo "saved $2";;
esac
