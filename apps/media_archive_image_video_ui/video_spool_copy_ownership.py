"""Prove copied spool ownership outside the writer transaction; never move data."""
from pathlib import Path
import hashlib,json,time
from .video_audio_recovery import owned_audio_path

class CopyOwnershipRequired(Exception):
    def __init__(self,row):self.row=tuple(row)

def stamp(path):
    s=path.stat();return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)

def evidence(c,source,revision,original,database):
    prefix=str(original)+'/'
    rows=list(c.execute('SELECT path,sha FROM v2_artifacts WHERE source_id=? AND revision=?',(source,revision)))
    audio=c.execute('SELECT audio_json FROM video_stream_inputs_v1 WHERE source_id=? AND revision=?',(source,revision)).fetchone()
    if audio and audio[0]:
        a=json.loads(audio[0])
        if a.get('path') and a.get('sha'):rows.append((a['path'],a['sha']))
    removed=set()
    for (raw,) in c.execute("SELECT payload FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state IN ('TRANSIENT_AUDIO_CLEANUP_INTENT','TRANSIENT_AUDIO_CLEANED')",(source,revision)):
        p=json.loads(raw);removed.update(p.get('removed_paths',p.get('paths',[])))
    return sorted({(p,h,Path(p).suffix.lower() in ('.mka','.wav','.flac') and (p in removed or str(owned_audio_path(database,p)) in removed)) for p,h in rows if p.startswith(prefix)})

def prove(budget,token,source,revision,row):
    original=Path(row[0]);local=budget.directory.resolve()/'spool'/original.name
    if original.is_symlink() or local.is_symlink() or owned_audio_path(budget.database,original)!=local.resolve():raise ValueError('spool_copy_path_unproven')
    os=stamp(original);ls=stamp(local)
    # macOS mount-session device numbers can change; the original inode may not.
    if os[1]!=row[2] or os[0]!=budget.directory.stat().st_dev:raise ValueError('spool_copy_original_owner_changed')
    with budget.connection() as c:
        current=c.execute('SELECT path,device,inode,state FROM video_spool_owners_v1 WHERE token=?',(token,)).fetchone()
        attempt=c.execute("SELECT attempt_id FROM video_stream_read_attempts_v1 WHERE source_id=? AND revision=? AND path=? AND status='EXTRACTION_DONE'",(source,revision,str(original))).fetchone()
        if tuple(current or ())!=row or not attempt or original.name!=revision+'-'+attempt[0]:raise ValueError('spool_copy_reader_identity_changed')
        records=evidence(c,source,revision,original,budget.database)
    witnesses=[]
    for p,digest,removed in records:
        target=local/Path(p).relative_to(original)
        if not target.exists() and removed:witnesses.append((str(target),None));continue
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(local):raise ValueError('spool_copy_artifact_missing')
        before=stamp(target);h=hashlib.sha256()
        with target.open('rb') as f:
            for block in iter(lambda:f.read(1048576),b''):h.update(block)
        if h.hexdigest()!=digest or stamp(target)!=before:raise ValueError('spool_copy_artifact_changed')
        witnesses.append((str(target),before))
    if stamp(original)!=os or stamp(local)!=ls:raise ValueError('spool_copy_directory_changed')
    return dict(token=token,source=source,revision=revision,row=row,original_stamp=os,local=str(local),local_stamp=ls,records=records,witnesses=witnesses)

def owned_directory(budget,c,row,source,revision,proof):
    original=Path(row[0])
    if original.parent.resolve()==budget.directory.resolve()/'spool':
        if original.is_symlink():raise ValueError('spool_directory_not_owned')
        return original,(row[1],row[2])
    if proof is None:raise CopyOwnershipRequired(row)
    if tuple(row)!=proof['row'] or (source,revision)!=(proof['source'],proof['revision']):raise ValueError('spool_copy_proof_stale')
    local=Path(proof['local'])
    if local.is_symlink() or local.parent.resolve()!=budget.directory.resolve()/'spool' or owned_audio_path(budget.database,original)!=local:raise ValueError('spool_copy_path_unproven')
    if stamp(original)!=proof['original_stamp'] or stamp(local)!=proof['local_stamp']:raise ValueError('spool_copy_directory_changed')
    if evidence(c,source,revision,original,budget.database)!=proof['records']:raise ValueError('spool_copy_evidence_changed')
    for p,expected in proof['witnesses']:
        path=Path(p)
        if expected is None:
            if path.exists() or path.is_symlink():raise ValueError('spool_copy_cleanup_changed')
        elif path.is_symlink() or stamp(path)!=expected:raise ValueError('spool_copy_artifact_changed')
    return local,proof['local_stamp'][:2]

def release(budget,method,token,source,revision,owner,epoch):
    try:return method(token,source,revision,owner,epoch)
    except CopyOwnershipRequired as request:
        proof=prove(budget,token,source,revision,request.row)
        result=method(token,source,revision,owner,epoch,copy_proof=proof)
        record={'at':time.time(),'token':token,'source_id':source,'revision':revision,'original_owner':proof['row'],'copy_path':proof['local'],'copy_stat':proof['local_stamp'],'artifact_count':len(proof['records']),'evidence_sha':hashlib.sha256(json.dumps(proof['records'],sort_keys=True).encode()).hexdigest(),'outcome':result}
        with (Path(budget.database).parent/'spool-copy-ownership.jsonl').open('a') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
        return result
