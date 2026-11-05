#!/usr/bin/env python3
"""Exercise thumbnailer source methods with ASan/UBSan and padded NV12 input."""
import argparse
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True, help='patched libcamera source tree')
args = parser.parse_args()
p = args.source / 'src/android/jpeg'
if not (p / 'thumbnailer.cpp').is_file():
    parser.error('--source must contain src/android/jpeg/thumbnailer.cpp')
s=(p/'thumbnailer.cpp').read_text()
methods=s[s.index('Thumbnailer::Thumbnailer()'):]
new='unsigned int sourceStride)' in s
pre=r'''
#include <algorithm>
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cassert>
#include <iostream>
#include <vector>
#define LOG(a,b) std::cerr
#define ASSERT(x) assert(x)
namespace libcamera {
struct Size { unsigned width,height; };
using PixelFormat=int;
namespace formats { const int NV12=1; }
struct Span { unsigned char *p; size_t n; unsigned char *data() const {return p;} size_t size() const{return n;} };
struct FrameBuffer { std::vector<Span> data; bool valid=true; };
struct MappedFrameBuffer { enum class MapFlag {Read}; const FrameBuffer *frame; MappedFrameBuffer(const FrameBuffer *f,MapFlag):frame(f) {} bool isValid() const{return frame->valid;} int error()const{return 1;} const std::vector<Span>&planes()const{return frame->data;} };
}
using namespace libcamera;
class Thumbnailer { public: Thumbnailer(); void configure(const Size &,PixelFormat''' + (', unsigned int' if new else '') + r'''); void createThumbnail(const FrameBuffer&,const Size&,std::vector<unsigned char>*); const PixelFormat &pixelFormat()const{return pixelFormat_;} private: PixelFormat pixelFormat_; Size sourceSize_; [[maybe_unused]] unsigned sourceStride_; bool valid_; };
'''
conf=lambda fmt,stride: 't.configure({6,4},'+str(fmt)+((','+str(stride)) if new else '')+');'
main=r'''
int main() { Thumbnailer t; '''+conf(1,8)+r'''
 std::vector<unsigned char> y(32,0),uv(16,0); for(int r=0;r<4;r++) for(int x=0;x<6;x++) y[r*8+x]=20+r*40+x; for(int r=0;r<2;r++) for(int x=0;x<6;x++) uv[r*8+x]=120+x;
 FrameBuffer f{{{y.data(),y.size()},{uv.data(),uv.size()}}}; std::vector<unsigned char> out;
 t.createThumbnail(f,{6,4},&out); assert(out.size()==36); for(int r=0;r<4;r++) for(int x=0;x<6;x++) assert(out[r*6+x]==y[r*8+x]);
 t.createThumbnail(f,{12,8},&out); assert(out.size()==144); assert(out[95]==y[3*8+5]);
 t.createThumbnail(f,{2,2},&out); assert(out.size()==6);
 t.createThumbnail(f,{0,2},&out); assert(out.empty());
 t.createThumbnail(f,{3,2},&out); assert(out.empty());
 t.createThumbnail(f,{9000,2},&out); assert(out.empty());
 f.data[1].n=1; t.createThumbnail(f,{2,2},&out); assert(out.empty());
 f.data.resize(1); t.createThumbnail(f,{2,2},&out); assert(out.empty());
 f.data={{y.data(),y.size()},{uv.data(),uv.size()}};
 '''+conf(2,8)+r''' t.createThumbnail(f,{2,2},&out); assert(out.empty());
 '''+conf(1,5)+r''' t.createThumbnail(f,{2,2},&out); assert(out.empty());
 t.configure({5,4},1,8); t.createThumbnail(f,{2,2},&out); assert(out.empty());
 t.configure({6,0},1,8); t.createThumbnail(f,{2,2},&out); assert(out.empty());
 t.configure({9000,4},1,9000); t.createThumbnail(f,{2,2},&out); assert(out.empty());
 t.configure({6,4},1,8); t.createThumbnail(f,{2,2},&out); assert(out.size()==6);
 puts("thumbnail padded/up/down/invalid checks PASS"); }
'''
with tempfile.TemporaryDirectory() as t:
 f=Path(t)/'test.cpp';f.write_text(pre+methods+main)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',str(f),'-o',str(Path(t)/'test')],check=True)
 subprocess.run([str(Path(t)/'test')],check=True)
