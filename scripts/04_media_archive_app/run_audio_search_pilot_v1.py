#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
import types
import wave
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.media_archive_image_video_ui.audio_search_timeline import (  # noqa: E402
    TranscriptSegment,
    classify_vad_timeline,
    transcript_search_evidence,
    vad_sample_intervals_to_ms,
    whisper_clip_timestamps,
)
from apps.media_archive_image_video_ui.audio_enhancement import (  # noqa: E402
    enhance_with_deepfilter,
    source_stat_contract,
)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, check=False, text=True, capture_output=True
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout or "")[-4000:]
        raise RuntimeError(
            f"media_command_failed:exit={completed.returncode}:"
            f"command={json.dumps(command, ensure_ascii=False)}:"
            f"diagnostic={diagnostic}"
        )
    return completed


def extract_pcm_audio(
    ffmpeg: str | Path,
    media_input: Path,
    output: Path,
    *,
    sample_rate: int,
    tolerate_corrupt_source: bool,
) -> None:
    """Decode audio normally, then salvage valid packets from damaged AAC.

    The tolerant attempt is never treated as success merely because FFmpeg
    returned zero: the downstream PCM reader still validates channel count,
    sample width, rate, and a non-empty WAV container.
    """
    base = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(media_input), "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(output),
    ]
    try:
        run(base)
        return
    except RuntimeError as standard_error:
        if not tolerate_corrupt_source:
            raise
        output.unlink(missing_ok=True)
        tolerant = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
            "-max_error_rate", "1.0", "-i", str(media_input),
            "-map", "0:a:0?", "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-af", "aresample=async=1:first_pts=0",
            "-c:a", "pcm_s16le", str(output),
        ]
        try:
            run(tolerant)
        except RuntimeError as tolerant_error:
            raise RuntimeError(
                "audio_decode_fallback_failed:"
                f"standard={standard_error}:tolerant={tolerant_error}"
            ) from tolerant_error


