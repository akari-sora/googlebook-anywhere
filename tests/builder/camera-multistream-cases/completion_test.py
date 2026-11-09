#!/usr/bin/env python3
"""Exercise the actual completion handler with two source-buffer consumers."""
from pathlib import Path
import subprocess
import tempfile
s = (Path(__file__).resolve().parents[1] / 'src/android/camera_device.cpp').read_text()
body = s[s.index('void CameraDevice::streamProcessingComplete('):s.index('std::string CameraDevice::logPrefix()', s.index('void CameraDevice::streamProcessingComplete('))]
stub = r'''
#include <cassert>
#include <atomic>
#include <thread>
#include <map>
#include <memory>
#include <mutex>
using MutexLocker = std::lock_guard<std::mutex>;
struct Stream {};
struct Camera3RequestDescriptor {
 enum class Status { Success, Error };
 struct StreamBuffer { Camera3RequestDescriptor *request; Stream *stream; Status status; std::unique_ptr<int> dstBuffer; };
 std::mutex streamsProcessMutex_;
 std::map<Stream *, StreamBuffer *> pendingStreamsToProcess_;
 std::shared_ptr<int> source;
};
struct CameraDevice {
 std::unique_ptr<Camera3RequestDescriptor> active;
 int finished=0;
 void setBufferStatus(Camera3RequestDescriptor::StreamBuffer &b, Camera3RequestDescriptor::Status s) { b.status=s; }
 void completeDescriptor(Camera3RequestDescriptor *d) { assert(active.get()==d); finished++; active.reset(); }
 void streamProcessingComplete(Camera3RequestDescriptor::StreamBuffer *, Camera3RequestDescriptor::Status);
};
'''
main = r'''
int main() {
 using S=Camera3RequestDescriptor::Status;
 for (int reverse=0;reverse<2;reverse++) {
  CameraDevice d; d.active=std::make_unique<Camera3RequestDescriptor>();
  int returns=0; d.active->source=std::shared_ptr<int>(new int(7),[&](int *p){delete p;returns++;});
  Stream x,y; Camera3RequestDescriptor::StreamBuffer a{d.active.get(),&x,S::Success,std::make_unique<int>(1)}, b{d.active.get(),&y,S::Success,std::make_unique<int>(1)};
  d.active->pendingStreamsToProcess_={{&x,&a},{&y,&b}};
  auto *first=reverse?&b:&a; auto *second=reverse?&a:&b;
  d.streamProcessingComplete(first,S::Error);
  assert(first->status==S::Error && !first->dstBuffer && second->dstBuffer && d.finished==0 && returns==0);
  d.streamProcessingComplete(second,S::Success);
  assert(d.finished==1 && returns==1);
 }
 for (int i=0;i<1000;i++) {
  CameraDevice d; d.active=std::make_unique<Camera3RequestDescriptor>();
  std::atomic<int> returns=0; d.active->source=std::shared_ptr<int>(new int(7),[&](int *p){delete p;returns++;});
  Stream x,y; Camera3RequestDescriptor::StreamBuffer a{d.active.get(),&x,S::Success,std::make_unique<int>(1)}, b{d.active.get(),&y,S::Success,std::make_unique<int>(1)};
  d.active->pendingStreamsToProcess_={{&x,&a},{&y,&b}};
  std::thread first([&]{d.streamProcessingComplete(&a,S::Success);});
  std::thread second([&]{d.streamProcessingComplete(&b,S::Error);});
  first.join();second.join();
  assert(d.finished==1 && returns==1 && !a.dstBuffer && !b.dstBuffer);
 }
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp'; p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-pthread','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('completion test PASS')
