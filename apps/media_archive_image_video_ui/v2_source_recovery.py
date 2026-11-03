"""Product-only recovery boundary; pinned inference dependencies stay frozen."""
import json
import time
from pathlib import Path
from .v2_product_lifecycle import ProductLifecycle
from .v2_runtime import ContractError


class RecoveringProductLifecycle(ProductLifecycle):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Super acquired the exclusive execution lease and fenced ownership.
        # Only this owner may reconcile interrupted producer state.
        try:self.reconcile_aborted()
        except Exception:
            self.close()
            raise

    def reconcile_aborted(self):
        self.store.assert_current_owner()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=list(c.execute("""SELECT p.*,a.source_revision active_revision
              FROM v2_product_sources p JOIN v2_active_sources a ON a.source_id=p.source_id
              WHERE p.scheduler_run_id=? AND p.state IN ('INGESTED','COMPLETE')
              AND a.source_revision LIKE 'aborted:%'""",(self.run,)))
            for row in rows:
                revision=row['active_revision'][len('aborted:'):]
                lineage=c.execute('SELECT * FROM v2_source_epochs WHERE source_id=? AND source_revision=? AND scheduler_run_id=? AND execution_epoch=?',(row['source_id'],revision,self.run,self.epoch)).fetchone()
                progress=c.execute('SELECT eos,error FROM v2_sources WHERE source_id=? AND revision=?',(row['source_id'],revision)).fetchone()
                if not lineage or not progress or progress['eos']!='ABORTED':
                    raise ContractError('aborted_recovery_lineage_unproven:'+row['source_id'])
                # A RUNNING decoder is not guessed away, even with a tombstone.
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_decode_attempts'").fetchone():
                    if c.execute("SELECT 1 FROM v2_decode_attempts WHERE source_id=? AND status='RUNNING'",(row['source_id'],)).fetchone():
                        raise ContractError('aborted_recovery_decoder_ownership_unproven:'+row['source_id'])
                    reports=[json.loads(r[0]) for r in c.execute('SELECT report FROM v2_decode_attempts WHERE source_id=? AND source_revision=?',(row['source_id'],revision))]
                else: reports=[]
                path=Path(json.loads(row['source_json'])['source_path'])
                unsupported=any(r.get('checkpoint_status')=='failed' and r.get('valid_jpg1280_count')==0 and 'no decoder found for:' in r.get('ffmpeg_stderr_tail','').lower() for r in reports)
                reason='source_missing' if not path.is_file() else ('decoder_unavailable' if unsupported else 'producer_aborted_recovery_pending')
                state='FAILED_FINAL' if reason in ('source_missing','decoder_unavailable') else 'NEW'
                now=time.time()
                c.execute('UPDATE v2_product_sources SET state=?,error=?,first_failed_at=COALESCE(first_failed_at,?),last_failed_at=? WHERE scheduler_run_id=? AND source_id=?',(state,reason,now,now,self.run,row['source_id']))
                if row['attempt_id']:
                    c.execute("UPDATE v2_product_source_attempts SET status='FAILED_FINAL',error=?,finished_at=COALESCE(finished_at,?) WHERE attempt_id=? AND status='SUCCESS'",('ingest_without_eos:'+reason,now,row['attempt_id']))
                payload={'status':state,'reason_code':reason,'previous_revision':revision,'recovery_contract':'exclusive_owner_new_attempt_or_evidence_terminal_v1'}
                if state=='FAILED_FINAL':
                    c.execute('INSERT OR IGNORE INTO v2_source_outcomes VALUES(?,?,?,?,?,?)',(row['source_id'],revision,'FAILED_FINAL',reason,'',json.dumps(payload,sort_keys=True)))
                self.store.emit(c,'SOURCE_RECOVERY_RECONCILED',row['source_id'],revision,1,payload)

    def ingested(self, sid, attempt, items):
        revision=next(r['source_revision'] for r in items if r['source_id']==sid)
        with self.store.connect() as c:
            row=c.execute('''SELECT s.eos,a.source_revision FROM v2_sources s
              JOIN v2_active_sources a ON a.source_id=s.source_id
              WHERE s.source_id=? AND s.revision=?''',(sid,revision)).fetchone()
            if not row or row['eos']!='EOS' or row['source_revision']!=revision:
                raise ContractError('producer_ingest_without_current_eos:'+sid)
        return super().ingested(sid,attempt,items)

    def failed_ingest(self, sid, attempt, error):
        super().failed_ingest(sid,attempt,error)
        # On Resume NEW makes recovery retry with a fresh attempt/revision.
        # Tombstones/history are never overwritten or activated as valid work.
        self.reconcile_aborted()
