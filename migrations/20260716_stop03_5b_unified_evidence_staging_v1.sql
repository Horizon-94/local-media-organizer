CREATE TABLE IF NOT EXISTS stop03_5_unified_evidence_runs (
    staging_run_id TEXT PRIMARY KEY,
    contract_version TEXT NOT NULL,
    qwen_run_id TEXT NOT NULL,
    ocr_run_id TEXT NOT NULL,
    qwen_count INTEGER NOT NULL,
    ocr_count INTEGER NOT NULL,
    evidence_count INTEGER NOT NULL,
    pass_count INTEGER NOT NULL,
    review_count INTEGER NOT NULL,
    fail_count INTEGER NOT NULL,
    candidate_id_set_sha256 TEXT NOT NULL,
    evidence_id_set_sha256 TEXT NOT NULL,
    payload_digest_sha256 TEXT NOT NULL UNIQUE,
    quality_config_sha256 TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success','failed')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stop03_5_unified_evidence_items (
    staging_item_id TEXT NOT NULL,
    staging_run_id TEXT NOT NULL,
    canonical_evidence_key TEXT NOT NULL,
    modality TEXT NOT NULL CHECK (modality IN ('qwenvl','ocr')),
    evidence_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    source_content_id TEXT NOT NULL,
    visual_unit_id TEXT NOT NULL,
    canonical_visual_unit_id TEXT NOT NULL,
    derived_id TEXT NOT NULL,
    candidate_role TEXT NOT NULL,
    reason_codes TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    runtime_visual_file_sha256 TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    quality_status TEXT NOT NULL CHECK (quality_status IN ('PASS','REVIEW')),
    quality_reasons TEXT NOT NULL,
    evidence_text TEXT NOT NULL,
    evidence_text_sha256 TEXT NOT NULL,
    evidence_attributes_json TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    source_result_id TEXT NOT NULL,
    source_execution_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(staging_run_id, staging_item_id),
    UNIQUE(staging_run_id, evidence_id),
    UNIQUE(staging_run_id, canonical_evidence_key),
    FOREIGN KEY(staging_run_id)
        REFERENCES stop03_5_unified_evidence_runs(staging_run_id)
);

CREATE INDEX IF NOT EXISTS idx_stop03_5_evidence_visual
ON stop03_5_unified_evidence_items(
    staging_run_id, canonical_visual_unit_id, modality
);

CREATE INDEX IF NOT EXISTS idx_stop03_5_evidence_source
ON stop03_5_unified_evidence_items(
    staging_run_id, source_content_id, modality
);

CREATE VIEW IF NOT EXISTS v_stop03_5_latest_unified_evidence AS
SELECT i.*
FROM stop03_5_unified_evidence_items AS i
JOIN stop03_5_unified_evidence_runs AS r
  ON r.staging_run_id=i.staging_run_id
WHERE r.status='success'
  AND r.staging_run_id=(
      SELECT r2.staging_run_id
      FROM stop03_5_unified_evidence_runs AS r2
      WHERE r2.status='success'
      ORDER BY r2.created_at DESC, r2.staging_run_id DESC
      LIMIT 1
  );
