#!/usr/bin/env python3
"""Exercise actual stop ordering with a simulated active worker and descriptor."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_device.cpp').read_text()
body=s[s.index('void CameraDevice::stop()'):s.index('unsigned int CameraDevice::maxJpegBufferSize()',s.index('void CameraDevice::stop()'))]
stub=r'''
#include <cassert>
#include <memory>
#include <mutex>
#include <queue>
#include <thread>
#include <vector>
using MutexLocker=std::lock_guard<std::mutex>;
struct Descriptor { bool *destroyed; ~Descriptor(){*destroyed=true;} };
struct Camera {void stop(){}};
struct Stream {std::thread worker; void stopWorker(){if(worker.joinable())worker.join();}};
struct CameraDevice {enum class State{Stopped,Running};State state_=State::Running;std::mutex stateMutex_,descriptorsMutex_;Camera cam;Camera *camera_=&cam;std::queue<std::unique_ptr<Descriptor>> descriptors_;std::vector<Stream> streams_;void stop();};
'''
main=r'''
int main(){for(int i=0;i<1000;i++){bool destroyed=false;CameraDevice d;d.descriptors_.push(std::unique_ptr<Descriptor>(new Descriptor{&destroyed}));d.streams_.emplace_back();d.streams_[0].worker=std::thread([&]{assert(!destroyed);});d.stop();assert(destroyed&&d.streams_.empty()&&d.state_==CameraDevice::State::Stopped);}}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-pthread','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('stop lifetime test PASS')
