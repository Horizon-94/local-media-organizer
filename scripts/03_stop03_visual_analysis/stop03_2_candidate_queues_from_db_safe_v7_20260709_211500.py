#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-2 Candidate Queues from DB - safe V7 generic-rule test.

Purpose:
- Read current project SQLite DB, not old Stop03-1 manifests.
- Merge manual high-value seeds, YOLOE labels, OpenCLIP embedding presence,
  source/derived lineage, and lightweight derived-image quality checks.
- Produce Qwen-VL high-value candidate queue and OCR trigger queue.
- Reject black / near-black / invalid derived frames from candidate queues.

Hard constraints:
- No network.
- No downloads.
- No dependency installs.
- No model loading.
- No original media write. Original source media is not decoded; only DB metadata
  and derived preview/frame JPGs are read for black-frame checks.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import math
import os
import platform
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# -----------------------------------------------------------------------------
# Fixed local runtime contract
# -----------------------------------------------------------------------------
SCRIPT_VERSION = "stop03_2_candidate_queues_from_db_safe_v7_20260709_211500"
POLICY_VERSION = "stop03_2_generic_high_value_rules_v2_20260709"
STAGE = "stop03_2_candidate_queues"
MODEL_NAME = "rule_based_db_high_value_candidate_selector_no_model"
MODEL_PATH = "no_model_rule_based_db_selector"

EXPECTED_PYTHON = Path("/Users/yourname/Documents/AI-Local/envs/media-archive-v06-visual/bin/python")
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
CURRENT_TEST_SOURCE_ROOT = Path("/Users/yourname/Documents/001DZLtest")
LEGACY_SOURCE_ROOT = Path("/Users/yourname/Documents/MEDIA_ARCHIVE_TEST_SOURCE")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03-2-candidate-queues-db-safe-v7_20260709_211500"

# Offline guardrails, even though this stage does not load models.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("ULTRALYTICS_OFFLINE", "1")
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")
os.environ.setdefault("YOLO_CONFIG_DIR", str(TEST_OUTPUT_ROOT / "ultralytics-offline-config"))

# -----------------------------------------------------------------------------
# Candidate policy
# -----------------------------------------------------------------------------
VIDEO_CANDIDATE_MIN_GAP_MS = 20000
VIDEO_MIN_SCORE_FLOOR = 0.35
IMAGE_YOLO_SCORE_THRESHOLD = 2.5
QWENVL_MAX_GLOBAL_DEFAULT = 900
OCR_MAX_GLOBAL_DEFAULT = 900

# Generic visual-value taxonomy. This stage is project-agnostic: it must work for
# travel, family, documentary, screen recordings, archive footage, rural footage,
# city footage, events, products, animals, documents, and other future projects.
# Domain-specific words are not privileged. They are treated under generic classes
# such as nature/plant/agriculture, vehicle/machine, human, text/screen, scene.
STRONG_LABEL_KEYWORDS: Dict[str, float] = {
    # Humans / social scenes
    "person": 2.8, "people": 2.8, "human": 2.6, "face": 2.4, "crowd": 2.4,
    "hand": 1.8, "body": 1.6,
    # Text / screen / documents: often useful for Qwen-VL context and OCR overlap
    "screen": 2.3, "screen recording": 2.6, "monitor": 2.1, "display": 2.1,
    "phone": 2.0, "cell phone": 2.0, "laptop": 2.0, "computer": 2.0, "tablet": 2.0,
    "text": 2.4, "sign": 2.3, "document": 2.4, "paper": 2.0, "book": 1.8,
    "poster": 1.8, "billboard": 2.0, "presentation slide": 2.2, "subtitle": 2.0,
    "whiteboard": 2.0, "blackboard": 2.0, "license plate": 1.8, "menu": 1.7,
    # Vehicles / machines / tools / equipment
    "car": 1.8, "truck": 2.0, "bus": 1.9, "train": 2.1, "motorcycle": 1.7,
    "bicycle": 1.5, "boat": 1.8, "airplane": 2.0, "vehicle": 1.8,
    "tractor": 2.2, "harvester": 2.2, "machine": 1.8, "tool": 1.5,
    "camera": 1.8, "microphone": 1.6,
    # Animals / living subjects
    "dog": 1.8, "cat": 1.8, "cow": 1.8, "sheep": 1.7, "horse": 1.8,
    "chicken": 1.6, "bird": 1.5, "animal": 1.8,
    # Places / structures / navigational scene anchors
    "building": 1.5, "house": 1.5, "street": 1.5, "road": 1.5, "bridge": 1.6,
    "station": 1.6, "store": 1.6, "shop": 1.6, "room": 1.4, "kitchen": 1.4,
    "village": 1.5, "city": 1.5,
    # Nature / land / plants / agriculture, generic not project-specific
    "field": 1.6, "farm": 1.7, "farmland": 1.7, "crop": 1.7, "plant": 1.3,
    "tree": 1.2, "flower": 1.2, "river": 1.4, "lake": 1.4, "sea": 1.4,
    "mountain": 1.4, "beach": 1.4,
    # Food / objects can be useful when central but should not dominate alone
    "food": 1.3, "table": 1.1, "chair": 1.0,
}

WEAK_BACKGROUND_KEYWORDS = {
    "sky", "cloud", "wall", "floor", "ceiling", "grass", "window", "door",
}

LABEL_COMBO_BONUS = 0.65
VISUAL_COMPLEXITY_BONUS = 0.45
GENERIC_VIDEO_SIGNAL_MIN_SCORE = 0.90
GENERIC_IMAGE_SIGNAL_MIN_SCORE = 2.20
GENERIC_LABEL_CHANGE_THRESHOLD = 0.55
# This is not a target. It is a safety cap to avoid accidental full-fanout.
QWENVL_MAX_GLOBAL_DEFAULT = 900
OCR_MAX_GLOBAL_DEFAULT = 900

OCR_LABEL_KEYWORDS = [
    "text", "sign", "screen", "screen recording", "monitor", "display", "phone",
    "cell phone", "laptop", "computer", "television", "tv", "book", "paper",
    "document", "poster", "billboard", "traffic sign", "license plate", "menu",
    "whiteboard", "blackboard", "label", "receipt", "invoice", "form", "subtitle",
    "screenshot", "webpage", "chat screenshot",
]

OCR_PATH_KEYWORDS = [
    "screen", "screenshot", "screenrecording", "screen_recording", "screen-recording",
    "rpreplay", "record screen", "recorded screen", "录屏", "屏幕录制", "截屏", "截图",
    "屏幕", "微信图片", "企业微信", "网页", "聊天记录", "文档", "合同", "发票", "收据",
    "菜单", "牌照", "车牌", "路牌", "招牌", "字幕",
]

COMMON_FIELDS = [
    "candidate_id", "queue_type", "visual_unit_id", "visual_unit_type",
    "candidate_visual_file", "visual_file", "visual_file_sha256", "derived_id",
    "original_source_file_id", "original_source_content_id",
    "original_source_path_at_processing_time", "source_relative_path",
    "media_type", "derived_type", "time_position_ms", "preview_role",
    "source_group_id", "source_group_kind", "policy_version", "candidate_score",
    "reason_codes", "selected_at", "black_frame_status", "luma_mean", "luma_std",
]

QWENVL_EXTRA_FIELDS = [
    "manual_seed_source", "manual_seed_label", "manual_seed_strength",
    "high_value_category", "video_frame_rank", "video_candidate_budget",
    "nearest_selected_gap_ms", "min_gap_ms", "min_gap_broken",
    "min_gap_exception_reason", "source_group_frame_count", "yoloe_labels",
    "yoloe_top_labels", "yoloe_detection_count", "embedding_present",
]

OCR_EXTRA_FIELDS = [
    "ocr_trigger_source", "ocr_trigger_labels", "ocr_trigger_keywords",
    "ocr_trigger_reason_codes", "known_ocr_like_source_group",
]

DECISION_FIELDS = [
    "visual_unit_id", "visual_unit_type", "source_group_id", "media_type",
    "is_qwenvl_candidate", "is_ocr_candidate", "qwenvl_candidate_id",
    "ocr_candidate_id", "candidate_score", "ocr_score", "reason_codes",
    "qwenvl_reject_reason_codes", "ocr_reject_reason_codes", "black_frame_status",
    "luma_mean", "luma_std", "visual_file",
]

