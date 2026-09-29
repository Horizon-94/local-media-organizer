from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.media_archive_image_video_ui_v125_candidate.native_bridge import (
    run_interview_content_catalog,
)
from apps.media_archive_image_video_ui_v125_candidate.repository import (
    ReadonlyMediaRepository,
)


def _database(path: Path) -> None:
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            CREATE TABLE source_assets(
                source_content_id TEXT PRIMARY KEY,
                absolute_path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                media_type TEXT NOT NULL
            );
            CREATE TABLE audio_speech_evidence(
                evidence_id TEXT PRIMARY KEY,
                source_content_id TEXT NOT NULL,
                start_time_ms INTEGER NOT NULL,
                end_time_ms INTEGER NOT NULL,
                transcript_text TEXT NOT NULL
            );
            INSERT INTO source_assets VALUES
                ('s1','/offline/a.mov','interview/a.mov','a.mov','video'),
                ('s2','/offline/b.mov','interview/b.mov','b.mov','video');
            INSERT INTO audio_speech_evidence VALUES
                ('e0','s1',100,900,'你为什么来到这里'),
                ('e1','s1',1000,5000,'我来到这里以后主要负责纪录片拍摄'),
                ('e2','s1',62000,68000,'后来开始学习剪辑，也重新理解了团队合作'),
                ('e3','s1',125000,132000,'对于未来我希望继续记录真实的人和生活'),
                ('e4','s2',2000,7000,'另一个采访谈到了产品设计和工作压力');
            """
        )


def test_interview_catalog_groups_existing_asr_by_video(tmp_path: Path) -> None:
    db = tmp_path / "media.sqlite"
    _database(db)
    repository = ReadonlyMediaRepository(db)

    report = run_interview_content_catalog(repository)

    assert report["status"] == "PASS"
    assert report["total"] == 2
    assert report["model_run"] is False
    assert report["original_media_read"] is False
    first = report["items"][0]
    assert first["file_name"] == "a.mov"
    assert first["segment_count"] == 4
    assert len(first["excerpts"]) == 3
    assert "纪录片拍摄" in first["summary"]
    assert len(first["minute_timeline"]) == 3
    assert first["question_answers"][0]["question"].endswith("你为什么来到这里")
    assert "纪录片拍摄" in first["question_answers"][0]["answer"]
    assert "为什么来到这里" in first["question_summary"]
    assert "纪录片拍摄" in first["answer_summary"]
    assert first["content_kind"] == "likely_interview"
    assert first["content_label"] == "疑似采访/问答"
    assert "提问与后续回答" in first["classification_reason"]


def test_interview_detection_is_generic_and_keeps_non_interview_speech_available(
    tmp_path: Path,
) -> None:
    db = tmp_path / "media.sqlite"
    _database(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO source_assets VALUES(?,?,?,?,?)",
            ("s3", "/offline/c.mov", "spoken/c.mov", "c.mov", "video"),
        )
        for index in range(18):
            con.execute(
                "INSERT INTO audio_speech_evidence VALUES(?,?,?,?,?)",
                (
                    f"c{index}", "s3", index * 10_000, index * 10_000 + 8_000,
                    "这是一段连续说明内容，按照顺序介绍现场情况和后续安排",
                ),
            )
    repository = ReadonlyMediaRepository(db)

    all_items = repository.interview_content_catalog(content_kind="all")["items"]
    likely = repository.interview_content_catalog(content_kind="likely_interview")["items"]
    long_spoken = repository.interview_content_catalog(content_kind="long_spoken_content")["items"]

    assert {row["source_content_id"] for row in all_items} == {"s1", "s2", "s3"}
    assert {row["source_content_id"] for row in likely} == {"s1"}
    assert {row["source_content_id"] for row in long_spoken} == {"s3"}
    spoken = next(row for row in all_items if row["source_content_id"] == "s3")
    assert spoken["content_kind"] == "long_spoken_content"
    assert spoken["content_label"] == "长口播/长对话"


def test_interview_catalog_can_limit_to_person_source_ids(tmp_path: Path) -> None:
    db = tmp_path / "media.sqlite"
    _database(db)
    repository = ReadonlyMediaRepository(db)

    page = repository.interview_content_catalog(["s2"])

    assert page["total"] == 1
    assert page["items"][0]["source_content_id"] == "s2"


def test_native_ui_exposes_interview_content_browser() -> None:
    code = Path(
        "apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift"
    ).read_text(encoding="utf-8")
    assert "采访与长内容：每个视频说了什么" in code
    assert "自动识别采访" in code
    assert "查看全部有转写视频" in code
    assert "长口播 / 长对话" in code
    assert "只看当前人物出现的视频" in code
    assert "内容概览：" in code
    assert "展开问答与逐分钟原话" in code
    assert "播放回答" in code
    assert "主要提问：" in code
    assert "回答涉及：" in code


def test_large_single_source_review_cluster_is_visible_for_interview_review(tmp_path: Path) -> None:
    db = tmp_path / "people.sqlite"
    preview = tmp_path / "face.jpg"
    preview.write_bytes(b"preview")
    with sqlite3.connect(db) as con:
        con.executescript(
            f"""
            CREATE TABLE source_assets(
                source_content_id TEXT PRIMARY KEY,absolute_path TEXT,
                relative_path TEXT,file_name TEXT,media_type TEXT
            );
            CREATE TABLE derived_assets(
                derived_id TEXT PRIMARY KEY,source_content_id TEXT,
                derived_path TEXT,time_position_ms INTEGER
            );
            CREATE TABLE v_stop03_1c_latest_person_cluster_members(
                person_cluster_id TEXT,member_count INTEGER,
                distinct_source_count INTEGER,cluster_confidence TEXT,
                human_review_status TEXT,anonymous_display_name TEXT,
                visual_unit_id TEXT,source_content_id TEXT,derived_id TEXT,
                media_type TEXT,time_position_ms INTEGER,face_id TEXT,
                representative_face_id TEXT,similarity_to_representative REAL
            );
            INSERT INTO source_assets VALUES('s1','/offline/a.mov','a.mov','a.mov','video');
            INSERT INTO derived_assets VALUES('d1','s1','{preview}',1000);
            INSERT INTO v_stop03_1c_latest_person_cluster_members VALUES(
                'review-person',442,1,'review','unreviewed','',
                'v1','s1','d1','video',1000,'f1','f1',1.0
            );
            """
        )
    catalog = ReadonlyMediaRepository(db).person_cluster_catalog()
    assert catalog["total"] == 1
    assert catalog["items"][0]["cluster_confidence"] == "review"
