import hashlib
import json
import subprocess
from pathlib import Path

from media_archive import app
from media_archive import config
from media_archive.preview.backends import TEST_COPY_JPG
from media_archive.scanner.light_scan import build_ffprobe_metadata_command


def _fake_ffprobe(command, timeout, **kwargs):
    assert command[0] == "ffprobe"
    assert "-show_frames" not in command
    assert "-show_packets" not in command
    assert "-count_frames" not in command
    assert "-count_packets" not in command
    payload = {
        "format": {
            "duration": "4.200000",
            "size": "12345",
            "bit_rate": "23514",
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30000/1001",
                "avg_frame_rate": "30000/1001",
                "duration": "4.200000",
                "pix_fmt": "yuv420p",
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "duration": "4.200000",
            },
        ],
    }
    return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _workspace_text(stage_dir: Path) -> str:
    parts = []
    for path in sorted(stage_dir.rglob("*")):
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts).lower()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_v01_light_scan_contract_outputs_and_forbidden_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_ffprobe)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("V0.1 must not use Popen")),
    )

    source = tmp_path / "中文 source（样本）" / "空格 目录"
    source.mkdir(parents=True)
    video = source / "单个 视频.MOV"
    video.write_bytes(b"fake video bytes")
    before = (video.stat().st_size, video.stat().st_mtime_ns)

    workspace = tmp_path / "workspace"
    assert app.main(["v01-scan", "--source", str(source), "--workspace", str(workspace), "--fresh"]) == 0

    stage_dir = workspace / "stages" / "v0.1"
    manifest_path = stage_dir / "manifests" / "V0.1_SCAN_MANIFEST.jsonl"
    audit_path = stage_dir / "reports" / "V0.1_FFMPEG_AUDIT.md"
    policy_path = stage_dir / "reports" / "V0.1_SCAN_POLICY_REPORT.md"
    timing_path = stage_dir / "reports" / "V0.1_PER_VIDEO_TIMING.csv"
    state_path = stage_dir / "state" / "V0.1_STAGE_STATE.json"

    assert manifest_path.exists()
    assert audit_path.exists()
    assert policy_path.exists()
    assert timing_path.exists()
    assert state_path.exists()

    records = _read_jsonl(manifest_path)
    assert len(records) == 1
    record = records[0]
    assert set(config.V01_MANIFEST_FIELDS) <= set(record)
    assert record["relative_path"] == "单个 视频.MOV"
    assert record["extension"] == ".mov"
    assert record["file_category"] == "video"
    assert record["media_type"] == "video"
    assert record["metadata_status"] == "success"
    assert record["duration"] == 4.2
    assert record["format_name"] == "mov,mp4,m4a,3gp,3g2,mj2"
    assert record["video_stream_count"] == 1
    assert record["audio_stream_count"] == 1
    assert record["width"] == 1920
    assert record["height"] == 1080
    assert "metadata_ok" in record["risk_flags"]
    assert "decode_validation_deferred" in record["risk_flags"]
    assert "deep_integrity_check_skipped_by_policy" in record["risk_flags"]
    assert record["next_stage_hint"] == "v02_video_frame_candidate"

    audit = audit_path.read_text(encoding="utf-8")
    assert "- record_count: 1" in audit
    assert "- metadata_probe: 1" in audit
    assert "- ordinary_video_integrity_check: 0" in audit
    assert "- ordinary_video_sample_decode: 0" in audit
    assert "- deep_integrity_check_skipped_by_policy: 1" in audit

    policy = policy_path.read_text(encoding="utf-8")
    assert "- scan_policy: light_scan_default" in policy
    assert "- metadata_probe_enabled: true" in policy
    assert "- deep_integrity_default_enabled: false" in policy
    assert "- sample_decode_default_enabled: false" in policy
    assert "- full_integrity_check_default_enabled: false" in policy
    assert "- ordinary_video_count: 1" in policy
    assert "- ordinary_video_integrity_check_default_count: 0" in policy
    assert "- ordinary_video_sample_decode_default_count: 0" in policy

    timing = timing_path.read_text(encoding="utf-8")
    assert "stage,purpose,source_path,source_size_bytes" in timing
    assert "metadata_probe" in timing

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "COMPLETED"
    assert state["record_count"] == 1
    assert state["metadata_probe"] == 1
    assert state["ordinary_video_integrity_check"] == 0
    assert state["ordinary_video_sample_decode"] == 0
    assert state["deep_integrity_check_skipped_by_policy"] == 1
    assert state["preview_generated"] is False
    assert state["audio_proxy_generated"] is False
    assert state["scene_detection_executed"] is False
    assert state["ai_model_called"] is False

    output_text = _workspace_text(stage_dir)
    for pattern in config.V01_FORBIDDEN_COMMAND_PATTERNS:
        assert pattern.lower() not in output_text
    assert ".jpg" not in output_text
    assert not list(stage_dir.rglob("*.jpg"))
    assert not (workspace / "speech").exists()
    assert not (workspace / "embeddings").exists()
    assert not (workspace / "search").exists()
    assert not (workspace / "index").exists()
    assert not (workspace / "database").exists()

    after = (video.stat().st_size, video.stat().st_mtime_ns)
    assert after == before


