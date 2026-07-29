#!/usr/bin/env python3
"""Create the V25 execution snapshot for the current database, without fixed counts.

The original contract locker intentionally asserts the 390-row acceptance
fixture.  A desktop library cannot use that project-specific assertion.  This
adapter reuses the frozen row semantics and immutable schema while deriving all
counts and digests from the database selected by the user.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


CONTRACT_NAME = "stop03_2_v25_candidate_snapshot"
SNAPSHOT_CONTRACT_VERSION = "stop03_2_v25_candidate_snapshot_v1"
SCRIPT_VERSION = "stop03_2_v25_dynamic_snapshot_v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_lock_module(project_root: Path):
    path = project_root / "scripts/03_stop03_visual_analysis/stop03_2_v25_candidate_contract_lock.py"
    spec = importlib.util.spec_from_file_location("stop03_2_v25_lock_semantics", path)
    if not spec or not spec.loader:
        raise RuntimeError("v25_lock_semantics_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS stop03_2_candidate_queue_items (
    candidate_id TEXT PRIMARY KEY,
    queue_type TEXT NOT NULL,
    visual_unit_id TEXT NOT NULL,
    source_content_id TEXT NOT NULL,
    derived_id TEXT,
    candidate_score REAL NOT NULL,
    reason_codes TEXT NOT NULL,
    black_frame_status TEXT NOT NULL,
    luma_mean REAL,
    luma_std REAL,
    run_id TEXT NOT NULL,
    script_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_s32_cq_vu
ON stop03_2_candidate_queue_items(visual_unit_id);
CREATE INDEX IF NOT EXISTS idx_s32_cq_queue
ON stop03_2_candidate_queue_items(queue_type,candidate_score);
"""


def backup_database_once(db: Path, out: Path) -> Path:
    target = out / "backups" / "database_before_candidate_ledger.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        return target
    source = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("candidate_ledger_database_backup_failed")
    return target


def prepare_ledger(db: Path, column_migration: Path, out: Path) -> dict[str, Any]:
    backup = backup_database_once(db, out)
    con = sqlite3.connect(str(db))
    try:
        con.executescript(BASE_LEDGER_SQL)
        existing = {str(row[1]) for row in con.execute("PRAGMA table_info(stop03_2_candidate_queue_items)")}
        added: list[str] = []
        for raw in column_migration.read_text(encoding="utf-8").split(";"):
            statement = "\n".join(
                line for line in raw.splitlines() if not line.strip().startswith("--")
            ).strip()
            if not statement:
                continue
            tokens = statement.split()
            name = tokens[5] if len(tokens) > 5 else ""
            if name and name not in existing:
                con.execute(statement)
                existing.add(name)
                added.append(name)
        con.commit()
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        con.close()
    return {
        "status": "PASS" if integrity == "ok" else "FAIL",
        "added_columns": added,
        "backup_path": str(backup),
    }


def build_snapshot(db: Path, project_root: Path, allowed_output_root: Path) -> dict[str, Any]:
    lock = load_lock_module(project_root)
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        base_rows = lock.candidate_base_rows(con)
        labels_by_visual = lock.load_yoloe_labels(con)
    created_at = utc_now()
    rows: list[dict[str, Any]] = []
    for base in base_rows:
        for field in lock.FORCED_ID_FIELDS:
            if not str(base.get(field) or "").strip():
                raise RuntimeError(f"dynamic_snapshot_missing_required_id:{field}")
        runtime = Path(str(base["runtime_visual_file"])).expanduser().resolve(strict=True)
        try:
            runtime.relative_to(allowed_output_root)
        except ValueError as exc:
            raise RuntimeError(f"runtime_visual_outside_library:{runtime}") from exc
        stat = runtime.stat()
        labels = labels_by_visual.get(str(base["visual_unit_id"]), [])
        labels_json = stable_json(labels)
        row = {
            **base,
            "runtime_visual_file": str(runtime),
            "runtime_visual_file_sha256": sha256_file(runtime),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "yoloe_labels_json": labels_json,
            "yoloe_label_count": len(labels),
            "yoloe_labels_sha256": sha256_text(labels_json),
            "yoloe_label_status": "labeled" if labels else "no_label",
            "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
            "snapshot_created_at": created_at,
            "frozen_status": "FROZEN",
        }
        semantic = {field: row.get(field) for field in lock.SEMANTIC_FIELDS}
        row["candidate_semantic_sha256"] = sha256_text(stable_json(semantic))
        rows.append({field: row.get(field) for field in lock.SNAPSHOT_COLUMNS})
    rows.sort(key=lambda item: str(item["candidate_id"]))
    counts = Counter(str(row["queue_type"]) for row in rows)
    id_digest = sha256_text("\n".join(str(row["candidate_id"]) for row in rows))
    semantic_digest = sha256_text(
        "\n".join(f"{row['candidate_id']}:{row['candidate_semantic_sha256']}" for row in rows)
    )
    return {
        "rows": rows,
        "summary": {
            "status": "PASS",
            "technical_status": "PASS",
            "contract_name": CONTRACT_NAME,
            "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
            "row_count": len(rows),
            "qwenvl_count": counts["qwenvl_high_value"],
            "ocr_count": counts["ocr_trigger"],
            "candidate_id_set_sha256": id_digest,
            "candidate_semantic_digest_sha256": semantic_digest,
            "rule_document_sha256": sha256_file(project_root / "docs/pipeline_rules/STOP03_2_GENERIC_HIGH_VALUE_RULES_V25.md"),
            "config_sha256": sha256_file(project_root / "configs/stop03_2_high_value_policy_v25.json"),
            "candidate_script_sha256": sha256_file(project_root / "scripts/03_stop03_visual_analysis/stop03_2_candidate_queues_from_db_safe_v25_0_20260711.py"),
        },
    }


