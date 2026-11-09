#!/usr/bin/env python3
"""Run actual postprocessor worker with blocked work and queued cancellation."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_stream.cpp').read_text()
body=s[s.index('CameraStream::PostProcessorWorker::PostProcessorWorker('):]
stub=r'''
#include <atomic>
#include <cassert>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <queue>
#include <thread>
#define LIBCAMERA_TSA_REQUIRES(x)
#define LIBCAMERA_TSA_GUARDED_BY(x)
#define ASSERT(x) assert(x)
using Mutex=std::mutex;using MutexLocker=std::unique_lock<std::mutex>;
struct ConditionVariable {std::condition_variable cv;template<class P>void wait(MutexLocker &l,P p){cv.wait(l,p);}void notify_one(){cv.notify_one();}};
struct Thread {std::thread thread;virtual ~Thread()=default;virtual void run()=0;void start(){thread=std::thread([this]{run();});}void wait(){if(thread.joinable())thread.join();}};
struct Camera3RequestDescriptor {struct StreamBuffer {int id;};};
struct PostProcessor {
 enum class Status{Success,Error};
 struct Signal{std::function<void(Camera3RequestDescriptor::StreamBuffer *,Status)> f;void emit(Camera3RequestDescriptor::StreamBuffer *b,Status s){f(b,s);}} processComplete;
 std::mutex m;std::condition_variable cv;bool entered=false,release=false;
 void process(Camera3RequestDescriptor::StreamBuffer *b){{std::unique_lock<std::mutex> l(m);entered=true;cv.notify_all();cv.wait(l,[&]{return release;});}processComplete.emit(b,Status::Success);}
};
struct CameraStream {struct PostProcessorWorker:Thread {
 enum class State {Stopped,Running,Flushing};
 explicit PostProcessorWorker(PostProcessor *);~PostProcessorWorker();void start();void queueRequest(Camera3RequestDescriptor::StreamBuffer *);void flush();void waitFlush();void run() override;
 PostProcessor *postProcessor_;Mutex mutex_;ConditionVariable cv_;std::queue<Camera3RequestDescriptor::StreamBuffer *> requests_;State state_=State::Stopped;
};};
'''
main=r'''
int main(){
 PostProcessor p;std::atomic<int> completed=0,cancelled=0,returned=0;
 p.processComplete.f=[&](auto *b,auto s){if(b->id==1){assert(s==PostProcessor::Status::Success);completed++;}else{assert(b->id==2&&s==PostProcessor::Status::Error);cancelled++;}};
 CameraStream::PostProcessorWorker w(&p);w.start();Camera3RequestDescriptor::StreamBuffer a{1},b{2};w.queueRequest(&a);
 {std::unique_lock<std::mutex> l(p.m);p.cv.wait(l,[&]{return p.entered;});}w.queueRequest(&b);w.flush();
 std::thread waiter([&]{w.waitFlush();returned++;});assert(!returned.load());
 {std::lock_guard<std::mutex> l(p.m);p.release=true;}p.cv.notify_all();waiter.join();
 assert(completed==1&&cancelled==1&&returned==1);
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-pthread','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('worker flush test PASS')
