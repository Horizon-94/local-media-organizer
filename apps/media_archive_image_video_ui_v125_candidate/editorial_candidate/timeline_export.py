from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from .source_timecode import embedded_ltc


FCPXML_VERSION = "1.9"
SUPPORTED_FRAME_RATES = {
    Fraction(24000, 1001): "23.976",
    Fraction(24, 1): "24",
    Fraction(25, 1): "25",
    Fraction(30000, 1001): "29.97",
    Fraction(30, 1): "30",
    Fraction(48, 1): "48",
    Fraction(50, 1): "50",
    Fraction(60000, 1001): "59.94",
    Fraction(60, 1): "60",
    Fraction(100, 1): "100",
    Fraction(120000, 1001): "119.88",
    Fraction(120, 1): "120",
}


def normalize_frame_rate(value: object) -> Fraction:
    aliases = {
        "23.976": Fraction(24000, 1001),
        "23.98": Fraction(24000, 1001),
        "29.97": Fraction(30000, 1001),
        "59.94": Fraction(60000, 1001),
        "119.88": Fraction(120000, 1001),
    }
    normalized = str(value or "25").strip()
    if normalized in aliases:
        return aliases[normalized]
    try:
        rate = Fraction(normalized)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("unsupported timeline frame rate") from exc
    if rate not in SUPPORTED_FRAME_RATES:
        raise ValueError(
            "timeline frame rate must be 23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60, 100, 119.88, or 120"
        )
    return rate


def estimate_script_duration_ms(text: str, track: str = "documentary") -> int:
    readable = "".join(character for character in str(text or "") if not character.isspace() and character not in "，,。！？!?；;：:\"'“”‘’（）()【】")
    chars_per_second = 5.2 if track == "short_video" else 4.0
    seconds = max(1.5, len(readable) / chars_per_second + 0.4)
    return int(round(seconds * 1000))


def _seconds_value(value: Fraction) -> str:
    if value < 0:
        raise ValueError("timeline time cannot be negative")
    return f"{value.numerator}s" if value.denominator == 1 else f"{value.numerator}/{value.denominator}s"


def _frame_aligned_time(milliseconds: int, frame_rate: Fraction, *, minimum_one_frame: bool = False) -> Fraction:
    if milliseconds < 0:
        raise ValueError("timeline time cannot be negative")
    exact_frames = Fraction(milliseconds, 1000) * frame_rate
    frame_count = (exact_frames.numerator * 2 + exact_frames.denominator) // (2 * exact_frames.denominator)
    if minimum_one_frame:
        frame_count = max(1, frame_count)
    return Fraction(frame_count, 1) / frame_rate


