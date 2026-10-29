#!/usr/bin/env python3
"""display.density (public issue #5): the optional profile key that sets the vendor ro.sf.lcd_density.
Profile parse (unset = 0 on every shipped profile, a set value loads), refusal of bad values (out of range, wrong
type, ro.sf.lcd_density through build_prop_set), the build.prop edit (only that line changes, fail closed when the
stock line is missing or repeated) and the builder/vendor.sh display block run on a fake vendor tree, so dropping the
edit there (or appending a second density line) turns this test red; the block also runs with
the three real profiles' display() hooks. No image, no network, no root.
usage: tests/builder/density-test.py"""
import contextlib, io, re, shutil, sys, tempfile, types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder" / "lib"))
sys.dont_write_bytecode = True
import devprofile                                    # noqa: E402

passed = failed = 0


def check(name, ok):
    global passed, failed
    print(("PASS " if ok else "FAIL ") + name)
    passed += bool(ok); failed += not ok


STOCK = "# fake vendor build.prop\nro.vendor.x=1\nro.sf.lcd_density=260\nro.cp_system_other_odex=1\nvendor.hwc.drm.enable_color_pipeline=true\n"

# default unchanged: every shipped profile loads with density 0 and the edit is then a no-op
for d in sorted((ROOT / "profiles").iterdir()):
    if (d / "profile.toml").is_file():
        prof = devprofile.load(d)
        check(f"{d.name}: display.density unset (0) by default", prof["display"]["density"] == 0)
        check(f"{d.name}: profile.toml does not set density", "density" not in prof["display"] or
              not re.search(r"(?m)^\s*density\s*=", (d / "profile.toml").read_text()))
check("density 0 leaves build.prop byte-identical", devprofile.set_lcd_density(STOCK, 0) == STOCK)

# the build.prop edit
out = devprofile.set_lcd_density(STOCK, 200)
check("density 200 sets ro.sf.lcd_density=200", out.splitlines().count("ro.sf.lcd_density=200") == 1
      and "ro.sf.lcd_density=260" not in out)
check("only the density line changes", [a for a, b in zip(STOCK.splitlines(), out.splitlines()) if a != b] ==
      ["ro.sf.lcd_density=260"] and len(STOCK.splitlines()) == len(out.splitlines()) and out.endswith("\n"))


def refuses(name, fn, msg):
    try:
        fn()
    except (SystemExit, devprofile.ProfileError) as e:
        check(name, msg in str(e)); return
    check(name + " (not refused)", False)


refuses("a build.prop without ro.sf.lcd_density is refused", lambda: devprofile.set_lcd_density("ro.x=1\n", 200), "not exactly once")
refuses("a build.prop with two ro.sf.lcd_density lines is refused",
        lambda: devprofile.set_lcd_density(STOCK + "ro.sf.lcd_density=240\n", 200), "not exactly once")
refuses("a second ro.sf.lcd_density written with a leading space is refused",
        lambda: devprofile.set_lcd_density("ro.sf.lcd_density=260\n ro.sf.lcd_density=240\n", 200), "not exactly once")
refuses("a second ro.sf.lcd_density written as 'ro.sf.lcd_density =..' is refused",
        lambda: devprofile.set_lcd_density("ro.sf.lcd_density=260\nro.sf.lcd_density =240\n", 200), "not exactly once")
check("a commented-out ro.sf.lcd_density line does not count",
      devprofile.set_lcd_density("#ro.sf.lcd_density=1\nro.sf.lcd_density=260\n", 200) == "#ro.sf.lcd_density=1\nro.sf.lcd_density=200\n")
check("density 0 leaves even a build.prop with spaced duplicates byte-identical",
      devprofile.set_lcd_density("ro.sf.lcd_density=260\n ro.sf.lcd_density =240\n", 0) == "ro.sf.lcd_density=260\n ro.sf.lcd_density =240\n")
check("qemu.sf.lcd_density / ro.sf.lcd_density_x lines are not taken for the density line",
      devprofile.set_lcd_density("ro.sf.lcd_density_x=1\nqemu.sf.lcd_density=1\nro.sf.lcd_density=260\n", 180)
      == "ro.sf.lcd_density_x=1\nqemu.sf.lcd_density=1\nro.sf.lcd_density=180\n")

