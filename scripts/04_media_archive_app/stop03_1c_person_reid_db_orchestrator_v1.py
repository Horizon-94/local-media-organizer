#!/usr/bin/env python3
"""Local-only anonymous person re-identification over existing derived images."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import resource
import shutil
import socket
import sqlite3
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


VERSION = "stop03_1c_person_reid_db_orchestrator_v1"
CONTRACT_VERSION = "stop03_1c_person_reid_db_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "configs/stop03_1c_person_reid_db_v1.json"
DEFAULT_MIGRATION = PROJECT_ROOT / "migrations/20260726_stop03_1c_person_reid_db_v1.sql"
TERMINAL = {"success", "no_face"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def stable_id(prefix: str, *parts: Any, size: int = 28) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return prefix + hashlib.sha256(payload).hexdigest()[:size]


def readonly_connection(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def writable_connection(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: Mapping[str, Any], lock: threading.Lock) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, ensure_ascii=False) + "\n"
    with lock, path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def ensure_under(path: Path, root: Path) -> None:
    resolved = path.expanduser().resolve()
    allowed = root.expanduser().resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise RuntimeError(f"output_outside_allowed_root:{resolved}")


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "contract_version": CONTRACT_VERSION,
        "runtime_backend": "opencv_dnn_cpu",
        "input_scope": "all_searchable_derived_visual_units",
        "original_media_read": False,
        "persist_face_crops": False,
        "network_policy": "blocked",
        "scheduling_mode": "dynamic_database_claim",
        "worker_model_load_policy": "once_per_worker",
        "same_visual_unit_cannot_link": True,
        "anonymous_identity_only": True,
        "fixed_dataset_counts": False,
    }
    mismatch = {
        key: {"actual": value.get(key), "expected": expected}
        for key, expected in required.items()
        if value.get(key) != expected
    }
    if mismatch:
        raise RuntimeError("person_reid_config_mismatch:" + canonical_json(mismatch))
    if int(value["embedding_dimension"]) <= 0:
        raise RuntimeError("person_reid_embedding_dimension_invalid")
    if not 0 < float(value["review_cosine_min"]) <= float(value["auto_merge_cosine_min"]) < 1:
        raise RuntimeError("person_reid_similarity_threshold_invalid")
    return value


def model_inventory(config: Mapping[str, Any]) -> dict[str, Any]:
    model_dir = Path(str(config["model_dir"])).expanduser().resolve()
    detector = model_dir / str(config["detector_file"])
    recognizer = model_dir / str(config["recognizer_file"])
    missing = [str(path) for path in (detector, recognizer) if not path.is_file()]
    if missing:
        raise RuntimeError("person_reid_model_missing:" + ",".join(missing))
    return {
        "model_dir": str(model_dir),
        "detector_path": str(detector),
        "recognizer_path": str(recognizer),
        "detector_size_bytes": detector.stat().st_size,
        "recognizer_size_bytes": recognizer.stat().st_size,
        "detector_sha256": sha256_file(detector),
        "recognizer_sha256": sha256_file(recognizer),
    }


def eligible_visual_units(db: Path) -> list[dict[str, Any]]:
    con = readonly_connection(db)
    try:
        rows = con.execute(
            """
            SELECT vu.visual_unit_id,vu.source_content_id,vu.derived_id,
                   vu.visual_file,vu.time_position_ms,sa.media_type,
                   sa.absolute_path AS source_absolute_path
            FROM visual_units AS vu
            JOIN source_assets AS sa
              ON sa.source_content_id=vu.source_content_id
            WHERE sa.media_type IN ('image','video')
              AND COALESCE(sa.online_status,1)=1
              AND COALESCE(sa.is_deleted_or_missing,0)=0
              AND COALESCE(vu.near_black,0)=0
            ORDER BY vu.visual_unit_id
            """
        ).fetchall()
    finally:
        con.close()
    values: list[dict[str, Any]] = []
    for row in rows:
        visual = Path(str(row["visual_file"])).expanduser()
        source = Path(str(row["source_absolute_path"])).expanduser()
        values.append({
            "visual_unit_id": str(row["visual_unit_id"]),
            "source_content_id": str(row["source_content_id"]),
            "derived_id": str(row["derived_id"]),
            "visual_file": str(visual),
            "source_absolute_path": str(source),
            "time_position_ms": int(row["time_position_ms"]),
            "media_type": str(row["media_type"]),
            "visual_exists": visual.is_file(),
            "visual_suffix_allowed": visual.suffix.lower() in IMAGE_SUFFIXES,
            "is_original_path": visual.resolve(strict=False) == source.resolve(strict=False),
            "visual_size_bytes": visual.stat().st_size if visual.is_file() else 0,
            "visual_mtime_ns": visual.stat().st_mtime_ns if visual.is_file() else 0,
        })
    return values


def build_preflight(
    db: Path,
    config_path: Path,
    migration: Path,
    backend: str = "opencv",
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    config = load_config(config_path)
    inventory = model_inventory(config)
    if not migration.is_file():
        raise RuntimeError(f"person_reid_migration_missing:{migration}")
    rows = eligible_visual_units(db)
    missing = [row["visual_unit_id"] for row in rows if not row["visual_exists"]]
    invalid_suffix = [row["visual_unit_id"] for row in rows if not row["visual_suffix_allowed"]]
    original_paths = [row["visual_unit_id"] for row in rows if row["is_original_path"]]
    runtime = {"cv2": False, "numpy": False, "opencv_dnn": False}
    try:
        import cv2  # type: ignore
        import numpy  # noqa: F401
        runtime = {
            "cv2": True,
            "numpy": True,
            "opencv_dnn": bool(hasattr(cv2, "dnn") and hasattr(cv2.dnn, "readNetFromONNX")),
            "opencv_version": str(cv2.__version__),
        }
    except ImportError:
        pass
    runtime_ready = all(
        runtime.get(key) for key in ("cv2", "numpy", "opencv_dnn")
    )
    status = "PASS" if (
        not (missing or invalid_suffix or original_paths)
        and (backend == "fake" or runtime_ready)
    ) else "FAIL"
    summary = {
        "status": status,
        "technical_status": status,
        "policy_status": "PASS" if not original_paths else "FAIL",
        "commit_status": "DO_NOT_COMMIT",
        "contract_version": CONTRACT_VERSION,
        "orchestrator_version": VERSION,
        "eligible_visual_unit_count": len(rows),
        "eligible_by_media": {
            kind: sum(row["media_type"] == kind for row in rows)
            for kind in ("image", "video")
        },
        "missing_derived_file_count": len(missing),
        "invalid_derived_suffix_count": len(invalid_suffix),
        "original_path_rejected_count": len(original_paths),
        "missing_visual_unit_ids_preview": missing[:10],
        "runtime": runtime,
        "model_inventory": inventory,
        "config_path": str(config_path),
        "config_sha256": sha256_file(config_path),
        "migration_path": str(migration),
        "migration_sha256": sha256_file(migration),
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "database_write": False,
        "model_run": False,
        "network_used": False,
        "download_used": False,
        "original_media_read": False,
        "face_crops_persisted": False,
        "fixed_input_count": False,
    }
    return summary, rows, inventory


def validate_migration_on_copy(db: Path, migration: Path, out: Path) -> dict[str, Any]:
    target = out / "dry_run" / "person_reid_schema_validation.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    source = readonly_connection(db)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        destination.executescript(migration.read_text(encoding="utf-8"))
        destination.commit()
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(destination.execute("PRAGMA foreign_key_check").fetchall())
        object_count = destination.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'stop03_1c%' "
            "OR name='v_stop03_1c_latest_person_cluster_members'"
        ).fetchone()[0]
    finally:
        destination.close()
        source.close()
    return {
        "validation_db_path": str(target),
        "validation_db_size_bytes": target.stat().st_size,
        "integrity_check": integrity,
        "foreign_key_error_count": foreign_keys,
        "person_reid_object_count": object_count,
    }


def backup_database_once(db: Path, out: Path) -> Path:
    target = out / "backups" / "database_before_person_reid_schema.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        return target
    source = readonly_connection(db)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("person_reid_database_backup_failed")
    return target


def block_network() -> None:
    def denied(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("person_reid_network_blocked")
    socket.socket.connect = denied  # type: ignore[assignment]
    socket.create_connection = denied  # type: ignore[assignment]


def _distance2bbox(points: Any, distance: Any, np: Any) -> Any:
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(points: Any, distance: Any, np: Any) -> Any:
    coordinates = []
    for index in range(0, distance.shape[1], 2):
        coordinates.extend([
            points[:, 0] + distance[:, index],
            points[:, 1] + distance[:, index + 1],
        ])
    return np.stack(coordinates, axis=-1)


def _nms(boxes: Any, threshold: float, np: Any) -> list[int]:
    if not len(boxes):
        return []
    x1, y1, x2, y2, scores = [boxes[:, index] for index in range(5)]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        index = int(order[0])
        keep.append(index)
        xx1 = np.maximum(x1[index], x1[order[1:]])
        yy1 = np.maximum(y1[index], y1[order[1:]])
        xx2 = np.minimum(x2[index], x2[order[1:]])
        yy2 = np.minimum(y2[index], y2[order[1:]])
        width = np.maximum(0.0, xx2 - xx1 + 1)
        height = np.maximum(0.0, yy2 - yy1 + 1)
        overlap = width * height / (areas[index] + areas[order[1:]] - width * height)
        order = order[np.where(overlap <= threshold)[0] + 1]
    return keep


def _scrfd_output_groups(outputs: Sequence[Any], np: Any) -> list[tuple[Any, Any, Any]]:
    """Pair SCRFD score/bbox/keypoint outputs by feature-map row count.

    OpenCV preserves the ONNX graph output order, which for buffalo_l is
    interleaved by stride (score, bbox, keypoints).  Do not assume that all
    score tensors come first.
    """
    by_width: dict[int, dict[int, Any]] = {1: {}, 4: {}, 10: {}}
    for output in outputs:
        array = np.asarray(output)
        width = int(array.shape[-1]) if array.ndim >= 2 else 1
        if width not in by_width:
            continue
        reshaped = array.reshape(-1, width)
        rows = int(reshaped.shape[0])
        if rows in by_width[width]:
            raise RuntimeError(f"duplicate_scrfd_output_shape:{rows}x{width}")
        by_width[width][rows] = reshaped
    row_counts = sorted(by_width[1], reverse=True)
    if (
        not row_counts
        or set(row_counts) != set(by_width[4])
        or set(row_counts) != set(by_width[10])
    ):
        raise RuntimeError("unsupported_scrfd_output_contract")
    return [
        (by_width[1][rows], by_width[4][rows], by_width[10][rows])
        for rows in row_counts
    ]


class OpenCVDnnBackend:
    def __init__(self, config: Mapping[str, Any]) -> None:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        self.cv2 = cv2
        self.np = np
        cv2.setNumThreads(int(config["opencv_threads_per_worker"]))
        model_dir = Path(str(config["model_dir"]))
        self.detector = cv2.dnn.readNetFromONNX(str(model_dir / str(config["detector_file"])))
        self.recognizer = cv2.dnn.readNetFromONNX(str(model_dir / str(config["recognizer_file"])))
        self.detector_size = tuple(int(value) for value in config["detector_input_size"])
        self.score_min = float(config["detector_score_min"])
        self.nms_iou = float(config["nms_iou_max"])
        self.minimum_face_pixels = int(config["minimum_face_pixels"])
        self.minimum_quality = float(config["minimum_quality_score"])
        self.dimension = int(config["embedding_dimension"])

    def _detect(self, image: Any) -> list[dict[str, Any]]:
        cv2, np = self.cv2, self.np
        height, width = image.shape[:2]
        input_width, input_height = self.detector_size
        ratio = min(input_width / width, input_height / height)
        resized = cv2.resize(image, (int(width * ratio), int(height * ratio)))
        canvas = np.zeros((input_height, input_width, 3), dtype=np.uint8)
        canvas[: resized.shape[0], : resized.shape[1]] = resized
        blob = cv2.dnn.blobFromImage(
            canvas, 1.0 / 128.0, (input_width, input_height),
            (127.5, 127.5, 127.5), swapRB=True,
        )
        self.detector.setInput(blob)
        names = self.detector.getUnconnectedOutLayersNames()
        outputs = list(self.detector.forward(names))
        if len(outputs) not in {6, 9, 10, 15}:
            raise RuntimeError(f"unsupported_scrfd_output_count:{len(outputs)}")
        groups = _scrfd_output_groups(outputs, np)
        strides = [8, 16, 32, 64, 128][:len(groups)]
        scores_all, boxes_all, keypoints_all = [], [], []
        for stride, (score_output, bbox_output, keypoint_output) in zip(strides, groups):
            scores = score_output.reshape(-1)
            bbox_predictions = bbox_output.reshape(-1, 4) * stride
            keypoint_predictions = keypoint_output.reshape(-1, 10) * stride
            feature_height = input_height // stride
            feature_width = input_width // stride
            centers = np.stack(
                np.mgrid[:feature_height, :feature_width][::-1], axis=-1
            ).astype(np.float32)
            centers = (centers * stride).reshape(-1, 2)
            cell_count = feature_height * feature_width
            if len(scores) % cell_count:
                raise RuntimeError(f"scrfd_anchor_count_invalid:stride_{stride}")
            anchors = len(scores) // cell_count
            if anchors > 1:
                centers = np.stack([centers] * anchors, axis=1).reshape(-1, 2)
            count = len(scores)
            if not (
                count == len(centers)
                == len(bbox_predictions)
                == len(keypoint_predictions)
            ):
                raise RuntimeError(f"scrfd_output_row_mismatch:stride_{stride}")
            positive = np.where(scores >= self.score_min)[0]
            if not len(positive):
                continue
            boxes = _distance2bbox(centers[:count], bbox_predictions[:count], np)[positive]
            points = _distance2kps(centers[:count], keypoint_predictions[:count], np)[positive]
            scores_all.append(scores[positive])
            boxes_all.append(boxes)
            keypoints_all.append(points.reshape(-1, 5, 2))
        if not scores_all:
            return []
        scores = np.concatenate(scores_all)
        boxes = np.concatenate(boxes_all) / ratio
        keypoints = np.concatenate(keypoints_all) / ratio
        detections = np.hstack([boxes, scores[:, None]]).astype(np.float32)
        keep = _nms(detections, self.nms_iou, np)
        result = []
        for item in keep:
            x1, y1, x2, y2, score = detections[item]
            x1, y1 = max(0.0, x1), max(0.0, y1)
            x2, y2 = min(float(width - 1), x2), min(float(height - 1), y2)
            face_pixels = min(x2 - x1, y2 - y1)
            area_ratio = max(0.0, (x2 - x1) * (y2 - y1) / max(1.0, width * height))
            quality = min(1.0, float(score) * math.sqrt(max(area_ratio, 0.0) * 16.0))
            if face_pixels < self.minimum_face_pixels or quality < self.minimum_quality:
                continue
            result.append({
                "bbox": [float(x1), float(y1), float(x2), float(y2)],
                "landmarks": keypoints[item].astype(float).tolist(),
                "detection_score": float(score),
                "quality_score": float(quality),
            })
        return result

    def _align(self, image: Any, landmarks: Any) -> Any:
        cv2, np = self.cv2, self.np
        target = np.array([
            [38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
            [41.5493, 92.3655], [70.7299, 92.2041],
        ], dtype=np.float32)
        matrix, _ = cv2.estimateAffinePartial2D(
            np.asarray(landmarks, dtype=np.float32), target, method=cv2.LMEDS
        )
        if matrix is None:
            raise RuntimeError("face_alignment_failed")
        return cv2.warpAffine(image, matrix, (112, 112), borderValue=0)

    def infer(self, visual_unit_id: str, path: Path) -> list[dict[str, Any]]:
        cv2, np = self.cv2, self.np
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("derived_image_decode_failed")
        faces = self._detect(image)
        for face in faces:
            aligned = self._align(image, face["landmarks"])
            blob = cv2.dnn.blobFromImage(
                aligned, 1.0 / 127.5, (112, 112),
                (127.5, 127.5, 127.5), swapRB=True,
            )
            self.recognizer.setInput(blob)
            vector = np.asarray(self.recognizer.forward(), dtype=np.float32).reshape(-1)
            if len(vector) != self.dimension:
                raise RuntimeError(f"recognition_dimension_mismatch:{len(vector)}")
            norm = float(np.linalg.norm(vector))
            if not math.isfinite(norm) or norm <= 0:
                raise RuntimeError("recognition_vector_invalid")
            face["embedding"] = (vector / norm).astype(np.float32).tolist()
        return faces


class FakeBackend:
    def __init__(self, fixture: Mapping[str, Any], delay_seconds: float = 0.0) -> None:
        self.fixture = fixture
        self.delay_seconds = delay_seconds

    def infer(self, visual_unit_id: str, _path: Path) -> list[dict[str, Any]]:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        value = self.fixture.get(visual_unit_id, [])
        if isinstance(value, Mapping) and value.get("error"):
            raise RuntimeError(str(value["error"]))
        return [dict(face) for face in value]


def apply_migration(db: Path, migration: Path) -> None:
    con = writable_connection(db)
    try:
        con.executescript(migration.read_text(encoding="utf-8"))
        con.commit()
    finally:
        con.close()


def prepare_run(
    db: Path,
    rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    inventory: Mapping[str, Any],
    workers: int,
    max_attempts: int,
) -> str:
    config_sha = sha256_file(Path(str(config["_config_path"])))
    visual_digest = sha256_bytes(canonical_json([
        [row["visual_unit_id"], row["visual_size_bytes"], row["visual_mtime_ns"]]
        for row in rows
    ]).encode("utf-8"))
    payload_digest = sha256_bytes(canonical_json({
        "contract": CONTRACT_VERSION,
        "detector": inventory["detector_sha256"],
        "recognizer": inventory["recognizer_sha256"],
        "config": config_sha,
        "visuals": visual_digest,
    }).encode("utf-8"))
    run_id = stable_id("stop03_1c_", payload_digest)
    now = utc_now()
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT status FROM stop03_1c_person_reid_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not existing:
            con.execute(
                """
                INSERT INTO stop03_1c_person_reid_runs(
                    run_id,contract_version,model_name,model_dir,
                    detector_sha256,recognizer_sha256,config_sha256,script_sha256,
                    scheduling_mode,workers,max_attempts,visual_unit_count,
                    pending_count,run_payload_digest,status,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, CONTRACT_VERSION, config["model_name"], inventory["model_dir"],
                    inventory["detector_sha256"], inventory["recognizer_sha256"],
                    config_sha, sha256_file(Path(__file__).resolve()),
                    "dynamic_database_claim", workers, max_attempts, len(rows),
                    len(rows), payload_digest, "planned", now,
                ),
            )
            for row in rows:
                execution_key = stable_id(
                    "faceexec_",
                    row["visual_unit_id"], row["visual_size_bytes"], row["visual_mtime_ns"],
                    inventory["detector_sha256"], inventory["recognizer_sha256"], config_sha,
                )
                con.execute(
                    """
                    INSERT INTO stop03_1c_person_reid_run_items(
                        run_id,visual_unit_id,execution_key,source_content_id,derived_id,
                        visual_file,time_position_ms,media_type,status,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id, row["visual_unit_id"], execution_key,
                        row["source_content_id"], row["derived_id"], row["visual_file"],
                        row["time_position_ms"], row["media_type"], "pending", now,
                    ),
                )
        else:
            con.execute(
                """
                UPDATE stop03_1c_person_reid_run_items
                SET status='pending',claimed_by_worker='',worker_pid=NULL,
                    started_at=NULL,last_error_code='',last_error_message=''
                WHERE run_id=? AND status='running'
                """,
                (run_id,),
            )
        con.execute(
            "UPDATE stop03_1c_person_reid_runs SET status='running',workers=?,"
            "max_attempts=?,error_message='' WHERE run_id=? AND status<>'success'",
            (workers, max_attempts, run_id),
        )
        con.commit()
    finally:
        con.close()
    refresh_counts(db, run_id)
    return run_id


def claim_one(db: Path, run_id: str, worker_id: str, max_attempts: int) -> dict[str, Any] | None:
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT * FROM stop03_1c_person_reid_run_items
            WHERE run_id=?
              AND status IN ('pending','failed')
              AND attempt_count<?
            ORDER BY visual_unit_id
            LIMIT 1
            """,
            (run_id, max_attempts),
        ).fetchone()
        if row is None:
            con.rollback()
            return None
        changed = con.execute(
            """
            UPDATE stop03_1c_person_reid_run_items
            SET status='running',attempt_count=attempt_count+1,
                claimed_by_worker=?,worker_pid=?,started_at=?,finished_at=NULL
            WHERE run_id=? AND visual_unit_id=? AND status IN ('pending','failed')
            """,
            (worker_id, os.getpid(), utc_now(), run_id, row["visual_unit_id"]),
        ).rowcount
        if changed != 1:
            con.rollback()
            return None
        con.commit()
        return dict(row)
    finally:
        con.close()


