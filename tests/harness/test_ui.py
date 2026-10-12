"""kano-ui.sh: UI commands (uiautomator dump, input tap, screencap) go only to a device at KANO_ADB_ADDR that runs
this project's vendor image (KANO_ANDROID_ID_PROP set there)."""
import json, unittest
from helpers import HARNESS, HarnessCase

UI = ('<hierarchy><node index="0" text="OK" resource-id="" class="android.widget.Button" package="x" '
      'content-desc="" clickable="true" bounds="[10,20][30,40]" /></hierarchy>')


class Ui(HarnessCase):
    def adb(self, props):
        self.fk.set("adb.json", json.dumps({"connect": True, "props": props, "ui": UI}))

    def ui(self, *args):
        return self.fk.run(["bash", HARNESS / "kano-ui.sh", *args], timeout=60)

    def test_tap_on_this_projects_android(self):
        self.adb({"init.svc.kano_bootfix": "stopped"})
        r = self.ui("tap", "OK")
        self.assertEqual(r.returncode, 0, self.explain(r))
        self.assertIn("-s 192.0.2.11:5555 shell input tap 20 30", self.fk.lines("adb.log"))

    def test_another_android_gets_nothing(self):
        self.adb({"sys.boot_completed": "1"})                 # something answers there, but not our image
        for args in (("tap", "OK"), ("list",), ("shot", str(self.fk.tmp / "s.png"))):
            with self.subTest(args=args):
                r = self.ui(*args)
                self.assertEqual(r.returncode, 3, self.explain(r))
                self.reason("is not this project's Android", r.stderr)
        log = self.fk.lines("adb.log")
        self.assertTrue(log)
        self.assertEqual([l for l in log if "getprop init.svc.kano_bootfix" not in l], [], log)


if __name__ == "__main__":
    unittest.main()
