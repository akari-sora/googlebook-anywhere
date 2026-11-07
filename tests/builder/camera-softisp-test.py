#!/usr/bin/env python3
"""Run focused SoftISP source regressions against explicitly supplied host inputs."""
import argparse
import pathlib
import subprocess
import tempfile

CONFIGURE_H = r'''
#include <cerrno>
#include <cstdint>
#include <functional>
#include <iostream>
#include <limits>
#include <memory>
#include <new>
#include <optional>
#include <sstream>
#include <tuple>
#include <vector>
#define HAVE_SOFTISP_LIBYUV 1
#define LOG(a,b) std::cerr
struct PixelFormat{int n;const char *toString()const{return "fmt";} bool operator==(PixelFormat p)const{return n==p.n;}};
namespace formats{constexpr PixelFormat NV12{1}, XRGB8888{2};}
struct Size { unsigned width,height; };
std::ostream &operator<<(std::ostream &o,Size){return o<<"size";}
struct SizeRange{bool contains(Size s)const{return s.width<=1920 && s.height<=1080;}};
std::ostream &operator<<(std::ostream &o,SizeRange){return o<<"range";}
struct ColorSpace{int n;bool operator!=(ColorSpace c)const{return n!=c.n;} static const ColorSpace Sycc; const char *toString()const{return n==1 ? "sycc" : "other";} static const char *toString(const std::optional<ColorSpace>& c){return c ? c->toString() : "unset";}};
const ColorSpace ColorSpace::Sycc{1};
struct StreamConfiguration{PixelFormat pixelFormat; Size size; unsigned stride; std::optional<ColorSpace> colorSpace;};
struct Rectangle{unsigned x,y,width,height; Rectangle()=default; Rectangle(Size s):x(0),y(0),width(s.width),height(s.height){} Size size()const{return {width,height};}};
struct Stats{int configure(const StreamConfiguration &,size_t){return 0;} Size patternSize(){return {2,2};} void setWindow(Rectangle){}};
struct Thread{void configure(unsigned,unsigned){}};
struct DebayerCpu {
 struct DebayerOutputConfig{unsigned bpp=32,stride=0,frameSize=0;} outputConfig_;
 struct Input{Size patternSize;unsigned stride=0;} inputConfig_;
 struct{double gamma;}params_; std::unique_ptr<Stats> stats_=std::make_unique<Stats>();
 std::vector<std::unique_ptr<Thread>>threads_; bool nv12_=false,ccmEnabled_=false; unsigned nv12Stride_=0; uint64_t nv12InputBytes_=0;
 std::unique_ptr<uint8_t[]>rgbScratch_,uScratch_,vScratch_; Rectangle window_{};
 DebayerCpu(){threads_.emplace_back(std::make_unique<Thread>());}
 int getInputConfig(PixelFormat,Input &c){c.patternSize={2,2}; return 0;}
 static int getOutputConfig(PixelFormat,DebayerOutputConfig &c){c.bpp=32;return 0;}
 SizeRange sizes(PixelFormat,Size){return {};}
 int setDebayerFunctions(PixelFormat,PixelFormat,bool){return 0;}
 int configure(const StreamConfiguration &,const std::vector<std::reference_wrapper<const StreamConfiguration>> &,bool);
 std::tuple<unsigned,unsigned> strideAndFrameSize(const PixelFormat &,const Size &);
};
'''

CONFIGURE_T = r'''
int main(){
 DebayerCpu c;
 StreamConfiguration input{{0},{1280,720},1280,ColorSpace::Sycc};
 StreamConfiguration out{formats::NV12,{641,480},0,ColorSpace::Sycc};
 auto run=[&]{std::vector<std::reference_wrapper<const StreamConfiguration>> a{std::cref(out)};return c.configure(input,a,false);};
 if(run()!=-EINVAL) return 1;
 out.size={640,479}; if(run()!=-EINVAL) return 2;
 out.size={0,480}; if(run()!=-EINVAL) return 3;
 out.size={640,480}; out.stride=640; if(run()!=0) return 4;
 out.stride=648; if(run()!=-EINVAL) return 5;
 out.stride=640; out.colorSpace=ColorSpace{2};
 std::ostringstream log; auto *saved=std::cerr.rdbuf(log.rdbuf()); int result=run(); std::cerr.rdbuf(saved);
 if(result!=-EINVAL || log.str().find("actual=other required=sycc")==std::string::npos) return 6;
 out.pixelFormat=formats::XRGB8888; out.stride=2560; if(run()!=0) return 7;
 std::cout<<"PASS: actual configure rejects zero-layout odd/empty NV12, bad stride/color; valid NV12 and RGB accepted\n";
}
'''

