-- Video Understanding Agent unified SQLite schema (v1.1 / migration 001)
-- Single source of truth; no ad-hoc CREATE TABLE elsewhere in the codebase.

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE assets (
    asset_id TEXT PRIMARY KEY,
    source_artifact_id TEXT,
    source_artifact_kind TEXT NOT NULL DEFAULT 'source_video'
        CHECK (source_artifact_kind = 'source_video'),
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (typeof(size_bytes) = 'integer' AND size_bytes > 0),
    duration_ms INTEGER NOT NULL CHECK (typeof(duration_ms) = 'integer' AND duration_ms > 0),
    width INTEGER NOT NULL CHECK (typeof(width) = 'integer' AND width > 0),
    height INTEGER NOT NULL CHECK (typeof(height) = 'integer' AND height > 0),
    video_codec TEXT NOT NULL,
    has_audio INTEGER NOT NULL CHECK (typeof(has_audio) = 'integer' AND has_audio IN (0, 1)),
    audio_codec TEXT,
    display_filename TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('importing', 'ready', 'failed')),
    created_at TEXT NOT NULL,
    CHECK (status != 'ready' OR source_artifact_id IS NOT NULL),
    FOREIGN KEY (source_artifact_id, asset_id, source_artifact_kind)
        REFERENCES artifacts(artifact_id, asset_id, kind)
);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    kind TEXT NOT NULL CHECK (
        kind IN ('source_video', 'audio', 'frame', 'crop', 'grid_preview', 'export')
    ),
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (typeof(size_bytes) = 'integer' AND size_bytes >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (artifact_id, asset_id),
    UNIQUE (artifact_id, asset_id, kind)
);

CREATE TABLE analysis_runs (
    analysis_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    config_hash TEXT NOT NULL,
    prompt_bundle_hash TEXT NOT NULL,
    index_state TEXT NOT NULL CHECK (
        index_state IN ('pending', 'indexing', 'ready', 'partial', 'failed')
    ),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'partial', 'succeeded', 'failed', 'cancelled')
    ),
    created_at TEXT NOT NULL,
    UNIQUE (analysis_id, asset_id)
);

CREATE TABLE questions (
    question_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    analysis_id TEXT NOT NULL,
    query_text TEXT NOT NULL CHECK (length(query_text) > 0),
    mode TEXT NOT NULL CHECK (mode IN ('qa', 'event_localization')),
    created_at TEXT NOT NULL,
    UNIQUE (question_id, analysis_id, asset_id),
    FOREIGN KEY (analysis_id, asset_id)
        REFERENCES analysis_runs(analysis_id, asset_id)
);

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    analysis_id TEXT NOT NULL,
    question_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('analysis', 'qa')),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'partial', 'succeeded', 'failed', 'cancelled')
    ),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    attempt_no INTEGER NOT NULL DEFAULT 0 CHECK (attempt_no >= 0),
    idempotency_scope TEXT,
    idempotency_key TEXT,
    idempotency_payload_hash TEXT,
    started_at_utc TEXT,
    deadline_at_utc TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (kind = 'analysis' AND question_id IS NULL)
        OR (kind = 'qa' AND question_id IS NOT NULL)
    ),
    CHECK (
        (idempotency_key IS NULL AND idempotency_scope IS NULL AND idempotency_payload_hash IS NULL)
        OR (
            idempotency_key IS NOT NULL
            AND idempotency_scope IS NOT NULL
            AND idempotency_payload_hash IS NOT NULL
        )
    ),
    FOREIGN KEY (analysis_id, asset_id)
        REFERENCES analysis_runs(analysis_id, asset_id),
    FOREIGN KEY (question_id, analysis_id, asset_id)
        REFERENCES questions(question_id, analysis_id, asset_id)
);

CREATE UNIQUE INDEX uq_jobs_analysis_run
    ON jobs(analysis_id)
    WHERE kind = 'analysis';

CREATE UNIQUE INDEX uq_jobs_qa_question
    ON jobs(question_id)
    WHERE kind = 'qa';

