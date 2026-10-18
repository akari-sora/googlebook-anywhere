# tools/minigbm

Drop-in for `/vendor/lib64/libminigbm_gralloc.so` on the amd-hx370 image.
Safe and native boot entries both load that one vendor file. The binary is not committed.
`profiles/amd-hx370` pins it under `[gralloc]`, and the builder installs `GBOS_MINIGBM`
only when a profile has that table. kano and generic-x86 have no `[gralloc]` table.
`NOTICES.txt` is in the release archive; the builder copies it into the image.
The build script, `amdgpu_linear.c`, and `upstream-delta.patch` stay in the git repo.

## What it builds

AOSP `platform/external/minigbm` `android17-release` `ce79bbee67b3e2ea7abb110e1415e9ceceaa6471`,
plus `upstream-delta.patch` and `amdgpu_linear.c` (`-DDRV_AMDGPU`). The patch is
upstream `c662136` (drop `VIRTGPU_PARAM_GUEST_VRAM` / `RESOURCE_SYNC`), `9d21b5c`
(`drv_is_protected_usage_permitted`, Xe `AIBinder_getCallingUid` check), and `0c4f081`
(`nvl_ids`, the Intel NVL-S/NVL-P PCI list, not AMD Navi). No public branch tip matches the stock binary: `android17-release` still
has those virtgpu param strings and lacks `nvl_ids`; `upstream-main` has `nvl_ids` but
dropped the pastel/lavapipe lock path whose strings are in the stock `.so`. The compile
defines `ANDROID_API_LEVEL=37` (soong `platform_sdk_version`) so
`cros_gralloc_convert_format` keeps `RGBA_FP16`, `RGBA_1010102`, and `RGBA_10101010`.
Upstream `amdgpu.c` is not linked. The result exports `backend_amdgpu` and the stock
backends, including `backend_vkms` (the safe entry still matches DRM name `vkms`).

| | |
| --- | --- |
| output | `libminigbm_gralloc.so` |
| sha256 | `297457940c9090754e71d4027e157167825edab4139eb7d8828d1660b98e0d42` |
| size | 112384 bytes |
| soname | `libminigbm_gralloc.so` |
| ELF | x86-64, Android 37, NDK r30 (16248370), stripped |
| install | `/vendor/lib64/libminigbm_gralloc.so`, mode `0644` |

That digest is the `[gralloc]` pin and the script's `LIB_SHA`. The script writes `BUILD-STATUS.txt` only after `STRINGS_OK` and only when the linked file's sha256 is `LIB_SHA`. The builder refuses any other bytes. `LIB_SHA`, `LIB_FROM_LINEAR`, `LIB_FROM_PATCH`, and `LIB_FROM_TAR` update together. `LIB_FROM_LINEAR`, `LIB_FROM_PATCH`, and `LIB_FROM_TAR` change only together with `LIB_SHA`, after a rebuild whose output sha256 is the new `LIB_SHA`. Editing a source and `LIB_FROM_LINEAR`, `LIB_FROM_PATCH`, or `LIB_FROM_TAR` without that rebuild leaves the profile tests green and makes the build refuse. The build script's "Update these together" means those four fields, not the `LIB_FROM_*` lines alone.

`stock-dynsyms.txt` is the stock dynamic-symbol list the script checks against. It is text, not a binary.

## Pins

Recorded in `SOURCES.txt` (host home directories are written as `$HOME`). The ones the script enforces, on the host that runs it:

| input | pin |
| --- | --- |
| minigbm commit / tree | `ce79bbee67b3e2ea7abb110e1415e9ceceaa6471` / `95c2eb6837a39a6ef62ae454c5d9767cf2066c53` |
| minigbm `git archive --format=tar --prefix=minigbm/` | sha256 `54b1d07b4645ad73294eab40dbbc4578af43cdee192d4ba5e0b1c2747f5d8834` |
| `upstream-delta.patch` | sha256 `0d2e6678b9c71a47a412245a85dfbf99702bda0f56da522fe65b996bb0bee022` |
| `amdgpu_linear.c` | sha256 `ecf3d67c2163e789374979a58ee593b5a63cdd9c373e6a03056cc6ca72ade1bd` |
| libdrm 2.4.123 | sha256 `a2b98567a149a74b0f50e91e825f9c0315d86e7be9b74394dae8b298caadb79e` |
| NDK r30 `Pkg.Revision` | `30.0.16248370` |
| clang compiler binary (`readlink -f` of `bin/clang`, NDK r30 `clang-21`) | sha256 `ad535f0b6fc303edba958b51636169ff0291c6e36463236c99ff8abcaa34247c` |
| API 37 `aidl` | sha256 `a4ad150a7cf6ea297c299cb779da7a58c537cfc07923d1e15ccbc554a30cd4ba` |