def _timecode_start(value: str, frame_rate: Fraction) -> Fraction:
    normalized = str(value or "").strip()
    if not normalized:
        return Fraction(0)
    fields = normalized.replace(";", ":").split(":")
    if len(fields) != 4 or not all(field.isdigit() for field in fields):
        raise ValueError(f"unsupported source timecode: {value}")
    hours, minutes, seconds, frames = (int(field) for field in fields)
    nominal_rate = round(float(frame_rate))
    frame_count = ((hours * 3600 + minutes * 60 + seconds) * nominal_rate) + frames
    if ";" in normalized:
        drop_frames = round(nominal_rate * 0.0666666667)
        total_minutes = hours * 60 + minutes
        frame_count -= drop_frames * (total_minutes - total_minutes // 10)
    return Fraction(frame_count, 1) / frame_rate


def probe_source_timing(path_value: object) -> dict[str, Any]:
    path = Path(str(path_value or "")).expanduser()
    if not path.is_absolute() or not path.is_file():
        volume = path.parts[2] if len(path.parts) > 2 and path.parts[1] == "Volumes" else "原始素材所在硬盘"
        raise ValueError(f"无法导出：请先挂载硬盘“{volume}”并确认原片仍在原路径：{path}")
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_tag_string,r_frame_rate,avg_frame_rate,width,height,duration:stream_tags=timecode",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ValueError(f"source media timing probe failed: {path.name}: {exc}") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise ValueError(f"source media has no readable streams: {path.name}")
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    if not isinstance(video, dict):
        raise ValueError(f"source media has no video stream: {path.name}")
    try:
        frame_rate = Fraction(str(video.get("r_frame_rate") or "0"))
        duration = Fraction(str(video.get("duration") or "0"))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"source media timing is invalid: {path.name}") from exc
    if frame_rate <= 0 or duration <= 0:
        raise ValueError(f"source media timing is incomplete: {path.name}")
    try:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"source media dimensions are invalid: {path.name}") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"source media dimensions are incomplete: {path.name}")
    timecode_stream = next(
        (
            row
            for row in streams
            if row.get("codec_type") == "data"
            and isinstance(row.get("tags"), dict)
            and row["tags"].get("timecode")
        ),
        video,
    )
    tags = timecode_stream.get("tags") if isinstance(timecode_stream.get("tags"), dict) else {}
    # RTMD data streams can carry a valid timecode but report both rates as
    # "0/0". That is ffprobe's unavailable marker, not a corrupt media rate.
    # Prefer an explicit timecode-track rate (e.g. QuickTime), then the already
    # validated video rate. Do not silently accept malformed non-empty values.
    timecode_rate = frame_rate
    timecode_rate_basis = "video_rate_fallback"
    for field in ("avg_frame_rate", "r_frame_rate"):
        raw_rate = str(timecode_stream.get(field) or "").strip()
        if raw_rate in {"", "0", "0/0", "0/1", "N/A"}:
            continue
        try:
            value = Fraction(raw_rate)
            if value <= 0:
                raise ValueError("nonpositive rate")
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"source media timecode rate is invalid: {path.name}") from exc
        timecode_rate = value
        timecode_rate_basis = f"timecode_stream_{field}"
        break
    source_timecode = str(tags.get("timecode") or "")
    if timecode_stream.get("codec_tag_string") == "rtmd":
        source_timecode, timecode_rate = embedded_ltc(path, frame_rate)
        timecode_rate_basis = "embedded_ltc_frame_zero"
    start = _timecode_start(source_timecode, timecode_rate)
    return {
        "source_start_seconds": str(start),
        "source_duration_seconds": str(duration),
        "source_frame_rate": str(frame_rate),
        "source_timecode_rate": str(timecode_rate),
        "source_timecode_rate_basis": timecode_rate_basis,
        "source_width": width,
        "source_height": height,
        "source_timecode": source_timecode,
        "source_timecode_reported_by_probe": str(tags.get("timecode") or ""),
        "has_audio": any(row.get("codec_type") == "audio" for row in streams),
    }


def _source_uri(value: object) -> str:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise ValueError("timeline source path must be absolute")
    return path.as_uri()


