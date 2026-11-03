"""One Registry authority: read-only eligibility and explicit CAS reconciliation.
No media reads, worker/model imports, database writes, or library discovery.
"""
import hashlib,json,sqlite3
from pathlib import Path

VALID={'VALID_LIBRARY','VALID_LIBRARY_SOURCE_OFFLINE','VALID_LIBRARY_SOURCE_UNCHECKED'}
STALE={'STALE_TASK_PATH','STALE_DATABASE','STALE_TASK_AND_DATABASE'}
def digest(path):
 p=Path(path);return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
def normalized(value):return str(Path(value).expanduser().resolve()) if value else None

def inspect(life,raw):
 r=dict(raw) if isinstance(raw,dict) else {};e={'classification':'MALFORMED_ENTRY','eligible':False,'row':r,'evidence':{}}
 try:
  tp=r.get('task_path');db=r.get('database')
  if not tp:return e
  p=life.guard(tp);d=life.guard(db) if db else None;te=p.is_file();de=bool(d and d.is_file())
  e['evidence'].update(task_exists=te,database_exists=de,task_path=normalized(p),database=normalized(db))
  # Missing identity/db in legacy rows does not establish absence of a database.
  if not te:
   e['classification']='STALE_TASK_AND_DATABASE' if db and not de else 'STALE_TASK_PATH' if db else 'UNKNOWN_NEEDS_REVIEW'
   return e
  t=life.read(p)
  if not isinstance(t,dict):return e
  # Legacy path-only registry rows may be recovered only from a bound production manifest.
  if not r.get('library_id') or not db:
   if t.get('task_contract')!='build150-production-task-v1' or t.get('processing_engine')!='BUILD150_ONLY':return e
   w=normalized(t.get('workspace'));candidate=t.get('database')
   if not t.get('task_id') or w!=normalized(p.parent/'workspace') or normalized(candidate)!=normalized(p.parent/'workspace/runtime/video.sqlite'):return e
   if r.get('library_id') and r['library_id']!=t['task_id']:return e
   if db and normalized(db)!=normalized(candidate):return e
   r.update(library_id=t['task_id'],task_id=t['task_id'],database=candidate,task_name=t.get('name',''),source_root=t.get('source_root'),created_at=t.get('created_at',''))
   db=candidate;d=life.guard(db);de=d.is_file();e['evidence'].update(database_exists=de,database=normalized(db),legacy_identity_recovered=True)
  if t.get('task_id')!=r['library_id'] or normalized(t.get('database'))!=normalized(db):
   e['classification']='UNKNOWN_NEEDS_REVIEW';return e
  if not de:e['classification']='STALE_DATABASE';return e
  w=t.get('workspace')
  if not w or not life.guard(w).is_dir():e['classification']='UNKNOWN_NEEDS_REVIEW';return e
  if r.get('durable_library_path') and normalized(Path(r['durable_library_path'])/'task.json')!=normalized(p):e['classification']='UNKNOWN_NEEDS_REVIEW';return e
  c=sqlite3.connect(d.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
  try:c.execute('PRAGMA query_only=ON');c.execute('SELECT name FROM sqlite_master LIMIT 1').fetchall()
  finally:c.close()
  # TEST provenance is explicit, never inferred from a name or a substring.
  if life.current().kind=='PRODUCTION' and any(str(r.get(k,'')).upper()=='TEST' or str(t.get(k,'')).upper()=='TEST' for k in ('origin','context')):
   e['classification']='TEST_POLLUTION_ENTRY';return e
  src=t.get('source_root') or r.get('source_root');online=None
  # Opening an indexed library does not authorize a source-volume probe.
  e.update(classification='VALID_LIBRARY_SOURCE_UNCHECKED',eligible=True,task=t)
  e['evidence'].update(source_online=online,source_availability='unchecked',source_reference_present=bool(src))
  return e
 except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
  e.update(classification='UNKNOWN_NEEDS_REVIEW');e['evidence']['error']=str(exc);return e

def project(life,raws=None):
 result=[];seen=[{}, {}, {}]
 for raw in life.rows() if raws is None else raws:
  e=inspect(life,raw);r=e['row']
  if not e['eligible'] and not r.get('library_id'):
   prior=seen[1].get(normalized(r.get('task_path')))
   if prior and prior['eligible'] and normalized(r.get('database')) in (None,normalized(prior['row'].get('database'))):
    e.update(classification='DUPLICATE_SAME_TASK_PATH',eligible=False)
  if e['eligible']:
   keys=[r.get('library_id'),normalized(r.get('task_path')),normalized(r.get('database'))]
   matches=[(i,seen[i][key]) for i,key in enumerate(keys) if key in seen[i]]
   if matches:
    i,previous=matches[0];prev=previous['row']
    # Conflicting declared identities are ambiguous, not safe to discard.
    if i==0 and (normalized(prev.get('task_path'))!=keys[1] or normalized(prev.get('database'))!=keys[2]):
     e.update(classification='UNKNOWN_NEEDS_REVIEW',eligible=False);previous.update(classification='UNKNOWN_NEEDS_REVIEW',eligible=False)
    else:e.update(classification=['DUPLICATE_SAME_LIBRARY','DUPLICATE_SAME_TASK_PATH','DUPLICATE_SAME_DATABASE'][i],eligible=False)
   else:
    for i,key in enumerate(keys):seen[i][key]=e
  result.append(e)
 return result

def current_valid(life,active,projection=None):
 entries=projection if projection is not None else project(life)
 for e in entries:
  r=e['row']
  if not e['eligible'] or r.get('library_id')!=(active.get('library_id') or active.get('task_id')):continue
  if active.get('task_path') and normalized(active['task_path'])!=normalized(r['task_path']):continue
  if active.get('database') and normalized(active['database'])!=normalized(r['database']):continue
  return e
 return None

def reconcile(life,expected_registry_sha,expected_active_sha,approved_removals=()):
 """Command only. Plan rows are matched byte-semantically against latest state."""
 ap=life.root()/'runtime/active_library.json'
 with life.locked():
  if digest(life.registry())!=expected_registry_sha or digest(ap)!=expected_active_sha:raise RuntimeError('ABORT_AND_RETRY_READ')
  payload=life.read(life.registry());raws=payload.get('libraries',[]);projection=project(life,raws);kept=[];actions=[]
  for e in projection:
   r=e['row'];cl=e['classification'];approved=any(r==item for item in approved_removals)
   remove=(cl in STALE or cl.startswith('DUPLICATE_') or cl=='TEST_POLLUTION_ENTRY' or approved)
   # An externally approved old provenance row must still be unavailable now.
   if approved and (e['eligible'] or e['evidence'].get('task_exists')):raise RuntimeError('APPROVED_ROW_NOW_EXISTS')
   if remove:actions.append({'row':r,'classification':cl,'action':'REMOVE_STALE_REGISTRY_ROW'})
   else:kept.append(r)
  active=life.read(ap) if ap.is_file() else {};clear=bool(active and not current_valid(life,active,project(life,kept)))
  # Recheck after IO validation as well, under the same advisory lock.
  if digest(life.registry())!=expected_registry_sha or digest(ap)!=expected_active_sha:raise RuntimeError('ABORT_AND_RETRY_READ')
  if kept!=raws:life.atomic(life.registry(),{**payload,'libraries':kept})
  if clear:life.atomic(ap,{})
  if life.rows()!=kept or (clear and life.read(ap)!={}):raise RuntimeError('POST_WRITE_VERIFY_FAILED')
  return {'status':'PASS','registry_mutated':kept!=raws,'active_mutated':clear,'actions':actions,'count_after':len(kept)}
