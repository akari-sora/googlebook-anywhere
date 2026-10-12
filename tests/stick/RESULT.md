# Stick installer: test results

## Current status (2026-10-13)

- `tests/stick/selftest.py`: **64/64** on macOS and on Linux (43 earlier checks + 21 for round 3: `check-cmdline`,
  setup-stick's `blacklist()`, and the installer's `in_use` on fake `/proc`, `/sys` and `lsblk`).
- `tests/stick/qemu-install.py` on Linux + KVM with the dumps of the real installer stick (its first and last MiB, its
  XBOOTLDR, its ext4 maintenance root with the Arch 7.2.3-arch1-3 kernel) and the image and pristine data the stick
  holds, running the current installer: **71/71, `STICK INSTALLER TEST OK (71/71)`, exit 0 (was 64/64; +7 checks: module-blacklist.txt injection in the read-back, the nomodule case 4 checks, the runtime command-line line in cancel and both installs). NVMe-only boots: install-flex wr_bytes 149476352, highest write 73776439296; install-blank 149742592 / 73776447488; userdata 19069403136-127999983104**. Cases: nolockdown, noinitcall, nomodule (refused, zero
  NVMe writes), super-mismatch (refused before the prompt), cancel, install over a Flex-like NVMe, install on a blank
  NVMe, each install then booted from the NVMe alone (Android writes /data inside userdata).
- Real KANO (2026-10-13): the installer entry booted with lockdown, `efi=noruntime`, the TPM/nvram initcalls blocked and
  no TPM or nvram device; the installer's checks passed and a wrong answer cancelled with nothing written; the internal
  disk booted again afterwards. That run used the installer from before the round-3 changes (no whole-`module_blacklist`
  check yet). Details in `install/stick/README.md` (實機驗證). **The full install onto the real internal NVMe has
  not been run; it is verified in QEMU only.**

## Round 3 (review3): changes

| Finding | Change | Covered by |
|---|---|---|
| Codex I4 (installer part): the installer did not check the whole `module_blacklist` at runtime | setup-stick.sh writes the entry's whole `module_blacklist` (its own `blacklist()` of the template, after checking it covers the image's entries and the built-in mapping) to `/usr/local/share/gbos-install/module-blacklist.txt`. gbos-install.sh step 1 runs the verifier's `check-cmdline`: exactly one `module_blacklist=` (the kernel keeps the last), containing every listed module by exact name; every mapped initcall in the `initcall_blacklist=` tokens; `efi=noruntime`; none of the listed modules in `/proc/modules`. Missing list file, empty list, list without the mapped modules -> refused. Device check extended to `/dev/cros_ec`, `/dev/mtd*`, `/dev/spidev*` | selftest (11 `check-cmdline` cases + setup-stick's `blacklist()`), QEMU `nomodule` (entry without `spi_intel_pci`: refused, zero writes) and the runtime-check line in cancel and both installs |
| Codex I3 (stick part): busy checks treated a failed query as "not in use" | `in_use` refuses when `/sys/block/<disk>` is missing or its `dev` is not the major:minor recorded at selection, when `lsblk` fails, and when awk on `/proc/mounts`/`/proc/swaps` exits with anything but 0 (in use) or 1 (not in use). The post-confirmation identity check also compares major:minor | selftest (9 `in_use` cases on fake files) |
| Opus m3: setup-stick could pair the old `/gbos-nvme` kernel/initrd with the new image's options | gbos-nvme*.conf are rewritten only when `/gbos-nvme/kernel.efi` is the image's and `/gbos-nvme/initrd.img` is the image's with its own boot_a GUID (checked with the verifier's `bootconfig-check`/`bootconfig-set`); otherwise they are left as they are and setup-stick says so | a run of that block on the real stick's XBOOTLDR files (same -> rewrite; one byte of initrd or kernel changed, or kernel missing -> left) |
| README `<pending>` | Replaced by the real-KANO evidence and the QEMU-only scope of the full install | - |

