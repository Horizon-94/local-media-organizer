import unittest,sys,importlib.util
from pathlib import Path
from collections import OrderedDict
P=Path(__file__).resolve().parents[2];D=P/'apps/media_archive_image_video_ui';sys.path.insert(0,str(P))
spec=importlib.util.spec_from_file_location('apps.media_archive_image_video_ui.candidate_preparation',D/'search_preparation.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Connection:
 def __init__(self,pages):self.pages=pages
 def execute(self,q):self.value=4096 if 'page_size' in q else self.pages;return self
 def fetchone(self):return (self.value,)
class Store:
 def __init__(self,pages=1):self.connection=Connection(pages);self.current=True;self.closed=False
 def close(self):self.closed=True
 def preparation_current(self):
  if isinstance(self.current,Exception):raise self.current
  return self.current
class Tests(unittest.TestCase):
 def setUp(self):self.p=m.PreparedLibraries(None)
 def add(self,k,pages=1):
  s=Store(pages);self.p.entries[k]={'store':s};self.p.states[k]={'state':'ready','retained':True};return s
 def test_count_lru(self):
  self.p.MAX_CONNECTIONS=2;a=self.add('a');self.add('b');self.add('c');self.p.trim(keep='c');self.assertTrue(a.closed);self.assertEqual(list(self.p.entries),['b','c'])
 def test_single_oversized(self):
  a=self.add('a',40000);self.p.trim(keep='a');self.assertTrue(a.closed);self.assertFalse(self.p.entries);self.assertEqual(self.p.states['a']['state'],'pending')
 def test_changed(self):
  a=self.add('a');a.current=False;self.assertEqual(self.p.status()[0]['state'],'pending')
 def test_real_error(self):
  a=self.add('a');a.current=PermissionError('denied');self.assertEqual(self.p.status()[0]['state'],'unavailable');self.assertTrue(a.closed)
 def test_independent_library(self):
  a=self.add('a');self.add('b');a.current=ValueError('bad');self.p.status();self.assertEqual(self.p.states['b']['state'],'ready');self.assertIn('b',self.p.entries)
 def test_cleanup(self):
  a=self.add('a');b=self.add('b');self.p.close();self.assertTrue(a.closed and b.closed);self.assertFalse(self.p.entries)
if __name__=='__main__':unittest.main()
