#!/usr/bin/env python3
"""Run actual device flush with a blocked postprocessor and queued callback."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_device.cpp').read_text()
body=s[s.index('void CameraDevice::flush()'):s.index('void CameraDevice::stop()',s.index('void CameraDevice::flush()'))]
stub=r'''
#include <atomic>
#include <cassert>
#include <condition_variable>
#include <mutex>
#include <thread>
#include <vector>
#define LIBCAMERA_TSA_REQUIRES(x)
using MutexLocker=std::unique_lock<std::mutex>;
struct ConditionVariable{std::condition_variable cv;template<class P>void wait(MutexLocker &l,P p){cv.wait(l,p);}};
struct Camera {void stop(){}};
struct Stream {std::thread worker;std::atomic<int> *cancelled;void flush(){cancelled->store(1);}void waitFlush(){if(worker.joinable())worker.join();}};
struct CameraDevice {enum class State{Stopped,Running,Flushing};State state_=State::Running;std::mutex stateMutex_;unsigned captureRequestsInProgress_=0;ConditionVariable captureRequestsFinished_;Camera cam;Camera *camera_=&cam;std::vector<Stream> streams_;void flush();};
'''
main=r'''
int main(){
 CameraDevice d;std::mutex m;std::condition_variable cv;bool entered=false,release=false;std::atomic<int> cancelled=0,callbacks=0,returned=0;
 d.streams_.emplace_back(); d.streams_[0].cancelled=&cancelled;
 d.streams_[0].worker=std::thread([&]{{std::unique_lock<std::mutex> lock(m);entered=true;cv.notify_all();cv.wait(lock,[&]{return release;});}callbacks++;});
 {std::unique_lock<std::mutex> lock(m);cv.wait(lock,[&]{return entered;});}
 std::thread caller([&]{d.flush();returned=1;});
 while(!cancelled.load())std::this_thread::yield();
 {std::lock_guard<std::mutex> lock(d.stateMutex_);assert(d.state_==CameraDevice::State::Flushing);}
 assert(!returned.load() && !callbacks.load());
 {std::lock_guard<std::mutex> lock(m);release=true;}cv.notify_all();caller.join();
 assert(returned==1 && callbacks==1 && d.state_==CameraDevice::State::Stopped);
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-pthread','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('flush blocked worker test PASS')
