-- 0001: Initial schema: metadata_records + sync_releases.
-- Normalized (diacritics-stripped, casefolded) columns are written by the
-- Python pipeline; FTS uses the 'simple' config over those normalized values
-- so indexing and query normalization are guaranteed identical.

CREATE TABLE metadata_records (
    md5               BYTEA PRIMARY KEY CHECK (octet_length(md5) = 16),
    title             TEXT NOT NULL DEFAULT '',
    title_norm        TEXT NOT NULL DEFAULT '',
    authors           TEXT[] NOT NULL DEFAULT '{}',
    author_tokens     TEXT[] NOT NULL DEFAULT '{}',
    publisher         TEXT,
    publication_year  SMALLINT,
    languages         TEXT[] NOT NULL DEFAULT '{}',
    extension         TEXT,
    filesize          BIGINT,

    isbn10            TEXT[] NOT NULL DEFAULT '{}',
    isbn13            TEXT[] NOT NULL DEFAULT '{}',
    doi               TEXT[] NOT NULL DEFAULT '{}',
    oclc              TEXT[] NOT NULL DEFAULT '{}',
    openlibrary_ids   TEXT[] NOT NULL DEFAULT '{}',

    work_key          TEXT,

    source_collection TEXT NOT NULL,
    source_record_id  TEXT,
    aacid             TEXT,
    source_timestamp  TIMESTAMPTZ,

    quality_score     SMALLINT NOT NULL DEFAULT 0,
    deleted           BOOLEAN NOT NULL DEFAULT FALSE,
    removed_reason    TEXT,
    ipfs_cid          TEXT,

    search_tsv        TSVECTOR NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Full text search over title (A), authors (B), publisher (C).
CREATE INDEX idx_metadata_search_tsv ON metadata_records USING GIN (search_tsv);
-- Exact identifier lookups.
CREATE INDEX idx_metadata_isbn13 ON metadata_records USING GIN (isbn13);
CREATE INDEX idx_metadata_isbn10 ON metadata_records USING GIN (isbn10);
CREATE INDEX idx_metadata_doi ON metadata_records USING GIN (doi);
-- Structured author filter via token containment (e.g. ARRAY['zizek']).
CREATE INDEX idx_metadata_author_tokens ON metadata_records USING GIN (author_tokens);
-- Logical work grouping (deterministic identifiers only).
CREATE INDEX idx_metadata_work_key ON metadata_records (work_key) WHERE work_key IS NOT NULL;

CREATE FUNCTION metadata_records_tsv_update() RETURNS trigger AS $$
BEGIN
    NEW.search_tsv :=
        setweight(to_tsvector('simple', coalesce(NEW.title_norm, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(array_to_string(NEW.author_tokens, ' '), '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(NEW.publisher, '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_metadata_records_tsv
BEFORE INSERT OR UPDATE OF title_norm, author_tokens, publisher
ON metadata_records
FOR EACH ROW EXECUTE FUNCTION metadata_records_tsv_update();

CREATE FUNCTION metadata_records_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_metadata_records_updated_at
BEFORE UPDATE ON metadata_records
FOR EACH ROW EXECUTE FUNCTION metadata_records_updated_at();

CREATE TABLE sync_releases (
    id                  BIGSERIAL PRIMARY KEY,
    collection          TEXT NOT NULL,
    release_identifier  TEXT NOT NULL,
    btih                TEXT,
    source_url          TEXT,
    data_size_bytes     BIGINT,
    status              TEXT NOT NULL DEFAULT 'discovered'
        CHECK (status IN ('discovered', 'downloading', 'importing', 'validating',
                          'completed', 'failed', 'blocked_storage')),
    discovered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    records_seen        BIGINT NOT NULL DEFAULT 0,
    records_inserted    BIGINT NOT NULL DEFAULT 0,
    records_updated     BIGINT NOT NULL DEFAULT 0,
    records_skipped     BIGINT NOT NULL DEFAULT 0,
    records_failed      BIGINT NOT NULL DEFAULT 0,
    error_message       TEXT,
    UNIQUE (collection, release_identifier)
);

CREATE INDEX idx_sync_releases_status ON sync_releases (status);
CREATE INDEX idx_sync_releases_collection_recent ON sync_releases (collection, discovered_at DESC);
