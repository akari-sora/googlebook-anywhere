#!/usr/bin/env python3
"""Offline Ally signed-regdb profile, fetch and refusal checks."""
import hashlib, os, pathlib, subprocess, tempfile, unittest
ROOT = pathlib.Path(__file__).resolve().parents[2]

class Regdb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.d = pathlib.Path(self.tmp.name); self.prof = self.d / 'rog-ally'; self.prof.mkdir()
        original = (ROOT/'profiles/rog-ally/profile.toml').read_text()
        head = original.split('[[firmware]]', 1)[0]
        self.data = {'regulatory.db': b'test database', 'regulatory.db.p7s': b'test detached signature'}
        self.text = head + ''.join('\n[[firmware]]\npath = "{0}"\nfrom = "wireless-regdb"\nsrc = "{0}"\nsha256 = "{1}"\nloaded_by = "vendor:lib/dev-modules/cfg80211.ko"\n'.format(n, hashlib.sha256(v).hexdigest()) for n,v in self.data.items())
        (self.prof/'profile.toml').write_text(self.text)
        (self.prof/'vendor.py').write_bytes((ROOT/'profiles/rog-ally/vendor.py').read_bytes())
        self.bin = self.d/'bin'; self.bin.mkdir()
        curl = self.bin/'curl'; curl.write_text('''#!/usr/bin/env python3
import base64, os, sys
url=sys.argv[-1]
assert url.startswith("https://kernel.googlesource.com/pub/scm/linux/kernel/git/wens/wireless-regdb/+/ea20dfa028cbb1b0e354efcdbcdbeec48308cb97/")
name=url.split("/")[-1].split("?")[0]
sys.stdout.buffer.write(base64.b64encode(open(os.path.join(os.environ["FAKE_REGDB"], name),"rb").read()))
'''); curl.chmod(0o755)
        sha = self.bin/'sha256sum'; sha.write_text('''#!/usr/bin/env python3
import hashlib,sys
if sys.argv[1:] == ["-c"]:
    ok=True
    for line in sys.stdin:
        digest,path=line.rstrip("\\n").split("  ",1)
        try: actual=hashlib.sha256(open(path,"rb").read()).hexdigest()
        except OSError: actual="missing"
        ok &= actual==digest
    sys.exit(0 if ok else 1)
with open(sys.argv[1],"rb") as f: print(hashlib.sha256(f.read()).hexdigest(),sys.argv[1])
'''); sha.chmod(0o755)
        self.source=self.d/'source';self.source.mkdir()
        for n,v in self.data.items(): (self.source/n).write_bytes(v)
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'], FAKE_REGDB=str(self.source))
    def check(self):
        return subprocess.run(['python3',str(ROOT/'builder/lib/devprofile.py'),'check',str(self.prof)],capture_output=True,text=True)
    def fetch(self):
        return subprocess.run(['bash',str(ROOT/'builder/fetch-inputs.sh'),str(self.d/'out'),str(self.prof)],env=self.env,capture_output=True,text=True)
    def test_profile_pair_and_dmi(self):
        self.assertEqual(self.check().returncode, 0)
        for changed in [self.text.replace('ea20dfa028cbb1b0e354efcdbcdbeec48308cb97','bad'), self.text.split('[[firmware]]')[0] + self.text.split('[[firmware]]')[1].join(['[[firmware]]','']), self.text.replace('src = "regulatory.db.p7s"','src = "other"')]:
            (self.prof/'profile.toml').write_text(changed); self.assertNotEqual(self.check().returncode,0)
        self.assertIn('product_name = "ROG Ally RC71L_RC71L"',(ROOT/'profiles/rog-ally/profile.toml').read_text())
    def test_fetch_cache_and_tamper(self):
        r=self.fetch(); self.assertEqual(r.returncode,0,r.stderr+r.stdout)
        for n,v in self.data.items(): self.assertEqual((self.d/'out/firmware'/n).read_bytes(),v)
        self.assertEqual(self.fetch().returncode,0)
        (self.d/'out/firmware/regulatory.db.p7s').unlink(); (self.source/'regulatory.db.p7s').write_bytes(b'changed')
        r=self.fetch(); self.assertNotEqual(r.returncode,0); self.assertIn('sha256 mismatch',r.stdout)
        self.assertFalse((self.d/'out/firmware/regulatory.db.p7s').exists())
    def test_vendor_pair_never_drifts(self):
        text=(ROOT/'builder/vendor.sh').read_text(); block=text.split('# Signed database bytes form one reviewed pair;',1)[1].split('if [ -n "$FWLIST" ]',1)[0]
        fw=self.d/'fw';fw.mkdir();
        for n,v in self.data.items(): (fw/n).write_bytes(v)
        listing='\n'.join(f'{n} wireless-regdb {n} {hashlib.sha256(v).hexdigest()}' for n,v in self.data.items())
        def run():
            return subprocess.run(['bash','-c','set -euo pipefail\nTMP=$1; FWLIST=$2; GBOS_ALLOW_FIRMWARE_DRIFT=1\n# '+block,'test',str(self.d),listing],env=self.env,capture_output=True,text=True)
        r = run(); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        (fw/'regulatory.db').write_bytes(b'altered');self.assertNotEqual(run().returncode,0)
        (fw/'regulatory.db').unlink();self.assertNotEqual(run().returncode,0)

if __name__ == '__main__': unittest.main()
