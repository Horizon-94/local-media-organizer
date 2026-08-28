import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from v126_test_fixtures import _fixture, _sha256
from apps.media_archive_image_video_ui_v125_candidate.editorial_favorites import editorial_search_candidate
from apps.media_archive_image_video_ui_v125_candidate.editorial_candidate.db_adapter import resolve_timeline_sources
from apps.media_archive_image_video_ui_v125_candidate import native_bridge


def search_fixture(tmp_path):
    db, _ = _fixture(tmp_path)
    with sqlite3.connect(db) as con:
        con.executescript("""CREATE TABLE visual_units(visual_unit_id TEXT,source_content_id TEXT,derived_id TEXT,time_position_ms INTEGER);
            ALTER TABLE derived_assets ADD COLUMN time_position_ms INTEGER DEFAULT 8000;
            ALTER TABLE source_assets ADD COLUMN relative_path TEXT DEFAULT 'clip.mov';
            ALTER TABLE source_assets ADD COLUMN media_type TEXT DEFAULT 'video';
            INSERT INTO visual_units VALUES('visual-1','s1','d1',8000);
            INSERT INTO visual_units VALUES('not-indexed','s1','other-derived',9000);""")
    return db


def test_exact_search_frame_conversion_is_readonly_and_export_resolvable(tmp_path):
    db = search_fixture(tmp_path); before = _sha256(db)
    result = editorial_search_candidate(db, db, 's1', 'visual-1')
    c = result['candidate']
    assert c['candidate_id'] == 'doc1' and c['pool'] == 'search_manual'
    assert c['anchor_time_ms'] == 8000 and c['provisional_in_ms'] == 6000
    assert c['provisional_out_ms'] == 10000 and c['requires_source_review']
    assert c['gate_reason_codes'] == ['USER_SEARCH_MANUAL']
    assert resolve_timeline_sources(db, ['doc1'])['doc1']['source_content_id'] == 's1'
    assert not any(result[k] for k in ['database_write','model_run','original_media_read'])
    assert _sha256(db) == before


@pytest.mark.parametrize('source,visual', [('s2','visual-1'), ('s1','missing'), ('s1','not-indexed'), ('','visual-1'), ('s1',''), ('s1','x'*201)])
def test_no_nearest_frame_or_cross_source_substitution(tmp_path, source, visual):
    db = search_fixture(tmp_path); before = _sha256(db)
    with pytest.raises(ValueError): editorial_search_candidate(db, db, source, visual)
    assert _sha256(db) == before


def test_cross_library_and_deleted_source_are_rejected(tmp_path):
    db = search_fixture(tmp_path)
    with pytest.raises(ValueError): editorial_search_candidate(db, tmp_path/'wrong.sqlite', 's1', 'visual-1')
    with sqlite3.connect(db) as con: con.execute('UPDATE source_assets SET is_deleted_or_missing=1')
    with pytest.raises(ValueError): editorial_search_candidate(db, db, 's1', 'visual-1')


def test_latest_exact_document_is_used_not_latest_other_frame(tmp_path):
    db = search_fixture(tmp_path)
    with sqlite3.connect(db) as con:
        for document, derived, created in [('new-same','d1','2026-02-01'),('new-other','elsewhere','2026-03-01')]:
            con.execute("""INSERT INTO stop03_5d_text_documents SELECT 'new-run',?,source_content_id,
                media_type,time_position_ms,document_kind,qwen_text,ocr_text,propagated_labels_json,
                source_relative_path,?,quality_status,? FROM stop03_5d_text_documents WHERE document_id='doc1'""",
                        (document,derived,created))
    assert editorial_search_candidate(db,db,'s1','visual-1')['candidate']['candidate_id'] == 'new-same'


def test_image_uses_still_hold_not_video_interval(tmp_path):
    db = search_fixture(tmp_path)
    with sqlite3.connect(db) as con: con.execute("UPDATE stop03_5d_text_documents SET media_type='image',time_position_ms=-1")
    c = editorial_search_candidate(db,db,'s1','visual-1')['candidate']
    assert c['anchor_time_ms'] is None and c['provisional_in_ms'] == 0 and c['provisional_out_ms'] == 5000


