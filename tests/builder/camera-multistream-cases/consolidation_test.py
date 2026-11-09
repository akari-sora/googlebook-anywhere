#!/usr/bin/env python3
"""Compile and exercise the candidate's real stream consolidation function."""
from pathlib import Path
import subprocess
import tempfile
source = (Path(__file__).resolve().parents[1] / 'src/android/camera_device.cpp').read_text()
body = source[source.index('bool consolidateJpegNv12('):source.index('void sortCamera3StreamConfigs(')]
stub = r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <utility>
#include <vector>
struct Size { unsigned width, height; };
namespace formats { constexpr int NV12 = 12; }
constexpr int GRALLOC_USAGE_HW_CAMERA_WRITE=1, GRALLOC_USAGE_SW_READ_OFTEN=2, GRALLOC_USAGE_SW_WRITE_OFTEN=4;
struct camera3_stream_t { unsigned width, height; unsigned usage=0; };
struct CameraStream { enum class Type { Direct, Internal, Mapped }; };
struct Camera3StreamConfig { struct Cfg { Size size; int pixelFormat; } config; struct Entry { camera3_stream_t *stream; CameraStream::Type type; }; std::vector<Entry> streams; };
'''
main = r'''
int main() {
 using T = CameraStream::Type;
 camera3_stream_t preview{1280,720}, yuv{1280,720}, jpeg{1924,1092};
 std::vector<Camera3StreamConfig> x={{{{1280,720},12},{}},{{{1924,1092},12},{}}};
 x[0].streams={{&preview,T::Direct},{&yuv,T::Mapped}}; x[1].streams={{&jpeg,T::Internal}};
 assert(consolidateJpegNv12(x,&jpeg));
 assert(x.size()==1 && x[0].config.size.width==1924 && x[0].streams.size()==3);
 assert(x[0].streams[0].stream==&jpeg && x[0].streams[0].type==T::Internal);
 assert(x[0].streams[1].type==T::Mapped && x[0].streams[2].type==T::Mapped);
 assert(preview.usage & GRALLOC_USAGE_SW_WRITE_OFTEN);
 assert(jpeg.usage & GRALLOC_USAGE_SW_READ_OFTEN);
 std::vector<Camera3StreamConfig> bigger={{{{2000,1200},12},{}},{{{1924,1092},12},{}}};
 bigger[0].streams={{&preview,T::Direct}}; bigger[1].streams={{&jpeg,T::Internal}};
 assert(!consolidateJpegNv12(bigger,&jpeg) && bigger.size()==2);
 bigger[0].config.pixelFormat=99;
 assert(!consolidateJpegNv12(bigger,&jpeg));
 assert(!consolidateJpegNv12(bigger,nullptr));
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp'; p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror','-Wno-missing-field-initializers',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('consolidation test PASS')
