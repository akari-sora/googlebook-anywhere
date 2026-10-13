#!/usr/bin/env python3
"""Mutation check for the harness guards: each mutation reverts one guard (or one layered pair of guards) in a fresh
temp copy of tools/harness - never in place - and runs the tests that are supposed to catch it with
KANO_HARNESS_DIR pointing at the copy. A mutation that leaves its tests green is a failure of this check.

usage: mutate.py [-j N] [--classify] [ID ...]      (default: all mutations, 4 in parallel)

--classify re-runs every killed mutation with HARNESS_BEHAVIOUR_ONLY=1, where the tests skip their message
assertions. "effect" = still red: the mutant really sends/writes/discards/powers off something it must not, or exits
wrongly. "message" = only the refusal reason changed: another guard layer still prevented the effect.

The copy sits at <tmp>/tools/harness next to links <tmp>/builder and <tmp>/profiles to the repo's builder/ and
profiles/, the layout the watcher reads its guard list from (profiles/kano/profile.toml through builder/lib/devprofile.py). The personal-data mutations plant a real value into the copy, taken at run time from the private
files (tools/release/private-patterns.txt, tools/harness/kano-env.local.sh), so this file holds none; without those
files (the public export) they are skipped, as is the test that would catch them.
"""
import os, re, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent.parent
HARNESS = Path(os.environ.get("KANO_HARNESS_DIR") or REPO / "tools" / "harness").resolve()


def private_literal():
    """A plain word from the private pattern list (planted in a copy, never written here); None without the list."""
    f = REPO / "tools" / "release" / "private-patterns.txt"
    words = [l for l in f.read_text().splitlines() if re.fullmatch(r"[A-Za-z0-9_-]{6,}", l)] if f.exists() else []
    return words[0] if words else None


def private_adb_addr():
    """The developer's KANO_ADB_ADDR from kano-env.local.sh; None without it."""
    f = HARNESS / "kano-env.local.sh"
    m = re.search(r'^: "\$\{KANO_ADB_ADDR:=([^}]+)\}"', f.read_text(), re.M) if f.exists() else None
    return m.group(1) if m else None

