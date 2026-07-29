#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03 YOLOE rerun + high-value candidate audit v2
- Reads existing Stop03 visual_unit join manifest (1628 derived images)
- Reads local prompt registry: A_CORE + OCR_TRIGGER by default
- Runs local YOLOE/YOLO model only from local model path
- Writes detection manifest, label distribution, high-value audit
- Does not read or write original media. Only reads derived JPG/preview images.
- Does not download anything. Offline env vars are set.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Hard offline guardrails
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")
os.environ.setdefault("ULTRALYTICS_OFFLINE", "1")

_MODEL = None
_CLASSES: List[str] = []
_DEVICE = "mps"
_IMGSZ = 640
_CONF = 0.25


def now_ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    ensure_dir(path.parent)
    if fieldnames is None:
        keys = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        fieldnames = keys
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def write_json(path: Path, obj: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_registry_classes(path: Path, include_b: bool = False, include_ocr_trigger: bool = True) -> Tuple[List[str], Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Prompt registry not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))

    classes: List[str] = []

    def add(label: str):
        label = str(label or "").strip()
        if label and label not in classes:
            classes.append(label)

    for item in data.get("A_CORE_CLASSES", []) or []:
        add(item.get("label"))

    if include_b:
        for item in data.get("B_EXTENDED_CLASSES", []) or []:
            add(item.get("label"))

    if include_ocr_trigger:
        sp = (data.get("special_policies") or {}).get("OCR_TRIGGER") or {}
        for item in sp.get("strong_labels", []) or []:
            add(item.get("label"))
        for item in sp.get("weak_labels", []) or []:
            add(item.get("label"))

    meta = {
        "registry_path": str(path),
        "schema_version": data.get("schema_version"),
        "a_core_count": len(data.get("A_CORE_CLASSES", []) or []),
        "b_extended_count": len(data.get("B_EXTENDED_CLASSES", []) or []),
        "include_b_extended": include_b,
        "include_ocr_trigger": include_ocr_trigger,
        "class_count_used": len(classes),
        "classes_used": classes,
    }
    return classes, meta


def patch_ultralytics_no_autoupdate() -> Dict[str, Any]:
    """Disable Ultralytics requirement auto-install in this worker.

    Important: local CLIP may be importable via a shim, but Ultralytics can still
    run its own requirement checker for `git+https://github.com/ultralytics/CLIP.git`.
    That checker is not allowed in this project because it can call pip/git.
    We patch only the requirement checker functions; model inference code is unchanged.
    """
    patch_report: Dict[str, Any] = {"patched_modules": [], "errors": []}
    try:
        import importlib
        import sys as _sys

        def _no_requirements(*args, **kwargs):
            return True

        def _patch_module(modname: str) -> None:
            try:
                mod = importlib.import_module(modname)
                changed = False
                for attr in ("check_requirements", "check_pip_update_available"):
                    if hasattr(mod, attr):
                        setattr(mod, attr, _no_requirements)
                        changed = True
                if changed:
                    patch_report["patched_modules"].append(modname)
            except Exception as e:  # noqa: BLE001
                patch_report["errors"].append(f"{modname}: {type(e).__name__}: {e}")

        # Patch common locations. Some modules import check_requirements directly,
        # so we patch both known modules and already-loaded modules.
        for name in [
            "ultralytics.utils.checks",
            "ultralytics.utils",
            "ultralytics.nn.tasks",
            "ultralytics.models.yolo.model",
            "ultralytics.models.yolo.detect.predict",
        ]:
            _patch_module(name)

        for name, mod in list(_sys.modules.items()):
            if not name.startswith("ultralytics") or mod is None:
                continue
            changed = False
            for attr in ("check_requirements", "check_pip_update_available"):
                if hasattr(mod, attr):
                    try:
                        setattr(mod, attr, _no_requirements)
                        changed = True
                    except Exception:
                        pass
            if changed and name not in patch_report["patched_modules"]:
                patch_report["patched_modules"].append(name)
    except Exception as e:  # noqa: BLE001
        patch_report["errors"].append(f"patch_failed: {type(e).__name__}: {e}")
    return patch_report


def init_worker(model_path: str, classes: List[str], device: str, imgsz: int, conf: float) -> None:
    global _MODEL, _CLASSES, _DEVICE, _IMGSZ, _CONF
    _CLASSES = list(classes)
    _DEVICE = device
    _IMGSZ = int(imgsz)
    _CONF = float(conf)
    try:
        # Import Ultralytics from the formal YOLO env, then disable its auto-install
        # requirement checker before YOLOE text prompt embedding is initialized.
        from ultralytics import YOLOE  # type: ignore
        from ultralytics import YOLO  # type: ignore
        patch_ultralytics_no_autoupdate()

        try:
            _MODEL = YOLOE(model_path)
        except Exception:
            _MODEL = YOLO(model_path)

        patch_ultralytics_no_autoupdate()

        # YOLOE open-vocabulary classes. Several ultralytics builds expose this API.
        # If unavailable, we do not download or guess; we run the local model as-is and record config status.
        if hasattr(_MODEL, "set_classes"):
            try:
                if hasattr(_MODEL, "get_text_pe"):
                    patch_ultralytics_no_autoupdate()
                    _MODEL.set_classes(_CLASSES, _MODEL.get_text_pe(_CLASSES))
                else:
                    _MODEL.set_classes(_CLASSES)
            except TypeError:
                _MODEL.set_classes(_CLASSES)
    except Exception as e:
        _MODEL = e


