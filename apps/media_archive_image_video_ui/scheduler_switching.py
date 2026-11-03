"""Explicit, fenced scheduler switching for the 1.3.0 product contract."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .central_database import connect
from .scheduler_identity import (
    ACTIVE_RUN_STATUSES,
    EVENT_DRIVEN_V2,
    LEGACY_BATCH,
    PIPELINE_CONTRACT_VERSION,
    SchedulerIdentityError,
    SchedulerOwnershipError,
    ensure_scheduler_identity_schema,
    start_scheduler_run,
)
from .v2_producer import fingerprint
from .v2_runtime import WorkerStore


SWITCH_STATES = frozenset({"PREPARING", "FENCED", "TARGET_CREATED", "COMPLETE", "FAILED"})
REUSE_FIELDS = (
    "input_fingerprint",
    "source_revision",
    "model_fingerprint",
    "config_fingerprint",
    "preprocess_fingerprint",
    "prompt_fingerprint",
    "code_policy_fingerprint",
    "sampling_fingerprint",
    "selection_fingerprint",
)


def _now() -> float:
    return time.time()


def _active_transition(con, task_id: str):
    return con.execute(
        "SELECT * FROM scheduler_switches WHERE task_id=? AND state IN ('PREPARING','FENCED','TARGET_CREATED') "
        "ORDER BY updated_at DESC LIMIT 1", (task_id,),
    ).fetchone()


def _latest_run(con, task_id: str):
    return con.execute(
        "SELECT * FROM scheduler_runs WHERE task_id=? ORDER BY execution_epoch DESC LIMIT 1", (task_id,)
    ).fetchone()


def _transition_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _fence_source(con, transition) -> None:
    source_id = transition["source_run_id"]
    if source_id:
        # Retire scheduler state in the same write transaction as the owner.
        # Historical semantic rows survive, but never stay current implicitly.
        if transition['source_mode'] == EVENT_DRIVEN_V2:
            from .v2_lineage import invalidate
            tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'v2_jobs' in tables:
                columns={r[1] for r in con.execute('PRAGMA table_info(v2_jobs)')}
                keys=[r[0] for r in con.execute(
                    "SELECT execution_key FROM v2_jobs WHERE scheduler_run_id=? OR scheduler_run_id IS NULL",(source_id,))] if 'scheduler_run_id' in columns else [r[0] for r in con.execute('SELECT execution_key FROM v2_jobs')]
                invalidate(con,keys)
            sources=list(con.execute('SELECT source_id,source_revision FROM v2_source_epochs WHERE scheduler_run_id=?',(source_id,))) if 'v2_source_epochs' in tables else list(con.execute('SELECT source_id,revision FROM v2_sources'))
            for source,revision in sources:
                con.execute("UPDATE v2_active_sources SET source_revision=?,generation=generation+1 WHERE source_id=? AND source_revision=?",('fenced:'+source_id+':'+revision,source,revision))
                con.execute('DELETE FROM v2_source_leases WHERE source_id=? AND source_revision=?',(source,revision))
                con.execute("UPDATE v2_source_closure SET status='STALE' WHERE source_id=? AND source_revision=?",(source,revision))
        con.execute(
            "UPDATE scheduler_runs SET status='ABORTED',finished_at=COALESCE(finished_at,?) "
            "WHERE scheduler_run_id=? AND status IN ('STARTING','RUNNING','RECOVERABLE')",
            (_now(), source_id),
        )


def _advance_switch(database: Path, switch_id: str) -> dict[str, Any]:
    with connect(database) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM scheduler_switches WHERE switch_id=?", (switch_id,)).fetchone()
        if row is None:
            raise SchedulerOwnershipError("scheduler_switch_not_found")
        if row["state"] == "PREPARING":
            _fence_source(con, row)
            con.execute("UPDATE scheduler_switches SET state='FENCED',updated_at=? WHERE switch_id=?", (_now(), switch_id))
        return _transition_dict(con.execute("SELECT * FROM scheduler_switches WHERE switch_id=?", (switch_id,)).fetchone())


def _create_target(database: Path, switch_id: str) -> dict[str, Any]:
    with connect(database) as con:
        row = con.execute("SELECT * FROM scheduler_switches WHERE switch_id=?", (switch_id,)).fetchone()
        if row is None:
            raise SchedulerOwnershipError("scheduler_switch_not_found")
        if row["state"] == "TARGET_CREATED":
            return _transition_dict(row)
        if row["state"] != "FENCED":
            return _transition_dict(row)
        task_id = row["task_id"]
        target_mode = row["target_mode"]
    target = start_scheduler_run(
        database, task_id, scheduler_mode=target_mode,
        request_id=f"switch-target:{switch_id}", _switch_internal=True,
    )
    with connect(database) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE scheduler_switches SET target_run_id=?,target_epoch=?,state='TARGET_CREATED',updated_at=? WHERE switch_id=? AND state='FENCED'",
            (target["scheduler_run_id"], target["execution_epoch"], _now(), switch_id),
        )
        return _transition_dict(con.execute("SELECT * FROM scheduler_switches WHERE switch_id=?", (switch_id,)).fetchone())


def _activate(database: Path, switch_id: str) -> dict[str, Any]:
    with connect(database) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM scheduler_switches WHERE switch_id=?", (switch_id,)).fetchone()
        if row is None:
            raise SchedulerOwnershipError("scheduler_switch_not_found")
        if row["state"] == "TARGET_CREATED":
            con.execute("UPDATE scheduler_switches SET state='COMPLETE',updated_at=? WHERE switch_id=?", (_now(), switch_id))
        return _transition_dict(con.execute("SELECT * FROM scheduler_switches WHERE switch_id=?", (switch_id,)).fetchone())


def recover_scheduler_switch(database: Path, switch_id: str) -> dict[str, Any]:
    """Finish a durable transition after a process crash at a phase boundary."""
    ensure_scheduler_identity_schema(database)
    transition = _advance_switch(Path(database), switch_id)
    transition = _create_target(Path(database), switch_id)
    transition = _activate(Path(database), switch_id)
    from .scheduler_task_state import project_current_task
    project_current_task(Path(database), transition['task_id'])
    return transition


def switch_scheduler(
    database: Path,
    task_id: str,
    target_mode: str,
    *,
    request_id: str | None = None,
    fail_after: str | None = None,
) -> dict[str, Any]:
    """Switch through durable PREPARING→FENCED→TARGET_CREATED→COMPLETE."""
    database = Path(database)
    if target_mode not in {LEGACY_BATCH, EVENT_DRIVEN_V2}:
        raise SchedulerIdentityError(f"unsupported_scheduler_mode:{target_mode}")
    request_id = request_id or uuid.uuid4().hex
    ensure_scheduler_identity_schema(database)
    with connect(database) as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute("SELECT * FROM scheduler_switches WHERE request_id=?", (request_id,)).fetchone()
        if existing is not None:
            if existing["target_mode"] != target_mode or existing["task_id"] != task_id:
                raise SchedulerOwnershipError("switch_request_conflict")
            switch_id = existing["switch_id"]
        else:
            active_transition = _active_transition(con, task_id)
            if active_transition is not None:
                raise SchedulerOwnershipError("scheduler_switch_in_progress")
            source = _latest_run(con, task_id)
            if source is None:
                raise SchedulerOwnershipError("scheduler_run_not_found")
            if source["scheduler_mode"] == target_mode and source["status"] in ACTIVE_RUN_STATUSES:
                raise SchedulerOwnershipError("target_scheduler_already_active")
            switch_id = uuid.uuid4().hex
            now = _now()
            con.execute(
                "INSERT INTO scheduler_switches(switch_id,task_id,request_id,source_run_id,source_mode,source_epoch,target_mode,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (switch_id, task_id, request_id, source["scheduler_run_id"], source["scheduler_mode"], source["execution_epoch"], target_mode, "PREPARING", now, now),
            )
    if fail_after == "requested":
        raise RuntimeError("switch_test_interrupted:requested")
    transition = _advance_switch(database, switch_id)
    if fail_after == "fenced":
        raise RuntimeError("switch_test_interrupted:fenced")
    transition = _create_target(database, switch_id)
    if fail_after == "target_created":
        raise RuntimeError("switch_test_interrupted:target_created")
    return _activate(database, switch_id)


def switch_scheduler_for_task(task_path: Path, target_mode: str, *, request_id: str | None = None, **kwargs) -> dict[str, Any]:
    """Product API used by callers; task callers do not edit scheduler rows."""
    path = Path(task_path).expanduser().resolve(strict=True)
    task = json.loads(path.read_text(encoding="utf-8"))
    if str(target_mode) == EVENT_DRIVEN_V2 and task.get('runtime', {}).get('model_root'):
        from .v2_task_materializer import materialize_v2_task
        task = materialize_v2_task(path)
    from .scheduler_task_state import remember_task, reconcile_task_scheduler
    remember_task(Path(task['database']), path, task)
    transition = switch_scheduler(Path(task["database"]), str(task["task_id"]), target_mode, request_id=request_id, **kwargs)
    # A repeated historical request returns its original transition record,
    # but its task projection always describes the current DB owner.
    reconcile_task_scheduler(Path(task['database']), str(task['task_id']))
    return transition


def _contract_fingerprint(contract: Mapping[str, Any]) -> str | None:
    if any(not str(contract.get(key) or "") for key in REUSE_FIELDS):
        return None
    return fingerprint({key: str(contract[key]) for key in REUSE_FIELDS})


def evaluate_result_reuse(source_contract: Mapping[str, Any], target_contract: Mapping[str, Any]) -> dict[str, Any]:
    source_fp = _contract_fingerprint(source_contract)
    target_fp = _contract_fingerprint(target_contract)
    reusable = source_fp is not None and source_fp == target_fp
    return {"reusable": reusable, "source_fingerprint": source_fp or "", "target_fingerprint": target_fp or "", "reason": "compatible_contract" if reusable else "incompatible_contract"}


def record_result_reuse(
    database: Path, target_run_id: str, result_key: str, source_contract: Mapping[str, Any],
    target_contract: Mapping[str, Any], *, source_run_id: str | None = None,
) -> dict[str, Any]:
    """Record reuse only for a result proven by persisted V2 provenance.

    The caller's source dictionary is intentionally not evidence.  A source
    job, completed result digest, scheduler identity, lineage and reuse
    contract must all be resolved from the product database first.
    """
    with connect(Path(database)) as con:
        con.execute("BEGIN IMMEDIATE")
        target = con.execute("SELECT status FROM scheduler_runs WHERE scheduler_run_id=?", (target_run_id,)).fetchone()
        if target is None or target[0] not in ACTIVE_RUN_STATUSES:
            raise SchedulerOwnershipError("target_scheduler_run_not_active")
        row = con.execute(
            "SELECT * FROM v2_jobs WHERE execution_key=?", (result_key,)
        ).fetchone() if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_jobs'"
        ).fetchone() else None
        reason = ""
        persisted = None
        if row is None:
            reason = "persisted_result_missing"
        elif row["status"] not in {"DONE", "SUPERSEDED"}:
            reason = "persisted_result_not_complete"
        elif not row["result_json"] or not row["result_sha"]:
            reason = "persisted_result_payload_missing"
        elif not row["scheduler_run_id"] or row["execution_epoch"] is None:
            reason = "persisted_result_provenance_missing"
        else:
            try:
                payload = json.loads(row["result_json"])
                provenance = json.loads(row["provenance"] or "{}")
                contract = provenance.get("reuse_contract", provenance)
                if fingerprint(payload) != row["result_sha"]:
                    reason = "persisted_result_digest_mismatch"
                elif not isinstance(contract, dict) or any(not str(contract.get(key) or "") for key in REUSE_FIELDS):
                    reason = "persisted_result_contract_missing"
                elif not row["source_id"] or not row["source_revision"]:
                    reason = "persisted_result_lineage_missing"
                elif not con.execute(
                    "SELECT 1 FROM scheduler_runs WHERE scheduler_run_id=? AND execution_epoch=?",
                    (row["scheduler_run_id"], row["execution_epoch"]),
                ).fetchone():
                    reason = "persisted_source_scheduler_missing"
                elif source_run_id is not None and source_run_id != row["scheduler_run_id"]:
                    reason = "source_result_provenance_mismatch"
                else:
                    persisted = contract
            except (TypeError, ValueError, json.JSONDecodeError):
                reason = "persisted_result_provenance_invalid"

        if persisted is None:
            return {
                "reusable": False, "source_fingerprint": "", "target_fingerprint": "",
                "reason": reason or "persisted_result_provenance_missing", "status": "REJECTED",
                "target_run_id": target_run_id, "result_key": result_key,
            }

        # The target contract is evidence only when it belongs to a persisted
        # target execution in this epoch.  A caller-supplied dictionary cannot
        # establish compatibility by itself.
        target_job = con.execute(
            "SELECT * FROM v2_jobs WHERE scheduler_run_id=? AND stage=? AND item_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (target_run_id, row["stage"], row["item_id"]),
        ).fetchone()
        if target_job is None:
            return {
                "reusable": False, "source_fingerprint": "", "target_fingerprint": "",
                "reason": "persisted_target_provenance_missing", "status": "REJECTED",
                "target_run_id": target_run_id, "result_key": result_key,
            }
        try:
            target_provenance = json.loads(target_job["provenance"] or "{}")
            persisted_target = target_provenance.get("reuse_contract", target_provenance)
        except (TypeError, ValueError, json.JSONDecodeError):
            persisted_target = None
        if not isinstance(persisted_target, dict) or any(
            not str(persisted_target.get(key) or "") for key in REUSE_FIELDS
        ):
            return {
                "reusable": False, "source_fingerprint": "", "target_fingerprint": "",
                "reason": "persisted_target_provenance_missing", "status": "REJECTED",
                "target_run_id": target_run_id, "result_key": result_key,
            }
        decision = evaluate_result_reuse(persisted, persisted_target)
        if not decision["reusable"]:
            return {**decision, "status": "REJECTED", "target_run_id": target_run_id, "result_key": result_key}
        con.execute(
            "INSERT OR REPLACE INTO scheduler_run_coverage(target_run_id,result_key,coverage_kind,status,source_run_id,contract_fingerprint,created_at) VALUES(?,?,?,?,?,?,?)",
            (target_run_id, result_key, "semantic_result", "REUSED", row["scheduler_run_id"], decision["target_fingerprint"], _now()),
        )
        return {**decision, "status": "REUSED", "target_run_id": target_run_id, "result_key": result_key}


class FencedWorkerStore(WorkerStore):
    """V2 store adapter that checks the persisted owner before every mutation."""

    def __init__(self, db, *, task_id: str, scheduler_run_id: str, execution_epoch: int, policy=None):
        # Large sources repeatedly traverse the same multi-million-edge DAG
        # during index/readiness reads. Versioned proofs retain the original
        # dirty-transaction fallback; an explicit 0 remains the diagnostic opt-out.
        import os
        self.current_lineage_cache_enabled = os.environ.get('MEDIA_ARCHIVE_CURRENT_LINEAGE_CACHE_OPT_IN', '1') == '1'
        # Every formal product entry, including single-source retries, must
        # use the validated exact-text dependency scope, not the legacy
        # invalidate-every-source fallback.
        self.targeted_projection_invalidation_enabled = True
        self.scheduler_database = Path(db)
        self.scheduler_task_id = task_id
        self.scheduler_run_id = scheduler_run_id
        self.execution_epoch = int(execution_epoch)
        super().__init__(db, policy=policy, scheduler_owner={
            'task_id': task_id, 'scheduler_run_id': scheduler_run_id,
            'execution_epoch': int(execution_epoch),
            'scheduler_mode': EVENT_DRIVEN_V2,
            'pipeline_contract_version': PIPELINE_CONTRACT_VERSION,
        })
        import threading
        self._timing_local = threading.local()
        with self.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS v2_job_timing(
              execution_key TEXT, attempt INTEGER, scheduler_run_id TEXT,
              execution_epoch INTEGER, claim_requested_at REAL, claim_granted_at REAL,
              run_started_at REAL, run_finished_at REAL,
              PRIMARY KEY(execution_key,attempt,scheduler_run_id,execution_epoch))''')
            # Durable delivery used to rescan/sort the whole outbox even when
            # a consumer had no new events. Cover owner/order metadata only;
            # event payloads, inbox dedupe and callback transactions unchanged.
            c.execute('''CREATE INDEX IF NOT EXISTS v2_outbox_owner_order
              ON v2_outbox(scheduler_run_id,execution_epoch,committed_at,event_id)''')
            c.execute('''CREATE INDEX IF NOT EXISTS v2_jobs_lineage_header
              ON v2_jobs(execution_key,status,source_id,source_revision)''')
            c.execute('''CREATE INDEX IF NOT EXISTS v2_jobs_model_headers
              ON v2_jobs(stage,status,execution_key,item_id,source_id,source_revision,result_sha)''')
            # Summary/retry probes need no model payload. Cover their owner
            # and status fields so quiescent passes do not read OCR-sized rows.
            # Current-lineage predicates and every completion gate are unchanged.
            c.execute('''CREATE INDEX IF NOT EXISTS v2_job_product_summary_headers
              ON v2_jobs(scheduler_run_id,execution_epoch,status,error,source_id,execution_key)''')
            from .v2_delivery_empty_cache import install as install_delivery_proof
            install_delivery_proof(c)
            from .v2_current_jobs_cache import install_schema
            if self.current_lineage_cache_enabled:install_schema(c)

    def assert_current_owner(self, connection=None) -> None:
        query="SELECT task_id,scheduler_mode,execution_epoch,status FROM scheduler_runs WHERE scheduler_run_id=?"
        if connection is None:
            with connect(self.scheduler_database, readonly=True) as con:
                row = con.execute(query, (self.scheduler_run_id,)).fetchone()
        else:
            # Reuse the event's fenced transaction. A separate reader can
            # block on our own spilled rollback-journal write transaction.
            row = connection.execute(query, (self.scheduler_run_id,)).fetchone()
        if row is None or row["task_id"] != self.scheduler_task_id or int(row["execution_epoch"]) != self.execution_epoch or row["status"] not in ACTIVE_RUN_STATUSES:
            raise SchedulerOwnershipError("stale_scheduler_execution_epoch")

    def connect(self):
        connection=super().connect()
        try:
            self.assert_current_owner(connection)
        except BaseException:
            connection.close()
            raise
        # Claim/event/status-only connections do not need the multi-million
        # edge proof. EpochConnection initializes it on first actual lineage
        # query, retaining the equivalent fallback view until then.
        connection.lineage_cache_requested = self.current_lineage_cache_enabled
        if self.current_lineage_cache_enabled:
            from .v2_current_jobs_cache import install_stub
            if install_stub(connection):return connection
        if connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('v2_jobs','v2_job_dependencies','v2_current_jobs')").fetchone()[0]==3:
            # Connection-local equivalent of the frozen current-jobs view.
            # Correlating the ancestor seed to a candidate lets SQLite use
            # execution_key/stage indexes rather than materializing ALL roots
            # before each point lookup. UNION retains cycle termination.
            # TEMP metadata is connection-local, not a durable mutation.
            # The guarded execute would acquire BEGIN IMMEDIATE here and
            # turn every read connection into a writer until context exit.
            from .v2_current_jobs_cache import correlated_predicate
            sqlite3.Connection.execute(connection, '''CREATE TEMP VIEW v2_current_jobs AS
              SELECT j.* FROM main.v2_jobs j WHERE j.status<>'SUPERSEDED' AND '''+correlated_predicate(connection))
        return connection

    def emit(self, connection, *args, **kwargs):
        self.assert_current_owner(connection)
        return super().emit(connection, *args, **kwargs)

    def register_job(self, *args, **kwargs):
        self.assert_current_owner()
        return super().register_job(*args, **kwargs)

    def claim(self, *args, **kwargs):
        self.assert_current_owner()
        requested = time.time()
        job = super().claim(*args, **kwargs)
        granted = time.time()  # Claim transaction committed; not handler completion.
        self._timing_local.job = job
        if job:
            with self.connect() as c:
                c.execute('INSERT OR REPLACE INTO v2_job_timing VALUES(?,?,?,?,?,?,NULL,NULL)',
                          (job['execution_key'],job['attempts'],self.scheduler_run_id,
                           self.execution_epoch,requested,granted))
            job['claim_granted_at'] = granted
        return job

    def current_claim(self):
        return getattr(self._timing_local, 'job', None)

    def run_one(self, stage, handler, validator, *args, **kwargs):
        def timed(payload):
            job = self._timing_local.job
            key = (job['execution_key'],job['attempts'],self.scheduler_run_id,self.execution_epoch)
            with self.connect() as c:
                c.execute('UPDATE v2_job_timing SET run_started_at=? WHERE execution_key=? AND attempt=? AND scheduler_run_id=? AND execution_epoch=?', (time.time(),*key))
            try:
                return handler(payload)
            finally:
                with self.connect() as c:
                    c.execute('UPDATE v2_job_timing SET run_finished_at=? WHERE execution_key=? AND attempt=? AND scheduler_run_id=? AND execution_epoch=?', (time.time(),*key))
        return super().run_one(stage, timed, validator, *args, **kwargs)

    def run_batch(self,stage,handler,validator,**kwargs):
        from .v2_job_batch import run_batch
        def stamp(jobs,column):
            with self.connect() as c:
                c.executemany('UPDATE v2_job_timing SET '+column+'=? WHERE execution_key=? AND attempt=? AND scheduler_run_id=? AND execution_epoch=?',
                    [(time.time(),j['execution_key'],j['attempts'],self.scheduler_run_id,self.execution_epoch) for j in jobs])
        return run_batch(self,stage,handler,validator,on_start=lambda jobs:stamp(jobs,'run_started_at'),
                         on_finish=lambda jobs:stamp(jobs,'run_finished_at'),**kwargs)

    def peek_runnable_job(self, stage, *, execution_contract=None):
        from .v2_product_lifecycle import product_claim_predicate
        expected = json.dumps(execution_contract,sort_keys=True) if execution_contract is not None else None
        with self.connect() as c:
            eligible = product_claim_predicate(c,self.scheduler_owner)
            query = 'SELECT * FROM v2_current_jobs WHERE '+eligible+''' AND stage=?
              AND attempts<max_attempts AND (status IN ('PENDING','FAILED_RETRYABLE')
              OR (status='RUNNING' AND lease_until<=?)) AND (? IS NULL OR provenance=?)
              '''
            args=(stage,self.clock(),expected,expected)
            affinity=getattr(self,'source_affinity',None);preferred=affinity.preferred(stage) if affinity else None
            row=c.execute(query+' AND source_id=? ORDER BY item_id,revision LIMIT 1',(*args,preferred)).fetchone() if preferred else None
            if row is None:row=c.execute(query+' ORDER BY item_id,revision LIMIT 1',args).fetchone()
            return dict(row) if row else None

    def complete(self, *args, **kwargs):
        self.assert_current_owner()
        if getattr(self,'targeted_projection_invalidation_enabled',False):
            from .v2_projection_peers import complete_with_targeted_projection
            result=complete_with_targeted_projection(self,*args,**kwargs)
        else:result=super().complete(*args, **kwargs)
        affinity=getattr(self,'source_affinity',None)
        if affinity:affinity.completed(args[0] if args else kwargs['job'])
        return result

    def fail(self, *args, **kwargs):
        self.assert_current_owner()
        return super().fail(*args, **kwargs)

    def consume(self, *args, **kwargs):
        self.assert_current_owner()
        return super().consume(*args, **kwargs)

    def deliver(self, consumer, callback, limit=100):
        self.assert_current_owner()
        if limit<=0:return 0
        from .v2_delivery_empty_cache import discover
        with self.connect() as c:
            events=[json.loads(r[0]) for r in discover(self,c,consumer,'all','''SELECT o.payload FROM v2_outbox o
              WHERE o.scheduler_run_id=? AND o.execution_epoch=?
              AND NOT EXISTS (SELECT 1 FROM v2_inbox i WHERE i.consumer=? AND i.event_id=o.event_id)
              ORDER BY o.committed_at,o.event_id LIMIT ?''',
              (self.scheduler_run_id,self.execution_epoch,consumer,limit))]
        count=0
        for event in events:
            if count>=limit:break
            if self.consume(event,consumer,callback):count+=1
        return count

    def deliver_subscribed(self,consumer,callback,event_types,limit=100):
        """Explicit no-op-free subscription; original consume/fencing unchanged."""
        self.assert_current_owner()
        from .v2_producer import fingerprint
        types=tuple(sorted(set(event_types)))
        if not types:raise ValueError('empty_event_subscription')
        policy={'contract':'event_type_subscription_v1','event_types':types,
                'callback':callback.__module__+'.'+callback.__qualname__,
                'nonmatching_events':'retained_audit_not_delivered'}
        digest=fingerprint(policy)
        if limit==0:return 0
        from .v2_delivery_empty_cache import discover,subscribed_rows
        registered=getattr(self,'_registered_delivery_policies',set())
        if (consumer,digest) not in registered:
            with self.connect() as c:
                c.execute('''CREATE TABLE IF NOT EXISTS v2_consumer_delivery_policies(
                    scheduler_run_id,execution_epoch,consumer,policy_sha,policy_json,created_at,
                    PRIMARY KEY(scheduler_run_id,execution_epoch,consumer,policy_sha))''')
                c.execute('INSERT OR IGNORE INTO v2_consumer_delivery_policies VALUES(?,?,?,?,?,?)',
                    (self.scheduler_run_id,self.execution_epoch,consumer,digest,json.dumps(policy,sort_keys=True),time.time()))
                c.execute('''CREATE INDEX IF NOT EXISTS v2_outbox_owner_type_order
                    ON v2_outbox(scheduler_run_id,execution_epoch,event_type,committed_at,event_id)''')
            registered.add((consumer,digest));self._registered_delivery_policies=registered
        with self.connect() as c:
            placeholders=','.join('?' for _ in types)
            events=[json.loads(r[0]) for r in discover(self,c,consumer,digest,'''SELECT o.payload FROM v2_outbox o
                WHERE o.scheduler_run_id=? AND o.execution_epoch=? AND o.event_type IN ('''+placeholders+''')
                AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer=? AND i.event_id=o.event_id)
                ORDER BY o.committed_at,o.event_id LIMIT ?''',
                (self.scheduler_run_id,self.execution_epoch,*types,consumer,limit),
                query=lambda:subscribed_rows(c,self.scheduler_run_id,self.execution_epoch,
                                             consumer,types,limit))]
        return sum(bool(self.consume(event,consumer,callback)) for event in events)

    def source_ready(self, *args, **kwargs):
        self.assert_current_owner()
        return super().source_ready(*args, **kwargs)

    def publish(self, *args, **kwargs):
        self.assert_current_owner()
        return super().publish(*args, **kwargs)

    def finish(self, *args, **kwargs):
        self.assert_current_owner()
        return super().finish(*args, **kwargs)
