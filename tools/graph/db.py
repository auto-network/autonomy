"""Database operations for the Autonomy Knowledge Graph."""

from __future__ import annotations
import json
import math
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tools.data_paths import (
    RealDataFallbackRefused,
    refuse_real_data_fallback_enabled,
    resolve_orgs_root,
)

from .models import Source, Thought, Derivation, Entity, Claim, Edge, Node, Attachment, new_id

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
REPO_ROOT = Path(__file__).resolve().parents[2]

# Schema-init perf guard (2026-07-19). `_init_schema` re-runs the whole schema
# script + every migration + tag seeding on EVERY writable open — all writes
# that take the SQLite write lock, and under concurrency they serialize and can
# stall the caller (notably the dashboard event loop) for many seconds. We skip
# that re-init when the DB's `PRAGMA user_version` already equals this constant.
# It is set MANUALLY and is deliberately dumb:
#   >>> If you change schema.sql, add/alter/remove any `_migrate_*` method, or
#   >>> change `_seed_tags`, you MUST bump this number, or existing databases
#   >>> will NOT pick up your change.
#
# NOTE (auto-06ziz): Setting *schema* materialization — the `autonomy.schema#1`
# and `autonomy.schema.synopsis#1` meta rows read by
# `graph set schema/example/find` — is deliberately NOT gated by this constant.
# Adding a Setting schema, editing one, or editing only its SYNOPSIS does NOT
# require a bump. Those meta rows are (re-)flushed once at dashboard startup by
# `schemas.registry.flush_schema_meta_all_orgs`, which the hot-reload runs on
# every code change. Coupling that flush to this version — whose change frequency
# and cost are unrelated — was the bug fixed here: a schema change forced an
# expensive full re-init, or (without a bump) never landed at all.
# (A future enhancement can auto-derive this from the schema; for now the whole
# point is to prove the perf win with the smallest possible change.)
_SCHEMA_USER_VERSION = 3
DEFAULT_DB = REPO_ROOT / "data" / "graph.db"
DEFAULT_ORGS_DIR = REPO_ROOT / "data" / "orgs"

# Keep SQLite's existing default lock wait explicit so contention tests can
# shorten it without changing production behavior.  An rw open gets three
# additional attempts after the first failure.
_SQLITE_CONNECT_TIMEOUT_S = 5.0
_RW_OPEN_BACKOFF_S = (0.05, 0.1, 0.2)

VALID_ORG_TYPES = ("shared", "personal")


class GraphDBNotReady(RuntimeError):
    """The database exists but cannot serve schema-backed reads yet."""


def _orgs_dir(root: Path | str | None = None) -> Path:
    """Resolve ``data/orgs/`` location, respecting ``AUTONOMY_ORGS_DIR`` env."""
    return resolve_orgs_root(root, default=DEFAULT_ORGS_DIR)


def _org_db_path(slug: str, root: Path | str | None = None) -> Path:
    """Return ``<orgs_dir>/<slug>.db``."""
    return _orgs_dir(root) / f"{slug}.db"


def _uuid7() -> str:
    """Generate a time-ordered UUID v7 in canonical 8-4-4-4-12 hex form.

    Mirrors :func:`org_ops.uuid7`; duplicated here to avoid circular imports
    between db and org_ops.
    """
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand = secrets.token_bytes(10)
    b = bytearray(16)
    b[0] = (ts_ms >> 40) & 0xFF
    b[1] = (ts_ms >> 32) & 0xFF
    b[2] = (ts_ms >> 24) & 0xFF
    b[3] = (ts_ms >> 16) & 0xFF
    b[4] = (ts_ms >> 8) & 0xFF
    b[5] = ts_ms & 0xFF
    b[6] = 0x70 | (rand[0] & 0x0F)
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)
    b[9] = rand[3]
    b[10:16] = rand[4:10]
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_caller_db_path(
    org: str | None = None,
    *,
    root: Path | str | None = None,
) -> Path:
    """Resolve the DB path for a given ``org``.

    Priority:
      1. ``GRAPH_DB`` env (test override / explicit pin) → that path.
      2. ``data/orgs/<org>.db`` — ``org`` defaults to
         ``'personal'`` (scopeless convergence, auto-txg5.3): every write
         without an explicit org lands in the operator's personal DB.
      3. Legacy ``data/graph.db`` (``DEFAULT_DB``) when the per-org DB
         file is absent — preserves pre-migration fallback so existing
         installations keep reading their legacy store until bootstrap
         has materialised the per-org files.
    """
    env_db = os.environ.get("GRAPH_DB")
    if env_db:
        return Path(env_db)
    slug = org or "personal"
    org_path = _org_db_path(slug, root)
    if org_path.exists():
        return org_path
    if refuse_real_data_fallback_enabled():
        raise RealDataFallbackRefused(
            f"refusing legacy graph DB fallback for missing org {slug!r}: "
            f"initialize {org_path} or set GRAPH_DB"
        )
    # Pre-migration fallback: legacy single DB when no per-org file yet.
    return DEFAULT_DB


# ── Per-org connection pool ────────────────────────────────────
# Module-level because the pool is process-lifetime; tests should call
# ``GraphDB.close_all_pooled()`` in teardown.

_CONNECTION_POOL: dict[tuple[str, str], "GraphDB"] = {}


# Search-ranking tunables. ``rank`` is BM25-derived (lower = better match);
# subtracting boosts pushes a row toward the top of the result set. A title
# hit on a noisy term should outrank the best body match on the same term,
# but the body floor (rank ≈ −10) shouldn't be drowned by a marginal title
# hit. See bead auto-kvka6 for the calibration discussion.
SEARCH_TITLE_BOOST = -50          # rank delta applied to sources_fts matches
SEARCH_TAG_OVERLAP_BOOST = -5     # rank delta per matching tag
SEARCH_TAG_OVERLAP_CAP = -25      # cumulative tag-overlap boost cap
# Round 7l: source-aware search. Multi-hit sources rank higher (more hits =
# stronger signal), but we don't let one source's hits drown out other
# sources' visibility. The bonus is a small log-shaped delta that nudges
# multi-hit sources up the ranking without overwhelming the title boost.
#   1 hit  → 0
#   2 hits → -1.0
#   8 hits → -3.0
#   30 hits → -4.95
SEARCH_HIT_COUNT_BONUS_FACTOR = -1.0
# Per-source excerpt cap returned to consumers in phase 2 of source-aware
# search. Cards on /search render a few excerpts max, and we don't want a
# 50-turn session to balloon the API payload.
SEARCH_EXCERPTS_PER_SOURCE = 10
# Phase 1 ceiling — how many candidate (source_id, rank) hits we pull
# from each FTS table before grouping. Generous so a single source's hits
# can't crowd out other distinct sources at the candidate-collection step.
SEARCH_PHASE1_FANOUT = 200

# Opt-in Round 8 ranker. It fuses the legacy whole-query ranking with one
# legacy ranking per distinct query term. This rewards sources that agree
# across query formulations without giving verbose metadata/thought/derivation
# storage channels independent votes.
SEARCH_VALID_RANKERS = ("legacy", "smart")
SEARCH_SMART_RRF_K = 60
SEARCH_SMART_STREAM_FANOUT = 100
SEARCH_SMART_MAX_TERM_STREAMS = 6


import re as _re

_SOURCE_ID_RE = _re.compile(r'^[0-9a-f]{6,}(?:-[0-9a-f]+)*$', _re.IGNORECASE)


def _is_source_id(query: str) -> bool:
    """Return True if *query* looks like a source ID (hex prefix, 6+ chars)."""
    return bool(_SOURCE_ID_RE.match(query.strip()))


def _sanitize_fts_query(query: str, or_mode: bool = False) -> str:
    """Sanitize a user query for safe use in FTS5 MATCH expressions.

    - Preserves existing double-quoted phrases as-is.
    - Strips FTS5 operator characters (-, :, (, ), *, ^, ~) from bare words.
    - Wraps each bare word in double quotes to prevent operator interpretation.
    - Joins terms with OR when or_mode=True, otherwise implicit AND (space-separated).

    Examples:
        'one-time architect'  -> '"one" "time" "architect"'
        '"exact phrase" other' -> '"exact phrase" "other"'
    """
    import re
    tokens = []
    # Pull out already-quoted phrases first, then split remaining bare words
    parts = re.split(r'("(?:[^"\\]|\\.)*")', query)
    for part in parts:
        if part.startswith('"') and part.endswith('"') and len(part) >= 2:
            # Already a quoted phrase — keep as-is
            tokens.append(part)
        else:
            # Strip FTS5 operator chars from bare text, then split into words
            cleaned = re.sub(r'[-:()*^~]', ' ', part)
            for word in cleaned.split():
                if word:
                    tokens.append(f'"{word}"')
    if not tokens:
        return '""'
    joiner = " OR " if or_mode else " "
    return joiner.join(tokens)


def _search_tokens(value: str) -> set[str]:
    """Tokenize text closely enough to FTS5 ``unicode61`` for soft signals.

    Tag names commonly use punctuation as a separator (``cross-org``,
    ``publication-state``), while callers naturally type spaces.  The old
    overlap check compared whole strings and therefore missed both tokens.
    Splitting on non-word characters (and underscore, which unicode61 also
    treats as a separator) makes tag boosts agree with lexical retrieval.
    """
    return {
        token.casefold()
        for token in _re.findall(r"[^\W_]+", value, flags=_re.UNICODE)
        if len(token) > 2
    }


