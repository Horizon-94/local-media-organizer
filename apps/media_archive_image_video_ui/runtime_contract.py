from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "media_archive_app_runtime_contract_v1"
REQUIRED_PYTHON = ("system", "visual", "yolo", "qwen", "ocr", "embedding")
REQUIRED_TOOLS = ("ffmpeg", "ffprobe", "sips")
REQUIRED_MODELS = (
    "yoloe", "yoloe_mobileclip", "openclip", "qwen",
    "ocr_detection", "ocr_recognition", "text_embedding", "person_reid",
)
REQUIRED_SCRIPTS = (
    "stage_runner", "source_scan", "image_preview", "video_frames",
    "prepare_visual_schema", "yoloe", "openclip", "dedup", "person_reid",
    "candidate_snapshot", "candidate_select", "optional_stage", "qwen",
    "ocr", "evidence", "propagation", "embedding", "search_adapter",
    "search_engine", "finder_tag_refresh", "supplement_contract",
    "supplement_qwen", "supplement_evidence_merge",
)
REQUIRED_CONFIGS = (
    "candidate", "person_reid", "qwen", "qwen_prompt", "ocr", "evidence", "propagation",
    "embedding_contract", "embedding_runtime", "hybrid_search",
)
REQUIRED_MIGRATIONS = (
    "person_reid", "ocr", "evidence", "propagation", "embedding", "supplement",
)


