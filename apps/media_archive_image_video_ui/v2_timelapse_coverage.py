"""Audited A9T covered sources, not synthetic previews or decoder EOS.

An exclusive product owner publishes a new planning revision atomically with
revocation of the old visual lineage. Completion is a revocable reference to
the actual current, sealed and completed representative sources.
"""
import json
import time
from pathlib import Path

from .v2_raw_audio_lane import RawAwareProductLifecycle
from .v2_producer import fingerprint
from .v2_runtime import ContractError
from .v2_lifecycle_headers import HeaderSummaryMixin


class CoverageAwareProductLifecycle(HeaderSummaryMixin,RawAwareProductLifecycle):
    def __init__(self, *args, image_plan, driver=None, source_assets=None, **kwargs):
        self.image_plan = image_plan
        self.driver = driver
        self.source_assets = source_assets or image_plan['rows']
        super().__init__(*args, **kwargs)
        try:
            self.reconcile_coverage()
            if driver is not None:
                self.reconcile_representative_previews()
        except BaseException:
            self.close()
            raise

    def reconcile_representative_previews(self):
        """Rebind a misclassified representative without decoding it again.

        The existing validated JPEG is input to a new processing revision.
        Old visual results are revoked, not relabelled as new-model evidence.
        A durable intent makes publication/handoff restartable at every step.
        """
        from .v2_lineage import invalidate
        from .v2_model_workers import sha_file
        from .v2_producer import validate_jpeg
        decisions = {d['parent_source_content_id']: d for d in self.image_plan['decisions'] if d['should_generate_preview']}
        with self.store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS v2_a9t_preview_reuse(
                scheduler_run_id TEXT,source_id TEXT,planning_revision TEXT,
                previous_revision TEXT,new_revision TEXT,intent_json TEXT,status TEXT,
                PRIMARY KEY(scheduler_run_id,source_id,planning_revision))''')
        for sid, decision in decisions.items():
            if decision['preview_role'] == 'normal_image':
                continue
            with self.store.connect() as c:
                record = c.execute('SELECT * FROM v2_a9t_preview_reuse WHERE scheduler_run_id=? AND source_id=? AND planning_revision=?',
                                   (self.run, sid, self.image_plan['fingerprint'])).fetchone()
                if record and record['status'] == 'SEALED':
                    if c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?', (sid,)).fetchone()[0] != record['new_revision']:
                        raise ContractError('a9t_reuse_active_revision_changed:' + sid)
                    continue
                if record:
                    intent = json.loads(record['intent_json'])
                else:
                    old = c.execute('''SELECT m.*,s.eos FROM v2_active_sources a
                        JOIN v2_source_metadata m ON m.source_id=a.source_id AND m.source_revision=a.source_revision
                        JOIN v2_sources s ON s.source_id=a.source_id AND s.revision=a.source_revision
                        WHERE a.source_id=?''', (sid,)).fetchone()
                    if not old or old['eos'] != 'EOS':
                        continue
                    metadata = json.loads(old['metadata'])
                    job = metadata.get('a9t_plan') or {}
                    if job.get('preview_role') == decision['preview_role']:
                        continue
                    artifacts = list(c.execute("SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? AND status='READY'", (sid, old['source_revision'])))
                    if len(artifacts) != 1 or job.get('backend') != decision['backend']:
                        raise ContractError('a9t_representative_preview_reuse_unproven:' + sid)
                    if c.execute("SELECT 1 FROM v2_jobs WHERE source_id=? AND status='RUNNING'", (sid,)).fetchone():
                        raise ContractError('a9t_representative_live_worker:' + sid)
                    intent = {'old_revision': old['source_revision'], 'source_sha': old['source_sha'],
                              'original_path': old['path'], 'metadata': metadata,
                              'artifact': dict(artifacts[0]), 'decision': decision}
                    c.execute('INSERT INTO v2_a9t_preview_reuse VALUES(?,?,?,?,NULL,?,?)',
                              (self.run, sid, self.image_plan['fingerprint'], old['source_revision'], json.dumps(intent, sort_keys=True), 'PLANNED'))
            artifact = intent['artifact']
            if validate_jpeg(artifact['path'])[0] != artifact['sha'] or sha_file(intent['original_path']) != intent['source_sha']:
                raise ContractError('a9t_representative_reuse_fingerprint_changed:' + sid)
            attempt = fingerprint(('a9t_preview_reuse', self.run, sid, self.image_plan['fingerprint'], intent['old_revision']))
            metadata = {**intent['metadata'], 'a9t_plan': decision, 'planning_revision': self.image_plan['fingerprint'],
                        'preview_reused_from_revision': intent['old_revision']}
            revision = self.store.source_ready(sid, fingerprint((intent['source_sha'], self.image_plan['fingerprint'], 'preview_reuse')),
                                               intent['source_sha'], intent['original_path'], metadata, execution_attempt=attempt)
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                invalidate(c, [r[0] for r in c.execute('SELECT execution_key FROM v2_jobs WHERE source_id=? AND source_revision=?', (sid, intent['old_revision']))])
                pass_index = c.execute('SELECT pass_index FROM v2_product_lifecycle WHERE scheduler_run_id=?', (self.run,)).fetchone()[0]
                c.execute("INSERT OR IGNORE INTO v2_product_source_attempts VALUES(?,?,?,?,?,'INGESTING',?,?,NULL,NULL)",
                          (attempt, self.run, self.epoch, sid, pass_index, revision, time.time()))
                c.execute("UPDATE v2_product_sources SET state='INGESTING',attempt_id=?,source_revision=? WHERE scheduler_run_id=? AND source_id=?", (attempt, revision, self.run, sid))
                c.execute("UPDATE v2_a9t_preview_reuse SET status='PUBLISHING',new_revision=? WHERE scheduler_run_id=? AND source_id=? AND planning_revision=?", (revision, self.run, sid, self.image_plan['fingerprint']))
            with self.store.source_lease(sid, revision):
                visual = self.store.publish(sid, revision, 0, artifact['path'])
                self.store.finish(sid, revision, 0, success=True)
            mapping = {'visual_unit_id': visual, 'source_content_id': sid, 'canonical_visual_unit_id': visual,
                       'visual_duplicate_group_id': fingerprint(('a9t_reuse', visual)), 'identity_status': 'canonical'}
            timelapse = [{'visual_unit_id': visual, 'sequence_id': decision['sequence_id'],
                          'representative_position': decision['representative_position'],
                          'source_relative_path': decision['source_relative_path'], 'visual_file': artifact['path'],
                          'parent_source_content_id': sid}] if decision['preview_role'] == 'timelapse_keyframe' else []
            self.driver.pipeline.selection.bind_context({sid: revision},
                {'source_assets': self.source_assets, 'finder_tags': self.image_plan['finder_tags'],
                 'visual_mappings': [mapping], 'timelapse_rows': timelapse})
            self.ingested(sid, attempt, [{'source_id': sid, 'source_revision': revision}])
            with self.store.connect() as c:
                c.execute("UPDATE v2_a9t_preview_reuse SET status='SEALED' WHERE scheduler_run_id=? AND source_id=? AND planning_revision=?", (self.run, sid, self.image_plan['fingerprint']))
                self.store.emit(c, 'A9T_REPRESENTATIVE_PREVIEW_REUSED', sid, revision, 1,
                                {'previous_revision': intent['old_revision'], 'planning_revision': self.image_plan['fingerprint'],
                                 'artifact_sha': artifact['sha'], 'preprocessing_rerun': False})

    def reconcile_coverage(self):
        from .v2_model_workers import sha_file
        from .v2_a9t_plan import detection_for
        from .v2_lineage import invalidate
        plan = self.image_plan
        detection_for(plan, plan['rows'], plan['finder_tags'])
        decisions = {d['parent_source_content_id']: d for d in plan['decisions']}
        sources = {r['source_content_id']: r for r in plan['rows']}
        with self.store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS v2_a9t_plans(
                scheduler_run_id TEXT,planning_revision TEXT,algorithm_sha256 TEXT,
                plan_json TEXT,created_at REAL,PRIMARY KEY(scheduler_run_id,planning_revision))''')
            c.execute('INSERT OR IGNORE INTO v2_a9t_plans VALUES(?,?,?,?,?)',
                      (self.run, plan['fingerprint'], plan['algorithm_sha256'], json.dumps(plan, sort_keys=True), time.time()))
            c.execute('''CREATE TABLE IF NOT EXISTS v2_timelapse_coverage(
                scheduler_run_id TEXT, source_id TEXT, planning_revision TEXT,
                source_revision TEXT, previous_revision TEXT, group_id TEXT,
                role TEXT, reason TEXT, representatives_json TEXT,
                representative_artifacts_json TEXT, status TEXT, updated_at REAL,
                PRIMARY KEY(scheduler_run_id,source_id,planning_revision))''')
        # Only covered members are revised here. Eligible previews never rerun.
        for sid, representatives in plan['covered_by'].items():
            source = sources[sid]
            path = Path(source['source_path'])
            stat = path.stat()
            if source.get('source_size', stat.st_size) != stat.st_size or source.get('source_mtime_ns', stat.st_mtime_ns) != stat.st_mtime_ns:
                raise ContractError('a9t_coverage_original_changed:' + sid)
            with self.store.connect() as c:
                old = c.execute('SELECT * FROM v2_timelapse_coverage WHERE scheduler_run_id=? AND source_id=? AND planning_revision=?',
                                (self.run, sid, plan['fingerprint'])).fetchone()
                if old:
                    active = c.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?', (sid,)).fetchone()
                    if not active or active[0] != old['source_revision']:
                        raise ContractError('a9t_coverage_active_revision_changed:' + sid)
                    continue
            sha = sha_file(path)
            if (path.stat().st_size, path.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ContractError('a9t_coverage_original_changed_during_hash:' + sid)
            content = fingerprint(('a9t_coverage_v1', sha, plan['fingerprint'], sid))
            revision = self.store.policy.revision(content)
            metadata = {**source, 'media_type': 'image', 'visual_policy': 'TIMELAPSE_COVERED',
                        'planning_revision': plan['fingerprint'], 'a9t_decision': decisions[sid],
                        'representative_sources': representatives}
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                self.store.assert_current_owner()
                membership = c.execute('SELECT state FROM v2_product_sources WHERE scheduler_run_id=? AND source_id=?', (self.run, sid)).fetchone()
                if not membership:
                    raise ContractError('a9t_coverage_product_membership_missing:' + sid)
                if c.execute("SELECT 1 FROM v2_jobs WHERE source_id=? AND status='RUNNING'", (sid,)).fetchone():
                    raise ContractError('a9t_coverage_live_worker:' + sid)
                active = c.execute('SELECT * FROM v2_active_sources WHERE source_id=?', (sid,)).fetchone()
                previous = active['source_revision'] if active else None
                if previous:
                    invalidate(c, [r[0] for r in c.execute('SELECT execution_key FROM v2_jobs WHERE source_id=? AND source_revision=?', (sid, previous))])
                self.store.register(c, sid, revision)
                c.execute('INSERT INTO v2_source_metadata VALUES(?,?,?,?,?,?)',
                          (sid, revision, content, sha, str(path.resolve()), json.dumps(metadata, sort_keys=True)))
                c.execute("UPDATE v2_sources SET eos='TIMELAPSE_COVERED',final_sequence=-1,error='',version=1 WHERE source_id=? AND revision=?", (sid, revision))
                c.execute('INSERT OR REPLACE INTO v2_active_sources VALUES(?,?,?)', (sid, revision, active['generation'] + 1 if active else 1))
                c.execute('INSERT INTO v2_timelapse_coverage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                          (self.run, sid, plan['fingerprint'], revision, previous, str(decisions[sid]['sequence_id']),
                           decisions[sid]['preview_role'], 'frozen_a9t_representative_coverage',
                           json.dumps(representatives), '[]', 'WAITING_REPRESENTATIVES', time.time()))
                c.execute("UPDATE v2_product_sources SET state='TIMELAPSE_COVERED',source_revision=?,error=NULL WHERE scheduler_run_id=? AND source_id=?", (revision, self.run, sid))
                self.store.emit(c, 'TIMELAPSE_COVERAGE_PLANNED', sid, revision, 1,
                                {'planning_revision': plan['fingerprint'], 'previous_revision': previous,
                                 'representatives': representatives, 'decision': decisions[sid]})

    def refresh(self):
        super().refresh()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = list(c.execute('''SELECT r.* FROM v2_timelapse_coverage r
                JOIN v2_active_sources a ON a.source_id=r.source_id AND a.source_revision=r.source_revision
                WHERE r.scheduler_run_id=?''', (self.run,)))
            proofs = {}
            for sid in {s for row in rows for s in json.loads(row['representatives_json'])}:
                rep = c.execute('''SELECT p.state,a.source_revision,s.eos,o.status outcome,h.status closure
                    FROM v2_product_sources p JOIN v2_active_sources a ON a.source_id=p.source_id
                    JOIN v2_sources s ON s.source_id=a.source_id AND s.revision=a.source_revision
                    LEFT JOIN v2_source_outcomes o ON o.source_id=s.source_id AND o.source_revision=s.revision
                    LEFT JOIN v2_source_closure h ON h.source_id=s.source_id AND h.source_revision=s.revision
                    WHERE p.scheduler_run_id=? AND p.source_id=? AND p.source_revision=a.source_revision''', (self.run, sid)).fetchone()
                refs = list(c.execute("SELECT artifact_id,sha FROM v2_artifacts WHERE source_id=? AND revision=? AND status='READY'", (sid, rep['source_revision']))) if rep else []
                valid = rep and rep['eos'] == 'EOS' and rep['state'] == 'COMPLETE' and rep['outcome'] in ('COMPLETE', 'DEGRADED') and rep['closure'] == 'COMPLETE' and refs
                proofs[sid] = (bool(valid), [{'source_id': sid, 'source_revision': rep['source_revision'], 'artifact_id': r['artifact_id'], 'sha': r['sha']} for r in refs] if valid else [])
            for row in rows:
                artifacts = []
                ready = True
                for sid in json.loads(row['representatives_json']):
                    valid, refs = proofs[sid]
                    if not valid:
                        ready = False
                        break
                    artifacts.extend(refs)
                status = 'COMPLETE' if ready else 'WAITING_REPRESENTATIVES'
                state = 'COMPLETE' if ready else 'TIMELAPSE_COVERED'
                if status == row['status'] and json.loads(row['representative_artifacts_json']) == artifacts:
                    continue
                self.store.assert_source_epoch(c, row['source_id'], row['source_revision'])
                c.execute('UPDATE v2_timelapse_coverage SET status=?,representative_artifacts_json=?,updated_at=? WHERE scheduler_run_id=? AND source_id=? AND planning_revision=?',
                          (status, json.dumps(artifacts, sort_keys=True), time.time(), self.run, row['source_id'], row['planning_revision']))
                c.execute('UPDATE v2_product_sources SET state=? WHERE scheduler_run_id=? AND source_id=?', (state, self.run, row['source_id']))
                payload = {'planning_revision': row['planning_revision'], 'representative_artifacts': artifacts}
                c.execute('INSERT OR REPLACE INTO v2_source_outcomes VALUES(?,?,?,?,?,?)',
                          (row['source_id'], row['source_revision'], 'COMPLETE' if ready else 'WAITING',
                           'timelapse_representative_coverage', '', json.dumps(payload, sort_keys=True)))
                self.store.emit(c, 'TIMELAPSE_COVERAGE_COMPLETE' if ready else 'TIMELAPSE_COVERAGE_REVOKED',
                                row['source_id'], row['source_revision'], 2, payload)

    def admitted(self):
        return [r for r in super().admitted() if r['source_id'] not in self.image_plan['covered_by']]
