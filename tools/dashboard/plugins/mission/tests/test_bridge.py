"""The bead bridge (bridge.py): membership, mapping, ladder, epics,
the summary/detail split, and containment when bd is absent or empty.

``_bd`` is stubbed with canned bd JSON; the live path is exercised by
the CLI coverage verb and, at cutover, the real mission render.
"""
from __future__ import annotations

from tools.dashboard.plugins.mission import bridge

MID = "eec0efa1-fba7-42bf-8fd9-4790166b8f59"
PILLARS = [
    {"pillar_id": "relay", "bead_labels": ["pillar:relay-network"]},
    {"pillar_id": "platform", "bead_labels": ["pillar:platform-core"]},
    {"pillar_id": "empty", "bead_labels": []},
]


def _stub(monkeypatch, lists, shows, comments=None):
    calls = []

    def fake(args, org=None):
        calls.append(args)
        if args[0] == "list":
            return lists
        if args[0] == "show":
            return shows
        if args[0] == "comments":
            return (comments or {}).get(args[1], [])
        raise AssertionError(args)
    monkeypatch.setattr(bridge, "_bd", fake)
    return calls


def _row(bid, **kw):
    base = {"id": bid, "title": bid + " title", "status": "open",
            "labels": [f"mission:{MID}", "pillar:relay-network"],
            "description": "spec text", "comment_count": 0}
    base.update(kw)
    return base


def test_mapping_ladder_epic_and_deps(monkeypatch):
    rows = [
        _row("a-open"),
        _row("a-spec", acceptance_criteria="must do X"),
        _row("a-run", status="in_progress"),
        _row("a-done", status="closed", close_reason="landed abc123",
             comment_count=2),
        _row("a-epic", issue_type="epic",
             labels=[f"mission:{MID}", "pillar:platform-core"],
             dependencies=[{"id": "a-run", "dependency_type": "parent-child"}]),
        _row("a-stray", labels=[f"mission:{MID}", "pillar:unmapped"]),
    ]
    calls = _stub(monkeypatch, rows, rows)
    out = bridge.load_beads(MID, PILLARS)
    relay = {b["id"]: b for b in out["relay"]}
    assert relay["a-open"]["state"] == "defined"
    assert relay["a-spec"]["state"] == "specified"
    assert relay["a-run"]["state"] == "running"
    assert relay["a-done"]["state"] == "complete"
    epic = out["platform"][0]
    assert epic["epic"] is True and epic["deps"] == ["a-run"]
    # unmapped pillar labels are excluded, not misfiled
    assert all("a-stray" != b["id"] for bl in out.values() for b in bl)
    assert "empty" not in out
    # the SCREEN shape: summary only — no description, no close reason,
    # no comments; the count says whether a detail fetch has anything
    assert relay["a-done"]["comment_count"] == 2
    assert "comment_count" not in relay["a-open"]
    for b in relay.values():
        assert not {"desc", "evidence", "comments"} & set(b)
    # ONE bd invocation for the whole screen: the list. No show, no
    # per-bead comments — that was the seven-second render.
    assert [c[0] for c in calls] == ["list"]


def test_detail_batches_show_and_comments(monkeypatch):
    rows = [_row("a-c", comment_count=1, description="spec text"),
            _row("a-quiet"),
            _row("a-done", status="closed", close_reason="landed abc123"),
            _row("a-foreign", labels=["mission:other", "pillar:relay-network"])]
    calls = _stub(monkeypatch, [], rows, comments={
        "a-c": [{"author": "terminal:auto-1", "created_at": "2026-08-24T00:00:00Z",
                 "text": "clarified in chat"}]})
    out = bridge.load_task_detail(
        MID, ["a-c", "a-quiet", "a-c", " ", "a-done", "a-foreign"])
    assert out["a-c"]["desc"] == "spec text"
    assert out["a-c"]["comments"][0]["by"] == "terminal:auto-1"
    assert out["a-quiet"]["comments"] == [] and out["a-quiet"]["evidence"] == ""
    assert out["a-done"]["evidence"] == "landed abc123"
    # a bead outside the mission is not served through it
    assert "a-foreign" not in out
    # one show for the batch (ids deduped, blanks dropped), and exactly
    # one comments invocation — the quiet beads cost nothing
    shows = [c for c in calls if c[0] == "show"]
    assert shows == [["show", "a-c", "a-quiet", "a-done", "a-foreign"]]
    assert sum(1 for c in calls if c[0] == "comments") == 1


def test_detail_is_bounded_and_empty_safe(monkeypatch):
    calls = _stub(monkeypatch, [], [])
    assert bridge.load_task_detail(MID, []) == {}
    assert bridge.load_task_detail(MID, ["", "  "]) == {}
    assert calls == []
    ids = [f"b-{i}" for i in range(bridge.DETAIL_LIMIT + 10)]
    bridge.load_task_detail(MID, ids)
    assert len(calls[-1]) - 1 == bridge.DETAIL_LIMIT


def test_no_labels_or_no_beads_short_circuits(monkeypatch):
    calls = _stub(monkeypatch, [], [])
    assert bridge.load_beads(MID, [{"pillar_id": "x", "bead_labels": []}]) == {}
    assert calls == []          # no bd invocation without a mapping
    assert bridge.load_beads(MID, PILLARS) == {}


def test_bd_failure_is_contained(monkeypatch):
    monkeypatch.setattr(bridge, "_bd", lambda args, org=None: None)
    assert bridge.load_beads(MID, PILLARS) == {}
    assert bridge.load_task_detail(MID, ["a-c"]) == {}


def test_beads_env_routes_to_org_tracker(monkeypatch, tmp_path):
    """A mission org with a provisioned tracker dir gets BEADS_DIR (and
    that tracker's SQL credentials) pointed at it; anyone else inherits
    the ambient tracker."""
    import tools.data_paths as data_paths
    monkeypatch.setattr(data_paths, "DATA_ROOT", tmp_path)
    org_dir = tmp_path / ".beads" / "orgs" / "anchore"
    org_dir.mkdir(parents=True)
    (org_dir / "metadata.json").write_text("{}")
    (org_dir / "credentials.env").write_text(
        "BEADS_DOLT_SERVER_USER=beads_anchore\nBEADS_DOLT_PASSWORD=pw\n")
    shared = tmp_path / ".beads"
    (shared / "credentials.env").write_text(
        "BEADS_DOLT_SERVER_USER=beads_autonomy\nBEADS_DOLT_PASSWORD=pa\n")
    env = bridge._beads_env("anchore")
    assert env["BEADS_DIR"] == str(org_dir)
    assert env["BEADS_DOLT_SERVER_USER"] == "beads_anchore"
    # shared-tracker orgs still authenticate — with the shared creds
    env = bridge._beads_env("autonomy")
    assert "BEADS_DIR" not in {
        k: v for k, v in env.items() if v == str(org_dir)}
    assert env["BEADS_DOLT_SERVER_USER"] == "beads_autonomy"
    assert bridge._beads_env(None)["BEADS_DOLT_SERVER_USER"] == "beads_autonomy"
