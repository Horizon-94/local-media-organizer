"""Skip only a proven current final head under targeted invalidation authority."""
import json
from pathlib import Path
from . import v2_projection as frozen
from .v2_producer import fingerprint


class CurrentHeadFinalProjection(frozen.FinalProjection):
    def policy_fingerprint(self):
        paths=[Path(frozen.__file__),frozen.SCRIPTS/'qwenvl_raw_media_v3.py',*[frozen.SCRIPTS/(n+'.py') for n in frozen.NAMES.values()],
            *[frozen.ROOT/'configs'/n for n in ('stop03_5a_joint_quality_audit_v1.json','stop03_5c_qwenvl_yolo_propagation_v1.json','stop03_5d_text_embedding_db_contract_v1.json')]]
        return fingerprint({str(p.relative_to(frozen.ROOT)):fingerprint(p.read_text()) for p in paths})

    def current_head(self,source,revision,snapshot):
        if not getattr(self.store,'targeted_projection_invalidation_enabled',False):return False
        with self.store.connect() as c:
            if not c.in_transaction:c.execute("BEGIN")
            # Locate only the requested head before asking for current ancestry.
            # A broad JOIN here rebuilds the whole lineage proof whenever any
            # source emits evidence. The final point query still rechecks the
            # head/snapshot, so a pointer change between reads fails closed.
            head=c.execute('SELECT execution_key FROM v2_final_projection_heads WHERE source_id=? AND source_revision=? AND snapshot_id=?',
                           (source,revision,snapshot)).fetchone()
            if not head:return False
            # Native late peer completion may leave a DONE head whose exact
            # semantic dependency set is incomplete. Do not skip recomputation.
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='video_peer_text_v1'").fetchone():
                from .video_peer_catalog import matching
                required={peer[0] for peer in matching(c,source,revision)}
                attached={row[0] for row in c.execute('SELECT parent_key FROM v2_job_dependencies WHERE child_key=?',(head['execution_key'],))}
                if not required.issubset(attached):return False
            row=c.execute("""SELECT j.provenance FROM v2_final_projection_heads h
                JOIN v2_current_jobs j USING(execution_key)
                JOIN v2_selection_snapshots s ON s.snapshot_id=h.snapshot_id
                WHERE j.execution_key=? AND h.source_id=? AND h.source_revision=? AND h.snapshot_id=?
                AND s.status='DONE' AND j.status='DONE' AND j.stage='final_manifest'""",(head['execution_key'],source,revision,snapshot)).fetchone()
        return bool(row and json.loads(row['provenance']).get('policy')==self.policy_fingerprint())

    def advance(self,source,revision,snapshot):
        # Late duplicate completions revoke affected heads in their exact
        # result transaction. Current ancestry also fences changed inputs.
        # Never use a cached COMPLETE flag or bypass that durable authority.
        if self.current_head(source,revision,snapshot):
            perf=getattr(self,'performance',None)
            if perf:perf.count('CURRENT_PROJECTION_HEAD_SKIP')
            return True
        return super().advance(source,revision,snapshot)
