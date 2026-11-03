"""Conservative source-local veto, not a replacement for frozen V25 scoring.

Only reject a nearly detail-free frame when the SAME source has a materially
clearer alternative. Missing/foreign metrics must never be treated as zero.
"""
import math

POLICY = 'source_local_low_detail_v1'


def low_detail_ids(frames):
    groups = {}
    for row in frames:
        if row.get('media_type') != 'video':
            continue
        value = row.get('sharpness_score')
        if row.get('sharpness_score_contract') != 'source_frame_dedup_central_db_v1':
            continue
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            continue
        groups.setdefault(row['source_content_id'], []).append(row)
    rejected = set()
    for rows in groups.values():
        best = max(row['sharpness_score'] for row in rows)
        if best < 3:
            continue
        for row in rows:
            structure = row.get('grid_structure')
            if (isinstance(structure, (int, float)) and math.isfinite(structure)
                    and structure < 3 and row['sharpness_score'] < 1
                    and row['sharpness_score'] < best * .15):
                rejected.add(row['visual_unit_id'])
    return rejected
