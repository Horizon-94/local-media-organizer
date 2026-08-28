from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]/'apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift'


def test_search_cards_use_background_cached_previews():
    code = SOURCE.read_text()
    card = code.split('struct SearchResultCard: View {',1)[1].split('struct SearchPage:',1)[0]
    assert 'NSImage(contentsOfFile:' not in card
    assert 'EditorialThumbnail(path: result.previewPath ?? "", contentMode: .fill)' in card
    assert '.frame(width: 250, height: 150)' in card
    assert 'EditorialDisclosure("人工人物归类", initiallyExpanded:' in card
    assert 'EditorialDisclosure("本地标签、备注与收藏", initiallyExpanded:' in card


def test_search_list_uses_native_row_reuse_without_changing_order_or_actions():
    code = SOURCE.read_text()
    page = code.split('struct SearchPage: View {',1)[1].split('struct EditorialFavoritesPanel:',1)[0]
    assert 'var body: some View { List {' in page
    assert 'ScrollView { LazyVStack(' not in page
    assert 'ForEach(model.searchResults) { result in' in page
    assert 'SearchResultSummaryRow(result: result)' in page
    assert '.buttonStyle(.borderless)' in page
    assert 'model.search(loadMore: true)' in page
    assert 'model.navigateBackInSearch()' in page
    assert 'DisclosureGroup(' not in page
    card = code.split('struct SearchResultCard:',1)[1].split('struct SearchPage:',1)[0]
    for action in ['model.open(result)', 'model.browseSourceFrames(sourceId)', 'model.saveResultAnnotation(',
                   'model.addSearchResultToEditorial(result, decision: "selected")',
                   'model.addSearchResultToEditorial(result, decision: "review")']:
        assert action in card


def test_legacy_row_identity_is_stable_and_export_identity_is_unchanged():
    code = SOURCE.read_text().split('struct SearchResult:',1)[1].split('struct UserAssetAnnotation:',1)[0]
    assert 'UUID()' not in code
    assert 'return resultId' in code
    assert 'visualUnitId' in code and 'audioEvidenceId' in code
    assert 'resultId ?? "\\(sourceContentId ?? "unknown")|\\(timecode ?? "00:00")|\\(previewPath ?? "")"' in code


def test_native_header_does_not_wrap_the_result_rows_in_a_single_eager_row():
    page = SOURCE.read_text().split('struct SearchPage: View {',1)[1].split('struct EditorialFavoritesPanel:',1)[0]
    header, rows = page.split('ForEach(model.searchResults)',1)
    assert '}.padding(.vertical, 20)' in header
    assert '.listRowInsets(' in header
    assert 'model.search(loadMore: true)' in rows
    assert 'prefix(' not in rows.split('.onAppear',1)[0]


def test_hotfix7_is_optimized_and_does_not_copy_a_full_app_or_change_version():
    code = (SOURCE.parents[2]/'tools/v126_ui_hotfix7.py').read_text()
    assert "'-O', '-target'" in code and "'-Onone'" not in code
    assert "check_app(closed=True)" in code
    assert "'1.2.6'" in code and '1.2.7' not in code
    assert 'copytree(' not in code
    assert "Rollback/App baseline changed" in code


def test_scrolling_summary_has_fixed_height_and_no_live_edit_forms():
    code = SOURCE.read_text()
    row = code.split('struct SearchResultSummaryRow: View {', 1)[1].split('struct SearchResultCard:', 1)[0]
    assert '.frame(height: model.editorialSearchTarget == nil ? 196 : 270)' in row
    assert 'TextField(' not in row and 'EditorialDisclosure(' not in row
    assert 'SearchResultCard(' not in row
    for action in ['inspect(.evidence)', 'inspect(.annotations)', 'inspect(.person)',
                   'model.open(result)', 'model.browseSourceFrames(sourceId)',
                   'model.addSearchResultToEditorial(result, decision: "selected")',
                   'model.addSearchResultToEditorial(result, decision: "review")',
                   'model.setSelectedForExport(result, selected: $0)']:
        assert action in row
    page = code.split('struct SearchPage: View {',1)[1].split('struct EditorialFavoritesPanel:',1)[0]
    assert '@State private var detailRequest: SearchDetailRequest?' in page
    assert '.sheet(item: $detailRequest)' in page
    assert 'SearchResultCard(result: request.result, initialPanel: request.panel)' in page
