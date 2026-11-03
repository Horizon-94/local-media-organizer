"""One frozen A9T decision snapshot, before bounded image materialization.

Planning reads source metadata only. It is not an extraction, EOS, evidence or
completion event. Covered members require a separate durable coverage contract.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from .v2_producer import fingerprint
from .v2_runtime import ContractError


def plan_images(module, rows, finder_tags):
    rows=deepcopy(rows)
    by_source={r['source_content_id']:r for r in rows}
    if len(by_source)!=len(rows):raise ContractError('a9t_plan_duplicate_source_identity')
    for row in rows:
        row['finder_tags']=[r.get('tag_name') or r.get('tag_raw') for r in finder_tags
                            if r['source_content_id']==row['source_content_id']]
    items=module.build_items(rows)
    detected=module.detect_timelapse(items,module.exiftool_available())
    decisions,_=module.plan_jobs(items,{'normal':Path('normal'),'keyframes':Path('keyframes')},
                                detected[3],detected[4],detected[5])
    owners={}
    for item in decisions:
        if item.get('sequence_id') and item.get('should_generate_preview'):
            owners.setdefault(str(item['sequence_id']),[]).append(item['parent_source_content_id'])
    covered={}
    for item in decisions:
        if item['preview_role']=='timelapse_member_skipped':
            representatives=owners.get(str(item['sequence_id']),[])
            if not representatives:raise ContractError('a9t_plan_coverage_without_representatives')
            covered[item['parent_source_content_id']]=sorted(representatives)
    payload={'contract':'frozen_a9t_complete_snapshot_plan_v1','rows':rows,
             'algorithm_sha256':hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
             'finder_tags':deepcopy(finder_tags),'decisions':decisions,
             'detected':[detected[0],detected[1],detected[2],detected[3],
                         sorted(detected[4]),sorted(detected[5])],
             'covered_by':covered}
    return {**payload,'fingerprint':fingerprint(payload)}


def detection_for(plan, rows, finder_tags):
    payload={k:v for k,v in plan.items() if k!='fingerprint'}
    if fingerprint(payload)!=plan['fingerprint']:raise ContractError('a9t_plan_digest_mismatch')
    if finder_tags!=plan['finder_tags']:raise ContractError('a9t_plan_finder_tags_changed')
    known={r['source_content_id']:r for r in plan['rows']}
    for row in rows:
        normalized={**{k:v for k,v in row.items() if k!='_product_attempt_id'},'finder_tags':[r.get('tag_name') or r.get('tag_raw') for r in finder_tags
                                         if r['source_content_id']==row['source_content_id']]}
        if known.get(row['source_content_id'])!=normalized:
            raise ContractError('a9t_plan_source_snapshot_changed:'+row['source_content_id'])
    detected=deepcopy(plan['detected'])
    detected[4]=set(detected[4]);detected[5]=set(detected[5])
    return detected
