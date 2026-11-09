# ROG Ally blacklist kernel experiment

`ally-module-blacklist.patch` targets Android `kernel/common` commit
`3938c8d97142d5cf31543aa96f864a2cf832dda0` and changes only the
`module_blacklist` parameter setter to accept up to 2048 bytes. Other `charp`
parameters keep their existing limit. The 2048-byte value limit does not increase
the separate x86 command-line size limit. Patch SHA-256:
`fd50154493d921fc122650a0cd0a68d3152cb156d40b7e674b1d1b24238e2e78`.

The test build also had pre-existing Ally HID/WMI source changes and used its
own configuration and toolchain. **This patch alone cannot reproduce the kernel
hash below.** The kernel binary is not published here.

| Test input | SHA-256 |
|---|---|
| bzImage | `669c39abe46025d6f7d397839338630d62d7f5886501c2e9134027ad34f4d0d0` |
| kernel config | `751e04c57f76be04f498c272e2991b7d35fb32733c3a473e86a75c4250aa8963` |
| Module.symvers | `8322887a813d0329dfc3e7293fbd9efa62d4548f25cce3160b48f8fbf3717b5f` |
| mmc_core.ko | `75c134283000260c3e447a7e54c9ee70e6443f143e29aed4d3661a87d42d0940` |
| cqhci.ko | `a487dc1acd43318b70de602fc52a1c63810b6cb39bcf985190b00a524f2be699` |
| sdhci.ko | `9c213e9d31d0f3f40f11d53683a05c28e2e578625047cebc5d2cb1f88ea9ae6e` |
| sdhci-uhs2.ko | `9cee1b7c14a2c155b11ef82c37585deac65e4ee2f40771623fe6ccc22ca8b271` |
| sdhci-pci.ko | `04e5a602d0b9d6776b48145854c3a26dc22a353bc905d40853b941170591e012` |
| usb-storage.ko | `e7a3ef2749da3161b1f473994bc77fd39c6db9b663e14553a982f5d78c34dce3` |

In isolated QEMU with the same kernel and modules, the control loaded all five
MMC modules; the 81-name blacklist test loaded four dependencies and the kernel
explicitly rejected `sdhci_pci`. Results and serial output were independently
read back. The USB storage module's 114 imports and 115 CRCs passed the existing
ABI check against that build's Module.symvers; the check reports the expected
in-tree symbol exports, which the builder handles explicitly. This is not a USB
boot test.

The repaired Android image has not been built. QEMU testing does not validate the
custom GL9755 driver, Android module policy, a physical SD card, or Ally hardware.
