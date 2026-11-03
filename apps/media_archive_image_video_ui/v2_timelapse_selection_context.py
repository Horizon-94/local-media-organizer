"""Bounded sequence barrier for frozen V25 first/middle/last comparison.

No model work, extraction or planning changes. Other image/video sources keep
their source-level streaming; only representatives of the same group join.
"""
from .v2_producer import fingerprint
from .v2_runtime import ContractError


def sequence_members(plan, source):
    decisions=plan.get('decisions',[]) if plan else []
    own=next((r for r in decisions if r['parent_source_content_id']==source),None)
    if not own or own['preview_role']!='timelapse_keyframe':return []
    return [r for r in decisions if r.get('sequence_id')==own['sequence_id'] and r.get('should_generate_preview') and r['preview_role']=='timelapse_keyframe']


def prepare_context(selection, source, revision):
    members=sequence_members(getattr(selection,'sequence_plan',None),source)
    if not members:return
    dependencies={};mappings=[];representatives=[]
    with selection.store.connect() as c:
        for member in members:
            sid=member['parent_source_content_id']
            row=c.execute('''SELECT s.revision,s.eos FROM v2_active_sources a
                JOIN v2_sources s ON s.source_id=a.source_id AND s.revision=a.source_revision
                WHERE a.source_id=?''',(sid,)).fetchone()
            if not row or row['eos']!='EOS':raise ContractError('timelapse_representative_producer_pending:'+sid)
            if sid==source and row['revision']!=revision:raise ContractError('timelapse_source_revision_changed')
            artifacts=list(c.execute("SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? AND status='READY'",(sid,row['revision'])))
            if len(artifacts)!=1:raise ContractError('timelapse_representative_artifact_not_unique:'+sid)
            artifact=artifacts[0];aid=artifact['artifact_id'];dependencies[sid]=row['revision']
            mappings.append({'visual_unit_id':aid,'source_content_id':sid,'canonical_visual_unit_id':aid,
                             'visual_duplicate_group_id':fingerprint((None,aid)),'identity_status':'canonical'})
            representatives.append({'visual_unit_id':aid,'sequence_id':member['sequence_id'],
                                    'representative_position':member['representative_position'],
                                    'source_relative_path':member['source_relative_path'],
                                    'visual_file':artifact['path'],'parent_source_content_id':sid})
        existing=selection.context(c,source,revision)
    # Historical partial contexts cannot be silently overwritten. A separate
    # audited correction must preserve their already-valid model results.
    if existing:
        if any(existing['dependencies'].get(sid)!=rev for sid,rev in dependencies.items()):
            raise ContractError('timelapse_partial_context_requires_audited_reconciliation')
        return
    selection.bind_context(dependencies,{'source_assets':selection.sequence_source_assets,
        'finder_tags':selection.sequence_plan['finder_tags'],'visual_mappings':mappings,'timelapse_rows':representatives})