STRIDE_H = r'''#include <tuple>
#include <limits>
#include <cstdint>
#include <cerrno>
#include <iostream>
#include <string>
#define HAVE_SOFTISP_LIBYUV 1
#define LOG(a,b) std::cerr
struct PixelFormat { int v; bool operator==(PixelFormat o) const { return v == o.v; } std::string toString() const {return std::to_string(v);} };
namespace formats { constexpr PixelFormat RGB888{1}, BGR888{2}, XRGB8888{3}, ARGB8888{4}, XBGR8888{5}, ABGR8888{6}, NV12{7}; }
struct Size { unsigned int width, height; };
struct DebayerCpu { struct DebayerOutputConfig {unsigned bpp;}; static int getOutputConfig(PixelFormat,DebayerOutputConfig &); std::tuple<unsigned,unsigned> strideAndFrameSize(const PixelFormat &,const Size &); };
'''

STRIDE_T = r'''
int main(){ DebayerCpu c; using T=std::tuple<unsigned,unsigned>;
 if(c.strideAndFrameSize(formats::NV12,{2,2})!=T{8,24}) return 1;
 if(c.strideAndFrameSize(formats::NV12,{640,480})!=T{640,460800}) return 2;
 if(c.strideAndFrameSize(formats::NV12,{3,2})!=T{0,0}) return 3;
 if(c.strideAndFrameSize(formats::NV12,{2,3})!=T{0,0}) return 4;
 if(c.strideAndFrameSize(formats::NV12,{0,2})!=T{0,0}) return 5;
 if(c.strideAndFrameSize(formats::NV12,{0xfffffffe,2})!=T{0,0}) return 6;
 if(c.strideAndFrameSize(formats::RGB888,{2,2})!=T{8,16}) return 7;
 if(c.strideAndFrameSize(formats::XRGB8888,{640,480})!=T{2560,1228800}) return 8;
 std::cout<<"PASS: extracted candidate stride: NV12 padded/even/overflow; RGB unchanged\n"; }
'''

