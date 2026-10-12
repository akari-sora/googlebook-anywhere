# Harness review fixes: result

Scope: `tools/harness/*` (KANO developer harness) and the new `tests/harness/`. Reviews: `review1/review-codex.md`
(C1, C2, C3, I1, I2, I5) and `review1/review-opus.md` (I1, I2, I10, Minor 1, 2, 3, 12). Nothing outside the owned
files was edited. No real ssh, adb, serial port or disk was touched. No commit.

## Files

| File | Change |
|---|---|
| `tools/harness/kano-env.sh` (new) | All settings as `: "${NAME:=default}"` lines, exported. Superseded by the personal-data split below: generic defaults, real values in `kano-env.local.sh` |
| `tools/harness/kano-ser.py` | EC allowlist (pure `check_ec_command`), refusal before any port is opened (exit 2), lazy pyserial, arg validation for ap-keys/ap-type/ap-spam/seconds, docstring scope note, "(Claude, …)" removed |
| `tools/harness/kano-delta-push.py` | Remote facts (serial, TRAN, size, raw GPT with CRC checks, kernel partition table, mounts, swaps, holders) + pure `check_stick`; per-write identity check in the same remote command as each `dd`; no `assert`; shared helpers |
| `tools/harness/kano-delta-push-nvme.py` | Uses those helpers; `check_nvme`: serial, not root disk, maintenance OS = root on the Kingston stick, nothing mounted/swap/held, allowed partitions identical in GPT and kernel, no overlap; per-write identity check |
| `tools/harness/kano-nvme-install.sh` | Fresh run dir per run; each step one `set -euo pipefail` remote script that starts with the guards; pass = ssh exit 0 AND the marker line is in this run's output (echoed last under `set -e`; matched as a whole line, not as the last line, since ssh stderr may arrive after it); GPT head/tail backup + sha256 before discard; f2fs rw mount before resize |
| `tools/harness/kano-nvme-update.sh` | Stale artifacts deleted, run.log rotated; every step's status checked; boot_a GUID + name read from the NVMe and compared before the push; adb only `KANO_ADB_ADDR`, `reboot -p` only if `KANO_ANDROID_ID_PROP` is set there; remote scripts guarded + strict; transfer/ESP/watcher failures fail the run |
| `tools/harness/kano-gbos-watch.py` | One implementation (`--profile usb\|nvme`); "unobservable" exit 4; this-boot baseline; async camera grab with timeout; EC timeout + `powerinfo` confirmation (exit 6 if unconfirmed); limits documented |
| `tools/harness/kano-gbos-watch-nvme.py` | Thin `exec` wrapper (`--profile nvme`); docstring name fixed |
| `tools/harness/kano-boot.sh`, `kano-ssh.sh`, `kano-ui.sh` | Source `kano-env.sh` from their own dir; siblings via own dir; EC call status checked in kano-boot.sh |
| `tools/harness/README.md` | Env file, every guard (and what it does not cover), known limits, tests |
| `tests/harness/run.sh`, `mutate.py`, `helpers.py`, `test_*.py` (7 modules), `fakes/` | Test harness (below) |

## Finding → change → test