def complete_item(
    db: Path,
    run_id: str,
    row: Mapping[str, Any],
    faces: Sequence[Mapping[str, Any]],
    elapsed: float,
) -> None:
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "DELETE FROM stop03_1c_face_embeddings WHERE run_id=? AND visual_unit_id=?",
            (run_id, row["visual_unit_id"]),
        )
        for index, face in enumerate(faces):
            vector = [float(value) for value in face["embedding"]]
            blob = struct.pack("<" + "f" * len(vector), *vector)
            face_id = stable_id(
                "face1c_", run_id, row["visual_unit_id"], index,
                canonical_json(face["bbox"]),
            )
            con.execute(
                """
                INSERT INTO stop03_1c_face_embeddings(
                    run_id,face_id,visual_unit_id,face_index,bbox_json,landmarks_json,
                    detection_score,quality_score,embedding_dimension,vector_dtype,
                    normalized,embedding_blob,embedding_byte_length,embedding_sha256,
                    created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, face_id, row["visual_unit_id"], index,
                    canonical_json(face["bbox"]), canonical_json(face["landmarks"]),
                    float(face["detection_score"]), float(face["quality_score"]),
                    len(vector), "float32", 1, blob, len(blob), sha256_bytes(blob), utc_now(),
                ),
            )
        status = "success" if faces else "no_face"
        con.execute(
            """
            UPDATE stop03_1c_person_reid_run_items
            SET status=?,face_count=?,elapsed_seconds=?,finished_at=?,
                last_error_code='',last_error_message=''
            WHERE run_id=? AND visual_unit_id=?
            """,
            (status, len(faces), elapsed, utc_now(), run_id, row["visual_unit_id"]),
        )
        con.commit()
    finally:
        con.close()


def fail_item(
    db: Path,
    run_id: str,
    row: Mapping[str, Any],
    error: BaseException,
    elapsed: float,
) -> None:
    message = str(error)[:2000]
    con = writable_connection(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            UPDATE stop03_1c_person_reid_run_items
            SET status='failed',elapsed_seconds=?,finished_at=?,
                last_error_code=?,last_error_message=?
            WHERE run_id=? AND visual_unit_id=?
            """,
            (elapsed, utc_now(), error.__class__.__name__, message, run_id, row["visual_unit_id"]),
        )
        con.commit()
    finally:
        con.close()


