-- 0003: Live-Download-Fortschritt für das Dashboard (DB-getrieben, containerübergreifend).
ALTER TABLE sync_releases
    ADD COLUMN download_done_bytes BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN download_total_bytes BIGINT NOT NULL DEFAULT 0;
