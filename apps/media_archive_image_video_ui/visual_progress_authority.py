"""Read-only independently fenced job and source progress; never inference."""
import collections
import sqlite3
import json

STAGES = ('yoloe','openclip','dhash','geometry','quality')


def project_visual_progress(c, run_id, epoch, contracts=None, *, task_id=None):
    tables={r[0] for r in c.execute('SELECT name FROM sqlite_master')}
    cols={r[1] for r in c.execute('PRAGMA table_info(v2_jobs)')}
    modern={'source_id','source_revision','item_id','input_sha'} <= cols and 'v2_active_sources' in tables
    if modern and {'v2_current_jobs','v2_job_dependencies'} <= tables:
        # TEMP-only equivalent; main URI remains read-only for UI callers.
        query_only=sqlite3.Connection.execute(c,'PRAGMA query_only').fetchone()[0]
        try:
            sqlite3.Connection.execute(c,'PRAGMA query_only=OFF')
            from .v2_current_jobs_cache import install_view
            import os
            accelerated = os.environ.get('MEDIA_ARCHIVE_CURRENT_LINEAGE_CACHE_OPT_IN')=='1' and install_view(c)
            if not accelerated:sqlite3.Connection.execute(c,'''CREATE TEMP VIEW IF NOT EXISTS v2_current_jobs AS
              SELECT j.* FROM main.v2_jobs j WHERE j.status<>'SUPERSEDED' AND NOT EXISTS (
                WITH RECURSIVE ancestry(parent_key) AS (
                  SELECT j.execution_key UNION SELECT d.parent_key FROM ancestry a
                  JOIN main.v2_job_dependencies d ON d.child_key=a.parent_key)
                SELECT 1 FROM ancestry a LEFT JOIN main.v2_jobs p ON p.execution_key=a.parent_key
                LEFT JOIN main.v2_active_sources active ON active.source_id=p.source_id
                WHERE p.execution_key IS NULL OR (a.parent_key<>j.execution_key AND p.status<>'DONE')
                OR (active.source_revision IS NOT NULL AND active.source_revision<>p.source_revision))''')
        finally:sqlite3.Connection.execute(c,'PRAGMA query_only='+str(int(query_only)))
    fence=" AND (j.status='SUPERSEDED' OR NOT EXISTS(SELECT 1 FROM v2_active_sources a WHERE a.source_id=j.source_id AND a.source_revision<>j.source_revision))" if modern else ''
    fields=','.join('j.'+col for col in ('execution_key','stage','status','source_id','source_revision','item_id','input_sha','revision','provenance') if col in cols)
    owner_clause='j.scheduler_run_id=? AND j.execution_epoch=?' if task_id is None else 'j.scheduler_run_id IN (SELECT scheduler_run_id FROM scheduler_runs WHERE task_id=?)'
    owner_args=(run_id,epoch) if task_id is None else (task_id,)
    jobs=[dict(r) for r in c.execute('SELECT '+fields+' FROM v2_jobs j WHERE '+owner_clause+fence,owner_args)]
    def valid(j):
        p=(contracts or {}).get(j['stage'])
        return not p or (j.get('revision')==p['revision'] and j.get('provenance')==json.dumps(p,sort_keys=True))
    runnable=collections.Counter(); runnable_keys=[]
    if modern and 'v2_current_jobs' in tables:
        from .v2_product_lifecycle import product_claim_predicate
        predicate=product_claim_predicate(c, {'scheduler_run_id':run_id,'execution_epoch':epoch})
        for r in c.execute('SELECT * FROM v2_current_jobs WHERE '+predicate+" AND scheduler_run_id=? AND execution_epoch=? AND status IN ('PENDING','FAILED_RETRYABLE') AND attempts<max_attempts",(run_id,epoch)):
            if valid(dict(r)):
                runnable[r['stage']]+=1
                runnable_keys.append(r['execution_key'])
    else:runnable.update(j['stage'] for j in jobs if j['status'] in ('PENDING','FAILED_RETRYABLE'))
    kinds={}
    if 'v2_product_sources' in tables:
        kind_query='SELECT source_id,source_json FROM v2_product_sources WHERE '+('scheduler_run_id=?' if task_id is None else 'scheduler_run_id IN (SELECT scheduler_run_id FROM scheduler_runs WHERE task_id=?)')
        for r in c.execute(kind_query,(run_id if task_id is None else task_id,)):
            p=json.loads(r['source_json']);kinds[r['source_id']]=p.get('media_kind',p.get('media_type'))
    artifacts=collections.defaultdict(list);eos=set()
    if modern and {'v2_artifacts','v2_sources'} <= tables:
        for r in c.execute("SELECT ar.* FROM v2_artifacts ar JOIN v2_active_sources a ON a.source_id=ar.source_id AND a.source_revision=ar.revision WHERE ar.status='READY'"):
            if r['source_id'] in kinds:artifacts[(r['source_id'],r['revision'])].append(dict(r))
        eos={(r[0],r[1]) for r in c.execute("SELECT s.source_id,s.revision FROM v2_sources s JOIN v2_active_sources a ON a.source_id=s.source_id AND a.source_revision=s.revision WHERE s.eos='EOS'")}
    done={(j.get('source_id'),j.get('source_revision'),j['stage'],j.get('item_id'),j.get('input_sha')) for j in jobs if j['status']=='DONE' and valid(j)}
    def metrics(js):
        b=collections.Counter(j['status'] for j in js)
        return {'job_total':sum(n for st,n in b.items() if st!='SUPERSEDED'),'job_done':b['DONE'],
                'job_running':b['RUNNING'],'job_pending':b['PENDING'],'job_retryable':b['FAILED_RETRYABLE'],
                'job_failed':b['FAILED_FINAL']+b['FAILED'],'job_superseded':b['SUPERSEDED']}
    visual={kind:{} for kind in ('video','image')}
    for kind in visual:
        ids={sid for sid,k in kinds.items() if k==kind}
        for st in STAGES:
            m=metrics([j for j in jobs if j['stage']==st and j.get('source_id') in ids])
            m['source_total']=len(ids)
            m['source_complete']=sum(key in eos and bool(ars) and all((key[0],key[1],st,ar['artifact_id'],ar['sha']) in done for ar in ars) for key,ars in artifacts.items() if key[0] in ids)
            visual[kind][st]=m
    stages={st:{**metrics([j for j in jobs if j['stage']==st]),'runnable':runnable[st]} for st in {j['stage'] for j in jobs}|set(STAGES)}
    blocks={}
    if 'scheduler_admission_blocks' in tables:
        for r in c.execute('SELECT job_type,diagnostics FROM scheduler_admission_blocks WHERE scheduler_run_id=? AND execution_epoch=?',(run_id,epoch)):blocks[r[0]]=json.loads(r[1])
    return {'visual':visual,'stages':stages,'blocks':blocks,'jobs':jobs,'modern':modern,'runnable_execution_keys':runnable_keys}
