from __future__ import annotations

from typing import Any


CONTRACT_VERSION = "p0d_keyframe_evidence_v1"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _text(candidate: dict[str, Any]) -> str:
    return " ".join([
        str(candidate.get("display_title") or ""),
        " ".join(str(value) for value in candidate.get("observations") or []),
    ])


def _explicit(candidate: dict[str, Any], field: str) -> list[str]:
    row = ((candidate.get("editorial_language") or {}).get(field) or {})
    return [str(value) for value in row.get("values") or []]


def _field(explicit: list[str], inferred: list[str], *, unknown_reason: str) -> dict[str, Any]:
    if explicit:
        return {"status": "observed_text_label", "values": _unique(explicit), "confidence": 0.9, "reason": "已有明确结构化文字证据"}
    if inferred:
        return {"status": "qwenvl_description_inference", "values": _unique(inferred), "confidence": 0.55, "reason": "由现有Qwen画面描述保守推测，不能替代专业标注"}
    return {"status": "unknown", "values": [], "confidence": 0.0, "reason": unknown_reason}


def _shot_scale(text: str) -> list[str]:
    if any(term in text for term in ("特写", "脸部", "面部特写", "手部特写", "局部细节")):
        return ["特写/近景候选"]
    if any(term in text for term in ("近处人物", "近距离", "人物占据画面大部分")):
        return ["近景/中近景候选"]
    if any(term in text for term in ("广阔", "远处建筑", "远处城市", "大面积田地", "整体环境", "航拍", "鸟瞰")):
        return ["远景/全景候选"]
    if "背景" in text and any(term in text for term in ("两位", "三位", "几位", "多人", "人物")):
        return ["中全景/全景候选"]
    return []


def _composition(text: str) -> list[str]:
    values: list[str] = []
    if "前景" in text and "背景" in text:
        values.append("前景—主体—背景层次候选")
    if any(term in text for term in ("远处", "纵深", "延伸", "河岸", "道路")) and "背景" in text:
        values.append("空间纵深候选")
    if any(term in text for term in ("居中", "中央", "画面中心")):
        values.append("居中构图候选")
    if "对称" in text:
        values.append("对称构图候选")
    if any(term in text for term in ("前景遮挡", "遮挡")):
        values.append("前景遮挡候选")
    return values


def _angle(text: str) -> list[str]:
    if any(term in text for term in ("鸟瞰", "正俯视")):
        return ["鸟瞰/极高机位候选"]
    if any(term in text for term in ("航拍", "俯拍", "高机位", "从上方")):
        return ["俯拍/高机位候选"]
    if any(term in text for term in ("低角度", "低机位", "仰拍", "从下方")):
        return ["仰拍/低机位候选"]
    if any(term in text for term in ("平视", "视线高度")):
        return ["平视候选"]
    return []


def _yolo_labels(candidate: dict[str, Any]) -> list[str]:
    labels = [str(value) for value in candidate.get("tags") or []]
    for observation in candidate.get("observations") or []:
        value = str(observation)
        if value.startswith("传播标签："):
            labels.extend(part.strip() for part in value.removeprefix("传播标签：").split("、"))
    return _unique(labels)


def _roles(text: str, scale: dict[str, Any]) -> list[str]:
    roles: list[str] = []
    values = set(scale["values"])
    if any("远景" in value or "全景" in value for value in values):
        roles.extend(["建立", "环境/呼吸"])
    if any(term in text for term in ("麦田", "农田", "田地", "田野", "街道", "村庄", "城市", "建筑", "树林", "天空", "河流", "海面", "现场")):
        roles.extend(["建立", "环境/呼吸"])
    if any("近景" in value or "特写" in value for value in values):
        roles.extend(["插入/细节", "反应候选"])
    if any(term in text for term in ("收割", "劳作", "行走", "操作", "驾驶", "工作", "挥镰")):
        roles.extend(["动作覆盖", "过程证据"])
    if any(term in text for term in ("表情", "微笑", "凝视", "低头", "回望")):
        roles.append("人物反应候选")
    if not roles:
        roles.append("待人工指定")
    return _unique(roles)


