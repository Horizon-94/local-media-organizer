from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]/'apps/media_archive_image_video_ui_v125_candidate/native_frontend.swift'


def test_scrolling_rows_do_not_build_edit_forms():
    code = SOURCE.read_text()
    row = code.split('struct EditorialSelectionSummaryRow: View {',1)[1].split('enum EditorialSelectionFocus',1)[0]
    assert 'TextField(' not in row
    assert 'EditorialSelectionRow(' not in row
    assert '.frame(height: 112)' in row
    assert 'reviewEditorialBeat(beat.beatId)' in row
    assert 'previewEditorial(beat,candidate)' in row
    assert 'editorialLockedCuts.contains(selection.id)' in row


def test_one_live_editor_popover_keeps_existing_controls():
    code = SOURCE.read_text()
    panel = code.split('List(saved)',1)[1].split('private var exportPanel:',1)[0]
    assert 'EditorialSelectionSummaryRow(selection: row)' in panel
    assert '.popover(item: $editingSelection' in panel
    assert 'first(where: { $0.id == selection.id })' in panel
    assert 'EditorialSelectionRow(beat: current.beat, candidate: current.candidate)' in panel


def test_project_read_is_queued_and_generation_guarded():
    code = SOURCE.read_text()
    methods = code.split('    func openEditorialProject()',1)[1].split('    func nextEditorialBatch',1)[0]
    assert 'Data(contentsOf:' not in methods
    assert 'JSONSerialization.jsonObject' not in methods
    assert 'EditorialProjectReader.read(' in methods
    assert 'EditorialProjectReader.decode(' in methods
    assert 'self.editorialGeneration == generation' in methods
    assert 'self.editorialDecisions == choices' in methods
    assert 'self.editorialCutOverrides == cuts' in methods
    assert 'self.editorialLockedCuts == locks' in methods
    page = code.split('struct EditorialPage: View {',1)[1].split('struct EditorialReferencePage:',1)[0]
    assert '.padding(18).disabled(model.editorialLoading)' in page
