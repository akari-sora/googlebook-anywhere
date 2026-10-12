#!/usr/bin/env bash
# Runs ON the KANO maintenance Linux booted from the "LPSS-I2C audio test" entry. No sound is played here.
set -uo pipefail
grep -q "pci-stub.ids=8086:51a8" /proc/cmdline || { echo "REFUSE: not the LPSS test entry"; exit 1; }
for d in 0000:00:1e.0 0000:00:1e.3; do echo "$d -> $(basename "$(readlink -f /sys/bus/pci/devices/$d/driver)")"; done
[ "$(basename "$(readlink -f /sys/bus/pci/devices/0000:00:1e.0/driver)")" = pci-stub ] || { echo "REFUSE: UART not on pci-stub"; exit 1; }
modprobe --ignore-install intel_lpss_pci && echo "intel_lpss_pci loaded"
for _ in $(seq 1 30); do grep -q sof /proc/asound/cards 2>/dev/null && break; sleep 2; done
for d in /sys/bus/pci/devices/0000:00:1[59e].*; do echo "$d -> $(basename "$(readlink -f $d/driver)")"; done
ls /sys/bus/i2c/devices/ | tr '\n' ' '; echo
echo "== cards"; cat /proc/asound/cards; echo "== pcm"; cat /proc/asound/pcm
dmesg | grep -i -E "sof|nau8825|max98373|topology|i2c_designware|i2c-designware" | tail -n 30
C=$(awk '/sofnau8825/ {print $1; exit}' /proc/asound/cards)
if [ -n "$C" ]; then
  echo "== controls"; amixer -c "$C" controls | sed 's/^numid=[0-9]*,//'
  echo "== contents"; amixer -c "$C" contents
  echo "== ucm"; for f in /usr/share/alsa/ucm2/conf.d/*sof* /usr/share/alsa/ucm2/conf.d/*nau*; do [ -e "$f" ] && basename "$f"; done; alsaucm -c hw:"$C" list _verbs 2>&1 | head; alsaucm -c hw:"$C" list _devices/HiFi 2>&1 | head -n 20
  for f in /usr/share/alsa/ucm2/conf.d/sof-nau8825/*.conf; do [ -e "$f" ] && { echo "== $f"; cat "$f"; }; done
else echo NO_CARD; fi
echo "== input"; grep -E "^N:" /proc/bus/input/devices