| Finding | Change | Tests |
|---|---|---|
| Codex C1, Opus I1 (install decides from cumulative log, guards can be stale) | per-run dir; status + marker from this run's file only; guards in every remote script, in the same invocation as `blkdiscard`/`dd`; fail-stop | `test_nvme_install`: stale markers + REFUSE, guard ssh failure + stale markers, guard output without marker, target flips right before discard, mounted / held / wrong PCI / root on NVMe / wrong entry |
| Codex I1 (install: no pipefail, unchecked steps) | remote `set -euo pipefail`; `sed -n 1,6p` instead of `head`; every step checked | `test_copy_failure_stops_before_the_gpt_rewrite`, `test_resize_failure_inside_a_pipeline_stops_the_install`, `test_happy_path` |
| Codex I2 (no original GPT backup) | step 2: first + last MiB to the run dir, size + sha256 vs remote | `test_backup_ssh_failure`, `test_backup_truncated`, `test_backup_corrupted`, `test_happy_path` (backup equals pre-install NVMe) |
| Codex C2, Opus Minor 1 (EC blacklist bypass) | allowlist, control/non-ASCII refusal before the port, exact case-insensitive names, arg shapes | `test_ser`: 14 allowed, 42 refused (incl. all listed cases), every proper prefix refused, CLI exit 2 without loading pyserial, `ec powerbtn 2` caller form, normalized bytes on the wire |
| Codex C3, Opus I2 (delta push: no serial/GPT check, asserts) | see delta-push rows above; `test_real_layout` runs the pure checks on the real 31-entry lapis GPT (no #11, 1-sector KERN-C/ROOT-C/RWFW-A/B) | `test_delta_push`: 11 stick + 12 NVMe cases (wrong serial, not USB, GPT mismatch, kernel table mismatch, mounted, held, swap, root disk, not maintenance OS, foreign partition overlap, facts failure, identity flip before write, happy paths, dry run) + no-`assert` check |
| Codex I1, Opus I10 (update: stale artifacts, unchecked steps, GUID constant) | see update row above | `test_nvme_update`: stale artifacts + failing step, dry-run refusal, GUID mismatch, p13 not boot_a, transfer failure, ESP result mismatch (and unmount), watcher failure (after a full successful push + ESP rewrite) |
| Opus Minor 2 (/24 scan + `adb reboot -p`) | scan removed; fixed address; identity property before `reboot -p` | `test_adb_only_the_fixed_address_and_only_this_projects_android` |
| Codex I5, Opus I4/Minor 12 (watcher) | unobservable (no this-boot cmdline in 120 s, or log silent 30 s inside that window), camera/EC timeouts, EC reply check, docs, name fix | `test_watch`: 12 cases |
| Opus Minor 3, 12 (constants, sibling paths) | `kano-env.sh`; own-dir siblings; Python parses the same file | `test_env`: shell == Python values (two override sets), defaults unchanged, no hardcoded IP/serial/path/key in owned scripts, no `~/`/`$T/`, ssh only via `kano-ssh.sh` with BatchMode |

## Running the tests

```
tests/harness/run.sh                 # syntax + 78 unit/integration tests + 47 mutations (~8 min on an M-series Mac)
tests/harness/run.sh --no-mutation   # ~4 min
python3 tests/harness/mutate.py --classify   # effect vs message for every mutation
```

How the fake works: `fakes/ssh` (first on PATH; `kano-ssh.sh` execs `ssh`) runs the remote command with the local
bash, PATH limited to stub tools (`fakes/remote.py`: lsblk, findmnt, dd, blkdiscard, sgdisk, blockdev, mount, cmp,
sha256sum, …) plus a few harmless real tools, and `/proc /sys /mnt /root /etc` literals rewritten into a temp
"fake root". `/dev/sda` and `/dev/nvme0n1` are disk-image files with real GPTs. So the guard code itself runs, and
the stubs log what was actually executed (`destructive.log`), separately from what was sent (`ssh.jsonl`). Fault
rules simulate ssh failure, truncated/corrupted output and a target that changes right before a write. HOME is a
temp dir and KANO_IP/KANO_ADB_ADDR are TEST-NET addresses. The remote scripts therefore also run under macOS bash
3.2, stricter than the KANO's bash 5.

Final run (macOS 27, `/bin/bash` 3.2.57, Python 3.14.4), from the repo root:

```
$ /bin/bash tests/harness/run.sh
== syntax: bash -n (GNU bash, version 3.2.57(1)-release (arm64-apple-darwin26)) and sh -n
ok    bash -n tools/harness/kano-boot.sh
ok    bash -n tools/harness/kano-env.sh
ok    bash -n tools/harness/kano-maint-kbdlight.sh
ok    bash -n tools/harness/kano-maint-loopback.sh
ok    bash -n tools/harness/kano-maint-lpss-probe.sh
ok    bash -n tools/harness/kano-nvme-install.sh
ok    bash -n tools/harness/kano-nvme-update.sh
ok    bash -n tools/harness/kano-ssh.sh
ok    bash -n tools/harness/kano-ui.sh
ok    bash -n tests/harness/run.sh
ok    sh -n tools/harness/kano-ssh.sh
ok    sh -n tools/harness/kano-env.sh
== syntax: python3 -m py_compile (Python 3.14.4), bytecode under /var/folders/9w/6m1x9qr10zv2p2sztxyyxyq00000gn/T//kano-harness-run.zJXVoc
ok    tools/harness/kano-delta-push-nvme.py
ok    tools/harness/kano-delta-push.py
ok    tools/harness/kano-gbos-watch-nvme.py
ok    tools/harness/kano-gbos-watch.py
ok    tools/harness/kano-ser.py
ok    tools/harness/kano-tone-find.py
ok    tests/harness/helpers.py
ok    tests/harness/mutate.py
ok    tests/harness/test_delta_push.py
ok    tests/harness/test_env.py
ok    tests/harness/test_nvme_install.py
ok    tests/harness/test_nvme_update.py
ok    tests/harness/test_real_layout.py
ok    tests/harness/test_ser.py
ok    tests/harness/test_watch.py
ok    tests/harness/fakes/remote.py
ok    tests/harness/fakes/ssh
ok    tests/harness/fakes/adb
ok    tests/harness/fakes/pyserial/serial/__init__.py

== unit and fake-KANO integration tests
... (78 test lines, all "ok")
Ran 78 tests in 228.772s
OK
ok    unit and integration tests

== mutation check: each guard reverted in a temp copy of the harness must turn its tests red
... (47 lines "killed ...", listed below)
47/47 mutations killed
ok    mutation check

ALL HARNESS TESTS PASSED
$ echo $?
0
```

Wall time is about 8 minutes (unit/integration 229 s, mutations about 4 min with 4 in parallel). The unit suite also
passes with `HARNESS_BEHAVIOUR_ONLY=1` (78 OK). In an earlier full run, `test_hung_camera_grab_does_not_stall_the_watch`
failed once under load: the log writer raced the watcher's start. The writer now waits until the watcher is up, and
the watcher suite then passed twice concurrently (12/12 each).

## Mutation check

`tests/harness/mutate.py` applies each mutation to a fresh temp copy of `tools/harness` (never in place), runs the
named tests with `KANO_HARNESS_DIR` pointing at the copy, and requires them to fail. Sanity checks: a no-op mutation
is reported SURVIVED, and sampled kills fail for the intended reason (e.g. without the guard in `03-copy` the fake
records `DD_OK` on a disk whose serial no longer matches).

`mutate.py --classify` (all 47; `run.sh` runs the same list without classification):

| Mutation (temp copy) | Finding | Result |
|---|---|---|
| `ser-control-chars` | Codex C2 | killed:message |
| `ser-non-ascii` | Codex C2 | killed:message |
| `ser-allowlist` | Codex C2 / Opus M1 | killed:effect |
| `ser-argument-shapes` | Codex C2 | killed:effect |
| `ser-prefix-match` | Opus M1 | killed:effect |
| `ser-eager-pyserial` | lazy import | killed:effect |
| `stick-serial` | Codex C3 / Opus I2 | killed:effect |
| `stick-usb` | Codex C3 | killed:effect |
| `stick-gpt-compare` | Codex C3 / Opus I2 | killed:message |
| `stick-gpt-compare+allowed-set` | Codex C3 / Opus I2 | killed:effect |
| `stick-kernel-table` | Codex C3 | killed:effect |
| `stick-busy-overlap` | Codex C3 | killed:effect |
| `foreign-partition-overlap` | Codex C3 | killed:effect |
| `stick-idcheck-per-write` | Codex C3 | killed:effect |
| `nvme-serial` | Codex C3 | killed:effect |
| `nvme-root-disk` | Codex C3 | killed:message |
| `nvme-root-disk+busy+maint-os` | Codex C3 | killed:effect |
| `nvme-maintenance-os` | Codex C3 | killed:effect |
| `nvme-busy` | Codex C3 | killed:effect |
| `nvme-bounds` | Codex C3 | killed:effect |
| `nvme-kernel-table` | Codex C3 | killed:effect |
| `nvme-idcheck-per-write` | Codex C3 | killed:effect |
| `install-decide-from-old-logs` | Codex C1 / Opus I1 | killed:effect |
| `install-guard-neutered` | Codex C1 / Opus I1 | killed:effect |
| `install-idle-guard` | Codex C1 | killed:effect |
| `install-guard-not-in-destructive-call` | Codex C1 (same invocation) | killed:effect |
| `install-backup-sha` | Codex I2 | killed:effect |
| `install-backup-unchecked` | Codex I2 | killed:effect |
| `install-step-exit-status` | Codex I1 | killed:message |
| `install-remote-pipefail` | Codex I1 | killed:effect |
| `update-stale-artifacts` | Opus I10 | killed:effect |
| `update-guid-from-disk` | Codex I1 | killed:effect |
| `update-boot-a-name` | Codex I1 | killed:effect |
| `update-adb-identity` | Opus M2 | killed:effect |
| `update-transfer-status` | Codex I1 | killed:message |
| `update-transfer-status+remote-sha` | Codex I1 | killed:effect |
| `update-esp-result-check` | Codex I1 | killed:effect |
| `update-watcher-status` | Codex I1 / I5 | killed:message |
| `update-watcher-status+boot-check` | Codex I1 / I5 | killed:effect |
| `watch-this-boot` | Codex I5 | killed:effect |
| `watch-no-cmdline` | Codex I5 | killed:effect |
| `watch-stall` | Codex I5 | killed:effect |
| `watch-camera-timeout` | Codex I5 / Opus M12 | killed:effect |
| `watch-ec-confirm` | Codex I5 | killed:effect |
| `watch-ec-timeout` | Codex I5 | killed:effect |
| `hardcoded-sibling-path` | Opus M12 | killed:effect |
| `hardcoded-constant` | Opus M3 | killed:effect |

47/47 killed; 40 effect, 7 message.

Layered guards. Some guards are backed by a second layer, so reverting just one layer leaves the tests green by
design. The table lists those cases; the mutations above revert the pair, or kill on the specific refusal reason.

Observed, not assumed. The single-layer mutations in the table that classify as `message`:

| Reverted alone | Layer that still prevents the effect | Pair/triple that kills by effect |
|---|---|---|
| `ser-control-chars`, `ser-non-ascii` | allowlist + argument shapes (a token with CR/LF/TAB/NUL or non-ASCII never matches) | `ser-allowlist`, `ser-argument-shapes` |
| `stick-gpt-compare` | overlap check: writes may only touch partitions identical in image and stick GPT | `stick-gpt-compare+allowed-set` |
| `nvme-root-disk` | maintenance-OS check (/ must be on the Kingston stick) and the mounted check | `nvme-root-disk+busy+maint-os` (then only the per-write remote check stops the dd; a write command was already sent) |
| `install-step-exit-status` | marker must be in this run's output | `install-decide-from-old-logs` |
| `update-transfer-status` | remote sha256 check of the transferred bootconfig | `update-transfer-status+remote-sha` |
| `update-watcher-status` | in that test Android never boots, so the boot_completed check still fails the run | `update-watcher-status+boot-check` |

The following one-off single-layer runs were not added to `mutate.py`:
- install, marker check pointed at the old logs (exit status still checked): **survived**. The exit-status check stops
  it.
- install, backup size check alone: killed only by message (the sha256 check stops it).
- install, backup step exit status alone: killed only by message (the size check stops it).
- update, stale-file deletion alone: killed, because the test asserts the stale files are gone. No push either way.
- update, initrd-prep exit status alone: killed only by message (`[ -s ... ]` fails because the stale files were
  deleted).
- stick push, facts-level serial check alone, measured on the identity-flip test: survived there. The per-write check
  stops it. On the wrong-serial test, `stick-serial` kills by effect: a `dd of=` is sent.

## Notes

- The `powerinfo` reply format (`power state N = G3|S5`) comes from Chromium EC `power/common.c` and is not confirmed
  on this EC. If it differs, the watcher reports "power cut not confirmed" (exit 6). It never falsely confirms.
- `--stall-timeout` defaults to 30 s, applied inside the 120 s observe window only. This is a judgment call: a slow
  kernel + initrd load from the stick is the likeliest false trip. It is tunable, and the update script then fails
  loudly, never silently.
- The tests run the scripts with `bash` from PATH, and the fake runs remote scripts with that bash too. Here that is
  `/bin/bash` 3.2.57. On a Mac with Homebrew bash first on PATH, the 3.2 coverage is lost.
- In-repo callers of `kano-ser.py ec` only use `apshutdown` and `powerbtn`. `kblight <n>` (used by hand earlier) is
  now refused.
- The pure checks were verified on the real lapis GPT (`test_real_layout`). The kernel's view of the real stick
  (`/sys/block/sda/sdaN`) was not observed. The kernel-table check is strict: if the kernel hid a 1-sector partition,
  the push would refuse rather than write.

The sections above describe the first round. The final state, with 104 tests and 59 mutations, is in **Personal-data split** at the end.

## Not done / limits

- **Not run on Linux.** No local Linux was available, and the LAN machines are off limits for this task. The test
  code avoids bash-4 features and GNU-only flags: fakes are Python, `base64 | tr -d '\n'`, `wc -c | tr -d ' '`, and
  `timeout` gets a shim if it is missing. It was run on macOS 27 with /bin/bash 3.2.57 and Python 3.14.
- **No real-device run** of the changed scripts.
- **ssh host key still not pinned** (`StrictHostKeyChecking=no`). The maintenance OS changes its key on every reflash,
  so pinning is not "simple". The protection against a wrong target is the remote serial and layout checks.
  Documented in README.
- **Maintenance-OS identity** = "/ is on a USB disk with `KANO_STICK_SERIAL`". The image has no stronger marker.
  **Android identity** = `init.svc.kano_bootfix` is set (a service only this project's vendor image defines).
  `ro.product.device` / serial values for the KANO are not known.
- (Resolved by the personal-data split below: no personal default remains in `kano-env.sh`.)
- Builder ↔ harness path mismatch (Opus I4: harness reads `kano-final<N>/…`) not changed. Builder is out of scope.
- `kano-nvme-update.sh` step 6 is only tested on the watcher-failure path. The power-button path has ~45 s of sleeps
  and was left untested.
- Behaviour changes callers may notice:
  - `kano-ser.py` refusals exit 2 (was 1). `ec kblight <n>` is now refused.
  - Watcher exit codes 4/6 are new. A watcher started after the kernel printed its command line now reports
    unobservable after 120 s.
  - `kano-nvme-install.sh` now mounts userdata rw once before `resize.f2fs`. This step comes from the real install's
    notes and is new procedure.

## Personal-data split

Goal: the public export ("googlebook-anywhere") carries no personal data. The private repo keeps the developer's
values in one file that the export drops.

**Settings.** Each value comes from the first non-empty source, in this order:

1. the environment;
2. `tools/harness/kano-env.local.sh` (optional);
3. `tools/harness/kano-env.sh` (generic defaults).

Both the shell scripts and the Python parsers (`kano-ser.py`, `kano-delta-push.py`) read in that order.
`kano-env.sh` has no default for any identity: `KANO_IP`, `KANO_SSH_KEY`, `KANO_ADB_ADDR`, `KANO_CR50_SERIAL`,
`KANO_AP_TTY`, `KANO_EC_TTY`, `KANO_STICK_SERIAL`, `KANO_NVME_SERIAL`, `KANO_NVME_PCI`, `KANO_NVME_BOOT_A_GUID`. Its
directories are neutral (`$HOME/kano-harness/runs`, `…/work`), `KANO_ADB` is `adb`, and `KANO_PYSERIAL_ROOT` is empty,
which means the system's pyserial. `kano-env.local.sh` holds the former defaults, including the development host's
paths.

**Refusals.** Every script that needs a missing value stops with `refusing: <NAME> is not set …` (exit 2; the watcher
uses exit 5) before any ssh, adb, serial or disk action:

| Script | Required |
|---|---|
| `kano-ssh.sh` | IP, key |
| `kano-delta-push*.py` | serials, checked alphanumeric (dry runs need nothing) |
| `kano-nvme-install.sh`, `kano-nvme-update.sh` | all identities they use |
| `kano-ser.py` | the TTY of the port it would open |
| `kano-boot.sh` | both TTYs |
| `kano-ui.sh` | adb address |
| `kano-gbos-watch*.py` | EC TTY (without it, it could not cut the power) |

An empty expected serial is also rejected inside the checks themselves (`check_stick`, `check_nvme`, remote guards).
Without that, `"" == ""` would accept a disk that reports no serial.

**Boot menu (coordinator item).** `kano-boot.sh` no longer greps a fixed pattern. `kano-ser.py menu <log> <want>`
(pure functions `menu_titles`, `pick_entry`) parses the AP UART log like a terminal:

- The menu draw starts at the clear screen (`ESC[2J`; failing that, the first cursor positioning) and ends at the first
  "Boot in".
- Text is collected per screen row from `ESC[row;colH`, so every drawn title counts, in screen order.
- A substring must match exactly one title. If it matches several, the script exits 6 after stopping the countdown and
  selects nothing; no match exits 5. `=<title>` matches a title exactly.
- `kano-boot.sh` also checks every key press, no longer needs bash 4 (`mapfile`) or GNU sed, and prints a usage line.

The fake menu in the tests follows the edk2 serial terminal output format. It has not been checked against a real
capture from this firmware. If the real draw differs, `kano-boot.sh` refuses (exit 4/5/6) rather than pressing keys
by a wrong index.

**Files changed**

- `tools/harness`:
  - `kano-env.sh`: rewritten.
  - `kano-env.local.sh`: new, private.
  - Also changed: `kano-ser.py`, `kano-delta-push.py`, `kano-delta-push-nvme.py`, `kano-nvme-install.sh`,
    `kano-nvme-update.sh`, `kano-gbos-watch.py`, `kano-ssh.sh`, `kano-boot.sh`, `kano-ui.sh`, `kano-tone-find.py`
    ("development host"), `README.md` (no host name; settings section documents `kano-env.local.sh`).
- `tests/harness`:
  - New: `test_unconfigured.py` (a), `test_no_personal_data.py` (c), `test_boot_menu.py`.
  - Rewritten: `test_env.py` (b).
  - `helpers.py`: a fake value for every setting (`TESTSTICK0001`, `TESTNVME0001`, `TESTCR50-0001`,
    `00000000-0000-4000-8000-000000000001`, PCI `0x1234:0x5678`, TEST-NET `192.0.2.x`, paths under a temp HOME); a
    test checks that no setting is missing. Also `bare_env` and `harness_copy`, which copies the harness without the
    local file.
  - Also changed: `test_nvme_install.py`, `test_nvme_update.py`, `test_delta_push.py`; `fakes/pyserial` (an EC
    `powerbtn` makes the fake firmware draw a menu); `mutate.py` (12 new mutations; the hardcoded-value mutations now
    use neutral or split strings); `run.sh`.

**Tests added**

- **(a) `test_unconfigured`:** works on a harness copy without a local file and with every `KANO_*` empty. Each of
  `kano-ssh.sh`, both delta pushes, install, update, `kano-ser.py` (ec, ap-keys), `kano-boot.sh`, `kano-ui.sh` and both
  watchers refuses. The fakes record no ssh, no adb, no serial open and no write. Dry runs still work. A stick or NVMe
  that reports no serial is not accepted when the expected serial is empty.
- **(b) `test_env.Settings`:**
  - Shell and both Python parsers agree for no local file, a fake local file, and the environment on top.
  - A local value overrides the default and can use a name set earlier in the same file.
  - The environment overrides both.
  - The developer's real local file only uses known names and leaves no identity empty. It is checked without printing
    values; the test skips if the file is absent.
- **(c) `test_no_personal_data`:** scans every file under `tools/harness` and `tests/harness` except
  `kano-env.local.sh`. Patterns are sha256 digests of the values' alphanumeric runs, so the test holds no values. It
  catches any spelling: case, and `.`/`-`/`_` separators. The patterns cover the stick, NVMe and Cr50 serials, the boot
  GUID, the private LAN prefix, the key-name suffix, the developer's project tree, host name and user name. Positive
  controls show the matcher finds a fake value. It flagged my own split string in `mutate.py` once; that string is now
  split inside the numbers.

**Final output** (macOS 27, `/bin/bash` 3.2.57, Python 3.14.4):

```
$ /bin/bash tests/harness/run.sh
== syntax: bash -n (GNU bash, version 3.2.57(1)-release (arm64-apple-darwin26)) and sh -n
... "ok" for bash -n on 11 shell files (incl. kano-env.local.sh), sh -n on 2, py_compile on 22 Python files
== unit and fake-KANO integration tests
Ran 104 tests in 326.497s
OK
== mutation check: each guard reverted in a temp copy of the harness must turn its tests red
... 59 x "killed"
59/59 mutations killed
ALL HARNESS TESTS PASSED
$ echo $?
0
```

`mutate.py --classify` on the 18 new or changed mutations: all are `killed:effect`. These are the 12 personal-data and
boot-menu mutations plus the 6 whose guard text changed. The 7 `killed:message` single-layer mutations listed above
are unchanged.

**For the export**

- Drop `tools/harness/kano-env.local.sh`.
- Export a fresh tree, not the history: commit `3b32739` and the first round of fixes still hold the real values in
  the harness files.
- `kano-maint-*.sh` (not owned) are scanned too and are clean.
