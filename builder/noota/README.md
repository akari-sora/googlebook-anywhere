# Read-only update engine (experimental HX370)

The `services.readonly_update_engine` profile option is currently restricted to
`amd-hx370` with `cpu_compat.patch_ifma = true`. It replaces the stock updater
with a read-only service while retaining the existing no-OTA guard. Image,
QEMU and physical-device acceptance must be checked separately.

On Linux with Android NDK 29.0.14206865 already installed, build the pinned
shim to a new output path:

```sh
ANDROID_NDK_HOME=/path/to/ndk/29.0.14206865 builder/noota/build.sh /path/to/new/update_engine
GBOS_READONLY_UPDATE_ENGINE=/path/to/new/update_engine  # export for the image build
export GBOS_READONLY_UPDATE_ENGINE
```

The helper checks the source and output hashes. Linux NDK reproduction has not
yet been verified; a mismatching output is rejected.

Run the host-only Binder lifecycle mock test with:

```sh
python3 -B tests/builder/noota/run.py
```

The test compiles the shipped source against local mock headers and times out
after 30 seconds. If `CXX` (or `c++` by default) is unavailable, it exits 77
and reports `SKIP`; this is not a passing lifecycle test or device validation.

QEMU Binder callback lifecycle probe and build instructions: [Android test](../../tests/builder/noota/android/README.md).
