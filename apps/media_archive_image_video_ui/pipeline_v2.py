"""Small, bounded event-driven scheduler for per-item pipeline work.

This module is deliberately independent of the legacy stage launcher.  It
provides the safe primitives needed before wiring real media stages: durable
execution keys, dependency events, bounded workers, a single SQLite writer,
leases/heartbeats, retries, pause/resume/cancel and idempotent completion.
The legacy serial orchestrator remains the default until A/B acceptance passes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


V2_CONTRACT = "media_archive_pipeline_v2_event_driven_v1"


@dataclass(frozen=True)
class WorkItem:
    stage_key: str
    item_key: str
    depends_on: tuple[tuple[str, str], ...] = ()
    execution_key: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    max_attempts: int = 2

    @property
    def key(self) -> tuple[str, str]:
        return self.stage_key, self.item_key


@dataclass(frozen=True)
class ResourceBudget:
    workers: int = 1
    queue_size: int = 8
    lease_seconds: float = 120.0

    def validate(self) -> None:
        if self.workers < 1 or self.queue_size < 1 or self.lease_seconds <= 0:
            raise ValueError("invalid_resource_budget")


class PipelineStore:
    """Durable task ledger; all writes are short and serialized by a lock."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS pipeline_v2_items(
                  stage_key TEXT NOT NULL, item_key TEXT NOT NULL,
                  execution_key TEXT NOT NULL, status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0, lease_owner TEXT,
                  lease_until REAL, result_json TEXT, error TEXT,
                  updated_at REAL NOT NULL, PRIMARY KEY(stage_key,item_key)
                );
                CREATE TABLE IF NOT EXISTS pipeline_v2_events(
                  event_id TEXT PRIMARY KEY, stage_key TEXT, item_key TEXT,
                  event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30.0)
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def register(self, item: WorkItem) -> None:
        execution_key = item.execution_key or f"{item.stage_key}:{item.item_key}"
        now = time.time()
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT execution_key FROM pipeline_v2_items WHERE stage_key=? AND item_key=?", item.key).fetchone()
            if existing and existing[0] != execution_key:
                raise ValueError("execution_key_conflict_requires_new_revision")
            con.execute(
                "INSERT OR IGNORE INTO pipeline_v2_items(stage_key,item_key,execution_key,status,updated_at) VALUES(?,?,?,?,?)",
                (item.stage_key, item.item_key, execution_key, "queued", now),
            )

    def status(self, key: tuple[str, str]) -> str | None:
        with self._connect() as con:
            row = con.execute("SELECT status FROM pipeline_v2_items WHERE stage_key=? AND item_key=?", key).fetchone()
        return str(row[0]) if row else None

    def claim(self, item: WorkItem, owner: str, lease_seconds: float) -> bool:
        now = time.time()
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT status,lease_until FROM pipeline_v2_items WHERE stage_key=? AND item_key=?", item.key).fetchone()
            if not row or row[0] == "completed":
                return False
            if row[0] == "running" and float(row[1] or 0) > now:
                return False
            cur = con.execute(
                "UPDATE pipeline_v2_items SET status='running',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE stage_key=? AND item_key=?",
                (owner, now + lease_seconds, now, *item.key),
            )
            return cur.rowcount == 1

    def heartbeat(self, key: tuple[str, str], owner: str, lease_seconds: float) -> bool:
        with self._lock, self._connect() as con:
            cur = con.execute("UPDATE pipeline_v2_items SET lease_until=?,updated_at=? WHERE stage_key=? AND item_key=? AND status='running' AND lease_owner=? AND lease_until>?", (time.time()+lease_seconds, time.time(), *key, owner, time.time()))
            return cur.rowcount == 1

    def finish(self, item: WorkItem, owner: str, result: Any = None, error: str = "") -> None:
        status = "failed" if error else "completed"
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute("UPDATE pipeline_v2_items SET status=?,lease_until=NULL,result_json=?,error=?,updated_at=? WHERE stage_key=? AND item_key=? AND status='running' AND lease_owner=? AND lease_until>?", (status, json.dumps(result, ensure_ascii=False, default=str), error[:4000], time.time(), *item.key, owner, time.time()))
            if cur.rowcount != 1:
                raise RuntimeError("completion_without_lease_ownership")
            con.execute("INSERT INTO pipeline_v2_events VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, item.stage_key, item.item_key, status, json.dumps(result if not error else {"error": error}, ensure_ascii=False, default=str), time.time()))

    def snapshot(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT stage_key,item_key,status,attempts,error FROM pipeline_v2_items ORDER BY stage_key,item_key").fetchall()
        return [dict(zip(("stage_key","item_key","status","attempts","error"), row)) for row in rows]


class EventDrivenScheduler:
    def __init__(self, store: PipelineStore, budgets: Mapping[str, ResourceBudget] | None = None):
        self.store = store
        self.budgets = dict(budgets or {})
        for budget in self.budgets.values():
            budget.validate()
        self._items: dict[tuple[str, str], WorkItem] = {}
        self._paused = threading.Event()
        self._cancelled = threading.Event()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancelled.set()

    def add(self, items: Iterable[WorkItem]) -> None:
        for item in items:
            self._items[item.key] = item
            self.store.register(item)

    def _ready(self, item: WorkItem) -> bool:
        return all(self.store.status(dep) == "completed" for dep in item.depends_on)

    def run(self, handlers: Mapping[str, Callable[[WorkItem], Any]]) -> list[dict[str, Any]]:
        pending = set(self._items)
        futures: dict[Future[Any], tuple[WorkItem, str]] = {}
        active_by_stage: dict[str, int] = {}
        pools = {stage: ThreadPoolExecutor(max_workers=budget.workers, thread_name_prefix=f"v2-{stage}") for stage, budget in self.budgets.items()}
        try:
            while pending or futures:
                if self._cancelled.is_set():
                    break
                if not self._paused.is_set():
                    for key in list(pending):
                        item = self._items[key]
                        if self.store.status(item.key) == "completed":
                            pending.remove(key)
                            continue
                        if not self._ready(item) or item.stage_key not in handlers:
                            continue
                        budget = self.budgets.get(item.stage_key, ResourceBudget())
                        # ThreadPoolExecutor's private queue is unbounded.  Do
                        # not submit beyond the explicit worker + queue budget;
                        # pending items remain in our durable ledger instead.
                        if active_by_stage.get(item.stage_key, 0) >= budget.workers + budget.queue_size:
                            continue
                        owner = f"{item.stage_key}-{uuid.uuid4().hex[:8]}"
                        if self.store.claim(item, owner, budget.lease_seconds):
                            future = pools.setdefault(item.stage_key, ThreadPoolExecutor(max_workers=budget.workers, thread_name_prefix=f"v2-{item.stage_key}")).submit(handlers[item.stage_key], item)
                            futures[future] = (item, owner)
                            active_by_stage[item.stage_key] = active_by_stage.get(item.stage_key, 0) + 1
                            pending.remove(key)
                for future in list(futures):
                    if not future.done():
                        continue
                    item, owner = futures.pop(future)
                    active_by_stage[item.stage_key] = max(0, active_by_stage.get(item.stage_key, 1) - 1)
                    try:
                        self.store.finish(item, owner, result=future.result())
                    except Exception as exc:  # retry is bounded and durable
                        if item.max_attempts > 1 and self.store.status(item.key) != "completed":
                            item = WorkItem(item.stage_key, item.item_key, item.depends_on, item.execution_key, item.payload, item.max_attempts - 1)
                            self._items[item.key] = item
                            pending.add(item.key)
                        self.store.finish(item, owner, error=f"{type(exc).__name__}: {exc}")
                if pending or futures:
                    time.sleep(0.01)
        finally:
            for pool in pools.values():
                pool.shutdown(wait=True, cancel_futures=self._cancelled.is_set())
        return self.store.snapshot()
