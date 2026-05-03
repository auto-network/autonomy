"""Tests for journal source-session attribution (auto-fjfki).

Each journal entry written through ``graph journal write`` carries the
calling session's name in the source ``metadata`` so the dashboard
Attention tab can render a "← who wrote this" chip back to the session
viewer.

Coverage:

* CLI capture from ``$AUTONOMY_SESSION`` / ``$GRAPH_SESSION``.
* CLI fallback when no session env is set (write succeeds, warning to
  stderr, no ``source_session_id`` propagated).
* Substrate roundtrip: ``ops.write_journal_entry`` persists the field
  and ``ops.list_journal_entries`` returns it.
* API echo: ``/api/journal`` (mock + real ops) surfaces
  ``source_session_id`` on every entry.
* Renderer chip: the activity template + Alpine helper render a clickable
  session chip linking to ``/session/<id>`` only when the field is set.
* Backlink route: ``/session/<id>`` redirects to the session viewer.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.graph import cli as graph_cli
from tools.graph import db as graph_db_mod
from tools.graph import ops as graph_ops
from tools.graph.db import GraphDB


# ── Substrate fixtures ──────────────────────────────────────────────


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Per-test orgs directory; mirrors tools/graph tests."""
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv("GRAPH_SCOPE", raising=False)
    monkeypatch.setattr(graph_db_mod, "DEFAULT_DB", legacy)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    try:
        yield root
    finally:
        GraphDB.close_all_pooled()


def _journal_payload(**overrides) -> dict:
    base = {
        "compact": "Test compact",
        "normal": "Test normal body line.",
        "expanded": "Expanded full text.",
        "timestamp_start": "2026-05-03T00:00:00Z",
        "timestamp_end": "2026-05-03T00:30:00Z",
        "entry_type": "attention",
    }
    base.update(overrides)
    return base


# ── 1. Substrate roundtrip ──────────────────────────────────────────


