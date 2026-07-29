PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS stop03_1c_person_reid_runs (
    run_id TEXT PRIMARY KEY,
    contract_version TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_dir TEXT NOT NULL,
    detector_sha256 TEXT NOT NULL,
    recognizer_sha256 TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    scheduling_mode TEXT NOT NULL CHECK (
        scheduling_mode = 'dynamic_database_claim'
    ),
    workers INTEGER NOT NULL CHECK (workers > 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    visual_unit_count INTEGER NOT NULL CHECK (visual_unit_count >= 0),
    pending_count INTEGER NOT NULL DEFAULT 0 CHECK (pending_count >= 0),
    running_count INTEGER NOT NULL DEFAULT 0 CHECK (running_count >= 0),
    success_count INTEGER NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    no_face_count INTEGER NOT NULL DEFAULT 0 CHECK (no_face_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    face_count INTEGER NOT NULL DEFAULT 0 CHECK (face_count >= 0),
    cluster_count INTEGER NOT NULL DEFAULT 0 CHECK (cluster_count >= 0),
    run_payload_digest TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (
        status IN ('planned','running','success','failed')
    ),
    created_at TEXT NOT NULL,
    finished_at TEXT,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS stop03_1c_person_reid_run_items (
    run_id TEXT NOT NULL,
    visual_unit_id TEXT NOT NULL,
    execution_key TEXT NOT NULL,
    source_content_id TEXT NOT NULL,
    derived_id TEXT NOT NULL,
    visual_file TEXT NOT NULL,
    time_position_ms INTEGER NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('image','video')),
    status TEXT NOT NULL CHECK (
        status IN ('pending','running','success','no_face','failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claimed_by_worker TEXT NOT NULL DEFAULT '',
    worker_pid INTEGER,
    face_count INTEGER NOT NULL DEFAULT 0 CHECK (face_count >= 0),
    elapsed_seconds REAL,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY(run_id, visual_unit_id),
    UNIQUE(run_id, execution_key),
    FOREIGN KEY(run_id) REFERENCES stop03_1c_person_reid_runs(run_id),
    FOREIGN KEY(visual_unit_id) REFERENCES visual_units(visual_unit_id),
    FOREIGN KEY(source_content_id) REFERENCES source_assets(source_content_id),
    FOREIGN KEY(derived_id) REFERENCES derived_assets(derived_id)
);

CREATE INDEX IF NOT EXISTS idx_stop03_1c_items_status
ON stop03_1c_person_reid_run_items(run_id, status, attempt_count);

CREATE TABLE IF NOT EXISTS stop03_1c_face_embeddings (
    run_id TEXT NOT NULL,
    face_id TEXT NOT NULL,
    visual_unit_id TEXT NOT NULL,
    face_index INTEGER NOT NULL CHECK (face_index >= 0),
    bbox_json TEXT NOT NULL,
    landmarks_json TEXT NOT NULL,
    detection_score REAL NOT NULL,
    quality_score REAL NOT NULL,
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0),
    vector_dtype TEXT NOT NULL CHECK (vector_dtype = 'float32'),
    normalized INTEGER NOT NULL CHECK (normalized = 1),
    embedding_blob BLOB NOT NULL,
    embedding_byte_length INTEGER NOT NULL CHECK (
        embedding_byte_length = length(embedding_blob)
    ),
    embedding_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, face_id),
    UNIQUE(run_id, visual_unit_id, face_index),
    FOREIGN KEY(run_id, visual_unit_id)
        REFERENCES stop03_1c_person_reid_run_items(run_id, visual_unit_id)
);

CREATE INDEX IF NOT EXISTS idx_stop03_1c_faces_visual_unit
ON stop03_1c_face_embeddings(run_id, visual_unit_id);

CREATE TABLE IF NOT EXISTS stop03_1c_person_clusters (
    run_id TEXT NOT NULL,
    person_cluster_id TEXT NOT NULL,
    representative_face_id TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    distinct_source_count INTEGER NOT NULL CHECK (distinct_source_count > 0),
    minimum_member_similarity REAL,
    cluster_confidence TEXT NOT NULL CHECK (
        cluster_confidence IN ('singleton','review','high')
    ),
    human_review_status TEXT NOT NULL DEFAULT 'unreviewed' CHECK (
        human_review_status IN ('unreviewed','confirmed','split','merged','rejected')
    ),
    anonymous_display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, person_cluster_id),
    UNIQUE(run_id, representative_face_id),
    FOREIGN KEY(run_id, representative_face_id)
        REFERENCES stop03_1c_face_embeddings(run_id, face_id)
);

CREATE TABLE IF NOT EXISTS stop03_1c_person_cluster_members (
    run_id TEXT NOT NULL,
    person_cluster_id TEXT NOT NULL,
    face_id TEXT NOT NULL,
    similarity_to_representative REAL NOT NULL,
    membership_reason TEXT NOT NULL CHECK (
        membership_reason IN ('singleton','auto_high_confidence','human_confirmed')
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, face_id),
    FOREIGN KEY(run_id, person_cluster_id)
        REFERENCES stop03_1c_person_clusters(run_id, person_cluster_id),
    FOREIGN KEY(run_id, face_id)
        REFERENCES stop03_1c_face_embeddings(run_id, face_id)
);

CREATE INDEX IF NOT EXISTS idx_stop03_1c_cluster_members
ON stop03_1c_person_cluster_members(run_id, person_cluster_id);

CREATE VIEW IF NOT EXISTS v_stop03_1c_latest_person_cluster_members AS
SELECT
    c.run_id,
    c.person_cluster_id,
    c.representative_face_id,
    c.member_count,
    c.distinct_source_count,
    c.cluster_confidence,
    c.human_review_status,
    c.anonymous_display_name,
    m.face_id,
    m.similarity_to_representative,
    f.visual_unit_id,
    i.source_content_id,
    i.derived_id,
    i.visual_file,
    i.media_type,
    i.time_position_ms
FROM stop03_1c_person_clusters AS c
JOIN stop03_1c_person_cluster_members AS m
  ON m.run_id=c.run_id
 AND m.person_cluster_id=c.person_cluster_id
JOIN stop03_1c_face_embeddings AS f
  ON f.run_id=m.run_id
 AND f.face_id=m.face_id
JOIN stop03_1c_person_reid_run_items AS i
  ON i.run_id=f.run_id
 AND i.visual_unit_id=f.visual_unit_id
WHERE c.run_id=(
    SELECT r.run_id
    FROM stop03_1c_person_reid_runs AS r
    WHERE r.status='success'
    ORDER BY r.created_at DESC,r.run_id DESC
    LIMIT 1
);
