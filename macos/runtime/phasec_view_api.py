"""Build151 UI-only projections. Never imports a processing owner or claims work."""
import argparse,json,os,sqlite3,shutil,time
from pathlib import Path
from urllib.parse import quote

def state_root():
 return Path(os.environ.get('MEDIA_ARCHIVE_APP_STATE_ROOT',str(Path.home()/'Library/Application Support/素材大整理/1.3.2-build151'))).expanduser()
def read(p,default=None):
 p=Path(p);return json.loads(p.read_text()) if p.is_file() else default
def bind_task():
 pointer=read(state_root()/'runtime/active_library.json',{})
 # The existing boundary uses active_library.json at application root/runtime;
 # accept only that registered task contract, never guess an old DB location.
 if not pointer:pointer=read(state_root()/'active_library.json',{})
 path=pointer.get('task_path')
 # Same canonical durable registry as History/Search; selection is only a hint.
 entries=read(state_root()/'runtime/library_registry_v1.json',{}).get('libraries',[])
 identity=pointer.get('library_id') or pointer.get('task_id')
 selected=next((r for r in entries if identity and r.get('library_id')==identity),None)
 if selected:path=selected.get('task_path')
 if not path:return None,None
 path=Path(path).resolve(strict=True);task=read(path)
 if task.get('task_contract')!='build150-production-task-v1' or task.get('processing_engine')!='BUILD150_ONLY':raise ValueError('BUILD151_VIEW_REQUIRES_CURRENT_TASK')
 expected=path.parent/'workspace/runtime/video.sqlite'
 if task.get('database')!=str(expected):raise ValueError('BUILD151_VIEW_TASK_PATH_MISMATCH')
 return path,task
def connect(task):
 p=Path(task['database']).resolve(strict=True)
 c=sqlite3.connect('file:'+quote(str(p))+'?mode=ro',uri=True,timeout=3);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');return c
def status():
 path,task=bind_task();out={'status':'PASS','library_name':None,'sources':None,'search_committed':None,'folders':[],'stages':[],'active':[],'coverage':{},'database':None,'task_state':None,'disk_free':shutil.disk_usage(state_root().parent if state_root().parent.exists() else Path.home()).free,'observed_at':time.time(),'processing_engine':'BUILD150_ONLY'}
 if not task:return out
 out['library_name']=task['name'];out['task_state']=read(task['state_path'],{}).get('status');out['task_id']=task['task_id'];out['task_path']=str(path)
 if not Path(task['database']).is_file():return out
 with connect(task) as c:
  names={r[0] for r in c.execute('SELECT name FROM sqlite_master')}
  if 'release_registry' in names:
   rows=list(c.execute('SELECT row_json FROM release_registry WHERE present=1'));counts={'image':0,'video':0}
   for row in rows:
    kind=json.loads(row[0]).get('media_kind');counts[kind]=counts.get(kind,0)+1
   out['sources']=counts
  if {'release_registry','release_search_heads'}<=names:
   out['search_committed']=c.execute('SELECT COUNT(*) FROM release_search_heads h JOIN release_registry r ON r.source_id=h.source_id AND r.revision=h.revision WHERE h.visible=1 AND r.present=1 AND r.committed=1').fetchone()[0]
  if 'release_folders' in names:out['folders']=[dict(r) for r in c.execute('SELECT folder,total,terminal,committed,state,policy FROM release_folders ORDER BY folder')]
  if 'release_folder_checkpoints' in names:out['folder_checkpoints']=[dict(r) for r in c.execute('SELECT * FROM release_folder_checkpoints ORDER BY folder')]
  if 'video_stream_jobs_v1' in names:
   out['stages']=[dict(r) for r in c.execute('SELECT stage,status,COUNT(*) count FROM video_stream_jobs_v1 WHERE status<>\'SUPERSEDED\' GROUP BY stage,status')]
   out['active']=[dict(r) for r in c.execute("SELECT job_id,source_id,stage,attempts,lease_until FROM video_stream_jobs_v1 WHERE status='RUNNING' ORDER BY stage,job_id")]
  if 'v2_vectors' in names:
   out['coverage']['Vector']=c.execute("SELECT COUNT(*) FROM v2_vectors WHERE namespace LIKE 'text:%'").fetchone()[0]
   out['coverage']['Visual Vector']=c.execute("SELECT COUNT(*) FROM v2_vectors WHERE namespace LIKE 'visual:%'").fetchone()[0]
  for label,table in [('Documents','v2_searchable_documents'),('FTS','v2_text_fts'),('Fusion','v2_fusion'),('closure pending','release_publication_queue')]:
   if table in names:out['coverage'][label]=c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
 db=Path(task['database']);out['database']={'size':db.stat().st_size,'wal_size':Path(str(db)+'-wal').stat().st_size if Path(str(db)+'-wal').exists() else None,'write_health':None}
 return out
