#!/usr/bin/env python3
"""Exercise the candidate's actual probe block against a simulated pipeline."""
from pathlib import Path
import subprocess, tempfile, sys
import os, shutil
p = Path(__file__).resolve().parents[2]
patch = p / 'builder/camera/patches/0014-hal-buffer-probe.patch'
script = (p / 'builder/camera/build-libcamera.sh').read_text()
pin = 'dd9e21314f09d884ae392d5ecf31528e5d9b6c020e9f74f030ae08e5a239a3ef'
import hashlib
assert hashlib.sha256(patch.read_bytes()).hexdigest() == pin
assert pin + '  $HERE/patches/' + patch.name in script
start = script.index('for p in "$HERE"/patches/*.patch; do')
loop = script[start:script.index('\n# libyuv (static', start)]
with tempfile.TemporaryDirectory() as d:
 root=Path(d); source=root/'source'; builder=root/'builder'
 shutil.copytree(p/'tests/builder/fixtures/camera-after-0013',source)
 (builder/'patches').mkdir(parents=True)
 target=builder/'patches'/patch.name; shutil.copyfile(patch,target)
 env={**os.environ,'HERE':str(builder),'S':str(source)}
 def apply(): return subprocess.run(['bash','-e','-c',loop],env=env,capture_output=True,text=True)
 assert apply().returncode == 0
 assert apply().returncode == 0
 device=source/'src/android/camera_device.cpp'; src=device.read_text()
 device.write_text(src.replace('Resolve unspecified buffer counts','Incompatible buffer counts'))
 result=apply(); assert result.returncode != 0 and '0014 HAL buffer probe patch is incompatible' in result.stderr
 # Exercise the actual checksum command with a clean, modified, and missing patch.
 line=next(x for x in script.splitlines() if pin in x and 'sha256sum -c --quiet' in x)
 if sys.platform=='darwin': line=line.replace('sha256sum -c --quiet','shasum -a 256 -c')
 def check(): return subprocess.run(['bash','-c',line],env=env,capture_output=True).returncode
 assert check()==0
 target.write_bytes(target.read_bytes()+b'tampered'); assert check()!=0
 target.unlink(); assert check()!=0

a = src.index('\t/* Resolve unspecified buffer counts')
b = src.index('\n\tfor (const auto &streamConfig : streamConfigs) {', a)
block = src[a:b]
code = r'''
#include <vector>
#include <memory>
#include <cassert>
struct Stream { unsigned int bufferCount=0; int width=100; };
struct Entry { Stream config; };
struct CameraConfiguration {
 enum Status { Valid, Adjusted, Invalid };
 std::vector<Stream> entries; bool shrink=false;
 void addConfiguration(Stream s) { entries.push_back(s); }
 unsigned int size() const { return entries.size(); }
 Stream &operator[](unsigned int i) { return entries[i]; }
 Status validate() {
  bool changed=false;
  if (shrink && entries.size()>1) { entries.pop_back(); changed=true; }
  for (auto &s: entries) {
   if (!s.bufferCount) { s.bufferCount=4; changed=true; }
   else if (s.bufferCount>32) { s.bufferCount=32; changed=true; }
   if (s.width>100) { s.width=100; changed=true; }
  }
  return changed ? Adjusted : Valid;
 }
};
struct Camera { bool shrink=false; std::unique_ptr<CameraConfiguration> generateConfiguration() { auto c=std::make_unique<CameraConfiguration>(); c->shrink=shrink; return c; } };
CameraConfiguration::Status run(std::vector<Entry> streamConfigs, bool shrink=false) {
 Camera camera; camera.shrink=shrink; auto *camera_=&camera;
''' + block + r'''
 auto actual=camera_->generateConfiguration();
 for (auto &entry:streamConfigs) actual->addConfiguration(entry.config);
 return actual->validate();
}
int main() {
 assert(run({{{0,100}},{{0,100}}})==CameraConfiguration::Valid);
 assert(run({{{0,100}},{{0,101}}})==CameraConfiguration::Adjusted);
 assert(run({{{33,100}},{{0,100}}})==CameraConfiguration::Adjusted);
 assert(run({{{0,101}}})==CameraConfiguration::Adjusted);
 assert(run({{{0,100}},{{0,100}}},true)==CameraConfiguration::Adjusted);
}
'''
with tempfile.TemporaryDirectory() as d:
 f=Path(d)/'test.cpp'; f.write_text(code)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(f),'-o',str(Path(d)/'test')],check=True)
 subprocess.run([str(Path(d)/'test')],check=True)
print('HAL probe simulation PASS')
