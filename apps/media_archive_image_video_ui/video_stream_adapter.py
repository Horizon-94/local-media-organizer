"""Frozen source-local policy adapters, never the central scheduler loop.

The experimental ledger owns dispatch/leases. Existing V2 tables are durable
policy/index evidence projections, published in the SAME result transaction.
No old worker claims, source-advance loop, global people reconciliation or model
inference is invoked here. Expensive policy code runs on coordination thread.
"""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time
from .v2_runtime import WorkerStore,ContractError
from .v2_producer import fingerprint
import os
from .v2_closure import ClosurePipeline
from .video_stream_ledger import StaleResult


import collections,os,threading,time
_INDEX_DISCOVERY_FULL_SQL="WITH pending AS MATERIALIZED(SELECT event_id FROM v2_outbox EXCEPT SELECT event_id FROM v2_inbox WHERE consumer=? AND event_id IS NOT NULL ORDER BY 1) SELECT o.aggregate_id source_id,count(*) n,min(o.rowid) sequence FROM pending p JOIN v2_outbox o ON o.event_id IS p.event_id GROUP BY o.aggregate_id"
_INDEX_DISCOVERY_HEAD_SQL="WITH pending AS MATERIALIZED(SELECT event_id FROM v2_outbox EXCEPT SELECT event_id FROM v2_inbox WHERE consumer=? AND event_id IS NOT NULL ORDER BY 1) SELECT o.event_id,o.aggregate_id,o.rowid FROM pending p JOIN v2_outbox o ON o.event_id IS p.event_id LIMIT ?"
class _PendingIndexDiscovery:
    MAX_HEADERS=50000
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.lock=threading.RLock();self.rows=None;self.meta=None;self.refreshed=0.;self.turns=0;self.oversize=False;self.last_mode='FULL';self.header_count=0
    @staticmethod
    def full(c,consumer):return [dict(zip(('source_id','n','sequence'),r))for r in c.execute(_INDEX_DISCOVERY_FULL_SQL,(consumer,))]
    def read(self,c,consumer):
        with self.lock:
            own=not c.in_transaction
            if own:c.execute('BEGIN')
            try:return self._read(c,consumer)
            except BaseException:
                self.rows=None;self.meta=None;raise
            finally:
                if own:c.rollback()
    def _read(self,c,consumer):
        if self.oversize:
            ready=self.full(c,consumer);self.oversize=sum(r['n']for r in ready)>self.MAX_HEADERS;self.last_mode='FULL_CAP';return ready
        schema=c.execute('PRAGMA schema_version').fetchone()[0]
        path=next((r[2]for r in c.execute('PRAGMA database_list')if r[1]=='main'),'')
        identity=(os.stat(path).st_dev,os.stat(path).st_ino)if path else ('memory',id(c))
        # A count detects deletes/replacements and rowid reuse across epochs.
        # These covering-index counts are cheaper than rebuilding the global
        # UUID set difference for every bounded delivery.
        nout,mout=c.execute('SELECT count(*),coalesce(max(rowid),0) FROM v2_outbox').fetchone()
        nin=c.execute('SELECT count(*) FROM v2_inbox WHERE consumer=?',(consumer,)).fetchone()[0]
        minbox=c.execute('SELECT coalesce(max(rowid),0) FROM v2_inbox').fetchone()[0]
        meta=(identity,schema,consumer,nout,mout,nin,minbox)
        refresh=(self.rows is None or self.meta[:3]!=meta[:3] or self.turns>=32 or self.clock()-self.refreshed>=30)
        if not refresh:
            old=self.meta
            added=list(c.execute('SELECT event_id,aggregate_id,rowid FROM v2_outbox WHERE rowid>? LIMIT ?',(old[4],self.MAX_HEADERS+1)))
            acked=list(c.execute('SELECT event_id FROM v2_inbox NOT INDEXED WHERE rowid>? AND consumer=? LIMIT ?',(old[6],consumer,self.MAX_HEADERS+1)))
            refresh=(len(added)>self.MAX_HEADERS or len(acked)>self.MAX_HEADERS or mout<old[4] or minbox<old[6] or nout!=old[3]+len(added) or nin!=old[5]+len(acked) or any(r[0]is None or r[1]is None for r in added) or any(r[0]is None for r in acked))
            if not refresh:
                for eid,sid,seq in added:
                    # Legacy orphan ACK may already exist before this event.
                    if not c.execute('SELECT 1 FROM v2_inbox WHERE consumer=? AND event_id=?',(consumer,eid)).fetchone():self.rows[eid]=(sid,seq)
                for (eid,)in acked:self.rows.pop(eid,None)
                if len(self.rows)>self.MAX_HEADERS:refresh=True
        if refresh:
            raw=list(c.execute(_INDEX_DISCOVERY_HEAD_SQL,(consumer,self.MAX_HEADERS+1)))
            if len(raw)>self.MAX_HEADERS or any(r[0]is None or r[1]is None for r in raw):
                self.rows=None;self.meta=None;self.oversize=len(raw)>self.MAX_HEADERS;self.last_mode='FULL_CAP_OR_LEGACY';return self.full(c,consumer)
            self.rows={eid:(sid,seq)for eid,sid,seq in raw};self.refreshed=self.clock();self.turns=0;self.last_mode='REBUILD'
        else:self.last_mode='INCREMENTAL'
        self.meta=meta;self.turns+=1;self.header_count=len(self.rows)
        groups={}
        for sid,seq in self.rows.values():
            if sid not in groups:groups[sid]=[0,seq]
            groups[sid][0]+=1;groups[sid][1]=min(groups[sid][1],seq)
        return [{'source_id':sid,'n':n,'sequence':seq}for sid,(n,seq)in sorted(groups.items())]


