-- 0002: Discarded-counter (verworfene Nicht-Buch-Records je Release sichtbar machen).
ALTER TABLE sync_releases ADD COLUMN records_discarded BIGINT NOT NULL DEFAULT 0;
