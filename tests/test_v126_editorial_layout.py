"""Editorial layout is presentation only; navigation must preserve edits."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift'


def test_status_and_sidebar_have_independent_space():
    code = SOURCE.read_text()
    root = code.split('struct RootView: View {', 1)[1].split('func runCheckAndExit', 1)[0]
    assert '@AppStorage("layout.sidebarVisible")' in root
    assert 'if sidebarVisible { Sidebar(); Divider() }' in root
    assert 'accessibilityLabel(sidebarVisible ? "隐藏左侧导航栏" : "显示左侧导航栏")' in root
    assert '.keyboardShortcut("s", modifiers: [.control, .command])' in root
    assert '.fixedSize(horizontal: false, vertical: true).layoutPriority(1)' in root
    assert '.frame(minWidth: 1000, minHeight: 680)' in root
    assert 'if model.page == .editorial && model.editorialBoard != nil' in root
    assert '.fullSizeContentView' not in code.split('let window = NSWindow(contentRect:', 1)[1]
    sidebar = code.split('struct Sidebar: View {', 1)[1].split('private let editorialReferenceTopics', 1)[0]
    assert 'ScrollView {' in sidebar and 'model.openMainPage(page)' in sidebar


def test_input_and_export_no_longer_squeeze_candidate_box():
    page = SOURCE.read_text().split('struct EditorialPage: View {', 1)[1].split('struct EditorialReferencePage:', 1)[0]
    body = page.split('private var inputPanel:', 1)[0]
    assert 'if model.editorialBoard == nil {' in body
    assert '.sheet(isPresented: $inputPresented)' in body
    assert '.sheet(isPresented: $exportPresented)' in body
    assert 'Button("文稿与指导 · 步骤 1")' in body
    assert 'Button("收起步骤 1，继续选片")' in body
    assert 'Button("打开导出设置…")' in body
    box = page.split('private var decisionPanel:', 1)[1].split('private var exportPanel:', 1)[0]
    assert 'TextField(' not in box and 'editorialFrameRate' not in box
    assert 'List(saved)' in box and '.buttonStyle(.borderless)' in box
    export = page.split('private var exportPanel:', 1)[1]
    for value in ['editorialTimelineName', 'editorialFrameRate', 'editorialIncludeBackups',
                  'exportEditorialTimeline()', 'exportEditorialJSON()', 'editorialExportStatus',
                  '24000/1001', '30000/1001', '60000/1001', '120000/1001']:
        assert value in export
    for key in ['inputPanel', 'candidatePanel', 'exportPanel']:
        panel = page.split('private var ' + key + ':', 1)[1]
        assert 'Panel {\n          ScrollView {' in panel[:100]


def test_box_scroll_only_follows_explicit_progress_events():
    code = SOURCE.read_text()
    box = code.split('private var decisionPanel:', 1)[1].split('private var exportPanel:', 1)[0]
    assert '.id(row.id)' in box
    assert '.onAppear {' in box and 'DispatchQueue.main.async' in box
    assert '.onChange(of: activeOrder)' in box
    assert r'.onChange(of: saved.map(\.id))' in box
    assert '定位到第' in box
    for not_a_scroll_trigger in ['editorialSessionStatus', '.onReceive', 'Timer.', 'geometry', 'withAnimation']:
        assert not_a_scroll_trigger not in box
    row = code.split('struct EditorialSelectionSummaryRow:', 1)[1].split('enum EditorialSelectionFocus', 1)[0]
    assert 'selected ? Color.green.opacity(0.08) : Color.orange.opacity(0.08)' in row
    assert '.frame(height: 112)' in row
    assert 'TextField(' not in row


def test_production_focus_rule_uses_current_sentence_not_first_or_latest(tmp_path):
    code = SOURCE.read_text()
    helper = 'enum EditorialSelectionFocus' + code.split('enum EditorialSelectionFocus', 1)[1].split('struct EditorialPage:', 1)[0]
    harness = '''
struct Beat { var order: Int }
struct EditorialSavedSelection { var id: String; var beat: Beat }
''' + helper + '''
func row(_ order: Int, _ role: String) -> EditorialSavedSelection {
    EditorialSavedSelection(id: "\\(order)::\\(role)", beat: Beat(order: order))
}
let rows = [row(1,"main"),row(36,"main"),row(37,"main"),row(37,"backup"),row(72,"main"),row(73,"main"),row(90,"backup")]
precondition(EditorialSelectionFocus.targetID(rows,activeOrder:37) == "37::backup")
precondition(EditorialSelectionFocus.targetID(rows,activeOrder:73) == "73::main")
precondition(EditorialSelectionFocus.targetID(rows,activeOrder:71) == "37::backup")
precondition(EditorialSelectionFocus.targetID(rows,activeOrder:74) == "73::main")
precondition(EditorialSelectionFocus.targetID(rows,activeOrder:150) == "90::backup")
precondition(EditorialSelectionFocus.targetID([row(90,"backup")],activeOrder:1) == "90::backup")
precondition(EditorialSelectionFocus.targetID([],activeOrder:73) == nil)
print("PASS: current sentence, earlier fallback, before first, after last, empty, main+backup")
'''
    swift = tmp_path/'focus.swift'; swift.write_text(harness)
    binary = tmp_path/'focus'
    subprocess.run(['xcrun','swiftc','-module-cache-path',str(ROOT/'.tmp-swift-cache'),str(swift),'-o',str(binary)],
                   check=True,capture_output=True,text=True,timeout=60)
    result = subprocess.run([str(binary)],check=True,capture_output=True,text=True,timeout=10)
    assert result.stdout.startswith('PASS:')
