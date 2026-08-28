-- 0010: Import-free subcollection statistics reanalysis.

CREATE TABLE IF NOT EXISTS filter_analysis_jobs (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    release_id          BIGINT NOT NULL REFERENCES sync_releases(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    filters_snapshot    JSONB NOT NULL DEFAULT '{}'::jsonb,
    progress_bytes      BIGINT NOT NULL DEFAULT 0,
    total_bytes         BIGINT NOT NULL DEFAULT 0,
    records_scanned     BIGINT NOT NULL DEFAULT 0,
    result_counts       JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS release_subcollection_stats (
    release_id          BIGINT NOT NULL REFERENCES sync_releases(id) ON DELETE CASCADE,
    subcollection       TEXT NOT NULL,
    matching_records    BIGINT NOT NULL,
    filter_blocked      BOOLEAN NOT NULL,
    analyzed_at         TIMESTAMPTZ NOT NULL,
    analysis_job_id     BIGINT REFERENCES filter_analysis_jobs(id) ON DELETE SET NULL,
    PRIMARY KEY (release_id, subcollection)
);
