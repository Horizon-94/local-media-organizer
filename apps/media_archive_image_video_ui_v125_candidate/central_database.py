from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CONTRACT = "media_archive_central_database_v2"
SCHEMA_VERSION = 4
TERMINAL_WORK_ITEM_STATUSES = {"success", "failed", "skipped", "cancelled"}


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS app_schema_meta (
    meta_key TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS archive_tasks (
    task_id TEXT PRIMARY KEY,
    task_name TEXT NOT NULL,
    task_path TEXT NOT NULL,
    source_root TEXT NOT NULL,
    workspace TEXT NOT NULL,
    database_path TEXT NOT NULL,
    software_version TEXT NOT NULL DEFAULT '',
    task_mode TEXT NOT NULL DEFAULT 'full',
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS original_files (
    original_file_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_file_id TEXT NOT NULL,
    source_content_id TEXT NOT NULL,
    absolute_path TEXT NOT NULL,
    relative_path TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT '',
    support_status TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER,
    mtime_ns INTEGER,
    content_sha256 TEXT NOT NULL DEFAULT '',
    fingerprint_status TEXT NOT NULL DEFAULT 'pending',
    fingerprint_error TEXT NOT NULL DEFAULT '',
    canonical_source_file_id TEXT NOT NULL DEFAULT '',
    scan_run_id TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, source_file_id),
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artifact_lineage (
    artifact_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    artifact_path TEXT NOT NULL DEFAULT '',
    artifact_sha256 TEXT NOT NULL DEFAULT '',
    original_file_id TEXT,
    parent_artifact_id TEXT,
    source_content_id TEXT NOT NULL DEFAULT '',
    stage_key TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    contract_version TEXT NOT NULL DEFAULT '',
    pipeline_version TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL DEFAULT '',
    parameters_hash TEXT NOT NULL DEFAULT '',
    output_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'valid',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(task_id, artifact_id),
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE,
    FOREIGN KEY(original_file_id) REFERENCES original_files(original_file_id) ON DELETE SET NULL,
    FOREIGN KEY(task_id,parent_artifact_id) REFERENCES artifact_lineage(task_id,artifact_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS stage_work_items (
    work_item_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    stage_key TEXT NOT NULL,
    item_key TEXT NOT NULL,
    original_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at REAL,
    heartbeat_at REAL,
    started_at REAL,
    finished_at REAL,
    error_code TEXT NOT NULL DEFAULT '',
    error_payload_json TEXT NOT NULL DEFAULT '{}',
    output_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(task_id, stage_key, item_key),
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE,
    FOREIGN KEY(original_file_id) REFERENCES original_files(original_file_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    stage_key TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS search_state (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'NOT_BUILT',
    schema_compatible INTEGER NOT NULL DEFAULT 0,
    database_readable INTEGER NOT NULL DEFAULT 0,
    required_tables_ready INTEGER NOT NULL DEFAULT 0,
    search_preflight_ready INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    verified_at REAL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS search_history (
    query_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    query_text TEXT NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    result_count INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS saved_searches (
    saved_search_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    query_text TEXT NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(task_id, display_name),
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_asset_annotations (
    annotation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_content_id TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    favorite INTEGER NOT NULL DEFAULT 0,
    rating INTEGER NOT NULL DEFAULT 0,
    ignored INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(task_id, source_content_id),
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS person_identity_overrides (
    identity_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    ignored INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(task_id, identity_id),
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS person_identity_cluster_map (
    task_id TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    machine_cluster_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(task_id, machine_cluster_id),
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE,
    FOREIGN KEY(task_id,identity_id) REFERENCES person_identity_overrides(task_id,identity_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS person_identity_visual_map (
    task_id TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    visual_unit_id TEXT NOT NULL,
    source_content_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(task_id, identity_id, visual_unit_id),
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE,
    FOREIGN KEY(task_id,identity_id) REFERENCES person_identity_overrides(task_id,identity_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS storage_inventory (
    inventory_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    path TEXT NOT NULL,
    category TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    safe_to_remove INTEGER NOT NULL DEFAULT 0,
    affects_resume INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT '',
    observed_at REAL NOT NULL,
    UNIQUE(task_id, path),
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_original_files_task_sha ON original_files(task_id, content_sha256);
CREATE INDEX IF NOT EXISTS idx_original_files_fingerprint ON original_files(task_id, fingerprint_status);
CREATE INDEX IF NOT EXISTS idx_artifact_lineage_original ON artifact_lineage(task_id, original_file_id);
CREATE INDEX IF NOT EXISTS idx_artifact_lineage_parent ON artifact_lineage(parent_artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifact_lineage_original_v3 ON artifact_lineage(task_id, original_file_id);
CREATE INDEX IF NOT EXISTS idx_artifact_lineage_parent_v3 ON artifact_lineage(task_id, parent_artifact_id);
CREATE INDEX IF NOT EXISTS idx_stage_work_items_status ON stage_work_items(task_id, stage_key, status);
CREATE INDEX IF NOT EXISTS idx_stage_work_items_lease ON stage_work_items(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_task_events_task_time ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_search_history_task_time ON search_history(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_annotations_task ON user_asset_annotations(task_id, favorite, rating);
CREATE INDEX IF NOT EXISTS idx_person_visual_task_visual ON person_identity_visual_map(task_id, visual_unit_id);
CREATE INDEX IF NOT EXISTS idx_person_visual_task_identity ON person_identity_visual_map(task_id, identity_id);
"""


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _task_value(task: Mapping[str, Any], key: str, default: str = "") -> str:
    return str(task.get(key) or default)


def connect(database: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(database).expanduser().resolve()
    if readonly:
        errors: list[str] = []
        con: sqlite3.Connection | None = None
        for query in ("mode=ro", "mode=ro&immutable=1"):
            try:
                con = sqlite3.connect(
                    f"{path.as_uri()}?{query}", uri=True, timeout=10.0,
                )
                con.execute("PRAGMA query_only=ON")
                con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                break
            except sqlite3.Error as exc:
                errors.append(str(exc))
                if con is not None:
                    con.close()
                con = None
        if con is None:
            raise sqlite3.OperationalError(
                "central_readonly_database_open_failed:" + " | ".join(errors)
            )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def backup_before_migration(database: Path, backup_dir: Path) -> Path | None:
    database = Path(database).expanduser().resolve()
    if not database.is_file() or database.stat().st_size == 0:
        return None
    backup_dir = Path(backup_dir).expanduser().resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"media_archive_before_central_v{SCHEMA_VERSION}_{stamp}.sqlite"
    suffix = 2
    while target.exists():
        target = target.with_name(
            f"media_archive_before_central_v{SCHEMA_VERSION}_{stamp}_{suffix}.sqlite"
        )
        suffix += 1
    source = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30.0)
    destination = sqlite3.connect(str(target), timeout=30.0)
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA quick_check").fetchone()
        if not result or str(result[0]) != "ok":
            raise RuntimeError("central_database_backup_quick_check_failed")
    finally:
        destination.close()
        source.close()
    return target


def _primary_key_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1]) for row in sorted(
            (row for row in con.execute(f'PRAGMA table_info("{table}")') if int(row[5] or 0)),
            key=lambda row: int(row[5]),
        )
    ]


def _prepare_v3_table_migration(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """Rename incompatible unpublished v2 tables; never discard their rows."""
    tables = {
        str(row[0]) for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    expected = {
        "artifact_lineage": ["task_id", "artifact_id"],
        "person_identity_overrides": ["task_id", "identity_id"],
        "person_identity_cluster_map": ["task_id", "machine_cluster_id"],
    }
    renamed: list[tuple[str, str]] = []
    # Keep an unconstrained snapshot, then replace the old table under its
    # original name.  ALTER TABLE ... RENAME would rewrite foreign-key targets
    # in unrelated tables to the legacy name, leaving a valid-looking migration
    # with a broken foreign-key graph.
    for table in (
        "person_identity_cluster_map", "person_identity_overrides", "artifact_lineage",
    ):
        if table not in tables or _primary_key_columns(con, table) == expected[table]:
            continue
        legacy = f"{table}_central_v2_legacy"
        if legacy in tables:
            raise RuntimeError(f"central_v3_legacy_table_already_exists:{legacy}")
        con.execute(f'CREATE TABLE "{legacy}" AS SELECT * FROM "{table}"')
        con.execute(f'DROP TABLE "{table}"')
        renamed.append((table, legacy))
        tables.add(legacy)
    return renamed


def _restore_v2_rows(con: sqlite3.Connection, renamed: Sequence[tuple[str, str]]) -> None:
    for table, legacy in renamed:
        target_columns = {str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')}
        legacy_columns = {str(row[1]) for row in con.execute(f'PRAGMA table_info("{legacy}")')}
        common = sorted(target_columns & legacy_columns)
        if not common:
            continue
        columns = ",".join(f'"{value}"' for value in common)
        con.execute(
            f'INSERT OR IGNORE INTO "{table}" ({columns}) SELECT {columns} FROM "{legacy}"'
        )


def _ensure_index_columns(
    con: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    base_name: str,
) -> None:
    expected = tuple(columns)
    for row in con.execute(f'PRAGMA index_list("{table}")'):
        actual = tuple(
            str(column[2]) for column in con.execute(f'PRAGMA index_info("{row[1]}")')
        )
        if actual == expected:
            return
    name = base_name
    suffix = 2
    while con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone():
        name = f"{base_name}_{suffix}"
        suffix += 1
    quoted = ",".join(f'"{column}"' for column in columns)
    con.execute(f'CREATE INDEX "{name}" ON "{table}" ({quoted})')


def ensure_schema(
    database: Path,
    *,
    task: Mapping[str, Any] | None = None,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    database = Path(database).expanduser().resolve()
    already_current = False
    if database.is_file():
        try:
            with connect(database, readonly=True) as probe:
                row = probe.execute(
                    "SELECT meta_value FROM app_schema_meta WHERE meta_key='schema_version'"
                ).fetchone()
                already_current = bool(row and int(row[0]) >= SCHEMA_VERSION)
        except sqlite3.Error:
            already_current = False
    backup = None
    if not already_current and backup_dir is not None:
        backup = backup_before_migration(database, backup_dir)
    with connect(database) as con:
        # Table replacement during v2 -> v3 must not make SQLite rewrite or
        # enforce stale foreign-key targets.  New connections still default to
        # foreign_keys=ON, and the final schema is audited independently.
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")
        try:
            renamed = _prepare_v3_table_migration(con)
            for statement in SCHEMA_SQL.split(";"):
                if statement.strip():
                    con.execute(statement)
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            if task:
                upsert_task(con, task, database=database)
            _restore_v2_rows(con, renamed)
            _ensure_index_columns(
                con, "artifact_lineage", ("task_id", "original_file_id"),
                "idx_artifact_lineage_original_v3_active",
            )
            _ensure_index_columns(
                con, "artifact_lineage", ("task_id", "parent_artifact_id"),
                "idx_artifact_lineage_parent_v3_active",
            )
            for key, value in {
                "contract": CONTRACT,
                "schema_version": str(SCHEMA_VERSION),
                "updated_at": now,
            }.items():
                con.execute(
                    "INSERT INTO app_schema_meta(meta_key,meta_value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value,updated_at=excluded.updated_at",
                    (key, value, now),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.execute("PRAGMA foreign_keys=ON")
    return {
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "database": str(database),
        "backup": str(backup) if backup else "",
        "migration_applied": not already_current,
    }


def upsert_task(
    con: sqlite3.Connection,
    task: Mapping[str, Any],
    *,
    database: Path | None = None,
) -> str:
    task_id = _task_value(task, "task_id")
    if not task_id:
        raise ValueError("central_database_task_id_missing")
    database_path = Path(database or _task_value(task, "database")).expanduser().resolve()
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    con.execute(
        """INSERT INTO archive_tasks
        (task_id,task_name,task_path,source_root,workspace,database_path,software_version,task_mode,status,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET
          task_name=excluded.task_name,task_path=excluded.task_path,source_root=excluded.source_root,
          workspace=excluded.workspace,database_path=excluded.database_path,
          software_version=excluded.software_version,task_mode=excluded.task_mode,
          status=excluded.status,updated_at=excluded.updated_at""",
        (
            task_id,
            _task_value(task, "name", "未命名素材库"),
            _task_value(task, "task_path"),
            _task_value(task, "source_root"),
            _task_value(task, "workspace"),
            str(database_path),
            _task_value(task, "software_version", _task_value(task, "app_version")),
            _task_value(task, "mode", "full"),
            _task_value(task, "status", "queued"),
            _task_value(task, "created_at", now),
            now,
        ),
    )
    con.execute(
        "INSERT INTO search_state(task_id,status,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(task_id) DO NOTHING",
        (task_id, "NOT_BUILT", time.time()),
    )
    return task_id


def sync_original_files(database: Path, task_id: str) -> dict[str, int]:
    with connect(database) as con:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_file_records'"
        ).fetchone()
        if not exists:
            return {"total": 0, "verified": 0, "pending": 0, "failed": 0}
        con.execute("BEGIN IMMEDIATE")
        try:
            rows = con.execute(
                """SELECT source_file_id,source_content_id,absolute_path,relative_path,
                          media_kind,support_status,size_bytes,mtime_ns,content_sha256,
                          canonical_source_file_id,scan_run_id
                   FROM source_file_records"""
            ).fetchall()
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            for row in rows:
                digest = str(row[8] or "").lower()
                status = "verified" if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) else "pending"
                original_id = "of_" + hashlib.sha256(
                    f"{task_id}\0{row[0]}".encode("utf-8")
                ).hexdigest()[:32]
                con.execute(
                    """INSERT INTO original_files
                    (original_file_id,task_id,source_file_id,source_content_id,absolute_path,
                     relative_path,media_type,support_status,size_bytes,mtime_ns,content_sha256,
                     fingerprint_status,canonical_source_file_id,scan_run_id,first_seen_at,last_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(task_id,source_file_id) DO UPDATE SET
                      source_content_id=excluded.source_content_id,absolute_path=excluded.absolute_path,
                      relative_path=excluded.relative_path,media_type=excluded.media_type,
                      support_status=excluded.support_status,size_bytes=excluded.size_bytes,
                      mtime_ns=excluded.mtime_ns,content_sha256=excluded.content_sha256,
                      fingerprint_status=excluded.fingerprint_status,
                      canonical_source_file_id=excluded.canonical_source_file_id,
                      scan_run_id=excluded.scan_run_id,last_seen_at=excluded.last_seen_at""",
                    (
                        original_id, task_id, str(row[0]), str(row[1]), str(row[2]),
                        str(row[3] or ""), str(row[4] or ""), str(row[5] or ""),
                        row[6], row[7], digest, status, str(row[9] or ""),
                        str(row[10] or ""), now, now,
                    ),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
        counts = dict(
            con.execute(
                "SELECT fingerprint_status,COUNT(*) FROM original_files WHERE task_id=? GROUP BY fingerprint_status",
                (task_id,),
            ).fetchall()
        )
    return {
        "total": sum(int(value) for value in counts.values()),
        "verified": int(counts.get("verified", 0)),
        "pending": int(counts.get("pending", 0)),
        "failed": int(counts.get("failed", 0)),
    }


def sync_artifact_lineage(database: Path, task_id: str) -> dict[str, int]:
    with connect(database) as con:
        tables = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"derived_assets", "visual_units", "original_files"}.issubset(tables):
            return {"derived": 0, "visual": 0, "missing_original": 0}
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """INSERT INTO artifact_lineage
                (artifact_id,task_id,artifact_kind,artifact_path,artifact_sha256,
                 original_file_id,source_content_id,stage_key,contract_version,updated_at)
                SELECT d.derived_id,?,d.derived_type,d.derived_path,COALESCE(d.sha256,''),
                       o.original_file_id,d.source_content_id,
                       CASE WHEN d.derived_type LIKE '%frame%' THEN 'video_frames' ELSE 'image_preview' END,
                       'media_archive_artifact_lineage_v1',CURRENT_TIMESTAMP
                FROM derived_assets d
                LEFT JOIN original_files o ON o.task_id=? AND o.source_content_id=d.source_content_id
                ON CONFLICT(task_id,artifact_id) DO UPDATE SET
                  artifact_path=excluded.artifact_path,artifact_sha256=excluded.artifact_sha256,
                  original_file_id=excluded.original_file_id,source_content_id=excluded.source_content_id,
                  stage_key=excluded.stage_key,updated_at=excluded.updated_at""",
                (task_id, task_id),
            )
            con.execute(
                """INSERT INTO artifact_lineage
                (artifact_id,task_id,artifact_kind,artifact_path,artifact_sha256,
                 original_file_id,parent_artifact_id,source_content_id,stage_key,contract_version,updated_at)
                SELECT v.visual_unit_id,?,'visual_unit',v.visual_file,'',
                       o.original_file_id,v.derived_id,v.source_content_id,
                       'visual_units','media_archive_artifact_lineage_v1',CURRENT_TIMESTAMP
                FROM visual_units v
                LEFT JOIN original_files o ON o.task_id=? AND o.source_content_id=v.source_content_id
                ON CONFLICT(task_id,artifact_id) DO UPDATE SET
                  artifact_path=excluded.artifact_path,original_file_id=excluded.original_file_id,
                  parent_artifact_id=excluded.parent_artifact_id,
                  source_content_id=excluded.source_content_id,updated_at=excluded.updated_at""",
                (task_id, task_id),
            )
            if "embeddings" in tables:
                con.execute(
                    """INSERT INTO artifact_lineage
                    (artifact_id,task_id,artifact_kind,artifact_path,original_file_id,
                     parent_artifact_id,source_content_id,stage_key,run_id,contract_version,
                     pipeline_version,model_id,status,updated_at)
                    SELECT 'openclip:'||e.embedding_id,?,'visual_embedding',COALESCE(e.vector_key,''),
                           o.original_file_id,e.visual_unit_id,e.source_content_id,'openclip',
                           COALESCE(e.run_id,''),'media_archive_artifact_lineage_v1','1.2.0',
                           COALESCE(e.model_name,''),'valid',CURRENT_TIMESTAMP
                    FROM embeddings e
                    LEFT JOIN original_files o ON o.task_id=? AND o.source_content_id=e.source_content_id
                    ON CONFLICT(task_id,artifact_id) DO UPDATE SET
                      artifact_path=excluded.artifact_path,original_file_id=excluded.original_file_id,
                      parent_artifact_id=excluded.parent_artifact_id,model_id=excluded.model_id,
                      updated_at=excluded.updated_at""",
                    (task_id, task_id),
                )
            if "visual_labels" in tables:
                con.execute(
                    """INSERT INTO artifact_lineage
                    (artifact_id,task_id,artifact_kind,artifact_path,original_file_id,
                     parent_artifact_id,source_content_id,stage_key,run_id,contract_version,
                     pipeline_version,model_id,status,updated_at)
                    SELECT 'yolo:'||l.label_id,?,'object_label','sqlite:visual_labels/'||l.label_id,
                           o.original_file_id,l.visual_unit_id,l.source_content_id,'yoloe',
                           COALESCE(l.run_id,''),'media_archive_artifact_lineage_v1','1.2.0',
                           COALESCE(l.model_name,''),'valid',CURRENT_TIMESTAMP
                    FROM visual_labels l
                    LEFT JOIN original_files o ON o.task_id=? AND o.source_content_id=l.source_content_id
                    ON CONFLICT(task_id,artifact_id) DO UPDATE SET
                      original_file_id=excluded.original_file_id,parent_artifact_id=excluded.parent_artifact_id,
                      model_id=excluded.model_id,updated_at=excluded.updated_at""",
                    (task_id, task_id),
                )
            if "stop03_3_qwenvl_results" in tables:
                con.execute(
                    """INSERT INTO artifact_lineage
                    (artifact_id,task_id,artifact_kind,artifact_path,artifact_sha256,
                     original_file_id,parent_artifact_id,source_content_id,stage_key,run_id,
                     contract_version,pipeline_version,model_id,parameters_hash,status,updated_at)
                    SELECT 'qwen:'||q.result_id,?,'qwen_description',COALESCE(q.raw_stdout_path,''),
                           COALESCE(q.raw_stdout_sha256,q.clean_text_sha256,''),o.original_file_id,
                           q.visual_unit_id,q.source_content_id,'qwen_optional_v2',COALESCE(q.run_id,''),
                           COALESCE(q.output_contract_version,''),'1.2.0',COALESCE(q.model_fingerprint_sha256,''),
                           COALESCE(q.orchestrator_config_sha256,''),COALESCE(q.result_status,'valid'),CURRENT_TIMESTAMP
                    FROM stop03_3_qwenvl_results q
                    LEFT JOIN original_files o ON o.task_id=? AND o.source_content_id=q.source_content_id
                    ON CONFLICT(task_id,artifact_id) DO UPDATE SET
                      artifact_path=excluded.artifact_path,artifact_sha256=excluded.artifact_sha256,
                      original_file_id=excluded.original_file_id,parent_artifact_id=excluded.parent_artifact_id,
                      model_id=excluded.model_id,parameters_hash=excluded.parameters_hash,
                      status=excluded.status,updated_at=excluded.updated_at""",
                    (task_id, task_id),
                )
            if "stop03_4_ocr_results" in tables:
                con.execute(
                    """INSERT INTO artifact_lineage
                    (artifact_id,task_id,artifact_kind,artifact_path,artifact_sha256,
                     original_file_id,parent_artifact_id,source_content_id,stage_key,
                     contract_version,pipeline_version,model_id,parameters_hash,status,updated_at)
                    SELECT 'ocr:'||r.result_id,?,'ocr_text',COALESCE(r.output_json_path,''),
                           COALESCE(r.output_json_sha256,r.ocr_text_sha256,''),o.original_file_id,
                           r.visual_unit_id,r.source_content_id,'ocr_optional_v2',
                           COALESCE(r.contract_version,''),'1.2.0',COALESCE(r.model_fingerprint_sha256,''),
                           COALESCE(r.config_sha256,''),COALESCE(r.result_status,'valid'),CURRENT_TIMESTAMP
                    FROM stop03_4_ocr_results r
                    LEFT JOIN original_files o ON o.task_id=? AND o.source_content_id=r.source_content_id
                    ON CONFLICT(task_id,artifact_id) DO UPDATE SET
                      artifact_path=excluded.artifact_path,artifact_sha256=excluded.artifact_sha256,
                      original_file_id=excluded.original_file_id,parent_artifact_id=excluded.parent_artifact_id,
                      model_id=excluded.model_id,parameters_hash=excluded.parameters_hash,
                      status=excluded.status,updated_at=excluded.updated_at""",
                    (task_id, task_id),
                )
            if {"stop03_1c_face_embeddings", "stop03_1c_person_reid_run_items"}.issubset(tables):
                con.execute(
                    """INSERT INTO artifact_lineage
                    (artifact_id,task_id,artifact_kind,artifact_path,artifact_sha256,
                     original_file_id,parent_artifact_id,source_content_id,stage_key,run_id,
                     contract_version,pipeline_version,model_id,status,updated_at)
                    SELECT 'face:'||f.face_id,?,'face_embedding','sqlite:stop03_1c_face_embeddings/'||f.face_id,
                           COALESCE(f.embedding_sha256,''),o.original_file_id,f.visual_unit_id,
                           i.source_content_id,'person_reid_optional_v1',COALESCE(f.run_id,''),
                           'media_archive_artifact_lineage_v1','1.2.0','InsightFace','valid',CURRENT_TIMESTAMP
                    FROM stop03_1c_face_embeddings f
                    JOIN stop03_1c_person_reid_run_items i
                      ON i.run_id=f.run_id AND i.visual_unit_id=f.visual_unit_id
                    LEFT JOIN original_files o ON o.task_id=? AND o.source_content_id=i.source_content_id
                    ON CONFLICT(task_id,artifact_id) DO UPDATE SET
                      artifact_sha256=excluded.artifact_sha256,original_file_id=excluded.original_file_id,
                      parent_artifact_id=excluded.parent_artifact_id,updated_at=excluded.updated_at""",
                    (task_id, task_id),
                )
            if {"stop03_5d_text_documents", "stop03_5d_text_vectors"}.issubset(tables):
                con.execute(
                    """INSERT INTO artifact_lineage
                    (artifact_id,task_id,artifact_kind,artifact_path,artifact_sha256,
                     original_file_id,parent_artifact_id,source_content_id,stage_key,run_id,
                     contract_version,pipeline_version,model_id,parameters_hash,output_bytes,status,updated_at)
                    SELECT 'text:'||d.document_id,?,'text_embedding','sqlite:stop03_5d_text_vectors/'||v.text_vector_id,
                           COALESCE(v.vector_sha256,''),o.original_file_id,d.canonical_visual_unit_id,
                           d.source_content_id,'embedding_optional_v2',COALESCE(d.embedding_run_id,''),
                           COALESCE(d.contract_version,''),'1.2.0',COALESCE(v.model_inventory_sha256,''),
                           COALESCE(v.model_config_sha256,''),COALESCE(v.vector_byte_length,0),
                           COALESCE(v.status,'valid'),CURRENT_TIMESTAMP
                    FROM stop03_5d_text_documents d
                    JOIN stop03_5d_text_vectors v
                      ON v.embedding_run_id=d.embedding_run_id
                     AND v.embedding_text_sha256=d.embedding_text_sha256
                    LEFT JOIN original_files o ON o.task_id=? AND o.source_content_id=d.source_content_id
                    ON CONFLICT(task_id,artifact_id) DO UPDATE SET
                      artifact_path=excluded.artifact_path,artifact_sha256=excluded.artifact_sha256,
                      original_file_id=excluded.original_file_id,parent_artifact_id=excluded.parent_artifact_id,
                      model_id=excluded.model_id,parameters_hash=excluded.parameters_hash,
                      output_bytes=excluded.output_bytes,status=excluded.status,updated_at=excluded.updated_at""",
                    (task_id, task_id),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
        derived = int(con.execute(
            "SELECT COUNT(*) FROM artifact_lineage WHERE task_id=? AND artifact_kind NOT IN ('visual_unit','object_label','visual_embedding','qwen_description','ocr_text','face_embedding','text_embedding')",
            (task_id,),
        ).fetchone()[0])
        visual = int(con.execute(
            "SELECT COUNT(*) FROM artifact_lineage WHERE task_id=? AND artifact_kind='visual_unit'",
            (task_id,),
        ).fetchone()[0])
        missing = int(con.execute(
            "SELECT COUNT(*) FROM artifact_lineage WHERE task_id=? AND original_file_id IS NULL",
            (task_id,),
        ).fetchone()[0])
    return {"derived": derived, "visual": visual, "missing_original": missing}


def enqueue_work_items(
    database: Path,
    *,
    task_id: str,
    stage_key: str,
    items: Iterable[Mapping[str, Any]],
    max_attempts: int = 3,
) -> int:
    now = time.time()
    inserted = 0
    with connect(database) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            for item in items:
                item_key = str(item.get("item_key") or "")
                if not item_key:
                    raise ValueError("central_work_item_key_missing")
                work_item_id = "wi_" + hashlib.sha256(
                    f"{task_id}\0{stage_key}\0{item_key}".encode("utf-8")
                ).hexdigest()[:32]
                before = con.total_changes
                con.execute(
                    """INSERT INTO stage_work_items
                    (work_item_id,task_id,stage_key,item_key,original_file_id,status,
                     attempt_count,max_attempts,created_at,updated_at)
                    VALUES(?,?,?,?,?,'pending',0,?,?,?)
                    ON CONFLICT(task_id,stage_key,item_key) DO UPDATE SET
                      original_file_id=COALESCE(excluded.original_file_id,stage_work_items.original_file_id),
                      max_attempts=MAX(stage_work_items.max_attempts,excluded.max_attempts),
                      updated_at=excluded.updated_at""",
                    (
                        work_item_id, task_id, stage_key, item_key,
                        item.get("original_file_id"), max(1, int(max_attempts)), now, now,
                    ),
                )
                inserted += int(con.total_changes > before)
            con.commit()
        except Exception:
            con.rollback()
            raise
    return inserted


def claim_work_item(
    database: Path,
    *,
    task_id: str,
    stage_key: str,
    worker_id: str,
    lease_seconds: float = 120.0,
) -> dict[str, Any] | None:
    now = time.time()
    with connect(database) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """UPDATE stage_work_items SET status='pending',lease_owner='',lease_expires_at=NULL,
                   heartbeat_at=NULL,updated_at=?
                   WHERE task_id=? AND stage_key=? AND status='running'
                   AND COALESCE(lease_expires_at,0)<?""",
                (now, task_id, stage_key, now),
            )
            row = con.execute(
                """SELECT * FROM stage_work_items
                   WHERE task_id=? AND stage_key=? AND status='pending'
                   AND attempt_count<max_attempts ORDER BY created_at,item_key LIMIT 1""",
                (task_id, stage_key),
            ).fetchone()
            if row is None:
                con.commit()
                return None
            updated = con.execute(
                """UPDATE stage_work_items SET status='running',attempt_count=attempt_count+1,
                   lease_owner=?,lease_expires_at=?,heartbeat_at=?,
                   started_at=COALESCE(started_at,?),finished_at=NULL,updated_at=?
                   WHERE work_item_id=? AND status='pending'""",
                (worker_id, now + max(5.0, lease_seconds), now, now, now, row["work_item_id"]),
            ).rowcount
            if updated != 1:
                con.rollback()
                return None
            claimed = dict(con.execute(
                "SELECT * FROM stage_work_items WHERE work_item_id=?", (row["work_item_id"],)
            ).fetchone())
            con.commit()
            return claimed
        except Exception:
            con.rollback()
            raise


def heartbeat_work_item(
    database: Path,
    *,
    work_item_id: str,
    worker_id: str,
    lease_seconds: float = 120.0,
) -> bool:
    now = time.time()
    with connect(database) as con:
        changed = con.execute(
            """UPDATE stage_work_items SET heartbeat_at=?,lease_expires_at=?,updated_at=?
               WHERE work_item_id=? AND status='running' AND lease_owner=?""",
            (now, now + max(5.0, lease_seconds), now, work_item_id, worker_id),
        ).rowcount
        con.commit()
    return changed == 1


def finish_work_item(
    database: Path,
    *,
    work_item_id: str,
    worker_id: str,
    status: str,
    error_code: str = "",
    error_payload: Mapping[str, Any] | None = None,
    output_payload: Mapping[str, Any] | None = None,
) -> bool:
    if status not in TERMINAL_WORK_ITEM_STATUSES:
        raise ValueError("central_work_item_terminal_status_invalid")
    now = time.time()
    with connect(database) as con:
        changed = con.execute(
            """UPDATE stage_work_items SET status=?,lease_owner='',lease_expires_at=NULL,
               heartbeat_at=?,finished_at=?,error_code=?,error_payload_json=?,
               output_payload_json=?,updated_at=?
               WHERE work_item_id=? AND status='running' AND lease_owner=?""",
            (
                status, now, now, error_code, _stable_json(error_payload or {}),
                _stable_json(output_payload or {}), now, work_item_id, worker_id,
            ),
        ).rowcount
        con.commit()
    return changed == 1


def append_event(
    database: Path,
    *,
    task_id: str,
    event_type: str,
    stage_key: str = "",
    severity: str = "info",
    message: str = "",
    payload: Mapping[str, Any] | None = None,
) -> str:
    event_id = "evt_" + uuid.uuid4().hex
    with connect(database) as con:
        con.execute(
            "INSERT INTO task_events(event_id,task_id,stage_key,event_type,severity,message,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (event_id, task_id, stage_key, event_type, severity, message, _stable_json(payload or {}), time.time()),
        )
        con.commit()
    return event_id


def update_search_state(
    database: Path,
    *,
    task_id: str,
    status: str,
    checks: Mapping[str, Any],
) -> None:
    allowed = {"NOT_BUILT", "BUILDING", "VERIFYING", "READY", "DEGRADED", "FAILED"}
    if status not in allowed:
        raise ValueError("central_search_state_invalid")
    now = time.time()
    with connect(database) as con:
        con.execute(
            """INSERT INTO search_state
            (task_id,status,schema_compatible,database_readable,required_tables_ready,
             search_preflight_ready,detail_json,verified_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,
             schema_compatible=excluded.schema_compatible,database_readable=excluded.database_readable,
             required_tables_ready=excluded.required_tables_ready,
             search_preflight_ready=excluded.search_preflight_ready,detail_json=excluded.detail_json,
             verified_at=excluded.verified_at,updated_at=excluded.updated_at""",
            (
                task_id, status, int(bool(checks.get("schema_compatible"))),
                int(bool(checks.get("database_readable"))),
                int(bool(checks.get("required_tables_ready"))),
                int(bool(checks.get("search_preflight_ready"))),
                _stable_json(checks), now, now,
            ),
        )
        con.commit()


def task_id_for_database(database: Path) -> str:
    with connect(database, readonly=True) as con:
        row = con.execute(
            """SELECT task_id FROM archive_tasks
               ORDER BY CASE task_mode WHEN 'full' THEN 0 WHEN 'legacy_migration' THEN 1 ELSE 2 END,
                        created_at,task_id LIMIT 1"""
        ).fetchone()
    if row is None or not str(row[0]):
        raise RuntimeError("central_database_task_identity_missing")
    return str(row[0])


def load_person_annotations(database: Path, task_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract": "media_archive_local_person_annotations_v1",
        "identities": {},
        "cluster_to_identity": {},
        "visual_memberships": {},
    }
    with connect(database, readonly=True) as con:
        for row in con.execute(
            "SELECT identity_id,display_name,tags_json,ignored FROM person_identity_overrides WHERE task_id=?",
            (task_id,),
        ):
            try:
                tags = json.loads(str(row[2] or "[]"))
            except json.JSONDecodeError:
                tags = []
            payload["identities"][str(row[0])] = {
                "display_name": str(row[1] or ""),
                "tags": tags if isinstance(tags, list) else [],
                "cluster_ids": [],
                "ignored": bool(row[3]),
            }
        for row in con.execute(
            "SELECT identity_id,machine_cluster_id FROM person_identity_cluster_map WHERE task_id=? ORDER BY machine_cluster_id",
            (task_id,),
        ):
            identity_id = str(row[0])
            cluster_id = str(row[1])
            payload["cluster_to_identity"][cluster_id] = identity_id
            identity = payload["identities"].setdefault(
                identity_id,
                {"display_name": "", "tags": [], "cluster_ids": [], "ignored": False},
            )
            identity["cluster_ids"].append(cluster_id)
        if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='person_identity_visual_map'"
        ).fetchone():
            for row in con.execute(
                "SELECT identity_id,visual_unit_id,source_content_id "
                "FROM person_identity_visual_map WHERE task_id=? "
                "ORDER BY identity_id,visual_unit_id",
                (task_id,),
            ):
                payload["visual_memberships"].setdefault(str(row[0]), []).append({
                    "visual_unit_id": str(row[1]),
                    "source_content_id": str(row[2]),
                })
    return payload


def save_person_annotations(
    database: Path,
    task_id: str,
    payload: Mapping[str, Any],
) -> None:
    now = time.time()
    identities = dict(payload.get("identities") or {})
    mapping = dict(payload.get("cluster_to_identity") or {})
    visual_memberships = dict(payload.get("visual_memberships") or {})
    with connect(database) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM person_identity_cluster_map WHERE task_id=?", (task_id,))
            con.execute("DELETE FROM person_identity_visual_map WHERE task_id=?", (task_id,))
            retained = set(str(key) for key in identities)
            for identity_id, value in identities.items():
                identity = dict(value or {})
                con.execute(
                    """INSERT INTO person_identity_overrides
                    (identity_id,task_id,display_name,tags_json,ignored,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(task_id,identity_id) DO UPDATE SET
                     display_name=excluded.display_name,tags_json=excluded.tags_json,
                     ignored=excluded.ignored,updated_at=excluded.updated_at""",
                    (
                        str(identity_id), task_id, str(identity.get("display_name") or ""),
                        _stable_json(list(identity.get("tags") or [])),
                        int(bool(identity.get("ignored"))), now, now,
                    ),
                )
            if retained:
                placeholders = ",".join("?" for _ in retained)
                con.execute(
                    f"DELETE FROM person_identity_overrides WHERE task_id=? AND identity_id NOT IN ({placeholders})",
                    (task_id, *sorted(retained)),
                )
            else:
                con.execute("DELETE FROM person_identity_overrides WHERE task_id=?", (task_id,))
            for cluster_id, identity_id in sorted(mapping.items()):
                if str(identity_id) not in retained:
                    continue
                con.execute(
                    "INSERT INTO person_identity_cluster_map(task_id,identity_id,machine_cluster_id,created_at) VALUES(?,?,?,?)",
                    (task_id, str(identity_id), str(cluster_id), now),
                )
            for identity_id, memberships in sorted(visual_memberships.items()):
                if str(identity_id) not in retained:
                    continue
                seen_visuals: set[str] = set()
                for membership in memberships or []:
                    value = dict(membership or {})
                    visual_unit_id = str(value.get("visual_unit_id") or "").strip()
                    source_content_id = str(value.get("source_content_id") or "").strip()
                    if not visual_unit_id or not source_content_id or visual_unit_id in seen_visuals:
                        continue
                    seen_visuals.add(visual_unit_id)
                    con.execute(
                        "INSERT INTO person_identity_visual_map"
                        "(task_id,identity_id,visual_unit_id,source_content_id,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (task_id, str(identity_id), visual_unit_id, source_content_id, now),
                    )
            con.commit()
        except Exception:
            con.rollback()
            raise


def record_search_history(
    database: Path,
    *,
    task_id: str,
    query_text: str,
    filters: Mapping[str, Any],
    result_count: int,
    elapsed_seconds: float,
    maximum_per_task: int = 100,
) -> str:
    query_id = "qry_" + uuid.uuid4().hex
    with connect(database) as con:
        con.execute(
            "INSERT INTO search_history(query_id,task_id,query_text,filters_json,result_count,elapsed_seconds,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                query_id, task_id, query_text, _stable_json(filters),
                max(0, int(result_count)), max(0.0, float(elapsed_seconds)), time.time(),
            ),
        )
        con.execute(
            """DELETE FROM search_history WHERE task_id=? AND query_id NOT IN
               (SELECT query_id FROM search_history WHERE task_id=? ORDER BY created_at DESC LIMIT ?)""",
            (task_id, task_id, max(1, int(maximum_per_task))),
        )
        con.commit()
    return query_id


def list_search_history(
    database: Path, task_id: str, *, limit: int = 30
) -> list[dict[str, Any]]:
    with connect(database, readonly=True) as con:
        rows = con.execute(
            "SELECT query_id,query_text,filters_json,result_count,elapsed_seconds,created_at "
            "FROM search_history WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
            (task_id, max(1, min(int(limit), 200))),
        ).fetchall()
    result = []
    for row in rows:
        try:
            filters = json.loads(str(row[2] or "{}"))
        except json.JSONDecodeError:
            filters = {}
        result.append({
            "query_id": str(row[0]), "query_text": str(row[1]),
            "filters": filters if isinstance(filters, dict) else {},
            "result_count": int(row[3] or 0),
            "elapsed_seconds": float(row[4] or 0), "created_at": float(row[5]),
        })
    return result


def save_search(
    database: Path,
    *,
    task_id: str,
    display_name: str,
    query_text: str,
    filters: Mapping[str, Any],
) -> str:
    name = " ".join(display_name.split())[:120]
    query = " ".join(query_text.split())[:512]
    if not name or not query:
        raise ValueError("saved_search_name_or_query_missing")
    saved_id = "saved_" + hashlib.sha256(
        f"{task_id}\0{name}".encode("utf-8")
    ).hexdigest()[:24]
    now = time.time()
    with connect(database) as con:
        con.execute(
            """INSERT INTO saved_searches
            (saved_search_id,task_id,display_name,query_text,filters_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(task_id,display_name) DO UPDATE SET query_text=excluded.query_text,
             filters_json=excluded.filters_json,updated_at=excluded.updated_at""",
            (saved_id, task_id, name, query, _stable_json(filters), now, now),
        )
        con.commit()
    return saved_id


def list_saved_searches(database: Path, task_id: str) -> list[dict[str, Any]]:
    with connect(database, readonly=True) as con:
        rows = con.execute(
            "SELECT saved_search_id,display_name,query_text,filters_json,updated_at "
            "FROM saved_searches WHERE task_id=? ORDER BY updated_at DESC,display_name",
            (task_id,),
        ).fetchall()
    result = []
    for row in rows:
        try:
            filters = json.loads(str(row[3] or "{}"))
        except json.JSONDecodeError:
            filters = {}
        result.append({
            "saved_search_id": str(row[0]), "display_name": str(row[1]),
            "query_text": str(row[2]),
            "filters": filters if isinstance(filters, dict) else {},
            "updated_at": float(row[4]),
        })
    return result


def upsert_asset_annotation(
    database: Path,
    *,
    task_id: str,
    source_content_id: str,
    tags: Sequence[str] = (),
    note: str = "",
    favorite: bool = False,
    rating: int = 0,
    ignored: bool = False,
) -> str:
    if not source_content_id:
        raise ValueError("central_annotation_source_content_id_missing")
    annotation_id = "ann_" + hashlib.sha256(
        f"{task_id}\0{source_content_id}".encode("utf-8")
    ).hexdigest()[:32]
    now = time.time()
    clean_tags = sorted({" ".join(str(value).split())[:80] for value in tags if str(value).strip()})
    with connect(database) as con:
        con.execute(
            """INSERT INTO user_asset_annotations
            (annotation_id,task_id,source_content_id,tags_json,note,favorite,rating,ignored,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(task_id,source_content_id) DO UPDATE SET tags_json=excluded.tags_json,
             note=excluded.note,favorite=excluded.favorite,rating=excluded.rating,
             ignored=excluded.ignored,updated_at=excluded.updated_at""",
            (
                annotation_id, task_id, source_content_id, _stable_json(clean_tags),
                " ".join(note.split())[:4000], int(favorite), max(0, min(5, int(rating))),
                int(ignored), now, now,
            ),
        )
        con.commit()
    return annotation_id


def asset_annotations(
    database: Path,
    *,
    task_id: str,
    source_content_ids: Sequence[str] = (),
) -> dict[str, dict[str, Any]]:
    clean_ids = sorted({str(value) for value in source_content_ids if str(value)})
    query = (
        "SELECT source_content_id,tags_json,note,favorite,rating,ignored,updated_at "
        "FROM user_asset_annotations WHERE task_id=?"
    )
    parameters: list[Any] = [task_id]
    if clean_ids:
        query += " AND source_content_id IN (" + ",".join("?" for _ in clean_ids) + ")"
        parameters.extend(clean_ids)
    with connect(database, readonly=True) as con:
        rows = con.execute(query, parameters).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            tags = json.loads(str(row[1] or "[]"))
        except json.JSONDecodeError:
            tags = []
        result[str(row[0])] = {
            "tags": tags if isinstance(tags, list) else [],
            "note": str(row[2] or ""),
            "favorite": bool(row[3]),
            "rating": int(row[4] or 0),
            "ignored": bool(row[5]),
            "updated_at": float(row[6] or 0),
        }
    return result


def central_audit(database: Path, task_id: str) -> dict[str, Any]:
    with connect(database, readonly=True) as con:
        tables = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "app_schema_meta", "archive_tasks", "original_files", "artifact_lineage",
            "stage_work_items", "task_events", "search_state", "search_history",
            "saved_searches", "user_asset_annotations", "person_identity_overrides",
            "person_identity_cluster_map", "storage_inventory",
        }
        counts: dict[str, int] = {}
        for table in sorted(required & tables):
            counts[table] = int(con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE task_id=?" if table != "app_schema_meta" else "SELECT COUNT(*) FROM app_schema_meta",
                (task_id,) if table != "app_schema_meta" else (),
            ).fetchone()[0])
        fingerprint = {str(row[0]): int(row[1]) for row in con.execute(
            "SELECT fingerprint_status,COUNT(*) FROM original_files WHERE task_id=? GROUP BY fingerprint_status",
            (task_id,),
        )} if "original_files" in tables else {}
        lineage_missing = int(con.execute(
            "SELECT COUNT(*) FROM artifact_lineage WHERE task_id=? AND original_file_id IS NULL",
            (task_id,),
        ).fetchone()[0]) if "artifact_lineage" in tables else 0
        integrity = str(con.execute("PRAGMA quick_check").fetchone()[0])
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
    return {
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "required_tables_missing": sorted(required - tables),
        "counts": counts,
        "fingerprint": fingerprint,
        "strong_fingerprint_complete": bool(fingerprint) and not any(
            fingerprint.get(key, 0) for key in ("pending", "failed")
        ),
        "lineage_missing_original": lineage_missing,
        "quick_check": integrity,
        "foreign_key_violations": foreign_keys,
    }
