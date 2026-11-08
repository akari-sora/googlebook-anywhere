# Camera source candidates

Patch `0006-softisp-cpu-nv12.patch` adds optional NV12 output to the pinned libcamera 0.7.2 CPU SoftISP when libyuv is available. It uses full-range sYCC conversion and validates the known output plane layout. This is an unreleased source candidate; a successful NDK build does not establish Android preview or capture.

Android gralloc import still requires reliable format, stride, plane and access metadata. The generic importer's tightly packed assumptions are not validated on the target, and preview plus JPEG configuration remains untested. Complete those integrations and device tests before treating this as a usable camera overlay.

Patch `0007-jpeg-bounds-and-stride.patch` bounds the libjpeg destination, checks JPEG BLOB trailer capacity, and uses the configured source stride. Patch `0008-native-buffer-safety.patch` rejects missing plane descriptors and invalid capacities, and handles descriptor duplication failures without terminating the HAL. These patches passed a full Android NDK build. They do not establish correct Android buffer import, camera enumeration, preview or photo capture. Patch `0009-thumbnail-stride.patch` uses the configured NV12 stride for thumbnails, clears stale output on invalid input, and bounds geometry and plane access. Its host ASan/UBSan checks and full NDK build passed; actual Android buffer mapping remains unverified.

Patch `0010-camera-init-diagnostics.patch` reports the failing initialization stage, return value and, when available, stream format and size. It preserves the existing return values and release paths and does not log images. The patched source passed a full Android NDK build; the resulting HAL contains the diagnostic messages. It was exercised on KANO; camera initialization failed and this patch adds diagnostics rather than a camera fix.

## Focused host tests

Run the tests against an already-patched libcamera source tree and a host-built libyuv static library:

```sh
python3 tests/builder/camera-softisp-test.py \
  --source /path/to/libcamera-v0.7.2 \
  --libyuv-include /path/to/libyuv/include \
  --libyuv-library /path/to/libyuv-build/libyuv.a
```

Python 3 and a host C++17 compiler (`c++`) are required. The supplied libyuv must be compatible with the host compiler and the libcamera builder's pinned revision. Supply a library whose transitive dependencies do not require extra linker flags. The test does not download dependencies or verify their revision.

The script compiles the actual `configure`, stride and `process` method bodies with test stubs. It checks invalid and valid layouts, RGB smoke behavior, NV12 processing and errors; it also tests real libyuv color values, U/V order and padding guards. Temporary compiler outputs are removed on exit. These focused host tests do not replace a full Android NDK build, actual allocator integration, or device acceptance tests.

The JPEG and native-buffer checks also accept a patched libcamera source tree:

```sh
python3 tests/builder/camera-jpeg-test.py --source /path/to/libcamera-v0.7.2
python3 tests/builder/camera-native-buffer-test.py --source /path/to/libcamera-v0.7.2
python3 tests/builder/camera-thumbnail-test.py --source /path/to/libcamera-v0.7.2
```

The JPEG check requires an installed host libjpeg and `pkg-config`. It exercises the extracted encoder methods with padded NV12 input, real JPEG decoding, output canaries and capacity boundaries. The native-buffer check exercises extracted methods with backend stubs, including descriptor shortages, closed descriptors, duplication failures and ownership cleanup. Neither test downloads dependencies. They do not exercise the complete BLOB postprocessor or a real Android mapper.

The thumbnail check compiles extracted methods with buffer stubs and requires a host compiler with AddressSanitizer and UndefinedBehaviorSanitizer support. It checks padded NV12 input, scaling, invalid reconfiguration, dimensions and plane capacity. Sources and targets are limited to even dimensions of at most 8192 pixels per side.

Patch `0011-config-diagnostics.patch` logs the failed stream configuration (including color space, stride, frame size and buffer count) and the NV12 color-space rejection values. It changes diagnostics only. On KANO 0011 it reported NV12 320x240 rejected with RAW/Linear/Rec601/Full instead of sYCC; it does not itself fix the camera.

Patch `0012-camera-probe-colorspace.patch` clears stale color space when Android HAL probes a different pixel format, so pipeline validation can select an appropriate default. The CPU NV12 sYCC guard remains intact. The full Android NDK build and independent readback of all eight output files passed on 2026-11-08. Preview and capture remain unverified.
