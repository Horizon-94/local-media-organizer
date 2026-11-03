"""Product summary adapter; frozen inference dependency remains unchanged."""
from .v2_product_lifecycle import PASSES


class HeaderSummaryMixin:
    def summary(self):
        self.refresh()
        with self.store.connect() as c:
            life=c.execute('SELECT * FROM v2_product_lifecycle WHERE scheduler_run_id=?',(self.run,)).fetchone()
            rows=[dict(r) for r in c.execute('SELECT source_id,state,error FROM v2_product_sources WHERE scheduler_run_id=?',(self.run,))]
            # Same live lineage/status authority, without model result payloads.
            # Error-free DONE jobs contribute to none of the summary fields.
            # Keep DONE-with-error rows for exact error_summary compatibility.
            from .v2_source_lineage import current_filtered_rows
            jobs=[dict(r) for r in current_filtered_rows(c,"j.scheduler_run_id=? AND j.execution_epoch=? AND (j.status<>'DONE' OR j.error IS NOT NULL)",
                (self.run,self.epoch),columns='j.execution_key,j.source_id,j.status,j.error')]
            admissions=0
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_admissions'").fetchone():
                admissions=c.execute("SELECT COUNT(*) FROM scheduler_admissions WHERE scheduler_run_id=? AND execution_epoch=? AND status='ACTIVE'",(self.run,self.epoch)).fetchone()[0]
            final_outcomes={r[0] for r in c.execute("SELECT s.source_id FROM v2_product_sources s JOIN v2_source_outcomes o ON o.source_id=s.source_id AND o.source_revision=s.source_revision WHERE s.scheduler_run_id=? AND o.status IN ('FAILED_FINAL','ABORTED','FAILED')",(self.run,))}
        failed_sources={r['source_id'] for r in jobs if r['status']=='FAILED_FINAL'}
        failed_sources.update(final_outcomes)
        failed_sources.update(r['source_id'] for r in rows if r['state']=='FAILED_FINAL')
        retryable={r['source_id'] for r in jobs if r['status']=='FAILED_RETRYABLE'}
        retryable.update(r['source_id'] for r in rows if r['state']=='RETRYABLE')
        terminal=all(r['state']=='COMPLETE' or r['source_id'] in failed_sources for r in rows)
        unfinished=any(r['status'] in ('PENDING','RUNNING','FAILED_RETRYABLE') for r in jobs)
        return {'current_pass':PASSES[life['pass_index']],'retryable_count':len(retryable),
            'failed_final_count':len(failed_sources),'failed_source_list':sorted(failed_sources),
            'error_summary':{r['source_id']:r['error'] for r in rows+jobs if r['error']},
            'complete':bool(rows) and terminal and not unfinished and not admissions,
            'active_admissions':admissions,'source_count':len(rows),'successful_count':sum(r['state']=='COMPLETE' for r in rows)}