def refresh_counts(db: Path, run_id: str) -> dict[str, int]:
    con = writable_connection(db)
    try:
        values = {
            status: int(count)
            for status, count in con.execute(
                "SELECT status,COUNT(*) FROM stop03_1c_person_reid_run_items "
                "WHERE run_id=? GROUP BY status",
                (run_id,),
            )
        }
        face_count = int(con.execute(
            "SELECT COUNT(*) FROM stop03_1c_face_embeddings WHERE run_id=?", (run_id,)
        ).fetchone()[0])
        con.execute(
            """
            UPDATE stop03_1c_person_reid_runs
            SET pending_count=?,running_count=?,success_count=?,no_face_count=?,
                failed_count=?,face_count=?
            WHERE run_id=?
            """,
            (
                values.get("pending", 0), values.get("running", 0),
                values.get("success", 0), values.get("no_face", 0),
                values.get("failed", 0), face_count, run_id,
            ),
        )
        con.commit()
    finally:
        con.close()
    return {
        "pending": values.get("pending", 0),
        "running": values.get("running", 0),
        "success": values.get("success", 0),
        "no_face": values.get("no_face", 0),
        "failed": values.get("failed", 0),
        "face_count": face_count,
    }


def run_workers(
    db: Path,
    run_id: str,
    workers: int,
    max_attempts: int,
    backend_factory: Callable[[int], Any],
    progress_path: Path,
) -> dict[str, Any]:
    progress_lock = threading.Lock()
    active_lock = threading.Lock()
    active = 0
    peak = 0

    def worker(index: int) -> dict[str, Any]:
        nonlocal active, peak
        backend = backend_factory(index)
        worker_id = f"worker-{index}"
        completed = failed = 0
        while True:
            row = claim_one(db, run_id, worker_id, max_attempts)
            if row is None:
                break
            started = time.perf_counter()
            with active_lock:
                active += 1
                peak = max(peak, active)
            try:
                faces = backend.infer(str(row["visual_unit_id"]), Path(str(row["visual_file"])))
                complete_item(db, run_id, row, faces, time.perf_counter() - started)
                completed += 1
                status = "success" if faces else "no_face"
            except BaseException as error:  # worker must continue after one bad item
                fail_item(db, run_id, row, error, time.perf_counter() - started)
                failed += 1
                status = "failed"
            finally:
                with active_lock:
                    active -= 1
            counts = refresh_counts(db, run_id)
            progress = {
                "timestamp": utc_now(), "run_id": run_id, "worker_id": worker_id,
                "last_completed_visual_unit_id": row["visual_unit_id"],
                "last_status": status, **counts,
            }
            append_jsonl(progress_path, progress, progress_lock)
            with active_lock:
                active_workers = active
            with progress_lock:
                print(json.dumps({
                    "contract": "media_archive_stage_runtime_contract_v1",
                    "event": "stage_progress",
                    "completed": counts["success"] + counts["no_face"] + counts["failed"],
                    "total": sum(counts[key] for key in (
                        "pending", "running", "success", "no_face", "failed"
                    )),
                    "success": counts["success"] + counts["no_face"],
                    "skipped": 0,
                    "failed": counts["failed"],
                    "current_item": str(row["visual_unit_id"]),
                    "actual_workers": active_workers,
                    "model_workers": workers,
                }, ensure_ascii=False), flush=True)
        return {"worker_id": worker_id, "completed": completed, "failed": failed}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        reports = list(pool.map(worker, range(1, workers + 1)))
    return {"worker_reports": reports, "measured_max_concurrency": peak}


