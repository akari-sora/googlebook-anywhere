#!/bin/sh
# ssh to the KANO maintenance OS (root@$KANO_IP, key $KANO_SSH_KEY; see kano-env.sh). Every harness ssh call goes
# through here. The host key is not pinned: the maintenance OS gets a new one whenever it is reflashed. That is why
# the disk-writing scripts check the target disk's serial (and more) on the remote side before every write.
HERE=$(cd "$(dirname "$0")" && pwd -P)
. "$HERE/kano-env.sh"
kano_require KANO_IP KANO_SSH_KEY
exec ssh -i "$KANO_SSH_KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "root@$KANO_IP" "$@"
