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
    for action in ['model.open(result)', 'model.browseSourceFrames(result)', 'model.saveResultAnnotation(',
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
                   'model.open(result)', 'model.browseSourceFrames(result)',
                   'model.addSearchResultToEditorial(result, decision: "selected")',
                   'model.addSearchResultToEditorial(result, decision: "review")',
                   'model.setSelectedForExport(result, selected: $0)']:
        assert action in row
    page = code.split('struct SearchPage: View {',1)[1].split('struct EditorialFavoritesPanel:',1)[0]
    assert '@State private var detailRequest: SearchDetailRequest?' in page
    assert '.sheet(item: $detailRequest)' in page
    assert 'SearchResultCard(result: request.result, initialPanel: request.panel)' in page


def test_search_page_defers_person_catalog_until_user_opens_person_tools():
    code = SOURCE.read_text()
    page = code.split('struct SearchPage: View {', 1)[1].split('struct EditorialFavoritesPanel:', 1)[0]
    assert 'EditorialDisclosure("人物搜索与本地人物管理（按需展开）")' in page
    assert 'Button("读取人物分组") { model.loadPersonClusters() }' in page
    on_appear = page.split('}.onAppear {', 1)[1]
    assert 'model.loadPersonClusters()' not in on_appear
    assert 'panel == .person && model.personClusterCatalog.isEmpty' in page


def test_search_elapsed_updates_do_not_rebuild_rows_five_times_per_second():
    code = SOURCE.read_text()
    timer = code.split('private func startSearchTimer()', 1)[1].split('private func stopSearchTimer()', 1)[0]
    assert 'withTimeInterval: 0.5' in timer


def test_visual_search_copy_matches_source_grouped_results():
    page = SOURCE.read_text().split('struct SearchPage: View {', 1)[1].split('struct EditorialFavoritesPanel:', 1)[0]
    assert '同一视频的相关时间点合并为一条素材结果' in page
    assert '同一素材的不同时间点仍是独立画面' not in page


def test_recent_search_restores_cached_rows_before_background_refresh():
    code = SOURCE.read_text()
    assert 'enum SearchResultCacheStore' in code
    assert 'LocalMediaOrganizer/SearchCache/recent_v1.json' in code
    assert 'results: Array(bufferedSearchResults.prefix(200))' in code
    method = code.split('func openSearchHistory', 1)[1].split('private func cacheSearchResult', 1)[0]
    assert 'searchResults = Array(cached.results.prefix(30))' in method
    assert 'search(forceRefresh: true, keepVisibleResults: true)' in method
    assert '正在后台刷新' in method
    page = code.split('struct SearchPage: View {', 1)[1].split('struct EditorialFavoritesPanel:', 1)[0]
    assert 'ForEach(model.searchHistory.prefix(10))' in page
    assert 'model.openSearchHistory(item)' in page


def test_large_search_payload_decodes_off_main_thread():
    code = SOURCE.read_text()
    decode = code.split('private func decodeSearchPayload', 1)[1].split('func search(', 1)[0]
    assert 'DispatchQueue.global(qos: .userInitiated).async' in decode
    assert 'decoder.decode(SearchResponse.self, from: data)' in decode
    assert 'DispatchQueue.main.async { completion(success, failure) }' in decode


def test_search_input_has_full_width_focus_target_and_results_are_copyable():
    code = SOURCE.read_text()
    page = code.split('struct SearchPage: View {', 1)[1].split('struct EditorialFavoritesPanel:', 1)[0]
    assert '@FocusState private var queryFieldFocused: Bool' in page
    assert '.frame(maxWidth: .infinity, minHeight: 38)' in page
    assert '.contentShape(Rectangle())' in page
    assert '.onTapGesture { queryFieldFocused = true }' in page
    row = code.split('struct SearchResultSummaryRow: View {', 1)[1].split('struct SearchResultCard:', 1)[0]
    assert '.textSelection(.enabled)' in row


def test_search_stream_publishes_ranked_rows_while_final_search_continues():
    code = SOURCE.read_text()
    helper = code.split('private func runSearchHelper', 1)[1].split('private func startSearchTimer', 1)[0]
    assert 'SEARCH_RESULT_STREAM_JSON=' in helper
    assert 'SearchResultStreamEvent.self' in helper
    search = code.split('func search(loadMore:', 1)[1].split('func loadSearchMetadata', 1)[0]
    assert 'stream: { event in' in search
    assert 'visibleLimit = min(60, self.bufferedSearchResults.count)' in search
    assert '搜索仍在继续 · 已先显示' in search
    assert 'streamedVisibleCount' in search