def default_model_root() -> Path:
    configured = os.environ.get("MEDIA_ARCHIVE_MODEL_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().absolute()
    return (Path.home() / "Library/Application Support/素材大整理/Models").absolute()


def _replace_placeholders(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for marker, replacement in replacements.items():
            value = value.replace(marker, replacement)
        return value
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_placeholders(item, replacements)
            for key, item in value.items()
        }
    return value


def load_runtime_contract(
    path: Path, *, model_root: Path | None = None,
) -> dict[str, Any]:
    contract_path = Path(path).expanduser().absolute()
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise ValueError(f"unsupported runtime contract: {payload.get('contract_version')}")
    app_contents = (
        contract_path.parent.parent
        if contract_path.parent.name == "Resources"
        else contract_path.parent
    )
    source_project_root = (
        contract_path.parent.parent
        if contract_path.parent.name == "configs"
        else app_contents / "Resources" / "Pipeline"
    )
    selected_project_root = Path(
        os.environ.get("MEDIA_ARCHIVE_PROJECT_ROOT", str(source_project_root))
    ).expanduser().absolute()
    selected_env_root = Path(
        os.environ.get(
            "MEDIA_ARCHIVE_ENV_ROOT",
            str(selected_project_root.parent / "envs"),
        )
    ).expanduser().absolute()
    selected_model_root = (model_root or default_model_root()).expanduser().absolute()
    ffmpeg = os.environ.get("MEDIA_ARCHIVE_FFMPEG") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    ffprobe = os.environ.get("MEDIA_ARCHIVE_FFPROBE") or shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    sips = os.environ.get("MEDIA_ARCHIVE_SIPS") or shutil.which("sips") or "/usr/bin/sips"
    payload = _replace_placeholders(payload, {
        "$APP_CONTENTS": str(app_contents),
        "$APP_RESOURCES": str(app_contents / "Resources"),
        "$PROJECT_ROOT": str(selected_project_root),
        "$ENV_ROOT": str(selected_env_root),
        "$MODEL_ROOT": str(selected_model_root),
        "$FFMPEG": ffmpeg,
        "$FFPROBE": ffprobe,
        "$SIPS": sips,
        "/Users/yourname/Documents/AI-Local/media-archive-clean": str(selected_project_root),
        "/Users/yourname/Documents/AI-Local/envs": str(selected_env_root),
        "/Users/yourname/Documents/model": str(selected_model_root),
    })
    payload["contract_path"] = str(contract_path)
    payload["model_root"] = str(selected_model_root)
    return payload


def _path_ok(path: Path, kind: str) -> bool:
    if kind == "directory":
        return path.is_dir()
    return path.is_file()


def validate_runtime_contract(
    path: Path, *, model_root: Path | None = None,
) -> dict[str, Any]:
    try:
        payload = load_runtime_contract(path, model_root=model_root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "FAIL", "ready": False, "contract_version": None,
            "contract_path": str(Path(path).expanduser().absolute()),
            "missing": [], "errors": [str(exc)],
        }

    missing: list[str] = []
    errors: list[str] = []
    project_root = Path(str(payload.get("project_root") or "")).expanduser()
    if not project_root.is_dir():
        missing.append("project_root")

    python = payload.get("python") or {}
    for key in REQUIRED_PYTHON:
        value = str(python.get(key) or "")
        if not value or not Path(value).expanduser().is_file():
            missing.append(f"python.{key}")

    tools = payload.get("tools") or {}
    for key in REQUIRED_TOOLS:
        value = str(tools.get(key) or "")
        if not value or not Path(value).expanduser().is_file():
            missing.append(f"tools.{key}")

    models = payload.get("models") or {}
    for key in REQUIRED_MODELS:
        item = models.get(key) or {}
        kind = str(item.get("kind") or "")
        value = str(item.get("path") or "")
        if kind not in {"file", "directory"}:
            errors.append(f"models.{key}.kind")
        elif not value or not _path_ok(Path(value).expanduser(), kind):
            missing.append(f"models.{key}")

    for section, required in (
        ("scripts", REQUIRED_SCRIPTS),
        ("configs", REQUIRED_CONFIGS),
        ("migrations", REQUIRED_MIGRATIONS),
    ):
        rows = payload.get(section) or {}
        for key in required:
            value = str(rows.get(key) or "")
            if not value or not Path(value).expanduser().is_file():
                missing.append(f"{section}.{key}")

    policies = payload.get("policies") or {}
    expected = {
        "original_media_access": "read_only",
        "model_directory_access": "read_only",
        "network": "disabled",
        "download": "disabled",
        "fixed_dataset_counts": False,
    }
    for key, expected_value in expected.items():
        if policies.get(key) != expected_value:
            errors.append(f"policies.{key}")

    # Frozen stage configs still contain their own safety declarations.  Check
    # them against the one authoritative app contract so a future local path
    # change cannot silently send a stage to a different runtime or model.
    try:
        qwen_config = json.loads(Path(payload["configs"]["qwen"]).read_text(encoding="utf-8"))
        ocr_config = json.loads(Path(payload["configs"]["ocr"]).read_text(encoding="utf-8"))
        embedding_config = json.loads(
            Path(payload["configs"]["embedding_contract"]).read_text(encoding="utf-8")
        )
        references = (
            ("configs.qwen.model_path", qwen_config.get("model_path"), models["qwen"]["path"]),
            ("configs.qwen.qwen_python", qwen_config.get("qwen_python"), python["qwen"]),
            ("configs.ocr.ocr_python", ocr_config.get("ocr_python"), python["ocr"]),
            ("configs.ocr.text_detection_model_dir", ocr_config.get("text_detection_model_dir"), models["ocr_detection"]["path"]),
            ("configs.ocr.text_recognition_model_dir", ocr_config.get("text_recognition_model_dir"), models["ocr_recognition"]["path"]),
            ("configs.embedding.model_path", embedding_config.get("model_path"), models["text_embedding"]["path"]),
            ("configs.embedding.python_path", embedding_config.get("python_path"), python["embedding"]),
        )
        if payload.get("config_path_policy") != "materialize_effective_runtime_configs_v1":
            for label, actual, authoritative in references:
                if str(actual or "") != str(authoritative or ""):
                    errors.append(label)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        errors.append(f"config_reference_validation:{exc}")

    ready = not missing and not errors
    return {
        "status": "PASS" if ready else "FAIL",
        "ready": ready,
        "contract_version": payload["contract_version"],
        "contract_path": payload["contract_path"],
        "model_root": payload["model_root"],
        "missing": missing,
        "errors": errors,
        "model_items": [
            {
                "key": key,
                "path": str((payload.get("models") or {}).get(key, {}).get("path") or ""),
                "ready": key not in {
                    item.split(".", 1)[1] for item in missing if item.startswith("models.")
                },
            }
            for key in REQUIRED_MODELS
        ],
        "counts": {
            "python": len(REQUIRED_PYTHON), "tools": len(REQUIRED_TOOLS), "models": len(REQUIRED_MODELS),
            "scripts": len(REQUIRED_SCRIPTS), "configs": len(REQUIRED_CONFIGS),
            "migrations": len(REQUIRED_MIGRATIONS),
        },
    }


def materialize_runtime_configs(
    payload: dict[str, Any], target_dir: Path,
) -> dict[str, Any]:
    """Create task-local effective configs without changing frozen sources.

    Model and Python locations are deployment details.  Keeping these small
    effective copies beside a task allows the same generic scripts to run from
    an app bundle, an external model disk, or the development checkout.
    """
    output = Path(target_dir).expanduser().absolute()
    output.mkdir(parents=True, exist_ok=True)
    configs = dict(payload["configs"])

    def write_json(key: str, updates: dict[str, Any]) -> None:
        source = Path(configs[key])
        document = json.loads(source.read_text(encoding="utf-8"))
        document.update(updates)
        destination = output / source.name
        destination.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        configs[key] = str(destination)

    models = payload.get("models") or {}
    python = payload.get("python") or {}
    if "person_reid" in configs and "person_reid" in models:
        write_json("person_reid", {
            "model_dir": models["person_reid"]["path"],
        })
    if (
        {"qwen", "qwen_prompt"} <= configs.keys()
        and "qwen" in models and "qwen" in python
    ):
        qwen_root = Path(models["qwen"]["path"])
        write_json("qwen", {
            "model_path": str(qwen_root),
            "model_weight_file": str(qwen_root / "model.safetensors"),
            "qwen_python": python["qwen"],
            "prompt_path": configs["qwen_prompt"],
        })
    if (
        "ocr" in configs
        and {"ocr_detection", "ocr_recognition"} <= models.keys()
        and "ocr" in python
    ):
        ocr_root = Path(models["ocr_detection"]["path"]).parent
        write_json("ocr", {
            "ocr_python": python["ocr"],
            "model_root": str(ocr_root),
            "text_detection_model_dir": models["ocr_detection"]["path"],
            "text_recognition_model_dir": models["ocr_recognition"]["path"],
            "paddlex_cache_root": str(output / "paddlex_cache"),
        })
    if (
        "embedding_contract" in configs
        and "text_embedding" in models and "embedding" in python
    ):
        write_json("embedding_contract", {
            "model_path": models["text_embedding"]["path"],
            "python_path": python["embedding"],
        })
    return configs


def task_runtime_from_contract(
    payload: dict[str, Any], *, ocr_workers: int, embedding_workers: int,
    requested_scheduler_mode: str, effective_config_dir: Path | None = None,
) -> dict[str, Any]:
    # Keep venv entry paths verbatim. Resolving these symlinks would silently
    # replace the venv with its base Homebrew interpreter.
    configs = (
        materialize_runtime_configs(payload, effective_config_dir)
        if effective_config_dir is not None else payload["configs"]
    )
    return {
        "runtime_contract_version": payload["contract_version"],
        "runtime_contract_path": payload["contract_path"],
        "project_root": str(Path(payload["project_root"]).expanduser().absolute()),
        "python": {key: str(Path(value).expanduser().absolute()) for key, value in payload["python"].items()},
        "tools": {key: str(Path(value).expanduser().absolute()) for key, value in payload["tools"].items()},
        "models": {key: str(Path(item["path"]).expanduser().absolute()) for key, item in payload["models"].items()},
        "scripts": {key: str(Path(value).expanduser().absolute()) for key, value in payload["scripts"].items()},
        "configs": {key: str(Path(value).expanduser().absolute()) for key, value in configs.items()},
        "migrations": {key: str(Path(value).expanduser().absolute()) for key, value in payload["migrations"].items()},
        "policies": dict(payload["policies"]),
        "ocr_workers": int(ocr_workers),
        "embedding_workers": int(embedding_workers),
        "requested_scheduler_mode": requested_scheduler_mode,
        "effective_scheduler_mode": "stage_serial",
    }
