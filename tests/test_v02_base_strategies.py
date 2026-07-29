import importlib
import json
import py_compile
from pathlib import Path

from media_archive import app
from media_archive.strategies import a9t_v3_generic, video_frame_c4_generic, video_frame_c6_pressure
from media_archive.video.runners import build_ffmpeg_command


ROOT = Path(__file__).resolve().parents[1]


def test_strategy_modules_import_and_py_compile_without_running(tmp_path):
    before = set(tmp_path.iterdir())
    modules = [
        "media_archive.strategies.a9t_v3_generic",
        "media_archive.strategies.video_frame_c4_generic",
        "media_archive.strategies.video_frame_c6_pressure",
    ]
    for module_name in modules:
        importlib.import_module(module_name)
    assert set(tmp_path.iterdir()) == before

    for relative_path in [
        "apps/media_archive/strategies/a9t_v3_generic.py",
        "apps/media_archive/strategies/video_frame_c4_generic.py",
        "apps/media_archive/strategies/video_frame_c6_pressure.py",
    ]:
        py_compile.compile(
            str(ROOT / relative_path),
            cfile=str(tmp_path / (Path(relative_path).name + ".pyc")),
            doraise=True,
        )


def test_a9t_v3_base_strategy_contract():
    contract = a9t_v3_generic.strategy_contract()
    assert contract["image_preview_strategy"] == "A9T-v3"
    assert contract["max_edge_px"] == 1280
    assert contract["sips_workers"] == 8
    assert contract["system_workers"] == 8
    assert contract["min_timelapse_count"] == 60
    assert contract["min_interval_seconds"] == 1.5
    assert contract["max_interval_seconds"] == 10.0
    assert contract["time_gap_split_seconds"] == 30.0
    assert contract["valid_interval_ratio_required"] == 0.85
    assert contract["numeric_monotonic_ratio_required"] == 0.90
    assert contract["representative_positions"] == ["first", "middle", "last"]
    assert ".jpg" in contract["sips_exts"]
    assert ".arw" in contract["system_exts"]
    source = (ROOT / "apps/media_archive/strategies/a9t_v3_generic.py").read_text(encoding="utf-8")
    assert "/Volumes/example-private-source" not in source
    assert "test-output/a9t-v3-generic-image-preview" not in source
    assert "shutil.rmtree(output_dir" not in source


def test_c4_and_c6_base_strategy_contracts():
    c4 = video_frame_c4_generic.strategy_contract()
    c6 = video_frame_c6_pressure.strategy_contract()
    assert c4["default_video_base"] == "C4"
    assert c4["concurrency"] == 4
    assert c4["sampling_offset_ms"] == 1000
    assert c4["sampling_interval_ms"] == 2000
    assert c4["decode_mode"] == "videotoolbox"
    assert c4["fallback_enabled"] is False
    assert c4["showinfo_enabled"] is False
    assert video_frame_c4_generic.estimate_expected_count(0.5) == 0
    assert video_frame_c4_generic.estimate_expected_count(1.0) == 1
    assert video_frame_c4_generic.estimate_expected_count(3.0) == 2
    assert video_frame_c4_generic.estimated_frame_time_ms(0) == 1000
    assert video_frame_c4_generic.estimated_frame_time_ms(1) == 3000

    assert c6["concurrency"] == 6
    assert c6["pressure_only_mode"] is True
    assert c6["default_enabled"] is False
    assert c6["sampling_offset_ms"] == c4["sampling_offset_ms"]
    assert c6["sampling_interval_ms"] == c4["sampling_interval_ms"]
    assert c6["fallback_enabled"] is False
    assert c6["showinfo_enabled"] is False

    command_text = " ".join(build_ffmpeg_command(Path("/tmp/in.mov"), Path("/tmp/out_%06d.jpg")))
    assert "-hwaccel videotoolbox" in command_text
    assert "fps=1/2.0:start_time=1.0" in command_text
    assert "showinfo" not in command_text
    assert "fallback" not in command_text


