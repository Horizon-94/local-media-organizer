"""Owned audio cleanup only after durable local SOURCE_COMPLETE.

The retained JPEG evidence is required by browsing/index reconstruction and
does not belong to this transient-audio cleanup contract.
"""
import hashlib,json
from pathlib import Path
from .v2_runtime import ContractError
from .v2_model_workers import sha_file
from .video_audio_recovery import owned_audio_path


def cleanup(runtime,source,revision):
    if not runtime.adapters.has_state(source,revision,'SOURCE_COMPLETE'):return False
    if runtime.adapters.has_state(source,revision,'TRANSIENT_AUDIO_CLEANED'):return True
    with runtime.ledger.connection() as c:
        row=c.execute('SELECT audio_json FROM video_stream_inputs_v1 WHERE source_id=? AND revision=?',(source,revision)).fetchone()
    audio=json.loads(row[0]) if row and row[0] else {}
    paths=[]
    if audio.get('status')=='READY':
        path=owned_audio_path(runtime.ledger.path,audio['path'])
        if not path.resolve().is_relative_to(runtime.root):raise ContractError('transient_audio_outside_owned_workspace')
        if path.exists() and sha_file(path)!=audio['sha']:raise ContractError('transient_audio_digest_changed')
        paths.append(path)
        identity=hashlib.sha256((audio['path']+audio['sha']).encode()).hexdigest()
        paths.append(runtime.root/'audio-model-inputs'/(identity+'.wav'))
    # Native packet audio from completed owned read attempts is intermediate,
    # too. Preserve JPEG evidence and original source media.
    with runtime.ledger.connection() as c:
        attempts=list(c.execute("SELECT attempt_id,path FROM video_stream_read_attempts_v1 WHERE source_id=? AND revision=? AND status='EXTRACTION_DONE'",(source,revision)))
    packet_proofs=[]
    for attempt in attempts:
        directory=owned_audio_path(runtime.ledger.path,attempt['path']);packet=directory/'source-audio.mka'
        if directory.name!=revision+'-'+attempt['attempt_id'] or not directory.resolve().is_relative_to(runtime.root.resolve()):raise ContractError('packet_audio_cleanup_owner_mismatch')
        if packet.is_file():
            if packet.is_symlink():raise ContractError('packet_audio_cleanup_symlink')
            packet_proofs.append({'path':str(packet),'sha256':sha_file(packet)})
            paths.append(packet)
    extra=getattr(runtime,'extra_transient_audio_paths',None)
    if extra is not None:paths.extend(owned_audio_path(runtime.ledger.path,p) for p in extra(source,revision))
    # Durable intent allows restart after an interruption between unlink and
    # the completed cleanup marker. No failure/RECOVERABLE source reaches here.
    runtime.adapters.state(source,revision,'TRANSIENT_AUDIO_CLEANUP_INTENT',
        {'paths':[str(p) for p in paths],'audio_sha':audio.get('sha'),'packet_audio':packet_proofs})
    for path in paths:
        if not path.resolve().is_relative_to(runtime.root):raise ContractError('transient_audio_cleanup_owner_mismatch')
        path.unlink(missing_ok=True)
    runtime.adapters.state(source,revision,'TRANSIENT_AUDIO_CLEANED',{'removed_paths':[str(p) for p in paths]})
    return True
