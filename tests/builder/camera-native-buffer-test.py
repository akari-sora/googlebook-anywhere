#!/usr/bin/env python3
"""Compile and exercise extracted production methods with deliberately malformed handles."""
import argparse
import pathlib
import subprocess
import tempfile

p = argparse.ArgumentParser()
p.add_argument('--source', required=True, type=pathlib.Path, help='patched libcamera source tree')
a = p.parse_args()

def method(path, marker):
    data = path.read_text()
    start = data.index(marker)
    opening = data.index('{', start)
    depth = 0
    for i in range(opening, len(data)):
        if data[i] == '{': depth += 1
        elif data[i] == '}':
            depth -= 1
            if not depth: return data[start:i + 1]
    raise ValueError(marker)

generic = method(a.source / 'src/android/mm/generic_camera_buffer.cpp', 'CameraBuffer::Private::Private(')
create = method(a.source / 'src/android/camera_device.cpp', 'std::unique_ptr<HALFrameBuffer>\nCameraDevice::createFrameBuffer(')
code = r'''
#include <cerrno>
#include <climits>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <fcntl.h>
#include <memory>
#include <sstream>
#include <string>
#include <sys/mman.h>
#include <sys/types.h>
#include <unistd.h>
#include <vector>
struct Log { std::string severity; explicit Log(const char *s) : severity(s) {} ~Log() { if (severity == "Fatal") std::abort(); } template<class T> Log &operator<<(const T &) { return *this; } };
#define LOG(category, severity) Log(#severity)
struct Handle { int version = 1; int numFds = 0; int numInts = 0; int data[4] = {-1,-1,-1,-1}; };
using buffer_handle_t = const Handle *;
using PixelFormat = int;
struct Size { unsigned int width = 0, height = 0; };
namespace formats { constexpr PixelFormat NV12=12, MJPEG=99; }
constexpr int HAL_PIXEL_FORMAT_BLOB=0x21, GRALLOC_HARDWARE_MODULE_ID=1, GRALLOC_MODULE_API_VERSION_0_2=2;
constexpr int GRALLOC_USAGE_SW_READ_OFTEN=1, GRALLOC_USAGE_SW_WRITE_OFTEN=2;
struct hw_module_t { int module_api_version=2; };
struct android_ycbcr { void *y=nullptr, *cb=nullptr, *cr=nullptr; size_t ystride=0, cstride=0, chroma_step=0; };
struct gralloc_module_t : hw_module_t {
 int (*registerBuffer)(const gralloc_module_t *,buffer_handle_t)=nullptr;
 int (*unregisterBuffer)(const gralloc_module_t *,buffer_handle_t)=nullptr;
 int (*lock_ycbcr)(const gralloc_module_t *,buffer_handle_t,int,int,int,unsigned,unsigned,android_ycbcr *)=nullptr;
 int (*lock)(const gralloc_module_t *,buffer_handle_t,int,int,int,unsigned,unsigned,void **)=nullptr;
 int (*unlock)(const gralloc_module_t *,buffer_handle_t)=nullptr;
 int (*perform)(const gralloc_module_t *,int,...)=nullptr;
};
static int hw_get_module(int,const hw_module_t **) { return -ENOTSUP; }
static bool knownKanoBlobGralloc(const gralloc_module_t *) { return false; }
struct Span { Span(uint8_t *,uint64_t) {} };
static off_t fakeLength = 0;
static off_t fake_lseek(int, off_t, int) { return fakeLength; }
#define lseek fake_lseek
struct PixelFormatInfo {
    bool valid = true;
    std::vector<unsigned int> sizes;
    static PixelFormatInfo current;
    static const PixelFormatInfo &info(PixelFormat) { return current; }
    bool isValid() const { return valid; }
    unsigned int numPlanes() const { return sizes.size(); }
    unsigned int planeSize(const Size &, unsigned int i) const { return sizes[i]; }
    unsigned int stride(unsigned int, unsigned int, unsigned int) const { return 1; }
};
PixelFormatInfo PixelFormatInfo::current;
static bool directValid = true;
static unsigned int directPlanes = 1;
static bool directConstructed = false;
class CameraBuffer {
public:
    class Private;
    CameraBuffer(buffer_handle_t, PixelFormat, const Size &, int) { directConstructed = true; }
    bool isValid() const { return directValid; }
    unsigned int numPlanes() const { return directPlanes; }
    unsigned int offset(unsigned int i) const { return i * 4; }
    unsigned int size(unsigned int) const { return 4; }
};
class CameraBuffer::Private {
public:
    struct PlaneInfo { unsigned int stride, offset, size; };
    int fd_, flags_; off_t bufferLength_; bool mapped_; int error_;
    const gralloc_module_t *gralloc_=nullptr; buffer_handle_t handle_=nullptr;
    bool registered_=false, grallocLocked_=false; size_t blobLength_=0;
    std::vector<PlaneInfo> planeInfo_; std::vector<Span> planes_;
    Private(CameraBuffer *, buffer_handle_t, PixelFormat, const Size &, int, bool=false);
    bool isValid() const { return error_ == 0; }
};
static int failDupAt = 0, dupCalls = 0, firstDuplicatedFd = -1;
static int test_dup(int fd) {
    ++dupCalls;
    if (failDupAt == dupCalls) { errno = EMFILE; return -1; }
    int result = ::dup(fd);
    if (firstDuplicatedFd == -1 && result >= 0) firstDuplicatedFd = result;
    return result;
}
#define dup test_dup
struct UniqueFD {
    int n; explicit UniqueFD(int fd) : n(fd) {}
    UniqueFD(UniqueFD &&other) : n(other.release()) {}
    UniqueFD(const UniqueFD &) = delete;
    ~UniqueFD() { if (n >= 0) ::close(n); }
    int release() { int result=n; n=-1; return result; }
    bool isValid() const { return n >= 0; }
};
struct SharedFD {
    std::shared_ptr<int> n;
    SharedFD() = default;
    explicit SharedFD(const int &fd) {
        if (fd < 0) return;
        int copied=::dup(fd);
        if (copied < 0) std::abort();
        n.reset(new int(copied), [](int *p) { ::close(*p); delete p; });
    }
    explicit SharedFD(UniqueFD fd) { int adopted=fd.release(); if (adopted >= 0) n.reset(new int(adopted), [](int *p) { ::close(*p); delete p; }); }
    bool isValid() const { return bool(n); }
    int get() const { return n ? *n : -1; }
};
struct FrameBuffer { struct Plane { SharedFD fd; unsigned int offset, length; }; };
struct HALFrameBuffer { std::vector<FrameBuffer::Plane> planes; HALFrameBuffer(const std::vector<FrameBuffer::Plane> &p, buffer_handle_t) : planes(p) {} };
class CameraDevice { public: std::unique_ptr<HALFrameBuffer> createFrameBuffer(buffer_handle_t, PixelFormat, const Size &); };
''' + generic + '\n' + create + r'''
int main(int argc, char **argv) {
    if (argc != 2) return 90;
    std::string name = argv[1]; Handle h; h.numFds = 1; h.data[0] = ::open("/dev/null", O_RDONLY); h.data[1] = ::open("/dev/null", O_RDONLY); if (h.data[0] < 0 || h.data[1] < 0) return 92;
    Size s{4,4}; CameraDevice device;
    if (name == "null") { auto x = device.createFrameBuffer(nullptr, 1, s); return (!x && !directConstructed) ? 0 : 1; }
    if (name == "negative-fds") { h.numFds = -1; auto x=device.createFrameBuffer(&h,1,s); return (!x && !directConstructed) ? 0 : 2; }
    if (name == "negative-ints") { h.numInts = -1; auto x=device.createFrameBuffer(&h,1,s); return (!x && !directConstructed) ? 0 : 3; }
    if (name == "zero-fds") { h.numFds = 0; auto x=device.createFrameBuffer(&h,1,s); return (!x && !directConstructed) ? 0 : 4; }
    if (name == "zero-planes") { directPlanes = 0; auto x=device.createFrameBuffer(&h,1,s); return !x ? 0 : 5; }
    if (name == "metadata-canary") { directPlanes = 2; h.numInts = 1; auto x=device.createFrameBuffer(&h,1,s); return !x ? 0 : 6; }
    if (name == "too-few-fds") { directPlanes = 3; h.numFds = 2; h.numInts = 1; h.data[2] = h.data[1]; auto x=device.createFrameBuffer(&h,1,s); return !x ? 0 : 7; }
    if (name == "closed-fd") { int dead=::open("/dev/null",O_RDONLY); if(dead<0) return 93; ::close(dead); h.data[0]=dead; auto x=device.createFrameBuffer(&h,1,s); return !x ? 0 : 19; }
    if (name == "dup-emfile") { failDupAt=1; auto x=device.createFrameBuffer(&h,1,s); return !x && ::fcntl(h.data[0],F_GETFD)>=0 ? 0 : 20; }
    if (name == "dup-second-failure") { directPlanes=2; h.numFds=2; failDupAt=2; auto x=device.createFrameBuffer(&h,1,s); errno=0; bool closed=firstDuplicatedFd>=0 && ::fcntl(firstDuplicatedFd,F_GETFD)==-1 && errno==EBADF; return !x && closed && ::fcntl(h.data[0],F_GETFD)>=0 && ::fcntl(h.data[1],F_GETFD)>=0 ? 0 : 21; }
    if (name == "dup-owned-lifetime") { auto x=device.createFrameBuffer(&h,1,s); if(!x || firstDuplicatedFd<0 || ::fcntl(firstDuplicatedFd,F_GETFD)<0) return 22; x.reset(); errno=0; bool closed=::fcntl(firstDuplicatedFd,F_GETFD)==-1 && errno==EBADF; return closed && ::fcntl(h.data[0],F_GETFD)>=0 ? 0 : 23; }
    if (name == "invalid-fd") { h.data[0] = -1; auto x=device.createFrameBuffer(&h,1,s); return !x ? 0 : 8; }
    if (name == "invalid-buffer") { directValid = false; auto x=device.createFrameBuffer(&h,1,s); return !x ? 0 : 9; }
    if (name == "one-plane") { auto x=device.createFrameBuffer(&h,1,s); return x && x->planes.size()==1 && x->planes[0].fd.get()>=0 ? 0 : 10; }
    if (name == "two-planes") { directPlanes=2; h.numFds=2; auto x=device.createFrameBuffer(&h,1,s); return x && x->planes.size()==2 && x->planes[1].fd.get()>=0 ? 0 : 11; }
    if (name == "generic-discontiguous") { PixelFormatInfo::current.sizes={4,4}; fakeLength=8; h.numFds=2; CameraBuffer::Private x(nullptr,&h,1,s,PROT_READ); return !x.isValid() && x.error_ == -EINVAL ? 0 : 16; }
    if (name == "generic-repeated-fd") { PixelFormatInfo::current.sizes={4,4}; fakeLength=8; h.numFds=2; h.data[1]=h.data[0]; CameraBuffer::Private x(nullptr,&h,1,s,PROT_READ); return x.isValid() ? 0 : 17; }
    if (name == "generic-no-fd") { PixelFormatInfo::current.sizes={4}; fakeLength=4; h.data[0]=-1; CameraBuffer::Private x(nullptr,&h,1,s,PROT_READ); return !x.isValid() && x.error_ == -EINVAL ? 0 : 18; }
    if (name == "generic-exact") { PixelFormatInfo::current.sizes={4,4}; fakeLength=8; CameraBuffer::Private x(nullptr,&h,1,s,PROT_READ); return x.isValid() ? 0 : 12; }
    if (name == "generic-short") { PixelFormatInfo::current.sizes={4,4}; fakeLength=7; CameraBuffer::Private x(nullptr,&h,1,s,PROT_READ); return !x.isValid() && x.error_ == -EINVAL ? 0 : 13; }
    if (name == "generic-first-short") { PixelFormatInfo::current.sizes={9}; fakeLength=8; CameraBuffer::Private x(nullptr,&h,1,s,PROT_READ); return !x.isValid() && x.error_ == -EINVAL ? 0 : 14; }
    if (name == "generic-overflow") { PixelFormatInfo::current.sizes={UINT_MAX-3,8}; fakeLength=(off_t)UINT_MAX+64; CameraBuffer::Private x(nullptr,&h,1,s,PROT_READ); return !x.isValid() && x.error_ == -EINVAL ? 0 : 15; }
    return 91;
}
'''
tests = ['null', 'negative-fds', 'negative-ints', 'zero-fds', 'zero-planes', 'metadata-canary', 'too-few-fds', 'closed-fd', 'dup-emfile', 'dup-second-failure', 'dup-owned-lifetime', 'invalid-fd', 'invalid-buffer', 'one-plane', 'two-planes', 'generic-exact', 'generic-short', 'generic-first-short', 'generic-overflow', 'generic-discontiguous', 'generic-repeated-fd', 'generic-no-fd']
with tempfile.TemporaryDirectory(prefix='native-handle-test-') as d:
    c = pathlib.Path(d) / 'test.cpp'
    exe = pathlib.Path(d) / 'test'
    c.write_text(code)
    subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror', str(c), '-o', str(exe)], check=True)
    failures = []
    for test in tests:
        r = subprocess.run([str(exe), test], capture_output=True)
        print(f'{test}: {"PASS" if r.returncode == 0 else "FAIL " + str(r.returncode)}')
        if r.returncode: failures.append(test)
    print(f'{len(tests) - len(failures)}/{len(tests)} passed')
    raise SystemExit(bool(failures))
