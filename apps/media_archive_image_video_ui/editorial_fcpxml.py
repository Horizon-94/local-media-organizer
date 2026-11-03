"""Pure FCPXML 1.9 serialization adapted from audited 1.2.8 exporter.
Current manifest identity/resolution replaces all legacy retrieval authority.
"""
from __future__ import annotations
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET
import json
import subprocess
import shutil

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


def _source_uri(value: object) -> str:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise ValueError("timeline source path must be absolute")
    return path.as_uri()


def _serialize_fcpxml(
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


def probe_timing(path, ffprobe=None):
    executable = ffprobe or shutil.which('ffprobe')
    if not executable:
        raise ValueError('ffprobe runtime unavailable for source timing')
    result = subprocess.run([str(executable), '-v', 'error', '-show_streams', '-show_format',
        '-of', 'json', str(path)], capture_output=True, text=True, check=True, timeout=10)
    payload = json.loads(result.stdout)
    video = next((row for row in payload.get('streams', []) if row.get('codec_type') == 'video'), None)
    if not video:
        raise ValueError('source has no readable video stream')
    rate = Fraction(video.get('avg_frame_rate') or video.get('r_frame_rate') or '0')
    duration = Fraction(video.get('duration') or payload.get('format', {}).get('duration') or '0')
    if rate <= 0 or duration <= 0:
        raise ValueError('source timing unavailable')
    return {'source_frame_rate': str(rate), 'source_duration_seconds': str(duration),
        'source_start_seconds': str(_timecode_start(video.get('tags', {}).get('timecode', ''), rate)),
        'source_width': int(video['width']), 'source_height': int(video['height']),
        'has_audio': any(row.get('codec_type') == 'audio' for row in payload.get('streams', []))}


def build_fcpxml(manifest, timeline_name='Editorial rough cut', frame_rate='30000/1001', *, probe=None, ffprobe=None):
    if manifest.get('contract') != 'editorial_manifest_v2':
        raise ValueError('Unsupported editorial manifest version')
    if any(row['candidate'] and (not row['candidate']['resolved'] or not row['candidate']['online'])
           for row in manifest['timeline_items']):
        raise ValueError('Selected source is offline/stale; FCPXML relink export unavailable')
    rate = normalize_frame_rate(frame_rate)
    items=[]; timings={}
    for order, item in enumerate(manifest['timeline_items'], 1):
        candidate=item['candidate']
        base={'beat_order': order, 'beat_text': item['beat_text'], 'duration_ms': item['duration_ms']}
        if candidate is None:
            items.append({**base, 'item_kind': 'script_gap'}); continue
        path=Path(candidate['resolved_path'])
        if not path.is_absolute() or not path.is_file():
            raise ValueError('current source unavailable during export')
        source_key=(candidate['source_id'], candidate['source_revision'])
        if source_key not in timings:
            if candidate['source_type'] == 'IMAGE':
                meta=candidate.get('source_metadata', {})
                timings[source_key]={'source_frame_rate': str(rate),
                    'source_duration_seconds': str(_frame_aligned_time(candidate['end_ms'],rate,minimum_one_frame=True)),
                    'source_start_seconds':'0', 'source_width':meta.get('width',1920),
                    'source_height':meta.get('height',1080), 'has_audio':False}
            else:
                timings[source_key]=probe(path) if probe else probe_timing(path,ffprobe)
        timing=timings[source_key]
        if candidate['source_type'] == 'IMAGE':
            timing['source_duration_seconds'] = str(max(Fraction(timing['source_duration_seconds']),
                _frame_aligned_time(candidate['end_ms'], rate, minimum_one_frame=True)))
        start=_frame_aligned_time(candidate['start_ms'],Fraction(timing['source_frame_rate']))
        duration=_frame_aligned_time(candidate['end_ms']-candidate['start_ms'],rate,minimum_one_frame=True)
        if start+duration > Fraction(timing['source_duration_seconds']):
            raise ValueError('frame-aligned cut exceeds source duration')
        items.append({**base, **timing, 'item_kind':'clip',
            'candidate_id':candidate['candidate_id'], 'source_content_id':'::'.join(source_key),
            'source_file':path.name, 'source_absolute_path':str(path),
            'start_ms':candidate['start_ms'], 'end_ms':candidate['end_ms'],
            'editorial_reason':'; '.join(str(row.get('text','')) for row in candidate['match_reasons']),
            'time_basis':candidate['time_basis']})
    return _serialize_fcpxml(items,timeline_name=timeline_name,frame_rate=rate).decode('utf-8')
