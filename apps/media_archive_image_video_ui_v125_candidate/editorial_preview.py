"""Resolve one editorial candidate to its original file; never decode or index media."""
from pathlib import Path
from typing import Any

from .editorial_candidate.db_adapter import resolve_timeline_sources


def resolve_editorial_preview(
    database: Path, candidate_id: str, source_content_id: str, board_database: Path,
) -> dict[str, Any]:
    if database.resolve() != board_database.resolve():
        raise ValueError("PREVIEW_LIBRARY_CHANGED：素材库已切换，请在当前素材库重新生成候选后播放。")
    if not candidate_id or not source_content_id or len(candidate_id) > 200:
        raise ValueError("PREVIEW_NO_SOURCE：黑屏或保留口播是剪辑方案，没有可播放的原片。")
    source = resolve_timeline_sources(database, [candidate_id]).get(candidate_id)
    if source is None or source["source_content_id"] != source_content_id:
        raise ValueError("PREVIEW_CANDIDATE_MISSING：当前素材库找不到这条候选的原片记录，请重新生成候选。")
    raw_path = source["source_absolute_path"]
    path = Path(raw_path)
    if not raw_path or not path.is_absolute():
        raise ValueError("PREVIEW_PATH_INVALID：数据库没有有效的原文件绝对路径。")
    if source["media_type"] not in {"video", "image", "audio"}:
        raise ValueError("PREVIEW_UNSUPPORTED_TYPE：这条候选不是可预览的媒体文件。")
    # Check the actual file on every click: cached online_status can predate a
    # drive reconnection. Do not fall back to a thumbnail or another same-name file.
    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        raise ValueError(f"PREVIEW_SOURCE_UNAVAILABLE：无法读取原片，请检查素材盘是否已连接、路径与读取权限。\n{path}") from exc
    return {
        "status": "PASS", "ok": True, "candidate_id": candidate_id,
        "source_content_id": source_content_id, "source_path": str(path),
        "media_type": source["media_type"], "database": str(database.resolve()),
    }
