#!/usr/bin/env python3
"""Fail-closed, same-length 15-SHA Adler32 scalar-gate patch PoC.

Only reviewed whole-file SHA256s, exact ELF mappings, detector/wrapper/VNNI
contexts are accepted. This operates on an exclusively owned extracted tree;
container rebuilding/signing is a separate, deliberately unimplemented gate.
"""
import argparse,hashlib,json,os,stat,struct,tempfile
from pathlib import Path
class PatchError(Exception):pass
def sha(data):return hashlib.sha256(data).hexdigest()
def require(ok,msg):
 if not ok:raise PatchError(msg)
def resolve_plain(path):
 path=Path(path).absolute();parts=path.parts;cur=Path(parts[0])
 for part in parts[1:]:
  cur=cur/part
  require(not stat.S_ISLNK(cur.lstat().st_mode),'symlink refused: '+str(cur))
 require(path.is_file(),'not a regular file')
 require(path.stat().st_nlink==1,'hardlinked input refused')
 return path
def output_report(path,root,manifest):
 # Refuse aliases before any ELF mutation. In particular a report must never
 # live inside the extracted target tree or replace the trusted manifest.
 path=Path(path).absolute();require('..' not in path.parts,'report traversal refused')
 cur=Path(path.parts[0])
 for part in path.parts[1:]:
  cur/=part
  if cur.exists() or cur.is_symlink():
   require(not cur.is_symlink(),'report symlink refused')
  else:require(cur==path,'report parent does not exist')
 if path.exists():
  st=path.stat();require(stat.S_ISREG(st.st_mode) and st.st_nlink==1,'report must be a plain unlinked file')
 path=path.parent.resolve(strict=True)/path.name
 require(path!=manifest and not path.is_relative_to(root),'report aliases manifest or target tree')
 tools=Path(__file__).resolve().parent
 require(path.is_relative_to(tools) and path.parent!=tools and path.parent.name=='reports' and path.suffix=='.json','report must be a JSON file in a dedicated step2 reports directory')
 return path
def target_metadata(path):
 st=path.stat()
 require(st.st_uid==os.geteuid() and st.st_gid==os.getegid(),'foreign uid/gid refused by narrow PoC')
 require(not st.st_mode&0o7000,'special permission bits refused by narrow PoC')
 require(not os.listxattr(path),'xattrs/ACL/capabilities require explicit container integration')
 return st
