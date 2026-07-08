from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agents.capabilities.jira.backend import adf, api
from tools.dashboard import approvals_routes, jira_routes
from tools.dashboard.dao import approval_requests as ar

CAPABILITY_DIR = Path(__file__).resolve().parents[3] / "agents" / "capabilities" / "jira"


def test_capability_ships_no_credential_surface():
    """The hard bar: nothing credential-shaped reaches the container. The
    manifest requests no secret mounts or env, and the mounted tools never
    reference the token or account email."""
    manifest = json.loads((CAPABILITY_DIR / "manifest.json").read_text())
    assert "required_secret_files" not in manifest
    assert "required_env" not in manifest
    for tool in (CAPABILITY_DIR / "tools").iterdir():
        text = tool.read_text()
        for needle in ("jira_token", "JIRA_TOKEN", "JIRA_EMAIL", "atlassian.net"):
            assert needle not in text, f"{tool.name} references {needle}"


# ── ADF conversion ──


def test_markdown_to_adf_blocks():
    doc = adf.markdown_to_adf(
        "# Title\n\nSome **bold** and `code`.\n\n```bash\nanchorectl version\n```\n"
        "- one\n- two\n")
    types = [n["type"] for n in doc["content"]]
    assert doc["type"] == "doc" and doc["version"] == 1
    assert types == ["heading", "paragraph", "codeBlock", "bulletList"]
    para = doc["content"][1]["content"]
    assert {"type": "text", "text": "bold", "marks": [{"type": "strong"}]} in para
    assert doc["content"][2]["attrs"]["language"] == "bash"


def test_adf_round_trip_preserves_meaning():
    md = "## Steps\n1. run `psql`\n2. check output\nplain line"
    back = adf.adf_to_markdown(adf.markdown_to_adf(md))
    assert "## Steps" in back and "1. run `psql`" in back and "plain line" in back


def test_process_ticket_converts_all_adf(tmp_path):
    raw = {
        "key": "ENT-1",
        "fields": {
            "summary": "s", "status": {"name": "Open"}, "priority": None,
            "assignee": None, "reporter": {"displayName": "R"},
            "issuetype": {"name": "Bug"}, "labels": [],
            "description": adf.markdown_to_adf("**desc**"),
            "comment": {"total": 1, "comments": [{
                "id": "9", "author": {"displayName": "A"},
                "created": "c", "updated": "u",
                "body": adf.markdown_to_adf("hi `there`"),
            }]},
        },
    }
    t = adf.process_ticket(raw)
    assert t["description"] == "**desc**"
    assert t["comments"]["entries"][0]["body"] == "hi `there`"
    assert t["assignee"] == "Unassigned"


# ── REST client against a mock transport ──


@pytest.fixture
def jira_env(tmp_path, monkeypatch):
    token_file = tmp_path / "jira_token"
    token_file.write_text("sekret-token\n")
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.test")
    monkeypatch.setenv("JIRA_EMAIL", "op@example.com")
    monkeypatch.setenv("JIRA_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approval_requests.db")
    return token_file


def _mock(monkeypatch, handler):
    monkeypatch.setattr(api, "_transport", httpx.MockTransport(handler))


def test_config_missing_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.setenv("JIRA_EMAIL", "x@y")
    monkeypatch.setenv("JIRA_TOKEN_FILE", str(tmp_path / "nope"))
    with pytest.raises(api.JiraError, match="JIRA_BASE_URL"):
        api.JiraConfig.from_env()