# -----------------------------------------------------------------------------
# Utility
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def safe_int(v: Any, default: int = -1) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def stable_id(prefix: str, *parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", errors="replace"))
        h.update(b"\0")
    return f"{prefix}_{h.hexdigest()[:32]}"


def resolve_path(p: Path) -> Path:
    return p.expanduser().resolve(strict=False)


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def atomic_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json(path: Path, obj: Any) -> None:
    atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    atomic_text(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows))


def stable_fields(preferred: Sequence[str], rows: Sequence[Dict[str, Any]]) -> List[str]:
    fields = list(preferred)
    seen = set(fields)
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                fields.append(k)
    return fields


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], preferred: Sequence[str]) -> None:
    ensure_dir(path.parent)
    fields = stable_fields(preferred, rows)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    tmp.replace(path)


def assert_db_path(path: Path) -> Path:
    rp = resolve_path(path)
    if not rp.exists():
        raise SystemExit(f"[BLOCKED] SQLite DB does not exist: {rp}")
    if is_relative_to(rp, resolve_path(CURRENT_TEST_SOURCE_ROOT)) or is_relative_to(rp, resolve_path(LEGACY_SOURCE_ROOT)):
        raise SystemExit(f"[BLOCKED] DB path is inside source media root: {rp}")
    allowed = [resolve_path(PROJECT_ROOT), resolve_path(TEST_OUTPUT_ROOT)]
    if not any(is_relative_to(rp, a) for a in allowed):
        raise SystemExit(f"[BLOCKED] DB path must be under project/test-output roots: {rp}")
    return rp


def assert_out_path(path: Path) -> Path:
    rp = resolve_path(path)
    if is_relative_to(rp, resolve_path(CURRENT_TEST_SOURCE_ROOT)) or is_relative_to(rp, resolve_path(LEGACY_SOURCE_ROOT)):
        raise SystemExit(f"[BLOCKED] Output path is inside source media root: {rp}")
    allowed = [resolve_path(TEST_OUTPUT_ROOT), resolve_path(PROJECT_ROOT / "outputs"), resolve_path(PROJECT_ROOT / "test-output")]
    if not any(is_relative_to(rp, a) for a in allowed):
        raise SystemExit(f"[BLOCKED] Output path must be under test-output/project output roots: {rp}")
    return rp

# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------
def connect_db(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def table_columns(con: sqlite3.Connection, name: str) -> List[str]:
    if not table_exists(con, name):
        return []
    return [str(r[1]) for r in con.execute(f"PRAGMA table_info({name})").fetchall()]


def count_table(con: sqlite3.Connection, name: str) -> Optional[int]:
    if not table_exists(con, name):
        return None
    return int(con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])


def pick_col(cols: Sequence[str], candidates: Sequence[str]) -> str:
    s = set(cols)
    for c in candidates:
        if c in s:
            return c
    return ""


def model_run_upsert_start(con: sqlite3.Connection, run_id: str, input_count: int) -> None:
    if not table_exists(con, "model_runs"):
        return
    cols = table_columns(con, "model_runs")
    vals = {
        "run_id": run_id,
        "stage": STAGE,
        "model_name": MODEL_NAME,
        "model_path": MODEL_PATH,
        "script_version": SCRIPT_VERSION,
        "script_path": str(Path(__file__).resolve(strict=False)),
        "status": "running",
        "input_count": int(input_count),
        "output_count": 0,
        "error_message": "",
        "started_at": now_iso(),
        "finished_at": None,
    }
    use = [c for c in vals if c in cols]
    if not use:
        return
    q = f"INSERT OR REPLACE INTO model_runs ({','.join(use)}) VALUES ({','.join('?' for _ in use)})"
    con.execute(q, [vals[c] for c in use])
    con.commit()


def model_run_finish(con: sqlite3.Connection, run_id: str, status: str, output_count: int, error_message: str = "") -> None:
    if not table_exists(con, "model_runs"):
        return
    cols = table_columns(con, "model_runs")
    vals = {
        "status": status,
        "output_count": int(output_count),
        "error_message": error_message or "",
        "finished_at": now_iso(),
    }
    pairs = []
    data = []
    for c, v in vals.items():
        if c in cols:
            pairs.append(f"{c}=?")
            data.append(v)
    if pairs:
        data.append(run_id)
        con.execute(f"UPDATE model_runs SET {', '.join(pairs)} WHERE run_id=?", data)
        con.commit()


def ensure_candidate_tables(con: sqlite3.Connection) -> None:
    con.execute(
        """
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
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_s32_cq_vu ON stop03_2_candidate_queue_items(visual_unit_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_s32_cq_queue ON stop03_2_candidate_queue_items(queue_type, candidate_score)")
    con.commit()


def write_candidate_items_db(con: sqlite3.Connection, run_id: str, rows: Sequence[Dict[str, Any]], clear_run_stage: bool) -> int:
    ensure_candidate_tables(con)
    if clear_run_stage:
        con.execute("DELETE FROM stop03_2_candidate_queue_items")
    ts = now_iso()
    n = 0
    for r in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO stop03_2_candidate_queue_items
            (candidate_id, queue_type, visual_unit_id, source_content_id, derived_id, candidate_score,
             reason_codes, black_frame_status, luma_mean, luma_std, run_id, script_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.get("candidate_id"), r.get("queue_type"), r.get("visual_unit_id"),
                r.get("original_source_content_id") or r.get("source_content_id"), r.get("derived_id"),
                float(r.get("candidate_score") or 0.0), r.get("reason_codes") or "",
                r.get("black_frame_status") or "", r.get("luma_mean"), r.get("luma_std"),
                run_id, SCRIPT_VERSION, ts,
            ),
        )
        n += 1
    con.commit()
    return n

# -----------------------------------------------------------------------------
# Runtime preflight
# -----------------------------------------------------------------------------
def dependency_report() -> Dict[str, Dict[str, Any]]:
    deps: Dict[str, Dict[str, Any]] = {}
    for mod_name in ["sqlite3", "csv", "json", "hashlib", "pathlib", "subprocess", "platform", "PIL"]:
        try:
            mod = importlib.import_module(mod_name)
            ver = getattr(mod, "__version__", "stdlib")
            if mod_name == "PIL":
                try:
                    from PIL import Image  # noqa: F401
                    import PIL as _PIL
                    ver = getattr(_PIL, "__version__", ver)
                except Exception:
                    raise
            deps[mod_name] = {"ok": True, "optional": False, "version": ver, "error": ""}
        except Exception as e:  # noqa: BLE001
            deps[mod_name] = {"ok": False, "optional": False, "version": "", "error": f"{type(e).__name__}: {e}"}
    return deps


def latest_run(con: sqlite3.Connection, stage: str) -> Optional[Dict[str, Any]]:
    if not table_exists(con, "model_runs"):
        return None
    row = con.execute(
        """
        SELECT run_id, stage, model_name, script_version, status, input_count, output_count, error_message, started_at, finished_at
        FROM model_runs
        WHERE stage=?
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (stage,),
    ).fetchone()
    return dict(row) if row else None


