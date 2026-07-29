PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS stop03_4_ocr_runs (
    run_id TEXT PRIMARY KEY,
    run_kind TEXT NOT NULL CHECK (run_kind IN ('smoke','full')),
    contract_version TEXT NOT NULL,
    queue_view TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    candidate_id_set_sha256 TEXT NOT NULL,
    model_root TEXT NOT NULL,
    detection_model_dir TEXT NOT NULL,
    recognition_model_dir TEXT NOT NULL,
    detection_model_sha256 TEXT NOT NULL,
    recognition_model_sha256 TEXT NOT NULL,
    model_fingerprint_sha256 TEXT NOT NULL,
    config_path TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    workers INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending','running','success','partial','failed','cancelled')
    ),
    pending_count INTEGER NOT NULL DEFAULT 0,
    running_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    no_text_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    reused_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS stop03_4_ocr_results (
    result_id TEXT PRIMARY KEY,
    execution_key TEXT NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL UNIQUE,
    result_status TEXT NOT NULL CHECK (result_status IN ('success','no_text')),
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
    ocr_text TEXT NOT NULL,
    ocr_text_preview TEXT NOT NULL,
    ocr_text_sha256 TEXT NOT NULL,
    ocr_lines_json TEXT NOT NULL,
    ocr_line_count INTEGER NOT NULL,
    mean_confidence REAL,
    min_confidence REAL,
    max_confidence REAL,
    output_json_path TEXT NOT NULL,
    output_json_sha256 TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    worker_pid INTEGER NOT NULL,
    ocr_api_used TEXT NOT NULL,
    detection_model_sha256 TEXT NOT NULL,
    recognition_model_sha256 TEXT NOT NULL,
    model_fingerprint_sha256 TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id)
        REFERENCES stop03_2_candidate_queue_frozen_v25(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_stop03_4_ocr_results_candidate
ON stop03_4_ocr_results(candidate_id);

CREATE TABLE IF NOT EXISTS stop03_4_ocr_run_items (
    run_item_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    execution_key TEXT NOT NULL,
    result_id TEXT,
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
        status IN ('pending','running','success','no_text',
                   'input_fingerprint_mismatch','failed','review')
    ),
    reused_existing_result INTEGER NOT NULL DEFAULT 0 CHECK (
        reused_existing_result IN (0,1)
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    claimed_by_worker TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(run_id, candidate_id),
    FOREIGN KEY(run_id) REFERENCES stop03_4_ocr_runs(run_id),
    FOREIGN KEY(candidate_id)
        REFERENCES stop03_2_candidate_queue_frozen_v25(candidate_id),
    FOREIGN KEY(result_id) REFERENCES stop03_4_ocr_results(result_id)
);

CREATE INDEX IF NOT EXISTS idx_stop03_4_ocr_items_run_status
ON stop03_4_ocr_run_items(run_id, status);
CREATE INDEX IF NOT EXISTS idx_stop03_4_ocr_items_execution
ON stop03_4_ocr_run_items(execution_key);

CREATE TABLE IF NOT EXISTS stop03_4_ocr_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    run_item_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    execution_key TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('success','no_text','input_fingerprint_mismatch','failed')
    ),
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    elapsed_seconds REAL NOT NULL,
    worker_pid INTEGER NOT NULL,
    output_json_path TEXT NOT NULL DEFAULT '',
    output_json_sha256 TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    UNIQUE(run_item_id, attempt_number),
    FOREIGN KEY(run_id) REFERENCES stop03_4_ocr_runs(run_id),
    FOREIGN KEY(run_item_id) REFERENCES stop03_4_ocr_run_items(run_item_id),
    FOREIGN KEY(candidate_id)
        REFERENCES stop03_2_candidate_queue_frozen_v25(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_stop03_4_ocr_attempts_run
ON stop03_4_ocr_attempts(run_id, status);
