# USB trial boot evidence

`GBOS_USB_SMOKE=1` adds a one-shot KANO-only root service. A normal vendor
build does not install its scripts, init service, runtime SELinux label, or
policy. After Android reports boot complete, it takes one static snapshot of
boot identity, `/proc/1/mountinfo`, and every readable block node and topology.
The service publishes `/dev/kano-usb-evidence/runtime.txt` atomically only
when the collector completes; otherwise it publishes a small `failure.txt`
when possible. `failure.txt` contains only a fixed category: `UNREAD framing`,
`UNREAD collector`, or `UNREAD collector_<reason>` for a known collector
reason. Partial snapshot contents are never copied into that marker. Failure
publication can itself fail, so a missing marker also means unreadable evidence.
Both live only in `/dev` for that boot, owned by root:shell and
mode 0640 inside a root:shell 0750 directory. The dedicated policy grants shell
only directory search/getattr and file open/read/getattr. No command, path, or
other collector input comes from ADB.

The collector runs in the existing permissive `kano_kmsg_logcat` domain; this
is not enforcing-domain confinement. The new evidence label grants the
shell read-only access, whose actual behavior still requires device validation.

A host must bind the snapshot boot ID to a live boot-ID read on both sides of
collection and compare live mountinfo and block names. The snapshot describes
its collection time: it cannot prove current device state after later changes,
or establish whether historical writes occurred. Missing or unreadable evidence
is not a pass. The source model is the host harness's `collect-runtime.sh`; the
vendor copy additionally rejects empty or malformed boot IDs, GUIDs, device
numbers, and empty mountinfo before publishing.

The option and policy need review and an Android build with actual `secilc`
validation, followed by real-device verification of labels, permissions,
service behavior and host binding before use in a physical trial.
