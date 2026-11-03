"""Reuse a durable terminal closure only while all current authority agrees."""
import json
from .v2_projection_current_head import CurrentHeadFinalProjection


def current_closed_outcome(pipeline,source,revision):
    projection=pipeline.final_projection
    if not isinstance(projection,CurrentHeadFinalProjection):return None
    if not getattr(pipeline.store,'targeted_projection_invalidation_enabled',False):return None
    with pipeline.store.connect() as c:
        pipeline.store.assert_source_epoch(c,source,revision)
        planner=getattr(pipeline.store,'fanout_planner',None)
        if planner and planner.pending(c,source,revision):return None
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_product_sources'").fetchone():return None
        # No cached COMPLETE flag: active revision, EOS, current ancestry,
        # snapshot and every source-local unfinished job are checked afresh.
        row=c.execute('''SELECT o.payload,h.join_digest,j.provenance,j.execution_key
            FROM v2_active_sources a JOIN v2_sources s
              ON s.source_id=a.source_id AND s.revision=a.source_revision
            JOIN v2_product_sources p ON p.source_id=s.source_id AND p.source_revision=s.revision
            JOIN v2_source_outcomes o ON o.source_id=s.source_id AND o.source_revision=s.revision
            JOIN v2_source_closure h ON h.source_id=s.source_id AND h.source_revision=s.revision
            JOIN v2_final_projection_heads f ON f.source_id=s.source_id AND f.source_revision=s.revision
            JOIN v2_jobs j ON j.execution_key=f.execution_key
            JOIN v2_selection_snapshots snap ON snap.snapshot_id=f.snapshot_id
            WHERE s.source_id=? AND s.revision=? AND s.eos='EOS' AND p.state='COMPLETE'
              AND h.status='COMPLETE' AND o.status='COMPLETE'
              AND snap.status='DONE' AND j.stage='final_manifest' AND j.status='DONE'
            ''',(source,revision)).fetchone()
        if not row:return None
        from .v2_source_lineage import current_source_rows
        if not current_source_rows(c,source,revision,'j.execution_key=?',(row['execution_key'],),columns='j.execution_key,j.status'):return None
        if current_source_rows(c,source,revision,"j.status<>'DONE'",columns='j.execution_key,j.status'):return None
    if not row:return None
    outcome=json.loads(row['payload'])
    if outcome.get('status')!='COMPLETE' or outcome.get('join_digest')!=row['join_digest']:return None
    if json.loads(row['provenance']).get('policy')!=projection.policy_fingerprint():return None
    return outcome
