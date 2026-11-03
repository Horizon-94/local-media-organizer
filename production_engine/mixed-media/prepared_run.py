"""Internal preparation/locking primitives; deliberately no user runner."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil

from mixed_inventory import identity

OUTPUT_ROOT = Path.home()/'Library/Application Support/素材大整理/1.3.2-build150/workspaces'


def code_identity(files):
    paths = sorted({str(Path(p).resolve(strict=True)) for p in files})
    if not paths:
        raise ValueError('EXPLICIT_CODE_FILES_REQUIRED')
    return {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}


def storage_budget(inv, workspace, *, profile=None, available=None, reserve=4 * 1024**3):
    """Budget derived artifacts only. Unknown is distinct from disk shortfall.

    profile must freeze preview decisions and the current SamplingPolicy body.
    Raster+header JPEG and 2x mono 16kHz s16 PCM bounds match the reader's
    reservation policy. Retained artifacts are charged separately from the
    full 8 GiB transient spool, never clipped to that cap.
    """
    if reserve < 0:
        raise ValueError('INVALID_RESERVE')
    anchor = Path(workspace).resolve()
    while not anchor.exists():
        anchor = anchor.parent
    available = shutil.disk_usage(anchor).free if available is None else available
    if available < 0:
        raise ValueError('INVALID_AVAILABLE')
    unknown = []
    def positive(value):
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (ValueError, TypeError):
            return None
    profile = profile or {}
    sampling = profile.get('sampling_policy', {})
    frozen = {'offset_ms': 2000, 'interval_ms': 3000, 'long_edge': 1280,
              'jpeg_quality': 3, 'explicit_timestamps_ms': None}
    if any(sampling.get(k, 'MISSING') != v for k, v in frozen.items()):
        unknown.append('FROZEN_SAMPLING_POLICY_REQUIRED')
    previews = profile.get('preview_required')
    images = sum(r['media_kind'] == 'image' for r in inv['files'])
    if type(previews) is not int or not 0 <= previews <= images:
        unknown.append('FROZEN_PREVIEW_REQUIRED_MISSING_OR_INVALID')
        previews = 0
    frames = 0
    audio_seconds = 0
    audio_native_bound=0
    oversized = []
    jpeg_bound = 1280 * 1280 * 3 + 65536
    videos = 0
    for row in inv['files']:
        kind = row['media_kind']
        if kind not in ('video', 'audio'):
            continue
        metadata = row.get('probe', {}).get('metadata', {})
        streams = metadata.get('streams')
        if not row.get('probe', {}).get('ok') or not isinstance(streams, list) or not streams:
            unknown.append(row['relative_path'] + ':STREAM_METADATA_UNKNOWN')
            continue
        relevant = [s for s in streams if s.get('codec_type') == kind
                    and not s.get('disposition', {}).get('attached_pic')]
        duration = positive(metadata.get('format', {}).get('duration'))
        if duration is None and relevant:
            durations = [positive(s.get('duration')) for s in relevant]
            if all(d is not None for d in durations):
                duration = max(durations)
        if duration is None or not relevant:
            unknown.append(row['relative_path'] + ':DURATION_OR_STREAM_UNKNOWN')
            continue
        has_audio = kind == 'audio' or any(s.get('codec_type') == 'audio' for s in streams)
        if has_audio:
            audio_seconds += duration
            audio_stream=next(s for s in streams if s.get('codec_type')=='audio')
            rate=positive(audio_stream.get('sample_rate'));channels=positive(audio_stream.get('channels'))
            if rate is None or channels is None:unknown.append(row['relative_path']+':AUDIO_RATE_CHANNELS_UNKNOWN')
            else:audio_native_bound+=math.ceil(duration*(rate*channels*4+16000*2*2))
        if kind == 'video':
            videos += 1
            # SamplingPolicy.count(round(seconds*1000)), plus the accepted
            # short-video adapter and one guard frame for extractor rounding.
            ms = round(duration * 1000)
            count = 1 if duration < 2 else (ms - 2000) // 3000 + 1
            frames += count + 1
            reader_count = max(1, int(max(0, duration - 2) // 3) + 2)
            source_spool = reader_count * jpeg_bound + math.ceil(duration * 64000) + 1048576
            if source_spool > 8 * 1024**3:
                oversized.append(row['relative_path'])
    cal=profile.get('storage_calibration')
    db_budget=(len(inv['files'])+frames)*8*1024**2
    metrics_budget=(len(inv['files'])+frames)*1024**2
    if cal:
        if (cal.get('verified_source_coverage') is not True or cal.get('inventory_sha')!=inv.get('inventory_sha') or type(cal.get('db_index_budget')) is not int or cal['db_index_budget']<4*1024**3):unknown.append('INVALID_STORAGE_CALIBRATION')
        else:db_budget=cal['db_index_budget'];metrics_budget=max(2*1024**3,(len(inv['files'])+frames)*256*1024)
    components = {'image_previews': previews * 8 * 1024**2,
                  'retained_video_jpeg': frames * jpeg_bound,
                  'audio_native_plus_pcm_caches': audio_native_bound,
                  'transient_spool': 8 * 1024**3 if videos else 0,
                  'db_index': db_budget,
                  'metrics': metrics_budget,
                  'safety': reserve}
    required = None if unknown else sum(components.values())
    return {'available': available, 'required': required,
            'shortfall': None if required is None else max(0, required - available),
            'enough_space': required is not None and available >= required and not oversized,
            'reason_code': 'STORAGE_METADATA_UNKNOWN' if unknown else
                           'SOURCE_EXCEEDS_SPOOL_CAP' if oversized else
                           'STORAGE_SHORTFALL' if available < required else 'OK',
            'unknown': unknown, 'oversized_sources': oversized, 'components': components,
            'preview_required': previews, 'video_frames_with_guard': frames,
            'audio_seconds': audio_seconds, 'jpeg_bound_bytes': jpeg_bound,
            'spool_hard_bytes': 8 * 1024**3,
            'calibration':cal,
            'estimate_policy': 'same-source-calibrated-v3' if cal else 'derived-artifacts-v2-no-original-copy'}


def _snapshot(inv, files, config):
    body = {k: v for k, v in inv.items() if k != 'inventory_sha'}
    if inv.get('inventory_sha') != identity(body):
        raise ValueError('INVALID_INVENTORY_SHA')
    files = list(files)
    if not files:
        raise ValueError('EXPLICIT_CODE_FILES_REQUIRED')
    modules = [Path(__file__).resolve(), Path(__file__).with_name('mixed_inventory.py'),
               Path(__file__).resolve().parents[1] / 'image-batch/image_inventory.py']
    return {'inventory_sha': inv['inventory_sha'], 'code': code_identity(files + modules),
            'config_sha': identity(config)}


def prepare(name, inv, code_files, config, *, output_root=OUTPUT_ROOT):
    """Explicitly create a fresh ticket directory, never a DB or result tree.

    output_root is injectable for isolated tests. Callers supply every runtime,
    module and configuration input; it is never inferred from Git HEAD.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', name):
        raise ValueError('UNSAFE_BATCH_NAME')
    root = Path(output_root)
    if root.is_symlink() or root.absolute() != root.resolve():
        raise ValueError('OUTPUT_ROOT_SYMLINK')
    source = Path(inv['source_root']).resolve(strict=True)
    destination = root / name
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('SOURCE_WORKSPACE_OVERLAP')
    snapshot = _snapshot(inv, code_files, config)
    config_body = json.loads(json.dumps(config, allow_nan=False))
    profile = config_body.get('pinned_profile')
    if not isinstance(profile, dict) or not profile:
        raise ValueError('PINNED_PROFILE_REQUIRED')
    budget = storage_budget(inv, root, profile=profile)
    if not budget['enough_space']:
        raise ValueError(budget.get('reason_code', 'STORAGE_SHORTFALL'))
    ticket = {'contract': 'mixed-prepared-v1', 'workspace': str(destination),
              'status': 'PREPARED_NOT_STARTED', 'run_id': name,
              'config_body': config_body, 'pinned_profile': profile,
              'source_root': str(source), **snapshot, 'storage_budget': budget}
    ticket['ticket_sha'] = identity(ticket)
    root.mkdir(parents=True, exist_ok=True)
    destination.mkdir()  # Exclusive: never reuse a historical workspace.
    (destination / 'runner.lock').touch(exist_ok=False)
    with (destination / 'ticket.json').open('x') as handle:
        json.dump(ticket, handle, sort_keys=True)
    return ticket


