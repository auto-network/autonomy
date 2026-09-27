"""auto-2v6ay.2 merge 2: session tokens and dispatch targets resolve the
caller's or asset's org, else personal -- never a literal org (D5)."""
from __future__ import annotations

from types import SimpleNamespace

from agents.dispatcher import bead_org
from tools.dashboard import server


def test_bead_org_reads_the_org_label():
    assert bead_org(["dashboard", "org:boatlore", "p1"]) == "boatlore"
    assert bead_org(["dashboard"]) is None
    assert bead_org(None) is None
    assert bead_org(["org:"]) is None


def test_generic_container_org_is_the_requested_one_else_personal():
    assert server._container_session_org({"org": "boatlore"}) == "boatlore"
    assert server._container_session_org({"register_project": "boatlore"}) == "boatlore"
    assert server._container_session_org({}) == "personal"
    assert server._container_session_org({"register_project": "host"}) == "personal"


def test_asset_dispatch_org_prefers_the_asset_then_the_caller_then_personal():
    bound = SimpleNamespace(org_bound=True, org="boatlore")
    operator = SimpleNamespace(org_bound=False, org="")
    assert server._asset_dispatch_org({"org": "anchore"}, None, bound) == "anchore"
    assert server._asset_dispatch_org(None, {"labels": ["org:anchore"]}, bound) == "anchore"
    assert server._asset_dispatch_org({"org": ""}, None, bound) == "boatlore"
    assert server._asset_dispatch_org(None, {"labels": []}, operator) == "personal"


def test_inline_dispatch_from_an_operator_with_no_org_targets_personal():
    operator = SimpleNamespace(org_bound=False, org="")
    assert server._inline_dispatch_target_org({}, operator) == "personal"
    assert server._inline_dispatch_target_org({"target_org": "boatlore"}, operator) == "boatlore"


# ── reviews of fa4023c7 / f599e288: only an org-exclusive tracker vouches ──

import json

import pytest


@pytest.fixture
def trackers(tmp_path, monkeypatch):
    """A DATA_ROOT with the shared tracker (database "auto") and org dirs:
    anchore on its own database, autonomy naming the shared one (as on Home).
    """
    from agents import dispatcher

    def tracker(path, database):
        path.mkdir(parents=True, exist_ok=True)
        (path / "metadata.json").write_text(json.dumps({"dolt_database": database}))
        return path

    tracker(tmp_path / ".beads", "auto")
    orgs = tmp_path / ".beads" / "orgs"
    dirs = {"anchore": tracker(orgs / "anchore", "anchore"),
            "autonomy": tracker(orgs / "autonomy", "auto")}
    monkeypatch.setattr(dispatcher, "DATA_ROOT", tmp_path)
    return dirs


def test_only_a_tracker_with_its_own_database_is_exclusive(trackers):
    from agents import dispatcher
    assert set(dispatcher._exclusive_bead_trackers()) == {"anchore"}


def test_a_bead_in_an_exclusive_tracker_runs_as_that_org_whatever_its_label(trackers):
    from agents import dispatcher
    bead = {"id": "anc-1", "labels": ["org:autonomy"], "_tracker_org": "anchore"}
    assert dispatcher.dispatch_org_for_bead(bead) == "anchore"


def test_shared_database_beads_use_their_label_unless_that_org_has_its_own(trackers):
    from agents import dispatcher
    shared = {"_tracker_org": None}
    assert dispatcher.dispatch_org_for_bead({**shared, "labels": ["org:autonomy"]}) == "autonomy"
    assert dispatcher.dispatch_org_for_bead({**shared, "labels": ["org:boatlore"]}) == "boatlore"
    assert dispatcher.dispatch_org_for_bead({**shared, "labels": ["org:anchore"]}) == "personal"
    assert dispatcher.dispatch_org_for_bead({**shared, "labels": []}) == "personal"


def test_an_org_dir_naming_the_shared_database_does_not_vouch(trackers, monkeypatch):
    """On Home orgs/autonomy/ names "auto", which also holds other orgs'
    beads: an org-Y bead there must run as Y, never as autonomy."""
    from agents import dispatcher
    by_tracker = {trackers["anchore"]: [{"id": "anc-1", "labels": ["org:anchore"]}],
                  trackers["autonomy"]: [{"id": "auto-9", "labels": ["org:boatlore"]},
                                         {"id": "auto-1", "labels": ["org:autonomy"]}],
                  None: [{"id": "auto-9", "labels": ["org:boatlore"]},
                         {"id": "auto-1", "labels": ["org:autonomy"]}]}
    monkeypatch.setattr(dispatcher, "run_bd",
                        lambda args, beads_dir=None: json.dumps(by_tracker[beads_dir]))
    beads = {b["id"]: b for b in dispatcher.get_ready_beads()}
    assert {i: b["_tracker_org"] for i, b in beads.items()} == {
        "anc-1": "anchore", "auto-9": None, "auto-1": None}
    assert dispatcher.dispatch_org_for_bead(beads["auto-9"]) == "boatlore"
    assert dispatcher.dispatch_org_for_bead(beads["auto-1"]) == "autonomy"
    assert dispatcher.dispatch_org_for_bead(beads["anc-1"]) == "anchore"


def test_a_database_named_by_two_org_dirs_is_nobodys_alone(trackers, tmp_path):
    from agents import dispatcher
    twin = tmp_path / ".beads" / "orgs" / "twin"
    twin.mkdir()
    (twin / "metadata.json").write_text(json.dumps({"dolt_database": "anchore"}))
    assert dispatcher._exclusive_bead_trackers() == {}
