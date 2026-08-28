from pathlib import Path
import sqlite3

from test_v125_editorial_favorites import favorite_fixture
from apps.media_archive_image_video_ui_v125_candidate.editorial_favorites import editorial_favorites
from v126_test_fixtures import _sha256

SOURCE = Path(__file__).parents[1] / 'apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift'


def test_favorite_representative_thumbnail_does_not_claim_exact_saved_frame(tmp_path):
    db = favorite_fixture(tmp_path)
    before = _sha256(db)
    source = editorial_favorites(db, db)['sources'][0]
    assert source['preview_path']
    assert source['preview_time_ms'] == 8000
    assert source['preview_origin'] == 'representative_existing_frame'
    assert _sha256(db) == before


def test_favorite_without_preview_keeps_source_and_explicit_unknown(tmp_path):
    db = favorite_fixture(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute('DELETE FROM stop03_5d_text_documents')
    result = editorial_favorites(db, db)
    assert len(result['sources']) == 1
    assert result['sources'][0]['preview_origin'] == 'unavailable'
    assert result['sources'][0]['preview_time_ms'] is None


def test_favorite_uses_same_nonblack_representative_as_collection(tmp_path):
    db = favorite_fixture(tmp_path)
    with sqlite3.connect(db) as con:
        con.executescript('''
            CREATE TABLE visual_units(visual_unit_id TEXT,source_content_id TEXT,derived_id TEXT,
                                      time_position_ms INTEGER,near_black INTEGER);
            ALTER TABLE derived_assets ADD COLUMN time_position_ms INTEGER DEFAULT 0;
        ''')
        derived = con.execute('SELECT derived_id FROM derived_assets LIMIT 1').fetchone()[0]
        con.executemany('INSERT INTO visual_units VALUES(?,?,?,?,?)', [
            ('black','s1',derived,0,1), ('clear','s1',derived,5000,0), ('later','s1',derived,9000,0)])
    assert editorial_favorites(db, db)['sources'][0]['preview_time_ms'] == 5000


def test_toolbar_and_review_navigation_wired_only_to_editorial_page():
    text = SOURCE.read_text()
    root = text.split('struct RootView:', 1)[1]
    assert 'model.page == .editorial && model.editorialBoard != nil' in root
    row = text.split('struct EditorialSelectionRow:', 1)[1].split('struct EditorialPage:', 1)[0]
    assert 'model.reviewEditorialBeat(beat.beatId)' in row
    assert 'model.finishEditorialReview()' in text
    assert '从我的收藏选画面 → 第' in text


def test_saved_box_is_flat_lazy_and_thumbnails_decode_off_ui_thread():
    text = SOURCE.read_text()
    panel = text.split('private var decisionPanel:', 1)[1].split('struct EditorialReferencePage:', 1)[0]
    assert 'List(saved)' in panel and 'ForEach(board.beats)' not in panel
    assert 'model.editorialSavedSelections()' in panel
    card = text.split('struct EditorialCandidateCard:', 1)[1].split('struct EditorialSelectionRow:', 1)[0]
    assert 'NSImage(contentsOfFile:' not in card
    assert 'EditorialThumbnail(path: candidate.previewPath)' in card
    assert 'static let queue = DispatchQueue(label: "editorial.thumbnail.decode"' in text
    assert 'cache.totalCostLimit = 32 * 1024 * 1024' in text


def test_preview_closes_release_views_observers_and_registry():
    text = SOURCE.read_text()
    controller = text.split('private final class EditorialPlaybackWindowController:', 1)[1].split('private struct EditorialPreviewResponse', 1)[0]
    assert 'self.playback.stop()' in controller and 'self.window?.contentView = nil' in controller
    assert 'callback?()' in controller
    assert 'self.videoPreviewControllers.removeAll { $0 === controller }' in text
    assert 'static func dismantleNSView' in text
    assert 'model?.closePreviewWindows()' in text
    assert 'cancelPendingSeeks()' in text and 'asset.cancelLoading()' in text


def test_guidance_recheck_uses_existing_scoped_request_not_new_inference():
    text = SOURCE.read_text()
    assert '按本句指导重新找画面（保留已选）") { model.refreshEditorialBeat() }' in text
    assert '未映射的G编号不会当作真实文件编号' in text
