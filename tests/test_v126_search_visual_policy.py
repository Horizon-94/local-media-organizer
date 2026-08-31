from pathlib import Path

from apps.media_archive_image_video_ui_v125_candidate.native_bridge import (
    bundled_search_runtime,
)
from apps.media_archive_image_video_ui_v125_candidate.search_jobs import SearchJobManager


def manager() -> SearchJobManager:
    return SearchJobManager(
        db_path=Path("/tmp/library.sqlite"), output_root=Path("/tmp/search"),
        search_script=Path("/tmp/search.py"), search_config=Path("/tmp/search.json"),
        embedding_python=Path("/tmp/text-python"),
        openclip_python=Path("/tmp/visual-python"),
    )


def test_visible_search_modes_do_not_scan_speech_evidence() -> None:
    for media_type in ("all", "image", "video"):
        command = manager().build_command(
            "老人", {"media_type": media_type, "limit": 30}, Path("/tmp/out")
        )
        assert "--disable-audio-evidence" in command
        assert "--audio-evidence-only" not in command
        assert "--native-readiness-verified" in command


def test_audio_filter_keeps_speech_and_does_not_disable_it() -> None:
    command = manager().build_command(
        "老人", {"media_type": "audio", "limit": 30}, Path("/tmp/out")
    )
    assert "--audio-evidence-only" in command
    assert "--disable-audio-evidence" not in command
    media_index = command.index("--media-type")
    assert command[media_index + 1] == "video"


def test_frontend_names_all_as_visible_media() -> None:
    source = (
        Path(__file__).parents[1]
        / "apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift"
    ).read_text(encoding="utf-8")
    assert 'value == "全部" ? "全部画面（图片＋视频）" : value' in source


def test_packaged_search_runtime_is_preferred_without_developer_checkout(
    tmp_path: Path,
) -> None:
    root = tmp_path / "SearchRuntime"
    adapter = (
        root / "scripts/04_media_archive_app/"
        "stop03_5e_hybrid_search_app_adapter_v1.py"
    )
    config = root / "configs/stop03_5e_hybrid_visual_text_search_v2.json"
    adapter.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    adapter.write_text("# packaged adapter\n", encoding="utf-8")
    config.write_text("{}\n", encoding="utf-8")

    assert bundled_search_runtime({"search_runtime_path": str(root)}) == (
        adapter,
        config,
    )
