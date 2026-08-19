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


@pytest.mark.parametrize("mock_mode", [False, True], ids=["real", "mock"])
def test_startup_refuses_a_corrupt_personal_store(
    test_db, mock_tmux, tmp_path, monkeypatch, mock_mode,
):
    """Asserts the PROPAGATION, not a log line: the lifespan must raise,
    not continue — and must raise FROM THE DELIBERATE GATE in BOTH startup
    modes. The mock branch is where review caught the gate dead: it
    started the worktree monitor (whose first sweep resolves workspaces →
    orgs → the personal store) and returned before bootstrap ever ran, so
    a corrupt store died inside a monitor broadcast with a raw traceback
    instead of the gate's operator message.

    The two modes gate differently by design: real mode provisions
    (ensure_bootstrap_orgs) and the raise surfaces there; mock mode serves
    fixtures and must not materialize real stores, so its gate is a
    read-only resolution probe of each local store — same refusal, zero
    writes."""
    if mock_mode:
        monkeypatch.setenv("DASHBOARD_MOCK", "1")
    else:
        monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
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
    gate_frame = (
        "_local_store_db_path" if mock_mode else "ensure_bootstrap_orgs"
    )
    # Adjacency, not membership: the monitor's own first sweep ALSO passes
    # through the resolver, so "_local_store_db_path somewhere in the
    # traceback" would be satisfied by the exact incidental crash the gate
    # exists to preempt. The deliberate gate is the one _on_startup calls
    # directly.
    call_pairs = list(zip(frames, frames[1:]))
    assert ("_on_startup", gate_frame) in call_pairs, (
        f"startup died somewhere else, not at the {gate_frame} gate: {frames}"
    )
    if mock_mode:
        # The mock gate must be the probe, not provisioning: a corrupt
        # legacy file refused with nothing created beside it.
        assert not (tmp_path / "personal.db").exists(), (
            "mock startup materialized a store"
        )
        assert "ensure_bootstrap_orgs" not in frames
