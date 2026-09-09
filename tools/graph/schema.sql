-- Autonomy Knowledge Graph Schema
-- SQLite with FTS5 for full-text search

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- SOURCES — origin records (conversations, files, URLs)
-- ============================================================
-- publication_state: scope+facet primitive (graph://8cf067e3-ca3).
--   'raw' (default)    — substrate/telemetry; excluded from cross-session search surface.
--   'curated'          — org-reviewed reference; visible in org default surface.
--   'published'        — cross-org reference; visible to subscriber orgs.
--   'canonical'        — authoritative, pinned top-rank.
-- deprecated/successor_id: terminal modifiers orthogonal to state.
-- moved_to_org: relocation marker for moved-from stubs kept in the origin org.
CREATE TABLE IF NOT EXISTS sources (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL,          -- 'conversation', 'musing', 'document', 'url', 'session', 'note', 'agentic'
    platform          TEXT,                   -- 'chatgpt', 'claude', 'claude-code', 'local', etc.
    title             TEXT,
    url               TEXT,
    file_path         TEXT UNIQUE,            -- local file path (for dedup on re-ingest)
    metadata          TEXT DEFAULT '{}',      -- JSON blob for extra fields
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ingested_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_activity_at  TEXT,                   -- timestamp of latest ingested turn (for activity-based ordering)
    publication_state TEXT NOT NULL DEFAULT 'curated'
        CHECK (publication_state IN ('raw','curated','published','canonical')),
    deprecated        INTEGER NOT NULL DEFAULT 0 CHECK (deprecated IN (0,1)),
    successor_id      TEXT,                   -- loose reference to another source (promotion succession)
    moved_to_org      TEXT,
    persona_id        TEXT,                   -- org-scoped human persona; nullable for legacy/imports
    session_id        TEXT,                   -- submitting tmux session; nullable for direct browser writes
    short_description TEXT,                   -- one or two sentences explaining the source's purpose
    keywords          TEXT                    -- comma-separated synonym/alias list, indexed by sources_fts
);
-- The ``type`` column is an open string; common values include the ones
-- listed above. The ``agentic`` value identifies short-lived agent-action
-- runs spawned from the dashboard; rows of that type are excluded from
-- /api/search and /collab Recent by default (see GraphDB.search's
-- ``excluded_source_types`` arg).
-- idx_sources_last_activity, idx_sources_publication_state, and
-- idx_sources_type are created via their _migrate_* methods, so legacy DBs
-- that pre-date these columns don't fail on CREATE INDEX during executescript.

-- ============================================================
-- THOUGHTS — user assertions, questions, intents (sovereign)
-- ============================================================
-- thoughts are session turns — publication_state is pinned to 'raw' (fixed-state).
CREATE TABLE IF NOT EXISTS thoughts (
    id                TEXT PRIMARY KEY,
    source_id         TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    content           TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'user',   -- 'user' for sovereign thoughts
    turn_number       INTEGER,                        -- position in conversation
    message_id        TEXT,                            -- platform message ID if available
    tags              TEXT DEFAULT '[]',              -- JSON array of topic tags
    metadata          TEXT DEFAULT '{}',              -- JSON blob
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    publication_state TEXT NOT NULL DEFAULT 'raw' CHECK (publication_state = 'raw'),
    persona_id        TEXT,
    session_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_thoughts_source ON thoughts(source_id);
CREATE INDEX IF NOT EXISTS idx_thoughts_source_turn ON thoughts(source_id, turn_number);

-- ============================================================
-- DERIVATIONS — AI responses (regenerable, non-sovereign)
-- ============================================================
CREATE TABLE IF NOT EXISTS derivations (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    thought_id  TEXT REFERENCES thoughts(id) ON DELETE SET NULL,  -- the thought this responds to
    content     TEXT NOT NULL,
    model       TEXT,                   -- 'chatgpt', 'claude', etc.
    turn_number INTEGER,
    message_id  TEXT,
    metadata    TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_derivations_source ON derivations(source_id);
CREATE INDEX IF NOT EXISTS idx_derivations_source_turn ON derivations(source_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_derivations_thought ON derivations(thought_id);

-- ============================================================
-- CLAIMS — structured assertions with provenance
-- ============================================================
CREATE TABLE IF NOT EXISTS claims (
    id          TEXT PRIMARY KEY,
    subject_id  TEXT NOT NULL,              -- entity or thought ID
    predicate   TEXT NOT NULL,              -- relationship type
    object_id   TEXT,                       -- entity or thought ID (nullable for literal values)
    object_val  TEXT,                       -- literal value when object is not an entity
    source_id   TEXT REFERENCES sources(id) ON DELETE SET NULL,
    asserted_by TEXT,                       -- 'user', 'agent', 'extracted'
    confidence  REAL DEFAULT 1.0,
    status      TEXT DEFAULT 'asserted',    -- 'asserted', 'extracted', 'inferred', 'contested', 'deprecated'
    evidence    TEXT,                       -- supporting text/reference
    metadata    TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims(subject_id);
CREATE INDEX IF NOT EXISTS idx_claims_predicate ON claims(predicate);
CREATE INDEX IF NOT EXISTS idx_claims_object ON claims(object_id);

-- ============================================================
-- EDGES — typed relationships between any objects
-- ============================================================
CREATE TABLE IF NOT EXISTS edges (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    source_type TEXT NOT NULL,   -- 'thought', 'derivation', 'entity', 'claim', 'source'
    target_id   TEXT NOT NULL,
    target_type TEXT NOT NULL,
    relation    TEXT NOT NULL,   -- 'mentions', 'responds_to', 'related_to', 'derived_from', 'supports', 'refutes', 'supersedes'
    weight      REAL DEFAULT 1.0,
    metadata    TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(source_id, target_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id, source_type);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id, target_type);
CREATE INDEX IF NOT EXISTS idx_edges_relation ON edges(relation);

-- ============================================================
-- FTS5 — full-text search
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS thoughts_fts USING fts5(
    id UNINDEXED,
    content,
    tags,
    content=thoughts,
    content_rowid=rowid
);

CREATE VIRTUAL TABLE IF NOT EXISTS derivations_fts USING fts5(
    id UNINDEXED,
    content,
    content=derivations,
    content_rowid=rowid
);

-- sources_fts indexes operator/Haiku-authored metadata (title +
-- short_description + keywords). Curated metadata gets a higher rank
-- weight than long-tail body text in GraphDB.search() so a clean title
-- match outranks a TF-density body match (see SEARCH_TITLE_BOOST in db.py).
-- The virtual table + triggers are created by _migrate_sources_fts in
-- db.py (NOT here) because the ``keywords`` column is added by
-- _migrate_source_keywords after this executescript runs on legacy DBs.
-- Trigger shape mirrors thoughts_ai / thoughts_ad / thoughts_au exactly.

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS thoughts_ai AFTER INSERT ON thoughts BEGIN
    INSERT INTO thoughts_fts(rowid, id, content, tags)
    VALUES (new.rowid, new.id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS thoughts_ad AFTER DELETE ON thoughts BEGIN
    INSERT INTO thoughts_fts(thoughts_fts, rowid, id, content, tags)
    VALUES ('delete', old.rowid, old.id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS thoughts_au AFTER UPDATE ON thoughts BEGIN
    INSERT INTO thoughts_fts(thoughts_fts, rowid, id, content, tags)
    VALUES ('delete', old.rowid, old.id, old.content, old.tags);
    INSERT INTO thoughts_fts(rowid, id, content, tags)
    VALUES (new.rowid, new.id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS derivations_ai AFTER INSERT ON derivations BEGIN
    INSERT INTO derivations_fts(rowid, id, content)
    VALUES (new.rowid, new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS derivations_ad AFTER DELETE ON derivations BEGIN
    INSERT INTO derivations_fts(derivations_fts, rowid, id, content)
    VALUES ('delete', old.rowid, old.id, old.content);
END;

-- ============================================================
-- HIERARCHY — tree structure for knowledge organization
-- ============================================================
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,      -- 'mission', 'module', 'component', 'feature', 'task', 'reference'
    title       TEXT NOT NULL,
    description TEXT,
    status      TEXT DEFAULT 'active',  -- 'active', 'planned', 'in_progress', 'completed', 'deprecated'
    sort_order  INTEGER DEFAULT 0,
    metadata    TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);

-- Link nodes to entities, thoughts, etc.
CREATE TABLE IF NOT EXISTS node_refs (
    node_id     TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    ref_id      TEXT NOT NULL,
    ref_type    TEXT NOT NULL,   -- 'entity', 'thought', 'derivation', 'claim', 'source', 'url'
    metadata    TEXT DEFAULT '{}',
    PRIMARY KEY (node_id, ref_id)
);

-- ============================================================
-- NOTE COMMENTS — annotations on note sources
-- ============================================================
-- note_comments are annotations — publication_state is pinned to 'raw' (fixed-state).
CREATE TABLE IF NOT EXISTS note_comments (
    id                TEXT PRIMARY KEY,
    source_id         TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    content           TEXT NOT NULL,
    anchor_json       TEXT,                  -- optional v1 selected-text anchor (canonical JSON)
    actor             TEXT DEFAULT 'user',
    integrated        INTEGER DEFAULT 0,    -- 0=active, 1=integrated (content rolled into note body)
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    publication_state TEXT NOT NULL DEFAULT 'raw' CHECK (publication_state = 'raw'),
    persona_id        TEXT,
    session_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_note_comments_source ON note_comments(source_id);

-- ============================================================
-- NOTE VERSIONS — append-only version history for notes
-- ============================================================
CREATE TABLE IF NOT EXISTS note_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    persona_id  TEXT,
    session_id  TEXT,
    UNIQUE(source_id, version)
);
CREATE INDEX IF NOT EXISTS idx_note_versions_source ON note_versions(source_id);

-- ============================================================
-- ATTACHMENTS — binary files with hash dedup and provenance
-- ============================================================
CREATE TABLE IF NOT EXISTS attachments (
    id          TEXT PRIMARY KEY,
    hash        TEXT NOT NULL,          -- SHA256 of file content
    filename    TEXT NOT NULL,          -- original filename
    mime_type   TEXT,                   -- e.g. image/png, application/json
    size_bytes  INTEGER NOT NULL,
    file_path   TEXT NOT NULL UNIQUE,   -- path in data/attachments/{hash[:2]}/{hash}.{ext}
    source_id   TEXT,                   -- linked graph source (session/note)
    turn_number INTEGER,               -- conversation turn
    metadata    TEXT DEFAULT '{}',      -- JSON: width, height, description, tags
    alt_text    TEXT,                   -- textual description / alt-text for accessibility and agent consumption
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    persona_id  TEXT,
    session_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_attachments_hash ON attachments(hash);
CREATE INDEX IF NOT EXISTS idx_attachments_source ON attachments(source_id);

-- ============================================================
-- NOTE READS — read tracking for collaborative notes
-- ============================================================
CREATE TABLE IF NOT EXISTS note_reads (
    source_id TEXT NOT NULL,
    actor     TEXT NOT NULL,
    ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (source_id, actor, ts)
);
CREATE INDEX IF NOT EXISTS idx_note_reads_source ON note_reads(source_id);

-- ============================================================
-- TAGS — first-class tag entities with descriptions
-- ============================================================
CREATE TABLE IF NOT EXISTS tags (
    name        TEXT PRIMARY KEY,
    description TEXT DEFAULT '',
    created_by  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================
-- THREADS — conversation threads for organizing captures
-- ============================================================
CREATE TABLE IF NOT EXISTS threads (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    priority    INTEGER NOT NULL DEFAULT 1,
    summary     TEXT,
    created_by  TEXT,
    metadata    TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);

-- ============================================================
-- CAPTURES — raw thought captures (inbox → threads)
-- ============================================================
-- DEPRECATED: captures were a lightly-used inbox feature exposed confusingly
-- as "thoughts". Do not add new product dependencies; replace consumers before removal.
CREATE TABLE IF NOT EXISTS captures (
    id                TEXT PRIMARY KEY,
    content           TEXT NOT NULL,
    thread_id         TEXT REFERENCES threads(id) ON DELETE SET NULL,
    source_id         TEXT REFERENCES sources(id) ON DELETE SET NULL,
    turn_number       INTEGER,
    status            TEXT NOT NULL DEFAULT 'captured',
    actor             TEXT DEFAULT 'user',
    metadata          TEXT DEFAULT '{}',
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    publication_state TEXT NOT NULL DEFAULT 'raw' CHECK (publication_state = 'raw')
);
CREATE INDEX IF NOT EXISTS idx_captures_thread ON captures(thread_id);
CREATE INDEX IF NOT EXISTS idx_captures_status ON captures(status);
CREATE INDEX IF NOT EXISTS idx_captures_created ON captures(created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts USING fts5(
    id UNINDEXED,
    content,
    content=captures,
    content_rowid=rowid
);

-- FTS triggers for captures sync
CREATE TRIGGER IF NOT EXISTS captures_ai AFTER INSERT ON captures BEGIN
    INSERT INTO captures_fts(rowid, id, content) VALUES (new.rowid, new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS captures_ad AFTER DELETE ON captures BEGIN
    INSERT INTO captures_fts(captures_fts, rowid, id, content) VALUES('delete', old.rowid, old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS captures_au AFTER UPDATE ON captures BEGIN
    INSERT INTO captures_fts(captures_fts, rowid, id, content) VALUES('delete', old.rowid, old.id, old.content);
    INSERT INTO captures_fts(rowid, id, content) VALUES (new.rowid, new.id, new.content);
END;

-- ============================================================
-- ORGS — bootstrap identity row for a per-org DB (graph://d970d946-f95)
-- ============================================================
-- Exactly one row per per-org DB; that row is the org's own identity
-- record. Queried at every connection open to know "which org is this DB?"
-- Rich identity (display name, color, favicon, etc.) lives as an
-- `autonomy.org#1` Setting in the same DB; this table is the thin,
-- schema-stable bootstrap layer.
CREATE TABLE IF NOT EXISTS orgs (
    id          TEXT PRIMARY KEY,        -- UUID v7, minted at DB create
    slug        TEXT NOT NULL,           -- matches data/orgs/<slug>.db filename
    type        TEXT NOT NULL CHECK (type IN ('shared','personal')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================
-- SETTINGS — layered configuration primitive (graph://0d3f750f-f9c)
-- ============================================================
-- A Setting is a structured, schema-validated, machine-consumable value.
-- Keyed by (set_id, key) within an org; (set_id, schema_revision) declares
-- the contract version. Cross-org reads honour publication_state via the
-- same filter rule as Notes (graph://bcce359d-a1d).
CREATE TABLE IF NOT EXISTS settings (
    id                TEXT PRIMARY KEY,
    set_id            TEXT NOT NULL,
    schema_revision   INTEGER NOT NULL,
    key               TEXT NOT NULL,
    payload           TEXT NOT NULL,            -- JSON conforming to (set_id, schema_revision)
    publication_state TEXT NOT NULL DEFAULT 'raw'
        CHECK (publication_state IN ('raw','curated','published','canonical')),
    supersedes        TEXT,                      -- target Setting id (per-field override)
    excludes          TEXT,                      -- target Setting id (drop)
    deprecated        INTEGER NOT NULL DEFAULT 0 CHECK (deprecated IN (0,1)),
    successor_id      TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at        TEXT,                         -- @cache(ttl=...) absolute TTL stamp; NULL = never expires
    -- Signed-settings envelope (graph://21a0da9e-1c2). All NULL on every row
    -- of a store that does not sign: personal.db, the machine store, and an
    -- org DB whose ledger is not founded. The signature covers the addressed
    -- record rebuilt from this row plus the owning org's genesis id
    -- (settingskit.record_from_row), so editing any signed column in place
    -- makes verification fail.
    signed_at         INTEGER,                      -- signer-asserted, unix ms; witness-bounded at the boundary
    signing_key       TEXT,                         -- hex Ed25519 key the signature verifies against
    signature         TEXT,                         -- hex Ed25519 over the domain-separated canonical record
    witness           TEXT,                         -- cited witness attestation, JSON; NULL = org never published
    terminal_persona  TEXT,                         -- persona boundary verification resolved the signer to
    -- A row is unsigned (all five NULL) or signed (the four non-witness
    -- columns all present; witness stays free, because a signed row of an
    -- org that has never published legitimately cites nothing). The 29
    -- partial states in between are storage corruption, refused here for
    -- fresh tables and by the equivalent triggers in _migrate_settings for
    -- tables that predate this constraint (SQLite cannot ADD CHECK).
    CHECK (
        (signed_at IS NULL AND signing_key IS NULL AND signature IS NULL
         AND witness IS NULL AND terminal_persona IS NULL)
        OR (signed_at IS NOT NULL AND signing_key IS NOT NULL
            AND signature IS NOT NULL AND terminal_persona IS NOT NULL)
    )
);
-- Indices created via _migrate_settings so legacy DBs survive executescript.

-- ============================================================
-- VAULT CONTENT + KEY CONTROL — scoped durable encrypted state
-- ============================================================
-- These tables live in the database whose scope owns the encrypted content.
-- Fleet sync carries their durable records; local progress/caches are rebuilt
-- or retained only on the machine that owns them.
CREATE TABLE IF NOT EXISTS policy_classes (
    class_id TEXT PRIMARY KEY,
    wire     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vault_factors (
    factor_id   TEXT PRIMARY KEY,
    factor_type TEXT NOT NULL,
    public_key  TEXT NOT NULL,
    armor       TEXT
);

CREATE TABLE IF NOT EXISTS root_anchors (
    anchor_id TEXT PRIMARY KEY,
    wire      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vault_secrets (
    setting_name TEXT PRIMARY KEY,
    wire         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vault_content_bodies (
    ciphertext_hash TEXT PRIMARY KEY,
    size_bytes      INTEGER NOT NULL,
    body            BLOB NOT NULL,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS vault_content_objects (
    object_id        TEXT NOT NULL,
    revision_id      TEXT NOT NULL,
    genesis_id       TEXT NOT NULL,
    domain_id        TEXT NOT NULL,
    storage_state_id TEXT NOT NULL,
    ciphertext_hash  TEXT NOT NULL,
    header_json      BLOB NOT NULL,
    created_at       INTEGER NOT NULL,
    PRIMARY KEY (object_id, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_vault_content_objects_hash
    ON vault_content_objects(ciphertext_hash);
CREATE INDEX IF NOT EXISTS idx_vault_content_objects_state
    ON vault_content_objects(storage_state_id);

-- Derived locally from vault_content_objects after materialization.
CREATE TABLE IF NOT EXISTS vault_state_object_counts (
    storage_state_id TEXT PRIMARY KEY,
    object_count     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS keycontrol_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keycontrol_state (
    state_id TEXT PRIMARY KEY,
    wire     BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS keycontrol_grant (
    grant_id             TEXT PRIMARY KEY,
    storage_state_id     TEXT NOT NULL,
    recipient_kem_key_id TEXT NOT NULL,
    wire                 BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS keycontrol_grant_state
    ON keycontrol_grant (storage_state_id);
CREATE TABLE IF NOT EXISTS keycontrol_credential (
    kem_key_id TEXT PRIMARY KEY,
    persona    TEXT NOT NULL,
    wire       BLOB
);
CREATE INDEX IF NOT EXISTS keycontrol_credential_persona
    ON keycontrol_credential (persona);
CREATE TABLE IF NOT EXISTS keycontrol_bridge (
    bridge_id       TEXT PRIMARY KEY,
    child_state_id  TEXT NOT NULL,
    parent_state_id TEXT NOT NULL,
    wire            BLOB
);
CREATE INDEX IF NOT EXISTS keycontrol_bridge_edge
    ON keycontrol_bridge (child_state_id, parent_state_id);
CREATE TABLE IF NOT EXISTS keycontrol_pending (
    record_type            TEXT NOT NULL,
    claimed_id             TEXT NOT NULL,
    unmet_dependency_kind  TEXT NOT NULL,
    unmet_dependency_id    TEXT NOT NULL,
    first_held_at_ms       INTEGER NOT NULL,
    first_delivery_peer_id TEXT,
    wire                   BLOB NOT NULL,
    wire_len               INTEGER NOT NULL,
    PRIMARY KEY (record_type, claimed_id)
);
CREATE INDEX IF NOT EXISTS keycontrol_pending_dependency
    ON keycontrol_pending (unmet_dependency_kind, unmet_dependency_id);
CREATE TABLE IF NOT EXISTS keycontrol_pending_usage (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    pending_rows  INTEGER NOT NULL,
    pending_bytes INTEGER NOT NULL
);

-- >>> DERIVED SCHEMA OBJECTS <<<
-- ============================================================
-- INDEXES, FTS AND TRIGGERS THAT ONCE EXISTED ONLY IN MIGRATIONS
-- ============================================================
-- EXECUTED AFTER the migrations, never before: every statement below
-- depends on a column that a migration adds to a LEGACY store (sources
-- .publication_state / .short_description / .keywords, settings
-- .terminal_persona and the envelope columns). Running them first raises
-- 'no such column' and aborts the whole open. On a FRESH database the base
-- above already creates those columns, so the split is invisible there and
-- both paths finish in the same shape -- which test_schema_is_complete
-- asserts.
-- These were created by _migrate_* methods and NOT by this file, so a
-- database built from schema.sql alone was missing 21 objects and two
-- columns -- including sources_fts and the uniqueness indexes the
-- (source_id, message_id) dedup depends on. A new install was correct only
-- because it also ran fourteen historical migrations.
--
-- This file is now the complete current shape, which is what makes a fresh
-- install structurally current instead of accidentally current.
-- test_schema_is_complete.py fails if a migration ever adds an object this
-- file does not.

CREATE INDEX IF NOT EXISTS idx_sources_last_activity ON sources(last_activity_at);
CREATE INDEX IF NOT EXISTS idx_sources_publication_state ON sources(publication_state);
CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(type);

-- Defence in depth against a re-ingest writing a turn twice; partial so
-- rows with no message_id (rare, legitimate) are unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS idx_thoughts_source_message_unique
    ON thoughts(source_id, message_id) WHERE message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_derivations_source_message_unique
    ON derivations(source_id, message_id) WHERE message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_settings_set ON settings(set_id, key);
CREATE INDEX IF NOT EXISTS idx_settings_schema ON settings(set_id, schema_revision);
CREATE INDEX IF NOT EXISTS idx_settings_state ON settings(publication_state);
CREATE INDEX IF NOT EXISTS idx_settings_expires_at ON settings(expires_at)
    WHERE expires_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_settings_one_base
    ON settings(set_id, schema_revision, key, publication_state)
    WHERE supersedes IS NULL AND excludes IS NULL AND deprecated = 0
      AND terminal_persona IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_settings_one_slot
    ON settings(set_id, schema_revision, key, publication_state, terminal_persona)
    WHERE supersedes IS NULL AND excludes IS NULL AND deprecated = 0
      AND terminal_persona IS NOT NULL;

-- A settings envelope is all-NULL (unsigned) or complete (witness optional).
CREATE TRIGGER IF NOT EXISTS trg_settings_envelope_insert
BEFORE INSERT ON settings WHEN NOT (
    (NEW.signed_at IS NULL AND NEW.signing_key IS NULL AND NEW.signature IS NULL
     AND NEW.witness IS NULL AND NEW.terminal_persona IS NULL)
 OR (NEW.signed_at IS NOT NULL AND NEW.signing_key IS NOT NULL
     AND NEW.signature IS NOT NULL AND NEW.terminal_persona IS NOT NULL))
BEGIN SELECT RAISE(ABORT, 'settings envelope columns must be all NULL (unsigned) or complete (witness optional)'); END;

CREATE TRIGGER IF NOT EXISTS trg_settings_envelope_update
BEFORE UPDATE ON settings WHEN NOT (
    (NEW.signed_at IS NULL AND NEW.signing_key IS NULL AND NEW.signature IS NULL
     AND NEW.witness IS NULL AND NEW.terminal_persona IS NULL)
 OR (NEW.signed_at IS NOT NULL AND NEW.signing_key IS NOT NULL
     AND NEW.signature IS NOT NULL AND NEW.terminal_persona IS NOT NULL))
BEGIN SELECT RAISE(ABORT, 'settings envelope columns must be all NULL (unsigned) or complete (witness optional)'); END;

CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5(
    id UNINDEXED, title, short_description, keywords,
    content='sources', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS sources_ai AFTER INSERT ON sources BEGIN
    INSERT INTO sources_fts(rowid, id, title, short_description, keywords)
    VALUES (new.rowid, new.id, new.title, new.short_description, new.keywords);
END;
CREATE TRIGGER IF NOT EXISTS sources_ad AFTER DELETE ON sources BEGIN
    INSERT INTO sources_fts(sources_fts, rowid, id, title, short_description, keywords)
    VALUES ('delete', old.rowid, old.id, old.title, old.short_description, old.keywords);
END;
CREATE TRIGGER IF NOT EXISTS sources_au AFTER UPDATE ON sources BEGIN
    INSERT INTO sources_fts(sources_fts, rowid, id, title, short_description, keywords)
    VALUES ('delete', old.rowid, old.id, old.title, old.short_description, old.keywords);
    INSERT INTO sources_fts(rowid, id, title, short_description, keywords)
    VALUES (new.rowid, new.id, new.title, new.short_description, new.keywords);
END;
