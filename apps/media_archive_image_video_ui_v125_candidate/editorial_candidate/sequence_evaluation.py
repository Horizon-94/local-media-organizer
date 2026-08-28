from __future__ import annotations

from collections import Counter
from typing import Any


CONTRACT_VERSION = "p2d_sequence_evaluation_v1"


def evaluate_sequence(items: list[dict[str, Any]], *, profile: str) -> dict[str, Any]:
    if profile not in {"documentary", "short_video"}:
        raise ValueError(f"unsupported sequence profile: {profile}")
    ordered = sorted(items, key=lambda row: (int(row.get("beat_order") or 0), int(row.get("choice_order") or 0)))
    issues: list[dict[str, Any]] = []
    strengths: list[str] = []
    unknowns: list[str] = []
    gaps = [
        str(row.get("script_id") or row.get("beat_id") or "")
        for row in ordered if not row.get("candidate_id") and not row.get("intentional_placeholder")
    ]
    selected = [row for row in ordered if row.get("candidate_id")]

    def issue(code: str, message: str, severity: str = "review") -> None:
        issues.append({"code": code, "severity": severity, "message": message})

    if gaps:
        issue("script_gaps", f"有 {len(gaps)} 个文稿段落没有镜头", "blocking")
    candidate_counts = Counter(str(row.get("candidate_id")) for row in selected)
    repeated_candidates = [key for key, count in candidate_counts.items() if count > 1]
    if repeated_candidates:
        issue("repeated_candidate", f"同一候选被重复使用 {len(repeated_candidates)} 次，需要确认是否有意回环")
    repeated_source_pairs = 0
    for previous, current in zip(selected, selected[1:]):
        previous_source = str(previous.get("source_content_id") or "")
        current_source = str(current.get("source_content_id") or "")
        if previous_source and previous_source == current_source:
            repeated_source_pairs += 1
    if repeated_source_pairs:
        issue("consecutive_same_source", f"有 {repeated_source_pairs} 处连续镜头来自同一原素材，可能产生跳切或信息重复")

    durations = [max(0, int(row.get("end_ms") or 0) - int(row.get("start_ms") or 0)) for row in selected]
    if durations:
        average = sum(durations) / len(durations)
        if profile == "short_video" and average > 4500:
            issue("slow_average_duration", f"平均候选长度 {average / 1000:.1f} 秒，快节奏短视频可能需要更密的有效变化")
        elif profile == "documentary" and average < 1200:
            issue("fragmented_average_duration", f"平均候选长度 {average / 1000:.1f} 秒，可能削弱观察过程和真实时间感")
        else:
            strengths.append(f"平均候选长度 {average / 1000:.1f} 秒，未触发该项目轨的基础节奏警告")

    roles = Counter(str(row.get("role") or "未知") for row in selected)
    if profile == "documentary":
        if roles.get("证据", 0) + roles.get("动作覆盖", 0) == 0:
            issue("missing_process_or_evidence", "序列缺少证据或完整过程镜头，旁白可能只剩氛围图")
        if roles.get("建立", 0) == 0:
            issue("missing_establishing", "序列没有建立镜头，地点和人物空间关系可能不清楚")
        if roles.get("反应", 0) == 0:
            issue("missing_reaction", "序列没有反应镜头，人物体验层可能偏弱")
    else:
        if roles.get("钩子", 0) == 0:
            issue("missing_hook", "没有明确钩子镜头；第一段仍需人工检查是否有问题、冲突或结果前置")
        if len(set(roles)) >= 3:
            strengths.append("镜头用途有三类以上，具备基础变化空间")

    known_scales = 0
    known_directions = 0
    for row in selected:
        language = row.get("editorial_language") or {}
        if (language.get("shot_scale") or {}).get("values"):
            known_scales += 1
        if (language.get("subject_orientation") or {}).get("values"):
            known_directions += 1
    if selected and known_scales < max(2, len(selected) // 2):
        unknowns.append("多数镜头没有景别证据，无法可靠评价远—中—近的变化")
    if selected and known_directions < max(2, len(selected) // 2):
        unknowns.append("多数镜头没有视线或运动方向证据，无法自动判断轴线与方向连续性")
    unknowns.extend([
        "没有语义声音分类，不能自动设计 J Cut、L Cut 或声音桥",
        "没有动作事件标注，不能证明剪点落在自然动作点",
    ])

    return {
        "contract_version": CONTRACT_VERSION,
        "profile": profile,
        "item_count": len(selected),
        "gap_count": len(gaps),
        "role_distribution": dict(sorted(roles.items())),
        "strengths": strengths,
        "issues": issues,
        "unknowns": list(dict.fromkeys(unknowns)),
        "status": "unresolved_gaps" if gaps else ("soft_review" if issues else "no_rule_warning"),
        "rule_mode": "soft_bonus_penalty",
        "human_decision_required": True,
    }
