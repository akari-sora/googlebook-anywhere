#!/system/bin/sh
# Static root collector; the opt-in init service publishes only completed output.
# This collector makes no changes and deliberately fails when evidence is unreadable.
set -eu
fail() { printf 'UNREAD %s\n' "$1"; exit 2; }
printf '%s\n' 'KANO_USB_RUNTIME_V1'
guid=$(getprop ro.boot.boot_part_uuid) || fail boot_guid
boot=$(cat /proc/sys/kernel/random/boot_id) || fail boot_id
complete=$(getprop sys.boot_completed) || fail boot_completed
[ -n "$guid" ] && [ -n "$boot" ] && [ "$complete" = 1 ] || fail properties
for value in "$guid" "$boot"; do
    printf '%s\n' "$value" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' || fail properties
    [ "$value" != 00000000-0000-0000-0000-000000000000 ] || fail properties
done
printf 'BOOT_GUID %s\nBOOT_ID %s\nBOOT_COMPLETED %s\n' "$guid" "$boot" "$complete"
printf '%s\n' BEGIN_MOUNTINFO
mounts=$(cat /proc/1/mountinfo) || fail mountinfo
[ -n "$mounts" ] || fail mountinfo
printf '%s\n' "$mounts"
printf '%s\n' END_MOUNTINFO
printf '%s\n' BEGIN_BLOCKS
set -- /sys/class/block/*
[ "$1" != '/sys/class/block/*' ] || fail blocks
for node do
    # A dangling link, denied traversal, or disappearing entry could hide NVMe.
    [ -e "$node" ] || fail block_entry
    name=${node##*/}
    case "$name" in *[!A-Za-z0-9_.-]*|'') fail name ;; esac
    dev=$(cat "$node/dev") || fail dev
    printf '%s\n' "$dev" | grep -Eq '^[0-9]+:[0-9]+$' || fail dev
    path=$(readlink -f "$node") || fail realpath
    case "$path" in /sys/devices/*) ;; *) fail realpath ;; esac
    case "$path:$dev" in *[!A-Za-z0-9_./:-]*) fail topology ;; esac
    # A partition's class entry must resolve to its actual parent block
    # device. Linux partitions commonly have no slaves directory of their own.
    [ "${path##*/}" = "$name" ] || fail parent
    parent=-; slaves=-
    if [ -e "$node/partition" ] || [ -L "$node/partition" ]; then
        [ -f "$node/partition" ] && [ ! -L "$node/partition" ] || fail parent
        number=$(cat "$node/partition") || fail parent
        case "$number" in ''|*[!0-9]*) fail parent ;; esac
        case "$number" in *[1-9]*) ;; *) fail parent ;; esac
        parent_path=${path%/*}; parent=${parent_path##*/}
        case "$parent" in *[!A-Za-z0-9_.-]*|'') fail parent ;; esac
        parent_node=/sys/class/block/$parent
        [ -e "$parent_node" ] || fail parent
        resolved=$(readlink -f "$parent_node") || fail parent
        [ "$resolved" = "$parent_path" ] || fail parent
        [ ! -e "$parent_node/partition" ] && [ ! -L "$parent_node/partition" ] || fail parent
        [ -f "$parent_node/dev" ] && [ ! -L "$parent_node/dev" ] || fail parent
        parent_dev=$(cat "$parent_node/dev") || fail parent
        case "$parent_dev" in *[!0-9:]*) fail parent ;; esac
        printf '%s\n' "$parent_dev" | grep -Eq '^[0-9]+:[0-9]+$' || fail parent
        [ "$parent_dev" != "$dev" ] || fail parent
        if [ -e "$node/slaves" ] || [ -L "$node/slaves" ]; then
            [ -d "$node/slaves" ] || fail slaves
            entries=$(ls -A "$node/slaves") || fail slaves
            [ -z "$entries" ] || fail slaves
        fi
    else
        [ -d "$node/slaves" ] || fail slaves
        entries=$(ls -A "$node/slaves") || fail slaves
        if [ -n "$entries" ]; then
            slaves=$(printf '%s\n' "$entries" | tr '\n' ',' | sed 's/,$//') || fail slaves
            case "$slaves" in *[!A-Za-z0-9_.,-]*) fail slaves ;; esac
        fi
    fi
    transport=other; serial=-; usb_device_seen=0
    dir=$path
    while [ "$dir" != /sys ] && [ "$dir" != / ]; do
        part=${dir##*/}
        case "$part" in usb[0-9]*) transport=usb ;; esac
        # The nearest USB device ancestor is the drive. Ignore serials belonging
        # to hubs/controllers further up the path (they can also have serials).
        case "$part" in
            *:*) ;; # USB interface, not the USB device
            [0-9]*-[0-9]*)
                if [ "$usb_device_seen" = 0 ]; then
                    usb_device_seen=1
                    if [ -f "$dir/serial" ]; then
                        value=$(cat "$dir/serial") || fail serial
                        case "$value" in *[!A-Za-z0-9_.-]*|'') fail serial ;; esac
                        serial=$value
                    fi
                fi ;;
        esac
        dir=${dir%/*}
    done
    printf 'NODE %s %s %s %s %s %s %s\n' "$name" "$dev" "$path" "$parent" "$slaves" "$transport" "$serial"
done
printf '%s\n' END_BLOCKS COMPLETE
