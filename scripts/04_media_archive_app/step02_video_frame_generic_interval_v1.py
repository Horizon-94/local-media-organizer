#!/usr/bin/env python3
"""Generic 1-5 second adapter for the frozen Step02 video-frame extractor.

The proven extractor remains unchanged.  This adapter selects a task-scoped
sampling contract before delegating to it, so frame ids, manifests, resume
state and database rows all record the interval actually selected by the user.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Sequence


ADAPTER_VERSION = "step02_video_frame_generic_interval_v1"
SUPPORTED_INTERVAL_SECONDS = (1, 2, 3, 4, 5)
AUDIO_COEXTRACT_CONTRACT = "media_archive_stage03_audio_coextract_v1"
AUDIO_FILENAME = "audio_16k_mono.flac"


def apply_runtime_safety_roots(module: Any) -> None:
    """Propagate roots injected by the generic stage wrapper to the late import."""
    output_root = globals().get("TEST_OUTPUT_ROOT")
    if output_root is not None:
        module.TEST_OUTPUT_ROOT = Path(output_root).expanduser().resolve()
    source_roots = globals().get("ALLOWED_SOURCE_ROOTS")
    if source_roots is not None and hasattr(module, "ALLOWED_SOURCE_ROOTS"):
        module.ALLOWED_SOURCE_ROOTS = {
            Path(root).expanduser().resolve() for root in source_roots
        }


def load_frozen_extractor(project_root: Path) -> Any:
    path = (
        project_root
        / "scripts/02_step01_step02_pipeline/"
        "step02_video_frame_c4s_from_db_safe_v7_20260709_183800.py"
    )
    spec = importlib.util.spec_from_file_location("step02_frozen_v7_generic_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"frozen_step02_import_failed:{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def configure_sampling_contract(module: Any, interval_seconds: int) -> dict[str, Any]:
    if interval_seconds not in SUPPORTED_INTERVAL_SECONDS:
        raise ValueError("frame_interval_seconds_must_be_one_of_1_2_3_4_5")
    interval_ms = interval_seconds * 1000
    module.SAMPLING_INTERVAL_MS = interval_ms
    module.SCRIPT_SCHEME = (
        "step02_video_frame_generic_interval_"
        f"{interval_ms}_offset{module.SAMPLING_OFFSET_MS}_jpg1280_v1"
    )
    module.SAMPLING_CONTRACT = module.sampling_contract()
    module.SAMPLING_CONTRACT["generic_interval_adapter_version"] = ADAPTER_VERSION
    module.SAMPLING_CONTRACT_ID = hashlib.sha256(
        json.dumps(
            module.SAMPLING_CONTRACT,
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:24]
    return dict(module.SAMPLING_CONTRACT)


def source_has_audio_stream(source: Path) -> bool:
    """Probe only the stream table; this does not scan or decode the media."""
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(source),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def configure_audio_coextraction(module: Any, enabled: bool) -> None:
    """Add a mono FLAC output to the proven frame command without editing it.

    The frame result remains authoritative.  Audio is optional, has its own
    durable manifest, and may never turn a successful frame extraction into a
    failed video checkpoint.
    """
    if not enabled:
        return

    original_build = module.build_ffmpeg_cmd
    original_single_build = module.build_single_frame_cmd
    original_attempt = module.run_single_decode_attempt
    original_process = module.process_one
    local = threading.local()
    manifest_lock = threading.Lock()

    def append_audio_output(command: list[str], output_dir: Path) -> list[str]:
        if not bool(getattr(local, "has_audio", False)):
            return command
        audio_path = output_dir / AUDIO_FILENAME
        tolerant_command = list(command)
        try:
            input_index = tolerant_command.index("-i")
        except ValueError:
            input_index = -1
        if input_index >= 0:
            tolerant_command[input_index:input_index] = [
                "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
                "-max_error_rate", "1.0",
            ]
        return [
            *tolerant_command,
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", "16000", "-c:a", "flac",
            "-compression_level", "5", str(audio_path),
        ]

    def build_ffmpeg_cmd(source: Path, pattern: Path, vf: str, decode_mode: str):
        return append_audio_output(
            original_build(source, pattern, vf, decode_mode), pattern.parent
        )

    def build_single_frame_cmd(
        source: Path, output_file: Path, time_ms: int, decode_mode: str
    ):
        return append_audio_output(
            original_single_build(source, output_file, time_ms, decode_mode),
            output_file.parent,
        )

    def run_single_decode_attempt(
        source: Path, output_dir: Path, pattern: Path, vf: str, decode_mode: str
    ):
        result = original_attempt(source, output_dir, pattern, vf, decode_mode)
        # An audio-output error must not invalidate frames that were otherwise
        # decoded.  Re-run frame-only only in this exceptional, proven case.
        if (
            bool(getattr(local, "has_audio", False))
            and not result.get("success")
            and int(result.get("valid_count") or 0) > 0
        ):
            setattr(local, "audio_error", str(result.get("stderr_tail") or ""))
            try:
                (output_dir / AUDIO_FILENAME).unlink(missing_ok=True)
            except OSError:
                pass
            module.clear_partial_frames(output_dir)
            local.has_audio = False
            try:
                frame_only = original_attempt(
                    source, output_dir, pattern, vf, decode_mode
                )
            finally:
                local.has_audio = True
            frame_only["audio_frame_only_retry"] = True
            return frame_only
        return result

    def process_one(
        job_no: int, total_jobs: int, source: Path, source_base: dict[str, Any],
        output_dir: Path, task_action: str,
    ):
        local.has_audio = source_has_audio_stream(source)
        local.audio_error = ""
        try:
            report, frame_rows, returned_source_base = original_process(
                job_no, total_jobs, source, source_base, output_dir, task_action
            )
        finally:
            has_audio = bool(getattr(local, "has_audio", False))

        audio_path = output_dir / AUDIO_FILENAME
        frame_ok = str(report.get("checkpoint_status") or "") == "completed"
        if frame_ok and has_audio and audio_path.is_file() and audio_path.stat().st_size > 0:
            audio_status = "ready"
            audio_bytes = audio_path.stat().st_size
        elif not has_audio:
            audio_status = "no_audio_stream"
            audio_bytes = 0
        elif not frame_ok:
            audio_status = "frame_failed_audio_not_committed"
            audio_bytes = 0
            try:
                audio_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            audio_status = "coextract_failed_frame_preserved"
            audio_bytes = 0

        row = {
            "contract": AUDIO_COEXTRACT_CONTRACT,
            "source_content_id": str(source_base.get("parent_source_content_id") or ""),
            "source_video_id": str(source_base.get("source_video_id") or ""),
            "source_path": str(source),
            "status": audio_status,
            "audio_path": str(audio_path) if audio_status == "ready" else "",
            "codec": "flac" if audio_status == "ready" else "",
            "sample_rate": 16000 if audio_status == "ready" else 0,
            "channels": 1 if audio_status == "ready" else 0,
            "size_bytes": audio_bytes,
            "error": str(getattr(local, "audio_error", ""))[-1500:],
        }
        report.update({
            "audio_coextract_contract": AUDIO_COEXTRACT_CONTRACT,
            "audio_coextract_status": audio_status,
            "audio_coextract_path": row["audio_path"],
            "audio_coextract_size_bytes": audio_bytes,
        })
        manifest = Path(module.OUT) / "audio_coextract_manifest.jsonl"
        with manifest_lock:
            with manifest.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return report, frame_rows, returned_source_base

    module.build_ffmpeg_cmd = build_ffmpeg_cmd
    module.build_single_frame_cmd = build_single_frame_cmd
    module.run_single_decode_attempt = run_single_decode_attempt
    module.process_one = process_one


def parse_adapter_args(argv: Sequence[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--frame-interval-seconds",
        type=int,
        choices=SUPPORTED_INTERVAL_SECONDS,
        required=True,
    )
    parser.add_argument("--coextract-audio", action="store_true")
    return parser.parse_known_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args, forwarded = parse_adapter_args(argv)
    project_root = Path(__file__).resolve().parents[2]
    frozen = load_frozen_extractor(project_root)
    apply_runtime_safety_roots(frozen)
    contract = configure_sampling_contract(frozen, args.frame_interval_seconds)
    configure_audio_coextraction(frozen, args.coextract_audio)
    print(
        json.dumps(
            {
                "adapter_version": ADAPTER_VERSION,
                "frame_interval_seconds": args.frame_interval_seconds,
                "sampling_contract_id": frozen.SAMPLING_CONTRACT_ID,
                "sampling_contract": contract,
                "audio_coextract_contract": (
                    AUDIO_COEXTRACT_CONTRACT if args.coextract_audio else None
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    previous = sys.argv
    try:
        sys.argv = [str(Path(frozen.__file__).resolve()), *forwarded]
        result = frozen.main()
        return int(result or 0)
    finally:
        sys.argv = previous


if __name__ == "__main__":
    raise SystemExit(main())
