# QEMU Android Binder probe

The fixed probe checks 14 read-only update-engine transactions, then tests callback capacity, child-process death cleanup and 32 concurrent distinct bind/unbind operations. The log parser requires all records to share one positive PID and rejects any probe failure. The concurrency probe is bounded coverage, not proof of every race.

Build with a local JDK and Android SDK; the output must not already exist:

```sh
python3 tests/builder/noota/android/build.py --sdk /path/to/android-sdk --jdk /path/to/jdk --build-tools 35.0.0 --api 35 --out /path/to/new-probe.jar
python3 -m unittest discover -s tests/builder/noota/android -p 'test_*.py'
```

The jar contains only `classes.dex`. Build and parser tests do not prove actual Android Binder execution, production SELinux access or hardware behavior. The existing disposable QEMU diagnostic setup uses a test policy; production policy must be verified separately.
