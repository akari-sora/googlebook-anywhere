# write-usb.sh round-2 fixes

Files changed: `install/write-usb.sh`, `tests/install/run.sh` (this file is new). Nothing committed.

## Changes mapped to findings

| Finding | Change in `install/write-usb.sh` |
|---|---|
| Codex C1 / Opus C1 (confirmation not bound to the disk that gets written) | `identity()` records the target before the prompt: what the argument resolves to (`readlink -e`), `[ -b ]`, major:minor from `stat`, `/sys/block/<name>/dev` (must equal it), sysfs device path, size, logical sector size, TYPE, TRAN and full SERIAL. After the answer, the script reads the identity again and reruns `not_root_disk` and `in_use` (mounts, swap, holders, O_EXCL probe). Any difference refuses and prints both records. If a query fails, the script refuses. The write is done by an embedded Python program (`wusb_py`, run as `python3 -I -`). It never uses `O_CREAT`. It runs `lstat` first and opens the disk `O_WRONLY|O_EXCL|O_NOFOLLOW` (EBUSY means refuse). `fstat` must show a block device with the recorded rdev. `/sys/dev/block/M:m` must still resolve to the recorded sysfs device. `lseek(SEEK_END)` must equal the recorded size and `BLKSSZGET` must be 512. It writes 16 MiB chunks with `os.write` loops, fsyncs every 256 MiB and again at the end, and hashes what it wrote against the `.sha256`. It then calls `BLKFLSBUF`, opens a second `O_RDONLY` fd with the same rdev check, calls `POSIX_FADV_DONTNEED`, and reads the range back in 16 MiB chunks, compares each chunk with the image (reporting the first differing byte) and checks the sha256. It keeps the exclusive fd open until the comparison is done, and prints progress. Exit 2 means it refused before the first byte (the shell then says nothing was written); exit 1 means it failed after writing started. |
| Opus I4 (fail-open lsblk, MOUNTPOINTS needs util-linux 2.37 or later, ZFS/btrfs/other namespaces, `--check-only` broken for users) | Every `lsblk`/`findmnt`/sysfs query captures its exit status and refuses on failure. `lsq` also refuses a multi-line answer. Mount detection uses `lsblk -nrp -o NAME,MAJ:MIN,MOUNTPOINT` (the disk, its partitions and anything stacked on them) and the full `findmnt -nrv -o SOURCE` table. `/proc/swaps` is matched by major:minor. The first lsblk line must be the confirmed node and rdev. The O_EXCL probe (`wusb_py probe`) runs before the prompt and again after it. Size comes from `/sys/block/<name>/size` × 512 and `blockdev` is no longer used. With `--check-only` as a normal user, only the O_EXCL probe is skipped, and the script says so. |
| Codex I3 (write-usb part) | `sgdisk -e` now runs after the read-back, and its exit status is checked. The identity is read again first. `sgdisk -v` must print `No problems found`. That string comes from the gdisk source; I could not run sgdisk on Linux to confirm it. PASS is printed only after that. If sgdisk is missing, `-e` fails, `-v` fails or reports problems, or the disk's identity changed, it prints "image content written and verified, but … the backup GPT was NOT confirmed at the end of the disk" with the fix command and exits 3. A missing sgdisk is also warned about before the prompt. |
| Codex I9 | The script refuses `queue/logical_block_size` ≠ 512 before the prompt and again in the re-check. The writer also checks it on the open fd (`BLKSSZGET`). |
| Kept | Usage line (now an explicit usage message with exit 2), whole disk only, USB transport only, root-disk check (`findmnt -nv` + `lsblk -s`, matched by major:minor; a root that is not on a disk node gets a note and is left to the probe), size ≥ image, image sha256 vs `.sha256` before the prompt, typed serial, `--check-only`, and the "run as root" message that `tests/builder/run.sh` greps for. New: the image must be whole 512-byte sectors. |