def test_cli_defaults_select_a9t_and_c4_not_c6(monkeypatch, tmp_path):
    calls = []

    def fake_a9t(source, output, preview_backend="auto", **kwargs):
        calls.append(("a9t", Path(source), Path(output), preview_backend))
        return {"image_preview_strategy": "A9T-v3"}

    def fake_c4(source, output, video_runner="ffmpeg_videotoolbox_jpg", fake_frame_count=3, **kwargs):
        calls.append(("c4", Path(source), Path(output), video_runner, fake_frame_count))
        return {"video_frame_strategy": "R2J-FIX-C4"}

    monkeypatch.setattr(app, "run_a9t_v3_image_preview", fake_a9t)
    monkeypatch.setattr(app, "run_video_frames_c4", fake_c4)
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()

    assert app.main(["preview-images", "--source", str(source), "--output", str(output)]) == 0
    assert app.main(["extract-video-frames", "--source", str(source), "--output", str(output)]) == 0
    assert calls[0][0] == "a9t"
    assert calls[0][3] == "auto"
    assert calls[1][0] == "c4"
    assert calls[1][3] == "ffmpeg_videotoolbox_jpg"
    assert not any(call[0] == "c6" for call in calls)


def test_build_v02_and_real_minimal_use_a9t_plus_c4(monkeypatch, tmp_path):
    import media_archive.workflows.v02_build as v02_build
    import media_archive.validation.real_minimal_validation as real_minimal

    calls = []

    def fake_a9t(source, output, preview_backend="auto", **kwargs):
        calls.append(("a9t", Path(source), Path(output), preview_backend))
        image_preview = Path(output) / "image_preview"
        image_preview.mkdir(parents=True, exist_ok=True)
        (image_preview / "all_image_decisions.jsonl").write_text("", encoding="utf-8")
        (image_preview / "preview_manifest.jsonl").write_text("", encoding="utf-8")
        (image_preview / "image_preview_summary.json").write_text(json.dumps({"image_preview_strategy": "A9T-v3"}), encoding="utf-8")
        return {"image_preview_strategy": "A9T-v3"}

    def fake_c4(source, output, video_runner="ffmpeg_videotoolbox_jpg", fake_frame_count=3, **kwargs):
        calls.append(("c4", Path(source), Path(output), video_runner))
        video_frames = Path(output) / "video_frames"
        video_frames.mkdir(parents=True, exist_ok=True)
        (video_frames / "video_frame_manifest.jsonl").write_text("", encoding="utf-8")
        (video_frames / "video_frame_summary.json").write_text(json.dumps({"video_frame_strategy": "R2J-FIX-C4"}), encoding="utf-8")
        return {"video_frame_strategy": "R2J-FIX-C4"}

    monkeypatch.setattr(v02_build, "run_a9t_v3_image_preview", fake_a9t)
    monkeypatch.setattr(v02_build, "run_video_frames_c4", fake_c4)
    monkeypatch.setattr(real_minimal, "run_a9t_v3_image_preview", fake_a9t)
    monkeypatch.setattr(real_minimal, "run_video_frames_c4", fake_c4)
    monkeypatch.setattr(v02_build, "run_quality_records", lambda *args, **kwargs: {})
    monkeypatch.setattr(v02_build, "build_unified_manifest", lambda *args, **kwargs: {})
    monkeypatch.setattr(v02_build, "validate_v02_combo", lambda *args, **kwargs: {"validation_status": "PASS"})
    monkeypatch.setattr(real_minimal, "_profile_or_blocked", lambda *args, **kwargs: {"ok": True, "profile_name": "small_expected", "expected": {
        "image_files_total": 0,
        "final_preview_count": 0,
        "video_files_total": 0,
        "total_produced_frame_count": 0,
        "timelapse_sequence_count": 0,
        "timelapse_total_image_count": 0,
        "timelapse_keyframe_count": 0,
        "normal_image_count": 0,
        "failed_count": 0,
    }})
    monkeypatch.setattr(real_minimal, "_validate_image_outputs", lambda *args, **kwargs: {"status": "PASS", "summary_keys": [], "failures": [], "blocked": [], "metrics": {
        "actual_source_image_count": 0,
        "actual_high_confidence_timelapse_sequence_count": 0,
        "actual_timelapse_total_image_count": 0,
        "actual_timelapse_keyframe_count": 0,
        "actual_normal_image_count": 0,
        "actual_final_preview_count": 0,
        "actual_preview_reduction_count": 0,
        "actual_preview_reduction_ratio": 0,
        "actual_preview_reduction_ratio_formula": "preview_reduction_count / source_image_count",
        "actual_failed_count": 0,
        "image_frozen_match_source_image_count": True,
        "image_frozen_match_timelapse_sequence_count": True,
        "image_frozen_match_timelapse_total_image_count": True,
        "image_frozen_match_timelapse_keyframe_count": True,
        "image_frozen_match_normal_image_count": True,
        "image_frozen_match_final_preview_count": True,
        "image_frozen_match_preview_reduction_count": True,
        "image_internal_normal_plus_timelapse_equals_source": True,
        "image_internal_normal_plus_keyframes_equals_preview": True,
        "image_internal_reduction_count_ok": True,
        "image_count_match_final_preview": None,
        "image_count_match_timelapse_sequences": None,
        "image_count_match_timelapse_total": None,
        "image_count_match_keyframes": None,
        "image_count_match_normal": None,
        "image_count_match_reduction": None,
    }})
    monkeypatch.setattr(real_minimal, "_validate_video_outputs", lambda *args, **kwargs: {"status": "PASS", "summary_keys": [], "failures": [], "blocked": [], "metrics": {
        "actual_video_files_total": 0,
        "actual_total_produced_frame_count": 0,
        "actual_success_video_count": 0,
        "actual_failed_video_count": 0,
        "actual_decode_mode": "videotoolbox",
        "actual_concurrency": 4,
        "actual_invalid_jpg1280_frame_count": 0,
        "actual_non_jpg_frame_count": 0,
        "actual_frame_dimension_checked_count": 0,
        "actual_max_frame_edge_px": 1280,
        "actual_vs_derived_frame_count_match": None,
        "actual_vs_frozen_frame_count_match": True,
        "video_frozen_match_video_files_total": True,
        "video_frozen_match_total_produced_frame_count": True,
        "video_frozen_match_success_video_count": True,
        "video_frozen_match_decode_mode": True,
        "video_frozen_match_concurrency": True,
        "video_internal_valid_frame_count_equals_manifest_count": True,
        "video_count_match_files": None,
        "video_count_match_frames": None,
        "video_count_match_success": None,
        "video_decode_mode_match": None,
        "video_concurrency_match": None,
        "video_all_frames_valid_jpg1280": True,
    }})

    source = tmp_path / "source"
    source.mkdir()
    (source / "probe.mov").write_bytes(b"probe")
    v02_build.build_v02(source, tmp_path / "build")
    real_minimal.validate_real_minimal_v02(
        str(source),
        str(source),
        tmp_path / "real",
        False,
        "auto",
        "ffmpeg_videotoolbox_jpg",
        {
                "expected_image_count": 1,
            "expected_preview_count": 0,
            "expected_video_count": 0,
            "expected_frame_count": 0,
            "expected_timelapse_sequence_count": 0,
            "expected_timelapse_total_image_count": 0,
            "expected_timelapse_keyframe_count": 0,
            "expected_normal_image_count": 0,
        },
    )

    assert [call[0] for call in calls].count("a9t") == 2
    assert [call[0] for call in calls].count("c4") == 2
    assert not any(call[0] == "c6" for call in calls)


def test_base_strategy_documents_exist_and_keep_v02_at_a9t_c4():
    required = [
        "docs/frozen_terminal_commands/generic/A9T-v3-generic.py",
        "docs/frozen_terminal_commands/generic/video_frame_c4_generic.py",
        "docs/frozen_terminal_commands/generic/video_frame_c6_generic.py",
        "docs/frozen_terminal_commands/V0.2_BASE_STRATEGY.md",
    ]
    for relative_path in required:
        assert (ROOT / relative_path).exists()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    baseline = (ROOT / "PROJECT_BASELINE.md").read_text(encoding="utf-8")
    assert "image = A9T-v3 only" in readme
    assert "video default = C4" in readme
    assert "C6 = pressure-only" in readme
    assert "V0.2 底座已固定为 A9T-v3 + C4" in baseline
    assert "C6 已保留为 pressure-only" in baseline
    assert "V0.3 尚未开始" in baseline
