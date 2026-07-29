import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Union


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "03_stop03_visual_analysis" / "stop03_2_candidate_queues_from_stop03_1.py"
POLICY_VERSION = "stop03_2_candidate_queues_fix_v2_20260708"


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _unit(
    visual_unit_id: str,
    visual_unit_type: str,
    group_id: str,
    rel_path: str,
    time_ms: Union[int, str],
    labels: list[str],
    embedding: list[float],
    preview_role: str,
    rating: str = "",
) -> tuple[dict, dict, dict]:
    time_text = str(time_ms) if time_ms != "" else ""
    visual_file = f"/derived/{visual_unit_id}_t{int(time_ms or 0):09d}ms.jpg"
    join = {
        "visual_unit_id": visual_unit_id,
        "visual_unit_type": visual_unit_type,
        "visual_file": visual_file,
        "visual_file_sha256": f"sha_{visual_unit_id}",
        "original_source_file_id": f"sf_{group_id}",
        "original_source_content_id": group_id,
        "original_source_path_at_processing_time": f"/source/{rel_path}",
        "source_relative_path": rel_path,
        "time_position_ms": time_text,
        "preview_role": preview_role,
        "source_manifest": "/manifest.csv",
        "yoloe_detection_count": str(len(labels)),
        "yoloe_detected_labels": "|".join(labels),
        "Rating": rating,
    }
    yoloe = {
        "visual_unit_id": visual_unit_id,
        "visual_unit_type": visual_unit_type,
        "visual_file": visual_file,
        "parent_source_file_id": join["original_source_file_id"],
        "parent_source_content_id": join["original_source_content_id"],
        "parent_source_path_at_processing_time": join["original_source_path_at_processing_time"],
        "source_relative_path": rel_path,
        "time_position_ms": time_text,
        "preview_role": preview_role,
        "source_manifest": "/manifest.csv",
        "status": "success",
        "detection_count": len(labels),
        "detected_labels": "|".join(labels),
        "detected_labels_json": json.dumps({label: 1 for label in labels}),
        "detections_json": json.dumps([{"label": label, "confidence": 0.9} for label in labels]),
    }
    emb = {
        "visual_unit_id": visual_unit_id,
        "visual_unit_type": visual_unit_type,
        "visual_file": visual_file,
        "visual_file_sha256": join["visual_file_sha256"],
        "parent_source_file_id": join["original_source_file_id"],
        "parent_source_content_id": join["original_source_content_id"],
        "parent_source_path_at_processing_time": join["original_source_path_at_processing_time"],
        "source_relative_path": rel_path,
        "time_position_ms": time_text,
        "preview_role": preview_role,
        "source_manifest": "/manifest.csv",
        "status": "success",
        "embedding_dim": len(embedding),
        "embedding_vector_sha256": f"embsha_{visual_unit_id}",
        "embedding_json": json.dumps(embedding),
    }
    return join, yoloe, emb


def _make_stop03_1(base: Path, run_root: Path) -> int:
    join: list[dict] = []
    yoloe: list[dict] = []
    emb: list[dict] = []
    image_supp: list[dict] = []

    def add(*args, sequence_id: str = "", representative_position: str = "", rating: str = "") -> None:
        j, y, e = _unit(*args, rating=rating)
        join.append(j)
        yoloe.append(y)
        emb.append(e)
        if j["visual_unit_type"] != "video_frame":
            image_supp.append({
                "visual_unit_id": j["visual_unit_id"],
                "preview_role": j["preview_role"],
                "sequence_id": sequence_id,
                "representative_position": representative_position,
                "Rating": rating,
            })

    base_vec = [1.0, 0.0, 0.0]
    for i in range(11):
        labels = ["person"]
        vec = base_vec
        rel = "video_a.mov"
        if i == 1:
            labels = ["screen", "text", "phone", "document", "keyboard", "monitor", "paper", "label"]
            vec = [0.0, 1.0, 0.0]
            rel = "RPReplay_screen_recording.mov"
        if i == 2:
            labels = ["vehicle", "sign", "menu", "poster", "display", "book", "laptop", "tv"]
            vec = [0.0, 0.0, 1.0]
        add(f"video_a_{i:02d}", "video_frame", "video_a", rel, i * 3000, labels, vec, "video_frame")

    for i in range(3):
        add(f"video_b_{i:02d}", "video_frame", "video_b", "video_b.mov", i * 10000, ["person"], base_vec, "video_frame")

    add("img_unmarked_doc", "image_preview", "img_unmarked_doc", "plain_document_photo.jpg", "", ["document"], [0.0, 1.0, 0.0], "normal_image")
    add("img_marked", "image_preview", "img_marked", "marked_photo.jpg", "", ["person"], [1.0, 0.0, 0.0], "normal_image", rating="5")

    for pos in ["start", "middle", "end"]:
        add(f"tl_low_{pos}", "image_preview", "timelapse_low", "timelapse_low.jpg", "", ["tree"], base_vec, "timelapse_keyframe", sequence_id="tl_low", representative_position=pos)
    for pos, vec, label in [
        ("start", [1.0, 0.0, 0.0], "tree"),
        ("middle", [0.0, 1.0, 0.0], "car"),
        ("end", [0.0, 0.0, 1.0], "building"),
    ]:
        add(f"tl_high_{pos}", "image_preview", "timelapse_high", "timelapse_high.jpg", "", [label], vec, "timelapse_keyframe", sequence_id="tl_high", representative_position=pos)

    _write_csv(base / "03_combined_report/manifests/stop03_1_visual_then_yoloe4_join_manifest.csv", join)
    _write_jsonl(base / "02_yoloe4_full/manifests/stop03_1a_yoloe_result_manifest.jsonl", yoloe)
    _write_csv(base / "02_yoloe4_full/manifests/stop03_1a_yoloe_result_manifest.csv", yoloe)
    _write_jsonl(base / "01_visual_embedding_full/manifests/stop03_1b_visual_embedding_result_manifest.jsonl", emb)
    _write_csv(base / "01_visual_embedding_full/manifests/stop03_1b_visual_embedding_result_manifest.csv", emb)
    _write_csv(run_root / "02_1_stop02_video_frames/manifests/video_frame_c4s_step01_queue_manifest.csv", [])
    _write_jsonl(run_root / "02_2_stop02_image_preview/manifests/image_preview_visual_unit_manifest.jsonl", image_supp)
    return len(join)


