-- Local Media Organizer 1.3.0 product migration.
-- This file is applied only by database_migration_1_3_0.py.  The runtime
-- CREATE IF NOT EXISTS calls remain defensive compatibility guards; they are
-- not the product upgrade path.

CREATE TABLE IF NOT EXISTS scheduler_project_defaults (
    task_id TEXT PRIMARY KEY,
    scheduler_mode TEXT NOT NULL CHECK(scheduler_mode IN ('legacy_batch','event_driven_v2')),
    pipeline_contract_version TEXT NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scheduler_runs (
    scheduler_run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    scheduler_mode TEXT NOT NULL CHECK(scheduler_mode IN ('legacy_batch','event_driven_v2')),
    execution_epoch INTEGER NOT NULL CHECK(execution_epoch > 0),
    pipeline_contract_version TEXT NOT NULL,
    product_schema_version INTEGER NOT NULL,
    source_processing_revision TEXT NOT NULL DEFAULT '',
    request_id TEXT UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('STARTING','RUNNING','RECOVERABLE','COMPLETED','FAILED','ABORTED','CANCELLED')),
    created_at REAL NOT NULL,
    started_at REAL NOT NULL,
    recovered_at REAL,
    finished_at REAL,
    FOREIGN KEY(task_id) REFERENCES archive_tasks(task_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS scheduler_one_active_run_per_task
    ON scheduler_runs(task_id) WHERE status IN ('STARTING','RUNNING','RECOVERABLE');
CREATE INDEX IF NOT EXISTS scheduler_runs_task_epoch
    ON scheduler_runs(task_id, execution_epoch);
CREATE INDEX IF NOT EXISTS scheduler_runs_task_status
    ON scheduler_runs(task_id, status);

CREATE TABLE IF NOT EXISTS v2_sources (
    source_id TEXT NOT NULL, revision TEXT NOT NULL, policy_hash TEXT NOT NULL,
    policy_json TEXT NOT NULL, produced_frontier INTEGER NOT NULL DEFAULT -1,
    eos TEXT NOT NULL DEFAULT 'OPEN', final_sequence INTEGER,
    version INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(source_id, revision)
);
CREATE TABLE IF NOT EXISTS v2_source_metadata (
    source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    content_revision TEXT NOT NULL, source_sha TEXT NOT NULL, path TEXT NOT NULL,
    metadata TEXT NOT NULL, PRIMARY KEY(source_id, source_revision)
);
CREATE TABLE IF NOT EXISTS v2_active_sources (
    source_id TEXT PRIMARY KEY, source_revision TEXT NOT NULL, generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_source_leases (
    source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    token TEXT NOT NULL, expires_at REAL NOT NULL,
    PRIMARY KEY(source_id, source_revision)
);
CREATE TABLE IF NOT EXISTS v2_source_outcomes (
    source_id TEXT NOT NULL, source_revision TEXT NOT NULL, status TEXT NOT NULL,
    reason_code TEXT NOT NULL, execution_key TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(source_id, source_revision)
);
CREATE TABLE IF NOT EXISTS v2_source_closure (
    source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    join_digest TEXT NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY(source_id, source_revision)
);

CREATE TABLE IF NOT EXISTS v2_sample_slots (
    source_id TEXT NOT NULL, source_revision TEXT NOT NULL, slot INTEGER NOT NULL,
    timestamp_ms INTEGER NOT NULL, status TEXT NOT NULL, artifact_id TEXT,
    error TEXT NOT NULL, PRIMARY KEY(source_id, source_revision, slot)
);
CREATE TABLE IF NOT EXISTS v2_artifacts (
    artifact_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, revision TEXT NOT NULL,
    slot INTEGER NOT NULL, time_ms INTEGER NOT NULL, sha TEXT NOT NULL,
    path TEXT NOT NULL, width INTEGER, height INTEGER, status TEXT NOT NULL,
    UNIQUE(source_id, revision, slot)
);
CREATE TABLE IF NOT EXISTS v2_artifact_mappings (
    visual_unit_id TEXT PRIMARY KEY, derived_id TEXT UNIQUE, artifact_id TEXT NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES v2_artifacts(artifact_id)
);
CREATE TABLE IF NOT EXISTS v2_decode_attempts (
    attempt_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    decoder TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_outbox (
    event_id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL, version INTEGER NOT NULL,
    event_type TEXT NOT NULL, payload TEXT NOT NULL, committed_at REAL NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v2_inbox (
    consumer TEXT NOT NULL, event_id TEXT NOT NULL, payload_sha TEXT NOT NULL,
    PRIMARY KEY(consumer, event_id)
);
CREATE TABLE IF NOT EXISTS v2_deliveries (
    consumer TEXT NOT NULL, event_id TEXT NOT NULL, owner TEXT NOT NULL,
    lease_until REAL, status TEXT NOT NULL, PRIMARY KEY(consumer, event_id)
);
CREATE TABLE IF NOT EXISTS v2_jobs (
    execution_key TEXT PRIMARY KEY, stage TEXT NOT NULL, item_id TEXT NOT NULL,
    revision INTEGER NOT NULL, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    input_sha TEXT NOT NULL, provenance TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL, owner TEXT, token TEXT, lease_until REAL,
    result_json TEXT, result_sha TEXT, error TEXT,
    UNIQUE(stage, item_id, revision)
);
CREATE TABLE IF NOT EXISTS v2_job_dependencies (
    child_key TEXT NOT NULL, parent_key TEXT NOT NULL,
    PRIMARY KEY(child_key, parent_key)
);
CREATE TABLE IF NOT EXISTS v2_retry_requests (
    request_id TEXT PRIMARY KEY, execution_key TEXT NOT NULL, generation INTEGER NOT NULL,
    actor TEXT NOT NULL, reason TEXT NOT NULL, previous_attempts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_selection_contexts (
    source_id TEXT NOT NULL, source_revision TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(source_id, source_revision)
);
CREATE TABLE IF NOT EXISTS v2_snapshot_evidence (
    snapshot_id TEXT NOT NULL, execution_key TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, execution_key)
);
CREATE TABLE IF NOT EXISTS v2_canonical (
    source_id TEXT NOT NULL, source_revision TEXT NOT NULL, frame_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL, canonical_revision TEXT NOT NULL, row_json TEXT NOT NULL,
    PRIMARY KEY(source_id, source_revision, frame_id)
);
CREATE TABLE IF NOT EXISTS v2_selection_snapshots (
    snapshot_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    evidence_digest TEXT NOT NULL, canonical_revision TEXT NOT NULL,
    policy_hash TEXT NOT NULL, code_hash TEXT NOT NULL, input_json TEXT NOT NULL,
    result_json TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_selected_candidates (
    candidate_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, source_id TEXT NOT NULL,
    source_revision TEXT NOT NULL, frame_id TEXT NOT NULL, purpose TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_documents (
    document_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    artifact_id TEXT, start_ms INTEGER, end_ms INTEGER, document_type TEXT NOT NULL,
    raw_result_ref TEXT NOT NULL, text TEXT NOT NULL, provenance TEXT NOT NULL,
    revision INTEGER NOT NULL, embedding_ref TEXT, status TEXT NOT NULL,
    UNIQUE(document_type, raw_result_ref)
);
CREATE TABLE IF NOT EXISTS v2_audio_branches (
    source_id TEXT NOT NULL, source_revision TEXT NOT NULL, status TEXT NOT NULL,
    artifact_json TEXT NOT NULL, segments_json TEXT NOT NULL,
    PRIMARY KEY(source_id, source_revision)
);
CREATE TABLE IF NOT EXISTS v2_fusion (
    link_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    artifact_id TEXT, start_ms INTEGER, end_ms INTEGER, kind TEXT NOT NULL,
    result_ref TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_final_projection_heads (
    source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    execution_key TEXT NOT NULL, snapshot_id TEXT NOT NULL,
    PRIMARY KEY(source_id, source_revision)
);
CREATE TABLE IF NOT EXISTS v2_final_projection_documents (
    execution_key TEXT NOT NULL, document_key TEXT NOT NULL,
    PRIMARY KEY(execution_key, document_key)
);

CREATE TABLE IF NOT EXISTS v2_vectors (
    entity_id TEXT NOT NULL, namespace TEXT NOT NULL, dimension INTEGER,
    vector_json TEXT NOT NULL, input_digest TEXT NOT NULL,
    PRIMARY KEY(entity_id, namespace)
);
CREATE TABLE IF NOT EXISTS v2_index_coverage (
    entity_id TEXT NOT NULL, index_kind TEXT NOT NULL, input_digest TEXT NOT NULL,
    backend TEXT NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY(entity_id, index_kind)
);
CREATE VIRTUAL TABLE IF NOT EXISTS v2_text_fts
USING fts5(document_id UNINDEXED, source_id UNINDEXED, text);

CREATE TABLE IF NOT EXISTS v2_person_observations (
    observation_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    artifact_id TEXT NOT NULL, time_ms INTEGER NOT NULL, bbox TEXT NOT NULL,
    confidence REAL NOT NULL, face_evidence TEXT NOT NULL, provenance TEXT NOT NULL,
    revision INTEGER NOT NULL, evidence_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_observation_heads (
    artifact_id TEXT NOT NULL, detection_slot TEXT NOT NULL, observation_id TEXT NOT NULL,
    PRIMARY KEY(artifact_id, detection_slot)
);
CREATE TABLE IF NOT EXISTS v2_tracklets (
    tracklet_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    tracker_hash TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_tracklet_membership (
    observation_id TEXT NOT NULL, version INTEGER NOT NULL, tracklet_id TEXT NOT NULL,
    authority TEXT NOT NULL, evidence_digest TEXT NOT NULL,
    PRIMARY KEY(observation_id, version)
);
CREATE TABLE IF NOT EXISTS v2_source_people (
    source_person_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
    source_revision TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_source_face_anchors (
    source_id TEXT NOT NULL, source_revision TEXT NOT NULL, cluster_id TEXT NOT NULL,
    source_person_id TEXT NOT NULL,
    PRIMARY KEY(source_id, source_revision, cluster_id)
);
CREATE TABLE IF NOT EXISTS v2_tracklet_people (
    tracklet_id TEXT NOT NULL, version INTEGER NOT NULL, source_person_id TEXT NOT NULL,
    authority TEXT NOT NULL, PRIMARY KEY(tracklet_id, version)
);
CREATE TABLE IF NOT EXISTS v2_global_people (
    global_person_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_global_memberships (
    source_person_id TEXT NOT NULL, version INTEGER NOT NULL, global_person_id TEXT NOT NULL,
    authority TEXT NOT NULL, PRIMARY KEY(source_person_id, version)
);
CREATE TABLE IF NOT EXISTS v2_person_aliases (
    global_person_id TEXT NOT NULL, version INTEGER NOT NULL,
    canonical_global_person_id TEXT NOT NULL, authority TEXT NOT NULL,
    decision_id TEXT NOT NULL, PRIMARY KEY(global_person_id, version)
);
CREATE TABLE IF NOT EXISTS v2_person_decisions (
    decision_id TEXT PRIMARY KEY, action TEXT NOT NULL, left_id TEXT NOT NULL,
    right_id TEXT NOT NULL, payload TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_person_constraints (
    left_source_person TEXT NOT NULL, right_source_person TEXT NOT NULL,
    decision_id TEXT NOT NULL, active INTEGER NOT NULL,
    PRIMARY KEY(left_source_person, right_source_person, decision_id)
);
CREATE TABLE IF NOT EXISTS v2_person_suggestions (
    left_id TEXT NOT NULL, right_id TEXT NOT NULL, status TEXT NOT NULL,
    evidence TEXT NOT NULL, PRIMARY KEY(left_id, right_id)
);
CREATE TABLE IF NOT EXISTS v2_manual_observation_authority (
    logical_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    artifact_id TEXT NOT NULL, bbox TEXT NOT NULL, tracklet_id TEXT NOT NULL,
    source_person_id TEXT NOT NULL, decision_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_person_review_observations (
    observation_id TEXT PRIMARY KEY, reason TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_person_decision_results (
    decision_id TEXT PRIMARY KEY, result_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS v2_dependency_parent ON v2_job_dependencies(parent_key);
CREATE INDEX IF NOT EXISTS v2_people_source ON v2_person_observations(source_id, source_revision, time_ms);
CREATE INDEX IF NOT EXISTS v2_fusion_timeline ON v2_fusion(source_id, source_revision, start_ms, end_ms);
CREATE INDEX IF NOT EXISTS v2_vectors_namespace ON v2_vectors(namespace);
CREATE INDEX IF NOT EXISTS v2_sources_revision ON v2_sources(source_id, revision);
CREATE INDEX IF NOT EXISTS v2_jobs_source_status ON v2_jobs(source_id, source_revision, status);
CREATE INDEX IF NOT EXISTS v2_sample_slots_status ON v2_sample_slots(source_id, source_revision, status);
CREATE INDEX IF NOT EXISTS v2_documents_source_status ON v2_documents(source_id, source_revision, status);
CREATE INDEX IF NOT EXISTS v2_index_coverage_status ON v2_index_coverage(index_kind, status);

CREATE VIEW IF NOT EXISTS v2_current_jobs AS
WITH RECURSIVE ancestry(child_key,parent_key) AS (
    SELECT execution_key, execution_key FROM v2_jobs
    UNION SELECT a.child_key, d.parent_key
    FROM ancestry a JOIN v2_job_dependencies d ON d.child_key=a.parent_key
)
SELECT j.* FROM v2_jobs j
WHERE j.status <> 'SUPERSEDED' AND NOT EXISTS (
    SELECT 1 FROM ancestry a
    LEFT JOIN v2_jobs p ON p.execution_key=a.parent_key
    LEFT JOIN v2_active_sources active ON active.source_id=p.source_id
    WHERE a.child_key=j.execution_key AND
      (p.execution_key IS NULL OR
       (a.parent_key<>j.execution_key AND p.status<>'DONE') OR
       (active.source_revision IS NOT NULL AND active.source_revision<>p.source_revision))
);

CREATE VIEW IF NOT EXISTS v2_searchable_documents AS
SELECT d.* FROM v2_documents d
WHERE d.document_type NOT IN ('qwen','ocr') OR NOT EXISTS(
    SELECT 1 FROM v2_final_projection_heads h
    JOIN v2_job_dependencies dep ON dep.child_key=h.execution_key
    WHERE dep.parent_key=d.raw_result_ref
);
