from pathlib import Path
import sqlite3
from types import SimpleNamespace
import json

import pytest

from apps.media_archive_image_video_ui_v125_candidate.editorial_preview import resolve_editorial_preview
from apps.media_archive_image_video_ui_v125_candidate.native_bridge import build_parser
from apps.media_archive_image_video_ui_v125_candidate import native_bridge
from v126_test_fixtures import _fixture, _sha256


def test_preview_resolves_original_not_thumbnail_and_preserves_files(tmp_path):
    database, _ = _fixture(tmp_path)
    before = {p: _sha256(p) for p in tmp_path.iterdir() if p.is_file()}
    result = resolve_editorial_preview(database, "doc1", "s1", database)
    assert result["ok"] and result["source_path"] == str(tmp_path / "麦田.MOV")
    assert result["media_type"] == "video"
    assert result["database"] == str(database.resolve())
    assert before == {p: _sha256(p) for p in before}


@pytest.mark.parametrize("candidate,source,reason", [
    ("keep-a-roll::1", "", "PREVIEW_NO_SOURCE"),
    ("doc-missing", "s1", "PREVIEW_CANDIDATE_MISSING"),
    ("doc1", "wrong-library-source", "PREVIEW_CANDIDATE_MISSING"),
    ("x" * 201, "s1", "PREVIEW_NO_SOURCE"),
])
def test_unknown_and_placeholder_never_open_arbitrary_files(tmp_path, candidate, source, reason):
    database, _ = _fixture(tmp_path)
    with pytest.raises(ValueError, match=reason):
        resolve_editorial_preview(database, candidate, source, database)


def test_changed_library_cannot_reuse_old_board_ids(tmp_path):
    database, _ = _fixture(tmp_path)
    with pytest.raises(ValueError, match="PREVIEW_LIBRARY_CHANGED"):
        resolve_editorial_preview(database, "doc1", "s1", tmp_path / "other.sqlite")


def test_reconnected_original_can_play_without_regenerating_board(tmp_path):
    database, _ = _fixture(tmp_path)
    original = tmp_path / "麦田.MOV"
    offline = tmp_path / "offline.MOV"
    original.rename(offline)  # Synthetic test fixture only, never real media.
    with pytest.raises(ValueError, match="PREVIEW_SOURCE_UNAVAILABLE") as error:
        resolve_editorial_preview(database, "doc1", "s1", database)
    assert str(original) in str(error.value)
    offline.rename(original)
    with sqlite3.connect(database) as con:
        con.execute("UPDATE source_assets SET online_status=0")
    assert resolve_editorial_preview(database, "doc1", "s1", database)["ok"]


@pytest.mark.parametrize("media_type", ["image", "audio"])
def test_original_still_and_audio_are_supported(tmp_path, media_type):
    database, _ = _fixture(tmp_path)
    with sqlite3.connect(database) as con:
        con.execute("UPDATE stop03_5d_text_documents SET media_type=?", (media_type,))
    assert resolve_editorial_preview(database, "doc1", "s1", database)["media_type"] == media_type


@pytest.mark.parametrize("path", ["", "relative.mov", "https://example.com/video.mov"])
def test_preview_never_guesses_paths_or_opens_remote_urls(tmp_path, path):
    database, _ = _fixture(tmp_path)
    with sqlite3.connect(database) as con:
        con.execute("UPDATE source_assets SET absolute_path=?", (path,))
    with pytest.raises(ValueError, match="PREVIEW_PATH_INVALID"):
        resolve_editorial_preview(database, "doc1", "s1", database)


def test_preview_bridge_requires_current_board_and_source_identity():
    args = build_parser().parse_args([
        "--config", "config.json", "editorial-preview", "--candidate-id", "doc1",
        "--source-content-id", "s1", "--board-database", "library.sqlite",
    ])
    assert args.command == "editorial-preview"
    assert args.board_database == Path("library.sqlite")


def test_preview_bridge_success_exit_code_and_error_payload(monkeypatch, tmp_path, capsys):
    database, _ = _fixture(tmp_path)
    monkeypatch.setattr(native_bridge, "load_runtime", lambda _: ({}, SimpleNamespace(db_path=database), None))
    args = ["--config", "config.json", "editorial-preview", "--candidate-id", "doc1",
            "--source-content-id", "s1", "--board-database", str(database)]
    assert native_bridge.main(args) == 0  # Swift refuses to play on nonzero exit.
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "PASS" and response["ok"]
    assert response["source_path"].endswith(".MOV")
    args[-1] = str(tmp_path / "different.sqlite")
    assert native_bridge.main(args) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "FAIL" and "PREVIEW_LIBRARY_CHANGED" in response["error"]
