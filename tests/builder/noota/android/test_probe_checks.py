import unittest
from probe_checks import probe_checks
class T(unittest.TestCase):
 def record(self,n):
  result='bound' if n in (3,4) else 'unsupported' if n==11 else 'denied'
  return '<14>[ %3d.123][ T20] kano-logcat: 11-07 13:05:59.806 E/GBOS_QEMU_PROBE( 3963): PROBE_TX code=%d result=%s'%(n,n,result)
 def test_actual_format_and_exact_mirrors(self):
  lines=[self.record(n) for n in range(1,15)];end='<14>[ 20.123][ T20] kano-logcat: 11-07 13:06:00.806 E/GBOS_QEMU_PROBE( 3963): PROBE_PASS'; log='\n'.join(v for row in lines+[end] for v in (row,row));self.assertTrue(all(v for k,v in probe_checks(log).items() if k!='probe lifecycle complete'))
  ready='<14>[   29.539175][ T1391] kano-logcat: 11-07 13:05:59.806 E/GBOS_UPDATE(  899): GBOS_UPDATE_READY';self.assertTrue(all(v for k,v in probe_checks(ready+'\n'+ready+'\n'+log).items() if k!='probe lifecycle complete'))
 def test_distinct_duplicates_fail(self):
  lines=[self.record(n) for n in range(1,15)];lines.extend([self.record(1).replace('[   1.123]','[  21.123]'), 'GBOS_QEMU_PROBE: PROBE_PASS']);self.assertFalse(probe_checks('\n'.join(lines))['probe all 14 outcomes'])
 def test_missing_and_fail(self):
  lines=[self.record(n) for n in range(1,14)]+['GBOS_QEMU_PROBE: PROBE_PASS'];self.assertFalse(probe_checks('\n'.join(lines))['probe all 14 outcomes']);self.assertFalse(probe_checks('\n'.join(lines+['GBOS_QEMU_PROBE: PROBE_FAIL']))['probe explicit pass'])
 def test_malformed_extra_records_fail(self):
  lines=[self.record(n) for n in range(1,15)]+['GBOS_QEMU_PROBE: PROBE_PASS']
  for extra in ('PROBE_TX code=3 result=error','PROBE_TX code=broken','PROBE_TX code=3 result=bound extra=1'):
   self.assertFalse(probe_checks('\n'.join(lines+['GBOS_QEMU_PROBE: '+extra]))['probe all 14 outcomes'])
 def test_interleaved_sanitized_records(self):
  lines=[self.record(n) for n in range(1,15)]
  lines.insert(7,'<14>[ 50.123][ T20] E/OTHER( 100): unrelated')
  lines.append('GBOS_QEMU_PROBE( 3963): PROBE_PASS')
  checks=probe_checks('\n'.join(lines));self.assertTrue(checks['probe all 14 outcomes']);self.assertTrue(checks['probe explicit pass'])
 def test_direct_duplicate_is_not_a_mirror(self):
  lines=['GBOS_QEMU_PROBE: PROBE_TX code=%d result=%s'%(n,'bound' if n in (3,4) else 'unsupported' if n==11 else 'denied') for n in range(1,15)];lines.insert(1,lines[0]);self.assertFalse(probe_checks('\n'.join(lines))['probe all 14 outcomes'])
 def test_direct_tag(self):
  log='\n'.join('GBOS_QEMU_PROBE: PROBE_TX code=%d result=%s'%(n,'bound' if n in (3,4) else 'unsupported' if n==11 else 'denied') for n in range(1,15))+'\nGBOS_QEMU_PROBE: PROBE_PASS';self.assertFalse(probe_checks(log)['probe single positive PID'])

class LifecycleChecks(unittest.TestCase):
 def test_requires_exact_complete_order(self):
  events=['LIFECYCLE_CHILD_READY_64_OVERFLOW_REJECTED','LIFECYCLE_CHILD_EXITED','LIFECYCLE_RECLAIMED_64_IDLE','LIFECYCLE_REPLACEMENTS_UNBOUND_64','CONCURRENT_DISTINCT_BIND_UNBIND_32']
  log=lambda xs:'\n'.join('GBOS_QEMU_PROBE( 3963): '+x for x in xs+['PROBE_PASS'])
  self.assertTrue(probe_checks(log(events))['probe lifecycle complete'])
  self.assertFalse(probe_checks(log(events).replace('( 3963)', '( 7)',1))['probe lifecycle complete'])
  self.assertFalse(probe_checks(log(events).replace('( 3963)', '',1))['probe lifecycle complete'])
  self.assertFalse(probe_checks(log(events)+'\nGBOS_QEMU_PROBE( 77): PROBE_FAIL')['probe lifecycle complete'])
  for bad in (events[:-1],events[1:],events+[events[0]],events[:2]+events[3:4]+events[2:3]+events[4:]):
   self.assertFalse(probe_checks(log(bad))['probe lifecycle complete'])
 def test_full_evidence_pid_binding(self):
  rows=['PROBE_TX code=%d result=%s'%(n,'bound' if n in (3,4) else 'unsupported' if n==11 else 'denied') for n in range(1,15)]
  rows+=['LIFECYCLE_CHILD_READY_64_OVERFLOW_REJECTED','LIFECYCLE_CHILD_EXITED','LIFECYCLE_RECLAIMED_64_IDLE','LIFECYCLE_REPLACEMENTS_UNBOUND_64','CONCURRENT_DISTINCT_BIND_UNBIND_32','PROBE_PASS']
  fmt=lambda xs:'\n'.join(xs)
  lines=['<14>[ %d.123][ T20] E/GBOS_QEMU_PROBE( 3963): %s'%(i+1,row) for i,row in enumerate(rows)]
  self.assertTrue(all(probe_checks(fmt(lines)).values()))
  for i in (0,14,19):
   mixed=lines.copy();mixed[i]=mixed[i].replace('( 3963)','( 7)');self.assertFalse(all(probe_checks(fmt(mixed)).values()))
   missing=lines.copy();missing[i]=missing[i].replace('( 3963)','');self.assertFalse(all(probe_checks(fmt(missing)).values()))
  zero=lines.copy();zero[0]=zero[0].replace('( 3963)','( 0)');self.assertFalse(all(probe_checks(fmt(zero)).values()))
  self.assertFalse(all(probe_checks(fmt(lines+['GBOS_QEMU_PROBE( 7): PROBE_FAIL'])).values()))

if __name__=='__main__':unittest.main()
