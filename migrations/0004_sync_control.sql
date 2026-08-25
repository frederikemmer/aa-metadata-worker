-- 0004: Dashboard-Steuerung des Sync-Workers (DB-getrieben, containerübergreifend).
-- sync_commands: Befehlsqueue (API schreibt, Worker pollt und markiert).
-- sync_control_state: Key/Value-Flags ('paused' = 'true'/'false').
CREATE TABLE IF NOT EXISTS sync_commands (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    command TEXT NOT NULL CHECK (command IN ('run_now', 'pause', 'resume')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    picked_at TIMESTAMPTZ,
    note TEXT
);

CREATE TABLE IF NOT EXISTS sync_control_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO sync_control_state (key, value) VALUES ('paused', 'false')
    ON CONFLICT (key) DO NOTHING;
