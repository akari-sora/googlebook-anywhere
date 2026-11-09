#!/usr/bin/env python3
"""Compile actual NV12 crop geometry and check the KANO aspect ratio."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/yuv/post_processor_yuv.cpp').read_text()
body=s[s.index('\t/* Center crop to the destination aspect ratio'):s.index('\n\tint ret = libyuv::NV12Scale')]
stub=r'''
#include <cassert>
#include <cstdint>
struct Size {unsigned width,height;};
struct PostProcessor {enum class Status{Error};};
struct Signal{void emit(void *,PostProcessor::Status) {assert(false);}};
struct Test {Size sourceSize_,destinationSize_;Signal processComplete;unsigned w=0,h=0,l=0,t=0;
 void crop(void *streamBuffer) {
'''
end=r'''
 w=cropWidth;h=cropHeight;l=left;t=top;
 }
};
int main(){Test x{{1924,1092},{1280,720},{}};x.crop(nullptr);assert(x.w==1924&&x.h==1082&&x.l==0&&x.t==4);assert(!(x.w%2||x.h%2||x.l%2||x.t%2));Test y{{1920,1080},{640,480},{}};y.crop(nullptr);assert(y.w==1440&&y.h==1080&&y.l==240&&y.t==0);}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+end)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('crop test PASS')
