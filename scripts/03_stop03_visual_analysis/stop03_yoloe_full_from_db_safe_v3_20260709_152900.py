#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03 YOLOE full from DB, local-only safe v3.

Purpose:
- Read visual_units from media_archive.sqlite after Step02 image/video processing.
- Run local YOLOE-26L only from local absolute paths.
- Bind local mobileclip2_b.ts and block any network/download fallback.
- Write detections to visual_labels and model_runs, plus CSV/JSON reports.
- Create/update visual_label_terms from local prompt registry for zh/bilingual search routing.

Safety:
- No network.
- No model download.
- No dependency install.
- No original media write; reads derived visual_file only.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import socket
import sqlite3
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCRIPT_VERSION = "stop03_yoloe_full_from_db_safe_v3_20260709_152900"
STAGE = "stop03_yoloe_full"
DEFAULT_DB = Path("/Users/yourname/Documents/AI-Local/media-archive-clean/media_archive.sqlite")
DEFAULT_MODEL = Path("/Users/yourname/Documents/model/yoloe26-l-seg/weights/yoloe26-l-seg.pt")
DEFAULT_MOBILECLIP = Path("/Users/yourname/Documents/model/yoloe26-l-seg/mobileclip2_b.ts")
DEFAULT_OUT = Path("/Users/yourname/Documents/AI-Local/test-output/stop03-yoloe-full-db-safe-v3_20260709_152900")
DEFAULT_REGISTRY = Path("/Users/yourname/Documents/本地素材大整理配置/提示词注册表/当前提示词_OCR_TRIGGER_v1.0.json")
PROJECT_ROOT = Path("/Users/yourname/Documents/AI-Local/media-archive-clean")
MODEL_ROOT = Path("/Users/yourname/Documents/model")

DEFAULT_CLASSES = [
    "person", "face", "human", "man", "woman", "child",
    "car", "vehicle", "truck", "bus", "motorcycle", "bicycle", "tractor", "train", "boat",
    "wheat", "crop", "field", "farmland", "tree", "plant", "animal", "dog", "cat", "bird",
    "building", "house", "street", "road", "bridge", "sky", "water", "river", "mountain",
    "screen", "phone", "computer", "sign", "signboard", "text", "document", "book", "poster", "receipt", "license plate",
    "food", "bowl", "cup", "bottle", "bag", "box", "chair", "table", "door", "window",
]

_OFFLINE_INSTALLED = False


def now_ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, obj: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    ensure_dir(path.parent)
    if fieldnames is None:
        seen = []
        s = set()
        for r in rows:
            for k in r.keys():
                if k not in s:
                    seen.append(k); s.add(k)
        fieldnames = seen
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def install_offline_guard(mobileclip_path: Path) -> Dict[str, Any]:
    """Hard-disable common network paths before model imports."""
    global _OFFLINE_INSTALLED
    report: Dict[str, Any] = {"installed": True, "mobileclip_path": str(mobileclip_path), "patched": [], "errors": []}
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("ULTRALYTICS_OFFLINE", "1")
    os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")
    os.environ.setdefault("YOLO_CONFIG_DIR", "/Users/yourname/Documents/AI-Local/test-output/ultralytics-offline-config")
    if _OFFLINE_INSTALLED:
        return report

    def blocked_connect(self, address):  # type: ignore[no-untyped-def]
        raise RuntimeError(f"OFFLINE_NETWORK_BLOCKED: socket.connect({address})")

    try:
        socket.socket.connect = blocked_connect  # type: ignore[assignment]
        report["patched"].append("socket.socket.connect")
    except Exception as e:
        report["errors"].append(f"socket_patch:{type(e).__name__}:{e}")

    # Best-effort patch for requests / urllib if already available. Do not import heavy network libs on purpose.
    try:
        import urllib.request  # noqa
        def blocked_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("OFFLINE_NETWORK_BLOCKED: urllib.request.urlopen")
        urllib.request.urlopen = blocked_urlopen  # type: ignore[assignment]
        report["patched"].append("urllib.request.urlopen")
    except Exception as e:
        report["errors"].append(f"urllib_patch:{type(e).__name__}:{e}")

    _OFFLINE_INSTALLED = True
    return report


