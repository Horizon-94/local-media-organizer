"""Bounded query-session numeric view. Original SELECT, float sum and sort stay intact."""
from array import array
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
import hashlib,sys,weakref

class NumericViews:
 def __init__(self,budget=512*1024*1024,memory=None):
  self.budget=budget;self.memory=memory;self.entries=OrderedDict();self.bytes=0;self.scope=None;self.share=budget
  self.model_identity=None;self.last_model_identity=None
  self.stats={'hits':0,'builds':0,'skipped':0,'evicted':0,'peak_bytes':0}
 def clear(self):self.entries.clear();self.bytes=0
 def drop(self,key):
  value=self.entries.pop(key);self.bytes-=value['bytes'];self.stats['evicted']+=1
 def select_scope(self,scope):
  scope=tuple(sorted(set(scope)))
  if scope!=self.scope:self.clear();self.scope=scope
  self.share=self.budget//max(1,len(scope))
 def healthy(self):
  if self.model_identity is not None:
   identity=self.model_identity()
   if identity!=self.last_model_identity:self.clear();self.last_model_identity=identity
  if self.memory is None:return True
  m=self.memory();return bool(m.get('healthy')) and m.get('pressure_level',0)<=1 and m.get('available_mb',0)>=4096
 def maintain(self):
  if not self.healthy():self.clear();return
  for key,e in list(self.entries.items()):
   store=e['store']()
   try:current=store is not None and store.preparation_current()
   except Exception:current=False
   if not current:self.drop(key)
 def rows(self,store,c,sql,args,decoder):
  if not self.healthy():self.clear();return c.execute(sql,args)
  path=str(store.path);token=repr(store.token);key=(path,hashlib.sha256(token.encode()).hexdigest(),sql,tuple(args))
  for old in list(self.entries):
   if old[0]==path and old[1]!=key[1]:self.drop(old)
  if key in self.entries:self.entries.move_to_end(key);self.stats['hits']+=1;return self.entries[key]['rows']
  rows=list(c.execute(sql,args));estimate=sum(8*r['dimension']+sys.getsizeof(r['entity_id'])+400 for r in rows)
  own=sum(e['bytes'] for k,e in self.entries.items() if k[0]==path)
  if estimate+own>self.share:self.stats['skipped']+=1;return rows
  cooked=[];size=0
  for r in rows:
   values=array('d',decoder(r['vector_json']));item={'entity_id':r['entity_id'],'dimension':r['dimension'],'vector_json':values};cooked.append(item)
   size+=sys.getsizeof(item)+sys.getsizeof(values)+sys.getsizeof(item['entity_id'])+sys.getsizeof(item['dimension'])+8
   if size+own>self.share:self.stats['skipped']+=1;return rows
  size+=sys.getsizeof(cooked)+sys.getsizeof(sql)+sys.getsizeof(key[1])
  if size+own>self.share:self.stats['skipped']+=1;return rows
  while self.entries and self.bytes+size>self.budget:self.drop(next(iter(self.entries)))
  self.entries[key]={'rows':cooked,'bytes':size,'store':weakref.ref(store)};self.bytes+=size;self.stats['builds']+=1;self.stats['peak_bytes']=max(self.stats['peak_bytes'],self.bytes)
  return cooked

def install(views):
 from apps.media_archive_image_video_ui import v2_index
 original=v2_index.SQLiteExactVectors.search;decoder=v2_index._vector_json_loads
 def decode(value):return value if isinstance(value,array) else decoder(value)
 v2_index._vector_json_loads=decode
 class Connection:
  def __init__(self,c,store):self.c=c;self.store=store
  def execute(self,sql,args=()):
   if sql.lstrip().startswith('SELECT v.* FROM v2_vectors v WHERE namespace=?'):return views.rows(self.store,self.c,sql,args,decoder)
   return self.c.execute(sql,args)
  def __getattr__(self,name):return getattr(self.c,name)
 class Store:
  def __init__(self,store):self.store=store
  @contextmanager
  def connect(self):
   with self.store.connect() as c:yield Connection(c,self.store)
 def search(self,namespace,vector,limit=20,**options):
  if not getattr(self.store,'reuse_preparation',False):return original(self,namespace,vector,limit,**options)
  temporary=v2_index.SQLiteExactVectors(Store(self.store))
  return original(temporary,namespace,vector,limit,**options)
 v2_index.SQLiteExactVectors.search=search
