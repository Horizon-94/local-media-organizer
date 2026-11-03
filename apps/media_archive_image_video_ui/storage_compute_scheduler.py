"""Evidence-driven storage and compute admission for the V2 event pipeline.

The scheduler deliberately owns admission only.  Durable media processing,
epoch fencing and result provenance remain the responsibility of the audited
V2 runtime.  A storage lease ends when a validated SSD derivative is
published; downstream jobs therefore never retain an original-media lease.
"""
from __future__ import annotations

import json
import math
import os
import plistlib
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from .v2_heavy_admission import HeavyAdmissionController


class AdmissionBlocked(RuntimeError):
    """The request is valid but must wait for a resource or fair turn."""

    def __init__(self, reason, **diagnostics):
        super().__init__(reason)
        self.diagnostics = {'blocking_reason': str(reason), **diagnostics}


WORKLOAD_CLASSES = {
    'dhash': 'cheap_visual_cpu', 'quality': 'cheap_visual_cpu',
    'geometry': 'cheap_visual_cpu', 'audio_extract': 'ffmpeg',
    'vad': 'vad_cpu', 'yoloe': 'yoloe', 'openclip': 'openclip',
    'whisper': 'whisper', 'qwen': 'qwen', 'ocr': 'ocr',
    'embedding': 'embedding', 'insightface': 'person',
    'video_source_ingest': 'video_source_ingest',
    'image_source_ingest': 'image_source_ingest',
}


def workload_class(stage):
    return WORKLOAD_CLASSES[stage]  # Never silently route unknown work to FFmpeg.


class StageRoundRobin:
    """Shared-resource cursors persist across passes and move only on grants."""
    def __init__(self, stages, policy, class_for=workload_class):
        self.stages = tuple(stages)
        heavy = policy.get('global', {}).get('heavy_classes', [])
        self.keys = {s: 'global_heavy' if class_for(s) in heavy else class_for(s) for s in stages}
        self.members = {}
        for s in stages:
            self.members.setdefault(self.keys[s], []).append(s)
        self.cursors = {k: 0 for k in self.members}

    def order(self):
        queues = {}
        for key, members in self.members.items():
            n = self.cursors[key]
            queues[key] = iter(members[n:] + members[:n])
        return [next(queues[self.keys[s]]) for s in self.stages]

    def granted(self, stage):
        key = self.keys[stage]
        members = self.members[key]
        self.cursors[key] = (members.index(stage) + 1) % len(members)


class StaleAdmission(RuntimeError):
    """The request belongs to an old scheduler owner/epoch."""


