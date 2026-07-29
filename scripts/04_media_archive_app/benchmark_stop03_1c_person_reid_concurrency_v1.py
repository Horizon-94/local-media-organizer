#!/usr/bin/env python3
"""Bounded local concurrency benchmark for Stop03-1C person ReID."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence


VERSION = "benchmark_stop03_1c_person_reid_concurrency_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = (
    PROJECT_ROOT
    / "scripts/04_media_archive_app/stop03_1c_person_reid_db_orchestrator_v1.py"
)
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_1c_person_reid_db_v1.json"
DEFAULT_MIGRATION = PROJECT_ROOT / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"


def load_orchestrator() -> Any:
    spec = importlib.util.spec_from_file_location("person_reid_benchmark_target", ORCHESTRATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("person_reid_orchestrator_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_under(path: Path, root: Path) -> None:
    resolved = path.expanduser().resolve()
    allowed = root.expanduser().resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise RuntimeError(f"benchmark_output_outside_allowed_root:{resolved}")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def create_slim_database(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE source_assets(
                source_content_id TEXT PRIMARY KEY,
                absolute_path TEXT NOT NULL UNIQUE,
                relative_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                extension TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime INTEGER NOT NULL,
                ctime INTEGER NOT NULL,
                volume_id TEXT NOT NULL DEFAULT 'LOCAL',
                online_status INTEGER DEFAULT 1,
                is_deleted_or_missing INTEGER DEFAULT 0
            );
            CREATE TABLE derived_assets(
                derived_id TEXT PRIMARY KEY,
                source_content_id TEXT NOT NULL
            );
            CREATE TABLE visual_units(
                visual_unit_id TEXT PRIMARY KEY,
                source_content_id TEXT NOT NULL,
                derived_id TEXT NOT NULL,
                visual_file TEXT NOT NULL,
                time_position_ms INTEGER NOT NULL DEFAULT -1,
                near_black INTEGER DEFAULT 0,
                near_dup_group_id TEXT,
                is_near_dup_representative INTEGER DEFAULT 0
            );
            """
        )
        sources: dict[str, dict[str, Any]] = {}
        derived: dict[str, str] = {}
        for row in rows:
            sources.setdefault(str(row["source_content_id"]), row)
            derived[str(row["derived_id"])] = str(row["source_content_id"])
        for source_id, row in sources.items():
            original = str(row["source_absolute_path"])
            con.execute(
                """
                INSERT INTO source_assets(
                    source_content_id,absolute_path,relative_path,file_name,extension,
                    media_type,size_bytes,mtime,ctime,volume_id,online_status,
                    is_deleted_or_missing
                ) VALUES(?,?,?,?,?,?,?,?,?,?,1,0)
                """,
                (
                    source_id, original, source_id, Path(original).name,
                    Path(original).suffix, row["media_type"], 0, 0, 0, "BENCHMARK",
                ),
            )
        for derived_id, source_id in derived.items():
            con.execute(
                "INSERT INTO derived_assets(derived_id,source_content_id) VALUES(?,?)",
                (derived_id, source_id),
            )
        for row in rows:
            con.execute(
                """
                INSERT INTO visual_units(
                    visual_unit_id,source_content_id,derived_id,visual_file,
                    time_position_ms,near_black,near_dup_group_id,
                    is_near_dup_representative
                ) VALUES(?,?,?,?,?,0,NULL,0)
                """,
                (
                    row["visual_unit_id"], row["source_content_id"], row["derived_id"],
                    row["visual_file"], row["time_position_ms"],
                ),
            )
        con.commit()
    finally:
        con.close()


def parse_workers(value: str) -> list[int]:
    workers = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not workers or any(item <= 0 for item in workers):
        raise argparse.ArgumentTypeError("workers-list must contain positive integers")
    return list(dict.fromkeys(workers))


def person_labeled_visual_ids(database: Path) -> set[str]:
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='visual_labels'"
        ).fetchone()
        if not exists:
            return set()
        return {
            str(row[0])
            for row in con.execute(
                "SELECT DISTINCT visual_unit_id FROM visual_labels "
                "WHERE lower(label)='person'"
            )
        }
    finally:
        con.close()


def select_sample(
    rows: Sequence[dict[str, Any]],
    count: int,
    preferred_ids: set[str],
) -> list[dict[str, Any]]:
    preferred = [
        row for row in rows if str(row["visual_unit_id"]) in preferred_ids
    ]
    remaining = [
        row for row in rows if str(row["visual_unit_id"]) not in preferred_ids
    ]
    return (preferred + remaining)[:count]