# (id, review finding, file, [(original text, mutated text), ...], tests that must go red)
MUTATIONS = [
    ("ser-control-chars", "Codex C2", "kano-ser.py",
     [("if o < 0x20 or o == 0x7F:", "if False:")], ["test_ser.Allowlist.test_refused_with_the_right_reason"]),
    ("ser-non-ascii", "Codex C2", "kano-ser.py",
     [("if o > 0x7E:", "if False:")], ["test_ser.Allowlist.test_refused_with_the_right_reason"]),
    ("ser-allowlist", "Codex C2 / Opus M1", "kano-ser.py",
     [("""        return None, f"'{name}' is not on the EC allowlist\"""", "        return line, None")],
     ["test_ser.Allowlist.test_refused_with_the_right_reason", "test_ser.Cli.test_refused_commands_exit_2_without_loading_pyserial"]),
    ("ser-argument-shapes", "Codex C2", "kano-ser.py",
     [("if len(shape) == len(args) and all(ARG[kind](a) for kind, a in zip(shape, args)):", "if True:")],
     ["test_ser.Allowlist.test_refused_with_the_right_reason"]),
    ("ser-lid-arguments", "lid commands take no arguments", "kano-ser.py",
     [('"lidclose": [()],', '"lidclose": [(), ("gpio",)],')], ["test_ser.Allowlist.test_refused_with_the_right_reason"]),
    ("ser-prefix-match", "Opus M1", "kano-ser.py",
     [("shapes = EC_ALLOW.get(name)", "shapes = next((v for k, v in EC_ALLOW.items() if k.startswith(name)), None)")],
     ["test_ser.Allowlist.test_no_prefix_or_abbreviation_passes"]),
    ("ser-eager-pyserial", "lazy import", "kano-ser.py",
     [("import os, re, sys, time\n", "import os, re, sys, time\nimport serial  # noqa\n")],
     ["test_ser.Cli.test_refused_commands_exit_2_without_loading_pyserial"]),

    ("stick-serial", "Codex C3 / Opus I2", "kano-delta-push.py",
     [("""    if not os.environ.get("KANO_STICK_SERIAL") or f.get("SERIAL", "").strip() != os.environ["KANO_STICK_SERIAL"]:""", "    if False:")],
     ["test_delta_push.StickPush.test_wrong_serial"]),
    ("stick-usb", "Codex C3", "kano-delta-push.py",
     [("""    if f.get("TRAN") != "usb":""", "    if False:")], ["test_delta_push.StickPush.test_not_a_usb_disk"]),
    ("stick-gpt-compare", "Codex C3 / Opus I2", "kano-delta-push.py",
     [("        if remote.get(p[0]) != p:", "        if False:")], ["test_delta_push.StickPush.test_gpt_mismatch"]),
    ("stick-gpt-compare+allowed-set", "Codex C3 / Opus I2", "kano-delta-push.py",
     [("        if remote.get(p[0]) != p:", "        if False:"),
      ("allowed={p[0] for p in img if remote.get(p[0]) == p}", "allowed={p[0] for p in img} | {p[0] for p in f['PARTS']}")],
     ["test_delta_push.StickPush.test_gpt_mismatch"]),
    ("stick-kernel-table", "Codex C3", "kano-delta-push.py",
     [("        if k is None or k[1:] != p[2:]:", "        if False:")],
     ["test_delta_push.StickPush.test_kernel_table_differs_from_gpt"]),
    ("stick-busy-overlap", "Codex C3", "kano-delta-push.py",
     [("if s < b and a < e and kname in busy:", "if False:")],
     ["test_delta_push.StickPush.test_write_into_a_mounted_partition", "test_delta_push.StickPush.test_write_into_a_held_partition"]),
    ("foreign-partition-overlap", "Codex C3", "kano-delta-push.py",
     [("if s < b and a < e and num not in allowed:", "if False:")],
     ["test_delta_push.StickPush.test_write_overlapping_a_partition_that_is_not_in_the_image",
      "test_delta_push.NvmePush.test_write_overlapping_another_nvme_partition"]),
    ("stick-idcheck-per-write", "Codex C3", "kano-delta-push.py",
     [("f, stick_ident(disk), \"no longer the Kingston", "f, \"true\", \"no longer the Kingston")],
     ["test_delta_push.StickPush.test_identity_rechecked_in_the_same_command_as_each_write"]),
    ("write-guard-mounts", "Codex I3 / Opus m12", "kano-delta-push.py",
     [("""[ $? -eq 1 ] || no "{n} is mounted\"""", """true || no "{n} is mounted\"""")],
     ["test_delta_push.StickPush.test_partition_mounted_after_the_checks",
      "test_delta_push.NvmePush.test_identity_rechecked_in_the_same_command_as_each_write"]),
    ("write-guard-swaps", "Codex I3", "kano-delta-push.py",
     [("""[ $? -eq 1 ] || no "{n} is used as swap\"""", """true || no "{n} is used as swap\"""")],
     ["test_delta_push.StickPush.test_swap_after_the_checks", "test_delta_push.NvmePush.test_swap_on_the_nvme_after_the_checks"]),
    ("write-guard-holders", "Codex I3", "kano-delta-push.py",
     [("""[ -z "$H" ] || no "{n} is held by $H\"""", """true || no "{n} is held by $H\"""")],
     ["test_delta_push.StickPush.test_partition_held_after_the_checks"]),
    ("write-guard-gpt", "Codex I3", "kano-delta-push.py",
     [("""[ "${{G%% *}}" = {gpt} ] || no""", """true || no""")], ["test_delta_push.StickPush.test_gpt_rewritten_after_the_checks"]),
    ("write-guard-kernel-table", "Codex I3", "kano-delta-push.py",
     [("""sh.append(f'[ "$(cat {sysd}/start)" = {start} ] && [ "$(cat {sysd}/size)" = {size} ] || '""", """sh.append(f'true || '""")],
     ["test_delta_push.StickPush.test_kernel_table_moved_after_the_checks"]),
    ("write-guard-unreadable-swaps", "Codex I3 (fail-open query)", "kano-delta-push.py",
     [("""'W=$(cat /proc/swaps) || no "cannot read /proc/swaps"'""", """'W=$(cat /proc/swaps)'""")],
     ["test_delta_push.StickPush.test_unreadable_swaps_at_the_write"]),
    ("write-guard-unreadable-holders", "Codex I3 (fail-open query)", "kano-delta-push.py",
     [("""f'H=$(ls -A {sysd}/holders) || no "cannot read the holders of {n}"'""", """f'H=$(ls -A {sysd}/holders 2>/dev/null)'""")],
     ["test_delta_push.StickPush.test_unreadable_holders_at_the_write"]),
    ("facts-holders-dir", "Codex I3 (fail-open query)", "kano-delta-push.py",
     [("""[ -d "/sys/block/$B/holders" ] || { echo "cannot read the holders of $B" >&2; exit 3; }\n""", "")],
     ["test_delta_push.StickPush.test_facts_without_a_holders_dir"]),

    ("nvme-serial", "Codex C3", "kano-delta-push-nvme.py",
     [("""    if not nvme_serial or f.get("SERIAL", "").strip() != nvme_serial:""", "    if False:")], ["test_delta_push.NvmePush.test_wrong_serial"]),
    ("nvme-root-disk", "Codex C3", "kano-delta-push-nvme.py",
     [("""    if f.get("ROOTDISK") == DISK or f.get("ROOTSRC", "").startswith(DEV):""", "    if False:")],
     ["test_delta_push.NvmePush.test_nvme_is_the_root_disk"]),
    ("nvme-root-disk+busy+maint-os", "Codex C3", "kano-delta-push-nvme.py",
     [("""    if f.get("ROOTDISK") == DISK or f.get("ROOTSRC", "").startswith(DEV):""", "    if False:"),
      ("""        if name == DISK or name.startswith(DISK + "p"):""", "        if False:"),
      ("""    if f.get("ROOTTRAN") != "usb" or not stick_serial or f.get("ROOTSERIAL", "").strip() != stick_serial:""", "    if False:")],
     ["test_delta_push.NvmePush.test_nvme_is_the_root_disk"]),
    ("nvme-maintenance-os", "Codex C3", "kano-delta-push-nvme.py",
     [("""    if f.get("ROOTTRAN") != "usb" or not stick_serial or f.get("ROOTSERIAL", "").strip() != stick_serial:""", "    if False:")],
     ["test_delta_push.NvmePush.test_not_the_maintenance_os"]),
    ("nvme-busy", "Codex C3", "kano-delta-push-nvme.py",
     [("""        if name == DISK or name.startswith(DISK + "p"):""", "        if False:")],
     ["test_delta_push.NvmePush.test_nvme_partition_mounted", "test_delta_push.NvmePush.test_nvme_held",
      "test_delta_push.NvmePush.test_nvme_used_as_swap"]),
    ("nvme-bounds", "Codex C3", "kano-delta-push-nvme.py",
     [("        if theirs[0][2:] != mine[0][2:]:", "        if False:")], ["test_delta_push.NvmePush.test_gpt_mismatch"]),
    ("nvme-kernel-table", "Codex C3", "kano-delta-push-nvme.py",
     [("        if k is None or k[1:] != mine[0][2:]:", "        if False:")],
     ["test_delta_push.NvmePush.test_kernel_table_differs_from_gpt"]),
    ("nvme-idcheck-per-write", "Codex C3", "kano-delta-push-nvme.py",
     [("f, nvme_ident(), \"not the internal NVMe", "f, \"true\", \"not the internal NVMe")],
     ["test_delta_push.NvmePush.test_wrong_serial_at_the_write"]),
    ("nvme-root-not-identified", "Codex I3 (empty lsblk)", "kano-delta-push-nvme.py",
     [("""P=$(lsblk -no PKNAME "$R") && [ -n "$P" ] && [ "$P" != {DISK} ] && '""", """[ "$(lsblk -no PKNAME "$R")" != {DISK} ] && true '"""),
      ("""            f'[ "$(lsblk -dno TRAN "/dev/$P")" = usb ] && [ "$(lsblk -dno SERIAL "/dev/$P" | tr -d \\' \\')" = {stick} ]')""",
       """            f'')""")],
     ["test_delta_push.NvmePush.test_root_disk_unknown_at_the_write"]),

    ("install-decide-from-old-logs", "Codex C1 / Opus I1", "kano-nvme-install.sh",
     [("""  [ "$rc" -eq 0 ] || die "$name: remote exit status $rc"\n  grep -qx "$marker" "$RUN/$name.out" || die "$name: '$marker' missing from this run's output\"""",
       """  grep -q "$marker" "$BASE"/install.log "$BASE"/run-*/"$name.out" 2>/dev/null || die "$name\"""")],
     ["test_nvme_install.Install.test_guard_ssh_failure_with_stale_markers",
      "test_nvme_install.Install.test_guard_output_without_marker"]),
    ("install-guard-neutered", "Codex C1 / Opus I1", "kano-nvme-install.sh",
     [("guard() { guard_id; guard_idle; echo", "guard() { : ; echo")],
     ["test_nvme_install.Install.test_stale_success_markers_and_refusing_guard",
      "test_nvme_install.Install.test_wrong_pci_id", "test_nvme_install.Install.test_root_on_the_nvme",
      "test_nvme_install.Install.test_wrong_entry"]),
    ("install-idle-guard", "Codex C1", "kano-nvme-install.sh",
     [("guard() { guard_id; guard_idle; echo", "guard() { guard_id; echo")],
     ["test_nvme_install.Install.test_mounted_nvme", "test_nvme_install.Install.test_held_nvme"]),
    ("install-guard-not-in-destructive-call", "Codex C1 (same invocation)", "kano-nvme-install.sh",
     [("remote 03-copy DD_OK <<'EOF'\nguard\n", "remote 03-copy DD_OK <<'EOF'\n")],
     ["test_nvme_install.Install.test_target_changes_between_guards_and_discard"]),
    ("install-backup-sha", "Codex I2", "kano-nvme-install.sh",
     [("""  [ -n "$want" ] && [ "$(sha256 "$f")" = "$want" ] || die""", "  true || die")],
     ["test_nvme_install.Install.test_backup_corrupted"]),
    ("install-backup-unchecked", "Codex I2", "kano-nvme-install.sh",
     [("""  [ "$rc" -eq 0 ] || die "$name: remote exit status $rc"\n}\n\n# initrd_nvme""", """  :\n}\n\n# initrd_nvme"""),
      ("""  [ "$n" = 1048576 ] || die""", "  true || die"),
      ("""  [ -n "$want" ] && [ "$(sha256 "$f")" = "$want" ] || die""", "  true || die")],
     ["test_nvme_install.Install.test_backup_ssh_failure", "test_nvme_install.Install.test_backup_truncated"]),
    ("install-step-exit-status", "Codex I1", "kano-nvme-install.sh",
     [("""  [ "$rc" -eq 0 ] || die "$name: remote exit status $rc"\n  grep -qx""", """  :\n  grep -qx"""),
      ("""remote 03-copy DD_OK <<'EOF'""", """remote 03-copy DD_OK <<'EOF' || true""")],
     ["test_nvme_install.Install.test_copy_failure_stops_before_the_gpt_rewrite"]),
    ("install-source-binding", "Codex I2 (source bound to the stick)", "kano-nvme-install.sh",
     [("""    die "$SRC/$f is not what the stick's ESP holds""", """    true "$SRC/$f is not what the stick's ESP holds""")],
     ["test_nvme_install.Install.test_source_that_is_not_the_stick_image"]),
    ("install-entry-set-unbound", "plain entry dropped (the stick holds the source's entry set)", "kano-nvme-install.sh",
     [("""  die "the stick's ESP does not hold the same boot entries as $SRC ($ENTRIES)\"""",
       """  true "the stick's ESP does not hold the same boot entries as $SRC ($ENTRIES)\"""")],
     ["test_nvme_install.Install.test_stick_with_other_entries_than_the_source"]),
    ("install-entry-set-any", "plain entry dropped (known entry sets only)", "kano-nvme-install.sh",
     [("""  *) die "entries in $SRC: '$ENTRIES'; expected""", """  *) true "entries in $SRC: '$ENTRIES'; expected""")],
     ["test_nvme_install.Install.test_missing_entry"]),
    ("install-bootconfig-checked-late", "Codex I2 (inputs before the discard)", "kano-nvme-install.sh",
     [("""initrd_nvme "$RUN/src-initrd.img" - - || die""", """true || die""")],
     ["test_nvme_install.Install.test_bad_bootconfig", "test_nvme_install.Install.test_bootconfig_without_a_vbmeta_digest"]),
    ("install-entry-guards-unchecked", "Codex I2 (inputs before the discard)", "kano-nvme-install.sh",
     [("""  grep -qE '^options (.* )?module_blacklist=[^ ]' "$RUN/src-$f" && grep -qE '^options .* efi=noruntime( |$)' "$RUN/src-$f" ||""",
       """  true ||""")],
     ["test_nvme_install.Install.test_entry_without_the_guards"]),
    ("install-idle-fail-open", "Codex I3 (grep status 2)", "kano-nvme-install.sh",
     [("""  none '^/dev/nvme0n1' /proc/swaps || refuse "swap on the NVMe\"""",
       """  if grep -q '^/dev/nvme0n1' /proc/swaps; then refuse "swap on the NVMe"; fi""")],
     ["test_nvme_install.Install.test_unreadable_swaps_is_not_idle"]),
    ("install-holders-dir", "Codex I3", "kano-nvme-install.sh",
     [("""  [ -d /sys/block/nvme0n1/holders ] || refuse "cannot read the holders of the NVMe\"""", "  :")],
     ["test_nvme_install.Install.test_missing_holders_dir_is_not_idle"]),
    ("install-remote-pipefail", "Codex I1", "kano-nvme-install.sh",
     [("""  printf 'set -euo pipefail\\nNVME_SERIAL""", """  printf 'set -eu\\nNVME_SERIAL""")],
     ["test_nvme_install.Install.test_resize_failure_inside_a_pipeline_stops_the_install"]),

    ("update-stale-artifacts", "Opus I10", "kano-nvme-update.sh",
     [("""rm -f "$OUT"/{bootconfig.txt""", """true "$OUT"/{bootconfig.txt"""),
      ("""[ $rc -eq 0 ] || die "initrd prep failed (exit $rc)\"""", ":")],
     ["test_nvme_update.Update.test_stale_artifacts_and_failing_step"]),
    ("update-guid-from-disk", "Codex I1", "kano-nvme-update.sh",
     [("""[ "$(lower "$GUID")" = "$(lower "$KANO_NVME_BOOT_A_GUID")" ] || die""", "true || die")],
     ["test_nvme_update.Update.test_boot_a_guid_read_from_the_disk_must_match"]),
    ("update-boot-a-name", "Codex I1", "kano-nvme-update.sh",
     [("""[ "$NAME" = boot_a ] || refuse""", "true || refuse")], ["test_nvme_update.Update.test_partition_13_must_be_boot_a"]),
    ("update-adb-identity", "Opus M2", "kano-nvme-update.sh",
     [("""  [ -n "$(A shell getprop "$KANO_ANDROID_ID_PROP" 2> /dev/null | tr -d '\\r\\n')" ]""", "  true")],
     ["test_nvme_update.Update.test_adb_only_the_fixed_address_and_only_this_projects_android"]),
    ("update-transfer-status", "Codex I1", "kano-nvme-update.sh",
     [("""< "$OUT/bootconfig.txt" || die "bootconfig transfer failed\"""", """< "$OUT/bootconfig.txt" || true""")],
     ["test_nvme_update.Update.test_bootconfig_transfer_failure"]),
    ("update-transfer-status+remote-sha", "Codex I1", "kano-nvme-update.sh",
     [("""< "$OUT/bootconfig.txt" || die "bootconfig transfer failed\"""", """< "$OUT/bootconfig.txt" || true"""),
      ("""[ "$(sha256sum /root/bootconfig-new.txt | cut -c1-64)" = "$BC" ] || refuse""", "true || refuse")],
     ["test_nvme_update.Update.test_bootconfig_transfer_failure"]),
    ("update-esp-result-check", "Codex I1", "kano-nvme-update.sh",
     [("""[ "$(sha256sum /root/initrd-nvme.new | cut -c1-64)" = "$WANT" ] || refuse""", "true || refuse")],
     ["test_nvme_update.Update.test_esp_rewrite_checks_the_result_and_unmounts"]),
    ("update-watcher-status", "Codex I1 / I5", "kano-nvme-update.sh",
     [("""[ $wrc -eq 0 ] || die "watcher exit""", """true || die "watcher exit""")],
     ["test_nvme_update.Update.test_watcher_failure_fails_the_run"]),

    ("update-idle-fail-open", "Codex I3 (grep status 2)", "kano-nvme-update.sh",
     [("""  none '^/dev/nvme0n1' /proc/swaps || refuse "swap on the NVMe\"""",
       """  if grep -q '^/dev/nvme0n1' /proc/swaps; then refuse "swap on the NVMe"; fi""")],
     ["test_nvme_update.Update.test_unreadable_swaps_is_not_idle"]),
    ("update-adb-after-boot", "Opus m14", "kano-nvme-update.sh",
     [("""if [ $up -eq 1 ]; then\n  log "boot: $(A shell""", """if true; then\n  log "boot: $(A shell""")],
     ["test_nvme_update.Update.test_watcher_failure_fails_the_run"]),
    ("update-watcher-status+boot-check", "Codex I1 / I5", "kano-nvme-update.sh",
     [("""[ $wrc -eq 0 ] || die "watcher exit""", """true || die "watcher exit"""),
      ("""[ $booted -eq 1 ] || die""", "true || die")],
     ["test_nvme_update.Update.test_watcher_failure_fails_the_run"]),
    ("watch-this-boot", "Codex I5", "kano-gbos-watch.py",
     [("base = len(raw) if CMDLINE in clean(raw) else 0", "base = 0")],
     ["test_watch.Watch.test_an_older_boots_command_line_does_not_count"]),
    ("watch-no-cmdline", "Codex I5", "kano-gbos-watch.py",
     [("if not verified and not in_window:", "if False:")],
     ["test_watch.Watch.test_log_without_this_boots_command_line_is_unobservable", "test_watch.Watch.test_missing_log_is_unobservable"]),
    ("watch-stall", "Codex I5", "kano-gbos-watch.py",
     [("if now - last_growth >= a.stall_timeout:", "if False:")], ["test_watch.Watch.test_log_that_stops_growing_is_unobservable"]),
    ("watch-camera-timeout", "Codex I5 / Opus M12", "kano-gbos-watch.py",
     [("if cam is not None and cam.poll() is None and now - cam_start > a.camera_timeout:", "if False:")],
     ["test_watch.Watch.test_hung_camera_grab_does_not_stall_the_watch"]),
    ("watch-ec-confirm", "Codex I5", "kano-gbos-watch.py",
     [("""if rc == 0 and re.search(r"power state \\d+ = (G3|S5)\\b", out):""", "if True:")],
     ["test_watch.Watch.test_unconfirmed_power_cut_has_its_own_exit_status"]),
    ("watch-ec-timeout", "Codex I5", "kano-gbos-watch.py",
     [("capture_output=True, text=True, timeout=timeout,", "capture_output=True, text=True,")],
     ["test_watch.Watch.test_hung_ec_port_times_out"]),
    ("watch-first-names-only", "Codex I4 (watcher)", "kano-gbos-watch.py",
     [("for m in sorted(modules - have)]", "for m in sorted(set(sorted(modules)[:3]) - have)]")],
     ["test_watch.Watch.test_a_later_blacklist_entry_missing_cuts_power"]),
    ("watch-any-module-blacklist", "Codex I4 (watcher)", "kano-gbos-watch.py",
     [('have = set(mb[-1].split(",")) if mb else set()', 'have = set(",".join(mb).split(",")) if mb else set()')],
     ["test_watch.Watch.test_only_the_last_module_blacklist_counts"]),
    ("watch-spi-intel-option", "Codex I4 (watcher)", "kano-gbos-watch.py",
     [('    out += [f"forbidden option {t}" for t in tokens if t.startswith("spi-intel.")]\n', "")],
     ["test_watch.Watch.test_spi_intel_option_cuts_power"]),
    ("watch-nvme-init-on-nvme", "Codex I4 (watcher)", "kano-gbos-watch.py",
     [('    if profile == "nvme" and "nvme_init" in ic:', '    if False:')], ["test_watch.Watch.test_nvme_profile"]),
    ("watch-guard-source-fail-open", "Codex I4 (watcher)", "kano-gbos-watch.py",
     [('        bad(f"cannot read the expected command line guards from {GUARD_SOURCE}: {e}")', "        guards = (set(), [], [])")],
     ["test_watch.Watch.test_unreadable_guard_source_does_not_start"]),
    ("watch-window-end-success", "Codex I7 / Opus m1", "kano-gbos-watch.py",
     [("    if not verified:\n        print(f\"UNOBSERVABLE: the", "    if False:\n        print(f\"UNOBSERVABLE: the")],
     ["test_watch.Watch.test_window_shorter_than_the_observe_timeout_is_unobservable"]),
    ("watch-durations", "Codex I7", "kano-gbos-watch.py",
     [('if not math.isfinite(v) or v < 0 or (v == 0 and name != "camera_every"):', "if False:")],
     ["test_watch.Watch.test_invalid_durations_do_not_start"]),

    ("hardcoded-sibling-path", "Opus M12", "kano-boot.sh",
     [("K=$HERE/kano-ser.py", "K=~/kano-tools/kano-ser.py")],
     ["test_env.NoHardcodedConstants.test_siblings_are_called_from_the_scripts_own_directory"]),
    ("hardcoded-ip-default", "Opus M3", "kano-ssh.sh",
     [('"root@$KANO_IP"', '"root@${KANO_IP:-192.0.2.123}"')],
     ["test_env.NoHardcodedConstants.test_no_ip_address_literals_in_scripts"]),

    # personal-data split: the planted values come from the private files at run time (see the docstring)
    ("personal-word", "personal-data split / Opus I2", "kano-tone-find.py",
     [("the development host's mic", lambda: private_literal() and f"the {private_literal()} mic")],
     ["test_no_personal_data.NoPersonalData.test_no_real_values_outside_the_local_file"]),
    ("personal-lan-address", "personal-data split / Opus I2", "kano-ui.sh",
     [('S="-s $KANO_ADB_ADDR"', lambda: private_adb_addr() and 'S="-s ${KANO_ADB_ADDR:-' + private_adb_addr() + '}"')],
     ["test_no_personal_data.NoPersonalData.test_no_real_values_outside_the_local_file"]),
    ("ui-android-identity", "Opus m14", "kano-ui.sh",
     [("""[ -n "$(timeout 30 $ADB $S shell getprop "$KANO_ANDROID_ID_PROP" 2> /dev/null | tr -d '\\r\\n')" ] ||""", "true ||")],
     ["test_ui.Ui.test_another_android_gets_nothing"]),
    ("env-local-file-not-read-by-shell", "personal-data split", "kano-env.sh",
     [('if [ -n "${HERE:-}" ] && [ -f "$HERE/kano-env.local.sh" ]; then . "$HERE/kano-env.local.sh"; fi', ":")],
     ["test_env.Settings.test_local_file_overrides_the_defaults"]),
    ("env-local-file-read-last-by-python", "personal-data split", "kano-ser.py",
     [('for name in ("kano-env.local.sh", "kano-env.sh"):', 'for name in ("kano-env.sh", "kano-env.local.sh"):')],
     ["test_env.Settings.test_local_file_overrides_the_defaults"]),
    ("env-require-shell", "personal-data split", "kano-env.sh",
     [("      exit 2\n", "      :\n")],
     ["test_unconfigured.Unconfigured.test_kano_ssh", "test_unconfigured.Unconfigured.test_nvme_update",
      "test_unconfigured.Unconfigured.test_kano_ui", "test_unconfigured.Unconfigured.test_kano_boot"]),
    ("env-require-python", "personal-data split", "kano-delta-push.py",
     [("            sys.exit(2)\n", "            pass\n")],
     ["test_unconfigured.Unconfigured.test_empty_stick_serial_never_matches_a_disk_without_serial",
      "test_unconfigured.Unconfigured.test_delta_push_stick"]),
    ("env-require-python+empty-serial-check", "personal-data split", "kano-delta-push.py",
     [("            sys.exit(2)\n", "            pass\n"),
      ('if not os.environ.get("KANO_STICK_SERIAL") or f.get', "if f.get")],
     ["test_unconfigured.Unconfigured.test_empty_stick_serial_never_matches_a_disk_without_serial"]),
    ("ser-require-tty", "personal-data split", "kano-ser.py",
     [('f"kano-env.local.sh or the environment (see README.md)", file=sys.stderr)\n            sys.exit(2)\n',
       'f"kano-env.local.sh or the environment (see README.md)", file=sys.stderr)\n            pass\n')],
     ["test_unconfigured.Unconfigured.test_kano_ser"]),
    ("boot-menu-ambiguity", "boot menu titles", "kano-ser.py",
     [("    if len(hits) > 1:", "    if False:")],
     ["test_boot_menu.Parse.test_pick", "test_boot_menu.Boot.test_ambiguous_title_selects_nothing"]),
    ("boot-menu-fixed-pattern", "boot menu titles", "kano-ser.py",
     [("        return [t for t in titles if t], status",
       '        return [t for t in titles if t and re.search("Googlebook KANO|KANO maintenance|Reboot Into Firmware", t)], status')],
     ["test_boot_menu.Parse.test_every_drawn_title_in_screen_order",
      "test_boot_menu.Boot.test_titles_outside_any_fixed_pattern_count"]),
    ("boot-menu-from-log-start", "boot menu titles", "kano-ser.py",
     [(r'        start = data.rfind(b"\x1b[2J", 0, m.start())', "        start = 0")],
     ["test_boot_menu.Parse.test_every_drawn_title_in_screen_order"]),
    ("boot-menu-any-boot-in", "Codex M3", "kano-ser.py",
     [('COUNTDOWN = re.compile(rb"Boot in [0-9]+ ?s\\.")', 'COUNTDOWN = re.compile(rb"Boot in")'),
      ('        if rows.get(status, b"").strip() != m.group(0):\n            continue\n', "")],
     ["test_boot_menu.Parse.test_a_title_containing_boot_in_does_not_end_the_menu",
      "test_boot_menu.Boot.test_a_title_containing_boot_in_cannot_hide_an_ambiguity"]),
    ("boot-editor-any-output", "Opus I4", "kano-ser.py",
     [('    return any(OPTION.search(m.group(1)) for m in edit.finditer(new.replace(b"\\0", b"")))', "    return bool(new)")],
     ["test_boot_menu.Parse.test_editor_detection"]),
    ("boot-append-without-editor", "Opus I4", "kano-boot.sh",
     [("""  python3 "$K" editor "$L" "$off" 10 || { echo "the menu editor did not open (loader.conf 'editor no'?); nothing typed, stopped"; exit 3; }""",
       "  sleep 1")],
     ["test_boot_menu.Boot.test_append_without_an_open_editor_types_nothing"]),
    ("boot-log-truncate-unchecked", "Codex M3", "kano-boot.sh",
     [(""": > "$L" || { echo "cannot empty the AP UART log $L; stopped before power-on"; exit 3; }""", """: > "$L\"""")],
     ["test_boot_menu.Boot.test_unwritable_log_stops_before_power_on"]),
    ("watch-require-ec-tty", "personal-data split", "kano-gbos-watch.py",
     [("        sys.exit(5)\n", "        pass\n")],
     ["test_unconfigured.Unconfigured.test_watchers_do_not_start_without_a_way_to_cut_power"]),
]