def readback(db: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        contract = con.execute(
            "SELECT * FROM pipeline_frozen_contracts WHERE contract_name=?", (CONTRACT_NAME,)
        ).fetchone()
        if contract is None:
            raise RuntimeError("dynamic_v25_contract_missing")
        rows = con.execute(
            "SELECT candidate_id,candidate_semantic_sha256,queue_type "
            "FROM stop03_2_candidate_queue_frozen_v25 ORDER BY candidate_id"
        ).fetchall()
        qwen = int(con.execute("SELECT COUNT(*) FROM v_stop03_2_v25_qwenvl_execution_queue").fetchone()[0])
        ocr = int(con.execute("SELECT COUNT(*) FROM v_stop03_2_v25_ocr_execution_queue").fetchone()[0])
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
    id_digest = sha256_text("\n".join(str(row["candidate_id"]) for row in rows))
    semantic_digest = sha256_text(
        "\n".join(f"{row['candidate_id']}:{row['candidate_semantic_sha256']}" for row in rows)
    )
    passed = (
        len(rows) == int(contract["row_count"])
        and qwen == int(contract["qwenvl_count"])
        and ocr == int(contract["ocr_count"])
        and id_digest == str(contract["candidate_id_set_sha256"])
        and semantic_digest == str(contract["candidate_semantic_digest_sha256"])
        and integrity == "ok"
        and foreign_keys == 0
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "row_count": len(rows),
        "qwenvl_count": qwen,
        "ocr_count": ocr,
        "candidate_id_set_sha256": id_digest,
        "candidate_semantic_digest_sha256": semantic_digest,
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_keys,
    }


def commit_snapshot(
    db: Path,
    project_root: Path,
    allowed_output_root: Path,
    migration: Path,
    out: Path,
) -> dict[str, Any]:
    snapshot = build_snapshot(db, project_root, allowed_output_root)
    summary = snapshot["summary"]
    out.mkdir(parents=True, exist_ok=False)
    backup = out / "backups" / db.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db, backup)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.executescript(migration.read_text(encoding="utf-8"))
        existing = con.execute(
            "SELECT 1 FROM pipeline_frozen_contracts WHERE contract_name=?", (CONTRACT_NAME,)
        ).fetchone()
        if existing:
            result = readback(db)
            result["status"] = "IDEMPOTENT_PASS" if result["status"] == "PASS" else "FAIL"
            return result
        con.execute("BEGIN IMMEDIATE")
        columns = tuple(load_lock_module(project_root).SNAPSHOT_COLUMNS)
        con.executemany(
            f"INSERT INTO stop03_2_candidate_queue_frozen_v25 ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [[row.get(field) for field in columns] for row in snapshot["rows"]],
        )
        contract = {
            "contract_name": CONTRACT_NAME,
            "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
            "row_count": summary["row_count"],
            "qwenvl_count": summary["qwenvl_count"],
            "ocr_count": summary["ocr_count"],
            "candidate_id_set_sha256": summary["candidate_id_set_sha256"],
            "candidate_semantic_digest_sha256": summary["candidate_semantic_digest_sha256"],
            "rule_document_sha256": summary["rule_document_sha256"],
            "config_sha256": summary["config_sha256"],
            "candidate_script_sha256": summary["candidate_script_sha256"],
            "locked_at": utc_now(),
            "status": "FROZEN",
        }
        fields = tuple(contract)
        con.execute(
            f"INSERT INTO pipeline_frozen_contracts ({','.join(fields)}) "
            f"VALUES ({','.join('?' for _ in fields)})",
            [contract[field] for field in fields],
        )
        con.commit()
        result = readback(db)
        if result["status"] != "PASS":
            raise RuntimeError("dynamic_snapshot_readback_failed")
    except Exception:
        con.rollback()
        con.close()
        shutil.copy2(backup, db)
        raise
    finally:
        if con:
            con.close()
    result.update({"backup_path": str(backup), **summary})
    atomic_json(out / "reports/dynamic_v25_snapshot_summary.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("prepare-ledger", "commit", "readback"), required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--allowed-output-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = args.db.expanduser().resolve(strict=True)
    project = args.project_root.expanduser().resolve(strict=True)
    allowed = args.allowed_output_root.expanduser().resolve(strict=True)
    out = args.out.expanduser().resolve()
    if not out.is_relative_to(allowed):
        raise RuntimeError("dynamic_snapshot_output_outside_allowed_root")
    if args.mode == "prepare-ledger":
        report = prepare_ledger(
            db,
            project / "migrations/20260711_stop03_2_candidate_queue_v25.sql",
            out,
        )
    elif args.mode == "commit":
        report = commit_snapshot(
            db,
            project,
            allowed,
            project / "migrations/20260711_stop03_2_v25_candidate_snapshot_qwenvl_v1.sql",
            out,
        )
    else:
        report = readback(db)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report.get("status") in {"PASS", "IDEMPOTENT_PASS"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
