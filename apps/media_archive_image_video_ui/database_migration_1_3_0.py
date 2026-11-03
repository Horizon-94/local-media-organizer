"""Explicit, restart-safe product migration for the 1.3.0 V2 schema.

The V2 modules retain defensive table creation for isolated fixture callers.
Product opening/upgrading must use :func:`migrate_to_1_3_0`, which records a
durable migration state and validates the complete schema before completion.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from .central_database import SCHEMA_VERSION, connect, ensure_schema


PRODUCT_SCHEMA_VERSION = 130
MIGRATION_ID = "media_archive_pipeline_v2_1_3_0"
MIGRATION_STATE_IN_PROGRESS = "IN_PROGRESS"
MIGRATION_STATE_COMPLETE = "COMPLETE"
SQL_PATH = Path(__file__).resolve().parents[2] / "migrations/20261002_pipeline_v2_1_3_0.sql"


class MigrationError(RuntimeError):
    """A database cannot be safely opened as a 1.3.0 database."""


REQUIRED_TABLES = {
    "v2_sources", "v2_source_metadata", "v2_active_sources", "v2_source_leases",
    "v2_source_outcomes", "v2_source_closure", "v2_sample_slots", "v2_artifacts",
    "v2_artifact_mappings", "v2_decode_attempts", "v2_outbox", "v2_inbox",
    "v2_deliveries", "v2_jobs", "v2_job_dependencies", "v2_retry_requests",
    "v2_selection_contexts", "v2_snapshot_evidence", "v2_canonical",
    "v2_selection_snapshots", "v2_selected_candidates", "v2_documents",
    "v2_audio_branches", "v2_fusion", "v2_final_projection_heads",
    "v2_final_projection_documents", "v2_vectors", "v2_index_coverage",
    "v2_text_fts", "v2_person_observations", "v2_observation_heads", "v2_tracklets",
    "v2_tracklet_membership", "v2_source_people", "v2_source_face_anchors",
    "v2_tracklet_people", "v2_global_people", "v2_global_memberships",
    "v2_person_aliases", "v2_person_decisions", "v2_person_constraints",
    "v2_person_suggestions", "v2_manual_observation_authority",
    "v2_person_review_observations", "v2_person_decision_results",
    "scheduler_project_defaults", "scheduler_runs",
}
REQUIRED_VIEWS = {"v2_current_jobs", "v2_searchable_documents"}
REQUIRED_INDEXES = {
    "v2_dependency_parent", "v2_people_source", "v2_fusion_timeline",
    "v2_vectors_namespace", "v2_sources_revision", "v2_jobs_source_status",
    "v2_sample_slots_status", "v2_documents_source_status", "v2_index_coverage_status",
    "scheduler_one_active_run_per_task", "scheduler_runs_task_epoch", "scheduler_runs_task_status",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _meta(con: sqlite3.Connection, key: str) -> str | None:
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_schema_meta'"
    ).fetchone():
        return None
    columns = {str(row[1]) for row in con.execute('PRAGMA table_info("app_schema_meta")')}
    if not {"meta_key", "meta_value"}.issubset(columns):
        raise MigrationError("corrupt_schema:app_schema_meta_columns")
    row = con.execute("SELECT meta_value FROM app_schema_meta WHERE meta_key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def inspect_migration_state(database: Path) -> dict[str, Any]:
    database = Path(database).expanduser().resolve()
    if not database.exists() or database.stat().st_size == 0:
        return {"state": "LEGACY_EMPTY", "schema_version": None, "product_schema_version": None}
    try:
        with connect(database, readonly=True) as con:
            quick = con.execute("PRAGMA quick_check").fetchone()
            if not quick or str(quick[0]).lower() != "ok":
                raise MigrationError("corrupt_schema:quick_check")
            schema = _meta(con, "schema_version")
            product = _meta(con, "product_schema_version")
            state = _meta(con, "product_migration_state")
            migration_id = _meta(con, "product_migration_id")
    except MigrationError:
        raise
    except (sqlite3.DatabaseError, OSError) as exc:
        raise MigrationError(f"corrupt_schema:{type(exc).__name__}") from exc
    try:
        schema_number = int(schema) if schema is not None else None
        product_number = int(product) if product is not None else None
    except ValueError as exc:
        raise MigrationError("corrupt_schema:non_numeric_version") from exc
    if schema_number is not None and schema_number > SCHEMA_VERSION:
        raise MigrationError(f"unsupported_newer_schema:{schema_number}")
    if product_number is not None and product_number > PRODUCT_SCHEMA_VERSION:
        raise MigrationError(f"unsupported_newer_product_schema:{product_number}")
    if migration_id and migration_id != MIGRATION_ID and product_number == PRODUCT_SCHEMA_VERSION:
        raise MigrationError(f"unsupported_product_migration:{migration_id}")
    if product_number == PRODUCT_SCHEMA_VERSION and state == MIGRATION_STATE_COMPLETE:
        phase = "COMPLETE"
    elif state == MIGRATION_STATE_IN_PROGRESS:
        phase = "IN_PROGRESS"
    else:
        phase = "LEGACY"
    return {
        "state": phase,
        "schema_version": schema_number,
        "product_schema_version": product_number,
        "migration_id": migration_id or "",
    }


def _write_state(database: Path, state: str) -> None:
    with connect(database) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS app_schema_meta(
                meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
        )
        now = _now()
        values = {
            "product_schema_version": str(PRODUCT_SCHEMA_VERSION),
            "product_migration_id": MIGRATION_ID,
            "product_migration_state": state,
            "product_migration_updated_at": now,
        }
        for key, value in values.items():
            con.execute(
                "INSERT INTO app_schema_meta(meta_key,meta_value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value,updated_at=excluded.updated_at",
                (key, value, now),
            )


def _schema_objects(con: sqlite3.Connection) -> dict[str, set[str]]:
    rows = con.execute(
        "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    objects = {"table": set(), "view": set(), "index": set()}
    for kind, name in rows:
        if kind in objects:
            objects[kind].add(str(name))
    return objects


def _reference_schema_contract() -> dict[str, Any]:
    """Build the expected V2 shape from the authoritative migration SQL.

    Using an isolated SQLite reference schema keeps this contract complete as
    the migration grows, while validation still compares the opened product
    database's real columns, views, primary keys and foreign keys.
    """
    reference = sqlite3.connect(":memory:")
    try:
        reference.execute("PRAGMA foreign_keys=ON")
        reference.executescript(SQL_PATH.read_text(encoding="utf-8"))
        contract: dict[str, Any] = {"columns": {}, "view_columns": {}, "primary_keys": {}, "foreign_keys": {}, "indexes": {}}
        for table in REQUIRED_TABLES:
            contract["columns"][table] = {
                str(row[1]) for row in reference.execute(f'PRAGMA table_info("{table}")')
            }
            contract["primary_keys"][table] = tuple(
                row[1] for row in sorted(
                    (row for row in reference.execute(f'PRAGMA table_info("{table}")') if row[5]),
                    key=lambda row: row[5],
                )
            )
            contract["foreign_keys"][table] = {
                (str(row[3]), str(row[2]), str(row[4]))
                for row in reference.execute(f'PRAGMA foreign_key_list("{table}")')
            }
        for view in REQUIRED_VIEWS:
            contract["view_columns"][view] = tuple(
                str(row[1]) for row in reference.execute(f'PRAGMA table_info("{view}")')
            )
        for table in REQUIRED_TABLES:
            for row in reference.execute(f'PRAGMA index_list("{table}")'):
                name, unique, partial = str(row[1]), bool(row[2]), bool(row[4])
                if name not in REQUIRED_INDEXES:
                    continue
                columns = tuple(r[2] for r in sorted(
                    reference.execute(f'PRAGMA index_info("{name}")').fetchall(), key=lambda item: item[0]))
                sql = reference.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
                ).fetchone()[0] or ''
                where = sql.upper().split(' WHERE ', 1)[1].strip() if ' WHERE ' in sql.upper() else ''
                contract["indexes"][name] = {
                    "table": table, "unique": unique, "partial": partial,
                    "columns": columns, "where": ' '.join(where.split()),
                }
        return contract
    finally:
        reference.close()


REQUIRED_COLUMNS = {
    'scheduler_runs': {'scheduler_run_id', 'task_id', 'scheduler_mode', 'execution_epoch', 'pipeline_contract_version', 'status'},
    'v2_sources': {'source_id', 'revision', 'policy_hash', 'policy_json', 'eos'},
    'v2_source_metadata': {'source_id', 'source_revision', 'content_revision', 'source_sha', 'path', 'metadata'},
    'v2_jobs': {'execution_key', 'stage', 'item_id', 'revision', 'source_id', 'source_revision', 'status'},
    'v2_outbox': {'event_id', 'aggregate_id', 'event_type', 'payload', 'delivered'},
    'v2_documents': {'document_id', 'source_id', 'source_revision', 'document_type', 'text', 'status'},
    'v2_vectors': {'entity_id', 'namespace', 'vector_json', 'input_digest'},
}


def _validate_schema_shape(con: sqlite3.Connection) -> None:
    objects = _schema_objects(con)
    missing_tables = sorted(REQUIRED_TABLES - objects['table'])
    missing_views = sorted(REQUIRED_VIEWS - objects['view'])
    missing_indexes = sorted(REQUIRED_INDEXES - objects['index'])
    malformed = {}
    contract = _reference_schema_contract()
    for table, required in contract["columns"].items():
        if table not in objects['table']:
            continue
        columns = {str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')}
        absent = sorted(required - columns)
        primary_key = tuple(
            row[1] for row in sorted(
                (row for row in con.execute(f'PRAGMA table_info("{table}")') if row[5]),
                key=lambda row: row[5],
            )
        )
        foreign_keys = {
            (str(row[3]), str(row[2]), str(row[4]))
            for row in con.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        expected_foreign_keys = contract["foreign_keys"][table]
        if absent or primary_key != contract["primary_keys"][table] or not expected_foreign_keys <= foreign_keys:
            malformed[table] = {
                "missing_columns": absent,
                "primary_key": {"expected": contract["primary_keys"][table], "actual": primary_key},
                "missing_foreign_keys": sorted(expected_foreign_keys - foreign_keys),
            }
    malformed_views = {}
    for view, expected in contract["view_columns"].items():
        if view not in objects['view']:
            continue
        actual = tuple(str(row[1]) for row in con.execute(f'PRAGMA table_info("{view}")'))
        # The runtime migration may extend a view with identity columns; the
        # contract requires the authoritative columns and ordering, while
        # permitting those additive runtime columns.
        if tuple(actual[:len(expected)]) != expected:
            malformed_views[view] = {"expected": expected, "actual": actual}
    malformed_indexes = {}
    for name, expected in contract["indexes"].items():
        actual = None
        for table in objects['table']:
            row = con.execute(f'PRAGMA index_list("{table}")').fetchall()
            match = next((item for item in row if item[1] == name), None)
            if match is None:
                continue
            info = tuple(r[2] for r in sorted(
                con.execute(f'PRAGMA index_info("{name}")').fetchall(), key=lambda item: item[0]))
            sql = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()[0] or ''
            upper_sql = sql.upper()
            where = upper_sql.split(' WHERE ', 1)[1].strip() if ' WHERE ' in upper_sql else ''
            actual = {
                "table": table, "unique": bool(match[2]), "partial": bool(match[4]),
                "columns": info, "where": ' '.join(where.split()),
            }
            break
        if actual != expected:
            malformed_indexes[name] = {"expected": expected, "actual": actual}
    if missing_tables or missing_views or missing_indexes or malformed or malformed_views or malformed_indexes:
        raise MigrationError(json.dumps({
            'missing_tables': missing_tables, 'missing_views': missing_views,
            'missing_indexes': missing_indexes, 'malformed_tables': malformed,
            'malformed_views': malformed_views,
            'malformed_indexes': malformed_indexes,
        }, sort_keys=True))
    quick = con.execute('PRAGMA quick_check').fetchone()
    if not quick or str(quick[0]).lower() != 'ok':
        raise MigrationError('post_migration_quick_check_failed')
    foreign = con.execute('PRAGMA foreign_key_check').fetchall()
    if foreign:
        raise MigrationError(f'post_migration_foreign_key_check_failed:{len(foreign)}')


def validate_1_3_0_schema(database: Path) -> dict[str, Any]:
    with connect(Path(database), readonly=True) as con:
        state = _meta(con, "product_migration_state")
        version = _meta(con, "product_schema_version")
        if state != MIGRATION_STATE_COMPLETE or version != str(PRODUCT_SCHEMA_VERSION):
            raise MigrationError("migration_incomplete")
        _validate_schema_shape(con)
    return {
        "schema_version": SCHEMA_VERSION,
        "product_schema_version": PRODUCT_SCHEMA_VERSION,
        "migration_id": MIGRATION_ID,
        "state": MIGRATION_STATE_COMPLETE,
        "table_count": len(REQUIRED_TABLES),
        "view_count": len(REQUIRED_VIEWS),
        "index_count": len(REQUIRED_INDEXES),
    }


def migrate_to_1_3_0(
    database: Path,
    *,
    task: Mapping[str, Any] | None = None,
    backup_dir: Path | None = None,
    fail_after_sql: bool = False,
) -> dict[str, Any]:
    """Upgrade one isolated project database to the 1.3.0 product schema."""
    database = Path(database).expanduser().resolve()
    before = inspect_migration_state(database)
    if before["state"] == "COMPLETE":
        result = {"migration_applied": False, **validate_1_3_0_schema(database)}
        from .v2_product_lifecycle import migrate_lifecycle
        migrate_lifecycle(database)
        return result

    # The marker is deliberately committed before the DDL.  If the process is
    # interrupted after this point, the next open sees IN_PROGRESS and reruns
    # the idempotent SQL instead of mistaking a partial DB for a complete one.
    _write_state(database, MIGRATION_STATE_IN_PROGRESS)
    try:
        ensure_schema(database, task=task, backup_dir=backup_dir)
        sql = SQL_PATH.read_text(encoding="utf-8")
        with connect(database) as con:
            con.executescript(sql)
            if fail_after_sql:
                raise RuntimeError("migration_test_interrupted_after_sql")
            # Structural and integrity validation must succeed while the
            # durable marker is still IN_PROGRESS.
            _validate_schema_shape(con)
        _write_state(database, MIGRATION_STATE_COMPLETE)
        result = validate_1_3_0_schema(database)
        from .v2_product_lifecycle import migrate_lifecycle
        migrate_lifecycle(database)
        result["migration_applied"] = True
        return result
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f'migration_sql_failed:{exc}') from exc
    except Exception:
        # Preserve the durable IN_PROGRESS state.  The DB is safe to retry;
        # callers must not treat it as a usable 1.3.0 database until rerun.
        raise
