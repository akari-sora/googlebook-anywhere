#!/system/bin/sh
# The QEMU boot gate needs evidence from the kernel, not modprobe's exit status.
# This runs after the synchronous backlight modprobe in the existing permissive
# kano_kmsg_logcat domain. Missing/unreadable /proc/modules emits no success.
new=0
stock=0
exec 3< /proc/modules || exit 1
while read -r name rest; do
    case "$name" in
        gbos_kbd_backlight) new=1 ;;
        cros_kbd_led_backlight) stock=1 ;;
    esac
done <&3
printf '<6>gbos-kbd-modules: new=%s stock=%s\n' "$new" "$stock" > /dev/kmsg
