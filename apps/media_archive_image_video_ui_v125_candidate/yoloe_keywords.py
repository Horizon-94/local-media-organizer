from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any


PROFILE_CONTRACT = "media_archive_yoloe_keyword_profile_v1"
MAX_KEYWORDS_PER_LAYER = 256
_LABEL_PATTERN = re.compile(r"^[^=\n\r\x00-\x1f]{1,120}$")


def _normalise_entry(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        label, separator, description = value.partition("=")
        label = label.strip()
        description = description.strip() if separator else ""
        entry = {"label": label, "zh": description or label, "category_zh": "用户自定义"}
    elif isinstance(value, dict):
        label = str(value.get("label") or "").strip()
        entry = {
            "label": label,
            "zh": str(value.get("zh") or label).strip(),
            "category_zh": str(value.get("category_zh") or "用户自定义").strip(),
        }
    else:
        raise ValueError("关键词必须是文字或关键词对象")
    if not _LABEL_PATTERN.fullmatch(entry["label"]):
        raise ValueError(f"无效的 YOLOE 关键词：{entry['label']!r}")
    return entry


def normalise_entries(values: Any, *, layer_name: str) -> list[dict[str, str]]:
    if isinstance(values, str):
        values = [line.strip() for line in values.splitlines() if line.strip()]
    if not isinstance(values, list):
        raise ValueError(f"{layer_name} 关键词格式无效")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        entry = _normalise_entry(value)
        identity = entry["label"].casefold()
        if identity in seen:
            continue
        seen.add(identity)
        entries.append(entry)
    if len(entries) > MAX_KEYWORDS_PER_LAYER:
        raise ValueError(f"{layer_name} 最多允许 {MAX_KEYWORDS_PER_LAYER} 个关键词")
    return entries


def profile_from_registry(registry: dict[str, Any]) -> dict[str, Any]:
    a_core = normalise_entries(registry.get("A_CORE_CLASSES") or [], layer_name="A 层")
    b_extended = normalise_entries(registry.get("B_EXTENDED_CLASSES") or [], layer_name="B 层")
    if not a_core:
        raise ValueError("A 层至少需要一个关键词")
    return {
        "contract": PROFILE_CONTRACT,
        "enable_b_extended": bool(
            (registry.get("layers") or {}).get("B_EXTENDED", {}).get("default_run", False)
        ),
        "a_core": a_core,
        "b_extended": b_extended,
    }


def normalise_profile(value: Any, *, fallback_registry: dict[str, Any]) -> dict[str, Any]:
    fallback = profile_from_registry(fallback_registry)
    if not isinstance(value, dict):
        return fallback
    a_core = normalise_entries(value.get("a_core", fallback["a_core"]), layer_name="A 层")
    b_extended = normalise_entries(
        value.get("b_extended", fallback["b_extended"]), layer_name="B 层"
    )
    if not a_core:
        raise ValueError("A 层至少需要一个关键词")
    return {
        "contract": PROFILE_CONTRACT,
        "enable_b_extended": bool(value.get("enable_b_extended", fallback["enable_b_extended"])),
        "a_core": a_core,
        "b_extended": b_extended,
    }


def load_registry(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def apply_profile(registry: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    effective = copy.deepcopy(registry)
    normalised = normalise_profile(profile, fallback_registry=registry)
    effective["A_CORE_CLASSES"] = normalised["a_core"]
    effective["B_EXTENDED_CLASSES"] = normalised["b_extended"]
    effective.setdefault("layers", {}).setdefault("B_EXTENDED", {})["default_run"] = normalised[
        "enable_b_extended"
    ]
    effective["effective_keyword_profile_contract"] = PROFILE_CONTRACT
    return effective


def materialise_registry(path: Path, base_registry_path: Path, profile: dict[str, Any]) -> Path:
    target = Path(path).expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    effective = apply_profile(load_registry(base_registry_path), profile)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(effective, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)
    return target


def entries_as_text(entries: Any) -> str:
    rows = normalise_entries(entries, layer_name="YOLOE")
    return "\n".join(
        entry["label"] if entry["zh"] == entry["label"] else f"{entry['label']} = {entry['zh']}"
        for entry in rows
    )