Not changed: holding an exclusive claim on the target during the write phase (Codex I3's last sentence). The installer
re-reads the partition table (`partx -u` / `BLKRRPART`) between writes, and `BLKRRPART` fails with `EBUSY` while another
process holds the disk with `O_EXCL`; the identity and busy checks run right before the first write instead.

## Round 2

Scope: `install/stick/*` and `tests/stick/*`. Reviews: `review2/review-codex.md` (I1, I2, I3 installer part, I8
qemu-install part, I9 installer part, M3 qemu-install loop) and `review2/review-opus.md` (I1, I2, I5, m9, m10 loader.conf
parts, m12, m13 qemu-install, m16 stick README). Nothing outside these files was edited (`builder/image.py`'s own
loader.conf, the top-level README and docs are other owners'). No disk, LAN device or QEMU touched; no commit.

(When round 2 was written, `qemu-install.py` had not run yet; see "Current status" above for its results.)

## Files

| File | Change |
|---|---|
| `install/stick/gbos-install.sh` | Rewritten flow: lockdown + `efi=noruntime` gate; full pre-write verification before the serial prompt; `set -Eeuo pipefail` + ERR trap into `die`; tee log; read-back after every write; final whole-result verification; stick menu default |
| `install/stick/gbos-install.entry.template` (new) | The XBOOTLDR installer entry, single source for setup-stick.sh and the QEMU test |
| `install/stick/setup-stick.sh` | Renders the template; built-in-module and blacklist-superset checks; reuses the installer's verifier; conditional menu default; `auto-firmware no`; read-backs |
| `install/stick/make-pristine.py` | Validates the image's MBR/GPT CRCs and backup position, refuses names the manifest cannot carry, `zstd -t` on every output, manifest written atomically |
| `install/stick/README.md` | New order of checks, what the entry blocks and what it does not, setup and test notes, no "same blocks" claim, no JOURNAL pointer |
| `install/stick/gbos-installer.service` | Unchanged |
| `tests/stick/qemu-install.py` | Rewritten: injects the current installer with debugfs, five cases, QMP zero-write checks, full layout/boot-file comparison |
| `tests/stick/gptlib.py` (new) | GPT writer/reader with CRC checks and a bootconfig patcher, written separately from the installer's code |
| `tests/stick/selftest.py` (new) | Runs make-pristine.py and the installer's embedded verifier on good and damaged synthetic data (no QEMU, root or disk) |

## Finding -> change

| Finding | Change | Covered by |
|---|---|---|
| Codex I2, Opus I1: head digest never checked; super/pristine checked only after wiping | Before the serial prompt: manifest format (fields, kinds, unique, no overlap), `head.img.zst` length + SHA-256 + protective MBR + header CRC + entries CRC + backup at image end + every entry equal to the manifest; every pristine file `zstd -t` + decompressed length and SHA-256; the stick's super SHA-256 (message: several minutes); the image ESP's initrd bootconfig and entries parsed. The verified head is written from that copy. A bad stick never asks for the serial. | selftest (11 damaged-data cases), QEMU `super-mismatch` |
| Opus I1: post-write hash may read page cache | Writes `oflag=direct conv=fsync`; `devsha` = `blockdev --flushbufs` + `dd iflag=direct`; ESP/XBOOTLDR read back with mtools after `--flushbufs` | QEMU install cases |
| Codex I3, Opus I2: no ERR trap, `die` exits 0, no log of raw errors | `set -Eeuo pipefail`, `inherit_errexit`, `trap ... ERR` -> `die "command failed (exit N) at line L: CMD"`; `die` inside `$(...)` only reports and exits 1; `finish <code> <action> <msg>`; `die` exits 1; `exec > >(tee -a /var/log/gbos-install-*.log)`; EXIT trap unmounts; with WROTE=1: "Installation stopped. The internal disk is incomplete and will not start; run the installer again." | bash -n, shellcheck; QEMU refusal cases |
| Codex I3: success printed without checking `sgdisk -v`; ESP files not read back | Before INSTALL OK: both GPT copies (CRCs, backup at the last LBA, identical entries), every partition start/end/type/name/attrs vs manifest, userdata from the new start to the last usable LBA, all GUIDs new and unique and none shared with the stick, boot_a GUID = initrd's; `sgdisk -v` exit 0 and text `No problems found`; /data first 16 MiB zero (direct read); ESP initrd (cmp + bootconfig), kernel, three entries, exact loader.conf; XBOOTLDR kernel/initrd = NVMe ESP's, entries reference `/gbos-nvme/*`; stick loader.conf | selftest verify-target (11 damaged layouts), QEMU |
| userdata attrs lost by `sgdisk -d/-n` (needed for "attrs as in the manifest") | `sgdisk -A N:set:bit` for each bit of the manifest's attrs | QEMU, selftest |
| Codex I9: no logical sector size check | Stick and target must report 512 in `queue/logical_block_size`, before the prompt | code only (no 4Kn case in QEMU) |
| Codex I1, Opus m12: maintenance entry weaker than the Android ones; README "same blocks" | Template: `module_blacklist` = builder/image.py `BL` (incl. cros_ec_proto) + mei_me, 8250_dw, intel_lpss_pci, intel_lpss, intel_lpss_acpi, i915, tpm_crb; nvme allowed; `efi=noruntime`; `lockdown=integrity`. setup-stick.sh refuses if any name is in `/usr/lib/modules/<XBOOTLDR kernel version>/modules.builtin` (version from the identical `/usr/lib/modules/*/vmlinuz`, else the bzImage header) or if an image entry blocks a module the template does not; checks `/sys/kernel/security/lockdown` when running that kernel. gbos-install.sh refuses unless lockdown is `[integrity]`/`[confidentiality]` and `/proc/cmdline` has `efi=noruntime`. README says what this does and does not cover. | QEMU static checks + `nolockdown` case |
| Opus m10 (loader.conf parts) | `auto-firmware no` in every loader.conf this code writes (NVMe ESP, stick ESP in both scripts) | QEMU exact compare |
| Opus m9: default `gbos-nvme.conf` set when it does not exist | setup-stick.sh sets it only when the entry exists, else keeps the current default; gbos-install sets it after it creates the entry (so its final message stays true) | QEMU install cases |
| Codex I8, Opus I5/m13 (qemu-install) | (a) current installer, service, enable link, pristine dir written into a copy of the ext4 maint root with `debugfs -w` (e2fsck -fp before, read back by content/mode/owner, e2fsck -fn after), XBOOTLDR entry rendered from the template, old `gbos-nvme*` removed; (b) cancel: QMP `wr_bytes`/`wr_operations`/`unmap_operations` = 0 before poweroff + `st_blocks` = 0; (c) `install-flex`: 12-partition Flex-like GPT with data where the image goes; (d) `super-mismatch`: one byte of the stick's super flipped -> refused before the prompt, zero writes, NVMe fingerprint unchanged, byte restored; (e) start/end/type/name/attrs of every partition, GUIDs, XBOOTLDR kernel/initrd/entries, userdata lower and upper bound in the NVMe-only boot; (f) all hash loops fail on short reads; (g) `ok   kernel lockdown:` line required, plus a `nolockdown` case that must be refused | Linux host |
| Codex M3, Opus m13 | `gptlib.sha` and the fingerprint raise on EOF | selftest |
| Opus m16 (stick README) | JOURNAL pointer removed; test usage with its 7 arguments | - |

## How to run

```sh
# anywhere with python3 + zstd (no root, no QEMU)
tests/stick/selftest.py /tmp/stick-selftest            # optional 2nd arg: a raw disk image with a GPT made by another tool

# Linux host with KVM, OVMF, mtools, e2fsprogs (no root)
install/stick/make-pristine.py googlebook-kano.img pristine/        # if not done yet
tests/stick/qemu-install.py <work dir> googlebook-kano.img stick-head.bin stick-tail.bin \
    xbootldr.img kano-maint.img pristine/
# stick-head.bin / stick-tail.bin: first / last MiB of the real stick; xbootldr.img / kano-maint.img: dd of those partitions
```

The QEMU test needs room for the stick copy and two installed 128 GB sparse NVMe images (each ~14 GB written); the NVMe
images are deleted after their case unless `GBOS_STICK_KEEP=1`. Serial logs: `<work dir>/<case>-serial.log`.
Expected end: `STICK INSTALLER TEST OK (n/n)`.

## What I ran (macOS, no Linux/QEMU available)

- `bash -n` and `shellcheck -s bash -S warning` on `gbos-install.sh` and `setup-stick.sh`: clean.
- `py_compile` (PYTHONPYCACHEPREFIX outside the repo) on `qemu-install.py`, `gptlib.py`, `selftest.py`,
  `make-pristine.py` and the Python block embedded in `gbos-install.sh`: clean.
- `tests/stick/selftest.py`: **40/40 PASS**, including a GPT written by macOS `hdiutil` (GPTSPUD layout, converted to raw)
  that both `gptlib` and the installer's `check-gpt` accept, and a copy with one flipped byte in its backup header that
  `gptlib` refuses — an independent check of the CRC offsets.
- The awk extraction setup-stick.sh uses to load the installer's verifier yields exactly the block the selftest runs.

Not run: anything that needs Linux (`gbos-install.sh`/`setup-stick.sh` end to end, mtools, sgdisk, debugfs, QEMU).

## Open points for the Linux run

- If the Arch kernel builds any blocked driver in (TPM is a candidate: `CONFIG_TCG_TPM`/`CONFIG_TCG_TIS*` may be `=y`),
  the QEMU static check fails and setup-stick.sh refuses. The fix is then an `initcall_blacklist` entry or another
  kernel, not removing the check.
- `lockdown=integrity` makes the kernel refuse unsigned modules. If the maintenance kernel's modules are not signed, the
  installer boot cannot load usb-storage/nvme/vfat and the install cases fail early; the serial logs will show it.
- `debugfs` paths assume `/usr/local/sbin` (a symlink to `bin` on current Arch is handled via `<dir>/.`).
- Not covered by the QEMU test: a 4Kn target (refused by code only), ESP files other than initrd/kernel/entries/loader.conf.

## Follow-up: built-in TPM and nvram in the maintenance kernel

Data from the KANO (Arch 7.2.3-arch1-3, from the coordinator): modules are signed and the lockdown LSM is on, so
`lockdown=integrity` works and the earlier "unsigned modules" open point is gone. `tpm`, `tpm_tis`, `tpm_tis_core`,
`tpm_crb` and `nvram` are built in, so the built-in check refused as written.

| Change | Where |
|---|---|
| New `install/stick/builtin-initcalls.txt`: built-in module -> initcall (`tpm_init`, `init_tis`, `crb_acpi_driver_init`, `nvram_module_init`, plus `mei_me_driver_init`); `tpm_tis_core  -  tpm_init,init_tis,crb_acpi_driver_init` (library, no initcall, accepted only while those are blocked) | single source |
| Template: `initcall_blacklist=@INITCALL_BLACKLIST@`, rendered from the mapping in file order | template, setup-stick.sh, qemu-install.py |
| setup-stick.sh refuses a blocked built-in module the mapping does not cover, a bad mapping line, a `-` row whose initcalls are not all listed, or a mapped module missing from `module_blacklist`; installs the mapping as `/usr/local/share/gbos-install/builtin-initcalls.txt` | setup-stick.sh |
| gbos-install.sh step 1 (with lockdown/efi=noruntime): every mapped initcall on `/proc/cmdline`, nothing in `/sys/class/tpm`, no `/dev/tpm0`, `/dev/tpmrm0`, `/dev/nvram`; logs `ok   built-in drivers blocked (...)` | gbos-install.sh |
| QEMU test: mapping injected and read back; static check mapping-aware; new `noinitcall` case (must be refused, zero writes); cancel and both installs require the `ok   built-in drivers blocked` line | qemu-install.py |
| README: built-ins blocked by `initcall_blacklist`, checked at runtime; "Verified on the KANO: <pending>" | README.md |

Ran: `selftest.py` **45/45** (5 new checks: shell and Python parse the mapping the same way, coverage of the TPM/nvram
rows, rendered entry, `module_blacklist` ⊇ `builder/image.py` BL and the mapped modules, line length). The runtime check
was exercised in bash with substituted paths (good command line, initcalls split over two tokens, missing initcalls,
`/dev/nvram` present, a `/sys/class/tpm` entry, near-miss names), and the setup-stick coverage logic with the KANO's
built-in list (passes; refuses a removed `nvram` row, a `-` row needing an unlisted initcall, a bad line, a mapped module
outside `module_blacklist`). `bash -n`, shellcheck `-S warning`, `py_compile`: clean. QEMU still not run. In QEMU there
is no TPM device, so the TPM part of the runtime check passes trivially there; `/dev/nvram` is a real test.
