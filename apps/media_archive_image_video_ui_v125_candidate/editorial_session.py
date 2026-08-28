"""Import legacy editing manifests as resumable, independent projects.

No reranking, scanning, inference, media access or source-database writes.
"""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .editorial_assistant import _compact_candidate, _engine
from .editorial_candidate.editorial_labels import LABELS


def manifest_session(manifest: dict, project: dict, database: Path) -> dict:
    if manifest.get("contract_version") != "editorial_manifest_v125_v1":
        raise ValueError("不是受支持的1.2.5剪辑清单；原文件未改动")
    source_beats = manifest.get("beats") or []
    if not source_beats or len(source_beats) > 2000:
        raise ValueError("剪辑清单没有有效句子，或超过2000句")
    evidence = {r["candidate_id"]: r for r in project.get("candidates", [])}
    items = {r["script_id"]: r for r in manifest.get("items", [])}
    decisions, overrides, locked, beats, missing = {}, {}, [], [], []
    allowed = {"selected", "review", "rejected"}
    legacy_choices = {(r["beat_id"], r["candidate_id"]): r["decision"] for r in manifest.get("decisions", [])}
    for old in source_beats:
        bid = old["beat_id"]
        item = items.get(bid, {})
        rows = ([item] if item.get("candidate_id") else []) + item.get("alternatives", [])
        candidates, a_roll, seen = [], None, set()
        for saved in rows:
            cid = saved.get("candidate_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            raw = copy.deepcopy(evidence.get(cid) or {"candidate_id": cid})
            is_keep = cid.startswith("keep-a-roll::")
            if cid not in evidence and not is_keep:
                missing.append(cid)
                raw.update(source_content_id=saved.get("source_content_id") or "", source_file=saved.get("source_path") or "",
                           display_title="旧版已存镜头：当前素材库未找到，保留选择待核对", media_type="video")
            origin = saved.get('manual_origin') or ('search_manual' if cid.startswith('manual-visual::') else '')
            candidate = _compact_candidate(raw, origin if origin in {'search_manual','favorite_manual'} else "restored")
            start = saved.get("source_in") if saved.get("source_in") is not None else saved.get("start_ms", 0)
            end = saved.get("source_out") if saved.get("source_out") is not None else saved.get("end_ms", saved.get("duration", 3000))
            if not isinstance(start, (float, int)) or not isinstance(end, (float, int)) or not (0 <= start < end < 86_400_000_000):
                raise ValueError(f"旧清单剪点无效：{bid}/{cid}；未覆盖现有工程")
            candidate.update(provisional_in_ms=int(start), provisional_out_ms=int(end),
                             role=saved.get("role") or saved.get("editorial_function") or "待核对",
                             editorial_function=saved.get("editorial_function"),
                             recommendation_reason=saved.get("recommendation_reason") or "旧版人工选择，未重新评分",
                             cinematic_scores=saved.get("scores") or {}, gate_status=saved.get("gate_status") or "SOFT_GATE",
                             gate_reason_codes=saved.get("gate_reason_codes") or [], is_placeholder=is_keep,
                             requires_source_review=True)
            if is_keep:
                candidate.update(media_type="a_roll_placeholder", display_title="保留人物口播", source_file="", source_content_id="")
                a_roll = candidate
            else:
                candidates.append(candidate)
            key = bid + "::" + cid
            choice = legacy_choices.get((bid, cid)) or saved.get("user_choice")
            if choice in allowed:
                decisions[key] = choice
            overrides[key] = [start / 1000, end / 1000]
            if saved.get("cut_locked"):
                locked.append(key)
        if a_roll is None:
            a_roll = _compact_candidate({"candidate_id": "keep-a-roll::" + bid, "display_title": "保留人物口播", "media_type": "a_roll_placeholder", "end_ms": 3000}, "a_roll")
            a_roll.update(provisional_in_ms=0, provisional_out_ms=3000, is_placeholder=True)
        requirement = old.get("selection_requirement") or {}
        beats.append({**old, "purpose": "已恢复 · 继续人工选片", "required_roles": [], "retrieval_candidate_count": len(candidates),
                      "retrieval_source_count": len({r["source_content_id"] for r in candidates}),
                      "shot_brief": (old.get("visual_strategy") or {}).get("shot_brief", ""),
                      **{key:requirement.get(key) for key in ("visual_task", "expected_primary_subject", "preferred_shot_roles", "visualizability", "sound_instruction", "a_roll_preference")},
                      "candidates": candidates, "a_roll_option": a_roll, "reserve_candidates": []})
    all_keys = {b["beat_id"] + "::" + c["candidate_id"] for b in beats for c in b["candidates"] + [b["a_roll_option"]]}
    for (bid, cid), choice in legacy_choices.items():
        key = bid + "::" + cid
        if choice not in allowed or key not in all_keys:
            raise ValueError(f"无法完整恢复人工选择：{bid}/{cid}；请保留原文件")
        decisions[key] = choice
    ids = [b["beat_id"] for b in beats]
    if len(set(ids)) != len(ids):
        raise ValueError("句子编号重复，拒绝覆盖现有工程")
    for i, beat in enumerate(beats):
        beat["context_before"] = [b["text"] for b in beats[max(0, i-2):i]]
        beat["context_after"] = [b["text"] for b in beats[i+1:i+3]]
    chosen = [i for i,b in enumerate(beats) if any(k.startswith(b["beat_id"] + "::") for k in decisions)]
    next_index = min(max(chosen, default=-1) + 1, len(beats)-1)
    script = "\n".join(b["text"] for b in beats)
    return {"status":"PASS", "format_version":"editorial_session_v1", "session_id":str(uuid.uuid4()), "saved_at":datetime.now(timezone.utc).isoformat(),
            "board":{"status":"PASS", "track":manifest["track"], "database":str(database.resolve()), "database_read_only":True,
                     "database_write":False, "model_run":False, "candidate_count":len(evidence), "ui_labels":LABELS, "beats":beats},
            "script":script, "generated_script":script, "guide_files":[], "generated_guides":[], "selected_file":"",
            "source_label":"旧版剪辑清单中的原文（不是重新生成）", "active_beat":next_index,
            "decisions":decisions, "cut_overrides":overrides, "locked_cuts":locked, "skipped_visuals":{},
            "timeline_name":manifest.get("timeline_name") or "文稿候选粗剪", "frame_rate":manifest.get("frame_rate") or "30000/1001",
            "include_backups":bool(manifest.get("include_backups")),
            "migration_note":f"恢复{len(beats)}句和{len(decisions)}条选择；按最后有选择的句子推断续做第{next_index+1}句。旧清单不保存浏览位置。{len(set(missing))}个候选未在当前库找到，未删除。"}


def import_manifest_session(database: Path, path: Path) -> dict:
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("清单超过128MB，请检查是否选对文件")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    _, adapter, _ = _engine()
    project = adapter.load_database_project(database, limit=25_000)
    # A human search choice need not belong to the current algorithm's recall
    # pool. Rehydrate only explicitly manual IDs using database-owned paths.
    saved_rows = [row for item in manifest.get('items', []) for row in [item, *item.get('alternatives', [])]]
    manual_ids = list(dict.fromkeys(str(row.get('candidate_id') or '') for row in saved_rows
        if row.get('manual_origin') in {'search_manual','favorite_manual'}
        or str(row.get('candidate_id') or '').startswith('manual-visual::')))
    known = {row['candidate_id'] for row in project['candidates']}
    for offset in range(0,len(manual_ids),500):
        references = adapter.resolve_timeline_sources(database,manual_ids[offset:offset+500])
        for cid, source in references.items():
            if cid in known:
                continue
            if cid.startswith('manual-visual::'):
                from .editorial_favorites import editorial_search_candidate
                compact = editorial_search_candidate(database,database,source['source_content_id'],cid.removeprefix('manual-visual::'))['candidate']
                raw = dict(candidate_id=cid, source_content_id=source['source_content_id'], source_file=source['source_file'],
                    media_type=source['media_type'], preview_absolute_path=compact['preview_path'],
                    start_ms=compact['start_ms'],end_ms=compact['end_ms'],anchor_time_ms=compact['anchor_time_ms'],
                    display_title=compact['display_title'],observations=[compact['description']])
            else:
                raw = dict(candidate_id=cid,source_content_id=source['source_content_id'],source_file=source['source_file'],
                    media_type=source['media_type'],preview_absolute_path=str(adapter.resolve_preview_path(database,cid) or ''),
                    display_title='从剪辑清单恢复的人工补选',observations=['从剪辑清单恢复的人工补选；未重新分析'])
            project['candidates'].append(raw); known.add(cid)
    result = manifest_session(manifest, project, database)
    # A different library must not silently turn all saved selections into gaps.
    real = [k.split("::",1)[1] for k in result["decisions"] if not k.split("::",1)[1].startswith("keep-a-roll::")]
    available = {c["candidate_id"] for c in project["candidates"]}
    if real and not any(cid in available for cid in real):
        raise ValueError("当前素材库找不到任何已选镜头，请先连接原选片素材库；原清单未改动")
    return result
