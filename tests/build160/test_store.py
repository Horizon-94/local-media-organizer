import sys,unittest,tempfile,sqlite3,importlib.util
from pathlib import Path
P=Path(__file__).resolve().parents[2];D=P/'apps/media_archive_image_video_ui';sys.path.insert(0,str(P));name='apps.media_archive_image_video_ui.candidate_store';spec=importlib.util.spec_from_file_location(name,D/'search_session_store.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from apps.media_archive_image_video_ui.build150_search_bindings import _query_bindings
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name)/'db.sqlite';c=sqlite3.connect(self.p);c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE values_v(v)');c.execute('INSERT INTO values_v VALUES(1)');c.commit();c.close();self.context='a';self.s=m.SessionReadOnlyStore(self.p);self.s.reuse_preparation=True;self.s.context_token=lambda:self.context;self.calls=0
  def build(c):self.calls+=1
  m._install_search_lineage_view=build
 def tearDown(self):self.s.close();self.tmp.cleanup()
 def prime(self):
  with self.s.connect() as c:
   self.s.prepared_readiness={'ready':True};_query_bindings.get()[id(c)]=[['embedding','space',{'model':'same'},{'proof':'same'}]]
  self.s.finish()
 def test_stable(self):
  self.prime()
  for _ in range(25):
   with self.s.connect() as c:self.assertEqual(self.s.prepared_readiness,{'ready':True});self.assertEqual(_query_bindings.get()[id(c)][0][1],'space')
   self.s.finish()
  self.assertEqual(self.calls,1)
 def test_external_commit(self):
  self.prime();c=sqlite3.connect(self.p);c.execute('INSERT INTO values_v VALUES(2)');c.commit();c.close();self.assertFalse(self.s.preparation_current())
  with self.s.connect() as c:self.assertIsNone(self.s.prepared_readiness);self.assertEqual(c.execute('SELECT count(*) FROM values_v').fetchone()[0],2)
  self.s.finish();self.assertEqual(self.calls,2)
 def test_commit_during_snapshot(self):
  self.prime();self.s.begin();c=sqlite3.connect(self.p);c.execute('INSERT INTO values_v VALUES(2)');c.commit();c.close();self.s.finish();self.assertIsNone(self.s.token);self.assertIsNone(self.s.prepared_bindings)
 def test_context_change(self):
  self.prime();self.context='b';self.assertFalse(self.s.preparation_current())
  with self.s.connect():self.assertIsNone(self.s.prepared_readiness)
  self.s.finish();self.assertEqual(self.calls,2)
 def test_schema(self):
  self.prime();c=sqlite3.connect(self.p);c.execute('CREATE TABLE second(v)');c.commit();c.close();self.assertFalse(self.s.preparation_current())
 def test_readonly(self):
  self.prime()
  with self.s.connect() as c:self.assertRaises(sqlite3.OperationalError,c.execute,'INSERT INTO values_v VALUES(3)')
  self.s.finish(False);self.assertIsNone(self.s.prepared_readiness)
 def test_cache_corruption(self):
  self.prime();self.s.prepared_readiness['ready']=False
  with self.s.connect():self.assertIsNone(self.s.prepared_readiness)
  self.s.finish()
 def test_live_replacement(self):
  self.prime();p=self.p.with_suffix('.other');c=sqlite3.connect(p);c.execute('CREATE TABLE other(v)');c.close();p.replace(self.p);self.assertFalse(self.s.preparation_current())
 def test_permissions(self):
  self.prime();self.p.chmod(0)
  try:self.assertRaises(PermissionError,self.s.preparation_current)
  finally:self.p.chmod(0o600)
 def test_journal(self):
  self.prime();j=Path(str(self.p)+'-journal');j.write_bytes(b'fixture')
  try:self.assertFalse(self.s.preparation_current())
  finally:j.unlink()
 def test_same_connection_commit(self):
  self.prime();c=self.s.connection;c.execute('PRAGMA query_only=OFF');c.execute('CREATE TEMP TABLE own_commit(v)');c.execute('INSERT INTO own_commit VALUES(2)');c.commit();c.execute('PRAGMA query_only=ON');self.assertFalse(self.s.preparation_current())
 def test_missing(self):self.prime();self.p.unlink();self.assertRaises(FileNotFoundError,self.s.preparation_current)
 def test_file_replace(self):
  self.prime();old=self.s.connection;self.s.close();p=self.p.with_suffix('.new');c=sqlite3.connect(p);c.execute('CREATE TABLE values_v(v)');c.commit();c.close();p.replace(self.p)
  with self.s.connect() as c:self.assertIsNot(c,old);self.assertIsNone(self.s.prepared_readiness)
  self.s.finish()
if __name__=='__main__':unittest.main()
