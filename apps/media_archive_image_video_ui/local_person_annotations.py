from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable


CONTRACT = "media_archive_local_person_annotations_v1"


def annotation_path(application_state: Path, database: Path) -> Path:
    key = hashlib.sha256(str(database.expanduser().resolve()).encode("utf-8")).hexdigest()[:24]
    return application_state / "people" / f"{key}.json"


def load_annotations(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"contract": CONTRACT, "identities": {}, "cluster_to_identity": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"contract": CONTRACT, "identities": {}, "cluster_to_identity": {}}
    if payload.get("contract") != CONTRACT:
        return {"contract": CONTRACT, "identities": {}, "cluster_to_identity": {}}
    payload.setdefault("identities", {})
    payload.setdefault("cluster_to_identity", {})
    return payload


def save_annotations(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _identity_id(cluster_id: str) -> str:
    return "local_person_" + hashlib.sha256(cluster_id.encode("utf-8")).hexdigest()[:20]


def resolve_clusters(payload: dict[str, Any], identifier: str) -> list[str]:
    identities = payload.get("identities") or {}
    if identifier in identities:
        return sorted({str(value) for value in identities[identifier].get("cluster_ids") or []})
    mapped = str((payload.get("cluster_to_identity") or {}).get(identifier) or "")
    if mapped and mapped in identities:
        return sorted({str(value) for value in identities[mapped].get("cluster_ids") or []})
    return [identifier]


def ensure_identity(payload: dict[str, Any], identifier: str) -> str:
    identities = payload.setdefault("identities", {})
    mapping = payload.setdefault("cluster_to_identity", {})
    if identifier in identities:
        return identifier
    if identifier in mapping and mapping[identifier] in identities:
        return str(mapping[identifier])
    identity_id = _identity_id(identifier)
    identities[identity_id] = {
        "display_name": "",
        "tags": [],
        "cluster_ids": [identifier],
    }
    mapping[identifier] = identity_id
    return identity_id


def name_identity(
    payload: dict[str, Any], identifier: str, display_name: str, tags: Iterable[str]
) -> str:
    identity_id = ensure_identity(payload, identifier)
    identity = payload["identities"][identity_id]
    identity["display_name"] = " ".join(display_name.split())[:120]
    identity["tags"] = sorted({" ".join(str(tag).split())[:80] for tag in tags if str(tag).strip()})
    return identity_id


def merge_identity(payload: dict[str, Any], source_identifier: str, target_identifier: str) -> str:
    target_id = ensure_identity(payload, target_identifier)
    source_clusters = resolve_clusters(payload, source_identifier)
    identities = payload["identities"]
    mapping = payload["cluster_to_identity"]
    for cluster_id in source_clusters:
        previous = str(mapping.get(cluster_id) or "")
        if previous and previous in identities and previous != target_id:
            identities[previous]["cluster_ids"] = [
                value for value in identities[previous].get("cluster_ids") or []
                if value != cluster_id
            ]
        mapping[cluster_id] = target_id
        if cluster_id not in identities[target_id]["cluster_ids"]:
            identities[target_id]["cluster_ids"].append(cluster_id)
    for identity_id in list(identities):
        if not identities[identity_id].get("cluster_ids"):
            identities.pop(identity_id, None)
    identities[target_id]["cluster_ids"] = sorted(set(identities[target_id]["cluster_ids"]))
    return target_id


def detach_cluster(payload: dict[str, Any], cluster_id: str) -> str:
    mapping = payload.setdefault("cluster_to_identity", {})
    identities = payload.setdefault("identities", {})
    previous = str(mapping.get(cluster_id) or "")
    previous_name = ""
    previous_tags: list[str] = []
    if previous in identities:
        previous_name = str(identities[previous].get("display_name") or "")
        previous_tags = list(identities[previous].get("tags") or [])
        identities[previous]["cluster_ids"] = [
            value for value in identities[previous].get("cluster_ids") or []
            if value != cluster_id
        ]
        if not identities[previous]["cluster_ids"]:
            identities.pop(previous, None)
    identity_id = _identity_id(cluster_id)
    identities[identity_id] = {
        "display_name": previous_name,
        "tags": previous_tags,
        "cluster_ids": [cluster_id],
    }
    mapping[cluster_id] = identity_id
    return identity_id


def grouped_catalog(
    rows: list[dict[str, Any]], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    by_cluster = {str(row["person_cluster_id"]): dict(row) for row in rows}
    identities = payload.get("identities") or {}
    mapping = payload.get("cluster_to_identity") or {}
    groups: dict[str, list[dict[str, Any]]] = {}
    for cluster_id, row in by_cluster.items():
        identifier = str(mapping.get(cluster_id) or cluster_id)
        groups.setdefault(identifier, []).append(row)
    result: list[dict[str, Any]] = []
    for identifier, members in groups.items():
        first = members[0]
        annotation = identities.get(identifier) or {}
        cluster_ids = sorted(str(row["person_cluster_id"]) for row in members)
        item = {
            **first,
            "person_cluster_id": identifier,
            "machine_cluster_ids": cluster_ids,
            "display_name": str(annotation.get("display_name") or ""),
            "tags": list(annotation.get("tags") or []),
            "member_count": sum(int(row.get("member_count") or 0) for row in members),
            "distinct_source_count": sum(int(row.get("distinct_source_count") or 0) for row in members),
            "merged_cluster_count": len(cluster_ids),
            "is_local_identity": identifier in identities,
        }
        result.append(item)
    return sorted(
        result,
        key=lambda row: (-int(row.get("member_count") or 0), str(row["person_cluster_id"])),
    )
