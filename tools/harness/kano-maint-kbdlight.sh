#!/usr/bin/env bash
# Runs ON the KANO maintenance Linux (root). Loads only the cros_ec_proto library + cros_kbd_led_backlight (ACPI GOOG0002 path).
# The EC transports (cros_ec_lpcs etc.), cros_ec core/dev/chardev stay blocked; refuse if any of them is loaded.
set -uo pipefail
for m in cros_ec cros_ec_lpcs cros_ec_dev cros_ec_chardev cros_ec_spi cros_ec_i2c cros_ec_ishtp cros_ec_uart; do
  lsmod | awk '{print $1}' | grep -qx "$m" && { echo "REFUSE: $m loaded"; exit 1; }
done
echo "== ACPI GOOG devices"; for p in /sys/bus/acpi/devices/GOOG* /sys/bus/acpi/devices/goog*; do [ -e "$p" ] || continue; d=$(basename "$p"); echo "$d status=$(cat "$p/status" 2>/dev/null) path=$(cat "$p/path" 2>/dev/null)"; done
ls -d /sys/bus/acpi/devices/GOOG0002:* >/dev/null 2>&1 || { echo "NO_GOOG0002"; exit 0; }
K=/usr/lib/modules/$(uname -r)/kernel/drivers/platform/chrome
for f in "$K"/cros_ec_proto* "$K"/cros-ec-proto* "$K"/cros_kbd_led*; do [ -e "$f" ] && basename "$f"; done
for m in cros_ec_proto cros_kbd_led_backlight; do
  [ -d /sys/module/$m ] && { echo "$m present ($( [ -e /sys/module/$m/initstate ] && echo module || echo built-in))"; continue; }
  f=$(modinfo -F filename $m 2>/dev/null); [ -n "$f" ] && [ -e "$f" ] || { echo "missing $m"; exit 1; }
  insmod "$f" && echo "insmod $m ok"
done
sleep 1; ls -l /sys/class/leds/ 
L=/sys/class/leds/chromeos::kbd_backlight
[ -d $L ] || { echo "NO_LED"; dmesg | tail -n 10; exit 0; }
echo "max=$(cat $L/max_brightness) cur=$(cat $L/brightness)"
for v in 0 100 0 50 100; do echo $v > $L/brightness; echo "set $v -> $(cat $L/brightness)"; sleep 4; done
echo $(( $(cat $L/max_brightness) / 2 )) > $L/brightness; echo "left at $(cat $L/brightness)"
for m in cros_ec cros_ec_lpcs cros_ec_dev cros_ec_chardev; do lsmod | awk '{print $1}' | grep -qx "$m" && echo "UNEXPECTED $m"; done
echo KBD_DONE