PROCESS_H = r'''
#include <libyuv/convert_from_argb.h>
#include <libyuv/planar_functions.h>
#include <cstdint>
#include <vector>
#include <memory>
#include <iostream>
#include <cassert>
#include <algorithm>
#define HAVE_SOFTISP_LIBYUV 1
#define LOG(a,b) std::cerr
#define LIBCAMERA_TSA_REQUIRES(x)
struct SharedFD { int x; int get()const{return x;} };
struct FrameMetadata { enum Status{FrameSuccess,FrameError}; struct Plane{unsigned bytesused=0;}; Status status=FrameSuccess; int sequence=0,timestamp=0; std::vector<Plane> p; auto &planes(){return p;} };
struct FrameBuffer {
 struct Plane { SharedFD fd; unsigned offset,length; };
 struct Priv { FrameMetadata m; FrameMetadata &metadata(){return m;} } priv;
 std::vector<Plane> p; std::vector<std::vector<uint8_t>> data; bool valid=true;
 const auto &planes()const{return p;} Priv *_d(){return &priv;} const FrameMetadata &metadata()const{return priv.m;}
};
struct MappedFrameBuffer { enum class MapFlag{Read,Write}; struct S {std::vector<uint8_t>*v; uint8_t*data(){return v->data();} size_t size(){return v->size();}}; bool valid; std::vector<S> p; MappedFrameBuffer(FrameBuffer*f,MapFlag):valid(f->valid){for(auto &b:f->data)p.push_back({&b});} bool isValid(){return valid;} auto &planes(){return p;} };
struct DmaSyncer {enum class SyncType{Read,Write}; DmaSyncer(SharedFD,SyncType){} };
struct DebayerParams{}; struct Bench{void startFrame(){} void finishFrame(){}};
struct Stats { void startFrame(uint32_t){} void finishFrame(uint32_t,int){} };
struct Mutex{void lock(){} void unlock(){}}; struct MutexLocker { explicit MutexLocker(Mutex&){} };
struct CV {template<class T> void wait(MutexLocker&,T f){assert(f());}};
struct Sig { int count=0; void emit(FrameBuffer*){++count;} };
static unsigned *pending;
struct DebayerCpuThread {void process(uint32_t,const uint8_t*,uint8_t*){} template<class F> void invokeMethod(F,int,uint32_t,const uint8_t*,uint8_t*dst){std::fill(dst,dst+16,0); *pending=0;} };
constexpr int ConnectionTypeQueued=0;
struct DebayerCpu {
 Bench bench_; bool nv12_=true; unsigned nv12Stride_=8; uint64_t nv12InputBytes_=4; struct{unsigned width=2,height=2;}window_; struct{unsigned stride=8;}outputConfig_;
 std::unique_ptr<uint8_t[]> rgbScratch_{new uint8_t[16]}, uScratch_{new uint8_t[1]}, vScratch_{new uint8_t[1]};
 unsigned workPending_=0; Mutex workPendingMutex_; CV workPendingCv_; std::vector<std::unique_ptr<DebayerCpuThread>> threads_; std::unique_ptr<Stats> stats_{new Stats}; Sig outputBufferReady,inputBufferReady;
 DebayerCpu(){pending=&workPending_; threads_.emplace_back(new DebayerCpuThread);}
 void updateLookupTables(const DebayerParams&){} void dmaSyncBegin(std::vector<DmaSyncer>&,FrameBuffer*,FrameBuffer*){}
 void process(uint32_t,FrameBuffer*,FrameBuffer*,const DebayerParams&);
};
'''

PROCESS_T = r'''
int main(){
 DebayerCpu c; DebayerParams params; FrameBuffer in; in.p={{{1},0,4}}; in.data={std::vector<uint8_t>(4)};
 FrameBuffer out; out.p={{{2},0,16},{{2},16,8}}; out.data={std::vector<uint8_t>(16,0xa5),std::vector<uint8_t>(8,0xa5)}; out.priv.m.p.resize(2);
 c.process(1,&in,&out,params); assert(out.priv.m.status==FrameMetadata::FrameSuccess); assert(out.priv.m.p[0].bytesused==16 && out.priv.m.p[1].bytesused==8); assert(out.data[0][0]==0 && out.data[1][0]==128 && out.data[1][1]==128); assert(c.outputBufferReady.count==1 && c.inputBufferReady.count==1);
 out.p[1].offset=15; c.process(2,&in,&out,params); assert(out.priv.m.status==FrameMetadata::FrameError && out.priv.m.p[0].bytesused==0 && c.outputBufferReady.count==2 && c.inputBufferReady.count==2);
 out.p[1].offset=16; out.data[1].resize(7); c.process(3,&in,&out,params); assert(out.priv.m.status==FrameMetadata::FrameError && out.priv.m.p[1].bytesused==0 && c.outputBufferReady.count==3 && c.inputBufferReady.count==3);
 out.data[1].resize(8); out.valid=false; c.process(4,&in,&out,params); assert(out.priv.m.status==FrameMetadata::FrameError && c.outputBufferReady.count==4 && c.inputBufferReady.count==4);
 out.valid=true; in.data[0].resize(3); c.process(5,&in,&out,params); assert(out.priv.m.status==FrameMetadata::FrameError && c.outputBufferReady.count==5 && c.inputBufferReady.count==5);
 in.data[0].resize(4); c.nv12_=false; out.valid=true; out.p.resize(1); out.data.resize(1); out.priv.m.p.resize(1); c.process(6,&in,&out,params); assert(out.priv.m.status==FrameMetadata::FrameSuccess && out.priv.m.p[0].bytesused==16 && c.outputBufferReady.count==6 && c.inputBufferReady.count==6);
 std::cout<<"PASS: extracted candidate process: real libyuv, metadata, two callbacks, overlap/short/mapping failures and RGB smoke\n";
}
'''

