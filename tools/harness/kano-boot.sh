#!/usr/bin/env bash
# Power-cycle the KANO and pick a systemd-boot entry by (part of) its title over the AP UART.
# usage: kano-boot.sh "<title substring>" | "=<exact title>" [append-to-cmdline]
# The substring must match exactly one entry title (kano-ser.py menu); otherwise nothing is selected.
# An append is typed only after the menu's line editor is seen open on this entry (loader.conf "editor yes"); with
# "editor no", `e` does nothing and every further character would be a menu hotkey (d sets the default entry, t/T/+/-
# the timeout, f reboots into the firmware setup: EFI variables in the SPI flash), so then nothing more is sent.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
. "$HERE/kano-env.sh"
kano_require KANO_EC_TTY KANO_AP_TTY KANO_AP_UART_LOG
[ $# -ge 1 ] && [ -n "$1" ] || { echo "usage: $0 \"<title substring>\" | \"=<exact title>\" [append-to-cmdline]" >&2; exit 2; }
WANT=$1; APPEND=${2:-}
K=$HERE/kano-ser.py
L=$KANO_AP_UART_LOG
has_menu() { python3 "$K" menu "$L" "" > /dev/null 2>&1; [ $? -eq 5 ]; }   # a menu is drawn ("" names no entry)
key() { python3 "$K" ap-keys "$1" || { echo "AP key $1 not sent; stopped"; exit 3; }; sleep "${2:-0.3}"; }
python3 "$K" ec apshutdown 2 >/dev/null || { echo "EC apshutdown failed"; exit 3; }
sleep 5; : > "$L" || { echo "cannot empty the AP UART log $L; stopped before power-on"; exit 3; }
python3 "$K" ec powerbtn 2 >/dev/null || { echo "EC powerbtn failed"; exit 3; }
for _ in $(seq 1 120); do has_menu && break; sleep 0.5; done
has_menu || { echo "no systemd-boot menu"; exit 4; }
key 1b5b41                                      # Up: stop the countdown (also when the entry is refused below)
# every title of the first menu draw, in screen order; WANT must name exactly one ("=<title>" = exact title)
OUT=$(python3 "$K" menu "$L" "$WANT") || { rc=$?; printf '%s\n' "$OUT"; echo "entry '$WANT' not selected"; exit $rc; }
printf '%s\n' "$OUT" | grep '^menu: '
idx=$(printf '%s\n' "$OUT" | sed -n 's/^index \([0-9][0-9]*\)$/\1/p')
title=$(printf '%s\n' "$OUT" | sed -n 's/^title //p')
[ -n "$idx" ] || { echo "menu parse gave no index"; exit 5; }
key 1b5b48                                      # Home: first entry
n=0; while [ $n -lt "$idx" ]; do key 1b5b42; n=$((n + 1)); done   # Down
if [ -n "$APPEND" ]; then
  off=$(wc -c < "$L") || { echo "cannot read $L; stopped"; exit 3; }
  key 65 0                                      # e: edit this entry's options
  python3 "$K" editor "$L" "$off" 10 || { echo "the menu editor did not open (loader.conf 'editor no'?); nothing typed, stopped"; exit 3; }
  key 1b5b46 0.5                                # End
  python3 "$K" ap-type " $APPEND" 30 || { echo "append not typed; stopped"; exit 3; }; sleep 1
fi
key 0d 0
echo "selected #$idx: $title"
