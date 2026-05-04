"""``_auto_provenance`` and ``cmd_bead`` host-mode refresh now run in-process.

Pre auto-qjme3, both call sites spawned ``graph sessions --all`` as a fresh
subprocess. Post auto-lq20j (API-first), that child inherited the default
``HttpClient`` and made a full HTTPS round-trip to the dashboard — adding
~7-13s to every ``--force-host`` write. The fix replaces the subprocess with
:func:`tools.graph.cli._auto_ingest`, which:

  * no-ops when ``isinstance(get_client(), HttpClient)`` (HTTP mode);
  * calls :func:`ingest_all_claude_code` directly otherwise — same DB the
    parent already opened, no spawn cost, no wrong-mode child.

These tests pin the contract: no ``graph`` subprocess gets fired, and the
in-process refresh hook is invoked with the parent's DB handle.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tools.graph import cli as graph_cli
from tools.graph import client as _client_mod


def test_auto_provenance_does_not_spawn_subprocess(monkeypatch):
    """``_auto_provenance`` must not shell out to ``graph sessions --all``.

    The whole point of auto-qjme3 is that the refresh stays in-process.
    A spy on ``subprocess.run`` proves no child gets fired.
    """
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", True)

    # Stub the in-process refresh so the test doesn't actually scan JSONL.
    auto_ingest_calls: list = []
    monkeypatch.setattr(
        graph_cli, "_auto_ingest",
        lambda db: auto_ingest_calls.append(db),
    )
    monkeypatch.setattr(graph_cli, "_resolve_current_source", lambda db: None)

    db_sentinel = MagicMock(name="db")
    with patch("tools.graph.cli.subprocess.run") as run_spy:
        graph_cli._auto_provenance(db_sentinel)

    assert run_spy.call_count == 0, (
        "_auto_provenance must not spawn a subprocess "
        f"(got {run_spy.call_args_list!r})"
    )
    assert auto_ingest_calls == [db_sentinel], (
        "_auto_provenance must call _auto_ingest with the caller's db handle"
    )


def test_auto_ingest_no_ops_under_http_client(monkeypatch):
    """Belt-and-suspenders: even if some future caller invoked
    ``_auto_provenance`` while the client is HTTP, ``_auto_ingest`` must
    short-circuit — no JSONL scan, no ingest, no surprises."""
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", False)

    ingest_calls: list = []
    monkeypatch.setattr(
        "tools.graph.cli.ingest_all_claude_code",
        lambda *a, **kw: ingest_calls.append((a, kw)),
    )

    db_sentinel = MagicMock(name="db")
    graph_cli._auto_ingest(db_sentinel)

    assert ingest_calls == [], (
        "_auto_ingest must no-op under HttpClient (got "
        f"{ingest_calls!r})"
    )


def test_auto_ingest_calls_ingest_in_force_host_mode(monkeypatch):
    """In ``--force-host`` mode (or under the test conftest's pin),
    ``_auto_ingest`` must drive ingestion in-process — not via subprocess."""
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", True)
    monkeypatch.delenv("GRAPH_DB", raising=False)

    ingest_calls: list = []
    monkeypatch.setattr(
        "tools.graph.cli.ingest_all_claude_code",
        lambda *a, **kw: ingest_calls.append((a, kw)),
    )

    db_sentinel = MagicMock(name="db")
    with patch("tools.graph.cli.subprocess.run") as run_spy:
        graph_cli._auto_ingest(db_sentinel)

    assert run_spy.call_count == 0, "no subprocess in host-mode refresh either"
    assert len(ingest_calls) == 1, (
        f"expected exactly one ingest_all_claude_code call, got {ingest_calls!r}"
    )
    args, kwargs = ingest_calls[0]
    # Per-session routing: when GRAPH_DB unset, sessions_db is None.
    assert args[0] is None
    assert kwargs.get("force") is False
