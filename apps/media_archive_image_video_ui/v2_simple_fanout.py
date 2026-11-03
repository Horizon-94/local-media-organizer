"""SIMPLE-only durable publication protocol; no inference/selection policy.

Event intake atomically commits an immutable manifest and inbox acknowledgement.
The acknowledgement means planned, not fully queued. Each later promotion and
its item marker commit in one owned transaction. No connection crosses threads.
"""
from collections import deque
from contextlib import contextmanager
import json
import threading
import time
from .v2_producer import fingerprint
from .v2_runtime import ContractError


class FanoutPlans:
    def __init__(self,store,budget,*,batch_limit=8,stop_requested=lambda:False):
        self.store=store;self.budget=budget;self.batch_limit=batch_limit
        if type(batch_limit) is not int or batch_limit<1:raise ContractError('simple_promotion_batch_invalid')
        self.stop_requested=stop_requested;self.local=threading.local();self.lock=threading.RLock()
        self.metrics={'creates':0,'replays':0,'promotions':0,'promoted_jobs':0,'sql_calls':0,
                      'create_seconds':deque(maxlen=2048),'promotion_seconds':deque(maxlen=2048)}
        with store.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS v2_simple_fanout_plans(
                    plan_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,input_sha TEXT NOT NULL,
                    source_id TEXT NOT NULL,source_revision TEXT NOT NULL,
                    scheduler_run_id TEXT,execution_epoch INTEGER,lane TEXT NOT NULL,branch TEXT NOT NULL,
                    manifest_sha TEXT NOT NULL,authority_json TEXT NOT NULL,total_count INTEGER NOT NULL,
                    promoted_count INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,last_served REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS v2_simple_fanout_items(
                    plan_id TEXT NOT NULL REFERENCES v2_simple_fanout_plans(plan_id),ordinal INTEGER NOT NULL,
                    execution_key TEXT NOT NULL,spec_json TEXT NOT NULL,promoted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(plan_id,ordinal),UNIQUE(plan_id,execution_key));
                CREATE INDEX IF NOT EXISTS v2_simple_fanout_dirty
                    ON v2_simple_fanout_plans(status,lane,scheduler_run_id,execution_epoch,last_served,plan_id);
                CREATE INDEX IF NOT EXISTS v2_simple_fanout_source
                    ON v2_simple_fanout_plans(source_id,source_revision,status,branch);
                CREATE INDEX IF NOT EXISTS v2_simple_fanout_remaining
                    ON v2_simple_fanout_items(plan_id,promoted,ordinal);
                CREATE TRIGGER IF NOT EXISTS v2_simple_fanout_immutable_plan
                    BEFORE UPDATE OF event_id,input_sha,source_id,source_revision,scheduler_run_id,
                    execution_epoch,lane,branch,manifest_sha,authority_json,total_count
                    ON v2_simple_fanout_plans BEGIN
                    SELECT RAISE(ABORT,'simple_fanout_manifest_immutable'); END;
                CREATE TRIGGER IF NOT EXISTS v2_simple_fanout_immutable_item
                    BEFORE UPDATE OF plan_id,ordinal,execution_key,spec_json ON v2_simple_fanout_items BEGIN
                    SELECT RAISE(ABORT,'simple_fanout_item_immutable'); END;
            ''')

    def sql(self,c,query,args=()):
        with self.lock:self.metrics['sql_calls']+=1
        return c.execute(query,args)

    def capture(self,c,key,spec):
        collector=getattr(self.local,'collector',None)
        if collector is None:return False
        if key in collector and collector[key]!=spec:raise ContractError('simple_manifest_identity_conflict')
        collector[key]=json.loads(json.dumps(spec,sort_keys=True))
        return True

    def is_capturing(self):return getattr(self.local,'collector',None) is not None

    def plan(self,c,event,callback,*,identity=None,authority=None):
        """Caller owns the event transaction. Replay never reruns callback."""
        started=time.monotonic();owner=self.store.scheduler_owner or {}
        source,revision=event['source_id'],event['source_revision']
        self.store.assert_source_epoch(c,source,revision)
        input_sha=fingerprint(identity if identity is not None else
            (event['event_id'],event['payload'],event.get('policy_or_contract_hash')))
        pid=fingerprint(('simple-fanout-v1',source,revision,input_sha,owner))
        old=self.sql(c,'SELECT input_sha FROM v2_simple_fanout_plans WHERE plan_id=?',(pid,)).fetchone()
        if old:
            if old[0]!=input_sha:raise ContractError('simple_plan_input_changed')
            with self.lock:self.metrics['replays']+=1
            return pid
        if self.is_capturing():raise ContractError('simple_nested_fanout_capture')
        collector={};self.local.collector=collector
        try:callback(c,event)
        finally:self.local.collector=None
        # One normal document event publishes one stage. Keeping this contract
        # explicit prevents silently grouping unrelated resource lanes.
        lanes={self.budget.lane(spec['stage']) for spec in collector.values()}
        if len(lanes)>1:raise ContractError('simple_event_multiple_resource_lanes')
        lane=next(iter(lanes),'none')
        branches={'audio' if s['stage'] in ('audio_extract','vad','whisper') or
                  s['payload'].get('document_type')=='whisper' else 'visual' for s in collector.values()}
        if len(branches)>1:raise ContractError('simple_event_multiple_completion_branches')
        branch=next(iter(branches),'none')
        pins={}
        for spec in collector.values():
            for key in spec['dependencies']:
                row=self.sql(c,'SELECT result_sha,status FROM v2_current_jobs WHERE execution_key=?',(key,)).fetchone()
                if not row or row[1]!='DONE':raise ContractError('simple_plan_parent_not_current')
                pins[key]=row[0]
        auth={'parents':pins,**(authority or {})}
        manifest=[(key,spec) for key,spec in collector.items()]
        self.sql(c,'''INSERT INTO v2_simple_fanout_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (pid,event['event_id'],input_sha,source,revision,owner.get('scheduler_run_id'),owner.get('execution_epoch'),
             lane,branch,fingerprint(manifest),json.dumps(auth,sort_keys=True),len(manifest),0,
             'FANOUT_PLANNED' if manifest else 'FANOUT_FULLY_PROMOTED',0))
        for ordinal,(key,spec) in enumerate(manifest):
            self.sql(c,'INSERT INTO v2_simple_fanout_items VALUES(?,?,?,?,0)',(pid,ordinal,key,json.dumps(spec,sort_keys=True)))
        with self.lock:
            self.metrics['creates']+=1;self.metrics['create_seconds'].append(time.monotonic()-started)
        return pid

    def _current(self,c,plan):
        owner=self.store.scheduler_owner or {}
        if (plan['scheduler_run_id'],plan['execution_epoch'])!=(owner.get('scheduler_run_id'),owner.get('execution_epoch')):return False
        active=self.sql(c,'SELECT source_revision FROM v2_active_sources WHERE source_id=?',(plan['source_id'],)).fetchone()
        if not active or active[0]!=plan['source_revision']:return False
        auth=json.loads(plan['authority_json']);pins=auth['parents']
        if pins:
            rows=self.sql(c,"SELECT execution_key,result_sha FROM v2_current_jobs WHERE status='DONE' AND execution_key IN (SELECT value FROM json_each(?))",(json.dumps(list(pins)),)).fetchall()
            if dict(rows)!=pins:return False
        for cid,snapshot,sha in auth.get('candidates',[]):
            row=self.sql(c,"SELECT snapshot_id,payload FROM v2_selected_candidates WHERE candidate_id=? AND status<>'SUPERSEDED'",(cid,)).fetchone()
            if not row or row[0]!=snapshot or fingerprint(json.loads(row[1]))!=sha:return False
        return True

    def promote(self,*,lanes=None):
        """One bounded quantum per lane. Busy propagates for outer safe retry.

        QueueFull ends only this plan's quantum; previous savepoints commit.
        Completion markers share the same transaction as their job insertion.
        """
        from .v2_simple_pipeline import QueueFull
        if self.stop_requested() or self.budget.cancel.is_set():return 0
        count=0
        for lane in lanes or ('audio','qwen','ocr','embedding'):
            if self.stop_requested() or self.budget.cancel.is_set():break
            started=time.monotonic();owner=self.store.scheduler_owner or {}
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                if self.stop_requested():continue
                plan=self.sql(c,'''SELECT * FROM v2_simple_fanout_plans WHERE status='FANOUT_PLANNED'
                    AND lane=? AND scheduler_run_id IS ? AND execution_epoch IS ?
                    ORDER BY last_served,plan_id LIMIT 1''',(lane,owner.get('scheduler_run_id'),owner.get('execution_epoch'))).fetchone()
                if not plan:continue
                if not self._current(c,plan):
                    self.sql(c,"UPDATE v2_simple_fanout_plans SET status='SUPERSEDED' WHERE plan_id=?",(plan['plan_id'],))
                    continue
                self.store.assert_source_epoch(c,plan['source_id'],plan['source_revision'])
                rows=self.sql(c,'''SELECT * FROM v2_simple_fanout_items WHERE plan_id=? AND promoted=0
                    ORDER BY ordinal LIMIT ?''',(plan['plan_id'],self.batch_limit)).fetchall()
                promoted=0
                for item in rows:
                    if self.stop_requested():break
                    c.execute('SAVEPOINT simple_promote_item')
                    try:
                        spec=json.loads(item['spec_json']);key=self.store.enqueue(c,**spec)
                        if key!=item['execution_key']:raise ContractError('simple_promotion_identity_changed')
                        if spec['stage'] in ('qwen','ocr'):
                            self.sql(c,"UPDATE v2_selected_candidates SET status='QUEUED' WHERE candidate_id=? AND status='READY'",(spec['item_id'],))
                        self.sql(c,'UPDATE v2_simple_fanout_items SET promoted=1 WHERE plan_id=? AND ordinal=?',
                                 (plan['plan_id'],item['ordinal']))
                        c.execute('RELEASE simple_promote_item');promoted+=1
                    except QueueFull:
                        c.execute('ROLLBACK TO simple_promote_item');c.execute('RELEASE simple_promote_item');break
                total=plan['promoted_count']+promoted
                self.sql(c,'UPDATE v2_simple_fanout_plans SET promoted_count=?,status=?,last_served=? WHERE plan_id=?',
                    (total,'FANOUT_FULLY_PROMOTED' if total==plan['total_count'] else 'FANOUT_PLANNED',time.time(),plan['plan_id']))
            count+=promoted
            with self.lock:
                self.metrics['promotions']+=1;self.metrics['promoted_jobs']+=promoted
                self.metrics['promotion_seconds'].append(time.monotonic()-started)
        return count

    def pending(self,c,source,revision,*,visual_only=False):
        return bool(self.sql(c,'''SELECT 1 FROM v2_simple_fanout_plans
            WHERE source_id=? AND source_revision=? AND status='FANOUT_PLANNED'
            AND (?=0 OR branch='visual') LIMIT 1''',(source,revision,int(visual_only))).fetchone())

    def documents(self,c,event,chain):
        """Capture original document callbacks without changing their payloads.

        Candidate snapshots already atomically freeze the whole source batch;
        first candidate intake plans every same-purpose member of that snapshot.
        Subsequent candidate events reuse this manifest.
        """
        p=event['payload']
        if event['event_type']=='EVIDENCE_READY' and p['stage'] not in ('audio_extract','vad','qwen','ocr','whisper'):
            return chain.callback(c,event)
        if event['event_type']=='EVIDENCE_READY':
            current=self.sql(c,"SELECT result_sha FROM v2_current_jobs WHERE execution_key=? AND status='DONE'",(p['execution_key'],)).fetchone()
            if not current or current[0]!=p['payload_sha']:return
        if event['event_type']=='CANDIDATE_READY':
            rows=self.sql(c,"SELECT candidate_id,payload FROM v2_selected_candidates WHERE snapshot_id=? AND purpose=? AND status<>'SUPERSEDED' ORDER BY candidate_id",(p['snapshot_id'],p['purpose'])).fetchall()
            if not rows:return
            candidates=[(r[0],p['snapshot_id'],fingerprint(json.loads(r[1]))) for r in rows]
            identity=('candidate-batch',p['snapshot_id'],p['purpose'],candidates,chain.contracts[p['purpose']])
            def batch(connection,_):
                for row in rows:chain.callback(connection,{**event,'payload':json.loads(row[1])})
            return self.plan(c,event,batch,identity=identity,authority={'candidates':candidates})
        return self.plan(c,event,chain.callback)
