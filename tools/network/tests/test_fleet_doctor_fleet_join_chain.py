"""The fleet:join invite-chain reset finds the publication where it is
recorded: the machine-homed link-operation journal (every publish since
auto-fkhq0.10a, the operator's own and an approved Central request alike),
not the approval queue, which no fleet:join publish has written since
2026-09-28."""

from __future__ import annotations

import time

import pytest

from tools.dashboard import link_operations
from tools.graph.db import GraphDB
from tools.network import fleet_doctor


@pytest.fixture
def machine_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("machine", type_="personal", path=tmp_path / "orgs" / "machine.db").close()
    yield
    GraphDB.close_all_pooled()


def test_a_journal_recorded_publication_is_found_and_reported(machine_store, capsys):
    link_operations.Journal.put("op-fleet", {
        "state": "done", "op": "publish", "initiator": "operator",
        "prepared_at": time.time() - 30, "finished_at": time.time(),
        "request": {"org": "personal", "target_type": "fleet:join",
                    "target_uuid": "11111111-1111-4111-8111-111111111111",
                    "meta": {"ttl": 604800}},
        "staged": {}, "execution": {"ok": True, "url": "https://relay/l/x", "token": "ab" * 16},
    })
    link_operations.Journal.put("op-note", {
        "state": "done", "op": "publish", "initiator": "operator",
        "prepared_at": time.time() - 20,
        "request": {"org": "personal", "target_type": "note", "target_uuid": "n1"},
        "staged": {}, "execution": {"ok": True},
    })

    found = fleet_doctor._find_stale_fleet_join_state()

    assert [item["id"] for item in found["link_operations"]] == ["op-fleet"]
    assert found["link_operations"][0]["target_uuid"] == "11111111-1111-4111-8111-111111111111"
    assert found["link_operations"][0]["state"] == "done"
    assert "approval_requests" not in found

    # The dry run names it and removes nothing.
    fleet_doctor.clear_stale_fleet_join(dry_run=True)
    out = capsys.readouterr().out
    assert "link-operation journal entry" in out and "op-fleet" in out
    assert link_operations.Journal.get("op-fleet") is not None
