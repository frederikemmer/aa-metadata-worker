-- 0009: Durable import performance history and editable upload filters.

CREATE TABLE IF NOT EXISTS import_performance_buckets (
    release_id          BIGINT NOT NULL REFERENCES sync_releases(id) ON DELETE CASCADE,
    bucket_start        TIMESTAMPTZ NOT NULL,
    first_sample_at     TIMESTAMPTZ NOT NULL,
    last_sample_at      TIMESTAMPTZ NOT NULL,
    first_records_seen  BIGINT NOT NULL,
    last_records_seen   BIGINT NOT NULL,
    first_bytes         BIGINT NOT NULL,
    last_bytes          BIGINT NOT NULL,
    PRIMARY KEY (release_id, bucket_start)
);

CREATE INDEX IF NOT EXISTS idx_import_performance_buckets_recent
    ON import_performance_buckets (bucket_start DESC);

CREATE TABLE IF NOT EXISTS upload_subcollection_filters (
    subcollection  TEXT PRIMARY KEY,
    blocked        BOOLEAN NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
