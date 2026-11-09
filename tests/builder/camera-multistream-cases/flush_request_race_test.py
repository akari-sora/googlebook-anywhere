#!/usr/bin/env python3
"""Exercise the real guard, flush and request boundaries with blocked preparation."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_device.cpp').read_text()
flush=s[s.index('void CameraDevice::flush()'):s.index('void CameraDevice::stop()',s.index('void CameraDevice::flush()'))]
guard=s[s.index('CameraDevice::CaptureRequestGuard::CaptureRequestGuard('):s.index('int CameraDevice::processCaptureRequest(')]
start=s[s.index('\tCaptureRequestGuard captureGuard(this);',s.index('int CameraDevice::processCaptureRequest(')):s.index('\n\tif (!isValidRequest',s.index('int CameraDevice::processCaptureRequest('))]
pos=s.index('\tMutexLocker stateLock(stateMutex_);',s.index('int CameraDevice::processCaptureRequest('))
end=s.index('\n\tif (state_ == State::Stopped)',pos)
branch=s[pos:end]
stub=r'''
#include <atomic>
#include <cassert>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <queue>
#include <thread>
#include <vector>
#define LIBCAMERA_TSA_REQUIRES(x)
using MutexLocker=std::unique_lock<std::mutex>;
struct CV {std::condition_variable cv;template<class P>void wait(MutexLocker &l,P p){cv.wait(l,p);}void notify_one(){cv.notify_one();}};
struct Camera3RequestDescriptor {bool accepted_=false;};
struct Camera {int starts=0,stops=0;void stop(){stops++;}};
struct Stream {void flush(){} void waitFlush(){}};
struct CameraDevice {
 enum class State{Stopped,Running,Flushing};
 class CaptureRequestGuard {public:explicit CaptureRequestGuard(CameraDevice *);~CaptureRequestGuard();bool startedDuringFlush()const{return startedDuringFlush_;}private:CameraDevice *device_;bool startedDuringFlush_;};
 State state_=State::Stopped;std::mutex stateMutex_,descriptorsMutex_;unsigned captureRequestsInProgress_=0;CV captureRequestsFinished_;Camera cam;Camera *camera_=&cam;std::vector<Stream> streams_;std::queue<std::unique_ptr<Camera3RequestDescriptor>> descriptors_;
 std::mutex preparationMutex;std::condition_variable preparationCV;int entered=0,releases=0;std::atomic<int> returned=0;
 void abortRequest(Camera3RequestDescriptor *){}void completeDescriptor(Camera3RequestDescriptor *){descriptors_.pop();returned++;}
 void flush();int processCaptureRequest();
};
'''
process=r'''
int CameraDevice::processCaptureRequest(){
''' + start + r'''
 auto descriptor=std::make_unique<Camera3RequestDescriptor>();
 {std::unique_lock<std::mutex> l(preparationMutex);int slot=++entered;preparationCV.notify_all();preparationCV.wait(l,[&]{return releases>=slot;});}
''' + branch + r'''
 if(state_==State::Stopped){camera_->starts++;state_=State::Running;}
 return 0;
}
'''
main=r'''
int main(){
 CameraDevice d;std::atomic<int> flushed=0;
 std::thread before([&]{assert(d.processCaptureRequest()==0);});
 {std::unique_lock<std::mutex> l(d.preparationMutex);d.preparationCV.wait(l,[&]{return d.entered==1;});}
 std::thread flusher([&]{d.flush();flushed=1;});
 while(true){{std::lock_guard<std::mutex> l(d.stateMutex_);if(d.state_==CameraDevice::State::Flushing)break;}std::this_thread::yield();}
 std::thread during([&]{assert(d.processCaptureRequest()==0);});
 {std::unique_lock<std::mutex> l(d.preparationMutex);d.preparationCV.wait(l,[&]{return d.entered==2;});d.releases=1;}d.preparationCV.notify_all();
 before.join();assert(d.returned==1&&!flushed.load());
 {std::lock_guard<std::mutex> l(d.preparationMutex);d.releases=2;}d.preparationCV.notify_all();during.join();flusher.join();
 assert(d.returned==2&&flushed==1&&d.cam.starts==0&&d.state_==CameraDevice::State::Stopped);
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+flush+guard+process+main)
 subprocess.run(['c++','-std=c++17','-pthread','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True,timeout=10)
print('flush request race test PASS')
