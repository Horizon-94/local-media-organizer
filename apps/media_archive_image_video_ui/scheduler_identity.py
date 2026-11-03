"""Durable 1.3.0 scheduler ownership and execution identity contract.

This module owns product identity state only. It does not select an
orchestrator, switch modes, or execute pipeline stages; those are later
integration milestones.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .central_database import connect
from .database_migration_1_3_0 import PRODUCT_SCHEMA_VERSION


LEGACY_BATCH = "legacy_batch"
EVENT_DRIVEN_V2 = "event_driven_v2"
VALID_SCHEDULER_MODES = frozenset({LEGACY_BATCH, EVENT_DRIVEN_V2})
PIPELINE_CONTRACT_VERSION = "media_archive_pipeline_contract_1_3_0_v1"
ACTIVE_RUN_STATUSES = frozenset({"STARTING", "RUNNING", "RECOVERABLE"})
TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "FAILED", "ABORTED", "CANCELLED"})


class SchedulerIdentityError(RuntimeError):
    pass


class InvalidSchedulerMode(SchedulerIdentityError):
    pass


class SchedulerOwnershipError(SchedulerIdentityError):
    pass


SCHEDULER_IDENTITY_SQL = """
CREATE TABLE IF NOT EXISTS scheduler_task_files (
    task_id TEXT PRIMARY KEY,
    task_path TEXT NOT NULL UNIQUE,
    configuration_json TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scheduler_project_defaults (
    task_id TEXT PRIMARY KEY,
    scheduler_mode TEXT NOT NULL CHECK(scheduler_mode IN ('legacy_batch','event_driven_v2')),
    pipeline_contract_version TEXT NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scheduler_runs (
    scheduler_run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    scheduler_mode TEXT NOT NULL CHECK(scheduler_mode IN ('legacy_batch','event_driven_v2')),
    execution_epoch INTEGER NOT NULL CHECK(execution_epoch > 0),
    pipeline_contract_version TEXT NOT NULL,
    product_schema_version INTEGER NOT NULL,
    source_processing_revision TEXT NOT NULL DEFAULT '',
    request_id TEXT UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('STARTING','RUNNING','RECOVERABLE','COMPLETED','FAILED','ABORTED','CANCELLED')),
    created_at REAL NOT NULL,
    started_at REAL NOT NULL,
    recovered_at REAL,
    finished_at REAL,
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS scheduler_one_active_run_per_task
    ON scheduler_runs(task_id) WHERE status IN ('STARTING','RUNNING','RECOVERABLE');
CREATE INDEX IF NOT EXISTS scheduler_runs_task_epoch
    ON scheduler_runs(task_id, execution_epoch);
CREATE INDEX IF NOT EXISTS scheduler_runs_task_status
    ON scheduler_runs(task_id, status);
CREATE TABLE IF NOT EXISTS scheduler_switches (
    switch_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    source_run_id TEXT,
    source_mode TEXT,
    source_epoch INTEGER,
    target_run_id TEXT,
    target_mode TEXT NOT NULL CHECK(target_mode IN ('legacy_batch','event_driven_v2')),
    target_epoch INTEGER,
    state TEXT NOT NULL CHECK(state IN ('PREPARING','FENCED','TARGET_CREATED','COMPLETE','FAILED')),
    reuse_summary TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS scheduler_switches_task_state
    ON scheduler_switches(task_id, state, updated_at);
CREATE TABLE IF NOT EXISTS scheduler_run_coverage (
    target_run_id TEXT NOT NULL,
    result_key TEXT NOT NULL,
    coverage_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    source_run_id TEXT,
    contract_fingerprint TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(target_run_id, result_key, coverage_kind),
    FOREIGN KEY(target_run_id) REFERENCES scheduler_runs(scheduler_run_id) ON DELETE CASCADE
);
"""


def _now() -> float:
    return time.time()


def _validate_mode(mode: str) -> str:
    value = str(mode or "").strip()
    if value not in VALID_SCHEDULER_MODES:
        raise InvalidSchedulerMode(f"unsupported_scheduler_mode:{value or '<empty>'}")
    return value


def ensure_scheduler_identity_schema(database: Path) -> None:
    with connect(Path(database)) as con:
        con.executescript(SCHEDULER_IDENTITY_SQL)


def _require_product_schema(con: sqlite3.Connection) -> None:
    rows = {
        str(row[0]): str(row[1])
        for row in con.execute(
            "SELECT meta_key,meta_value FROM app_schema_meta "
            "WHERE meta_key IN ('product_schema_version','product_migration_state')"
        )
    }
    if rows.get("product_schema_version") != str(PRODUCT_SCHEMA_VERSION) or rows.get("product_migration_state") != "COMPLETE":
        raise SchedulerIdentityError("product_schema_not_ready")


def _require_task(con: sqlite3.Connection, task_id: str) -> None:
    if not con.execute("SELECT 1 FROM archive_tasks WHERE task_id=?", (task_id,)).fetchone():
        raise SchedulerIdentityError(f"project_not_found:{task_id}")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _load_run(con: sqlite3.Connection, scheduler_run_id: str) -> dict[str, Any]:
    row = con.execute(
        "SELECT * FROM scheduler_runs WHERE scheduler_run_id=?", (scheduler_run_id,)
    ).fetchone()
    if row is None:
        raise SchedulerOwnershipError(f"scheduler_run_not_found:{scheduler_run_id}")
    return dict(row)


def get_project_scheduler_default(database: Path, task_id: str) -> str:
    """Return the explicit project default, or legacy for every old project."""
    ensure_scheduler_identity_schema(database)
    with connect(Path(database), readonly=True) as con:
        _require_product_schema(con)
        _require_task(con, task_id)
        row = con.execute(
            "SELECT scheduler_mode FROM scheduler_project_defaults WHERE task_id=?", (task_id,)
        ).fetchone()
        return _validate_mode(row[0]) if row else LEGACY_BATCH


def set_project_scheduler_default(database: Path, task_id: str, scheduler_mode: str) -> dict[str, Any]:
    mode = _validate_mode(scheduler_mode)
    ensure_scheduler_identity_schema(database)
    now = _now()
    with connect(Path(database)) as con:
        con.execute("BEGIN IMMEDIATE")
        _require_product_schema(con)
        _require_task(con, task_id)
        con.execute(
            """INSERT INTO scheduler_project_defaults(task_id,scheduler_mode,pipeline_contract_version,updated_at)
               VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET scheduler_mode=excluded.scheduler_mode,
               pipeline_contract_version=excluded.pipeline_contract_version,updated_at=excluded.updated_at""",
            (task_id, mode, PIPELINE_CONTRACT_VERSION, now),
        )
    return {"task_id": task_id, "scheduler_mode": mode, "pipeline_contract_version": PIPELINE_CONTRACT_VERSION}


def start_scheduler_run(
    database: Path,
    task_id: str,
    *,
    scheduler_mode: str | None = None,
    source_processing_revision: str = "",
    request_id: str | None = None,
    _switch_internal: bool = False,
) -> dict[str, Any]:
    """Create one immutable run identity under a single active-task owner."""
    ensure_scheduler_identity_schema(database)
    with connect(Path(database)) as con:
        con.execute("BEGIN IMMEDIATE")
        _require_product_schema(con)
        _require_task(con, task_id)
        if not _switch_internal and con.execute(
            "SELECT 1 FROM scheduler_switches WHERE task_id=? AND state IN ('PREPARING','FENCED','TARGET_CREATED')",
            (task_id,),
        ).fetchone():
            raise SchedulerOwnershipError("scheduler_switch_in_progress")
        if request_id:
            old = con.execute(
                "SELECT * FROM scheduler_runs WHERE task_id=? AND request_id=?",
                (task_id, request_id),
            ).fetchone()
            if old is not None:
                existing = dict(old)
                requested = _validate_mode(scheduler_mode) if scheduler_mode else existing["scheduler_mode"]
                if requested != existing["scheduler_mode"]:
                    raise SchedulerOwnershipError("request_id_mode_conflict")
                return existing
        mode = _validate_mode(scheduler_mode) if scheduler_mode else _project_default_in_transaction(con, task_id)
        active = con.execute(
            "SELECT scheduler_run_id FROM scheduler_runs WHERE task_id=? AND status IN ('STARTING','RUNNING','RECOVERABLE')",
            (task_id,),
        ).fetchone()
        if active is not None:
            raise SchedulerOwnershipError(f"active_scheduler_run_exists:{active[0]}")
        epoch = int(con.execute(
            "SELECT COALESCE(MAX(execution_epoch),0)+1 FROM scheduler_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0])
        now = _now()
        run_id = uuid.uuid4().hex
        con.execute(
            """INSERT INTO scheduler_runs(
                scheduler_run_id,task_id,scheduler_mode,execution_epoch,pipeline_contract_version,
                product_schema_version,source_processing_revision,request_id,status,created_at,started_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, task_id, mode, epoch, PIPELINE_CONTRACT_VERSION, PRODUCT_SCHEMA_VERSION,
             str(source_processing_revision or ""), request_id, "RUNNING", now, now),
        )
        return _load_run(con, run_id)


def _project_default_in_transaction(con: sqlite3.Connection, task_id: str) -> str:
    row = con.execute(
        "SELECT scheduler_mode FROM scheduler_project_defaults WHERE task_id=?", (task_id,)
    ).fetchone()
    return _validate_mode(row[0]) if row else LEGACY_BATCH


def load_scheduler_run(database: Path, scheduler_run_id: str) -> dict[str, Any]:
    ensure_scheduler_identity_schema(database)
    with connect(Path(database), readonly=True) as con:
        _require_product_schema(con)
        return _load_run(con, scheduler_run_id)


def load_active_scheduler_run(database: Path, task_id: str) -> dict[str, Any] | None:
    ensure_scheduler_identity_schema(database)
    with connect(Path(database), readonly=True) as con:
        _require_product_schema(con)
        _require_task(con, task_id)
        return _row(con.execute(
            "SELECT * FROM scheduler_runs WHERE task_id=? AND status IN ('STARTING','RUNNING','RECOVERABLE')",
            (task_id,),
        ).fetchone())


def validate_run_ownership(
    database: Path, scheduler_run_id: str, *, scheduler_mode: str, execution_epoch: int,
) -> dict[str, Any]:
    mode = _validate_mode(scheduler_mode)
    ensure_scheduler_identity_schema(database)
    with connect(Path(database), readonly=True) as con:
        _require_product_schema(con)
        run = _load_run(con, scheduler_run_id)
        if run["scheduler_mode"] != mode:
            raise SchedulerOwnershipError("scheduler_mode_mismatch")
        if int(run["execution_epoch"]) != int(execution_epoch):
            raise SchedulerOwnershipError("execution_epoch_mismatch")
        if run["status"] not in ACTIVE_RUN_STATUSES:
            raise SchedulerOwnershipError(f"scheduler_run_not_active:{run['status']}")
        return run


def resume_scheduler_run(
    database: Path, scheduler_run_id: str, *, scheduler_mode: str, execution_epoch: int,
) -> dict[str, Any]:
    mode = _validate_mode(scheduler_mode)
    ensure_scheduler_identity_schema(database)
    with connect(Path(database)) as con:
        con.execute("BEGIN IMMEDIATE")
        _require_product_schema(con)
        run = _load_run(con, scheduler_run_id)
        if run["scheduler_mode"] != mode:
            raise SchedulerOwnershipError("scheduler_mode_mismatch")
        if int(run["execution_epoch"]) != int(execution_epoch):
            raise SchedulerOwnershipError("execution_epoch_mismatch")
        if run["status"] not in ACTIVE_RUN_STATUSES:
            raise SchedulerOwnershipError(f"scheduler_run_not_recoverable:{run['status']}")
        con.execute(
            "UPDATE scheduler_runs SET recovered_at=CASE WHEN status='RECOVERABLE' THEN ? ELSE recovered_at END,status='RUNNING' WHERE scheduler_run_id=?",
            (_now(), scheduler_run_id),
        )
        return _load_run(con, scheduler_run_id)


def mark_scheduler_run_recoverable(
    database: Path, scheduler_run_id: str, *, scheduler_mode: str, execution_epoch: int,
) -> dict[str, Any]:
    run = validate_run_ownership(database, scheduler_run_id, scheduler_mode=scheduler_mode, execution_epoch=execution_epoch)
    with connect(Path(database)) as con:
        con.execute(
            "UPDATE scheduler_runs SET status='RECOVERABLE' WHERE scheduler_run_id=? AND status IN ('STARTING','RUNNING')",
            (scheduler_run_id,),
        )
    run["status"] = "RECOVERABLE"
    return run


def finish_scheduler_run(
    database: Path, scheduler_run_id: str, *, scheduler_mode: str, execution_epoch: int,
    status: str = "COMPLETED",
) -> dict[str, Any]:
    if status not in TERMINAL_RUN_STATUSES:
        raise SchedulerIdentityError(f"invalid_terminal_status:{status}")
    mode = _validate_mode(scheduler_mode)
    with connect(Path(database)) as con:
        con.execute('BEGIN IMMEDIATE')
        run = _load_run(con, scheduler_run_id)
        if run['scheduler_mode'] != mode or int(run['execution_epoch']) != int(execution_epoch):
            raise SchedulerOwnershipError('scheduler_terminal_identity_mismatch')
        if run['status'] not in ACTIVE_RUN_STATUSES:
            raise SchedulerOwnershipError('scheduler_run_not_active:' + run['status'])
        con.execute(
            "UPDATE scheduler_runs SET status=?,finished_at=? WHERE scheduler_run_id=?",
            (status, _now(), scheduler_run_id),
        )
        return _load_run(con, scheduler_run_id)
