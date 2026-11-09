#!/usr/bin/env python3
"""Exercise actual pre-queue error completion without losing buffers."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_device.cpp').read_text()
a=s.index('\tauto failRequest =',s.index('int CameraDevice::processCaptureRequest('));b=s.index('\n\tif (!descriptor->request_)',a);body=s[a:b]
stub=r'''
#include <cassert>
#include <cerrno>
#include <memory>
#include <mutex>
#include <queue>
#include <sstream>
#define LOG(...) std::ostringstream()
using MutexLocker=std::lock_guard<std::mutex>;
struct Camera3RequestDescriptor{bool accepted_=false;int fence=31;};
struct Device{std::mutex descriptorsMutex_;std::queue<std::unique_ptr<Camera3RequestDescriptor>> descriptors_;int notified=0,returned=0;
 void abortRequest(Camera3RequestDescriptor *d){assert(d->accepted_);notified++;}void completeDescriptor(Camera3RequestDescriptor *d){assert(d->fence==31);returned++;descriptors_.pop();}
 int run(){auto descriptor=std::make_unique<Camera3RequestDescriptor>();
'''
end=r'''
 return failRequest(-ENOMEM);}
};
int main(){Device d;assert(d.run()==0);assert(d.notified==1&&d.returned==1&&d.descriptors_.empty());}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+end)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('preallocation failure test PASS')
