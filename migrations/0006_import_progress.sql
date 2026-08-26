-- Live import progress for the dashboard: byte position within the payload
-- while streaming (compressed bytes consumed vs. total file size).
ALTER TABLE sync_releases
    ADD COLUMN IF NOT EXISTS import_done_bytes BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS import_total_bytes BIGINT NOT NULL DEFAULT 0;
