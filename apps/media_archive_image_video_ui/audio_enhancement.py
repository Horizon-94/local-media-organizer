from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class AudioEnhancementResult:
    backend: str
    input_path: str
    output_path: str
    model_path: str
    command: list[str]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def deepfilter_command(
    *,
    executable: Path,
    model: Path,
    input_wav: Path,
    output_dir: Path,
) -> list[str]:
    """Build the offline, Apple-Silicon DeepFilterNet invocation.

    DeepFilterNet's native executable requires 48 kHz WAV input. Sampling-rate
    conversion belongs to the caller so this adapter never reads source video.
    """
    return [
        str(executable),
        "--model",
        str(model),
        "--output-dir",
        str(output_dir),
        "--compensate-delay",
        str(input_wav),
    ]


def _valid_wav_files(output_dir: Path) -> list[Path]:
    return sorted(
        path.resolve()
        for path in output_dir.glob("*.wav")
        if path.is_file() and path.stat().st_size > 44
    )


def source_stat_contract(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def reusable_audio_pilot_report(
    report: Mapping[str, object],
    *,
    video: Path,
    deep_filter_executable: Path | None,
    deep_filter_model: Path | None,
    enhancement_failure_policy: str,
) -> bool:
    """Return true only for a complete result made from the same source/config."""
    expected_config = {
        "deep_filter_executable": (
            str(deep_filter_executable.resolve()) if deep_filter_executable else None
        ),
        "deep_filter_model": str(deep_filter_model.resolve()) if deep_filter_model else None,
        "enhancement_failure_policy": enhancement_failure_policy,
    }
    return bool(
        report.get("contract") == "media_archive_audio_search_pilot_v2"
        and report.get("status") == "PASS"
        and report.get("source_read_only") is True
        and report.get("source_video") == str(video.resolve())
        and report.get("source_stat") == source_stat_contract(video)
        and report.get("processing_config") == expected_config
    )


def enhance_with_deepfilter(
    *,
    executable: Path,
    model: Path,
    input_wav: Path,
    output_dir: Path,
    runner: Runner = subprocess.run,
) -> AudioEnhancementResult:
    executable = executable.resolve(strict=True)
    model = model.resolve(strict=True)
    input_wav = input_wav.resolve(strict=True)
    if input_wav.suffix.lower() != ".wav":
        raise ValueError("DeepFilterNet input must be a WAV file")
    if not model.name.endswith(".tar.gz"):
        raise ValueError("DeepFilterNet native model must be a .tar.gz file")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = deepfilter_command(
        executable=executable,
        model=model,
        input_wav=input_wav,
        output_dir=output_dir,
    )
    runner(command, check=True, text=True, capture_output=True)
    outputs = _valid_wav_files(output_dir)
    if len(outputs) != 1:
        raise RuntimeError(
            "DeepFilterNet must create exactly one non-empty WAV; "
            f"outputs={len(outputs)} output_dir={output_dir}"
        )
    return AudioEnhancementResult(
        backend="deepfilternet3_native_arm64",
        input_path=str(input_wav),
        output_path=str(outputs[0]),
        model_path=str(model),
        command=command,
    )
