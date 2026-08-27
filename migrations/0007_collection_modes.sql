-- 0007: Per-collection sync mode + last imported release tracking.
--
-- sync mode per collection:
--   'auto'   (default) – always download the newest cumulative release, then
--                        import it. Incremental thanks to the .prev seed base.
--   'import' – import from the local .prev payload; if a NEWER cumulative
--              release exists, fetch it as a small delta (shared byte-identical
--              prefix) and import, keeping the DB current. Only when a download
--              stalls do we import the locally available payload resiliently
--              instead of waiting indefinitely for rare tail pieces.
--
-- last_imported_identifier records which release the local .prev payload
-- corresponds to, so we can detect whether a delta update is required.

CREATE TABLE IF NOT EXISTS collection_sync_modes (
    collection                TEXT PRIMARY KEY,
    mode                      TEXT NOT NULL DEFAULT 'auto'
        CHECK (mode IN ('auto', 'import')),
    last_imported_identifier  TEXT,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
