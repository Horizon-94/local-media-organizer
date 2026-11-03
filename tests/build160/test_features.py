from pathlib import Path
import sys,sqlite3,unittest,json,contextlib,os
sys.dont_write_bytecode=True
P=Path(__file__).resolve().parents[2];sys.path.insert(0,str(P))
from apps.media_archive_image_video_ui import spoken_content_catalog as catalog,search_result_contract as contract,search_evidence as evidence
class Classification(unittest.TestCase):
 def test_sparse_span_is_not_long(self):self.assertEqual(catalog.classify_metrics(600000,15000,12,400,2,5000),'fragmented_speech')
 def test_long(self):self.assertEqual(catalog.classify_metrics(140000,110000,12,400,0,90000),'long_spoken_content')
 def test_interview_is_suspected(self):self.assertEqual(catalog.classify_metrics(140000,110000,12,400,1,90000),'likely_interview')
 def test_question_language(self):self.assertTrue(catalog.is_question('为什么这样？'));self.assertTrue(catalog.is_question('How are you?'))
class Fusion(unittest.TestCase):
 def hit(self,i,cat,t=1000,end=1000,source='s',rev='r',score=.01):
  return dict(result_id=i,source_id=source,source_revision=rev,evidence_category=cat,time_position_ms=t,result_end_ms=end,score=score,search_intent='all',visual_unit_id='f',text_preview='text',media_type='video')
 def test_overlap_independent_bonus(self):
  r=contract.source_results([self.hit('a','visual_description'),self.hit('b','dialogue',500,2000)]);self.assertAlmostEqual(r[0]['final_score'],.0125)
 def test_distant_no_bonus(self):
  r=contract.source_results([self.hit('a','visual_description'),self.hit('b','dialogue',60000,61000)]);self.assertAlmostEqual(r[0]['final_score'],.01)
 def test_same_text_family_no_bonus(self):
  r=contract.source_results([self.hit('a','dialogue'),self.hit('b','dialogue')]);self.assertEqual(r[0]['final_score'],.01)
 def test_revision_and_source_no_cross(self):
  r=contract.source_results([self.hit('a','visual_description'),self.hit('b','dialogue',rev='other'),self.hit('c','ocr',source='other')]);self.assertEqual(len(r),3);self.assertTrue(all(x['final_score']==.01 for x in r))
 def test_bonus_cap(self):
  r=contract.source_results([self.hit(str(i),c) for i,c in enumerate(['visual_description','visual_semantic','dialogue','ocr'])]);self.assertAlmostEqual(r[0]['final_score'],.01375)
 def test_extension_not_direct_or_bonus(self):
  r=contract.source_results([self.hit('a','expanded_visual_clue'),self.hit('b','dialogue')]);self.assertEqual(r[0]['final_score'],.01)
 def test_invisible_not_revived(self):
  h=self.hit('a','dialogue');h['visible']=False;self.assertEqual(contract.source_results([h]),[])
class Evidence(unittest.TestCase):
 def test_all_modes_extension_consistent(self):
  row=dict(evidence_category='visual_description',evidence_text='蓝色汽车',text='蓝色汽车【检索】老人',evidence_is_direct=True)
  for mode in ('all','visual','text'):
   got=evidence.direct_lexical_rows([row],'老人',mode)[0];self.assertEqual(got['evidence_category'],'expanded_visual_clue');self.assertFalse(got['evidence_is_direct'])
class OCRPolicy(unittest.TestCase):
 def test_ocr_remains_text_evidence_in_all_modes(self):
  row=evidence.EvidenceReader(None).classify(dict(document_id='ocr-1',source_id='demo',source_revision='r',document_type='ocr',artifact_id='frame-1',text='DEMO 2026',start_ms=1000))
  self.assertEqual(row['evidence_category'],'ocr');self.assertEqual(row['evidence_text'],'DEMO 2026')
  self.assertTrue(evidence.allowed(row,'all'));self.assertTrue(evidence.allowed(row,'text'));self.assertFalse(evidence.allowed(row,'visual'))

