"""Opt-in USB boot evidence; build-time installation only."""
from pathlib import Path

FILES = ('etc/kano-usb-collect.sh', 'etc/kano-usb-evidence.sh',
         'etc/init/kano-usb-evidence.rc')
CONTEXT = r'/dev/kano-usb-evidence(/.*)? u:object_r:kano_usb_evidence:s0'
CIL = '''
;; Dedicated tmpfs evidence label; shell can inspect but cannot mutate it.
(type kano_usb_evidence)
(roletype object_r kano_usb_evidence)
(typeattributeset dev_type (kano_usb_evidence))
(typeattributeset file_type (kano_usb_evidence))
(typetransition kano_kmsg_logcat kano_usb_evidence file kano_usb_evidence)
(typetransition kano_kmsg_logcat kano_usb_evidence dir kano_usb_evidence)
(allow shell_202604 kano_usb_evidence (dir (search getattr)))
(allow shell_202604 kano_usb_evidence (file (open read getattr)))
'''
RC = '''# Ephemeral USB smoke evidence; absent from normal images.
service kano_usb_evidence /system/bin/sh /vendor/etc/kano-usb-evidence.sh
    disabled
    oneshot
    user root
    group shell
    seclabel u:r:kano_kmsg_logcat:s0

on property:sys.boot_completed=1
    mkdir /dev/kano-usb-evidence 0750 root shell
    restorecon /dev/kano-usb-evidence
    start kano_usb_evidence
'''
WRAPPER = '''#!/system/bin/sh
# All paths and commands are fixed. No adb-provided input or persistent data.
set -eu
umask 077
D=/dev/kano-usb-evidence
[ -d "$D" ] && [ ! -L "$D" ] || exit 2
# Repeated boot_completed events cannot replace the first boot snapshot.
mkdir "$D/.started" 2>/dev/null || exit 2
T="$D/.runtime.tmp"
# Refuse even a successful collector exit if its output was truncated or
# section framing was corrupted. Detailed topology is verified by the host.
valid() {
    awk '
      NR==1 { if ($0 != "KANO_USB_RUNTIME_V1") exit 1; next }
      NR==2 { if ($1 != "BOOT_GUID" || NF != 2) exit 1; next }
      NR==3 { if ($1 != "BOOT_ID" || NF != 2) exit 1; next }
      NR==4 { if ($0 != "BOOT_COMPLETED 1") exit 1; next }
      NR==5 { if ($0 != "BEGIN_MOUNTINFO") exit 1; phase=1; next }
      phase==1 { if ($0 == "END_MOUNTINFO") { if (!mounts) exit 1; phase=2; next } mounts++; next }
      phase==2 { if ($0 != "BEGIN_BLOCKS") exit 1; phase=3; next }
      phase==3 { if ($0 == "END_BLOCKS") { if (!nodes) exit 1; phase=4; next }
                  if ($1 != "NODE" || NF != 8) exit 1; nodes++; next }
      phase==4 { if ($0 != "COMPLETE") exit 1; phase=5; next }
      { exit 1 }
      END { if (phase != 5) exit 1 }
    ' "$1"
}
if /system/bin/sh /vendor/etc/kano-usb-collect.sh > "$T" 2>/dev/null; then
    if valid "$T"; then
        chown root:shell "$T" && chmod 0640 "$T" && restorecon "$T" &&
            mv "$T" "$D/runtime.txt" || exit 2
        exit 0
    fi
    reason='UNREAD framing'
else
    # Read only the final line, and output fixed category names. Never publish
    # partial topology or arbitrary collector text. Unknown failures stay generic.
    reason=$(awk '
      { last = $0 }
      END {
        if (last ~ /^UNREAD (boot_guid|boot_id|boot_completed|properties|mountinfo|blocks|block_entry|name|dev|realpath|topology|parent|slaves|serial)$/)
            print "UNREAD collector_" substr(last, 8)
        else print "UNREAD collector"
      }
    ' "$T") || reason='UNREAD collector'
fi
rm -f "$T"
printf '%s\\n' "$reason" > "$D/.failure.tmp"
chown root:shell "$D/.failure.tmp" && chmod 0640 "$D/.failure.tmp" &&
    restorecon "$D/.failure.tmp" && mv "$D/.failure.tmp" "$D/failure.txt" || exit 2
exit 2
'''


def install(tree, vmap, source):
    """Install only for the explicit image switch; caller owns validation."""
    source = Path(source)
    values = (source.read_text(), WRAPPER, RC)
    for rel, value in zip(FILES, values):
        path = tree / rel
        if path.exists() or path.is_symlink() or rel in vmap:
            raise ValueError('USB evidence path already exists')
        path.write_text(value)
        path.chmod(0o644)
        vmap[rel] = dict(vmap['etc/init/fwupd.rc'])
    context = tree / 'etc/selinux/vendor_file_contexts'
    if not context.is_file() or context.is_symlink():
        raise ValueError('vendor runtime file contexts missing')
    old = context.read_text()
    if 'kano_usb_evidence' in old or '/dev/kano-usb-evidence' in old:
        raise ValueError('USB evidence label already exists')
    context.write_text(old.rstrip('\n') + '\n' + CONTEXT + '\n')
    policy = tree / 'etc/selinux/vendor_sepolicy.cil'
    policy.write_text(policy.read_text().rstrip('\n') + '\n' + CIL)