Test hook: `GBOS_WUSB_TEST_SYSFS=<dir>` replaces `/sys`. It is honoured only with `--check-only`; without it the script refuses before the root check, and a test covers that. It is documented in the header of `write-usb.sh`. `--check-only` deliberately runs the same after-prompt re-check (without the prompt), so the identity change test can exercise it without root and without a writable device.

## Tests (`tests/install/run.sh`, Opus m15 write-usb part)

Part 1 runs on any OS that has python3. It extracts the writer from between the `# >>> / # <<< write-usb.py` markers, so the shipped code is what gets tested. The writer and probe must refuse:
- a regular file, a symlink to one and `/dev/null`, leaving the file unchanged;
- a missing path and a path in a missing directory, without creating anything;
- an image larger than the disk, a malformed sha256 and a bad command line.

On Linux they must also refuse a real block node whose major:minor is not the confirmed one; this happens at `lstat`, before any open.

Part 2 is Linux only and uses `--check-only` with `lsblk`/`findmnt` stubs. The fake sysfs is built on the major:minor of an unattached `/dev/loopN` (size 0). As root, if no such node exists, it falls back to `losetup -f`, which only finds or creates a free node and attaches nothing. Without root on a host where snaps hold every loop node, those cases are SKIPped and reported. The cases:
- **Kept:** accept; non-USB; partition; no serial; mounted; smaller than the image; character device; root disk (real `/sys`); sha256 mismatch.
- **lsblk fails:** SERIAL, TRAN, and the partition/mount query.
- **findmnt fails:** the source of `/`, and the mount table.
- **Mounted, seen only by findmnt.**
- **Holders:** on a partition, on the disk, and holders directory missing.
- **Logical sector 4096.**
- **Sysfs/node disagreement:** the sysfs `dev` disagrees with the node, or the size cannot be read.
- **Identity change between rounds:** the SERIAL changes after the first query, so the script refuses in the re-check, prints "serial OTHER456", and there is no `CHECKS OK` or `== writing`.
- **Mount appears between rounds:** a mount shows up on the second query, so the script refuses in the re-check.
- **Sysfs hook without `--check-only`:** refused.
- **Skipped probe reported:** as a non-root user, the skip of the root-only probe is reported.

Nothing opens a block device for writing.

## What I ran (macOS, no Linux available)

- `bash -n` on both files: OK. `shellcheck -S warning` on both files and on the two stubs extracted from `run.sh`: clean. `install/write-usb.sh` is also clean at all severities; `run.sh` has only SC2015 info notes, on `ok`/`bad`, which always return 0.
- `python3 -m py_compile` on the extracted writer: OK.
- `bash tests/install/run.sh` on macOS: 13 passed, 0 failed, 1 skipped (Linux part).
- A local simulation that I did not ship ran the Linux part on macOS with bash 3.2: 45 passed, 0 failed. It used GNU coreutils from Homebrew as `stat`/`readlink`/`sha256sum`, fake "real" lsblk/findmnt, a fake `/proc/swaps` and real-sysfs directory, and the read-only node of `/dev/disk19` as the pretend disk, which is only stat'ed. I also ran 7 mutations, each removing one guard: the identity comparison, lsblk failure handling, the sector check, holders, findmnt failure handling, the in-use re-check, and the MOUNTPOINT check. Each one turned at least one test red.
- A local harness that I did not ship checked the writer's write/read-back logic against a regular file by stubbing S_ISBLK, O_EXCL and the ioctls. The happy path wrote and compared correctly. A sector corrupted after it was written was caught, with exit 1 and the exact byte offset. A size, sysfs path or rdev mismatch was refused with exit 2 and the target untouched. A sha256 that does not match the image ended in exit 1.

Not verified (needs Linux and real hardware): the real util-linux output formats, O_EXCL EBUSY on a mounted USB disk, BLKFLSBUF/fadvise on a real device, and `sgdisk -e`/`-v` on a written stick, including the exact `No problems found` text that `sgdisk -v` prints. On Linux, run `bash tests/install/run.sh` as a normal user and once as root (as root the probe really opens the unattached loop device read-only).

