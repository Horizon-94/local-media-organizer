#!/usr/bin/env python3
"""Build a read-only media inventory and a bounded representative sample.

The script never opens media for writing.  It stores local-only paths in the
requested output directory; callers must not commit the generated manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".braw"}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".hif", ".tif", ".tiff",
    ".dng", ".arw", ".rw2", ".cr2", ".cr3",
}


def ffprobe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height,avg_frame_rate",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=30
        )
        return json.loads(completed.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return {"error": type(exc).__name__, "detail": str(exc)[:500]}


def video_summary(path: Path) -> dict[str, Any]:
    stat = path.stat()
    metadata = ffprobe(path) if path.suffix.lower() != ".braw" else {
        "error": "ffprobe_braw_not_supported"
    }
    streams = metadata.get("streams") or []
    video_stream = next((row for row in streams if row.get("codec_type") == "video"), {})
    audio_streams = [row for row in streams if row.get("codec_type") == "audio"]
    fmt = metadata.get("format") or {}
    return {
        "path": str(path),
        "relative_path": None,
        "bytes": stat.st_size,
        "extension": path.suffix.lower(),
        "codec": video_stream.get("codec_name"),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "fps": video_stream.get("avg_frame_rate"),
        "duration_seconds": float(fmt["duration"]) if fmt.get("duration") else None,
        "has_audio": bool(audio_streams),
        "probe_error": metadata.get("error"),
    }


def choose_videos(rows: list[dict[str, Any]], limit: int, max_bytes: int) -> list[dict[str, Any]]:
    """Greedy diversity sampler; source files remain in place and read-only."""
    selected: list[dict[str, Any]] = []
    selected_paths: set[str] = set()
    used = 0

    def add(row: dict[str, Any]) -> None:
        nonlocal used
        if len(selected) >= limit or row["path"] in selected_paths:
            return
        if selected and used + int(row["bytes"]) > max_bytes:
            return
        selected.append(row)
        selected_paths.add(row["path"])
        used += int(row["bytes"])

    usable = [row for row in rows if not row.get("probe_error")]
    buckets = [
        sorted(usable, key=lambda row: row.get("duration_seconds") or 0, reverse=True),
        sorted(usable, key=lambda row: row.get("duration_seconds") or 0),
        sorted(usable, key=lambda row: row["bytes"], reverse=True),
        sorted(usable, key=lambda row: row["bytes"]),
        [row for row in usable if row.get("codec") == "hevc"],
        [row for row in usable if row.get("codec") == "h264"],
        [row for row in usable if row.get("has_audio")],
        [row for row in usable if not row.get("has_audio")],
        [row for row in usable if int(row.get("width") or 0) >= 3840],
    ]
    while len(selected) < limit and any(buckets):
        for bucket in buckets:
            while bucket and bucket[0]["path"] in selected_paths:
                bucket.pop(0)
            if bucket:
                add(bucket.pop(0))
            if len(selected) >= limit:
                break
        if all(not bucket for bucket in buckets):
            break
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--video-limit", type=int, default=10)
    parser.add_argument("--image-limit", type=int, default=20)
    parser.add_argument("--max-sample-gib", type=float, default=8.0)
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"source_not_directory:{source}")
    output.mkdir(parents=True, exist_ok=True)

    extension_counts: Counter[str] = Counter()
    total_bytes = 0
    videos: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    for root, _directories, names in os.walk(source):
        for name in names:
            path = Path(root) / name
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                stat = path.stat()
            except OSError:
                continue
            suffix = path.suffix.lower()
            extension_counts[suffix or "(none)"] += 1
            total_bytes += stat.st_size
            if suffix in VIDEO_EXTENSIONS:
                row = video_summary(path)
                row["relative_path"] = str(path.relative_to(source))
                videos.append(row)
            elif suffix in IMAGE_EXTENSIONS:
                images.append({
                    "path": str(path),
                    "relative_path": str(path.relative_to(source)),
                    "bytes": stat.st_size,
                    "extension": suffix,
                })

    sample_videos = choose_videos(
        videos, args.video_limit, int(args.max_sample_gib * 1024 ** 3)
    )
    image_buckets: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(images, key=lambda item: item["bytes"], reverse=True):
        image_buckets.setdefault(row["extension"], []).append(row)
    sample_images: list[dict[str, Any]] = []
    while len(sample_images) < args.image_limit and any(image_buckets.values()):
        for extension in sorted(image_buckets):
            if image_buckets[extension]:
                sample_images.append(image_buckets[extension].pop(0))
                if len(sample_images) >= args.image_limit:
                    break

    payload = {
        "contract": "local_media_pipeline_benchmark_inventory_v1",
        "source": str(source),
        "readonly": True,
        "total_bytes": total_bytes,
        "total_gib": round(total_bytes / 1024 ** 3, 3),
        "total_gb": round(total_bytes / 1000 ** 3, 3),
        "file_count": sum(extension_counts.values()),
        "video_count": len(videos),
        "image_count": len(images),
        "extension_counts": dict(extension_counts.most_common()),
        "video_inventory": videos,
        "sample": {
            "video_count": len(sample_videos),
            "image_count": len(sample_images),
            "bytes": sum(row["bytes"] for row in sample_videos + sample_images),
            "videos": sample_videos,
            "images": sample_images,
        },
    }
    destination = output / "inventory.json"
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "inventory": str(destination),
        "video_count": len(videos),
        "image_count": len(images),
        "sample_video_count": len(sample_videos),
        "sample_image_count": len(sample_images),
        "sample_gib": round(payload["sample"]["bytes"] / 1024 ** 3, 3),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
