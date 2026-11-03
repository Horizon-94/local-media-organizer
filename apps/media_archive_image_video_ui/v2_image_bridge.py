"""Publish validated existing A9T-v3 JPEG previews; never decode original images."""
from pathlib import Path
from .v2_runtime import ContractError


def publish_preview(store,source,revision,slot,preview):
    path=Path(preview.get('output_path') or preview.get('visual_file') or '')
    role=preview.get('preview_role')
    if role not in ('normal_image','timelapse_keyframe','manual_finder_tag_timelapse_member'):
        raise ContractError('a9t_preview_role_not_publishable')
    with store.connect() as c:
        meta=c.execute('SELECT metadata,path FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
    if not meta or path.resolve()==Path(meta['path']).resolve():raise ContractError('a9t_derived_preview_required')
    import json
    if json.loads(meta['metadata']).get('media_type')!='image':raise ContractError('image_metadata_required')
    aid=store.publish(source,revision,slot,path)
    # Caller translates legacy visual IDs using this returned mapping before
    # binding the unchanged V25 image inputs with SourceSelection.bind_context.
    return {'legacy_visual_unit_id':preview.get('visual_unit_id'),'visual_unit_id':aid,'preview_role':role}