def patch_ultralytics_local_mobileclip(mobileclip_path: Path) -> Dict[str, Any]:
    """Patch Ultralytics helpers that otherwise may attempt to download mobileclip2_b.ts."""
    report: Dict[str, Any] = {"patched": [], "errors": [], "mobileclip_path": str(mobileclip_path)}
    mobileclip_path = mobileclip_path.resolve()

    def local_asset(path_or_name: Any = None, *args: Any, **kwargs: Any) -> str:
        val = str(path_or_name or "")
        if "mobileclip2_b.ts" in val or val in ("", "None"):
            return str(mobileclip_path)
        p = Path(val).expanduser()
        if p.exists():
            return str(p)
        raise RuntimeError(f"OFFLINE_DOWNLOAD_BLOCKED: attempt_download_asset({val})")

    def no_requirements(*args: Any, **kwargs: Any) -> bool:
        return True

    try:
        import importlib
        import sys as _sys
        candidates = [
            "ultralytics.utils.downloads",
            "ultralytics.utils.checks",
            "ultralytics.utils",
            "ultralytics.nn.tasks",
            "ultralytics.nn.text_model",
            "ultralytics.models.yolo.model",
        ]
        for modname in candidates:
            try:
                mod = importlib.import_module(modname)
            except Exception as e:
                report["errors"].append(f"import:{modname}:{type(e).__name__}:{e}")
                continue
            changed = False
            for attr in ("check_requirements", "check_pip_update_available"):
                if hasattr(mod, attr):
                    try:
                        setattr(mod, attr, no_requirements); changed = True
                    except Exception:
                        pass
            for attr in ("attempt_download_asset", "safe_download"):
                if hasattr(mod, attr):
                    try:
                        setattr(mod, attr, local_asset); changed = True
                    except Exception:
                        pass
            if changed:
                report["patched"].append(modname)

        # Patch any already-loaded ultralytics module copies.
        for name, mod in list(_sys.modules.items()):
            if not name.startswith("ultralytics") or mod is None:
                continue
            changed = False
            for attr in ("check_requirements", "check_pip_update_available"):
                if hasattr(mod, attr):
                    try:
                        setattr(mod, attr, no_requirements); changed = True
                    except Exception:
                        pass
            for attr in ("attempt_download_asset", "safe_download"):
                if hasattr(mod, attr):
                    try:
                        setattr(mod, attr, local_asset); changed = True
                    except Exception:
                        pass
            if changed and name not in report["patched"]:
                report["patched"].append(name)

        # MobileCLIPTS direct constructor hook.
        try:
            import ultralytics.nn.text_model as utm  # type: ignore
            if hasattr(utm, "MobileCLIPTS") and not getattr(utm.MobileCLIPTS, "_local_offline_patched", False):
                orig_init = utm.MobileCLIPTS.__init__
                def patched_init(self, device, weight="mobileclip2_b.ts", *args, **kwargs):  # type: ignore[no-untyped-def]
                    return orig_init(self, device, weight=str(mobileclip_path), *args, **kwargs)
                utm.MobileCLIPTS.__init__ = patched_init
                utm.MobileCLIPTS._local_offline_patched = True
                report["patched"].append("ultralytics.nn.text_model.MobileCLIPTS.__init__")
        except Exception as e:
            report["errors"].append(f"MobileCLIPTS_patch:{type(e).__name__}:{e}")
    except Exception as e:
        report["errors"].append(f"ultralytics_patch:{type(e).__name__}:{e}")
    return report



def _append_unique(items: List[str], value: Any) -> None:
    s = str(value or "").strip()
    if s and s not in items:
        items.append(s)


def _json_list(values: Iterable[Any]) -> str:
    out: List[str] = []
    for v in values:
        _append_unique(out, v)
    return json.dumps(out, ensure_ascii=False)


