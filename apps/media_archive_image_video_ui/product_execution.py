"""The single 1.3.0 product execution entrypoint.

This module owns routing only.  Legacy stage algorithms remain in
``pipeline_orchestrator`` and the event-driven implementation remains the
audited V2 WorkerStore/ClosurePipeline graph.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .central_database import connect
from .database_migration_1_3_0 import migrate_to_1_3_0
from .pipeline_orchestrator import execute_pipeline
from .scheduler_task_state import remember_task, project_current_task, reconcile_task_scheduler
from .scheduler_identity import (
    EVENT_DRIVEN_V2,
    LEGACY_BATCH,
    PIPELINE_CONTRACT_VERSION,
    SchedulerIdentityError,
    load_scheduler_run,
    finish_scheduler_run,
    mark_scheduler_run_recoverable,
    SchedulerOwnershipError,
    load_active_scheduler_run,
    resume_scheduler_run,
    start_scheduler_run,
    validate_run_ownership,
)


class ProductExecutionError(RuntimeError):
    """A product run cannot be safely started or routed."""


def _load_task(task_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(task_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductExecutionError(f"task_load_failed:{exc}") from exc
    if not isinstance(value, dict):
        raise ProductExecutionError("task_contract_invalid")
    for key in ("task_id", "database"):
        if not str(value.get(key) or "").strip():
            raise ProductExecutionError(f"task_{key}_missing")
    return value


def _write_task(task_path: Path, task: Mapping[str, Any]) -> None:
    path = Path(task_path)
    temporary = path.with_name(f".{path.name}.product-execution.tmp")
    temporary.write_text(json.dumps(dict(task), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _current_v2_stop_requested(database: Path, run_id: str, epoch: int) -> bool:
    """Read durable stop intent for the owner currently being dispatched."""
    if not database or not run_id or int(epoch or 0) < 1:
        return False
    try:
        with connect(database, readonly=True) as c:
            row = c.execute(
                "SELECT r.status,l.stop_requested "
                "FROM scheduler_runs r LEFT JOIN v2_product_lifecycle l "
                "ON l.scheduler_run_id=r.scheduler_run_id "
                "WHERE r.scheduler_run_id=? AND r.execution_epoch=?",
                (run_id, int(epoch)),
            ).fetchone()
        return bool(row and row[0] in ('STARTING', 'RUNNING') and row[1])
    except Exception:
        return False


def attach_real_handler_factory(driver: Any, task: Mapping[str, Any], database: Path):
    """Attach the normal production handler boundary for non-fixture runs."""
    from .v2_execution_profile import apply_profile
    from .v2_runtime_resources import runtime_workers as resolve_runtime_workers
    runtime_workers=resolve_runtime_workers(task)
    apply_profile(driver,task,runtime_workers=runtime_workers)
    # Do not let the upgraded model inherit the old 4B cold-load envelope.
    qwen_path = Path((task.get('real_handler_configs') or {}).get('qwen', {}).get('model_path') or '')
    config_path = qwen_path / 'config.json'
    model_config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    text_config = model_config.get('text_config', {})
    if (model_config.get('model_type') == 'qwen3_5' and
            text_config.get('hidden_size') == 4096 and text_config.get('num_hidden_layers') == 32):
        scheduler = getattr(driver, 'admission_scheduler', None)
        if scheduler is not None:
            from .v2_execution_profile import apply_runtime_workers
            runtime_workers = {**(runtime_workers or {}), 'qwen':1}
            apply_runtime_workers(driver,runtime_workers)
            scheduler.heavy_controller.policy['activation_mb']['qwen'] = 10240
    if task.get('fixture_handlers') or driver.handler_factory is not None:
        return None
    from .v2_real_handlers import build_handler_factory
    factory = build_handler_factory(task.get('workspace') or database.parent,
                                    task.get('real_handler_configs'),runtime_workers=runtime_workers)
    driver.handler_factory = factory
    return factory


def create_product_run(
    task_path: Path,
    *,
    requested_mode: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Migrate/open a task and persist exactly one new scheduler identity."""
    task_path = Path(task_path).expanduser().resolve(strict=True)
    task = _load_task(task_path)
    database = Path(str(task["database"])).expanduser().resolve()
    migrate_to_1_3_0(database, task=task)
    remember_task(database, task_path, task)
    try:
        run = start_scheduler_run(
            database,
            str(task["task_id"]),
            scheduler_mode=requested_mode,
            request_id=request_id,
        )
    except SchedulerIdentityError as exc:
        raise ProductExecutionError(str(exc)) from exc
    project_current_task(database, str(task['task_id']))
    return run


