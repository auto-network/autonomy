"""A dashboard never starts on a personal store it cannot read.

The fatal-stop is not about migration: with the automated mover deleted,
the resolver still raises LocalStoreUnreadableError for a corrupt file at
the legacy path, and swallowing that in the generic resilient catch would
start a dashboard whose every later personal-store resolution raises for
the life of the process. Damage is not absence — absence is served (the
resolver answers the real home), damage stops startup and demands the
operator inspect or restore.
"""

from __future__ import annotations

import importlib

import pytest
from starlette.testclient import TestClient

from tools.data_paths import LocalStoreUnreadableError


def test_startup_refuses_a_corrupt_personal_store(
    test_db, mock_tmux, tmp_path, monkeypatch,
):
    """Asserts the PROPAGATION, not a log line: the lifespan must raise,
    not continue. This test fails on 4fdf7b08, where the deletion of the
    mover's error plumbing also removed the one fatal-stop whose raiser —
    the resolver — deliberately survives."""
    monkeypatch.setenv("DASHBOARD_DB", test_db)
    monkeypatch.setenv(
        "DASHBOARD_EVENT_BUS_STATE", str(tmp_path / "event_bus.state"),
    )
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    (orgs_dir / "personal.db").write_bytes(b"garbage: not a sqlite database")
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))

    from tools import data_paths
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()

    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from tools.dashboard import server
    importlib.reload(server)

    with pytest.raises(LocalStoreUnreadableError) as caught:
        with TestClient(server.app):
            pass

    # The raise must come from the DELIBERATE bootstrap gate, not from
    # whichever later startup step happens to be unguarded today — an
    # incidental propagation also fails startup on the broken commit, and
    # a test satisfied by it would certify an accident as a design.
    import traceback

    frames = [
        frame.name
        for frame in traceback.extract_tb(caught.value.__traceback__)
    ]
    assert "ensure_bootstrap_orgs" in frames, (
        f"startup died somewhere else, not at the bootstrap gate: {frames}"
    )