def image_quality(path: str) -> Dict[str, Any]:
    try:
        from PIL import Image, ImageStat
        im = Image.open(path).convert("L")
        stat = ImageStat.Stat(im)
        mean = float(stat.mean[0])
        std = float(stat.stddev[0])
        near_black = bool(mean < 8 and std < 5)
        near_white = bool(mean > 247 and std < 5)
        return {"luma_mean": round(mean, 3), "luma_std": round(std, 3), "near_black": near_black, "near_white": near_white}
    except Exception as e:
        return {"luma_mean": "", "luma_std": "", "near_black": "", "near_white": "", "quality_error": str(e)[:200]}


def run_one(task: Dict[str, str]) -> Dict[str, Any]:
    global _MODEL, _CLASSES, _DEVICE, _IMGSZ, _CONF
    t0 = time.time()
    row = dict(task)
    image_path = row.get("visual_file") or row.get("runtime_input_image_path") or ""
    out: Dict[str, Any] = {
        "visual_unit_id": row.get("visual_unit_id", ""),
        "visual_unit_type": row.get("visual_unit_type", ""),
        "visual_file": image_path,
        "visual_file_sha256": row.get("visual_file_sha256", ""),
        "original_source_content_id": row.get("original_source_content_id") or row.get("parent_source_content_id") or "",
        "source_relative_path": row.get("source_relative_path", ""),
        "time_position_ms": row.get("time_position_ms", ""),
        "imgsz": _IMGSZ,
        "conf": _CONF,
        "device": _DEVICE,
        "status": "failed",
        "detection_count": 0,
        "detected_labels": "",
        "detected_labels_json": "{}",
        "detections_json": "[]",
        "error_message": "",
        "elapsed_ms": "",
    }
    out.update(image_quality(image_path))

    try:
        if isinstance(_MODEL, Exception):
            raise RuntimeError(f"worker model init failed: {_MODEL}")
        if not image_path or not Path(image_path).exists():
            raise FileNotFoundError(f"visual_file missing: {image_path}")

        # Run local prediction only. verbose=False prevents huge logs.
        results = _MODEL.predict(source=image_path, imgsz=_IMGSZ, conf=_CONF, device=_DEVICE, verbose=False)
        result = results[0] if isinstance(results, list) else results
        names = getattr(result, "names", None) or getattr(_MODEL, "names", {}) or {}
        boxes = getattr(result, "boxes", None)
        detections: List[Dict[str, Any]] = []
        label_counts: Counter[str] = Counter()

        if boxes is not None and getattr(boxes, "cls", None) is not None:
            cls_list = boxes.cls.detach().cpu().tolist() if hasattr(boxes.cls, "detach") else list(boxes.cls)
            conf_list = boxes.conf.detach().cpu().tolist() if getattr(boxes, "conf", None) is not None and hasattr(boxes.conf, "detach") else ([] if getattr(boxes, "conf", None) is None else list(boxes.conf))
            xyxy_list = boxes.xyxy.detach().cpu().tolist() if getattr(boxes, "xyxy", None) is not None and hasattr(boxes.xyxy, "detach") else ([] if getattr(boxes, "xyxy", None) is None else list(boxes.xyxy))
            for i, cls_val in enumerate(cls_list):
                try:
                    cls_id = int(cls_val)
                except Exception:
                    cls_id = -1
                label = names.get(cls_id, str(cls_id)) if isinstance(names, dict) else str(cls_id)
                score = float(conf_list[i]) if i < len(conf_list) else None
                bbox = xyxy_list[i] if i < len(xyxy_list) else None
                label_counts[label] += 1
                detections.append({
                    "label": label,
                    "class_id": cls_id,
                    "confidence": round(score, 6) if score is not None else "",
                    "bbox_xyxy": [round(float(x), 3) for x in bbox] if bbox is not None else [],
                })

        out["status"] = "success"
        out["detection_count"] = len(detections)
        out["detected_labels"] = "|".join(sorted(label_counts.keys()))
        out["detected_labels_json"] = json.dumps(dict(label_counts), ensure_ascii=False, sort_keys=True)
        out["detections_json"] = json.dumps(detections, ensure_ascii=False)
    except Exception as e:
        out["status"] = "failed"
        out["error_message"] = (str(e) + "\n" + traceback.format_exc())[:4000]
    finally:
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 3)
    return out


