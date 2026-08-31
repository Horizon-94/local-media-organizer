from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_portable_search_uses_bundled_python_wrappers() -> None:
    source = (
        ROOT / "scripts/04_media_archive_app/build_native_image_video_app_v127.py"
    ).read_text(encoding="utf-8")
    assert '"$APP_CONTENTS/Helpers/PipelinePython/embedding"' in source
    assert '"$APP_CONTENTS/Helpers/PipelinePython/visual"' in source
    assert "if bundle_pipeline_runtimes else" in source


def test_dmg_instructions_describe_model_setup_and_restart() -> None:
    source = (
        ROOT
        / "scripts/04_media_archive_app/build_native_image_video_app_v124_candidate.py"
    ).read_text(encoding="utf-8")
    assert "不包含第三方模型" in source
    assert "设置 → 本地模型位置" in source
    assert "退出并重新打开应用" in source


def test_app_explains_persistent_external_model_root() -> None:
    frontend = (
        ROOT / "apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift"
    ).read_text(encoding="utf-8")
    bridge = (
        ROOT / "apps/media_archive_image_video_ui_v125_candidate/native_bridge.py"
    ).read_text(encoding="utf-8")
    assert "全部项目显示“已找到”后，请退出并重新打开应用" in frontend
    assert "全部模型检查通过后，请退出并重新打开应用" in bridge
    assert '"model_directory_write": False' in bridge