def load_local_silero(package_root: Path) -> Any:
    """Load only the local JIT VAD path without requiring torchaudio.

    The pilot already decodes WAV data with the standard library, so Silero's
    optional torchaudio I/O helpers are neither used nor required here.
    """
    utils_path = package_root / "utils_vad.py"
    model_path = package_root / "data" / "silero_vad.jit"
    if not utils_path.is_file():
        raise FileNotFoundError(f"Silero utilities missing: {utils_path}")
    if not model_path.is_file():
        raise FileNotFoundError(f"Silero JIT model missing: {model_path}")
    spec = importlib.util.spec_from_file_location(
        "media_archive_local_silero_utils", utils_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot create Silero module spec")
    module = importlib.util.module_from_spec(spec)
    previous_torchaudio = sys.modules.get("torchaudio")
    if previous_torchaudio is None:
        torchaudio_stub = types.ModuleType("torchaudio")
        torchaudio_stub.__version__ = "0.0"
        sys.modules["torchaudio"] = torchaudio_stub
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_torchaudio is None:
            sys.modules.pop("torchaudio", None)
        else:
            sys.modules["torchaudio"] = previous_torchaudio
    return types.SimpleNamespace(
        load_silero_vad=lambda onnx=False: module.init_jit_model(str(model_path)),
        get_speech_timestamps=module.get_speech_timestamps,
    )


def read_pcm16_mono(path: Path) -> tuple[Any, int, int]:
    import numpy as np
    import torch

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sampling_rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if channels != 1 or sample_width != 2 or sampling_rate != 16_000:
        raise RuntimeError("audio extraction contract requires mono PCM16 at 16kHz")
    if frames <= 0 or not raw:
        raise RuntimeError("audio extraction produced no decodable PCM frames")
    audio = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    return torch.from_numpy(audio), sampling_rate, frames


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--audio-input", type=Path)
    parser.add_argument("--known-no-audio", action="store_true")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-content-id", default="")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="/opt/homebrew/bin/ffmpeg")
    parser.add_argument("--ffprobe", default="/opt/homebrew/bin/ffprobe")
    parser.add_argument("--silero-root", type=Path, required=True)
    parser.add_argument("--whisper-model", type=Path, required=True)
    parser.add_argument("--deep-filter-executable", type=Path)
    parser.add_argument("--deep-filter-model", type=Path)
    parser.add_argument(
        "--enhancement-failure-policy", choices=("fallback", "fail"), default="fallback"
    )
    parser.add_argument("--allow-no-audio", action="store_true")
    args = parser.parse_args()

    audio_input = args.audio_input.resolve(strict=True) if args.audio_input else None
    can_run_without_source = bool(audio_input or args.known_no_audio)
    video = args.video.expanduser().absolute()
    source_root = args.source_root.expanduser().absolute()
    try:
        video.relative_to(source_root)
    except ValueError as exc:
        raise RuntimeError("video must be inside the authorized source root") from exc
    if video.is_file():
        video = video.resolve(strict=True)
        source_root = source_root.resolve(strict=True)
        try:
            video.relative_to(source_root)
        except ValueError as exc:
            raise RuntimeError("video must be inside the authorized source root") from exc
        before = video.stat()
    elif can_run_without_source:
        before = None
    else:
        video.resolve(strict=True)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    wav_path = output / "audio_16k_mono.wav"
    started = time.time()

    if args.known_no_audio:
        audio_streams: list[dict[str, Any]] = []
    elif audio_input:
        audio_streams = [{"codec_type": "audio", "source": "stage03_coextract"}]
    else:
        probe = run([
            args.ffprobe, "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(video),
        ])
        probe_payload = json.loads(probe.stdout)
        audio_streams = [
            row for row in probe_payload.get("streams", [])
            if row.get("codec_type") == "audio"
        ]
    if not audio_streams:
        if not args.allow_no_audio:
            raise RuntimeError("source has no audio stream")
        result = {
            "contract": "media_archive_audio_search_pilot_v2",
            "status": "PASS", "source_read_only": True,
            "source_video": str(video),
            "source_available": before is not None,
            "source_stat": source_stat_contract(video) if before is not None else None,
            "processing_config": {
                "deep_filter_executable": (
                    str(args.deep_filter_executable.resolve())
                    if args.deep_filter_executable else None
                ),
                "deep_filter_model": (
                    str(args.deep_filter_model.resolve()) if args.deep_filter_model else None
                ),
                "enhancement_failure_policy": args.enhancement_failure_policy,
            },
            "duration_ms": 0, "audio_stream_count": 0,
            "audio_input_mode": "stage03_known_no_audio" if args.known_no_audio else "source_video",
            "vad_backend": "silero_vad_local_jit", "speech_enhancement": None,
            "speech_intervals": [], "audio_timeline": [],
            "whisper_model": str(args.whisper_model), "transcript_segment_count": 0,
            "transcript_text": "", "search_evidence": [],
            "non_speech_policy": "ignored_by_product_scope",
            "database_write": False, "embedding_write": False,
            "elapsed_seconds": round(time.time() - started, 3),
        }
        result_path = output / "audio_search_pilot.json"
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({
            "status": "PASS", "speech_intervals": 0, "transcript_segments": 0,
            "result": str(result_path),
        }, ensure_ascii=False))
        return 0
    enhancement: dict[str, object] | None = None
    if bool(args.deep_filter_executable) != bool(args.deep_filter_model):
        raise RuntimeError(
            "DeepFilterNet requires both --deep-filter-executable and --deep-filter-model"
        )
    media_input = audio_input or video
    if args.deep_filter_executable and args.deep_filter_model:
        raw_48k = output / "audio_48k_mono.wav"
        extract_pcm_audio(
            args.ffmpeg, media_input, raw_48k, sample_rate=48000,
            tolerate_corrupt_source=audio_input is None,
        )
        try:
            enhanced = enhance_with_deepfilter(
                executable=args.deep_filter_executable,
                model=args.deep_filter_model,
                input_wav=raw_48k,
                output_dir=output / "deepfilter",
            )
            enhancement = {"status": "APPLIED", **enhanced.as_dict()}
            run([
                args.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", enhanced.output_path, "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(wav_path),
            ])
        except Exception as exc:
            if args.enhancement_failure_policy == "fail":
                raise
            enhancement = {
                "status": "FALLBACK_ORIGINAL_AUDIO",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
            extract_pcm_audio(
                args.ffmpeg, media_input, wav_path, sample_rate=16000,
                tolerate_corrupt_source=audio_input is None,
            )
    else:
        extract_pcm_audio(
            args.ffmpeg, media_input, wav_path, sample_rate=16000,
            tolerate_corrupt_source=audio_input is None,
        )

    audio, sampling_rate, frame_count = read_pcm16_mono(wav_path)
    duration_ms = round(frame_count * 1000 / sampling_rate)
    silero = load_local_silero(args.silero_root.resolve(strict=True))
    vad_model = silero.load_silero_vad(onnx=False)
    sample_intervals = silero.get_speech_timestamps(
        audio, vad_model, sampling_rate=sampling_rate,
        min_speech_duration_ms=250, min_silence_duration_ms=300,
        speech_pad_ms=120,
    )
    speech_intervals = vad_sample_intervals_to_ms(
        sample_intervals, sampling_rate=sampling_rate
    )
    clips = whisper_clip_timestamps(speech_intervals)

    whisper_payload: dict[str, Any] = {"segments": [], "text": ""}
    if clips:
        from mlx_whisper.transcribe import transcribe

        whisper_payload = transcribe(
            str(wav_path), path_or_hf_repo=str(args.whisper_model.resolve(strict=True)),
            clip_timestamps=clips, word_timestamps=True, verbose=False,
            temperature=0.0, condition_on_previous_text=False,
        )

    evidence: list[dict[str, object]] = []
    source_id = str(args.source_content_id or video.name)
    for row in whisper_payload.get("segments") or []:
        text = str(row.get("text") or "").strip()
        start_ms = round(float(row.get("start") or 0.0) * 1000)
        end_ms = round(float(row.get("end") or 0.0) * 1000)
        if not text or end_ms <= start_ms:
            continue
        evidence.append(transcript_search_evidence(TranscriptSegment(
            source_content_id=source_id,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            text=text,
            language=str(whisper_payload.get("language") or "") or None,
        )))

    if before is not None:
        after = video.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("source read-only contract violated")
    result = {
        "contract": "media_archive_audio_search_pilot_v2",
        "status": "PASS",
        "source_read_only": True,
        "source_video": str(video),
        "source_available": before is not None,
        "source_stat": source_stat_contract(video) if before is not None else None,
        "audio_input_mode": "stage03_coextract" if audio_input else "source_video",
        "audio_input_path": str(audio_input) if audio_input else None,
        "processing_config": {
            "deep_filter_executable": (
                str(args.deep_filter_executable.resolve())
                if args.deep_filter_executable else None
            ),
            "deep_filter_model": (
                str(args.deep_filter_model.resolve()) if args.deep_filter_model else None
            ),
            "enhancement_failure_policy": args.enhancement_failure_policy,
        },
        "duration_ms": duration_ms,
        "audio_stream_count": len(audio_streams),
        "vad_backend": "silero_vad_local_jit",
        "speech_enhancement": enhancement,
        "speech_intervals": speech_intervals,
        "audio_timeline": classify_vad_timeline(duration_ms, speech_intervals),
        "whisper_model": str(args.whisper_model),
        "transcript_segment_count": len(evidence),
        "transcript_text": str(whisper_payload.get("text") or "").strip(),
        "search_evidence": evidence,
        "non_speech_policy": "ignored_by_product_scope",
        "database_write": False,
        "embedding_write": False,
        "elapsed_seconds": round(time.time() - started, 3),
    }
    (output / "audio_search_pilot.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "status": result["status"],
        "speech_intervals": len(speech_intervals),
        "transcript_segments": len(evidence),
        "result": str(output / "audio_search_pilot.json"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
