#!/usr/bin/env python3
"""Exercise actual private gralloc ABI selector with controlled identities."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/mm/generic_camera_buffer.cpp').read_text()
body=s[s.index('bool knownKanoBlobGralloc('):s.index('} /* namespace */')]
stub=r'''
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstring>
struct Common {const char *id="gralloc",*name="CrOS Gralloc";void *dso=(void*)1;int module_api_version=2;};
struct gralloc_module_t {Common common;void(*perform)()=nullptr;void(*registerBuffer)()=nullptr;void(*unregisterBuffer)()=nullptr;void(*lock)()=nullptr;void(*unlock)()=nullptr;};
constexpr auto GRALLOC_HARDWARE_MODULE_ID="gralloc";constexpr int GRALLOC_MODULE_API_VERSION_0_2=2;
struct Dl_info {};static bool own=true, dep=true, found=true;
int dladdr(const void*,Dl_info*){return found;}void *dlsym(void*,const char*){return dep?(void*)2:nullptr;}
bool loadedBuildIdMatches(const void *p,const uint8_t *id,size_t n){return p&&n==16&&(id[0]==0xa7?own:dep);}
void fn(){}
'''
main=r'''
int main(){gralloc_module_t m;m.perform=fn;m.registerBuffer=fn;m.unregisterBuffer=fn;m.lock=fn;m.unlock=fn;assert(knownKanoBlobGralloc(&m));own=false;assert(!knownKanoBlobGralloc(&m));own=true;dep=false;assert(!knownKanoBlobGralloc(&m));dep=true;found=false;assert(!knownKanoBlobGralloc(&m));found=true;m.common.name="other";assert(!knownKanoBlobGralloc(&m));m.common.name="CrOS Gralloc";m.common.module_api_version=1;assert(!knownKanoBlobGralloc(&m));m.common.module_api_version=2;m.perform=nullptr;assert(!knownKanoBlobGralloc(&m));assert(!knownKanoBlobGralloc(nullptr));}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('gralloc identity guard test PASS')
