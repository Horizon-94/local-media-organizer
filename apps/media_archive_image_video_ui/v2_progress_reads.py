"""Read-only lifecycle projection with the product correlated lineage view."""
import json
from .central_database import connect
from .v2_current_jobs_cache import install_readonly_view
from .v2_product_lifecycle import PASSES


def read_lifecycle_summary(database, run_id):
    with connect(database, readonly=True) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_product_lifecycle'").fetchone():
            return None
        life = c.execute('SELECT * FROM v2_product_lifecycle WHERE scheduler_run_id=?', (run_id,)).fetchone()
        if not life:
            return None
        install_readonly_view(c)
        sources = list(c.execute('SELECT source_id,state,error FROM v2_product_sources WHERE scheduler_run_id=?', (run_id,)))
        jobs = list(c.execute("SELECT source_id,status,error FROM v2_current_jobs WHERE scheduler_run_id=? AND status IN ('FAILED_RETRYABLE','FAILED_FINAL')", (run_id,)))
        # Scoped repair adopts only its sources; failures on other sources of
        # the same task must remain visible, not disappear with the new run.
        if 'task_id' in {row[1] for row in c.execute('PRAGMA table_info(scheduler_runs)')}:
            jobs = list(c.execute("""SELECT j.source_id,j.status,j.error FROM v2_current_jobs j
                JOIN scheduler_runs r ON r.scheduler_run_id=j.scheduler_run_id
                WHERE r.task_id=(SELECT task_id FROM scheduler_runs WHERE scheduler_run_id=?)
                AND j.status IN ('FAILED_RETRYABLE','FAILED_FINAL')""",(run_id,)))
        outcome_errors = []
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_source_outcomes'").fetchone():
            for sid, payload in c.execute('''SELECT p.source_id,o.payload FROM v2_product_sources p
                JOIN v2_source_outcomes o ON o.source_id=p.source_id AND o.source_revision=p.source_revision
                WHERE p.scheduler_run_id=? AND o.status IN ('FAILED_FINAL','FAILED','ABORTED')''',(run_id,)):
                value=json.loads(payload)
                outcome_errors.append({'source_id':sid,'error':value.get('reason') or value.get('reason_code') or '来源未完成'})
    retryable = {r['source_id'] for r in sources if r['state']=='RETRYABLE'}
    retryable.update(r['source_id'] for r in jobs if r['status']=='FAILED_RETRYABLE')
    final = {r['source_id'] for r in sources if r['state']=='FAILED_FINAL'}
    final.update(r['source_id'] for r in jobs if r['status']=='FAILED_FINAL')
    return {'current_pass':PASSES[life['pass_index']], 'retryable_count':len(retryable),
            'failed_final_count':len(final), 'failed_source_list':sorted(final),
            'error_summary':{r['source_id']:r['error'] for r in outcome_errors+sources+jobs if r['error']}}
