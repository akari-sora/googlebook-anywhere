# Compatibility (Googlebook Anywhere v0.2.1)

This document describes which machines the Googlebook Anywhere v0.2.1 image
can be installed on, the current support level for each model, and what has
**not yet been verified on real hardware**.

- The only machine verified on real hardware is an **Alder Lake-P engineering
  sample** of the Acer Chromebook Spin 714 (codename KANO) — iGPU `8086:46a6`.
- All other KANO SKUs and all other x86-64 machines have **not been run on real
  hardware**; their status is always marked "expected to work, untested".
- This document only covers image-level compatibility. For firmware flashing,
  writing the USB, and safety trade-offs, see `FIRMWARE.md`, `SAFETY.md`, and
  `HOW-IT-WORKS.md`.

> 中文版：`COMPATIBILITY.md`.

## Verified machine

| Item | Value |
|---|---|
| Model | Acer Chromebook Spin 714 (KANO, Board ID 1, brya / kano) |
| Verified unit | Alder Lake-P K0 **engineering sample** (exact Acer retail MPN `NX.*` unconfirmed) |
| CPU | 12th Gen Intel Core i7-1260P (Alder Lake-P 4P+8E 28W, CPUID `0x906a2` / `06-9a-02`) |
| iGPU | `8086:46a6` (ADL-P GT2 Iris Xe 96 EU, rev 08) |
| Host bridge | `8086:4621` |
| CBI SKU | `131073` (`0x20001`, FW_CONFIG=`0x13` (hardware reading)) |
| RAM / storage | 16 GB LPDDR4X-4266 (DRAM strap ID 0) + 128 GB `SAMSUNG MZVLQ128HCHQ` NVMe |

> Note: `131073` is the ChromeOS CBI SKU ID (`0x20001`), **not** a CPU model.
> This is an engineering sample and **cannot** flash public firmware.
>
> Board ID and FW_CONFIG (`0x13`) are hardware readings, kept in private research notes; the old table value `0x100000013` has no source in this repo. Host bridge `4621`, LPDDR4X-4266, 4P+8E 28W, and iGPU rev 08 have no measurement in the repo.

## KANO family: Acer Chromebook Spin 714

The table below lists the iGPU and status of each SKU in the KANO (brya / kano)
family. The iGPU column shows the PCI IDs allowed by `xe.force_probe` in
`profiles/kano/profile.toml`. `4626` and `4628` are the 1-channel memory variants
of `46a6` and `46a8`. `46a3`, `46b2`, `a721`, `a7ac`, and `a7ad` are platform
candidates (macro membership); the KANO shipping mapping is unconfirmed. They are
not confirmed alternate IDs.

| Model (retail) | CPU | iGPU PCI ID | v0.2.0 behavior | v0.2.1 status |
|---|---|---|---|---|
| **Verified unit** (above, CBI SKU `131073`) | i7-1260P | `8086:46a6` | works | **an earlier image was tested on this engineering sample; v0.2.1 not yet tested on hardware** |
| CP714-1WN-76B7 / -73F4 etc. | i7-1260P | `8086:46a6` (1-channel variant `8086:4626`) | inferred: `46a6` works, `4626` black screen | expected to work, untested |
| CP714-1WN-7988 | i7-1260P | `8086:46a6` (1-channel variant `8086:4626`) | inferred: `46a6` works, `4626` black screen | expected to work, untested |
| CP714-1WN-53M9 / -574L etc. | i5-1235U | `8086:46a8` (1-channel variant `8086:4628`) | inferred: black screen | expected to work, untested |
| CP714-1WN-34BQ / -38M1 etc. | i3-1215U | `8086:46b3` (platform candidates `8086:46a3`, `8086:46b2`; KANO mapping unconfirmed) | inferred: black screen | expected to work, untested |
| Other ADL-P/U KANO SKUs | i5-1240P / i7-1255U / i7-1280P | `8086:46a6` / `8086:46a8` | inferred: `46a6` works, `46a8` black screen | untested |
| CP714-2WN-320J / -3184 etc. | i3-1315U | `8086:a7a9` (platform candidates `8086:a7ac`, `8086:a7ad`; KANO mapping unconfirmed) | inferred: black screen | expected to work, untested; audio untested |
| CP714-2W-56B2 / -5596 etc. | i5-1335U / i5-1345U | `8086:a7a1` (platform candidate `8086:a721`; KANO mapping unconfirmed) | inferred: black screen | expected to work, untested; audio untested |
| CP714-2W-732F / -71M6 etc. | i7-1355U / i7-1365U vPro | `8086:a7a1` (platform candidate `8086:a721`; KANO mapping unconfirmed) | inferred: black screen | expected to work, untested; audio untested |

### Why did v0.2.0 only work on one machine?

v0.2.0's GPU allowed only a single iGPU ID (`xe.force_probe=46a6`). The iGPUs of
other KANO SKUs (`46a8`, `46b3`, `a7a1`, `a7a9`, …) were rejected by
`xe_pci_probe()` with `-ENODEV`, and lapis ships no fallback DRM/fbdev driver, so
the result was a **black or frozen screen**. Audio (`8086:51c8` → `sof-adl.ri`)
still initialized in the background, but the user could not see anything.

### What changed in v0.2.1

v0.2.1 expands `xe.force_probe` in `profiles/kano/profile.toml` from a single ID
to **12 iGPU PCI IDs** (covering ADL-P/U and RPL-U, including single-channel
memory variants, with `46a6` first and **no** wildcard `*`). The ADL-P IDs are
defined in the kernel's `drivers/gpu/drm/xe/xe_pci.c` (`adl_p_desc`) and
`include/drm/intel/pciids.h`.