def runtime_preflight(con: sqlite3.Connection, db_path: Path, out_path: Path) -> Dict[str, Any]:
    deps = dependency_report()
    missing_deps = [k for k, v in deps.items() if not v.get("ok") and not v.get("optional")]
    required_tables = ["visual_units", "derived_assets", "source_assets", "visual_labels", "visual_label_terms", "embeddings", "model_runs"]
    missing_tables = [t for t in required_tables if not table_exists(con, t)]
    counts = {t: count_table(con, t) for t in required_tables + ["manual_high_value_visual_seeds", "stop03_2_candidate_queue_items"]}
    blockers = []
    if missing_deps:
        blockers.append("missing_dependencies=" + ",".join(missing_deps))
    if missing_tables:
        blockers.append("missing_tables=" + ",".join(missing_tables))
    for t in ["visual_units", "derived_assets", "source_assets", "visual_labels", "embeddings"]:
        if counts.get(t) is not None and int(counts.get(t) or 0) <= 0:
            blockers.append(f"empty_{t}")
    return {
        "script_version": SCRIPT_VERSION,
        "validation_status": "PASS" if not blockers else "BLOCKED",
        "python_executable": str(Path(sys.executable).resolve(strict=False)),
        "python_launcher_note": "sys.executable may resolve to Homebrew realpath inside venv; expected launcher is recorded separately",
        "expected_python": str(EXPECTED_PYTHON),
        "expected_python_realpath": str(EXPECTED_PYTHON.resolve(strict=False)),
        "expected_python_match": Path(sys.executable).resolve(strict=False) == EXPECTED_PYTHON.resolve(strict=False),
        "expected_script_local": str(PROJECT_ROOT / "scripts/03_stop03_visual_analysis" / Path(__file__).name),
        "project_root": str(PROJECT_ROOT),
        "test_output_root": str(TEST_OUTPUT_ROOT),
        "default_db": str(DEFAULT_DB),
        "db_path": str(db_path),
        "default_out": str(DEFAULT_OUT),
        "out_path": str(out_path),
        "current_test_source_root_read_protected": str(CURRENT_TEST_SOURCE_ROOT),
        "legacy_source_root_read_protected": str(LEGACY_SOURCE_ROOT),
        "model_usage_policy": "not_used_by_stop03_2_candidate_queues_db_safe",
        "input_policy": "read_current_sqlite_tables_visual_units_derived_assets_source_assets_visual_labels_embeddings_manual_high_value_visual_seeds; source_file_id_optional_not_required",
        "source_media_policy": "original_media_not_decoded_not_written; derived_visual_files_read_for_black_frame_validation_only",
        "derived_write_policy": "write_only_to_project_or_test_output_roots_and_project_sqlite_candidate_tables",
        "required_local_assets": {
            "project_root": str(PROJECT_ROOT),
            "test_output_root": str(TEST_OUTPUT_ROOT),
            "db": str(db_path),
            "output_base_parent": str(out_path.parent),
            "expected_python_launcher": str(EXPECTED_PYTHON),
        },
        "assets": {
            "project_root": {"path": str(PROJECT_ROOT), "exists": PROJECT_ROOT.exists()},
            "test_output_root": {"path": str(TEST_OUTPUT_ROOT), "exists": TEST_OUTPUT_ROOT.exists()},
            "db": {"path": str(db_path), "exists": db_path.exists(), "size_bytes": db_path.stat().st_size if db_path.exists() else None},
            "output_base_parent": {"path": str(out_path.parent), "exists": out_path.parent.exists()},
            "current_test_source_root_read_protected": {"path": str(CURRENT_TEST_SOURCE_ROOT), "exists": CURRENT_TEST_SOURCE_ROOT.exists()},
            "expected_python_launcher": {"path": str(EXPECTED_PYTHON), "exists": EXPECTED_PYTHON.exists(), "realpath": str(EXPECTED_PYTHON.resolve(strict=False))},
        },
        "dependencies": deps,
        "missing_required_dependencies": missing_deps,
        "tables": {t: table_columns(con, t) for t in required_tables + ["manual_high_value_visual_seeds", "stop03_2_candidate_queue_items"] if table_exists(con, t)},
        "counts": counts,
        "upstream_latest_runs": {
            "step02_image": latest_run(con, "step02_2_image_preview"),
            "step02_video": latest_run(con, "step02_video_frame_c4s"),
            "yoloe": latest_run(con, "stop03_yoloe_full"),
            "openclip": latest_run(con, "stop03_1b_openclip_visual_embedding"),
        },
        "offline_env": {
            "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", ""),
            "TRANSFORMERS_OFFLINE": os.environ.get("TRANSFORMERS_OFFLINE", ""),
            "HF_DATASETS_OFFLINE": os.environ.get("HF_DATASETS_OFFLINE", ""),
            "ULTRALYTICS_OFFLINE": os.environ.get("ULTRALYTICS_OFFLINE", ""),
            "NO_ALBUMENTATIONS_UPDATE": os.environ.get("NO_ALBUMENTATIONS_UPDATE", ""),
            "YOLO_CONFIG_DIR": os.environ.get("YOLO_CONFIG_DIR", ""),
        },
        "safety": {
            "network": "blocked_by_offline_env_not_used_by_stop03_2",
            "download": "not_used",
            "dependency_install": "not_used",
            "source_media_read": "not_used_original_media; derived_visual_file_read_only_for_black_frame_check",
            "source_media_write": "blocked_by_design_and_output_path_guard",
            "model_loading": "not_used_by_stop03_2_candidate_queues_db_safe",
        },
        "blockers": blockers,
        "platform": {"python": sys.version, "system": platform.platform()},
    }