def test_run_entrypoint_starts_with_v01_and_stops_before_later_stages(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_ffprobe)
    source = tmp_path / "source"
    source.mkdir()
    (source / "clip.mov").write_bytes(b"video")
    workspace = tmp_path / "workspace"

    assert app.main(["run", "--source", str(source), "--workspace", str(workspace), "--fresh"]) == 0

    run_state = json.loads((workspace / "RUN_STATE.json").read_text(encoding="utf-8"))
    assert run_state["first_stage"] == "V0.1"
    assert run_state["execution_order"] == ["V0.1"]
    assert run_state["run_status"] == "STOPPED_AFTER_V0.1"
    assert run_state["complete_run_success"] is False
    assert run_state["stages"]["V0.1"]["status"] == "COMPLETED"
    for stage in ["V0.2", "V0.3", "V0.4", "V0.5"]:
        assert run_state["stages"][stage]["status"] == "not_run"
    assert (workspace / "stages" / "v0.1" / "manifests" / "V0.1_SCAN_MANIFEST.jsonl").exists()


def test_later_stage_entrypoints_do_not_pollute_v01_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_ffprobe)
    source = tmp_path / "source"
    source.mkdir()
    (source / "clip.mov").write_bytes(b"video")
    workspace = tmp_path / "workspace"
    assert app.main(["v01-scan", "--source", str(source), "--workspace", str(workspace), "--fresh"]) == 0

    manifest_path = workspace / "stages" / "v0.1" / "manifests" / "V0.1_SCAN_MANIFEST.jsonl"
    policy_path = workspace / "stages" / "v0.1" / "reports" / "V0.1_SCAN_POLICY_REPORT.md"
    before_hashes = {_path: _sha256(_path) for _path in [manifest_path, policy_path]}

    assert app.main(
        [
            "preview-images",
            "--source",
            str(source),
            "--output",
            str(workspace),
            "--preview-backend",
            TEST_COPY_JPG,
        ]
    ) == 0

    after_hashes = {_path: _sha256(_path) for _path in [manifest_path, policy_path]}
    assert after_hashes == before_hashes

    records = _read_jsonl(manifest_path)
    assert set(records[0]) == set(config.V01_MANIFEST_FIELDS)
    forbidden_later_fields = {
        "preview_path",
        "frame_path",
        "scene_score",
        "embedding",
        "search_index_id",
        "sample_decode_status",
        "integrity_check_status",
    }
    assert not (set(records[0]) & forbidden_later_fields)


def test_v01_ffprobe_command_contract():
    command = build_ffprobe_metadata_command(Path("/tmp/video with space.mov"))
    text = " ".join(command)
    assert command[:3] == ["ffprobe", "-v", "error"]
    assert "format=duration,size,bit_rate,format_name" in command
    assert "stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,duration,pix_fmt" in command
    for pattern in config.V01_FORBIDDEN_COMMAND_PATTERNS:
        assert pattern not in text.lower()