def total_memory_bytes() -> int:
    try:
        return int(
            subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True
            ).strip()
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        page_count = int(os.sysconf("SC_PHYS_PAGES"))
        return page_size * page_count


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return float(ordered[index])


def item_timing_summary(database: Path) -> dict[str, Any]:
    con = sqlite3.connect(database)
    try:
        rows = con.execute(
            """
            SELECT claimed_by_worker,elapsed_seconds
            FROM stop03_1c_person_reid_run_items
            WHERE status IN ('success','no_face') AND elapsed_seconds IS NOT NULL
            ORDER BY visual_unit_id
            """
        ).fetchall()
    finally:
        con.close()
    values = [float(row[1]) for row in rows]
    worker_values: dict[str, list[float]] = {}
    for worker, elapsed in rows:
        worker_values.setdefault(str(worker), []).append(float(elapsed))
    return {
        "completed_item_timing_count": len(values),
        "average_item_seconds": statistics.fmean(values) if values else None,
        "p50_item_seconds": percentile(values, 0.50),
        "p95_item_seconds": percentile(values, 0.95),
        "max_item_seconds": max(values) if values else None,
        "per_worker_average_item_seconds": {
            worker: statistics.fmean(items)
            for worker, items in sorted(worker_values.items())
        },
        "per_worker_completed_item_count": {
            worker: len(items) for worker, items in sorted(worker_values.items())
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--migration", type=Path, default=DEFAULT_MIGRATION)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allowed-output-root", type=Path, required=True)
    parser.add_argument("--sample-count", type=int, default=24)
    parser.add_argument("--workers-list", type=parse_workers, default=parse_workers("1,2,3,4,6,8"))
    parser.add_argument("--auto-expand", action="store_true")
    parser.add_argument("--max-workers", type=int, default=16)
    parser.add_argument("--worker-step", type=int, default=2)
    parser.add_argument("--memory-stop-percent", type=float, default=80.0)
    parser.add_argument("--throughput-degradation-stop-percent", type=float, default=10.0)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--confirm-real-local-model-benchmark", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.confirm_real_local_model_benchmark:
        raise RuntimeError("real_local_person_reid_benchmark_confirmation_required")
    if args.sample_count <= 0:
        raise RuntimeError("benchmark_sample_count_must_be_positive")
    if args.max_workers <= 0 or args.worker_step <= 0:
        raise RuntimeError("benchmark_worker_bounds_invalid")
    if not 0 < args.memory_stop_percent < 100:
        raise RuntimeError("benchmark_memory_stop_percent_invalid")
    source_db = args.source_db.expanduser().resolve(strict=True)
    config = args.config.expanduser().resolve(strict=True)
    migration = args.migration.expanduser().resolve(strict=True)
    allowed = args.allowed_output_root.expanduser().resolve(strict=True)
    out = args.out.expanduser().resolve()
    ensure_under(out, allowed)
    out.mkdir(parents=True, exist_ok=True)

    person = load_orchestrator()
    all_rows = person.eligible_visual_units(source_db)
    preferred_ids = person_labeled_visual_ids(source_db)
    rows = select_sample(all_rows, args.sample_count, preferred_ids)
    if len(rows) < args.sample_count:
        raise RuntimeError(
            f"benchmark_not_enough_derived_visual_units:{len(rows)}_of_{args.sample_count}"
        )
    if any(row["is_original_path"] for row in rows):
        raise RuntimeError("benchmark_original_media_path_rejected")

    memory_bytes = total_memory_bytes()
    workers_to_test = list(args.workers_list)
    results: list[dict[str, Any]] = []
    stop_reason = "configured_worker_list_completed"
    index = 0
    while index < len(workers_to_test):
        workers = workers_to_test[index]
        index += 1
        case = out / f"workers_{workers}"
        case.mkdir(parents=True, exist_ok=True)
        database = case / "benchmark.sqlite"
        create_slim_database(database, rows)
        log = case / "run.log"
        command = [
            sys.executable, str(ORCHESTRATOR),
            "--mode", "run",
            "--db", str(database),
            "--config", str(config),
            "--migration", str(migration),
            "--out", str(case / "output"),
            "--allowed-output-root", str(out),
            "--workers", str(workers),
            "--max-attempts", str(args.max_attempts),
            "--confirm-central-db-write",
        ]
        started = time.perf_counter()
        with log.open("w", encoding="utf-8") as handle:
            completed = subprocess.run(
                command, stdout=handle, stderr=subprocess.STDOUT, check=False,
                env={
                    **os.environ,
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "HF_DATASETS_OFFLINE": "1",
                    "PYTHONUNBUFFERED": "1",
                },
            )
        wall_seconds = time.perf_counter() - started
        summary_path = case / "output/run_summary.json"
        summary = (
            json.loads(summary_path.read_text(encoding="utf-8"))
            if summary_path.is_file() else {}
        )
        result = {
            "workers": workers,
            "exit_code": completed.returncode,
            "status": summary.get("status", "FAIL"),
            "sample_count": len(rows),
            "success_count": summary.get("success_count", 0),
            "no_face_count": summary.get("no_face_count", 0),
            "failed_count": summary.get("failed_count", len(rows)),
            "face_count": summary.get("face_count", 0),
            "wall_seconds": wall_seconds,
            "inference_run_seconds": summary.get("total_elapsed_seconds"),
            "throughput_visual_units_per_second": summary.get(
                "throughput_visual_units_per_second", 0.0
            ),
            "measured_max_concurrency": summary.get("measured_max_concurrency", 0),
            "peak_rss_bytes": summary.get("peak_rss_bytes", 0),
            "peak_rss_percent_of_total_memory": (
                100.0 * float(summary.get("peak_rss_bytes", 0)) / memory_bytes
                if memory_bytes else None
            ),
            "log_path": str(log),
            "summary_path": str(summary_path),
            **item_timing_summary(database),
        }
        results.append(result)
        write_json(out / "benchmark_progress.json", {"results": results})
        if completed.returncode != 0:
            stop_reason = f"workers_{workers}_failed"
            break
        memory_percent = float(result["peak_rss_percent_of_total_memory"] or 0.0)
        if memory_percent >= args.memory_stop_percent:
            stop_reason = f"memory_stop_reached_at_workers_{workers}"
            break
        if args.auto_expand and index == len(workers_to_test):
            next_workers = workers + args.worker_step
            if next_workers > args.max_workers:
                stop_reason = "max_workers_reached"
                break
            projected_memory_percent = memory_percent * next_workers / max(workers, 1)
            if projected_memory_percent >= args.memory_stop_percent:
                stop_reason = (
                    f"next_workers_{next_workers}_projected_memory_"
                    f"{projected_memory_percent:.1f}_percent"
                )
                break
            throughputs = [
                float(row["throughput_visual_units_per_second"] or 0.0)
                for row in results
            ]
            if len(throughputs) >= 2:
                best_before_current = max(throughputs[:-1])
                current = throughputs[-1]
                degradation = (
                    100.0 * (best_before_current - current) / best_before_current
                    if best_before_current else 0.0
                )
                if degradation >= args.throughput_degradation_stop_percent:
                    stop_reason = (
                        f"throughput_degraded_{degradation:.1f}_percent_"
                        f"at_workers_{workers}"
                    )
                    break
            workers_to_test.append(next_workers)

    baseline = next((row for row in results if row["workers"] == 1), None)
    base_throughput = float(
        (baseline or {}).get("throughput_visual_units_per_second") or 0.0
    )
    for result in results:
        throughput = float(result["throughput_visual_units_per_second"] or 0.0)
        result["speedup_vs_one_worker"] = (
            throughput / base_throughput if base_throughput else None
        )
        result["parallel_efficiency"] = (
            throughput / (base_throughput * int(result["workers"]))
            if base_throughput else None
        )
    passed = [
        row for row in results
        if row["status"] == "PASS" and int(row["failed_count"]) == 0
    ]
    best = max(
        passed, key=lambda row: float(row["throughput_visual_units_per_second"] or 0.0),
        default=None,
    )
    summary = {
        "status": "PASS" if results and len(passed) == len(results) else "FAIL",
        "technical_status": "PASS" if passed else "FAIL",
        "benchmark_version": VERSION,
        "source_database_write": False,
        "sample_count": len(rows),
        "person_labeled_sample_count": sum(
            str(row["visual_unit_id"]) in preferred_ids for row in rows
        ),
        "total_memory_bytes": memory_bytes,
        "memory_stop_percent": args.memory_stop_percent,
        "workers_tested": [row["workers"] for row in results],
        "stop_reason": stop_reason,
        "best_workers": best["workers"] if best else None,
        "best_throughput_visual_units_per_second": (
            best["throughput_visual_units_per_second"] if best else None
        ),
        "results": results,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "model_directory_write": False,
    }
    write_json(out / "benchmark_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
