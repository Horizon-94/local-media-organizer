"""Ownership and pinned-contract foundation for the product V2 driver.

Construction is not processing completion. Media admission and the bounded pump
are separate integration steps. No inference libraries are imported here.
"""
from copy import deepcopy
from collections import deque
import contextlib
import inspect
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType

from .central_database import connect
from .scheduler_identity import (
    ACTIVE_RUN_STATUSES, EVENT_DRIVEN_V2, PIPELINE_CONTRACT_VERSION,
    SchedulerOwnershipError,
)
from .v2_runtime import ContractError
from .storage_compute_scheduler import AdmissionBlocked, StageRoundRobin, workload_class


def wait_for_producer_admission(operation, stop_requested, *, sleeper=time.sleep):
    """Admission pressure is waiting, never a decode failure/retry sweep.

    ProductMedia obtains original-read admission before publishing a revision.
    Stop is checked at every bounded wait; decode and ownership errors propagate.
    """
    while True:
        if stop_requested():
            raise RuntimeError('producer_admission_wait_stopped')
        try:
            return operation()
        except AdmissionBlocked:
            sleeper(0.1)

REQUIRED_STAGES = ('yoloe', 'openclip', 'dhash', 'quality', 'geometry',
                   'qwen', 'ocr', 'audio_extract', 'vad', 'whisper', 'embedding')
MODEL_STAGES = {'yoloe', 'openclip', 'qwen', 'ocr', 'vad', 'whisper', 'embedding', 'insightface'}
WORKER_CONTRACT = 'pipeline-v2-local-worker-v2'
OWNER_FIELDS = ('task_id', 'scheduler_mode', 'scheduler_run_id', 'execution_epoch', 'pipeline_contract_version')


class _CooperativeSchedulerRevisit(Exception):
    """Return durable advancement control to the outer scheduler."""


class _CooperativeStopRequested(Exception):
    """Abort a bounded durable-advance slice at the supported stop boundary."""


class _ProducerRefillRequested(Exception):
    """Return to the producer collector as soon as its source slot closes."""