An earlier image was tested on this engineering sample; v0.2.1 not yet tested on hardware.
v0.2.1 has **not been verified on any real hardware yet** — it is expected to fix
the black-screen SKUs above, but this needs on-device confirmation.

### 13th-gen (CP714-2WN, codename Zydron) audio

The 13th-gen `CP714-2WN` shares the `KANO` board and `Google_Kano.*` firmware,
but its audio uses the Raptor Lake (RPL) topology: it needs
`intel/sof/community/sof-rpl.ri` and
`intel/sof-tplg/sof-rpl-max98373-nau8825.tplg`. **Untested.**

## Other x86-64 machines (`generic-x86`, experimental)

For x86-64 UEFI machines (for example AMD) whose GPU has no driver in the
Googlebook kernel. The CPU must meet "Minimum CPU" below (Intel 12th gen or AMD Zen 5 and newer).

- Output goes through the firmware **GOP framebuffer**; drawing is done on the
  CPU (**SwiftShader** software rendering).
- **No GPU acceleration**; it is slow, and resolution is determined by firmware.
- **Verified only in QEMU, never booted on a physical PC.**
- Requires UEFI boot from USB and **Secure Boot disabled** (the image has no
  Secure Boot signature).
- Features that need machine firmware, such as Wi-Fi and audio, generally do not
  work; the usable range is whatever `install/gbos-probe.sh` reports.

## Minimum CPU: expected Intel 12th gen or AMD Zen 5 and newer (not tested)

On 2026-10-17 the lapis base (ramdisk first-stage `/init`, `/system` and `/vendor` `bin` and `lib64`, and 44 APEXes; 2,865 ELF files) was disassembled:

- Many libraries contain **AVX2 / FMA / BMI (x86-64-v3)** code (media codecs, TFLite, ML libraries; about 720,000
  instructions in total).
- Some contain **GFNI**, **AVX-VNNI**, **SHA-NI**, **256-bit VAES / VPCLMULQDQ**, **AVX-512** and **AVX-VNNI-INT8** (libcrypto, libyuv, ML, audio and
  camera libraries).
- 16 ELF files with Rust code (3 copies of `libzmij`, 2 of `ectool`) contain one **AVX-IFMA** instruction each (for example `libinputflinger`, the USB HAL, `ectool`,
  crosvm).

Much of this runs without a CPU check: first-stage `/init` itself uses x86-64-v3, GFNI, AVX-VNNI and SHA-NI directly;
`libcrypto` always takes SHA-NI and, without AVX-512, always takes 256-bit VAES / VPCLMULQDQ; zlib's adler32 checks
only SSSE3 and then executes AVX-VNNI. AVX-512 in `libcrypto` is behind a runtime check (confirmed by disassembly);
the other AVX-512 uses (41 files in total, for example ML, audio and camera libraries and `libjxl`) and AVX-VNNI-INT8 are presumed to be behind runtime checks (KANO lacks both and works on hardware).

Only KANO (Intel 12th gen Alder Lake-P) has been verified on hardware. The AMD HX 370 (Zen 5) has never run on real
hardware; a Ryzen AI 5 PRO 340 (also Zen 5) was measured to have all of these instructions except AVX-IFMA and AVX-VNNI-INT8 (`avx_vnni=1 avx_ifma=0 avx512ifma=1 sha_ni=1 gfni=1 vaes=1 vpclmulqdq=1 avx_vnni_int8=0`; for AVX-IFMA see the
last point).

- The earlier statement "AVX only, Sandy Bridge or newer" was **wrong** and is withdrawn.
- **Expected to work**: Intel 12th gen (Alder Lake) and newer, AMD Zen 5 and newer.
- **Expected to fail** at boot with illegal instructions (SIGILL): Intel 11th gen and older (no VEX-encoded AVX-VNNI;
  the 6th–10th gen Skylake family and older Core CPUs also lack SHA-NI and GFNI), and AMD Zen 4 and older (Zen 4 has
  AVX-512 VNNI but not VEX-encoded AVX-VNNI). This is inferred; this project has not tested those CPUs. The third-party
  project gbOS86 reports the same on an Intel 8th gen (i5-8365U): `/init` executes SHA-NI at boot and zlib executes
  AVX-VNNI; it only boots by trapping illegal instructions in the kernel and emulating them in software (slower). This
  project does not do that.
- **Known risk (AVX-IFMA)**: those 16 AVX-IFMA instructions run without a CPU check when a non-zero floating-point
  number is turned into text (for example a JSON error message, or crosvm printing a float). Neither KANO nor Zen 5 has
  this instruction set (on Zen 5 executing it raises SIGILL, measured), so a program that reaches that path crashes.
  It has not been seen in normal boot and use.

## `amd-hx370` (experimental, never run on real hardware)

- This is an **experimental** profile for AMD machines and has **never been run
  on real hardware**.
- It provides two boot entries:
  - **default "safe"**: does not load `amdgpu`.
  - **"native amdgpu (experimental)"**: loads the native `amdgpu`. Stock minigbm has no amdgpu backend, so this entry may show a black screen; choose the safe entry.
- The build needs a self-built kernel (`GBOS_DEV_KERNEL_DIR`) and RADV, or `build.sh` refuses (as `profiles/amd-hx370/profile.toml` says).
- If the machine has **Secure Boot** enabled: **back up the BitLocker recovery
  key before** changing Secure Boot settings, or you may be unable to decrypt.

## Reporting a new machine

To get a new machine supported, run the read-only probe on the target machine
using any Linux USB:

```
install/gbos-probe.sh --out gbos-probe
```

It only reads `/sys` and `/proc`; the result is in `gbos-probe/report.txt`.
Attach the report to a report ticket (the GitLab issue template). For the full
new-device bring-up flow, see `NEW-DEVICE.md`.
