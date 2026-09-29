"""Uniform production-stage adapter descriptors for host calibration.

This module deliberately does not provide toy model calls.  Each descriptor
points at the 1.2.8 production entrypoint and builds the same command shape
used by that entrypoint.  A descriptor is not considered READY until a host
smoke result is persisted by the coordinator.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
import json
import os


@dataclass(frozen=True)
class AdapterSpec:
    adapter_id: str
    runtime: Path
    entrypoint: Path
    model: Path
    supports_workers: bool
    supports_persistent: bool

    def build_command(self, *, db: Path, out: Path, workers: int, mode: str = "run", config: Path | None = None) -> list[str]:
        """Build a formal-stage command; never substitute a toy workload."""
        common = [str(self.runtime), str(self.entrypoint), "--db", str(db), "--out", str(out)]
        if self.adapter_id == "yoloe":
            return common + ["--model", str(self.model), "--device", "mps", "--limit", "5", "--concurrency", str(workers)]
        if self.adapter_id == "openclip":
            return common + ["--model", str(self.model), "--model-name", "ViT-B-32", "--device", "mps", "--workers", str(workers), "--limit", "5"]
        if self.adapter_id == "ocr":
            args = common + ["--mode", mode, "--workers", str(workers), "--limit", "5"]
            return args + (["--config", str(config)] if config else [])
        if self.adapter_id == "embedding":
            args = common + ["--mode", mode, "--model", str(self.model), "--workers", str(workers), "--max-attempts", "1"]
            return args + (["--runtime-config", str(config)] if config else [])
        if self.adapter_id == "person-reid":
            return common + ["--mode", mode, "--workers", str(workers), "--limit", "5", "--backend", "opencv"]
        if self.adapter_id == "whisper":
            # The audio stage has a larger formal contract; callers must pass
            # its resolved audio/embedding dependencies in config.  Refuse to
            # invent paths rather than silently running a different workload.
            if config is None:
                raise ValueError("whisper_adapter_requires_resolved_runtime_config")
            return common + ["--workers", str(workers), "--limit", "5", "--config", str(config)]
        if self.adapter_id == "ffmpeg":
            # Stage03 uses one process per video task.  workers therefore maps
            # to the coordinator's task pool, not ffmpeg -threads.
            return common + ["--offset", "2", "--interval", "3", "--long-edge", "1280", "--qv", "3", "--workers", str(workers)]
        raise ValueError(f"unsupported_formal_adapter:{self.adapter_id}")


def adapter_contract_complete(spec: AdapterSpec) -> bool:
    return all((spec.runtime.is_file(), spec.entrypoint.is_file(), spec.model.exists()))


def materialize_config(source: Path, target: Path, *, task_runtime: Path) -> Path:
    """Resolve the placeholders used by packaged 1.2.8 JSON contracts.

    Production configs intentionally keep machine-specific roots symbolic. A
    benchmark copy must resolve them into its isolated output tree; editing
    the installed config in-place is forbidden.
    """
    replacements = {
        "$MODEL_ROOT": str(Path(os.environ.get("MEDIA_ARCHIVE_MODEL_ROOT", "/Users/horizon/Documents/model")).resolve()),
        "$TASK_RUNTIME": str(task_runtime.resolve()),
        "$BUNDLED_PIPELINE_ENVS": str(Path("/Applications/本地数据库 1.2.8.app/Contents/Helpers/PipelinePython").resolve()),
        "$APP_ROOT": "/Applications/本地数据库 1.2.8.app",
    }
    raw = json.loads(source.read_text(encoding="utf-8"))
    def resolve(value: Any) -> Any:
        if isinstance(value, str):
            for key, replacement in replacements.items():
                value = value.replace(key, replacement)
            return value
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        return value
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(resolve(raw), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
