from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any


KNOWLEDGE_ROOT = Path(__file__).with_name("knowledge")
KNOWLEDGE_TYPES = {
    "established_principle",
    "common_practice",
    "research_finding",
    "heuristic",
    "project_preference",
}
FILES = {
    "shot_features": "taxonomy/shot_features.json",
    "editorial_functions": "taxonomy/editorial_functions.json",
    "narrative_intents": "taxonomy/narrative_intents.json",
    "weights": "rules/weights.json",
    "editing_rules": "rules/editing_rules.json",
    "sequence_rules": "rules/sequence_rules.json",
    "directing_rules": "rules/directing_rules.json",
    "documentary_rules": "rules/documentary_rules.json",
    "audiovisual_rules": "rules/audiovisual_rules.json",
    "visible_target_concepts": "rules/visible_target_concepts.json",
    "sources": "sources/sources.json",
}


def _read(name: str, relative: str) -> dict[str, Any]:
    path = KNOWLEDGE_ROOT / relative
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cinematic knowledge file is invalid: {name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"cinematic knowledge file must be an object: {name}")
    contract = str(payload.get("contract_version") or "")
    knowledge_type = str(payload.get("knowledge_type") or "")
    if not contract.startswith("cinematic_"):
        raise ValueError(f"cinematic knowledge contract is invalid: {name}")
    if knowledge_type not in KNOWLEDGE_TYPES:
        raise ValueError(f"cinematic knowledge type is invalid: {name}")
    return payload


def _unique(values: list[object], label: str) -> list[str]:
    rows = [str(value) for value in values if str(value)]
    if len(rows) != len(set(rows)):
        raise ValueError(f"cinematic knowledge contains duplicate {label}")
    return rows


@lru_cache(maxsize=1)
def load_knowledge() -> dict[str, Any]:
    payload = {name: _read(name, relative) for name, relative in FILES.items()}
    functions = _unique(payload["editorial_functions"].get("functions") or [], "editorial function")
    intents = _unique(payload["narrative_intents"].get("intents") or [], "narrative intent")
    if "KEEP_A_ROLL" not in functions:
        raise ValueError("cinematic knowledge must include KEEP_A_ROLL")
    if len(functions) < 20 or len(intents) < 18:
        raise ValueError("cinematic taxonomy is incomplete")
    weights = payload["weights"].get("weights") or {}
    if not isinstance(weights, dict) or abs(sum(float(value) for value in weights.values()) - 1.0) > 0.0001:
        raise ValueError("cinematic rerank weights must sum to 1.0")
    for name in ("editing_rules", "sequence_rules", "directing_rules", "documentary_rules", "audiovisual_rules"):
        rules = payload[name].get("rules") or []
        if not isinstance(rules, list) or not rules:
            raise ValueError(f"cinematic rules are empty: {name}")
        _unique([row.get("id") for row in rules if isinstance(row, dict)], f"{name} id")
    fields = payload["shot_features"].get("fields") or {}
    if not isinstance(fields, dict) or "camera_motion" not in fields or "subject_motion" not in fields:
        raise ValueError("shot features must separate camera_motion and subject_motion")
    payload["summary"] = {
        "contract_version": "cinematic_selection_knowledge_summary_v1",
        "shot_feature_groups": len(fields),
        "shot_feature_values": sum(len(values) for values in fields.values()),
        "editorial_functions": len(functions),
        "narrative_intents": len(intents),
        "editing_rules": len(payload["editing_rules"]["rules"]),
        "sequence_rules": len(payload["sequence_rules"]["rules"]),
        "directing_rules": len(payload["directing_rules"]["rules"]),
        "documentary_rules": len(payload["documentary_rules"]["rules"]),
        "audiovisual_rules": len(payload["audiovisual_rules"]["rules"]),
        "visible_target_concepts": len(payload["visible_target_concepts"].get("concepts") or []),
        "sources": len(payload["sources"].get("sources") or []),
    }
    return payload


def knowledge_summary() -> dict[str, Any]:
    return dict(load_knowledge()["summary"])
