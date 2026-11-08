"""Parse QEMU probe records; identical full kernel records are mirrored."""
import re

def probe_checks(log):
    lines=[]
    seen_kernel=set()
    for line in log.splitlines():
        if re.match(r"^<\d+>\[", line):
            if line in seen_kernel:
                continue
            seen_kernel.add(line)
        lines.append(line)
    text='\n'.join(lines)
    tag=r'GBOS_QEMU_PROBE(?:\s*\(\s*\d+\s*\))?\s*:\s*'
    records=re.findall(r'GBOS_QEMU_PROBE(?:\s*\(\s*(\d+)\s*\))?\s*:\s*((?:PROBE_TX|PROBE_PASS|LIFECYCLE_|CONCURRENT_)[^\r\n]*)',text)
    pids=[pid for pid,_ in records]
    pid_ok=bool(pids) and all(pid and int(pid)>0 for pid in pids) and len(set(pids))==1
    payloads=re.findall(tag+r'(PROBE_TX[^\r\n]*)',text)
    parsed=[re.fullmatch(r'PROBE_TX code=(\d+) result=(bound|unsupported|denied)',v) for v in payloads]
    rows=[m.groups() for m in parsed if m is not None]
    expected={str(n):('bound' if n in (3,4) else 'unsupported' if n==11 else 'denied') for n in range(1,15)}
    lifecycle=re.findall(tag+r'((?:LIFECYCLE_|CONCURRENT_)[^\r\n]*)',text)
    lifecycle_expected=['LIFECYCLE_CHILD_READY_64_OVERFLOW_REJECTED','LIFECYCLE_CHILD_EXITED','LIFECYCLE_RECLAIMED_64_IDLE','LIFECYCLE_REPLACEMENTS_UNBOUND_64','CONCURRENT_DISTINCT_BIND_UNBIND_32']
    return {'probe single positive PID':pid_ok,
            'probe lifecycle complete':pid_ok and lifecycle==lifecycle_expected and len(re.findall(tag+r'PROBE_PASS\b',text))==1 and text.rfind('CONCURRENT_DISTINCT_BIND_UNBIND_32') < text.rfind('PROBE_PASS') and not re.search(tag+r'PROBE_FAIL\b',text),
            'probe all 14 outcomes':pid_ok and len(payloads)==14 and len(rows)==14 and dict(rows)==expected and len({k for k,v in rows})==14,
            'probe explicit pass':pid_ok and len(re.findall(tag+r'PROBE_PASS\b',text))==1 and not re.search(tag+r'PROBE_FAIL\b',text)}
