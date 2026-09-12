"""Organization routing contracts for the Beads dashboard."""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request

from tools.dashboard import api_auth, server
from tools import data_paths


def _request(path: str, principal: api_auth.ApiPrincipal) -> Request:
    path_only, _, query = path.partition("?")
    bits = path_only.strip("/").split("/")
    path_params = {"id": bits[2]} if len(bits) >= 4 and bits[1] == "bead" else {}
    return Request({
        "type": "http",
        "method": "GET",
        "path": path_only,
        "query_string": query.encode(),
        "headers": [],
        "path_params": path_params,
        "state": {
            "api_principal": principal,
            "api_organization": principal.org if principal.org_bound else None,
        },
    })


LOCAL = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject="host-1")
ANCHORE = api_auth.ApiPrincipal(
    api_auth.ApiPrincipalKind.ORG_SESSION, subject="anc-session", org="anchore"
)


def test_bead_deps_excludes_symmetric_relations_from_blocker_sections(monkeypatch):
    calls = []

    async def fake_run(cmd, timeout=30, *, empty=None, beads_dir=None):
        calls.append(cmd)
        if "--direction=up" in cmd:
            return [
                {"id": "auto-next", "dependency_type": "blocks"},
                {"id": "auto-related", "dependency_type": "relates-to"},
            ]
        return [
            {"id": "auto-prior", "dependency_type": "blocks"},
            {"id": "auto-related", "dependency_type": "relates-to"},
            {"id": "auto-epic", "dependency_type": "parent-child"},
        ]

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(server, "run_cli_json", fake_run)

    response = asyncio.run(server.api_bead_deps(_request("/api/bead/auto-x/deps", LOCAL)))

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "blockers": [{"id": "auto-prior", "dependency_type": "blocks"}],
        "dependents": [{"id": "auto-next", "dependency_type": "blocks"}],
    }
    assert len(calls) == 2


def test_blocking_bead_edges_accepts_legacy_type_field():
    assert server._blocking_bead_edges([
        {"id": "auto-block", "type": "blocks"},
        {"id": "auto-related", "type": "relates-to"},
    ]) == [{"id": "auto-block", "type": "blocks"}]


def test_list_routes_global_selection_to_organization_tracker(monkeypatch):
    calls = []

    async def fake_run(cmd, timeout=30, *, empty=None, beads_dir=None):
        calls.append((cmd, beads_dir))
        return []

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(server, "run_cli_json", fake_run)
    monkeypatch.setattr(data_paths, "org_beads_dir", lambda org: f"tracker:{org}")

    response = asyncio.run(server.api_beads_list(_request("/api/beads/list?org=anchore", LOCAL)))

    assert response.status_code == 200
    assert calls == [
        (["bd", "list", "--json", "-n", "100", "--sort", "updated"], "tracker:anchore")
    ]


def test_unknown_selected_tracker_never_falls_back_to_default(monkeypatch):
    called = False

    async def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(server, "run_cli_json", fake_run)
    monkeypatch.setattr(data_paths, "org_beads_dir", lambda org: None)

    response = asyncio.run(server.api_beads_list(_request("/api/beads/list?org=unknown", LOCAL)))

    assert response.status_code == 200
    assert json.loads(response.body) == []
    assert called is False


def test_list_pins_org_session_and_refuses_cross_org_before_bd(monkeypatch):
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(server, "run_cli_json", fake_run)
    monkeypatch.setattr(data_paths, "org_beads_dir", lambda org: f"tracker:{org}")

    own = asyncio.run(server.api_beads_list(_request("/api/beads/list", ANCHORE)))
    refused = asyncio.run(
        server.api_beads_list(_request("/api/beads/list?org=autonomy", ANCHORE))
    )

    assert own.status_code == 200
    assert refused.status_code == 403
    assert len(calls) == 1
    assert calls[0][1]["beads_dir"] == "tracker:anchore"


