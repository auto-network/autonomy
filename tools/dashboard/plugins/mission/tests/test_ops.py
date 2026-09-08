"""Ops contracts: source failure is not an empty or execution-ready queue."""
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from tools.dashboard.plugins.mission import ops
from tools.dashboard.plugins.mission.entrypoints import api, schemas
from tools.graph.schemas.registry import SchemaValidationError


def fixture_sources(monkeypatch, rows=None, extra=None, edges=None, sessions=None):
    rows = rows if rows is not None else [{"id": "a", "status": "open", "labels": []}]
    extra = extra if extra is not None else [{"id": "a", "has_design": 1}]
    edges = edges if edges is not None else []
    def read(args, org=None):
        assert org == "autonomy"
        if args[0] == "list":
            assert args[-2:] == ["--limit", "0"]
            return rows
        return edges if "FROM dependencies" in args[1] else extra
    monkeypatch.setattr(ops.bridge, "_bd", read)
    monkeypatch.setattr(ops.compose, "_read", lambda *a: [
        SimpleNamespace(key="m", payload={"name": "This mission"}),
        SimpleNamespace(key="other", payload={"name": "Other mission"})])
    monkeypatch.setattr(ops.compose, "load_pillars", lambda *a: [])
    monkeypatch.setattr(ops.compose, "load_items", lambda *a: [
        {"item_id": "kept", "surface_id": "p"},
        {"item_id": "gone", "retired": True}])
    monkeypatch.setattr(ops, "_session_rows", lambda: sessions or [])
    monkeypatch.setattr(ops, "session_org_slug", lambda r: r.get("org", "?"))


def test_all_rows_membership_and_org_containment(monkeypatch):
    rows = [{"id": str(i), "status": "open", "labels": []} for i in range(4000)]
    rows[0]["labels"] = ["mission:m", "mission:other", "mission:missing"]
    rows[1]["labels"] = ["mission:other"]
    rows[2]["labels"] = ["org:foreign"]
    fixture_sources(monkeypatch, rows=rows, sessions=[
        {"tmux_name": "auto-a", "org": "autonomy", "label": "Visible", "token": "secret"},
        {"tmux_name": "auto-b", "org": "foreign", "label": "Hidden"},
        {"tmux_name": "host-c", "org": "personal"}, {"tmux_name": "unknown"}])
    out = ops.load_ops("autonomy", "m")
    assert len(out["tasks"]) == 3999
    assert out["tasks"][0]["mission_ids"] == ["m", "other", "missing"]
    assert out["tasks"][0]["allocation"] == "selected_mission"
    assert out["tasks"][1]["allocation"] == "other_mission"
    assert out["tasks"][2]["allocation"] == "unallocated"
    assert [s["id"] for s in out["sessions"]] == ["auto-a"]
    assert "secret" not in str(out)
    assert all(i["item_id"] != "gone" for i in out["items"])


@pytest.mark.parametrize("status,spec,deps,known,expected", [
    ("closed", False, ["missing"], False, "complete"),
    ("open", True, [], False, "unknown"),
    ("open", True, ["missing"], True, "unknown"),
    ("blocked", True, [], True, "blocked"),
    ("deferred", True, [], True, "deferred"),
    ("open", True, ["open"], True, "dependency_wait"),
    ("in_progress", True, [], True, "running"),
    ("open", True, ["closed"], True, "ready"),
    ("open", False, [], True, "needs_specification"),
    ("unexpected", True, [], True, "unknown"),
])
def test_readiness_precedence(status, spec, deps, known, expected):
    row = {"status": status, "has_design": spec, "blocks_on": deps,
           "metadata_known": known, "dependencies_known": known}
    assert ops.readiness(row, {"open": {"status": "open"},
                               "closed": {"status": "closed"}}) == expected


def test_parent_membership_does_not_block(monkeypatch):
    fixture_sources(monkeypatch, edges=[
        {"issue_id": "a", "depends_on_id": "absent-parent", "type": "parent-child"}])
    assert ops.load_ops("autonomy", "m")["tasks"][0]["readiness"] == "ready"


def test_failed_sources_are_not_known_empty(monkeypatch):
    fixture_sources(monkeypatch)
    monkeypatch.setattr(ops.bridge, "_bd", lambda *a, **kw: None)
    out = ops.load_ops("autonomy", "m")
    assert out["tasks"] == []
    assert {e["source"] for e in out["errors"]} == {"tasks", "metadata", "dependencies"}


def test_missing_enrichment_is_unknown(monkeypatch):
    fixture_sources(monkeypatch, extra=[])
    assert ops.load_ops("autonomy", "m")["tasks"][0]["readiness"] == "unknown"


def test_non_autonomy_missing_tracker_never_falls_back(monkeypatch):
    fixture_sources(monkeypatch)
    monkeypatch.setattr(ops, "org_beads_dir", lambda org: None)
    monkeypatch.setattr(ops.bridge, "_bd", lambda *a, **kw: pytest.fail("foreign fallback"))
    out = ops.load_ops("foreign", "m")
    assert out["tasks"] == []
    assert any(e["source"] == "tracker" for e in out["errors"])


@pytest.mark.asyncio
async def test_unknown_mission_is_404(monkeypatch):
    monkeypatch.setattr(api, "_owning_org", lambda *a: None)
    request = Request({"type": "http", "path_params": {"mission_id": "missing"}})
    assert (await api.ops_data(request)).status_code == 404


def test_metadata_projection_never_leaks_other_keys():
    valid = {"headline": "Words survive a restart", "phase": "testing",
             "phase_at": "2026-09-08T02:00:00Z", "reported_by": "auto-a"}
    assert ops.project_metadata({"mission_ops": valid, "secret": "hidden"}) == valid
    assert ops.project_metadata('{"mission_ops":{"phase":"made-up"}}') == {}
    assert ops.project_metadata("not json") == {}


@pytest.mark.parametrize("href", ["javascript:alert(1)", "//evil.test", "/\\evil",
    "https://user:pass@example.com", "/admin", "graph://bad\nvalue"])
def test_unsafe_artifacts_refused(href):
    with pytest.raises(SchemaValidationError):
        schemas.validate_briefing({"artifacts": [{"label": "Open", "href": href}]})


def test_briefing_limits_and_compatibility():
    schemas.MissionContentV1.validate({"kind": "question", "state": "open", "title": "Q"})
    schemas.validate_briefing({"artifacts": [{"label": "Design", "href": "/design/abc"}],
                              "subtitle": "A concrete consequence"})
    for bad in ({"subtitle": "x" * 241}, {"bogus": "x"}, {"options": [{}]},
                {"generated_at": "yesterday"}, {"options": "not-list"}):
        with pytest.raises(SchemaValidationError):
            schemas.validate_briefing(bad)
    with pytest.raises(SchemaValidationError):
        schemas.MissionContentV1.validate({"kind": "scope", "title": "S", "briefing": {"subtitle": "x"}})
    for kind, value in (("scope", {}), ("question", []), ("question", "")):
        with pytest.raises(SchemaValidationError):
            schemas.MissionContentV1.validate({"kind": kind, "state": "open" if kind == "question" else "",
                                               "title": "Q", "briefing": value})