The script checks each of those before it compiles. A gzip of the same archive is not checked: its sha256 changes with the zlib. The `x86_64-linux-android37-clang` and `clang++` paths are the NDK wrapper scripts (the clang wrapper is 200 bytes, sha256 `7673632c25f0bdbaff3c04a456650df08465cf0ad5ea765059f5ae2dfae7e590` on several NDK revisions). The script checks that both paths exist and are regular files whose first line is a `#!` script. That is a presence and type check, not a compiler pin. A change to the wrapper text is stopped by `LIB_SHA` when the linked library bytes change. The compiler pin is the `clang-21` binary above, together with `Pkg.Revision`.

Not pinned: the full NDK tarball, the C++ headers, and the nine prebuilt libraries the link line copies out of the image. `LIB_SHA` is the library this recipe must reproduce on the host that runs it. A second clean machine has not been compared.

`stock-undefsyms.txt` is `llvm-nm -D -u` of the stock library. The build requires `__stack_chk_fail` and `__open_2` in the new library. `__open_2` is the FORTIFY form of `open()` (`virtgpu_virgl.c` calls `open()`; stock imports `__open_2`). Stock has no `__*_chk` imports (`__stack_chk_fail` does not match that suffix), so that set is not a gate. `NEEDED` must stay the stock set. The link line keeps `-Wl,-z,relro -Wl,-z,now`. The C compile line is `"$CC" "${CFLAGS[@]}"` and the `cros_gralloc_*.cc` line is `"$CXX" "${CXXFLAGS[@]}"`, so the hardening flags come from those arrays: `-fstack-protector-strong`, `-D_FORTIFY_SOURCE=2`, `-ftrivial-auto-var-init=zero`, `-funwind-tables`, plus `-DANDROID_API_LEVEL=37`. C++ also adds `-fvisibility-inlines-hidden`. After compile, `llvm-nm -u` must show `__stack_chk_fail` on each `cros_gralloc_*.o` and on at least one C object, and `__open_2` on `virtgpu_virgl.o`. The linked library must import both. `-ftrivial-auto-var-init=zero` has no symbol of its own; the flag text is the check. `BUILD-STATUS.txt` is removed before those checks and is written `PASS` only after the string diff prints `STRINGS_OK` and the linked sha256 equals `LIB_SHA`.

YV12 textures round luma up to twice the 256-byte alignment so Android's chroma stride holds (640-wide is 1024/512/512). Upstream GFX10+ does not always double. Radeon 890M is GFX11.5. Importing that LINEAR pitch into RADV and sampling it is a hardware gate in the native-entry checklist (`docs/NEW-DEVICE.md`), not a QEMU check.

## Run on a Linux host

NDK r30, unpacked at `$HOME/gbos-phase/fp/tools/android-ndk-r30` (override with `NDK=`).
The API 37 `aidl` defaults to `$HOME/gbos-phase/fp/tools/bt/android-37.0/aidl` (`AIDL=`).
`fsck.erofs` defaults to `$HOME/gbos-phase/amd-t2/base/cache/otatools/bin/fsck.erofs` (`FSCK=`).
The script re-execs under `nice -n 19` and skips the compile when `/proc/pressure/io` `some` `avg60` is above 40. `WORK` defaults to `$HOME/gbos-phase/minigbm4`. Scratch files go to `/tmp/minigbm4`.

```sh
bash tools/minigbm/build-libminigbm-gralloc.sh
```

The builder does not run this script. It takes the finished library:

```sh
export GBOS_MINIGBM=$HOME/gbos-phase/minigbm4/out/libminigbm_gralloc.so
```

The default path is `$GBOS_INPUTS/amd-hx370/libminigbm_gralloc.so`. The sha256 must match `[gralloc]` or the build stops. kano and generic-x86 have no `[gralloc]` table, so they never read the file.
