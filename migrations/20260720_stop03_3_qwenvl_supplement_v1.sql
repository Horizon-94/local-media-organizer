PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS stop03_3_qwenvl_supplement_candidates (
    candidate_id TEXT PRIMARY KEY,
    source_content_id TEXT NOT NULL,
    visual_unit_id TEXT NOT NULL,
    canonical_visual_unit_id TEXT NOT NULL,
    derived_id TEXT NOT NULL,
    candidate_role TEXT NOT NULL,
    reason_codes TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type='image'),
    source_relative_path TEXT NOT NULL,
    runtime_visual_file TEXT NOT NULL,
    runtime_visual_file_sha256 TEXT NOT NULL,
    candidate_score REAL NOT NULL,
    source_candidate_run_id TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_content_id) REFERENCES source_assets(source_content_id),
    FOREIGN KEY(visual_unit_id) REFERENCES visual_units(visual_unit_id),
    FOREIGN KEY(derived_id) REFERENCES derived_assets(derived_id)
);

CREATE INDEX IF NOT EXISTS idx_s33_supplement_candidate_visual
ON stop03_3_qwenvl_supplement_candidates(visual_unit_id);

CREATE TABLE IF NOT EXISTS stop03_3_qwenvl_supplement_runs (
    run_id TEXT PRIMARY KEY,
    contract_version TEXT NOT NULL,
    candidate_count INTEGER NOT NULL CHECK(candidate_count>=0),
    success_count INTEGER NOT NULL DEFAULT 0 CHECK(success_count>=0),
    review_count INTEGER NOT NULL DEFAULT 0 CHECK(review_count>=0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_count>=0),
    workers INTEGER NOT NULL CHECK(workers>0),
    max_tokens INTEGER NOT NULL CHECK(max_tokens>0),
    model_fingerprint_sha256 TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','running','success','failed')),
    created_at TEXT NOT NULL,
    finished_at TEXT,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS stop03_3_qwenvl_supplement_items (
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    execution_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('pending','running','success','review','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
    elapsed_seconds REAL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY(run_id,candidate_id),
    FOREIGN KEY(run_id) REFERENCES stop03_3_qwenvl_supplement_runs(run_id),
    FOREIGN KEY(candidate_id) REFERENCES stop03_3_qwenvl_supplement_candidates(candidate_id)
);

CREATE TABLE IF NOT EXISTS stop03_3_qwenvl_supplement_results (
    result_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    execution_key TEXT NOT NULL UNIQUE,
    evidence_id TEXT NOT NULL UNIQUE,
    result_status TEXT NOT NULL CHECK(result_status IN ('success','review','failed')),
    clean_text TEXT NOT NULL,
    clean_text_sha256 TEXT NOT NULL,
    generation_tokens INTEGER,
    finish_reason TEXT NOT NULL,
    runtime_visual_file_sha256 TEXT NOT NULL,
    output_contract_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id,candidate_id)
        REFERENCES stop03_3_qwenvl_supplement_items(run_id,candidate_id)
);
