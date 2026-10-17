"""amd-hx370 vendor edits (hooks.vendor in profile.toml). builder/vendor.sh calls display().

Two boot entries are decided at build time (no runtime graphics switching):
  - Default "Googlebook AMD HX370 safe" (lapis-quiet.conf): amdgpu is blacklisted on the kernel
    cmdline and never modprobed; /vendor/etc/amd-hx370-graphics.sh loads gbos_efifb.ko and sets
    ro.hardware.vulkan=pastel, ro.hwui.use_vulkan=false, debug.renderengine.vulkan=false.
  - "Googlebook AMD HX370 native amdgpu (experimental)" (lapis-debug.conf): amdgpu.modeset=1 is
    on the kernel cmdline; /vendor/etc/amd-hx370-graphics.sh modprobes amdgpu once on early-boot,
    never starts gbos_efifb, sets ro.hardware.vulkan=radeon, ro.hwui.use_vulkan=true,
    debug.renderengine.vulkan=true, and sets vendor.hwc.drm.device from sysfs (DRIVER=amdgpu).
"""


def expected(prof):
    """(new paths, changed paths). build.prop is already in the builder's changed set."""
    return [], []


def _remove(bt, key):
    lines = bt.splitlines()
    hits = [i for i, line in enumerate(lines) if line.startswith(key + "=")]
    if len(hits) != 1:
        raise SystemExit("prop not exactly once: " + key)
    del lines[hits[0]]
    text = "\n".join(lines)
    return text + ("\n" if bt.endswith("\n") else "")


def _assign(bt, key, value):
    lines = bt.splitlines()
    hits = [i for i, line in enumerate(lines) if line.startswith(key + "=")]
    if len(hits) > 1:
        raise SystemExit("prop set more than once: " + key)
    if hits:
        lines[hits[0]] = f"{key}={value}"
        text = "\n".join(lines)
        return text + ("\n" if bt.endswith("\n") else "")
    return bt.rstrip("\n") + f"\n{key}={value}\n"


def display(ctx):
    """Client HWC, RADV/pastel (by boot entry), and ANGLE. Colour-pipeline props are the same ones
    generic-x86 sets, so the first boot does not depend on DCN colour-pipeline properties."""
    bp = ctx.tree / "build.prop"
    bt = bp.read_text()
    for k, old, new in ctx.prof["display"]["build_prop_set"]:
        line = f"{k}={old}"
        if bt.splitlines().count(line) != 1 or sum(1 for l in bt.splitlines() if l.startswith(k + "=")) != 1:
            raise SystemExit("prop not exactly once: " + line)
        bt = bt.replace("\n" + line + "\n", "\n" + f"{k}={new}" + "\n", 1)
    if ctx.prof["display"]["hwc_backend"] == "client":
        if any(l.startswith("vendor.hwc.backend_override=") for l in bt.splitlines()):
            raise SystemExit("backend_override already set")
        bt = bt.rstrip("\n") + (
            "\n# amd-hx370: SurfaceFlinger composes every layer; drm_hwcomposer scans out the client target\n"
            "vendor.hwc.backend_override=client\n"
        )
    # GLES stays on the system ANGLE library (libEGL_angle.so).
    # Remove ro.hardware.vulkan, ro.hwui.use_vulkan, and debug.renderengine.vulkan from build.prop
    # so /vendor/etc/amd-hx370-graphics.sh (run synchronously on early-boot) sets them according to
    # the boot entry chosen at boot time (safe: pastel/false/false; native: radeon/true/true).
    bt = _remove(bt, "ro.hardware.vulkan")
    bt = _remove(bt, "ro.hwui.use_vulkan")
    if any(l.startswith("debug.renderengine.vulkan=") for l in bt.splitlines()):
        bt = _remove(bt, "debug.renderengine.vulkan")
    bt = bt.rstrip("\n") + (
        "\n# amd-hx370: ro.hardware.vulkan, ro.hwui.use_vulkan and debug.renderengine.vulkan are set by\n"
        "# /vendor/etc/amd-hx370-graphics.sh on early-boot from the chosen systemd-boot entry\n"
    )
    bt = _assign(bt, "ro.hardware.egl", "angle")
    bp.write_text(bt)
    print("hwc backend:", [l for l in bt.splitlines() if "backend_override" in l or l.startswith("ro.hardware.")])
