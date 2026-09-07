"""Per-session token ledger — cumulative billed tokens (bead auto-pbrhs).

``tmux_sessions.context_tokens`` is a GAUGE: how large the context is right
now. It falls when a session compacts, so it can never answer "what did this
session spend". These counters answer that, by summing each assistant turn's
own ``usage`` block, and the summing is what makes a compaction harmless —
the next turn simply bills less input.

The idempotency the ledger needs already existed: ``persist_tail_state``
acks a drain window only if the row's cursor is still where that window's
read began, so an ack for bytes already drained is dropped whole. These
tests pin that the ledger rides inside that guard rather than beside it.
"""
from __future__ import annotations

import sqlite3

import pytest

from tools.dashboard.dao import dashboard_db
from tools.dashboard.session_harness import (
    ClaudeSessionHarness,
    CodexSessionHarness,
    _usage_int,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    dashboard_db.init_db(tmp_path / "dashboard.db")
    yield dashboard_db
    dashboard_db.reset_conn()


def _columns(conn) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(tmux_sessions)")}


def _insert(db, tmux_name="s1", *, path="/t/a.jsonl", generation="1:2:0", offset=0):
    db.get_conn().execute(
        "INSERT INTO tmux_sessions (tmux_name, type, project, created_at, "
        "jsonl_path, jsonl_generation, file_offset) VALUES (?,?,?,?,?,?,?)",
        (tmux_name, "terminal", "p", 0.0, path, generation, offset),
    )
    db.get_conn().commit()


def _ledger(db, tmux_name="s1") -> dict[str, int]:
    row = db.get_conn().execute(
        "SELECT * FROM tmux_sessions WHERE tmux_name=?", (tmux_name,),
    ).fetchone()
    return {c: row[c] for c in db.USAGE_LEDGER_COLUMNS}


def _turn(*, inp=0, cache_create=0, cache_read=0, out=0) -> dict:
    return {"type": "assistant", "message": {"usage": {
        "input_tokens": inp,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read,
        "output_tokens": out,
    }}}


# ── schema ────────────────────────────────────────────────────


def test_a_fresh_database_has_the_ledger(db):
    assert set(db.USAGE_LEDGER_COLUMNS) <= _columns(db.get_conn())


def test_an_existing_database_is_migrated(tmp_path):
    """The live dashboard.db predates these columns; init must add them
    rather than only defining them for new installs."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    # The shape the live table had before this bead: everything the earlier
    # migrations expect to find, and none of the ledger.
    conn.execute(
        "CREATE TABLE tmux_sessions ("
        "  tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,"
        "  type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,"
        "  bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,"
        "  file_offset INTEGER DEFAULT 0, last_activity REAL,"
        "  entry_count INTEGER DEFAULT 0, context_tokens INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO tmux_sessions (tmux_name, type, project, created_at) "
        "VALUES ('old', 'terminal', 'p', 0.0)"
    )
    conn.commit()
    conn.close()

    dashboard_db.init_db(path)
    try:
        assert set(dashboard_db.USAGE_LEDGER_COLUMNS) <= _columns(
            dashboard_db.get_conn(),
        )
        # An existing session starts the ledger at zero, not at NULL, so
        # readers can sum without coalescing.
        row = dashboard_db.get_conn().execute(
            "SELECT * FROM tmux_sessions WHERE tmux_name='old'",
        ).fetchone()
        assert all(row[c] == 0 for c in dashboard_db.USAGE_LEDGER_COLUMNS)
    finally:
        dashboard_db.reset_conn()


def test_the_ledger_is_projected_to_readers(db):
    assert set(db.USAGE_LEDGER_COLUMNS) <= set(db._OVERLAY_COLUMNS)


# ── persistence ───────────────────────────────────────────────


def _ack(db, **kw):
    return db.persist_tail_state(
        kw.pop("tmux_name", "s1"),
        expect_path="/t/a.jsonl",
        expect_generation="1:2:0",
        **kw,
    )


def test_usage_accumulates_across_windows(db):
    _insert(db)
    assert _ack(db, expect_offset=0, file_offset=100,
                usage_add={"usage_input_tokens": 5, "usage_output_tokens": 7,
                           "usage_turns": 1})
    assert _ack(db, expect_offset=100, file_offset=200,
                usage_add={"usage_input_tokens": 3, "usage_output_tokens": 2,
                           "usage_turns": 1})
    ledger = _ledger(db)
    assert ledger["usage_input_tokens"] == 8
    assert ledger["usage_output_tokens"] == 9
    assert ledger["usage_turns"] == 2


def test_a_dropped_ack_does_not_count_its_tokens(db):
    """The idempotency criterion. A window whose bytes were already drained
    fails the cursor CAS, and its tokens must go with it -- otherwise a
    re-read of an unchanged transcript inflates the spend."""
    _insert(db)
    assert _ack(db, expect_offset=0, file_offset=100,
                usage_add={"usage_input_tokens": 5, "usage_turns": 1})
    # Same window replayed: the cursor has moved on, so this ack is stale.
    assert not _ack(db, expect_offset=0, file_offset=100,
                    usage_add={"usage_input_tokens": 5, "usage_turns": 1})
    assert _ledger(db)["usage_input_tokens"] == 5


def test_an_ack_for_a_replaced_file_does_not_count_its_tokens(db):
    _insert(db)
    ok = db.persist_tail_state(
        "s1", expect_path="/t/a.jsonl", expect_generation="9:9:9",
        expect_offset=0, file_offset=100,
        usage_add={"usage_input_tokens": 5},
    )
    assert not ok
    assert _ledger(db)["usage_input_tokens"] == 0


def test_an_unknown_usage_column_is_refused(db):
    _insert(db)
    with pytest.raises(ValueError, match="unknown usage column"):
        _ack(db, expect_offset=0, file_offset=10,
             usage_add={"usage_input_tokns": 5})


def test_no_usage_leaves_the_ledger_alone(db):
    _insert(db)
    assert _ack(db, expect_offset=0, file_offset=10, entry_count_add=3)
    assert _ledger(db) == dict.fromkeys(db.USAGE_LEDGER_COLUMNS, 0)


# ── extraction ────────────────────────────────────────────────


def test_claude_reads_the_usage_totals():
    delta = ClaudeSessionHarness().extract_usage_delta(
        _turn(inp=2, cache_create=42874, cache_read=11919, out=143),
    )
    assert delta == {
        "usage_input_tokens": 2,
        "usage_cache_creation_tokens": 42874,
        "usage_cache_read_tokens": 11919,
        "usage_output_tokens": 143,
    }


def test_claude_ignores_the_per_message_iterations_breakdown():
    """``usage.iterations`` repeats the same numbers per message; counting
    both the totals and the breakdown would bill every turn twice."""
    entry = _turn(inp=2, out=143)
    entry["message"]["usage"]["iterations"] = [
        {"input_tokens": 2, "output_tokens": 143},
    ]
    assert ClaudeSessionHarness().extract_usage_delta(entry) == {
        "usage_input_tokens": 2,
        "usage_cache_creation_tokens": 0,
        "usage_cache_read_tokens": 0,
        "usage_output_tokens": 143,
    }


@pytest.mark.parametrize("entry", [
    {"type": "user", "message": {"usage": {"input_tokens": 5}}},
    {"type": "assistant"},
    {"type": "assistant", "message": {}},
    {"type": "assistant", "message": {"usage": None}},
    {"type": "assistant", "message": {"usage": {}}},
])
def test_an_entry_that_billed_nothing_yields_nothing(entry):
    assert ClaudeSessionHarness().extract_usage_delta(entry) is None


def test_codex_accrues_no_ledger_yet():
    """Codex needs its own accounting rule (per-turn vs cumulative). Zero is
    honest; a guess would silently corrupt a spend figure."""
    assert CodexSessionHarness().extract_usage_delta(
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 10, "output_tokens": 3}}}},
    ) is None


@pytest.mark.parametrize("value,expected", [
    (5, 5), ("5", 5), (0, 0), (-3, 0), (None, 0), ("abc", 0), ({}, 0),
])
def test_a_malformed_count_never_poisons_the_sum(value, expected):
    assert _usage_int(value) == expected


# ── the property the gauge could not give us ──────────────────


def test_a_compaction_cannot_reduce_the_ledger(db):
    """A compaction drops the context size, so context_tokens falls. The
    ledger must keep rising: the post-compaction turn simply bills less
    input, and its usage is still added."""
    _insert(db)
    harness = ClaudeSessionHarness()
    before = harness.extract_usage_delta(_turn(inp=1, cache_read=90_000, out=50))
    after = harness.extract_usage_delta(_turn(inp=1, cache_read=200, out=50))

    _ack(db, expect_offset=0, file_offset=100,
         usage_add={**before, "usage_turns": 1},
         context_tokens=90_001)
    _ack(db, expect_offset=100, file_offset=200,
         usage_add={**after, "usage_turns": 1},
         context_tokens=201)

    row = db.get_conn().execute(
        "SELECT context_tokens FROM tmux_sessions WHERE tmux_name='s1'",
    ).fetchone()
    ledger = _ledger(db)
    assert row["context_tokens"] == 201                      # the gauge fell
    assert ledger["usage_cache_read_tokens"] == 90_200       # the counter rose
    assert ledger["usage_output_tokens"] == 100
    assert ledger["usage_turns"] == 2
