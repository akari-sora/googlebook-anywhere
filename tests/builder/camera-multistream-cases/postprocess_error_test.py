#!/usr/bin/env python3
"""Exercise actual per-buffer error reporting without losing valid metadata."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_device.cpp').read_text()
a=s.index('void CameraDevice::setBufferStatus(');b=s.index('/**\n * \\brief Handle post-processing completion',a);body=s[a:b]
stub=r'''
#include <cassert>
constexpr int CAMERA3_MSG_ERROR_BUFFER=1;
struct Camera3RequestDescriptor;
struct Stream {void *camera3Stream(){return this;}};
struct Camera3RequestDescriptor {enum class Status{Success,Error};struct StreamBuffer{Status status=Status::Success;Camera3RequestDescriptor *request;Stream *stream;};Status status_=Status::Success;int frameNumber_=1;};
struct CameraDevice {int notified=0;void notifyError(int,void *,int){notified++;}void setBufferStatus(Camera3RequestDescriptor::StreamBuffer &,Camera3RequestDescriptor::Status);};
'''
main=r'''
int main(){CameraDevice d;Camera3RequestDescriptor req;Stream s;Camera3RequestDescriptor::StreamBuffer b{Camera3RequestDescriptor::Status::Success,&req,&s};d.setBufferStatus(b,Camera3RequestDescriptor::Status::Error);assert(b.status==Camera3RequestDescriptor::Status::Error&&d.notified==1&&req.status_==Camera3RequestDescriptor::Status::Success);}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('postprocess error test PASS')
