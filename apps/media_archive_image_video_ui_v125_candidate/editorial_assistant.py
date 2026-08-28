from __future__ import annotations

import importlib
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from .editorial_candidate.clip_timing import cut_range, timeline_duration_ms, validate_source_range
from .editorial_candidate.editorial_labels import LABELS


MAX_SCRIPT_BYTES = 1_000_000
_ENGINE_CACHE: tuple[Any, Any, Any] | None = None


def _engine() -> tuple[Any, Any, Any]:
    global _ENGINE_CACHE
    if _ENGINE_CACHE is not None:
        return _ENGINE_CACHE
    package = f"{__package__}.editorial_candidate" if __package__ else "editorial_candidate"
    core = importlib.import_module(f"{package}.core")
    adapter = importlib.import_module(f"{package}.db_adapter")
    timeline = importlib.import_module(f"{package}.timeline_export")
    _ENGINE_CACHE = (core, adapter, timeline)
    return _ENGINE_CACHE


def _read_script(path: Path) -> str:
    resolved = path.expanduser().resolve(strict=True)
    if resolved.stat().st_size > MAX_SCRIPT_BYTES:
        raise ValueError("文稿超过 1MB，请拆分后再生成候选")
    text = resolved.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("文稿内容为空")
    return text


def _compact_candidate(candidate: dict[str, Any], pool: str) -> dict[str, Any]:
    decision = candidate.get("editorial_decision") or {}
    plain = decision.get("plain_language_explanation") or {}
    keyframe = candidate.get("keyframe_analysis") or {}
    duration = decision.get("duration") or {}
    rerank = candidate.get("cinematic_rerank") or {}
    gate = candidate.get("selection_gate") or {}
    subject = candidate.get("candidate_subject_profile") or {}
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "source_content_id": str(candidate.get("source_content_id") or ""),
        "source_file": str(candidate.get("source_file") or ""),
        "media_type": str(candidate.get("media_type") or ""),
        "preview_path": str(candidate.get("preview_absolute_path") or ""),
        "display_title": str(candidate.get("display_title") or ""),
        "description": str((candidate.get("observations") or [""])[0]),
        "pool": pool,
        "role": str(candidate.get("role") or "待定"),
        "shortlist_rank": int(candidate.get("shortlist_rank") or 99),
        "recommendation": str(decision.get("recommendation") or ""),
        "evidence_mode": str(decision.get("evidence_mode") or ""),
        "anchor_time_ms": candidate.get("anchor_time_ms"),
        "start_ms": int(candidate.get("start_ms") or 0),
        "end_ms": int(candidate.get("end_ms") or 0),
        "time_basis": str(candidate.get("time_basis") or ""),
        "match_reasons": [str(value) for value in candidate.get("match_reasons") or []],
        "risks": [str(value) for value in candidate.get("risks") or []],
        "sentence_need": str(plain.get("sentence_need") or ""),
        "candidate_contribution": str(plain.get("candidate_contribution") or ""),
        "fit_reason": str(rerank.get("recommendation_reason") or plain.get("fit_reason") or ""),
        "visual_language": str(plain.get("visual_language") or ""),
        "fit_boundary": str(plain.get("fit_boundary") or ""),
        "acceptance_check": str(plain.get("acceptance_check") or ""),
        "editing_method": str(decision.get("editing_method") or ""),
        "provisional_in_ms": int(duration.get("provisional_in_ms") or candidate.get("start_ms") or 0),
        "provisional_out_ms": int(duration.get("provisional_out_ms") or candidate.get("end_ms") or 0),
        "duration_reason": str(duration.get("warning") or "文稿估时与当前候选窗口的交集；不是最终剪点"),
        "shot_scale": [str(value) for value in (keyframe.get("shot_scale") or {}).get("values") or []],
        "composition": [str(value) for value in (keyframe.get("composition") or {}).get("values") or []],
        "camera_angle": [str(value) for value in (keyframe.get("camera_angle") or {}).get("values") or []],
        "narrative_intent": str(rerank.get("narrative_intent") or ""),
        "editorial_function": str(rerank.get("editorial_function") or ""),
        "cinematic_scores": {
            str(key): float(value) for key, value in (rerank.get("scores") or {}).items()
        },
        "cinematic_penalties": {
            str(key): float(value) for key, value in (rerank.get("penalties") or {}).items()
        },
        "cinematic_final_score": float(rerank.get("final_score") or 0.0),
        "recommendation_reason": str(rerank.get("recommendation_reason") or ""),
        "visual_strategy": rerank.get("visual_strategy") or {},
        "actual_primary_subject": str(subject.get("actual_primary_subject") or gate.get("actual_primary_subject") or "UNKNOWN"),
        "secondary_subjects": [str(value) for value in subject.get("secondary_subjects") or []],
        "human_presence": bool(subject.get("human_presence")),
        "human_salience": str(subject.get("human_salience") or "UNKNOWN"),
        "candidate_shot_role": str(gate.get("candidate_shot_role") or rerank.get("editorial_function") or "UNKNOWN"),
        "gate_status": str(gate.get("gate_status") or "PASS"),
        "gate_penalty": float(gate.get("gate_penalty") or 0.0),
        "gate_reason_codes": [str(value) for value in gate.get("reason_codes") or []],
        "gate_reasons": [str(value) for value in gate.get("reasons") or []],
        "subject_match_score": float(gate.get("subject_match_score") or 0.0),
        "shot_role_match_score": float(gate.get("shot_role_match_score") or 0.0),
        "evidence_score": float(gate.get("evidence_score") or 0.0),
        "truthfulness_score": float(gate.get("truthfulness_score") or 0.0),
        "requires_source_review": bool(gate.get("requires_source_review")),
        "rank_reason": str(gate.get("rank_reason") or ""),
        "guide_source_tier": int((candidate.get("guide_source_match") or {}).get("tier", 4)),
        "guide_source_label": str((candidate.get("guide_source_match") or {}).get("label") or ""),
        "is_placeholder": str(candidate.get("media_type") or "").endswith("placeholder"),
    }


