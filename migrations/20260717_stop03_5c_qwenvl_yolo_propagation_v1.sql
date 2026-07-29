CREATE TABLE IF NOT EXISTS stop03_5c_propagation_runs (
    propagation_run_id TEXT PRIMARY KEY,
    contract_version TEXT NOT NULL,
    source_staging_run_id TEXT NOT NULL,
    source_qwen_run_id TEXT NOT NULL,
    source_qwen_video_anchor_count INTEGER NOT NULL,
    source_ocr_anchor_count INTEGER NOT NULL CHECK (source_ocr_anchor_count = 0),
    unique_video_frame_count INTEGER NOT NULL,
    visual_unit_aliases_collapsed_count INTEGER NOT NULL,
    candidate_neighbor_pair_count INTEGER NOT NULL,
    propagation_row_count INTEGER NOT NULL,
    propagation_target_count INTEGER NOT NULL,
    target_with_direct_qwenvl_count INTEGER NOT NULL,
    propagation_id_set_sha256 TEXT NOT NULL,
    payload_digest_sha256 TEXT NOT NULL UNIQUE,
    policy_config_sha256 TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success','failed')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_staging_run_id)
        REFERENCES stop03_5_unified_evidence_runs(staging_run_id)
);

CREATE TABLE IF NOT EXISTS stop03_5c_propagation_items (
    propagation_run_id TEXT NOT NULL,
    propagation_id TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    source_staging_run_id TEXT NOT NULL,
    source_evidence_id TEXT NOT NULL,
    source_candidate_id TEXT NOT NULL,
    source_content_id TEXT NOT NULL,
    source_visual_unit_id TEXT NOT NULL,
    source_canonical_visual_unit_id TEXT NOT NULL,
    source_derived_id TEXT NOT NULL,
    source_frame_index INTEGER NOT NULL,
    source_time_position_ms INTEGER NOT NULL,
    target_canonical_visual_unit_id TEXT NOT NULL,
    target_derived_id TEXT NOT NULL,
    target_frame_index INTEGER NOT NULL,
    target_time_position_ms INTEGER NOT NULL,
    frame_offset INTEGER NOT NULL CHECK (
        frame_offset IN (-3,-2,-1,1,2,3)
    ),
    propagation_direction TEXT NOT NULL CHECK (
        propagation_direction IN ('previous','next')
    ),
    propagation_step INTEGER NOT NULL CHECK (
        propagation_step BETWEEN 1 AND 3
    ),
    target_has_direct_qwenvl INTEGER NOT NULL CHECK (
        target_has_direct_qwenvl IN (0,1)
    ),
    propagated_label TEXT NOT NULL,
    propagated_label_zh TEXT NOT NULL,
    source_yolo_confidence REAL NOT NULL CHECK (
        source_yolo_confidence >= 0.0 AND source_yolo_confidence <= 1.0
    ),
    target_yolo_confidence REAL NOT NULL CHECK (
        target_yolo_confidence >= 0.0 AND target_yolo_confidence <= 1.0
    ),
    propagated_text TEXT NOT NULL,
    propagated_text_sha256 TEXT NOT NULL,
    source_text_sha256 TEXT NOT NULL,
    gate_status TEXT NOT NULL CHECK (
        gate_status = 'passed_qwen_source_target_yolo_intersection'
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY(propagation_run_id, propagation_id),
    UNIQUE(
        propagation_run_id,
        source_evidence_id,
        target_derived_id,
        propagated_label
    ),
    FOREIGN KEY(propagation_run_id)
        REFERENCES stop03_5c_propagation_runs(propagation_run_id),
    FOREIGN KEY(source_staging_run_id)
        REFERENCES stop03_5_unified_evidence_runs(staging_run_id),
    FOREIGN KEY(source_derived_id) REFERENCES derived_assets(derived_id),
    FOREIGN KEY(target_derived_id) REFERENCES derived_assets(derived_id)
);

CREATE INDEX IF NOT EXISTS idx_stop03_5c_target
ON stop03_5c_propagation_items(
    propagation_run_id,target_derived_id,propagated_label
);

CREATE INDEX IF NOT EXISTS idx_stop03_5c_source
ON stop03_5c_propagation_items(
    propagation_run_id,source_evidence_id,propagation_step
);

CREATE VIEW IF NOT EXISTS v_stop03_5c_latest_propagation AS
SELECT i.*
FROM stop03_5c_propagation_items AS i
JOIN stop03_5c_propagation_runs AS r
  ON r.propagation_run_id=i.propagation_run_id
WHERE r.status='success'
  AND r.propagation_run_id=(
      SELECT r2.propagation_run_id
      FROM stop03_5c_propagation_runs AS r2
      WHERE r2.status='success'
      ORDER BY r2.created_at DESC,r2.propagation_run_id DESC
      LIMIT 1
  );
