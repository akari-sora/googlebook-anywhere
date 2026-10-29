"""Minimal checked ELF64 little-endian x86-64 reader; Python standard library."""
import struct

class Elf:
    def __init__(self, data):
        self.data = data
        if len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01':
            raise ValueError('expected ELF64 little-endian version 1')
        if struct.unpack_from('<H', data, 18)[0] != 62:
            raise ValueError('expected x86-64 ELF')
        phoff, shoff = struct.unpack_from('<QQ', data, 32)
        phsize, phnum, shsize, shnum, shstr = struct.unpack_from('<HHHHH', data, 54)
        if phsize != 56 or shsize != 64 or not phnum or not shnum:
            raise ValueError('unsupported ELF table layout')
        self.programs = [dict(zip(('type','flags','offset','vaddr','paddr','filesz','memsz','align'),
                                 self.unpack('<IIQQQQQQ', phoff + i * phsize))) for i in range(phnum)]
        self.sections = [dict(zip(('name_offset','type','flags','addr','offset','size','link','info','align','entsize'),
                                 self.unpack('<IIQQQQIIQQ', shoff + i * shsize))) for i in range(shnum)]
        names = self.section_data(self.sections[shstr])
        for section in self.sections:
            section['name'] = self.string(names, section['name_offset'])
    def unpack(self, fmt, offset):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(self.data):
            raise ValueError('truncated ELF structure')
        return struct.unpack_from(fmt, self.data, offset)
    @staticmethod
    def string(data, offset):
        if not 0 <= offset < len(data):
            raise ValueError('invalid ELF string offset')
        end = data.find(b'\0', offset)
        if end < 0:
            raise ValueError('unterminated ELF string')
        return data[offset:end].decode('utf-8', 'replace')
    def section_data(self, section):
        start, size = section['offset'], section['size']
        if start + size > len(self.data):
            raise ValueError('truncated ELF section')
        return self.data[start:start + size]
    def v2o(self, address, size=1, executable=False):
        matches = [p for p in self.programs if p['type'] == 1 and
                   p['vaddr'] <= address and address + size <= p['vaddr'] + p['filesz']
                   and (not executable or p['flags'] & 1)]
        if len(matches) != 1:
            raise ValueError(f'VA {address:#x} is not in one file-backed load segment')
        p = matches[0]
        return p['offset'] + address - p['vaddr']
    def at(self, address, size):
        offset = self.v2o(address, size)
        return self.data[offset:offset+size]
    def symbols(self, table=None):
        tables = [table] if table else [s for s in self.sections if s['type'] in (2,11)]
        for section in tables:
            if section['entsize'] != 24 or section['size'] % 24:
                raise ValueError('unsupported symbol table')
            names = self.section_data(self.sections[section['link']])
            for off in range(section['offset'], section['offset'] + section['size'], 24):
                name, info, other, index, value, size = self.unpack('<IBBHQQ', off)
                yield dict(name=self.string(names,name),info=info,other=other,index=index,value=value,size=size)
