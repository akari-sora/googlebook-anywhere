# Display density (`[display] density`)

Why Settings > Display > Display size cannot go small enough on the stock image, and which `display.density` values
fit common 14" panels (public issue #5). Nothing here was measured on a device; numbers without a source are
arithmetic from the cited inputs. Steps to check a value on a real machine are still open.

## Where Android takes the base density from

- `DisplayMetrics.getDeviceDensity()` uses `qemu.sf.lcd_density` if set, else `ro.sf.lcd_density`, else 160
  (`DENSITY_DEFAULT`) [A1]. This is the display's base density.
- The builder writes `display.density` into `ro.sf.lcd_density` in vendor `build.prop`. When it is set (not 0) the
  builder stops if that key is not assigned exactly once there; unset (0) leaves the file untouched.
- The Googlebook base is believed to carry `ro.sf.lcd_density=260` in vendor `build.prop`, per public sources (two
  public ports of the same Googlebook recovery read or keep it there [B1][B2]); to be verified on the device.
- `adb shell wm density <dpi>` and the Display size setting store a forced density under `/data` [A3]. It survives
  updates that keep data and hides a new base value: set Display size back to Default or run `adb shell wm density
  reset` after changing `density`.

## The slider

SettingsLib `DisplayDensityUtils` builds the Display size steps from the base density [A2]. With the AOSP defaults
(`display_density_min_scale` 85 %, interval 9 %) there is one smaller step, at 85 % of the base, rounded down to even.
Whether Googlebook's Settings overlays change these fractions is not known from public source.

| base density | smallest step expected with the AOSP 85 % step |
|---|---|
| 260 (stock, per public sources) | 220 |
| 200 | 170 |
| 180 | 152 |
| 160 | 136 |

## Suggested values

Chromium picks an internal LCD's scale factor from its dpi (above 150 dpi: 1.25, above 180: 1.6, ...) [A4]; in Android
terms a factor s is a density of 160 * s.

| panel | ppi | ChromeOS LCD factor | 160 * s |
|---|---|---|---|
| 14" 1920x1080 | 157.4 | 1.25 | 200 |
| 14" 1920x1200 | 161.7 | 1.25 | 200 |
| 14" 2560x1600 | 215.6 | 1.6 | 256 |

So **200** is a suggested starting point from the panel math for 14" FHD/WUXGA panels, not a measured visual match
with ChromeOS. 160 gives the same base as gbrunch, which sets `qemu.sf.lcd_density 160` at runtime [B1]; gbOS86 ships
180 for a 1920x1080 panel [B2]. The KANO profile covers both 1920x1200 and 2560x1600 SKUs, so it leaves `density`
unset; no built-in profile changes its default display settings.

The "ASUS CX34" in issues #6, #8 and #13 is the Chromebook Vibe CX34 Flip (CX3401, 14" 1920x1200), not the
Chromebook Plus CX34 (CX3402, 14" 1920x1080); #3 is probably the same model (its reporter wrote "cx43 vibe").
Issue #13 reports `sw873dp w1396dp h873dp 220dpi` on it (873 * 220 / 160 is about 1200 px, matching 1920x1200); whether
Display size was at Default for that reading is not known yet.

## Sources

- [A1] AOSP frameworks/base `core/java/android/util/DisplayMetrics.java`, https://android.googlesource.com/platform/frameworks/base
- [A2] AOSP frameworks/base `packages/SettingsLib/src/com/android/settingslib/display/DisplayDensityUtils.java` and `packages/SettingsLib/res/values/dimens.xml`
- [A3] https://source.android.com/docs/core/display/multi_display/displays ("Display window settings")
- [A4] Chromium `ui/display/manager/display_change_observer.cc`, https://chromium.googlesource.com/chromium/src/+/refs/heads/main/ui/display/manager/display_change_observer.cc
- [B1] https://github.com/danbyumagic/gbrunch (tools/package_compat_update_v6.py)
- [B2] https://github.com/linuxkid473/gbos-x86 (README.md "Display size", usb/gbos.cfg)
- Panels: Acer Chromebook Spin 714 CP714-1WN spec pages (1920x1200, 2560x1600), https://www.acer.com/us-en/chromebooks/acer-chromebook-spin-714-cp714-1wn ; ASUS Chromebook Plus CX34 (CX3402) tech specs (1920x1080), https://www.asus.com/laptops/for-home/chromebook/asus-chromebook-plus-cx34-cx3402/techspec/ ; ASUS Chromebook Vibe CX34 Flip (CX3401) tech specs (1920x1200), https://www.asus.com/us/laptops/for-home/chromebook/asus-chromebook-vibe-cx34-flip-cx3401-12th-gen-intel/techspec/