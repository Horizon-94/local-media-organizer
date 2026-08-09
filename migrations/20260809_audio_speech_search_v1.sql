PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS audio_enrichment_runs(
    run_id TEXT PRIMARY KEY,
    contract_version TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    source_video_count INTEGER NOT NULL DEFAULT 0,
    completed_video_count INTEGER NOT NULL DEFAULT 0,
    speech_video_count INTEGER NOT NULL DEFAULT 0,
    no_speech_video_count INTEGER NOT NULL DEFAULT 0,
    no_audio_video_count INTEGER NOT NULL DEFAULT 0,
    failed_video_count INTEGER NOT NULL DEFAULT 0,
    transcript_segment_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('running','success','failed','interrupted')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audio_processing_items(
    source_content_id TEXT PRIMARY KEY REFERENCES source_assets(source_content_id),
    source_path TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    config_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(
        status IN ('pending','running','success','no_speech','no_audio','failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    transcript_segment_count INTEGER NOT NULL DEFAULT 0,
    output_report_path TEXT NOT NULL DEFAULT '',
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audio_processing_status
ON audio_processing_items(status,source_content_id);

CREATE TABLE IF NOT EXISTS audio_speech_evidence(
    evidence_id TEXT PRIMARY KEY,
    source_content_id TEXT NOT NULL REFERENCES source_assets(source_content_id),
    source_path TEXT NOT NULL,
    start_time_ms INTEGER NOT NULL CHECK(start_time_ms >= 0),
    end_time_ms INTEGER NOT NULL CHECK(end_time_ms > start_time_ms),
    hit_time_ms INTEGER NOT NULL CHECK(hit_time_ms >= start_time_ms),
    transcript_text TEXT NOT NULL CHECK(length(trim(transcript_text)) > 0),
    language TEXT,
    preview_windows_json TEXT NOT NULL,
    evidence_type TEXT NOT NULL DEFAULT 'speech_text',
    transcript_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audio_speech_source_time
ON audio_speech_evidence(source_content_id,start_time_ms,end_time_ms);

CREATE TABLE IF NOT EXISTS audio_text_embeddings(
    evidence_id TEXT PRIMARY KEY
        REFERENCES audio_speech_evidence(evidence_id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    model_path TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK(dimension > 0),
    vector_dtype TEXT NOT NULL CHECK(vector_dtype='float32'),
    normalized INTEGER NOT NULL CHECK(normalized=1),
    vector_blob BLOB NOT NULL,
    vector_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success','failed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audio_search_metadata(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR REPLACE INTO audio_search_metadata(key,value)
VALUES('schema_version','media_archive_audio_speech_search_v1');
