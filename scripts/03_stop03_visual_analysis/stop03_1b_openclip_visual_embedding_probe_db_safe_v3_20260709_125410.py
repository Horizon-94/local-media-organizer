#!/usr/bin/env python3
"""
Stop03-1B OpenCLIP visual embedding - DB safe runner.

Purpose:
- Read visual_units from the project SQLite database.
- Load OpenCLIP only from the local safetensors path registered in the local model registry.
- Write embedding metadata back to embeddings table.
- Write vectors as JSONL under the allowed test-output directory and store vector_key in SQLite.

Hard constraints:
- No network.
- No download.
- No dependency install.
- No original media write.
- No manifest-driven input in default mode.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import socket
import sqlite3
import statistics
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_VERSION = "stop03_1b_openclip_visual_embedding_probe_db_safe_v3_20260709_125410"

PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
TEST_OUTPUT_ROOT = Path("/Users/yourname/Documents/AI-Local/test-output")
DOWNLOAD_SCRIPT_ROOT = Path("/Users/yourname/Downloads/pyjiaoben")
SOURCE_MEDIA_ROOT = Path("/Users/yourname/Documents/MEDIA_ARCHIVE_TEST_SOURCE")
MODEL_ROOT = Path("/Users/yourname/Documents/model")
DEFAULT_DB = PROJECT_ROOT / "media_archive.sqlite"
DEFAULT_OUT = TEST_OUTPUT_ROOT / "stop03-1b-openclip-db-safe-v3_20260709_125410-smoke"
REGISTRY_FILES = [
    PROJECT_ROOT / "docs/model_registry/LOCAL_MODEL_REGISTRY.md",
    PROJECT_ROOT / "docs/model_registry/LOCAL_RUNTIME_MODEL_SCRIPT_INVENTORY.md",
]
DEFAULT_MODEL_PATH = MODEL_ROOT / "openclip-vit-b-32-laion2b-s34b-b79k/open_clip_model.safetensors"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}

# Worker globals.
MODEL = None
PREPROCESS = None
DEVICE = None
MODEL_NAME = None
MODEL_PATH = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fail(code: str, message: str, detail: Optional[str] = None, exit_code: int = 2) -> None:
    payload = {"status": code, "message": message}
    if detail:
        payload["detail"] = detail
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    raise SystemExit(exit_code)


def stable_id(prefix: str, *parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", errors="ignore"))
        h.update(b"\0")
    return f"{prefix}_{h.hexdigest()[:32]}"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def resolve_path(p: Path) -> Path:
    return p.expanduser().resolve(strict=False)


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def assert_under_allowed_output(path: Path) -> Path:
    rp = resolve_path(path)
    allowed = [resolve_path(TEST_OUTPUT_ROOT), resolve_path(PROJECT_ROOT / "test-output"), resolve_path(PROJECT_ROOT / "outputs")]
    if is_relative_to(rp, resolve_path(SOURCE_MEDIA_ROOT)):
        fail("BLOCKED_OUTPUT_IN_SOURCE_MEDIA", "Output path is inside source media root.", str(rp))
    if not any(is_relative_to(rp, root) for root in allowed):
        fail(
            "BLOCKED_OUTPUT_PATH",
            "Output path must be under test-output or project outputs.",
            str(rp),
        )
    return rp


def assert_db_path(path: Path) -> Path:
    rp = resolve_path(path)
    allowed = [resolve_path(PROJECT_ROOT), resolve_path(TEST_OUTPUT_ROOT)]
    if is_relative_to(rp, resolve_path(SOURCE_MEDIA_ROOT)):
        fail("BLOCKED_DB_IN_SOURCE_MEDIA", "Database path is inside source media root.", str(rp))
    if not any(is_relative_to(rp, root) for root in allowed):
        fail("BLOCKED_DB_PATH", "Database path is outside project/test-output roots.", str(rp))
    if not rp.exists():
        fail("BLOCKED_MISSING_DB", "SQLite database does not exist.", str(rp))
    return rp


def assert_model_path(path: Path) -> Path:
    rp = resolve_path(path)
    if not is_relative_to(rp, resolve_path(MODEL_ROOT)):
        fail("BLOCKED_MODEL_OUTSIDE_MODEL_ROOT", "Model must be under /Users/yourname/Documents/model.", str(rp))
    if not rp.exists():
        fail("BLOCKED_MISSING_LOCAL_MODEL", "OpenCLIP local safetensors file does not exist.", str(rp))
    if rp.suffix.lower() != ".safetensors":
        fail("BLOCKED_UNEXPECTED_MODEL_FILE", "OpenCLIP model file must be local .safetensors.", str(rp))
    size_mb = rp.stat().st_size / 1024 / 1024
    if size_mb < 100:
        fail("BLOCKED_MODEL_TOO_SMALL", "OpenCLIP model file is unexpectedly small.", f"{rp} size_mb={size_mb:.2f}")
    return rp


def install_offline_env() -> None:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")


def install_network_guard() -> None:
    """Fail fast for accidental network attempts in this process.

    This is intentionally conservative. It patches common Python network paths.
    Worker processes call this again in init_worker.
    """

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("BLOCKED_NETWORK_CALL: this script is offline-only")

    socket.create_connection = blocked  # type: ignore[assignment]
    socket.socket.connect = blocked  # type: ignore[assignment]

    try:
        import urllib.request  # noqa

        urllib.request.urlopen = blocked  # type: ignore[assignment]
        urllib.request.urlretrieve = blocked  # type: ignore[assignment]
    except Exception:
        pass

    try:
        import requests  # type: ignore

        requests.get = blocked  # type: ignore[attr-defined]
        requests.post = blocked  # type: ignore[attr-defined]
        requests.request = blocked  # type: ignore[attr-defined]
    except Exception:
        pass


def read_registry_text() -> str:
    texts = []
    missing = []
    for p in REGISTRY_FILES:
        if p.exists():
            texts.append(p.read_text(encoding="utf-8", errors="ignore"))
        else:
            missing.append(str(p))
    if missing:
        fail("BLOCKED_MISSING_REGISTRY", "Required model registry file is missing.", "; ".join(missing))
    return "\n".join(texts)


def resolve_openclip_model_from_registry(explicit_model: Optional[str]) -> Path:
    registry = read_registry_text()
    if explicit_model:
        model_path = Path(explicit_model)
    else:
        # Use the registered formal OpenCLIP safetensors path. Do not infer a remote model name.
        target = str(DEFAULT_MODEL_PATH)
        if target not in registry:
            fail(
                "BLOCKED_OPENCLIP_NOT_IN_REGISTRY",
                "Expected OpenCLIP safetensors path is not present in local registry.",
                target,
            )
        model_path = DEFAULT_MODEL_PATH
    return assert_model_path(model_path)


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def require_columns(conn: sqlite3.Connection, table: str, cols: Sequence[str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    missing = [c for c in cols if c not in existing]
    if missing:
        fail("BLOCKED_DB_SCHEMA_MISMATCH", f"Missing columns in {table}.", ", ".join(missing))


def ensure_db_contract(conn: sqlite3.Connection, create_runtime_tables: bool = False) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ["visual_units", "embeddings"]:
        if t not in tables:
            fail("BLOCKED_DB_SCHEMA_MISMATCH", f"Missing required table: {t}.")

    require_columns(
        conn,
        "visual_units",
        ["visual_unit_id", "source_content_id", "visual_file", "time_position_ms"],
    )
    require_columns(
        conn,
        "embeddings",
        [
            "embedding_id",
            "visual_unit_id",
            "source_content_id",
            "model_name",
            "model_path",
            "dimension",
            "vector_key",
            "run_id",
        ],
    )

    if not create_runtime_tables:
        return

    # Extra run/error tables are local metadata for traceability. They do not touch source media.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_runs (
            run_id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_path TEXT NOT NULL,
            script_version TEXT NOT NULL,
            script_path TEXT,
            input_count INTEGER NOT NULL DEFAULT 0,
            output_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processing_errors (
            error_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            visual_unit_id TEXT,
            source_content_id TEXT,
            error_type TEXT NOT NULL,
            error_message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def db_audit(db_path: Path) -> Dict[str, Any]:
    conn = connect_db(db_path)
    ensure_db_contract(conn, create_runtime_tables=False)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    counts = {}
    for t in ["source_assets", "derived_assets", "visual_units", "embeddings", "visual_labels", "model_runs", "processing_errors"]:
        if t in tables:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    sample = [dict(r) for r in conn.execute("SELECT visual_unit_id, source_content_id, visual_file, time_position_ms FROM visual_units LIMIT 5")]
    conn.close()
    return {"db_path": str(db_path), "tables": tables, "counts": counts, "visual_units_sample": sample}


@dataclass
class VisualUnit:
    visual_unit_id: str
    source_content_id: str
    visual_file: str
    time_position_ms: int


def fetch_pending_visual_units(
    conn: sqlite3.Connection,
    model_name: str,
    model_path: Path,
    limit: int,
    include_existing: bool,
) -> List[VisualUnit]:
    params: List[Any] = [model_name, str(model_path)]
    where = ""
    if not include_existing:
        where = """
        WHERE NOT EXISTS (
            SELECT 1 FROM embeddings e
            WHERE e.visual_unit_id = vu.visual_unit_id
              AND e.model_name = ?
              AND e.model_path = ?
        )
        """
    else:
        params = []

    sql = f"""
        SELECT vu.visual_unit_id, vu.source_content_id, vu.visual_file, vu.time_position_ms
        FROM visual_units vu
        {where}
        ORDER BY vu.visual_unit_id
    """
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    rows = []
    for r in conn.execute(sql, params):
        rows.append(
            VisualUnit(
                visual_unit_id=str(r["visual_unit_id"]),
                source_content_id=str(r["source_content_id"]),
                visual_file=str(r["visual_file"]),
                time_position_ms=int(r["time_position_ms"]),
            )
        )
    return rows


def monitor_resources(stop_event: Event, out_csv: Path, interval: float) -> None:
    try:
        import psutil  # type: ignore
    except Exception:
        return

    fields = [
        "timestamp",
        "elapsed_seconds",
        "process_cpu_cores_estimated",
        "process_rss_mb_sum",
        "child_process_count",
        "system_cpu_percent",
        "system_memory_percent",
        "swap_used_mb",
    ]
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        try:
            root = psutil.Process(os.getpid())
            for p in [root] + root.children(recursive=True):
                try:
                    p.cpu_percent(interval=None)
                except Exception:
                    pass
        except Exception:
            pass

        while not stop_event.is_set():
            time.sleep(max(1.0, interval))
            cpu_sum = rss_sum = 0.0
            child_count = 0
            sys_cpu = sys_mem = swap_mb = ""
            try:
                root = psutil.Process(os.getpid())
                procs = [root] + root.children(recursive=True)
                child_count = max(0, len(procs) - 1)
                for p in procs:
                    try:
                        cpu_sum += p.cpu_percent(interval=None)
                        rss_sum += p.memory_info().rss / 1024 / 1024
                    except Exception:
                        pass
                vm = psutil.virtual_memory()
                sm = psutil.swap_memory()
                sys_cpu = psutil.cpu_percent(interval=None)
                sys_mem = vm.percent
                swap_mb = sm.used / 1024 / 1024
            except Exception:
                pass
            w.writerow(
                {
                    "timestamp": now_iso(),
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "process_cpu_cores_estimated": round(cpu_sum / 100.0, 3),
                    "process_rss_mb_sum": round(rss_sum, 3),
                    "child_process_count": child_count,
                    "system_cpu_percent": sys_cpu,
                    "system_memory_percent": sys_mem,
                    "swap_used_mb": round(swap_mb, 3) if swap_mb != "" else "",
                }
            )
            f.flush()


def init_worker(model_path: str, model_name: str, device: str) -> None:
    global MODEL, PREPROCESS, DEVICE, MODEL_NAME, MODEL_PATH
    install_offline_env()
    install_network_guard()

    import torch  # type: ignore
    import open_clip  # type: ignore
    from safetensors.torch import load_file  # type: ignore

    if device == "auto":
        DEVICE = "mps" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "cpu"
    else:
        DEVICE = device

    MODEL_NAME = model_name
    MODEL_PATH = model_path

    # Critical: pretrained=None prevents open_clip from resolving/downloading a pretrained tag.
    model, _, preprocess = open_clip.create_model_and_transforms(
        model_name,
        pretrained=None,
        device=DEVICE,
    )
    state = load_file(model_path, device="cpu")
    missing, unexpected = model.load_state_dict(state, strict=False)
    model = model.to(DEVICE)
    model.eval()
    MODEL = model
    PREPROCESS = preprocess


def infer_one(row: Dict[str, Any]) -> Dict[str, Any]:
    global MODEL, PREPROCESS, DEVICE, MODEL_NAME, MODEL_PATH
    start = time.perf_counter()
    out = dict(row)
    out.update(
        {
            "script_version": SCRIPT_VERSION,
            "worker_pid": os.getpid(),
            "device": DEVICE,
            "model_name": MODEL_NAME,
            "model_path": MODEL_PATH,
            "status": "failed",
            "start_time": now_iso(),
            "end_time": "",
            "elapsed_ms": None,
            "visual_file_sha256": "",
            "embedding_dim": None,
            "embedding_norm": None,
            "embedding_vector_sha256": "",
            "embedding_vector": None,
            "error_message": "",
        }
    )
    try:
        import torch  # type: ignore
        from PIL import Image  # type: ignore

        p = Path(str(row["visual_file"]))
        if not p.exists():
            raise FileNotFoundError(f"visual_file not found: {p}")
        if p.suffix.lower() not in IMAGE_EXTS:
            raise ValueError(f"unsupported image extension: {p.suffix}")

        out["visual_file_sha256"] = sha256_file(p)
        image = Image.open(p).convert("RGB")
        tensor = PREPROCESS(image).unsqueeze(0).to(DEVICE)
        with torch.no_grad():
            emb = MODEL.encode_image(tensor)
            emb = emb / emb.norm(dim=-1, keepdim=True)
        vec = emb.detach().cpu().float().numpy()[0].tolist()
        packed = json.dumps(vec, ensure_ascii=False, separators=(",", ":"))
        out.update(
            {
                "status": "success",
                "embedding_dim": len(vec),
                "embedding_norm": round(sum(x * x for x in vec) ** 0.5, 8),
                "embedding_vector_sha256": hashlib.sha256(packed.encode("utf-8")).hexdigest(),
                "embedding_vector": vec,
            }
        )
    except Exception as e:
        out["error_message"] = str(e) + "\n" + traceback.format_exc(limit=5)
    finally:
        out["end_time"] = now_iso()
        out["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 3)
    return out


def pct(vals: Sequence[float], q: float) -> Optional[float]:
    if not vals:
        return None
    vals = sorted(vals)
    return vals[int(round((len(vals) - 1) * q))]


def summarize_resource(resource_csv: Path) -> Tuple[float, float, float]:
    max_cpu = max_rss = max_swap = 0.0
    if not resource_csv.exists():
        return max_cpu, max_rss, max_swap
    with resource_csv.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            for key, target in []:
                pass
            try:
                max_cpu = max(max_cpu, float(r.get("process_cpu_cores_estimated") or 0))
            except Exception:
                pass
            try:
                max_rss = max(max_rss, float(r.get("process_rss_mb_sum") or 0))
            except Exception:
                pass
            try:
                max_swap = max(max_swap, float(r.get("swap_used_mb") or 0))
            except Exception:
                pass
    return round(max_cpu, 3), round(max_rss, 3), round(max_swap, 3)


def insert_run(conn: sqlite3.Connection, run_id: str, model_name: str, model_path: Path, script_path: str, input_count: int) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO model_runs
        (run_id, stage, model_name, model_path, script_version, script_path, input_count, output_count, status, started_at, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)
        """,
        (
            run_id,
            "stop03_1b_openclip_visual_embedding",
            model_name,
            str(model_path),
            SCRIPT_VERSION,
            script_path,
            input_count,
            "running",
            now_iso(),
        ),
    )
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: str, status: str, output_count: int, error_message: Optional[str]) -> None:
    conn.execute(
        """
        UPDATE model_runs
        SET status=?, output_count=?, finished_at=?, error_message=?
        WHERE run_id=?
        """,
        (status, output_count, now_iso(), error_message, run_id),
    )
    conn.commit()


