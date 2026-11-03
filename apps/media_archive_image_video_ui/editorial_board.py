"""Editorial v1: read-only current Search adapter and portable human decisions.

No retrieval/ranking implementation, inference, schema or source writes live here.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .editorial_session_core import (attach_candidate_snapshot, load_session,
    new_session, save_session, set_decision, validate_session)
from .video_content_timeline import _open_readonly

MANIFEST_CONTRACT = "editorial_manifest_v2"
DEFAULT_CANDIDATE_LIMIT = 8
IMAGE_HOLD_MS = 5000


def identity(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:24]


class CurrentLibrary:
    def __init__(self, database):
        self.database = Path(database)

    def resolve(self, source_id, revision=None):
        with _open_readonly(self.database) as con:
            row = con.execute("""SELECT a.source_revision,m.path,m.metadata
                FROM v2_active_sources a JOIN v2_source_metadata m
                ON m.source_id=a.source_id AND m.source_revision=a.source_revision
                WHERE a.source_id=?""", (source_id,)).fetchone()
        if not row or str(row[0]).startswith(("aborted:", "fenced:")):
            return {"resolved": False, "online": False, "reason": "SOURCE_NOT_CURRENT"}
        if revision is not None and str(row[0]) != str(revision):
            return {"resolved": False, "online": False, "reason": "STALE_SOURCE_REVISION"}
        meta = json.loads(row[2] or "{}")
        path = str(row[1] or "")
        return {"resolved": True, "source_revision": str(row[0]), "original_path": path,
                "online": bool(path and Path(path).is_file()), "metadata": meta,
                "source_type": str(meta.get("media_type", "image")).upper(),
                "reason": "" if path and Path(path).is_file() else "SOURCE_OFFLINE"}

    def evidence(self, source_id, revision, document_id=None, artifact_id=None):
        with _open_readonly(self.database) as con:
            rows = con.execute("""SELECT d.document_id,d.artifact_id,d.raw_result_ref,d.provenance,
                j.scheduler_run_id,j.execution_epoch,j.source_revision
                FROM v2_documents d JOIN v2_current_jobs j ON j.execution_key=d.raw_result_ref
                WHERE d.source_id=? AND d.source_revision=? AND j.status='DONE'
                AND j.source_id=d.source_id AND j.source_revision=d.source_revision
                ORDER BY d.document_id""", (source_id, revision)).fetchall()
        refs = [dict(row) for row in rows if
                (document_id and row['document_id'] == document_id) or
                (artifact_id and row['artifact_id'] == artifact_id)]
        for ref in refs:
            ref['provenance'] = json.loads(ref['provenance'] or '{}')
        return {"evidence_refs": refs}


def candidate_from_result(library, beat_id, result, *, manual=False):
    sid = str(result.get("source_id") or result.get("source_content_id") or "")
    current = library.resolve(sid, result.get("source_revision"))
    if not current['resolved'] or result.get('visible') is False or result.get('current') is False:
        return None
    clusters = result.get('match_clusters') or []
    cluster = result.get('top_match') or {}
    if not cluster.get('cluster_id') and clusters:
        cluster = clusters[0]
    video = current['source_type'] == 'VIDEO'
    cid = cluster.get('cluster_id') if video else None
    frame = cluster.get('representative_frame') or result
    anchor = int(cluster.get('representative_time_ms') or result.get('time_position_ms') or 0)
    start = max(0, int(cluster.get('start_time_ms', anchor))) if video else 0
    end = int(cluster.get('end_time_ms', anchor)) if video else IMAGE_HOLD_MS
    if end <= start:
        start = max(0, anchor - 2000); end = anchor + 2000
    duration = current['metadata'].get('duration_ms')
    if video and duration:
        end = min(end, int(duration)); start = min(start, max(0, end - 1))
    artifact = frame.get('visual_unit_id') or frame.get('artifact_id') or result.get('visual_unit_id')
    doc = frame.get('result_id') or result.get('result_id')
    provenance = library.evidence(sid, current['source_revision'], doc, artifact)
    if doc and not any(ref['document_id'] == doc for ref in provenance['evidence_refs']):
        return None
    return {'candidate_id': identity(sid, current['source_revision'], cid or artifact or doc or 'source'),
        'beat_id': beat_id, 'source_id': sid, 'source_revision': current['source_revision'],
        'source_type': current['source_type'], 'cluster_id': cid,
        'artifact_id': artifact, 'document_id': doc, 'representative_time_ms': anchor,
        'start_ms': start, 'end_ms': end,
        'time_basis': 'SEARCH_TEMPORAL_CLUSTER' if video else 'EDITORIAL_IMAGE_HOLD',
        'score': float(result.get('final_score') or result.get('score') or 0),
        'match_reasons': copy.deepcopy(result.get('match_reasons') or []),
        'provenance': provenance,
        'original_path': current['original_path'], 'online': current['online'],
        'display_name': result.get('display_name') or Path(current['original_path']).name,
        'preview_path': frame.get('preview_path') or result.get('representative_thumbnail'),
        'alternate_locations': copy.deepcopy(clusters), 'manual_origin': manual,
        'human_status': 'MANUAL' if manual else 'UNREVIEWED', 'locked': False}


def beat_for(session, beat_id):
    return next(row for row in session['beats'] if row['beat_id'] == beat_id)


def candidate_for(session, beat_id, candidate_id):
    return next(row for row in beat_for(session, beat_id)['candidate_snapshots']
                if row['candidate_id'] == candidate_id)


def key_for(beat_id, candidate_id):
    return f'{beat_id}::{candidate_id}'


def state_for(session, beat_id, candidate):
    key = key_for(beat_id, candidate['candidate_id'])
    decision = session['decisions'].get(key, {})
    return {**candidate, 'human_status': decision.get('status', candidate.get('human_status', 'UNREVIEWED')),
            'locked': decision.get('locked', candidate.get('locked', False)),
            'manual_origin': decision.get('manual_origin', False) or candidate.get('manual_origin', False),
            **(dict(zip(('start_ms', 'end_ms'), session['cut_overrides'][key]))
               if key in session['cut_overrides'] else {})}


def merge_results(session, beat_id, library, results, limit=DEFAULT_CANDIDATE_LIMIT):
    beat = beat_for(session, beat_id)
    old = {row['candidate_id']: row for row in beat['candidate_snapshots']}
    fresh = []
    for result in results[:max(1, min(30, int(limit)))]:
        candidate = candidate_from_result(library, beat_id, result)
        if candidate and candidate['candidate_id'] not in {row['candidate_id'] for row in fresh}:
            fresh.append(candidate)
    merged = []
    for candidate in fresh:
        cid = candidate['candidate_id']; previous = old.pop(cid, None)
        if previous:
            human = state_for(session, beat_id, previous)
            key = key_for(beat_id, cid)
            if human['human_status'] != 'UNREVIEWED' or human['locked'] or human['manual_origin'] or key in session['cut_overrides']:
                candidate = {**previous, 'currently_not_returned_by_search': False}
        merged.append(candidate)
    for previous in old.values():
        human = state_for(session, beat_id, previous)
        if human['human_status'] != 'UNREVIEWED' or human['locked'] or human['manual_origin'] or key_for(beat_id, previous['candidate_id']) in session['cut_overrides']:
            merged.append({**previous, 'currently_not_returned_by_search': True})
    session['candidate_snapshots'] = [row for row in session['candidate_snapshots'] if row.get('beat_id') != beat_id]
    beat['candidate_snapshots'] = []
    for candidate in merged:
        attach_candidate_snapshot(session, beat_id, candidate)
    return session


def refresh(session, beat_id, library, manager, limit=DEFAULT_CANDIDATE_LIMIT):
    # Formal V2 Search has a synchronous current projection path and no inference.
    # Reject legacy routing rather than accidentally launching query models.
    if manager is None or manager._active_scheduler_mode() != 'event_driven_v2':
        raise ValueError('Editorial requires the current V2 Search context (no model fallback)')
    job = manager.start({'query': beat_for(session, beat_id)['text'], 'offset': 0})
    payload = manager.results(job['job_id'])
    if not payload or payload.get('search_result_contract') != 'media_archive_source_search_v2':
        raise ValueError('current Search result contract unavailable')
    merge_results(session, beat_id, library, payload['results'], limit)
    session['search_progress'] = copy.deepcopy(payload.get('progress') or {})
    return session


def edit(session, library, beat_id, candidate_id, *, status=None, locked=None, trim=None, allow_duplicate=False):
    candidate = candidate_for(session, beat_id, candidate_id)
    human = state_for(session, beat_id, candidate)
    current = library.resolve(candidate['source_id'], candidate['source_revision'])
    if trim is not None:
        start, end = map(int, trim)
        if start < 0 or end <= start:
            raise ValueError('invalid trim range')
        duration = current.get('metadata', {}).get('duration_ms')
        if duration and candidate['source_type'] == 'VIDEO':
            end = min(end, int(duration))
            if start >= end:
                raise ValueError('trim outside current source duration')
        trim = (start, end)
    if status == 'SELECTED' and not allow_duplicate:
        for beat in session['beats']:
            if beat['beat_id'] == beat_id:
                continue
            for other in beat['candidate_snapshots']:
                if other['candidate_id'] == candidate_id and state_for(session, beat['beat_id'], other)['human_status'] == 'SELECTED':
                    raise ValueError('Exact visual/cluster already selected in another beat; explicit override required')
    set_decision(session, beat_id, candidate_id, status or human['human_status'],
                 locked=locked, trim=trim, manual=human['manual_origin'])
    return session


def board(session, library=None):
    result = copy.deepcopy(session)
    for beat in result['beats']:
        beat['candidate_snapshots'] = [state_for(session, beat['beat_id'], row) for row in beat['candidate_snapshots']]
        for row in beat['candidate_snapshots']:
            if library:
                current = library.resolve(row['source_id'], row['source_revision'])
                row['online'] = current['online']; row['resolution_reason'] = current['reason']
                row['preview_allowed'] = current['resolved'] and current['online']
    return {'session': result, 'model_run': False, 'database_write': False}


def manifest(session, library):
    validate_session(session)
    selected, backups, unresolved, items = [], [], [], []
    offset = 0
    for beat in sorted(session['beats'], key=lambda row: row['order']):
        primary = None
        for raw in beat['candidate_snapshots']:
            row = state_for(session, beat['beat_id'], raw)
            if row['human_status'] not in {'SELECTED', 'BACKUP'}:
                continue
            current = library.resolve(row['source_id'], row['source_revision'])
            row = {**row, 'resolved': current['resolved'], 'online': current['online'],
                   'resolved_path': current.get('original_path'), 'resolution_reason': current['reason'],
                   'source_metadata': current.get('metadata', {})}
            if not current['resolved'] or not current['online']:
                unresolved.append({'beat_id': beat['beat_id'], 'candidate_id': row['candidate_id'], 'reason': current['reason']})
            if row['human_status'] == 'SELECTED':
                primary = row; selected.append(row)
            else:
                backups.append(row)
        duration = primary['end_ms'] - primary['start_ms'] if primary else max(1, beat['estimated_narration_ms'])
        items.append({'beat_id': beat['beat_id'], 'beat_text': beat['text'], 'kind': 'clip' if primary else 'gap',
                      'timeline_start_ms': offset, 'duration_ms': duration, 'candidate': primary})
        offset += duration
    return {'contract': MANIFEST_CONTRACT, 'script_id': session['script_id'],
            'script_revision': session['script_revision'], 'beats': copy.deepcopy(session['beats']),
            'decisions': copy.deepcopy(session['decisions']), 'selected_clips': selected, 'backups': backups,
            'timeline_items': items, 'unresolved_items': unresolved,
            'session_snapshot': copy.deepcopy(session), 'model_run': False, 'database_write': False}


def import_manifest(payload, library):
    if payload.get('contract') != MANIFEST_CONTRACT:
        raise ValueError('Unsupported editorial manifest version')
    session = copy.deepcopy(validate_session(payload['session_snapshot']))
    session['unresolved_items'] = manifest(session, library)['unresolved_items']
    return session


def bridge_command(command, config, manager, request):
    library = CurrentLibrary(config['database']) if config.get('database') else None
    path = Path(request['project_path']).expanduser()
    if command == 'editorial-project-new':
        session = new_session(request['script'], timeline_name=request.get('timeline_name', 'Editorial rough cut'))
    elif command == 'editorial-import-manifest':
        payload = json.loads(Path(request['input_path']).read_text(encoding='utf-8'))
        if library is None:
            raise ValueError('Select a current library before importing a manifest')
        session = import_manifest(payload, library)
    else:
        session = load_session(path)
    bid = request.get('beat_id') or session['active_beat_id']
    if command == 'editorial-project-save':
        if request.get('active_beat_id'):
            beat_for(session, request['active_beat_id']); session['active_beat_id'] = request['active_beat_id']
    elif command == 'editorial-candidates-refresh':
        refresh(session, bid, library, manager, request.get('limit', DEFAULT_CANDIDATE_LIMIT))
    elif command == 'editorial-candidate-manual-add':
        if library is None:
            raise ValueError('Select a current library before adding candidates')
        result = request.get('search_result') or {'source_id': request['source_id']}
        candidate = candidate_from_result(library, bid, result, manual=True)
        if candidate is None:
            raise ValueError('manual source is not current')
        existing = next((row for row in beat_for(session, bid)['candidate_snapshots']
                         if row['candidate_id'] == candidate['candidate_id']), None)
        if existing is None:
            attach_candidate_snapshot(session, bid, candidate)
            edit(session, library, bid, candidate['candidate_id'], status='MANUAL')
    elif command in {'editorial-candidate-decision', 'editorial-candidate-trim', 'editorial-candidate-lock'}:
        edit(session, library, bid, request['candidate_id'], status=request.get('status'),
             locked=request.get('locked'), trim=request.get('trim'), allow_duplicate=request.get('allow_duplicate', False))
    elif command == 'editorial-preview':
        candidate = state_for(session, bid, candidate_for(session, bid, request['candidate_id']))
        current = library.resolve(candidate['source_id'], candidate['source_revision'])
        if not current['resolved'] or not current['online']:
            raise ValueError(current['reason'])
        return {'original_path': current['original_path'], 'source_type': candidate['source_type'],
                'start_ms': candidate['start_ms'], 'end_ms': candidate['end_ms'], 'model_run': False}
    elif command in {'editorial-manifest', 'editorial-fcpxml'}:
        if library is None:
            raise ValueError('Select a current library before exporting')
        payload = manifest(session, library)
        output = Path(request['output_path'])
        if command == 'editorial-fcpxml':
            from .editorial_fcpxml import build_fcpxml
            text = build_fcpxml(payload, session['timeline_name'], session['frame_rate'],
                                ffprobe='/opt/homebrew/bin/ffprobe')
        else:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        output.write_text(text, encoding='utf-8')
        return {'output_path': str(output), 'contract': payload['contract'], 'unresolved_items': payload['unresolved_items'], 'model_run': False}
    # Every human edit and navigation uses B1's atomic writer. Open is read-only.
    if command != 'editorial-project-open':
        save_session(session, path)
    return board(session, library)
