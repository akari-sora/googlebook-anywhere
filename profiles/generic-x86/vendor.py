"""generic-x86 vendor edits (hooks.vendor in profile.toml): only the display settings. builder/vendor.sh calls display()
with ctx.tree (the extracted vendor tree) and ctx.prof (the validated profile); see profiles/kano/vendor.py for the
full set of hooks a model can have."""


def expected(prof):
    """(new paths, changed paths) the hooks add to the vendor tree: build.prop only, which every build changes."""
    return [], []


def display(ctx):
    """build.prop: the profile's property changes and the hardware composer backend. On the GOP framebuffer
    (gbos_efifb) drm_hwcomposer must hand every layer to SurfaceFlinger's client composition; the framebuffer device
    has one plane and no colour pipeline."""
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
        bt = bt.rstrip("\n") + "\n# generic-x86: one GOP framebuffer plane; compose everything into the client target\nvendor.hwc.backend_override=client\n"
    bp.write_text(bt)
    print("hwc backend:", [l for l in bt.splitlines() if "backend_override" in l])
