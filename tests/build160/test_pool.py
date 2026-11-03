import sys,importlib.util,unittest
from pathlib import Path
P=Path(__file__).resolve().parents[2];D=P/'apps/media_archive_image_video_ui';sys.path.insert(0,str(P))
spec=importlib.util.spec_from_file_location('apps.media_archive_image_video_ui.candidate_pool',D/'search_encoder_pool.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Worker:
 def __init__(self,*a):self.live=True;self.calls=[]
 def is_healthy(self):return self.live
 def __call__(self,p):self.calls.append(p);return {'query_ready':True} if p.get('request_kind')=='query_prepare' else {'vector':[.25,.75]}
 def close(self):self.live=False
class Tests(unittest.TestCase):
 def setUp(self):
  self.now=0;self.mem={'healthy':True,'pressure_level':0,'available_mb':18000};self.rss=1024
  self.p=m.QueryEncoderPool(factory=Worker,memory=lambda:self.mem,clock=lambda:self.now,rss=lambda:self.rss)
 def tearDown(self):self.p.close()
 def prepare(self):self.p.encode('embedding',{},None,'python',prepare_only=True)
 def test_prepare_no_inference(self):
  self.prepare();w=next(iter(self.p.workers.values()))[0];self.assertEqual(w.calls[0]['request_kind'],'query_prepare');self.assertNotIn('text',w.calls[0]);self.assertEqual(self.p.encode('embedding',{},'arbitrary','python'),[.25,.75]);self.assertEqual(self.p.stats['created'],1)
 def test_five_minutes_lease(self):
  self.prepare()
  for n in range(0,361,15):self.now=n;self.p.renew_lease();self.p.maintain()
  self.assertEqual(len(self.p.workers),1);self.assertEqual(len(next(iter(self.p.workers.values()))[0].calls),1)
 def test_expired(self):self.prepare();self.p.renew_lease();self.now=61;self.p.maintain();self.assertFalse(self.p.workers)
 def test_pressure(self):self.prepare();self.p.renew_lease();self.mem['pressure_level']=2;self.p.maintain();self.assertFalse(self.p.workers)
 def test_budget(self):self.prepare();self.p.renew_lease();self.rss=6145;self.p.maintain();self.assertFalse(self.p.workers)
 def test_dead_worker(self):self.prepare();next(iter(self.p.workers.values()))[0].live=False;self.p.maintain();self.assertFalse(self.p.workers)
 def test_identity_change(self):self.prepare();self.p.encode('embedding',{'identity':'changed'},None,'python',prepare_only=True);self.assertEqual(len(self.p.workers),1);self.assertEqual(self.p.stats['evicted'],1)
 def test_low_memory(self):self.mem['available_mb']=5000;self.assertRaises(RuntimeError,self.prepare);self.assertFalse(self.p.workers)
if __name__=='__main__':unittest.main()
