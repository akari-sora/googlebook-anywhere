#!/usr/bin/env python3
"""Execute vendor.sh's real module-init generation; no module or hardware load."""
import ast
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
MODPROBE = "    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/dev-modules "
LEGACY_AMD = "".join(MODPROBE + name + "\n" for name in ("amdgpu", "snd-sof-amd-acp70", "simpledrm"))
WIFI_RC = """# KANO: join the Wi-Fi network described in /data/kano-wifi.conf (written by the maintenance OS, never in this image).
service kano_wifi /system/bin/sh /vendor/etc/kano-wifi.sh
    oneshot
    disabled
    user root
    seclabel u:r:kano_kmsg_logcat:s0

on property:sys.boot_completed=1
    start kano_wifi
"""
WIFI_DIAG = 'sleep 5; echo "<6>gbos-dev-modules: $(cut -d\' \' -f1 /proc/modules | tr \'\\n\' \' \')" > /dev/kmsg\n'


def generation_code():
    """Select actual AST statements, including the missing-module abort and rc write."""
    source = (ROOT / "builder/vendor.sh").read_text()
    marker = 'python3 - "$TMP" "$HERE/lib" << \'PY\'\n'
    body = source.split(marker, 1)[1].split("\nPY\n", 1)[0]
    nodes = ast.parse(body).body
    start = next(i for i, node in enumerate(nodes) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "missing_ko" for target in node.targets))
    end = next(i for i in range(start, len(nodes)) if isinstance(nodes[i], ast.Expr)
               and isinstance(nodes[i].value, ast.Call)
               and isinstance(nodes[i].value.func, ast.Attribute)
               and nodes[i].value.func.attr == "write_text"
               and isinstance(nodes[i].value.func.value, ast.BinOp)
               and isinstance(nodes[i].value.func.value.right, ast.Constant)
               and nodes[i].value.func.value.right.value == "etc/init/kano-hw.rc")
    return compile(ast.Module(body=nodes[start:end + 1], type_ignores=[]), "vendor-module-routing", "exec")


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / "vendor"
        (self.tree / "etc/init").mkdir(parents=True)
        self.kernel = self.root / "kernel"
        (self.kernel / "modules").mkdir(parents=True)
        self.code = generation_code()

    def run_generation(self, model, *, kernel=True, amdgpu=True, head="", profile_lines="", missing=()):
        if amdgpu:
            (self.kernel / "modules/amdgpu.ko").write_bytes(b"public-stub-not-a-module")
        else:
            (self.kernel / "modules/amdgpu.ko").unlink(missing_ok=True)
        prof = {"model": {"id": model}}
        calls = []
        def missing_dev_modules(actual_prof, directory):
            self.assertIs(actual_prof, prof)
            self.assertEqual(directory, str(self.kernel) if kernel else "")
            calls.append("check")
            return list(missing)
        def append_module_boot(actual_head, actual_prof):
            self.assertIs(actual_prof, prof)
            calls.append("append")
            return actual_head + profile_lines if profile_lines else actual_head
        stub = types.SimpleNamespace(missing_dev_modules=missing_dev_modules, append_module_boot=append_module_boot)
        namespace = {"devprofile": stub, "os": os, "Path": Path,
                     "prof": prof, "hw_head": head, "tree": self.tree}
        with patch.dict(os.environ, {"GBOS_DEV_KERNEL_DIR": str(self.kernel) if kernel else ""}):
            exec(self.code, namespace)
        self.assertEqual(calls, ["check", "append"])
        namespace["rc"] = (self.tree / "etc/init/kano-hw.rc").read_text()
        return namespace

    def test_ally_safe_profile_never_emits_native_graphics_or_acp70(self):
        result = self.run_generation("rog-ally")
        self.assertEqual(result["dev_kmod_boot"], "")
        self.assertEqual(result["rc"], WIFI_RC)
        self.assertEqual(result["dev_wifi_hook"], WIFI_DIAG)

    def test_kano_legacy_no_dev_kernel_is_byte_identical(self):
        head = "on early-boot\n    # existing KANO hardware head\n\n"
        result = self.run_generation("kano", kernel=False, head=head)
        self.assertIs(result["hw_head"], head)
        self.assertEqual(result["dev_kmod_boot"], "")
        self.assertEqual(result["dev_wifi_hook"], "")
        self.assertEqual(result["rc"], head + WIFI_RC)

    def test_legacy_non_ally_amd_fallback_is_byte_identical(self):
        for model in ("kano", "generic-x86", "other-model"):
            with self.subTest(model=model):
                result = self.run_generation(model, head="legacy-head\n")
                self.assertEqual(result["dev_kmod_boot"], LEGACY_AMD)
                self.assertEqual(result["rc"], "legacy-head\n" + WIFI_RC + LEGACY_AMD)
                self.assertEqual(result["dev_wifi_hook"], WIFI_DIAG)

    def test_hx370_keeps_profile_routing_and_no_fallback(self):
        head = "existing-head\n"
        lines = "on early-boot\n    # profile-specific synchronous module commands\n\n"
        result = self.run_generation("amd-hx370", head=head, profile_lines=lines)
        self.assertEqual(result["hw_head"], head + lines)
        self.assertEqual(result["dev_kmod_boot"], "")
        self.assertEqual(result["rc"], head + lines + WIFI_RC)
        self.assertEqual(result["dev_wifi_hook"], WIFI_DIAG)

    def test_disabled_dev_kernel_or_missing_amdgpu_emits_no_fallback(self):
        for model in ("rog-ally", "kano", "amd-hx370", "generic-x86"):
            for kernel, amdgpu in ((False, True), (True, False), (False, False)):
                with self.subTest(model=model, kernel=kernel, amdgpu=amdgpu):
                    result = self.run_generation(model, kernel=kernel, amdgpu=amdgpu, head="unchanged\n")
                    self.assertEqual(result["dev_kmod_boot"], "")
                    self.assertEqual(result["dev_wifi_hook"], WIFI_DIAG if model == "rog-ally" and kernel else "")
                    self.assertEqual(result["rc"], "unchanged\n" + WIFI_RC)

    def image_checks(self, model, modules, *, extra="", amdgpu=True, kernel=True):
        if amdgpu:
            (self.kernel / "modules/amdgpu.ko").write_bytes(b"public-stub-not-a-module")
        else:
            (self.kernel / "modules/amdgpu.ko").unlink(missing_ok=True)
        nodes = ast.parse((ROOT / "builder/image.py").read_text()).body
        node = next(n for n in nodes if isinstance(n, ast.If)
                    and isinstance(n.test, ast.Name) and n.test.id == "DEV_KERNEL_DIR"
                    and any(isinstance(x, ast.Constant) and x.value == "dev-kernel: self-built Linux version"
                            for x in ast.walk(n)))
        log = ("Linux version 6.18.21-android17-5-test (gbos@gbos)\n"
               "rust_binder: Loaded Rust Binder.\n"
               "Service 'exec 1 (/vendor/bin/modprobe --dirname=/vendor/lib/dev-modules amdgpu)' (pid 2) exited with status 0\n"
               + "gbos-dev-modules: " + modules + "\n" + extra)
        if model == "rog-ally":
            log = log.replace("Service 'exec 1 (/vendor/bin/modprobe --dirname=/vendor/lib/dev-modules amdgpu)' (pid 2) exited with status 0\n", "")
        namespace = {"DEV_KERNEL_DIR": str(self.kernel) if kernel else "", "AMD_HX370": model == "amd-hx370",
                     "PROF": {"model": {"id": model}}, "Path": Path, "checks": {},
                     "has": lambda pattern: re.search(pattern, log) is not None}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "image-module-checks", "exec"), namespace)
        return namespace["checks"]

    def test_ally_safe_image_requires_efifb_without_native_audio_or_gpu(self):
        for amdgpu in (False, True):
            with self.subTest(amdgpu_artifact=amdgpu):
                checks = self.image_checks("rog-ally", "gbos_efifb rust_binder", amdgpu=amdgpu)
                self.assertIn("dev-kernel: Ally safe entry: gbos_efifb live", checks)
                self.assertTrue(all(checks.values()), checks)

    def test_ally_safe_image_rejects_missing_display_or_native_loads(self):
        for modules, extra in (("rust_binder", ""), ("gbos_efifb amdgpu", ""),
                               ("gbos_efifb simpledrm", ""),
                               ("gbos_efifb snd_sof_amd_acp70", ""),
                               ("gbos_efifb", "modprobe --dirname=/vendor/lib/dev-modules amdgpu\n"),
                               ("gbos_efifb", "modprobe --dirname=/vendor/lib/dev-modules simpledrm\n"),
                               ("gbos_efifb", "modprobe --dirname=/vendor/lib/dev-modules snd-sof-amd-acp70\n"),
                               ("gbos_efifb", "modprobe --dirname=/vendor/lib/dev-modules snd_sof_amd_acp70\n")):
            with self.subTest(modules=modules, extra=extra):
                self.assertFalse(all(self.image_checks("rog-ally", modules, extra=extra).values()))

    def test_legacy_image_module_checks_still_require_audio_and_graphics(self):
        for model in ("kano", "generic-x86", "other-model"):
            with self.subTest(model=model):
                self.assertTrue(all(self.image_checks(model, "amdgpu snd_sof_amd_acp70 simpledrm").values()))
                for missing in ("amdgpu", "snd_sof_amd_acp70", "simpledrm"):
                    modules = " ".join(x for x in ("amdgpu", "snd_sof_amd_acp70", "simpledrm") if x != missing)
                    self.assertFalse(all(self.image_checks(model, modules).values()))
        self.assertTrue(all(self.image_checks("amd-hx370", "gbos_efifb").values()))
        self.assertEqual(self.image_checks("rog-ally", "", kernel=False), {})

    def test_ally_build_requires_explicit_track2_input_like_hx370(self):
        source = (ROOT / "builder/build.sh").read_text()
        gate = source.split('MODEL=$(python3 "$HERE/lib/devprofile.py" get "$GBOS_PROFILE" model.id)\n', 1)[1]
        gate = gate.split('if [ -n "${GBOS_DEV_KERNEL_DIR:-}" ]; then', 1)[0]
        for model in ("rog-ally", "amd-hx370", "kano", "generic-x86"):
            for directory in ("", "/public-fixture-not-a-validated-artifact"):
                with self.subTest(model=model, directory=directory):
                    env = {**os.environ, "MODEL": model, "GBOS_DEV_KERNEL_DIR": directory}
                    result = subprocess.run(["bash", "-c", 'die() { exit 37; };\n' + gate],
                                            env=env, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 37 if model in ("rog-ally", "amd-hx370") and not directory else 0)

    def test_missing_profile_module_still_aborts_before_append_or_write(self):
        prof = {"model": {"id": "rog-ally"}}
        append = unittest.mock.Mock()
        stub = types.SimpleNamespace(missing_dev_modules=lambda *_: ["required-module"], append_module_boot=append)
        namespace = {"devprofile": stub, "os": os, "Path": Path,
                     "prof": prof, "hw_head": "", "tree": self.tree}
        with self.assertRaisesRegex(SystemExit, "required-module"):
            exec(self.code, namespace)
        append.assert_not_called()
        self.assertFalse((self.tree / "etc/init/kano-hw.rc").exists())


if __name__ == "__main__":
    unittest.main()
