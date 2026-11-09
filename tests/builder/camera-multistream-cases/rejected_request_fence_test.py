#!/usr/bin/env python3
"""Compile actual rejected-request destructor; verify fd ownership."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_request.cpp').read_text()
body=s[s.index('Camera3RequestDescriptor::~Camera3RequestDescriptor()'):s.index('/**\n * \\struct',s.index('Camera3RequestDescriptor::~Camera3RequestDescriptor()'))]
stub=r'''
#include <cassert>
#include <fcntl.h>
#include <unistd.h>
#include <vector>
struct UniqueFD {int fd;explicit UniqueFD(int f):fd(f){} UniqueFD(UniqueFD&&o):fd(o.release()){} ~UniqueFD(){if(fd>=0)close(fd);}[[nodiscard]] int release(){int x=fd;fd=-1;return x;}};
struct Buffer {UniqueFD fence;explicit Buffer(int fd):fence(fd){}};
struct Camera3RequestDescriptor {bool accepted_=false;std::vector<Buffer> buffers_;~Camera3RequestDescriptor();};
'''
main=r'''
int main(){int fds[2];assert(pipe(fds)==0);{Camera3RequestDescriptor d;d.buffers_.emplace_back(fds[0]);}assert(fcntl(fds[0],F_GETFD)>=0);close(fds[0]);close(fds[1]);assert(pipe(fds)==0);{Camera3RequestDescriptor d;d.accepted_=true;d.buffers_.emplace_back(fds[0]);}assert(fcntl(fds[0],F_GETFD)==-1);close(fds[1]);}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('rejected request fence test PASS')
