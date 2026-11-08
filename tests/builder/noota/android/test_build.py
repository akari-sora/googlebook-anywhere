import os,pathlib,subprocess,sys,tempfile,unittest,zipfile
P=pathlib.Path(__file__).parent
class Build(unittest.TestCase):
 def test_build_exclusive_and_deterministic(self):
  with tempfile.TemporaryDirectory() as t:
   d=pathlib.Path(t);sdk=d/'sdk';jdk=d/'jdk';(sdk/'platforms/android-35').mkdir(parents=True);(sdk/'platforms/android-35/android.jar').touch();(sdk/'build-tools/35.0.0').mkdir(parents=True);(jdk/'bin').mkdir(parents=True)
   javac=jdk/'bin/javac';javac.write_text('#!/bin/sh\nwhile [ "$1" != "-d" ]; do shift; done\nmkdir -p "$2/org/googlebook/noota"\nprintf class > "$2/org/googlebook/noota/Probe.class"\n');javac.chmod(0o755)
   d8=sdk/'build-tools/35.0.0/d8';d8.write_text('#!/bin/sh\n[ \"$JAVA_HOME\" = \"$EXPECTED_JDK\" ] || exit 23\nwhile [ "$1" != "--output" ]; do shift; done\nprintf dex > "$2/classes.dex"\n');d8.chmod(0o755)
   def run(out):return subprocess.run([sys.executable,str(P/'build.py'),'--sdk',str(sdk),'--jdk',str(jdk),'--build-tools','35.0.0','--api','35','--out',str(out)],capture_output=True,env={**os.environ,'JAVA_HOME':'/wrong-ambient-jdk','EXPECTED_JDK':str(jdk.resolve())})
   a=d/'a.jar';b=d/'b.jar';self.assertEqual(run(a).returncode,0);self.assertEqual(run(b).returncode,0);self.assertEqual(a.read_bytes(),b.read_bytes())
   with zipfile.ZipFile(a) as z:self.assertEqual(z.namelist(),['classes.dex']);self.assertEqual(z.read('classes.dex'),b'dex')
   old=a.read_bytes();self.assertNotEqual(run(a).returncode,0);self.assertEqual(a.read_bytes(),old)
   d8.unlink();self.assertNotEqual(run(d/'c.jar').returncode,0);self.assertFalse((d/'c.jar').exists())
if __name__=='__main__':unittest.main()
