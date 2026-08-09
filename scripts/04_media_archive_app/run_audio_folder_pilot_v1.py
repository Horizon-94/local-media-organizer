#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.media_archive_image_video_ui.audio_enhancement import (  # noqa: E402
    reusable_audio_pilot_report,
)


VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mts", ".mxf"}


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--silero-root", type=Path, required=True)
    parser.add_argument("--whisper-model", type=Path, required=True)
    parser.add_argument("--deep-filter-executable", type=Path)
    parser.add_argument("--deep-filter-model", type=Path)
    parser.add_argument(
        "--enhancement-failure-policy", choices=("fallback", "fail"), default="fallback"
    )
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--ffprobe", default="/opt/homebrew/bin/ffprobe")
    args = parser.parse_args()

    source_root = args.source_root.resolve(strict=True)
    source_dir = args.source_dir.resolve(strict=True)
    source_dir.relative_to(source_root)
    if not 1 <= args.limit <= 10:
        raise RuntimeError("audio_folder_pilot_limit_must_be_1_to_10")
    videos = sorted(
        (
            path for path in source_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in VIDEO_SUFFIXES
        ),
        key=lambda path: (path.stat().st_size, path.name.casefold()),
    )[: args.limit]
    if not videos:
        raise RuntimeError("audio_folder_pilot_no_videos")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    single_pilot = Path(__file__).with_name("run_audio_search_pilot_v1.py")
    items: list[dict[str, object]] = []
    for video in videos:
        before = video.stat()
        probe = run([
            args.ffprobe, "-v", "error", "-show_streams", "-of", "json", str(video)
        ])
        if probe.returncode != 0:
            items.append({
                "video": str(video), "status": "FAIL", "reason": "FFPROBE_FAILED",
                "stderr": probe.stderr[-2000:],
            })
            continue
        streams = json.loads(probe.stdout).get("streams") or []
        audio_stream_count = sum(row.get("codec_type") == "audio" for row in streams)
        if audio_stream_count == 0:
            items.append({
                "video": str(video), "status": "NO_AUDIO_STREAM",
                "audio_stream_count": 0, "source_read_only": True,
            })
            continue
        item_id = hashlib.sha256(str(video).encode("utf-8")).hexdigest()[:16]
        item_out = output / item_id
        report_path = item_out / "audio_search_pilot.json"
        if report_path.is_file():
            try:
                prior = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prior = {}
            if reusable_audio_pilot_report(
                prior,
                video=video,
                deep_filter_executable=args.deep_filter_executable,
                deep_filter_model=args.deep_filter_model,
                enhancement_failure_policy=args.enhancement_failure_policy,
            ):
                segment_count = int(prior.get("transcript_segment_count") or 0)
                items.append({
                    "video": str(video),
                    "status": "PASS_SPEECH" if segment_count else "PASS_NO_SPEECH",
                    "audio_stream_count": audio_stream_count,
                    "speech_interval_count": len(prior.get("speech_intervals") or []),
                    "transcript_segment_count": segment_count,
                    "transcript_text": prior.get("transcript_text") or "",
                    "speech_enhancement": prior.get("speech_enhancement"),
                    "source_read_only": True,
                    "pilot_report": str(report_path),
                    "reused_existing_result": True,
                })
                continue
        command = [
            sys.executable,
            str(single_pilot),
            "--video", str(video),
            "--source-root", str(source_root),
            "--output-dir", str(item_out),
            "--silero-root", str(args.silero_root),
            "--whisper-model", str(args.whisper_model),
            "--enhancement-failure-policy", args.enhancement_failure_policy,
        ]
        if bool(args.deep_filter_executable) != bool(args.deep_filter_model):
            raise RuntimeError(
                "DeepFilterNet requires both --deep-filter-executable and --deep-filter-model"
            )
        if args.deep_filter_executable and args.deep_filter_model:
            command.extend([
                "--deep-filter-executable", str(args.deep_filter_executable),
                "--deep-filter-model", str(args.deep_filter_model),
            ])
        completed = run(command)
        if completed.returncode != 0 or not report_path.is_file():
            items.append({
                "video": str(video), "status": "FAIL", "reason": "AUDIO_PILOT_FAILED",
                "exit_code": completed.returncode, "stderr": completed.stderr[-3000:],
            })
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        after = video.stat()
        unchanged = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        segment_count = int(report.get("transcript_segment_count") or 0)
        items.append({
            "video": str(video),
            "status": "PASS_SPEECH" if segment_count else "PASS_NO_SPEECH",
            "audio_stream_count": audio_stream_count,
            "speech_interval_count": len(report.get("speech_intervals") or []),
            "transcript_segment_count": segment_count,
            "transcript_text": report.get("transcript_text") or "",
            "speech_enhancement": report.get("speech_enhancement"),
            "source_read_only": unchanged,
            "pilot_report": str(report_path),
            "reused_existing_result": False,
        })

    failed = [row for row in items if row["status"] == "FAIL"]
    source_read_only = all(row.get("source_read_only", True) for row in items)
    manifest = {
        "contract": "media_archive_audio_folder_pilot_v1",
        "status": "PASS" if not failed and source_read_only else "FAIL",
        "source_root": str(source_root),
        "source_dir": str(source_dir),
        "selected_video_count": len(videos),
        "speech_video_count": sum(row["status"] == "PASS_SPEECH" for row in items),
        "no_speech_video_count": sum(row["status"] == "PASS_NO_SPEECH" for row in items),
        "no_audio_stream_count": sum(row["status"] == "NO_AUDIO_STREAM" for row in items),
        "failed_count": len(failed),
        "reused_count": sum(bool(row.get("reused_existing_result")) for row in items),
        "source_read_only": source_read_only,
        "items": items,
    }
    manifest_path = output / "audio_folder_pilot.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "status": manifest["status"],
        "selected": len(videos),
        "speech": manifest["speech_video_count"],
        "no_speech": manifest["no_speech_video_count"],
        "no_audio": manifest["no_audio_stream_count"],
        "failed": manifest["failed_count"],
        "manifest": str(manifest_path),
    }, ensure_ascii=False))
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
