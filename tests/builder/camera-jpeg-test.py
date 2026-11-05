#!/usr/bin/env python3
"""Exercise JPEG destination and full encoder methods using installed libjpeg."""
import argparse
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True, help='patched libcamera source tree')
args = parser.parse_args()
source = args.source / 'src/android/jpeg'
if not (source / 'encoder_libjpeg.cpp').is_file():
    parser.error('--source must contain src/android/jpeg/encoder_libjpeg.cpp')

p = source
s = (p / 'encoder_libjpeg.cpp').read_text()
callbacks = s[s.index('struct BoundedDestination'):s.index('struct JPEGPixelFormatInfo')]
code = '''#include <cstdio>
#include <cstddef>
#include <vector>
#include <cassert>
#include <jpeglib.h>
''' + callbacks + r'''
int encode(unsigned char *out, size_t cap) {
 if (!cap) return -1;
 jpeg_compress_struct c{}; jpeg_error_mgr err; c.err=jpeg_std_error(&err); jpeg_create_compress(&c);
 c.image_width=16; c.image_height=16; c.input_components=3; c.in_color_space=JCS_RGB; jpeg_set_defaults(&c);
 BoundedDestination d{}; d.data=out; d.capacity=cap; d.manager.init_destination=initBoundedDestination; d.manager.empty_output_buffer=discardOverflow; d.manager.term_destination=finishBoundedDestination; c.dest=&d.manager;
 jpeg_start_compress(&c,TRUE); unsigned char row[48] = {}; JSAMPROW r = row;
 while(c.next_scanline < c.image_height) jpeg_write_scanlines(&c,&r,1);
 jpeg_finish_compress(&c); int n=d.overflow ? -1 : (d.switched ? d.capacity : d.capacity-d.manager.free_in_buffer); c.dest=nullptr; jpeg_destroy_compress(&c); return n;
}
int main() {
 std::vector<unsigned char> a(8194, 0xa5); int n=encode(a.data()+1,8192); assert(n>0 && a[0]==0xa5 && a.back()==0xa5);
 jpeg_decompress_struct c{}; jpeg_error_mgr e; c.err=jpeg_std_error(&e); jpeg_create_decompress(&c); jpeg_mem_src(&c,a.data()+1,n); assert(jpeg_read_header(&c,TRUE)==JPEG_HEADER_OK); assert(c.image_width==16 && c.image_height==16); jpeg_start_decompress(&c); std::vector<unsigned char> row(c.output_width*c.output_components); while(c.output_scanline<c.output_height){JSAMPROW r=row.data();jpeg_read_scanlines(&c,&r,1);} jpeg_finish_decompress(&c); jpeg_destroy_decompress(&c);
 for(size_t cap: {size_t(0),size_t(1),size_t(8),size_t(n-1),size_t(n)}) { std::vector<unsigned char> b(cap+2,0xa5); int v=encode(b.data()+1,cap); assert(b.front()==0xa5 && b.back()==0xa5); assert((cap==size_t(n)) == (v==n)); }
 puts("real JPEG decode and canary boundaries PASS");
}
'''
with tempfile.TemporaryDirectory() as t:
 f=Path(t)/'test.cpp'; f.write_text(code)
 flags=subprocess.check_output(['pkg-config','--cflags','--libs','libjpeg'],text=True).split()
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(f),*flags,'-o',str(Path(t)/'test')],check=True)
 subprocess.run([str(Path(t)/'test')],check=True)