CREATE UNIQUE INDEX uq_jobs_idempotency
    ON jobs(kind, idempotency_scope, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    kind TEXT NOT NULL CHECK (kind IN ('frame', 'transcript')),
    point_ms INTEGER CHECK (point_ms IS NULL OR typeof(point_ms) = 'integer'),
    start_ms INTEGER CHECK (start_ms IS NULL OR typeof(start_ms) = 'integer'),
    end_ms INTEGER CHECK (end_ms IS NULL OR typeof(end_ms) = 'integer'),
    artifact_id TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (evidence_id, asset_id),
    CHECK (
        (
            kind = 'frame'
            AND point_ms IS NOT NULL
            AND typeof(point_ms) = 'integer'
            AND point_ms >= 0
            AND start_ms IS NULL
            AND end_ms IS NULL
            AND artifact_id IS NOT NULL
        )
        OR (
            kind = 'transcript'
            AND point_ms IS NULL
            AND start_ms IS NOT NULL
            AND end_ms IS NOT NULL
            AND typeof(start_ms) = 'integer'
            AND typeof(end_ms) = 'integer'
            AND start_ms >= 0
            AND end_ms > start_ms
        )
    ),
    FOREIGN KEY (artifact_id, asset_id)
        REFERENCES artifacts(artifact_id, asset_id)
);

CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('observed', 'heard', 'inferred')),
    text TEXT NOT NULL CHECK (length(text) > 0),
    start_ms INTEGER NOT NULL CHECK (typeof(start_ms) = 'integer' AND start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (typeof(end_ms) = 'integer' AND end_ms > start_ms),
    region_hint TEXT,
    provenance TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (observation_id, asset_id),
    FOREIGN KEY (analysis_id, asset_id)
        REFERENCES analysis_runs(analysis_id, asset_id)
);

CREATE TABLE observation_evidence (
    observation_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    PRIMARY KEY (observation_id, evidence_id),
    FOREIGN KEY (observation_id, asset_id)
        REFERENCES observations(observation_id, asset_id),
    FOREIGN KEY (evidence_id, asset_id)
        REFERENCES evidence(evidence_id, asset_id)
);

CREATE TABLE answers (
    answer_id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL UNIQUE,
    analysis_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('answered', 'partial', 'not_answerable')),
    payload TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (question_id, analysis_id, asset_id)
        REFERENCES questions(question_id, analysis_id, asset_id)
);

CREATE TABLE search_docs (
    doc_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('transcript', 'observation')),
    source_id TEXT NOT NULL,
    original_text TEXT NOT NULL,
    tokens TEXT NOT NULL,
    start_ms INTEGER NOT NULL CHECK (typeof(start_ms) = 'integer' AND start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (typeof(end_ms) = 'integer' AND end_ms > start_ms),
    index_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (analysis_id, asset_id)
        REFERENCES analysis_runs(analysis_id, asset_id)
);

CREATE INDEX ix_search_docs_scope_version
    ON search_docs(asset_id, analysis_id, index_version);

CREATE VIRTUAL TABLE search_fts USING fts5(
    doc_id UNINDEXED,
    tokens,
    tokenize = 'unicode61'
);

CREATE TABLE budget_accounts (
    scope_id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('analysis', 'qa')),
    limits_json TEXT NOT NULL,
    reserved_json TEXT NOT NULL,
    consumed_json TEXT NOT NULL,
    started_at_utc TEXT,
    deadline_at_utc TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0 CHECK (cancelled IN (0, 1)),
    has_unknown_external_result INTEGER NOT NULL DEFAULT 0
        CHECK (has_unknown_external_result IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE usage_records (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    scope_id TEXT NOT NULL REFERENCES budget_accounts(scope_id),
    parent_tool_call_id TEXT,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'succeeded', 'failed', 'not_sent', 'unknown')
    ),
    reservation_json TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    cost_status TEXT NOT NULL CHECK (cost_status IN ('estimated', 'confirmed', 'unknown')),
    provider_request_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE stage_results (
    analysis_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    item_key TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    artifact_id TEXT,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('committed', 'invalidated', 'failed')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (analysis_id, stage, item_key),
    FOREIGN KEY (analysis_id, asset_id)
        REFERENCES analysis_runs(analysis_id, asset_id),
    FOREIGN KEY (artifact_id, asset_id)
        REFERENCES artifacts(artifact_id, asset_id)
);
