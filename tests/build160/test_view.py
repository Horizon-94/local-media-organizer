import unittest,sqlite3,json,sys,math,struct
from pathlib import Path
from apps.media_archive_image_video_ui.search_numeric_view import NumericViews
class Store:
 def __init__(self):self.path='/fixture/db';self.token='a';self.current=True
 def preparation_current(self):return self.current
class Tests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.c.execute('create table v2_vectors(entity_id,namespace,dimension,vector_json)');self.data=[[0.1,0.2,-0.3],[0.1,0.2,-0.3],[1.0,1e-300,-1e-300]]
  self.c.executemany('insert into v2_vectors values(?,?,?,?)',[(str(i),'n',3,json.dumps(v)) for i,v in enumerate(self.data)]);self.store=Store();self.v=NumericViews(budget=100000);self.v.select_scope(['one']);self.sql='SELECT v.* FROM v2_vectors v WHERE namespace=?'
 def tearDown(self):self.c.close()
 def rows(self):return list(self.v.rows(self.store,self.c,self.sql,('n',),json.loads))
 def test_exact(self):
  rows=self.rows();self.rows();self.assertEqual(self.v.stats['hits'],1)
  for q in [[0.3,0.5,0.8],[1e-300,1.0,-0.9],[0.1,0.2,-0.3]]:
   norm=math.sqrt(sum(x*x for x in q));a=sorted([(str(i),sum(x*y for x,y in zip(v,q))/norm) for i,v in enumerate(self.data)],key=lambda r:(-r[1],r[0]));b=sorted([(r['entity_id'],sum(x*y for x,y in zip(r['vector_json'],q))/norm) for r in rows],key=lambda r:(-r[1],r[0]));self.assertEqual(a,b)
   for x,y in zip(a,b):self.assertEqual(struct.pack('d',x[1]),struct.pack('d',y[1]))
 def test_changed(self):
  self.rows();self.c.execute('delete from v2_vectors where entity_id="0"');self.store.token='b';self.assertEqual(len(self.rows()),2);self.assertEqual(len(self.v.entries),1)
 def test_scope(self):self.rows();self.v.select_scope(['two']);self.assertEqual(self.v.bytes,0)
 def test_budget(self):self.v.budget=1;self.v.select_scope(['a','b']);rows=self.rows();self.assertEqual(len(rows),3);self.assertFalse(self.v.entries)
 def test_pressure(self):self.rows();self.v.memory=lambda:{'healthy':False};self.v.maintain();self.assertEqual(self.v.bytes,0)
 def test_expired(self):self.rows();self.store.current=False;self.v.maintain();self.assertFalse(self.v.entries)
 def test_close(self):self.rows();self.v.clear();self.assertEqual(self.v.bytes,0)
if __name__=='__main__':unittest.main()
