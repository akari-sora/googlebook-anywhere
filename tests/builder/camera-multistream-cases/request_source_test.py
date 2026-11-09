#!/usr/bin/env python3
"""Exercise the real mapped-request source allocation loop."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_device.cpp').read_text()
start=s.index('\tfor (const auto &[i, buffer]',s.index('Now handle the Mapped streams'))
end=s.index('\n\t/*\n\t * Translate controls',start)
body=s[start:end]
stub=r'''
#include <cassert>
#include <cerrno>
#include <cstddef>
#include <map>
#include <memory>
#include <set>
#include <sstream>
#include <vector>
struct Size{int width=1924,height=1092;}; std::ostream &operator<<(std::ostream &o,Size){return o<<"size";}
struct Camera3 {int width=1280,height=720,format=1;};
using camera3_stream_t=Camera3;
struct FrameBuffer{};
struct Config{Size size;int pixelFormat=12;};
struct Stream {
 enum class Type{Mapped,Internal}; Type typ=Type::Mapped; Camera3 camera; Config cfg; Stream *source=nullptr; int allocations=0,returned=0; bool fail=false; FrameBuffer frame;
 Type type(){return typ;} Camera3 *camera3Stream(){return &camera;} Config &configuration(){return cfg;} Stream *sourceStream(){return source;} Stream *stream(){return this;}
 FrameBuffer *getBuffer(){allocations++; return fail?nullptr:&frame;} void putBuffer(FrameBuffer *){returned++;}
};
using CameraStream=Stream;
struct Buffer{Stream *stream;std::shared_ptr<FrameBuffer> internalBuffer;};
struct Request{int count=0,result=0;int addBuffer(Stream *,FrameBuffer *){count++;return result;}};
struct Descriptor {std::vector<Buffer> buffers_; std::map<Stream *,Buffer *> pendingStreamsToProcess_; int streamsProcessMutex_=0;std::unique_ptr<Request> request_=std::make_unique<Request>();};
struct MutexLocker{explicit MutexLocker(int &) {} void unlock(){}};
namespace utils { int hex(int x){return x;} template<class V> struct E { V &v; struct I{size_t n;V &v;bool operator!=(const I&o){return n!=o.n;}void operator++(){n++;}auto operator*(){return std::pair<size_t,typename V::value_type &>{n,v[n]};}};I begin(){return {0,v};}I end(){return {v.size(),v};}};template<class V>E<V> enumerate(V &v){return {v};}}
#define LOG(...) std::ostringstream()
#define ASSERT(x) assert(x)
int run(Descriptor *descriptor,std::set<CameraStream *> &requestedStreams, int *failure=nullptr){
 auto failRequest=[&](int error){if(failure)*failure=error;return 0;};
'''
endstub=r'''
 return 0;
}
int main(){
 Stream source;source.typ=Stream::Type::Internal;Stream a,b;a.source=&source;b.source=&source;
 {Descriptor d;d.buffers_={{&a,{}},{&b,{}}};std::set<Stream *> requested;
 assert(run(&d,requested)==0);assert(d.request_->count==1&&a.allocations==1&&b.allocations==0&&d.pendingStreamsToProcess_.size()==2);}
 assert(a.returned==1);
 {Descriptor d;d.buffers_={{&a,{}},{&b,{}}};std::set<Stream *> requested{&source};assert(run(&d,requested)==0);assert(d.request_->count==0&&a.allocations==1);}
 {Descriptor d;d.buffers_={{&a,{}}};d.request_->result=-EINVAL;std::set<Stream *> requested;int failure=0;assert(run(&d,requested,&failure)==0&&failure==-EINVAL);}
 assert(a.returned==2);
 {a.fail=true;Descriptor d;d.buffers_={{&a,{}}};std::set<Stream *> requested;int failure=0;assert(run(&d,requested,&failure)==0&&failure==-ENOMEM&&d.request_->count==0);}
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+endstub)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('request source test PASS')