def build_fcpxml(
    items: Iterable[dict[str, Any]],
    *,
    timeline_name: str = "P0 候选粗剪",
    frame_rate: object = 25,
    resolve_compatible: bool = False,
) -> bytes:
    """Build a relinkable rough cut from provisional sampled-frame ranges."""

    timeline_rate = normalize_frame_rate(frame_rate)
    all_rows = list(items)
    rows = [row for row in all_rows if row.get("item_kind") != "backup_clip"]
    backups: dict[int, list[dict[str, Any]]] = {}
    for row in all_rows:
        if row.get("item_kind") == "backup_clip":
            backups.setdefault(int(row["beat_order"]), []).append(row)
    if not rows:
        raise ValueError("at least one clip or script gap is required")
    if set(backups) - {int(row["beat_order"]) for row in rows}:
        raise ValueError("backup clip must have a main clip or script gap in the same beat")
    rows.sort(key=lambda row: (int(row["beat_order"]), int(row.get("choice_order", 0))))

    clean_name = str(timeline_name or "P0 候选粗剪").strip()[:120] or "P0 候选粗剪"
    frame_duration = _seconds_value(Fraction(1, 1) / timeline_rate)
    root = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    resources = ET.SubElement(root, "resources")
    if resolve_compatible:
        # Match Resolve's own native generator/title exports. No sidecar media,
        # transcoding, or burned-in subtitles. Legacy callers retain gap format.
        ET.SubElement(resources, "effect", {
            "id": "black", "name": "Vivid",
            "uid": ".../Generators.localized/Solids.localized/Vivid.localized/Vivid.motn",
        })
        ET.SubElement(resources, "effect", {
            "id": "script-title", "name": "Basic Title",
            "uid": ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti",
        })
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": f"FFVideoFormat1080p{SUPPORTED_FRAME_RATES[timeline_rate]}",
            "frameDuration": frame_duration,
            "width": "1920",
            "height": "1080",
        },
    )

    by_source: dict[str, dict[str, Any]] = {}
    for row in all_rows:
        if row.get("item_kind") in {"script_gap", "a_roll_gap"}:
            duration_ms = int(row.get("duration_ms") or 0)
            if duration_ms <= 0:
                raise ValueError("timeline script gap has an invalid duration")
            continue
        source_id = str(row.get("source_content_id") or row.get("candidate_id") or "")
        if not source_id:
            raise ValueError("timeline item is missing source identity")
        start_ms = int(row["start_ms"])
        end_ms = int(row["end_ms"])
        if end_ms <= start_ms:
            raise ValueError("timeline item has an invalid provisional range")
        existing = by_source.get(source_id)
        if existing is None:
            source_start = Fraction(str(row.get("source_start_seconds") or "0"))
            source_duration = Fraction(str(row.get("source_duration_seconds") or Fraction(end_ms, 1000)))
            source_frame_rate = Fraction(str(row.get("source_frame_rate") or timeline_rate))
            source_width = int(row.get("source_width") or 1920)
            source_height = int(row.get("source_height") or 1080)
            if source_frame_rate <= 0 or source_width <= 0 or source_height <= 0:
                raise ValueError("timeline source media format is invalid")
            by_source[source_id] = {
                "resource_id": f"a{len(by_source) + 1}",
                "source_file": str(row.get("source_file") or Path(str(row["source_absolute_path"])).name),
                "source_absolute_path": str(row["source_absolute_path"]),
                "source_start": source_start,
                "source_duration": source_duration,
                "source_frame_rate": source_frame_rate,
                "source_width": source_width,
                "source_height": source_height,
                "has_audio": bool(row.get("has_audio", False)),
            }
        else:
            minimum_duration = Fraction(end_ms, 1000)
            existing["source_duration"] = max(Fraction(existing["source_duration"]), minimum_duration)

    source_formats: dict[tuple[int, int, Fraction], str] = {}
    for source in by_source.values():
        format_key = (
            int(source["source_width"]),
            int(source["source_height"]),
            Fraction(source["source_frame_rate"]),
        )
        if format_key not in source_formats:
            format_id = f"f{len(source_formats) + 1}"
            source_formats[format_key] = format_id
            width, height, source_rate = format_key
            ET.SubElement(
                resources,
                "format",
                {
                    "id": format_id,
                    "name": f"SourceFormat{width}x{height}p{float(source_rate):.3f}",
                    "frameDuration": _seconds_value(Fraction(1, 1) / source_rate),
                    "width": str(width),
                    "height": str(height),
                },
            )
        source["format_id"] = source_formats[format_key]

    for source in by_source.values():
        asset = ET.SubElement(
            resources,
            "asset",
            {
                "id": str(source["resource_id"]),
                "name": Path(str(source["source_file"])).name,
                "start": _seconds_value(Fraction(source["source_start"])),
                "duration": _seconds_value(Fraction(source["source_duration"])),
                "hasVideo": "1",
                "format": str(source["format_id"]),
                **({"hasAudio": "1"} if source["has_audio"] else {}),
            },
        )
        ET.SubElement(
            asset,
            "media-rep",
            {
                "kind": "original-media",
                "src": _source_uri(source["source_absolute_path"]),
                "suggestedFilename": Path(str(source["source_file"])).name,
            },
        )

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": "P0 候选粗剪"})
    project = ET.SubElement(event, "project", {"name": clean_name})
    total_duration = sum(
        (
            _frame_aligned_time(int(row.get("duration_ms") or 0), timeline_rate, minimum_one_frame=True)
            if row.get("item_kind") in {"script_gap", "a_roll_gap"}
            else _frame_aligned_time(int(row["end_ms"]) - int(row["start_ms"]), timeline_rate, minimum_one_frame=True)
        )
        for row in rows
    )
    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": "r1",
            "duration": _seconds_value(total_duration),
            "tcStart": "0s",
            "tcFormat": "NDF",
            "audioLayout": "stereo",
            "audioRate": "48k",
        },
    )
    spine = ET.SubElement(sequence, "spine")
    reference_lane = max(2, max((len(v) + 1 for v in backups.values()), default=2))

    def attach_script(parent: ET.Element, row: dict[str, Any], start: Fraction, duration: Fraction) -> None:
        if not resolve_compatible:
            return
        order = int(row['beat_order'])
        title = ET.SubElement(parent, "title", {
            "ref": "script-title", "lane": str(reference_lane), "offset": _seconds_value(start),
            "start": "0s", "duration": _seconds_value(duration), "enabled": "0",
            "name": f"文稿 {order:03d}｜{row.get('beat_text', '')}",
        })
        text = ET.SubElement(title, "text")
        style_id = f"script-style-{order}-{row.get('choice_order', 0)}"
        ET.SubElement(text, "text-style", {"ref": style_id}).text = str(row.get('beat_text') or '')
        style = ET.SubElement(title, "text-style-def", {"id": style_id})
        ET.SubElement(style, "text-style", {
            "font": "PingFang SC", "fontSize": "48", "fontColor": "1 1 1 1", "alignment": "center",
        })
        ET.SubElement(title, "note").text = "精剪文稿参考｜默认禁用，不覆盖画面；需要时启用文字片段或在检查器阅读。"

    def attach_backups(parent: ET.Element, beat_order: int, parent_start: Fraction, parent_duration: Fraction) -> None:
        for lane, backup in enumerate(backups.pop(beat_order, []), 1):
            source_id = str(backup.get("source_content_id") or backup.get("candidate_id"))
            source = by_source[source_id]
            source_in = Fraction(source["source_start"]) + _frame_aligned_time(int(backup["start_ms"]), Fraction(source["source_frame_rate"]))
            duration = min(parent_duration, _frame_aligned_time(int(backup["end_ms"]) - int(backup["start_ms"]), timeline_rate, minimum_one_frame=True))
            alternative = ET.SubElement(parent, "asset-clip", {
                "name": f"备选 {lane} · {beat_order:02d} · {Path(str(backup.get('source_file') or '')).name}",
                "ref": str(source["resource_id"]),
                "lane": str(lane),
                "offset": _seconds_value(parent_start),
                "start": _seconds_value(source_in),
                "duration": _seconds_value(duration),
                "enabled": "0",
            })
            ET.SubElement(alternative, "note").text = "人工备选｜默认禁用，不遮挡主剪、不参与播放｜" + str(backup.get("beat_text") or "")
            ET.SubElement(alternative, "marker", {
                "start": _seconds_value(source_in), "duration": frame_duration,
                "value": f"备选｜第 {beat_order} 句｜{backup.get('beat_text', '')}"[:250],
            })

    timeline_offset = Fraction(0)
    for row in rows:
        beat_order = int(row["beat_order"])
        beat_text = str(row.get("beat_text") or "").strip()
        if row.get("item_kind") in {"script_gap", "a_roll_gap"}:
            duration_ms = int(row["duration_ms"])
            timeline_duration = _frame_aligned_time(duration_ms, timeline_rate, minimum_one_frame=True)
            is_a_roll = row.get("item_kind") == "a_roll_gap"
            gap = ET.SubElement(
                spine,
                "video" if resolve_compatible else "gap",
                {
                    **({"ref": "black", "enabled": "1"} if resolve_compatible else {}),
                    "name": f"{beat_order:02d} · {'保留 A-roll' if is_a_roll else '黑屏缺口'}",
                    "offset": _seconds_value(timeline_offset),
                    "start": "0s",
                    "duration": _seconds_value(timeline_duration),
                },
            )
            note = ET.SubElement(gap, "note")
            note.text = (
                f"A-roll 占位｜段落 {beat_order:02d}｜{beat_text}｜知识库建议保护人物表达；请在精剪时指定真实主叙述原片"
                if is_a_roll else
                f"黑屏缺口占位｜段落 {beat_order:02d}｜{beat_text}｜尚未选镜，可补镜头或保留黑场"
            )
            attach_backups(gap, beat_order, Fraction(0), timeline_duration)
            attach_script(gap, row, Fraction(0), timeline_duration)
            ET.SubElement(
                gap,
                "marker",
                {
                    "start": "0s",
                    "duration": frame_duration,
                    "value": f"{'KEEP_A_ROLL' if is_a_roll else '缺口'}｜段落 {beat_order:02d}｜{beat_text}"[:250],
                    "completed": "0",
                },
            )
            timeline_offset += timeline_duration
            continue
        source_id = str(row.get("source_content_id") or row.get("candidate_id"))
        start_ms = int(row["start_ms"])
        duration_ms = int(row["end_ms"]) - start_ms
        source_start = Fraction(by_source[source_id]["source_start"])
        source_rate = Fraction(by_source[source_id]["source_frame_rate"])
        clip_start = source_start + _frame_aligned_time(start_ms, source_rate)
        timeline_duration = _frame_aligned_time(duration_ms, timeline_rate, minimum_one_frame=True)
        role = str(row.get("role") or "待定")
        clip = ET.SubElement(
            spine,
            "asset-clip",
            {
                "name": f"{beat_order:02d} · {Path(str(row.get('source_file') or '')).name}",
                "ref": str(by_source[source_id]["resource_id"]),
                "offset": _seconds_value(timeline_offset),
                "start": _seconds_value(clip_start),
                "duration": _seconds_value(timeline_duration),
            },
        )
        note = ET.SubElement(clip, "note")
        if row.get("time_basis") == "detected_shot_window":
            shot_in_ms = int(row.get("shot_in_ms") or 0)
            shot_out_ms = int(row.get("shot_out_ms") or 0)
            note.text = (
                f"候选粗剪｜段落 {beat_order:02d}｜{beat_text}｜用途：{role}｜"
                f"检测镜头边界 {shot_in_ms / 1000:.3f}-{shot_out_ms / 1000:.3f}s｜"
                "候选窗口已受镜头边界约束，clean 入出点未验证"
            )
        else:
            note.text = f"候选粗剪｜段落 {beat_order:02d}｜{beat_text}｜用途：{role}｜临时范围，入出点未验证"
        if row.get("source_timecode") and row.get("source_timecode_rate_basis") == "video_rate_fallback":
            note.text += "｜时间码轨未提供帧率，按原视频帧率换算；请在剪辑软件复核"
        attach_backups(clip, beat_order, clip_start, timeline_duration)
        attach_script(clip, row, clip_start, timeline_duration)
        ET.SubElement(
            clip,
            "marker",
            {
                "start": _seconds_value(clip_start),
                "duration": frame_duration,
                "value": f"段落 {beat_order:02d}｜{beat_text}"[:250],
                "completed": "0",
            },
        )
        timeline_offset += timeline_duration

    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    declaration_end = xml_body.find(b"?>") + 2
    return xml_body[:declaration_end] + b"\n<!DOCTYPE fcpxml>" + xml_body[declaration_end:]
