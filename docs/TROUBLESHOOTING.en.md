# Troubleshooting (Googlebook Anywhere)

This document describes known boot and usage problems in a
**symptom → likely cause → what to do** format. It covers only items with a
basis in the inputs; anything **not yet verified on real hardware** is marked
"untested on hardware".

For firmware flashing, writing the USB, and safety trade-offs, see `FIRMWARE.md`,
`SAFETY.md`, and `HOW-IT-WORKS.md`. For which machines can be installed, see
`COMPATIBILITY.md`. To report a new machine, see `NEW-DEVICE.md`.

> 中文版：`TROUBLESHOOTING.md`.

## Boot problems

### Black or frozen screen after selecting a boot entry (KANO, SKUs that are not `46a6`)

- **Symptom**: On a KANO, the boot menu appears and loading starts, but then only
  a black or frozen screen remains; the power LED may be on.
- **Likely cause**: The image you used **allowed only a single iGPU ID**
  (`xe.force_probe=46a6`). The iGPUs of other KANO SKUs (`46a8`, `46b3`, `a7a1`,
  `a7a9`, …) were rejected by `xe_pci_probe()` with `-ENODEV`, and Googlebook OS
  (lapis) ships no fallback DRM/fbdev driver, so there was no picture. Audio still
  initialized in the background; you just could not see anything.
- **What to do**:
  - This is why **v0.2.0 worked on only the one `46a6` machine**.
  - **Since v0.2.1, `xe.force_probe` covers 12 iGPU PCI IDs** (ADL-P/U and RPL-U) and is expected to fix this, but each SKU is still "untested on hardware". These SKUs were also untested in v0.3.0; see the [v0.3.1 verification record](VERIFICATION-v0.3.1.md) for later evidence.
  - To find which SKU / iGPU your machine is, see the KANO family table in
    `COMPATIBILITY.md`.

### First boot is very slow, or a "not responding" dialog appears

- **Symptom**: The first boot from the USB stick takes several minutes, and a
  "not responding / no response" dialog may appear.
- **Likely cause**: The USB stick is slow; the first boot does more
  initialization.
- **What to do**:
  - A slow stick taking **several minutes on first boot is normal**; wait.
  - When the "not responding" dialog appears, choose **"Wait"**, not end.
  - A faster USB 3.0 stick improves this noticeably.

### First-boot setup wizard loops on a system update

