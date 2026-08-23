from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import hypot
from pathlib import Path
from typing import Iterable, Sequence


CONTRACT = "media_archive_face_anchored_track_suggestions_v1"


@dataclass(frozen=True)
class PersonDetection:
    detection_id: str
    visual_unit_id: str
    source_content_id: str
    time_position_ms: int
    bbox: tuple[float, float, float, float]
    confidence: float = 0.0
    face_cluster_ids: tuple[str, ...] = ()
    shot_id: str = ""


@dataclass
class Tracklet:
    tracklet_id: str
    source_content_id: str
    detections: list[PersonDetection] = field(default_factory=list)
    face_cluster_ids: set[str] = field(default_factory=set)

    def append(self, detection: PersonDetection) -> None:
        self.detections.append(detection)
        self.face_cluster_ids.update(detection.face_cluster_ids)

    @property
    def identity_status(self) -> str:
        if len(self.face_cluster_ids) > 1:
            return "CONFLICT_REQUIRES_REVIEW"
        if len(self.face_cluster_ids) == 1:
            return "FACE_ANCHORED_TRACK"
        return "UNANCHORED_TRACK"

    @property
    def propagated_member_count(self) -> int:
        if self.identity_status != "FACE_ANCHORED_TRACK":
            return 0
        return sum(not detection.face_cluster_ids for detection in self.detections)


@dataclass(frozen=True)
class LinkerConfig:
    max_gap_ms: int = 6500
    minimum_iou: float = 0.04
    maximum_center_distance: float = 0.22
    minimum_area_ratio: float = 0.28
    minimum_match_score: float = 0.30
    ambiguity_margin: float = 0.06


@dataclass(frozen=True)
class LinkerResult:
    tracklets: tuple[Tracklet, ...]
    ambiguous_unlinked: int


@dataclass(frozen=True)
class TrackSuggestion:
    visual_unit_id: str
    source_content_id: str
    time_position_ms: int
    person_cluster_id: str
    anchor_distance_ms: int
    tracklet_member_count: int
    frame_person_count: int
    score: float
    review_reason: str


def _area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _iou(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    intersection = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )
    union = _area(left) + _area(right) - intersection
    return intersection / union if union > 0.0 else 0.0