# profile parse and refusals on a copy of the KANO profile
tmp = Path(tempfile.mkdtemp(prefix="gbos-density."))
try:
    def kano_with(edit):
        d = tmp / "kano"
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(ROOT / "profiles" / "kano", d)
        t = (d / "profile.toml").read_text()
        assert t.count("\n[display]\n") == 1
        (d / "profile.toml").write_text(edit(t))
        return devprofile.load(d)

    add = lambda v: (lambda t: t.replace("\n[display]\n", f"\n[display]\ndensity = {v}\n", 1))
    for v in (120, 160, 200, 480):
        check(f"display.density = {v} loads", kano_with(add(v))["display"]["density"] == v)
    for v, msg in (("119", "from 120 to 480"), ("481", "from 120 to 480"), ("-160", "from 120 to 480"),
                   ('"200"', "expected an integer"), ("200.5", "expected an integer"), ("true", "expected an integer")):
        refuses(f"display.density = {v} is refused", lambda v=v: kano_with(add(v)), msg)
    refuses("ro.sf.lcd_density through build_prop_set is refused",
            lambda: kano_with(lambda t: t.replace('build_prop_set = [["vendor.hwc.drm.enable_color_pipeline", "true", "false"],',
                                                   'build_prop_set = [["ro.sf.lcd_density", "260", "160"], ["vendor.hwc.drm.enable_color_pipeline", "true", "false"],', 1)),
            "set the density with display.density, not ro.sf.lcd_density")
    refuses("qemu.sf.lcd_density through build_prop_set is refused, naming qemu.sf.lcd_density",
            lambda: kano_with(lambda t: t.replace('build_prop_set = [["vendor.hwc.drm.enable_color_pipeline", "true", "false"],',
                                                   'build_prop_set = [["qemu.sf.lcd_density", "260", "160"], ["vendor.hwc.drm.enable_color_pipeline", "true", "false"],', 1)),
            "set the density with display.density, not qemu.sf.lcd_density")

    # builder/vendor.sh: run its display block (from the model display comment to the cppreopts comment) on a fake tree
    vs = (ROOT / "builder" / "vendor.sh").read_text()
    m = re.search(r"(?ms)^# Model display settings.*?(?=^# No cppreopts)", vs)
    check("vendor.sh has the display block", bool(m))

    def keycount(text):
        return sum(1 for l in text.splitlines() if "=" in l and not l.lstrip().startswith("#")
                   and l.split("=", 1)[0].strip() == devprofile.DENSITY_PROP)

    def run_block(density, stock=STOCK, prof=None, hooks=None, name="plain"):
        tree = tmp / f"tree-{name}-{density}"
        tree.mkdir()
        (tree / "build.prop").write_text(stock)
        if prof is None:
            prof = {"display": {"stack": "native", "hwc_backend": "", "build_prop_set": [], "density": density}}
        else:
            prof = dict(prof, display=dict(prof["display"], density=density))
        g = {"tree": tree, "bp": tree / "build.prop", "prof": prof, "hooks": hooks,
             "ctx": types.SimpleNamespace(tree=tree, prof=prof), "devprofile": devprofile, "print": lambda *a, **k: None}
        with contextlib.redirect_stdout(io.StringIO()):     # the hooks print their own summary lines
            exec(compile(m.group(0), "vendor.sh display block", "exec"), g)
        return (tree / "build.prop").read_text()

    def same_but_density(base, out, v):
        """out is base with only the ro.sf.lcd_density=260 line replaced, and the key is there once"""
        return (out == base.replace("\nro.sf.lcd_density=260\n", f"\nro.sf.lcd_density={v}\n", 1)
                and out != base and keycount(out) == 1)
    if m:
        check("vendor.sh writes display.density = 180: the full build.prop is the stock one with only that line replaced",
              same_but_density(STOCK, run_block(180), 180))
        check("vendor.sh leaves build.prop unchanged with display.density unset", run_block(0) == STOCK)
        # the three real profiles with their real hooks.vendor display(): density after the hook, nothing else changes
        import importlib.util
        for d in sorted((ROOT / "profiles").iterdir()):
            if not (d / "profile.toml").is_file():
                continue
            prof = devprofile.load(d)
            hooks = None
            if prof["hooks"]["vendor"]:
                spec = importlib.util.spec_from_file_location(f"hooks_{d.name}".replace("-", "_"), d / prof["hooks"]["vendor"])
                hooks = importlib.util.module_from_spec(spec); spec.loader.exec_module(hooks)
            stock = STOCK.replace("vendor.hwc.drm.enable_color_pipeline=true\n", "") + "".join(
                f"{k}={old}\n" for k, old, _ in prof["display"]["build_prop_set"]) + \
                "ro.hardware.vulkan=intel\nro.hwui.use_vulkan=true\nro.hardware.egl=mesa\n"
            base = run_block(0, stock, prof, hooks, d.name)
            check(f"{d.name}: real display() hook runs, density unset changes nothing beyond the hook", base != stock or not hooks)
            for v in (160, 200):
                check(f"{d.name}: real hook + display.density = {v}: only the density line differs, key once",
                      same_but_density(base, run_block(v, stock, prof, hooks, d.name), v))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"density tests: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)