- **Symptom**: The first-boot setup wizard asks for Wi-Fi. After the network connects it finds a system update, starts the download, then returns to the same step, and can repeat. This was reported on the v0.2.3 KANO image (issue #6, ASUS CX34).
- **Likely cause**: Google's OTA payload passes signature checks (`/system/etc/security/otacerts.zip` is unchanged), but, inferred and not verified on hardware, these images cannot finish that update: a delta checks the source blocks it reads, and vendor is always modified (system and system_ext too when the IFMA patch is on); a full payload has to switch the boot slot through the blocked boot-control / crossystem path, and the boot menu does not follow Google's boot-control slot switch. The server still offers the same update, so the wizard tries again. A Google OTA and the project's updater (`install/gbos-update.py`) would compete for the inactive slot.
- **What to do**:
  - **v0.3.0 adds a stop action, with one stopped-state readback on hardware.** Vendor carries `etc/init/gbos-no-ota.rc`: right after `update_engine.rc` enables `update_engine` on the same trigger (`ro.boot.slot_suffix=*`), init runs `stop update_engine`. QEMU verified that the vendor action ran and `update_engine` never started. After the 2026-10-29 KANO keep-data update, one readback at 65 minutes of uptime showed `stopped`; this establishes the service state at that time only. See the [v0.3.0 verification record](VERIFICATION-v0.3.0.md).
  - v0.3.0 images carry `gbos-no-ota.rc`, but this does not yet prove the loop is resolved on hardware. On v0.2.3 or older, the default image has no adb (adb exists only when the build was given a public key), with no known adb-less workaround.
  - Not verified on hardware: that `update_engine` never runs at all on a device, that the wizard then finishes, and how GMS reacts. QEMU has no network, so it cannot show Google actually offering an update.

### Boot fails, or some programs crash after boot (CPU too old)

- **Symptom**: It fails almost right after the menu, panics, or some apps crash as soon as they open.
- **Likely cause**: **The CPU is too old.** Many lapis binaries and libraries emit AVX2, GFNI, AVX-VNNI and SHA-NI
  instructions without runtime CPU dispatch; **when an unsupported instruction is actually executed**, the CPU raises `#UD` and the program gets SIGILL (for example, first-stage `/init` contains SHA-NI instructions, and if that path runs on a CPU without SHA-NI, PID 1 gets SIGILL and the kernel panics).
- **What to do**:
  - Only Intel 12th gen (KANO) has been verified on hardware. Intel CPUs of 11th gen and older are expected to fail
    (no AVX-VNNI; the 6th–10th gen Skylake family and older Core CPUs also lack SHA-NI, which, per the third-party gbOS86 notes,
    `/init` executes on the first boot).
  - AMD Zen 4 and older are expected to fail (no VEX-encoded AVX-VNNI); Zen 5 (HX 370) is the target but has not run
    on hardware yet.
  - Images with `patch_ifma` enabled precisely patch 17 host targets (the original 16 plus `system_ext_a/bin/trusty_security_vm_launcher`). If an individual program still gets SIGILL on KANO or Zen 5, include logcat, the program name, and build-info. Static scanning is not runtime or hardware proof, and a non-protected Microdroid path can still reach the retained guest `microdroid_manager` instruction.
  - If the IFMA scanner stops a build, inspect the reported partition, typed container chain, SHA-256, offset, and encoding. A missing or changed candidate pin, an extra raw VEX candidate, a recognized but unopenable or over-depth nested container, or unexplained nonzero bytes in a concatenated initramfs is refused. Disassembly that classifies bytes as part of another instruction does not remove the candidate from the report. The scanner recognizes xz, gzip, zstd, lz4, zip, newc, EROFS, ext4, Android sparse, AVB0, and bzImage through the union of known magic and image suffixes (including a decoded payload's inner suffix after stripping trailing kind markers and one matching `.gz`/`.gzip`, `.xz`, `.zst`/`.zstd`, or `.lz4` suffix per decoding layer, falling back to stripping one generic compression suffix when the outer filename extension does not match the actual compression magic), and a match through either route must open completely; matching Linux `lib/decompress_inflate.c:__gunzip`, gzip is decoded via header flags and raw deflate (including flushing buffered output at input EOF for `>64 KiB` streams without an 8-byte trailer) while skipping the 8-byte trailer without failing on CRC/ISIZE mismatch or a missing/truncated trailer after a valid `Z_STREAM_END` (raw deflate corruption remains a hard error); legacy LZ4 (`0x184C2102`) is decoded like Linux `lib/decompress_unlz4.c:unlz4()` (continuing the same stream on any subsequent `0x184C2102` magic, ending at the chunk boundary on a trailing `<4`-byte fragment or `0`-size chunk—including an empty stream of just the 4-byte magic or `magic + 00000000` producing `0` bytes—regardless of whether any chunk was decoded, handing any `<4`-byte trailing fragment and subsequent bytes to the outer initramfs loop so all-NUL is padding and any non-NUL byte is refused with an unpinnable `non-zero initramfs tail` `ScanError`; streaming chunks `<=8,421,520` B (`LZ4_compressBound(8 MiB)`) directly to a scratch file and decoding them in a single `lz4 -dc` batch pass—including valid multi-chunk streams whose total decoded output exceeds `8 MiB`, which is safe without a pre-batch fast-path check because the `lz4` CLI's legacy decoder (`programs/lz4io.c:LZ4IO_decodeLegacyStream`) enforces the `8 MiB` (`LEGACY_BLOCKSIZE`) per-chunk output cap on each chunk itself while the scanner's post-batch `batch_size <= len(chunks) * 8 MiB` (`LEGACY_LZ4_BLOCK_MAX_OUTPUT`) check is only a defense-in-depth sanity bound—and bisecting in `O(log N)` subprocess calls on batch failure to isolate the first failing chunk; for a zero-gap next member immediately after at least one decoded chunk, bounding the legacy stream and scanning the next member when the 4-byte little-endian header exceeds `8,421,520` (`cpio-newc`, `xz`, `zstd`, modern `lz4` frame `0x184D2204`, or `gzip` with `FLG != 0`), whereas a zero-gap `gzip` with `FLG == 0`—whose header `1f 8b 08 00` decodes as little-endian size `559,903 <= 8,421,520`—is decoded as an LZ4 chunk when at least `559,903` bytes follow and fails closed as an unpinnable hard `ScanError` like the kernel, and is scanned as a `gzip` member only when the declared `559,903`-byte block is truncated, as a conservative fail-closed superset of the kernel's in-memory `unlz4()` out-of-bounds read residual; and scanning earlier decoded chunks before stopping with an unpinnable hard `ScanError` on any other `>8,421,520` B size or truncated/corrupt chunk, or when a `0`-output failed legacy LZ4 stream is followed by any non-NUL byte); `zstd`, modern LZ4 frames (`0x184D2204`), and legacy LZ4 subprocesses stream CLI output over a pipe with background `stderr` draining and a scaled timeout of `30s + 1s per 4 MiB` of input (keeping the watchdog armed across `stdout` reading, `stderr` thread draining, and `wait()` until the child has exited and been reaped, without false timeouts when the child exits right at the deadline), and on a `zstd` or modern LZ4 frame failure the scanner frames complete non-final block prefixes with a clean end marker in `_write_terminated_block_prefix` (always bisecting on the return code `returncode == 0` of the end-marked prefix to locate the first failing block, without depending on `stderr` wording; for `zstd`, `_write_terminated_block_prefix` clears `Single_Segment_flag`, `Frame_Content_Size`, and the content checksum flag and, only when the original frame is single-segment, sets a `128 MiB` window byte `0x88` (other frames keep their original `Window_Descriptor`), so `>128 MiB` single-segment frames whose later blocks reference history beyond `128 MiB` may salvage fewer blocks as a residual while the failed frame stays an unpinnable hard `ScanError`) and also probes the raw prefix through the first failing block to keep the longest decodable prefix, and any compressed frame or newc member that produces `> 0` decoded bytes before failing has its partial decoded output scanned first and then stops with an unpinnable hard `ScanError`, while any modern `zstd`/`lz4` frame (top level, nested, or initramfs tail) that fails to decode for any reason, including a content checksum or content size error, is an unpinnable hard `ScanError` whatever the decoder wrote: bytes it did write are scanned for the error message only (naming IFMA hits), a diagnostic scan that stops early on any exception (including `LZMAError`/`zlib.error`/`EOFError` from a ZIP member) is named in the error and never turned into acceptance, and no unsupported pin applies; a modern `zstd`/`lz4` decoder that is missing or cannot start is the same unpinnable hard `ScanError` (only legacy LZ4 without an `lz4` tool stays an `lz4 unavailable` unsupported image that needs an exact pin); an undecodable ZIP/APEX member is recorded as damaged (bytes already copied out of it are scanned first), never stops the scan of the members after it, and then stops the build with an error no pin can accept (only a failed `gzip`/`xz` frame that produces `0` decoded bytes or a failed legacy LZ4 chunk call that wrote no bytes can take `ImageOpenError`; a failed legacy LZ4 call that did write bytes has them scanned first and then stops with an unpinnable hard `ScanError`); furthermore, in direct initramfs-member context (`vendor_ramdisk_entry` or `boot_ramdisk` roots, or a nearest file node whose leaf name is lower-cased first and the compression suffix is stripped after that by `_is_initramfs_filename` (`.gz`/`.gzip`/`.xz`/`.zst`/`.zstd`/`.lz4`, `.cpio`, and `.img`), then matches the `INITRAMFS_FILE_PREFIXES` `ramdisk*`/`initrd*`/`microdroid_initrd*`/`vendor_ramdisk*`, and its `initramfs_tail`/`sparse_payload` descendants—never applied partition-wide across `boot_*`/`init_boot_*`/`vendor_boot_*`/`standalone_initrd_*` to files like `kernel`/`Image.gz`, and never applied to compressed regular files inside a `cpio_member` such as `.ko.xz`, `.ko.gz`, firmware `.bin.zst`/`.bin.lz4`, or `.txt.gz`; known real names outside this filename heuristic such as `microdroid_crashdump_initrd.img` and AOSP `microdroid_gki-*_initrd_*.img` only lack the `Reset` defense-in-depth check while every byte is still decoded and scanned), any compressed member whose decoded output does not end in a Linux `init/initramfs.c:unpack_to_rootfs()` `Reset` state as modeled by `_decoded_is_cpio_reset_sequence`—namely `0` bytes (for an empty `gzip`, `xz`, `zstd`, modern `lz4`, or legacy `lz4` member) or all-NUL padding (both accepted at any member position by the scanner as a scanning superset, whereas the kernel accepts them only after a previous member already ended in `Reset` and stops with an error on the first member, extracting nothing more), or one or more complete 4-byte-aligned `cpio-newc` entries (with or without `TRAILER!!!`, requiring 4-byte alignment before every entry to match kernel `do_reset()` `"broken padding"`, while refusing a `TRAILER!!!` entry with a non-zero body, which is stricter than the kernel skipping the body via `SkipIt` to `Reset`) plus trailing NUL padding—has its decoded output scanned first and then stops with an unpinnable hard `ScanError` (trailing `#BOOTCONFIG\n` stripping applies only to outer objects, never to the decoded payload of a compressed member). The kernel `decompress_method()` 2-byte compressed magics (bzip2 `42 5a`, LZMA-alone `5d 00`, LZO `89 4c`, legacy gzip `1f 9e`) plus Android boot `ANDROID!` and vendor boot `VNDRBOOT` apply to every file and ZIP member after leading NULs (taking precedence over later container checks) and are explicitly unsupported (recording `unsupported <kind> at 0x...` as the reason), proceeding only with an exact unsupported pin (currently only AVB0 `microdroid_vbmeta.img`; `0` 2-byte magic hits on the pinned base)—add an exact unsupported pin when a new base's ordinary data starts with one of those 2-byte prefixes after leading NULs. Initramfs input follows Linux kernel syntax: every compressed frame and newc archive is parsed, including top-level and in-member NUL padding before a 4-byte-aligned newc archive (where a complete entry sequence without `TRAILER!!!` is also accepted when ending at EOF, before NUL padding, or before a compressed member) and any number of leading NULs before a supported compressed frame (recording `initramfs_tail` `offset:0x...` in `container_chain`). The following refusals are unpinnable hard `ScanError`s: (1) an unbounded/truncated stream (`length is None`) at offset 0 (`position == 0`) or after a prior member (`saw_member`), or at `position > 0` with `not saw_member` when the bytes after the frame header are not all NUL, (2) any failed frame after a real member (`saw_member`), (3) non-NUL bytes after a failed frame when those bytes are not a valid following initramfs member, (4) a corrupt `cpio-newc` archive (at offset 0 or after aligned NULs), and (5) any frame that decoded `> 0` bytes before failing (or any modern `zstd`/`lz4` frame that failed to decode, or any compressed member in direct initramfs-member context whose decoded output does not end in a `cpio-newc` `Reset` state); only when `not saw_member` and `0` decoded bytes were produced (and the failed frame is not a modern `zstd`/`lz4` frame, i.e. it is `gzip`, `xz`, or legacy LZ4) can a failed frame take the `ImageOpenError` unsupported path: (a) a structurally bounded (`length is not None`) `0`-output failed frame at `position == 0` (or after leading NULs at `position > 0` when followed by a valid initramfs member) is pinnable at frame level by that frame's exact SHA-256 and `container_chain` (including `initramfs_tail` `offset:0x...` when `position > 0`) while every following member is still decoded and scanned (whereas non-NUL trailing bytes that are not a valid following member still fail closed with an unpinnable `ScanError`), and (b) when `position > 0`, `not saw_member`, `0` decoded bytes were produced, and the failed frame reaches EOF or is followed only by NUL bytes (for `length is None`, all bytes after the frame header are NUL to EOF), it joins the six unsupported magics on the outer-object `ImageOpenError` unsupported path, pinned by full outer-object SHA-256 and `container_chain` (with the frame kind and real offset `0x...` in the reason); an ordinary file with neither known container magic nor an image suffix is treated as data. Each executable `PF_X PT_LOAD` is scanned at 4096-byte page granularity (synthesizing up to 4 zero bytes at EOF when clamped, and refusing overlapping page-rounded VA ranges with different file deltas or `p_filesz == 0` with a non-page-aligned `p_offset`).
  - Pinnable allowlist (v0.3.0): an exact `unsupported_allowlist` pin can only accept content of which no byte was ever decoded: an unknown or unsupported image kind identified by magic (such as AVB0 vbmeta), the nesting-depth limit, a missing erofs/sparse/ext4 tool or an image that tool refuses with no output written, a 0-output gzip/xz frame after leading NULs that is followed only by NULs, legacy LZ4 under the 0-output rule, a ZIP whose central directory cannot be parsed, and a bzImage whose payload was never decoded (no HdrS header, a payload start outside the file, or a payload_length past end of file whose remaining bytes decode to nothing - if they decode, they are scanned and the build stops unpinnably - or an unknown payload magic; also a zstd/LZ4-frame payload when the host has no decoder for it). The never-pinnable formats (LZO, legacy LZ4) win over "payload_length past end of file and nothing decodable": such a payload still stops the build and no pin can accept it. A legacy LZ4 bzImage payload follows Linux `unlz4()` (a later magic continues the same stream; a 0-size chunk or fewer than 4 bytes left ends it) and is never pinnable: any failure scans what decoded and stops the build. An LZO kernel (`CONFIG_KERNEL_LZO`) is decoded with `lzop -dc` and scanned; without a usable `lzop` it stops the build and no pin can accept it. The bzImage decoders are bounded (timeout 30 s plus 1 s per 4 MiB, 512 MiB output); hitting a bound scans what was produced and stops the build. A bzImage payload that produced any byte and then failed a checksum/CRC or trailing-data check is also scanned first and then stops the build with an error no pin can accept; gzip follows the kernel's `__gunzip`, so a CRC32/ISIZE mismatch is not an error and the payload is decoded and scanned. Once content was decoded or partly decoded (a bzImage with trailing bytes after its decoded payload, a `.ko.gz`/`.ko.xz`/`.ko.zst` that decodes to non-ELF, a ZIP with a member that cannot be read), the scanner scans the decoded bytes first (IFMA hits are reported first) and then stops the build with an error no pin can accept; any other unexpected exception also stops the build and never crashes the scanner.
  - If a data file happens to carry a zstd (`28 b5 2f fd`) or lz4 (`04 22 4d 18`) magic after leading NULs, the scanner decodes it as a modern frame and a failure stops the build with no pin possible; check with `zstd -t`/`lz4 -t` whether it really is compressed data, and if the magic is a coincidence, report the container chain and SHA-256 from the report so the scanner can be fixed instead of pinning.
  - Details: `COMPATIBILITY.en.md` "Minimum CPU" and `builder/ifma/manifest.json`.

### Won't boot on a regular PC (`generic-x86`)

- **Symptom**: The image is written and inserted into a PC, but it will not boot.
- **Likely cause**: The image has no Secure Boot signature; the machine cannot
  UEFI-boot from USB; or the CPU is older than Intel 12th Gen / AMD Zen 5.
- **What to do**:
  - The target machine must **UEFI-boot from USB** and have **Secure Boot
    disabled**.
  - If the CPU is older than **Intel 12th Gen / AMD Zen 5** (for example Intel 11th Gen and older, or AMD Zen 4 and older), see "Boot fails, or some programs crash after boot (CPU too old)" above.
  - The default boot entry is **Googlebook generic x86 quiet**.
  - This path is **verified only in QEMU and has never been booted on a physical
    PC** (untested on hardware).

### On a regular PC, Android shows the internal eMMC as external storage

- **Symptom**: After `generic-x86` boots, Android shows the internal eMMC as an
  external storage device.
- **Likely cause**: The `generic-x86` image hides the internal **NVMe** at boot
  (`initcall_blacklist=nvme_init`), but a machine whose **internal disk is eMMC
  is not hidden yet**.
- **What to do**: **Do not** format it and **do not** choose "use as internal
  storage", or you may overwrite its contents.

## Audio problems

### No sound on the 13th-gen CP714-2WN (Zydron)

- **Symptom**: The screen works, but the speakers are silent.
- **Likely cause**: Although the 13th-gen `CP714-2WN` shares the `KANO` board and
  `Google_Kano.*` firmware, its audio uses the Raptor Lake (RPL) topology, which
  needs different SOF files.
- **What to do**:
  - The matching audio needs `intel/sof/community/sof-rpl.ri` and
    `intel/sof-tplg/sof-rpl-max98373-nau8825.tplg`.
  - This path was **untested on hardware** in v0.3.0; v0.3.1 adds no hardware evidence for this SKU.
  - The 12th-gen KANO (ADL-P/U, `8086:51c8` → `sof-adl.ri`) has working speakers
    on the verified unit.

## Sleep and wake

### Pressing the keyboard does not wake the screen while it is off (known issue)

- **Symptom**: With the screen off, pressing the keyboard does nothing; the
  screen stays off.
- **Likely cause**: Before the waking keystroke reaches Android,
  `gbos_wakefilter.ko` has already dropped its `KEY_WAKEUP`.
- **What to do**:
  - **Use the power button or open/close the lid** to wake it.
  - This known issue was not fixed in v0.3.0; complete sleep/wake testing for v0.3.1 is still pending. Use the power button or open/close the lid.

### The screen wakes on its own (v0.1 stealth wake)

- **Symptom**: On the v0.1 image, the screen lights up by itself within tens of
  seconds after the lid is closed or the screen is turned off.
- **Likely cause**: Every stealth wake carried a power-button `KEY_WAKEUP`.
- **What to do**: **From v0.2**, the image ships `gbos_wakefilter.ko`, which drops
  it when the power button did not wake the system. On the KANO, over a 300-second
  lid-closed period there were 10 stealth wakes and the screen never lit.

The experimental KANO-only `GBOS_EC=drain` option is intended to improve self-wake: on 2026-10-29 a KANO stayed asleep for about 33 minutes with no self-wake and woke from the keyboard; S0ix, the real power button, the lid and longer standby are not verified yet (see the [v0.3.0 verification record](VERIFICATION-v0.3.0.md)). Sleep-failure recovery can reset the AP and lose data. See [KANO status](STATUS-KANO.md).

### No "charging" indication

- **Symptom**: With the charger connected, Android does not show charging.
- **Likely cause**: An ACPI charger driver is needed.
- **What to do**: **From v0.2**, the image ships `ac.ko` (read-only ACPI, **does
  not send EC commands**); on the KANO, Android shows charging when the charger is
  connected. Older (v0.1) images do not have this driver.

## Install and writing

### v0.2.0 can only boot from USB and cannot install to the internal disk

- **Symptom**: You want to install to the internal NVMe but find no install
  option.
- **Likely cause**: The image built directly by `install.sh` **has no maintenance
  system** and cannot install to the internal disk.
- **What to do**:
  - To install to the internal NVMe, **use an "installer stick"**: use
    `install/make-installer-stick.sh` to turn the built image into an installer
    stick file, then boot it and select **Install Googlebook to the internal
    disk**.
  - **This erases everything on the internal disk.**
  - The maintenance system is on `kano-maint`; you do not need to run
    `setup-stick.sh`.
  - Note: **this fresh install has not been done on a real KANO yet** (untested
    on hardware).
  - Full steps are in `INSTALL-INTERNAL.md`.

### What to keep when writing a new version

- **Symptom**: After an update the machine will not start, or you want to go back
  to the previous version.
- **Likely cause**: An update must use the **same AVB key**; old versions stay on
  the boot menu.
- **What to do**:
  - **Keep all of `~/gbos-work/keys/`**, especially the AVB `vbmeta.pem` and the APEX payload/container identity in `apex-keys/`; updates must reuse both keysets.
  - The old version stays on the boot menu as "(previous version)".
  - See `UPGRADE.md`.

### Secure Boot and BitLocker (AMD machines)

- **Symptom**: You want to try `amd-hx370` on an AMD machine, but Secure Boot is on.
- **Likely cause**: `amd-hx370` is an **experimental** profile that has **never
  been run on real hardware** (untested on hardware).
- **What to do**:
  - **Back up the BitLocker recovery key before** changing any Secure Boot
    setting, or you may be unable to decrypt the disk afterward.
  - It provides two boot entries: the default "safe" (does not load `amdgpu`) and
    "native amdgpu (experimental)" (loads the native `amdgpu`). Both entries run
    the same five early-boot module loads: `bluetooth`, `btusb`,
    `snd_hda_codec_alc269`, `iwlmvm`, and `snd_hda_intel`. The safe/native
    difference is `amdgpu`; neither entry skips HDA, Bluetooth, or `iwlmvm`.
  - This profile ships a rebuilt gralloc with `backend_amdgpu` (sha256 pinned in `[gralloc]`). The safe and native entries share that one file. The native entry has still never been run on hardware, and it may show a black screen; choose the safe entry.
  - The build needs a self-built kernel (`GBOS_DEV_KERNEL_DIR`), RADV, and a `GBOS_MINIGBM` that `[gralloc]` names with a matching sha256 (`tools/minigbm/build-libminigbm-gralloc.sh`, git repo only), or `build.sh` refuses (as `profiles/amd-hx370/profile.toml` says).
  - The build needs `GBOS_DEV_KERNEL_DIR/modules/bluetooth.ko`. If that file is
    missing, the build stops. That is a build prerequisite, not something a
    firmware file added after boot can repair.
  - A module being loaded, an ALSA card appearing, and Android audio HAL/routing
    being able to play are different layers. This profile's `[audio]` fields
    (`card`, `config_dir`, `sources`) are still empty.
  - On failure, separate the layers with read-only checks:
    `dmesg | grep -iE 'firmware|hda|bluetooth|btusb|iwl'`, `lsmod`, `/proc/asound/cards`,
    `dumpsys media.audio_flinger`, and SELinux AVC denials. The module-load
    boundary is SAFETY item 9 (`SAFETY.md`).
  - See `SAFETY.md`.

## The smallest display size is still too large

The steps of Settings > Display > Display size are scaled from the base density `ro.sf.lcd_density`; the stock value is 260 per public sources (to be verified on the device), and the smallest step is expected at about 220 with the AOSP 85% step. For a 14" 1920x1080 or 1920x1200 panel, add `density = 200` (suggested starting point from the panel math; smallest step expected at about 170) or `density = 160` to the `[display]` table of the machine profile and rebuild. If Display size was changed or `adb shell wm density` was used, set it back to Default or run `adb shell wm density reset` first, or the new base value stays hidden. See `docs/DISPLAY-DENSITY.md`.

## Hardware feature limits

Support and limits depend on the image and machine; see each record below:

| Feature | Status |
|---|---|
| Auto-rotate, ambient light sensor | Android listed no available sensors on the tested configuration; this does not establish that the hardware is absent |
| Keyboard backlight control | Backlight control remains limited in v0.3.0. R6 verified control endpoints and on/off, but had a brightness-curve defect; v0.3.1 changes the source curve without hardware verification of the new curve. See [KANO status](STATUS-KANO.md). |
| Built-in camera | In development, **off by default** |
| Touchscreen, stylus | Detected, but **no actual click test yet** (untested on hardware) |
| Wi-Fi, audio (`generic-x86`) | Needs machine firmware, which the generic image mostly does not ship, so usually unavailable |

`generic-x86` output goes through the firmware **GOP framebuffer** and drawing is
done on the **CPU (SwiftShader)**: **no GPU acceleration, and it is slow**; the
resolution is determined by firmware. The usable range is whatever the probe
report says (next section).

## Reporting a problem or a new machine

Run the read-only probe on the target machine using **any Linux USB** (for
example an Ubuntu Live USB):

```
install/gbos-probe.sh --out gbos-probe
```

- It **only reads `/sys` and `/proc`**: it loads no modules, touches no disks, and
  **reads no serial numbers**.
- The result is in `gbos-probe/report.txt`: which devices Googlebook has drivers
  for (Wi-Fi, audio, touchpad, storage, …) and which it does not.
- Attach the report to a report ticket (the **GitLab issue template**) so the new
  machine can be brought into support.
- The full bring-up flow is in `NEW-DEVICE.md`.

> Reminder: to protect others' privacy, check that the report contains no personal
> information before you share it.