def build_editorial_board(
    database: Path,
    script_path: Path,
    track: str,
    guide_path: Path | list[Path] | None = None,
    *,
    target_beat_id: str | None = None,
    reserved_visuals: list[dict[str, Any]] | None = None,
    bound_guidance: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if track not in {"documentary", "short_video"}:
        raise ValueError("项目类型必须是纪录片或网感视频")
    core, adapter, _ = _engine()
    package = f"{__package__}.editorial_candidate" if __package__ else "editorial_candidate"
    knowledge = importlib.import_module(f"{package}.cinematic_knowledge")
    guide_module = importlib.import_module(f"{package}.editorial_guide")
    project = adapter.load_database_project(database, limit=25_000)
    script = _read_script(script_path)
    guide = guide_module.load_editorial_guides(guide_path) if guide_path else None
    board = core.build_board(
        script,
        project,
        track,
        prepared_corpus=core.prepare_corpus(project),
        editorial_guide=guide,
        target_beat_id=target_beat_id,
        reserved_visuals=reserved_visuals,
        comparison_mode=True,
        bound_guidance=bound_guidance,
    )
    beats = []
    for beat in board["beats"]:
        candidates = []
        for pool, rows in (beat.get("candidate_pools") or {}).items():
            candidates.extend(_compact_candidate(row, str(pool)) for row in rows)
        candidates.sort(key=lambda row: (int(row["shortlist_rank"]), row["candidate_id"]))
        reserve_candidates = []
        for pool, rows in (beat.get("candidate_reserve_pools") or {}).items():
            reserve_candidates.extend(_compact_candidate(row, str(pool)) for row in rows)
        reserve_candidates.sort(key=lambda row: (int(row["shortlist_rank"]), row["candidate_id"]))
        strategy = beat.get("visual_strategy") or {}
        requirement = beat.get("selection_requirement") or {}
        a_roll_option = beat.get("a_roll_option")
        beats.append({
            "beat_id": str(beat["beat_id"]),
            "order": int(beat["order"]),
            "text": str(beat["text"]),
            "purpose": str(beat["purpose"]),
            "estimated_narration_ms": int((beat.get("p0d_intent", {}).get("duration_plan", {}).get("estimated_narration_seconds") or 0) * 1000),
            "required_roles": [str(value) for value in beat.get("required_roles") or []],
            "context_before": [str(value) for value in beat.get("context_before") or []],
            "context_after": [str(value) for value in beat.get("context_after") or []],
            "sequence_position": beat.get("sequence_position") or {},
            "project_editorial_guidance": requirement.get("project_editorial_guidance"),
            "guide_search_message": str((beat.get("guide_source_search") or {}).get("message") or "未加载逐句表"),
            "guide_match_status": "MATCHED" if requirement.get("project_editorial_guidance") else ("UNMATCHED" if guide else "NOT_LOADED"),
            "guide_source_search": beat.get("guide_source_search") or {},
            "retrieval_candidate_count": int(beat.get("retrieval_candidate_count") or 0),
            "retrieval_source_count": int(beat.get("retrieval_source_count") or 0),
            "excluded_visual_count": int(beat.get("excluded_visual_count") or 0),
            "narrative_intent": str(strategy.get("narrative_intent") or ""),
            "visual_strategy": strategy,
            "shot_brief": str(strategy.get("shot_brief") or ""),
            "selection_requirement": requirement,
            "visual_task": str(requirement.get("visual_task") or "UNKNOWN"),
            "expected_primary_subject": str(requirement.get("expected_primary_subject") or "UNKNOWN"),
            "expected_subject_terms": [str(value) for value in requirement.get("expected_subject_terms") or []],
            "visual_target_labels": [str(value) for value in requirement.get("visual_target_labels") or []],
            "preferred_shot_roles": [str(value) for value in requirement.get("preferred_shot_roles") or []],
            "visualizability": str(requirement.get("visualizability") or "NEEDS_HUMAN_REVIEW"),
            "sound_instruction": bool(requirement.get("sound_instruction")),
            "a_roll_preference": str(requirement.get("a_roll_preference") or "LOW"),
            "fallback_plan": requirement.get("fallback_plan") or {},
            "a_roll_option": _compact_candidate(a_roll_option, "a_roll") if a_roll_option else None,
            "gap_status": beat.get("gap_status") or {},
            "gate_diagnostics": [
                {
                    "candidate_id": str(row.get("candidate_id") or ""),
                    "source_file": str(row.get("source_file") or ""),
                    "gate_status": str((row.get("selection_gate") or {}).get("gate_status") or "HARD_GATE"),
                    "reason_codes": [str(value) for value in (row.get("selection_gate") or {}).get("reason_codes") or []],
                    "reasons": [str(value) for value in (row.get("selection_gate") or {}).get("reasons") or []],
                }
                for row in beat.get("gate_diagnostics") or []
            ],
            "channel_diagnostics": beat.get("channel_diagnostics") or [],
            "candidates": candidates,
            "reserve_candidates": reserve_candidates,
            "candidate_groups": {
                channel: [_compact_candidate(row, str(row.get("pool") or "supplement")) for row in rows]
                for channel, rows in (beat.get("candidate_groups") or {}).items()
            },
        })
    return {
        "status": "PASS",
        "contract_version": "editorial_board_native_v125_development",
        "track": track,
        "ui_labels": LABELS,
        "database": str(database),
        "database_read_only": True,
        "database_write": False,
        "model_run": False,
        "candidate_count": int(board.get("candidate_count") or 0),
        "ignored_chapter_cards": [
            str(value) for value in board.get("ignored_chapter_cards") or []
        ],
        "editorial_guide_summary": board.get("editorial_guide_summary"),
        "knowledge_summary": knowledge.knowledge_summary(),
        "beats": beats,
    }


def export_editorial_timeline(database: Path, request_path: Path, output_path: Path) -> dict[str, Any]:
    core, adapter, timeline = _engine()
    payload = json.loads(request_path.expanduser().read_text(encoding="utf-8"))
    beats = [row for row in payload.get("beats") or [] if isinstance(row, dict)]
    request_candidates = {
        str(candidate.get("candidate_id") or ""): candidate
        for beat in beats for candidate in beat.get("candidates") or []
        if isinstance(candidate, dict)
    }
    decisions = [row for row in payload.get("decisions") or [] if isinstance(row, dict)]
    selected = [row for row in decisions if row.get("decision") == "selected"]
    backups = [row for row in decisions if row.get("decision") == "review" and not str(row.get("candidate_id") or "").startswith("keep-a-roll::")] if payload.get("include_backups", False) else []
    project = adapter.load_database_project(database, limit=25_000)
    candidate_map = {str(row["candidate_id"]): row for row in project["candidates"]}
    source_map = adapter.resolve_timeline_sources(database, [str(row.get("candidate_id") or "") for row in selected + backups])
    selected_by_beat: dict[str, list[dict[str, Any]]] = {}
    for row in selected:
        selected_by_beat.setdefault(str(row.get("beat_id") or ""), []).append(row)
    backups_by_beat: dict[str, list[dict[str, Any]]] = {}
    for row in backups:
        backups_by_beat.setdefault(str(row.get("beat_id") or ""), []).append(row)
    items: list[dict[str, Any]] = []
    timing_cache: dict[str, dict[str, Any]] = {}
    track = str(payload.get("track") or "documentary")
    for beat in sorted(beats, key=lambda row: int(row.get("order") or 0)):
        beat_id = str(beat.get("beat_id") or "")
        choices = selected_by_beat.get(beat_id, [])
        if not choices:
            text = str(beat.get("text") or "")
            items.append({
                "item_kind": "script_gap", "beat_order": int(beat.get("order") or 0),
                "beat_text": text, "duration_ms": timeline.estimate_script_duration_ms(text, track),
            })
        for choice_order, choice in enumerate(choices + backups_by_beat.get(beat_id, [])):
            candidate_id = str(choice.get("candidate_id") or "")
            if candidate_id.startswith("keep-a-roll::"):
                request_candidate = request_candidates.get(candidate_id) or {}
                duration_ms = max(
                    500,
                    int(request_candidate.get("provisional_out_ms") or 0)
                    - int(request_candidate.get("provisional_in_ms") or 0),
                )
                items.append({
                    "item_kind": "a_roll_gap",
                    "beat_order": int(beat.get("order") or 0),
                    "beat_text": str(beat.get("text") or ""),
                    "duration_ms": duration_ms,
                    "role": "KEEP_A_ROLL",
                    "choice_order": choice_order,
                })
                continue
            candidate = candidate_map.get(candidate_id)
            source = source_map.get(candidate_id)
            request_candidate = request_candidates.get(candidate_id) or {}
            # A manually chosen favorite may belong to an older reused indexing
            # run and therefore not occur in the current algorithm's recall pool.
            # Resolve its ID/path from SQLite, never trust a path from the request.
            if candidate is None and source is not None and request_candidate.get("manual_origin") in {"favorite_manual", "search_manual"}:
                if request_candidate.get("source_content_id") != source["source_content_id"]:
                    raise ValueError("人工补选候选的原文件编号不一致，已停止导出")
                candidate = {**request_candidate, "time_basis": "sample_anchor_window"}
            if candidate is None or source is None:
                raise ValueError(f"无法解析时间线候选：{candidate_id}")
            source_path = str(source["source_absolute_path"])
            if source_path not in timing_cache:
                timing_cache[source_path] = timeline.probe_source_timing(source_path)
            start_ms, end_ms = cut_range(request_candidates.get(candidate_id) or {}, candidate)
            validate_source_range(start_ms, end_ms, timing_cache[source_path], payload.get("frame_rate") or "30000/1001")
            items.append({
                **source, **timing_cache[source_path], "item_kind": "backup_clip" if choice.get("decision") == "review" else "selected_clip",
                "candidate_id": candidate_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "time_basis": str(candidate.get("time_basis") or "sample_anchor_window"),
                "shot_in_ms": candidate.get("shot_in_ms"), "shot_out_ms": candidate.get("shot_out_ms"),
                "beat_order": int(beat.get("order") or 0), "beat_text": str(beat.get("text") or ""),
                "role": str(choice.get("role") or candidate.get("role") or "待定"),
                "choice_order": choice_order,
            })
    if not items:
        raise ValueError("没有可导出的文稿段落")
    xml = timeline.build_fcpxml(
        items,
        timeline_name=str(payload.get("timeline_name") or "文稿候选粗剪"),
        frame_rate=payload.get("frame_rate") or "30000/1001",
        resolve_compatible=True,
    )
    resolved_output = output_path.expanduser().resolve()
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_bytes(xml)
    return {
        "status": "PASS", "path": str(resolved_output), "selected_clip_count": len([row for row in items if row.get("item_kind") == "selected_clip"]),
        "placeholder_only": not bool(timing_cache),
        "verified_source_count": len(timing_cache),
        "contains_media": False,
        "gap_count": len([row for row in items if row.get("item_kind") == "script_gap"]),
        "a_roll_placeholder_count": len([row for row in items if row.get("item_kind") == "a_roll_gap"]),
        "backup_clip_count": len(backups),
        "backup_enabled": False,
        "script_reference_count": len([row for row in items if row.get("item_kind") != "backup_clip"]),
        "script_reference_enabled": False,
        "black_placeholders_are_native_generators": True,
        "database_write": False, "model_run": False,
    }


def export_editorial_manifest(database: Path, request_path: Path, output_path: Path) -> dict[str, Any]:
    _, adapter, timeline = _engine()
    payload = json.loads(request_path.expanduser().read_text(encoding="utf-8"))
    beats = sorted(
        [row for row in payload.get("beats") or [] if isinstance(row, dict)],
        key=lambda row: int(row.get("order") or 0),
    )
    decisions = [row for row in payload.get("decisions") or [] if isinstance(row, dict)]
    decision_map = {
        (str(row.get("beat_id") or ""), str(row.get("candidate_id") or "")): str(row.get("decision") or "")
        for row in decisions
    }
    candidate_ids = [
        str(candidate.get("candidate_id") or "")
        for beat in beats for candidate in beat.get("candidates") or []
        if isinstance(candidate, dict) and not str(candidate.get("candidate_id") or "").startswith("keep-a-roll::")
    ]
    source_map = {}
    unique_ids = list(dict.fromkeys(candidate_ids))
    for offset in range(0, len(unique_ids), 500):
        source_map.update(adapter.resolve_timeline_sources(database, unique_ids[offset:offset + 500]))
    track = str(payload.get("track") or "documentary")
    cursor_ms = 0
    items: list[dict[str, Any]] = []
    for beat in beats:
        beat_id = str(beat.get("beat_id") or "")
        text = str(beat.get("text") or "")
        candidates = [row for row in beat.get("candidates") or [] if isinstance(row, dict)]
        selected = next(
            (row for row in candidates if decision_map.get((beat_id, str(row.get("candidate_id") or ""))) == "selected"),
            None,
        )
        if selected is None:
            duration_ms = timeline_duration_ms(0, timeline.estimate_script_duration_ms(text, track), payload.get("frame_rate") or "30000/1001")
            item = {
                "beat_order": int(beat.get("order") or 0),
                "candidate_id": None,
                "source_content_id": None,
                "script_id": beat_id,
                "script_text": text,
                "narrative_intent": str(beat.get("narrative_intent") or ""),
                "visual_strategy": beat.get("visual_strategy") or {},
                "selection_requirement": beat.get("selection_requirement") or {},
                "selected_source": None,
                "source_path": None,
                "source_in": None,
                "source_out": None,
                "timeline_in": cursor_ms,
                "timeline_out": cursor_ms + duration_ms,
                "duration": duration_ms,
                "editorial_function": "BLACK_GAP",
                "recommendation_reason": "当前句尚未人工选镜；时间线保留带文稿标记的黑屏缺口，可在精剪时补镜、保留黑场或改为声音段。",
                "scores": {},
                "gate_status": "GAP",
                "gate_penalty": 0.0,
                "user_choice": "unselected_gap",
                "intentional_placeholder": False,
                "alternatives": [_manifest_alternative(row, beat_id, decision_map, source_map) for row in candidates],
            }
        else:
            candidate_id = str(selected.get("candidate_id") or "")
            start_ms, end_ms = cut_range(selected)
            duration_ms = timeline_duration_ms(start_ms, end_ms, payload.get("frame_rate") or "30000/1001")
            source = source_map.get(candidate_id) or {}
            is_keep = candidate_id.startswith("keep-a-roll::")
            item = {
                "beat_order": int(beat.get("order") or 0),
                "candidate_id": candidate_id,
                "source_content_id": None if is_keep else str(selected.get("source_content_id") or source.get("source_content_id") or ""),
                "script_id": beat_id,
                "script_text": text,
                "narrative_intent": str(selected.get("narrative_intent") or beat.get("narrative_intent") or ""),
                "visual_strategy": selected.get("visual_strategy") or beat.get("visual_strategy") or {},
                "selection_requirement": beat.get("selection_requirement") or {},
                "selected_source": "KEEP_A_ROLL" if is_keep else candidate_id,
                "manual_origin": str(selected.get("manual_origin") or ""),
                "source_path": None if is_keep else source.get("source_absolute_path"),
                "source_in": None if is_keep else start_ms,
                "source_out": None if is_keep else end_ms,
                "timeline_in": cursor_ms,
                "timeline_out": cursor_ms + duration_ms,
                "duration": duration_ms,
                "editorial_function": str(selected.get("editorial_function") or selected.get("role") or ""),
                "recommendation_reason": str(selected.get("recommendation_reason") or selected.get("fit_reason") or ""),
                "scores": selected.get("cinematic_scores") or {},
                "gate_status": str(selected.get("gate_status") or "PASS"),
                "gate_penalty": float(selected.get("gate_penalty") or 0.0),
                "gate_reason_codes": selected.get("gate_reason_codes") or [],
                "actual_primary_subject": str(selected.get("actual_primary_subject") or "UNKNOWN"),
                "candidate_shot_role": str(selected.get("candidate_shot_role") or "UNKNOWN"),
                "truthfulness_score": float(selected.get("truthfulness_score") or 0.0),
                "requires_source_review": bool(selected.get("requires_source_review")),
                "user_choice": "selected",
                "cut_locked": bool(selected.get("cut_locked")),
                "cut_origin": str(selected.get("cut_origin") or "suggested"),
                "intentional_placeholder": is_keep,
                "role": str(selected.get("role") or selected.get("editorial_function") or ""),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "editorial_language": {
                    "shot_scale": {"values": selected.get("shot_scale") or []},
                    "composition": {"values": selected.get("composition") or []},
                    "subject_orientation": {"values": []},
                },
                "alternatives": [
                    _manifest_alternative(row, beat_id, decision_map, source_map)
                    for row in candidates if str(row.get("candidate_id") or "") != candidate_id
                ],
            }
        backup_lane = 0
        for alternative in item["alternatives"]:
            if alternative["user_choice"] == "review":
                alternative["duration"] = timeline_duration_ms(alternative["source_in"], alternative["source_out"], payload.get("frame_rate") or "30000/1001")
                backup_lane += 1
                alternative.update({
                    "timeline_in": item["timeline_in"],
                    "timeline_out": item["timeline_in"] + min(item["duration"], alternative["duration"]),
                    "lane": backup_lane,
                    "enabled": False,
                    "included_in_xml": bool(payload.get("include_backups", False)),
                })
        item["project_editorial_guidance"] = beat.get("project_editorial_guidance") or {}
        items.append(item)
        cursor_ms = item["timeline_out"]
    script_text = "\n".join(str(row.get("text") or "") for row in beats)
    package = f"{__package__}.editorial_candidate" if __package__ else "editorial_candidate"
    sequence = importlib.import_module(f"{package}.sequence_evaluation")
    manifest = {
        "contract_version": "editorial_manifest_v125_v1",
        "script_id": hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
        "track": track,
        "timeline_name": str(payload.get("timeline_name") or "文稿候选粗剪"),
        "frame_rate": str(payload.get("frame_rate") or "30000/1001"),
        "database_read_only": True,
        "database_write": False,
        "model_run": False,
        "beats": [{key: value for key, value in row.items() if key != "candidates"} for row in beats],
        "decisions": decisions,
        "browsing_skips": payload.get("browsing_skips") or {},
        "include_backups": bool(payload.get("include_backups", False)),
        "items": items,
        "sequence_check": sequence.evaluate_sequence(items, profile=track),
    }
    resolved_output = output_path.expanduser().resolve()
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "PASS", "path": str(resolved_output), "item_count": len(items), "database_write": False, "model_run": False}


