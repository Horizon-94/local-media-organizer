from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from urllib.parse import quote
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def offline_search_environment(cache_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    # Embedded Python may expose additional PYTHON* variables depending on the
    # macOS/Xcode runtime.  A child virtual environment must derive its prefix
    # from its own executable, never from the app's interpreter state.
    for inherited in tuple(environment):
        if inherited.startswith("PYTHON") or inherited in {"__PYVENV_LAUNCHER__", "VIRTUAL_ENV"}:
            environment.pop(inherited, None)
    environment.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "DEVELOPER_DIR": "/Library/Developer/CommandLineTools",
        "HF_HOME": str(cache_root / "cache" / "huggingface"),
        "TORCH_HOME": str(cache_root / "cache" / "torch"),
        "XDG_CACHE_HOME": str(cache_root / "cache" / "xdg"),
    })
    return environment


@dataclass
class SearchJob:
    job_id: str
    query_sha256: str
    status: str
    created_at: float
    output_dir: Path
    log_path: Path
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    exit_code: int | None = None
    response_json: Path | None = None
    error: str = ""
    finished_at: float | None = None

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "query_sha256": self.query_sha256,
            "status": self.status,
            "created_at": self.created_at,
            "elapsed_seconds": round((self.finished_at or time.time()) - self.created_at, 2),
            "exit_code": self.exit_code,
            "results_ready": bool(self.response_json and self.response_json.is_file()),
            "error": self.error,
        }


