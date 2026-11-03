"""Internal read-only mixed inventory. No decoding, models, or CLI entry point."""
from __future__ import annotations

from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

SOURCE_ROOT = Path(__import__('os').environ.get('MEDIA_ARCHIVE_SOURCE_ROOT', str(Path.home() / 'Movies'))).expanduser()


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def image_authority():
    path = Path(__file__).resolve().parents[1] / 'image-batch/image_inventory.py'
    spec = importlib.util.spec_from_file_location('_mixed_image_authority', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decoder_support(timeout=15):
    """Inspect installed decoder registry without opening or decoding media."""
    result = subprocess.run(['ffprobe', '-v', 'error', '-decoders'],
                            capture_output=True, text=True, timeout=timeout, check=True)
    return sorted({line.split()[1] for line in result.stdout.splitlines()
                   if len(line.split()) >= 2 and line.lstrip().startswith(('V', 'A'))})


def probe_metadata(path, timeout=15):
    command = ['ffprobe', '-v', 'error', '-threads', '1', '-probesize', '5000000',
               '-analyzeduration', '5000000', '-protocol_whitelist', 'file',
               '-show_format', '-show_streams', '-of', 'json', str(path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            return {'ok': False, 'reason_code': 'FFPROBE_FAILED',
                    'returncode': result.returncode, 'stderr': result.stderr}
        return {'ok': True, 'metadata': json.loads(result.stdout)}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'reason_code': 'FFPROBE_TIMEOUT'}
    except (OSError, ValueError) as exc:
        return {'ok': False, 'reason_code': 'FFPROBE_UNAVAILABLE_OR_INVALID',
                'error': str(exc)}


def inventory(root=SOURCE_ROOT, *, authority=None, probe=probe_metadata,
              decoders=decoder_support,include_audio=True,progress=None,defer_video_probe=False):
    authority = authority if authority is not None else image_authority()
    if progress:progress('DISCOVERING_SOURCES',source_root=str(root))
    import inspect
    base = authority.inventory(root,progress=progress) if progress and 'progress' in inspect.signature(authority.inventory).parameters else authority.inventory(root)  # One traversal.
    if progress:progress('INVENTORYING',source_count_discovered=len(base['files']),total_source_bytes=base['total_bytes'],inventory_complete=0,inventory_total=len(base['files']))
    support = sorted(set(decoders()))
    rows = []
    for inventory_position,original in enumerate(base['files'],1):
        if progress:progress('INVENTORYING',current_source=original['relative_path'],inventory_complete=inventory_position-1,inventory_total=len(base['files']))
        row = dict(original)
        ext = row['extension'].lower()
        kind = row['media_kind']
        if ext == '.thm':
            kind = 'sidecar'
        elif ext in authority._scanner['VIDEO_EXTS'] or ext in {'.crm', '.braw'}:
            kind = 'video'
        elif ext in authority._scanner['AUDIO_EXTS']:
            kind = 'audio'
        if kind=='video' and defer_video_probe:
            row['probe_pending']=True
        elif kind in ('video', 'audio') and (kind!='audio' or include_audio):
            result = probe(row['path'])
            row['probe'] = result
            streams = result.get('metadata', {}).get('streams', [])
            relevant = [s for s in streams if s.get('codec_type') == kind
                        and not s.get('disposition', {}).get('attached_pic')]
            supported = result.get('ok') and relevant and all(
                s.get('codec_name') in support for s in relevant)
            if not result.get('ok') and kind=='video':
                # A transient read/probe failure does not prove unsupported media.
                row['probe_pending']=True
                row['reason_code']=result.get('reason_code','FFPROBE_FAILED')
                row['probe_error']=result
            elif not supported:
                row['reason_code'] = ('PROBE_FAILED' if not result.get('ok')
                                      else 'DECODER_SUPPORT_UNCONFIRMED')
                kind = 'unsupported_' + kind
        row['media_kind'] = kind
        rows.append(row)
    result = {k: v for k, v in base.items() if k not in ('inventory_sha', 'files', 'counts', 'families')}
    result.update(contract='mixed-inventory-readonly-v1', files=rows,
                  counts=dict(Counter(r['media_kind'] for r in rows)),
                  decoder_support=support)
    # Preserve family membership, deriving classification lists from mixed rows.
    result['families'] = [dict(f, images=[r['source_id'] for r in rows
                            if r['family_id'] == f['family_id'] and r['media_kind'] == 'image'],
                            sidecars=[r['relative_path'] for r in rows
                            if r['family_id'] == f['family_id'] and r['media_kind'] == 'sidecar'])
                          for f in base.get('families', [])]
    result['inventory_sha'] = identity(result)
    if progress:progress('INVENTORYING',inventory_complete=len(rows),counts=result['counts'],inventory_total=len(rows))
    return result

def volume_evidence(source,workspace,inv):
    value=image_authority().storage_preflight(source,workspace,inv)
    return {key:value[key] for key in ('source_volume','workspace_volume','workspace_reason')}


def prepare_video(row, support, cache_root, *, probe=probe_metadata, cancel=lambda:False):
    """Read detailed metadata only at this source's admission boundary.
    Cache is task-owned; source files and model configuration remain read-only.
    """
    if row['media_kind']!='video' or not row.get('probe_pending'):return row
    from progressive_ingestion import atomic,read,identity,IngestionInterrupted
    if cancel():raise IngestionInterrupted('MEDIA_CHECK_INTERRUPTED')
    path=Path(row['path']);before=identity(path.stat())
    if (before['size'],before['mtime_ns'],before['inode']) != (row['bytes'],row['mtime_ns'],row['inode']):
        raise ValueError('INVENTORY_SOURCE_CHANGED: '+str(path))
    key=hashlib.sha256((row['source_id']+':'+row['relative_path']).encode()).hexdigest()
    cache_path=Path(cache_root)/'media_probe_cache'/(key+'.json')
    cached=read(cache_path,{})
    result=cached.get('probe') if cached.get('identity')==before and cached.get('path')==str(path) else None
    if not result or not result.get('ok'):
        # Bounded retries belong to metadata admission, not model/job Retry.
        # Failed probes never enter the success cache or disappear from inventory.
        for attempt in range(1,4):
            if cancel():raise IngestionInterrupted('MEDIA_CHECK_INTERRUPTED')
            result=probe(path)
            if result.get('ok'):break
            result={**result,'attempt':attempt,'path':str(path)}
        if not result.get('ok'):
            raise ValueError('MEDIA_READ_RETRY_REQUIRED: '+json.dumps(result,ensure_ascii=False))
    if cancel():raise IngestionInterrupted('MEDIA_CHECK_INTERRUPTED')
    if identity(path.stat())!=before:raise ValueError('SOURCE_CHANGED_DURING_MEDIA_CHECK: '+str(path))
    streams=result.get('metadata',{}).get('streams',[])
    relevant=[x for x in streams if x.get('codec_type')=='video' and not x.get('disposition',{}).get('attached_pic')]
    if not result.get('ok') or not relevant or not all(x.get('codec_name') in support for x in relevant):
        raise ValueError('MEDIA_CHECK_FAILED: '+json.dumps({'path':str(path),'probe':result},ensure_ascii=False))
    atomic(cache_path,{'identity':before,'path':str(path),'probe':result})
    prepared={**row,'probe':result,'probe_pending':False}
    prepared.pop('probe_error',None);prepared.pop('reason_code',None)
    return prepared


def commit_video_metadata(runtime,row,revision):
    """Persist newly verified inventory metadata through the existing fenced writer.
    No results, completion flags, search definitions or schema are changed.
    """
    if row['media_kind']!='video' or not getattr(runtime,'search_publication',False):return
    from apps.media_archive_image_video_ui.video_commit_queue import SQLCommand,WriteProposal
    patch=json.dumps({'probe':row['probe'],'probe_pending':False,'probe_error':None,'reason_code':None},sort_keys=True)
    sql="UPDATE release_registry SET row_json=json_patch(row_json,?) WHERE source_id=? AND revision=? AND present=1 AND json_extract(row_json,'$.probe_pending')=1"
    checks=((SQLCommand('SELECT owner,epoch FROM video_stream_control_v1 WHERE singleton=1'),(runtime.ledger.owner,runtime.ledger.epoch)),)
    runtime.ledger.commit_queue.submit(WriteProposal('source_media_metadata',row['source_id'],(SQLCommand(sql,(patch,row['source_id'],revision)),),checks)).result()
