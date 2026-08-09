#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--initial-folder-report", type=Path, required=True)
    parser.add_argument("--retry-report", type=Path, required=True)
    parser.add_argument("--embedding-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    initial = load(args.initial_folder_report)
    retry = load(args.retry_report)
    embedding = load(args.embedding_report)
    accepted_initial = [
        row for row in initial.get("items", [])
        if row.get("status") in {"PASS_SPEECH", "PASS_NO_SPEECH", "NO_AUDIO_STREAM"}
    ]
    failed_initial = [row for row in initial.get("items", []) if row.get("status") == "FAIL"]
    retry_source = str(retry.get("source_video") or "")
    retry_matches_failure = (
        len(failed_initial) == 1 and failed_initial[0].get("video") == retry_source
    )
    checks = {
        "three_real_videos_selected": initial.get("selected_video_count") == 3,
        "two_no_speech_results_preserved": sum(
            row.get("status") == "PASS_NO_SPEECH" for row in accepted_initial
        ) == 2,
        "failed_item_retried_only": retry_matches_failure,
        "retry_vad_whisper_pass": (
            retry.get("status") == "PASS"
            and len(retry.get("speech_intervals") or []) > 0
            and int(retry.get("transcript_segment_count") or 0) > 0
        ),
        "embedding_search_pass": embedding.get("status") == "PASS",
        "all_sources_read_only": (
            initial.get("source_read_only") is True
            and retry.get("source_read_only") is True
            and embedding.get("source_read_only") is True
        ),
        "production_database_untouched": embedding.get("production_database_write") is False,
        "offline_embedding": embedding.get("network_used") is False,
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    summary = {
        "contract": "media_archive_audio_search_acceptance_v1",
        "status": status,
        "checks": checks,
        "final_real_video_counts": {
            "selected": 3,
            "speech": 1 if checks["retry_vad_whisper_pass"] else 0,
            "no_speech": 2,
            "no_audio_stream": 0,
            "failed": 0 if checks["failed_item_retried_only"] else 1,
        },
        "speech_interval_count": len(retry.get("speech_intervals") or []),
        "transcript_segment_count": retry.get("transcript_segment_count"),
        "embedding_dimension": embedding.get("dimension"),
        "pipeline_placement": {
            "recommended_stage": "NEW_STAGE_04_AUDIO_VAD_WHISPER",
            "reason": "必须读取原视频音轨，应紧跟现有第3阶段视频抽帧，并位于素材盘可拔出边界之前",
            "text_embedding_reuse": "语音文本在后续统一文本向量阶段与OCR/Qwen文本一起向量化",
            "resulting_stage_count": 20,
            "implementation_status": "NOT_IMPLEMENTED_IN_PRODUCTION_PIPELINE",
        },
        "non_speech_boundary": (
            "Silero只可靠区分人声与非人声；音乐、风噪、环境音仍需独立音频事件分类器"
        ),
        "evidence": {
            "initial_folder_report": str(args.initial_folder_report.resolve()),
            "retry_report": str(args.retry_report.resolve()),
            "embedding_report": str(args.embedding_report.resolve()),
        },
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "output": str(output), "checks": checks}, ensure_ascii=False))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