COLOR_TEST = r'''#include <libyuv/convert_from_argb.h>
#include <libyuv/planar_functions.h>
#include <array>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <iostream>
int main() {
  struct Color { uint8_t b,g,r; int y,u,v; };
  const Color colors[] = {{0,0,0,0,128,128},{255,255,255,255,128,128},
                          {0,0,255,76,85,255},{255,0,0,29,255,107}};
  for (auto c : colors) {
    std::array<uint8_t,16> rgb;
    for(int i=0;i<4;i++){rgb[i*4]=c.b;rgb[i*4+1]=c.g;rgb[i*4+2]=c.r;rgb[i*4+3]=255;}
    std::array<uint8_t,18> y; y.fill(0xa5);
    std::array<uint8_t,10> uv; uv.fill(0xa5);
    uint8_t u=0,v=0;
    int ret=libyuv::ARGBToJ420(rgb.data(),8,y.data()+1,8,&u,1,&v,1,2,2);
    assert(!ret);
    libyuv::MergeUVPlane(&u,1,&v,1,uv.data()+1,8,1,1);
    for(int i : {1,2,9,10}) assert(std::abs(int(y[i])-c.y)<=2);
    assert(std::abs(int(uv[1])-c.u)<=2 && std::abs(int(uv[2])-c.v)<=2);
    for(int i : {0,3,4,5,6,7,8,11,12,13,14,15,16,17}) assert(y[i]==0xa5);
    for(int i : {0,3,4,5,6,7,8,9}) assert(uv[i]==0xa5);
  }
  std::cout << "PASS: supplied libyuv full-range color vectors, NV12 U/V order, padded-stride guards\n";
}
'''

def extract(source, start, end):
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=pathlib.Path,
                        help='root of the patched libcamera source tree')
    parser.add_argument('--libyuv-include', required=True, type=pathlib.Path,
                        help='host libyuv include directory')
    parser.add_argument('--libyuv-library', required=True, type=pathlib.Path,
                        help='host libyuv static library')
    args = parser.parse_args()
    source_path = args.source / 'src/libcamera/software_isp/debayer_cpu.cpp'
    if not source_path.is_file():
        parser.error(f'missing source: {source_path}')
    if not (args.libyuv_include / 'libyuv/convert_from_argb.h').is_file():
        parser.error(f'missing libyuv headers in {args.libyuv_include}')
    if not args.libyuv_library.is_file():
        parser.error(f'missing libyuv static library: {args.libyuv_library}')
    source = source_path.read_text()
    try:
        configure = extract(source, 'int DebayerCpu::configure(', '\n/**')
        stride = extract(source, 'std::tuple<unsigned int, unsigned int>\nDebayerCpu::strideAndFrameSize',
                         '\nvoid DebayerCpuThread::setupInputMemcpy')
        output_config = extract(source, 'int DebayerCpu::getOutputConfig(', '\n/*')
        process = extract(source, 'void DebayerCpu::process(', '\nint DebayerCpu::start()')
    except ValueError as exc:
        parser.error(f'expected libcamera methods not found: {exc}')
    cases = [
        ('configure', CONFIGURE_H + configure + stride + CONFIGURE_T, False),
        ('stride', STRIDE_H + output_config + stride + STRIDE_T, False),
        ('process', PROCESS_H + process + PROCESS_T, True),
        ('color', COLOR_TEST, True),
    ]
    with tempfile.TemporaryDirectory(prefix='camera-softisp-test-') as tmp:
        for name, code, libyuv in cases:
            cpp = pathlib.Path(tmp) / f'{name}.cc'
            binary = pathlib.Path(tmp) / name
            cpp.write_text(code)
            command = ['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror']
            if libyuv:
                command += ['-I', str(args.libyuv_include)]
            command += [str(cpp)]
            if libyuv:
                command += [str(args.libyuv_library)]
            command += ['-o', str(binary)]
            subprocess.run(command, check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
