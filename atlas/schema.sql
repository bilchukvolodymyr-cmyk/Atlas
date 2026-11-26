-- Atlas core schema, version 1.
-- events is the source of truth (append-only); loops/ideas/entries/metrics hold current state.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,            -- shortcut / web / cli
    external_id  TEXT NOT NULL,            -- UUID from the client (Idempotency-Key)
    received_at  TEXT NOT NULL,            -- ISO-8601 UTC
    raw_text     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new', 'processing', 'done', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY,
    inbox_id     INTEGER NOT NULL REFERENCES inbox(id),
    happened_at  TEXT,                     -- NULL = unknown, never guessed
    kind         TEXT NOT NULL CHECK (kind IN ('event', 'note', 'decision', 'problem')),
    text         TEXT NOT NULL,
    summary      TEXT
);

CREATE TABLE IF NOT EXISTS loops (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('task', 'reminder', 'waiting_for', 'agreement')),
    title           TEXT NOT NULL,
    desired_outcome TEXT,
    state           TEXT NOT NULL CHECK (state IN
                    ('needs_info', 'active', 'waiting', 'done_claimed', 'verifying', 'closed', 'dropped')),
    next_action     TEXT,
    waiting_on      TEXT,
    assigned_by     TEXT,
    due_at          TEXT,
    remind_at       TEXT,
    importance      TEXT CHECK (importance IS NULL OR importance IN ('low', 'normal', 'high')),
    recurrence      TEXT,
    done_criteria   TEXT CHECK (done_criteria IS NULL OR done_criteria IN ('self', 'artifact', 'deferred')),
    created_from    INTEGER NOT NULL REFERENCES inbox(id),
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ideas (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN
                    ('new', 'exploring', 'planned', 'in_work', 'done', 'dropped')),
    created_from    INTEGER NOT NULL REFERENCES inbox(id),
    last_touched_at TEXT NOT NULL,
    snoozed_until   TEXT,
    version         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS metrics (
    id       INTEGER PRIMARY KEY,
    date     TEXT NOT NULL,               -- YYYY-MM-DD
    site     TEXT NOT NULL DEFAULT 'all',
    name     TEXT NOT NULL,
    value    REAL NOT NULL,
    unit     TEXT,
    inbox_id INTEGER NOT NULL REFERENCES inbox(id),
    UNIQUE (date, site, name)
);

CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY,
    object_type   TEXT,
    object_id     INTEGER,
    inbox_id      INTEGER NOT NULL REFERENCES inbox(id),
    text          TEXT NOT NULL,
    missing_field TEXT,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'expired')),
    asked_at      TEXT NOT NULL
);

-- Append-only journal. (inbox_id, seq) makes re-applying the same message a no-op.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    inbox_id    INTEGER NOT NULL REFERENCES inbox(id),
    seq         INTEGER NOT NULL,
    object_type TEXT NOT NULL,
    object_id   INTEGER NOT NULL,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,            -- JSON: {"before": ..., "after": ..., ...}
    actor       TEXT NOT NULL CHECK (actor IN ('user', 'llm', 'system')),
    at          TEXT NOT NULL,
    UNIQUE (inbox_id, seq)
);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE INDEX IF NOT EXISTS idx_events_object ON events(object_type, object_id, id);
CREATE INDEX IF NOT EXISTS idx_loops_state ON loops(state);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);