DEFAULT_POLICY = {
    # Same source window as the existing bounded outbox delivery (100).
    # Low-water is half that window; production still has one frozen-adapter
    # source slot. No inference concurrency is changed by this policy.
    "producer": {"source_low_water": 50, "source_high_water": 100,
                 "advancement_slice_seconds": 0.1},
    "storage": {
        "rotational_video_lanes": 3,
        "rotational_image_lanes": 3,
        "other_original_lanes": 1,
        "mixed_policy": "serialize",
        "unknown_source_bucket": "UNKNOWN_ROTATIONAL_SOURCE",
        "hdd_original_lanes": 1,
        "ssd_original_lanes": 3,
        "unknown_original_lanes": 1,
    },
    "compute": {
        "ffmpeg": 1,
        "cheap_visual_cpu": 1,
        "vad_cpu": 1,
        "yoloe": 1,
        "openclip": 1,
        "ocr": 2,
        "embedding": 1,
        "qwen": 1,
        "whisper": 1,
        "person": 1,
    },
    "global": {
        "heavy_lane_limit": 1,
        "heavy_classes": ["qwen", "whisper"],
        "heavy_model_headroom_mb": 3072,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        return json.loads(json.dumps(DEFAULT_POLICY))
    with Path(path).open(encoding="utf-8") as fh:
        return _merge(DEFAULT_POLICY, json.load(fh))


def resolve_physical_device(path: str | Path) -> tuple[str, str]:
    """Return (device bucket, diagnostic), conservatively when unresolved.

    ``diskutil`` is queried only for attribution; ``st_dev`` is retained as a
    stable fallback for non-macOS tests.  Unknown paths are never assumed to be
    different disks.
    """
    path = str(path)
    try:
        proc = subprocess.run(["diskutil", "info", "-plist", path], capture_output=True,
                              check=True, timeout=3)
        info = plistlib.loads(proc.stdout)
        whole = info.get("ParentWholeDisk") or info.get("DeviceIdentifier")
        if whole:
            solid = info.get('SolidState')
            kind = 'ssd' if solid is True else 'hdd' if solid is False else 'unknown'
            return str(whole), 'diskutil:' + kind
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        pass
    try:
        return f"st_dev:{os.stat(path).st_dev}", "stat-fallback"
    except OSError:
        return DEFAULT_POLICY["storage"]["unknown_source_bucket"], "unknown"


@dataclass(frozen=True)
class Admission:
    token_id: str
    task_id: str
    workload_class: str
    resource_key: str | None
    requires_original: bool
    scheduler_run_id: str | None
    execution_epoch: int | None


class StorageComputeScheduler:
    """Small SQLite-backed admission gate with explicit resource boundaries."""

    def __init__(self, database: str | Path, *, policy: dict[str, Any] | None = None,
                 device_resolver: Callable[[str | Path], tuple[str, str]] | None = None,
                 pressure_provider: Callable[[], Any] | None = None):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.policy = _merge(DEFAULT_POLICY, policy or {})
        self.device_resolver = device_resolver or resolve_physical_device
        self.device_kinds = {}
        self.pressure_provider = pressure_provider or (lambda: {"free_percent": 100})
        self.heavy_controller = HeavyAdmissionController({'headroom_mb':self.policy['global']['heavy_model_headroom_mb']})
        self._memory_reservations = {}
        self._admission_lock = threading.RLock()
        self._queue: list[dict[str, Any]] = []
        self.artifact_events: list[dict[str, Any]] = []
        self._fair_turn = 0
        self._init_db()
        self.recover()

    def _connect(self):
        c = sqlite3.connect(self.database, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        with self._connect() as c:
            # GUI snapshots are long read transactions. Rollback journaling
            # makes their shared lock block inference-result commits and even
            # admission diagnostics. WAL preserves SQLite's transaction and
            # ownership barriers while letting these readers coexist safely.
            mode=c.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if mode not in ('wal','memory'):
                raise RuntimeError('scheduler_concurrent_journal_unavailable:'+str(mode))
            c.executescript("""
            CREATE TABLE IF NOT EXISTS scheduler_admissions(
              token_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
              workload_class TEXT NOT NULL, resource_key TEXT,
              requires_original INTEGER NOT NULL, status TEXT NOT NULL,
              scheduler_run_id TEXT, execution_epoch INTEGER,
              acquired_at REAL NOT NULL, released_at REAL, release_reason TEXT,
              derivative_path TEXT, owner TEXT
            );
            CREATE TABLE IF NOT EXISTS scheduler_owner(
              task_id TEXT PRIMARY KEY, scheduler_run_id TEXT NOT NULL,
              execution_epoch INTEGER NOT NULL
            );
            """)

            columns = {r[1] for r in c.execute('PRAGMA table_info(scheduler_admissions)')}
            for name in ('job_type', 'job_id'):
                if name not in columns:
                    c.execute(f'ALTER TABLE scheduler_admissions ADD COLUMN {name} TEXT')
            c.execute('''CREATE TABLE IF NOT EXISTS scheduler_admission_blocks(
              scheduler_run_id TEXT,execution_epoch INTEGER,job_type TEXT,diagnostics TEXT,
              PRIMARY KEY(scheduler_run_id,execution_epoch,job_type))''')

    def recover(self) -> int:
        """Release process-local leases after ordinary restart."""
        self._memory_reservations.clear()
        with self._connect() as c:
            cur = c.execute("UPDATE scheduler_admissions SET status='RELEASED', released_at=?, release_reason='restart' WHERE status='ACTIVE'", (time.time(),))
            return cur.rowcount

    def switch_owner(self, task_id: str, scheduler_run_id: str, execution_epoch: int):
        with self._connect() as c:
            row = c.execute("SELECT * FROM scheduler_owner WHERE task_id=?", (task_id,)).fetchone()
            if row and execution_epoch < row["execution_epoch"]:
                raise StaleAdmission("execution_epoch_regressed")
            if row and (row["scheduler_run_id"] != scheduler_run_id or row["execution_epoch"] != execution_epoch):
                c.execute("UPDATE scheduler_admissions SET status='RELEASED', released_at=?, release_reason='owner_switch' WHERE status='ACTIVE' AND scheduler_run_id=? AND execution_epoch=?",
                          (time.time(), row["scheduler_run_id"], row["execution_epoch"]))
            c.execute("INSERT OR REPLACE INTO scheduler_owner VALUES(?,?,?)", (task_id, scheduler_run_id, execution_epoch))
        if row and (row['scheduler_run_id']!=scheduler_run_id or row['execution_epoch']!=execution_epoch):
            self._memory_reservations.clear()

    def _assert_owner(self, task_id: str, run_id: str | None, epoch: int | None):
        if run_id is None and epoch is None:
            return
        with self._connect() as c:
            row = c.execute("SELECT * FROM scheduler_owner WHERE scheduler_run_id=? AND execution_epoch=?", (run_id, epoch)).fetchone()
        if not row or row["scheduler_run_id"] != run_id or row["execution_epoch"] != epoch:
            raise StaleAdmission("stale_scheduler_admission")

    def _pressure_free(self) -> float:
        value = self.pressure_provider()
        if isinstance(value, dict):
            value = value.get("free_percent", value.get("free_memory_percent", 100))
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def admission_paused(self, workload_class: str) -> bool:
        if workload_class not in self.policy['global']['heavy_classes']:return False
        decision=self.heavy_controller.decide(workload_class,self._memory_sample(),list(self._memory_reservations.values()))
        return bool(decision and decision['reason'])

    def _memory_sample(self):
        sample=self.pressure_provider()
        sample=dict(sample) if isinstance(sample,dict) else {'free_percent':sample}
        # Fixture adapters may supply a percentage only. The production probe
        # supplies measured reclaimable memory and OS pressure separately.
        if 'available_mb' not in sample:
            try:sample['available_mb']=float(sample.get('physical_mb',32768))*float(sample.get('free_percent',0))/100
            except (TypeError,ValueError):sample.update(available_mb=0,healthy=False)
        sample.setdefault('pressure_level',1)
        sample.setdefault('swap_used_mb',0)
        sample.setdefault('healthy',True)
        return sample

    def _active(self, *, resource_key: str | None = None, workload_class: str | None = None):
        clauses = ["status='ACTIVE'"]; args: list[Any] = []
        if resource_key is not None:
            clauses.append("resource_key=?"); args.append(resource_key)
        if workload_class is not None:
            clauses.append("workload_class=?"); args.append(workload_class)
        with self._connect() as c:
            return c.execute("SELECT * FROM scheduler_admissions WHERE " + " AND ".join(clauses), args).fetchall()

    def _storage_capacity(self, resource_key, workload_class):
        kind = self.device_kinds.get(resource_key)
        if kind in ('hdd', 'ssd', 'unknown'):
            return int(self.policy['storage'].get(kind + '_original_lanes', 1))
        key = ('rotational_video_lanes' if workload_class == 'video_source_ingest' else
               'rotational_image_lanes' if workload_class == 'image_source_ingest' else 'other_original_lanes')
        return int(self.policy['storage'].get(key, 1))

    def _storage_allowed(self, resource_key: str, workload_class: str) -> bool:
        active = self._active(resource_key=resource_key)
        original = [r for r in active if r["requires_original"]]
        if self.policy["storage"].get("mixed_policy") == "serialize" and original:
            if any(r["workload_class"] != workload_class for r in original):
                return False
        cap = self._storage_capacity(resource_key, workload_class)
        return sum(1 for r in original if r["workload_class"] == workload_class) < cap

    def _compute_allowed(self, workload_class: str) -> bool:
        cap = int(self.policy["compute"].get(workload_class, 1))
        if len(self._active(workload_class=workload_class)) >= cap:
            return False
        if workload_class in self.policy["global"]["heavy_classes"]:
            heavy = sum(len(self._active(workload_class=kind)) for kind in self.policy["global"]["heavy_classes"])
            if heavy >= int(self.policy["global"]["heavy_lane_limit"]):
                return False
        return True

    def acquire(self, task_id: str, workload_class: str, *, source_path: str | Path | None = None,
                 requires_original: bool = False, scheduler_run_id: str | None = None,
                 execution_epoch: int | None = None, from_derivative: bool = False,
                 job_type: str | None = None, job_id: str | None = None,
                 resident_ready: bool = False) -> Admission:
        with self._admission_lock:
            try:
                return self._acquire(task_id,workload_class,source_path=source_path,
                    requires_original=requires_original,scheduler_run_id=scheduler_run_id,
                    execution_epoch=execution_epoch,from_derivative=from_derivative,
                    job_type=job_type,job_id=job_id,resident_ready=resident_ready)
            except sqlite3.OperationalError as exc:
                code=getattr(exc,'sqlite_errorcode',None)
                busy=(code is not None and (code & 255) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED))
                if not busy:raise
                # The connection context has rolled back any partial grant.
                # Retry through the existing Stop-aware admission wait, not
                # through decode retries or a product-fatal model exception.
                raise AdmissionBlocked('DB_BUSY',requested_job_type=job_type or workload_class,
                    requested_job_id=job_id or task_id,requested_workload_class=workload_class,
                    sqlite_errorcode=code,operation='resource_admission',timestamp=time.time()) from exc

    def _acquire(self, task_id, workload_class, *, source_path, requires_original,
                 scheduler_run_id, execution_epoch, from_derivative, job_type, job_id, resident_ready):
        self._assert_owner(task_id, scheduler_run_id, execution_epoch)
        def blocked(reason, holders, capacity, **extra):
            exc = AdmissionBlocked(reason,
                requested_job_type=job_type or workload_class,
                requested_job_id=job_id or task_id,
                requested_workload_class=workload_class, capacity=capacity,
                current_holder_count=len(holders),
                holder_job_type=[r['job_type'] or r['task_id'] for r in holders],
                holder_job_id=[r['job_id'] or r['task_id'] for r in holders],
                holder_workload_class=[r['workload_class'] for r in holders], timestamp=time.time(), **extra)
            with self._connect() as c:
                c.execute('INSERT OR REPLACE INTO scheduler_admission_blocks VALUES(?,?,?,?)',
                          (scheduler_run_id,execution_epoch,job_type or workload_class,json.dumps(exc.diagnostics,sort_keys=True)))
            raise exc
        memory=self.heavy_controller.decide(workload_class,self._memory_sample(),
            list(self._memory_reservations.values()),resident_ready=resident_ready) if workload_class in self.policy['global']['heavy_classes'] else None
        if memory and memory['reason']:
            blocked(memory['reason'],self._active(),memory['selected_limit'],memory=memory)
        if workload_class in ('yoloe','openclip','ocr','embedding','person'):
            from .v2_runtime_resources import AUXILIARY_MEMORY_MB
            budget=AUXILIARY_MEMORY_MB.get(workload_class,{})
            requested_memory=float(budget.get('warm' if resident_ready else 'cold',0))
            sample=self._memory_sample()
            try:
                available=float(sample.get('available_mb',0))
                level=int(sample['pressure_level']);swap=float(sample['swap_used_mb'])
                valid=sample.get('healthy') and all(math.isfinite(v) and v>=0 for v in (available,swap))
            except (ValueError,TypeError):
                valid=False;available=0.;level=0;swap=0.
            reserved=sum(float(r['memory_mb']) for r in self._memory_reservations.values())
            headroom=float(self.heavy_controller.policy['headroom_mb'])
            growth=self.heavy_controller.observe_swap(self.heavy_controller.clock(),swap) if valid else 0.
            reason=('model_memory_probe_unavailable' if not valid
                else 'model_memory_pressure' if level>=4 or growth>self.heavy_controller.policy['swap_growth_mb_per_minute']
                else 'model_memory_headroom' if available-reserved-requested_memory<headroom else None)
            if reason:blocked(reason,self._active(),0,memory={'available_mb':available,
                'reserved_mb':reserved,'headroom_target_mb':headroom,'pressure_level':level,
                'required_mb':requested_memory,'swap_growth_mb_per_minute':growth})
        resource_key = None
        if requires_original:
            resource_key, device_detail = self.device_resolver(source_path) if source_path is not None else (self.policy["storage"]["unknown_source_bucket"], "missing-path")
            if device_detail.startswith('diskutil:'):
                self.device_kinds[resource_key] = device_detail.split(':', 1)[1]
            elif device_detail in ('missing-path', 'unknown', 'stat-fallback'):
                self.device_kinds[resource_key] = 'unknown'
            if not self._storage_allowed(resource_key, workload_class):
                blocked('storage_capacity', self._active(resource_key=resource_key),
                        self._storage_capacity(resource_key, workload_class))
        elif from_derivative:
            resource_key = "SSD_DERIVATIVE"
        if (not requires_original or workload_class in self.policy['compute']) and not self._compute_allowed(workload_class):
            holders = self._active(workload_class=workload_class)
            capacity = int(self.policy['compute'].get(workload_class, 1))
            if not holders and workload_class in self.policy['global']['heavy_classes']:
                holders = [r for r in self._active() if r['workload_class'] in self.policy['global']['heavy_classes']]
                capacity = int(self.policy['global']['heavy_lane_limit'])
            blocked('compute_capacity', holders, capacity)
        token = Admission(str(uuid.uuid4()), task_id, workload_class, resource_key,
                          requires_original, scheduler_run_id, execution_epoch)
        with self._connect() as c:
            c.execute("INSERT INTO scheduler_admissions(token_id,task_id,workload_class,resource_key,requires_original,status,scheduler_run_id,execution_epoch,acquired_at,owner) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (token.token_id, token.task_id, token.workload_class, token.resource_key, int(token.requires_original), "ACTIVE", token.scheduler_run_id, token.execution_epoch, time.time(), "scheduler"))
            c.execute('UPDATE scheduler_admissions SET job_type=?,job_id=? WHERE token_id=?',
                      (job_type, job_id, token.token_id))
            c.execute('DELETE FROM scheduler_admission_blocks WHERE scheduler_run_id=? AND execution_epoch=? AND job_type=?',
                      (scheduler_run_id,execution_epoch,job_type or workload_class))
        if memory:
            self._memory_reservations[token.token_id]={'stage':workload_class,'memory_mb':memory['required_mb']}
            self.heavy_controller.granted(workload_class)
        elif workload_class in ('yoloe','openclip','ocr','embedding','person') and requested_memory:
            self._memory_reservations[token.token_id]={'stage':workload_class,'memory_mb':requested_memory}
        return token

    def release(self, token: Admission, *, reason: str = "complete", derivative_path: str | Path | None = None):
        with self._admission_lock:
            return self._release(token,reason=reason,derivative_path=derivative_path)

    def continue_heavy_job(self, stage):
        """Keep one admitted resident for a bounded next job, never preempt it."""
        with self._admission_lock:
            sample=self._memory_sample()
            active=list(self._memory_reservations.values())
            decision=self.heavy_controller.decide(stage,sample,active,resident_ready=True)
            if not decision:return False
            if decision['reason'] not in (None,'heavy_selected_capacity'):return False
            count=sum(r['stage']==stage for r in active)
            return (count<=decision['selected_limit'] and
                    decision['available_mb']>=decision['headroom_target_mb']+decision['required_mb'])

    def _release(self, token, *, reason, derivative_path):
        self._assert_owner(token.task_id, token.scheduler_run_id, token.execution_epoch)
        with self._connect() as c:
            row = c.execute("SELECT status FROM scheduler_admissions WHERE token_id=?", (token.token_id,)).fetchone()
            if not row or row["status"] != "ACTIVE":
                self._memory_reservations.pop(token.token_id,None)
                return False
            c.execute("UPDATE scheduler_admissions SET status='RELEASED', released_at=?, release_reason=?, derivative_path=? WHERE token_id=?", (time.time(), reason, str(derivative_path) if derivative_path else None, token.token_id))
        self._memory_reservations.pop(token.token_id,None)
        return True

    def bind_claim(self, token, job):
        """Bind the actual atomically claimed job, correcting any peek race."""
        self._assert_owner(token.task_id, token.scheduler_run_id, token.execution_epoch)
        with self._connect() as c:
            c.execute("UPDATE scheduler_admissions SET job_type=?,job_id=? WHERE token_id=? AND status='ACTIVE'",
                      (job['stage'],job['execution_key'],token.token_id))

    def publish_derivative(self, token: Admission, derivative_path: str | Path):
        if not token.requires_original:
            raise ValueError("derivative_release_requires_original_lease")
        return self.release(token, reason="derivative_ready", derivative_path=derivative_path)

    def submit(self, task_id: str, workload_class: str, *, source_path: str | Path | None = None,
               requires_original: bool = False, scheduler_run_id: str | None = None,
               execution_epoch: int | None = None, priority: int = 0,
               from_derivative: bool = False) -> dict[str, Any]:
        item = {"task_id": task_id, "workload_class": workload_class, "source_path": str(source_path) if source_path else None,
                "requires_original": requires_original, "from_derivative": from_derivative,
                "scheduler_run_id": scheduler_run_id, "execution_epoch": execution_epoch, "priority": priority, "enqueued_at": time.monotonic()}
        self._queue.append(item)
        return item

    def admit_next(self) -> Admission | None:
        """Admit the oldest compatible class with simple aging fairness."""
        if not self._queue:
            return None
        now = time.monotonic()
        ordered = sorted(enumerate(self._queue), key=lambda pair: (-(pair[1]["priority"] + int(now - pair[1]["enqueued_at"])), pair[0]))
        for index, item in ordered:
            try:
                token = self.acquire(**{k: item[k] for k in ("task_id", "workload_class", "source_path", "requires_original", "scheduler_run_id", "execution_epoch")}, from_derivative=item.get("from_derivative", False))
            except (AdmissionBlocked, StaleAdmission):
                continue
            self._queue.pop(index)
            return token
        return None

    def on_artifact_ready(self, token: Admission, artifact_type: str, derivative_path: str | Path,
                          downstream: Iterable[tuple[str, str]] = ()) -> list[Admission]:
        """Publish a derivative, then admit eligible downstream compute jobs."""
        self.publish_derivative(token, derivative_path)
        admitted = []
        for task_id, workload_class in downstream:
            try:
                admitted.append(self.acquire(task_id, workload_class, from_derivative=True,
                                             scheduler_run_id=token.scheduler_run_id, execution_epoch=token.execution_epoch))
            except AdmissionBlocked:
                self.submit(task_id, workload_class, scheduler_run_id=token.scheduler_run_id,
                            execution_epoch=token.execution_epoch)
        return admitted

    def artifact_event(self, source_id: str, artifact_type: str, payload: dict[str, Any]):
        """Observe a durable V2 artifact event without introducing a barrier."""
        self.artifact_events.append({"source_id": source_id, "artifact_type": artifact_type,
                                     "payload": dict(payload), "at": time.time()})

    def active_tokens(self) -> list[Admission]:
        rows = self._active()
        return [Admission(r["token_id"], r["task_id"], r["workload_class"], r["resource_key"], bool(r["requires_original"]), r["scheduler_run_id"], r["execution_epoch"]) for r in rows]
