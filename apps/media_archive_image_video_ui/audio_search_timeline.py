from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping


SEARCH_WINDOW_OPTIONS_MS = (5_000, 10_000)
PREVIEW_ANCHOR_OFFSET_MS = 2_000


@dataclass(frozen=True)
class TranscriptSegment:
    source_content_id: str
    start_time_ms: int
    end_time_ms: int
    text: str
    language: str | None = None

    @property
    def hit_time_ms(self) -> int:
        return self.start_time_ms + max(0, self.end_time_ms - self.start_time_ms) // 2


def preview_window_for_hit(
    hit_time_ms: int,
    window_ms: int,
    *,
    source_duration_ms: int | None = None,
    anchor_offset_ms: int = PREVIEW_ANCHOR_OFFSET_MS,
) -> dict[str, int | bool]:
    """Use the same playback window contract as the visual search path."""
    if hit_time_ms < 0:
        raise ValueError("hit_time_ms must be non-negative")
    if window_ms not in SEARCH_WINDOW_OPTIONS_MS:
        raise ValueError(f"window_ms must be one of {SEARCH_WINDOW_OPTIONS_MS}")
    start = max(0, hit_time_ms - anchor_offset_ms)
    end = start + window_ms
    requires_clamp = source_duration_ms is not None and end > source_duration_ms
    if source_duration_ms is not None:
        end = min(end, source_duration_ms)
    return {
        "start_time_ms": start,
        "end_time_ms": end,
        "hit_time_ms": hit_time_ms,
        "requires_source_duration_clamp": requires_clamp,
    }


def transcript_search_evidence(segment: TranscriptSegment) -> dict[str, object]:
    if not segment.source_content_id.strip():
        raise ValueError("source_content_id is required")
    if segment.start_time_ms < 0 or segment.end_time_ms <= segment.start_time_ms:
        raise ValueError("transcript interval must be positive")
    text = segment.text.strip()
    if not text:
        raise ValueError("transcript text is required")
    payload = asdict(segment)
    payload.update({
        "evidence_type": "speech_text",
        "hit_time_ms": segment.hit_time_ms,
        "embedding_required": True,
        "preview_windows": {
            str(window_ms): preview_window_for_hit(segment.hit_time_ms, window_ms)
            for window_ms in SEARCH_WINDOW_OPTIONS_MS
        },
    })
    return payload


def vad_sample_intervals_to_ms(
    intervals: Iterable[Mapping[str, int]],
    *,
    sampling_rate: int = 16_000,
) -> list[dict[str, int]]:
    if sampling_rate <= 0:
        raise ValueError("sampling_rate must be positive")
    return [{
        "start_time_ms": round(int(item["start"]) * 1000 / sampling_rate),
        "end_time_ms": round(int(item["end"]) * 1000 / sampling_rate),
    } for item in intervals if int(item["end"]) > int(item["start"])]


def whisper_clip_timestamps(intervals: Iterable[Mapping[str, int]]) -> list[float]:
    clips: list[float] = []
    for item in intervals:
        start = int(item["start_time_ms"])
        end = int(item["end_time_ms"])
        if start < 0 or end <= start:
            continue
        clips.extend((start / 1000.0, end / 1000.0))
    return clips


def classify_vad_timeline(
    duration_ms: int,
    speech_intervals: Iterable[Mapping[str, int]],
) -> list[dict[str, int | str]]:
    """Classify only speech vs unknown non-speech; VAD is not an event model."""
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    normalized: list[tuple[int, int]] = []
    for item in speech_intervals:
        start = max(0, int(item["start_time_ms"]))
        end = min(duration_ms, int(item["end_time_ms"]))
        if end <= start:
            continue
        if normalized and start <= normalized[-1][1]:
            normalized[-1] = (normalized[-1][0], max(normalized[-1][1], end))
        else:
            normalized.append((start, end))

    rows: list[dict[str, int | str]] = []
    cursor = 0
    for start, end in normalized:
        if start > cursor:
            rows.append({
                "start_time_ms": cursor,
                "end_time_ms": start,
                "audio_class": "non_speech_unclassified",
            })
        rows.append({
            "start_time_ms": start,
            "end_time_ms": end,
            "audio_class": "speech",
        })
        cursor = end
    if cursor < duration_ms:
        rows.append({
            "start_time_ms": cursor,
            "end_time_ms": duration_ms,
            "audio_class": "non_speech_unclassified",
        })
    return rows
