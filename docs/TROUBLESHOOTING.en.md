# Troubleshooting (Googlebook Anywhere v0.2.1)

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
  - **v0.2.1 expands `xe.force_probe` to 12 iGPU PCI IDs** (covering ADL-P/U and
    RPL-U) and is expected to fix this.
  - But **v0.2.1 has not been verified on any real hardware yet** (untested on
    hardware) — retry with the v0.2.1 image and report the result (see the end
    of this document).
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
  - If on KANO or Zen 5 only a single system program crashes, it may be the AVX-IFMA instruction in a few Rust
    programs (neither CPU has it); please report the SIGILL and the program name from logcat.
  - Details: `COMPATIBILITY.en.md` "Minimum CPU" (2026-10-17 disassembly of the base).

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
  - This path is **untested on hardware**; test it with the v0.2.1 image and
    report back.
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
  - This is a known issue, not fixed in v0.2.1. Use the power button or open/close the lid.

### The screen wakes on its own (v0.1 stealth wake)

- **Symptom**: On the v0.1 image, the screen lights up by itself within tens of
  seconds after the lid is closed or the screen is turned off.
- **Likely cause**: Every stealth wake carried a power-button `KEY_WAKEUP`.
- **What to do**: **From v0.2**, the image ships `gbos_wakefilter.ko`, which drops
  it when the power button did not wake the system. On the KANO, over a 300-second
  lid-closed period there were 10 stealth wakes and the screen never lit.

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
  - To install to the internal NVMe, **v0.2.1 uses an "installer stick"**: use
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
  - **Keep `~/gbos-work/keys/`**; the update must use the same key.
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

## Hardware feature limits

The following are **currently unavailable or deliberately blocked**, mostly
related to the EC (embedded controller) or machine firmware:

| Feature | Status |
|---|---|
| Auto-rotate, ambient light sensor | Requires the EC; deliberately blocked or unsupported |
| Keyboard backlight control | Requires the EC; deliberately blocked or unsupported |
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