def test_existing_search_frame_without_text_document_can_be_selected_and_previewed(tmp_path):
    from apps.media_archive_image_video_ui_v125_candidate.editorial_preview import resolve_editorial_preview
    db = search_fixture(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute('DELETE FROM stop03_5d_text_documents')
        con.execute('UPDATE visual_units SET time_position_ms=-1')
    before = _sha256(db)
    c = editorial_search_candidate(db,db,'s1','visual-1')['candidate']
    assert c['candidate_id'] == 'manual-visual::visual-1'
    assert c['anchor_time_ms'] == 8000 and c['provisional_in_ms'] == 6000
    assert '暂无' in c['description'] and c['role'] == '人工待定'
    assert resolve_timeline_sources(db,[c['candidate_id']])[c['candidate_id']]['source_content_id'] == 's1'
    assert resolve_editorial_preview(db,c['candidate_id'],'s1',db)['status'] == 'PASS'
    with pytest.raises(ValueError): resolve_editorial_preview(db,c['candidate_id'],'other-source',db)
    assert _sha256(db) == before
    with sqlite3.connect(db) as con: con.execute('UPDATE derived_assets SET time_position_ms=-1')
    with pytest.raises(ValueError, match='时间点'): editorial_search_candidate(db,db,'s1','visual-1')


def test_native_command_uses_active_database(tmp_path, monkeypatch, capsys):
    db = search_fixture(tmp_path)
    monkeypatch.setattr(native_bridge,'load_runtime',lambda _: ({},SimpleNamespace(db_path=db),None))
    args=['--config','cfg','editorial-search-candidate','--board-database',str(db),'--source-content-id','s1','--visual-unit-id','visual-1']
    assert native_bridge.main(args) == 0
    assert json.loads(capsys.readouterr().out)['candidate']['candidate_id'] == 'doc1'
    args[4] = str(tmp_path/'wrong.sqlite')
    assert native_bridge.main(args) == 2


@pytest.mark.parametrize('without_text_document', [False,True])
def test_search_manual_exports_xml_and_json_outside_recall_with_human_cuts(tmp_path, monkeypatch, without_text_document):
    from apps.media_archive_image_video_ui_v125_candidate import editorial_assistant as assistant
    from apps.media_archive_image_video_ui_v125_candidate.editorial_candidate import timeline_export as timeline
    db = search_fixture(tmp_path)
    if without_text_document:
        with sqlite3.connect(db) as con: con.execute('DELETE FROM stop03_5d_text_documents')
    c = editorial_search_candidate(db,db,'s1','visual-1')['candidate']
    c.update(manual_origin='search_manual',provisional_in_ms=1000,provisional_out_ms=3000)
    cid = c['candidate_id']
    source = resolve_timeline_sources(db,[cid])
    monkeypatch.setattr(assistant,'_engine',lambda: (None,SimpleNamespace(
        load_database_project=lambda *_a,**_k:{'candidates':[]},resolve_timeline_sources=lambda *_a:source),timeline))
    monkeypatch.setattr(timeline,'probe_source_timing',lambda _: {'source_start_seconds':'0','source_duration_seconds':'60','source_frame_rate':'25'})
    request=tmp_path/'request.json'
    payload={'frame_rate':'25','beats':[{'beat_id':'1','order':1,'text':'观察空间。','candidates':[c]}],
             'decisions':[{'beat_id':'1','candidate_id':cid,'decision':'selected'}]}
    request.write_text(json.dumps(payload))
    assistant.export_editorial_timeline(db,request,tmp_path/'timeline.fcpxml')
    assistant.export_editorial_manifest(db,request,tmp_path/'manifest.json')
    assert ET.parse(tmp_path/'timeline.fcpxml').find('.//spine/asset-clip').get('start') == '1s'
    item=json.loads((tmp_path/'manifest.json').read_text())['items'][0]
    assert item['source_path'] == source[cid]['source_absolute_path']
    assert item['recommendation_reason'] == c['recommendation_reason']
    assert item['manual_origin'] == 'search_manual'
    from apps.media_archive_image_video_ui_v125_candidate import editorial_session
    from apps.media_archive_image_video_ui_v125_candidate.editorial_candidate import db_adapter
    monkeypatch.setattr(editorial_session,'_engine',lambda: (None,SimpleNamespace(
        load_database_project=lambda *_a,**_k:{'candidates':[]},
        resolve_timeline_sources=db_adapter.resolve_timeline_sources,
        resolve_preview_path=db_adapter.resolve_preview_path),None))
    restored=editorial_session.import_manifest_session(db,tmp_path/'manifest.json')
    rc=restored['board']['beats'][0]['candidates'][0]
    assert rc['candidate_id'] == cid and rc['pool'] == 'search_manual'
    assert restored['decisions']['1::'+cid] == 'selected'
    assert restored['cut_overrides']['1::'+cid] == [1.0,3.0]
    c['source_content_id']='forged'; request.write_text(json.dumps(payload))
    with pytest.raises(ValueError,match='编号不一致'): assistant.export_editorial_timeline(db,request,tmp_path/'bad.fcpxml')


def test_native_wiring_has_scope_feedback_lazy_disclosures_and_no_automatic_search():
    text=(Path(__file__).parents[1]/'apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift').read_text()
    route=text.split('func startEditorialSearch()',1)[1].split('func returnFromEditorialSearch()',1)[0]
    assert 'query = direction.isEmpty ? beat.text : direction' in route
    assert 'search()' not in route and 'prewarmSearch()' not in route
    assert 'target.sessionId == editorialSessionId' in text and 'target.generation == editorialGeneration' in text
    assert 'self.editorialSearchTarget == target' in text
    disclosure=text.split('struct EditorialDisclosure<',1)[1].split('struct EditorialCandidateCard:',1)[0]
    assert 'frame(minHeight: 40)' in disclosure and 'if expanded { content() }' in disclosure
    assert 'timeIntervalSince(lastToggle) >= 0.35' in disclosure
    assert '["favorite_manual", "search_manual"].contains(candidate.pool)' in text
    assert '未加入：第' in text and '已经' in text and '确认替换' in text
    assert 'model.editorialSearchMessages[result.exportSelectionId]' in text
