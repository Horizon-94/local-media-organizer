"""Complete integrity proof once per UI session, outside interactive snapshots.
Normal atomic commits retain the proof's explicit snapshot timestamp. A new
session may reuse a successful proof only for identical quiescent database files.
A replaced or changed database still requires a new complete check.
"""
import hashlib,json,os,sqlite3,time
from pathlib import Path

def _connection(path):
 c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True);c.execute('PRAGMA query_only=ON');return c

def _identity(path,c):
 p=Path(path).resolve();s=p.stat()
 return {'path':str(p),'device':s.st_dev,'inode':s.st_ino,'schema_version':c.execute('PRAGMA schema_version').fetchone()[0]}

def _report_path(path):
 session=os.environ.get('BUILD151_UI_SESSION','')
 if not session:raise ValueError('INTEGRITY_UI_SESSION_REQUIRED')
 root=Path(os.environ['MEDIA_ARCHIVE_APP_STATE_ROOT'])/'runtime/integrity'
 key=hashlib.sha256((session+'\0'+str(Path(path).resolve())).encode()).hexdigest()
 return root/(key+'.json')

def _stable_files(path):
 # No cache of live WAL/journal snapshots; no reliance on size or mtime alone.
 p=Path(path).resolve();files={}
 for suffix in ('','-wal','-journal'):
  target=Path(str(p)+suffix)
  try:st=target.stat()
  except FileNotFoundError:
   if not suffix:raise
   files[suffix]=None;continue
  if suffix and st.st_size:return None
  files[suffix]={'device':st.st_dev,'inode':st.st_ino,'size':st.st_size,
                 'mtime_ns':st.st_mtime_ns,'ctime_ns':st.st_ctime_ns}
 return {'contract':'unchanged-quiescent-files-v1','path':str(p),'files':files}

def _shared_path(path):
 key=hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()
 return _report_path(path).parent/('stable-'+key+'.json')

def _write_report(path,value):
 path.parent.mkdir(parents=True,exist_ok=True)
 tmp=path.with_suffix(f'.{os.getpid()}.tmp')
 tmp.write_text(json.dumps(value,ensure_ascii=False));os.replace(tmp,path)

def read_report(path):
 pending={'integrity_check':'pending','foreign_key_error_count':None,'checked_at':None,'check_scope':'complete audit of a recorded SQLite snapshot'}
 if not os.environ.get('BUILD151_UI_SESSION'):return pending
 try:
  with _connection(path) as c:identity=_identity(path,c)
  p=_report_path(path)
  if p.is_file():
   value=json.loads(p.read_text())
   if value.get('identity')==identity and value.get('session')==os.environ['BUILD151_UI_SESSION']:return value
  shared=_shared_path(path)
  before=_stable_files(path)
  if before is None or not shared.is_file():return pending
  try:value=json.loads(shared.read_text())
  except (OSError,ValueError):return pending
  if not isinstance(value,dict):return pending
  if (value.get('identity')!=identity or value.get('stable_files')!=before
      or value.get('status')!='PASS' or value.get('integrity_check')!='ok'
      or value.get('foreign_key_error_count')!=0 or _stable_files(path)!=before):return pending
  # Keep the original audit timestamp/session; do not claim a new full scan.
  return {**value,'reused_unchanged_files':True,'consumer_session':os.environ['BUILD151_UI_SESSION']}
 except (OSError,sqlite3.Error,ValueError) as e:
  return {**pending,'integrity_check':'error','error':str(e)}

def audit(path):
 start=time.time();out={'integrity_check':'error','foreign_key_error_count':None,'snapshot_started_at':start,'pid':os.getpid(),'session':os.environ['BUILD151_UI_SESSION'],'check_scope':'PRAGMA integrity_check + PRAGMA foreign_key_check; one read transaction'}
 c=None;before=None
 try:
  before=_stable_files(path)
  c=_connection(path);c.execute('BEGIN');out['identity']=_identity(path,c)
  values=[str(x[0]) for x in c.execute('PRAGMA integrity_check')]
  out['integrity_check']='ok' if values==['ok'] else '; '.join(values)
  out['foreign_key_error_count']=len(c.execute('PRAGMA foreign_key_check').fetchall())
 except (OSError,sqlite3.Error) as e:out['error']=str(e);out['integrity_check']='error'
 finally:
  if c is not None:c.close()
 out['checked_at']=time.time();out['seconds']=out['checked_at']-start
 out['status']='PASS' if out['integrity_check']=='ok' and out['foreign_key_error_count']==0 else 'FAIL'
 if out['status']=='PASS' and before is not None:
  try:
   if _stable_files(path)==before:
    out['stable_files']=before
    _write_report(_shared_path(path),out)
  except OSError:pass # Optional proof persistence cannot turn a real audit into success/failure.
 _write_report(_report_path(path),out)
 return out
