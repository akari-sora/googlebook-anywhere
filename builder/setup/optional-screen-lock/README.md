# Optional screen lock during first setup

This prebuilt product RRO is intended to restore the Settings application's existing, optional
Skip button. It changes exactly one resource:
`bool/config_hide_skip_security_options_in_suw=false`. The normal confirmation
and activity-result handling remain in Settings and Setup Wizard.

The initial candidate is opt-in with `GBOS_SETUP_PIN_SKIP=1`, together with
`GBOS_ANDROID_MODEL=profile`, for the pinned KANO / Google build 16658739 route.
It is not enabled by default. It does not fix the separate generic Not now
activity-result defect by itself. Physical setup completion remains unverified.
Enabled candidates add `setup_pin_skip=true` to their build-record options and
require this revision's record reader. Older strict readers reject that record;
default builds retain the legacy option set and remain readable by those readers.

The target is `com.android.settings`; the package is
`org.googlebook.overlay.optionalscreenlock`. Its static priority is 101, following
the stock desktop overlay's 100. The builder refuses an existing product overlay
configuration directory rather than assuming manifest priority applies to it.
The original Google Settings and overlay APKs remain intact.

The prebuilt is stored as base64 text so the source export and installer archive
retain their existing text-only artifact rules. The builder strictly decodes it
and checks the decoded APK against the digest below.

Normal image builds consume the pinned prebuilt; no Android SDK or APK-signing
tool is added to the image-build dependencies. The APK SHA-256 is
`23936a909cf3777539405f5c449cb3a3d28a65c92f0cef380f42fa4ce79ba236`.
Its signing certificate SHA-256 is
`4f42eac96b0d17bec9322d3ef2a730084f5dd50317221c23fa08a2d82374ba5c`.
`certificate.txt` is the public PEM certificate; no private signing material belongs in this tree.

## Regeneration

Use the existing Android SDK build-tools 36.0.0 and Android 37 platform.
The observed aapt2 SHA-256 was
`a8844d4089b442b034aed8953deee1893253053c900e03141ae7173e3edd8157`;
the Android 37 android.jar SHA-256 was
`bf1b4387cc7ca94fc6ef684f040d9d16fbf16248e181819f020736ea2053f177`.
Compile `res` with `aapt2 compile --dir`, link with this manifest and android.jar,
version code/name 1, then `zipalign -P 16 -f 4`. Sign using the matching PKCS#8
private key and public certificate with apksigner (minimum SDK 29, v1 disabled,
v2/v3 enabled). Verify with `apksigner verify --verbose --print-certs` and
`zipalign -c -P 16 4`. The retained APK verified with APK Signature Scheme v3.

Regeneration is a separate reviewed artifact change: verify the resource map,
only bool value, package, target, priority, certificate and new artifact hash,
then update the builder pin. A successful build or signature verification does
not prove runtime overlay activation. Validate the resolved resource on the
candidate and complete the ordinary Skip / confirmation flow on the device.

References: [Android RRO rules](https://source.android.com/docs/core/runtime/rros)
and [RRO diagnostics](https://source.android.com/docs/core/runtime/rro-troubleshoot).
