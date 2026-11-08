# Camera device patch fixture

`src/android/camera_device.cpp` is from upstream libcamera v0.7.2 with the
Googlebook camera patch 0008 applied; patches 0001–0007 and 0009–0013 do not
modify this file. It is used to test ordered application of patch 0014 without
downloading the full source. The source license and copyright remain in the
file header. This fixture is not a build or hardware test.
