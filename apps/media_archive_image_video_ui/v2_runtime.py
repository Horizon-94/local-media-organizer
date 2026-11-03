"""DB-fenced per-artifact jobs and transactional outbox, no model imports."""
from __future__ import annotations
import json
import math
import threading
import time
import uuid
from .v2_producer import ProducerStore, fingerprint

class ContractError(RuntimeError): pass
class LostLease(ContractError): pass
class SupersededCompletion(LostLease): pass

class WorkerStore(ProducerStore):
    def __init__(self, db, policy=None, clock=time.time, *, scheduler_owner=None):
        super().__init__(db,policy,scheduler_owner=scheduler_owner)
        self.clock=clock
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS v2_jobs(execution_key TEXT PRIMARY KEY,stage TEXT,item_id TEXT,
              revision INTEGER,source_id TEXT,source_revision TEXT,input_sha TEXT,provenance TEXT,payload TEXT,
              status TEXT,attempts INTEGER DEFAULT 0,max_attempts INTEGER,owner TEXT,token TEXT,lease_until REAL,
              result_json TEXT,result_sha TEXT,error TEXT,UNIQUE(stage,item_id,revision));
            CREATE TABLE IF NOT EXISTS v2_job_dependencies(child_key TEXT,parent_key TEXT,PRIMARY KEY(child_key,parent_key));
            CREATE INDEX IF NOT EXISTS v2_dependency_parent ON v2_job_dependencies(parent_key);
            CREATE INDEX IF NOT EXISTS v2_jobs_lineage_header
              ON v2_jobs(execution_key,status,source_id,source_revision);
            CREATE INDEX IF NOT EXISTS v2_jobs_pending_probe ON v2_jobs(stage,status,lease_until)
              WHERE status IN ('PENDING','FAILED_RETRYABLE','RUNNING');
            CREATE VIEW IF NOT EXISTS v2_current_jobs AS
              WITH RECURSIVE ancestry(child_key,parent_key) AS (
                SELECT execution_key,execution_key FROM v2_jobs
                UNION SELECT a.child_key,d.parent_key FROM ancestry a JOIN v2_job_dependencies d ON d.child_key=a.parent_key
              )
              SELECT j.* FROM v2_jobs j WHERE j.status<>'SUPERSEDED' AND NOT EXISTS (
                SELECT 1 FROM ancestry a LEFT JOIN v2_jobs p ON p.execution_key=a.parent_key
                LEFT JOIN v2_active_sources active ON active.source_id=p.source_id
                WHERE a.child_key=j.execution_key AND (p.execution_key IS NULL OR
                  (a.parent_key<>j.execution_key AND p.status<>'DONE') OR
                  (active.source_revision IS NOT NULL AND active.source_revision<>p.source_revision)));
            CREATE TABLE IF NOT EXISTS v2_source_outcomes(source_id TEXT,source_revision TEXT,status TEXT,reason_code TEXT,execution_key TEXT,payload TEXT,PRIMARY KEY(source_id,source_revision));
            CREATE TABLE IF NOT EXISTS v2_retry_requests(request_id TEXT PRIMARY KEY,execution_key TEXT,generation INTEGER,actor TEXT,reason TEXT,previous_attempts INTEGER);
            CREATE TABLE IF NOT EXISTS v2_inbox(consumer TEXT,event_id TEXT,payload_sha TEXT,
              PRIMARY KEY(consumer,event_id));
            CREATE TABLE IF NOT EXISTS v2_deliveries(consumer TEXT,event_id TEXT,owner TEXT,lease_until REAL,
              status TEXT,PRIMARY KEY(consumer,event_id));
            """)

            from .v2_epoch_fence import add_epoch_columns
            add_epoch_columns(c,'v2_jobs')

    def assert_job_epoch(self, job):
        if self.scheduler_owner and any(job.get(key)!=self.scheduler_owner[key] for key in ('scheduler_run_id','execution_epoch')):
            from .scheduler_identity import SchedulerOwnershipError
            raise SchedulerOwnershipError('stale_job_execution_epoch')

    def candidate_status(self,c,stage,item,status):
        if stage not in ('qwen','ocr'):return
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_selected_candidates'").fetchone():
            c.execute("UPDATE v2_selected_candidates SET status=? WHERE candidate_id=? AND status<>'SUPERSEDED'",(status,item))

    def enqueue(self,c,stage,item_id,revision,source,source_revision,input_sha,provenance,payload,max_attempts=3,dependencies=()):
        self.assert_source_epoch(c,source,source_revision)
        # Failed decoder attempt events remain audit records, never new work.
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_decode_attempts'").fetchone():
            if c.execute("SELECT 1 FROM v2_decode_attempts WHERE source_id=? AND source_revision=? AND status='ABORTED'",(source,source_revision)).fetchone():return None
        if revision<1 or max_attempts<1 or not input_sha or not provenance:
            raise ContractError('job_contract_missing')
        dependencies=sorted(set(dependencies))
        anchored=False
        if dependencies and stage=='final_projection':
            # The matching manifest already proves this EXACT parent set.
            # Verify its immutable identity, direct edges and live transitive
            # barrier; never trust a cached DONE flag or caller-provided proof.
            anchor=c.execute("""SELECT execution_key FROM v2_jobs WHERE stage='final_manifest'
                AND item_id=? AND revision=? AND input_sha=? AND provenance=? AND status='DONE'
                AND source_id=? AND source_revision=?""",
                (source+':'+source_revision,revision,input_sha,json.dumps(provenance,sort_keys=True),source,source_revision)).fetchone()
            if anchor:
                edges={row[0] for row in c.execute('SELECT parent_key FROM v2_job_dependencies WHERE child_key=?',(anchor[0],))}
                anchored=(edges==set(dependencies) and bool(c.execute(
                    "SELECT 1 FROM v2_current_jobs WHERE execution_key=? AND status='DONE'",(anchor[0],)).fetchone()))
        if dependencies and not anchored:
            current={row[0] for row in c.execute("""SELECT execution_key FROM v2_current_jobs
                WHERE execution_key IN (SELECT value FROM json_each(?)) AND status='DONE'""",
                (json.dumps(dependencies),))}
            if current!=set(dependencies):return None
        key=fingerprint((stage,item_id,revision,input_sha,provenance,payload,dependencies))
        old=c.execute('SELECT execution_key FROM v2_jobs WHERE stage=? AND item_id=? AND revision=?',(stage,item_id,revision)).fetchone()
        if old and old[0]!=key: raise ContractError('same_revision_different_contract')
        latest=c.execute('SELECT MAX(revision) FROM v2_jobs WHERE stage=? AND item_id=?',(stage,item_id)).fetchone()[0]
        if latest is not None and revision<latest: return None
        capture=getattr(self,'enqueue_capture',None)
        if capture is not None and capture(c,key,{'stage':stage,'item_id':item_id,'revision':revision,
                'source':source,'source_revision':source_revision,'input_sha':input_sha,'provenance':provenance,
                'payload':payload,'max_attempts':max_attempts,'dependencies':dependencies}):return key
        guard=getattr(self,'enqueue_guard',None)
        if guard is not None and old is None:guard(c,stage)
        if latest is not None and revision>latest:
            # Completion/index coverage must be invalidated in the SAME revision
            # transaction, not after a best-effort Python callback.
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'v2_source_closure' in tables:
                c.execute("UPDATE v2_source_closure SET status='STALE' WHERE source_id=? AND source_revision=?",(source,source_revision))
            from .v2_visual_closure import invalidate_visual
            invalidate_visual(c,source,source_revision,stage,payload)
            self.emit(c,'EVIDENCE_REVISION_CHANGED',source,source_revision,revision,{'stage':stage,'item_id':item_id,'old_revision':latest,'new_revision':revision})
        from .v2_lineage import invalidate
        invalidate(c,[r[0] for r in c.execute('SELECT execution_key FROM v2_jobs WHERE stage=? AND item_id=? AND revision<?',(stage,item_id,revision))])
        if latest is not None and revision>latest and 'v2_vectors' in tables:
            if stage in ('openclip','embedding'):c.execute('DELETE FROM v2_vectors WHERE entity_id=?',(item_id,))
            if 'v2_documents' in tables:
                stale=list(c.execute("SELECT document_id FROM v2_documents WHERE raw_result_ref IN (SELECT execution_key FROM v2_jobs WHERE stage=? AND item_id=? AND status='SUPERSEDED')",(stage,item_id)))
                for doc in stale:c.execute('DELETE FROM v2_vectors WHERE entity_id=?',(doc[0],))
        c.execute("INSERT OR IGNORE INTO v2_jobs(execution_key,stage,item_id,revision,source_id,source_revision,input_sha,provenance,payload,status,max_attempts) VALUES(?,?,?,?,?,?,?,?,?,'PENDING',?)",
            (key,stage,item_id,revision,source,source_revision,input_sha,json.dumps(provenance,sort_keys=True),json.dumps(payload,sort_keys=True),max_attempts))
        if self.scheduler_owner:
            old=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
            if old['scheduler_run_id'] is not None:self.assert_job_epoch(dict(old))
            c.execute('UPDATE v2_jobs SET scheduler_run_id=?,execution_epoch=? WHERE execution_key=?',(self.scheduler_owner['scheduler_run_id'],self.scheduler_owner['execution_epoch'],key))
        # The verified manifest has this exact parent set and is itself current.
        # A projection already depends on that manifest; repeating every ancestor
        # for every document creates quadratic edges and generation-trigger work.
        # Keep the logical key above unchanged, and retain the identical transitive
        # ancestry through the manifest. Non-projection jobs keep direct edges.
        stored_dependencies=[anchor[0]] if anchored else dependencies
        c.execute('INSERT OR IGNORE INTO v2_job_dependencies SELECT ?,value FROM json_each(?)',(key,json.dumps(stored_dependencies)))
        return key

    def register_job(self,stage,item_id,revision,source,source_revision,input_sha,provenance,payload,max_attempts=3,dependencies=()):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            return self.enqueue(c,stage,item_id,revision,source,source_revision,input_sha,provenance,payload,max_attempts,dependencies)

    def consume(self,event,consumer,callback):
        """Inbox and downstream jobs commit together. Consumer must only write via c."""
        sha=fingerprint(event)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if self.scheduler_owner:
                owner=self.scheduler_owner
                persisted=c.execute('SELECT payload FROM v2_outbox WHERE event_id=? AND scheduler_run_id=? AND execution_epoch=?',(event['event_id'],owner['scheduler_run_id'],owner['execution_epoch'])).fetchone()
                if not persisted or any(event.get(k)!=owner[k] for k in owner):return False
                if fingerprint(json.loads(persisted[0]))!=sha:raise ContractError('event_id_payload_conflict')
            old=c.execute('SELECT payload_sha FROM v2_inbox WHERE consumer=? AND event_id=?',(consumer,event['event_id'])).fetchone()
            if old:
                if old[0]!=sha: raise ContractError('event_id_payload_conflict')
                return False
            callback(c,event)
            c.execute('INSERT INTO v2_inbox VALUES(?,?,?)',(consumer,event['event_id'],sha))
            return True

    def claim(self,stage,owner,lease_seconds=120,*,execution_contract=None):
        if lease_seconds<=0: raise ContractError('invalid_lease')
        expected=json.dumps(execution_contract,sort_keys=True) if execution_contract is not None else None
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            t=self.clock()
            expired=list(c.execute("SELECT * FROM v2_jobs WHERE stage=? AND status='RUNNING' AND lease_until<=? AND attempts>=max_attempts AND (? IS NULL OR provenance=?)",(stage,t,expected,expected)))
            for failed in expired:
                c.execute("UPDATE v2_jobs SET status='FAILED_FINAL',error='attempts_exhausted' WHERE execution_key=?",(failed['execution_key'],))
                self.emit(c,'EVIDENCE_FAILED',failed['source_id'],failed['source_revision'],failed['revision'],{'execution_key':failed['execution_key'],'stage':stage,'status':'FAILED_FINAL','error':'lease_expired_attempts_exhausted'})
                self.candidate_status(c,stage,failed['item_id'],'FAILED_FINAL')
            from .v2_product_lifecycle import product_claim_predicate
            eligible = product_claim_predicate(c, self.scheduler_owner)
            query="SELECT * FROM v2_current_jobs WHERE " + eligible + " AND stage=? AND attempts<max_attempts AND (status IN ('PENDING','FAILED_RETRYABLE') OR (status='RUNNING' AND lease_until<=?)) AND (? IS NULL OR provenance=?)"
            args=(stage,t,expected,expected);affinity=getattr(self,'source_affinity',None)
            preferred=affinity.preferred(stage) if affinity else None
            row=c.execute(query+' AND source_id=? ORDER BY item_id,revision LIMIT 1',(*args,preferred)).fetchone() if preferred else None
            if row is None:row=c.execute(query+' ORDER BY item_id,revision LIMIT 1',args).fetchone()
            if not row:return None
            self.assert_job_epoch(dict(row))
            if self.scheduler_owner and c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_product_lifecycle'").fetchone():
                c.execute("INSERT OR REPLACE INTO v2_product_job_passes SELECT scheduler_run_id,?,pass_index FROM v2_product_lifecycle WHERE scheduler_run_id=?", (row['execution_key'], self.scheduler_owner['scheduler_run_id']))
            token=uuid.uuid4().hex
            c.execute("UPDATE v2_jobs SET status='RUNNING',attempts=attempts+1,owner=?,token=?,lease_until=? WHERE execution_key=?",(owner,token,t+lease_seconds,row['execution_key']))
            self.candidate_status(c,stage,row['item_id'],'RUNNING')
            claimed=dict(c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(row['execution_key'],)).fetchone())
            # Reserve the hint's fairness turn while SQLite serializes claims.
            # This is not persisted ownership or evidence authority.
            if affinity:claimed['source_affinity_turn']=affinity.claimed(claimed,preferred)
        return claimed

    def has_runnable_jobs(self, stage, *, execution_contract=None):
        """Return whether a stage needs a handler before constructing one."""
        expected=json.dumps(execution_contract,sort_keys=True) if execution_contract is not None else None
        # FencedWorkerStore overrides connect() without the readonly keyword;
        # this is a read-only SELECT even when using that shared connection.
        with self.connect() as c:
            now=self.clock()
            parameters=(stage,now,expected,expected)
            # An empty stage needs no ancestry traversal at all. This cheap
            # negative probe cannot grant work; nonempty candidates still pass
            # the unchanged current-lineage and lifecycle barriers below.
            if not c.execute("""SELECT 1 FROM v2_jobs INDEXED BY v2_jobs_pending_probe WHERE stage=? AND attempts<max_attempts
                AND status IN ('PENDING','FAILED_RETRYABLE','RUNNING')
                AND (status IN ('PENDING','FAILED_RETRYABLE') OR
                     (status='RUNNING' AND lease_until<=?))
                AND (? IS NULL OR provenance=?) LIMIT 1""",parameters).fetchone():
                return False
            from .v2_product_lifecycle import product_claim_predicate
            eligible = product_claim_predicate(c, self.scheduler_owner)
            row=c.execute("SELECT 1 FROM v2_current_jobs WHERE " + eligible + """
              AND stage=? AND attempts<max_attempts
              AND (status IN ('PENDING','FAILED_RETRYABLE') OR
                   (status='RUNNING' AND lease_until<=?))
              AND (? IS NULL OR provenance=?) LIMIT 1""",
              parameters).fetchone()
        return row is not None

    def heartbeat(self,key,token,lease_seconds=120):
        with self.connect() as c:
            cur=c.execute("UPDATE v2_jobs SET lease_until=? WHERE execution_key=? AND token=? AND status='RUNNING' AND lease_until>?",(self.clock()+lease_seconds,key,token,self.clock()))
            return cur.rowcount==1

    def complete(self,job,result,validator,event_type='EVIDENCE_READY',transaction_hook=None):
        self.assert_job_epoch(job)
        validator(result)
        raw=json.dumps(result,sort_keys=True,allow_nan=False)
        sha=fingerprint(result)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(job['execution_key'],)).fetchone()
            # A newer authoritative revision may legitimately supersede an
            # in-flight result.  Keep the fence strict, but classify this
            # outcome so the worker loop can discard the stale result without
            # terminalizing the whole product run.
            if not c.execute('SELECT 1 FROM v2_current_jobs WHERE execution_key=?',(job['execution_key'],)).fetchone():raise SupersededCompletion('completion_lineage_inactive')
            if row['provenance']!=job['provenance']:raise ContractError('claimed_contract_changed')
            if row['status']=='DONE' and row['token']==job['token']:
                if row['result_sha']!=sha:raise ContractError('completion_digest_conflict')
                return
            if row['status']!='RUNNING' or row['token']!=job['token'] or row['lease_until']<=self.clock(): raise LostLease('completion_without_current_ownership')
            c.execute("UPDATE v2_jobs SET status='DONE',result_json=?,result_sha=?,lease_until=NULL WHERE execution_key=?",(raw,sha,job['execution_key']))
            self.candidate_status(c,job['stage'],job['item_id'],'DONE')
            if job['stage'] in ('qwen','ocr') and c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_final_projection_heads'").fetchone():
                # New peers change the legacy project-wide duplicate quality
                # snapshot, including the previous absence of duplicate text.
                from .v2_lineage import invalidate
                invalidate(c,[r[0] for r in c.execute('SELECT execution_key FROM v2_final_projection_heads')])
            payload={'execution_key':job['execution_key'],'stage':job['stage'],'item_id':job['item_id'],
                     'evidence_revision':job['revision'],'input_sha':job['input_sha'],
                     'provenance':json.loads(job['provenance']),'payload_sha':sha,'result':result}
            if transaction_hook:transaction_hook(c,job,result)
            self.emit(c,event_type,job['source_id'],job['source_revision'],job['revision'],payload,
                      producer_id=job['stage'],contract_hash=fingerprint(json.loads(job['provenance'])))

    def fail(self,job,error,retryable=True):
        if 'SPECIAL_REVIEW_REQUIRED:' in str(error):
            retryable = False
        self.assert_job_epoch(job)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            life = None
            if self.scheduler_owner and c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_product_lifecycle'").fetchone():
                life = c.execute('SELECT * FROM v2_product_lifecycle WHERE scheduler_run_id=?', (self.scheduler_owner['scheduler_run_id'],)).fetchone()
            if life and life['stop_requested']:
                cur = c.execute("UPDATE v2_jobs SET status='PENDING',attempts=attempts-1,owner=NULL,token=NULL,lease_until=NULL WHERE execution_key=? AND status='RUNNING' AND token=?", (job['execution_key'],job['token']))
                if cur.rowcount != 1: raise LostLease('handoff_without_ownership')
                self.candidate_status(c,job['stage'],job['item_id'],'PENDING')
                return
            retryable = retryable and (not life or life['pass_index'] < 2)
            status='FAILED_RETRYABLE' if retryable and job['attempts']<job['max_attempts'] else 'FAILED_FINAL'
            cur=c.execute("UPDATE v2_jobs SET status=?,error=?,lease_until=NULL WHERE execution_key=? AND status='RUNNING' AND token=? AND lease_until>?",(status,str(error),job['execution_key'],job['token'],self.clock()))
            if cur.rowcount!=1:raise LostLease('failure_without_ownership')
            self.candidate_status(c,job['stage'],job['item_id'],status)
            self.emit(c,'EVIDENCE_FAILED',job['source_id'],job['source_revision'],job['revision'],{'execution_key':job['execution_key'],'stage':job['stage'],'status':status,'error':str(error)})

    def reap_expired(self,source,revision):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            life = None
            if self.scheduler_owner and c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_product_lifecycle'").fetchone():
                life = c.execute('SELECT * FROM v2_product_lifecycle WHERE scheduler_run_id=?', (self.scheduler_owner['scheduler_run_id'],)).fetchone()
            if life:
                from .v2_source_lineage import current_source_rows
                expired=current_source_rows(c,source,revision,"j.status='RUNNING' AND j.lease_until<=?",(self.clock(),))
                for job in expired:
                    status = 'FAILED_FINAL' if life['pass_index'] >= 2 or job['attempts'] >= job['max_attempts'] else 'FAILED_RETRYABLE'
                    c.execute("UPDATE v2_jobs SET status=?,token=NULL,owner=NULL,lease_until=NULL,error='lease_expired' WHERE execution_key=?", (status,job['execution_key']))
                    self.candidate_status(c,job['stage'],job['item_id'],status)
                    self.emit(c,'EVIDENCE_FAILED',source,revision,job['revision'],{'execution_key':job['execution_key'],'stage':job['stage'],'status':status,'error':'lease_expired'})
                return
            from .v2_source_lineage import current_source_rows
            rows=current_source_rows(c,source,revision,"j.status='RUNNING' AND j.lease_until<=? AND j.attempts>=j.max_attempts",(self.clock(),))
            for job in rows:
                c.execute("UPDATE v2_jobs SET status='FAILED_FINAL',token=NULL,error='lease_expired_attempts_exhausted' WHERE execution_key=?",(job['execution_key'],))
                self.candidate_status(c,job['stage'],job['item_id'],'FAILED_FINAL')
                self.emit(c,'EVIDENCE_FAILED',source,revision,job['revision'],{'execution_key':job['execution_key'],'stage':job['stage'],'status':'FAILED_FINAL','error':'lease_expired_attempts_exhausted'})

    def retry_final(self,key,*,request_id,actor,reason,connection=None):
        """Explicit audited retry generation; deterministic input identity stays.

        Only final failures can be reopened. Existing successful results are never
        rewritten. Changed inputs/contracts must use a new execution revision.
        """
        if not request_id or not actor or not reason:raise ContractError('retry_authority_required')
        if connection is not None:
            if not connection.in_transaction:raise ContractError('retry_requires_transaction')
            return self._retry_final_transaction(connection,key,request_id=request_id,actor=actor,reason=reason)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            return self._retry_final_transaction(c,key,request_id=request_id,actor=actor,reason=reason)

    def _retry_final_transaction(self,c,key,*,request_id,actor,reason):
        old=c.execute('SELECT * FROM v2_retry_requests WHERE request_id=?',(request_id,)).fetchone()
        if old:
            if (old['execution_key'],old['actor'],old['reason'])!=(key,actor,reason):raise ContractError('retry_request_conflict')
            return old['generation']
        job=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(key,)).fetchone()
        if not job or job['status']!='FAILED_FINAL':raise ContractError('retry_requires_current_final_failure')
        generation=c.execute('SELECT COALESCE(MAX(generation),0)+1 FROM v2_retry_requests WHERE execution_key=?',(key,)).fetchone()[0]
        c.execute('INSERT INTO v2_retry_requests VALUES(?,?,?,?,?,?)',(request_id,key,generation,actor,reason,job['attempts']))
        c.execute("UPDATE v2_jobs SET status='PENDING',attempts=0,owner=NULL,token=NULL,lease_until=NULL,error=NULL WHERE execution_key=?",(key,))
        self.candidate_status(c,job['stage'],job['item_id'],'QUEUED')
        c.execute("UPDATE v2_source_outcomes SET status='RECOVERING',reason_code='manual_retry',payload=? WHERE source_id=? AND source_revision=?",(json.dumps({'request_id':request_id,'generation':generation}),job['source_id'],job['source_revision']))
        self.emit(c,'WORK_RETRY_REQUESTED',job['source_id'],job['source_revision'],generation,{'execution_key':key,'request_id':request_id,'actor':actor,'reason':reason,'generation':generation})
        return generation

    def record_outcome(self,source,revision,result):
        with self.connect() as c:
            self.assert_source_epoch(c,source,revision)
            # Product migration keeps the provenance column non-null.  A
            # WAITING/ABORTED source outcome may legitimately have no job key;
            # preserve that semantic distinction with an empty provenance
            # value instead of failing only on migrated databases.
            c.execute('INSERT OR REPLACE INTO v2_source_outcomes VALUES(?,?,?,?,?,?)',(source,revision,result['status'],result.get('reason_code',result.get('reason','')),result.get('execution_key') or '',json.dumps(result,sort_keys=True)))
        return result

    def deliver(self,consumer,callback,limit=100):
        with self.connect() as c:
            events=[json.loads(r[0]) for r in c.execute('''SELECT o.payload FROM v2_outbox o
              WHERE NOT EXISTS (SELECT 1 FROM v2_inbox i WHERE i.consumer=? AND i.event_id=o.event_id)
              AND (? IS NULL OR (o.scheduler_run_id=? AND o.execution_epoch=?))
              ORDER BY o.committed_at,o.event_id LIMIT ?''',(consumer,(self.scheduler_owner or {}).get('scheduler_run_id'),(self.scheduler_owner or {}).get('scheduler_run_id'),(self.scheduler_owner or {}).get('execution_epoch'),limit))]
        count=0
        for event in events:
            if count>=limit: break
            if self.consume(event,consumer,callback):count+=1
        return count

    def deliver_fair(self,consumer,callback,priority_types=(),limit=100,priority_only=False):
        """Deliver a bounded priority slice, then a bounded FIFO slice.

        This is still the same durable inbox/consume path.  It only changes
        selection of the next event so readiness events cannot sit behind a
        large historical audit backlog.  The caller must invoke this once per
        scheduler quantum; it must not loop until the queue is empty.
        """
        priority_types=tuple(priority_types)
        self._last_fair_stale=0
        with self.connect() as c:
            scope=(consumer,(self.scheduler_owner or {}).get('scheduler_run_id'),
                   (self.scheduler_owner or {}).get('scheduler_run_id'),
                   (self.scheduler_owner or {}).get('execution_epoch'))
            base='''FROM v2_outbox o
              WHERE NOT EXISTS (SELECT 1 FROM v2_inbox i WHERE i.consumer=? AND i.event_id=o.event_id)
              AND (? IS NULL OR (o.scheduler_run_id=? AND o.execution_epoch=?))'''
            args=list(scope)
            rows=[]
            if priority_types:
                placeholders=','.join('?' for _ in priority_types)
                rows.extend(r[0] for r in c.execute('SELECT o.payload '+base+f" AND o.event_type IN ({placeholders}) ORDER BY o.committed_at,o.event_id LIMIT ?",(*args,*priority_types,limit)).fetchall())
            remaining=max(0,limit-len(rows))
            if remaining and not priority_only:
                rows.extend(r[0] for r in c.execute('SELECT o.payload '+base+" ORDER BY o.committed_at,o.event_id LIMIT ?",(*args,remaining)).fetchall())
        count=0
        for payload in rows:
            if count>=limit:break
            try:
                if self.consume(json.loads(payload),consumer,callback):count+=1
            except ContractError as error:
                # A fenced coordination proposal may be stale for this
                # event only.  Leave it unacked for a later snapshot, but do
                # not let it block the rest of this bounded quantum.
                if str(error)!='coordination_snapshot_changed_retry':raise
                self._last_fair_stale+=1
        return count

    def fanout(self,event,contracts):
        if event['event_type']!='FRAME_READY':return False
        def create(c,e):
            for stage in ('yoloe','openclip','dhash','quality','geometry'):
                if stage not in contracts:raise ContractError('missing_fanout_contract:'+stage)
                self.enqueue(c,stage,e['payload']['frame_id'],contracts[stage]['revision'],e['source_id'],e['source_revision'],
                    e['payload']['derived_sha'],contracts[stage],e['payload'])
        return self.consume(event,'visual-fanout:'+fingerprint(contracts),create)

    def run_one(self,stage,handler,validator,owner='worker',lease_seconds=120,event_type='EVIDENCE_READY',*,execution_contract=None):
        job=self.claim(stage,owner,lease_seconds,execution_contract=execution_contract)
        if not job:return False
        stopped=threading.Event()
        def renew():
            while not stopped.wait(lease_seconds/3):
                if not self.heartbeat(job['execution_key'],job['token'],lease_seconds):return
        thread=threading.Thread(target=renew,daemon=True);thread.start()
        try:
            result=handler(json.loads(job['payload']))
            self.complete(job,result,validator,event_type)
        except SupersededCompletion:
            return False
        except LostLease:raise
        except Exception as exc:self.fail(job,exc)
        finally:stopped.set();thread.join()
        return True

def validate_evidence(kind,result):
    if not isinstance(result,dict):raise ContractError('evidence_not_object')
    required={'yoloe':('detections',),'openclip':('vector',),'dhash':('dhash',),
              'quality':('brightness','black_ratio','sharpness'),'geometry':('width','height'),
              'insightface':('faces',)}
    if any(k not in result for k in required[kind]):raise ContractError('evidence_missing_field')
    if kind=='insightface':
        faces=result['faces']
        if not isinstance(faces,list):raise ContractError('invalid_face_list')
        for face in faces:
            if not isinstance(face,dict):raise ContractError('invalid_face')
            box=face.get('bbox');vector=face.get('embedding')
            if not isinstance(box,(list,tuple)) or len(box)!=4 or not all(type(x) in (int,float) and math.isfinite(x) for x in box) or box[2]<=box[0] or box[3]<=box[1]:raise ContractError('invalid_face_bbox')
            if not isinstance(vector,list) or len(vector)!=512 or not all(type(x) in (int,float) and math.isfinite(x) for x in vector):raise ContractError('invalid_face_embedding')
            if not math.isclose(sum(x*x for x in vector),1.0,abs_tol=1e-3):raise ContractError('invalid_face_embedding_norm')
            for key in ('quality_score','detection_score'):
                value=face.get(key)
                if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:raise ContractError('invalid_face_score')
    if kind=='openclip':
        v=result['vector']
        if not v or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in v):raise ContractError('invalid_vector')
        if sum(x*x for x in v)<=0:raise ContractError('zero_vector')
    if kind=='yoloe' and not isinstance(result['detections'],list):raise ContractError('missing_not_zero_detection')
    if kind=='yoloe':
        for d in result['detections']:
            if not isinstance(d,dict) or not d.get('label') or not isinstance(d.get('confidence'),(int,float)) or not math.isfinite(d['confidence']) or not 0<=d['confidence']<=1:raise ContractError('invalid_detection')
            b=d.get('bbox',d.get('bbox_xyxy'))
            if not isinstance(b,(list,tuple)) or len(b)!=4 or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in b) or b[2]<=b[0] or b[3]<=b[1]:raise ContractError('invalid_detection_bbox')
    if kind=='quality':
        if any(not isinstance(result[k],(int,float)) or not math.isfinite(result[k]) for k in required[kind]):raise ContractError('invalid_quality')
        if not 0<=result['brightness']<=255 or not 0<=result['black_ratio']<=1 or result['sharpness']<0:raise ContractError('invalid_quality_range')
    if kind=='geometry' and any(not isinstance(result[k],int) or result[k]<=0 for k in required[kind]):raise ContractError('invalid_geometry')
    if kind=='dhash':
        try:value=result['dhash'] if isinstance(result['dhash'],int) else int(result['dhash'],16)
        except (TypeError,ValueError):raise ContractError('invalid_dhash')
        if not 0<=value<2**64:raise ContractError('invalid_dhash')
