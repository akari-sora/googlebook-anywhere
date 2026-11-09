#!/usr/bin/env python3
"""Exercise actual abort path returning an unconsumed acquire fence."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_device.cpp').read_text()
body=s[s.index('void CameraDevice::abortRequest('):s.index('bool CameraDevice::isValidRequest(',s.index('void CameraDevice::abortRequest('))]
stub=r'''
#include <cassert>
#include <memory>
#include <vector>
constexpr int CAMERA3_MSG_ERROR_REQUEST=1;
struct UniqueFD {int fd=-1;UniqueFD &operator=(int x){fd=x;return *this;}};
struct Fence {int fd;int release(){int x=fd;fd=-1;return x;}};
struct FrameBuffer {std::unique_ptr<Fence> fence;std::unique_ptr<Fence> releaseFence(){return std::move(fence);}};
struct Camera3RequestDescriptor {enum class Status{Success,Error};struct Buffer{std::unique_ptr<FrameBuffer> frameBuffer;UniqueFD fence;Status status=Status::Success;};int frameNumber_=0;std::vector<Buffer> buffers_;Status status_=Status::Success;};
struct CameraDevice {mutable int notified=0;void notifyError(int,void*,int)const{notified++;}void abortRequest(Camera3RequestDescriptor *)const;};
'''
main=r'''
int main(){CameraDevice d;Camera3RequestDescriptor r;r.buffers_.resize(2);r.buffers_[0].frameBuffer=std::make_unique<FrameBuffer>();r.buffers_[0].frameBuffer->fence=std::unique_ptr<Fence>(new Fence{42});r.buffers_[1].fence=77;d.abortRequest(&r);assert(d.notified==1&&r.status_==Camera3RequestDescriptor::Status::Error);assert(r.buffers_[0].fence.fd==42&&r.buffers_[1].fence.fd==77);assert(r.buffers_[0].status==Camera3RequestDescriptor::Status::Error);}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('abort fence test PASS')