def _center_distance(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return hypot(
        (left[0] + left[2] - right[0] - right[2]) / 2.0,
        (left[1] + left[3] - right[1] - right[3]) / 2.0,
    )


def _match_score(
    previous: PersonDetection,
    current: PersonDetection,
    config: LinkerConfig,
) -> float | None:
    if previous.source_content_id != current.source_content_id:
        return None
    gap = current.time_position_ms - previous.time_position_ms
    if gap <= 0 or gap > config.max_gap_ms:
        return None
    if previous.shot_id and current.shot_id and previous.shot_id != current.shot_id:
        return None
    overlap = _iou(previous.bbox, current.bbox)
    distance = _center_distance(previous.bbox, current.bbox)
    larger = max(_area(previous.bbox), _area(current.bbox))
    smaller = min(_area(previous.bbox), _area(current.bbox))
    area_ratio = smaller / larger if larger > 0.0 else 0.0
    if area_ratio < config.minimum_area_ratio:
        return None
    if overlap < config.minimum_iou and distance > config.maximum_center_distance:
        return None
    center_affinity = max(0.0, 1.0 - distance / config.maximum_center_distance)
    return 0.55 * overlap + 0.30 * center_affinity + 0.15 * area_ratio


def build_tracklets(
    detections: Iterable[PersonDetection],
    config: LinkerConfig = LinkerConfig(),
) -> LinkerResult:
    by_source: dict[str, list[PersonDetection]] = defaultdict(list)
    for detection in detections:
        by_source[detection.source_content_id].append(detection)
    result: list[Tracklet] = []
    ambiguous_unlinked = 0
    next_id = 1
    for source_content_id in sorted(by_source):
        frames: dict[int, list[PersonDetection]] = defaultdict(list)
        for detection in by_source[source_content_id]:
            frames[detection.time_position_ms].append(detection)
        active: list[Tracklet] = []
        for time_position_ms in sorted(frames):
            active = [
                tracklet for tracklet in active
                if time_position_ms - tracklet.detections[-1].time_position_ms
                <= config.max_gap_ms
            ]
            candidates: list[tuple[float, str, str, Tracklet, PersonDetection]] = []
            scores_by_detection: dict[str, list[float]] = defaultdict(list)
            for tracklet in active:
                for detection in frames[time_position_ms]:
                    score = _match_score(tracklet.detections[-1], detection, config)
                    if score is None or score < config.minimum_match_score:
                        continue
                    candidates.append(
                        (score, tracklet.tracklet_id, detection.detection_id, tracklet, detection)
                    )
                    scores_by_detection[detection.detection_id].append(score)
            ambiguous_ids = {
                detection_id for detection_id, scores in scores_by_detection.items()
                if len(scores) > 1
                and sorted(scores, reverse=True)[0] - sorted(scores, reverse=True)[1]
                < config.ambiguity_margin
            }
            ambiguous_unlinked += len(ambiguous_ids)
            assigned_tracks: set[str] = set()
            assigned_detections: set[str] = set()
            for _score, _track_id, _detection_id, tracklet, detection in sorted(
                candidates, key=lambda row: (-row[0], row[1], row[2])
            ):
                if detection.detection_id in ambiguous_ids:
                    continue
                if tracklet.tracklet_id in assigned_tracks:
                    continue
                if detection.detection_id in assigned_detections:
                    continue
                tracklet.append(detection)
                assigned_tracks.add(tracklet.tracklet_id)
                assigned_detections.add(detection.detection_id)
            for detection in sorted(frames[time_position_ms], key=lambda row: row.detection_id):
                if detection.detection_id in assigned_detections:
                    continue
                tracklet = Tracklet(
                    tracklet_id=f"track_{next_id:08d}",
                    source_content_id=source_content_id,
                )
                next_id += 1
                tracklet.append(detection)
                result.append(tracklet)
                active.append(tracklet)
    return LinkerResult(tuple(result), ambiguous_unlinked)


def candidate_suggestions(
    detections: Sequence[PersonDetection],
    target_cluster_ids: Iterable[str],
    *,
    max_anchor_distance_ms: int = 12_000,
) -> list[TrackSuggestion]:
    targets = {str(value) for value in target_cluster_ids if str(value)}
    if not targets:
        return []
    frame_counts = Counter(detection.visual_unit_id for detection in detections)
    suggestions: dict[str, TrackSuggestion] = {}
    for tracklet in build_tracklets(detections).tracklets:
        if tracklet.identity_status != "FACE_ANCHORED_TRACK":
            continue
        cluster_id = next(iter(tracklet.face_cluster_ids))
        if cluster_id not in targets:
            continue
        anchor_times = [
            detection.time_position_ms for detection in tracklet.detections
            if cluster_id in detection.face_cluster_ids
        ]
        for detection in tracklet.detections:
            if detection.face_cluster_ids:
                continue
            distance = min(abs(detection.time_position_ms - value) for value in anchor_times)
            if distance > max_anchor_distance_ms:
                continue
            crowd = frame_counts[detection.visual_unit_id] > 1
            score = max(0.0, 1.0 - distance / (max_anchor_distance_ms + 1))
            score *= 0.72 if crowd else 0.92
            candidate = TrackSuggestion(
                visual_unit_id=detection.visual_unit_id,
                source_content_id=detection.source_content_id,
                time_position_ms=detection.time_position_ms,
                person_cluster_id=cluster_id,
                anchor_distance_ms=distance,
                tracklet_member_count=len(tracklet.detections),
                frame_person_count=frame_counts[detection.visual_unit_id],
                score=round(score, 6),
                review_reason=(
                    "多人画面：轨迹可能交叉，请人工确认"
                    if crowd else "同一视频中靠近已识别人脸的连续人体轨迹"
                ),
            )
            previous = suggestions.get(candidate.visual_unit_id)
            if previous is None or candidate.score > previous.score:
                suggestions[candidate.visual_unit_id] = candidate
    return sorted(
        suggestions.values(),
        key=lambda value: (-value.score, value.source_content_id, value.time_position_ms),
    )


def _normalised_bbox(value: str, width: int, height: int) -> tuple[float, float, float, float]:
    raw = json.loads(value)
    if not isinstance(raw, list) or len(raw) < 4 or width <= 0 or height <= 0:
        raise ValueError("invalid_bbox")
    return tuple(
        max(0.0, min(1.0, coordinate / dimension))
        for coordinate, dimension in zip(
            map(float, raw[:4]), (width, height, width, height)
        )
    )  # type: ignore[return-value]


def _face_belongs_to_person(
    face: tuple[float, float, float, float],
    person: tuple[float, float, float, float],
) -> bool:
    center_x = (face[0] + face[2]) / 2.0
    center_y = (face[1] + face[3]) / 2.0
    return (
        person[0] <= center_x <= person[2]
        and person[1] <= center_y <= person[3]
        and face[1] <= person[1] + 0.65 * (person[3] - person[1])
    )


def load_database_suggestions(
    database: Path,
    target_cluster_ids: Iterable[str],
) -> list[TrackSuggestion]:
    """Read existing derived detections only.  Never read source media or write SQLite."""
    con = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        face_run = con.execute(
            """
            SELECT run_id FROM stop03_1c_person_reid_runs
            WHERE status='success' ORDER BY created_at DESC,run_id DESC LIMIT 1
            """
        ).fetchone()
        if face_run is None:
            return []
        yolo_run = con.execute(
            """
            SELECT run_id,COUNT(DISTINCT visual_unit_id) AS coverage,
                   MAX(created_at) AS newest
            FROM visual_labels WHERE lower(label)='person'
            GROUP BY run_id ORDER BY coverage DESC,newest DESC LIMIT 1
            """
        ).fetchone()
        if yolo_run is None:
            return []
        faces_by_visual: dict[str, list[tuple[tuple[float, float, float, float], str]]] = defaultdict(list)
        for row in con.execute(
            """
            SELECT f.visual_unit_id,f.bbox_json,m.person_cluster_id,da.width,da.height
            FROM stop03_1c_face_embeddings AS f
            JOIN stop03_1c_person_cluster_members AS m
              ON m.run_id=f.run_id AND m.face_id=f.face_id
            JOIN stop03_1c_person_reid_run_items AS i
              ON i.run_id=f.run_id AND i.visual_unit_id=f.visual_unit_id
            JOIN derived_assets AS da ON da.derived_id=i.derived_id
            WHERE f.run_id=?
            """,
            (str(face_run["run_id"]),),
        ):
            faces_by_visual[str(row["visual_unit_id"])].append((
                _normalised_bbox(str(row["bbox_json"]), int(row["width"]), int(row["height"])),
                str(row["person_cluster_id"]),
            ))
        detections: list[PersonDetection] = []
        for row in con.execute(
            """
            SELECT vl.label_id,vl.visual_unit_id,vl.source_content_id,vl.confidence,vl.bbox,
                   CASE WHEN vu.time_position_ms>=0 THEN vu.time_position_ms
                        ELSE da.time_position_ms END AS effective_time_position_ms,
                   da.width,da.height
            FROM visual_labels AS vl
            JOIN visual_units AS vu ON vu.visual_unit_id=vl.visual_unit_id
            JOIN derived_assets AS da ON da.derived_id=vu.derived_id
            WHERE vl.run_id=? AND lower(vl.label)='person'
              AND da.derived_type='video_frame_jpg1280'
              AND da.time_position_ms>=0 AND da.width>0 AND da.height>0
            ORDER BY vl.source_content_id,effective_time_position_ms,vl.label_id
            """,
            (str(yolo_run["run_id"]),),
        ):
            person_bbox = _normalised_bbox(
                str(row["bbox"]), int(row["width"]), int(row["height"])
            )
            visual_id = str(row["visual_unit_id"])
            clusters = tuple(sorted({
                cluster for face_bbox, cluster in faces_by_visual.get(visual_id, [])
                if _face_belongs_to_person(face_bbox, person_bbox)
            }))
            detections.append(PersonDetection(
                detection_id=f"person_label_{row['label_id']}",
                visual_unit_id=visual_id,
                source_content_id=str(row["source_content_id"]),
                time_position_ms=int(row["effective_time_position_ms"]),
                bbox=person_bbox,
                confidence=float(row["confidence"] or 0.0),
                face_cluster_ids=clusters,
            ))
        return candidate_suggestions(detections, target_cluster_ids)
    finally:
        con.close()