def validate_contracts(contracts):
    if not isinstance(contracts, dict) or not set(REQUIRED_STAGES) <= contracts.keys():
        raise ContractError('production_worker_contracts_missing')
    if set(contracts) - set(REQUIRED_STAGES) - {'insightface'}:
        raise ContractError('unsupported_production_worker_stage')
    def digest(value):
        return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)
    for stage, policy in contracts.items():
        if not isinstance(policy, dict) or policy.get('execution_contract') != WORKER_CONTRACT or policy.get('stage') != stage:
            raise ContractError('unsupported_production_worker_contract:' + stage)
        if type(policy.get('revision')) is not int or policy['revision'] < 1:
            raise ContractError('production_worker_revision_invalid:' + stage)
        if not all(digest(policy.get(k)) for k in ('code', 'config', 'preprocess')):
            raise ContractError('production_worker_fingerprint_missing:' + stage)
        dependencies = policy.get('dependencies')
        if not isinstance(dependencies, dict) or not dependencies or not all(digest(v) for v in dependencies.values()):
            raise ContractError('production_worker_dependencies_missing:' + stage)
        models = policy.get('model_files')
        if not isinstance(models, dict) or (stage in MODEL_STAGES and not models):
            raise ContractError('production_worker_models_missing:' + stage)
        if any(not isinstance(files, dict) or not files or not all(digest(v) for v in files.values()) for files in models.values()):
            raise ContractError('production_worker_models_invalid:' + stage)
        if not isinstance(policy.get('packages'), dict) or not isinstance(policy.get('python'), str) or not policy['python']:
            raise ContractError('production_worker_environment_missing:' + stage)
        if 'prompt' not in policy or (policy['prompt'] is not None and not digest(policy['prompt'])):
            raise ContractError('production_worker_prompt_invalid:' + stage)
    # Reject non-JSON values and detach caller-owned mutable configuration.
    try:
        return json.loads(json.dumps(contracts, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ContractError('production_worker_contract_not_json') from exc


def validate_owner(database, run):
    if run.get('scheduler_mode') != EVENT_DRIVEN_V2 or run.get('pipeline_contract_version') != PIPELINE_CONTRACT_VERSION:
        raise SchedulerOwnershipError('unsupported_v2_product_contract')
    if type(run.get('execution_epoch')) is not int or run['execution_epoch'] < 1:
        raise SchedulerOwnershipError('invalid_v2_execution_epoch')
    with connect(database, readonly=True) as c:
        row = c.execute('SELECT * FROM scheduler_runs WHERE scheduler_run_id=?', (run.get('scheduler_run_id'),)).fetchone()
    if not row or any(row[k] != run.get(k) for k in OWNER_FIELDS) or row['status'] not in ACTIVE_RUN_STATUSES:
        raise SchedulerOwnershipError('stale_scheduler_execution_epoch')
    return {key: row[key] for key in OWNER_FIELDS}


class V2ProductDriver:
    def __init__(self, database, run, contracts, *, handler_factory=None, admission_scheduler=None):
        # Validate before any runtime constructor can create schema/jobs/events.
        self.contracts = validate_contracts(contracts)
        self.owner = MappingProxyType(validate_owner(database, run))
        self.database = database
        self.admission_scheduler = admission_scheduler
        if self.admission_scheduler is not None:
            self.admission_scheduler.switch_owner(self.owner['task_id'], self.owner['scheduler_run_id'], self.owner['execution_epoch'])
        # Handler selection is an inference boundary.  It must never decide
        # whether product orchestration runs; tests and deployments can inject
        # handlers here without bypassing admission, event pumping, or closure.
        self.handler_factory = handler_factory
        from .scheduler_switching import FencedWorkerStore
        from .v2_closure import ClosurePipeline
        self.store = FencedWorkerStore(database, task_id=self.owner['task_id'],
            scheduler_run_id=self.owner['scheduler_run_id'], execution_epoch=self.owner['execution_epoch'])
        self.pipeline = ClosurePipeline(self.store, deepcopy(self.contracts), scheduler=self.admission_scheduler)
        self.product_lifecycle_enabled = True

    def producer(self, policy=None, artifact_root=None):
        from .v2_producer import ProducerStore
        store=ProducerStore(self.database, policy, artifact_root, scheduler_owner=dict(self.owner))
        store.before_publish=getattr(self,'simple_publish_hook',None)
        return store

    def install_video_adapter(self, module):
        from .v2_legacy_producer import install
        install(module, self.database, scheduler_owner=dict(self.owner),before_publish=getattr(self,'simple_publish_hook',None))

    def media(self, workspace):
        from .v2_product_media import ProductMediaProducer
        return ProductMediaProducer(self, workspace)

    def _durable_completion_snapshot(self, stage_runnable=None):
        """Return authoritative current-run completion state.

        The admitted list is an orchestration cache.  It can lag the durable
        source/outcome projection after a streaming revision closes, so it must
        not be the final completion authority.  A missing/older schema is
        reported as unavailable for small injected stores; production stores
        have all of these tables.
        """
        try:
            with connect(self.database, readonly=True) as c:
                from .v2_current_jobs_cache import install_readonly_view
                install_readonly_view(c)
                active = list(c.execute(
                    """SELECT a.source_id,a.source_revision,s.eos,
                              o.status AS outcome_status
                         FROM v2_active_sources a
                         JOIN v2_sources s
                           ON s.source_id=a.source_id AND s.revision=a.source_revision
                         JOIN v2_source_epochs e
                           ON e.source_id=a.source_id AND e.source_revision=a.source_revision
                          AND e.scheduler_run_id=? AND e.execution_epoch=?
                         LEFT JOIN v2_source_outcomes o
                           ON o.source_id=a.source_id AND o.source_revision=a.source_revision""",
                    (self.owner['scheduler_run_id'], self.owner['execution_epoch'])))
                if not active:
                    return None
                raw_skips = set()
                covered_sources = set()
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_timelapse_coverage'").fetchone():
                    covered_sources = {(r['source_id'], r['source_revision']) for r in c.execute(
                        "SELECT source_id,source_revision FROM v2_timelapse_coverage WHERE scheduler_run_id=? AND status='COMPLETE'",
                        (self.owner['scheduler_run_id'],))}
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_raw_visual_skips'").fetchone():
                    raw_skips = {(r['source_id'],r['source_revision']) for r in c.execute(
                        "SELECT source_id,source_revision FROM v2_raw_visual_skips WHERE status IN ('ACTIVE','COMPLETE')")}
                current_jobs = list(c.execute(
                    """SELECT j.status
                         FROM v2_current_jobs j
                         JOIN v2_source_epochs e
                           ON e.source_id=j.source_id AND e.source_revision=j.source_revision
                          AND e.scheduler_run_id=? AND e.execution_epoch=?
                        WHERE j.scheduler_run_id=? AND j.execution_epoch=?""",
                    (self.owner['scheduler_run_id'], self.owner['execution_epoch'],
                     self.owner['scheduler_run_id'], self.owner['execution_epoch'])))
                active_admissions = c.execute(
                    """SELECT COUNT(*) FROM scheduler_admissions
                        WHERE status='ACTIVE' AND scheduler_run_id=? AND execution_epoch=?""",
                    (self.owner['scheduler_run_id'], self.owner['execution_epoch'])).fetchone()[0]
        except Exception:
            return None

        terminal_statuses = {'COMPLETE', 'DEGRADED', 'FAILED_FINAL', 'FAILED', 'ABORTED'}
        terminal_sources = sum(1 for row in active
                               if (row['eos'] == 'EOS' or
                                   (row['eos']=='VISUAL_SKIPPED' and (row['source_id'],row['source_revision']) in raw_skips) or
                                   (row['eos']=='TIMELAPSE_COVERED' and (row['source_id'],row['source_revision']) in covered_sources))
                               and row['outcome_status'] in terminal_statuses)
        nonterminal_jobs = sum(1 for row in current_jobs
                               if row['status'] in ('PENDING', 'RUNNING', 'FAILED_RETRYABLE'))
        runnable = bool(stage_runnable and any(stage_runnable(stage) for stage in self.contracts))
        snapshot = {
            'current_source_count': len(active),
            'terminal_source_count': terminal_sources,
            'current_nonterminal_jobs': nonterminal_jobs,
            'current_runnable_jobs': int(runnable),
            'active_workers': sum(1 for row in current_jobs if row['status'] == 'RUNNING'),
            'active_admissions': int(active_admissions),
        }
        snapshot['complete'] = bool(
            snapshot['current_source_count'] > 0
            and snapshot['terminal_source_count'] == snapshot['current_source_count']
            and snapshot['current_nonterminal_jobs'] == 0
            and snapshot['current_runnable_jobs'] == 0
            and snapshot['active_workers'] == 0
            and snapshot['active_admissions'] == 0
        )
        return snapshot

    @staticmethod
    def _fixture_handler(stage, vectors):
        from .v2_documents import validate_result
        from .v2_runtime import validate_evidence
        if stage == 'yoloe':
            return lambda payload: {'detections': []}, lambda result: validate_evidence('yoloe', result)
        if stage == 'insightface':
            return lambda payload: {'faces': []}, lambda result: validate_evidence('insightface', result)
        if stage == 'openclip':
            return lambda payload: {'vector': vectors.get(payload.get('frame_id'), [1.0, 0.0])}, lambda result: validate_evidence('openclip', result)
        if stage in ('dhash', 'quality', 'geometry'):
            from .v2_model_workers import create_handler
            return create_handler(stage, {}), lambda result, stage=stage: validate_evidence(stage, result)
        if stage == 'qwen':
            def fixture_qwen(payload):
                from .v2_model_workers import module
                schema = module('qwen').raw_media_contract
                values = {field: '无法可靠判断' for field in schema.FIELDS}
                text = '\n'.join(line for heading, fields in schema.GROUPS
                    for line in (heading, *(field + '：' + values[field] for field in fields)))
                return {'contract': 'production-qwen-sections-v1', 'qwen_schema_version': schema.SCHEMA_VERSION,
                    'text': text, 'sections': {field + '：': value for field, value in values.items()},
                    'raw': {'cleanup_status': 'ok', 'truncation_status': 'complete'}}
            return fixture_qwen, lambda result: validate_result('qwen', result)
        if stage == 'ocr':
            return lambda payload: {'text': 'fixture OCR'}, lambda result: validate_result('ocr', result)
        if stage == 'audio_extract':
            return lambda payload: {'status': 'NO_AUDIO'}, lambda result: validate_result('audio_extract', result)
        if stage == 'vad':
            return lambda payload: {'segments': []}, lambda result: validate_result('vad', result)
        if stage == 'whisper':
            return lambda payload: {'text': 'fixture speech'}, lambda result: validate_result('whisper', result)
        if stage == 'embedding':
            return lambda payload: {'vector': [1.0, 0.0]}, lambda result: validate_result('embedding', result)
        raise ContractError('fixture_handler_not_supported:' + stage)

    def run_bounded(self, task, *, max_iterations=200, dispatch_start_timeout_seconds=None):
        """Run the persisted product graph to completion or an explicit boundary.

        This is orchestration only. All durable effects remain in the audited
        producer, outbox, worker, document and index components.
        """
        sources = task.get('v2_sources') or []
        simple=task.get('experimental_pipeline_profile')=='SIMPLE_PIPELINE'
        if task.get('experimental_pipeline_profile') not in (None,'CONTROL','SIMPLE_PIPELINE'):
            raise ContractError('experimental_pipeline_profile_invalid')
        repair=task.get('candidate_repair')
        planning_sources=sources
        if repair:
            if repair.get('contract')!='completed_candidate_metadata_repair_v1':raise ContractError('candidate_repair_contract_invalid')
            sources=[s for s in sources if s['source_content_id'] in repair['scope']]
            if {s['source_content_id'] for s in sources}!=set(repair['scope']):raise ContractError('candidate_repair_inventory_missing')
            from .v2_completed_candidate_repair import install_metadata_corrections
            install_metadata_corrections(self.pipeline.selection,repair)
            self.pipeline.selection.repair_reuse_contracts=self.contracts
            self.pipeline.final_projection.repair_source_scope=set(repair['scope'])
        cheap_batch_size=task.get('cheap_visual_batch_size',getattr(self,'cheap_visual_batch_size',1))
        if type(cheap_batch_size) is not int or cheap_batch_size not in (1,2,4,8,16,32):
            raise ContractError('invalid_cheap_visual_batch_size')
        openclip_batch_size=task.get('openclip_batch_size',1)
        if type(openclip_batch_size) is not int or openclip_batch_size not in (1,2,4,8):
            raise ContractError('invalid_openclip_batch_size')
        if not isinstance(sources, list) or not sources:
            raise ContractError('product_sources_missing')
        media = self.media(task.get('workspace') or str(self.database.parent))
        for source in sources:
            if source.get('media_kind', source.get('media_type')) not in ('video','image'):
                raise ContractError('unsupported_product_source_media:' + str(source.get('media_kind')))
        lifecycle = None
        image_plan = None
        if getattr(self, 'a9t_coverage_enabled', False) and any(s.get('media_kind', s.get('media_type')) == 'image' for s in sources):
            from .v2_a9t_plan import plan_images
            from .v2_product_media import load_module
            module = load_module('scripts/02_step01_step02_pipeline/step02_2_image_preview_from_db_safe_v6_20260709_182200.py')
            image_plan = plan_images(module, [s for s in planning_sources if s.get('media_kind', s.get('media_type')) == 'image'],
                                     task.get('finder_tags') or [])
        if getattr(self, 'product_lifecycle_enabled', False):
            from .v2_source_recovery import RecoveringProductLifecycle as ProductLifecycle
            if getattr(self,'raw_visual_skip_enabled',False):
                from .v2_raw_audio_lane import RawAwareProductLifecycle as ProductLifecycle
            options = {'resume_handoff': bool(task.get('_resume_handoff')),
                       'execution_lease': getattr(self, '_product_execution_lease', None)}
            if image_plan is not None and not repair:
                from .v2_timelapse_coverage import CoverageAwareProductLifecycle as ProductLifecycle
                options['image_plan'] = image_plan
                options['driver'] = self
                options['source_assets'] = task.get('source_assets') or sources
            lifecycle = ProductLifecycle(self.store, sources, **options)
        selection = getattr(self.pipeline, 'selection', None)
        if selection is not None:
            selection.sequence_plan = image_plan
            selection.sequence_source_assets = task.get('source_assets') or sources
        admitted = lifecycle.admitted() if lifecycle else []
        source_affinity=task.get('visual_source_affinity',False)
        if type(source_affinity) is not bool:raise ContractError('invalid_visual_source_affinity')
        self.store.source_affinity=None
        if source_affinity:
            from .v2_source_affinity import SourceAffinity
            self.store.source_affinity=SourceAffinity(self.store)
            for item in admitted:self.store.source_affinity.admit(item['source_id'],item['source_revision'])
        def admit_rows(rows):
            admitted.extend(rows)
            if source_affinity:
                for item in rows:self.store.source_affinity.admit(item['source_id'],item['source_revision'])
        from .v2_performance import Performance, attach
        perf = Performance(task.get('performance_path') or (str(task['scheduler_trace_path'])+'.perf.jsonl' if task.get('scheduler_trace_path') else None))
        self.performance = perf
        runnable_seen = {}
        previous_pass_activity = [0,0]
        attach(perf, self.store, self.pipeline)
        from .v2_runtime_observer import register_live_stack_signal
        register_live_stack_signal(self,task)
        if self.admission_scheduler is not None:
            perf.wrap(self.admission_scheduler, 'acquire', 'ADMISSION_CHECK')
        original_pipeline=self.pipeline
        coordination=None;direct_budget=None;people_lane=None
        if simple:
            from .v2_simple_pipeline import CoordinationConsumer,DirectQueueBudget,SimplePipelineFacade,PeopleLane
            stopped=lambda:bool(lifecycle and lifecycle.state()['stop_requested'])
            direct_budget=DirectQueueBudget(self.store,self.pipeline.fanout_consumer,
                policy=task.get('simple_queue_watermarks'),stop_requested=stopped)
            direct_budget.document_consumer=self.pipeline.documents.consumer
            from .v2_simple_fanout import FanoutPlans
            if self.pipeline.__class__.__name__=='ClosurePipeline':
                planner=FanoutPlans(self.store,direct_budget,stop_requested=stopped)
                self.store.fanout_planner=planner;self.store.enqueue_capture=planner.capture
            if self.pipeline.__class__.__name__=='ClosurePipeline':
                people_lane=PeopleLane(self.pipeline.people,stop_requested=stopped,performance=perf)
                self.pipeline.people=people_lane
            clone_factory=None
            if self.pipeline.__class__.__name__=='ClosurePipeline':
                def clone_factory():
                    from .v2_closure import ClosurePipeline
                    clone=ClosurePipeline(self.store,deepcopy(self.contracts),deepcopy(self.pipeline.selection_config),scheduler=self.admission_scheduler)
                    clone.selection.sequence_plan=deepcopy(image_plan)
                    clone.selection.sequence_source_assets=deepcopy(task.get('source_assets') or sources)
                    clone.people=people_lane
                    clone.index.performance=perf
                    clone.final_projection.performance=perf
                    perf.wrap(clone.index,'callback','INDEX_CALLBACK')
                    perf.wrap(clone,'advance_source','SOURCE_ADVANCE')
                    perf.wrap(clone.selection,'select','SELECTION_ADVANCE')
                    perf.wrap(clone,'_canonicalize_local','JOIN_CHECK')
                    return clone
            try:
                coordination=CoordinationConsumer(self.pipeline,refresh_source=getattr(lifecycle,'refresh_source',None),
                    stop_requested=stopped,performance=perf,clone_factory=clone_factory)
            except BaseException:
                try:
                    if people_lane is not None:people_lane.close()
                finally:
                    if people_lane is not None:original_pipeline.people=people_lane.people
                    if lifecycle is not None:lifecycle.close()
                    self.store.fanout_planner=None;self.store.enqueue_capture=None
                raise
            self.pipeline=SimplePipelineFacade(self.pipeline,coordination,direct_budget)
            self.simple_publish_hook=direct_budget.before_publish
            self.store.enqueue_guard=direct_budget.enqueue_guard
            self.store.publication_guard=direct_budget.publication_guard

        def measured_sleep(seconds):
            perf.count('PASSES_IDLE_SLEEP')
            with perf.phase('SLEEP'):time.sleep(seconds)
        vectors = {}
        fallback_completed_stages = set()

        def stage_runnable(stage):
            checker = getattr(self.store, 'has_runnable_jobs', None)
            if checker is None:
                # Small injected test stores may not expose the production
                # query; the real WorkerStore always does.
                # Do not invent runnable work before the first source has
                # produced an artifact.  Production stores use the durable
                # predicate above; this conservative fallback keeps injected
                # stores from starting downstream work ahead of preprocessing.
                return bool(admitted) and stage not in fallback_completed_stages
            with perf.phase('RUNNABLE_FILTER',stage):
                return checker(stage, execution_contract=self.contracts[stage])

        def preprocess_attempt(source):
            if lifecycle is None:
                return preprocess(source)
            attempt = lifecycle.begin(source)
            if attempt is None:
                return []
            owned_source = {**source, '_product_attempt_id': attempt}
            try:
                items = wait_for_producer_admission(
                    lambda: preprocess(owned_source),
                    lambda: bool(lifecycle.state()['stop_requested']))
                lifecycle.ingested(source['source_content_id'], attempt, items)
                return items
            except SchedulerOwnershipError:
                # Old ownership is never turned into a retry failure.
                raise
            except Exception as exc:
                import sqlite3
                if isinstance(exc, sqlite3.DatabaseError):
                    raise
                if isinstance(exc, OSError) and getattr(exc, 'errno', None) in (28, 30):
                    raise  # Full/read-only output storage is project-fatal.
                lifecycle.failed_ingest(source['source_content_id'], attempt, f'{type(exc).__name__}: {exc}')
                return []
            finally:
                if self.admission_scheduler is not None:
                    for lease in self.admission_scheduler.active_tokens():
                        if (lease.task_id == source['source_content_id'] and lease.requires_original
                                and lease.scheduler_run_id == self.owner['scheduler_run_id']):
                            self.admission_scheduler.release(lease, reason='source_attempt_closed')

        def preprocess(source):
            kind = source.get('media_kind', source.get('media_type'))
            # The legacy-compatible adapters emit human-readable progress
            # diagnostics. Product-worker stdout is the JSON protocol, so
            # keep those diagnostics inside this bounded source boundary.
            with contextlib.redirect_stdout(io.StringIO()):
                if kind == 'video':
                    return [media.video(source, interval_seconds=int(source.get('interval_seconds', 3)),
                        offset_ms=int(source.get('offset_ms', 2000)))]
                if kind == 'image':
                    report = media.images([source], source_assets=task.get('source_assets') or [],
                        finder_tags=task.get('finder_tags') or [], image_plan=image_plan)
                    return [{'source_id': sid, 'source_revision': rev}
                        for sid, rev in report['dependencies'].items()]
                raise ContractError('unsupported_product_source_media:' + str(kind))

        # One bounded producer lane avoids concurrent mutation of the frozen
        # Stage03 adapter while allowing the next original-media read to run
        # concurrently with downstream V2 work from the previous source.
        from .storage_compute_scheduler import DEFAULT_POLICY
        refill_policy = {**DEFAULT_POLICY['producer'],
            **getattr(self.admission_scheduler, 'policy', {}).get('producer', {})}
        low, high = int(refill_policy['source_low_water']), int(refill_policy['source_high_water'])
        slice_seconds = float(refill_policy['advancement_slice_seconds'])
        if not 0 <= low < high or slice_seconds <= 0:
            raise ContractError('invalid_producer_refill_policy')
        source_queues = {'video': deque(), 'image': deque()}
        paused = {'video': False, 'image': False}
        next_kind = 'video'

        def enqueue_sources(rows):
            for row in rows:
                source_queues[row.get('media_kind', row.get('media_type'))].append(row)

        enqueue_sources(lifecycle.pending_ingest() if lifecycle else sources)

        def next_ingest():
            nonlocal next_kind
            if simple:
                for kind in (next_kind,'image' if next_kind=='video' else 'video'):
                    allowed,reason=direct_budget.can_refill(kind)
                    self.producer_pressure=getattr(self,'producer_pressure',{})
                    self.producer_pressure[kind]={'direct_queue_counts':dict(direct_budget.last),'blocked_reason':reason}
                    if source_queues[kind] and allowed:
                        next_kind='image' if kind=='video' else 'video'
                        return source_queues[kind].popleft()
                return None
            backlog = {'video': 0, 'image': 0}
            model_backlog = {'video': 0, 'image': 0}
            model_sources=None
            raw_authorized=set()
            if lifecycle:
                with connect(self.database, readonly=True) as c:
                    from .v2_producer_pressure import pending_model_sources
                    model_sources=pending_model_sources(c,self.owner['scheduler_run_id'],self.owner['execution_epoch'])
                    raw_sealed=set()
                    if getattr(self,'raw_visual_skip_enabled',False):
                        from .v2_raw_audio_lane import producer_raw_refill_state
                        raw_sealed,raw_authorized=producer_raw_refill_state(c,self.owner['scheduler_run_id'],self.owner['execution_epoch'])
                    for row in c.execute("SELECT source_id,source_json FROM v2_product_sources WHERE scheduler_run_id=? AND state='INGESTED'", (self.owner['scheduler_run_id'],)):
                        if row[0] in raw_sealed:continue
                        data = json.loads(row[1])
                        kind = data.get('media_kind', data.get('media_type'))
                        if kind in backlog:
                            backlog[kind] += 1
                            if model_sources is not None and row[0] in model_sources:model_backlog[kind]+=1
            else:
                for item in admitted:
                    if item.get('outcome', {}).get('status') not in ('COMPLETE', 'DEGRADED'):
                        kind = source_kinds.get(item['source_id'])
                        if kind in backlog:
                            backlog[kind] += 1
            for kind in (next_kind, 'image' if next_kind == 'video' else 'video'):
                if model_sources is not None:
                    from .v2_producer_pressure import update_pressure
                    paused[kind],reason=update_pressure(paused[kind],model_backlog[kind],backlog[kind],
                        low=int(refill_policy.get('model_source_low_water',2)),
                        target=int(refill_policy.get('model_source_target_water',6)),
                        high=int(refill_policy.get('model_source_high_water',12)),
                        closure_high=int(refill_policy.get('closure_source_high_water',high+max(12,high//2))))
                    self.producer_pressure=getattr(self,'producer_pressure',{})
                    self.producer_pressure[kind]={'model_sources':model_backlog[kind],'closure_sources':backlog[kind],'blocked_reason':reason}
                else:
                    if paused[kind] and backlog[kind] <= low:paused[kind] = False
                    if backlog[kind] >= high:paused[kind] = True
                raw_refill=bool(source_queues[kind] and kind=='video' and source_queues[kind][0]['source_content_id'] in raw_authorized)
                if source_queues[kind] and (not paused[kind] or raw_refill):
                    next_kind = 'image' if kind == 'video' else 'video'
                    return source_queues[kind].popleft()
            return None

        source_kinds = {s['source_content_id']: s.get('media_kind', s.get('media_type')) for s in sources}
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='v2-source')
        # Downstream work has its own bounded executor.  Source preprocessing
        # remains one lane, while admitted derivative jobs can overlap without
        # making the orchestration thread wait on model inference.
        policy_compute = {}
        if self.admission_scheduler is not None:
            policy_compute = getattr(self.admission_scheduler, 'policy', {}).get('compute', {})
        executor_workers = max(8, sum(int(v) for v in policy_compute.values()) or 0)
        downstream_executor = ThreadPoolExecutor(max_workers=executor_workers, thread_name_prefix='v2-downstream')
        inflight = []
        dispatch_timeout = (float(dispatch_start_timeout_seconds) if
                            dispatch_start_timeout_seconds is not None else
                            float(os.environ.get('MEDIA_ARCHIVE_DISPATCH_START_TIMEOUT_SECONDS', '30')))
        if dispatch_timeout <= 0:
            raise ContractError('dispatch_start_timeout_must_be_positive')
        future = None
        # A producer future is real in-flight work.  Keep the orchestration
        # thread from busy-spinning while that bounded source read/preview
        # operation owns the next transition.  The larger bound is exposed by
        # the deterministic max_iterations argument so tests can exercise the
        # liveness fence without wall-clock sleeps.
        producer_wait_iterations = 0
        no_progress_iterations = 0
        reconciliation_attempts = 0
        stages = REQUIRED_STAGES + (('insightface',) if 'insightface' in self.contracts else ())
        stage_fairness = StageRoundRobin(stages, getattr(self.admission_scheduler, 'policy', {}))
        stage_trace = []
        self.last_stage_trace = stage_trace
        trace_path = task.get('scheduler_trace_path')
        diagnostic_started = None
        diagnostic_dumped = False
        diagnostic_snapshot_item = None
        diagnostic_snapshot_lock = threading.RLock()
        resource_denials = {}
        resource_idle_since = None
        resource_idle_marker = None

        def durable_progress_marker():
            """Return a cheap durable marker for event/state progression.

            The marker is deliberately advisory.  The database remains the
            authority; an unavailable marker (including injected test stores
            without a schema) simply disables the extra same-iteration pass.
            """
            try:
                with connect(self.database, readonly=True) as c:
                    # SQLite's rowid is monotonically assigned for this
                    # append-only outbox and avoids a full COUNT(*) scan on
                    # every bounded scheduling iteration.
                    row = c.execute('SELECT COALESCE(MAX(rowid), 0) FROM v2_outbox').fetchone()
                return (int(row[0]),) if row else None
            except Exception:
                return None

        def trace(stage, **values):
            # Keep only transition-level diagnostics, rather than a noisy
            # record for every false runnable check.
            record = {'stage': stage, 'time': time.time(), **values}
            stage_trace.append(record)
            if len(stage_trace) > 256:
                del stage_trace[:-256]
            if trace_path:
                try:
                    path = Path(trace_path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open('a', encoding='utf-8') as handle:
                        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
                except OSError:
                    # Diagnostics must never change scheduler correctness.
                    pass

        def execution_trace(item, event, **values):
            trace(item['stage'], event=event, execution_id=item['execution_id'],
                  job_id=item.get('job_id'), source_id=item.get('source_id'),
                  run_id=self.owner.get('scheduler_run_id'),
                  epoch=self.owner.get('execution_epoch'),
                  admission_id=getattr(item.get('lease'), 'token_id', None),
                  future_id=item.get('future_id'),
                  handler_id=id(item['handler']) if item.get('handler') else None,
                  worker_id=item.get('worker_id'),
                  memory_admission=item.get('memory_admission'),
                  pass_kind=item.get('pass_kind'),
                  claimed=item.get('claimed', False),
                  thread_id=threading.get_ident(),
                  thread_name=threading.current_thread().name,
                  **values)

        def release_trace(item, event, release):
            execution_trace(item, event)
            try:
                release()
            except Exception as exc:
                execution_trace(item, event.replace('_ENTER', '_EXCEPTION'),
                                error=type(exc).__name__)
                raise
            execution_trace(item, event.replace('_ENTER', '_RETURN'))

        def snapshot_threads(item=None):
            stacks = []
            frames = sys._current_frames()
            for thread in threading.enumerate():
                frame = frames.get(thread.ident)
                if frame is None:
                    continue
                stacks.append(f"Thread {thread.name} ({thread.ident})\\n" +
                              ''.join(traceback.format_stack(frame)))
            trace('scheduler', event='THREAD_STACK_SNAPSHOT',
                  execution_id=item.get('execution_id') if item else None,
                  execution_stage=item.get('stage') if item else None,
                  admission_id=getattr(item.get('lease'), 'token_id', None) if item else None,
                  future_id=item.get('future_id') if item else None,
                  handler_id=id(item['handler']) if item and item.get('handler') else None,
                  thread_id=threading.get_ident(),
                  thread_name=threading.current_thread().name,
                  stack='\\n'.join(stacks))

        def arm_stack_snapshot(item):
            nonlocal diagnostic_snapshot_item
            # Capture Python-owned frames at a safe point. Native asynchronous
            # frame walking can hang (and cancellation blocks the collector)
            # with this bundled Python. Diagnostics must never own task progress.
            with diagnostic_snapshot_lock:
                if not trace_path or item.get('stack_snapshot_armed'):
                    return
                if diagnostic_snapshot_item is not None:
                    return
                path = Path(str(trace_path) + '.stacks')
                handle = path.open('a', encoding='utf-8')
                item['stack_snapshot_armed'] = True
                item['stack_snapshot_file'] = handle
                diagnostic_snapshot_item = item
                execution_trace(item, 'THREAD_STACK_SNAPSHOT_ARMED', path=str(path))
                try:
                    frames = sys._current_frames()
                    for thread in threading.enumerate():
                        frame = frames.get(thread.ident)
                        if frame is not None:
                            handle.write(f'Thread {thread.name} ({thread.ident})\n')
                            handle.write(''.join(traceback.format_stack(frame)))
                    handle.flush()
                except Exception:
                    pass

        def cancel_stack_snapshot(item):
            nonlocal diagnostic_snapshot_item
            with diagnostic_snapshot_lock:
                handle = item.get('stack_snapshot_file')
                if handle is not None and diagnostic_snapshot_item is item:
                    handle.close()
                    item['stack_snapshot_file'] = None
                    diagnostic_snapshot_item = None

        def collect_finished():
            completed = 0
            remaining = []
            for item in inflight:
                execution_trace(item, 'COLLECTOR_SCAN_SEES_FUTURE',
                                future_done=item['future'].done())
                if not item['future'].done():
                    remaining.append(item)
                    continue
                execution_trace(item, 'COLLECTOR_FUTURE_DONE_TRUE')
                item['collector_done_seen'] = True
                cancel_stack_snapshot(item)
                timer = item.get('diagnostic_timer')
                if timer is not None:
                    timer.cancel()
                controller=getattr(self.admission_scheduler,'heavy_controller',None)
                try:
                    execution_trace(item, 'COLLECTOR_RESULT_ENTER')
                    with perf.phase('FUTURE_WAIT',item['stage']):had_job=item['future'].result()
                    controller=getattr(self.admission_scheduler,'heavy_controller',None)
                    if (had_job and controller is not None and item['stage'] in controller.selected and
                            item.get('job_id')):
                        with connect(self.database,readonly=True) as c:
                            committed=c.execute("SELECT status FROM v2_jobs WHERE execution_key=?",(item['job_id'],)).fetchone()
                        if committed and committed[0]=='DONE':controller.completion(item['stage'])
                    execution_trace(item, 'COLLECTOR_RESULT_RETURN')
                    item['result'] = 'finished'
                    execution_trace(item, 'FUTURE_FINISHED')
                except Exception as exc:
                    item['result'] = 'failed'
                    execution_trace(item, 'COLLECTOR_RESULT_EXCEPTION',
                                    error=type(exc).__name__)
                    trace(item['stage'], event='execution_failure', error=type(exc).__name__)
                    execution_trace(item, 'FUTURE_FINISHED', error=type(exc).__name__)
                finally:
                    lease = item.get('lease')
                    if lease is not None and self.admission_scheduler is not None:
                        release_trace(
                            item, 'ADMISSION_RELEASE_ENTER',
                            lambda: self.admission_scheduler.release(lease, reason='compute_complete'))
                        execution_trace(item, 'ADMISSION_RELEASED', reason='compute_complete')
                    if (self.handler_factory is not None and
                            hasattr(self.handler_factory, 'release')):
                        release_trace(
                            item, 'HANDLER_RELEASE_ENTER',
                            lambda: self.handler_factory.release(item.get('handler')))
                        execution_trace(item, 'HANDLER_RELEASED', reason='future_complete')
                        if (controller is not None and item['stage'] in controller.selected and
                                hasattr(self.handler_factory,'trim_idle')):
                            retired=self.handler_factory.trim_idle(item['stage'],controller.selected[item['stage']])
                            if retired:execution_trace(item,'SCALE_DOWN_WORKER_RETIRED',count=retired)
                item['removed_from_inflight'] = True
                execution_trace(item, 'EXECUTION_REMOVED_FROM_INFLIGHT')
                completed += 1
            inflight[:] = remaining
            return completed

        cooperative_collected = 0
        cooperative_revisit_marker = None
        cooperative_revisit_armed = False
        advancement_started = None
        source_revisit_yielded = set()

        def advancement_yield():
            """Service the collector and yield new durable work to scheduling."""
            nonlocal cooperative_collected
            trace('scheduler', event='ADVANCEMENT_SLICE_YIELD',
                  collector_pending=bool(inflight))
            cooperative_collected += collect_finished()
            if cooperative_revisit_armed:
                marker = durable_progress_marker()
                if (marker is not None and cooperative_revisit_marker is not None and
                        marker != cooperative_revisit_marker):
                    trace('scheduler', event='ADVANCEMENT_YIELD_REQUESTED',
                          reason='DURABLE_PROGRESS_REQUIRES_SCHEDULER_REVISIT',
                          marker_before=cooperative_revisit_marker,
                          marker_after=marker)
                    raise _CooperativeSchedulerRevisit()

        last_refill_probe = 0.0

        def stop_aware_advancement_yield():
            nonlocal future,last_refill_probe
            advancement_yield()
            if lifecycle and lifecycle.state()['stop_requested']:
                raise _CooperativeStopRequested()
            # Watermarks can recover during a long closure plan, while no
            # producer future exists. Do not wait for that entire plan to
            # return before replenishing the sole original-media lane.
            now=time.monotonic()
            if future is None and any(source_queues.values()) and now-last_refill_probe>=max(1.0,slice_seconds):
                last_refill_probe=now
                next_source=next_ingest()
                trace('scheduler',event='PRODUCER_REFILL_PROBE',
                    selected_source=next_source.get('source_content_id') if next_source else None,
                    pressure=getattr(self,'producer_pressure',{}))
                if next_source is not None:
                    future=executor.submit(preprocess_attempt,next_source)
            # A completed producer is a wakeup even when the outbox marker
            # has not changed.  Advancing every admitted source can otherwise
            # delay the sole producer collector by many minutes.
            if future is not None and future.done():
                raise _ProducerRefillRequested()

        def supports_cooperative(method):
            try:
                parameters = inspect.signature(method).parameters
            except (TypeError, ValueError):
                return False
            return 'yield_hook' in parameters

        def advance_events_fair():
            method = self.pipeline.advance_events
            perf.count('MARKER_QUANTA')
            with perf.phase('EVENT_OUTBOX'):
                if supports_cooperative(method):
                    return method(cooperative=True, yield_hook=stop_aware_advancement_yield)
                return method()

        def advance_source_fair(source, revision):
            nonlocal cooperative_revisit_armed, cooperative_revisit_marker, cooperative_collected
            method = self.pipeline.advance_source
            # A source is also a runnable scheduler participant. Completed
            # worker markers must not cancel every source before its join.
            # Cooperative production advancement now pumps one event quantum
            # per boundary; stop/refill/collector yields remain active.
            armed = cooperative_revisit_armed
            previous_marker = cooperative_revisit_marker
            source_key = (source, revision)
            # A source interrupted for new work gets one complete bounded
            # advancement on its next visit. This prevents a stream of worker
            # completions from repeatedly cancelling the same source join.
            cooperative_revisit_armed = armed and source_key not in source_revisit_yielded
            # Drain old completions before taking this source's baseline.
            # New durable work created *inside* advancement must still yield.
            cooperative_collected += collect_finished()
            cooperative_revisit_marker = durable_progress_marker()
            try:
                perf.count('SOURCE_ADVANCE_CALLS')
                with perf.phase('SOURCE_ADVANCE'):
                    if supports_cooperative(method):
                        result = method(source, revision, yield_hook=stop_aware_advancement_yield)
                    else:
                        result = method(source, revision)
                    source_revisit_yielded.discard(source_key)
                    return result
            except _CooperativeSchedulerRevisit:
                source_revisit_yielded.add(source_key)
                raise
            finally:
                cooperative_revisit_armed = armed
                cooperative_revisit_marker = previous_marker

        def recover_unstarted():
            """Cancel only executions that never entered the worker.

            A queued Future is allowed to wait while executor capacity is
            legitimately occupied.  Once it has exceeded the dispatch-start
            bound, cancellation is attempted; release happens only when
            cancellation succeeds, so a running/claimed job cannot lose its
            admission or ownership.
            """
            recovered = 0
            now = time.monotonic()
            remaining = []
            for item in inflight:
                future = item['future']
                if item.get('started_at') is not None or future.done():
                    remaining.append(item)
                    continue
                if item.get('submitted_at') is None or now - item['submitted_at'] < dispatch_timeout:
                    remaining.append(item)
                    continue
                if not future.cancel():
                    remaining.append(item)
                    continue
                execution_trace(item, 'DISPATCH_TIMEOUT', elapsed=now - item['submitted_at'])
                lease = item.get('lease')
                if lease is not None and self.admission_scheduler is not None:
                    self.admission_scheduler.release(lease, reason='dispatch_start_timeout')
                    execution_trace(item, 'ADMISSION_RELEASED', reason='dispatch_start_timeout')
                if self.handler_factory is not None and hasattr(self.handler_factory, 'release'):
                    self.handler_factory.release(item.get('handler'))
                    execution_trace(item, 'HANDLER_RELEASED', reason='dispatch_start_timeout')
                recovered += 1
            inflight[:] = remaining
            return recovered

        def _future_done_callback(done, item):
            item['done_callback_seen'] = True
            execution_trace(
                item, 'FUTURE_DONE_CALLBACK',
                future_done=done.done(),
                future_cancelled=done.cancelled(),
                future_exception=(type(done.exception()).__name__
                                  if done.done() and not done.cancelled() and done.exception()
                                  else None))
            arm_stack_snapshot(item)

        def _run_stage_pass(pass_kind):
            """Give every stage one bounded scheduling opportunity.

            A second pass is requested only by durable progress observed in
            the current loop.  This keeps fairness and the existing bounded
            liveness fence while ensuring downstream work created after the
            first pass is reconsidered before the loop can declare a stall.
            """
            pass_progress = 0
            marker = durable_progress_marker()
            if pass_kind == 'revisit':
                trace('scheduler', event='STAGE_REVISIT_STARTED', marker=marker)
            with perf.phase('FAIRNESS_SELECT'):
                stage_order = stage_fairness.order()
            for stage in stage_order:
                if simple and not direct_budget.can_dispatch(len(inflight)):
                    trace(stage,event='SIMPLE_COORDINATION_EVENT_BACKPRESSURE',blocked_reason='SIMPLE_COORDINATION_EVENTS_HIGH_WATER')
                    continue
                if lifecycle and lifecycle.state()['stop_requested']:
                    break
                runnable = stage_runnable(stage)
                if not runnable:
                    resource_denials.pop(stage,None)
                    controller=getattr(self.admission_scheduler,'heavy_controller',None)
                    if controller is not None:controller.clear_wait(stage)
                    if self.handler_factory is not None and hasattr(self.handler_factory,'release_idle_due'):
                        # Queue-empty is not a scale-up request. Keep the
                        # existing bounded idle grace, never permanent residents.
                        # The factory excludes leased/running inference workers.
                        upstream={'qwen':{'yoloe','openclip','dhash','geometry','quality'},
                            'ocr':{'yoloe','openclip','dhash','geometry','quality'},
                            'embedding':{'qwen','ocr','whisper','audio_extract','vad'},
                            'whisper':{'audio_extract','vad'}}.get(stage,set())
                        refill=bool(future is not None or any(source_queues.values()) or
                            any(i['stage'] in upstream for i in inflight))
                        release=self.handler_factory.release_idle_due
                        retired=(release(stage,upstream_pending=refill)
                            if getattr(self.handler_factory,'bounded_upstream_idle',False) else release(stage))
                        if retired:trace(stage,event='IDLE_MODEL_RETIRED',count=retired)
                    continue
                if pass_kind == 'revisit':
                    trace(stage, event='LATE_RUNNABLE_JOB_SEEN', marker=marker)
                admission = 'not_required'
                lease = None
                item = {'execution_id': uuid.uuid4().hex, 'stage': stage,
                        'handler': None, 'future': None, 'future_id': None,
                        'submitted_at': None, 'started_at': None, 'claimed': False,
                        'pass_kind': pass_kind}
                execution_trace(item, 'JOB_RUNNABLE')
                peek = getattr(self.store, 'peek_runnable_job', None)
                requested = peek(stage, execution_contract=self.contracts[stage]) if peek else None
                if requested:
                    item['job_id'] = requested['execution_key']
                    item['source_id'] = requested['source_id']
                    runnable_seen.setdefault(item['job_id'],time.monotonic())
                if self.admission_scheduler is not None:
                    admission = 'attempted'
                    execution_trace(item, 'ADMISSION_REQUESTED')
                    try:
                        lease = self.admission_scheduler.acquire(
                            f"compute:{stage}", workload_class(stage),
                            from_derivative=stage != 'audio_extract',
                            requires_original=stage == 'audio_extract',
                            source_path=json.loads(requested['payload']).get('path') if requested and stage == 'audio_extract' else None,
                            job_type=stage, job_id=requested['execution_key'] if requested else None,
                            resident_ready=bool(self.handler_factory is not None and
                                hasattr(self.handler_factory,'resident_ready') and
                                self.handler_factory.resident_ready(stage)),
                            scheduler_run_id=self.owner['scheduler_run_id'],
                            execution_epoch=self.owner['execution_epoch'])
                        admission = 'granted'
                        resource_denials.pop(stage,None)
                        controller=getattr(self.admission_scheduler,'heavy_controller',None)
                        if controller is not None and stage in controller.last_decisions:
                            item['memory_admission']=dict(controller.last_decisions[stage])
                            trace(stage,event='HEAVY_ADMISSION_GRANTED',memory=item['memory_admission'],job_id=item.get('job_id'))
                        stage_fairness.granted(stage)
                        item['lease'] = lease
                        execution_trace(item, 'ADMISSION_GRANTED')
                    except AdmissionBlocked as exc:
                        admission = 'blocked'
                        if str(exc).startswith(('heavy_memory_','model_memory_')):
                            resource_denials[stage]=getattr(exc,'diagnostics',{})
                        trace(stage, pass_kind=pass_kind, marker=marker,
                              runnable=True, admission=admission,
                              reason=type(exc).__name__, **getattr(exc, 'diagnostics', {}))
                        if (str(exc) in ('heavy_memory_headroom','heavy_memory_pressure','heavy_memory_fair_turn',
                                         'model_memory_headroom','model_memory_pressure') and
                                self.handler_factory is not None and hasattr(self.handler_factory,'retire_idle_models')):
                            retire=self.handler_factory.retire_idle_models
                            protected=set()
                            if str(exc).endswith('_headroom'):
                                # A deferred cold activation is not an excuse
                                # to evict warm models that have ready work.
                                protected=({s for s in stages if stage_runnable(s)}
                                    if inflight or future is not None else {stage})
                            retired=(retire(protected_stages=protected,
                                maximum_workers=1 if str(exc).endswith('_headroom') else None)
                                if 'protected_stages' in inspect.signature(retire).parameters else retire())
                            if retired:trace(stage,event='MEMORY_IDLE_MODELS_RETIRED',retired=retired)
                        # An admission block can coexist with an in-flight
                        # future for the same stage.  Do not close that
                        # handler here; completion owns release/idle cleanup.
                        continue
                handler_attempted = False
                run_one_attempted = False
                claimed = False
                handler = None
                try:
                    handler_attempted = True
                    execution_trace(item, 'HANDLER_ACQUIRE_START')
                    if self.handler_factory is not None:
                        handler, validator = self.handler_factory(stage, vectors, task)
                    elif task.get('fixture_handlers'):
                        handler, validator = self._fixture_handler(stage, vectors)
                    else:
                        raise ContractError('production_worker_handler_factory_required:' + stage)
                    item['handler'] = handler
                    resident=getattr(handler,'_resident_handler',handler)
                    process=getattr(resident,'process',None)
                    item['worker_id']=getattr(process,'pid',id(resident))
                    execution_trace(item, 'HANDLER_ACQUIRED')
                    run_one_attempted = True
                    def execute(item=item, handler=handler, validator=validator, stage=stage):
                        execution_trace(item, 'ASYNC_WRAPPER_ENTER')
                        item['started_at'] = time.monotonic()
                        execution_trace(item, 'FUTURE_STARTED')
                        execution_trace(item, 'RUN_ONE_ENTER')
                        execution_trace(item, 'RUN_ONE_CALL_ENTER')
                        def observed_handler(payload):
                            current_claim = getattr(self.store, 'current_claim', None)
                            job = current_claim() if current_claim else None
                            if job:
                                item['job_id'],item['source_id']=job['execution_key'],job['source_id']
                                item['claimed']=True
                                first_seen=runnable_seen.pop(item['job_id'],None)
                                if first_seen is not None:perf.record('SCHEDULER_WAIT',time.monotonic()-first_seen,stage)
                                bind=getattr(self.admission_scheduler,'bind_claim',None)
                                if bind and item.get('lease'):bind(item['lease'],job)
                                execution_trace(item,'CLAIM_GRANTED_AT',claim_granted_at=job['claim_granted_at'])
                            execution_trace(item,'RUN_STARTED_AT',run_started_at=time.time())
                            try:
                                with perf.phase('HANDLER',stage):return handler(payload)
                            finally:execution_trace(item,'RUN_FINISHED_AT',run_finished_at=time.time())
                        try:
                            with perf.phase('JOB_PATH',stage):
                                # Orchestration microbatch, NOT model batch inference.
                                # Each claim/result/outbox commits independently; the
                                # scheduler can observe source-ready after any job.
                                limit=cheap_batch_size if stage in ('dhash','geometry','quality') else 2 if stage=='qwen' else 1
                                deadline=time.monotonic()+(30 if stage=='qwen' else 0.25)
                                count=0
                                if stage=='openclip' and openclip_batch_size>1:
                                    batch_handler=getattr(handler,'batch',None)
                                    if batch_handler is None:raise ContractError('configured_true_batch_handler_missing')
                                    batch_jobs=[]
                                    def batch_claimed(job):
                                        batch_jobs.append(job)
                                        perf.count('TRUE_BATCH_CLAIMS_openclip')
                                        item['job_id'],item['source_id']=job['execution_key'],job['source_id']
                                        item['claimed']=True
                                        execution_trace(item,'CLAIM_GRANTED_AT',claim_granted_at=job.get('claim_granted_at'))
                                        first_seen=runnable_seen.pop(job['execution_key'],None)
                                        if first_seen is not None:perf.record('SCHEDULER_WAIT',time.monotonic()-first_seen,stage)
                                    def observed_batch(payloads):
                                        perf.count('TRUE_BATCH_INVOCATIONS_openclip')
                                        bind=getattr(self.admission_scheduler,'bind_claim',None)
                                        if bind and item.get('lease'):bind(item['lease'],batch_jobs[0])
                                        execution_trace(item,'TRUE_BATCH_ADMISSION_MEMBERS',job_ids=[j['execution_key'] for j in batch_jobs],source_ids=[j['source_id'] for j in batch_jobs])
                                        with perf.phase('HANDLER',stage):return batch_handler(payloads)
                                    count=self.store.run_batch(stage,observed_batch,validator,
                                        batch_size=openclip_batch_size,owner='product-worker',
                                        execution_contract=self.contracts[stage],on_claim=batch_claimed,
                                        should_stop=lambda:bool(lifecycle and lifecycle.state()['stop_requested']))
                                else:
                                    for batch_index in range(limit):
                                        if batch_index and (time.monotonic()>=deadline or
                                                (lifecycle and lifecycle.state()['stop_requested'])):break
                                        if batch_index and stage=='qwen' and self.admission_scheduler is not None:
                                            continuation=getattr(self.admission_scheduler,'continue_heavy_job',None)
                                            if continuation is None or not continuation(stage):break
                                        done=self.store.run_one(
                                            stage, observed_handler, validator, owner='product-fixture',
                                            execution_contract=self.contracts[stage])
                                        if not done:break
                                        count+=1
                                        perf.count('MICROBATCH_JOBS_'+stage)
                                result=bool(count)
                                item['completed_job_count']=count
                        except Exception as exc:
                            execution_trace(item, 'RUN_ONE_CALL_EXCEPTION',
                                            error=type(exc).__name__)
                            raise
                        execution_trace(item, 'RUN_ONE_CALL_RETURN')
                        item['claimed'] = bool(result)
                        if result and getattr(self.store, 'has_runnable_jobs', None) is None:
                            fallback_completed_stages.add(stage)
                        execution_trace(item, 'JOB_CLAIMED' if result else 'RUN_ONE_NO_CLAIM')
                        execution_trace(item, 'ASYNC_WRAPPER_PRE_RETURN')
                        execution_trace(item, 'ASYNC_WRAPPER_RETURN')
                        return result
                    execution = downstream_executor.submit(execute)
                    item['future'] = execution
                    item['future_id'] = id(execution)
                    item['submitted_at'] = time.monotonic()
                    execution_trace(item, 'FUTURE_CREATED')
                    execution_trace(item, 'FUTURE_SUBMITTED')
                    execution.add_done_callback(
                        lambda done, item=item: _future_done_callback(done, item))
                    inflight.append(item)
                    claimed = True
                    pass_progress += 1
                finally:
                    trace(stage, pass_kind=pass_kind, marker=marker,
                          runnable=True, admission=admission,
                          handler_acquisition=handler_attempted,
                          run_one=run_one_attempted, claimed=claimed,
                          dispatched=claimed, handler_id=id(handler) if handler is not None else None)
                    if lease is not None and not claimed:
                        release_trace(
                            item, 'ADMISSION_RELEASE_ENTER',
                            lambda: self.admission_scheduler.release(lease, reason='dispatch_failed'))
                        execution_trace(item, 'ADMISSION_RELEASED', reason='dispatch_failed')
                    if (handler is not None and not claimed and
                            self.handler_factory is not None and
                            hasattr(self.handler_factory, 'release')):
                        release_trace(
                            item, 'HANDLER_RELEASE_ENTER',
                            lambda: self.handler_factory.release(handler))
                        execution_trace(item, 'HANDLER_RELEASED', reason='dispatch_failed')
                    if (self.handler_factory is not None and
                            not claimed and hasattr(self.handler_factory, 'release_idle') and
                            not stage_runnable(stage)):
                        self.handler_factory.release_idle(stage)
            return pass_progress

        def run_stage_pass(pass_kind):
            perf.count('SCHEDULER_PASSES_TOTAL')
            with perf.phase('PASS',pass_kind=pass_kind):
                grants = _run_stage_pass(pass_kind)
            perf.count('PASSES_WITH_GRANT' if grants else 'PASSES_WITHOUT_GRANT')
            perf.count('GRANTS',grants)
            if not grants:
                if perf.counters['SOURCE_ADVANCE_CALLS']>previous_pass_activity[0]:perf.count('PASSES_ONLY_SOURCE_ADVANCE')
                elif perf.counters['MARKER_QUANTA']>previous_pass_activity[1]:perf.count('PASSES_ONLY_MARKER_WORK')
            previous_pass_activity[:]=[perf.counters['SOURCE_ADVANCE_CALLS'],perf.counters['MARKER_QUANTA']]
            perf.flush()
            return grants

        def stalled_result(reason):
            diagnostics = {'reason': reason, 'scheduler_run_id': self.owner.get('scheduler_run_id'),
                           'execution_epoch': self.owner.get('execution_epoch'),
                           'pending_by_stage': {}, 'waiting_sources': 0,
                           'last_event_id': None,
                           'no_progress_iterations': no_progress_iterations,
                           'reconciliation_attempts': reconciliation_attempts}
            if reason=='resource_admission_no_progress':
                diagnostics['resource_admission_stall']=True
                diagnostics['blocked_stages']=dict(resource_denials)
            try:
                with connect(self.database, readonly=True) as c:
                    for row in c.execute("SELECT stage,COUNT(*) AS n FROM v2_jobs WHERE status IN ('PENDING','FAILED_RETRYABLE','RUNNING') GROUP BY stage"):
                        diagnostics['pending_by_stage'][row['stage']] = row['n']
                    diagnostics['waiting_sources'] = c.execute("SELECT COUNT(*) FROM v2_source_outcomes WHERE status='WAITING'").fetchone()[0]
                    row = c.execute('SELECT MAX(event_id) FROM v2_outbox').fetchone()
                    diagnostics['last_event_id'] = row[0] if row else None
            except Exception as exc:
                diagnostics['diagnostic_error'] = type(exc).__name__
            trace('scheduler',event='SCHEDULER_STALLED_DIAGNOSTICS',**diagnostics)
            if reason=='resource_admission_no_progress':
                return {'status':'RECOVERABLE','execution_status':'resource_wait',
                    'reason_code':'RESOURCE_ADMISSION_STALL','diagnostics':diagnostics,'searchable':False}
            return {'status': 'SCHEDULER_STALLED', 'execution_status': 'stalled',
                    'reason_code': 'SCHEDULER_STALLED', 'diagnostics': diagnostics,
                    'searchable': False}
        try:
            source_cursor = 0
            source_hint_turn = 0
            first = next_ingest()
            if first is not None and not (lifecycle and lifecycle.state()['stop_requested']):
                future = executor.submit(preprocess_attempt, first)
            iteration = 0
            last_fast_tick=None
            while True:
                if simple:
                    now=time.monotonic()
                    if last_fast_tick is not None:perf.record('FAST_DISPATCH',now-last_fast_tick)
                    last_fast_tick=now
                progress = 0
                progress += collect_finished()
                progress += recover_unstarted()
                stopping = bool(lifecycle and lifecycle.state()['stop_requested'])
                if stopping:
                    # Stop is a safe-boundary handoff, not a final cancellation.
                    # No stage pass, source submission or sweep is allowed here.
                    if future is not None and future.done():
                        admit_rows(future.result())
                        future = None
                    if future is None and not inflight:
                        return {'status':'RECOVERABLE', 'reason_code':'USER_STOP_FOR_RESUME',
                                'lifecycle':lifecycle.summary(), 'searchable':False}
                    measured_sleep(0.02)
                    continue
                if future is not None and future.done():
                    producer_wait_iterations = 0
                    admit_rows(future.result())
                    progress += 1
                    next_source = next_ingest()
                    future = executor.submit(preprocess_attempt, next_source) if next_source is not None else None
                    # Give the next bounded producer a scheduling opportunity
                    # before this thread begins consuming the ready artifact.
                    if future is not None:
                        measured_sleep(0.001)
                if future is None:
                    next_source = next_ingest()
                    if next_source is not None:
                        future = executor.submit(preprocess_attempt, next_source)
                        progress += 1
                before_stage_marker = durable_progress_marker()
                # Stop may be requested while the first event advancement is
                # already inside a cooperative yield.  Treat that race as a
                # safe handoff and return to the existing stop branch instead
                # of allowing the private control exception to become FAILED.
                try:
                    progress += advance_events_fair()
                    progress += run_stage_pass('primary')
                except _CooperativeStopRequested:
                    trace('scheduler', event='STOP_BOUNDARY_REACHED')
                    continue
                except _ProducerRefillRequested:
                    trace('scheduler', event='PRODUCER_REFILL_WAKEUP')
                    continue
                cooperative_revisit_marker = durable_progress_marker()
                cooperative_revisit_armed = True
                advancement_started = time.monotonic()
                returned_for_revisit = False
                try:
                    cooperative_revisit_armed = False
                    progress += advance_events_fair()
                    cooperative_revisit_armed = True
                    # Rotate the starting source. A bounded pass must not
                    # repeatedly visit only the first already-admitted source.
                    count = len(admitted)
                    from .v2_source_affinity import source_advancement_order
                    source_hint_turn += 1
                    hint=getattr(getattr(self.pipeline,'index',None),'_index_source_hint',None)
                    for position,rotate_cursor in source_advancement_order(admitted,source_cursor,hint,source_hint_turn%4!=0):
                        item = admitted[position]
                        if rotate_cursor:source_cursor = (position + 1) % count
                        else:perf.count('INDEX_SOURCE_JOIN_HINT')
                        previous_outcome = item.get('outcome')
                        outcome = advance_source_fair(item['source_id'], item['source_revision'])
                        refresh_source=getattr(lifecycle,'refresh_source',None)
                        if not simple and refresh_source and refresh_source(item['source_id'],item['source_revision']):
                            progress+=1
                        item['outcome'] = outcome
                        # Re-reading an unchanged terminal projection is not
                        # durable progress and must not keep the orchestration
                        # loop alive forever.
                        if outcome != previous_outcome:
                            progress += 1
                        if time.monotonic() - advancement_started >= slice_seconds:
                            returned_for_revisit = True
                            break
                except _CooperativeStopRequested:
                    trace('scheduler', event='STOP_BOUNDARY_REACHED')
                except _ProducerRefillRequested:
                    returned_for_revisit = True
                    trace('scheduler', event='PRODUCER_REFILL_WAKEUP')
                except _CooperativeSchedulerRevisit:
                    returned_for_revisit = True
                    trace('scheduler', event='ADVANCEMENT_RETURNED_TO_SCHEDULER')
                finally:
                    cooperative_revisit_armed = False
                    cooperative_revisit_marker = None
                progress += cooperative_collected
                cooperative_collected = 0
                after_stage_marker = durable_progress_marker()
                if (returned_for_revisit or
                        (after_stage_marker is not None and
                         after_stage_marker != before_stage_marker)):
                    # Durable work may have become runnable after the first
                    # stage pass (for example DOCUMENT_READY -> embedding).
                    # Revisit all stages once in this bounded loop before
                    # completion/stall checks; later progress is handled by
                    # the next outer iteration.
                    progress += run_stage_pass('revisit')
                    if not (lifecycle and lifecycle.state()['stop_requested']):
                        try:
                            progress += advance_events_fair()
                        except _ProducerRefillRequested:
                            continue
                if lifecycle and future is None and not inflight and (coordination is None or coordination.idle()) and (people_lane is None or people_lane.consumer.idle()):
                    summary = lifecycle.summary()
                    if not any(stage_runnable(s) for s in stages):
                        if lifecycle.advance_pass():
                            trace('scheduler', event='PRODUCT_PASS_STARTED', **lifecycle.summary())
                            enqueue_sources(lifecycle.pending_ingest())
                            next_source = next_ingest()
                            future = executor.submit(preprocess_attempt, next_source) if next_source else None
                            iteration = 0
                            continue
                        if summary['complete']:
                            return {'status':'COMPLETE', 'execution_status':'completed',
                                    'sources':admitted, 'lifecycle':summary,
                                    'searchable':summary['successful_count'] > 0}
                if lifecycle is None and future is None and admitted and not inflight and not any(source_queues.values()) and (coordination is None or coordination.idle()) and (people_lane is None or people_lane.consumer.idle()):
                    durable_completion = self._durable_completion_snapshot(stage_runnable)
                    if durable_completion is not None and durable_completion['complete']:
                        trace('scheduler', event='durable_completion', **durable_completion)
                        return {'status': 'COMPLETE', 'execution_status': 'completed',
                            'iterations': iteration + 1, 'sources': admitted,
                            'completion_diagnostics': durable_completion,
                            'searchable': True}
                    # Small injected stores used by legacy unit tests may not
                    # have the V2 current-lineage schema.  Retain the old
                    # fallback only when the durable authority is unavailable.
                    if durable_completion is None and all(
                            item.get('outcome', {}).get('status') == 'COMPLETE' for item in admitted):
                        return {'status': 'COMPLETE', 'execution_status': 'completed',
                            'iterations': iteration + 1, 'sources': admitted,
                            'searchable': True}
                if future is None and not inflight and resource_denials:
                    current_marker=durable_progress_marker()
                    now=time.monotonic()
                    if resource_idle_since is None or current_marker!=resource_idle_marker:
                        resource_idle_since=now
                        resource_idle_marker=current_marker
                    elif now-resource_idle_since>=60:
                        return stalled_result('resource_admission_no_progress')
                else:
                    resource_idle_since=None
                iteration += 1
                if progress:
                    no_progress_iterations = 0
                    diagnostic_started = None
                    diagnostic_dumped = False
                else:
                    no_progress_iterations += 1
                    if inflight:
                        if diagnostic_started is None:
                            diagnostic_started = time.monotonic()
                        if (not diagnostic_dumped and
                                time.monotonic() - diagnostic_started >= 5.0):
                            snapshot_threads()
                            diagnostic_dumped = True
                if future is not None and not future.done():
                    # Yield the GIL to the producer.  Without this backoff a
                    # slow real image/video preprocess can be starved by the
                    # orchestration loop and look like a dead scheduler.
                    producer_wait_iterations += 1
                    if producer_wait_iterations >= max_iterations * 50:
                        return stalled_result('producer_future_no_progress')
                    measured_sleep(0.02)
                    continue
                # Total iterations are not a stall: a large source-level run
                # can legitimately take thousands of productive passes. Only
                # consecutive no-progress passes qualify, after giving every
                # admitted source at least one rotation to advance its join.
                if future is None and no_progress_iterations >= max(max_iterations,len(admitted)):
                    if inflight or (coordination is not None and not coordination.idle()) or (people_lane is not None and not people_lane.consumer.idle()):
                        # Active downstream work is legitimate progress even
                        # when no durable row has changed in this pass.
                        iteration = 0
                        no_progress_iterations = 0
                        measured_sleep(0.005)
                        continue
                    # Reconcile the durable event graph once more before
                    # declaring a stall.  This is deliberately bounded: a
                    # missed wake-up may create fresh runnable jobs here, but
                    # an inconsistent graph must surface diagnostics instead
                    # of spinning forever.
                    reconciliation_attempts += 1
                    recovered = advance_events_fair()
                    runnable = any(stage_runnable(stage) for stage in stages)
                    if recovered or runnable:
                        iteration = 0
                        no_progress_iterations = 0
                        continue
                    return stalled_result('no_progress_after_reconciliation')
                if not progress:
                    measured_sleep(0.005)
        finally:
            if direct_budget is not None:direct_budget.cancel.set()
            try:
                executor.shutdown(wait=True)
                downstream_executor.shutdown(wait=True)
                if coordination is not None:coordination.close()
            finally:
                try:
                    if people_lane is not None:people_lane.close()
                finally:
                    if people_lane is not None:original_pipeline.people=people_lane.people
                    self.pipeline=original_pipeline
                    self.simple_publish_hook=None
                    if coordination is not None:
                        self.store.enqueue_guard=None;self.store.publication_guard=None
                        self.store.fanout_planner=None;self.store.enqueue_capture=None
                    try:perf.flush(force=True)
                    finally:
                        if lifecycle is not None:lifecycle.close()

    def runtime(self):
        """Internal construction checkpoint; never represents processed media."""
        return {'runtime': 'audited_pipeline_v2', 'database': str(self.database),
                'store': self.store, 'pipeline': self.pipeline, 'driver': self}
