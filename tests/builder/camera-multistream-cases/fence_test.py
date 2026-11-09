#!/usr/bin/env python3
"""Exercise the real CameraStream waitFence implementation using pipe fds."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/camera_stream.cpp').read_text()
body=s[s.index('int CameraStream::waitFence('):s.index('int CameraStream::process(')]
stub='''#include <cassert>\n#include <cerrno>\n#include <poll.h>\n#include <unistd.h>\nstruct CameraStream { int waitFence(int); };\n'''
main=r'''
int main(){CameraStream s;int fds[2];assert(pipe(fds)==0);char c='x';assert(write(fds[1],&c,1)==1);assert(s.waitFence(fds[0])==0);assert(read(fds[0],&c,1)==1);assert(s.waitFence(fds[0])==-ETIME);int invalid=fds[0];close(fds[0]);assert(s.waitFence(invalid)==-EINVAL);close(fds[1]);}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('fence wait test PASS')
