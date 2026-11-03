"""Canonical materialization of a product task into the V2 execution contract.

The UI creates ordinary task metadata first.  This module is the single
production boundary that turns that metadata into the immutable source and
worker-contract inputs required by ``event_driven_v2``.  It is deliberately
idempotent and never writes the source tree or model directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .v2_model_workers import contract as build_worker_contract
from .v2_producer import fingerprint


VIDEO_EXTENSIONS = {
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".mxf",
    ".braw", ".r3d", ".crm", ".ari",
}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".hif",
    ".dng", ".arw", ".cr2", ".cr3", ".nef", ".nrw", ".rw2", ".raf",
    ".orf", ".webp", ".bmp", ".gif",
}
REQUIRED_STAGES = (
    "yoloe", "openclip", "dhash", "quality", "geometry", "qwen", "ocr",
    "audio_extract", "vad", "whisper", "embedding",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.materialize.tmp")
    temporary.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _source_kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    return None


def _source_inventory(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    root = Path(str(task.get("source_root") or "")).expanduser().resolve(strict=True)
    task_id = str(task.get("task_id") or "")
    existing = {
        str(row.get("source_relative_path")): row
        for row in (task.get("v2_sources") or [])
        if isinstance(row, Mapping) and str(row.get("source_relative_path") or "")
    }
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.name.startswith("._"):
            continue
        kind = _source_kind(path)
        if not kind:
            continue
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        prior = existing.get(relative) or {}
        # The identity is a content-independent source identity.  The current
        # file fingerprint belongs to the V2 source revision, so a changed file
        # keeps the same source while producing a new revision downstream.
        source_id = str(prior.get("source_content_id") or fingerprint(("v2-source", task_id, relative)))
        file_id = str(prior.get("source_file_id") or fingerprint(("v2-file", task_id, relative)))
        rows.append({
            "source_content_id": source_id,
            "source_file_id": file_id,
            "source_path": str(path),
            "source_relative_path": relative,
            "media_kind": kind,
            "next_action": str(prior.get("next_action") or "process"),
            "interval_seconds": float(prior.get("interval_seconds") or 3),
            "source_size": int(stat.st_size),
            "source_mtime_ns": int(stat.st_mtime_ns),
        })
    if not rows:
        raise ValueError("v2_source_inventory_empty")
    return rows


def _runtime_configs(task: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    runtime = dict(task.get("runtime") or {})
    python = dict(runtime.get("python") or {})
    models = dict(runtime.get("models") or {})
    tools = dict(runtime.get("tools") or {})
    configs = dict(runtime.get("configs") or {})
    from .v2_device_policy import default_devices
    devices = dict(runtime.get('v2_devices') if runtime.get('v2_devices') is not None else default_devices(runtime))
    allowed_devices = {'yoloe': {'cpu','mps'}, 'openclip': {'cpu','mps','auto'}}
    if any(stage not in allowed_devices or value not in allowed_devices[stage]
           for stage,value in devices.items()):
        raise ValueError('v2_visual_device_selection_invalid')
    model_root = str(runtime.get("model_root") or os.environ.get("MEDIA_ARCHIVE_MODEL_ROOT") or "").strip()
    if not model_root:
        raise ValueError("v2_model_root_missing")
    model_root = str(Path(model_root).expanduser().resolve(strict=True))
    project_root = Path(str(runtime.get("project_root") or "")).expanduser().resolve()
    pipeline_configs = project_root / "configs"
    registry = str(configs.get("yoloe_registry") or (pipeline_configs / "yoloe_keyword_registry_default_v1.json"))
    prompt = str(configs.get("qwen_prompt") or (pipeline_configs / "qwenvl_prompt_raw_media_v3_384.txt"))
    result: dict[str, dict[str, Any]] = {
        "yoloe": {
            "python_path": str(python.get("yolo") or python.get("visual") or ""),
            "model_path": str(models.get("yoloe") or ""),
            "mobileclip_path": str(models.get("yoloe_mobileclip") or ""),
            "registry": registry, "allowed_model_root": model_root,
            "include_b": True, "include_ocr_trigger": True, "device": devices.get('yoloe','cpu'),
            "imgsz": 640, "confidence": 0.25,
        },
        "openclip": {
            "python_path": str(python.get("visual") or ""),
            "model_path": str(models.get("openclip") or ""),
            "model_name": "ViT-B-32", "allowed_model_root": model_root, "device": devices.get('openclip','cpu'),
        },
        "ocr": {
            "python_path": str(python.get("ocr") or ""),
            "text_detection_model_dir": str(models.get("ocr_detection") or ""),
            "text_recognition_model_dir": str(models.get("ocr_recognition") or ""),
            "allowed_model_root": model_root,
            "paddlex_cache_root": str(Path(str(task.get("workspace") or ".")) / "paddlex_cache"),
        },
        "embedding": {
            "python_path": str(python.get("embedding") or ""),
            "model_path": str(models.get("text_embedding") or ""),
            "allowed_model_root": model_root, "device": "cpu",
        },
        "qwen": {
            "python_path": str(python.get("qwen") or ""),
            "model_path": str(models.get("qwen") or ""), "prompt_path": prompt,
            "allowed_model_root": model_root, "max_tokens": 384,
            "temperature": 0.0, "top_p": 1.0,
            **({"repetition_penalty": 1.1, "repetition_context_size": 64,
                "schema_retry_policy":"raw_media_v3_schema_reask_uncertainty_once_v2"}
               if Path(prompt).name == "qwenvl_prompt_raw_media_v3_384.txt" else {}),
            "response_timeout_seconds": float(os.environ.get("MEDIA_ARCHIVE_QWEN_RESPONSE_TIMEOUT_SECONDS", "300")),
        },
        "whisper": {
            "python_path": str(python.get("whisper") or ""),
            "model_path": str(models.get("whisper") or ""),
            "allowed_model_root": model_root, "parameters": {"language": "zh", "task": "transcribe", "initial_prompt": "以下是中文语音的简体中文转写。保留英文原文、数字和标点。"}, "text_normalization": "zh-hans-transcription-v1",
        },
        "vad": {
            "python_path": str(python.get("vad") or ""),
            "package_root": str(models.get("silero_vad") or ""),
            "allowed_model_root": model_root, "parameters": {},
        },
        "audio_extract": {
            "python_path": str(python.get("visual") or ""),
            "ffmpeg": str(tools.get("ffmpeg") or "/opt/homebrew/bin/ffmpeg"),
            "ffprobe": str(tools.get("ffprobe") or "/opt/homebrew/bin/ffprobe"),
            "artifact_root": str(Path(str(task.get("workspace") or ".")) / "audio"),
        },
    }
    person_path = configs.get("person_reid")
    person = json.loads(Path(person_path).read_text(encoding="utf-8")) if person_path and Path(person_path).is_file() else {}
    person.update({
        "python_path": str(python.get("person_reid") or python.get("yolo") or ""),
        "model_dir": str(models.get("person_reid") or ""), "allowed_model_root": model_root,
    })
    result["insightface"] = person
    for stage in REQUIRED_STAGES:
        if stage in result and not result[stage].get("python_path") and stage in {"yoloe", "openclip", "ocr", "embedding", "qwen", "whisper", "vad"}:
            raise ValueError(f"v2_runtime_python_missing:{stage}")
    return result


def _finder_metadata(sources):
    """Reuse frozen Step01 read-only metadata semantics before image planning.

    This only runs for a new manifest. Existing immutable tasks are not silently
    changed on Resume, and metadata probe errors cannot become an empty input.
    """
    from .v2_product_media import load_module
    scanner = load_module('scripts/02_step01_step02_pipeline/step01_source_scan_lineage_dedup_db_safe_v7_20260709_175400.py')
    images = [deepcopy(s) for s in sources]
    # Spotlight is not authoritative: existing files can have intact xattrs
    # while mdls reports "could not find". Read the original binary plist,
    # without changing the frozen planner/candidate semantics.
    enriched, tags = scanner.enrich_with_local_metadata(images, False)
    for source in enriched:
        values, status = _read_finder_xattr(Path(source['source_path']))
        parsed = []
        for raw in values:
            name, color = scanner.parse_macos_tag(raw)
            parsed.append({'tag_raw':raw,'tag_name':name,'tag_color':color})
            tags.append({'tag_id':'tag_'+scanner.sha256_text(f"finder_tag_v1|{source.get('source_file_id')}|{raw}",24),
                         'source_file_id':source.get('source_file_id',''),
                         'source_content_id':source['source_content_id'],
                         'source_path':source['source_path'],
                         'tag_raw':raw,'tag_name':name,'tag_color':color})
        source['finder_tag_status'] = status
        source['finder_tags_json'] = json.dumps(parsed,ensure_ascii=False)
    for source in enriched:
        if source.get('finder_tag_status') not in ('ok','none','disabled'):
            raise ValueError('v2_finder_metadata_unavailable:' + source['source_content_id'] + ':' + str(source.get('finder_tag_status')))
    by_id = {s['source_content_id']:s for s in enriched}
    return [by_id.get(s['source_content_id'],s) for s in sources], tags


def _read_finder_xattr(path):
    if not Path('/usr/bin/xattr').exists():
        raise ValueError('v2_finder_metadata_reader_unavailable')
    probe = subprocess.run(['/usr/bin/xattr','-px','com.apple.metadata:_kMDItemUserTags',str(path)],
                           capture_output=True,text=True,timeout=3)
    if probe.returncode:
        if path.is_file() and 'No such xattr' in probe.stderr:
            return [], 'none'
        raise ValueError('v2_finder_metadata_read_failed:'+str(path)+':'+probe.stderr.strip()[:160])
    try:
        values = plistlib.loads(bytes.fromhex(probe.stdout))
        if not isinstance(values,list) or any(not isinstance(v,str) for v in values):
            raise ValueError('invalid_tag_plist')
    except Exception as exc:
        raise ValueError('v2_finder_metadata_invalid:'+str(path)) from exc
    return values, 'ok' if values else 'none'


def _candidate_source_assets(sources):
    """Translate V2 manifest fields into the frozen V25 source schema."""
    rows=[]
    for source in sources:
        relative=source.get('relative_path') or source['source_relative_path']
        path=Path(source.get('source_path') or relative)
        rows.append({**source,'media_type':source.get('media_type') or source['media_kind'],
                     'relative_path':relative,'file_name':source.get('file_name') or path.name,
                     'extension':source.get('extension') or path.suffix.lower(),
                     'is_deleted_or_missing':source.get('is_deleted_or_missing',0)})
    return rows


def materialize_v2_task(task_path: Path, *, persist: bool = True) -> dict[str, Any]:
    """Materialize one task once, returning a detached authoritative manifest."""
    task_path = Path(task_path).expanduser().resolve(strict=True)
    task = json.loads(task_path.read_text(encoding="utf-8"))
    current = task.get("v2_sources")
    contracts = task.get("v2_contracts")
    if isinstance(current, list) and current and isinstance(contracts, Mapping) and set(REQUIRED_STAGES) <= set(contracts):
        changed = False
        if not task.get("scheduler_trace_path"):
            task["scheduler_trace_path"] = str(Path(str(task.get("workspace") or ".")) / "scheduler_trace.jsonl")
            changed = True
        runtime = dict(task.get("runtime") or {})
        policies = dict(runtime.get("policies") or {})
        if policies.get("scheduler_default") != "event_driven_v2":
            policies["scheduler_default"] = "event_driven_v2"
            changed = True
        if policies.get("pipeline_async_status") != "IMPLEMENTED":
            policies["pipeline_async_status"] = "IMPLEMENTED"
            changed = True
        if changed:
            runtime["policies"] = policies
            task["runtime"] = runtime
            if persist:
                _atomic_write_json(task_path, task)
        return task
    sources = _source_inventory(task)
    if 'finder_tags' not in task:
        sources, task['finder_tags'] = _finder_metadata(sources)
        task['finder_metadata_contract'] = 'readonly_finder_xattr_frozen_step01_rows_v1'
    configs = _runtime_configs(task)
    from .v2_heavy_admission import DEFAULT_HEAVY_BUDGET
    task['heavy_admission_policy']=json.loads(json.dumps(DEFAULT_HEAVY_BUDGET))
    # These are pool ceilings, not eager model loads or static job shards.
    configs['qwen'].setdefault('resident_worker_limit',3)
    configs['whisper'].setdefault('resident_worker_limit',2)
    from .v2_execution_profile import freeze_profile,MEASURED_NEW_TASK_PROFILE
    # Existing materialized tasks return above: no frozen result change.
    profile=freeze_profile((task.get('runtime') or {}).get('v2_execution_profile',MEASURED_NEW_TASK_PROFILE))
    if profile is not None:
        for stage,n in profile['workers'].items():configs[stage]['resident_worker_limit']=n
        configs['openclip']['inference_batch_size']=profile['openclip_batch']
        task['v2_execution_profile']=profile
        task['openclip_batch_size']=profile['openclip_batch']
        task['cheap_visual_batch_size']=profile['cheap_visual_batch']
        task['visual_source_affinity']=profile['source_affinity']
    stages = REQUIRED_STAGES + (('insightface',) if configs.get('insightface', {}).get('enabled') is True else ())
    built = {stage: build_worker_contract(stage, configs.get(stage, {}), 1) for stage in stages}
    task["source_assets"] = _candidate_source_assets(sources)
    task["v2_sources"] = list(sources)
    from .v2_a9t_plan import plan_images
    from .v2_product_media import load_module
    image_sources = [s for s in sources if s['media_kind'] == 'image']
    if image_sources:
        task['a9t_full_snapshot_plan'] = plan_images(
            load_module('scripts/02_step01_step02_pipeline/step02_2_image_preview_from_db_safe_v6_20260709_182200.py'),
            image_sources, task.get('finder_tags') or [])
    task["real_handler_configs"] = configs
    task["v2_contracts"] = built
    task["v2_materialization"] = {
        "contract": "media_archive_v2_task_materialization_v1",
        "source_inventory_fingerprint": fingerprint([(row["source_relative_path"], row["source_content_id"]) for row in sources]),
        "source_count": len(sources),
        "stages": list(stages),
    }
    # Keep the authoritative execution trace beside the task workspace so the
    # real UI/diagnostics can distinguish durable V2 progress from legacy
    # stage counters without introducing a second state authority.
    task["scheduler_trace_path"] = str(Path(str(task.get("workspace") or ".")) / "scheduler_trace.jsonl")
    runtime = dict(task.get("runtime") or {})
    policies = dict(runtime.get("policies") or {})
    policies["scheduler_default"] = "event_driven_v2"
    policies["pipeline_async_status"] = "IMPLEMENTED"
    runtime["policies"] = policies
    task["runtime"] = runtime
    if persist:
        _atomic_write_json(task_path, task)
    return task
