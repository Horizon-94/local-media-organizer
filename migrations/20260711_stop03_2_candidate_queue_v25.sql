-- Stop03-2 V25 candidate queue migration.
-- The V25 finalizer checks PRAGMA table_info and executes only statements for
-- missing columns, making this migration idempotent through the finalizer.

ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN candidate_role TEXT;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN canonical_visual_unit_id TEXT;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN duplicate_group_id TEXT;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN frame_index INTEGER;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN time_position_ms INTEGER;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN canonical_time_ms INTEGER;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN group_start_ms INTEGER;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN group_end_ms INTEGER;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN segment_start_ms INTEGER;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN segment_end_ms INTEGER;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN policy_version TEXT;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN script_sha256 TEXT;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN config_sha256 TEXT;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN rule_document_sha256 TEXT;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN central_dedup_run_id TEXT;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN yoloe_run_id TEXT;
ALTER TABLE stop03_2_candidate_queue_items ADD COLUMN openclip_run_id TEXT;
