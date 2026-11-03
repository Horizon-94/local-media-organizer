"""Exact, release-approved implementation transition; durable job IDs stay put.

contracts.json remains the historical semantic/execution identity. The separate
lineage receipt identifies which attested implementation continues that identity.
No DB mutation, result relabelling, state reset, or transaction replay occurs here.
"""
import copy
import hashlib
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _changed(old,new,path=()):
    if isinstance(old,dict) and isinstance(new,dict) and old.keys()==new.keys():
        return [x for k in old for x in _changed(old[k],new[k],path+(k,))]
    return [] if old==new else [(path,old,new)]


def resolve_contracts(frozen,actual,manifest_path,approved_sha,pipeline_root,identity):
    if frozen is None or frozen==actual:
        return actual,None
    def reject(reason):
        raise ValueError('RESUME_CONTRACT_CHANGED:'+reason)
    try:
        raw=Path(manifest_path).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=approved_sha:reject('UNAPPROVED_MANIFEST')
        m=json.loads(raw)
        if m['APP_IDENTITY']!=identity:reject('APP_IDENTITY')
        # Model config fingerprints include the project's own runtime paths.
        # Match only explicit attested file transitions for another project;
        # config/model/prompt/policy changes are never eligible for this route.
        if digest(actual)!=m['NEW_CONTRACT_ID'] and m.get('PROJECT_SCOPED_FILE_TRANSITIONS'):
            scoped=_changed(frozen,actual);old_files={};new_files={}
            def accept(name,before,after):
                allowed=m['PROJECT_SCOPED_FILE_TRANSITIONS'].get(name,[])
                if [before,after] not in allowed:reject('UNAPPROVED_PROJECT_FILE_TRANSITION')
                old_files[name]=before;new_files[name]=after
            for path,before,after in scoped:
                if len(path)==4 and path[2]=='dependencies':accept(path[3],before,after)
                elif len(path)==3 and path[2]=='dependencies' and isinstance(before,dict) and isinstance(after,dict):
                    for name in before.keys()|after.keys():
                        if before.get(name)!=after.get(name):accept(name,before.get(name),after.get(name))
                elif len(path)==4 and path[:3] in (('video','whisper','search_publication_code'),('video','whisper','video_repair_code')):
                    accept('production_engine/mixed-media/'+path[3],before,after)
                elif len(path)==4 and path[0] in ('video','audio') and path[1:3]==('whisper','audio_core_code') and path[3] in ('asr_quality.py','audio_usable_documents.py'):
                    accept('production_engine/mixed-media/'+path[3],before,after)
                elif len(path)==3 and path[2] in ('code','preprocess'):pass
                else:reject('PROJECT_SEMANTIC_DIFF')
            if not old_files:reject('NO_PROJECT_IMPLEMENTATION_TRANSITION')
            m={**m,'OLD_CONTRACT_ID':digest(frozen),'NEW_CONTRACT_ID':digest(actual),
                'OLD_FILE_HASHES':old_files,'NEW_FILE_HASHES':new_files,
                'ALLOWED_CHANGED_FILES':sorted(old_files),'APPROVED_DIFF_PATHS':[list(p) for p,_,_ in scoped]}
        if m['OLD_CONTRACT_ID']!=digest(frozen):
            origin=m.get('APPROVED_SOURCE_CONTRACTS',{}).get(digest(frozen))
            permitted={'OLD_CONTRACT_ID','OLD_FILE_HASHES','NEW_FILE_HASHES','ALLOWED_CHANGED_FILES','APPROVED_DIFF_PATHS'}
            if not isinstance(origin,dict) or set(origin)!=permitted:reject('OLD_ID')
            m={**m,**origin}
            if m['OLD_CONTRACT_ID']!=digest(frozen):reject('OLD_ID')
        if m['NEW_CONTRACT_ID']!=digest(actual):reject('NEW_ID')
        if m['CHANGE_CLASSIFICATION']!='DURABILITY_LIVENESS_IMPLEMENTATION_CHANGE':reject('CLASSIFICATION')
        if m['CHAIN_ALLOWED'] is not False:reject('CHAIN_FORBIDDEN')
        changes=_changed(frozen,actual)
        if [list(p) for p,_,_ in changes]!=m['APPROVED_DIFF_PATHS']:reject('DIFF_PATHS')
        approved=m['ALLOWED_CHANGED_FILES'];seen=set()
        for path,old,new in changes:
            if len(path)==4 and path[2]=='dependencies':
                name=path[3]
            elif len(path)==3 and path[2]=='dependencies' and isinstance(old,dict) and isinstance(new,dict):
                # Exact approved dependency additions for this implementation.
                # The manifest digest and every old/new file hash stay mandatory.
                for name in old.keys()|new.keys():
                    if old.get(name)==new.get(name):continue
                    if name not in approved:reject('UNAPPROVED_FILE')
                    if old.get(name)!=m['OLD_FILE_HASHES'].get(name) or new.get(name)!=m['NEW_FILE_HASHES'].get(name):reject('FILE_TRANSITION')
                    seen.add(name)
                continue
            elif len(path)==4 and path[:3] in (('video','whisper','search_publication_code'),('video','whisper','video_repair_code')):
                name='production_engine/mixed-media/'+path[3]
            elif len(path)==4 and path[0] in ('video','audio') and path[1:3]==('whisper','audio_core_code') and path[3] in ('asr_quality.py','audio_usable_documents.py'):
                # Exact attested cancellation/owned-cleanup implementation transition.
                name='production_engine/mixed-media/'+path[3]
            elif len(path)==3 and path[2] in ('code','preprocess'):
                for c in (frozen[path[0]][path[1]],actual[path[0]][path[1]]):
                    if c['code']!=digest(c['dependencies']):reject('DERIVED_CODE')
                    if c['preprocess']!=digest(('existing-production-preprocess',c['dependencies'],c['packages'])):reject('DERIVED_PREPROCESS')
                continue
            else:reject('SEMANTIC_DIFF')
            if name not in approved:reject('UNAPPROVED_FILE')
            if old!=m['OLD_FILE_HASHES'][name] or new!=m['NEW_FILE_HASHES'][name]:reject('FILE_TRANSITION')
            seen.add(name)
        if seen!=set(approved):reject('INCOMPLETE_FILE_APPROVAL')
        root=Path(pipeline_root).resolve()
        for name,expected in m['IMPLEMENTATION_FILE_HASHES'].items():
            p=(root/name).resolve()
            if not p.is_relative_to(root):reject('FILE_OUTSIDE_RELEASE')
            if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:reject('IMPLEMENTATION_SHA:'+name)
        if not m['VALIDATION_EVIDENCE']:reject('NO_EVIDENCE')
    except (OSError,KeyError,TypeError,json.JSONDecodeError) as error:
        reject('INVALID_APPROVAL:'+type(error).__name__)
    receipt={k:m[k] for k in ('OLD_CONTRACT_ID','NEW_CONTRACT_ID','OLD_FILE_HASHES','NEW_FILE_HASHES','ALLOWED_CHANGED_FILES','CHANGE_CLASSIFICATION','VALIDATION_EVIDENCE','CREATED_AT','APP_IDENTITY')}
    receipt.update(MIGRATION_MANIFEST_SHA=approved_sha,
                   SEMANTIC_JOB_IDENTITY='HISTORICAL_CONTRACT_UNCHANGED',
                   IMPLEMENTATION_IDENTITY=m['NEW_CONTRACT_ID'],
                   HISTORICAL_DONE_RELABELLED=False,CHAIN_ALLOWED=False)
    return copy.deepcopy(frozen),receipt