def build_label_terms_from_registry(registry: Optional[Path], include_b: bool, include_ocr_trigger: bool) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build bilingual label term rows from the local prompt registry.

    visual_labels.label remains the stable English model label.
    visual_label_terms provides Chinese display/search/routing metadata.
    """
    terms: Dict[str, Dict[str, Any]] = {}
    meta: Dict[str, Any] = {
        "registry_path": str(registry) if registry else "",
        "registry_exists": bool(registry and registry.exists()),
        "schema_version": "",
        "term_count": 0,
        "source_layers": {},
        "policy": "visual_labels.label stays English; visual_label_terms adds zh/search/routing metadata",
    }
    if not registry or not registry.exists():
        return [], meta
    data = json.loads(registry.read_text(encoding="utf-8"))
    schema_version = str(data.get("schema_version") or "")
    meta["schema_version"] = schema_version
    query_mappings = data.get("query_mappings") or {}

    def ensure(label: str) -> Dict[str, Any]:
        label = str(label or "").strip()
        if label not in terms:
            terms[label] = {
                "label": label,
                "label_zh": "",
                "category_zh": "",
                "source_layers": [],
                "used_by": [],
                "trigger_strengths": [],
                "search_terms": [label],
                "registry_path": str(registry),
                "registry_schema_version": schema_version,
            }
        return terms[label]

    def add_item(item: Any, source_layer: str, used_by: Iterable[str], trigger_strength: str = "") -> None:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            zh = str(item.get("zh") or "").strip()
            category_zh = str(item.get("category_zh") or "").strip()
            item_source = str(item.get("source") or source_layer).strip() or source_layer
            item_strength = str(item.get("trigger_strength") or trigger_strength).strip()
        else:
            label = str(item or "").strip()
            zh = ""
            category_zh = ""
            item_source = source_layer
            item_strength = trigger_strength
        if not label:
            return
        t = ensure(label)
        if zh and not t.get("label_zh"):
            t["label_zh"] = zh
        if category_zh and not t.get("category_zh"):
            t["category_zh"] = category_zh
        _append_unique(t["source_layers"], source_layer)
        if item_source and item_source != source_layer:
            _append_unique(t["source_layers"], item_source)
        for u in used_by:
            _append_unique(t["used_by"], u)
        if item_strength:
            _append_unique(t["trigger_strengths"], item_strength)
        for x in [label, zh]:
            _append_unique(t["search_terms"], x)

    for item in data.get("A_CORE_CLASSES", []) or []:
        add_item(item, "A_CORE", ["YOLOE"], "")
    if include_b:
        for item in data.get("B_EXTENDED_CLASSES", []) or []:
            add_item(item, "B_EXTENDED", ["YOLOE", "Qwen-VL"], "")
    if include_ocr_trigger:
        sp = (data.get("special_policies") or {}).get("OCR_TRIGGER") or {}
        for item in sp.get("strong_labels", []) or []:
            add_item(item, "OCR_TRIGGER", ["YOLOE", "OCR"], "strong")
        for item in sp.get("weak_labels", []) or []:
            add_item(item, "OCR_TRIGGER", ["YOLOE", "OCR"], "weak")

    # Add Chinese query aliases that map to each English label.
    for query, labels in query_mappings.items():
        for label in labels or []:
            label = str(label or "").strip()
            if not label:
                continue
            t = ensure(label)
            _append_unique(t["search_terms"], query)
            _append_unique(t["search_terms"], label)

    rows: List[Dict[str, Any]] = []
    for label in sorted(terms):
        t = terms[label]
        label_zh = t.get("label_zh") or label
        search_terms = t.get("search_terms") or [label]
        # Embedding text is deliberately Chinese-first, with English as stable fallback.
        embedding_parts = [
            f"中文标签：{label_zh}",
            f"英文标签：{label}",
        ]
        if t.get("category_zh"):
            embedding_parts.append(f"类别：{t.get('category_zh')}")
        if search_terms:
            embedding_parts.append("检索词：" + "、".join(search_terms))
        if t.get("trigger_strengths"):
            embedding_parts.append("OCR触发：" + "、".join(t.get("trigger_strengths") or []))
        rows.append({
            "label": label,
            "label_zh": label_zh,
            "category_zh": t.get("category_zh") or "",
            "source_layer": "|".join(t.get("source_layers") or []),
            "trigger_strength": "|".join(t.get("trigger_strengths") or []),
            "used_by_json": _json_list(t.get("used_by") or []),
            "search_terms_json": _json_list(search_terms),
            "embedding_text": "。".join(embedding_parts),
            "registry_path": str(registry),
            "registry_schema_version": schema_version,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    layer_counter = Counter()
    for r in rows:
        for part in str(r.get("source_layer") or "").split("|"):
            if part:
                layer_counter[part] += 1
    meta["term_count"] = len(rows)
    meta["source_layers"] = dict(layer_counter)
    return rows, meta


def ensure_visual_label_terms_table(con: sqlite3.Connection) -> None:
    con.execute("""
    CREATE TABLE IF NOT EXISTS visual_label_terms (
        label TEXT PRIMARY KEY,
        label_zh TEXT NOT NULL,
        category_zh TEXT,
        source_layer TEXT,
        trigger_strength TEXT,
        used_by_json TEXT,
        search_terms_json TEXT,
        embedding_text TEXT,
        registry_path TEXT,
        registry_schema_version TEXT,
        created_at TEXT
    )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_visual_label_terms_label_zh ON visual_label_terms(label_zh)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_visual_label_terms_source_layer ON visual_label_terms(source_layer)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_visual_label_terms_trigger_strength ON visual_label_terms(trigger_strength)")
    con.commit()