# -----------------------------------------------------------------------------
# Data loading and scoring
# -----------------------------------------------------------------------------
def load_manual_seeds(con: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    if not table_exists(con, "manual_high_value_visual_seeds"):
        return {}
    seeds: Dict[str, Dict[str, Any]] = {}
    for r in con.execute("SELECT * FROM manual_high_value_visual_seeds").fetchall():
        d = dict(r)
        vu = str(d.get("visual_unit_id") or "")
        if not vu:
            continue
        old = seeds.get(vu)
        if old:
            old["seed_label"] = "|".join(sorted(set(str(old.get("seed_label") or "").split("|") + [str(d.get("seed_label") or "")])))
        else:
            seeds[vu] = d
    return seeds


def load_labels(con: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    if not table_exists(con, "visual_labels"):
        return {}

    # DB-safe schema adaptation:
    # Current formal table has `confidence`, not `score`. Older experimental
    # manifests/tables sometimes used `score`. Do not reference a missing column
    # in SQL, because SQLite fails at prepare time even inside COALESCE().
    vl_cols = table_columns(con, "visual_labels")
    if "confidence" in vl_cols:
        conf_expr = "vl.confidence"
        conf_expr_plain = "confidence"
    elif "score" in vl_cols:
        conf_expr = "vl.score"
        conf_expr_plain = "score"
    else:
        conf_expr = "0.0"
        conf_expr_plain = "0.0"

    vt_exists = table_exists(con, "visual_label_terms")
    if vt_exists:
        sql = f"""
        SELECT
          vl.visual_unit_id,
          vl.label,
          MAX(COALESCE({conf_expr}, 0.0)) AS max_conf,
          COUNT(*) AS box_count,
          vt.label_zh,
          vt.source_layer,
          vt.trigger_strength,
          vt.search_terms_json
        FROM visual_labels vl
        LEFT JOIN visual_label_terms vt ON vl.label = vt.label
        GROUP BY vl.visual_unit_id, vl.label, vt.label_zh, vt.source_layer, vt.trigger_strength, vt.search_terms_json
        """
    else:
        sql = f"""
        SELECT visual_unit_id, label, MAX(COALESCE({conf_expr_plain}, 0.0)) AS max_conf, COUNT(*) AS box_count
        FROM visual_labels
        GROUP BY visual_unit_id, label
        """
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in con.execute(sql).fetchall():
        d = dict(r)
        out[str(d.get("visual_unit_id") or "")].append(d)
    return out


def load_embedding_presence(con: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    if not table_exists(con, "embeddings"):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for r in con.execute("SELECT visual_unit_id, embedding_id, dimension, vector_key, model_name, model_path FROM embeddings").fetchall():
        d = dict(r)
        out[str(d.get("visual_unit_id") or "")] = d
    return out


def load_units(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    vu_cols = table_columns(con, "visual_units")
    da_cols = table_columns(con, "derived_assets")
    sa_cols = table_columns(con, "source_assets")
    time_col = pick_col(vu_cols, ["time_position_ms", "time_ms"])
    frame_col = pick_col(vu_cols, ["frame_index"])
    sql = f"""
    SELECT
      vu.visual_unit_id,
      vu.source_content_id,
      vu.derived_id,
      vu.visual_file,
      {('vu.' + time_col) if time_col else '-1'} AS vu_time_position_ms,
      {('vu.' + frame_col) if frame_col else '-1'} AS vu_frame_index,
      da.time_position_ms AS da_time_position_ms,
      da.frame_index AS da_frame_index,
      COALESCE(vu.near_black, 0) AS db_near_black,
      vu.luma_mean AS db_luma_mean,
      vu.luma_std AS db_luma_std,
      da.derived_type,
      da.derived_path,
      da.sha256 AS derived_sha256,
      sa.media_type,
      sa.extension,
      sa.relative_path AS source_relative_path,
      sa.absolute_path AS original_source_path_at_processing_time,
      '' AS original_source_file_id
    FROM visual_units vu
    LEFT JOIN derived_assets da ON vu.derived_id = da.derived_id
    LEFT JOIN source_assets sa ON vu.source_content_id = sa.source_content_id
    ORDER BY sa.media_type, vu.source_content_id, vu_time_position_ms, vu.visual_unit_id
    """
    rows = [dict(r) for r in con.execute(sql).fetchall()]
    labels = load_labels(con)
    embs = load_embedding_presence(con)
    seeds = load_manual_seeds(con)
    for r in rows:
        vu = str(r.get("visual_unit_id") or "")
        r["labels"] = labels.get(vu, [])
        r["embedding"] = embs.get(vu, {})
        r["manual_seed"] = seeds.get(vu, {})
        vu_time = safe_int(r.get("vu_time_position_ms"), -1)
        da_time = safe_int(r.get("da_time_position_ms"), -1)
        vu_frame = safe_int(r.get("vu_frame_index"), -1)
        da_frame = safe_int(r.get("da_frame_index"), -1)
        eff_frame = vu_frame if vu_frame >= 0 else da_frame
        if vu_time >= 0:
            eff_time = vu_time
            time_source = "visual_units.time_position_ms"
        elif da_time >= 0:
            eff_time = da_time
            time_source = "derived_assets.time_position_ms"
        elif eff_frame >= 0:
            # Last-resort fallback for C4S-derived frames; records must report fallback.
            eff_time = eff_frame * 3000
            time_source = "frame_index_x_3000ms_fallback"
        else:
            eff_time = -1
            time_source = "missing_time_position"
        r["time_position_ms"] = eff_time
        r["time_position_source"] = time_source
        r["frame_index"] = eff_frame
        r["visual_unit_type"] = "video_frame" if str(r.get("media_type") or "") == "video" else "image_visual_unit"
        r["source_group_id"] = str(r.get("source_content_id") or "unknown_source_group")
        r["source_group_kind"] = "video" if r["visual_unit_type"] == "video_frame" else "normal_image"
        if "timelapse" in str(r.get("derived_type") or "").lower() or "keyframe" in str(r.get("derived_path") or "").lower():
            r["source_group_kind"] = "timelapse"
        r["preview_role"] = r["source_group_kind"]
    return rows

# -----------------------------------------------------------------------------
# Black / bad-frame validation
# -----------------------------------------------------------------------------
def resolve_visual_path(row: Dict[str, Any]) -> Path:
    for k in ["visual_file", "derived_path"]:
        v = str(row.get(k) or "").strip()
        if not v:
            continue
        p = Path(v).expanduser()
        if not p.is_absolute():
            # current DB usually stores absolute derived paths; fallback to project root.
            p = PROJECT_ROOT / p
        return p.resolve(strict=False)
    return Path("")


def black_frame_metrics(path: Path, sample_edge: int = 96) -> Dict[str, Any]:
    if not path or str(path) == ".":
        return {"black_frame_status": "missing_visual_path", "black_rejected": True, "luma_mean": None, "luma_std": None, "black_pixel_ratio": None, "error": "missing_visual_path"}
    if not path.exists():
        return {"black_frame_status": "missing_visual_file", "black_rejected": True, "luma_mean": None, "luma_std": None, "black_pixel_ratio": None, "error": str(path)}
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as im:
            im = im.convert("L")
            im.thumbnail((sample_edge, sample_edge))
            stat = ImageStat.Stat(im)
            mean = float(stat.mean[0])
            std = float(stat.stddev[0])
            px = list(im.getdata())
            black_ratio = sum(1 for v in px if v <= 8) / max(1, len(px))
        # Strict enough to catch full black frames but avoid rejecting normal night scenes with texture.
        near_black = (mean <= 8.0 and std <= 5.0) or (black_ratio >= 0.985 and mean <= 16.0)
        status = "near_black_rejected" if near_black else "ok"
        return {"black_frame_status": status, "black_rejected": bool(near_black), "luma_mean": round(mean, 4), "luma_std": round(std, 4), "black_pixel_ratio": round(black_ratio, 6), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"black_frame_status": "invalid_visual_file", "black_rejected": True, "luma_mean": None, "luma_std": None, "black_pixel_ratio": None, "error": f"{type(e).__name__}: {e}"}


def attach_quality(rows: List[Dict[str, Any]], *, no_image_quality_check: bool = False) -> List[Dict[str, Any]]:
    for r in rows:
        if no_image_quality_check:
            r.update({"black_frame_status": "not_checked", "black_rejected": False, "luma_mean": None, "luma_std": None, "black_pixel_ratio": None, "quality_error": ""})
        else:
            m = black_frame_metrics(resolve_visual_path(r))
            r.update({
                "black_frame_status": m.get("black_frame_status"),
                "black_rejected": bool(m.get("black_rejected")),
                "luma_mean": m.get("luma_mean"),
                "luma_std": m.get("luma_std"),
                "black_pixel_ratio": m.get("black_pixel_ratio"),
                "quality_error": m.get("error") or "",
            })
    return rows

# -----------------------------------------------------------------------------
# Scoring / selection
# -----------------------------------------------------------------------------
def label_name(label: Dict[str, Any]) -> str:
    return str(label.get("label") or "").strip()


def label_conf(label: Dict[str, Any]) -> float:
    try:
        return float(label.get("max_conf") or label.get("confidence") or label.get("score") or 0.0)
    except Exception:
        return 0.0


def label_category(name: str) -> str:
    n = name.lower().strip()
    if any(k in n for k in ["person", "people", "human", "face", "crowd", "hand", "body"]):
        return "human_social"
    if any(k in n for k in ["text", "sign", "screen", "monitor", "display", "document", "paper", "book", "poster", "slide", "subtitle", "whiteboard", "blackboard", "license", "menu", "phone", "laptop", "computer", "tablet"]):
        return "text_screen_document"
    if any(k in n for k in ["car", "truck", "bus", "train", "motorcycle", "bicycle", "boat", "airplane", "vehicle", "tractor", "harvester", "machine", "tool", "camera", "microphone"]):
        return "vehicle_machine_equipment"
    if any(k in n for k in ["dog", "cat", "cow", "sheep", "horse", "chicken", "bird", "animal"]):
        return "animal_living_subject"
    if any(k in n for k in ["building", "house", "street", "road", "bridge", "station", "store", "shop", "room", "kitchen", "village", "city"]):
        return "place_structure_scene"
    if any(k in n for k in ["field", "farm", "crop", "plant", "tree", "flower", "river", "lake", "sea", "mountain", "beach"]):
        return "nature_landscape_plant"
    if any(k in n for k in ["food", "table", "chair"]):
        return "object_context"
    if n in WEAK_BACKGROUND_KEYWORDS or any(k in n for k in WEAK_BACKGROUND_KEYWORDS):
        return "weak_background"
    return "other"


def label_weight(label: Dict[str, Any]) -> float:
    name = label_name(label).lower()
    source_layer = str(label.get("source_layer") or "")
    # OCR-trigger labels can still contribute to OCR. For Qwen-VL, they only
    # contribute visual value when the label itself is a generic visible object
    # class such as screen/sign/document, not merely because of OCR_TRIGGER layer.
    best = 0.0
    for kw, w in STRONG_LABEL_KEYWORDS.items():
        if kw in name:
            best = max(best, float(w))
    if best <= 0 and (name in WEAK_BACKGROUND_KEYWORDS or any(k in name for k in WEAK_BACKGROUND_KEYWORDS)):
        return 0.0
    return best


def label_set(row: Dict[str, Any]) -> set[str]:
    return {label_name(l).lower() for l in (row.get("labels") or []) if label_name(l)}


def jaccard_distance(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return 1.0 - (len(a & b) / max(1, len(a | b)))


def row_detection_count(row: Dict[str, Any]) -> int:
    return sum(int(l.get("box_count") or 1) for l in row.get("labels") or [])


def compute_scores(row: Dict[str, Any]) -> Dict[str, Any]:
    labels = row.get("labels") or []
    q = 0.0
    q_reasons: List[str] = []
    ocr = 0.0
    ocr_reasons_list: List[str] = []
    ocr_labels = []
    categories = set()
    strong_hits = 0
    for lab in labels:
        name = label_name(lab)
        lname = name.lower()
        conf = max(0.25, label_conf(lab))
        boxes = int(lab.get("box_count") or 1)
        w = label_weight(lab)
        cat = label_category(lname)
        if w > 0:
            strong_hits += 1
            categories.add(cat)
            s = w * conf * (1.0 + min(1.0, math.log1p(max(1, boxes)) / 3.0))
            q += s
            q_reasons.append(f"generic_yoloe:{cat}:{name}:w={w:.1f}:conf={conf:.2f}:boxes={boxes}")
        elif cat == "weak_background":
            q_reasons.append(f"weak_background_label_ignored:{name}")
        # OCR queue rule remains independent from Qwen-VL.
        if any(k in lname for k in OCR_LABEL_KEYWORDS) or "OCR_TRIGGER" in str(lab.get("source_layer") or ""):
            oscore = 2.0 * conf * (1.0 + min(1.0, math.log1p(max(1, boxes)) / 3.0))
            ocr += oscore
            ocr_labels.append(name)
            ocr_reasons_list.append(f"ocr_label:{name}:conf={conf:.2f}:boxes={boxes}")
    if len(categories) >= 2:
        q += LABEL_COMBO_BONUS * (len(categories) - 1)
        q_reasons.append("generic_label_category_combo:" + "+".join(sorted(categories)))
    det_count = row_detection_count(row)
    distinct_labels = len(label_set(row))
    if det_count >= 3 or distinct_labels >= 3:
        q += VISUAL_COMPLEXITY_BONUS
        q_reasons.append(f"visual_complexity:boxes={det_count}:labels={distinct_labels}")
    # Embedding presence alone is not a high-value reason. It is recorded for
    # traceability and for future novelty/dedup, but does not add Qwen score.
    if row.get("embedding"):
        row["embedding_present"] = True
    if row.get("manual_seed"):
        q = max(q, 100.0)
        seed = row["manual_seed"]
        q_reasons.insert(0, f"manual_high_value_seed:{seed.get('seed_source','')}:{seed.get('seed_label','')}:strong")
    hay = " ".join([str(row.get("source_relative_path") or ""), str(row.get("original_source_path_at_processing_time") or ""), str(row.get("visual_file") or "")]).lower()
    path_hits = sorted({kw for kw in OCR_PATH_KEYWORDS if kw.lower() in hay})
    if path_hits:
        ocr += 3.0 + 0.25 * len(path_hits)
        ocr_reasons_list.append("ocr_path_keyword:" + "|".join(path_hits[:8]))
    if row.get("black_rejected"):
        q_reasons.append("black_or_invalid_visual_rejected")
        ocr_reasons_list.append("black_or_invalid_visual_rejected")
    row["qwen_score"] = round(q, 6)
    row["ocr_score"] = round(ocr, 6)
    row["qwen_reasons"] = q_reasons
    row["ocr_reasons"] = ocr_reasons_list
    row["ocr_trigger_labels"] = sorted(set(ocr_labels))
    row["ocr_trigger_keywords"] = path_hits
    row["generic_label_categories"] = sorted(categories)
    row["strong_label_hit_count"] = strong_hits
    return row


def generic_video_frame_signal(row: Dict[str, Any], prev_row: Optional[Dict[str, Any]], next_row: Optional[Dict[str, Any]], idx: int, n: int) -> Tuple[bool, List[str], float]:
    """Return whether a video frame has enough generic high-value segment evidence.

    V7 rule:
    - generic_visual_subject_signal is only a base signal, never enough by itself.
    - OCR must be an emergence/change signal, not every repeated OCR-like frame.
    - high_information_jump is relative to neighbors, not just static many boxes.
    """
    reasons: List[str] = []
    score_boost = 0.0
    qscore = float(row.get("qwen_score") or 0.0)
    ocr_score = float(row.get("ocr_score") or 0.0)
    labels = label_set(row)
    prev_labels = label_set(prev_row or {})
    next_labels = label_set(next_row or {})
    prev_dist = jaccard_distance(labels, prev_labels) if prev_row else 0.0
    next_dist = jaccard_distance(labels, next_labels) if next_row else 0.0
    label_change = max(prev_dist, next_dist)
    row["neighbor_yoloe_label_jaccard_distance_max"] = round(label_change, 6)
    row["neighbor_prev_id"] = (prev_row or {}).get("visual_unit_id", "")
    row["neighbor_next_id"] = (next_row or {}).get("visual_unit_id", "")

    if qscore >= GENERIC_VIDEO_SIGNAL_MIN_SCORE:
        reasons.append("generic_visual_subject_signal")

    strong_reasons: List[str] = []
    if label_change >= GENERIC_LABEL_CHANGE_THRESHOLD and qscore >= 0.35:
        reasons.append("major_object_set_change")
        strong_reasons.append("major_object_set_change")
        score_boost += 0.75

    prev_ocr = float((prev_row or {}).get("ocr_score") or 0.0)
    next_ocr = float((next_row or {}).get("ocr_score") or 0.0)
    ocr_emerges = ocr_score > 0 and (idx == 0 or idx == n - 1 or prev_ocr <= 0 or next_ocr <= 0 or label_change >= 0.35)
    if ocr_emerges:
        reasons.append("ocr_region_emerges")
        strong_reasons.append("ocr_region_emerges")
        score_boost += 0.35

    det = row_detection_count(row)
    distinct = len(labels)
    prev_det = row_detection_count(prev_row or {})
    next_det = row_detection_count(next_row or {})
    prev_distinct = len(prev_labels)
    next_distinct = len(next_labels)
    # Relative information jump: current frame is materially richer than at least one neighbor.
    info_jump = False
    if det >= max(prev_det, next_det) + 4 and det >= 4:
        info_jump = True
    if distinct >= max(prev_distinct, next_distinct) + 2 and distinct >= 2:
        info_jump = True
    if det >= 10 and distinct >= 2 and label_change >= 0.25:
        info_jump = True
    if info_jump:
        reasons.append("high_information_jump")
        strong_reasons.append("high_information_jump")
        score_boost += 0.35

    if idx == 0 or idx == n - 1:
        # Boundary is weak; useful only when there is actual visual/OCR signal.
        if qscore >= 1.5 or ocr_score > 0:
            reasons.append("video_coverage_boundary")
            strong_reasons.append("video_coverage_boundary")
            score_boost += 0.15

    return bool(strong_reasons), reasons, score_boost

def base_candidate(row: Dict[str, Any], queue_type: str, score: float, reasons: Sequence[str]) -> Dict[str, Any]:
    seed = row.get("manual_seed") or {}
    cid = stable_id("cand", POLICY_VERSION, queue_type, row.get("visual_unit_id"))
    return {
        "candidate_id": cid,
        "queue_type": queue_type,
        "visual_unit_id": row.get("visual_unit_id"),
        "visual_unit_type": row.get("visual_unit_type"),
        "candidate_visual_file": row.get("visual_file") or row.get("derived_path"),
        "visual_file": row.get("visual_file") or row.get("derived_path"),
        "visual_file_sha256": row.get("derived_sha256") or "",
        "derived_id": row.get("derived_id") or "",
        "original_source_file_id": row.get("original_source_file_id") or "",
        "original_source_content_id": row.get("source_content_id") or "",
        "source_content_id": row.get("source_content_id") or "",
        "original_source_path_at_processing_time": row.get("original_source_path_at_processing_time") or "",
        "source_relative_path": row.get("source_relative_path") or "",
        "media_type": row.get("media_type") or "",
        "derived_type": row.get("derived_type") or "",
        "time_position_ms": row.get("time_position_ms"),
        "preview_role": row.get("preview_role") or "",
        "source_group_id": row.get("source_group_id") or "",
        "source_group_kind": row.get("source_group_kind") or "",
        "policy_version": POLICY_VERSION,
        "candidate_score": round(float(score), 6),
        "reason_codes": "|".join(reasons),
        "selected_at": now_iso(),
        "black_frame_status": row.get("black_frame_status") or "",
        "luma_mean": row.get("luma_mean"),
        "luma_std": row.get("luma_std"),
        "manual_seed_source": seed.get("seed_source", ""),
        "manual_seed_label": seed.get("seed_label", ""),
        "manual_seed_strength": seed.get("seed_strength", ""),
        "yoloe_labels": "|".join(sorted({label_name(l) for l in row.get("labels") or [] if label_name(l)})),
        "yoloe_top_labels": "|".join(sorted({label_name(l) for l in sorted(row.get("labels") or [], key=label_conf, reverse=True)[:8] if label_name(l)})),
        "yoloe_detection_count": row_detection_count(row),
        "embedding_present": bool(row.get("embedding")),
    }


def select_qwenvl(rows: Sequence[Dict[str, Any]], *, top_global: int, per_video_budget_scale: float, image_yolo_threshold: float) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    del per_video_budget_scale  # V6 no longer uses fixed per-video budgets; kept for CLI compatibility.
    candidates: List[Dict[str, Any]] = []
    decisions: Dict[str, Dict[str, Any]] = {}
    video_reports: List[Dict[str, Any]] = []

    # 1) Manual visual seeds: forced unless black/invalid.
    for r in rows:
        vu = str(r.get("visual_unit_id"))
        if r.get("manual_seed"):
            if r.get("black_rejected"):
                decisions[vu] = {"q_reject": "manual_seed_but_black_or_invalid_visual", "q_score": r.get("qwen_score", 0.0), "q_reason": "black_rejected"}
                continue
            cat = "manual_timelapse_seed" if r.get("source_group_kind") == "timelapse" else "manual_finder_tag_image_seed"
            c = base_candidate(r, "qwenvl_high_value", 100.0, r.get("qwen_reasons") or ["manual_high_value_seed"])
            c.update({
                "high_value_category": cat,
                "video_frame_rank": "",
                "video_candidate_budget": "dynamic_segment_rule_with_time_gap_and_dedup_no_fixed_target",
                "nearest_selected_gap_ms": "",
                "min_gap_ms": "",
                "min_gap_broken": False,
                "min_gap_exception_reason": "",
                "source_group_frame_count": 1,
            })
            candidates.append(c)
            decisions[vu] = {"q_reject": "", "q_score": 100.0, "q_reason": "manual_high_value_seed"}

    selected_ids = {str(c["visual_unit_id"]) for c in candidates}

    # 2) Video candidates: dynamic high-value segments, not percentage and not tiny fixed budget.
    video_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("visual_unit_type") == "video_frame":
            video_groups[str(r.get("source_group_id") or r.get("source_content_id") or "unknown")].append(r)
    for gid, group0 in sorted(video_groups.items()):
        group = sorted(group0, key=lambda r: (int(r.get("time_position_ms") or 0), str(r.get("visual_unit_id"))))
        raw_nonblack = [r for r in group if not r.get("black_rejected")]
        # V7: de-duplicate duplicated visual_units that point to the same derived frame.
        # This prevents the same JPG/timecode from being selected multiple times.
        dedup_map: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
        duplicate_visual_unit_count = 0
        for rr in raw_nonblack:
            key = (str(rr.get("derived_id") or rr.get("visual_file") or rr.get("derived_path") or rr.get("visual_unit_id")), int(rr.get("time_position_ms") or -1), str(rr.get("derived_path") or rr.get("visual_file") or ""))
            old_rr = dedup_map.get(key)
            if old_rr is None:
                dedup_map[key] = rr
            else:
                duplicate_visual_unit_count += 1
                if (float(rr.get("qwen_score") or 0.0), row_detection_count(rr), str(rr.get("visual_unit_id"))) > (float(old_rr.get("qwen_score") or 0.0), row_detection_count(old_rr), str(old_rr.get("visual_unit_id"))):
                    dedup_map[key] = rr
        nonblack = sorted(dedup_map.values(), key=lambda r: (int(r.get("time_position_ms") or 0), str(r.get("visual_unit_id"))))
        signal_rows: List[Tuple[Dict[str, Any], List[str], float]] = []
        for i, r in enumerate(nonblack):
            prev_r = nonblack[i - 1] if i > 0 else None
            next_r = nonblack[i + 1] if i + 1 < len(nonblack) else None
            ok, reasons, boost = generic_video_frame_signal(r, prev_r, next_r, i, len(nonblack))
            if ok:
                signal_rows.append((r, reasons, boost))
        if not signal_rows and nonblack:
            best = max(nonblack, key=lambda r: (float(r.get("qwen_score") or 0.0), row_detection_count(r), -abs(int(r.get("time_position_ms") or 0))))
            signal_rows = [(best, ["video_min_one_best_available_frame"], 0.0)]

        picked: List[Tuple[Dict[str, Any], List[str], float]] = []
        for r, reasons, boost in sorted(signal_rows, key=lambda x: int(x[0].get("time_position_ms") or 0)):
            t_ms = int(r.get("time_position_ms") or 0)
            score = float(r.get("qwen_score") or 0.0) + boost
            can_break = bool(set(reasons) & {"major_object_set_change", "high_information_jump", "ocr_region_emerges", "video_min_one_best_available_frame"})
            if not picked:
                picked.append((r, reasons, boost))
                continue
            nearest_idx = min(range(len(picked)), key=lambda j: abs(t_ms - int(picked[j][0].get("time_position_ms") or 0)))
            nearest_gap = abs(t_ms - int(picked[nearest_idx][0].get("time_position_ms") or 0))
            if nearest_gap >= VIDEO_CANDIDATE_MIN_GAP_MS or can_break:
                picked.append((r, reasons, boost))
            else:
                old_r, old_reasons, old_boost = picked[nearest_idx]
                old_score = float(old_r.get("qwen_score") or 0.0) + old_boost
                if score > old_score + 0.25:
                    picked[nearest_idx] = (r, reasons + ["replaced_nearby_lower_score_frame"], boost)

        for rank, (r, reasons, boost) in enumerate(sorted(picked, key=lambda x: int(x[0].get("time_position_ms") or 0)), start=1):
            score = float(r.get("qwen_score") or 0.0) + boost
            c = base_candidate(r, "qwenvl_high_value", score, (r.get("qwen_reasons") or []) + reasons)
            nearest = min((abs(int(r.get("time_position_ms") or 0) - int(p[0].get("time_position_ms") or 0)) for p in picked if p[0] is not r), default="")
            min_gap_broken = nearest != "" and nearest < VIDEO_CANDIDATE_MIN_GAP_MS
            exception = "|".join([x for x in reasons if x in {"major_object_set_change", "high_information_jump", "ocr_region_emerges", "video_min_one_best_available_frame"}]) if min_gap_broken else ""
            c.update({
                "high_value_category": "video_high_value_segment_candidate",
                "video_frame_rank": rank,
                "video_candidate_budget": "dynamic_segment_rule_with_time_gap_and_dedup_no_fixed_target",
                "nearest_selected_gap_ms": nearest,
                "min_gap_ms": VIDEO_CANDIDATE_MIN_GAP_MS,
                "min_gap_broken": min_gap_broken,
                "min_gap_exception_reason": exception,
                "source_group_frame_count": len(group),
            })
            candidates.append(c)
            decisions[str(r.get("visual_unit_id"))] = {"q_reject": "", "q_score": score, "q_reason": "video_dynamic_segment_selected"}
        video_reports.append({
            "source_group_id": gid,
            "frame_count": len(group),
            "nonblack_frame_count": len(nonblack),
            "raw_nonblack_frame_count": len(raw_nonblack),
            "duplicate_visual_unit_same_derived_count": duplicate_visual_unit_count,
            "black_rejected_count": len(group) - len(raw_nonblack),
            "missing_effective_time_count": sum(1 for rr in nonblack if int(rr.get("time_position_ms") or -1) < 0),
            "video_candidate_budget": "dynamic_segment_rule_with_time_gap_and_dedup_no_fixed_target",
            "candidate_signal_count": len(signal_rows),
            "selected_count": len(picked),
            "overselect_review_flag": bool(len(picked) > 30 or (len(nonblack) > 0 and len(picked) / max(1, len(nonblack)) > 0.50)),
            "selected_ratio_note": "audit_only_not_selection_rule",
        })
        picked_ids = {str(p[0].get("visual_unit_id")) for p in picked}
        for r in group:
            vu = str(r.get("visual_unit_id"))
            if vu not in decisions:
                decisions[vu] = {"q_reject": "black_or_invalid_visual" if r.get("black_rejected") else "not_selected_duplicate_or_no_generic_high_value_signal", "q_score": r.get("qwen_score", 0.0), "q_reason": ""}

    # 3) Non-manual still images. No project-specific terms. No embedding-only.
    image_pool = [r for r in rows if r.get("visual_unit_type") != "video_frame" and str(r.get("visual_unit_id")) not in selected_ids and not r.get("manual_seed")]
    image_pool = [r for r in image_pool if not r.get("black_rejected") and float(r.get("qwen_score") or 0.0) >= image_yolo_threshold and row_detection_count(r) > 0]
    image_pool.sort(key=lambda r: (-float(r.get("qwen_score") or 0.0), str(r.get("visual_unit_id"))))
    for r in image_pool:
        kind = "timelapse_candidate" if r.get("source_group_kind") == "timelapse" else "image_generic_visual_signal_candidate"
        c = base_candidate(r, "qwenvl_high_value", float(r.get("qwen_score") or 0.0), r.get("qwen_reasons") or ["image_generic_visual_signal"])
        c.update({
            "high_value_category": kind,
            "video_frame_rank": "",
            "video_candidate_budget": "",
            "nearest_selected_gap_ms": "",
            "min_gap_ms": "",
            "min_gap_broken": False,
            "min_gap_exception_reason": "",
            "source_group_frame_count": 1,
        })
        candidates.append(c)
        decisions[str(r.get("visual_unit_id"))] = {"q_reject": "", "q_score": r.get("qwen_score", 0.0), "q_reason": kind}
        if top_global > 0 and len(candidates) >= top_global:
            break

    # Safety cap only. It is not a target count.
    candidates = sorted(candidates, key=lambda c: (-float(c.get("candidate_score") or 0.0), str(c.get("source_group_id")), int(c.get("time_position_ms") or -1), str(c.get("visual_unit_id"))))
    if top_global > 0:
        candidates = candidates[:top_global]
    return candidates, decisions, video_reports


def select_ocr(rows: Sequence[Dict[str, Any]], *, top_global: int) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    ocr_rows = [r for r in rows if not r.get("black_rejected") and float(r.get("ocr_score") or 0.0) > 0]
    ocr_rows.sort(key=lambda r: (-float(r.get("ocr_score") or 0.0), str(r.get("visual_unit_id"))))
    if top_global > 0:
        ocr_rows = ocr_rows[:top_global]
    out = []
    decisions: Dict[str, Dict[str, Any]] = {}
    for r in ocr_rows:
        reasons = r.get("ocr_reasons") or ["ocr_trigger"]
        c = base_candidate(r, "ocr_trigger", float(r.get("ocr_score") or 0.0), reasons)
        c.update({
            "ocr_trigger_source": "path_or_label",
            "ocr_trigger_labels": "|".join(r.get("ocr_trigger_labels") or []),
            "ocr_trigger_keywords": "|".join(r.get("ocr_trigger_keywords") or []),
            "ocr_trigger_reason_codes": "|".join(reasons),
            "known_ocr_like_source_group": bool(r.get("ocr_trigger_keywords")),
        })
        out.append(c)
        decisions[str(r.get("visual_unit_id"))] = {"o_reject": "", "o_score": r.get("ocr_score", 0.0), "o_reason": "ocr_selected"}
    for r in rows:
        vu = str(r.get("visual_unit_id"))
        if vu not in decisions:
            decisions[vu] = {"o_reject": "black_or_invalid_visual" if r.get("black_rejected") else "no_ocr_trigger_evidence", "o_score": r.get("ocr_score", 0.0), "o_reason": ""}
    return out, decisions


def build_decisions(rows: Sequence[Dict[str, Any]], qwenvl: Sequence[Dict[str, Any]], ocr: Sequence[Dict[str, Any]], qdec: Dict[str, Dict[str, Any]], odec: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    q_by = {str(c["visual_unit_id"]): c for c in qwenvl}
    o_by = {str(c["visual_unit_id"]): c for c in ocr}
    decisions = []
    for r in rows:
        vu = str(r.get("visual_unit_id"))
        q = q_by.get(vu)
        o = o_by.get(vu)
        qd = qdec.get(vu, {})
        od = odec.get(vu, {})
        decisions.append({
            "visual_unit_id": vu,
            "visual_unit_type": r.get("visual_unit_type"),
            "source_group_id": r.get("source_group_id"),
            "media_type": r.get("media_type"),
            "is_qwenvl_candidate": bool(q),
            "is_ocr_candidate": bool(o),
            "qwenvl_candidate_id": q.get("candidate_id") if q else "",
            "ocr_candidate_id": o.get("candidate_id") if o else "",
            "candidate_score": q.get("candidate_score") if q else qd.get("q_score", r.get("qwen_score", 0.0)),
            "ocr_score": o.get("candidate_score") if o else od.get("o_score", r.get("ocr_score", 0.0)),
            "reason_codes": "|".join(x for x in [q.get("reason_codes") if q else qd.get("q_reason", ""), o.get("reason_codes") if o else od.get("o_reason", "")] if x),
            "qwenvl_reject_reason_codes": "" if q else qd.get("q_reject", "no_qwenvl_rule_applied"),
            "ocr_reject_reason_codes": "" if o else od.get("o_reject", "no_ocr_rule_applied"),
            "black_frame_status": r.get("black_frame_status"),
            "luma_mean": r.get("luma_mean"),
            "luma_std": r.get("luma_std"),
            "visual_file": r.get("visual_file") or r.get("derived_path"),
        })
    return decisions

# -----------------------------------------------------------------------------
# Reports
# -----------------------------------------------------------------------------
def write_reports(out: Path, rows: Sequence[Dict[str, Any]], qwenvl: Sequence[Dict[str, Any]], ocr: Sequence[Dict[str, Any]], decisions: Sequence[Dict[str, Any]], video_reports: Sequence[Dict[str, Any]], summary: Dict[str, Any]) -> Dict[str, str]:
    manifests = out / "manifests"
    reports = out / "reports"
    ensure_dir(manifests)
    ensure_dir(reports)
    write_jsonl(manifests / "qwenvl_high_value_candidate_queue.jsonl", qwenvl)
    write_csv(manifests / "qwenvl_high_value_candidate_queue.csv", qwenvl, COMMON_FIELDS + QWENVL_EXTRA_FIELDS)
    write_jsonl(manifests / "ocr_trigger_candidate_queue.jsonl", ocr)
    write_csv(manifests / "ocr_trigger_candidate_queue.csv", ocr, COMMON_FIELDS + OCR_EXTRA_FIELDS)
    write_jsonl(manifests / "visual_unit_candidate_decision_manifest.jsonl", decisions)
    write_csv(manifests / "visual_unit_candidate_decision_manifest.csv", decisions, DECISION_FIELDS)
    black_rows = [r for r in rows if r.get("black_rejected")]
    black_fields = ["visual_unit_id", "media_type", "source_relative_path", "time_position_ms", "black_frame_status", "luma_mean", "luma_std", "black_pixel_ratio", "quality_error", "visual_file", "derived_path"]
    write_csv(reports / "black_or_invalid_visual_rejects.csv", black_rows, black_fields)
    label_counter = Counter(label_name(l) for r in rows for l in (r.get("labels") or []) if label_name(l))
    label_rows = [{"label": k, "count": v} for k, v in sorted(label_counter.items(), key=lambda kv: (-kv[1], kv[0]))]
    write_csv(reports / "yoloe_label_inventory.csv", label_rows, ["label", "count"])
    write_csv(reports / "video_budget_report.csv", video_reports, [])
    write_json(reports / "stop03_2_candidate_summary.json", summary)
    md = [
        "# Stop03-2 Candidate Queue Summary", "",
        f"- script_version: {summary.get('script_version')}",
        f"- validation_status: {summary.get('validation_status')}",
        f"- input_visual_units: {summary.get('input_visual_units')}",
        f"- qwenvl_total_count: {summary.get('qwenvl_total_count')}",
        f"- qwen_manual_seed_count: {summary.get('qwen_manual_seed_count')}",
        f"- qwen_video_frame_count: {summary.get('qwen_video_frame_count')}",
        f"- qwen_image_yoloe_count: {summary.get('qwen_image_yoloe_count')}",
        f"- ocr_total_count: {summary.get('ocr_total_count')}",
        f"- black_or_invalid_reject_count: {summary.get('black_or_invalid_reject_count')}",
        f"- black_leak_into_qwenvl_count: {summary.get('black_leak_into_qwenvl_count')}",
        f"- black_leak_into_ocr_count: {summary.get('black_leak_into_ocr_count')}",
        "", "本阶段不运行 YOLOE / OpenCLIP / Qwen-VL / OCR，只读数据库和派生预览/抽帧 JPG。",
    ]
    atomic_text(reports / "stop03_2_candidate_summary.md", "\n".join(md) + "\n")
    return {
        "qwenvl_csv": str(manifests / "qwenvl_high_value_candidate_queue.csv"),
        "qwenvl_jsonl": str(manifests / "qwenvl_high_value_candidate_queue.jsonl"),
        "ocr_csv": str(manifests / "ocr_trigger_candidate_queue.csv"),
        "ocr_jsonl": str(manifests / "ocr_trigger_candidate_queue.jsonl"),
        "decision_csv": str(manifests / "visual_unit_candidate_decision_manifest.csv"),
        "decision_jsonl": str(manifests / "visual_unit_candidate_decision_manifest.jsonl"),
        "black_rejects_csv": str(reports / "black_or_invalid_visual_rejects.csv"),
        "summary_json": str(reports / "stop03_2_candidate_summary.json"),
        "summary_md": str(reports / "stop03_2_candidate_summary.md"),
        "video_budget_report_csv": str(reports / "video_budget_report.csv"),
    }

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stop03-2 candidate queues from current DB, DB-safe, no model rerun")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--db-audit-only", action="store_true")
    p.add_argument("--clear-existing-candidate-items", action="store_true")
    p.add_argument("--max-qwen", type=int, default=QWENVL_MAX_GLOBAL_DEFAULT, help="Safety cap only; not a target count.")
    p.add_argument("--max-ocr", type=int, default=OCR_MAX_GLOBAL_DEFAULT, help="Safety cap only; not a target count.")
    p.add_argument("--top-qwen", type=int, default=None, help="Deprecated alias for --max-qwen.")
    p.add_argument("--top-ocr", type=int, default=None, help="Deprecated alias for --max-ocr.")
    p.add_argument("--video-budget-scale", type=float, default=1.0, help="Deprecated compatibility option; V6 uses dynamic segment rules.")
    p.add_argument("--image-yolo-threshold", type=float, default=GENERIC_IMAGE_SIGNAL_MIN_SCORE)
    p.add_argument("--no-image-quality-check", action="store_true", help="Disable derived JPG black-frame validation. Not recommended.")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    db = assert_db_path(args.db)
    out = assert_out_path(args.out)
    con = connect_db(db)
    preflight = runtime_preflight(con, db, out)

    if args.preflight_only:
        print(json.dumps({"validation_status": preflight["validation_status"], "runtime_preflight": preflight}, ensure_ascii=False, indent=2))
        return 0 if not preflight.get("blockers") else 2

    if args.db_audit_only:
        print(json.dumps({
            "script_version": SCRIPT_VERSION,
            "mode": "db_audit_only",
            "db_path": str(db),
            "runtime_preflight": preflight,
            "policy": "read DB only; no model rerun; no original media write; derived JPG quality check only in full run",
        }, ensure_ascii=False, indent=2))
        return 0 if not preflight.get("blockers") else 2

    blockers = preflight.get("blockers") or []
    if blockers:
        raise SystemExit("[BLOCKED] preflight failed: " + "; ".join(blockers))
    ensure_dir(out)
    ensure_dir(out / "manifests")
    ensure_dir(out / "reports")

    rows = load_units(con)
    run_id = f"{SCRIPT_VERSION}_{now_stamp()}_{os.getpid()}"
    model_run_upsert_start(con, run_id, len(rows))
    try:
        rows = [compute_scores(r) for r in rows]
        rows = attach_quality(rows, no_image_quality_check=bool(args.no_image_quality_check))
        # Recompute reject-only reasons after quality attaches.
        rows = [compute_scores(r) for r in rows]
        max_qwen = args.top_qwen if args.top_qwen is not None else args.max_qwen
        max_ocr = args.top_ocr if args.top_ocr is not None else args.max_ocr
        qwenvl, qdec, video_reports = select_qwenvl(rows, top_global=max_qwen, per_video_budget_scale=args.video_budget_scale, image_yolo_threshold=args.image_yolo_threshold)
        ocr, odec = select_ocr(rows, top_global=max_ocr)
        decisions = build_decisions(rows, qwenvl, ocr, qdec, odec)
        q_ids = {str(c["visual_unit_id"]) for c in qwenvl}
        o_ids = {str(c["visual_unit_id"]) for c in ocr}
        black_ids = {str(r["visual_unit_id"]) for r in rows if r.get("black_rejected")}
        summary = {
            "validation_status": "PASS" if qwenvl else "PASS_EMPTY_QWENVL",
            "script_version": SCRIPT_VERSION,
            "policy_version": POLICY_VERSION,
            "run_id": run_id,
            "input_visual_units": len(rows),
            "input_video_visual_units": sum(1 for r in rows if r.get("visual_unit_type") == "video_frame"),
            "input_image_visual_units": sum(1 for r in rows if r.get("visual_unit_type") != "video_frame"),
            "manual_seed_input_count": sum(1 for r in rows if r.get("manual_seed")),
            "qwenvl_total_count": len(qwenvl),
            "qwen_manual_seed_count": sum(1 for c in qwenvl if c.get("high_value_category") == "manual_finder_tag_image_seed"),
            "qwen_video_frame_count": sum(1 for c in qwenvl if c.get("high_value_category") == "video_high_value_segment_candidate"),
            "qwen_image_yoloe_count": sum(1 for c in qwenvl if c.get("high_value_category") == "image_generic_visual_signal_candidate"),
            "qwen_timelapse_count": sum(1 for c in qwenvl if "timelapse" in str(c.get("high_value_category") or "")),
            "qwen_category_counts": dict(Counter(str(c.get("high_value_category") or "") for c in qwenvl)),
            "ocr_media_type_counts": dict(Counter(str(c.get("media_type") or "") for c in ocr)),
            "qwenvl_media_type_counts": dict(Counter(str(c.get("media_type") or "") for c in qwenvl)),
            "ocr_total_count": len(ocr),
            "both_qwenvl_and_ocr_count": len(q_ids & o_ids),
            "neither_count": sum(1 for d in decisions if not d["is_qwenvl_candidate"] and not d["is_ocr_candidate"]),
            "black_or_invalid_reject_count": len(black_ids),
            "black_status_counts": dict(Counter(str(r.get("black_frame_status") or "") for r in rows)),
            "black_leak_into_qwenvl_count": len(q_ids & black_ids),
            "black_leak_into_ocr_count": len(o_ids & black_ids),
            "video_source_group_count": len({str(r.get("source_group_id")) for r in rows if r.get("visual_unit_type") == "video_frame"}),
            "video_selected_group_count": len({str(c.get("source_group_id")) for c in qwenvl if c.get("high_value_category") == "video_high_value_segment_candidate"}),
            "video_time_position_source_counts": dict(Counter(str(r.get("time_position_source") or "") for r in rows if r.get("visual_unit_type") == "video_frame")),
            "video_effective_time_negative_count": sum(1 for r in rows if r.get("visual_unit_type") == "video_frame" and int(r.get("time_position_ms") or -1) < 0),
            "video_overselect_review_group_count": sum(1 for vr in video_reports if vr.get("overselect_review_flag")),
            "settings": {
                "max_qwen_safety_cap": max_qwen,
                "max_ocr_safety_cap": max_ocr,
                "selection_mode": "generic_dynamic_reason_based_time_gap_dedup_not_percentage_not_fixed_target",
                "video_budget_scale": args.video_budget_scale,
                "image_yolo_threshold": args.image_yolo_threshold,
                "black_frame_check_enabled": not args.no_image_quality_check,
                "black_reject_rule": "mean<=8 and std<=5 OR black_pixel_ratio>=0.985 and mean<=16",
            },
            "runtime_preflight": preflight,
            "safety": preflight.get("safety"),
            "model_rerun": {"yoloe": False, "openclip": False, "qwen_vl": False, "ocr": False},
        }
        outputs = write_reports(out, rows, qwenvl, ocr, decisions, video_reports, summary)
        written = write_candidate_items_db(con, run_id, [*qwenvl, *ocr], clear_run_stage=bool(args.clear_existing_candidate_items))
        summary["candidate_rows_written_to_db"] = written
        summary["outputs"] = outputs
        write_json(out / "reports/stop03_2_candidate_summary.json", summary)
        model_run_finish(con, run_id, "done", len(qwenvl) + len(ocr), "")
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["black_leak_into_qwenvl_count"] == 0 and summary["black_leak_into_ocr_count"] == 0 else 2
    except Exception as e:  # noqa: BLE001
        model_run_finish(con, run_id, "failed", 0, f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