def analyze_keyframe(candidate: dict[str, Any]) -> dict[str, Any]:
    text = _text(candidate)
    scale = _field(_explicit(candidate, "shot_scale"), _shot_scale(text), unknown_reason="代表帧和现有描述不足以可靠判断景别")
    composition = _field(_explicit(candidate, "composition"), _composition(text), unknown_reason="缺少主体位置、画面边界或构图术语证据")
    angle = _field(_explicit(candidate, "camera_angle"), _angle(text), unknown_reason="代表帧描述没有明确机位高度或俯仰关系")
    yolo = _yolo_labels(candidate)
    qwen_observations = [str(value) for value in candidate.get("observations") or [] if not str(value).startswith("传播标签：")]
    media_type = str(candidate.get("media_type") or "video")
    roles = _roles(text, scale)
    if media_type == "image":
        roll_role = "b_roll_still_candidate"
        roll_reason = "静态图片可作说明、档案或节奏停顿，不能作为人物同期主叙述A-roll"
    else:
        roll_role = "b_roll_candidate"
        roll_reason = "当前只有画面代表帧和视觉描述，没有已验证的人物同期讲话、采访身份或连续主动作证据"
    return {
        "contract_version": CONTRACT_VERSION,
        "evidence_sources": {"preview_keyframe": bool(candidate.get("preview_url") or candidate.get("preview_uri") or candidate.get("preview_absolute_path")), "qwenvl_description": bool(qwen_observations), "yolo_labels": bool(yolo)},
        "visible_facts": qwen_observations,
        "yolo_labels": yolo,
        "shot_scale": scale,
        "composition": composition,
        "camera_angle": angle,
        "camera_motion": {"status": "unknown_from_single_frame", "values": [], "reason": "单帧不能区分固定、推拉、摇移、跟拍、变焦或主体自身运动；必须读取相邻帧或原片"},
        "color_tone": {"status": "not_used_for_fit", "values": [], "reason": "素材可能处于LOG或未完成色彩管理状态，本阶段不根据灰度、饱和度或色温判断情绪与适配度"},
        "possible_editorial_roles": roles,
        "roll_role": roll_role,
        "roll_role_reason": roll_reason,
        "neighbor_requirements": [
            "读取同一原素材当前采样点前后代表帧，确认主体、动作和景别是否持续",
            "播放原片确认运镜、稳定性、动作完整性和真实镜头边界",
            "检查同期声、对白、环境声和可否使用J-cut/L-cut或声音桥",
        ],
        "human_decision_required": True,
    }


def build_neighbor_index(sentences: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_source: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
    for sentence in sentences:
        for candidate in sentence.get("candidates") or []:
            source_id = str(candidate.get("source_content_id") or "")
            anchor = candidate.get("anchor_time_ms")
            if not source_id or anchor is None:
                continue
            key = (str(candidate.get("candidate_id") or ""), int(anchor))
            by_source.setdefault(source_id, {})[key] = candidate
    return {
        source_id: sorted(rows.values(), key=lambda row: int(row.get("anchor_time_ms") or 0))
        for source_id, rows in by_source.items()
    }


def find_neighbor_context(
    candidate: dict[str, Any],
    index: dict[str, list[dict[str, Any]]],
    *,
    max_delta_ms: int = 15000,
) -> dict[str, Any]:
    source_id = str(candidate.get("source_content_id") or "")
    anchor = candidate.get("anchor_time_ms")
    if not source_id or anchor is None:
        return {"status": "unavailable", "previous": None, "next": None, "reason": "候选缺少原素材标识或采样时间"}
    current_id = str(candidate.get("candidate_id") or "")
    rows = [row for row in index.get(source_id, []) if str(row.get("candidate_id") or "") != current_id]
    before = [row for row in rows if int(row.get("anchor_time_ms") or 0) < int(anchor)]
    after = [row for row in rows if int(row.get("anchor_time_ms") or 0) > int(anchor)]

    def compact(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        delta = int(row.get("anchor_time_ms") or 0) - int(anchor)
        if abs(delta) > max_delta_ms:
            return None
        return {
            "candidate_id": row.get("candidate_id"),
            "anchor_time_ms": row.get("anchor_time_ms"),
            "delta_ms": delta,
            "display_title": row.get("display_title"),
            "observations": row.get("observations") or [],
            "preview_uri": row.get("preview_url") or row.get("preview_uri"),
            "yolo_labels": _yolo_labels(row),
        }

    previous = compact(before[-1] if before else None)
    following = compact(after[0] if after else None)
    if previous or following:
        return {
            "status": "nearest_analyzed_keyframes_only",
            "previous": previous,
            "next": following,
            "reason": "这是现有复核结果中同一原素材、±15秒内最近的已分析采样点；不是逐帧连续分析，不能据此认定运镜。",
        }
    nearest_deltas = sorted(abs(int(row.get("anchor_time_ms") or 0) - int(anchor)) for row in rows)
    suffix = f"；最近的其他已分析点相距{nearest_deltas[0] / 1000:.1f}秒" if nearest_deltas else ""
    return {
        "status": "no_near_analyzed_keyframe",
        "previous": None,
        "next": None,
        "reason": "现有复核结果中没有同一原素材±15秒内的已分析采样点" + suffix + "，必须读取原片或补齐邻帧分析。",
    }