def _run(base: Path, run_root: Path, out: Path, expected: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--stop03-1-base",
            str(base),
            "--run-root",
            str(run_root),
            "--out",
            str(out),
            "--expected-units",
            str(expected),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _read_csv(path: Path) -> list[dict]:
    return list(csv.DictReader(path.open(encoding="utf-8", newline="")))


def test_stop03_2_fix_v2_candidate_rules(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    base = run_root / "stop03_1"
    expected = _make_stop03_1(base, run_root)
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"

    r1 = _run(base, run_root, out1, expected)
    assert r1.returncode == 0, r1.stderr + r1.stdout
    r2 = _run(base, run_root, out2, expected)
    assert r2.returncode == 0, r2.stderr + r2.stdout

    qwenvl = _read_csv(out1 / "manifests/qwenvl_high_value_candidate_queue.csv")
    ocr = _read_csv(out1 / "manifests/ocr_trigger_candidate_queue.csv")
    decisions = _read_csv(out1 / "manifests/visual_unit_candidate_decision_manifest.csv")
    q2 = _read_csv(out2 / "manifests/qwenvl_high_value_candidate_queue.csv")
    summary = json.loads((out1 / "reports/stop03_2_candidate_summary.json").read_text(encoding="utf-8"))

    q_ids = {r["visual_unit_id"] for r in qwenvl}
    o_ids = {r["visual_unit_id"] for r in ocr}
    assert len(decisions) == expected
    assert q_ids & o_ids
    assert "video_a_01" in q_ids & o_ids
    assert "img_unmarked_doc" not in q_ids
    assert "img_marked" in q_ids
    assert {"video_a", "video_b"} <= {r["source_group_id"] for r in qwenvl if r["source_group_kind"] == "video"}

    video_a_selected = [r for r in qwenvl if r["source_group_id"] == "video_a"]
    assert 1 <= len(video_a_selected) < 11
    assert any(r["min_gap_broken"] == "True" and r["min_gap_exception_reason"] for r in video_a_selected)
    assert summary["video_min_one_per_group_pass"] is True

    assert [r["visual_unit_id"] for r in qwenvl if r["timelapse_sequence_id"] == "tl_low"] == ["tl_low_middle"]
    assert 2 <= len([r for r in qwenvl if r["timelapse_sequence_id"] == "tl_high"]) <= 3
    assert "img_unmarked_doc" in o_ids
    assert summary["known_ocr_like_source_group_count"] >= 1
    assert summary["known_ocr_like_source_group_hit_count"] == summary["known_ocr_like_source_group_count"]

    score_by_id = {r["visual_unit_id"]: float(r["candidate_score"]) for r in decisions}
    assert score_by_id["video_a_00"] < score_by_id["video_a_01"]
    assert {r["candidate_id"] for r in qwenvl} == {r["candidate_id"] for r in q2}
    for row in [*qwenvl, *ocr]:
        expected_id = hashlib.sha256((POLICY_VERSION + row["queue_type"] + row["visual_unit_id"]).encode("utf-8")).hexdigest()
        assert row["candidate_id"] == expected_id
        assert row["policy_version"] == POLICY_VERSION
        assert row["reason_codes"]

    assert (out1 / "manifests/yoloe_label_inventory.csv").exists()
    assert (out1 / "manifests/yoloe_label_inventory.json").exists()
    assert not list(out1.rglob("*exclusion*"))