def test_set_field_sends_adf_not_plain_string(jira_env, monkeypatch):
    """Jira Cloud rejects plain strings for textarea custom fields even though
    editmeta reports them as string — the write must carry an ADF document."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    _mock(monkeypatch, handler)
    out = api.set_field(api.JiraConfig.from_env(), "ENT-1", "customfield_10153",
                        "step 1\nstep 2")
    assert out == {"field_id": "customfield_10153"}
    assert (seen["method"], seen["path"]) == ("PUT", "/rest/api/3/issue/ENT-1")
    value = seen["body"]["fields"]["customfield_10153"]
    assert isinstance(value, dict) and value["type"] == "doc"   # ADF, not a string


def test_comment_sends_adf_and_shapes_response(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["body"]["type"] == "doc"
        return httpx.Response(201, json={
            "id": "64029", "author": {"displayName": "Op"}, "created": "now"})

    _mock(monkeypatch, handler)
    out = api.add_comment(api.JiraConfig.from_env(), "ENT-1", "root cause: …")
    assert out == {"id": "64029", "author": "Op", "created": "now"}


def test_editmeta_discovers_field_id(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/issue/ENT-1/editmeta"
        return httpx.Response(200, json={"fields": {
            "summary": {"name": "Summary"},
            "customfield_10153": {"name": "Confirm Plan",
                                  "schema": {"type": "string"}},
        }})

    _mock(monkeypatch, handler)
    cfg = api.JiraConfig.from_env()
    assert api.editmeta_field_id(cfg, "ENT-1", "Confirm Plan") == "customfield_10153"
    with pytest.raises(api.JiraError, match="not editable"):
        api.editmeta_field_id(cfg, "ENT-1", "No Such Field")


def test_createmeta_shaping(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"projects": [{"issuetypes": [{"fields": {
            "customfield_10170": {"name": "Severity", "allowedValues": [
                {"id": "1", "value": "High"}]},
            "components": {"allowedValues": [{"id": "c1", "name": "api"}]},
            "versions": {"allowedValues": [
                {"id": "v1", "name": "Enterprise 5.19", "released": True,
                 "releaseDate": "2026-06-01"},
                {"id": "v2", "name": "Enterprise 5.20", "released": False,
                 "releaseDate": "2026-08-01"},
                {"id": "v3", "name": "Other 1.0", "released": True,
                 "releaseDate": "2026-07-01"},
            ]},
            "priority": {"allowedValues": [{"id": "p1", "name": "P1"}]},
        }}]}]})

    _mock(monkeypatch, handler)
    meta = api.createmeta(api.JiraConfig.from_env(), "ENTERPRISE", "Bug",
                          version_prefix="Enterprise")
    assert meta["severity"] == [{"id": "1", "value": "High"}]
    assert [v["id"] for v in meta["versions"]] == ["v2", "v1"]   # prefix-filtered, newest first
    assert meta["latest_released_version"] == {"id": "v1", "name": "Enterprise 5.19"}
    assert meta["priorities"] == [{"id": "p1", "name": "P1"}]


def test_error_carries_jira_detail_never_credentials(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errorMessages": [
            "Operation value must be an Atlassian Document (see the Atlassian Document Format)"]})

    _mock(monkeypatch, handler)
    with pytest.raises(api.JiraError) as exc:
        api.set_field(api.JiraConfig.from_env(), "ENT-1", "customfield_10153", "x")
    assert "Atlassian Document" in str(exc.value)
    assert "sekret-token" not in str(exc.value)


# ── routes + the jira_write executor on the approval rendezvous ──


def _app():
    return Starlette(routes=[*approvals_routes.ROUTES, *jira_routes.ROUTES])


def test_read_route(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "key": "ENT-1",
            "fields": {"summary": "s", "status": {"name": "Open"},
                       "issuetype": {"name": "Bug"},
                       "description": adf.markdown_to_adf("d")},
        })

    _mock(monkeypatch, handler)
    client = TestClient(_app())
    t = client.get("/api/jira/issue/ENT-1").json()
    assert t["key"] == "ENT-1" and t["description"] == "d"


def test_jira_write_full_flow_comment(jira_env, monkeypatch):
    """Agent stages the write -> operator approves (bare verdict) -> executor
    posts the comment host-side -> the agent's held GET gets the outcome."""
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["path"] = request.url.path
        posted["body"] = json.loads(request.content)
        return httpx.Response(201, json={
            "id": "64029", "author": {"displayName": "Op"}, "created": "now"})

    _mock(monkeypatch, handler)

    async def scenario():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            r = await c.post("/api/approvals", json={
                "kind": "jira_write", "session": "auto-0708-115817",
                "request": {"op": "comment", "key": "ENTERPRISE-8385",
                            "body_markdown": "root cause analysis…"}})
            rid = r.json()["id"]
            held = asyncio.create_task(c.get(f"/api/approvals/{rid}?wait=30"))
            await asyncio.sleep(0.05)
            assert (await c.post(f"/api/approvals/{rid}/decision",
                                 json={"approved": True})).json() == {"ok": True}
            result = (await held).json()["result"]
            assert result["approved"] is True
            assert result["execution"] == {"ok": True, "id": "64029",
                                           "author": "Op", "created": "now"}
            assert posted["path"] == "/rest/api/3/issue/ENTERPRISE-8385/comment"
            assert posted["body"]["body"]["type"] == "doc"

    asyncio.run(scenario())


def test_jira_write_declined_never_touches_jira(jira_env, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)

    _mock(monkeypatch, handler)
    client = TestClient(_app())
    rid = client.post("/api/approvals", json={
        "kind": "jira_write", "session": "auto-1",
        "request": {"op": "comment", "key": "ENT-1", "body_markdown": "x"},
    }).json()["id"]
    assert client.post(f"/api/approvals/{rid}/decision",
                       json={"approved": False}).json() == {"ok": True}
    assert client.get(f"/api/approvals/{rid}").json()["result"] == {"approved": False}
    assert calls == []   # no Jira traffic without an approval


def test_jira_write_set_field_discovers_id_and_sends_adf(jira_env, monkeypatch):
    """The Confirm Plan path: field id discovered via editmeta at execution
    time, value written as ADF."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/editmeta"):
            return httpx.Response(200, json={"fields": {
                "customfield_10153": {"name": "Confirm Plan"}}})
        body = json.loads(request.content)
        assert body["fields"]["customfield_10153"]["type"] == "doc"
        return httpx.Response(204)

    _mock(monkeypatch, handler)

    async def scenario():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            r = await c.post("/api/approvals", json={
                "kind": "jira_write", "session": "auto-1",
                "request": {"op": "set_field", "key": "ENTERPRISE-8385",
                            "field_name": "Confirm Plan",
                            "body_markdown": "1. run `anchorectl version`\n2. expect 5.19"}})
            rid = r.json()["id"]
            await c.post(f"/api/approvals/{rid}/decision", json={"approved": True})
            result = (await c.get(f"/api/approvals/{rid}?wait=10")).json()["result"]
            assert result["execution"] == {"ok": True, "field_id": "customfield_10153"}
            assert ("GET", "/rest/api/3/issue/ENTERPRISE-8385/editmeta") in seen
            assert ("PUT", "/rest/api/3/issue/ENTERPRISE-8385") in seen

    asyncio.run(scenario())


def test_jira_write_failure_lands_in_result(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errorMessages": ["bad field"]})

    _mock(monkeypatch, handler)

    async def scenario():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            r = await c.post("/api/approvals", json={
                "kind": "jira_write", "session": "auto-1",
                "request": {"op": "create", "fields": {"summary": "s"}}})
            rid = r.json()["id"]
            await c.post(f"/api/approvals/{rid}/decision", json={"approved": True})
            result = (await c.get(f"/api/approvals/{rid}?wait=10")).json()["result"]
            assert result["approved"] is True
            assert result["execution"]["ok"] is False
            assert "bad field" in result["execution"]["error"]

    asyncio.run(scenario())