Note for the README owner: `README.md:51` could add that gdisk (`sgdisk`) is required for PASS and that only 512-byte-sector disks are accepted. I did not edit it.

# Round 3 (2026-10-13)

Files changed: `install/write-usb.sh`, `install/docker-install.sh`, `install/install.sh`, `install/kano-check.sh`,
`tests/install/run.sh`, `tests/install/kano-check-test.sh`; new `tests/install/gpt-test.py`, `tests/install/install-sh-test.sh`.

- **write-usb.sh, backup GPT (Codex I1).** The writer moves the backup GPT itself, through the same `O_EXCL`
  descriptor that wrote and read back the image. It does what `sgdisk -e` does: the backup entries and header go to
  the end of the disk; the primary header gets the new backup LBA and last usable LBA; the protective MBR size is
  updated; the old backup is left in place. It then drops the cache and reads all of it back.
  - The image's protective MBR and both GPTs are checked before the prompt (`gptcheck`) and again before the first
    write. A damaged image GPT is refused with nothing written.
  - `sgdisk` is now only an independent, read-only `sgdisk -v` at the end. It must be installed before the prompt:
    without it the run is refused and nothing is written.
  - Compared with `sgdisk -e`, the result is byte for byte the same on a sparse 9 → 10 GiB test image. On a reflink copy
    of the real 19 GB image grown to 32 GB, the MBR, both GPTs and the old backup all match.
- **write-usb.sh, sibling `.not-self-tested` (Opus m8).** If `<image>.not-self-tested` exists, the script warns before the
  prompt, quotes that file's first line, and asks you to type `untested` before the serial. The installer stick writes this marker because that build does not boot the image.
- **docker-install.sh.**
  - Empty argument arrays work under `set -u` with the bash 3.2 that macOS ships (Opus I5). The old script failed with
    `CARGS[*]: unbound variable`; the new test reproduces that failure on the old file.
  - The container also gets `/dev/kvm`'s group through `--group-add` (Codex I10, Opus m2).
  - The root refusal and the Docker check now happen before any `mkdir`.
  - Paths containing `:` are refused.
  - `--no-selftest` is documented, and it now also skips passing the KVM device.
- **install.sh, reuse (Codex I5, Opus m7).**
  - `build/params` records a content digest of `builder/` and the profile, covering uncommitted and untracked files and
    excluding Markdown. It also records every setting `builder/build.sh` reads: `GBOS_DEFAULT_ENTRY`,
    `GBOS_LOADER_EDITOR`, `GBOS_IMAGE_NAME`, `GBOS_QEMU_SECONDS`, the firmware, audio and profile paths, and the drift
    flag.
  - With `GBOS_ALLOW_FIRMWARE_DRIFT=1` a build is never reused.
  - The base records a digest of `base/`, and its files are checked against `SHA256SUMS` before reuse. That check is
    cached by size, mtime and inode, like the zip.
  - A refusal prints the differing lines.
  - `--zip`, `--key` and `--adb-pubkey` must be existing files when the arguments are parsed.
  - The header no longer calls the base bootable.
- **kano-check.sh (Codex M5, Opus m9).**
  - GO only for stepping 3 or 4; steppings 0–2 are STOP.
  - An unknown stepping, a CPU that is not 6/154, an unreadable `/proc/cpuinfo` or unidentified firmware is UNKNOWN
    (exit 2), never GO.
  - The GO line says it is not a guarantee.
  - The tests now check each expected line separately.

Tests: `tests/install/run.sh` (Linux: 53 as a user + 1 SKIP for the root-only loop part; 57 as root, 0 skipped),
`tests/install/install-sh-test.sh` (Linux user: 50; macOS: the 19 Docker cases with bash 3.2), `tests/install/kano-check-test.sh` 15.
A CI container without loop devices now lists every pretend-disk case it could not run as its own SKIP line and prints
a NOTE with the count.
