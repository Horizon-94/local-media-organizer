from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]


def _builder_namespace() -> dict:
    path = ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v128.py"
    spec = importlib.util.spec_from_file_location("v128_builder_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.builder_namespace()


def test_v128_has_distinct_release_identity() -> None:
    namespace = _builder_namespace()
    assert namespace["APP_RELEASE_NAME"] == "本地数据库 1.2.8"
    assert namespace["APP_BUNDLE_VERSION"] == "1.2.8"
    assert namespace["APP_SEMVER"] == "1.2.8"
    assert namespace["APP_BUILD_NUMBER"] == "128"


def test_interview_detection_contains_no_project_specific_rule() -> None:
    source = (
        ROOT / "apps/media_archive_image_video_ui_v125_candidate/repository.py"
    ).read_text(encoding="utf-8")
    section = source.split("def _spoken_content_classification", 1)[1].split(
        "def interview_content_catalog", 1
    )[0]
    for forbidden in ("麦子", "当下3年", "采访整理", "A7M4", "/Users/horizon"):
        assert forbidden not in section


def test_v128_ui_exposes_detection_and_complete_fallback() -> None:
    source = (
        ROOT / "apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift"
    ).read_text(encoding="utf-8")
    assert "自动识别采访" in source
    assert "长口播 / 长对话" in source
    assert "查看全部有转写视频" in source
    assert "判断依据：" in source
