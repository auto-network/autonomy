-- Autonomy Knowledge Graph Schema
-- SQLite with FTS5 for full-text search

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- SOURCES — origin records (conversations, files, URLs)
-- ============================================================
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,          -- 'conversation', 'musing', 'document', 'url', 'session'
    platform    TEXT,                   -- 'chatgpt', 'claude', 'claude-code', 'local', etc.
    project     TEXT,                   -- project identifier (e.g. '-home-jeremy-workspace-autonomy')
    title       TEXT,
    url         TEXT,
    file_path   TEXT UNIQUE,            -- local file path (for dedup on re-ingest)
    metadata    TEXT DEFAULT '{}',      -- JSON blob for extra fields
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================
-- THOUGHTS — user assertions, questions, intents (sovereign)
-- ============================================================
CREATE TABLE IF NOT EXISTS thoughts (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'user',   -- 'user' for sovereign thoughts
    turn_number INTEGER,                        -- position in conversation
    message_id  TEXT,                            -- platform message ID if available
    tags        TEXT DEFAULT '[]',              -- JSON array of topic tags
    metadata    TEXT DEFAULT '{}',              -- JSON blob
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_thoughts_source ON thoughts(source_id);

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
CREATE INDEX IF NOT EXISTS idx_derivations_thought ON derivations(thought_id);

-- ============================================================
-- ENTITIES — named concepts (Autonomy Core, CRDT, sovereignty line, etc.)
-- ============================================================
CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    canonical_name  TEXT NOT NULL UNIQUE,   -- lowercased, for dedup
    type            TEXT DEFAULT 'concept', -- 'concept', 'person', 'technology', 'project', 'organization'
    description     TEXT,
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

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
-- ENTITY MENTIONS — junction: which entities appear in which content
-- ============================================================
CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id   TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    content_id  TEXT NOT NULL,           -- thought or derivation ID
    content_type TEXT NOT NULL,          -- 'thought' or 'derivation'
    count       INTEGER DEFAULT 1,
    PRIMARY KEY (entity_id, content_id)
);
CREATE INDEX IF NOT EXISTS idx_mentions_content ON entity_mentions(content_id);

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
CREATE TABLE IF NOT EXISTS note_comments (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    actor       TEXT DEFAULT 'user',
    integrated  INTEGER DEFAULT 0,    -- 0=active, 1=integrated (content rolled into note body)
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
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
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
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
