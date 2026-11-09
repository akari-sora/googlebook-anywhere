#!/usr/bin/env python3
"""Compile actual generic YUV import block against controlled gralloc responses."""
from pathlib import Path
import subprocess, tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/mm/generic_camera_buffer.cpp').read_text()
body=s[s.index('\tif (requireYuvLayout && pixelFormat == formats::NV12)'):s.index('\tif (pixelFormat == formats::MJPEG)')]
stub=r'''
#include <cassert>
#include <cstdint>
#include <climits>
#include <cerrno>
#include <vector>
using buffer_handle_t = void *;
struct hw_module_t { unsigned module_api_version=2; };
struct android_ycbcr {void *y,*cb,*cr; size_t ystride,cstride,chroma_step;};
struct gralloc_module_t : hw_module_t {
 int (*registerBuffer)(const gralloc_module_t *,buffer_handle_t);
 int (*unregisterBuffer)(const gralloc_module_t *,buffer_handle_t);
 int (*lock_ycbcr)(const gralloc_module_t *,buffer_handle_t,int,int,int,unsigned,unsigned,android_ycbcr *);
 int (*unlock)(const gralloc_module_t *,buffer_handle_t);
};
constexpr int GRALLOC_MODULE_API_VERSION_0_2=2, GRALLOC_HARDWARE_MODULE_ID=1, PROT_READ=1, PROT_WRITE=2, GRALLOC_USAGE_SW_READ_OFTEN=1, GRALLOC_USAGE_SW_WRITE_OFTEN=2;
namespace formats { constexpr int NV12=12; }
struct Size {unsigned width,height;};
struct Plane {unsigned stride,offset,size;};
struct Span {Span(uint8_t *,uint64_t) {}};
static gralloc_module_t module; static android_ycbcr sample; static int registered=0,locked=0,unregistered=0,unlocked=0,lockret=0;
int hw_get_module(int,const hw_module_t **m){*m=&module;return 0;}
int reg(const gralloc_module_t *,buffer_handle_t){registered++;return 0;}
int unreg(const gralloc_module_t *,buffer_handle_t){unregistered++;return 0;}
int lock(const gralloc_module_t *,buffer_handle_t,int,int,int,unsigned,unsigned,android_ycbcr *x){if(lockret)return lockret; *x=sample;locked++;return 0;}
int unlock(const gralloc_module_t *,buffer_handle_t){unlocked++;return 0;}
struct Test {
 int error_=0; long bufferLength_=0; const gralloc_module_t *gralloc_=nullptr; void *handle_=nullptr; bool registered_=false,grallocLocked_=false,mapped_=false; std::vector<Plane> planeInfo_; std::vector<Span> planes_;
 void run(bool requireYuvLayout,int pixelFormat,Size size,int flags,buffer_handle_t camera3Buffer) {
'''
end=r'''
 }
 ~Test(){if(grallocLocked_)gralloc_->unlock(gralloc_,handle_);if(registered_)gralloc_->unregisterBuffer(gralloc_,handle_);}
};
int main(){
 module.registerBuffer=reg;module.unregisterBuffer=unreg;module.lock_ycbcr=lock;module.unlock=unlock;
 std::vector<uint8_t> data(24); sample={data.data(),data.data()+16,data.data()+17,4,4,2};
 {Test t;t.bufferLength_=24;t.run(true,12,{4,4},3,data.data());assert(t.mapped_&&t.error_==0&&t.planeInfo_[1].offset==16);}
 assert(registered==1&&locked==1&&unlocked==1&&unregistered==1);
 sample.cr=data.data()+15; {Test t;t.bufferLength_=24;t.run(true,12,{4,4},3,data.data());assert(t.error_==-EINVAL&&!t.mapped_);}
 assert(unlocked==2&&unregistered==2);
 sample.cr=data.data()+17; sample.cstride=3; {Test t;t.bufferLength_=24;t.run(true,12,{4,4},3,data.data());assert(t.error_==-EINVAL);}
 assert(unlocked==3&&unregistered==3);
 lockret=-5; {Test t;t.bufferLength_=24;t.run(true,12,{4,4},3,data.data());assert(t.error_==-5);}
 assert(unlocked==3&&unregistered==4);
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+end)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('gralloc layout test PASS')
