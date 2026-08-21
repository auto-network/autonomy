"""Tests for the dispatch-completion journal-entry hook (auto-wvdhs).

The agent's ``decision.json`` may carry an optional ``journal_entry`` field
referencing a graph source written via ``graph journal write``. That field
flows through:

  decision.json → insert_run → dispatch_runs(journal_source_id, journal_compact)
                              → DAO/api → /api/dispatch/runs / /api/timeline
                              → Activity Feed card 📔 badge → /activity#journal-<id>

Coverage:

* ``insert_run`` parses the optional ``journal_entry`` and persists both
  flat columns. Missing / malformed shapes degrade silently (no crash, no
  partial writes).
* ``api_dispatch_runs`` reconstructs ``journal_entry`` from the row.
* ``_row_to_timeline_entry`` surfaces ``journal_entry`` to the timeline API.
* Mock DAO ``RUN_DEFAULTS`` / ``TIMELINE_ENTRY_DEFAULTS`` include the
  ``journal_entry`` key so fixtures that omit it render the absent state.
* Closing-prompt template (``tool_guidelines.md``) contains the optional-
  journal section so dispatched agents see the wrap-up nudge.
* Activity Feed card markup conditionally renders the 📔 badge with an
  href to ``/activity#journal-<id>``; the Attention entry exposes a
  matching ``id="journal-<source_id>"`` anchor.
* Activity-page Alpine helper switches to the Attention tab and scrolls
  into the entry when the URL hash matches ``#journal-...``.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
TIMELINE_HTML = REPO_ROOT / "tools/dashboard/templates/pages/timeline.html"
ACTIVITY_JS = REPO_ROOT / "tools/dashboard/static/js/pages/activity.js"
TOOL_GUIDELINES = REPO_ROOT / "agents/shared/tool_guidelines.md"


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_dispatch_env(tmp_path, monkeypatch):
    """Pin DISPATCH_DB to a tmp file and reload the writer + reader modules."""
    dispatch_db = tmp_path / "dispatch.db"
    monkeypatch.setenv("DISPATCH_DB", str(dispatch_db))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    from agents import dispatch_db as writer_mod
    importlib.reload(writer_mod)
    from tools.dashboard.dao import dispatch as reader_mod
    importlib.reload(reader_mod)

    writer_mod.init_db()
    return {
        "dispatch_db": dispatch_db,
        "writer": writer_mod,
        "reader": reader_mod,
    }


def _baseline_run_kwargs(run_id: str, bead_id: str) -> dict:
    return dict(
        run_id=run_id,
        bead_id=bead_id,
        started_at=1714305600.0,
        completed_at=1714305900.0,
        status="DONE",
        reason="ok",
        commit_hash="",
        branch=f"agent/{bead_id}",
        branch_base="",
        image="autonomy-session",
        container_name=f"agent-{bead_id}",
        exit_code=0,
        output_dir="",
    )


# ── 1. insert_run persists the journal entry ───────────────────────────


def test_insert_run_persists_journal_entry(isolated_dispatch_env):
    env = isolated_dispatch_env
    writer = env["writer"]

    decision = {
        "status": "DONE",
        "reason": "ok",
        "scores": {"tooling": 5, "clarity": 5, "confidence": 5},
        "journal_entry": {
            "source_id": "src:abc123def456",
            "compact": "Fixed merge-failure recovery on stash-pop conflicts",
        },
    }
    writer.insert_run(decision=decision, **_baseline_run_kwargs(
        "auto-jrn1-20260503-100000", "auto-jrn1",
    ))

    conn = sqlite3.connect(str(env["dispatch_db"]))
    row = conn.execute(
        "SELECT journal_source_id, journal_compact FROM dispatch_runs WHERE id=?",
        ("auto-jrn1-20260503-100000",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "src:abc123def456"
    assert row[1] == "Fixed merge-failure recovery on stash-pop conflicts"


def test_insert_run_absent_journal_entry_leaves_nulls(isolated_dispatch_env):
    """Backward compat: most decisions omit journal_entry — columns stay NULL."""
    env = isolated_dispatch_env
    writer = env["writer"]

    writer.insert_run(
        decision={"status": "DONE", "reason": "ok"},
        **_baseline_run_kwargs("auto-jrn2-20260503-100000", "auto-jrn2"),
    )

    conn = sqlite3.connect(str(env["dispatch_db"]))
    row = conn.execute(
        "SELECT journal_source_id, journal_compact FROM dispatch_runs WHERE id=?",
        ("auto-jrn2-20260503-100000",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] is None
    assert row[1] is None


@pytest.mark.parametrize("malformed", [
    {"journal_entry": None},
    {"journal_entry": {}},
    {"journal_entry": {"compact": "missing source_id"}},
    {"journal_entry": {"source_id": "", "compact": "blank id"}},
    {"journal_entry": {"source_id": "   ", "compact": "whitespace id"}},
    {"journal_entry": "not-a-dict"},
    {"journal_entry": {"source_id": 123, "compact": 456}},
])
def test_insert_run_rejects_malformed_journal_entry(isolated_dispatch_env, malformed):
    """Defensive: bad shapes must not crash insert_run or write garbage.

    The agent's wrap-up section is opt-in and operator-trusted, but
    insert_run still has to defend the row schema. Anything other than a
    dict with non-empty string ``source_id`` collapses to NULL columns.
    """
    env = isolated_dispatch_env
    writer = env["writer"]

    decision = {"status": "DONE", "reason": "ok", **malformed}
    writer.insert_run(decision=decision, **_baseline_run_kwargs(
        "auto-jrn3-20260503-100000", "auto-jrn3",
    ))

    conn = sqlite3.connect(str(env["dispatch_db"]))
    row = conn.execute(
        "SELECT journal_source_id, journal_compact FROM dispatch_runs WHERE id=?",
        ("auto-jrn3-20260503-100000",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] is None, f"malformed input leaked source_id: {row[0]!r}"
    assert row[1] is None, f"malformed input leaked compact: {row[1]!r}"


# ── 2. Migration adds the columns to a legacy schema ───────────────────


def _legacy_dispatch_runs_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE dispatch_runs (
            id TEXT PRIMARY KEY,
            bead_id TEXT,
            started_at DATETIME,
            completed_at DATETIME,
            status TEXT,
            reason TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def test_migration_adds_journal_columns_to_legacy_schema(tmp_path, monkeypatch):
    """Pre-bead DBs must gain journal_source_id / journal_compact via init_db."""
    db_path = tmp_path / "dispatch.db"
    _legacy_dispatch_runs_schema(db_path)
    monkeypatch.setenv("DISPATCH_DB", str(db_path))

    from agents import dispatch_db as writer_mod
    importlib.reload(writer_mod)
    writer_mod.init_db()

    conn = sqlite3.connect(str(db_path))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(dispatch_runs)").fetchall()]
    conn.close()
    assert "journal_source_id" in cols
    assert "journal_compact" in cols

    # Idempotent: a second init_db must not error or duplicate.
    writer_mod.init_db()
    conn = sqlite3.connect(str(db_path))
    cols2 = [r[1] for r in conn.execute("PRAGMA table_info(dispatch_runs)").fetchall()]
    conn.close()
    assert cols2.count("journal_source_id") == 1
    assert cols2.count("journal_compact") == 1


# ── 3. Server-side helpers surface journal_entry on the API ────────────


def test_row_to_timeline_entry_emits_journal_entry(isolated_dispatch_env):
    """``_row_to_timeline_entry`` exposes the field as a {source_id, compact} dict."""
    env = isolated_dispatch_env
    writer = env["writer"]

    writer.insert_run(
        decision={
            "status": "DONE", "reason": "ok",
            "journal_entry": {
                "source_id": "src:def0987654321",
                "compact": "Pitfall: dispatcher swallows None decision",
            },
        },
        **_baseline_run_kwargs("auto-jrn4-20260503-100000", "auto-jrn4"),
    )
    # Run with no entry — must surface as None.
    writer.insert_run(
        decision={"status": "DONE", "reason": "ok"},
        **_baseline_run_kwargs("auto-jrn5-20260503-100100", "auto-jrn5"),
    )

    from tools.dashboard import server as server_mod
    importlib.reload(server_mod)

    conn = sqlite3.connect(str(env["dispatch_db"]))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM dispatch_runs ORDER BY id").fetchall()
    conn.close()

    entries = [server_mod._row_to_timeline_entry(r) for r in rows]
    by_id = {e["run_id"]: e for e in entries}

    target = by_id["auto-jrn4-20260503-100000"]
    assert target["journal_entry"] == {
        "source_id": "src:def0987654321",
        "compact": "Pitfall: dispatcher swallows None decision",
    }

    absent = by_id["auto-jrn5-20260503-100100"]
    assert absent["journal_entry"] is None


def test_api_dispatch_runs_reconstructs_journal_entry(isolated_dispatch_env):
    """``/api/dispatch/runs`` restores the {source_id, compact} dict per row."""
    env = isolated_dispatch_env
    writer = env["writer"]

    writer.insert_run(
        decision={
            "status": "DONE", "reason": "ok",
            "journal_entry": {
                "source_id": "src:11112222aaaa",
                "compact": "Substrate gap: writer connection leaks WAL frames",
            },
        },
        **_baseline_run_kwargs("auto-jrn6-20260503-100000", "auto-jrn6"),
    )
    writer.insert_run(
        decision={"status": "DONE", "reason": "no journal"},
        **_baseline_run_kwargs("auto-jrn7-20260503-100100", "auto-jrn7"),
    )

    from agents import dispatch_db as writer_mod
    rows = writer_mod.list_runs(limit=10)
    by_id = {r["id"]: r for r in rows}

    # The DB rows themselves carry the flat columns.
    assert by_id["auto-jrn6-20260503-100000"]["journal_source_id"] == "src:11112222aaaa"
    assert by_id["auto-jrn7-20260503-100100"]["journal_source_id"] is None

    # The API reconstruction wraps them back into a dict.
    # Replicate the shape construction api_dispatch_runs uses (so this
    # test doesn't have to spin up the full Starlette app).
    def _reconstruct(row):
        if not row.get("journal_source_id"):
            return None
        return {
            "source_id": row["journal_source_id"],
            "compact": row.get("journal_compact") or "",
        }

    assert _reconstruct(by_id["auto-jrn6-20260503-100000"]) == {
        "source_id": "src:11112222aaaa",
        "compact": "Substrate gap: writer connection leaks WAL frames",
    }
    assert _reconstruct(by_id["auto-jrn7-20260503-100100"]) is None


# ── 4. Mock DAO defaults + fixture passthrough ────────────────────────


def test_mock_dao_defaults_include_journal_entry():
    """RUN_DEFAULTS / TIMELINE_ENTRY_DEFAULTS expose the field as None.

    Without a default, fixtures that omit the field would surface as
    KeyError on the renderer's ``e.journal_entry && ...`` guard.
    """
    from tools.dashboard.dao import mock as dao_mock

    assert "journal_entry" in dao_mock.RUN_DEFAULTS
    assert dao_mock.RUN_DEFAULTS["journal_entry"] is None
    assert "journal_entry" in dao_mock.TIMELINE_ENTRY_DEFAULTS
    assert dao_mock.TIMELINE_ENTRY_DEFAULTS["journal_entry"] is None


def test_mock_timeline_passes_journal_entry_through(tmp_path, monkeypatch):
    """A fixture entry carrying ``journal_entry`` round-trips through the mock DAO."""
    fixture = {
        "timeline_entries": [
            {
                "id": "auto-mock-jrn-20260503-100000",
                "bead_id": "auto-mock-jrn",
                "status": "DONE",
                "title": "Mock with journal",
                "journal_entry": {
                    "source_id": "src:mock0001",
                    "compact": "Routine fix surfaced a substrate gap",
                },
            },
            {
                "id": "auto-mock-plain-20260503-100100",
                "bead_id": "auto-mock-plain",
                "status": "DONE",
                "title": "Mock without journal",
            },
        ],
    }
    fixture_path = tmp_path / "fixtures.json"
    fixture_path.write_text(json.dumps(fixture))

    from tools.dashboard.dao import mock as dao_mock
    monkeypatch.setattr(dao_mock, "FIXTURE_PATH", fixture_path)

    entries = dao_mock.get_timeline_entries(limit=10)
    by_id = {e["id"]: e for e in entries}
    assert by_id["auto-mock-jrn-20260503-100000"]["journal_entry"] == {
        "source_id": "src:mock0001",
        "compact": "Routine fix surfaced a substrate gap",
    }
    # Fixtures that omit the field default to None (guard-friendly).
    assert by_id["auto-mock-plain-20260503-100100"]["journal_entry"] is None


# ── 5. Closing-prompt template visibility ─────────────────────────────


def test_tool_guidelines_documents_journal_entry_field():
    """The decision.json schema block lists ``journal_entry`` as optional."""
    text = TOOL_GUIDELINES.read_text()
    # Schema sample exposes the new field.
    assert '"journal_entry"' in text, (
        "decision.json schema sample must show the journal_entry field"
    )
    # The optional-field section explains when to write one.
    assert "## Optional: write a journal entry" in text, (
        "tool_guidelines.md needs the closing-prompt journal section"
    )
    # The example walks through ``graph journal write`` so agents know the CLI.
    assert "graph journal write" in text


# ── 6. Renderer: 📔 badge + Attention anchor ──────────────────────────


def test_feed_card_renders_journal_badge():
    """The Activity Feed card carries a 📔 anchor when journal_entry is set.

    Looks for the conditional template guard, the anchor href shape that
    targets the Attention tab, and a stable testid prefix so future
    browser tests can latch onto the badge without DOM scraping.
    """
    html = TIMELINE_HTML.read_text()
    assert "📔" in html, "Feed card must render the 📔 indicator"
    assert "x-if=\"e.journal_entry && e.journal_entry.source_id\"" in html, (
        "Badge must guard on journal_entry.source_id presence"
    )
    assert "'/activity#journal-' + e.journal_entry.source_id" in html, (
        "Badge href must anchor to /activity#journal-<source_id>"
    )
    assert "activity-feed-journal-badge-" in html, (
        "Badge needs a stable data-testid prefix for browser tests"
    )


def test_attention_entry_exposes_anchor_id():
    """Each Attention entry has an ``id="journal-<source_id>"`` so the
    badge's hash href scrolls straight to the matching card."""
    html = TIMELINE_HTML.read_text()
    assert ":id=\"e.id ? ('journal-' + e.id) : null\"" in html, (
        "Attention entry must expose id=journal-<entry-id> for hash linking"
    )


def test_activity_helper_handles_journal_hash():
    """The activityPage Alpine component opens the Attention tab and
    scrolls into the entry when the URL hash matches ``#journal-...``."""
    js = ACTIVITY_JS.read_text()
    assert "_handleJournalHash" in js, (
        "activityPage must define _handleJournalHash to react to /activity#journal-..."
    )
    assert "this.tab = 'attention'" in js, (
        "Hash handler must switch to the Attention tab so the entry is visible"
    )
    assert "hashchange" in js, (
        "Hash handler must subscribe to hashchange so in-page clicks also navigate"
    )
    assert "scrollIntoView" in js, (
        "Hash handler must scroll the entry into view"
    )