def write_db_results(
    db_path: Path,
    run_id: str,
    results: Sequence[Dict[str, Any]],
    vector_jsonl: Path,
    model_name: str,
    model_path: Path,
) -> Tuple[int, int]:
    conn = connect_db(db_path)
    success = failed = 0
    with vector_jsonl.open("a", encoding="utf-8") as vf:
        for r in results:
            visual_unit_id = str(r.get("visual_unit_id"))
            source_content_id = str(r.get("source_content_id"))
            if r.get("status") == "success":
                success += 1
                embedding_id = stable_id("emb", visual_unit_id, model_name, model_path, r.get("embedding_vector_sha256"), run_id)
                vector_key = f"jsonl:{vector_jsonl}#{embedding_id}"
                vf.write(
                    json.dumps(
                        {
                            "embedding_id": embedding_id,
                            "visual_unit_id": visual_unit_id,
                            "source_content_id": source_content_id,
                            "model_name": model_name,
                            "model_path": str(model_path),
                            "dimension": int(r.get("embedding_dim") or 0),
                            "vector_sha256": r.get("embedding_vector_sha256"),
                            "vector": r.get("embedding_vector"),
                            "run_id": run_id,
                            "created_at": now_iso(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO embeddings
                    (embedding_id, visual_unit_id, source_content_id, model_name, model_path, dimension, vector_key, run_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        embedding_id,
                        visual_unit_id,
                        source_content_id,
                        model_name,
                        str(model_path),
                        int(r.get("embedding_dim") or 0),
                        vector_key,
                        run_id,
                    ),
                )
            else:
                failed += 1
                error_id = stable_id("err", run_id, visual_unit_id, r.get("error_message"), time.time_ns())
                conn.execute(
                    """
                    INSERT OR REPLACE INTO processing_errors
                    (error_id, run_id, stage, visual_unit_id, source_content_id, error_type, error_message, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        error_id,
                        run_id,
                        "stop03_1b_openclip_visual_embedding",
                        visual_unit_id,
                        source_content_id,
                        "embedding_failed",
                        str(r.get("error_message") or ""),
                        now_iso(),
                    ),
                )
        conn.commit()
    conn.close()
    return success, failed


def write_summary(out_dir: Path, summary: Dict[str, Any]) -> None:
    reports = out_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "stop03_1b_openclip_db_safe_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = ["# Stop03-1B OpenCLIP DB Safe Summary", ""]
    for k, v in summary.items():
        if k != "outputs":
            lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Outputs")
    for k, v in summary.get("outputs", {}).items():
        lines.append(f"- {k}: `{v}`")
    (reports / "stop03_1b_openclip_db_safe_summary.md").write_text("\n".join(lines), encoding="utf-8")


def run_db_audit(args: argparse.Namespace) -> None:
    db_path = assert_db_path(Path(args.db))
    audit = db_audit(db_path)
    print(json.dumps({"script_version": SCRIPT_VERSION, "mode": "db_audit_only", **audit}, ensure_ascii=False, indent=2))


def run_main(args: argparse.Namespace) -> None:
    install_offline_env()
    install_network_guard()

    db_path = assert_db_path(Path(args.db))
    out_dir = assert_under_allowed_output(Path(args.out))
    model_path = resolve_openclip_model_from_registry(args.model)

    if args.telemetry_interval < 1.0:
        fail("BLOCKED_BAD_INTERVAL", "telemetry interval must be >= 1.0 seconds.", str(args.telemetry_interval))
    if args.workers < 1 or args.workers > 8:
        fail("BLOCKED_BAD_WORKERS", "workers must be between 1 and 8.", str(args.workers))

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = out_dir / "manifests"
    telemetry_dir = out_dir / "telemetry"
    vectors_dir = out_dir / "vectors"
    for d in [manifest_dir, telemetry_dir, vectors_dir]:
        d.mkdir(parents=True, exist_ok=True)

    conn = connect_db(db_path)
    ensure_db_contract(conn, create_runtime_tables=True)
    rows = fetch_pending_visual_units(
        conn,
        model_name=args.model_name,
        model_path=model_path,
        limit=args.limit,
        include_existing=args.include_existing,
    )

    run_id = f"{SCRIPT_VERSION}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}"
    insert_run(conn, run_id, args.model_name, model_path, str(Path(__file__).resolve(strict=False)), len(rows))
    conn.close()

    input_jsonl = manifest_dir / "stop03_1b_openclip_db_input_visual_units.jsonl"
    result_jsonl = manifest_dir / "stop03_1b_openclip_db_result_manifest.jsonl"
    result_csv = manifest_dir / "stop03_1b_openclip_db_result_manifest.csv"
    vector_jsonl = vectors_dir / "openclip_vectors.jsonl"
    resource_csv = telemetry_dir / "resource_samples.csv"

    with input_jsonl.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r.__dict__, ensure_ascii=False) + "\n")

    print("== Stop03-1B OpenCLIP DB safe start ==")
    print(json.dumps(
        {
            "script_version": SCRIPT_VERSION,
            "db": str(db_path),
            "out": str(out_dir),
            "model_path": str(model_path),
            "model_name": args.model_name,
            "workers": args.workers,
            "device": args.device,
            "pending_visual_units": len(rows),
            "limit": args.limit,
        },
        ensure_ascii=False,
        indent=2,
    ))

    if not rows:
        summary = {
            "script_version": SCRIPT_VERSION,
            "run_id": run_id,
            "status": "blocked_empty_visual_units",
            "reason": "visual_units table has no pending rows for this model/path",
            "db_path": str(db_path),
            "model_path": str(model_path),
            "input_visual_units": 0,
            "success_count": 0,
            "failed_count": 0,
            "safety": {
                "network": "not_used",
                "download": "not_used",
                "dependency_install": "not_used",
                "source_media_write": "not_used",
                "model_loading": "not_started_because_no_rows",
            },
            "outputs": {"input_jsonl": str(input_jsonl)},
        }
        write_summary(out_dir, summary)
        conn2 = connect_db(db_path)
        finish_run(conn2, run_id, "blocked_empty_visual_units", 0, summary["reason"])
        conn2.close()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    stop_event = Event()
    monitor = Thread(target=monitor_resources, args=(stop_event, resource_csv, args.telemetry_interval), daemon=True)
    monitor.start()

    wall_start = time.perf_counter()
    processed: List[Dict[str, Any]] = []
    fields = [
        "visual_unit_id",
        "source_content_id",
        "visual_file",
        "time_position_ms",
        "script_version",
        "worker_pid",
        "device",
        "model_name",
        "model_path",
        "status",
        "start_time",
        "end_time",
        "elapsed_ms",
        "visual_file_sha256",
        "embedding_dim",
        "embedding_norm",
        "embedding_vector_sha256",
        "error_message",
    ]

    try:
        with result_jsonl.open("w", encoding="utf-8") as jf, result_csv.open("w", encoding="utf-8", newline="") as cf:
            cw = csv.DictWriter(cf, fieldnames=fields, extrasaction="ignore")
            cw.writeheader()
            with ProcessPoolExecutor(
                max_workers=args.workers,
                initializer=init_worker,
                initargs=(str(model_path), args.model_name, args.device),
            ) as ex:
                futures = [ex.submit(infer_one, r.__dict__) for r in rows]
                for i, fut in enumerate(as_completed(futures), 1):
                    r = fut.result()
                    processed.append(r)
                    clean = dict(r)
                    clean.pop("embedding_vector", None)
                    jf.write(json.dumps(clean, ensure_ascii=False) + "\n")
                    jf.flush()
                    cw.writerow(clean)
                    cf.flush()
                    if i % 50 == 0 or i == len(rows):
                        ok = sum(1 for x in processed if x.get("status") == "success")
                        bad = len(processed) - ok
                        print(f"[progress] {i}/{len(rows)} success={ok} failed={bad}")
    finally:
        stop_event.set()
        monitor.join(timeout=5)

    db_success, db_failed = write_db_results(db_path, run_id, processed, vector_jsonl, args.model_name, model_path)
    wall_seconds = round(time.perf_counter() - wall_start, 3)
    elapsed = [float(r["elapsed_ms"]) for r in processed if r.get("status") == "success" and r.get("elapsed_ms") is not None]
    dims = sorted({int(r["embedding_dim"]) for r in processed if r.get("status") == "success" and r.get("embedding_dim")})
    max_cpu, max_rss, max_swap = summarize_resource(resource_csv)
    status = "success" if db_success > 0 and db_failed == 0 else ("partial_failed" if db_success > 0 else "failed")

    summary = {
        "script_version": SCRIPT_VERSION,
        "run_id": run_id,
        "status": status,
        "db_path": str(db_path),
        "model_name": args.model_name,
        "model_path": str(model_path),
        "device": args.device,
        "workers": args.workers,
        "input_visual_units": len(rows),
        "processed_this_run": len(processed),
        "success_count": db_success,
        "failed_count": db_failed,
        "wall_seconds": wall_seconds,
        "avg_task_ms": round(statistics.mean(elapsed), 3) if elapsed else None,
        "p50_task_ms": round(pct(elapsed, 0.5), 3) if elapsed else None,
        "p90_task_ms": round(pct(elapsed, 0.9), 3) if elapsed else None,
        "embedding_dim": dims[0] if len(dims) == 1 else dims,
        "max_process_cpu_cores_estimated": max_cpu,
        "max_process_rss_mb_sum": max_rss,
        "max_swap_used_mb": max_swap,
        "safety": {
            "network": "blocked_by_network_guard",
            "download": "not_used",
            "dependency_install": "not_used",
            "source_media_write": "not_used",
            "model_loading": "local_safetensors_only",
        },
        "outputs": {
            "input_jsonl": str(input_jsonl),
            "result_jsonl": str(result_jsonl),
            "result_csv": str(result_csv),
            "vector_jsonl": str(vector_jsonl),
            "resource_csv": str(resource_csv),
        },
    }
    write_summary(out_dir, summary)
    conn3 = connect_db(db_path)
    finish_run(conn3, run_id, status, db_success, None if status == "success" else f"failed_count={db_failed}")
    conn3.close()
    print("== Stop03-1B OpenCLIP DB safe finished ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--model", default=None)
    ap.add_argument("--model-name", default="ViT-B-32")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--device", default="auto", choices=["auto", "mps", "cpu"])
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--include-existing", action="store_true")
    ap.add_argument("--telemetry-interval", type=float, default=2.0)
    ap.add_argument("--db-audit-only", action="store_true")
    args = ap.parse_args()

    if args.db_audit_only:
        run_db_audit(args)
    else:
        run_main(args)


if __name__ == "__main__":
    main()