def upsert_visual_label_terms(con: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    ensure_visual_label_terms_table(con)
    if not rows:
        return 0
    cols = [
        "label", "label_zh", "category_zh", "source_layer", "trigger_strength",
        "used_by_json", "search_terms_json", "embedding_text",
        "registry_path", "registry_schema_version", "created_at",
    ]
    q = f"""
    INSERT OR REPLACE INTO visual_label_terms ({','.join(cols)})
    VALUES ({','.join('?' for _ in cols)})
    """
    con.executemany(q, [[r.get(c, "") for c in cols] for r in rows])
    con.commit()
    return len(rows)

def load_classes(registry: Optional[Path], include_b: bool, include_ocr_trigger: bool, classes_arg: str = "") -> Tuple[List[str], Dict[str, Any]]:
    classes: List[str] = []
    meta: Dict[str, Any] = {"source": "default", "registry_path": str(registry) if registry else ""}

    def add(x: Any) -> None:
        label = str(x or "").strip()
        if label and label not in classes:
            classes.append(label)

    if registry and registry.exists():
        data = json.loads(registry.read_text(encoding="utf-8"))
        for item in data.get("A_CORE_CLASSES", []) or []:
            add(item.get("label") if isinstance(item, dict) else item)
        if include_b:
            for item in data.get("B_EXTENDED_CLASSES", []) or []:
                add(item.get("label") if isinstance(item, dict) else item)
        if include_ocr_trigger:
            sp = (data.get("special_policies") or {}).get("OCR_TRIGGER") or {}
            for item in (sp.get("strong_labels", []) or []) + (sp.get("weak_labels", []) or []):
                add(item.get("label") if isinstance(item, dict) else item)
        meta.update({"source": "registry", "schema_version": data.get("schema_version"), "class_count": len(classes)})
    if classes_arg.strip():
        for x in classes_arg.split(","):
            add(x)
        meta["source"] = meta.get("source", "") + "+cli_classes"
    if not classes:
        for x in DEFAULT_CLASSES:
            add(x)
        meta.update({"source": "default_builtin", "class_count": len(classes)})
    meta["class_count"] = len(classes)
    meta["classes"] = classes
    meta["classes_sha256"] = sha256_text("\n".join(classes))
    return classes, meta


def table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def table_info_map(con: sqlite3.Connection, table: str) -> Dict[str, Dict[str, Any]]:
    """Return PRAGMA table_info map. Used to avoid inserting TEXT into INTEGER PK label_id."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for r in con.execute(f"PRAGMA table_info({table})").fetchall():
            out[str(r[1])] = {
                "cid": r[0],
                "name": r[1],
                "type": str(r[2] or ""),
                "notnull": r[3],
                "dflt_value": r[4],
                "pk": r[5],
            }
    except Exception:
        pass
    return out


def count_table(con: sqlite3.Connection, table: str) -> Optional[int]:
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:
        return None


def insert_model_run(con: sqlite3.Connection, *, run_id: str, model_path: Path, mobileclip_path: Path, input_count: int, status: str = "running") -> None:
    cols = table_columns(con, "model_runs")
    data = {
        "run_id": run_id,
        "stage": STAGE,
        "model_name": "yoloe26-l-seg",
        "model_path": str(model_path),
        "script_version": SCRIPT_VERSION,
        "status": status,
        "input_count": input_count,
        "output_count": 0,
        "error_message": "",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
    }
    # Compatibility with older schema names if present.
    data.update({
        "stage_name": STAGE,
        "script_path": str(Path(__file__).resolve()),
        "script_sha256": sha256_file(Path(__file__).resolve()) if Path(__file__).exists() else "",
        "model_local_path": str(model_path),
        "model_version": "local",
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "error_summary": "",
    })
    if not cols:
        return
    ins_cols = [c for c in cols if c in data and data[c] is not None]
    if not ins_cols:
        return
    q = f"INSERT OR REPLACE INTO model_runs ({','.join(ins_cols)}) VALUES ({','.join('?' for _ in ins_cols)})"
    con.execute(q, [data[c] for c in ins_cols])
    con.commit()


def finish_model_run(con: sqlite3.Connection, *, run_id: str, status: str, output_count: int, error_message: str = "") -> None:
    cols = table_columns(con, "model_runs")
    if not cols:
        return
    data = {
        "status": status,
        "output_count": output_count,
        "error_message": error_message,
        "error_summary": error_message,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    set_cols = [c for c in cols if c in data]
    if "run_id" in cols:
        con.execute(f"UPDATE model_runs SET {','.join(c+'=?' for c in set_cols)} WHERE run_id=?", [data[c] for c in set_cols] + [run_id])
    else:
        return
    con.commit()


def fetch_visual_units(con: sqlite3.Connection, limit: int = 0, only_missing: bool = False) -> List[Dict[str, Any]]:
    base = """
    SELECT vu.visual_unit_id,
           vu.source_content_id,
           vu.derived_id,
           vu.visual_file,
           vu.time_position_ms,
           vu.near_black,
           vu.luma_mean,
           vu.luma_std,
           da.derived_type,
           da.derived_path,
           da.sha256 AS derived_sha256,
           sa.relative_path AS source_relative_path,
           sa.absolute_path AS source_absolute_path
    FROM visual_units vu
    LEFT JOIN derived_assets da ON vu.derived_id = da.derived_id
    LEFT JOIN source_assets sa ON vu.source_content_id = sa.source_content_id
    WHERE vu.visual_file IS NOT NULL AND vu.visual_file <> ''
    """
    if only_missing and table_columns(con, "visual_labels"):
        base += """
        AND NOT EXISTS (
            SELECT 1 FROM visual_labels vl
            WHERE vl.visual_unit_id = vu.visual_unit_id
              AND COALESCE(vl.model_name, '') = 'yoloe26-l-seg'
        )
        """
    base += " ORDER BY vu.source_content_id, vu.time_position_ms, vu.visual_unit_id"
    if limit and limit > 0:
        base += f" LIMIT {int(limit)}"
    cur = con.execute(base)
    return [dict(r) for r in cur.fetchall()]


def detect_result_to_rows(result: Any, model: Any, classes: List[str]) -> List[Dict[str, Any]]:
    names = getattr(result, "names", None) or getattr(model, "names", {}) or {}
    boxes = getattr(result, "boxes", None)
    detections: List[Dict[str, Any]] = []
    if boxes is None or getattr(boxes, "cls", None) is None:
        return detections
    cls_list = boxes.cls.detach().cpu().tolist() if hasattr(boxes.cls, "detach") else list(boxes.cls)
    conf_obj = getattr(boxes, "conf", None)
    conf_list = conf_obj.detach().cpu().tolist() if conf_obj is not None and hasattr(conf_obj, "detach") else ([] if conf_obj is None else list(conf_obj))
    xyxy_obj = getattr(boxes, "xyxy", None)
    xyxy_list = xyxy_obj.detach().cpu().tolist() if xyxy_obj is not None and hasattr(xyxy_obj, "detach") else ([] if xyxy_obj is None else list(xyxy_obj))
    for i, cls_val in enumerate(cls_list):
        try:
            cls_id = int(cls_val)
        except Exception:
            cls_id = -1
        if isinstance(names, dict):
            label = str(names.get(cls_id, cls_id))
        elif 0 <= cls_id < len(classes):
            label = classes[cls_id]
        else:
            label = str(cls_id)
        conf = float(conf_list[i]) if i < len(conf_list) else None
        bbox = xyxy_list[i] if i < len(xyxy_list) else []
        detections.append({
            "label": label,
            "class_id": cls_id,
            "confidence": round(conf, 6) if conf is not None else None,
            "bbox_xyxy": [round(float(x), 3) for x in bbox] if bbox else [],
        })
    return detections


def insert_visual_label(con: sqlite3.Connection, *, run_id: str, row: Dict[str, Any], det: Dict[str, Any], model_path: Path, mobileclip_path: Path) -> bool:
    cols = table_columns(con, "visual_labels")
    info = table_info_map(con, "visual_labels")
    if not cols:
        raise RuntimeError("visual_labels table missing or unreadable")
    bbox = det.get("bbox_xyxy") or []
    generated_label_id = "vl_" + sha256_text(json.dumps([row.get("visual_unit_id"), run_id, det], ensure_ascii=False, sort_keys=True))[:24]
    bbox_json = json.dumps(bbox, ensure_ascii=False)
    det_json = json.dumps(det, ensure_ascii=False, sort_keys=True)
    data = {
        # visual_label_id/id are for compatible schemas only. label_id is handled below because it may be INTEGER PK.
        "visual_label_id": generated_label_id,
        "id": generated_label_id,
        "visual_unit_id": row.get("visual_unit_id"),
        "source_content_id": row.get("source_content_id"),
        "derived_id": row.get("derived_id"),
        "label": det.get("label"),
        "class_id": det.get("class_id"),
        "confidence": det.get("confidence"),
        "score": det.get("confidence"),
        "bbox": bbox_json,
        "bbox_json": bbox_json,
        "detection_json": det_json,
        "model_name": "yoloe26-l-seg",
        "model_path": str(model_path),
        "text_encoder_asset": str(mobileclip_path),
        "run_id": run_id,
        "model_run_id": run_id,
        "stage": STAGE,
        "script_version": SCRIPT_VERSION,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # Only provide label_id when it is not an INTEGER primary key. Current project DB uses INTEGER label_id.
    label_id_info = info.get("label_id", {})
    label_id_type = str(label_id_info.get("type", "")).upper()
    label_id_pk = int(label_id_info.get("pk") or 0)
    if "label_id" in cols and not ("INT" in label_id_type and label_id_pk):
        data["label_id"] = generated_label_id
    if len(bbox) >= 4:
        data.update({"bbox_x1": bbox[0], "bbox_y1": bbox[1], "bbox_x2": bbox[2], "bbox_y2": bbox[3]})
    ins_cols = [c for c in cols if c in data and data[c] is not None]
    if not ins_cols:
        raise RuntimeError(f"visual_labels schema has no compatible insert columns: {cols}")
    q = f"INSERT OR REPLACE INTO visual_labels ({','.join(ins_cols)}) VALUES ({','.join('?' for _ in ins_cols)})"
    con.execute(q, [data[c] for c in ins_cols])
    return True

def db_audit(db: Path, model: Path, mobileclip: Path, registry: Optional[Path]) -> Dict[str, Any]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    counts = {t: count_table(con, t) for t in ["source_assets", "derived_assets", "visual_units", "visual_labels", "visual_label_terms", "embeddings", "model_runs", "processing_errors"]}
    schema = {t: table_columns(con, t) for t in ["visual_units", "derived_assets", "visual_labels", "visual_label_terms", "model_runs"]}
    schema_info = {t: table_info_map(con, t) for t in ["visual_units", "derived_assets", "visual_labels", "visual_label_terms", "model_runs"]}
    sample = []
    try:
        sample = [dict(r) for r in con.execute("""
            SELECT vu.visual_unit_id, vu.visual_file, vu.source_content_id, vu.derived_id, da.derived_type, da.derived_path
            FROM visual_units vu LEFT JOIN derived_assets da ON vu.derived_id=da.derived_id
            ORDER BY vu.visual_unit_id LIMIT 5
        """).fetchall()]
    except Exception as e:
        sample = [{"error": str(e)}]
    con.close()
    return {
        "script_version": SCRIPT_VERSION,
        "mode": "db_audit_only",
        "db_path": str(db),
        "model_path": str(model),
        "model_exists": model.exists(),
        "mobileclip_path": str(mobileclip),
        "mobileclip_exists": mobileclip.exists(),
        "mobileclip_size_bytes": mobileclip.stat().st_size if mobileclip.exists() else None,
        "registry_path": str(registry) if registry else "",
        "registry_exists": registry.exists() if registry else False,
        "counts": counts,
        "schemas": schema,
        "schema_info": schema_info,
        "sample_visual_units": sample,
        "network_policy": "offline_guard_installed_before_ultralytics_import; missing local asset fails; no download fallback",
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Stop03 YOLOE full DB safe local-only runner")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--mobileclip", default=str(DEFAULT_MOBILECLIP))
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="YOLOE prompt registry JSON; default is local OCR_TRIGGER v1.0 registry")
    ap.add_argument("--classes", default="", help="Optional comma-separated extra/override labels")
    ap.add_argument("--include-b-extended", action="store_true")
    ap.add_argument("--no-ocr-trigger", action="store_true")
    ap.add_argument("--device", default="mps")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--db-audit-only", action="store_true")
    ap.add_argument("--clear-existing-yoloe-labels", action="store_true", help="Delete old yoloe26-l-seg labels before run")
    ap.add_argument("--only-missing", action="store_true", help="Skip visual_units with existing yoloe26-l-seg labels; zero-detection units cannot be inferred from labels alone")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    db = Path(args.db)
    out = Path(args.out)
    model_path = Path(args.model)
    mobileclip_path = Path(args.mobileclip)
    registry = Path(args.registry) if args.registry else None
    ensure_dir(out / "reports")
    ensure_dir(out / "manifests")

    audit = db_audit(db, model_path, mobileclip_path, registry)
    if args.db_audit_only:
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return 0 if audit.get("model_exists") and audit.get("mobileclip_exists") else 2

    problems: List[str] = []
    if not db.exists(): problems.append(f"db_missing:{db}")
    if not model_path.exists(): problems.append(f"model_missing:{model_path}")
    if not mobileclip_path.exists(): problems.append(f"mobileclip_missing:{mobileclip_path}")
    if registry and not registry.exists(): problems.append(f"registry_missing:{registry}")
    # Basic local path policy.
    for p, name in [(model_path, "model"), (mobileclip_path, "mobileclip")]:
        try:
            rp = p.resolve()
            if not str(rp).startswith(str(MODEL_ROOT)):
                problems.append(f"{name}_outside_model_root:{rp}")
        except Exception:
            pass
    if problems:
        summary = {"validation_status": "FAIL", "problems": problems, "audit": audit}
        write_json(out / "reports" / "stop03_yoloe_full_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    offline_report = install_offline_guard(mobileclip_path)

    # Import after offline guard.
    try:
        from ultralytics import YOLOE, YOLO  # type: ignore
    except Exception as e:
        summary = {"validation_status": "FAIL", "error": f"ultralytics_import_failed:{type(e).__name__}:{e}", "offline_report": offline_report}
        write_json(out / "reports" / "stop03_yoloe_full_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    ultralytics_patch_report = patch_ultralytics_local_mobileclip(mobileclip_path)
    classes, class_meta = load_classes(registry, args.include_b_extended, not args.no_ocr_trigger, args.classes)
    term_rows, term_meta = build_label_terms_from_registry(registry, args.include_b_extended, not args.no_ocr_trigger)

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    term_rows_upserted = upsert_visual_label_terms(con, term_rows)
    terms_csv = out / "reports" / "stop03_yoloe_label_terms.csv"
    write_csv(terms_csv, term_rows)
    vu_count = count_table(con, "visual_units") or 0
    rows = fetch_visual_units(con, limit=args.limit, only_missing=args.only_missing)
    run_id = now_ts()
    insert_model_run(con, run_id=run_id, model_path=model_path, mobileclip_path=mobileclip_path, input_count=len(rows))

    if args.clear_existing_yoloe_labels and "visual_labels" in [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
        cols = table_columns(con, "visual_labels")
        if "model_name" in cols:
            con.execute("DELETE FROM visual_labels WHERE model_name='yoloe26-l-seg'")
            con.commit()

    t0 = time.time()
    try:
        patch_ultralytics_local_mobileclip(mobileclip_path)
        try:
            model = YOLOE(str(model_path))
        except Exception:
            model = YOLO(str(model_path))
        patch_ultralytics_local_mobileclip(mobileclip_path)
        if hasattr(model, "set_classes"):
            patch_ultralytics_local_mobileclip(mobileclip_path)
            if hasattr(model, "get_text_pe"):
                model.set_classes(classes, model.get_text_pe(classes))
            else:
                model.set_classes(classes)
    except Exception as e:
        err = f"model_init_failed:{type(e).__name__}:{e}"
        finish_model_run(con, run_id=run_id, status="failed", output_count=0, error_message=err)
        summary = {"validation_status": "FAIL", "error": err, "traceback": traceback.format_exc(), "offline_report": offline_report, "ultralytics_patch_report": ultralytics_patch_report}
        write_json(out / "reports" / "stop03_yoloe_full_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    result_rows: List[Dict[str, Any]] = []
    label_counter: Counter[str] = Counter()
    processed = 0
    failed = 0
    detection_rows = 0
    positive_units = 0

    for idx, r in enumerate(rows, start=1):
        row = dict(r)
        image_path = Path(str(row.get("visual_file") or ""))
        rec: Dict[str, Any] = {
            "run_id": run_id,
            "visual_unit_id": row.get("visual_unit_id"),
            "source_content_id": row.get("source_content_id"),
            "derived_id": row.get("derived_id"),
            "visual_file": str(image_path),
            "source_relative_path": row.get("source_relative_path"),
            "time_position_ms": row.get("time_position_ms"),
            "status": "failed",
            "detection_count": 0,
            "labels": "",
            "error_message": "",
            "elapsed_ms": 0,
        }
        print(f"[start {idx}/{len(rows)}] visual_unit_id={row.get('visual_unit_id')} file={image_path.name}", flush=True)
        one_t0 = time.time()
        try:
            if not image_path.exists():
                raise FileNotFoundError(f"visual_file_missing:{image_path}")
            res = model.predict(source=str(image_path), imgsz=args.imgsz, conf=args.conf, device=args.device, verbose=False)
            result = res[0] if isinstance(res, list) else res
            detections = detect_result_to_rows(result, model, classes)
            labels = []
            for det in detections:
                insert_visual_label(con, run_id=run_id, row=row, det=det, model_path=model_path, mobileclip_path=mobileclip_path)
                detection_rows += 1
                labels.append(str(det.get("label") or ""))
                if det.get("label"):
                    label_counter[str(det.get("label"))] += 1
            con.commit()
            processed += 1
            if detections:
                positive_units += 1
            rec.update({"status": "success", "detection_count": len(detections), "labels": "|".join(sorted(set(labels)))})
            print(f"[done {idx}/{len(rows)}] visual_unit_id={row.get('visual_unit_id')} detections={len(detections)} labels={rec.get('labels')}", flush=True)
        except Exception as e:
            failed += 1
            con.rollback()
            rec.update({"status": "failed", "error_message": (str(e) + "\n" + traceback.format_exc())[:4000]})
            print(f"[fail {idx}/{len(rows)}] visual_unit_id={row.get('visual_unit_id')} error={str(e)[:300]}", flush=True)
        finally:
            rec["elapsed_ms"] = round((time.time() - one_t0) * 1000, 3)
            result_rows.append(rec)
        # Always printed per unit above; keep compact aggregate every 50 for long runs.
        if idx % 50 == 0 or idx == len(rows):
            print(f"progress {idx}/{len(rows)} processed={processed} failed={failed} detections={detection_rows}", flush=True)

    status = "done" if failed == 0 else "done_with_failures"
    finish_model_run(con, run_id=run_id, status=status, output_count=detection_rows, error_message="" if failed == 0 else f"failed_units={failed}")
    con.close()

    result_csv = out / "manifests" / "stop03_yoloe_full_result_manifest.csv"
    label_csv = out / "reports" / "stop03_yoloe_label_distribution.csv"
    write_csv(result_csv, result_rows)
    write_csv(label_csv, [{"label": k, "count": v} for k, v in label_counter.most_common()], ["label", "count"])
    summary = {
        "validation_status": "PASS" if failed == 0 else "PASS_WITH_FAILED_UNITS",
        "script_version": SCRIPT_VERSION,
        "run_id": run_id,
        "db_path": str(db),
        "source_safety": "read_visual_units_derived_images_only_no_original_media_write",
        "network": "blocked; no download; local model/mobileclip required",
        "model_path": str(model_path),
        "mobileclip_path": str(mobileclip_path),
        "class_meta": class_meta,
        "term_meta": {**term_meta, "rows_upserted": term_rows_upserted, "terms_csv": str(terms_csv)},
        "settings": {"device": args.device, "imgsz": args.imgsz, "conf": args.conf, "limit": args.limit},
        "counts": {
            "db_visual_units_total": vu_count,
            "input_rows": len(rows),
            "processed_units": processed,
            "failed_units": failed,
            "positive_units": positive_units,
            "visual_label_rows_inserted": detection_rows,
        },
        "top_labels": dict(label_counter.most_common(50)),
        "elapsed_seconds": round(time.time() - t0, 3),
        "offline_report": offline_report,
        "ultralytics_patch_report": ultralytics_patch_report,
        "outputs": {"result_csv": str(result_csv), "label_distribution_csv": str(label_csv), "label_terms_csv": str(terms_csv)},
    }
    write_json(out / "reports" / "stop03_yoloe_full_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