def run_one(m, work, classify=False):
    mid, finding, name, edits, tests = m
    edits = [(old, new() if callable(new) else new) for old, new in edits]
    if any(new is None for _, new in edits):
        return mid, finding, "skipped", "private data not present here (public export)", 0
    top = Path(tempfile.mkdtemp(prefix=f"mut-{mid}-", dir=work))
    copy = top / "tools" / "harness"
    shutil.copytree(HARNESS, copy)
    (top / "builder").symlink_to(REPO / "builder")
    (top / "profiles").symlink_to(REPO / "profiles")
    f = copy / name
    text = f.read_text()
    for old, new in edits:
        if text.count(old) != 1:
            shutil.rmtree(top, True)
            return mid, finding, "BAD-MUTATION", f"{old!r} occurs {text.count(old)} times in {name}", 0
        text = text.replace(old, new)
    f.write_text(text)
    env = dict(os.environ, KANO_HARNESS_DIR=str(copy), PYTHONDONTWRITEBYTECODE="1")
    env.pop("HARNESS_BEHAVIOUR_ONLY", None)
    t = time.time()

    def red(extra):
        try:
            r = subprocess.run([sys.executable, "-m", "unittest", *tests], cwd=TESTS, env=dict(env, **extra),
                               capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
            return r.returncode != 0, (r.stderr.strip().splitlines() or [""])[-1]
        except subprocess.TimeoutExpired:
            return True, "tests timed out (counts as red)"
    try:
        killed, detail = red({})
        verdict = "killed" if killed else "SURVIVED"
        if killed and classify:
            verdict = "killed:effect" if red({"HARNESS_BEHAVIOUR_ONLY": "1"})[0] else "killed:message"
    finally:
        shutil.rmtree(top, True)
    return mid, finding, verdict, detail, time.time() - t


def main(argv):
    jobs, classify = 4, False
    if argv[:1] == ["-j"]:
        jobs, argv = int(argv[1]), argv[2:]
    if argv[:1] == ["--classify"]:
        classify, argv = True, argv[1:]
    todo = [m for m in MUTATIONS if not argv or m[0] in argv]
    work = Path(tempfile.mkdtemp(prefix="kano-harness-mutants-"))
    bad = skipped = 0
    try:
        with ThreadPoolExecutor(jobs) as ex:
            for mid, finding, verdict, detail, secs in ex.map(lambda m: run_one(m, work, classify), todo):
                print(f"{verdict:15} {mid:38} {finding:27} {secs:5.0f}s  {detail}", flush=True)
                bad += not verdict.startswith(("killed", "skipped"))
                skipped += verdict == "skipped"
    finally:
        shutil.rmtree(work, True)
    print(f"{len(todo) - bad - skipped}/{len(todo) - skipped} mutations killed" + (f", {skipped} skipped" if skipped else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
