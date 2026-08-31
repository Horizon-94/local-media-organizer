from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.media_archive_image_video_ui_v125_candidate import native_bridge


def _manager(tmp_path: Path) -> SimpleNamespace:
    files = {}
    for name in ("search.py", "search.json", "embedding-python", "visual-python"):
        path = tmp_path / name
        path.write_text("fixture", encoding="utf-8")
        files[name] = path
    return SimpleNamespace(
        output_root=tmp_path / "runtime",
        search_script=files["search.py"],
        search_config=files["search.json"],
        embedding_python=files["embedding-python"],
        openclip_python=files["visual-python"],
    )


def test_all_library_search_ranks_labels_and_deduplicates(monkeypatch, tmp_path: Path):
    first_db = tmp_path / "first.sqlite"
    second_db = tmp_path / "second.sqlite"
    first_db.touch(); second_db.touch()
    libraries = [
        {"task_id": "one", "task_name": "人物项目", "database": str(first_db)},
        {"task_id": "two", "task_name": "城市项目", "database": str(second_db)},
    ]
    monkeypatch.setattr(native_bridge, "existing_libraries", lambda _config: libraries)
    history = []
    monkeypatch.setattr(native_bridge, "record_search_history", lambda *args, **kwargs: history.append(kwargs))
    monkeypatch.setattr(native_bridge, "task_id_for_database", lambda _path: "active")

    def fake_search(_config, repository, _manager, query, *_args, **kwargs):
        context = kwargs["library_context"]
        is_first = Path(repository.db_path) == first_db
        rows = [
            {
                "result_id": f"{context['task_id']}|shared",
                "source_content_id": f"source-{context['task_id']}",
                "source_path": "/Volumes/media/shared.mov",
                "score": 0.70 if is_first else 0.95,
                "library_task_id": context["task_id"],
                "library_task_name": context["task_name"],
                "library_database": context["database"],
            },
            {
                "result_id": f"{context['task_id']}|unique",
                "source_content_id": f"unique-{context['task_id']}",
                "source_path": f"/Volumes/media/{context['task_id']}.mov",
                "score": 0.80 if is_first else 0.60,
                "library_task_id": context["task_id"],
                "library_task_name": context["task_name"],
                "library_database": context["database"],
            },
        ]
        return {
            "status": "PASS", "result_items": rows, "result_total_count": 2,
            "coverage": {
                "eligible_visual_unit_count": 10,
                "scanned_visual_vector_count": 8,
                "scanned_text_vector_count": 6,
            },
            "result_count_by_media": {"video": 2},
        }

    monkeypatch.setattr(native_bridge, "run_search", fake_search)
    active = SimpleNamespace(db_path=first_db)
    report = native_bridge.run_search_all_libraries(
        {}, active, _manager(tmp_path), "老人", "video", 5000,
        result_limit=20,
    )

    assert report["status"] == "PASS"
    assert report["search_scope"] == "all"
    assert report["library_count"] == 2
    assert report["cross_library_duplicate_count"] == 1
    assert len(report["result_items"]) == 3
    assert report["result_items"][0]["library_task_name"] == "城市项目"
    assert report["coverage"]["eligible_visual_unit_count"] == 20
    assert report["result_count_by_media"] == {"video": 4}
    assert history[0]["filters"]["search_scope"] == "all"


def test_requested_database_must_be_registered(monkeypatch, tmp_path: Path):
    allowed = tmp_path / "allowed.sqlite"
    rejected = tmp_path / "rejected.sqlite"
    allowed.touch(); rejected.touch()
    monkeypatch.setattr(native_bridge, "existing_libraries", lambda _config: [
        {"database": str(allowed)},
    ])
    repository = native_bridge.repository_for_request({}, None, allowed)
    assert repository is not None and repository.db_path == allowed.resolve()
    with pytest.raises(RuntimeError, match="不属于已登记素材库"):
        native_bridge.repository_for_request({}, None, rejected)


def test_v127_frontend_exposes_scope_and_library_identity():
    source = Path(
        "apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift"
    ).read_text(encoding="utf-8")
    assert 'Text("当前素材库").tag("当前素材库")' in source
    assert 'Text("全部素材库（\\(model.snapshot?.existingLibraries.count ?? 0) 个）")' in source
    assert '"--search-scope"' in source
    assert "libraryTaskName" in source
    assert "文稿补选只搜索当前工程素材库" in source


def test_v127_parser_accepts_all_library_scope():
    parser = native_bridge.build_parser()
    args = parser.parse_args(["--config", "/tmp/config.json", "search", "--query", "老人", "--search-scope", "all"])
    assert args.search_scope == "all"
