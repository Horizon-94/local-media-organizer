PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pipeline_frozen_contracts (
    contract_name TEXT PRIMARY KEY,
    snapshot_contract_version TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    qwenvl_count INTEGER NOT NULL,
    ocr_count INTEGER NOT NULL,
    candidate_id_set_sha256 TEXT NOT NULL,
    candidate_semantic_digest_sha256 TEXT NOT NULL,
    rule_document_sha256 TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    candidate_script_sha256 TEXT NOT NULL,
    locked_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'FROZEN')
);

CREATE TABLE IF NOT EXISTS stop03_2_candidate_queue_frozen_v25 (
    candidate_id TEXT PRIMARY KEY,
    queue_type TEXT NOT NULL,
    candidate_role TEXT NOT NULL,
    candidate_score REAL NOT NULL,
    reason_codes TEXT NOT NULL,
    source_content_id TEXT NOT NULL,
    visual_unit_id TEXT NOT NULL,
    canonical_visual_unit_id TEXT NOT NULL,
    derived_id TEXT NOT NULL,
    duplicate_group_id TEXT NOT NULL DEFAULT '',
    frame_index INTEGER NOT NULL,
    time_position_ms INTEGER NOT NULL,
    canonical_time_ms INTEGER NOT NULL,
    group_start_ms INTEGER NOT NULL,
    group_end_ms INTEGER NOT NULL,
    segment_start_ms INTEGER NOT NULL,
    segment_end_ms INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    visual_unit_type TEXT NOT NULL,
    source_relative_path TEXT NOT NULL,
    runtime_visual_file TEXT NOT NULL,
    runtime_visual_file_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    yoloe_labels_json TEXT NOT NULL,
    yoloe_label_count INTEGER NOT NULL,
    yoloe_labels_sha256 TEXT NOT NULL,
    yoloe_label_status TEXT NOT NULL CHECK (yoloe_label_status IN ('labeled','no_label')),
    policy_version TEXT NOT NULL,
    rule_document_sha256 TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    candidate_script_sha256 TEXT NOT NULL,
    central_dedup_run_id TEXT NOT NULL,
    yoloe_run_id TEXT NOT NULL,
    openclip_run_id TEXT NOT NULL,
    candidate_semantic_sha256 TEXT NOT NULL,
    snapshot_contract_version TEXT NOT NULL,
    snapshot_created_at TEXT NOT NULL,
    frozen_status TEXT NOT NULL CHECK (frozen_status = 'FROZEN'),
    FOREIGN KEY(candidate_id) REFERENCES stop03_2_candidate_queue_items(candidate_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_stop03_2_frozen_v25_queue
ON stop03_2_candidate_queue_frozen_v25(queue_type, candidate_role);
CREATE INDEX IF NOT EXISTS idx_stop03_2_frozen_v25_visual
ON stop03_2_candidate_queue_frozen_v25(visual_unit_id, canonical_visual_unit_id);
CREATE INDEX IF NOT EXISTS idx_stop03_2_frozen_v25_source
ON stop03_2_candidate_queue_frozen_v25(source_content_id, time_position_ms);

CREATE TABLE IF NOT EXISTS stop03_3_qwenvl_runs (
    run_id TEXT PRIMARY KEY,
    v25_contract_name TEXT NOT NULL,
    candidate_id_set_sha256 TEXT NOT NULL,
    candidate_semantic_digest_sha256 TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    model_path TEXT NOT NULL,
    model_sha256 TEXT NOT NULL,
    model_config_sha256 TEXT NOT NULL,
    model_tokenizer_files_json TEXT NOT NULL,
    model_tokenizer_files_sha256 TEXT NOT NULL,
    model_inventory_json TEXT NOT NULL,
    model_inventory_sha256 TEXT NOT NULL,
    model_fingerprint_sha256 TEXT NOT NULL,
    prompt_path TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    orchestrator_config_sha256 TEXT NOT NULL,
    output_contract_version TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    temperature REAL NOT NULL,
    top_p REAL NOT NULL,
    workers INTEGER NOT NULL,
    script_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','running','success','partial','failed','cancelled')),
    pending_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_message TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(v25_contract_name) REFERENCES pipeline_frozen_contracts(contract_name)
);

CREATE TABLE IF NOT EXISTS stop03_3_qwenvl_run_items (
    run_item_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    execution_key TEXT NOT NULL UNIQUE,
    source_content_id TEXT NOT NULL,
    visual_unit_id TEXT NOT NULL,
    canonical_visual_unit_id TEXT NOT NULL,
    derived_id TEXT NOT NULL,
    candidate_role TEXT NOT NULL,
    reason_codes TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    media_type TEXT NOT NULL,
    time_position_ms INTEGER NOT NULL,
    runtime_visual_file TEXT NOT NULL,
    runtime_visual_file_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending','running','success','truncated','parse_failed',
                   'missing_required_fields','input_fingerprint_mismatch','failed','review')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY(run_id) REFERENCES stop03_3_qwenvl_runs(run_id),
    FOREIGN KEY(candidate_id) REFERENCES stop03_2_candidate_queue_frozen_v25(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_stop03_3_qwenvl_items_run_status
ON stop03_3_qwenvl_run_items(run_id, status);
CREATE INDEX IF NOT EXISTS idx_stop03_3_qwenvl_items_candidate
ON stop03_3_qwenvl_run_items(candidate_id);

CREATE TABLE IF NOT EXISTS stop03_3_qwenvl_results (
    result_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    run_item_id TEXT NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL,
    execution_key TEXT NOT NULL UNIQUE,
    evidence_id TEXT NOT NULL UNIQUE,
    source_content_id TEXT NOT NULL,
    visual_unit_id TEXT NOT NULL,
    canonical_visual_unit_id TEXT NOT NULL,
    derived_id TEXT NOT NULL,
    candidate_role TEXT NOT NULL,
    reason_codes TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    result_status TEXT NOT NULL CHECK (
        result_status IN ('success','truncated','parse_failed',
                          'missing_required_fields','input_fingerprint_mismatch','failed','review')
    ),
    clean_text TEXT NOT NULL,
    qwen_text_preview TEXT NOT NULL,
    clean_text_sha256 TEXT NOT NULL,
    raw_stdout_path TEXT NOT NULL,
    raw_stdout_sha256 TEXT NOT NULL,
    stderr_path TEXT NOT NULL,
    stderr_sha256 TEXT NOT NULL,
    metrics_path TEXT NOT NULL,
    metrics_sha256 TEXT NOT NULL,
    runtime_metrics_json TEXT NOT NULL,
    prompt_tokens INTEGER,
    generation_tokens INTEGER,
    peak_memory_gb REAL,
    finish_reason TEXT NOT NULL,
    truncation_status TEXT NOT NULL,
    cleanup_status TEXT NOT NULL,
    cleanup_warnings TEXT NOT NULL,
    output_contract_version TEXT NOT NULL,
    runtime_visual_file_sha256 TEXT NOT NULL,
    model_sha256 TEXT NOT NULL,
    model_config_sha256 TEXT NOT NULL,
    model_tokenizer_files_json TEXT NOT NULL,
    model_tokenizer_files_sha256 TEXT NOT NULL,
    model_inventory_sha256 TEXT NOT NULL,
    model_fingerprint_sha256 TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    orchestrator_config_sha256 TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES stop03_3_qwenvl_runs(run_id),
    FOREIGN KEY(run_item_id) REFERENCES stop03_3_qwenvl_run_items(run_item_id),
    FOREIGN KEY(candidate_id) REFERENCES stop03_2_candidate_queue_frozen_v25(candidate_id)
);

CREATE VIEW IF NOT EXISTS v_stop03_2_v25_qwenvl_execution_queue AS
SELECT f.*
FROM stop03_2_candidate_queue_frozen_v25 AS f
JOIN pipeline_frozen_contracts AS c
  ON c.contract_name = 'stop03_2_v25_candidate_snapshot'
 AND c.status = 'FROZEN'
WHERE f.queue_type = 'qwenvl_high_value';

CREATE VIEW IF NOT EXISTS v_stop03_2_v25_ocr_execution_queue AS
SELECT f.*
FROM stop03_2_candidate_queue_frozen_v25 AS f
JOIN pipeline_frozen_contracts AS c
  ON c.contract_name = 'stop03_2_v25_candidate_snapshot'
 AND c.status = 'FROZEN'
WHERE f.queue_type = 'ocr_trigger';

CREATE TRIGGER IF NOT EXISTS trg_stop03_2_frozen_v25_no_update
BEFORE UPDATE ON stop03_2_candidate_queue_frozen_v25
BEGIN
    SELECT RAISE(ABORT, 'stop03_2_v25_snapshot_is_immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_stop03_2_frozen_v25_no_delete
BEFORE DELETE ON stop03_2_candidate_queue_frozen_v25
BEGIN
    SELECT RAISE(ABORT, 'stop03_2_v25_snapshot_is_immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_stop03_2_frozen_v25_no_insert_after_lock
BEFORE INSERT ON stop03_2_candidate_queue_frozen_v25
WHEN EXISTS (
    SELECT 1 FROM pipeline_frozen_contracts
    WHERE contract_name = 'stop03_2_v25_candidate_snapshot' AND status = 'FROZEN'
)
BEGIN
    SELECT RAISE(ABORT, 'stop03_2_v25_snapshot_is_locked');
END;

CREATE TRIGGER IF NOT EXISTS trg_pipeline_frozen_contracts_no_update
BEFORE UPDATE ON pipeline_frozen_contracts
WHEN OLD.status = 'FROZEN'
BEGIN
    SELECT RAISE(ABORT, 'pipeline_frozen_contract_is_immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_pipeline_frozen_contracts_no_delete
BEFORE DELETE ON pipeline_frozen_contracts
WHEN OLD.status = 'FROZEN'
BEGIN
    SELECT RAISE(ABORT, 'pipeline_frozen_contract_is_immutable');
END;