def _manifest_alternative(
    candidate: dict[str, Any],
    beat_id: str,
    decision_map: dict[tuple[str, str], str],
    source_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    candidate_id = str(candidate.get("candidate_id") or "")
    source = source_map.get(candidate_id) or {}
    start_ms = int(candidate.get("provisional_in_ms") if candidate.get("provisional_in_ms") is not None else candidate.get("start_ms") or 0)
    end_ms = int(candidate.get("provisional_out_ms") if candidate.get("provisional_out_ms") is not None else candidate.get("end_ms") or start_ms)
    return {
        "candidate_id": candidate_id,
        "manual_origin": str(candidate.get("manual_origin") or ""),
        "source_content_id": source.get("source_content_id"),
        "source_path": source.get("source_absolute_path"),
        "source_in": start_ms,
        "source_out": end_ms,
        "duration": max(0, end_ms - start_ms),
        "editorial_function": str(candidate.get("editorial_function") or candidate.get("role") or ""),
        "recommendation_reason": str(candidate.get("recommendation_reason") or candidate.get("fit_reason") or ""),
        "scores": candidate.get("cinematic_scores") or {},
        "gate_status": str(candidate.get("gate_status") or "PASS"),
        "gate_penalty": float(candidate.get("gate_penalty") or 0.0),
        "gate_reason_codes": candidate.get("gate_reason_codes") or [],
        "actual_primary_subject": str(candidate.get("actual_primary_subject") or "UNKNOWN"),
        "candidate_shot_role": str(candidate.get("candidate_shot_role") or "UNKNOWN"),
        "truthfulness_score": float(candidate.get("truthfulness_score") or 0.0),
        "requires_source_review": bool(candidate.get("requires_source_review")),
        "user_choice": decision_map.get((beat_id, candidate_id), "unreviewed"),
        "cut_locked": bool(candidate.get("cut_locked")),
        "cut_origin": str(candidate.get("cut_origin") or "suggested"),
    }
