"""Presentation-only terminal proof; never changes task states or authorizations."""
import json,sqlite3,time,hashlib,os,zlib
from pathlib import Path

def inspect(c):
    # Same durable pending contract as the terminal-drain verification. Never
    # infer source failures from task counts or override a live scheduler.
    native=c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE status IN ('PENDING','RUNNING','RETRY_PENDING','BLOCKED') LIMIT 1").fetchone()
    canonical=c.execute("SELECT 1 FROM v2_current_jobs j JOIN v2_active_sources a ON a.source_id=j.source_id AND a.source_revision=j.source_revision WHERE j.status IN ('PENDING','RUNNING','FAILED_RETRYABLE') LIMIT 1").fetchone()
    event=c.execute("SELECT event_id FROM v2_outbox EXCEPT SELECT event_id FROM v2_inbox WHERE consumer='fusion-index' AND event_id IS NOT NULL LIMIT 1").fetchone()
    if native or canonical or event:return {'ended':False,'reason':'durable_pending_exists'}
    total=c.execute('SELECT COUNT(*) FROM release_registry WHERE present=1').fetchone()[0]
    published=c.execute('''SELECT COUNT(*) FROM release_registry r JOIN release_search_heads h ON h.source_id=r.source_id AND h.revision=r.revision
        JOIN v2_active_sources a ON a.source_id=r.source_id AND a.source_revision=r.revision
        WHERE r.present=1 AND r.committed=1 AND h.visible=1''').fetchone()[0]
    failures=c.execute("SELECT COUNT(*) FROM video_stream_jobs_v1 j JOIN release_registry r ON r.source_id=j.source_id AND r.revision=j.revision WHERE r.present=1 AND j.status='TERMINAL_FAILED'").fetchone()[0]
    return {'ended':True,'reason':'finished_batch_without_durable_pending','published_sources':published,'unpublished_sources':total-published,'total_sources':total,'retained_failed_tasks':failures}


def retained_failures(c, limit=100):
    rows=c.execute("""SELECT j.job_id,j.source_id,j.revision,j.stage,j.attempts,r.relative_path,
        a.error_type,substr(a.error_message,1,2000),a.finished_at
        FROM video_stream_jobs_v1 j JOIN release_registry r
          ON r.source_id=j.source_id AND r.revision=j.revision
        LEFT JOIN video_stream_attempts_v1 a ON a.job_id=j.job_id AND a.attempt_number=(
          SELECT MAX(x.attempt_number) FROM video_stream_attempts_v1 x WHERE x.job_id=j.job_id)
        WHERE r.present=1 AND j.status='TERMINAL_FAILED'
        ORDER BY r.relative_path,j.stage,j.job_id LIMIT ?""", (limit,)).fetchall()
    keys=('job_id','source_id','revision','stage','attempts','relative_path','error_type','error_message','finished_at')
    result=[dict(zip(keys,row)) for row in rows]
    for row in result:
        try:reference=json.loads(row['error_message'] or 'null')
        except (ValueError,TypeError):continue
        if isinstance(reference,dict) and set(reference)=={'$canonical_result'}:
            import sys
            pipeline=Path(__file__).resolve().parent/'Pipeline'
            if pipeline.is_dir() and str(pipeline) not in sys.path:sys.path.insert(0,str(pipeline))
            from apps.media_archive_image_video_ui.video_canonical_store import load_result
            row['error_reference']=reference['$canonical_result']
            try:
                value=load_result(c,reference['$canonical_result'])
                row['error_message']=(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False))[:2000]
            except (ValueError,KeyError,RuntimeError,sqlite3.Error,zlib.error) as error:
                row['error_message']='错误证据读取失败：'+type(error).__name__+': '+str(error)
    return result

def project(task,state,scheduler_active,state_root):
    snap=state.get('build150_snapshot') or {}
    if scheduler_active is not False or not snap.get('finished_at') or snap.get('status') not in ('PASS_BATCH','FAIL_BATCH'):
        return {'ended':False,'reason':'not_confirmed_finished_batch'}
    db=Path(task['database']).resolve()
    def stamp(p):
        try:s=p.stat();return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]
        except FileNotFoundError:return None
    def identity():return [3,str(db),stamp(db),stamp(Path(str(db)+'-wal')),stamp(Path(task['state_path'])),snap['finished_at']]
    before=identity();cache=Path(state_root)/'runtime'/('terminal-search-'+hashlib.sha256(str(db).encode()).hexdigest()[:20]+'.json')
    try:
        old=json.loads(cache.read_text())
        if old['identity']==before:return old['projection']
    except (OSError,ValueError,KeyError):pass
    started=time.monotonic();c=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=.5)
    try:
        c.set_progress_handler(lambda:int(time.monotonic()-started>5),10000);c.execute('BEGIN');value=inspect(c)
        if value.get('ended'):
            value['retained_failures']=retained_failures(c)
            value['retained_failures_truncated']=value.get('retained_failed_tasks',0)>len(value['retained_failures'])
        value.update(observed_at=time.time(),query_seconds=time.monotonic()-started)
    except sqlite3.Error as error:return {'ended':None,'reason':'terminal_pending_unknown','error':str(error)}
    finally:c.close()
    if before!=identity():return {'ended':None,'reason':'terminal_state_changed_during_read'}
    try:
        cache.parent.mkdir(parents=True,exist_ok=True);temp=cache.with_suffix('.tmp-'+str(os.getpid()));temp.write_text(json.dumps({'identity':before,'projection':value}));os.replace(temp,cache)
    except OSError:pass  # A non-authoritative UI cache may fail without changing truth.
    return value
