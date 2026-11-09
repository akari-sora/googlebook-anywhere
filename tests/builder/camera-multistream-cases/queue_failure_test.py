#!/usr/bin/env python3
"""Compile actual request queue block and verify failed queue completes once."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_device.cpp').read_text()
a=s.index('\tRequest *request = descriptor->request_.get();',s.index('int CameraDevice::processCaptureRequest('))
b=s.index('\n\treturn 0;',a)
body=s[a:b]
stub=r'''
#include <cassert>
#include <cerrno>
#include <memory>
#include <mutex>
#include <queue>
#include <sstream>
#include <vector>
#define LOG(...) std::ostringstream()
using MutexLocker=std::lock_guard<std::mutex>;
struct Request{};
struct CameraStream{enum class Type{Direct,Mapped};Type t;Type type(){return t;}};
struct UniqueFD{int fd;void reset(){fd=-1;}};
struct Buffer {CameraStream *stream;UniqueFD fence;};
struct Camera3RequestDescriptor{std::unique_ptr<Request> request_=std::make_unique<Request>();bool accepted_=false;std::vector<Buffer> buffers_;};
struct Camera{int result;int queueRequest(Request *){return result;}};
struct Device{Camera c;Camera *camera_=&c;std::mutex descriptorsMutex_;std::queue<std::unique_ptr<Camera3RequestDescriptor>> descriptors_;int aborted=0,completed=0;
 void abortRequest(Camera3RequestDescriptor *d){assert(d->accepted_);assert(d->buffers_[0].fence.fd==-1);assert(d->buffers_[1].fence.fd==20);aborted++;}
 void completeDescriptor(Camera3RequestDescriptor *){completed++;descriptors_.pop();}
 int run(std::unique_ptr<Camera3RequestDescriptor> descriptor){int ret=0;
'''
end=r'''
 return 0;}
};
int main(){for(int result:{0,-EIO}){Device d;d.c.result=result;CameraStream a{CameraStream::Type::Direct},b{CameraStream::Type::Mapped};auto descriptor=std::make_unique<Camera3RequestDescriptor>();descriptor->buffers_={{&a,{10}},{&b,{20}}};assert(d.run(std::move(descriptor))==0);if(result)assert(d.aborted==1&&d.completed==1&&d.descriptors_.empty());else assert(d.aborted==0&&d.completed==0&&d.descriptors_.front()->accepted_);}}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+end)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('queue failure test PASS')
