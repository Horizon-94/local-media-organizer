import unittest,importlib.util,sys
from pathlib import Path
P=Path(__file__).resolve().parents[2];D=P/'apps/media_archive_image_video_ui'
spec=importlib.util.spec_from_file_location('candidate',D/'prepared_federation.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
p=D/'federated_search.py';spec=importlib.util.spec_from_file_location('baseline',p);a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class Tests(unittest.TestCase):
 def libs(self,n):return [{'database':f'/fixture/{i}/db','task_path':f'/fixture/{i}/task.json','task_name':str(i)} for i in range(n)]
 def execute(self,l,offset):
  rows=[{'result_id':str(i),'source_id':str(i),'score':1.0/(i+1),'source_revision':'r','time_position_ms':i*1000} for i in range(63)]
  return {'status':'PASS','result_items':rows[offset:offset+30],'result_total_count':len(rows),'next_result_offset':offset+30 if offset+30<len(rows) else None,'coverage':{'eligible_visual_unit_count':63,'scanned_visual_vector_count':63,'scanned_text_vector_count':63}}
 def test_equivalent(self):
  for n in [1,2,3,23]:
   for offset in [0,30,60,120]:
    old=a.search_libraries(self.libs(n),self.execute,offset=offset);new=m.search_libraries(self.libs(n),self.execute,offset=offset)
    for k in ['status','result_items','result_total_count','next_result_offset','coverage','partial_results']:self.assertEqual(old[k],new[k],(n,offset,k))
 def test_independent_error(self):
  def run(l,o):
   if l['task_name']=='1':raise ValueError('unavailable')
   return self.execute(l,o)
  r=m.search_libraries(self.libs(3),run);self.assertEqual(r['status'],'PASS');self.assertTrue(r['partial_results']);self.assertEqual(len(r['completed_libraries']),2);self.assertEqual(r['library_errors'][0]['library'],'1')
 def test_timeout(self):
  seen=[]
  def run(l,o):
   seen.append(l['task_name'])
   if l['task_name']=='1':raise TimeoutError('deadline')
   return self.execute(l,o)
  r=m.search_libraries(self.libs(3),run);self.assertEqual(r['status'],'INCOMPLETE');self.assertEqual(seen,['0','1']);self.assertEqual(len(r['result_items']),30);self.assertEqual(len(r['library_errors']),2)
 def test_inner_timeout(self):
  r=m.search_libraries(self.libs(2),lambda l,o:{'status':'INCOMPLETE','error':'TimeoutError'});self.assertEqual(r['status'],'INCOMPLETE')
 def test_duplicate_source_scoped(self):
  r=m.search_libraries(self.libs(3),self.execute);self.assertEqual(len({x['result_id'] for x in r['result_items']}),30);self.assertEqual(len({x['library_database'] for x in r['result_items']}),3)
 def test_all_failed(self):
  r=m.search_libraries(self.libs(2),lambda l,o:{'status':'FAIL','error':'real error'});self.assertEqual(r['status'],'FAIL');self.assertEqual(len(r['library_errors']),2)
 def test_not_advancing(self):
  r=m.search_libraries(self.libs(1),lambda l,o:{'status':'PASS','result_items':[],'next_result_offset':0});self.assertEqual(r['status'],'FAIL');self.assertIn('pagination',r['library_errors'][0]['reason'])
 def test_progress_scoped(self):
  seen=[];m.search_libraries(self.libs(3),self.execute,on_results=lambda rows,*args:seen.append(rows));self.assertEqual(len(seen),3);self.assertTrue(all('library_task_path' in x for batch in seen for x in batch))
class CacheTests(unittest.TestCase):
 def test_exact_key_and_copy(self):
  calls=[]
  def encode(*args):calls.append(args);return [0.1,0.2]
  c=m.RequestEncoderCache(encode)
  x=c('embedding',{'model':'a'},'人','/python');x[0]=9
  self.assertEqual(c('embedding',{'model':'a'},'人','/python'),[0.1,0.2]);self.assertEqual(len(calls),1)
  for stage,cfg,q,runtime in [('openclip',{'model':'a'},'人','/python'),('embedding',{'model':'b'},'人','/python'),('embedding',{'model':'a'},'车','/python'),('embedding',{'model':'a'},'人','/other')]:c(stage,cfg,q,runtime)
  self.assertEqual(len(calls),5)
 def test_bound(self):
  c=m.RequestEncoderCache(lambda *x:[0.1]*8,max_entries=2)
  for q in ['a','b','c']:c('embedding',{},q,'/python')
  self.assertEqual(len(c.entries),2)
  c=m.RequestEncoderCache(lambda *x:[0.1]*8,max_bytes=1);c('embedding',{},'a','/python');self.assertEqual(c.bytes,0);self.assertFalse(c.entries)
 def test_partial_preserved(self):
  lib=Tests().libs(1);r=m.search_libraries(lib,lambda l,o:{'status':'INCOMPLETE','result_items':[{'result_id':'legal','score':0.5}]});self.assertEqual(r['status'],'INCOMPLETE');self.assertEqual(len(r['result_items']),1)
class GateTests(unittest.TestCase):
 def test_bad_proof_cannot_use_cached_vector(self):
  import contextlib
  from unittest.mock import patch
  sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
  from apps.media_archive_image_video_ui import v2_semantic_search as sem
  class Store:
   @contextlib.contextmanager
   def connect(self):yield object()
  cache=m.RequestEncoderCache(lambda *a:[0.1,0.2]);cache('embedding',{'model':'same'},'人','/python')
  with patch.object(sem,'bindings',return_value=[('embedding','namespace',{'model':'same'},{'bad':True})]),patch.object(sem,'weights_match',return_value=False):
   with self.assertRaisesRegex(RuntimeError,'contract_mismatch'):
    sem.retrieve(Store(),'人',embedding_python='/python',openclip_python='/python',policy={},encoder=cache)
  self.assertEqual(cache.hits,0)
if __name__=='__main__':unittest.main()
