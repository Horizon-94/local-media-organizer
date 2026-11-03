import sys,sqlite3,tempfile,json,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from apps.media_archive_image_video_ui import search_session_store as sm,v2_index
from apps.media_archive_image_video_ui.search_numeric_view import NumericViews,install
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite';c=sqlite3.connect(self.path);c.executescript('''PRAGMA journal_mode=WAL;CREATE TABLE v2_vectors(entity_id,namespace,dimension,vector_json,input_digest);CREATE TABLE v2_documents(document_id,source_id,source_revision,raw_result_ref,embedding_ref);CREATE TABLE v2_searchable_documents(document_id);CREATE TABLE v2_current_jobs(execution_key,status,result_sha,item_id,stage);CREATE TABLE v2_artifacts(artifact_id,source_id,revision);CREATE TABLE v2_active_sources(source_id,source_revision);''');c.execute('INSERT INTO v2_vectors VALUES(?,?,?,?,?)',('x','n',2,'[0.1,0.2]','digest'));c.commit();c.close();self.old=sm._install_search_lineage_view;sm._install_search_lineage_view=lambda c:None
  self.store=sm.SessionReadOnlyStore(self.path);self.store.reuse_preparation=True;self.views=NumericViews(100000);self.views.select_scope(['one']);self.original=v2_index.SQLiteExactVectors.search;self.decoder=v2_index._vector_json_loads;install(self.views)
 def tearDown(self):self.store.close();self.views.clear();sm._install_search_lineage_view=self.old;v2_index.SQLiteExactVectors.search=self.original;v2_index._vector_json_loads=self.decoder;self.tmp.cleanup()
 def query(self):
  try:r=v2_index.SQLiteExactVectors(self.store).search('n',[0.1,0.2]);self.store.finish();return r
  except BaseException:self.store.finish(False);raise
 def test_external_commit(self):
  a=self.query();self.query();self.assertEqual(self.views.stats['hits'],1);c=sqlite3.connect(self.path);c.execute("UPDATE v2_vectors SET vector_json='[0.9,0.1]'");c.commit();c.close();self.views.maintain();self.assertEqual(self.views.bytes,0);b=self.query();self.assertNotEqual(a,b)
 def test_model_identity(self):
  value=['one'];self.views.model_identity=lambda:tuple(value);self.query();value[0]='two';self.views.maintain();self.assertEqual(self.views.bytes,0)
 def test_closed_store(self):self.query();self.store.close();self.views.maintain();self.assertEqual(self.views.bytes,0)
 def test_missing(self):
  self.query();self.path.unlink();self.views.maintain();self.assertEqual(self.views.bytes,0);self.assertRaises(FileNotFoundError,self.query)
 def test_permission(self):
  self.query();self.path.chmod(0)
  try:self.views.maintain();self.assertEqual(self.views.bytes,0);self.assertRaises(PermissionError,self.query)
  finally:self.path.chmod(0o600)
 def test_partial_build_not_published(self):
  with self.store.connect() as c:
   def cancelled(value):raise KeyboardInterrupt('test cancellation')
   with self.assertRaises(KeyboardInterrupt):self.views.rows(self.store,c,'SELECT v.* FROM v2_vectors v WHERE namespace=?',('n',),cancelled)
  self.store.finish(False);self.assertEqual(self.views.bytes,0);self.assertFalse(self.views.entries)
if __name__=='__main__':unittest.main()
