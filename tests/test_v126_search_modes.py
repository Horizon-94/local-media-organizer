"""Ordinary library search must not implicitly become screenplay selection."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift'


def test_search_does_not_bind_a_loaded_project_on_appearance():
    code = SOURCE.read_text()
    page = code.split('struct SearchPage: View {', 1)[1].split('struct EditorialFavoritesPanel:', 1)[0]
    appearance = page.split('}.onAppear {', 1)[1]
    assert 'bindEditorialSearchTarget' not in appearance
    assert 'if model.editorialSearchTarget == nil { model.prewarmSearch() }' in appearance
    assert '} else if model.editorialBoard != nil {' not in page
    assert '} else if model.editorialSearchTarget != nil {' in page
    assert 'EditorialDisclosure("查看指导 / 更换补选句子")' in page
    assert 'Button("退出补选，普通搜索") { model.openMainPage(.search) }' in page
    row = code.split('struct SearchResultSummaryRow:', 1)[1].split('struct SearchResultCard:', 1)[0]
    assert '文稿选片中打开工程后' not in row
    assert 'if let beat = model.editorialSearchBeat {' in row
    assert 'if model.editorialSearchTarget != nil {' in row


def test_main_entries_reset_only_the_search_handoff_not_the_project():
    code = SOURCE.read_text()
    sidebar = code.split('struct Sidebar: View {', 1)[1].split('private let editorialReferenceTopics:', 1)[0]
    assert 'Button { model.openMainPage(page) }' in sidebar
    assert 'Button("进入搜索素材") { model.openMainPage(.search) }' in code
    route = code.split('func openMainPage(', 1)[1].split('func startEditorialSearch()', 1)[0]
    assert 'editorialSearchTarget = nil' in route
    assert 'editorialSearchMessages = [:]' in route
    for forbidden in ['editorialBoard =', 'editorialDecisions', 'editorialCutOverrides',
                      'searchResults =', 'query =', 'runHelper(', 'search()', 'prewarmSearch()']:
        assert forbidden not in route


def test_actual_swift_search_route_transitions(tmp_path):
    code = SOURCE.read_text()
    methods = code[code.index('    var editorialSearchBeat: EditorialBeat? {'):
                   code.index('    func addSearchResultToEditorial(')]
    harness = r'''
import Foundation
enum ArchivePage { case search, editorial, favorites, history }
struct Guide { var visualDirection: String? }
struct EditorialBeat { var beatId: String; var order: Int; var text: String; var projectEditorialGuidance: Guide? }
struct Board { var database: String; var beats: [EditorialBeat] }
struct EditorialSearchTarget: Equatable { var sessionId: String; var generation: Int; var database: String; var beatId: String }
struct Preflight { var databasePath: String }
struct Runtime { var databasePreflight: Preflight? }
struct Snapshot { var searchRuntime: Runtime }
final class Harness {
    var editorialSearchTarget: EditorialSearchTarget?
    var editorialBoard: Board? = Board(database: "/fixture/index.sqlite", beats: [
        EditorialBeat(beatId: "one", order: 1, text: "第一句", projectEditorialGuidance: Guide(visualDirection: "空间关系")),
        EditorialBeat(beatId: "two", order: 2, text: "第二句", projectEditorialGuidance: nil)])
    var snapshot: Snapshot? = Snapshot(searchRuntime: Runtime(databasePreflight: Preflight(databasePath: "/fixture/index.sqlite")))
    var editorialSessionId = "saved-session", editorialGeneration = 3, editorialActiveBeat = 0
    var editorialLoading = false, editorialSearchPending = false, searching = false
    var editorialSearchMessages: [String:String] = [:], editorialDecisionStatus = ""
    var page: ArchivePage = .editorial
    var query = "用户关键词", searchPathPrefix = "位置", searchDateFrom = "", searchDateTo = ""
    var searchRequireOCR = true, searchRequirePerson = true, mediaType = "视频"
    var activePersonClusterId = "", activePersonSourceId = "", activeSourceContentId = "", selectedPersonClusterId = ""
    var searchResults = ["frame-A"], bufferedSearchResults = ["frame-B"], searchCoverage: String? = "all"
    var searchTotalCount = 2, nextSearchOffset: Int? = 1, serverNextSearchOffset: Int? = 1, lastSearchSignature = "search"
    var selectedExportResults = ["frame-A":true], searchStatus = "", searchDiagnostic = "", navigation = ["previous"]
    // Sentinels: changing navigation must not clear human choices or cut points.
    var editorialDecisions = ["one::A":"selected", "two::B":"review"]
    var editorialCutOverrides = ["one::A":[1.25,3.5]], lockedCuts = ["one::A"]
    func clearSearchNavigation() { navigation = [] }
    func activateEditorialBeat(_ index: Int) { editorialActiveBeat = index }
    // METHODS
}
let m = Harness()
let decisions = m.editorialDecisions, cuts = m.editorialCutOverrides, locks = m.lockedCuts
// Restoring a project alone cannot make normal search sentence-bound.
precondition(m.editorialSearchBeat == nil)
m.openMainPage(.search)
precondition(m.editorialSearchBeat == nil && m.query == "用户关键词")
precondition(m.searchResults == ["frame-A"] && m.bufferedSearchResults == ["frame-B"])
precondition(m.navigation == ["previous"] && m.selectedExportResults == ["frame-A":true])
// Explicit opt-in still carries guidance; it does not start a search itself.
m.startEditorialSearch()
precondition(m.editorialSearchBeat?.beatId == "one" && m.query == "空间关系" && !m.searching)
m.bindEditorialSearchTarget("two")
precondition(m.editorialSearchBeat?.beatId == "two" && m.query == "空间关系")
m.returnFromEditorialSearch()
precondition(m.page == .editorial && m.editorialActiveBeat == 1 && m.editorialSearchTarget == nil)
precondition(!m.searchStatus.contains("第 ") && m.searchStatus.contains("搜索条件已保留"))
m.startEditorialSearch()
precondition(m.editorialSearchBeat?.beatId == "two" && m.query == "第二句")
// Main navigation or 'ordinary search' explicitly leaves the handoff.
let pendingTarget = m.editorialSearchTarget
m.editorialSearchMessages = ["A":"已加入"]
m.searchStatus = "找到2个素材"
m.editorialSearchPending = true
m.openMainPage(.favorites)
precondition(m.editorialSearchTarget == nil && m.editorialSearchMessages.isEmpty)
precondition(m.searchStatus == "找到2个素材")
precondition(m.editorialSearchTarget != pendingTarget) // late helper result is fenced out
precondition(m.editorialSearchPending) // only the helper callback owns this flag
m.editorialSearchPending = false
m.openMainPage(.search)
precondition(m.editorialSearchBeat == nil)
// Cross-library/generation targets remain invalid; ordinary navigation is safe.
m.snapshot = Snapshot(searchRuntime: Runtime(databasePreflight: Preflight(databasePath: "/fixture/other.sqlite")))
m.startEditorialSearch()
precondition(m.editorialSearchBeat == nil && !m.editorialDecisionStatus.isEmpty)
m.openMainPage(.search)
precondition(m.editorialSearchTarget == nil)
m.snapshot = Snapshot(searchRuntime: Runtime(databasePreflight: Preflight(databasePath: "/fixture/index.sqlite")))
m.startEditorialSearch(); m.editorialGeneration += 1
precondition(m.editorialSearchBeat == nil)
m.openMainPage(.history)
precondition(m.editorialDecisions == decisions && m.editorialCutOverrides == cuts && m.lockedCuts == locks)
precondition(m.editorialBoard!.beats.count == 2 && m.editorialSessionId == "saved-session")
print("PASS: ordinary/explicit/return/sidebar/stale-target/library/generation routes; choices and cuts preserved")
'''.replace('// METHODS', methods)
    swift = tmp_path / 'route.swift'
    swift.write_text(harness)
    binary = tmp_path / 'route'
    subprocess.run(['xcrun', 'swiftc', '-swift-version', '5', '-module-cache-path', str(ROOT / '.tmp-swift-cache'),
                    str(swift), '-o', str(binary)], check=True, capture_output=True, text=True, timeout=90)
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=10)
    assert result.stdout.startswith('PASS:')