def _unpack_vector(blob: bytes, dimension: int) -> list[float]:
    return list(struct.unpack("<" + "f" * dimension, blob))


def _cluster_face_records(
    records: Sequence[Mapping[str, Any]], threshold: float,
) -> list[dict[str, Any]]:
    """Deterministically merge face embeddings across original source assets.

    The previous online-centroid algorithm permanently assigned each face in
    ``face_id`` order.  A profile or dark frame seen early could therefore
    create a second identity that was never reconsidered, even when the two
    final centroids were almost identical.  This implementation evaluates
    candidate pairs globally, strongest first, and rechecks the two component
    centroids before every merge.  Faces from the same visual unit remain a
    hard cannot-link constraint.
    """
    import numpy as np  # type: ignore

    ordered = sorted(records, key=lambda item: str(item["row"]["face_id"]))
    if not ordered:
        return []
    dimensions = {int(item["vector"].shape[0]) for item in ordered}
    if len(dimensions) != 1:
        raise RuntimeError("person_reid_embedding_dimension_mismatch")
    vectors = np.stack([
        np.asarray(item["vector"], dtype=np.float32) for item in ordered
    ])
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if bool(np.any(norms <= 1e-12)):
        raise RuntimeError("person_reid_zero_embedding")
    vectors = vectors / norms

    parent = list(range(len(ordered)))
    members = {index: {index} for index in range(len(ordered))}
    visual_units = {
        index: {str(ordered[index]["row"]["visual_unit_id"])}
        for index in range(len(ordered))
    }
    vector_sums = {index: vectors[index].copy() for index in range(len(ordered))}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    # Blocked similarity calculation keeps the temporary matrix bounded while
    # retaining a globally sorted, deterministic candidate list.
    edges: list[tuple[float, str, str, int, int]] = []
    block_size = 1024
    for left_start in range(0, len(ordered), block_size):
        left_end = min(left_start + block_size, len(ordered))
        for right_start in range(left_start, len(ordered), block_size):
            right_end = min(right_start + block_size, len(ordered))
            scores = vectors[left_start:left_end] @ vectors[right_start:right_end].T
            for left_local, right_local in zip(*np.where(scores >= threshold)):
                left = left_start + int(left_local)
                right = right_start + int(right_local)
                if right <= left:
                    continue
                left_row = ordered[left]["row"]
                right_row = ordered[right]["row"]
                if str(left_row["visual_unit_id"]) == str(right_row["visual_unit_id"]):
                    continue
                edges.append((
                    -float(scores[left_local, right_local]),
                    str(left_row["face_id"]), str(right_row["face_id"]),
                    left, right,
                ))
    edges.sort()

    for _negative_score, _left_id, _right_id, left, right in edges:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            continue
        if visual_units[left_root] & visual_units[right_root]:
            continue
        left_sum = vector_sums[left_root]
        right_sum = vector_sums[right_root]
        left_centroid = left_sum / max(float(np.linalg.norm(left_sum)), 1e-12)
        right_centroid = right_sum / max(float(np.linalg.norm(right_sum)), 1e-12)
        if float(np.dot(left_centroid, right_centroid)) < threshold:
            continue
        # Stable root selection prevents input order and hash order from
        # changing cluster identifiers or representatives.
        left_key = min(str(ordered[index]["row"]["face_id"]) for index in members[left_root])
        right_key = min(str(ordered[index]["row"]["face_id"]) for index in members[right_root])
        keep, discard = (
            (left_root, right_root) if left_key <= right_key
            else (right_root, left_root)
        )
        parent[discard] = keep
        members[keep].update(members.pop(discard))
        visual_units[keep].update(visual_units.pop(discard))
        vector_sums[keep] = vector_sums[keep] + vector_sums.pop(discard)

    clusters: list[dict[str, Any]] = []
    for root in sorted(members, key=lambda value: min(
        str(ordered[index]["row"]["face_id"]) for index in members[value]
    )):
        indices = sorted(
            members[root], key=lambda index: str(ordered[index]["row"]["face_id"])
        )
        centroid_sum = vectors[indices].sum(axis=0)
        centroid = centroid_sum / max(float(np.linalg.norm(centroid_sum)), 1e-12)
        representative_index = min(
            indices,
            key=lambda index: (
                -float(np.dot(vectors[index], centroid)),
                str(ordered[index]["row"]["face_id"]),
            ),
        )
        representative_vector = vectors[representative_index]
        clusters.append({
            "members": [
                (
                    dict(ordered[index]["row"]),
                    float(np.dot(vectors[index], representative_vector)),
                )
                for index in indices
            ],
            "representative_face_id": str(
                ordered[representative_index]["row"]["face_id"]
            ),
        })
    return clusters


