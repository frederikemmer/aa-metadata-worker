-- 0005: Series und Edition-Metadaten für Reihen-Suche und Multi-Version-Gruppierung.
ALTER TABLE metadata_records
    ADD COLUMN series_name TEXT,
    ADD COLUMN series_position SMALLINT,
    ADD COLUMN edition TEXT;

CREATE INDEX idx_metadata_series_name ON metadata_records (series_name) WHERE series_name IS NOT NULL;