class SearchJobManager:
    def __init__(
        self,
        *,
        db_path: Path,
        output_root: Path,
        search_script: Path,
        search_config: Path,
        embedding_python: Path,
        openclip_python: Path,
    ):
        self.db_path = Path(db_path).resolve()
        self.output_root = Path(output_root).resolve()
        self.search_script = Path(search_script).resolve()
        self.search_config = Path(search_config).resolve()
        # A virtualenv's ``bin/python`` is commonly a symlink to the base
        # interpreter.  Resolving it discards the venv identity and its
        # site-packages, which is fatal when an embedded macOS app launches a
        # second model runtime.  Preserve the absolute venv entry path.
        self.embedding_python = Path(embedding_python).expanduser().absolute()
        self.openclip_python = Path(openclip_python).expanduser().absolute()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, SearchJob] = {}
        self._lock = threading.Lock()

    def _database_preflight(self) -> tuple[sqlite3.Connection | None, dict[str, Any]]:
        """Open only the selected existing task database and report why it failed.

        ``mode=ro`` prevents a search from creating an empty SQLite file when
        a task pointer is stale.  A completed library on an external/APFS
        volume can reject SQLite's normal shared-lock sidecar; immutable mode
        remains safely query-only and is only tried after the file has already
        been proven to exist and be readable.
        """
        path = self.db_path
        report: dict[str, Any] = {
            "database_path": str(path),
            "database_path_absolute": path.is_absolute(),
            "database_exists": path.exists(),
            "database_is_file": path.is_file(),
            "database_readable": os.access(path, os.R_OK),
            "database_open_mode": "readonly_uri",
            "database_error": "",
        }
        if not (report["database_path_absolute"] and report["database_is_file"] and report["database_readable"]):
            report["database_error"] = "数据库文件不存在、不是普通文件或当前不可读"
            return None, report
        try:
            uri = f"file:{quote(path.as_posix())}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=5.0)
            con.execute("PRAGMA query_only=ON")
            # SQLite may create the connection before the volume rejects the
            # first shared-lock read.  Prove the selected task is genuinely
            # readable before reporting a normal readonly success.
            con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return con, report
        except sqlite3.Error as uri_error:
            try:
                con.close()
            except (UnboundLocalError, AttributeError):
                pass
            report["database_open_mode"] = "readonly_immutable_uri_fallback"
            report["database_uri_error"] = str(uri_error)
            try:
                immutable_uri = f"file:{quote(path.as_posix())}?mode=ro&immutable=1"
                con = sqlite3.connect(immutable_uri, uri=True, timeout=5.0)
                con.execute("PRAGMA query_only=ON")
                con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                return con, report
            except sqlite3.Error as native_error:
                report["database_error"] = str(native_error)
                return None, report

    def readiness(self) -> dict[str, Any]:
        paths = {
            "database": self.db_path,
            "search_script": self.search_script,
            "search_config": self.search_config,
            "embedding_python": self.embedding_python,
            "openclip_python": self.openclip_python,
        }
        checks = {key: path.is_file() for key, path in paths.items()}
        database_ready = False
        text_enrichment_ready = False
        text_vector_link_coverage = False
        uncovered_video_source_count = 0
        con, database_preflight = self._database_preflight()
        checks["database"] = bool(con is not None)
        if con is not None:
            try:
                objects = {
                    str(row[0]) for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                    )
                }
                required = {
                    "source_assets", "derived_assets", "visual_units", "embeddings",
                    "model_runs", "visual_labels", "visual_label_terms",
                }
                database_ready = required.issubset(objects) and bool(
                    con.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone()
                )
                if database_ready:
                    visual_units = int(con.execute(
                        "SELECT COUNT(*) FROM visual_units"
                    ).fetchone()[0] or 0)
                    embedding_columns = {
                        str(row[1]) for row in con.execute("PRAGMA table_info(embeddings)")
                    }
                    # Current libraries link vectors by visual_unit_id.  Keep
                    # older visual-only libraries searchable when their
                    # one-vector-per-unit table predates that explicit column.
                    embedded_units = int(con.execute(
                        "SELECT COUNT(DISTINCT visual_unit_id) FROM embeddings"
                        if "visual_unit_id" in embedding_columns
                        else "SELECT COUNT(*) FROM embeddings"
                    ).fetchone()[0] or 0)
                    checks["visual_vector_coverage"] = (
                        visual_units > 0 and embedded_units == visual_units
                    )
                    video_sources = int(con.execute(
                        "SELECT COUNT(*) FROM source_assets WHERE media_type='video'"
                    ).fetchone()[0] or 0)
                    video_covered = int(con.execute(
                        "SELECT COUNT(DISTINCT v.source_content_id) FROM visual_units v "
                        "JOIN source_assets s USING(source_content_id) WHERE s.media_type='video'"
                    ).fetchone()[0] or 0)
                    checks["video_source_coverage"] = video_sources == video_covered
                    uncovered_video_source_count = max(0, video_sources - video_covered)
                    # Unsupported or failed source videos remain visible as a
                    # warning, but must not disable search for the complete set
                    # of successfully derived visual units.
                    database_ready = database_ready and checks["visual_vector_coverage"]
                if {
                    "stop03_5d_text_vectors", "stop03_5d_text_embedding_runs",
                }.issubset(objects):
                    text_enrichment_ready = bool(con.execute(
                        "SELECT 1 FROM stop03_5d_text_embedding_runs "
                        "WHERE status='success' LIMIT 1"
                    ).fetchone())
                    if {
                        "stop03_5d_text_documents", "stop03_5d_document_vector_links",
                    }.issubset(objects):
                        documents = int(con.execute(
                            "SELECT COUNT(*) FROM stop03_5d_text_documents"
                        ).fetchone()[0] or 0)
                        links = int(con.execute(
                            "SELECT COUNT(*) FROM stop03_5d_document_vector_links"
                        ).fetchone()[0] or 0)
                        vectors = int(con.execute(
                            "SELECT COUNT(*) FROM stop03_5d_text_vectors WHERE status='success'"
                        ).fetchone()[0] or 0)
                        text_vector_link_coverage = (
                            documents > 0 and links == documents and vectors > 0
                        )
            except (OSError, sqlite3.Error) as exc:
                database_ready = False
                database_preflight["database_error"] = str(exc)
            finally:
                con.close()
        checks["database_search_ready"] = database_ready
        checks["text_enrichment_ready"] = text_enrichment_ready
        checks["text_vector_link_coverage"] = text_vector_link_coverage
        required_checks = (
            "database", "search_script", "search_config", "embedding_python",
            "openclip_python", "database_search_ready",
        )
        hybrid_ready = text_enrichment_ready and text_vector_link_coverage
        return {
            "ready": all(checks[key] for key in required_checks),
            "search_mode": "HYBRID_VISUAL_TEXT" if hybrid_ready else "VISUAL_ONLY",
            "checks": checks,
            "uncovered_video_source_count": uncovered_video_source_count,
            "database_preflight": database_preflight,
        }

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        query = " ".join(str(request.get("query", "")).split())
        if not (1 <= len(query) <= 512):
            raise ValueError("搜索文字长度必须在 1 到 512 个字符之间")
        readiness = self.readiness()
        if not readiness["ready"]:
            preflight = readiness.get("database_preflight", {})
            if preflight.get("database_error"):
                raise RuntimeError(
                    "搜索数据库不可用："
                    f"{preflight['database_error']}（{preflight.get('database_path', '')}）"
                )
            missing = [name for name, passed in readiness["checks"].items() if not passed]
            raise RuntimeError("搜索运行环境未通过预检：" + "、".join(missing))
        with self._lock:
            if any(job.status in {"queued", "running"} for job in self._jobs.values()):
                raise RuntimeError("已有一个搜索正在运行，请等待它完成")
            job_id = "ui5e_" + uuid.uuid4().hex[:24]
            job_root = self.output_root / "search_jobs" / job_id
            job_root.mkdir(parents=True, exist_ok=False)
            job = SearchJob(
                job_id=job_id,
                query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
                status="queued",
                created_at=time.time(),
                output_dir=job_root,
                log_path=job_root / "search.log",
            )
            self._jobs[job_id] = job

        command = self.build_command(query, request, job_root / "output")
        env = offline_search_environment(job_root)
        log_file = job.log_path.open("wb")
        try:
            job.process = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, env=env)
        except Exception:
            log_file.close()
            job.status = "failed"
            raise
        job.status = "running"
        threading.Thread(target=self._wait, args=(job, log_file), daemon=True).start()
        return job.public()

    def build_command(self, query: str, request: dict[str, Any], output: Path) -> list[str]:
        limit = max(1, min(int(request.get("limit", 30)), 200))
        offset = max(0, int(request.get("offset", 0)))
        temporal = max(0, min(int(request.get("temporal_dedup_ms", 5000)), 60000))
        preview = int(request.get("preview_window_ms", 10000))
        preview = preview if preview in {5000, 10000} else 10000
        command = [
            str(self.embedding_python), str(self.search_script),
            "--mode", "query", "--db", str(self.db_path),
            "--config", str(self.search_config), "--out", str(output),
            "--query", query, "--openclip-python", str(self.openclip_python),
            "--result-offset", str(offset), "--result-limit", str(limit),
            "--temporal-dedup-ms", str(temporal), "--preview-window-ms", str(preview),
            "--timecode-precision", "millisecond", "--device", str(request.get("device", "auto")),
            "--confirm-real-local-query", "--native-app-result-contract",
            "--native-readiness-verified",
        ]
        media_type = str(request.get("media_type", "all"))
        if media_type in {"image", "video"}:
            command.extend(["--media-type", media_type])
            command.append("--disable-audio-evidence")
        elif media_type == "audio":
            # Speech evidence is retained as text attached to its source video,
            # not as a standalone audio asset.
            command.extend(["--media-type", "video", "--audio-evidence-only"])
        else:
            # "all" is all visible media, not every internal evidence channel.
            command.append("--disable-audio-evidence")
        prefix = " ".join(str(request.get("path_prefix", "")).split())
        if prefix:
            command.extend(["--source-relative-path-prefix", prefix])
        for key, option in (
            ("source_mtime_min", "--source-mtime-min"),
            ("source_mtime_max", "--source-mtime-max"),
        ):
            if request.get(key) is not None:
                command.extend([option, str(int(request[key]))])
        if request.get("has_ocr"):
            command.append("--has-ocr")
        if request.get("has_person"):
            command.append("--has-person")
        return command

    def _wait(self, job: SearchJob, log_file: Any) -> None:
        try:
            assert job.process is not None
            job.exit_code = job.process.wait()
        finally:
            log_file.close()
            job.finished_at = time.time()
        candidates = sorted(job.output_dir.glob("query5ev2_*/reports/search_results.json"))
        if job.exit_code == 0 and len(candidates) == 1:
            job.response_json = candidates[0]
            job.status = "success"
        else:
            job.status = "failed"
            job.error = f"搜索退出码 {job.exit_code}，结果文件数量 {len(candidates)}"

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.public() if job else None

    def results(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            path = job.response_json if job else None
        if not path or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        public_results = []
        for row in payload.get("result_items", payload.get("results", [])):
            keep = {
                key: row.get(key) for key in (
                    "result_id", "source_content_id", "visual_unit_id", "derived_id",
                    "source_relative_path", "media_type", "frame_index", "time_position_ms",
                    "timecode", "preview_segment_start_ms", "preview_segment_end_ms",
                    "preview_segment_start_timecode", "preview_segment_end_timecode",
                    "hybrid_score", "openclip_cosine", "text_semantic_score", "text_evidence_present",
                    "text_preview", "yoloe_labels", "environment_label",
                    "environment_user_confirmation_required",
                    "audio_transcript_match", "audio_evidence_id", "audio_start_time_ms",
                    "audio_end_time_ms", "audio_hit_time_ms",
                )
            }
            keep["preview_url"] = f"/api/preview/{row.get('derived_id')}"
            if row.get("media_type") == "video":
                keep["media_url"] = f"/api/media/{row.get('source_content_id')}"
            public_results.append(keep)
        return {
            "job": job.public() if job else None,
            "status": payload.get("status"),
            "technical_status": payload.get("technical_status", payload.get("status")),
            "policy_status": payload.get("policy_status"),
            "runtime": payload.get("runtime", {}),
            "coverage": {
                "eligible_visual_unit_count": payload.get("eligible_visual_unit_count", 0),
                "scanned_visual_vector_count": payload.get("scanned_visual_vector_count", 0),
                "scanned_text_vector_count": payload.get("scanned_text_vector_count", 0),
            },
            "ranking": payload.get("ranking", {
                "post_temporal_dedup_result_count": payload.get("result_total_count", 0),
                "result_offset": payload.get("result_offset", 0),
                "result_limit": payload.get("result_limit", 0),
                "next_result_offset": payload.get("next_result_offset"),
            }),
            "results": public_results,
        }

    def stop_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.process and job.process.poll() is None:
                job.process.terminate()
