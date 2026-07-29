CREATE TABLE IF NOT EXISTS stop03_5d_text_embedding_runs (
    embedding_run_id TEXT PRIMARY KEY,
    contract_version TEXT NOT NULL,
    source_staging_run_id TEXT NOT NULL,
    source_propagation_run_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_path TEXT NOT NULL,
    model_inventory_sha256 TEXT NOT NULL,
    model_config_sha256 TEXT NOT NULL,
    model_dimension INTEGER NOT NULL CHECK (model_dimension > 0),
    vector_dtype TEXT NOT NULL CHECK (vector_dtype IN ('float32','float16')),
    normalize_embeddings INTEGER NOT NULL CHECK (normalize_embeddings IN (0,1)),
    scheduling_mode TEXT NOT NULL CHECK (
        scheduling_mode = 'dynamic_database_claim'
    ),
    workers INTEGER NOT NULL CHECK (workers > 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    document_count INTEGER NOT NULL CHECK (document_count >= 0),
    unique_text_count INTEGER NOT NULL CHECK (unique_text_count >= 0),
    reused_document_count INTEGER NOT NULL CHECK (reused_document_count >= 0),
    direct_only_count INTEGER NOT NULL CHECK (direct_only_count >= 0),
    propagation_only_count INTEGER NOT NULL CHECK (propagation_only_count >= 0),
    direct_and_propagation_count INTEGER NOT NULL CHECK (
        direct_and_propagation_count >= 0
    ),
    pending_count INTEGER NOT NULL DEFAULT 0 CHECK (pending_count >= 0),
    running_count INTEGER NOT NULL DEFAULT 0 CHECK (running_count >= 0),
    success_count INTEGER NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    document_id_set_sha256 TEXT NOT NULL,
    text_job_id_set_sha256 TEXT NOT NULL,
    document_payload_digest_sha256 TEXT NOT NULL,
    run_payload_digest_sha256 TEXT NOT NULL UNIQUE,
    policy_config_sha256 TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('planned','running','success','failed')
    ),
    created_at TEXT NOT NULL,
    finished_at TEXT,
    error_message TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(source_staging_run_id)
        REFERENCES stop03_5_unified_evidence_runs(staging_run_id),
    FOREIGN KEY(source_propagation_run_id)
        REFERENCES stop03_5c_propagation_runs(propagation_run_id)
);

CREATE TABLE IF NOT EXISTS stop03_5d_text_documents (
    embedding_run_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    source_content_id TEXT NOT NULL,
    derived_id TEXT NOT NULL,
    canonical_visual_unit_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    derived_type TEXT NOT NULL,
    frame_index INTEGER NOT NULL,
    time_position_ms INTEGER NOT NULL,
    source_relative_path TEXT NOT NULL,
    document_kind TEXT NOT NULL CHECK (
        document_kind IN (
            'direct_only',
            'propagation_only',
            'direct_and_propagation'
        )
    ),
    qwen_text TEXT NOT NULL,
    ocr_text TEXT NOT NULL,
    propagated_labels_json TEXT NOT NULL,
    embedding_text TEXT NOT NULL CHECK (length(embedding_text) > 0),
    embedding_text_sha256 TEXT NOT NULL,
    source_evidence_ids_json TEXT NOT NULL,
    source_propagation_ids_json TEXT NOT NULL,
    direct_qwen_count INTEGER NOT NULL CHECK (direct_qwen_count >= 0),
    direct_ocr_count INTEGER NOT NULL CHECK (direct_ocr_count >= 0),
    propagation_row_count INTEGER NOT NULL CHECK (propagation_row_count >= 0),
    quality_status TEXT NOT NULL CHECK (quality_status = 'PASS'),
    created_at TEXT NOT NULL,
    PRIMARY KEY(embedding_run_id, document_id),
    UNIQUE(embedding_run_id, derived_id),
    FOREIGN KEY(embedding_run_id)
        REFERENCES stop03_5d_text_embedding_runs(embedding_run_id),
    FOREIGN KEY(source_content_id) REFERENCES source_assets(source_content_id),
    FOREIGN KEY(derived_id) REFERENCES derived_assets(derived_id),
    FOREIGN KEY(canonical_visual_unit_id)
        REFERENCES visual_units(visual_unit_id)
);

CREATE INDEX IF NOT EXISTS idx_stop03_5d_document_source
ON stop03_5d_text_documents(
    embedding_run_id, source_content_id, media_type
);

CREATE INDEX IF NOT EXISTS idx_stop03_5d_document_text_sha
ON stop03_5d_text_documents(
    embedding_run_id, embedding_text_sha256
);

CREATE TABLE IF NOT EXISTS stop03_5d_text_vectors (
    embedding_run_id TEXT NOT NULL,
    text_vector_id TEXT NOT NULL,
    execution_key TEXT NOT NULL,
    embedding_text_sha256 TEXT NOT NULL,
    model_inventory_sha256 TEXT NOT NULL,
    model_config_sha256 TEXT NOT NULL,
    model_dimension INTEGER NOT NULL CHECK (model_dimension > 0),
    vector_dtype TEXT NOT NULL CHECK (vector_dtype IN ('float32','float16')),
    normalized INTEGER NOT NULL CHECK (normalized IN (0,1)),
    status TEXT NOT NULL CHECK (
        status IN ('pending','running','success','failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claimed_by_worker TEXT NOT NULL DEFAULT '',
    worker_pid INTEGER,
    elapsed_seconds REAL,
    vector_blob BLOB,
    vector_byte_length INTEGER,
    vector_sha256 TEXT,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY(embedding_run_id, text_vector_id),
    UNIQUE(embedding_run_id, execution_key),
    UNIQUE(embedding_run_id, embedding_text_sha256),
    CHECK (
        status <> 'success'
        OR (
            vector_blob IS NOT NULL
            AND vector_byte_length = length(vector_blob)
            AND vector_sha256 IS NOT NULL
            AND vector_sha256 <> ''
        )
    ),
    FOREIGN KEY(embedding_run_id)
        REFERENCES stop03_5d_text_embedding_runs(embedding_run_id)
);

CREATE TABLE IF NOT EXISTS stop03_5d_document_vector_links (
    embedding_run_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    text_vector_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(embedding_run_id, document_id),
    FOREIGN KEY(embedding_run_id, document_id)
        REFERENCES stop03_5d_text_documents(embedding_run_id, document_id),
    FOREIGN KEY(embedding_run_id, text_vector_id)
        REFERENCES stop03_5d_text_vectors(embedding_run_id, text_vector_id)
);

CREATE VIEW IF NOT EXISTS v_stop03_5d_latest_text_documents AS
SELECT d.*,l.text_vector_id,v.status AS vector_status,
       v.model_dimension,v.vector_dtype,v.normalized
FROM stop03_5d_text_documents AS d
JOIN stop03_5d_text_embedding_runs AS r
  ON r.embedding_run_id=d.embedding_run_id
JOIN stop03_5d_document_vector_links AS l
  ON l.embedding_run_id=d.embedding_run_id
 AND l.document_id=d.document_id
JOIN stop03_5d_text_vectors AS v
  ON v.embedding_run_id=l.embedding_run_id
 AND v.text_vector_id=l.text_vector_id
WHERE r.status='success'
  AND v.status='success'
  AND r.embedding_run_id=(
      SELECT r2.embedding_run_id
      FROM stop03_5d_text_embedding_runs AS r2
      WHERE r2.status='success'
      ORDER BY r2.created_at DESC,r2.embedding_run_id DESC
      LIMIT 1
  );