def atomic_report(path,payload):
 fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as out:json.dump(payload,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
  os.replace(tmp,path)
  dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
class Elf:
 def __init__(self,d):
  require(len(d)>=64 and d[:7]==b'\x7fELF\x02\x01\x01','not ELF64 little-endian')
  require(struct.unpack_from('<H',d,18)[0]==62,'not x86-64')
  off=struct.unpack_from('<Q',d,32)[0];ent,n=struct.unpack_from('<HH',d,54)
  require(ent==56 and off+n*ent<=len(d),'invalid program headers');self.loads=[]
  for i in range(n):
   typ,flags,fo,va,pa,fs,ms,align=struct.unpack_from('<IIQQQQQQ',d,off+i*ent)
   if typ==1:
    require(fs<=ms and fo+fs<=len(d),'invalid PT_LOAD');self.loads.append((fo,va,fs,ms,flags))
 def mapping(self,va,n,executable=False,writable=False,bss=False):
  matches=[]
  for off,v,fs,ms,flags in self.loads:
   if v<=va and va+n<=v+(ms if bss else fs) and (not executable or flags&1) and (not writable or flags&2):matches.append(off+va-v)
  require(len(matches)==1,'ambiguous, absent, or wrong-permission mapping');return matches[0]
def validate(data,r):
 require(len(data)==r['size'],'file size mismatch');e=Elf(data)
 require([list(x) for x in e.loads]==r['elf_loads'],'PT_LOAD table mismatch')
 require(r['layout'] in {'classic','libz'},'unsupported wrapper layout')
 require((r['layout']=='libz')==(r['id'] in {2,73,122}),'ID/layout mismatch')
 require(r['original_hex']=='2500020000' and r['replacement_hex']=='2500000000','unsupported replacement')
 old=bytes.fromhex(r['original_hex']);new=bytes.fromhex(r['replacement_hex'])
 require(len(old)==len(new)==5,'replacement length')
 require(r['patch_va']==r['detector_va']+15 and r['patch_offset']==r['detector_offset']+15,'detector relationship')
 require(r['detector_size']==50,'detector size')
 require(r['crc_gate_va']==r['gate_va']+4,'CRC gate relationship')
 require(e.mapping(r['gate_va'],8,writable=True,bss=True)>=0,'gate mapping')
 for field,n in [('detector',64),('wrapper',len(bytes.fromhex(r['wrapper_context_hex']))),('patch',5),('vnni',5)]:
  require(e.mapping(r[field+'_va'],n,executable=True)==r[field+'_offset'],field+' mapping mismatch')
 # Bind dispatcher guard, direct call and helper to the exact reviewed mapping.
 for name,n in [('call',5),('helper',64),('gate_check',13)]:
  require(e.mapping(r[name+'_va'],n,executable=True)==r[name+'_offset'],name+' mapping mismatch')
  x=bytes.fromhex(r[name+'_context_hex'])
  require(len(x)==n and data[r[name+'_offset']:r[name+'_offset']+n]==x,name+' context differs')
 call=bytes.fromhex(r['call_context_hex'])
 require(call[0]==0xe8 and r['call_va']+5+struct.unpack_from('<i',call,1)[0]==r['helper_va'],'helper call target')
 require(r['helper_va']<=r['vnni_va']<r['helper_va']+0x1000,'VNNI outside reviewed helper')
 check=bytes.fromhex(r['gate_check_context_hex'])
 require(check[:2]==b'\x83\x3d' and check[6]==0 and r['gate_check_va']+7+struct.unpack_from('<i',check,2)[0]==r['gate_va'],'dispatcher gate target')
 wrapper=bytes.fromhex(r['wrapper_context_hex'])
 require(len(wrapper)==192 and len(bytes.fromhex(r['vnni_hex']))==5,'wrapper/VNNI context length')
 if r['layout']=='classic':
  require(wrapper[:20].hex()=='554889e553504889d04885f6741e4883f8407218','classic wrapper prologue')
  require(r['gate_check_va']==r['wrapper_va']+0x14 and r['call_va']==r['wrapper_va']+0x20,'classic dispatcher layout')
  require(check[7:].hex()=='740f4889c2e8','classic scalar branch')
 else:
  require(wrapper[:19].hex()=='554889e54885f6740a4883fa400f839b000000','libz wrapper prologue')
  require(r['gate_check_va']==r['wrapper_va']+0xae and r['call_va']==r['wrapper_va']+0xbb,'libz dispatcher layout')
  require(check[7:9]==b'\x0f\x84' and r['gate_check_va']+13+struct.unpack_from('<i',check,9)[0]==r['wrapper_va']+0x13,'libz scalar branch')
 context=bytes.fromhex(r['context_hex']);require(len(context)==64 and context[15:20]==old,'bad detector context')
 require(context[:15].hex()=='b8010000004887f30fa24887f389c8','detector prefix')
 require(context[26:43].hex()=='c1ea1a89c8c1e81421d0d1e921c183e101' and context[49]==0xc3,'CRC computation/return changed')
 # Verify both RIP-relative stores still point at the reviewed Adler and CRC gates.
 require(context[20:22]==b'\x89\x05' and r['detector_va']+26+struct.unpack_from('<i',context,22)[0]==r['gate_va'],'Adler store target')
 require(context[43:45]==b'\x89\x0d' and r['detector_va']+49+struct.unpack_from('<i',context,45)[0]==r['crc_gate_va'],'CRC store target')
 actual=sha(data)
 if actual==r['sha256']:state='original';want=context
 elif actual==r['patched_sha256']:state='patched';want=context[:15]+new+context[20:]
 else:raise PatchError('unknown full-file SHA256 '+actual)
 require(data[r['detector_offset']:r['detector_offset']+64]==want,'detector context differs')
 for name,value in [('wrapper',r['wrapper_context_hex']),('vnni',r['vnni_hex'])]:
  x=bytes.fromhex(value);o=r[name+'_offset'];require(data[o:o+len(x)]==x,name+' context differs')
 o=r['patch_offset'];patched=data[:o]+new+data[o+5:]
 require(len(patched)==len(data) and sha(patched)==r['patched_sha256'],'patched hash mismatch')
 return state,patched

def main():
 p=argparse.ArgumentParser();p.add_argument('--manifest',required=True);p.add_argument('--root',required=True);p.add_argument('--apply',action='store_true');p.add_argument('--report',required=True);a=p.parse_args()
 manifest=resolve_plain(a.manifest)
 m=json.loads(manifest.read_text());require(m.get('schema')==1 and m.get('policy')=='force_scalar_adler_only_preserve_crc_gate','bad manifest schema/policy')
 rows=m.get('targets',[]);ids={1,2,3,4,27,33,67,68,73,77,79,82,84,88,122}
 require(len(rows)==15 and {r['id'] for r in rows}==ids,'expected exactly 15 reviewed Adler IDs')
 require(all(r['name']==f"id-{r['id']:03d}" for r in rows),'ID/name mismatch')
 require(len({r['path'] for r in rows})==len({r['sha256'] for r in rows})==15,'duplicate path/hash')
 root=Path(a.root).absolute();require('..' not in root.parts,'root traversal refused')
 # Only disposable trees below this remote tool directory can be mutated.
 # Resolve every ancestor before accepting root to avoid a symlink bypass.
 cur=Path(root.parts[0])
 for part in root.parts[1:]:
  cur/=part;require(not cur.is_symlink(),'root symlink refused')
 root=root.resolve(strict=True);tools=Path(__file__).resolve().parent
 require(root!=tools and root.is_relative_to(tools),'source tree refused: root must be a disposable step2 subtree')
 report_path=output_report(a.report,root,manifest);todo=[]
 for r in rows:
  rel=Path(r['path']);require(not rel.is_absolute() and '..' not in rel.parts,'unsafe relative path')
  f=resolve_plain(root/rel);require(f.is_relative_to(root),'path escapes root')
  metadata=target_metadata(f)
  data=f.read_bytes();state,patched=validate(data,r);todo.append((r,f,data,state,patched,metadata))
 report=[]
 # Validate the complete target set before the first mutation.
 for r,f,data,state,patched,metadata in todo:
  if a.apply and state=='original':
   require(resolve_plain(f).read_bytes()==data,'concurrent modification detected')
   current=target_metadata(f)
   require((current.st_dev,current.st_ino,current.st_mode,current.st_uid,current.st_gid)==(metadata.st_dev,metadata.st_ino,metadata.st_mode,metadata.st_uid,metadata.st_gid),'target metadata changed')
   fd,tmp=tempfile.mkstemp(prefix='.'+f.name+'.adler-',dir=f.parent)
   try:
    with os.fdopen(fd,'wb') as out:out.write(patched);out.flush();os.fsync(out.fileno())
    os.chown(tmp,metadata.st_uid,metadata.st_gid)
    os.chmod(tmp,stat.S_IMODE(metadata.st_mode))
    os.utime(tmp,ns=(metadata.st_atime_ns,metadata.st_mtime_ns))
    os.replace(tmp,f)
    dfd=os.open(f.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(dfd)
    finally:os.close(dfd)
   finally:
    if os.path.exists(tmp):os.unlink(tmp)
  result=f.read_bytes();expected=r['patched_sha256'] if a.apply or state=='patched' else r['sha256']
  require(sha(result)==expected,'final readback mismatch')
  report.append({'name':r['name'],'path':r['path'],'before':state,'after':'patched' if a.apply else state,'sha256':sha(result),'size':len(result),'changed_byte_offsets':[i for i,(x,y) in enumerate(zip(data,result)) if x!=y]})
 atomic_report(report_path,{'schema':1,'apply':a.apply,'targets':report})
 print(json.dumps({'apply':a.apply,'states':[x['after'] for x in report],'count':len(report)}))
if __name__=='__main__':
 try:main()
 except (PatchError,OSError,ValueError,KeyError,struct.error) as e:raise SystemExit('REFUSED: '+str(e))