def monitor(workspace):
    """Read only; missing/unprepared workspaces remain untouched."""
    workspace = Path(workspace)
    if not (workspace / 'ticket.json').is_file():
        return {'status': 'waiting'}
    return {'status': 'prepared', 'ticket': json.loads((workspace / 'ticket.json').read_text())}


@contextmanager
def locked_runner(workspace, *, current_inventory, code_files, config):
    """One flock covers startup verification and the entire caller operation."""
    workspace = Path(workspace)
    if workspace.is_symlink():
        raise ValueError('WORKSPACE_SYMLINK')
    fd = os.open(workspace / 'runner.lock', os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ticket = json.loads((workspace / 'ticket.json').read_text())
        if ticket.get('ticket_sha') != identity({k: v for k, v in ticket.items() if k != 'ticket_sha'}):
            raise ValueError('TICKET_CHANGED')
        if ticket['workspace'] != str(workspace.resolve()):
            raise ValueError('WORKSPACE_CHANGED')
        if (ticket.get('status') != 'PREPARED_NOT_STARTED'
                or ticket.get('run_id') != workspace.name
                or identity(ticket.get('config_body')) != ticket['config_sha']
                or ticket.get('pinned_profile') != config.get('pinned_profile')):
            raise ValueError('PREPARED_INPUT_CHANGED')
        actual = current_inventory()  # Fresh inventory, after acquiring the lock.
        expected = _snapshot(actual, code_files, config)
        if actual['source_root'] != ticket['source_root'] or any(ticket[k] != v for k, v in expected.items()):
            raise ValueError('PREPARED_INPUT_CHANGED')
        yield ticket
    finally:
        os.close(fd)