def chunks(lst: List[Dict[str, str]], n: int) -> List[List[Dict[str, str]]]:
    return [lst[i:i+n] for i in range(0, len(lst), n)]


def high_value_reason(row: Dict[str, Any], ocr_labels: set[str]) -> Tuple[bool, str, str]:
    labels = set(str(row.get("detected_labels") or "").split("|")) - {""}
    reasons = []
    if str(row.get("near_black")).lower() == "true" or row.get("near_black") is True:
        return False, "excluded_near_black", ""
    if str(row.get("near_white")).lower() == "true" or row.get("near_white") is True:
        reasons.append("near_white_review")
    if int(row.get("detection_count") or 0) > 0:
        reasons.append("yolo_positive")
    if labels & ocr_labels:
        reasons.append("ocr_trigger_label")
    # Keep broad: this audit is to inspect YOLO-assisted high-value, not replace scene-change/Qwen rules.
    selected = any(r in reasons for r in ["yolo_positive", "ocr_trigger_label"])
    return selected, "|".join(reasons), "|".join(sorted(labels))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--join", required=True, help="Stop03-1 join manifest containing 1628 visual units")
    ap.add_argument("--registry", required=True, help="Prompt registry JSON containing A_CORE_CLASSES and OCR_TRIGGER")
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="mps")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--include-b-extended", action="store_true")
    ap.add_argument("--no-ocr-trigger", action="store_true")
    ap.add_argument("--expect-count", type=int, default=1628)
    args = ap.parse_args()

    t0 = time.time()
    run_root = Path(args.run_root)
    join_path = Path(args.join)
    registry_path = Path(args.registry)
    model_path = Path(args.model)
    out_dir = Path(args.out)
    manifests_dir = out_dir / "manifests"
    reports_dir = out_dir / "reports"
    ensure_dir(manifests_dir)
    ensure_dir(reports_dir)

    problems: List[str] = []
    if not join_path.exists():
        problems.append(f"join_missing:{join_path}")
    if not registry_path.exists():
        problems.append(f"registry_missing:{registry_path}")
    if not model_path.exists():
        problems.append(f"model_missing:{model_path}")
    if problems:
        summary = {"validation_status": "FAIL", "problems": problems}
        write_json(reports_dir / "stop03_yoloe_rerun_and_highvalue_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    classes, registry_meta = load_registry_classes(registry_path, include_b=args.include_b_extended, include_ocr_trigger=not args.no_ocr_trigger)
    rows = read_csv(join_path)
    if args.expect_count and len(rows) != args.expect_count:
        problems.append(f"join_count_mismatch:{len(rows)}!={args.expect_count}")

    tasks = []
    for r in rows:
        image = r.get("visual_file", "")
        if not image:
            problems.append(f"missing_visual_file:{r.get('visual_unit_id','')}")
        tasks.append(r)

    result_rows: List[Dict[str, Any]] = []
    workers = max(1, int(args.workers))

    with ProcessPoolExecutor(max_workers=workers, initializer=init_worker, initargs=(str(model_path), classes, args.device, args.imgsz, args.conf)) as ex:
        futs = [ex.submit(run_one, task) for task in tasks]
        done = 0
        for fut in as_completed(futs):
            result_rows.append(fut.result())
            done += 1
            if done % 100 == 0 or done == len(futs):
                print(f"progress {done}/{len(futs)}", flush=True)

    # stable order by input visual_unit_id order
    order = {r.get("visual_unit_id", ""): i for i, r in enumerate(rows)}
    result_rows.sort(key=lambda r: order.get(str(r.get("visual_unit_id", "")), 10**12))

    # label distribution
    label_counter: Counter[str] = Counter()
    for r in result_rows:
        try:
            d = json.loads(r.get("detected_labels_json") or "{}")
            for k, v in d.items():
                label_counter[k] += int(v)
        except Exception:
            pass

    # OCR trigger label set
    reg_data = json.loads(registry_path.read_text(encoding="utf-8"))
    ocr_labels = set()
    sp = (reg_data.get("special_policies") or {}).get("OCR_TRIGGER") or {}
    for item in (sp.get("strong_labels", []) or []) + (sp.get("weak_labels", []) or []):
        if item.get("label"):
            ocr_labels.add(item["label"])

    audit_rows: List[Dict[str, Any]] = []
    for r in result_rows:
        selected, reason, labels = high_value_reason(r, ocr_labels)
        ar = dict(r)
        ar.update({
            "yolo_high_value_candidate": int(bool(selected)),
            "candidate_reason": reason,
            "candidate_labels": labels,
        })
        audit_rows.append(ar)

    result_csv = manifests_dir / "yoloe_rerun_result_manifest.csv"
    result_jsonl = manifests_dir / "yoloe_rerun_result_manifest.jsonl"
    audit_csv = manifests_dir / "yoloe_high_value_candidate_audit.csv"
    label_csv = reports_dir / "yoloe_label_distribution.csv"

    result_fields = [
        "visual_unit_id", "visual_unit_type", "visual_file", "visual_file_sha256", "original_source_content_id",
        "source_relative_path", "time_position_ms", "imgsz", "conf", "device", "status", "elapsed_ms",
        "detection_count", "detected_labels", "detected_labels_json", "detections_json",
        "luma_mean", "luma_std", "near_black", "near_white", "quality_error", "error_message",
    ]
    write_csv(result_csv, result_rows, result_fields)
    write_jsonl(result_jsonl, result_rows)
    write_csv(audit_csv, audit_rows, result_fields + ["yolo_high_value_candidate", "candidate_reason", "candidate_labels"])
    write_csv(label_csv, [{"label": k, "count": v} for k, v in label_counter.most_common()], ["label", "count"])

    success_count = sum(1 for r in result_rows if r.get("status") == "success")
    failed_count = len(result_rows) - success_count
    positive_rows = sum(1 for r in result_rows if int(r.get("detection_count") or 0) > 0)
    label_nonempty_rows = sum(1 for r in result_rows if (r.get("detected_labels") or "").strip())
    near_black_rows = sum(1 for r in result_rows if r.get("near_black") is True)
    yolo_hv_rows = sum(1 for r in audit_rows if int(r.get("yolo_high_value_candidate") or 0) == 1)

    status = "PASS"
    if problems or failed_count > 0:
        status = "FAIL"
    elif positive_rows == 0:
        status = "FAIL"
        problems.append("zero_positive_yolo_detections_after_rerun")
    elif near_black_rows > 0:
        status = "PASS_WITH_REVIEW"

    summary = {
        "validation_status": status,
        "elapsed_seconds": round(time.time() - t0, 3),
        "mode": "rerun_yoloe_on_existing_derived_images_only",
        "source_safety": "read_derived_images_only_no_original_media_write",
        "network": "disabled_by_offline_env_vars_no_download_logic",
        "model_download": "not_allowed_model_path_must_be_local",
        "run_root": str(run_root),
        "join": str(join_path),
        "registry": registry_meta,
        "model": str(model_path),
        "settings": {"workers": workers, "device": args.device, "imgsz": args.imgsz, "conf": args.conf},
        "counts": {
            "input_rows": len(rows),
            "success_count": success_count,
            "failed_count": failed_count,
            "detection_count_positive_rows": positive_rows,
            "label_nonempty_rows": label_nonempty_rows,
            "near_black_rows": near_black_rows,
            "yolo_high_value_candidate_rows": yolo_hv_rows,
        },
        "top_labels": dict(label_counter.most_common(50)),
        "problems": problems,
        "outputs": {
            "result_csv": str(result_csv),
            "result_jsonl": str(result_jsonl),
            "high_value_audit_csv": str(audit_csv),
            "label_distribution_csv": str(label_csv),
        },
    }
    write_json(reports_dir / "stop03_yoloe_rerun_and_highvalue_summary.json", summary)
    md = [
        "# Stop03 YOLOE Rerun + High-value Audit v1",
        "",
        f"- validation_status: `{summary['validation_status']}`",
        f"- mode: `{summary['mode']}`",
        f"- source_safety: `{summary['source_safety']}`",
        f"- network: `{summary['network']}`",
        "",
        "## Counts",
    ]
    for k, v in summary["counts"].items():
        md.append(f"- {k}: `{v}`")
    md.extend(["", "## Top labels", "```json", json.dumps(summary["top_labels"], ensure_ascii=False, indent=2), "```", "", "## Problems", "```json", json.dumps(problems, ensure_ascii=False, indent=2), "```", "", "## Outputs"])
    for k, v in summary["outputs"].items():
        md.append(f"- {k}: `{v}`")
    (reports_dir / "stop03_yoloe_rerun_and_highvalue_summary.md").write_text("\n".join(md), encoding="utf-8")

    print("== Stop03 YOLOE rerun + high-value audit finished ==")
    print(json.dumps({
        "validation_status": summary["validation_status"],
        "counts": summary["counts"],
        "top_labels": dict(label_counter.most_common(20)),
        "summary_md": str(reports_dir / "stop03_yoloe_rerun_and_highvalue_summary.md"),
        "result_csv": str(result_csv),
        "high_value_audit_csv": str(audit_csv),
    }, ensure_ascii=False, indent=2))
    return 0 if status in ("PASS", "PASS_WITH_REVIEW") else 2


if __name__ == "__main__":
    raise SystemExit(main())