def _default_v2_runtime(
    database: Path,
    contracts: Mapping[str, Any] | None,
    _run: Mapping[str, Any],
) -> dict[str, Any]:
    """Construct only after validating product ownership and pinned contracts."""
    from .v2_product_driver import V2ProductDriver
    from .storage_compute_scheduler import StorageComputeScheduler, load_policy
    from .v2_resource_budget import CachedSystemMemory
    policy_path = Path(__file__).resolve().parents[2] / 'configs' / 'storage_aware_scheduler_policy_v1.json'
    scheduler = StorageComputeScheduler(database, policy=load_policy(policy_path) if policy_path.is_file() else None,
                                        pressure_provider=CachedSystemMemory())
    driver = V2ProductDriver(database, _run, contracts, admission_scheduler=scheduler)
    driver.raw_visual_skip_enabled = True
    driver.a9t_coverage_enabled = True
    from .v2_orchestration_tuning import measured_cheap_batch
    driver.cheap_visual_batch_size = measured_cheap_batch()
    driver.store.targeted_projection_invalidation_enabled = True
    return driver.runtime()


class ProductExecutionEntrypoint:
    """Start/resume one persisted run and dispatch it to its owner."""

    def __init__(
        self,
        *,
        legacy_runner: Callable[..., Any] = execute_pipeline,
        v2_runtime_factory: Callable[..., Any] = _default_v2_runtime,
    ) -> None:
        self.legacy_runner = legacy_runner
        self.v2_runtime_factory = v2_runtime_factory

    def start(
        self,
        task_path: Path,
        *,
        requested_mode: str | None = None,
        request_id: str | None = None,
        v2_contracts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        preview = _load_task(task_path)
        if requested_mode == EVENT_DRIVEN_V2 and preview.get('runtime', {}).get('model_root'):
            from .v2_task_materializer import materialize_v2_task
            materialize_v2_task(task_path)
        run = create_product_run(task_path, requested_mode=requested_mode, request_id=request_id)
        return self.dispatch(task_path, run, v2_contracts=v2_contracts, resume=False)

    def reconcile_candidates(self,task_path: Path,*,request_id: str):
        """Candidate-only correction of one completed task, not a full restart."""
        from .v2_completed_candidate_repair import plan_metadata_corrections,begin_completed_candidate_repair
        task_path=Path(task_path).expanduser().resolve(strict=True)
        task=_load_task(task_path)
        if task.get('scheduler_mode')!=EVENT_DRIVEN_V2:raise ProductExecutionError('candidate_repair_requires_v2')
        base_id=(task.get('candidate_repair') or {}).get('base_run_id') or task['scheduler_run_id']
        if load_scheduler_run(Path(task['database']),base_id)['status']!='COMPLETED':
            raise ProductExecutionError('candidate_repair_base_not_completed_v2')
        plan=task.get('candidate_repair') or plan_metadata_corrections(task)
        # Persist the bounded intent BEFORE new ownership becomes recoverable.
        # A crash must never recover the new run as a full producer restart.
        task['candidate_repair']=plan
        task['finder_tags']=plan['finder_tags']
        _write_task(task_path,task)
        database=Path(task['database'])
        remember_task(database,task_path,task)
        run=begin_completed_candidate_repair(database,plan['base_run_id'],plan['scope'],task['v2_contracts'],request_id=request_id)
        for key in ('scheduler_run_id','execution_epoch','scheduler_mode','pipeline_contract_version'):
            task[key]=run[key]
        _write_task(task_path,task)
        remember_task(database,task_path,task)
        project_current_task(database,task['task_id'])
        if run['status']=='COMPLETED':return {'status':'PASS','run':run,'result':{'status':'COMPLETE','repair_already_completed':True}}
        if run['status']=='FAILED':
            from .v2_completed_candidate_repair import recover_failed_candidate_repair
            run=recover_failed_candidate_repair(task,run)
        if run['status']=='RECOVERABLE':
            run=resume_scheduler_run(database,run['scheduler_run_id'],scheduler_mode=EVENT_DRIVEN_V2,
                execution_epoch=run['execution_epoch'])
            run['_resume_handoff']=True
        return self.dispatch(task_path,run,resume=True)

    def resume(
        self,
        task_path: Path,
        *,
        requested_mode: str | None = None,
        v2_contracts: Mapping[str, Any] | None = None,
        resume_execution: bool = True,
        repair_media_tools_request_id: str | None = None,
    ) -> dict[str, Any]:
        task_path = Path(task_path).expanduser().resolve(strict=True)
        task = _load_task(task_path)
        if ((requested_mode == EVENT_DRIVEN_V2 or task.get("scheduler_mode") == EVENT_DRIVEN_V2)
                and task.get('runtime', {}).get('model_root')):
            from .v2_task_materializer import materialize_v2_task
            task = materialize_v2_task(task_path)
        database = Path(str(task["database"])).expanduser().resolve()
        migrate_to_1_3_0(database, task=task)
        # Reject an incompatible active request before task reconstruction or
        # scheduler recovery can rewrite any authoritative execution state.
        preflight = load_active_scheduler_run(database, str(task['task_id']))
        if preflight and preflight.get('pipeline_contract_version') != PIPELINE_CONTRACT_VERSION:
            raise ProductExecutionError('unsupported_pipeline_contract')
        remember_task(database, task_path, task)
        try:
            persisted = reconcile_task_scheduler(database, str(task['task_id']))
        except SchedulerIdentityError as exc:
            raise ProductExecutionError(str(exc)) from exc
        run_id = str(persisted['scheduler_run_id'])
        mode = str(requested_mode or persisted["scheduler_mode"])
        if repair_media_tools_request_id:
            if mode != EVENT_DRIVEN_V2:
                raise ProductExecutionError('media_tool_recovery_requires_v2')
            from .v2_failure_recovery import recover_failed_media_tools_run
            persisted = recover_failed_media_tools_run(task, persisted, request_id=repair_media_tools_request_id)
        if mode == EVENT_DRIVEN_V2:
            from .v2_failure_recovery import recover_failed_sqlite_run
            persisted = recover_failed_sqlite_run(task, persisted)
            from .v2_product_lifecycle import reconcile_orphaned_execution
            reconcile_orphaned_execution(database, run_id, int(persisted['execution_epoch']))
        if persisted.get('pipeline_contract_version') != PIPELINE_CONTRACT_VERSION:
            raise ProductExecutionError('unsupported_pipeline_contract')
        try:
            run = resume_scheduler_run(
                database,
                run_id,
                scheduler_mode=mode,
                execution_epoch=int(persisted["execution_epoch"]),
            )
        except SchedulerIdentityError as exc:
            raise ProductExecutionError(str(exc)) from exc
        project_current_task(database, str(task['task_id']))
        run['_resume_handoff'] = persisted['status'] == 'RECOVERABLE'
        return self.dispatch(task_path, run, v2_contracts=v2_contracts, resume=resume_execution)

    def repair_completed_projection_schema(self, task_path, *, request_id):
        """Reconcile sealed sources rejected by the obsolete three-section gate.

        Reuse the audited scoped-repair ownership transition. No extraction,
        candidate policy change or successful model-result replacement.
        """
        from .v2_completed_candidate_repair import begin_completed_candidate_repair
        task_path = Path(task_path).resolve(strict=True)
        task = _load_task(task_path)
        with connect(Path(task['database']), readonly=True) as c:
            rows = list(c.execute("""SELECT o.source_id,o.source_revision,o.payload,e.scheduler_run_id
                FROM v2_source_outcomes o JOIN v2_active_sources a
                ON a.source_id=o.source_id AND a.source_revision=o.source_revision
                JOIN v2_source_epochs e ON e.source_id=o.source_id AND e.source_revision=o.source_revision
                JOIN scheduler_runs r ON r.scheduler_run_id=e.scheduler_run_id
                WHERE r.task_id=? AND o.status='FAILED_FINAL'
                AND o.reason_code='final_projection_contract_failed'""", (task['task_id'],)))
        rows = [r for r in rows if json.loads(r['payload']).get('reason') ==
                'final_quality_failed:missing_required_sections:1）概括|2）元素|3）检索价值']
        if not rows:
            return {'status':'PASS', 'already_reconciled':True}
        owners = {r['scheduler_run_id'] for r in rows}
        if len(owners) != 1:
            raise ProductExecutionError('projection_schema_repair_requires_single_sealed_owner')
        base = owners.pop()
        plan = {'contract':'completed_candidate_metadata_repair_v1','base_run_id':base,
                'scope':{r['source_id']:r['source_revision'] for r in rows},
                'corrections':{},'finder_tags':task.get('finder_tags',[]),
                'reason':'raw_media_projection_schema_adapter_recovery','request_id':request_id}
        task['candidate_repair'] = plan
        _write_task(task_path, task)
        run = begin_completed_candidate_repair(Path(task['database']), base, plan['scope'],
                                              task['v2_contracts'], request_id=request_id, allow_video=True)
        for key in ('scheduler_run_id','execution_epoch','scheduler_mode','pipeline_contract_version'):
            task[key] = run[key]
        _write_task(task_path, task)
        remember_task(Path(task['database']),task_path,task)
        if run['status']=='COMPLETED':
            return {'status':'PASS','already_reconciled':True,'run':run}
        from .scheduler_switching import FencedWorkerStore
        store = FencedWorkerStore(Path(task['database']),task_id=run['task_id'],
                                  scheduler_run_id=run['scheduler_run_id'],execution_epoch=run['execution_epoch'])
        for row in rows:
            store.record_outcome(row['source_id'], row['source_revision'],
                                 {'status':'RECOVERING','reason_code':'projection_schema_recovery',
                                  'request_id':request_id,'previous_outcome':json.loads(row['payload'])})
        return self.dispatch(task_path,run,resume=True)

    def retry_failed_qwen(self,task_path,*,candidate_id,new_config,new_contract,request_id,repetition_only=False,same_parameters=False,schema_retry_only=False):
        from .v2_completed_candidate_repair import plan_failed_qwen_contract_retry,begin_completed_candidate_repair,enqueue_failed_qwen_contract_retry
        task_path=Path(task_path).resolve(strict=True);task=_load_task(task_path)
        previous=task.get('candidate_repair') or {}
        if previous.get('retry_request_id')==request_id:
            plan=previous
            if plan['retry_candidate_id']!=candidate_id or plan['run_handler_configs']['qwen']!=new_config or plan['run_contracts']['qwen']!=new_contract:
                raise ProductExecutionError('qwen_retry_request_conflict')
        else:plan=plan_failed_qwen_contract_retry(task,candidate_id,new_config,new_contract,request_id,repetition_only=repetition_only,same_parameters=same_parameters,schema_retry_only=schema_retry_only)
        from .scheduler_identity import load_scheduler_run
        current=load_scheduler_run(Path(task['database']),task['scheduler_run_id'])
        if current['status']=='RECOVERABLE' and (repetition_only or same_parameters or schema_retry_only) and not previous:
            from .v2_qwen_source_retry import retry_on_recoverable_run
            return retry_on_recoverable_run(task,plan)
        task['candidate_repair']=plan;_write_task(task_path,task)
        database=Path(task['database'])
        run=begin_completed_candidate_repair(database,plan['base_run_id'],plan['scope'],task['v2_contracts'],request_id=request_id,allow_video=True)
        for key in ('scheduler_run_id','execution_epoch','scheduler_mode','pipeline_contract_version'):task[key]=run[key]
        _write_task(task_path,task);remember_task(database,task_path,task);project_current_task(database,task['task_id'])
        if run['status']=='COMPLETED':return {'status':'PASS','run':run,'result':{'status':'COMPLETE','repair_already_completed':True}}
        if run['status']=='FAILED':
            from .v2_completed_candidate_repair import recover_failed_candidate_repair
            run=recover_failed_candidate_repair(task,run)
        if run['status']=='RECOVERABLE':
            run=resume_scheduler_run(database,run['scheduler_run_id'],scheduler_mode=EVENT_DRIVEN_V2,execution_epoch=run['execution_epoch'])
            run['_resume_handoff']=True
        enqueue_failed_qwen_contract_retry(database,run,plan)
        return self.dispatch(task_path,run,resume=True)

    def dispatch(
        self,
        task_path: Path,
        run: Mapping[str, Any],
        *,
        v2_contracts: Mapping[str, Any] | None = None,
        resume: bool,
    ) -> dict[str, Any]:
        task = _load_task(Path(task_path))
        repair=task.get('candidate_repair') or {}
        if repair.get('run_contracts'):
            # Run-local override. Never rewrite the other sources' frozen
            # model configs/contracts or reinterpret their valid results.
            task={**task,'v2_contracts':repair['run_contracts'],'real_handler_configs':repair['run_handler_configs']}
        database = Path(str(task["database"])).expanduser().resolve()
        mode = str(run.get("scheduler_mode") or "")
        try:
            owned = validate_run_ownership(
                database,
                str(run["scheduler_run_id"]),
                scheduler_mode=mode,
                execution_epoch=int(run["execution_epoch"]),
            )
        except (KeyError, SchedulerIdentityError) as exc:
            raise ProductExecutionError(str(exc)) from exc
        # Compatibility and identity rejection is a preflight outcome.  Keep
        # it outside the execution exception handler so an invalid request
        # cannot terminalize the scheduler row it never validly entered.
        if run.get('pipeline_contract_version') != PIPELINE_CONTRACT_VERSION:
            raise ProductExecutionError('unsupported_pipeline_contract')
        # All request identity checks happen before the execution exception
        # path. Invalid task/run/mode/epoch requests must not terminalize the
        # authoritative run they failed to enter.
        from .v2_product_driver import OWNER_FIELDS, validate_owner
        if any(task.get(key) != owned[key] for key in OWNER_FIELDS):
            raise SchedulerOwnershipError('task_scheduler_identity_mismatch')
        if mode == EVENT_DRIVEN_V2:
            validate_owner(database, run)
        try:
            if mode == LEGACY_BATCH:
                result = self.legacy_runner(Path(task_path), resume=resume)
            elif mode == EVENT_DRIVEN_V2:
                from .v2_product_lifecycle import ProductExecutionLease
                with ProductExecutionLease(database, owned['scheduler_run_id']) as execution_lease:
                    result = self.v2_runtime_factory(database, v2_contracts if v2_contracts is not None else task.get("v2_contracts"), owned)
                    # Source admission and bounded orchestration belong to the
                    # product driver.  fixture_handlers only selects cheap model
                    # handlers; it is not an orchestration permission bit.
                    if task.get('v2_sources') and isinstance(result, Mapping) and result.get('driver'):
                        driver = result['driver']
                        driver._product_execution_lease = execution_lease
                        real_factory = attach_real_handler_factory(driver, task, database)
                        try:
                            _publish_product_state(task, status='running',worker_pid=os.getpid())
                            result = driver.run_bounded({**task, '_resume_handoff':bool(run.get('_resume_handoff'))})
                        finally:
                            if real_factory is not None:
                                real_factory.close()
            else:
                raise ProductExecutionError(f"unsupported_scheduler_mode:{mode}")
            if not isinstance(result, Mapping):
                raise ProductExecutionError('execution_result_invalid')
            for key in ('status', 'error', 'failed_stage_key', 'reason_code'):
                _native_field(result.get(key), key)
        except (Exception, KeyboardInterrupt, SystemExit) as exc:
            import traceback
            traceback.print_exc()
            if str(exc) == 'product_execution_already_owned':
                raise ProductExecutionError(str(exc)) from exc
            # A Stop request is a durable handoff.  A final worker boundary
            # can surface a message-less SystemExit while that request is
            # already committed; preserve RECOVERABLE instead of FAILED.
            if (mode == EVENT_DRIVEN_V2 and _current_v2_stop_requested(
                    database, str(run.get('scheduler_run_id') or ''),
                    int(run.get('execution_epoch') or 0))):
                result = {'status': 'RECOVERABLE', 'error': None,
                          'reason_code': 'USER_STOP_FOR_RESUME'}
            else:
                cancelled = isinstance(exc, KeyboardInterrupt) or (isinstance(exc, SystemExit) and exc.code in (130, 143))
                result = {'status': 'cancelled' if cancelled else 'failed',
                          'error': str(exc), 'reason_code': 'TASK_CANCELLED' if cancelled else 'PRODUCT_EXECUTION_FAILED'}
            _record_exception_state(task, result)
        status = execution_result_status(mode, result)
        if result.get('status') == 'RECOVERABLE':
            owned = mark_scheduler_run_recoverable(database, str(owned['scheduler_run_id']),
                scheduler_mode=mode, execution_epoch=int(owned['execution_epoch']))
            project_current_task(database, str(task['task_id']))
        elif status != 'DISPATCHED':
            terminal = {'success': 'COMPLETED', 'COMPLETE': 'COMPLETED',
                        'cancelled': 'CANCELLED', 'aborted': 'ABORTED'}.get(result.get('status'), 'FAILED')
            try:
                owned = finish_scheduler_run(database, str(owned['scheduler_run_id']),
                    scheduler_mode=mode, execution_epoch=int(owned['execution_epoch']), status=terminal)
            except SchedulerOwnershipError:
                # A switch owns the new epoch. Never finish or rewrite that run.
                owned = load_scheduler_run(database, str(owned['scheduler_run_id']))
                result = {'status': 'aborted', 'error': 'scheduler ownership lost', 'reason_code': 'SCHEDULER_OWNERSHIP_LOST'}
                status = 'FAIL'
            project_current_task(database, str(task['task_id']))
        if task.get('state_path') and owned.get('execution_epoch') == task.get('execution_epoch') and result.get('reason_code') != 'SCHEDULER_OWNERSHIP_LOST':
            _publish_product_state(task,
                status={'COMPLETED':'success','RECOVERABLE':'recoverable'}.get(owned['status'],owned['status'].lower()),
                lifecycle=result.get('lifecycle'),worker_pid=None,current_child_pid=None)
        return {'status': status, 'scheduler_mode': mode, 'runtime': mode, 'run': owned, 'result': result}


def _publish_product_state(task, **changes):
    """Serialize native state publication with the current scheduler owner."""
    if not task.get('state_path'):
        return
    from .pipeline_orchestrator import atomic_json
    with connect(Path(task['database'])) as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT * FROM scheduler_runs WHERE task_id=? ORDER BY execution_epoch DESC LIMIT 1', (task['task_id'],)).fetchone()
        if not current or any(current[k] != task.get(k) for k in ('scheduler_run_id','scheduler_mode','execution_epoch')):
            return
        # Publication is a projection of durable state, never an independent
        # lifecycle transition (including after a completed/recoverable handoff).
        expected_status = {'STARTING':'running', 'COMPLETED':'success'}.get(current['status'], current['status'].lower())
        if changes.get('status') != expected_status:
            return
        path = Path(task['state_path'])
        state = json.loads(path.read_text()) if path.is_file() else {'task_id':task['task_id']}
        state.update(changes)
        atomic_json(path,state)


def execution_result_status(mode: str, result: Mapping[str, Any]) -> str:
    if mode == EVENT_DRIVEN_V2 and result.get('status') is None:
        return 'DISPATCHED'  # Runtime construction only, until the product driver runs.
    return 'PASS' if result.get('status') in ('success', 'COMPLETE', 'RECOVERABLE') else 'FAIL'


def _record_exception_state(task: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """Match the legacy worker's explicit failure state for raised exceptions."""
    if not task.get('state_path'):
        return
    # The inner legacy pipeline fences its own writes, but product dispatch is
    # an outer write boundary.  A switch can retire this owner between the
    # inner exception and this handler; in that case the target's shared state
    # file is current truth and must remain byte-for-byte untouched.
    run_id = str(task.get('scheduler_run_id') or '').strip()
    database = Path(str(task.get('database') or ''))
    if run_id and database.is_file():
        try:
            # Hold the scheduler write lock through the state publication.
            # Switching uses the same transaction boundary, so it cannot
            # retire this owner between validation and atomic_json().
            con = connect(database)
            try:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute(
                    'SELECT scheduler_mode,scheduler_run_id,execution_epoch,status '
                    'FROM scheduler_runs WHERE scheduler_run_id=?', (run_id,)
                ).fetchone()
                if (
                    row is None
                    or row['scheduler_mode'] != task.get('scheduler_mode')
                    or row['scheduler_run_id'] != run_id
                    or int(row['execution_epoch']) != int(task.get('execution_epoch'))
                    or row['status'] not in ('STARTING', 'RUNNING', 'RECOVERABLE')
                ):
                    return
                path = Path(task['state_path'])
                state = json.loads(path.read_text()) if path.is_file() else {'task_id': task['task_id']}
                now = time.time()
                state.update(status=result['status'], error=result['error'], reason_code=result['reason_code'],
                             current_child_pid=None, updated_at_epoch=now, finished_at_epoch=now)
                for stage in state.get('stages', []):
                    if stage.get('status') == 'running':
                        stage.update(status=result['status'], finished_at_epoch=now, reason_code=result['reason_code'])
                from .pipeline_orchestrator import atomic_json
                atomic_json(path, state)
                con.commit()
            finally:
                con.close()
            return
        except (OSError, TypeError, ValueError, KeyError):
            return
    from .pipeline_orchestrator import atomic_json
    path = Path(task['state_path'])
    state = json.loads(path.read_text()) if path.is_file() else {'task_id': task['task_id']}
    now = time.time()
    state.update(status=result['status'], error=result['error'], reason_code=result['reason_code'],
                 current_child_pid=None, updated_at_epoch=now, finished_at_epoch=now)
    for row in state.get('stages', []):
        if row.get('status') == 'running':
            row.update(status=result['status'], finished_at_epoch=now, reason_code=result['reason_code'])
    atomic_json(path, state)


NATIVE_RESPONSE_CONTRACT = "media_archive_product_worker_response_v1"


def _native_field(value: Any, key: str, expected: type = str) -> Any:
    if value is not None and type(value) is not expected:
        raise ProductExecutionError(f"native_response_invalid_field:{key}")
    return value


def native_worker_response(dispatched: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit wire schema; runtime/store/pipeline objects stay Python-only."""
    run = dispatched["run"]
    mode = _native_field(dispatched["scheduler_mode"], "scheduler_mode")
    result = dispatched["result"]
    status = execution_result_status(mode, result)
    if mode == LEGACY_BATCH or status != 'DISPATCHED':
        execution_status = _native_field(result.get("status"), "status")
        if mode == EVENT_DRIVEN_V2 and status == "FAIL":execution_status = "failed"
        summary = {key: _native_field(result.get(key), key) for key in
                   ("status", "error", "failed_stage_key", "reason_code")}
        if result.get('lifecycle') is not None:
            summary['lifecycle'] = result['lifecycle']
        summary["completed_stage_count"] = _native_field(result.get("completed_stage_count"), "completed_stage_count", int)
    else:
        # Construction is not pipeline completion or validation evidence.
        execution_status = "runtime_initialized"
        summary = {"status": execution_status, "runtime": "audited_pipeline_v2"}
    response = {
        "contract": NATIVE_RESPONSE_CONTRACT,
        "status": status,
        "exit_code": 0 if status in ("PASS", "DISPATCHED") else (143 if execution_status == "cancelled" else 2),
        "identifier": _native_field(run["task_id"], "task_id"),
        "message": summary.get("error") or execution_status,
        "error": summary.get("error"),
        "reason_code": summary.get("reason_code"),
        "execution_status": execution_status,
        "runtime": _native_field(dispatched["runtime"], "runtime"),
        "scheduler_mode": mode,
        "scheduler_run_id": _native_field(run["scheduler_run_id"], "scheduler_run_id"),
        "execution_epoch": _native_field(run["execution_epoch"], "execution_epoch", int),
        "pipeline_contract_version": _native_field(run["pipeline_contract_version"], "pipeline_contract_version"),
        "product_schema_version": _native_field(run["product_schema_version"], "product_schema_version", int),
        "scheduler_run_status": _native_field(run["status"], "scheduler_run_status"),
        "result": summary,
    }
    return response


def execute_product_task(task_path: Path, *, resume: bool, repair_media_tools_request_id: str | None = None) -> dict[str, Any]:
    """Native JSON boundary for a previously persisted product identity."""
    try:
        entrypoint = ProductExecutionEntrypoint()
        return native_worker_response(entrypoint.resume(task_path, resume_execution=resume, repair_media_tools_request_id=repair_media_tools_request_id))
    except Exception as exc:
        return {
            "contract": NATIVE_RESPONSE_CONTRACT,
            "status": "FAIL",
            "execution_status": "failed",
            "exit_code": 2,
            "error": str(exc),
            "message": str(exc),
            "reason_code": "PRODUCT_EXECUTION_FAILED",
        }