def footer():
 # Sidebar consumes only connection availability and disk space, never counts.
 path,task=bind_task()
 out={'status':'PASS','database':None,'disk_free':shutil.disk_usage(state_root().parent if state_root().parent.exists() else Path.home()).free,'observed_at':time.time()}
 if task and Path(task['database']).is_file():
  with connect(task) as c:
   c.execute('PRAGMA schema_version').fetchone()
  out['database']={'path':task['database']}
 return out

def published_sources():
 _,task=bind_task()
 if not task:return {'status':'PASS','items':[]}
 with connect(task) as c:
  rows=c.execute('''SELECT r.source_id,r.revision,r.relative_path,r.row_json FROM release_registry r
   JOIN release_search_heads h ON h.source_id=r.source_id AND h.revision=r.revision
   WHERE r.present=1 AND r.committed=1 AND h.visible=1 ORDER BY r.relative_path''')
  return {'status':'PASS','items':[dict(r) for r in rows]}
def source_detail(source_id):
 _,task=bind_task()
 if not task:raise ValueError('NO_CURRENT_TASK')
 with connect(task) as c:
  row=c.execute('SELECT * FROM release_registry WHERE source_id=? AND present=1',(source_id,)).fetchone()
  if not row:raise ValueError('SOURCE_NOT_IN_CURRENT_TASK')
  source=json.loads(row['row_json']);steps=[]
  for r in c.execute('SELECT stage,status,COUNT(*) n FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND status<>\'SUPERSEDED\' GROUP BY stage,status',(source_id,row['revision'])):
   steps.append(dict(key=r['stage']+':'+r['status'],name=r['stage'],state=r['status'],done=r['n'] if r['status']=='DONE' else 0,total=r['n'],reason='durable video_stream_jobs_v1'))
  committed=c.execute('SELECT 1 FROM release_search_heads WHERE source_id=? AND revision=? AND visible=1',(source_id,row['revision'])).fetchone() and row['committed']
  return dict(status='PASS',source_id=source_id,filename=Path(row['relative_path']).name,lifecycle='SEARCH_COMMITTED' if committed else 'TERMINAL' if row['terminal'] else 'NOT_COMMITTED',active_revision=row['revision'],steps=steps,first_blocking_reason='' if committed else '等待来源闭环和搜索提交',candidate_reason_lines=['source-relative: '+row['relative_path'],'resolved: '+source.get('path','')],database_write=False,processing_engine='BUILD150_ONLY')
def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['status','footer','sources','source-detail']);p.add_argument('--source-id');a=p.parse_args()
 try:result=footer() if a.command=='footer' else status() if a.command=='status' else source_detail(a.source_id) if a.command=='source-detail' else published_sources();print(json.dumps(result,ensure_ascii=False));return 0
 except (OSError,ValueError,sqlite3.Error,KeyError) as e:print(json.dumps({'status':'FAIL','error':str(e)},ensure_ascii=False));return 2
if __name__=='__main__':raise SystemExit(main())