def test_approval_uses_selected_tracker(monkeypatch):
    calls = []

    async def fake_run(cmd, timeout=30, stdin_data=None, beads_dir=None):
        calls.append((cmd, beads_dir))
        return "", "", 0

    async def no_nag(bead_id, org=None):
        calls.append((["nag", bead_id], org))

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(server, "run_cli", fake_run)
    monkeypatch.setattr(server, "_maybe_send_dashboard_approval_nag", no_nag)
    monkeypatch.setattr(data_paths, "org_beads_dir", lambda org: f"tracker:{org}")

    response = asyncio.run(
        server.api_bead_approve(_request("/api/bead/anc-1/approve?org=anchore", LOCAL))
    )

    assert response.status_code == 200
    assert json.loads(response.body)["ok"] is True
    assert calls[0][1] == "tracker:anchore"
    assert calls[1] == (["nag", "anc-1"], "anchore")


def test_approval_refuses_cross_org_before_bd(monkeypatch):
    called = False

    async def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        return "", "", 0

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(server, "run_cli", fake_run)

    response = asyncio.run(
        server.api_bead_approve(_request("/api/bead/auto-1/approve?org=autonomy", ANCHORE))
    )

    assert response.status_code == 403
    assert called is False


def test_list_hydrates_edges_in_one_org_scoped_batch(monkeypatch):
    calls = []
    edges = [{"issue_id": "anc-b", "depends_on_id": "anc-a", "type": "blocks"}]

    async def fake_run(cmd, timeout=30, *, empty=None, beads_dir=None):
        calls.append((cmd, beads_dir))
        return edges if cmd[1] == "dep" else [{"id": "anc-b"}, {"id": "anc-a"}]

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(server, "run_cli_json", fake_run)
    monkeypatch.setattr(data_paths, "org_beads_dir", lambda org: f"tracker:{org}")
    response = asyncio.run(server.api_beads_list(_request("/api/beads/list", ANCHORE)))
    assert json.loads(response.body) == [
        {"id": "anc-b", "dependencies": edges}, {"id": "anc-a", "dependencies": []}]
    assert calls[1] == (["bd", "dep", "list", "anc-b", "anc-a", "--json"], "tracker:anchore")
    assert len(calls) == 2


def test_dependency_hydration_normalizes_single_id_cli_shape(monkeypatch):
    async def fake_run(*args, **kwargs):
        return [{"id": "anc-epic", "dependency_type": "parent-child"}]

    monkeypatch.setattr(server, "run_cli_json", fake_run)
    rows = [{"id": "anc-a"}]
    asyncio.run(server._hydrate_bead_dependencies(rows, beads_dir="tracker:anchore"))
    assert rows[0]["dependencies"] == [
        {"issue_id": "anc-a", "depends_on_id": "anc-epic", "type": "parent-child"}]


def test_detail_cli_fallback_hydrates_child_blockers_in_same_org(monkeypatch):
    calls = []

    async def fake_run(cmd, timeout=30, *, empty=None, beads_dir=None):
        calls.append((cmd, beads_dir))
        if cmd[1] == "show":
            return [{"id": "anc-epic"}]
        if "--direction=up" in cmd:
            return [{"id": "anc-b", "dependency_type": "parent-child"},
                    {"id": "anc-a", "dependency_type": "parent-child"},
                    {"id": "anc-related", "dependency_type": "relates-to"}]
        if "anc-b" in cmd:
            return [{"issue_id": "anc-b", "depends_on_id": "anc-a", "type": "blocks"}]
        return []

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(server.dao_beads, "get_bead", lambda *args: None)
    monkeypatch.setattr(server, "run_cli_json", fake_run)
    monkeypatch.setattr(data_paths, "org_beads_dir", lambda org: f"tracker:{org}")
    request = _request("/api/dao/bead/anc-epic", ANCHORE)
    request.scope["path_params"] = {"id": "anc-epic"}
    response = asyncio.run(server.api_dao_bead(request))
    children = json.loads(response.body)["children"]
    assert [c["id"] for c in children] == ["anc-b", "anc-a"]
    assert children[0]["dependencies"][0]["depends_on_id"] == "anc-a"
    assert children[1]["dependencies"] == []
    assert len(calls) == 4
    assert all(org == "tracker:anchore" for _, org in calls)
