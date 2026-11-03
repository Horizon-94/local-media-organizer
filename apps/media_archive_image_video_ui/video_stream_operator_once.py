"""Explicit, idempotent one-attempt grants for CURRENT native failed jobs.

No execution identity, attempt history, result, or retry limit is replaced.
Authorization requires the stopped owner/epoch and exact audited failed rows.
The ordinary native claim and completion protocols remain authoritative.
"""
import hashlib
import json
import time


def ensure_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS video_stream_operator_batches_v1(
      request_id TEXT PRIMARY KEY,target_sha TEXT NOT NULL,authorized_at REAL NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS video_stream_operator_source_history_v1(
      request_id TEXT,source_id TEXT,revision TEXT,previous_state TEXT,previous_outcome TEXT,
      PRIMARY KEY(request_id,source_id,revision))''')
    c.execute('''CREATE TABLE IF NOT EXISTS video_stream_operator_once_v1(
      request_id TEXT NOT NULL,job_id TEXT NOT NULL,attempt_number INTEGER NOT NULL,
      source_id TEXT NOT NULL,revision TEXT NOT NULL,input_sha TEXT NOT NULL,
      contract TEXT NOT NULL,payload_sha TEXT NOT NULL,authorized_at REAL NOT NULL,
      claimed_token TEXT,claimed_owner TEXT,claimed_epoch INTEGER,
      PRIMARY KEY(request_id,job_id),UNIQUE(job_id,attempt_number))''')


def payload_sha(payload):
    return hashlib.sha256(payload.encode()).hexdigest()


def grant(c, request_id, targets, *, owner, epoch):
    """Caller must hold runtime.lock and BEGIN IMMEDIATE; no implicit commit."""
    if not c.in_transaction or not request_id or not targets or len(targets)>14:
        raise ValueError('operator_once_invalid_request')
    control=c.execute('SELECT owner,epoch,stopped FROM video_stream_control_v1 WHERE singleton=1').fetchone()
    if tuple(control or ())!=(owner,epoch,1):
        raise ValueError('operator_once_requires_stopped_owner_epoch')
    if len({t['job']['job_id'] for t in targets})!=len(targets):
        raise ValueError('operator_once_duplicate_target')
    scope=sorted((t['job']['job_id'],t['job']['attempts'],t['job']['source_id'],
                  t['job']['revision'],t['job']['input_sha'],t['job']['contract'],
                  payload_sha(t['job']['payload'])) for t in targets)
    digest=hashlib.sha256(json.dumps(scope,sort_keys=True).encode()).hexdigest()
    previous=c.execute('SELECT target_sha FROM video_stream_operator_batches_v1 WHERE request_id=?',(request_id,)).fetchone()
    if previous and previous[0]!=digest:raise ValueError('operator_once_batch_scope_changed')
    c.execute('INSERT OR IGNORE INTO video_stream_operator_batches_v1 VALUES(?,?,?)',(request_id,digest,time.time()))
    reopened=set()
    for target in targets:
        expected=target['job'];key=expected['job_id'];number=expected['attempts']+1
        existing=c.execute('SELECT * FROM video_stream_operator_once_v1 WHERE request_id=? AND job_id=?',(request_id,key)).fetchone()
        identity=(number,expected['source_id'],expected['revision'],expected['input_sha'],expected['contract'],payload_sha(expected['payload']))
        if existing:
            if tuple(existing[k] for k in ('attempt_number','source_id','revision','input_sha','contract','payload_sha'))!=identity:
                raise ValueError('operator_once_request_identity_changed')
            continue
        job=c.execute('SELECT * FROM video_stream_jobs_v1 WHERE job_id=?',(key,)).fetchone()
        if not job or job['stage'] not in ('qwen','vad') or job['status']!='TERMINAL_FAILED':
            raise ValueError('operator_once_not_current_terminal_failure')
        if job['token'] is not None or job['lease_until'] is not None:
            raise ValueError('operator_once_active_ownership')
        for name in ('source_id','revision','stage','input_sha','contract','payload','attempts'):
            if job[name]!=expected[name]:raise ValueError('operator_once_failed_snapshot_changed:'+name)
        source=c.execute('SELECT revision FROM video_stream_sources_v1 WHERE source_id=?',(job['source_id'],)).fetchone()
        active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(job['source_id'],)).fetchone()
        if not source or not active or source[0]!=job['revision'] or active[0]!=job['revision']:
            raise ValueError('operator_once_stale_revision')
        payload=json.loads(job['payload']);canonical=target['canonical'];canonical_key=payload.get('_evidence_key')
        if canonical_key!=canonical['execution_key']:raise ValueError('operator_once_canonical_identity_changed')
        old=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(canonical_key,)).fetchone()
        if not old or old['status']!='FAILED_FINAL':raise ValueError('operator_once_canonical_not_failed')
        for name in ('source_id','source_revision','stage','input_sha','provenance','payload','attempts','result_sha'):
            if old[name]!=canonical[name]:raise ValueError('operator_once_canonical_changed:'+name)
        if (old['source_id'],old['source_revision'],old['stage'],old['input_sha'])!=(job['source_id'],job['revision'],job['stage'],job['input_sha']) or json.loads(old['provenance'])!=json.loads(job['contract']):
            raise ValueError('operator_once_contract_mismatch')
        audit=c.execute('SELECT status,error_type,error_message,finished_at FROM video_stream_attempts_v1 WHERE job_id=? AND attempt_number=?',(key,job['attempts'])).fetchone()
        previous=target['attempts'][-1]
        if not audit or tuple(audit)!=tuple(previous[k] for k in ('status','error_type','error_message','finished_at')):
            raise ValueError('operator_once_failure_audit_changed')
        if audit['finished_at'] is None or audit['status']!='TERMINAL_FAILED':raise ValueError('operator_once_missing_terminal_audit')
        c.execute('INSERT INTO video_stream_operator_once_v1(request_id,job_id,attempt_number,source_id,revision,input_sha,contract,payload_sha,authorized_at) VALUES(?,?,?,?,?,?,?,?,?)',(request_id,key,*identity,time.time()))
        c.execute("UPDATE video_stream_jobs_v1 SET status='RETRY_PENDING' WHERE job_id=?",(key,))
        c.execute("UPDATE v2_jobs SET status='FAILED_RETRYABLE' WHERE execution_key=?",(canonical_key,))
        if job['stage']=='qwen':
            c.execute("UPDATE v2_selected_candidates SET status='FAILED_RETRYABLE' WHERE candidate_id=? AND source_id=? AND source_revision=? AND status='FAILED_FINAL'",(payload['candidate_id'],job['source_id'],job['revision']))
        c.execute('INSERT INTO video_stream_events_v1(job_id,kind,payload) VALUES(?,?,?)',(key,'OPERATOR_ONE_ATTEMPT_AUTHORIZED',json.dumps({'request_id':request_id,'previous_attempt':job['attempts'],'authorized_attempt':number,'canonical_key':canonical_key},sort_keys=True)))
        reopened.add((job['source_id'],job['revision']))
    for source,revision in reopened:
        complete=c.execute("SELECT 1 FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='SOURCE_COMPLETE'",(source,revision)).fetchone()
        if complete:raise ValueError('operator_once_completed_source_requires_separate_review')
        old=c.execute("SELECT * FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='SOURCE_FAILED_FINAL'",(source,revision)).fetchone()
        outcome=c.execute('SELECT * FROM v2_source_outcomes WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
        c.execute('INSERT INTO video_stream_operator_source_history_v1 VALUES(?,?,?,?,?)',(request_id,source,revision,json.dumps(dict(old),sort_keys=True) if old else None,json.dumps(dict(outcome),sort_keys=True) if outcome else None))
        if old:
            # Move the active terminal fact to explicit history; do not erase it.
            # Existing source-local recovery now revalidates and seals delivery.
            c.execute("UPDATE video_stream_source_states_v1 SET state=? WHERE source_id=? AND revision=? AND state='SOURCE_FAILED_FINAL'",('SOURCE_FAILED_FINAL_BEFORE_OPERATOR_RETRY:'+request_id,source,revision))
        if outcome and outcome['status']=='FAILED_FINAL':
            c.execute("UPDATE v2_source_outcomes SET status='RECOVERING',reason_code='operator_one_attempt' WHERE source_id=? AND source_revision=?",(source,revision))


def permission(c,job):
    if job['stage'] not in ('qwen','vad'):return None
    row=c.execute('SELECT * FROM video_stream_operator_once_v1 WHERE job_id=? AND attempt_number=? AND claimed_token IS NULL',(job['job_id'],job['attempts']+1)).fetchone()
    if not row:return None
    expected=(job['source_id'],job['revision'],job['input_sha'],job['contract'],payload_sha(job['payload']))
    if tuple(row[k] for k in ('source_id','revision','input_sha','contract','payload_sha'))!=expected:
        raise ValueError('operator_once_claim_identity_changed')
    return dict(row)


def consume(c,permission,*,token,owner,epoch):
    changed=c.execute('UPDATE video_stream_operator_once_v1 SET claimed_token=?,claimed_owner=?,claimed_epoch=? WHERE request_id=? AND job_id=? AND claimed_token IS NULL',(token,owner,epoch,permission['request_id'],permission['job_id'])).rowcount
    if changed!=1:raise ValueError('operator_once_already_consumed')
    return {'request_id':permission['request_id'],'job_id':permission['job_id'],
            'attempt_number':permission['attempt_number'],'token':token}
