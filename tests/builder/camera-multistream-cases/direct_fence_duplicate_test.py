#!/usr/bin/env python3
"""Exercise actual direct-buffer setup and original/duplicate fd ownership."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_device.cpp').read_text()
a=s.index('\t\tcase CameraStream::Type::Direct:',s.index('int CameraDevice::processCaptureRequest('));b=s.index('\n\t\tcase CameraStream::Type::Internal:',a)
body=s[a:b]
stub=r'''
#include <cassert>
#include <cerrno>
#include <fcntl.h>
#include <memory>
#include <sstream>
#include <unistd.h>
#define LOG(...) std::ostringstream()
struct UniqueFD {int fd=-1;UniqueFD()=default;explicit UniqueFD(int f):fd(f){}UniqueFD &operator=(UniqueFD&&o){if(fd>=0)close(fd);fd=o.fd;o.fd=-1;return *this;}~UniqueFD(){if(fd>=0)close(fd);}bool isValid()const{return fd>=0;}int get()const{return fd;}};
struct FrameBuffer{};struct Config{int pixelFormat=12,size=4;};struct CameraStream{enum class Type{Direct,Internal};Config cfg;Config &configuration(){return cfg;}};
struct Buffer{int dummy=0;int *camera3Buffer=&dummy;std::unique_ptr<FrameBuffer> frameBuffer;UniqueFD fence;};
struct Device {std::unique_ptr<FrameBuffer> createFrameBuffer(int,int,int){return std::make_unique<FrameBuffer>();}
 int run(Buffer &buffer,CameraStream *cameraStream,int *copy){FrameBuffer *frameBuffer=nullptr;UniqueFD acquireFence;std::ostringstream ss;struct {void unlock(){}} lock;auto failRequest=[](int e){return e;};
 switch(CameraStream::Type::Direct){
'''
end=r'''
 default:break;}
 assert(frameBuffer);*copy=fcntl(acquireFence.get(),F_GETFD);return 0;}
};
int main(){int fds[2];assert(pipe(fds)==0);Buffer b;b.fence=UniqueFD(fds[0]);CameraStream s;Device d;int copied=-1;assert(d.run(b,&s,&copied)==0);assert(copied&FD_CLOEXEC);assert(fcntl(fds[0],F_GETFD)>=0);close(fds[1]);}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+end)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('direct fence duplicate test PASS')
