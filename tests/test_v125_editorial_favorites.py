import json
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest

from apps.media_archive_image_video_ui_v125_candidate.editorial_favorites import editorial_favorites
from apps.media_archive_image_video_ui_v125_candidate.editorial_candidate.db_adapter import resolve_timeline_sources
from apps.media_archive_image_video_ui_v125_candidate import native_bridge
from v126_test_fixtures import _fixture, _sha256


def favorite_fixture(tmp_path):
    db, _ = _fixture(tmp_path)
    with sqlite3.connect(db) as con:
        con.executescript("""
            ALTER TABLE source_assets ADD COLUMN relative_path TEXT DEFAULT 'clip.mov';
            ALTER TABLE source_assets ADD COLUMN media_type TEXT DEFAULT 'video';
            CREATE TABLE archive_tasks(task_id TEXT, task_mode TEXT, created_at TEXT);
            INSERT INTO archive_tasks VALUES('main','full','2026-01-01');
            CREATE TABLE user_asset_annotations(task_id TEXT,source_content_id TEXT,favorite INTEGER,note TEXT,updated_at REAL);
            INSERT INTO user_asset_annotations VALUES('main','s1',1,'已有收藏',1);
        """)
    return db


def test_favorites_are_readonly_real_candidate_ids_and_not_false_recommendations(tmp_path):
    db = favorite_fixture(tmp_path); before = _sha256(db)
    listing = editorial_favorites(db, db)
    assert len(listing['sources']) == 1 and not listing['candidates']
    result = editorial_favorites(db, db, 's1')
    c = result['candidates'][0]
    assert c['candidate_id'] == 'doc1' and c['gate_status'] == 'SOFT_GATE'
    assert c['requires_source_review'] and c['pool'] == 'favorite_manual'
    assert c['provisional_in_ms'] == 6000 and c['provisional_out_ms'] == 10000
    assert resolve_timeline_sources(db, [c['candidate_id']])['doc1']['source_content_id'] == 's1'
    assert not result['database_write'] and not result['model_run'] and not result['original_media_read']
    assert _sha256(db) == before


def test_favorites_pagination_uses_all_existing_frames_without_repeating_pages(tmp_path):
    db = favorite_fixture(tmp_path)
    with sqlite3.connect(db) as con:
        for i in range(1, 23):
            con.execute("""INSERT INTO stop03_5d_text_documents SELECT embedding_run_id,?,source_content_id,
                media_type,?,document_kind,qwen_text,ocr_text,propagated_labels_json,source_relative_path,
                derived_id,quality_status,created_at FROM stop03_5d_text_documents WHERE document_id='doc1'""", (f'extra{i}', i*10000))
    pages = [editorial_favorites(db, db, 's1', n) for n in (0, 9, 18)]
    assert [len(p['candidates']) for p in pages] == [9, 9, 5]
    assert [p['next_offset'] for p in pages] == [9, 18, None]
    assert len({c['candidate_id'] for p in pages for c in p['candidates']}) == 23


@pytest.mark.parametrize('case', ['other_database', 'not_favorite', 'wrong_task', 'negative_offset'])
def test_invalid_scope_cannot_mix_libraries_or_nonfavorites(tmp_path, case):
    db = favorite_fixture(tmp_path)
    if case == 'wrong_task':
        with sqlite3.connect(db) as con: con.execute("UPDATE user_asset_annotations SET task_id='other'")
    with pytest.raises(ValueError):
        editorial_favorites(db, tmp_path/'other.sqlite' if case == 'other_database' else db,
                           'missing' if case == 'not_favorite' else 's1', -1 if case == 'negative_offset' else 0)


def test_empty_favorites_and_missing_index_are_explicit(tmp_path):
    db = favorite_fixture(tmp_path)
    with sqlite3.connect(db) as con: con.execute('DELETE FROM stop03_5d_text_documents')
    assert editorial_favorites(db, db, 's1')['candidates'] == []
    with sqlite3.connect(db) as con: con.execute('DROP TABLE user_asset_annotations')
    assert editorial_favorites(db, db)['sources'] == []


def test_native_entry_returns_correct_exit_and_scope(tmp_path, monkeypatch, capsys):
    db = favorite_fixture(tmp_path)
    monkeypatch.setattr(native_bridge, 'load_runtime', lambda _: ({}, SimpleNamespace(db_path=db), None))
    args = ['--config','cfg','editorial-favorites','--source-content-id','s1','--board-database',str(db)]
    assert native_bridge.main(args) == 0
    assert json.loads(capsys.readouterr().out)['candidates'][0]['candidate_id'] == 'doc1'
    args[-1] = str(tmp_path/'wrong.sqlite')
    assert native_bridge.main(args) == 2


def test_decision_transaction_does_not_persist_each_redundant_removal():
    text = (Path(__file__).parents[1]/'apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift').read_text()
    method = text.split('func setEditorialDecision(',1)[1].split('func editorialCut(',1)[0]
    assert 'var choices = editorialDecisions' in method
    assert 'editorialDecisions.removeValue' not in method
    assert '只保存在本次会话' not in method
    assert 'applicationShouldTerminate' in text and 'finishEditorialSaves' in text
    assert 'application.mainMenu = mainMenu' in text
    assert '#selector(NSApplication.terminate(_:)), keyEquivalent: "q"' in text


def test_manual_favorite_exports_even_outside_global_recall_and_uses_database_path(tmp_path, monkeypatch):
    from apps.media_archive_image_video_ui_v125_candidate import editorial_assistant as assistant
    from apps.media_archive_image_video_ui_v125_candidate.editorial_candidate import timeline_export as timeline
    from xml.etree import ElementTree as ET
    db = favorite_fixture(tmp_path)
    candidate = editorial_favorites(db, db, 's1')['candidates'][0]
    candidate.update(manual_origin='favorite_manual', provisional_in_ms=1000, provisional_out_ms=3000)
    source = resolve_timeline_sources(db, ['doc1'])
    adapter = SimpleNamespace(load_database_project=lambda *_a, **_k: {'candidates': []},
                              resolve_timeline_sources=lambda *_a: source)
    monkeypatch.setattr(assistant, '_engine', lambda: (None, adapter, timeline))
    monkeypatch.setattr(timeline, 'probe_source_timing', lambda _: {'source_start_seconds':'0','source_duration_seconds':'60','source_frame_rate':'25'})
    request = tmp_path/'request.json'
    payload = {'frame_rate':'25', 'beats':[{'beat_id':'1','order':1,'text':'观察空间。','candidates':[candidate]}],
               'decisions':[{'beat_id':'1','candidate_id':'doc1','decision':'selected'}]}
    request.write_text(json.dumps(payload))
    assistant.export_editorial_timeline(db, request, tmp_path/'timeline.fcpxml')
    assistant.export_editorial_manifest(db, request, tmp_path/'manifest.json')
    assert ET.parse(tmp_path/'timeline.fcpxml').find('.//spine/asset-clip').get('start') == '1s'
    item = json.loads((tmp_path/'manifest.json').read_text())['items'][0]
    assert item['source_path'] == str(tmp_path/'麦田.MOV')
    assert item['recommendation_reason'] == candidate['recommendation_reason']
    candidate['source_content_id'] = 'wrong'
    request.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='编号不一致'):
        assistant.export_editorial_timeline(db, request, tmp_path/'bad.fcpxml')
