from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v1.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("release_privacy_builder", BUILDER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_portable_release_has_fail_closed_developer_path_audit() -> None:
    source = BUILDER_PATH.read_text(encoding="utf-8")
    assert "sanitize_embedded_project_files" in source
    assert "embedded_developer_private_path_detected" in source
    assert '"developer_private_paths": privacy_audit' in source
    assert '"runtime_developer_private_paths": runtime_privacy_audit' in source


def test_privacy_sanitizer_removes_project_model_env_and_historical_paths() -> None:
    builder = load_builder()
    with tempfile.TemporaryDirectory() as temporary:
        pipeline = Path(temporary) / "Pipeline"
        document = pipeline / "configs/runtime.json"
        document.parent.mkdir(parents=True)
        project = Path.home() / "Documents/AI-Local/media-archive-clean"
        document.write_text(json.dumps({
            "project": str(project),
            "model": str(Path.home() / "Documents/model/example"),
            "python": str(Path.home() / "Documents/AI-Local/envs/example/bin/python"),
            "old_fixture": str(Path.home() / "Desktop/example"),
        }), encoding="utf-8")
        report = builder.sanitize_embedded_project_files(
            pipeline, project_root=project,
        )
        assert report["status"] == "PASS"
        assert report["remaining_violation_count"] == 0
        assert str(Path.home()) not in document.read_text(encoding="utf-8")


def test_privacy_sanitizer_cleans_python_distribution_record_metadata() -> None:
    builder = load_builder()
    with tempfile.TemporaryDirectory() as temporary:
        pipeline_envs = Path(temporary) / "PipelineEnvs"
        record = pipeline_envs / "example-1.0.dist-info" / "RECORD"
        record.parent.mkdir(parents=True)
        record.write_text(
            f"{Path.home()}/Library/Caches/example.pyc,,\nexample.py,,\n",
            encoding="utf-8",
        )
        report = builder.sanitize_embedded_project_files(
            pipeline_envs,
            project_root=Path.home() / "Documents/AI-Local/media-archive-clean",
        )
        assert report["status"] == "PASS"
        assert report["sanitized_file_count"] == 1
        assert str(Path.home()) not in record.read_text(encoding="utf-8")


def test_release_source_identity_changes_when_shipped_pipeline_script_changes() -> None:
    builder = load_builder()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for directory in (
            "apps/media_archive_image_video_ui", "scripts", "configs",
            "migrations", "tools", "docs/pipeline_rules", "docs/model_registry",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        shipped = root / "scripts/search.py"
        shipped.write_text("VERSION = 1\n", encoding="utf-8")
        first = builder._source_identity(root)
        shipped.write_text("VERSION = 2\n", encoding="utf-8")
        second = builder._source_identity(root)
        assert first["content_sha256"] != second["content_sha256"]
        assert first["file_count"] == second["file_count"] == 1