def build_clusters(db: Path, run_id: str, threshold: float) -> int:
    import numpy as np  # type: ignore
    con = writable_connection(db)
    try:
        rows = con.execute(
            """
            SELECT f.face_id,f.visual_unit_id,f.embedding_dimension,f.embedding_blob,
                   i.source_content_id
            FROM stop03_1c_face_embeddings AS f
            JOIN stop03_1c_person_reid_run_items AS i
              ON i.run_id=f.run_id AND i.visual_unit_id=f.visual_unit_id
            WHERE f.run_id=?
            ORDER BY f.face_id
            """,
            (run_id,),
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            vector = np.asarray(
                _unpack_vector(row["embedding_blob"], int(row["embedding_dimension"])),
                dtype=np.float32,
            )
            records.append({"row": dict(row), "vector": vector})
        clusters = _cluster_face_records(records, threshold)
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM stop03_1c_person_cluster_members WHERE run_id=?", (run_id,))
        con.execute("DELETE FROM stop03_1c_person_clusters WHERE run_id=?", (run_id,))
        for cluster in clusters:
            members = cluster["members"]
            representative = cluster["representative_face_id"]
            cluster_id = stable_id(
                "person1c_", run_id,
                *(sorted(str(row["face_id"]) for row, _ in members)),
            )
            distinct_sources = len({row["source_content_id"] for row, _ in members})
            minimum = min(similarity for _, similarity in members)
            confidence = (
                "singleton" if len(members) == 1
                else "high" if distinct_sources >= 2
                else "review"
            )
            now = utc_now()
            con.execute(
                """
                INSERT INTO stop03_1c_person_clusters(
                    run_id,person_cluster_id,representative_face_id,member_count,
                    distinct_source_count,minimum_member_similarity,cluster_confidence,
                    human_review_status,anonymous_display_name,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, cluster_id, representative, len(members), distinct_sources,
                    minimum, confidence, "unreviewed", "", now,
                ),
            )
            for row, similarity in members:
                con.execute(
                    """
                    INSERT INTO stop03_1c_person_cluster_members(
                        run_id,person_cluster_id,face_id,similarity_to_representative,
                        membership_reason,created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        run_id, cluster_id, row["face_id"], similarity,
                        "singleton" if len(members) == 1 else "auto_high_confidence", now,
                    ),
                )
        con.execute(
            "UPDATE stop03_1c_person_reid_runs SET cluster_count=? WHERE run_id=?",
            (len(clusters), run_id),
        )
        con.commit()
        return len(clusters)
    finally:
        con.close()


def finish_run(db: Path, run_id: str) -> dict[str, Any]:
    counts = refresh_counts(db, run_id)
    terminal = counts["pending"] == 0 and counts["running"] == 0 and counts["failed"] == 0
    con = writable_connection(db)
    try:
        cluster_count = int(con.execute(
            "SELECT COUNT(*) FROM stop03_1c_person_clusters WHERE run_id=?", (run_id,)
        ).fetchone()[0])
        status = "success" if terminal else "failed"
        con.execute(
            "UPDATE stop03_1c_person_reid_runs SET status=?,finished_at=?,error_message=? "
            "WHERE run_id=?",
            (status, utc_now(), "" if terminal else "person_reid_items_incomplete", run_id),
        )
        con.commit()
    finally:
        con.close()
    return readback(db, run_id)


def readback(db: Path, run_id: str) -> dict[str, Any]:
    con = readonly_connection(db)
    try:
        run = con.execute(
            "SELECT * FROM stop03_1c_person_reid_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise RuntimeError(f"person_reid_run_not_found:{run_id}")
        duplicates = int(con.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT execution_key FROM stop03_1c_person_reid_run_items
                WHERE run_id=? GROUP BY execution_key HAVING COUNT(*)>1
            )
            """,
            (run_id,),
        ).fetchone()[0])
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
        feature_payload_bytes = int(con.execute(
            """
            SELECT
                COALESCE((SELECT SUM(
                              LENGTH(embedding_blob)
                            + LENGTH(bbox_json)
                            + LENGTH(landmarks_json)
                            + LENGTH(face_id)
                          )
                          FROM stop03_1c_face_embeddings WHERE run_id=?),0)
              + COALESCE((SELECT SUM(LENGTH(person_cluster_id)+LENGTH(face_id))
                          FROM stop03_1c_person_cluster_members WHERE run_id=?),0)
            """,
            (run_id, run_id),
        ).fetchone()[0] or 0)
        result = dict(run)
    finally:
        con.close()
    return {
        "status": "PASS" if result["status"] == "success" else "FAIL",
        "technical_status": "PASS" if result["status"] == "success" else "FAIL",
        "policy_status": "HUMAN_REVIEW_REQUIRED",
        "run_id": run_id,
        "run_status": result["status"],
        "workers": result["workers"],
        "visual_unit_count": result["visual_unit_count"],
        "success_count": result["success_count"],
        "no_face_count": result["no_face_count"],
        "failed_count": result["failed_count"],
        "pending_count": result["pending_count"],
        "running_count": result["running_count"],
        "face_count": result["face_count"],
        "person_cluster_count": result["cluster_count"],
        "execution_key_duplicates": duplicates,
        "database_integrity_check": integrity,
        "foreign_key_error_count": foreign_keys,
        "network_used": False,
        "download_used": False,
        "original_video_read": False,
        "face_crops_persisted": False,
        "feature_payload_bytes": feature_payload_bytes,
        "feature_payload_megabytes": round(feature_payload_bytes / (1024 * 1024), 3),
        "storage_policy": "embeddings_and_relationships_only_no_media_copies_v1",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight-only", "dry-run", "run", "readback"), required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--migration", type=Path, default=DEFAULT_MIGRATION)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--allowed-output-root", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Deterministic visual-unit cap for isolated smoke validation; 0 means all.",
    )
    parser.add_argument("--backend", choices=("opencv", "fake"), default="opencv")
    parser.add_argument("--fake-fixture", type=Path)
    parser.add_argument("--fake-delay-seconds", type=float, default=0.0)
    parser.add_argument("--confirm-central-db-write", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = args.db.expanduser().resolve(strict=True)
    config_path = args.config.expanduser().resolve(strict=True)
    migration = args.migration.expanduser().resolve(strict=True)
    if args.mode == "readback":
        if not args.run_id:
            raise RuntimeError("person_reid_readback_run_id_required")
        print(json.dumps(readback(db, args.run_id), ensure_ascii=False, indent=2))
        return 0
    if args.out is None or args.allowed_output_root is None:
        raise RuntimeError("person_reid_output_and_allowed_root_required")
    out = args.out.expanduser().resolve()
    allowed = args.allowed_output_root.expanduser().resolve(strict=True)
    ensure_under(out, allowed)
    out.mkdir(parents=True, exist_ok=True)
    preflight, rows, inventory = build_preflight(
        db, config_path, migration, backend=args.backend
    )
    if args.limit < 0:
        raise RuntimeError("person_reid_limit_must_be_non_negative")
    if args.limit:
        rows = rows[:args.limit]
    preflight["selected_visual_unit_count"] = len(rows)
    preflight["selection_limit"] = int(args.limit)
    write_json_atomic(out / "preflight_summary.json", preflight)
    if preflight["technical_status"] != "PASS":
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 2
    if args.mode == "preflight-only":
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "dry-run":
        validation = validate_migration_on_copy(db, migration, out)
        summary = {
            **preflight,
            "status": "PASS" if validation["integrity_check"] == "ok"
            and validation["foreign_key_error_count"] == 0 else "FAIL",
            "commit_status": "DO_NOT_COMMIT",
            "migration_validation": validation,
            "central_database_write": False,
        }
        write_json_atomic(out / "dry_run_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["status"] == "PASS" else 2
    if not args.confirm_central_db_write:
        raise RuntimeError("person_reid_central_db_write_confirmation_required")
    block_network()
    run_started = time.perf_counter()
    config = load_config(config_path)
    config["_config_path"] = str(config_path)
    workers = int(args.workers or config["default_workers"])
    max_attempts = int(args.max_attempts or config["default_max_attempts"])
    if workers <= 0 or max_attempts <= 0:
        raise RuntimeError("person_reid_workers_or_attempts_invalid")
    backup_path = backup_database_once(db, out)
    apply_migration(db, migration)
    run_id = prepare_run(db, rows, config, inventory, workers, max_attempts)
    existing = readback(db, run_id)
    if existing["run_status"] == "success":
        existing["database_backup_path"] = str(backup_path)
        print(json.dumps(existing, ensure_ascii=False, indent=2))
        return 0
    if args.backend == "fake":
        if args.fake_fixture is None:
            raise RuntimeError("person_reid_fake_fixture_required")
        fixture = json.loads(args.fake_fixture.read_text(encoding="utf-8"))
        factory = lambda _index: FakeBackend(fixture, args.fake_delay_seconds)
    else:
        factory = lambda _index: OpenCVDnnBackend(config)
    worker_report = run_workers(
        db, run_id, workers, max_attempts, factory, out / "logs" / "progress.jsonl"
    )
    counts = refresh_counts(db, run_id)
    if counts["failed"] == 0 and counts["pending"] == 0 and counts["running"] == 0:
        build_clusters(db, run_id, float(config["auto_merge_cosine_min"]))
    final = {**finish_run(db, run_id), **worker_report}
    final["total_elapsed_seconds"] = time.perf_counter() - run_started
    final["throughput_visual_units_per_second"] = (
        len(rows) / final["total_elapsed_seconds"]
        if final["total_elapsed_seconds"] > 0 else 0.0
    )
    final["peak_rss_bytes"] = peak_rss_bytes()
    final["database_backup_path"] = str(backup_path)
    write_json_atomic(out / "run_summary.json", final)
    (out / "run_id.txt").write_text(run_id + "\n", encoding="utf-8")
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0 if final["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