class CombinedOCR(unittest.TestCase):
 def test_direct_ocr_is_not_visual_extension(self):
  proof={'direct_qwen_count':1,'qwen_text':'红色横幅','direct_ocr_count':1,'ocr_text':'DEMO 2026'}
  row=evidence.EvidenceReader(None).classify(dict(document_id='combined',source_id='demo',source_revision='r',artifact_id='frame',document_type='final_visual',text='红色横幅 DEMO 2026',provenance=json.dumps({'legacy_document':proof})))
  hit=evidence.direct_lexical_rows([row],'DEMO','visual')[0]
  self.assertEqual(hit['evidence_category'],'ocr');self.assertEqual(hit['evidence_text'],'DEMO 2026');self.assertFalse(evidence.allowed(hit,'visual'));self.assertTrue(evidence.allowed(hit,'text'))
  direct=evidence.direct_lexical_rows([row],'红色','visual')[0];self.assertEqual(direct['evidence_category'],'visual_description')

class Catalogue(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
  self.c.executescript('''CREATE TABLE v2_documents(document_id,source_id,source_revision,text,start_ms,end_ms,document_type,raw_result_ref);
  CREATE TABLE v2_current_jobs(execution_key,status,source_id,source_revision);
  CREATE TABLE v2_active_sources(source_id,source_revision);
  CREATE TABLE release_search_heads(source_id,revision,visible);
  CREATE TABLE release_registry(source_id,revision,present,committed);
  CREATE TABLE v2_source_metadata(source_id,source_revision,path,metadata);''')
  self.old=catalog._open_readonly
  @contextlib.contextmanager
  def opened(_):yield self.c
  catalog._open_readonly=opened
 def tearDown(self):catalog._open_readonly=self.old;self.c.close()
 def add(self,sid,visible=1,current='r',text='示例转写',segments=1,gap=1000,duration=1000):
  self.c.execute('INSERT INTO v2_active_sources VALUES (?,?)',(sid,current));self.c.execute('INSERT INTO release_search_heads VALUES (?,?,?)',(sid,'r',visible));self.c.execute('INSERT INTO release_registry VALUES (?,?,1,1)',(sid,'r'));self.c.execute('INSERT INTO v2_source_metadata VALUES (?,?,?,?)',(sid,'r','/demo/'+sid+'.mp4','{"media_type":"video"}'))
  for i in range(segments):
   did=sid+str(i);self.c.execute('INSERT INTO v2_documents VALUES (?,?,?,?,?,?,?,?)',(did,sid,'r',text,i*gap,i*gap+duration,'whisper',did));self.c.execute('INSERT INTO v2_current_jobs VALUES (?,\'DONE\',?,?)',(did,sid,'r'))
 def test_page_only(self):
  for i in range(25):self.add(str(i).zfill(2))
  a=catalog.catalogue(Path('unused'));b=catalog.catalogue(Path('unused'),offset=20);self.assertEqual(a['total'],25);self.assertEqual(len(a['items']),20);self.assertEqual(len(b['items']),5);self.assertFalse({x['source_id'] for x in a['items']} & {x['source_id'] for x in b['items']})
 def test_current_visible_empty_person(self):
  self.add('visible');self.add('hidden',visible=0);self.add('old',current='new');self.assertEqual(catalog.catalogue(Path('unused'))['total'],1);self.assertEqual(catalog.catalogue(Path('unused'),source_ids=set())['total'],0)
 def test_sparse_not_long_and_empty_text(self):
  self.add('sparse',segments=12,gap=60000,text='很多文字'*10);self.add('empty',text=' ');self.assertEqual(catalog.catalogue(Path('unused'),content_kind='long_spoken_content')['total'],0);self.assertEqual(catalog.catalogue(Path('unused'))['total'],1)
 def test_overlap_union(self):
  self.add('union',segments=2,gap=500,duration=1000);self.assertEqual(catalog.catalogue(Path('unused'))['items'][0]['covered_ms'],1500)
class AnswerWindow(unittest.TestCase):
 setUp=Catalogue.setUp
 tearDown=Catalogue.tearDown
 add=Catalogue.add
 def test_fragmented_answer_combines_adjacent_transcript(self):
  self.add('interview',segments=14,gap=10000,duration=10000,text='这是已有转写中的后续内容示例')
  self.c.execute("UPDATE v2_documents SET text='你觉得如何？' WHERE document_id='interview0'")
  r=catalog.catalogue(Path('unused'),content_kind='likely_interview')
  self.assertEqual(r['total'],1);self.assertGreaterEqual(len(r['items'][0]['qa_clues'][0]['answer']),24)
 def test_question_cannot_borrow_next_question_answer(self):
  self.add('interview',segments=14,gap=10000,duration=10000,text='你觉得如何？')
  self.assertEqual(catalog.catalogue(Path('unused'),content_kind='likely_interview')['total'],0)

class Candidates(unittest.TestCase):
 def search(self,**options):
  from apps.media_archive_image_video_ui.v2_index import SQLiteExactVectors
  rows=[{'entity_id':str(i),'dimension':2,'vector_json':json.dumps([1-i/100,0])} for i in range(10)]
  class Cursor:
   def __init__(self,rows):self.rows=rows
   def __iter__(self):return iter(self.rows)
   def fetchone(self):return self.rows[0] if self.rows else None
  class C:
   def execute(self,sql,args=()):return Cursor([] if 'sqlite_master' in sql else rows)
  class Store:
   @contextlib.contextmanager
   def connect(self):yield C()
  return SQLiteExactVectors(Store()).search('ns',[1,0],limit=4,**options)
 def test_filter_before_topk(self):
  self.assertEqual([x[0] for x in self.search(candidate_groups={str(i):'visual' for i in range(6,10)})],['6','7','8','9'])
 def test_total_budget_and_family_reservation(self):
  hits=self.search(candidate_groups={str(i):'dialogue' if i<6 else 'visual' for i in range(10)},group_limits={'dialogue':2,'visual':2});self.assertEqual([x[0] for x in hits],['0','1','6','7']);self.assertEqual(len(hits),4)
 def test_original_topk_unchanged_without_options(self):self.assertEqual([x[0] for x in self.search()],['0','1','2','3'])

class CategoryCache(unittest.TestCase):
 def setUp(self):
  from apps.media_archive_image_video_ui import v2_semantic_search as semantic
  self.s=semantic;semantic._CATEGORY_CACHE.clear();self.budget=semantic.CATEGORY_CACHE_BYTES;self.calls=0
  class Store:
   reuse_preparation=True;token=1;current=True
   def preparation_current(self):return self.current
  self.store=Store()
 def tearDown(self):self.s._CATEGORY_CACHE.clear();self.s.CATEGORY_CACHE_BYTES=self.budget
 def build(self):self.calls+=1;return {'document':'dialogue'}
 def test_reuse_same_snapshot(self):
  for _ in range(3):self.s.cached_categories(self.store,'text',self.build)
  self.assertEqual(self.calls,1)
 def test_changed_identity_rebuilds(self):
  self.s.cached_categories(self.store,'text',self.build);self.store.token=2;self.s.cached_categories(self.store,'text',self.build);self.assertEqual(self.calls,2)
 def test_closed_snapshot_drops_cache(self):
  self.s.cached_categories(self.store,'text',self.build);self.store.current=False;self.s.cached_categories(self.store,'text',self.build);self.assertEqual(self.calls,2);self.assertFalse(self.s._CATEGORY_CACHE)
 def test_budget_is_hard_cap(self):
  self.s.CATEGORY_CACHE_BYTES=1;self.s.cached_categories(self.store,'text',self.build);self.assertFalse(self.s._CATEGORY_CACHE)
 def test_request_store_never_reuses(self):
  self.store.reuse_preparation=False
  for _ in range(2):self.s.cached_categories(self.store,'text',self.build)
  self.assertEqual(self.calls,2)

if __name__=='__main__':unittest.main()