class GraphDB:
    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        mode: Literal["rw", "ro"] = "rw",
        org: str | None = None,
    ):
        if db_path is None:
            db_path = resolve_caller_db_path(org)
        self.db_path = Path(db_path)
        self.read_only = False
        self._immutable = False
        self._pooled = False  # set to True by for_org when cached
        if mode == "ro":
            self._open_ro()
            return

        last_error: sqlite3.OperationalError | OSError | None = None
        for delay_s in (0.0, *_RW_OPEN_BACKOFF_S):
            if delay_s:
                time.sleep(delay_s)
            try:
                self._open_rw_once()
                return
            except (sqlite3.OperationalError, OSError) as exc:
                self._discard_failed_connection()
                last_error = exc

        assert last_error is not None
        if self._rw_path_is_read_only(last_error):
            self._open_ro()
            return
        raise last_error

    def _open_rw_once(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.db_path),
            timeout=_SQLITE_CONNECT_TIMEOUT_S,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _discard_failed_connection(self) -> None:
        conn = getattr(self, "conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:
            pass
        del self.conn

    def _rw_path_is_read_only(
        self,
        error: sqlite3.OperationalError | OSError,
    ) -> bool:
        """True only when falling back from rw to ro is justified."""
        parent_writable = os.access(self.db_path.parent, os.W_OK)
        file_writable = (
            not self.db_path.exists()
            or os.access(self.db_path, os.W_OK)
        )
        message = str(error).lower()
        sqlite_reported_read_only = (
            self.db_path.exists()
            and ("readonly" in message or "read-only" in message)
        )
        return (
            not parent_writable
            or not file_writable
            or sqlite_reported_read_only
        )

    def _open_ro(self):
        """Open the DB read-only. Used when the filesystem is ro-mounted
        or when ``mode='ro'`` is requested explicitly.

        ``check_same_thread=False`` allows the process-lifetime connection
        pool (``GraphDB.for_org``) to share ro connections across Starlette
        threadpool workers. SQLite's serialized threading mode + GIL +
        single-query-per-call (no cursor held across awaits, no
        transactions on ro connections) make this safe. Hot-patched
        2026-04-21 after a dashboard 500-error regression; formalize in
        follow-up bead."""
        try:
            self.conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=_SQLITE_CONNECT_TIMEOUT_S,
            )
            self.conn.row_factory = sqlite3.Row
        except (sqlite3.OperationalError, OSError):
            self.conn = sqlite3.connect(
                f"file:{self.db_path}?immutable=1",
                uri=True,
                check_same_thread=False,
                timeout=_SQLITE_CONNECT_TIMEOUT_S,
            )
            self.conn.row_factory = sqlite3.Row
            self._immutable = True
        user_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        has_settings = self.conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
        if user_version == 0 and has_settings is None:
            self.conn.close()
            raise GraphDBNotReady(
                f"database schema is not initialized yet: {self.db_path}"
            )
        self.read_only = True

    def _init_schema(self):
        # Perf guard: skip the whole re-init (all writes) when this DB is already
        # at the expected schema version. See _SCHEMA_USER_VERSION above — bump
        # that constant whenever you change the schema/migrations/seed below.
        if self.conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_USER_VERSION:
            return
        schema = SCHEMA_PATH.read_text()
        self.conn.executescript(schema)
        self._migrate_attachments_alt_text()
        self._migrate_sources_last_activity()
        self._migrate_publication_state()
        self._migrate_source_moves()
        self._migrate_source_short_description()
        self._migrate_source_keywords()
        self._migrate_drop_source_project()
        # sources_fts depends on `short_description` + `keywords` columns, so
        # it must run AFTER both column migrations above.
        self._migrate_sources_fts()
        self._migrate_sources_type_index()
        self._migrate_settings()
        self._migrate_orgs()
        self._migrate_message_id_unique()
        self._seed_tags()
        # NOTE (auto-06ziz): schema-meta Settings are intentionally NOT flushed
        # here. Materializing them is decoupled from _SCHEMA_USER_VERSION and now
        # runs once at dashboard startup via
        # ``schemas.registry.flush_schema_meta_all_orgs``. See the constant's
        # comment above.
        # Stamp the version so subsequent opens hit the fast-path guard above.
        self.conn.execute(f"PRAGMA user_version = {_SCHEMA_USER_VERSION}")

    def _migrate_attachments_alt_text(self):
        """Add alt_text column to attachments table if missing (idempotent)."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(attachments)").fetchall()}
        if "alt_text" not in cols:
            self.conn.execute("ALTER TABLE attachments ADD COLUMN alt_text TEXT")
            self.conn.commit()

    def _migrate_publication_state(self):
        """Add publication_state / deprecated / successor_id (idempotent).

        Lands the scope+facet primitive on sources, plus pinned-to-'raw' columns
        on thoughts, note_comments, and captures. See graph://8cf067e3-ca3.
        """
        # sources: three fields
        scols = {r[1] for r in self.conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "publication_state" not in scols:
            self.conn.execute(
                "ALTER TABLE sources ADD COLUMN publication_state TEXT NOT NULL DEFAULT 'raw' "
                "CHECK (publication_state IN ('raw','curated','published','canonical'))"
            )
            self.conn.execute(
                "UPDATE sources SET publication_state = 'curated' "
                "WHERE publication_state = 'raw'"
            )
        if "deprecated" not in scols:
            self.conn.execute(
                "ALTER TABLE sources ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0 "
                "CHECK (deprecated IN (0,1))"
            )
        if "successor_id" not in scols:
            self.conn.execute("ALTER TABLE sources ADD COLUMN successor_id TEXT")

        # thoughts / note_comments / captures: pinned to 'raw' via CHECK
        for table in ("thoughts", "note_comments", "captures"):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "publication_state" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN publication_state TEXT NOT NULL DEFAULT 'raw' "
                    "CHECK (publication_state = 'raw')"
                )

        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sources_publication_state "
            "ON sources(publication_state)"
        )
        self.conn.commit()

    def _migrate_source_moves(self):
        """Add ``moved_to_org`` to sources (idempotent)."""
        scols = {r[1] for r in self.conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "moved_to_org" not in scols:
            self.conn.execute("ALTER TABLE sources ADD COLUMN moved_to_org TEXT")
            self.conn.commit()

    def _migrate_source_short_description(self):
        """Add ``short_description`` column to sources (idempotent).

        First-class column (not metadata JSON) so SQL can sort/filter and
        the API has a stable shape for card previews / hover tooltips /
        search summaries. NULL by default; populated by the writer or by
        the forthcoming "Update Title & Summary" Haiku action.
        """
        scols = {r[1] for r in self.conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "short_description" not in scols:
            self.conn.execute("ALTER TABLE sources ADD COLUMN short_description TEXT")
            self.conn.commit()

    def _migrate_source_keywords(self):
        """Add ``keywords`` column to sources (idempotent).

        Free-form synonym/alias list — not navigational tags. Stored as a
        comma-separated string so the FTS5 unicode61 tokenizer indexes
        each token independently. Populated by the ``note.update-summary``
        Haiku action; NULL on rows it hasn't processed.
        """
        scols = {r[1] for r in self.conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "keywords" not in scols:
            self.conn.execute("ALTER TABLE sources ADD COLUMN keywords TEXT")
            self.conn.commit()

    def _migrate_drop_source_project(self):
        """Drop the legacy ``project`` column from sources (idempotent).

        Org scoping is which database a row lives in (auto-p6vn7) — the
        ``project`` field duplicated that as a second, driftable value and
        is no longer written or read. No index, trigger, or FTS table
        references it (FTS indexes title/short_description/keywords),
        so the drop is safe on its own.
        """
        scols = {r[1] for r in self.conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "project" in scols:
            self.conn.execute("ALTER TABLE sources DROP COLUMN project")
            self.conn.commit()

    def _migrate_sources_fts(self):
        """Create the sources_fts FTS5 table + sync triggers (idempotent).

        Indexes ``title``, ``short_description``, and ``keywords`` so curated
        per-source metadata can outrank long-tail body matches in
        :meth:`search`. The triggers mirror the thoughts_ai/ad/au shape from
        schema.sql exactly — same rowid-keyed inserts, same magic 'delete'
        row on remove/replace.

        On first creation we run an ``INSERT('rebuild')`` to backfill
        existing source rows; subsequent runs detect the table already
        exists and skip the rebuild. Must run AFTER
        :meth:`_migrate_source_keywords` so the column exists when the
        rebuild reads it.
        """
        table_existed = self._has_table('sources_fts')
        self.conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5(
                id UNINDEXED,
                title,
                short_description,
                keywords,
                content='sources',
                content_rowid='rowid',
                tokenize='unicode61'
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
        """)
        if not table_existed:
            self.conn.executescript(
                "INSERT INTO sources_fts(sources_fts) VALUES('rebuild');"
            )
        self.conn.commit()

    def _migrate_sources_type_index(self):
        """Index sources.type so type-based filters (notes, agentic runs, etc.) stay cheap."""
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(type)"
        )
        self.conn.commit()

    def _migrate_settings(self):
        """Create the settings table + indices if missing (idempotent).

        See graph://0d3f750f-f9c (Setting Primitive). Additive migration —
        existing data unaffected. Index creation lives here so legacy DBs
        that never had `settings` survive the executescript pass.
        """
        # CREATE TABLE itself runs via schema.sql executescript; this guard
        # exists for the rare case where executescript skipped (edge cases).
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            " id TEXT PRIMARY KEY,"
            " set_id TEXT NOT NULL,"
            " schema_revision INTEGER NOT NULL,"
            " key TEXT NOT NULL,"
            " payload TEXT NOT NULL,"
            " publication_state TEXT NOT NULL DEFAULT 'raw'"
            "   CHECK (publication_state IN ('raw','curated','published','canonical')),"
            " supersedes TEXT,"
            " excludes TEXT,"
            " deprecated INTEGER NOT NULL DEFAULT 0 CHECK (deprecated IN (0,1)),"
            " successor_id TEXT,"
            " created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),"
            " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
            ")"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_settings_set "
            "ON settings(set_id, key)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_settings_state "
            "ON settings(publication_state)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_settings_schema "
            "ON settings(set_id, schema_revision)"
        )
        # One live base row per composite key. Partial, so it constrains ONLY
        # base rows: overrides (supersedes) and exclusions (excludes) are not
        # in the index and stay unlimited, and deprecating a base frees the
        # slot. publication_state is included because the resolver deliberately
        # allows e.g. a raw draft beside a canonical row and picks by
        # precedence.
        #
        # Without this, `add_setting` could append a second base for a key that
        # already had one and the resolver would silently pick the newer,
        # leaving the older shadowing it — which is how one workspace primer
        # reached seven live bases and how overrides aimed at the "wrong" base
        # went dead. The API now upserts, so this is the backstop that keeps it
        # true for any future caller.
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_settings_one_base "
            "ON settings(set_id, schema_revision, key, publication_state) "
            "WHERE supersedes IS NULL AND excludes IS NULL AND deprecated = 0"
        )
        # Cache GC (auto-5ch66): cache-tagged schemas stamp an absolute
        # ``expires_at`` on every write. Non-cache rows leave it NULL
        # forever — the partial index keeps the index file tight on a
        # DB where 99% of rows are non-cache.
        cols = {
            r[1]
            for r in self.conn.execute("PRAGMA table_info(settings)").fetchall()
        }
        if "expires_at" not in cols:
            self.conn.execute(
                "ALTER TABLE settings ADD COLUMN expires_at TEXT"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_settings_expires_at "
            "ON settings(expires_at) WHERE expires_at IS NOT NULL"
        )
        self.conn.commit()

    def _migrate_orgs(self):
        """Create the bootstrap ``orgs`` table if missing (idempotent).

        See graph://d970d946-f95 (Org Registry & Identity). Each per-org
        DB carries this table with exactly one row identifying the org.
        Legacy single-DB world: table exists but stays empty until the
        per-org DB migration (auto-txg5.2) populates it.
        """
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS orgs ("
            " id TEXT PRIMARY KEY,"
            " slug TEXT NOT NULL,"
            " type TEXT NOT NULL CHECK (type IN ('shared','personal')),"
            " created_at TEXT NOT NULL DEFAULT "
            "  (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
            ")"
        )
        self.conn.commit()

    def _migrate_sources_last_activity(self):
        """Add last_activity_at column + index to sources table if missing (idempotent).

        Backfills from metadata.ended_at (or created_at) so existing rows sort
        sensibly until the next ingest refreshes them.
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "last_activity_at" not in cols:
            self.conn.execute("ALTER TABLE sources ADD COLUMN last_activity_at TEXT")
            self.conn.execute(
                "UPDATE sources SET last_activity_at = "
                "COALESCE(json_extract(metadata, '$.ended_at'), created_at) "
                "WHERE last_activity_at IS NULL"
            )
            self.conn.commit()
        # Create the index regardless — safe to call repeatedly thanks to IF NOT EXISTS.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sources_last_activity "
            "ON sources(last_activity_at)"
        )
        self.conn.commit()

    def _migrate_message_id_unique(self):
        """UNIQUE(source_id, message_id) partial index on thoughts and
        derivations (auto-4y579).

        ``_dedup_new_turns``'s within-batch/existing-row checks
        (tools/graph/ingest.py) are the primary defense against duplicate
        turns; this index is defense-in-depth at the storage layer for
        whatever a future caller gets wrong — e.g. a write path that
        doesn't route through ``_dedup_new_turns`` at all. Paired with
        ``INSERT OR IGNORE`` in :meth:`insert_thought` /
        :meth:`insert_derivation` so a losing writer degrades to a no-op,
        never a crash.

        ``CREATE UNIQUE INDEX`` fails outright against a table that
        already has a violating pair, so this repairs any existing
        violation FIRST — same logic as
        ``tools/graph/checks/dedupe_codex_message_ids.py``: within each
        ``(source_id, message_id)`` group, keep the lowest-``turn_number``
        row, delete the rest. Guarded by an index-existence check so the
        (only ever necessary once) full-table scan doesn't run on every
        connection open — once the index exists, future duplicates are
        rejected at insert time and there's nothing left to repair.
        """
        for table in ("thoughts", "derivations"):
            idx_name = f"idx_{table}_source_message_unique"
            exists = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                (idx_name,),
            ).fetchone()
            if exists:
                continue

            rows = self.conn.execute(
                f"SELECT id, source_id, message_id, turn_number FROM {table} "
                f"WHERE message_id IS NOT NULL"
            ).fetchall()
            by_key: dict[tuple, list] = {}
            for r in rows:
                by_key.setdefault((r["source_id"], r["message_id"]), []).append(r)
            for group in by_key.values():
                if len(group) <= 1:
                    continue
                group.sort(key=lambda r: r["turn_number"])
                for r in group[1:]:
                    self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (r["id"],))

            self.conn.execute(
                f"CREATE UNIQUE INDEX {idx_name} "
                f"ON {table}(source_id, message_id) WHERE message_id IS NOT NULL"
            )
        self.conn.commit()

    def close(self):
        """Close the underlying connection. Pool-managed instances are
        evicted from the pool first so the slot becomes available for
        the next ``for_org`` call.
        """
        if self._pooled:
            keys = [k for k, v in _CONNECTION_POOL.items() if v is self]
            for k in keys:
                _CONNECTION_POOL.pop(k, None)
            self._pooled = False
        try:
            self.conn.close()
        except sqlite3.ProgrammingError:
            pass  # already closed

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Per-org factory + pool ────────────────────────────────

    @classmethod
    def create_org_db(
        cls,
        slug: str,
        *,
        type_: str = "shared",
        path: Path | str | None = None,
        root: Path | str | None = None,
        org_id: str | None = None,
        created_at: str | None = None,
    ) -> "GraphDB":
        """Create a per-org DB at ``data/orgs/<slug>.db``.

        Applies the canonical schema (every table, index, FTS5 virtual
        table, plus the bootstrap ``orgs`` table, ``settings`` table and
        ``publication_state`` columns) and inserts the single identifying
        ``orgs`` row.

        Raises :class:`FileExistsError` when the DB file already exists
        (callers that want idempotency should check ``path.exists()`` or
        use :func:`org_ops.create_org` which layers the identity seed on
        top).
        """
        if type_ not in VALID_ORG_TYPES:
            raise ValueError(
                f"invalid org type {type_!r}; valid: {VALID_ORG_TYPES}"
            )
        resolved = _org_db_path(slug, root) if path is None else Path(path)
        if resolved.exists():
            raise FileExistsError(f"org DB already exists: {resolved}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        db = cls(resolved)  # runs full schema init on the empty file
        oid = org_id or _uuid7()
        created = created_at or _now_iso()
        db.conn.execute(
            "INSERT INTO orgs(id, slug, type, created_at) VALUES (?, ?, ?, ?)",
            (oid, slug, type_, created),
        )
        db.conn.commit()
        return db

    @classmethod
    def open_org_db(
        cls,
        slug: str,
        *,
        mode: Literal["rw", "ro"] = "rw",
        root: Path | str | None = None,
    ) -> "GraphDB":
        """Open an existing per-org DB.

        Raises :class:`FileNotFoundError` if ``data/orgs/<slug>.db`` is
        absent. Caller owns the returned connection's lifetime; for a
        pooled connection use :meth:`for_org`.
        """
        path = _org_db_path(slug, root)
        if not path.exists():
            raise FileNotFoundError(f"per-org DB not found: {path}")
        return cls(path, mode=mode)

    @classmethod
    def for_org(
        cls,
        slug: str,
        *,
        mode: Literal["rw", "ro"] = "rw",
        root: Path | str | None = None,
    ) -> "GraphDB":
        """Return a process-lifetime cached connection to a per-org DB.

        Pool key is ``(slug, mode)`` — ``'rw'`` and ``'ro'`` are distinct
        slots. Subsequent calls with the same key return the same
        :class:`GraphDB` instance (same underlying connection). Calling
        ``close()`` on the returned instance evicts the slot.
        """
        key = (slug, mode)
        cached = _CONNECTION_POOL.get(key)
        if cached is not None:
            return cached
        db = cls.open_org_db(slug, mode=mode, root=root)
        db._pooled = True
        _CONNECTION_POOL[key] = db
        return db

    @classmethod
    def close_all_pooled(cls) -> None:
        """Evict and close every pooled connection. Used by test teardown
        and dashboard shutdown."""
        for db in list(_CONNECTION_POOL.values()):
            db._pooled = False
            try:
                db.conn.close()
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                pass
        _CONNECTION_POOL.clear()

    @classmethod
    def pooled_slots(cls) -> list[tuple[str, str]]:
        """Return currently-cached ``(slug, mode)`` pool keys — test helper."""
        return list(_CONNECTION_POOL.keys())

    @property
    def is_immutable(self) -> bool:
        """True if opened with immutable=1 (no WAL visibility)."""
        return self.read_only and self._immutable

    # ── Sources ──────────────────────────────────────────────

    def insert_source(self, src: Source) -> Source:
        self.conn.execute(
            """INSERT INTO sources (id, type, platform, title, url, file_path, metadata,
                                    created_at, ingested_at, last_activity_at,
                                    publication_state, deprecated, successor_id, moved_to_org,
                                    short_description, keywords)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (src.id, src.type, src.platform, src.title, src.url,
             src.file_path, json.dumps(src.metadata), src.created_at, src.ingested_at,
             src.last_activity_at,
             src.publication_state, int(bool(src.deprecated)), src.successor_id,
             src.moved_to_org, src.short_description, src.keywords),
        )
        self.conn.commit()
        return src

    def update_source_title(self, source_id: str, title: str):
        """Update the title of a source. Last write wins."""
        self.conn.execute(
            "UPDATE sources SET title = ? WHERE id = ?", (title, source_id)
        )
        self.conn.commit()

    def update_source_short_description(self, source_id: str, short_description: str | None):
        """Update the short_description of a source. Last write wins."""
        self.conn.execute(
            "UPDATE sources SET short_description = ? WHERE id = ?",
            (short_description, source_id),
        )
        self.conn.commit()

    def update_source_keywords(self, source_id: str, keywords: str | None):
        """Update the keywords column on a source. Last write wins.

        ``keywords`` is a free-form comma-separated synonym/alias list
        indexed by ``sources_fts``. Pass ``None`` to clear.
        """
        self.conn.execute(
            "UPDATE sources SET keywords = ? WHERE id = ?",
            (keywords, source_id),
        )
        self.conn.commit()

    def update_source_metadata(self, source_id: str, metadata: dict):
        self.conn.execute(
            "UPDATE sources SET metadata = ?, ingested_at = ? WHERE id = ?",
            (json.dumps(metadata), self.conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now')").fetchone()[0], source_id),
        )
        self.conn.commit()

    def update_source_summary(self, source_id: str, *, title: str | None = None,
                              metadata: dict | None = None, last_activity_at: str | None = None):
        """Refresh title / metadata / last_activity_at on incremental re-ingest.

        Any field passed as None is left untouched. Bumps ingested_at.
        """
        sets = []
        vals: list = []
        if title is not None:
            sets.append("title = ?")
            vals.append(title)
        if metadata is not None:
            sets.append("metadata = ?")
            vals.append(json.dumps(metadata))
        if last_activity_at is not None:
            sets.append("last_activity_at = ?")
            vals.append(last_activity_at)
        if not sets:
            return
        sets.append("ingested_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
        vals.append(source_id)
        self.conn.execute(
            f"UPDATE sources SET {', '.join(sets)} WHERE id = ?", vals
        )
        self.conn.commit()

    def get_max_turn(self, source_id: str) -> int:
        """Get the highest turn number already ingested for a source."""
        row = self.conn.execute(
            """SELECT MAX(turn_number) as max_turn FROM (
                SELECT turn_number FROM thoughts WHERE source_id = ?
                UNION ALL
                SELECT turn_number FROM derivations WHERE source_id = ?
            )""",
            (source_id, source_id),
        ).fetchone()
        return row["max_turn"] or 0

    def get_source_by_path(self, file_path: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE file_path = ?", (file_path,)
        ).fetchone()
        return dict(row) if row else None

    def delete_source(self, source_id: str):
        self.conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        self.conn.commit()

    # ── Thoughts ─────────────────────────────────────────────

    def insert_thought(self, t: Thought) -> Thought:
        # OR IGNORE (auto-4y579): a losing writer against
        # UNIQUE(source_id, message_id) (message_id IS NOT NULL) degrades
        # to a no-op instead of raising IntegrityError — _dedup_new_turns
        # is the primary defense (within-batch + existing-row checks) so
        # this should only ever fire on a genuine race between two
        # concurrent writers that both passed dedup before either
        # committed.
        self.conn.execute(
            """INSERT OR IGNORE INTO thoughts (id, source_id, content, role, turn_number, message_id, tags, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (t.id, t.source_id, t.content, t.role, t.turn_number, t.message_id,
             json.dumps(t.tags), json.dumps(t.metadata), t.created_at),
        )
        return t

    def get_thoughts_by_source(self, source_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM thoughts WHERE source_id = ? ORDER BY turn_number", (source_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Derivations ──────────────────────────────────────────

    def insert_derivation(self, d: Derivation) -> Derivation:
        # OR IGNORE (auto-4y579): see insert_thought's comment — same
        # UNIQUE(source_id, message_id) defense-in-depth.
        self.conn.execute(
            """INSERT OR IGNORE INTO derivations (id, source_id, thought_id, content, model, turn_number, message_id, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d.id, d.source_id, d.thought_id, d.content, d.model, d.turn_number,
             d.message_id, json.dumps(d.metadata), d.created_at),
        )
        return d

    # ── Entities ─────────────────────────────────────────────

    def upsert_entity(self, name: str, entity_type: str = "concept", description: str | None = None) -> str:
        canonical = name.lower().strip()
        row = self.conn.execute(
            "SELECT id FROM entities WHERE canonical_name = ?", (canonical,)
        ).fetchone()
        if row:
            return row["id"]
        eid = new_id()
        self.conn.execute(
            """INSERT INTO entities (id, name, canonical_name, type, description)
               VALUES (?, ?, ?, ?, ?)""",
            (eid, name, canonical, entity_type, description),
        )
        return eid

    def get_entity(self, canonical_name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM entities WHERE canonical_name = ?", (canonical_name.lower().strip(),)
        ).fetchone()
        return dict(row) if row else None

    def list_entities(self, entity_type: str | None = None, limit: int = 100) -> list[dict]:
        if entity_type:
            rows = self.conn.execute(
                "SELECT * FROM entities WHERE type = ? ORDER BY name LIMIT ?",
                (entity_type, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM entities ORDER BY name LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Entity Mentions ──────────────────────────────────────

    def add_mention(self, entity_id: str, content_id: str, content_type: str, count: int = 1):
        self.conn.execute(
            """INSERT INTO entity_mentions (entity_id, content_id, content_type, count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(entity_id, content_id)
               DO UPDATE SET count = count + excluded.count""",
            (entity_id, content_id, content_type, count),
        )

    # ── Claims ───────────────────────────────────────────────

    def insert_claim(self, c: Claim) -> Claim:
        self.conn.execute(
            """INSERT INTO claims (id, subject_id, predicate, object_id, object_val, source_id,
                                   asserted_by, confidence, status, evidence, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (c.id, c.subject_id, c.predicate, c.object_id, c.object_val, c.source_id,
             c.asserted_by, c.confidence, c.status, c.evidence, json.dumps(c.metadata), c.created_at),
        )
        return c

    # ── Edges ────────────────────────────────────────────────

    def insert_edge(self, e: Edge) -> Edge:
        self.conn.execute(
            """INSERT OR IGNORE INTO edges (id, source_id, source_type, target_id, target_type, relation, weight, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (e.id, e.source_id, e.source_type, e.target_id, e.target_type,
             e.relation, e.weight, json.dumps(e.metadata), e.created_at),
        )
        return e

    # ── Collab / Tags ─────────────────────────────────────────

    def add_source_tag(self, source_id: str, tag: str) -> bool:
        """Append a tag to the source's metadata.tags array. Returns True if added, False if already present."""
        row = self.conn.execute("SELECT metadata FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not row:
            return False
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        tags = meta.get("tags", [])
        if tag in tags:
            return False
        tags.append(tag)
        meta["tags"] = tags
        self.update_source_metadata(source_id, meta)
        return True

    def remove_source_tag(self, source_id: str, tag: str) -> bool:
        """Remove a tag from the source's metadata.tags array. Returns True if removed, False if not present."""
        row = self.conn.execute("SELECT metadata FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not row:
            return False
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        tags = meta.get("tags", [])
        if tag not in tags:
            return False
        tags.remove(tag)
        meta["tags"] = tags
        self.update_source_metadata(source_id, meta)
        return True

    def sources_with_tag(self, tag: str) -> list[dict]:
        """Return all sources that have the given tag in metadata.tags."""
        rows = self.conn.execute(
            """SELECT * FROM sources
               WHERE json_extract(metadata, '$.tags') LIKE '%' || ? || '%'""",
            (tag,),
        ).fetchall()
        # Filter for exact tag match (the LIKE query is approximate)
        result = []
        for r in rows:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
            if tag in meta.get("tags", []):
                result.append(dict(r))
        return result

    def _ensure_note_reads_table(self):
        """Auto-migration: create note_reads table if missing. No-op on read-only DBs."""
        if self.read_only:
            return
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS note_reads (
                source_id TEXT NOT NULL,
                actor     TEXT NOT NULL,
                ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                PRIMARY KEY (source_id, actor, ts)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_note_reads_source ON note_reads(source_id)")

    def record_read(self, source_id: str, actor: str):
        """Record a read event for a collab note. Silently skips on read-only DBs."""
        if self.read_only:
            return
        try:
            self._ensure_note_reads_table()
            self.conn.execute(
                "INSERT OR IGNORE INTO note_reads (source_id, actor) VALUES (?, ?)",
                (source_id, actor),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def _has_table(self, name: str) -> bool:
        """Check if a table exists in the database."""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def list_collab_sources(self, limit: int = 50) -> list[dict]:
        """List sources tagged 'collab', ranked by activity (comments*3 + reads*1)."""
        self._ensure_note_reads_table()
        has_reads = self._has_table("note_reads")
        if has_reads:
            reads_join = "LEFT JOIN (SELECT source_id, COUNT(*) AS cnt FROM note_reads GROUP BY source_id) r ON r.source_id = s.id"
            reads_col = "COALESCE(r.cnt, 0)"
        else:
            reads_join = ""
            reads_col = "0"
        query = f"""
            SELECT s.*,
                   COALESCE(c.cnt, 0) AS comment_count,
                   {reads_col} AS read_count
            FROM sources s
            LEFT JOIN (SELECT source_id, COUNT(*) AS cnt FROM note_comments GROUP BY source_id) c
                ON c.source_id = s.id
            {reads_join}
            WHERE json_extract(s.metadata, '$.tags') LIKE '%"collab"%'
            ORDER BY ({reads_col} * 1 + COALESCE(c.cnt, 0) * 3) DESC, s.created_at DESC
            LIMIT ?
        """
        rows = self.conn.execute(query, (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── Nodes (hierarchy) ────────────────────────────────────

    def insert_node(self, n: Node) -> Node:
        self.conn.execute(
            """INSERT INTO nodes (id, parent_id, type, title, description, status, sort_order, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (n.id, n.parent_id, n.type, n.title, n.description, n.status,
             n.sort_order, json.dumps(n.metadata), n.created_at, n.updated_at),
        )
        self.conn.commit()
        return n

    def get_children(self, parent_id: str | None) -> list[dict]:
        if parent_id is None:
            rows = self.conn.execute(
                "SELECT * FROM nodes WHERE parent_id IS NULL ORDER BY sort_order, title"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM nodes WHERE parent_id = ? ORDER BY sort_order, title",
                (parent_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_node(self, node_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return dict(row) if row else None

    def get_tree(self, root_id: str | None = None, depth: int = 10) -> list[dict]:
        """Get subtree as flat list with depth info."""
        results = []
        self._walk_tree(root_id, 0, depth, results)
        return results

    def _walk_tree(self, parent_id: str | None, current_depth: int, max_depth: int, results: list):
        if current_depth > max_depth:
            return
        children = self.get_children(parent_id)
        for child in children:
            child["_depth"] = current_depth
            results.append(child)
            self._walk_tree(child["id"], current_depth + 1, max_depth, results)

    def add_node_ref(self, node_id: str, ref_id: str, ref_type: str, metadata: dict | None = None):
        self.conn.execute(
            """INSERT OR IGNORE INTO node_refs (node_id, ref_id, ref_type, metadata)
               VALUES (?, ?, ?, ?)""",
            (node_id, ref_id, ref_type, json.dumps(metadata or {})),
        )
        self.conn.commit()

    # ── Publication state filter ─────────────────────────────

    VALID_STATES = ("raw", "curated", "published", "canonical")

    @staticmethod
    def _build_state_filter(
        states: list[str] | None,
        include_raw: bool,
        session_source_ids: list[str] | None,
        session_author_pattern: str | None,
        *,
        alias: str = "s",
    ) -> tuple[str, list]:
        """Build a SQL predicate and params for filtering by publication_state.

        Semantics:
          - If `states` is given, restrict to exactly those states (authoritative
            override, used for `--state X,Y`).
          - Else if `include_raw` is True, no filter is applied.
          - Else (default): exclude raw sources unless the row belongs to the
            current session (by id membership or metadata.author match).

        Returns ("", []) when no filter should be applied.

        Withdrawn sources (``deprecated = 1``) are hidden from both the
        explicit ``--state`` override and the default branch — a withdrawal
        is a different axis than publication state and applies regardless of
        which states are otherwise in scope. ``--include raw`` is a
        deliberate escape hatch (see ``graph://8cf067e3-ca3``) and is left
        unfiltered here; explicit-ID lookups (``_search_source_id``) never
        call this helper at all, so a withdrawn source still resolves directly.
        """
        if states:
            placeholders = ",".join("?" for _ in states)
            return f" AND {alias}.publication_state IN ({placeholders}) AND {alias}.deprecated = 0", list(states)
        if include_raw:
            return "", []
        # Default: hide raw from other sessions; keep raw from current session.
        clauses = [f"{alias}.publication_state != 'raw'"]
        params: list = []
        if session_source_ids:
            ph = ",".join("?" for _ in session_source_ids)
            clauses.append(f"{alias}.id IN ({ph})")
            params.extend(session_source_ids)
        if session_author_pattern:
            clauses.append(f"json_extract({alias}.metadata, '$.author') LIKE ?")
            params.append(session_author_pattern)
        return " AND (" + " OR ".join(clauses) + f") AND {alias}.deprecated = 0", params

    # ── Search ───────────────────────────────────────────────

    _DEFAULT_EXCLUDED_SOURCE_TYPES: tuple[str, ...] = ("agentic",)

    SEARCH_VALID_ORDERS: tuple[str, ...] = ("relevance", "recent")

    def _build_excluded_types_clause(
        self, excluded_source_types: list[str] | None
    ) -> tuple[str, list[str]]:
        """Build a ``AND s.type NOT IN (...)`` clause excluding auxiliary source types.

        Defaults to ``['agentic']`` so dashboard-spawned agent-action runs
        don't pollute the global search surface. Pass an empty list to
        disable the filter entirely (e.g. for an "Auxiliary runs" tab).
        """
        if excluded_source_types is None:
            excluded = list(self._DEFAULT_EXCLUDED_SOURCE_TYPES)
        else:
            excluded = list(excluded_source_types)
        if not excluded:
            return "", []
        placeholders = ",".join("?" * len(excluded))
        return f" AND s.type NOT IN ({placeholders})", excluded

    @staticmethod
    def _build_session_type_clause(
        session_type: list[str] | None, *, alias: str = "s",
    ) -> tuple[str, list[str]]:
        """Build a strict ``metadata.session_type IN (...)`` predicate.

        ``session_type`` semantics:
          - ``None``: no filter applied; rows with NULL session_type are kept.
          - ``[]``: emit a contradiction (``AND 0``) so the query returns
            zero rows. Callers wanting "no filter" must pass ``None``.
          - ``[...]``: ``json_extract(metadata, '$.session_type') IN (...)``.
            ``IN`` is NULL-safe in SQL: NULL never matches any list, so rows
            without session_type are intentionally invisible — that contract
            is pinned in the search-order/session_type unit tests.
        """
        if session_type is None:
            return "", []
        if not session_type:
            return " AND 0", []
        placeholders = ",".join("?" * len(session_type))
        clause = (
            f" AND json_extract({alias}.metadata, '$.session_type') "
            f"IN ({placeholders})"
        )
        return clause, list(session_type)

    def search(self, query: str, limit: int = 20, or_mode: bool = False, tag: str | None = None,
               states: list[str] | None = None, include_raw: bool = False,
               session_source_ids: list[str] | None = None,
               session_author_pattern: str | None = None,
               excluded_source_types: list[str] | None = None,
               order: str = "relevance",
               session_type: list[str] | None = None,
               source_type: list[str] | None = None,
               ranker: str = "legacy") -> list[dict]:
        """Full-text search across thoughts and derivations, scoped to this DB.

        Org scoping is which database this method runs against — callers
        that want another org's content open that org's DB (or merge
        across DBs at the ``ops.search`` layer); this method never
        filters rows by a ``project`` field (auto-p6vn7).

        If *query* looks like a hex source ID (6+ hex chars), resolves it
        directly via prefix lookup and returns the source plus linked sources
        before falling back to FTS for content mentions.

        ``excluded_source_types`` defaults to ``['agentic']`` — auxiliary
        agent-action rows are kept out of the global search surface. Pass
        ``[]`` to disable the filter entirely.

        ``order`` selects the result ordering — ``'relevance'`` (default)
        ranks by FTS BM25 + boosts; ``'recent'`` orders by
        ``source.created_at DESC``. Any other value raises ``ValueError``.

        ``ranker`` selects relevance scoring. ``'legacy'`` preserves BM25 plus
        fixed title/tag/hit-count boosts. ``'smart'`` uses reciprocal-rank
        fusion over the legacy whole-query list and one legacy list per query
        term. Smart ranking is ignored for ``order='recent'`` because recency
        is authoritative there.

        ``session_type`` filters strictly on ``metadata.session_type``: rows
        whose JSON ``session_type`` is NULL or absent are NEVER returned
        when the filter is active. ``None`` disables the filter; ``[]``
        returns zero rows.

        Round 7l: ``LIMIT`` applies to **distinct sources**, not raw FTS
        rows. A 30-hit session contributes one source-row to the result
        set (with hit_count attached), not 30 rows competing for slots.
        Multi-hit sources rank higher via a log-shaped hit-count bonus.
        Excerpts (the per-turn FTS hits) come back as additional rows
        after the source-row, capped at ``SEARCH_EXCERPTS_PER_SOURCE``
        per source — the dashboard's ``_group_search_results`` collapses
        them back into per-card excerpt arrays.
        """
        if order not in self.SEARCH_VALID_ORDERS:
            raise ValueError(
                f"unknown search order {order!r}; "
                f"expected one of {self.SEARCH_VALID_ORDERS}"
            )
        if ranker not in SEARCH_VALID_RANKERS:
            raise ValueError(
                f"unknown search ranker {ranker!r}; "
                f"expected one of {SEARCH_VALID_RANKERS}"
            )
        if _is_source_id(query):
            # Explicit ID lookup — user asked for this specific source, do not filter by state.
            return self._search_source_id(
                query.strip(), limit=limit, tag=tag,
                excluded_source_types=excluded_source_types,
            )

        if ranker == "smart" and order == "relevance":
            return self._search_smart_query_fusion(
                query, limit=limit, or_mode=or_mode, tag=tag,
                states=states, include_raw=include_raw,
                session_source_ids=session_source_ids,
                session_author_pattern=session_author_pattern,
                excluded_source_types=excluded_source_types,
                session_type=session_type, source_type=source_type,
            )

        fts_query = _sanitize_fts_query(query, or_mode=or_mode)

        tag_clause = ""
        tag_params: list[str] = []
        if tag:
            tag_clause = " AND json_extract(s.metadata, '$.tags') LIKE ?"
            tag_params = [f'%"{tag}"%']

        state_clause, state_params = self._build_state_filter(
            states, include_raw, session_source_ids, session_author_pattern,
        )

        excl_clause, excl_params = self._build_excluded_types_clause(excluded_source_types)
        st_clause, st_params = self._build_session_type_clause(session_type)

        # source_type semantics mirror session_type:
        #   None  → no filter (every kind competes)
        #   []    → contradiction (no rows) — explicit empty list never
        #           silently widens to "all"
        #   [...] → s.type IN (...)
        type_clause = ""
        type_params: list = []
        if source_type is not None:
            if not source_type:
                type_clause = " AND 0"
            else:
                placeholders = ",".join("?" * len(source_type))
                type_clause = f" AND s.type IN ({placeholders})"
                type_params = list(source_type)

        common_filters = (
            tag_clause + state_clause + excl_clause + st_clause + type_clause
        )
        common_params: list = (
            tag_params + state_params + excl_params + st_params + type_params
        )

        # ── Phase 1: collect hit-rows per FTS table ─────────────────────
        # Each query is capped at SEARCH_PHASE1_FANOUT so one pathological
        # source can't drown out other candidates at the collection step.
        # We pull the raw FTS hits then aggregate per source_id in Python.
        hit_rows: list[dict] = []

        sources_hits = self.conn.execute(
            f"""SELECT s.id as source_id,
                      (rank + ?) as rank,
                      s.title as content,
                      NULL as turn_number,
                      NULL as tags,
                      s.id as id,
                      s.title as source_title, s.platform,
                      s.type as source_type,
                      s.created_at as source_created_at,
                      s.short_description, s.keywords,
                      s.metadata as source_metadata,
                      'source' as result_type
               FROM sources_fts fts
               JOIN sources s ON s.rowid = fts.rowid
               WHERE sources_fts MATCH ?{common_filters}
               ORDER BY rank
               LIMIT ?""",
            (SEARCH_TITLE_BOOST, fts_query, *common_params, SEARCH_PHASE1_FANOUT),
        ).fetchall()
        hit_rows.extend(dict(r) for r in sources_hits)

        thoughts_hits = self.conn.execute(
            f"""SELECT t.source_id as source_id,
                      rank as rank,
                      t.content as content,
                      t.turn_number as turn_number,
                      t.tags as tags,
                      t.id as id,
                      s.title as source_title, s.platform,
                      s.type as source_type,
                      s.created_at as source_created_at,
                      s.short_description, s.keywords,
                      s.metadata as source_metadata,
                      'thought' as result_type
               FROM thoughts_fts fts
               JOIN thoughts t ON t.rowid = fts.rowid
               JOIN sources s ON s.id = t.source_id
               WHERE thoughts_fts MATCH ?{common_filters}
               ORDER BY rank
               LIMIT ?""",
            (fts_query, *common_params, SEARCH_PHASE1_FANOUT),
        ).fetchall()
        hit_rows.extend(dict(r) for r in thoughts_hits)

        deriv_hits = self.conn.execute(
            f"""SELECT d.source_id as source_id,
                      rank as rank,
                      d.content as content,
                      d.turn_number as turn_number,
                      NULL as tags,
                      d.id as id,
                      s.title as source_title, s.platform,
                      s.type as source_type,
                      s.created_at as source_created_at,
                      s.short_description, s.keywords,
                      s.metadata as source_metadata,
                      'derivation' as result_type
               FROM derivations_fts fts
               JOIN derivations d ON d.rowid = fts.rowid
               JOIN sources s ON s.id = d.source_id
               WHERE derivations_fts MATCH ?{common_filters}
               ORDER BY rank
               LIMIT ?""",
            (fts_query, *common_params, SEARCH_PHASE1_FANOUT),
        ).fetchall()
        hit_rows.extend(dict(r) for r in deriv_hits)

        if not hit_rows:
            return []

        # ── Phase 1b: aggregate per source ──────────────────────────────
        # Per source: best (most-negative) rank wins as the source-rank
        # baseline; hit-count adds a log-shaped negative bonus so a
        # 30-hit session ranks above a 1-hit session for the same query.
        # Title-boost is already folded into individual sources_fts rows
        # via (rank + SEARCH_TITLE_BOOST) above, so MIN(rank) carries it.
        per_source: dict[str, dict] = {}
        per_source_excerpts: dict[str, list[dict]] = {}
        for h in hit_rows:
            sid = h.get("source_id")
            if sid is None:
                continue
            cur = per_source.get(sid)
            r_rank = h.get("rank") or 0
            if cur is None:
                # Capture source-level fields from the first hit; later
                # hits enrich the excerpt list and update best-rank.
                source_row = {
                    k: h.get(k) for k in (
                        "source_id", "source_title", "platform",
                        "source_type", "source_created_at",
                        "short_description", "keywords", "source_metadata",
                    )
                }
                source_row["id"] = sid
                source_row["best_rank"] = r_rank
                source_row["hit_count"] = 1
                per_source[sid] = source_row
                per_source_excerpts[sid] = [h]
            else:
                cur["hit_count"] += 1
                if r_rank < cur["best_rank"]:
                    cur["best_rank"] = r_rank
                per_source_excerpts[sid].append(h)

        # ── Phase 2: compute per-source rank ───────────────────────────
        query_tokens = _search_tokens(query)
        for sid, src in per_source.items():
            hit_count = src["hit_count"]
            best_rank = src["best_rank"]
            meta = src.get("source_metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}

            tag_tokens: set[str] = set()
            for tag in meta.get("tags") or []:
                if isinstance(tag, str):
                    tag_tokens.update(_search_tokens(tag))

            # Multi-hit bonus — log-shaped so it nudges without dominating.
            # log2(1+30)≈4.95 → ~-4.95 bonus.
            bonus = SEARCH_HIT_COUNT_BONUS_FACTOR * math.log2(1 + hit_count)
            src_rank = best_rank + bonus
            # Tag-overlap soft signal: query tokens matching source tags
            # add a capped negative delta. Skipped under recency.
            if order == "relevance" and query_tokens:
                overlap = len(query_tokens & tag_tokens)
                if overlap > 0:
                    src_rank += max(
                        SEARCH_TAG_OVERLAP_BOOST * overlap,
                        SEARCH_TAG_OVERLAP_CAP,
                    )
            src["rank"] = src_rank

        # Order sources at the source level (not row level) so LIMIT N
        # picks N distinct sources — this is the core Round 7l fix.
        if order == "recent":
            # Stable sort: created_at DESC primary, rank ASC tiebreak
            # (BM25 is negative — lower wins). Python's sorted is stable,
            # so apply the secondary key first then the primary key.
            ordered_sources = sorted(
                per_source.values(), key=lambda s: s.get("rank") or 0,
            )
            ordered_sources.sort(
                key=lambda s: s.get("source_created_at") or "",
                reverse=True,
            )
        else:
            ordered_sources = sorted(
                per_source.values(), key=lambda s: s.get("rank") or 0,
            )

        top_sources = ordered_sources[:limit]

        # ── Phase 3: shape output ───────────────────────────────────────
        # Emit one "source" result_type row per surviving source, then
        # the per-turn excerpt rows beneath it. Downstream consumers
        # (CLI: read row.result_type; dashboard: _group_search_results
        # collapses by source_id). Excerpts inherit the source-level
        # rank so they stay clustered when the merged list is re-sorted
        # in ops.search / cross-org merge layers.
        results: list[dict] = []
        for src in top_sources:
            sid = src["source_id"]
            src_rank = src["rank"]
            # Pick the top excerpt to drive the source-row's content.
            # If there's a sources_fts hit, that one wins (its content
            # is the source title and its rank carries the title boost).
            excerpts = per_source_excerpts[sid]
            excerpts_sorted = sorted(
                excerpts,
                key=lambda h: (
                    0 if h.get("result_type") == "source" else 1,
                    h.get("rank") or 0,
                ),
            )
            head = excerpts_sorted[0]
            head_row = dict(head)
            # Source-level fields override per-hit values where they
            # differ (rank in particular — the source-level rank carries
            # hit-count + tag-overlap deltas).
            head_row["source_id"] = sid
            head_row["rank"] = src_rank
            head_row["hit_count"] = src["hit_count"]
            results.append(head_row)
            # Emit additional excerpts (capped) for the dashboard's
            # group step. Skip the head row to avoid duplication.
            tail = excerpts_sorted[1:SEARCH_EXCERPTS_PER_SOURCE]
            for ex in tail:
                ex_row = dict(ex)
                ex_row["source_id"] = sid
                # Tail rows carry hit_count so downstream aggregators
                # (the dashboard's _group_search_results) can show the
                # real match count even though excerpts are capped at
                # SEARCH_EXCERPTS_PER_SOURCE — a 30-hit session still
                # surfaces "30 matches" rather than the visible 10.
                ex_row["hit_count"] = src["hit_count"]
                # Excerpts keep their own rank so the per-source excerpt
                # sort in _group_search_results lands the strongest
                # excerpt first within the card. Source-level rank lives
                # only on the head row.
                results.append(ex_row)

        return results

    def _search_smart_query_fusion(
        self,
        query: str,
        *,
        limit: int,
        or_mode: bool,
        tag: str | None,
        states: list[str] | None,
        include_raw: bool,
        session_source_ids: list[str] | None,
        session_author_pattern: str | None,
        excluded_source_types: list[str] | None,
        session_type: list[str] | None,
        source_type: list[str] | None,
    ) -> list[dict]:
        """Fuse whole-query and per-term legacy rankings at source level.

        Each constituent stream is already source-aware and keeps the mature
        title, tag, hit-count, state, and type behavior of legacy search. RRF
        combines only distinct-source positions, so repeated excerpts and the
        three physical FTS tables cannot create extra votes. The term-stream
        cap bounds work for long generated queries; recent agent searches are
        normally one or two terms.
        """
        ordered_terms: list[str] = []
        seen_terms: set[str] = set()
        for token in _re.findall(r"[^\W_]+", query, flags=_re.UNICODE):
            token = token.casefold()
            if len(token) <= 2 or token in seen_terms:
                continue
            seen_terms.add(token)
            ordered_terms.append(token)

        stream_specs: list[tuple[str, str, bool]] = [
            ("strict", query, or_mode),
        ]
        # A one-term stream would be identical to the strict stream. Avoid
        # doing the same three FTS queries twice in that common case.
        if len(ordered_terms) > 1:
            stream_specs.extend(
                (f"term:{term}", term, False)
                for term in ordered_terms[:SEARCH_SMART_MAX_TERM_STREAMS]
            )

        stream_limit = max(limit, SEARCH_SMART_STREAM_FANOUT)
        stream_groups: dict[str, dict[str, list[dict]]] = {}
        stream_positions: dict[str, dict[str, int]] = {}
        scores: dict[str, float] = {}

        for name, stream_query, stream_or_mode in stream_specs:
            rows = self.search(
                stream_query,
                limit=stream_limit,
                or_mode=stream_or_mode,
                tag=tag,
                states=states,
                include_raw=include_raw,
                session_source_ids=session_source_ids,
                session_author_pattern=session_author_pattern,
                excluded_source_types=excluded_source_types,
                order="relevance",
                session_type=session_type,
                source_type=source_type,
                ranker="legacy",
            )
            groups: dict[str, list[dict]] = {}
            positions: dict[str, int] = {}
            for row in rows:
                sid = row.get("source_id") or row.get("id")
                if not sid:
                    continue
                if sid not in groups:
                    groups[sid] = []
                    positions[sid] = len(positions) + 1
                    scores[sid] = scores.get(sid, 0.0) + (
                        1.0 / (SEARCH_SMART_RRF_K + positions[sid])
                    )
                groups[sid].append(row)
            stream_groups[name] = groups
            stream_positions[name] = positions

        if not scores:
            return []

        # Match the evaluated prototype exactly: descending fused score with
        # source ID as the deterministic tie-break. No raw BM25 scores cross
        # stream boundaries.
        ordered_sources = sorted(scores, key=lambda sid: (-scores[sid], sid))
        results: list[dict] = []
        for sid in ordered_sources[:limit]:
            available_streams = [
                name for name, _, _ in stream_specs
                if sid in stream_groups[name]
            ]
            representative = (
                "strict" if sid in stream_groups["strict"]
                else min(
                    available_streams,
                    key=lambda name: stream_positions[name][sid],
                )
            )
            representative_rows = stream_groups[representative][sid]
            head = dict(representative_rows[0])
            head["source_id"] = sid
            head["rank"] = -scores[sid]
            head["hit_count"] = max(
                int(stream_groups[name][sid][0].get("hit_count") or 1)
                for name in available_streams
            )
            head["ranking_explain"] = {
                "ranker": "smart",
                "query_terms": ordered_terms,
                "stream_ranks": {
                    name: stream_positions[name][sid]
                    for name in available_streams
                },
                "rrf_score": scores[sid],
                "term_streams_truncated": max(
                    0, len(ordered_terms) - SEARCH_SMART_MAX_TERM_STREAMS,
                ),
            }
            results.append(head)

            # Use the strict-query excerpts when available, then fill from
            # the strongest term streams. This keeps the most coherent
            # passage visible while still giving term-only candidates useful
            # snippets. Dedupe rows that appear in multiple formulations.
            seen_rows = {
                (head.get("id"), head.get("result_type"), head.get("content")),
            }
            excerpt_streams = [representative] + sorted(
                (name for name in available_streams if name != representative),
                key=lambda name: stream_positions[name][sid],
            )
            emitted = 1
            for name in excerpt_streams:
                for row in stream_groups[name][sid]:
                    row_key = (
                        row.get("id"), row.get("result_type"), row.get("content"),
                    )
                    if row_key in seen_rows:
                        continue
                    seen_rows.add(row_key)
                    excerpt = dict(row)
                    excerpt["source_id"] = sid
                    excerpt["hit_count"] = head["hit_count"]
                    results.append(excerpt)
                    emitted += 1
                    if emitted >= SEARCH_EXCERPTS_PER_SOURCE:
                        break
                if emitted >= SEARCH_EXCERPTS_PER_SOURCE:
                    break

        return results

    def _search_source_id(self, query: str, limit: int = 20, tag: str | None = None,
                          excluded_source_types: list[str] | None = None) -> list[dict]:
        """Resolve a source-ID-shaped query directly.

        Returns:
            1. The source itself (prefix match)
            2. Sources linked TO it (edges where target_id matches)
            3. Sources linked FROM it (edges where source_id matches)
            4. FTS fallback — thoughts/derivations whose content mentions the ID

        ``excluded_source_types`` is forwarded to the FTS fallback so that
        agentic rows mentioning an ID don't pollute results. The direct
        prefix lookup is NOT filtered — when a user asks for an exact ID
        the agentic source itself should still be returned.
        """
        results: list[dict] = []
        seen_source_ids: set[str] = set()

        # 1. Direct prefix lookup
        source = self.get_source(query)
        if source:
            if tag:
                meta = source.get("metadata")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                if tag not in (meta.get("tags") or []):
                    source = None

        if source:
            sid = source["id"]
            seen_source_ids.add(sid)
            meta = source.get("metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            results.append({
                "id": sid,
                "content": source.get("title") or sid,
                "turn_number": None,
                "source_id": sid,
                "source_title": source.get("title") or sid,
                "short_description": source.get("short_description"),
                "platform": source.get("platform"),
                "result_type": "source",
                "rank": -1000,  # always first
                "source_type": source.get("type"),
                "created_at": source.get("created_at"),
                "source_created_at": source.get("created_at"),
                "source_metadata": source.get("metadata"),
            })

            # 2 & 3. Linked sources via edges (both directions)
            edges = self.neighbors(sid, limit=50)
            for edge in edges:
                other_id = edge["target_id"] if edge["source_id"] == sid else edge["source_id"]
                direction = "→" if edge["source_id"] == sid else "←"
                if other_id in seen_source_ids:
                    continue

                # Resolve the other end — it could be a source, thought, or derivation
                other_source = None
                other_type = edge["target_type"] if edge["source_id"] == sid else edge["source_type"]

                if other_type == "source":
                    other_source = self.get_source(other_id)
                else:
                    # Edge points to a thought/derivation — look up its parent source
                    row = self.conn.execute(
                        "SELECT source_id FROM thoughts WHERE id = ? "
                        "UNION SELECT source_id FROM derivations WHERE id = ?",
                        (other_id, other_id),
                    ).fetchone()
                    if row:
                        parent_sid = row[0]
                        if parent_sid not in seen_source_ids:
                            other_source = self.get_source(parent_sid)
                            other_id = parent_sid

                if other_source and other_id not in seen_source_ids:
                    if tag:
                        ometa = other_source.get("metadata")
                        if isinstance(ometa, str):
                            try:
                                ometa = json.loads(ometa)
                            except (json.JSONDecodeError, TypeError):
                                ometa = {}
                        if tag not in (ometa.get("tags") or []):
                            continue
                    seen_source_ids.add(other_id)
                    edge_meta = edge.get("metadata", "{}")
                    if isinstance(edge_meta, str):
                        try:
                            edge_meta = json.loads(edge_meta)
                        except (json.JSONDecodeError, TypeError):
                            edge_meta = {}
                    turn = edge_meta.get("turn") or edge_meta.get("turn_number")
                    relation = edge.get("relation", "linked")
                    results.append({
                        "id": other_id,
                        "content": f"{direction} {relation}: {other_source.get('title') or other_id}",
                        "turn_number": turn,
                        "source_id": other_id,
                        "source_title": other_source.get("title") or other_id,
                        "platform": other_source.get("platform"),
                        "result_type": "edge",
                        "rank": -500,
                        "relation": relation,
                        "direction": direction,
                        "source_type": other_source.get("type"),
                    })

        # 4. FTS fallback — content that mentions this ID string
        remaining = limit - len(results)
        if remaining > 0:
            try:
                fts_query = _sanitize_fts_query(query)
                tag_clause = ""
                tag_params: list[str] = []
                if tag:
                    tag_clause = " AND json_extract(s.metadata, '$.tags') LIKE ?"
                    tag_params = [f'%"{tag}"%']
                excl_clause, excl_params = self._build_excluded_types_clause(excluded_source_types)
                for table, content_table, rtype in [
                    ("thoughts_fts", "thoughts", "thought"),
                    ("derivations_fts", "derivations", "derivation"),
                ]:
                    rows = self.conn.execute(
                        f"""SELECT t.id, t.content, t.turn_number, t.source_id,
                                   s.title as source_title, s.platform,
                                   s.type as source_type,
                                   s.created_at as source_created_at,
                                   s.short_description, s.keywords,
                                   s.metadata as source_metadata,
                                   '{rtype}' as result_type, rank
                            FROM {table} fts
                            JOIN {content_table} t ON t.rowid = fts.rowid
                            JOIN sources s ON s.id = t.source_id
                            WHERE {table} MATCH ?{tag_clause}{excl_clause}
                            ORDER BY rank LIMIT ?""",
                        (fts_query, *tag_params, *excl_params, remaining),
                    ).fetchall()
                    for r in rows:
                        rd = dict(r)
                        if rd["source_id"] not in seen_source_ids:
                            results.append(rd)
            except Exception:
                pass  # FTS may not match hex strings — that's fine

        return results[:limit]

    def search_entities(self, query: str, limit: int = 20) -> list[dict]:
        """Search entities by name."""
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE canonical_name LIKE ? ORDER BY name LIMIT ?",
            (f"%{query.lower()}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Stats ────────────────────────────────────────────────

    def stats(self) -> dict:
        result = {}
        for table in ["sources", "thoughts", "derivations", "entities", "claims", "edges", "entity_mentions", "nodes"]:
            row = self.conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            result[table] = row["cnt"]
        return result

    # ── Graph Queries ────────────────────────────────────────

    def neighbors(self, node_id: str, relation: str | None = None, limit: int = 50) -> list[dict]:
        """Find all edges from/to a given node."""
        if relation:
            rows = self.conn.execute(
                """SELECT * FROM edges
                   WHERE (source_id = ? OR target_id = ?) AND relation = ?
                   LIMIT ?""",
                (node_id, node_id, relation, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM edges
                   WHERE source_id = ? OR target_id = ?
                   LIMIT ?""",
                (node_id, node_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def entity_thoughts(self, entity_id: str) -> list[dict]:
        """Find all thoughts that mention a given entity."""
        rows = self.conn.execute(
            """SELECT t.*, s.title as source_title, s.platform
               FROM entity_mentions em
               JOIN thoughts t ON t.id = em.content_id AND em.content_type = 'thought'
               JOIN sources s ON s.id = t.source_id
               WHERE em.entity_id = ?
               ORDER BY t.created_at""",
            (entity_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Source Reading ─────────────────────────────────────────

    def get_source(self, source_id: str) -> dict | None:
        """Look up a source by ID, prefix, or session UUID (stored in metadata)."""
        # Exact match
        row = self.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row:
            return dict(row)
        # Prefix match
        row = self.conn.execute("SELECT * FROM sources WHERE id LIKE ? LIMIT 1", (f"{source_id}%",)).fetchone()
        if row:
            return dict(row)
        # Session UUID match (stored in metadata JSON)
        row = self.conn.execute(
            """SELECT * FROM sources WHERE json_extract(metadata, '$.session_id') LIKE ? LIMIT 1""",
            (f"{source_id}%",),
        ).fetchone()
        return dict(row) if row else None

    def resolve_source_strict(self, value: str) -> dict | list[dict] | None:
        """Strict source resolution: exact → prefix → session_uuid → file_path.

        Returns:
            dict — single match (success)
            list[dict] — multiple matches (caller should error with candidates)
            None — no match
        """
        # 1. Exact source ID match
        row = self.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (value,)
        ).fetchone()
        if row:
            return dict(row)

        # 2. Prefix match on source ID
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE id LIKE ?", (f"{value}%",)
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]

        # 3. Match by session_uuid in metadata (the JSONL filename stem)
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE json_extract(metadata, '$.session_uuid') LIKE ?",
            (f"{value}%",),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]

        # 4. Match by session_id in metadata (legacy, same as session_uuid)
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE json_extract(metadata, '$.session_id') LIKE ?",
            (f"{value}%",),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]

        # 5. Match by file_path containing the value (JSONL UUID in path)
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE file_path LIKE ?",
            (f"%/{value}%.jsonl",),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]

        return None

    def withdraw_source(self, source_id: str) -> dict:
        """Mark a source withdrawn (``deprecated = 1``). Reversible flag flip,

        not a delete: the record and any links to/from it survive untouched,
        and it stays reachable via direct ID lookup (``graph read``); it is
        only hidden from search (``_build_state_filter``) and listings
        (``list_sources``, both of which funnel through that same helper).
        """
        cur = self.conn.execute(
            "UPDATE sources SET deprecated = 1 WHERE id = ? AND deprecated = 0",
            (source_id,),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise LookupError(f"No source found matching '{source_id}'")
        return {"source_id": row["id"], "title": row["title"] or "", "already_withdrawn": cur.rowcount == 0}

    def get_latest_turn(self, source_id: str) -> int | None:
        """Return the highest turn_number for a source, or None if no turns."""
        row = self.conn.execute(
            "SELECT MAX(turn_number) as max_turn FROM thoughts WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return row["max_turn"] if row and row["max_turn"] is not None else None

    def get_recent_turns(self, source_id: str, limit: int = 50) -> list[dict]:
        """Return the most recent N turns for a source, newest first."""
        rows = self.conn.execute(
            """SELECT turn_number, content FROM thoughts
               WHERE source_id = ? ORDER BY turn_number DESC LIMIT ?""",
            (source_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_source_content(self, source_id: str) -> list[dict]:
        """Get all thoughts and derivations for a source, ordered by turn number."""
        rows = self.conn.execute(
            """SELECT id, content, role, turn_number, message_id, metadata, created_at,
                      'thought' as entry_type
               FROM thoughts WHERE source_id = ?
               UNION ALL
               SELECT id, content, model as role, turn_number, message_id, metadata, created_at,
                      'derivation' as entry_type
               FROM derivations WHERE source_id = ?
               ORDER BY turn_number""",
            (source_id, source_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_sources(self, query: str, limit: int = 20) -> list[dict]:
        """Search sources by title."""
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE title LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_sources(self, source_type: str | None = None, limit: int = 20,
                     since: str | None = None, until: str | None = None, author: str | None = None,
                     tags: list[str] | None = None,
                     states: list[str] | None = None, include_raw: bool = False,
                     session_source_ids: list[str] | None = None,
                     session_author_pattern: str | None = None) -> list[dict]:
        """List sources with optional filters, scoped to this DB.

        Org scoping is which database this method runs against; it never
        filters rows by a ``project`` field (auto-p6vn7).

        Publication-state defaults: excludes raw sources from other sessions.
        Pass `include_raw=True` to disable the filter, or `states=[...]` to
        restrict to specific states.

        ``author`` is an exact match against ``metadata.author`` — this is
        also how callers filter to a specific creating session, since notes
        are stamped with the creating tmux session's name by default (see
        ``cmd_note``).
        """
        query = "SELECT * FROM sources s WHERE 1=1"
        params: list = []
        if source_type:
            query += " AND s.type = ?"
            params.append(source_type)
        if since:
            query += " AND s.created_at >= ?"
            params.append(since)
        if until:
            query += " AND s.created_at <= ?"
            params.append(until)
        if author:
            query += " AND json_extract(s.metadata, '$.author') = ?"
            params.append(author)
        if tags:
            for tag in tags:
                query += " AND json_extract(s.metadata, '$.tags') LIKE ?"
                params.append(f'%"{tag}"%')
        state_clause, state_params = self._build_state_filter(
            states, include_raw, session_source_ids, session_author_pattern,
        )
        query += state_clause
        params.extend(state_params)
        query += " ORDER BY s.created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── Note Comments ───────────────────────────────────────

    def insert_comment(self, source_id: str, content: str, actor: str = "user") -> dict:
        cid = new_id()
        self.conn.execute(
            """INSERT INTO note_comments (id, source_id, content, actor)
               VALUES (?, ?, ?, ?)""",
            (cid, source_id, content, actor),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM note_comments WHERE id = ?", (cid,)).fetchone()
        return dict(row)

    def get_comments(self, source_id: str, include_integrated: bool = False) -> list[dict]:
        if include_integrated:
            rows = self.conn.execute(
                "SELECT * FROM note_comments WHERE source_id = ? ORDER BY created_at ASC",
                (source_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM note_comments WHERE source_id = ? AND integrated = 0 ORDER BY created_at ASC",
                (source_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def integrate_comment(self, comment_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE note_comments SET integrated = 1 WHERE id = ? AND integrated = 0",
            (comment_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ── Note Versions ─────────────────────────────────────

    def insert_note_version(self, source_id: str, version: int, content: str):
        self.conn.execute(
            """INSERT INTO note_versions (source_id, version, content)
               VALUES (?, ?, ?)""",
            (source_id, version, content),
        )

    def get_note_version(self, source_id: str, version: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM note_versions WHERE source_id = ? AND version = ?",
            (source_id, version),
        ).fetchone()
        return dict(row) if row else None

    def list_note_versions(self, source_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM note_versions WHERE source_id = ? ORDER BY version ASC",
            (source_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_max_note_version(self, source_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(version) as max_v FROM note_versions WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return row["max_v"] or 0

    def update_thought_content(self, thought_id: str, content: str):
        self.conn.execute(
            "UPDATE thoughts SET content = ? WHERE id = ?",
            (content, thought_id),
        )

    # ── Attachments ─────────────────────────────────────────

    def insert_attachment(self, att: Attachment) -> Attachment:
        self.conn.execute(
            """INSERT INTO attachments (id, hash, filename, mime_type, size_bytes, file_path,
                                        source_id, turn_number, metadata, alt_text, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (att.id, att.hash, att.filename, att.mime_type, att.size_bytes, att.file_path,
             att.source_id, att.turn_number, json.dumps(att.metadata), att.alt_text, att.created_at),
        )
        self.conn.commit()
        return att

    def get_attachment(self, att_id: str) -> dict | None:
        """Look up attachment by ID or prefix."""
        row = self.conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
        if row:
            return dict(row)
        row = self.conn.execute("SELECT * FROM attachments WHERE id LIKE ? LIMIT 1", (f"{att_id}%",)).fetchone()
        return dict(row) if row else None

    def get_attachment_by_hash(self, hash: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM attachments WHERE hash = ?", (hash,)).fetchone()
        return dict(row) if row else None

    def list_attachments(self, source_id: str | None = None, limit: int = 50) -> list[dict]:
        if source_id:
            rows = self.conn.execute(
                "SELECT * FROM attachments WHERE source_id LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"{source_id}%", limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM attachments ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Tags ──────────────────────────────────────────────────

    def _seed_tags(self):
        """Seed tags table from existing note metadata (idempotent)."""
        rows = self.conn.execute(
            "SELECT metadata FROM sources WHERE type='note' AND metadata LIKE '%tags%'"
        ).fetchall()
        for row in rows:
            meta = json.loads(row["metadata"] or "{}")
            for tag in meta.get("tags", []):
                self.conn.execute(
                    "INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,)
                )
        self.conn.commit()

    def list_tags(self, limit: int = 100) -> list[dict]:
        """List tags with note counts, sorted by usage."""
        rows = self.conn.execute("""
            SELECT t.name, t.description, t.updated_at,
                   COUNT(s.id) as note_count
            FROM tags t
            LEFT JOIN sources s ON s.type = 'note'
                AND json_extract(s.metadata, '$.tags') LIKE '%' || t.name || '%'
            GROUP BY t.name
            ORDER BY note_count DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def update_tag_description(self, name: str, description: str, actor: str = "user") -> bool:
        """Set or update a tag's description. Creates the tag if it doesn't exist."""
        self.conn.execute("""
            INSERT INTO tags (name, description, created_by, updated_at)
            VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(name) DO UPDATE SET
                description = excluded.description,
                updated_at = excluded.updated_at
        """, (name, description, actor))
        self.conn.commit()
        return True

    # ── Captures ──────────────────────────────────────────────

    def insert_capture(self, capture_id: str, content: str, *,
                       source_id: str | None = None, turn_number: int | None = None,
                       thread_id: str | None = None, actor: str = "user") -> None:
        """Insert a thought capture."""
        self.conn.execute(
            "INSERT INTO captures (id, content, source_id, turn_number, thread_id, actor)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (capture_id, content, source_id, turn_number, thread_id, actor),
        )
        self.conn.commit()

    def list_captures(self, thread_id: str | None = None, status: str | None = None,
                      since: str | None = None, limit: int = 20) -> list[dict]:
        """List captures, optionally filtered."""
        query = "SELECT * FROM captures WHERE 1=1"
        params: list = []
        if thread_id:
            query += " AND thread_id = ?"
            params.append(thread_id)
        if status:
            if status != "*":
                query += " AND status = ?"
                params.append(status)
        elif not thread_id:
            # Default: show unthreaded captures (inbox)
            query += " AND thread_id IS NULL"
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def assign_capture_to_thread(self, capture_id: str, thread_id: str) -> None:
        """Assign a capture to a thread."""
        self.conn.execute(
            "UPDATE captures SET thread_id = ?, status = 'threaded' WHERE id = ?",
            (thread_id, capture_id),
        )
        self.conn.commit()

    # ── Threads ───────────────────────────────────────────────

    def insert_thread(self, thread_id: str, title: str, *, priority: int = 1,
                      created_by: str | None = None) -> None:
        """Create a new thread."""
        self.conn.execute(
            "INSERT INTO threads (id, title, priority, created_by) VALUES (?, ?, ?, ?)",
            (thread_id, title, priority, created_by),
        )
        self.conn.commit()

    def list_threads(self, status: str | None = "active", limit: int = 20) -> list[dict]:
        """List threads with capture counts."""
        query = """
            SELECT t.*, COUNT(c.id) as capture_count
            FROM threads t
            LEFT JOIN captures c ON c.thread_id = t.id
        """
        params: list = []
        if status:
            query += " WHERE t.status = ?"
            params.append(status)
        query += " GROUP BY t.id ORDER BY t.priority, t.updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def update_thread_status(self, thread_id: str, status: str) -> None:
        """Update thread status (active/parked/done)."""
        self.conn.execute(
            "UPDATE threads SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (status, thread_id),
        )
        self.conn.commit()

    def get_thread(self, thread_id: str) -> dict | None:
        """Get a single thread by ID or prefix."""
        row = self.conn.execute("SELECT * FROM threads WHERE id = ? OR id LIKE ?",
                                (thread_id, thread_id + '%')).fetchone()
        return dict(row) if row else None

    def commit(self):
        self.conn.commit()
