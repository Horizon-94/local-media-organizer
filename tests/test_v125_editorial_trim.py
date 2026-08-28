import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from apps.media_archive_image_video_ui_v125_candidate import editorial_assistant as assistant
from apps.media_archive_image_video_ui_v125_candidate.editorial_candidate import timeline_export as timeline
from v126_test_fixtures import _clip


def test_confirmed_trim_is_used_by_xml_and_recorded_in_json(monkeypatch, tmp_path):
    main, backup = _clip("main", 1), _clip("backup", 2)
    sources = {row["candidate_id"]: row for row in [main, backup]}
    adapter = SimpleNamespace(load_database_project=lambda *_a, **_k: {"candidates": [main, backup]},
                              resolve_timeline_sources=lambda *_a: sources)
    monkeypatch.setattr(assistant, "_engine", lambda: (None, adapter, timeline))
    monkeypatch.setattr(timeline, "probe_source_timing", lambda _: {"source_start_seconds": "0", "source_duration_seconds": "60", "source_frame_rate": "25"})
    trimmed = [{**row, "provisional_in_ms": 10000, "provisional_out_ms": 12700,
                "cut_locked": True, "cut_origin": "human_preview_confirmed"} for row in [main, backup]]
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"frame_rate": "25", "beats": [
        {"beat_id": "1", "order": 1, "text": "观察设备的状态。", "candidates": trimmed}],
        "decisions": [{"beat_id": "1", "candidate_id": "main", "decision": "selected"},
                      {"beat_id": "1", "candidate_id": "backup", "decision": "review"}]}))
    assistant.export_editorial_manifest(tmp_path / "db", request, tmp_path / "manifest.json")
    assistant.export_editorial_timeline(tmp_path / "db", request, tmp_path / "cut.fcpxml")
    item = json.loads((tmp_path / "manifest.json").read_text())["items"][0]
    assert item["source_in"] == 10000 and item["source_out"] == 12700
    assert item["cut_locked"] and item["cut_origin"] == "human_preview_confirmed"
    alternative = item["alternatives"][0]
    assert alternative["cut_locked"] and alternative["cut_origin"] == "human_preview_confirmed"
    assert alternative["source_in"] == 10000 and alternative["source_out"] == 12700
    xml = ET.parse(tmp_path / "cut.fcpxml")
    assert xml.find(".//spine/asset-clip").get("start") == "10s"


def test_old_requests_without_lock_metadata_remain_compatible(monkeypatch, tmp_path):
    source = _clip("old", 1)
    adapter = SimpleNamespace(resolve_timeline_sources=lambda *_a: {"old": source})
    monkeypatch.setattr(assistant, "_engine", lambda: (None, adapter, timeline))
    request = tmp_path / "old.json"
    request.write_text(json.dumps({"beats": [{"beat_id": "1", "text": "说明。", "candidates": [source]}],
                                   "decisions": [{"beat_id": "1", "candidate_id": "old", "decision": "selected"}]}))
    assistant.export_editorial_manifest(tmp_path / "db", request, tmp_path / "manifest.json")
    item = json.loads((tmp_path / "manifest.json").read_text())["items"][0]
    assert item["cut_locked"] is False and item["cut_origin"] == "suggested"