p=source
s=(p/'encoder_libjpeg.cpp').read_text()
callbacks=s[s.index('struct BoundedDestination'):s.index('struct JPEGPixelFormatInfo')]
methods=s[s.index('EncoderLibJpeg::EncoderLibJpeg()'):s.index('int EncoderLibJpeg::encode(Camera3RequestDescriptor')]
encode=s[s.index('int EncoderLibJpeg::encode(const std::vector'):]
pre=r'''
#include <cstdio>
#include <cstddef>
#include <cstdint>
#include <climits>
#include <cerrno>
#include <cassert>
#include <vector>
#include <iostream>
#include <jpeglib.h>
#define LOG(a,b) std::cerr
#define ASSERT(x) assert(x)
namespace libcamera {
template <typename T> struct Span { T *p; size_t n; T *data() const { return p; } size_t size() const { return n; } bool empty() const { return n==0; } };
struct Size { unsigned int width, height; };
struct PixelFormatInfo { struct P { unsigned bytesPerGroup,verticalSubSampling; }; P planes[2]; unsigned pixelsPerGroup; unsigned numPlanes() const { return 2; } unsigned stride(unsigned width,unsigned) const {return width;} };
struct StreamConfiguration { Size size; int pixelFormat; unsigned stride; };
}
using namespace libcamera;
struct JPEGPixelFormatInfo { J_COLOR_SPACE colorSpace; const PixelFormatInfo &pixelFormatInfo; bool nvSwap; };
static const PixelFormatInfo format={{{2,1},{2,2}},2};
JPEGPixelFormatInfo findPixelInfo(int) { return {JCS_YCbCr,format,false}; }
class EncoderLibJpeg { public: EncoderLibJpeg(); ~EncoderLibJpeg(); int configure(const StreamConfiguration &); int encode(const std::vector<Span<uint8_t>> &,Span<uint8_t>,Span<const uint8_t>,unsigned); private: void compressRGB(const std::vector<Span<uint8_t>>&); void compressNV(const std::vector<Span<uint8_t>>&); jpeg_compress_struct compress_; jpeg_error_mgr jerr_; const PixelFormatInfo *pixelFormatInfo_; unsigned stride_,chromaStride_; bool nv_,nvSwap_; };
'''
main=r'''
int main() {
 const int width=642,height=8,stride=648; EncoderLibJpeg encoder;
 assert(encoder.configure({{643,height},0,648}) == -EINVAL);
 assert(encoder.configure({{width,height},0,width-2}) == -EINVAL);
 assert(encoder.configure({{width,height},0,stride}) == 0);
 std::vector<uint8_t> y(stride*height, 0),uv(stride*height/2,0);
 for(int r=0;r<height;r++) for(int x=0;x<width;x++) y[r*stride+x]=(r<4 ? 40 : 210);
 for(int r=0;r<height/2;r++) for(int x=0;x<width;x+=2) {uv[r*stride+x]=128; uv[r*stride+x+1]=128;}
 std::vector<Span<uint8_t>> planes{{y.data(),y.size()},{uv.data(),uv.size()}};
 std::vector<uint8_t> out(20002,0xa5); Span<const uint8_t> noexif{nullptr,0};
 auto shortPlanes=planes; shortPlanes[1].n=1; assert(encoder.encode(shortPlanes,{out.data()+1,20000},noexif,95)==-EINVAL);
 int n=encoder.encode(planes,{out.data()+1,20000},noexif,95); assert(n>0 && out.front()==0xa5 && out.back()==0xa5);
 jpeg_decompress_struct c{}; jpeg_error_mgr e; c.err=jpeg_std_error(&e); jpeg_create_decompress(&c); jpeg_mem_src(&c,out.data()+1,n); assert(jpeg_read_header(&c,TRUE)==JPEG_HEADER_OK); c.out_color_space=JCS_RGB; jpeg_start_decompress(&c); std::vector<uint8_t> row(width*3);
 while(c.output_scanline<c.output_height) {int r=c.output_scanline; JSAMPROW p=row.data(); jpeg_read_scanlines(&c,&p,1); for(int x: {0,321,641}) {int v=row[x*3]; assert(r<3 ? v<70 : r>4 ? v>180 : true);}}
 jpeg_finish_decompress(&c); jpeg_destroy_decompress(&c);
 for(int cap: {1,n-1,n}) {std::vector<uint8_t> b(cap+2,0xa5);int v=encoder.encode(planes,{b.data()+1,(size_t)cap},noexif,95);assert(b.front()==0xa5 && b.back()==0xa5);assert(cap==n ? v==n : v==-ENOSPC);}
 puts("full encoder methods: padded NV12 decode, invalid geometry, exact/small destination PASS");
}
'''
with tempfile.TemporaryDirectory() as t:
 f=Path(t)/'test.cpp';f.write_text(pre+callbacks+methods+encode+main)
 flags=subprocess.check_output(['pkg-config','--cflags','--libs','libjpeg'],text=True).split()
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(f),*flags,'-o',str(Path(t)/'test')],check=True)
 subprocess.run([str(Path(t)/'test')],check=True)
