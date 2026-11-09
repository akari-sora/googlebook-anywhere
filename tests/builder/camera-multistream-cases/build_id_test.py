#!/usr/bin/env python3
"""Compile and exercise the actual loaded ELF build-id matching code."""
from pathlib import Path
import subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/android/mm/generic_camera_buffer.cpp').read_text()
body=s[s.index('struct BuildIdMatch {'):s.index('bool knownKanoBlobGralloc(')]
stub=r'''
#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>
using Half=unsigned short;
struct Nhdr {uint32_t n_namesz,n_descsz,n_type;};
#define ElfW(x) x
constexpr int PT_LOAD=1,PT_NOTE=4,PF_R=4,NT_GNU_BUILD_ID=3;
struct Phdr {unsigned p_type=0,p_flags=0;uintptr_t p_vaddr=0;size_t p_memsz=0;};
struct dl_phdr_info {uintptr_t dlpi_addr;const Phdr *dlpi_phdr;Half dlpi_phnum;};
static dl_phdr_info fake;
int dl_iterate_phdr(int (*fn)(dl_phdr_info *,size_t,void *),void *data){return fn(&fake,sizeof(fake),data);}
'''
main=r'''
int main(){
 std::vector<uint8_t> memory(256);auto *h=reinterpret_cast<Nhdr *>(memory.data()+16);*h={4,4,NT_GNU_BUILD_ID};memcpy(memory.data()+28,"GNU",4);uint8_t id[]={1,2,3,4};memcpy(memory.data()+32,id,4);
 Phdr p[2]={{PT_LOAD,PF_R,0,memory.size()},{PT_NOTE,0,16,20}};fake={reinterpret_cast<uintptr_t>(memory.data()),p,2};
 const void *address=memory.data()+128;
 assert(loadedBuildIdMatches(address,id,4));uint8_t other[]={1,2,3,5};assert(!loadedBuildIdMatches(address,other,4));
 h->n_type=0;assert(!loadedBuildIdMatches(address,id,4));h->n_type=NT_GNU_BUILD_ID;
 p[1].p_memsz=65537;assert(!loadedBuildIdMatches(address,id,4));p[1].p_memsz=19;assert(!loadedBuildIdMatches(address,id,4));
 p[1].p_memsz=20;p[0].p_flags=0;assert(!loadedBuildIdMatches(address,id,4));assert(!loadedBuildIdMatches(nullptr,id,4));
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'test.cpp';p.write_text(stub+body+main)
 subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(p),'-o',d+'/test'],check=True)
 subprocess.run([d+'/test'],check=True)
print('build ID match test PASS')
