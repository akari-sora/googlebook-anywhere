#!/usr/bin/env python3
"""Exercise actual BLOB import with padded and invalid fake gralloc buffers."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/mm/generic_camera_buffer.cpp').read_text()
body=s[s.index('\tif (pixelFormat == formats::MJPEG)'):s.index('\n\tconst unsigned int numPlanes')]
stub=r'''
#include <cassert>
#include <cstdint>
#include <climits>
#include <cerrno>
#include <vector>
using buffer_handle_t=void *;
struct hw_module_t {};
struct gralloc_module_t:hw_module_t {int(*registerBuffer)(const gralloc_module_t*,void*);int(*unregisterBuffer)(const gralloc_module_t*,void*);int(*perform)(const gralloc_module_t*,int,...);int(*lock)(const gralloc_module_t*,void*,int,int,int,uint32_t,int,void**);int(*unlock)(const gralloc_module_t*,void*);};
#include <cstdarg>
constexpr int GRALLOC_HARDWARE_MODULE_ID=1,HAL_PIXEL_FORMAT_BLOB=0x21, PROT_READ=1,PROT_WRITE=2,GRALLOC_USAGE_SW_READ_OFTEN=1,GRALLOC_USAGE_SW_WRITE_OFTEN=2;
namespace formats {constexpr int MJPEG=99;}
struct Plane{unsigned stride,offset,size;};struct Span{Span(uint8_t *,uint32_t){}};
static gralloc_module_t module;static bool known=true;static int32_t format=0x21;static uint32_t width=24,height=1;static int unregCount=0,unlockCount=0,lockFail=0;
int hw_get_module(int,const hw_module_t **m){*m=&module;return 0;}bool knownKanoBlobGralloc(const gralloc_module_t *){return known;}
int reg(const gralloc_module_t *,void*){return 0;}int unreg(const gralloc_module_t *,void*){unregCount++;return 0;}int unlock(const gralloc_module_t *,void*){unlockCount++;return 0;}
int perform(const gralloc_module_t*,int op,...){va_list ap;va_start(ap,op);(void)va_arg(ap,void*);if(op==1)*va_arg(ap,int32_t*)=format;else{*va_arg(ap,uint32_t*)=width;*va_arg(ap,uint32_t*)=height;}va_end(ap);return 0;}
int lock(const gralloc_module_t*,void *h,int,int,int,uint32_t,int,void **p){if(lockFail)return -5;*p=h;return 0;}
struct Test{int error_=0;long bufferLength_=0;size_t blobLength_=0;const gralloc_module_t *gralloc_=nullptr;void *handle_=nullptr;bool registered_=false,grallocLocked_=false,mapped_=false;std::vector<Plane> planeInfo_;std::vector<Span> planes_;
 void run(int pixelFormat,int flags,void *camera3Buffer){
'''
end=r'''
 }
 ~Test(){if(grallocLocked_)gralloc_->unlock(gralloc_,handle_);if(registered_)gralloc_->unregisterBuffer(gralloc_,handle_);}
};
int main(){module.registerBuffer=reg;module.unregisterBuffer=unreg;module.perform=perform;module.lock=lock;module.unlock=unlock;std::vector<uint8_t> bytes(32);
 {Test t;t.bufferLength_=32;t.run(99,3,bytes.data());assert(t.mapped_&&t.blobLength_==24&&t.planeInfo_[0].size==24);}assert(unlockCount==1&&unregCount==1);
 known=false;{Test t;t.bufferLength_=32;t.run(99,3,bytes.data());assert(t.error_==-ENOTSUP);}known=true;
 width=33;{Test t;t.bufferLength_=32;t.run(99,3,bytes.data());assert(t.error_==-EINVAL&&!t.mapped_);}width=24;
 height=2;{Test t;t.bufferLength_=32;t.run(99,3,bytes.data());assert(t.error_==-EINVAL);}height=1;
 format=42;{Test t;t.bufferLength_=32;t.run(99,3,bytes.data());assert(t.error_==-EINVAL);}format=0x21;
 lockFail=1;{Test t;t.bufferLength_=32;t.run(99,3,bytes.data());assert(t.error_==-5);}assert(unlockCount==1&&unregCount==5);
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+end)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('BLOB layout test PASS')