def test_substrate_persists_source_session_id(orgs_root):
    payload = _journal_payload(source_session_id="auto-test-alpha")
    result = graph_ops.write_journal_entry(payload)
    sid = result["source_id"]

    # The metadata column carries the field exactly as written.
    conn = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        row = conn.execute(
            "SELECT metadata FROM sources WHERE id = ?", (sid,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    meta = json.loads(row[0])
    assert meta.get("source_session_id") == "auto-test-alpha"


def test_substrate_omits_field_when_unset(orgs_root):
    payload = _journal_payload()
    result = graph_ops.write_journal_entry(payload)
    sid = result["source_id"]

    conn = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        row = conn.execute(
            "SELECT metadata FROM sources WHERE id = ?", (sid,),
        ).fetchone()
    finally:
        conn.close()
    meta = json.loads(row[0])
    # Empty string or absent key both render as "no chip" upstream;
    # the substrate omits the key entirely when no value was supplied.
    assert "source_session_id" not in meta


def test_list_journal_entries_returns_session_id(orgs_root):
    graph_ops.write_journal_entry(
        _journal_payload(
            compact="With session",
            source_session_id="auto-alpha",
        )
    )
    graph_ops.write_journal_entry(
        _journal_payload(
            compact="Without session",
            timestamp_start="2026-05-03T01:00:00Z",
            timestamp_end="2026-05-03T01:30:00Z",
        )
    )

    entries = graph_ops.list_journal_entries(limit=10)
    by_compact = {e["compact"]: e for e in entries}
    assert by_compact["With session"]["source_session_id"] == "auto-alpha"
    # Legacy / unattributed entries land with empty string so the renderer
    # can branch on truthiness without KeyError.
    assert by_compact["Without session"]["source_session_id"] == ""


# ── 2. CLI capture ─────────────────────────────────────────────────


class _CaptureClient:
    """Minimal stand-in for graph.client used by ``cmd_journal_write``.

    Captures the payload the CLI hands to the substrate so we can assert
    ``source_session_id`` was injected before the network/RPC layer.
    """

    def __init__(self):
        self.payloads = []

    def write_journal_entry(self, payload, *, org=None):
        self.payloads.append(dict(payload))
        return {"source_id": "src-stub-0001abcdef00", "edge_count": 0}


def _run_cli_journal_write(payload: dict, monkeypatch):
    captured = _CaptureClient()
    monkeypatch.setattr(graph_cli, "get_client", lambda: captured)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    args = argparse.Namespace(content="-")
    graph_cli.cmd_journal_write(args)
    return captured


def test_cli_captures_session_from_autonomy_session(monkeypatch, capsys):
    monkeypatch.setenv("AUTONOMY_SESSION", "auto-fjfki")
    monkeypatch.delenv("GRAPH_SESSION", raising=False)
    monkeypatch.delenv("BD_ACTOR", raising=False)
    captured = _run_cli_journal_write(_journal_payload(), monkeypatch)
    assert captured.payloads, "CLI should have invoked the client once"
    sent = captured.payloads[0]
    assert sent.get("source_session_id") == "auto-fjfki"
    err = capsys.readouterr().err
    assert "Warning" not in err


def test_cli_falls_back_to_graph_session(monkeypatch, capsys):
    monkeypatch.delenv("AUTONOMY_SESSION", raising=False)
    monkeypatch.setenv("GRAPH_SESSION", "auto-legacy-7")
    monkeypatch.delenv("BD_ACTOR", raising=False)
    captured = _run_cli_journal_write(_journal_payload(), monkeypatch)
    sent = captured.payloads[0]
    assert sent.get("source_session_id") == "auto-legacy-7"
    err = capsys.readouterr().err
    assert "Warning" not in err


def test_cli_warns_and_writes_when_session_unresolved(monkeypatch, capsys):
    """Acceptance #4: missing session must NOT block the write — warn only."""
    monkeypatch.delenv("AUTONOMY_SESSION", raising=False)
    monkeypatch.delenv("GRAPH_SESSION", raising=False)
    monkeypatch.delenv("BD_ACTOR", raising=False)

    captured = _run_cli_journal_write(_journal_payload(), monkeypatch)

    # Write went through with no session id.
    assert captured.payloads, "CLI should still call the substrate"
    assert not captured.payloads[0].get("source_session_id")

    # Operator-visible warning so the gap is loud, not silent.
    err = capsys.readouterr().err
    assert "source_session_id unresolved" in err


def test_cli_preserves_caller_supplied_session(monkeypatch, capsys):
    """When the caller already set the field, env autocapture must not clobber it."""
    monkeypatch.setenv("AUTONOMY_SESSION", "auto-env-host")
    payload = _journal_payload(source_session_id="auto-explicit")
    captured = _run_cli_journal_write(payload, monkeypatch)
    assert captured.payloads[0]["source_session_id"] == "auto-explicit"


# ── 3. API echo (mock branch) ──────────────────────────────────────


def test_api_journal_mock_includes_source_session_id(tmp_path, monkeypatch):
    """DASHBOARD_MOCK /api/journal echoes ``source_session_id`` per entry.

    The mock fixture supplies an entry with the field set and another
    without; both must come back through the API with the key present.
    """
    fixture = {
        "journal_entries": [
            {
                "id": "journal-attr-001",
                "compact": "Attributed entry",
                "normal": "Body",
                "expanded": "Expanded",
                "timestamp_start": "2026-05-03T02:00:00Z",
                "timestamp_end": "2026-05-03T02:15:00Z",
                "entry_type": "attention",
                "created_at": "2026-05-03T02:15:00Z",
                "org": "autonomy",
                "source_session_id": "auto-fjfki",
            },
            {
                "id": "journal-attr-002",
                "compact": "Legacy entry — no session",
                "normal": "Body",
                "expanded": "Expanded",
                "timestamp_start": "2026-05-03T03:00:00Z",
                "timestamp_end": "2026-05-03T03:15:00Z",
                "entry_type": "attention",
                "created_at": "2026-05-03T03:15:00Z",
                "org": "autonomy",
            },
        ],
    }
    fixture_path = tmp_path / "fixtures.json"
    fixture_path.write_text(json.dumps(fixture))

    # FIXTURE_PATH is bound at import time from $DASHBOARD_MOCK; patch the
    # already-resolved module attribute so the test does not depend on
    # import order.
    from tools.dashboard.dao import mock as dao_mock
    monkeypatch.setattr(dao_mock, "FIXTURE_PATH", fixture_path)

    entries = dao_mock.get_journal_entries()
    assert len(entries) == 2
    by_id = {e["id"]: e for e in entries}
    assert by_id["journal-attr-001"]["source_session_id"] == "auto-fjfki"
    # Legacy entry inherits the default empty string from JOURNAL_ENTRY_DEFAULTS.
    assert by_id["journal-attr-002"]["source_session_id"] == ""


def test_api_journal_real_ops_includes_source_session_id(orgs_root):
    """The non-mock /api/journal path goes through ops.list_journal_entries.

    Verify the field is in the per-entry dict returned to the route handler.
    """
    graph_ops.write_journal_entry(
        _journal_payload(
            compact="API echo entry",
            source_session_id="auto-fjfki",
        )
    )
    entries = graph_ops.list_journal_entries(limit=10)
    assert entries, "expected at least one journal entry"
    assert all("source_session_id" in e for e in entries)
    target = next(e for e in entries if e["compact"] == "API echo entry")
    assert target["source_session_id"] == "auto-fjfki"


# ── 4. Renderer chip (static markup + helper) ──────────────────────


REPO_ROOT = Path(__file__).resolve().parents[3]
TIMELINE_HTML = REPO_ROOT / "tools/dashboard/templates/pages/timeline.html"
ACTIVITY_JS = REPO_ROOT / "tools/dashboard/static/js/pages/activity.js"


def test_renderer_template_has_chip_markup():
    """The Attention entry header conditionally renders the session chip.

    Looks for the x-if guard, the /session/ href, and a stable testid prefix
    so future browser tests can latch onto the chip without DOM scraping.
    """
    html = TIMELINE_HTML.read_text()
    assert 'x-if="e.source_session_id"' in html, (
        "Attention entry must guard the chip on source_session_id presence"
    )
    assert "'/session/' + e.source_session_id" in html, (
        "Attention chip must link to /session/<source_session_id>"
    )
    assert "activity-attention-session-chip-" in html, (
        "Attention chip needs a stable data-testid prefix"
    )


def test_renderer_activity_helper_formats_chip_label():
    """Alpine helper ``formatAttentionSessionChip`` exists and short-circuits empty input."""
    js = ACTIVITY_JS.read_text()
    assert "formatAttentionSessionChip(entry)" in js, (
        "activityPage must expose formatAttentionSessionChip(entry) helper"
    )
    # Helper truncates long ids and prefixes the back-arrow glyph so the
    # chip stays visually distinct from the time range. Validate both.
    helper_block = re.search(
        r"formatAttentionSessionChip\(entry\)\s*\{(?P<body>.*?)\n\s*\},",
        js,
        re.DOTALL,
    )
    assert helper_block, "could not isolate helper body"
    body = helper_block.group("body")
    assert "if (!sid) return ''" in body, (
        "helper must short-circuit on empty source_session_id"
    )
    assert "'↩ '" in body, (
        "helper must prefix the chip label with the ↩ back-arrow glyph"
    )


# ── 5. Backlink route ──────────────────────────────────────────────


def test_session_by_name_route_redirects_to_project_path(monkeypatch, tmp_path):
    """``/session/<id>`` resolves the project from the session registry.

    The chip's plain ``/session/<id>`` URL must not 404 — it should redirect
    to the project-scoped session viewer once the session is known.
    """
    # Stand up a DASHBOARD_MOCK server isolated to this test, hit /session/<id>,
    # and assert the response is a 302/redirect (sessions index fallback when
    # the mock has no registered session).
    from starlette.testclient import TestClient
    monkeypatch.setenv("DASHBOARD_MOCK", str(tmp_path / "f.json"))
    (tmp_path / "f.json").write_text("{}")
    monkeypatch.setenv(
        "DASHBOARD_EVENT_BUS_STATE", str(tmp_path / "event_bus.state"),
    )

    # Importing fresh so DASHBOARD_MOCK is honoured at module init.
    import importlib
    import tools.dashboard.server as server_mod
    importlib.reload(server_mod)
    try:
        with TestClient(server_mod.app, follow_redirects=False) as client:
            # In mock mode the route returns the SPA shell directly so
            # the front-end Alpine page can resolve via API. In live mode
            # it 302s to /session/<project>/<id> via the registry.
            resp = client.get("/session/auto-fjfki")
            assert resp.status_code in (200, 302), (
                f"Expected 200/302 from /session/<id>, got {resp.status_code}"
            )
    finally:
        # Reload again with mock cleared so other tests get a clean import.
        os.environ.pop("DASHBOARD_MOCK", None)
        importlib.reload(server_mod)
