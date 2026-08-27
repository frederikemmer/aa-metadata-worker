-- 0008: Persist lightweight import performance and discard diagnostics.
-- JSON objects stay bounded by the importer (three samples per reason).

ALTER TABLE sync_releases
    ADD COLUMN import_started_at TIMESTAMPTZ,
    ADD COLUMN discard_reasons JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN discard_samples JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE sync_releases
    ADD CONSTRAINT sync_releases_discard_reasons_object
        CHECK (jsonb_typeof(discard_reasons) = 'object'),
    ADD CONSTRAINT sync_releases_discard_samples_object
        CHECK (jsonb_typeof(discard_samples) = 'object');
