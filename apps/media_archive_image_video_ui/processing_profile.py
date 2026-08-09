from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


PROFILE_CONTRACT = "media_archive_processing_profile_v1"
SCHEDULER_MODES = {"auto", "pipeline_async", "stage_serial"}
HIGH_VALUE_MODES = {"frozen_v25_compatible", "target_15", "target_20", "target_30"}
IMAGE_SCOPES = {"frozen_current_policy", "all_images"}
VIDEO_FRAME_INTERVALS = {1.0, 2.0, 3.0, 4.0, 5.0}


def _system_profiler(data_type: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["/usr/sbin/system_profiler", data_type, "-json"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=15,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def detect_hardware() -> dict[str, Any]:
    hardware_rows = _system_profiler("SPHardwareDataType").get("SPHardwareDataType", [])
    display_rows = _system_profiler("SPDisplaysDataType").get("SPDisplaysDataType", [])
    hardware = hardware_rows[0] if hardware_rows else {}
    gpu = display_rows[0] if display_rows else {}
    processor_text = str(hardware.get("number_processors", ""))
    processor_values = [int(value) for value in re.findall(r"\d+", processor_text)]
    total_cpu = processor_values[0] if processor_values else int(os.cpu_count() or 1)
    performance_cpu = processor_values[1] if len(processor_values) > 1 else None
    efficiency_cpu = processor_values[2] if len(processor_values) > 2 else None
    memory_text = str(hardware.get("physical_memory", ""))
    memory_match = re.search(r"([\d.]+)\s*GB", memory_text, re.IGNORECASE)
    memory_gb = float(memory_match.group(1)) if memory_match else None
    try:
        gpu_cores = int(str(gpu.get("sppci_cores", "")))
    except ValueError:
        gpu_cores = None
    observation = {
        "chip": str(hardware.get("chip_type") or gpu.get("sppci_model") or "未知"),
        "machine_family": str(hardware.get("machine_name") or "Mac"),
        "cpu_cores_total": total_cpu,
        "cpu_performance_cores": performance_cpu,
        "cpu_efficiency_cores": efficiency_cpu,
        "gpu_name": str(gpu.get("sppci_model") or gpu.get("_name") or "未知"),
        "gpu_cores": gpu_cores,
        "unified_memory_gb": memory_gb,
    }
    observation["recommendation"] = recommend_workers(observation)
    return observation


def recommend_workers(hardware: dict[str, Any]) -> dict[str, int]:
    cpu = max(1, int(hardware.get("cpu_cores_total") or os.cpu_count() or 1))
    memory = float(hardware.get("unified_memory_gb") or 16)
    # Heavy workers are constrained primarily by unified memory.  This is a
    # conservative starting point; runtime telemetry may lower, never silently
    # raise, the active count.
    memory_heavy = max(1, int(max(0.0, memory - 8.0) // 7.0))
    model_workers = max(1, min(4, cpu // 2, memory_heavy))
    # The estimate is deliberately separate from the default.  More workers
    # can fit in memory without necessarily improving Metal throughput.  The
    # UI therefore starts conservatively and exposes this value only as an
    # adjustable ceiling for a user-run benchmark on that Mac.
    estimated_model_max = max(model_workers, min(8, cpu, int(max(0.0, memory - 8.0) // 5.0)))
    ocr_workers = max(1, min(6, cpu // 3 or 1))
    return {
        "model_workers": model_workers,
        "estimated_max_model_workers": estimated_model_max,
        "ocr_workers": ocr_workers,
        "estimated_max_ocr_workers": max(ocr_workers, min(12, cpu, int(max(1.0, memory // 3.0)))),
        "embedding_workers": max(1, min(4, model_workers)),
        "frame_extract_workers": max(1, min(8, cpu // 2 or 1)),
        "io_workers": max(2, min(12, cpu)),
    }


def build_processing_profile(
    hardware: dict[str, Any],
    *,
    scheduler_mode: str,
    model_workers: int,
    frame_extract_workers: int,
    video_frame_interval_seconds: float,
    high_value_mode: str,
    image_scope: str,
) -> dict[str, Any]:
    if scheduler_mode not in SCHEDULER_MODES:
        raise ValueError("unsupported scheduler mode")
    if high_value_mode not in HIGH_VALUE_MODES:
        raise ValueError("unsupported high-value mode")
    if image_scope not in IMAGE_SCOPES:
        raise ValueError("unsupported image scope")
    if not 1 <= int(model_workers) <= 8:
        raise ValueError("model workers must be between 1 and 8")
    if not 1 <= int(frame_extract_workers) <= 16:
        raise ValueError("frame extract workers must be between 1 and 16")
    interval = float(video_frame_interval_seconds)
    if interval not in VIDEO_FRAME_INTERVALS:
        raise ValueError("video frame interval must be one of 1, 2, 3, 4, 5 seconds")
    target_map = {
        "frozen_v25_compatible": None,
        "target_15": 0.15,
        "target_20": 0.20,
        "target_30": 0.30,
    }
    profile = {
        "contract_version": PROFILE_CONTRACT,
        "hardware_observation": hardware,
        "scheduler": {
            "mode": scheduler_mode,
            "event_driven_database_handoff": scheduler_mode == "pipeline_async",
            "model_workers": int(model_workers),
            "frame_extract_workers": int(frame_extract_workers),
            "runtime_may_reduce_workers_for_memory_pressure": True,
            "runtime_may_silently_increase_workers": False,
        },
        "video_sampling": {
            "frame_interval_seconds": interval,
            "current_frozen_default_seconds": 3.0,
            "supported_intervals_seconds": sorted(VIDEO_FRAME_INTERVALS),
            "generic_interval_contract_version": "step02_video_frame_generic_interval_v1",
            "requires_new_generic_step02_contract_when_changed": False,
            "effective_in_current_pipeline": True,
        },
        "high_value_policy": {
            "mode": high_value_mode,
            "target_ratio": target_map[high_value_mode],
            "current_frozen_v25_unchanged": True,
            "generic_density_contract_version": "stop03_2_generic_density_policy_v1",
            "supported_target_ratios": [0.15, 0.20, 0.30],
            "requires_new_candidate_policy_version": False,
            "density_effective_in_current_pipeline": True,
            "image_scope": image_scope,
            "image_scope_effective_in_current_pipeline": True,
            "all_images_high_value_cost_warning": image_scope == "all_images",
        },
        "model_update_gate": {
            "required_steps": [
                "register_model_inventory_and_fingerprint",
                "offline_small_smoke",
                "compare_with_current_frozen_model",
                "human_review",
                "explicit_activation",
            ],
            "automatic_model_replacement": False,
            "network_download_from_app": False,
        },
        "activation_status": "READY_FOR_GENERIC_PIPELINE",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    semantic = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    profile["profile_id"] = "profile_" + hashlib.sha256(semantic.encode("utf-8")).hexdigest()[:24]
    return profile


def save_processing_profile(output_root: Path, profile: dict[str, Any]) -> Path:
    directory = Path(output_root).expanduser().resolve() / "profiles"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "processing_profile_v1.json"
    temporary = directory / f".{target.name}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target