class EvidenceStore(WorkerStore):
    def __init__(self,ledger,policy=None):
        self.ledger=ledger
        super().__init__(ledger.path,policy)
        self.targeted_projection_invalidation_enabled=True
        # Source-local fences must not scan other sources while holding writer.
        with self.connect() as c:
            c.execute('CREATE INDEX IF NOT EXISTS p0_mapping_artifact ON v2_artifact_mappings(artifact_id)')
            c.execute('CREATE INDEX IF NOT EXISTS p0_jobs_source_stage ON v2_jobs(source_id,stage)')
            # Measured source-snapshot scans; additive read indexes only.
            c.execute('CREATE INDEX IF NOT EXISTS p0_attempts_source ON video_stream_attempts_v1(source_id)')
            c.execute('CREATE INDEX IF NOT EXISTS p0_events_job ON video_stream_events_v1(job_id)')
            c.execute('CREATE INDEX IF NOT EXISTS p0_inbox_event ON v2_inbox(event_id)')
            c.execute('CREATE INDEX IF NOT EXISTS p0_documents_source ON v2_documents(source_id)')
            from .video_delivery_queries import install
            install(c)


    @contextmanager
    def connect(self):
        with self.ledger.connection() as c:
            self.ledger.fence(c,allow_stop=True)
            yield c
            self.ledger.fence(c,allow_stop=True)

    def consume(self,event,consumer,callback):
        # Retired revision events remain audit facts, not work for the active
        # source. ACK their skip so an old SOURCE_READY cannot starve FIFO.
        with self.connect() as reader:
            active=reader.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(event['source_id'],)).fetchone()
            prior=reader.execute('SELECT payload_sha FROM v2_inbox WHERE consumer=? AND event_id=?',(consumer,event['event_id'])).fetchone()
        if active is not None and active[0]!=event['source_revision']:
            from .video_commit_queue import SQLCommand,WriteProposal,StaleProposal
            digest=fingerprint(event)
            if prior:
                if prior[0]!=digest:raise ContractError('event_id_payload_conflict')
                return False
            checks=((SQLCommand('SELECT owner,epoch FROM video_stream_control_v1 WHERE singleton=1'),(self.ledger.owner,self.ledger.epoch)),
                    (SQLCommand('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(event['source_id'],)),(active[0],)))
            try:self.ledger.commit_queue.submit(WriteProposal('skip_stale_event',event['source_id'],
                (SQLCommand('INSERT OR IGNORE INTO v2_inbox VALUES(?,?,?)',(consumer,event['event_id'],digest)),),checks)).result()
            except StaleProposal as error:raise ContractError('coordination_snapshot_changed_retry') from error
            return True
        # Durable identical ACK is already the original consume terminal result.
        # Legacy scheduler-owner validation remains on the original path.
        if prior and self.scheduler_owner is None:
            if prior[0]!=fingerprint(event):raise ContractError('event_id_payload_conflict')
            return False
        from .video_detached_coordination import run
        return run(self,event['source_id'],event['source_revision'],'consume:'+consumer,
            lambda store,c:WorkerStore.consume(store,event,consumer,callback))

    def _deliver_batch(self,consumer,callback,limit,priority_only=False,partition=None):
        """One fenced snapshot per bounded source quantum, not per event."""
        if limit<=0:return 0
        with self.connect() as c:
            clause=''
            if consumer=='fusion-index':
                if getattr(self,'index_phase',None):self.index_phase('DISCOVERING')
                discovery_started=time.perf_counter()
                # Runtime owns the source decision. SQLite supplies only
                # current eligible event headers, never a persisted queue.
                discovery=getattr(self,'_index_pending_discovery',None)
                if discovery is None:self._index_pending_discovery=discovery=_PendingIndexDiscovery()
                ready=discovery.read(c,consumer)
                discovery_ms=(time.perf_counter()-discovery_started)*1000
                candidates=[{'source_id':r['source_id'],'sequence':r['sequence'],'status':'PENDING','work_items':r['n']} for r in ready]
                affinity=getattr(self,'index_affinity',None)
                ordered=affinity.order('index',candidates) if affinity else []
                if affinity:affinity.backlogs['index']=sum(r['n'] for r in ready)
                if affinity and candidates and not ordered:return 0
                if ordered:
                    aggregate=ordered[0]['source_id'];clause=' AND o.aggregate_id='+"'"+aggregate.replace("'","''")+"'"
                    self.current_source_work=next(r['n'] for r in ready if r['source_id']==aggregate)
            if consumer=='video-stream-documents-v1':
                from .video_delivery_queries import delivery_predicate
                clause=' AND '+('0' if priority_only else delivery_predicate(partition))
            # CANDIDATE_READY is excluded above; the old CASE key was always 1.
            order=''
            events=[json.loads(row[0]) for row in c.execute('SELECT o.payload FROM v2_outbox o WHERE NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer=? AND i.event_id=o.event_id)'+clause+' ORDER BY '+order+'o.committed_at,o.event_id LIMIT ?',(consumer,limit))]
        groups={}
        for event in events:groups.setdefault((event['source_id'],event['source_revision']),[]).append(event)
        count=0;self._last_fair_stale=0
        from .video_detached_coordination import run
        for (source,revision),batch in groups.items():
            with self.connect() as c:active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()
            if active is not None and active[0]!=revision:
                # Retired immutable events are ACKed under the existing
                # owner/current-revision fence, not repeatedly prepared.
                for event in batch:count+=bool(self.consume(event,consumer,lambda c,e:None))
                if consumer=='fusion-index' and getattr(self,'index_affinity',None):self.index_affinity.release('index','retired_revision')
                continue
            self.proposal_context={'trigger_event':[e['event_id'] for e in batch],'claimed_count':len(batch),'batch_size':len(batch),'queue_depth':getattr(self,'index_affinity',None).backlogs.get('index') if getattr(self,'index_affinity',None) else None}
            if consumer=='fusion-index':
                self.proposal_context.update(candidate_query_ms=discovery_ms,discovery_mode=discovery.last_mode,discovery_cached_headers=discovery.header_count)
            document_root=(getattr(self,'document_root_snapshots',False) and consumer=='video-stream-documents-v1'
                and all(e['event_type']=='EVIDENCE_READY' and e['payload'].get('stage') in ('qwen','ocr','whisper','embedding') for e in batch))
            self.proposal_events=batch if consumer=='fusion-index' or document_root else None
            if getattr(self,'lane_report',None):self.lane_report(source,revision,len(batch),getattr(self,'current_source_work',None))
            def calculate(store,c):
                return sum(bool(WorkerStore.consume(store,event,consumer,callback)) for event in batch)
            evidence=[(e['payload']['execution_key'],e['payload']['payload_sha']) for e in batch
                      if e['event_type']=='EVIDENCE_READY' and e['payload'].get('stage') in ('audio_extract','vad')]
            narrow=evidence if consumer=='video-stream-documents-v1' and len(evidence)==len(batch) else None
            try:
                delivered=run(self,source,revision,'consume_batch:'+consumer,calculate,event_evidence=narrow);count+=delivered
                if delivered:
                    from .build151_whisper_feed import call
                    call(self.ledger,'upstream_committed',batch,consumer)
            except ContractError as error:
                if str(error)!='coordination_snapshot_changed_retry':raise
                self._last_fair_stale+=1
                if consumer=='fusion-index' and getattr(self,'index_affinity',None):self.index_affinity.backoff('index',source+':'+revision)
            if consumer=='fusion-index' and getattr(self,'index_affinity',None):
                for _ in batch:self.index_affinity.claimed('index',source+':'+revision)
            self.proposal_context={}
            self.proposal_events=None
        return count

    def deliver(self,consumer,callback,limit=100):
        return self._deliver_batch(consumer,callback,min(limit,32))

    def deliver_fair(self,consumer,callback,priority_types=(),limit=100,priority_only=False):
        return self._deliver_batch(consumer,callback,min(limit,32),priority_only)

    def enqueue(self,c,stage,item_id,revision,source,source_revision,input_sha,provenance,payload,max_attempts=None,dependencies=()):
        if max_attempts is None:max_attempts=4 if stage in ('qwen','ocr') else 3
        if stage=='final_projection' and dependencies:
            head=c.execute("SELECT execution_key FROM v2_jobs WHERE stage='final_manifest' AND item_id=? AND revision=? AND input_sha=? AND status='DONE'",(source+':'+source_revision,revision,input_sha)).fetchone()
            if head:
                edges={row[0] for row in c.execute('SELECT parent_key FROM v2_job_dependencies WHERE child_key=?',(head[0],))}
                if edges==set(dependencies):dependencies=[head[0]]
        return super().enqueue(c,stage,item_id,revision,source,source_revision,input_sha,provenance,payload,max_attempts,dependencies)


class FrozenAdapters:
    def __init__(self,ledger,contracts):
        self.ledger,self.contracts=ledger,contracts
        self.store=EvidenceStore(ledger)
        self.store.adapter=self
        self.pipeline=ClosurePipeline(self.store,contracts)
        from .video_stream_index import VideoFusionIndex
        self.pipeline.index=VideoFusionIndex(self.store)
        ledger.completion_hook=self.mirror_result
        ledger.prepare_completion=self.prepare_mirror_result
        ledger.apply_prepared_completion=self.apply_prepared_mirror_result
        ledger.attempt_failure_hook=self.mirror_failed_attempt
        from .video_result_storage import compact
        # OpenCLIP consumers use the canonical top-level vector, never its
        # duplicate diagnostic copy. YOLO registry sharing remains candidate-
        # only until final-projection hydration has a proven scoped adapter.
        ledger.result_projection=lambda c,job,value:compact(c,'openclip',value) if job['stage']=='openclip' else value
        ledger.reference_results=True
        with self.store.connect() as c:
            # Namespaced streaming schema, not a production migration. Reader
            # qualification precedes new native reference-only writes.
            from .video_canonical_store import SCHEMA
            for statement in SCHEMA.split(';'):
                if statement.strip().startswith('CREATE TABLE'):
                    c.execute(statement.replace('CREATE TABLE','CREATE TABLE IF NOT EXISTS',1))
            c.executescript('''
              CREATE INDEX IF NOT EXISTS video_stream_source_outbox_v1 ON v2_outbox(aggregate_id,event_type,event_id);
              CREATE TABLE IF NOT EXISTS video_stream_mirrors_v1(job_id TEXT,stage TEXT,execution_key TEXT UNIQUE,PRIMARY KEY(job_id,stage));
              CREATE TABLE IF NOT EXISTS video_stream_source_states_v1(source_id TEXT,revision TEXT,state TEXT,payload TEXT,PRIMARY KEY(source_id,revision,state));
              CREATE TABLE IF NOT EXISTS video_stream_selection_proofs_v1(snapshot_id TEXT PRIMARY KEY,proof_key TEXT UNIQUE,source_id TEXT,source_revision TEXT);
            ''')
        from .video_commit_queue import CommitQueue
        from .video_stream_commit import prepare
        from pathlib import Path
        ledger.prepare_sql_completion=lambda job,payload,digest,ms,parts:prepare(self,job,payload,digest,ms,parts)
        from .video_selection_proof import parent
        from pathlib import Path
        self.store.selection_parent_provider=lambda c,event:parent(self.store,c,event)
        from .video_coordination import install_tokens
        from .video_stream_people import MANUAL
        with self.store.connect() as c:
            tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'v2_%'")
                    if any(col[1]=='source_id' for col in c.execute('PRAGMA table_info('+r[0]+')'))]
        install_tokens(self.store,(*tables,*MANUAL,'v2_job_dependencies','v2_snapshot_evidence'))
        ledger.commit_queue=CommitQueue(ledger.path,trace_path=Path(ledger.path).with_name('serialized-transactions.jsonl'),history_limit=1024)
        from .video_peer_catalog import schema,bootstrap
        schema(self);bootstrap(self)

    def state(self,source,revision,state,payload=None):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT revision FROM video_stream_sources_v1 WHERE source_id=?',(source,)).fetchone()[0]!=revision:
                raise ContractError('stale_source_state')
            if state in ('PRESELECTION_READY','SELECTION_DONE'):
                active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)).fetchone()
                if not active or active[0]!=revision:raise ContractError('stale_source_state')
                if state=='SELECTION_DONE':
                    published=c.execute("SELECT payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='FIRST_SELECTION_PUBLISHED'",(source,revision)).fetchone()
                    if published:
                        from .video_coordination import current_evidence_check
                        check=current_evidence_check(json.loads(published[0])['proof_key'])
                        current=c.execute(check.sql,check.parameters).fetchone()
                        if not current or current[0]!='DONE' or tuple(current[2:])!=(source,revision):raise ContractError('coordination_snapshot_changed_retry')
            if state in ('QWEN_FIRST_PASS_DONE','OCR_FIRST_PASS_DONE'):
                stage='qwen' if state.startswith('QWEN') else 'ocr'
                branch=c.execute('SELECT expected FROM video_stream_branches_v1 WHERE source_id=? AND revision=? AND stage=?',(source,revision,stage)).fetchone()
                count=c.execute("SELECT count(*) FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage=? AND (attempts>0 OR (status='DONE' AND job_id LIKE 'import:%'))",(source,revision,stage)).fetchone()[0]
                if not branch or count!=branch[0]:raise ContractError('first_pass_unattempted_candidate')
            c.execute('INSERT OR IGNORE INTO video_stream_source_states_v1 VALUES(?,?,?,?)', (source,revision,state,json.dumps(payload or {},sort_keys=True)))

    def has_state(self,source,revision,state):
        with self.store.connect() as c:
            return bool(c.execute('SELECT 1 FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state=?',(source,revision,state)).fetchone())

    def mirror_failed_attempt(self,c,job,status,error):
        key=job['payload'].get('_evidence_key')
        if not key:return
        old=c.execute('SELECT source_id,source_revision,status FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
        active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(job['source_id'],)).fetchone()
        if not old or old['source_id']!=job['source_id'] or old['source_revision']!=job['revision'] or not active or active[0]!=job['revision'] or old['status'] in ('DONE','SUPERSEDED'):
            raise StaleResult('failed_canonical_attempt_no_longer_current')
        # Failed physical attempts remain distinct from untouched PENDING.
        # Both authorities are updated in the SAME transaction.
        c.execute('UPDATE v2_jobs SET attempts=?,status=? WHERE execution_key=?',
            (job['attempts'],'FAILED_FINAL' if status=='TERMINAL_FAILED' else 'FAILED_RETRYABLE',key))
        self.store.candidate_status(c,job['stage'],job['payload'].get('candidate_id'),'FAILED_FINAL' if status=='TERMINAL_FAILED' else 'FAILED_RETRYABLE')
        self.store.emit(c,'VIDEO_ATTEMPT_FAILED',job['source_id'],job['revision'],1,
            {'execution_key':key,'job_id':job['job_id'],'attempts':job['attempts'],
             'status':status,'error_type':type(error).__name__,
             'error':str(error).split('{',1)[0],
             'attempt_audit_ref':{'job_id':job['job_id'],'attempt_number':job['attempts']}})

    def prepare_mirror_result(self,job,result):
        """Immutable, pre-encoded evidence values; no writer is held here."""
        parts=result.items() if job['stage']=='frame_basics' else [(job['stage'],result)]
        return tuple((stage,json.dumps(value,sort_keys=True),fingerprint(value))
                     for stage,value in parts)

    def mirror_result(self,c,job,result):
        # Compatibility entry for explicit legacy callers. Native commit uses
        # prepare/apply so model-value serialization precedes BEGIN IMMEDIATE.
        return self.apply_prepared_mirror_result(c,job,self.prepare_mirror_result(job,result))

    def apply_prepared_mirror_result(self,c,job,prepared):
        payload={k:v for k,v in job['payload'].items() if not k.startswith('_')}
        for stage,value_json,digest in prepared:
            contract=self.contracts[stage]
            item=payload.get('frame_id') if stage in ('dhash','geometry','quality','yoloe','openclip','insightface') else payload.get('candidate_id') if stage in ('qwen','ocr') else payload.get('document_id') if stage=='embedding' else payload.get('segment_id') if stage=='whisper' else job['source_id']+':'+job['revision']
            key=job['payload'].get('_evidence_key')
            if key:
                old=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
                if not old or old['stage']!=stage or old['input_sha']!=job['input_sha'] or json.loads(old['provenance'])!=contract:
                    raise ContractError('mirror_contract_mismatch')
            else:
                key=self.store.enqueue(c,stage,item,contract['revision'],job['source_id'],job['revision'],job['input_sha'],contract,payload)
                old=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
            active=c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(job['source_id'],)).fetchone()
            if old is None or old['status']=='SUPERSEDED' or old['source_id']!=job['source_id'] or old['source_revision']!=job['revision'] or not active or active[0]!=job['revision']:
                raise StaleResult('canonical_evidence_no_longer_current')
            if old['status']=='DONE':
                if old['result_sha']!=digest:raise ContractError('existing_success_would_change')
            else:
                c.execute("UPDATE v2_jobs SET status='DONE',attempts=?,result_json=?,result_sha=? WHERE execution_key=? AND status NOT IN ('DONE','SUPERSEDED')",
                    (job['attempts'],value_json,digest,key))
                self.store.candidate_status(c,stage,item,'DONE')
                self.store.emit(c,'EVIDENCE_READY',job['source_id'],job['revision'],contract['revision'],
                    {'stage':stage,'item_id':item,'execution_key':key,'payload_sha':digest,'result':json.loads(value_json)})
            c.execute('INSERT OR IGNORE INTO video_stream_mirrors_v1 VALUES(?,?,?)',(job['job_id'],stage,key))

    def document_events(self,limit=16):
        def route(c,event):
            # SOURCE_READY must NOT schedule another original-file audio read.
            p=event['payload']
            if event['event_type']=='EVIDENCE_READY' and p['stage'] in ('ocr','whisper') and isinstance(p['result'].get('text'),str) and not p['result']['text'].strip():
                job=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(p['execution_key'],)).fetchone()
                if not job or job['status']!='DONE' or job['result_sha']!=p['payload_sha']:return
                payload=json.loads(job['payload']);did=fingerprint((p['stage'],job['execution_key'],job['result_sha']))
                provenance={'execution_key':job['execution_key'],'input_sha':job['input_sha'],
                    'contract':json.loads(job['provenance']),'result_sha':job['result_sha'],'empty_text_contract':'video-no-text-v1'}
                c.execute("INSERT OR IGNORE INTO v2_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,'SKIP_NO_TEXT')",(did,event['source_id'],event['source_revision'],payload.get('artifact_id'),payload.get('start_ms'),payload.get('end_ms'),p['stage'],job['execution_key'],'',json.dumps(provenance,sort_keys=True),job['revision']))
                self.pipeline.index.covered(c,did,'no-text-skip',job['result_sha'],'video-no-text-v1')
                self.store.emit(c,'DOCUMENT_SKIPPED_NO_TEXT',event['source_id'],event['source_revision'],job['revision'],{'document_id':did,'result_sha':job['result_sha']})
                return
            if event['event_type'] in ('CANDIDATE_READY','AUDIO_ARTIFACT_READY','EVIDENCE_READY'):
                self.pipeline.documents.callback(c,event)
        return self.store.deliver('video-stream-documents-v1',route,limit=limit)

    def document_events_fair(self, limit=16, priority_only=False):
        """Deliver a bounded, priority-aware coordination quantum.

        Candidate publication is a readiness edge for model work.  It must not
        wait behind an older, unbounded document/evidence backlog.  Keep the
        normal inbox/lease/consume path intact, but select a small candidate
        slice first and then a small FIFO slice for the remaining event types.
        """
        consumer = 'video-stream-documents-v1'
        def route(c,event):
            return self._document_route(c,event)
        return self.store.deliver_fair(consumer, route, priority_types=('CANDIDATE_READY',), limit=limit, priority_only=priority_only)

    def _document_route(self, c, event):
        p=event['payload']
        if event['event_type']=='EVIDENCE_READY' and p['stage'] in ('ocr','whisper') and isinstance(p['result'].get('text'),str) and not p['result']['text'].strip():
            job=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(p['execution_key'],)).fetchone()
            if not job or job['status']!='DONE' or job['result_sha']!=p['payload_sha']:return
            payload=json.loads(job['payload']);did=fingerprint((p['stage'],job['execution_key'],job['result_sha']))
            provenance={'execution_key':job['execution_key'],'input_sha':job['input_sha'],
                'contract':json.loads(job['provenance']),'result_sha':job['result_sha'],'empty_text_contract':'video-no-text-v1'}
            c.execute("INSERT OR IGNORE INTO v2_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,'SKIP_NO_TEXT')",(did,event['source_id'],event['source_revision'],payload.get('artifact_id'),payload.get('start_ms'),payload.get('end_ms'),p['stage'],job['execution_key'],'',json.dumps(provenance,sort_keys=True),job['revision']))
            self.pipeline.index.covered(c,did,'no-text-skip',job['result_sha'],'video-no-text-v1')
            self.store.emit(c,'DOCUMENT_SKIPPED_NO_TEXT',event['source_id'],event['source_revision'],job['revision'],{'document_id':did,'result_sha':job['result_sha']})
            return
        if event['event_type'] in ('CANDIDATE_READY','AUDIO_ARTIFACT_READY','EVIDENCE_READY'):
            self.pipeline.documents.callback(c,event)

    def promote_policy_jobs(self,source=None,revision=None,stages=('vad','whisper','embedding'),limit=128):
        # A queued/RUNNING native job is already published even while its
        # canonical mirror remains PENDING. Do not spend the finite quantum
        # repeatedly publishing those same oldest jobs.
        where=' AND j.source_id=? AND j.source_revision=?' if source is not None else ''
        args=(*stages,*((source,revision) if source is not None else ()),limit)
        with self.store.connect() as c:
            jobs=[dict(r) for r in c.execute("SELECT j.* FROM v2_jobs j INDEXED BY p0_pending_promotion JOIN v2_active_sources a ON a.source_id=j.source_id AND a.source_revision=j.source_revision WHERE j.status IN ('PENDING','FAILED_RETRYABLE') AND j.stage IN ("+','.join('?' for _ in stages)+")"+where+" AND (j.stage<>'audio_extract' OR json_extract(j.payload,'$.status') IN ('READY','NO_AUDIO')) AND NOT EXISTS(SELECT 1 FROM video_stream_jobs_v1 n WHERE CAST(json_extract(n.payload,'$._evidence_key') AS TEXT)=j.execution_key) ORDER BY j.rowid LIMIT ?",args)]
        for job in jobs:
            if os.environ.get('VIDEO_CLOSURE_ONLY')=='1' and job['stage']=='embedding':
                continue
            payload=json.loads(job['payload'])
            payload['_evidence_key']=job['execution_key']
            if payload.get('path') and payload.get('sha'):payload['_file_sha']=payload['sha']
            self.ledger.publish(job['source_id'],job['source_revision'],job['stage'],job['input_sha'],json.loads(job['provenance']),payload,max_attempts=job['max_attempts'])

    def selection_prepare(self,source,revision):
        with self.store.connect() as c:
            published=c.execute("SELECT payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='FIRST_SELECTION_PUBLISHED'",(source,revision)).fetchone()
            if published:
                snapshot=json.loads(published[0])['snapshot']
                row=c.execute("SELECT result_json FROM v2_selection_snapshots WHERE snapshot_id=? AND source_id=? AND source_revision=? AND status='DONE'",(snapshot,source,revision)).fetchone()
                if not row:raise ContractError('selection_publication_snapshot_missing')
                result=json.loads(row[0])
        if published:
            from .video_selection_proof import ensure
            ensure(self,snapshot,source,revision)
            return snapshot,result
        with self.store.connect() as c:
            committed=c.execute("SELECT snapshot_id,result_json FROM v2_selection_snapshots WHERE source_id=? AND source_revision=? AND status='DONE' ORDER BY rowid DESC LIMIT 1",(source,revision)).fetchone()
            if committed:committed=(committed[0],committed[1])  # hydrate while the readonly connection is open
        if committed:
            from .video_selection_proof import ensure
            try:proof_key=ensure(self,committed[0],source,revision)
            except ContractError as error:
                if str(error) not in ('selection_proof_evidence_not_current','selection_proof_snapshot_evidence_changed'):raise
            else:
                self.publish_first_selection(source,revision,committed[0],proof_key)
                return committed[0],json.loads(committed[1])
        # Only local frozen primitives: never advance_source/advance_events.
        from .video_detached_coordination import pipeline_operation
        pipeline_operation(self,source,revision,'canonical_preparation',lambda p:p.canonicalize(source,revision))
        required={stage:self.contracts[stage]['revision'] for stage in self.pipeline.selection.REQUIRED}
        self.state(source,revision,'PRESELECTION_READY')
        snapshot,result=pipeline_operation(self,source,revision,'selection',lambda p:p.selection.select(source,revision,required))
        from .video_selection_proof import ensure
        proof_key=ensure(self,snapshot,source,revision)
        self.publish_first_selection(source,revision,snapshot,proof_key)
        return snapshot,result

    def publish_first_selection(self,source,revision,snapshot,proof_key):
        from .video_commit_queue import SQLCommand,WriteProposal,StaleProposal
        from .video_coordination import current_evidence_check
        with self.store.connect() as c:
            digest=c.execute('SELECT result_sha FROM v2_jobs WHERE execution_key=?',(proof_key,)).fetchone()[0]
        checks=((SQLCommand('SELECT owner,epoch FROM video_stream_control_v1 WHERE singleton=1'),(self.ledger.owner,self.ledger.epoch)),
                (SQLCommand('SELECT revision FROM video_stream_sources_v1 WHERE source_id=?',(source,)),(revision,)),
                (SQLCommand('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(source,)),(revision,)),
                (SQLCommand('SELECT source_id,source_revision,status FROM v2_selection_snapshots WHERE snapshot_id=?',(snapshot,)),(source,revision,'DONE')),
                (current_evidence_check(proof_key),('DONE',digest,source,revision)))
        command=SQLCommand("INSERT OR IGNORE INTO video_stream_source_states_v1 VALUES(?,?,'FIRST_SELECTION_PUBLISHED',?)",(source,revision,json.dumps({'snapshot':snapshot,'proof_key':proof_key},sort_keys=True)))
        commands=(command,)
        commands+=(entitlement_command(source,revision,snapshot),)
        try:self.ledger.commit_queue.submit(WriteProposal('first_selection_publication',source,commands,checks)).result()
        except StaleProposal as error:raise ContractError('coordination_snapshot_changed_retry') from error

    def selection(self,source,revision):
        return self.selection_prepare(source,revision)



    def closure(self,source,revision):
        if not self.has_state(source,revision,'LOCAL_PEOPLE_DONE'):return
        with self.store.connect() as c:
            row=c.execute("SELECT payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='SELECTION_DONE'",(source,revision)).fetchone()
            failure=c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND status='TERMINAL_FAILED'",(source,revision)).fetchone()
        if failure:
            return
        if not row:return
        for stage,complete in (('qwen','QWEN_COMPLETE'),('ocr','OCR_COMPLETE')):
            if self.ledger.branch_state(source,revision,stage)!=complete:return
        snapshot=json.loads(row[0])['snapshot']
        from .video_detached_coordination import pipeline_operation
        # A proven current head is already the exact no-op branch of advance.
        # Read it before constructing a full detached source snapshot. Subsequent
        # closure still uses its original transactional lineage/peer/epoch fences.
        import time
        checked=time.perf_counter()
        projection=getattr(self.pipeline,'final_projection',None)
        current=getattr(projection,'current_head',None)
        matched=bool(current and current(source,revision,snapshot))
        try:
            with Path(self.ledger.path).with_name('projection-current-head-checks.jsonl').open('a') as f:
                f.write(json.dumps({'at':time.time(),'source_id':source,'revision':revision,'snapshot':snapshot,'matched':matched,'elapsed_ms':(time.perf_counter()-checked)*1000})+'\n')
        except OSError:pass
        if not matched and not pipeline_operation(self,source,revision,'final_projection',lambda p:p.final_projection.advance(source,revision,snapshot)):return
        # Index preparation runs independently. Closure cannot repair the
        # entire source as an accidental substitute for event consumption.
        with self.store.connect() as c:
            missing=c.execute("SELECT 1 FROM v2_documents d WHERE d.source_id=? AND d.source_revision=? AND d.status='EMBEDDED' AND (NOT EXISTS(SELECT 1 FROM v2_index_coverage x WHERE x.entity_id=d.document_id AND x.index_kind='fts' AND x.status='READY') OR NOT EXISTS(SELECT 1 FROM v2_index_coverage x WHERE x.entity_id=d.document_id AND x.index_kind='vector' AND x.status='READY')) LIMIT 1",(source,revision)).fetchone()
        if missing:return
        required={stage:self.contracts[stage]['revision'] for stage in self.pipeline.selection.REQUIRED}
        pipeline_operation(self,source,revision,'visual_closure',lambda p:p.index.close_source(source,revision,p.selection,required,self.contracts['insightface'],require_final_projection=True,visual_only=True,allow_empty_text=True))
        self.state(source,revision,'VISUAL_COMPLETE')
        with self.store.connect() as c:
            if c.execute("SELECT 1 FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage IN ('audio_extract','vad','whisper','embedding') AND status IN ('PENDING','RUNNING','RETRY_PENDING') LIMIT 1",(source,revision)).fetchone():return
        pipeline_operation(self,source,revision,'source_closure',lambda p:p.index.close_source(source,revision,p.selection,required,self.contracts['insightface'],require_final_projection=True,allow_empty_text=True))
        self.state(source,revision,'AUDIO_COMPLETE')
        self.state(source,revision,'SOURCE_COMPLETE')


# This receipt is written in the SAME durable publication transaction. It
# describes a candidate handoff, never a new selection or processing policy.
def entitlement_command(source,revision,snapshot):
    from .video_commit_queue import SQLCommand
    return SQLCommand("""INSERT OR IGNORE INTO video_model_delivery_v1
        (event_id,source_id,revision,candidate_id,snapshot_id,entitled_at)
        SELECT event_id,?,?,json_extract(payload,'$.payload.candidate_id'),?,
            (julianday('now')-2440587.5)*86400.0
        FROM v2_outbox WHERE aggregate_id=? AND event_type='CANDIDATE_READY'
          AND json_extract(payload,'$.payload.snapshot_id')=?""",
        (source,revision,snapshot,source+':'+revision,snapshot))


class ModelDeliveryLane:
    """One bounded, independently serviced durable candidate handoff.

    Existing canonical inbox enqueue, native job identity, fences and model
    consumers remain authoritative. The source branch seal remains closure
    bookkeeping; a complete candidate seal grants its own inference readiness.
    """
    consumer='video-stream-documents-v1'
    def __init__(self,runtime):
        self.runtime=runtime;self.ledger=runtime.ledger
        self.path=runtime.root/'model-delivery-events.jsonl'
    def trace(self,phase,**fields):
        import time
        with self.path.open('a') as f:f.write(json.dumps(dict(timestamp=time.time(),phase=phase,**fields),sort_keys=True)+'\n')
    def recover_receipts(self,limit):
        with self.ledger.connection() as c:
            old=list(c.execute("""SELECT s.source_id,s.revision,s.payload FROM video_stream_source_states_v1 s
                JOIN video_stream_sources_v1 a ON a.source_id=s.source_id AND a.revision=s.revision
                WHERE s.state='SELECTION_DONE'
                AND NOT EXISTS(SELECT 1 FROM video_stream_source_states_v1 p WHERE p.source_id=s.source_id AND p.revision=s.revision AND p.state IN ('FIRST_SELECTION_PUBLISHED','SOURCE_COMPLETE','SOURCE_FAILED_FINAL'))
                AND EXISTS(SELECT 1 FROM v2_jobs j WHERE j.source_id=s.source_id AND j.source_revision=s.revision AND j.stage IN ('qwen','ocr') AND j.status IN ('PENDING','FAILED_RETRYABLE')) ORDER BY s.rowid LIMIT ?""",(limit,)))
        for source,revision,raw in old:
            snapshot,_=self.runtime.for_source(source).selection_prepare(source,revision)
            if snapshot!=json.loads(raw)['snapshot']:raise ContractError('delivery_receipt_recovery_snapshot_changed')
        with self.ledger.connection() as c:
            rows=list(c.execute("""WITH sealed_delivery_keys AS MATERIALIZED (SELECT json_extract(n.payload,'$._evidence_key') AS execution_key FROM video_model_delivery_v1 m CROSS JOIN video_stream_jobs_v1 n ON n.job_id=m.job_id WHERE m.sealed_at IS NOT NULL) SELECT s.source_id,s.revision,s.payload
                FROM video_stream_source_states_v1 s
                JOIN video_stream_sources_v1 a ON a.source_id=s.source_id AND a.revision=s.revision
                WHERE s.state='FIRST_SELECTION_PUBLISHED'
                AND NOT EXISTS(SELECT 1 FROM video_stream_source_states_v1 d WHERE d.source_id=s.source_id AND d.revision=s.revision AND d.state IN ('SOURCE_COMPLETE','SOURCE_FAILED_FINAL'))
                AND (NOT EXISTS(SELECT 1 FROM video_stream_source_states_v1 d WHERE d.source_id=s.source_id AND d.revision=s.revision AND d.state='SELECTION_DONE')
                  OR EXISTS(SELECT 1 FROM v2_jobs j WHERE j.source_id=s.source_id AND j.source_revision=s.revision AND j.stage IN ('qwen','ocr') AND j.status IN ('PENDING','FAILED_RETRYABLE') AND NOT EXISTS(SELECT 1 FROM sealed_delivery_keys k WHERE k.execution_key=j.execution_key))
                  OR EXISTS(SELECT 1 FROM video_stream_branches_v1 b WHERE b.source_id=s.source_id AND b.revision=s.revision AND b.expected=0 AND EXISTS(SELECT 1 FROM video_stream_jobs_v1 n WHERE n.source_id=b.source_id AND n.revision=b.revision AND n.stage=b.stage)))
                ORDER BY s.rowid LIMIT ?""",(limit,)))
        for source,revision,raw in rows:
            with self.ledger.connection() as c:
                c.execute('BEGIN IMMEDIATE');self.ledger.fence(c)
                if not c.execute('SELECT 1 FROM v2_active_sources WHERE source_id=? AND source_revision=?',(source,revision)).fetchone():continue
                command=entitlement_command(source,revision,json.loads(raw)['snapshot'])
                c.execute(command.sql,command.parameters)
        return rows
    def deliver_one(self,row):
        import time
        from .v2_model_workers import sha_file
        from .video_selection_proof import parent
        source,revision=row['source_id'],row['revision'];adapter=self.runtime.for_source(source)
        with self.ledger.connection() as c:
            raw=c.execute('SELECT payload FROM v2_outbox WHERE event_id=?',(row['event_id'],)).fetchone()
            if not raw:raise ContractError('model_delivery_event_missing')
            event=json.loads(raw[0])
        p=event['payload']
        if (source,revision,row['candidate_id'],row['snapshot_id'])!=(event['source_id'],event['source_revision'],p['candidate_id'],p['snapshot_id']):
            raise ContractError('model_delivery_identity_mismatch')
        self.trace('DELIVERY_START',source_id=source,revision=revision,candidate_id=p['candidate_id'],event_id=row['event_id'],entitled_at=row['entitled_at'])
        # Reuses the exact durable inbox and canonical callback, directly. No
        # document queue ordering, Search, Peer or coordination round is called.
        adapter.store.consume(event,self.consumer,adapter.pipeline.documents.callback)
        with self.ledger.connection() as c:
            canonical=c.execute("SELECT * FROM v2_current_jobs WHERE source_id=? AND source_revision=? AND stage=? AND item_id=?",(source,revision,p['purpose'],p['candidate_id'])).fetchone()
            if not canonical:raise ContractError('model_delivery_canonical_input_missing')
            canonical=dict(canonical)
            if not c.execute('SELECT 1 FROM v2_inbox WHERE consumer=? AND event_id=?',(self.consumer,row['event_id'])).fetchone():
                raise ContractError('model_delivery_durable_inbox_missing')
        payload=json.loads(canonical['payload']);payload['_evidence_key']=canonical['execution_key']
        image=payload.get('image_path')
        if not image or not payload.get('derived_sha') or sha_file(image)!=payload['derived_sha']:
            raise ContractError('model_delivery_native_input_invalid')
        key=self.ledger.publish(source,revision,canonical['stage'],canonical['input_sha'],json.loads(canonical['provenance']),payload,max_attempts=canonical['max_attempts'])
        body=dict(source_id=source,revision=revision,event_id=row['event_id'],candidate_id=p['candidate_id'],snapshot_id=p['snapshot_id'],
            stage=canonical['stage'],canonical_key=canonical['execution_key'],native_job_id=key,input_sha=canonical['input_sha'],
            contract=json.loads(canonical['provenance']),payload=payload)
        digest=fingerprint(body);sealed=time.time()
        with self.ledger.connection() as c:
            c.execute('BEGIN IMMEDIATE');self.ledger.fence(c)
            if not c.execute('SELECT 1 FROM video_stream_sources_v1 WHERE source_id=? AND revision=?',(source,revision)).fetchone():raise ContractError('model_delivery_revision_invalid')
            receipt=c.execute("SELECT payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='FIRST_SELECTION_PUBLISHED'",(source,revision)).fetchone()
            if not receipt or json.loads(receipt[0])['snapshot']!=p['snapshot_id']:raise ContractError('model_delivery_publication_receipt_invalid')
            parents=parent(adapter.store,c,event)
            current=c.execute('SELECT * FROM v2_current_jobs WHERE execution_key=?',(canonical['execution_key'],)).fetchone()
            if not current or current['input_sha']!=canonical['input_sha'] or current['payload']!=canonical['payload'] or current['provenance']!=canonical['provenance']:
                raise ContractError('model_delivery_canonical_changed')
            deps={x[0] for x in c.execute('SELECT parent_key FROM v2_job_dependencies WHERE child_key=?',(canonical['execution_key'],))}
            coverage=c.execute("WITH RECURSIVE needed(k) AS (SELECT parent_key FROM v2_job_dependencies WHERE child_key=? UNION SELECT d.parent_key FROM needed n JOIN v2_job_dependencies d ON d.child_key=n.k) SELECT count(DISTINCT e.execution_key) FROM needed n JOIN v2_snapshot_evidence e ON e.execution_key=n.k WHERE e.snapshot_id=?",(canonical['execution_key'],p['snapshot_id'])).fetchone()[0]
            required=c.execute('SELECT count(*) FROM v2_snapshot_evidence WHERE snapshot_id=?',(p['snapshot_id'],)).fetchone()[0]
            if not deps or coverage!=required:raise ContractError('model_delivery_required_dependency_missing')
            native=c.execute('SELECT * FROM video_stream_jobs_v1 WHERE job_id=?',(key,)).fetchone()
            if not native or json.loads(native['payload'])!=payload or native['input_sha']!=canonical['input_sha'] or json.loads(native['contract'])!=body['contract']:
                raise ContractError('model_delivery_manifest_incomplete')
            old=c.execute('SELECT manifest_sha FROM video_model_delivery_v1 WHERE event_id=?',(row['event_id'],)).fetchone()
            if not old:raise ContractError('model_delivery_entitlement_missing')
            if old[0] is not None and old[0]!=digest:raise ContractError('model_delivery_seal_changed')
            c.execute('UPDATE video_model_delivery_v1 SET job_id=?,sealed_at=coalesce(sealed_at,?),manifest=?,manifest_sha=? WHERE event_id=?',(key,sealed,json.dumps(body,sort_keys=True),digest,row['event_id']))
        self.trace('MANIFEST_SEALED',source_id=source,revision=revision,candidate_id=p['candidate_id'],event_id=row['event_id'],job_id=key,entitled_at=row['entitled_at'],sealed_at=sealed)
        return key
    def finalize_source(self,source,revision,raw):
        adapter=self.runtime.for_source(source);snapshot=json.loads(raw)['snapshot']
        with self.ledger.connection() as c:
            row=c.execute("SELECT result_json FROM v2_selection_snapshots WHERE snapshot_id=? AND source_id=? AND source_revision=? AND status='DONE'",(snapshot,source,revision)).fetchone()
            if not row:raise ContractError('model_delivery_snapshot_invalid')
            result=json.loads(row[0])
            pending=c.execute('SELECT 1 FROM video_model_delivery_v1 WHERE source_id=? AND revision=? AND snapshot_id=? AND sealed_at IS NULL LIMIT 1',(source,revision,snapshot)).fetchone()
            if pending:return
            counts={stage:c.execute('SELECT count(*) FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage=?',(source,revision,stage)).fetchone()[0] for stage in ('qwen','ocr')}
            if counts['qwen']!=len(result['q_rows']) or counts['ocr']!=len(result['o_rows']):return
        # A proven old zero transport counter can be reconciled only after
        # every item is independently complete. It never gates item inference.
        for stage,count in counts.items():
            with self.ledger.connection() as c:
                c.execute('BEGIN IMMEDIATE');self.ledger.fence(c)
                old=c.execute('SELECT expected FROM video_stream_branches_v1 WHERE source_id=? AND revision=? AND stage=?',(source,revision,stage)).fetchone()
                if old and old[0]==0 and count>0:
                    if c.execute("SELECT 1 FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state='SOURCE_COMPLETE'",(source,revision)).fetchone():raise ContractError('completed_manifest_repair_forbidden')
                    from .video_selection_proof import parent
                    parent(adapter.store,c,{'source_id':source,'source_revision':revision,'payload':{'snapshot_id':snapshot}})
                    expected={r[0] for r in c.execute('SELECT candidate_id FROM v2_selected_candidates WHERE snapshot_id=? AND purpose=?',(snapshot,stage))}
                    native={r[0] for r in c.execute("SELECT json_extract(payload,'$.candidate_id') FROM video_stream_jobs_v1 WHERE source_id=? AND revision=? AND stage=?",(source,revision,stage))}
                    if native!=expected or len(expected)!=count:raise ContractError('source_counter_reconciliation_identity_mismatch')
                    c.execute('UPDATE video_stream_branches_v1 SET expected=? WHERE source_id=? AND revision=? AND stage=?',(count,source,revision,stage))
                    invalid=('QWEN_FIRST_PASS_DONE','QWEN_COMPLETE') if stage=='qwen' else ('OCR_FIRST_PASS_DONE','OCR_COMPLETE')
                    for state in invalid:c.execute('DELETE FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state=?',(source,revision,state))
            self.ledger.seal_branch(source,revision,stage,count)
        adapter.state(source,revision,'SELECTION_DONE',{'snapshot':snapshot,'qwen':counts['qwen'],'ocr':counts['ocr']})
        self.runtime.reclaim_closed_reservations(source,revision)
    def step(self,limit=8):
        from .video_stream_ledger import StaleResult
        sources=self.recover_receipts(limit)
        with self.ledger.connection() as c:
            rows=[dict(r) for r in c.execute("""SELECT m.* FROM video_model_delivery_v1 m
                JOIN video_stream_sources_v1 s ON s.source_id=m.source_id AND s.revision=m.revision
                WHERE m.sealed_at IS NULL ORDER BY CASE WHEN EXISTS(
                    SELECT 1 FROM video_stream_operator_once_v1 o JOIN video_stream_jobs_v1 n ON n.job_id=o.job_id
                    WHERE o.claimed_token IS NULL AND o.source_id=m.source_id AND o.revision=m.revision
                    AND n.status='RETRY_PENDING' AND n.attempts+1=o.attempt_number
                    AND json_extract(n.payload,'$.candidate_id')=m.candidate_id) THEN 0 ELSE 1 END,
                    m.entitled_at,m.rowid LIMIT ?""",(limit,))]
        count=0
        for row in rows:
            with self.runtime.stop.new_work('model_delivery') as allowed:
                if not allowed:return count
                self.runtime.stop.work_started('model_delivery',source_id=row['source_id'],event_id=row['event_id'])
            # Stop acceptance lock is released before hashing or writer drain.
            try:self.deliver_one(row);count+=1
            except StaleResult:
                if self.runtime.stop.is_set():return count
                with self.ledger.connection() as c:
                    active=c.execute('SELECT revision FROM video_stream_sources_v1 WHERE source_id=?',(row['source_id'],)).fetchone()
                if active and active[0]!=row['revision']:
                    self.trace('RETIRED_REVISION',source_id=row['source_id'],revision=row['revision'],event_id=row['event_id'])
                    continue
                raise
            except ContractError as error:
                if str(error) not in ('coordination_snapshot_changed_retry','selection_proof_changed_retry'):raise
                self.trace('RETRY_CONCURRENT_CHANGE',source_id=row['source_id'],revision=row['revision'],event_id=row['event_id'],reason=str(error))
        for source,revision,raw in sources:
            if self.runtime.stop.is_set():break
            self.finalize_source(source,revision,raw)
        return count
    def run(self):
        from .video_stream_ledger import StaleResult
        try:
            while not self.runtime.stop.is_set():
                self.step()
                self.runtime.stop.wait(.05)
        except StaleResult as error:
            if not self.runtime.stop.is_set():
                self.runtime.errors.append(str(error))
                self.runtime.stop.request('PROCESSING_FATAL','MODEL_DELIVERY_FENCE_FAILURE',error=str(error))
        except Exception as error:
            self.runtime.errors.append(str(error))
            self.runtime.stop.request('PROCESSING_FATAL','MODEL_DELIVERY_LANE_FAILURE',error=str(error))


class NativeDeliveryService:
    """Independent audio-input or result-document handoff, original inbox/ACK.

    Each service owns its preparation context; neither shares mutable fair
    cursor state, an admission quantum or a Source Coordination round.
    """
    def __init__(self,runtime,partition):
        import copy
        self.runtime=runtime;self.partition=partition;self.store=copy.copy(runtime.adapters.store)
        self.store.proposal_context={};self.store.proposal_events=None
    def step(self):
        with self.runtime.stop.new_work('native_delivery_'+self.partition) as allowed:
            if not allowed:return 0
            self.runtime.stop.work_started('native_delivery_'+self.partition)
        count=self.store._deliver_batch('video-stream-documents-v1',self.runtime.adapters._document_route,16,partition=self.partition)
        stages=('vad','whisper') if self.partition=='audio' else ('embedding',)
        self.runtime.adapters.promote_policy_jobs(stages=stages,limit=128)
        return count
    def run(self):
        try:
            import time
            calls=completed=commit_wakes=fallback_wakes=0;query_seconds=0.;last_report=0.
            pulse=self.runtime.ledger.delivery_pulse
            while not self.runtime.stop.is_set():
                generation=pulse.version();started=time.monotonic()
                count=self.step();calls+=1;completed+=count
                elapsed=time.monotonic()-started;query_seconds+=elapsed
                if not count:
                    if pulse.wait(generation,self.runtime.stop):commit_wakes+=1
                    else:fallback_wakes+=1
                self.runtime.stop.wait(.02)
                now=time.time()
                if now-last_report>=1:
                    last_report=now
                    value=dict(at=now,partition=self.partition,calls=calls,completed_events=completed,
                        step_seconds=query_seconds,last_step_seconds=elapsed,commit_wakes=commit_wakes,
                        bounded_fallback_wakes=fallback_wakes,notification_errors=self.runtime.ledger.delivery_notification_errors)
                    target=self.runtime.root/('delivery-'+self.partition+'.json');temp=target.with_suffix('.tmp')
                    try:temp.write_text(json.dumps(value));temp.replace(target)
                    except OSError:pass  # Profiling cannot fail delivery.
        except StaleResult:
            if not self.runtime.stop.is_set():
                self.runtime.errors.append('NATIVE_DELIVERY_FENCE:'+self.partition);self.runtime.stop.request('PROCESSING_FATAL','NATIVE_DELIVERY_FENCE_FAILURE',partition=self.partition)
        except Exception as error:
            self.runtime.errors.append('NATIVE_DELIVERY:'+self.partition+':'+str(error));self.runtime.stop.request('PROCESSING_FATAL','NATIVE_DELIVERY_FAILURE',partition=self.partition,error=str(error))
