from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agents.capabilities.jira.backend import adf, api, queries
from tools.dashboard import approvals_routes, jira_routes
from tools.dashboard.dao import approval_requests as ar

CAPABILITY_DIR = Path(__file__).resolve().parents[3] / "agents" / "capabilities" / "jira"


def _fake_curl(tmp_path: Path, payload: dict) -> tuple[Path, Path]:
    """Install a PATH-first curl stub that emits *payload* and records argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "curl.log"
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_CURL_LOG\"\n"
        f"printf '%s\\n' '{json.dumps(payload)}'\n"
    )
    curl.chmod(0o755)
    return bin_dir, log


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


def test_capability_manifest_exposes_every_executable_tool():
    """A shipped jira-* command must not be mounted but missing from PATH."""
    manifest = json.loads((CAPABILITY_DIR / "manifest.json").read_text())
    tool_target = manifest["tool_target"]
    tools_dir = CAPABILITY_DIR / "tools"
    executable_tools = {
        tool.name
        for tool in tools_dir.iterdir()
        if tool.name.startswith("jira-") and os.access(tool, os.X_OK)
    }
    assert tool_target["source"] == "agents/capabilities/jira/tools"
    assert tool_target["target"] == "/opt/jira-tools"
    assert set(tool_target["expose_commands"]) == executable_tools


def test_jira_fields_cli_filters_and_prints_allowed_values(tmp_path):
    bin_dir, log = _fake_curl(tmp_path, {"fields": [
        {"id": "summary", "name": "Summary", "type": "string",
         "items": None, "required": True, "allowed": []},
        {"id": "customfield_10172", "name": "Target Fix Versions",
         "type": "option", "items": None, "required": False,
         "allowed": ["Enterprise 6.1.0"]},
    ]})
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_CURL_LOG": str(log),
        "GRAPH_ORG": "anchore",
    }
    result = subprocess.run(
        [str(CAPABILITY_DIR / "tools" / "jira-fields"),
         "ENTERPRISE-1", "Target Fix"],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert "Target Fix Versions (customfield_10172) [option]" in result.stdout
    assert "allowed: Enterprise 6.1.0" in result.stdout
    assert "Summary" not in result.stdout


def test_jira_update_invalid_field_fails_before_approval(tmp_path):
    bin_dir, log = _fake_curl(tmp_path, {"fields": [
        {"id": "customfield_10172", "name": "Target Fix Versions",
         "type": "option", "items": None, "required": False,
         "allowed": ["Enterprise 6.1.0"]},
    ]})
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_CURL_LOG": str(log),
        "GRAPH_ORG": "anchore",
        "AUTONOMY_SESSION": "auto-test",
    }
    result = subprocess.run(
        [str(CAPABILITY_DIR / "tools" / "jira-update"),
         "ENTERPRISE-1", "--field", "Target Fix Version"],
        input="Enterprise 6.1.0\n", env=env, text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert "unknown editable field 'Target Fix Version'" in result.stderr
    assert "close matches: Target Fix Versions (customfield_10172)" in result.stderr
    assert "Nothing was staged for approval" in result.stderr
    calls = log.read_text()
    assert "/api/jira/fields/ENTERPRISE-1" in calls
    assert "/api/approvals" not in calls


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


def test_image_targets_finds_block_images_only():
    md = ("intro with inline ![nope](inline.png) stays text\n"
          "![screenshot](failure.png)\n"
          "  ![padded](padded.png)  \n"
          "![](bare.png)\n")
    assert adf.image_targets(md) == ["failure.png", "padded.png", "bare.png"]


def test_markdown_to_adf_media_resolution():
    md = "Before\n![the failure](shot.png)\n![missing](gone.png)\nAfter"
    uuid = "9ea8bd05-3372-4f5c-9c8e-8e1b7f6f2a10"
    doc = adf.markdown_to_adf(
        md, media_resolver={"shot.png": uuid}.get)
    types = [n["type"] for n in doc["content"]]
    assert types == ["paragraph", "mediaSingle", "paragraph", "paragraph"]
    media = doc["content"][1]["content"][0]
    assert media == {"type": "media", "attrs": {
        "type": "file", "id": uuid, "collection": "", "alt": "the failure"}}
    # The unresolvable reference stays literal text — nothing is dropped.
    assert doc["content"][2]["content"][0]["text"] == "![missing](gone.png)"


def test_markdown_to_adf_without_resolver_is_unchanged():
    doc = adf.markdown_to_adf("![alt](shot.png)")
    assert [n["type"] for n in doc["content"]] == ["paragraph"]


def test_adf_to_markdown_renders_media_nodes():
    doc = {"type": "doc", "version": 1, "content": [
        {"type": "mediaSingle", "attrs": {"layout": "center"}, "content": [
            {"type": "media", "attrs": {"type": "file", "id": "u-u-i-d",
                                        "alt": "shot.png"}}]},
        {"type": "mediaGroup", "content": [
            {"type": "media", "attrs": {"type": "external",
                                        "url": "https://x/img.png"}}]},
    ]}
    back = adf.adf_to_markdown(doc)
    assert "![shot.png](media:u-u-i-d)" in back
    assert "![media](https://x/img.png)" in back


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


def _stub_install_setting(monkeypatch, payload):
    """Isolate resolve() from the live graph: read_set returns exactly one
    install member with the given payload (or raises when payload is None)."""
    import types
    from tools.graph import ops as graph_ops

    def fake_read_set(set_id, **kw):
        if payload is None:
            raise RuntimeError("no graph in tests")
        member = types.SimpleNamespace(payload=payload)
        return types.SimpleNamespace(members=[member])

    monkeypatch.setattr(graph_ops, "read_set", fake_read_set)


def test_config_missing_is_a_clear_error(monkeypatch, tmp_path):
    _stub_install_setting(monkeypatch, None)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.setenv("JIRA_EMAIL", "x@y")
    monkeypatch.setenv("JIRA_TOKEN_FILE", str(tmp_path / "nope"))
    with pytest.raises(api.JiraError, match="base_url"):
        api.JiraConfig.resolve()


def test_config_resolves_from_org_install_setting(monkeypatch, tmp_path):
    """Non-secret config comes from the issue_tracker org install Setting;
    the token comes from the host file the Setting points at."""
    token_file = tmp_path / "tok"
    token_file.write_text("sekret\n")
    _stub_install_setting(monkeypatch, {
        "contract": "issue_tracker",
        "broker_config": {"base_url": "https://jira.example/",
                          "email": "op@example.com",
                          "token_file": str(token_file)},
    })
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    cfg = api.JiraConfig.resolve(org="autonomy")
    assert cfg == api.JiraConfig(base_url="https://jira.example",
                                 email="op@example.com", token="sekret")


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
    out = api.set_field(api.JiraConfig.resolve(), "ENT-1", "customfield_10153",
                        "step 1\nstep 2")
    assert out == {"field_id": "customfield_10153"}
    assert (seen["method"], seen["path"]) == ("PUT", "/rest/api/3/issue/ENT-1")
    value = seen["body"]["fields"]["customfield_10153"]
    assert isinstance(value, dict) and value["type"] == "doc"   # ADF, not a string


def test_set_editable_field_coerces_fix_version_array(jira_env, monkeypatch):
    """Existing-ticket updates use editmeta to shape Fix Version correctly."""
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/rest/api/3/issue/ENT-1/editmeta"
            return httpx.Response(200, json={"fields": {
                "fixVersions": {
                    "name": "Fix versions",
                    "schema": {"type": "array", "items": "version"},
                },
            }})
        posted["body"] = json.loads(request.content)
        return httpx.Response(204)

    _mock(monkeypatch, handler)
    out = api.set_editable_field(
        api.JiraConfig.resolve(),
        "ENT-1",
        "Fix versions",
        "Enterprise 6.2.0\n",
    )
    assert posted["body"] == {
        "fields": {"fixVersions": [{"name": "Enterprise 6.2.0"}]},
    }
    assert out == {"field_id": "fixVersions"}


def test_set_editable_field_resolves_user_account_id(jira_env, monkeypatch):
    """User-picker updates resolve display name to Jira's accountId shape."""
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/editmeta"):
            return httpx.Response(200, json={"fields": {
                "customfield_9": {
                    "name": "Developer",
                    "schema": {"type": "user"},
                },
            }})
        if request.url.path == "/rest/api/3/user/search":
            assert request.url.params["query"] == "Jeremy Spilman"
            return httpx.Response(200, json=[{
                "accountId": "jeremy-account",
                "displayName": "Jeremy Spilman",
            }])
        posted["body"] = json.loads(request.content)
        return httpx.Response(204)

    _mock(monkeypatch, handler)
    out = api.set_editable_field(
        api.JiraConfig.resolve(),
        "ENT-1",
        "customfield_9",
        "Jeremy Spilman\n",
    )
    assert posted["body"] == {
        "fields": {"customfield_9": {"accountId": "jeremy-account"}},
    }
    assert out == {"field_id": "customfield_9"}


def test_comment_sends_adf_and_shapes_response(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["body"]["type"] == "doc"
        return httpx.Response(201, json={
            "id": "64029", "author": {"displayName": "Op"}, "created": "now"})

    _mock(monkeypatch, handler)
    out = api.add_comment(api.JiraConfig.resolve(), "ENT-1", "root cause: …")
    assert out == {"id": "64029", "author": "Op", "created": "now"}


def test_comment_embeds_attached_images_as_media(jira_env, monkeypatch):
    """A block-level ``![alt](filename)`` referencing an issue attachment
    becomes a mediaSingle node. The media UUID only exists in the signed
    URL the content endpoint redirects to, so resolution parses Location
    from an unfollowed GET."""
    uuid = "9ea8bd05-3372-4f5c-9c8e-8e1b7f6f2a10"
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/rest/api/3/issue/ENT-1":
            assert request.url.params["fields"] == "attachment"
            return httpx.Response(200, json={"fields": {"attachment": [
                {"id": "9000", "filename": "failure.png", "created": "2026-01-01"},
                {"id": "10001", "filename": "failure.png", "created": "2026-07-01"},
            ]}})
        if request.url.path == "/rest/api/3/attachment/content/10001":
            return httpx.Response(303, headers={
                "Location": f"https://api.media.test/file/{uuid}/binary?tok=x"})
        assert request.url.path == "/rest/api/3/issue/ENT-1/comment"
        body = json.loads(request.content)["body"]
        types = [n["type"] for n in body["content"]]
        assert types == ["paragraph", "mediaSingle", "paragraph"]
        assert body["content"][1]["content"][0]["attrs"]["id"] == uuid
        # Unattached filename fell back to literal text, not a failed write.
        assert body["content"][2]["content"][0]["text"] == "![x](not-attached.png)"
        return httpx.Response(201, json={"id": "1", "author": {}, "created": "c"})

    _mock(monkeypatch, handler)
    api.add_comment(api.JiraConfig.resolve(), "ENT-1",
                    "See:\n![shot](failure.png)\n![x](not-attached.png)")
    # Duplicate filename resolved to the NEWEST attachment (id 10001).
    assert "/rest/api/3/attachment/content/10001" in calls


def test_bodies_without_images_skip_media_resolution(jira_env, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(201, json={"id": "1", "author": {}, "created": "c"})

    _mock(monkeypatch, handler)
    api.add_comment(api.JiraConfig.resolve(), "ENT-1",
                    "plain text, inline ![img](x.png) included")
    assert calls == ["/rest/api/3/issue/ENT-1/comment"]


def test_set_field_embeds_attached_images_as_media(jira_env, monkeypatch):
    uuid = "0f0e0d0c-0b0a-4123-8456-789abcdef012"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/issue/ENT-1" and request.method == "GET":
            return httpx.Response(200, json={"fields": {"attachment": [
                {"id": "77", "filename": "arch.png", "created": "2026-07-01"}]}})
        if request.url.path == "/rest/api/3/attachment/content/77":
            return httpx.Response(303, headers={
                "Location": f"https://api.media.test/file/{uuid}/binary"})
        assert (request.method, request.url.path) == ("PUT", "/rest/api/3/issue/ENT-1")
        value = json.loads(request.content)["fields"]["description"]
        assert value["content"][0]["type"] == "mediaSingle"
        assert value["content"][0]["content"][0]["attrs"]["id"] == uuid
        return httpx.Response(204)

    _mock(monkeypatch, handler)
    out = api.set_field(api.JiraConfig.resolve(), "ENT-1", "description",
                        "![arch](arch.png)")
    assert out == {"field_id": "description"}


def test_editmeta_discovers_field_id(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/issue/ENT-1/editmeta"
        return httpx.Response(200, json={"fields": {
            "summary": {"name": "Summary"},
            "customfield_10153": {"name": "Confirm Plan",
                                  "schema": {"type": "string"}},
        }})

    _mock(monkeypatch, handler)
    cfg = api.JiraConfig.resolve()
    assert api.editmeta_field_id(cfg, "ENT-1", "Confirm Plan") == "customfield_10153"
    with pytest.raises(api.JiraError) as exc:
        api.editmeta_field_id(cfg, "ENT-1", "No Such Field")
    assert str(exc.value) == (
        "field 'No Such Field' is invalid for jira-update on ENT-1. "
        "Valid fields: Confirm Plan (customfield_10153), Summary (summary)"
    )


def test_list_editable_fields_exposes_exact_names_schema_and_allowed_values(
    jira_env, monkeypatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/issue/ENT-1/editmeta"
        return httpx.Response(200, json={"fields": {
            "summary": {
                "name": "Summary", "required": True,
                "schema": {"type": "string"},
            },
            "customfield_10172": {
                "name": "Target Fix Versions",
                "schema": {"type": "option"},
                "allowedValues": [
                    {"id": "1", "value": "Enterprise 6.1.0"},
                    {"id": "2", "value": "Enterprise 6.2.0"},
                ],
            },
            "assignee": {
                "name": "Assignee", "schema": {"type": "user"},
                "allowedValues": [{"accountId": "abc", "displayName": "Jane Doe"}],
            },
        }})

    _mock(monkeypatch, handler)
    fields = api.list_editable_fields(api.JiraConfig.resolve(), "ENT-1")
    assert [field["name"] for field in fields] == [
        "Assignee", "Summary", "Target Fix Versions",
    ]
    target = fields[2]
    assert target == {
        "id": "customfield_10172",
        "name": "Target Fix Versions",
        "required": False,
        "type": "option",
        "items": None,
        "allowed": ["Enterprise 6.1.0", "Enterprise 6.2.0"],
    }
    assert fields[0]["allowed"] == ["Jane Doe"]


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
    meta = api.createmeta(api.JiraConfig.resolve(), "ENTERPRISE", "Bug",
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
        api.set_field(api.JiraConfig.resolve(), "ENT-1", "customfield_10153", "x")
    assert "Atlassian Document" in str(exc.value)
    assert "sekret-token" not in str(exc.value)


def test_get_attachment_follows_signed_redirect(jira_env, monkeypatch):
    """Jira's content endpoint 303s to a signed media URL on another host —
    the broker follows it and returns the bytes + metadata."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/attachment/777":
            return httpx.Response(200, json={"filename": "repro.log",
                                             "mimeType": "text/plain"})
        if request.url.path == "/rest/api/3/attachment/content/777":
            return httpx.Response(303, headers={
                "Location": "https://media.test/signed/777"})
        if request.url.host == "media.test":
            assert "authorization" not in [k.lower() for k in request.headers]
            return httpx.Response(200, content=b"log line\n")
        raise AssertionError(f"unexpected call: {request.url}")

    _mock(monkeypatch, handler)
    content, filename, mime = api.get_attachment(api.JiraConfig.resolve(), "777")
    assert (content, filename, mime) == (b"log line\n", "repro.log", "text/plain")


def test_add_attachment_multipart_with_xsrf_header(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/issue/ENT-1/attachments"
        assert request.headers.get("X-Atlassian-Token") == "no-check"
        assert b"repro.log" in request.content and b"log line" in request.content
        return httpx.Response(200, json=[{"id": "777", "filename": "repro.log",
                                          "size": 9}])

    _mock(monkeypatch, handler)
    out = api.add_attachment(api.JiraConfig.resolve(), "ENT-1", "repro.log",
                             b"log line\n", "text/plain")
    assert out == {"id": "777", "filename": "repro.log", "size": 9}


# ── JQL search ──


def test_search_issues_posts_jql_and_shapes_rows(jira_env, monkeypatch):
    """Search uses the current POST /rest/api/3/search/jql endpoint (the
    legacy startAt-pagination /search API is gone) and returns terse
    cleaned rows shaped like the list-item pattern."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "issues": [{"key": "ENT-1", "fields": {
                "summary": "s", "status": {"name": "Pending RC"},
                "priority": {"name": "P2"}, "assignee": None,
                "fixVersions": [{"name": "Enterprise 6.1.0"}],
                "customfield_10020": [{"name": "Sprint 42"}],
                "customfield_10016": 3, "updated": "2026-07-01"}}],
            "nextPageToken": "tok123"})

    _mock(monkeypatch, handler)
    out = api.search_issues(api.JiraConfig.resolve(), "project = ENT",
                            max_results=25)
    assert (seen["method"], seen["path"]) == ("POST", "/rest/api/3/search/jql")
    assert seen["body"]["jql"] == "project = ENT"
    assert seen["body"]["maxResults"] == 25
    assert "summary" in seen["body"]["fields"]   # terse fields requested
    assert out["items"] == [{
        "key": "ENT-1", "summary": "s", "status": "Pending RC",
        "priority": "P2", "assignee": "Unassigned",
        "fix_versions": ["Enterprise 6.1.0"], "sprint": ["Sprint 42"],
        "story_points": 3, "updated": "2026-07-01"}]
    assert out["next_page_token"] == "tok123"


def test_search_issues_paginates_by_token(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["nextPageToken"] == "tok123"
        return httpx.Response(200, json={"issues": []})

    _mock(monkeypatch, handler)
    out = api.search_issues(api.JiraConfig.resolve(), "x", page_token="tok123")
    assert out == {"items": [], "next_page_token": None}   # last page


# ── workflow transitions ──


_TRANSITIONS_JSON = {"transitions": [
    {"id": "31", "name": "Start Review", "to": {"name": "Code Review"},
     "fields": {
         "fixVersions": {"name": "Fix versions", "required": True,
                         "schema": {"type": "array", "items": "version"},
                         "allowedValues": [{"name": "Enterprise 6.1.0"}]},
         "customfield_9": {"name": "Developer", "required": True,
                           "schema": {"type": "user"}},
         "summary": {"name": "Summary", "required": False,
                     "schema": {"type": "string"}},
     }},
    {"id": "41", "name": "Ready for RC", "to": {"name": "Pending RC"},
     "fields": {}},
]}


def test_list_transitions_annotates_required_fields(jira_env, monkeypatch):
    """expand=transitions.fields + a follow-up issue read: each required
    screen field carries has_value so the CLI preflight can compute what's
    missing before staging an approval."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transitions"):
            assert request.url.params["expand"] == "transitions.fields"
            return httpx.Response(200, json=_TRANSITIONS_JSON)
        assert request.url.path == "/rest/api/3/issue/ENT-1"
        assert set(request.url.params["fields"].split(",")) == {
            "fixVersions", "customfield_9"}
        return httpx.Response(200, json={"fields": {
            "fixVersions": [{"name": "Enterprise 6.1.0"}],
            "customfield_9": None}})

    _mock(monkeypatch, handler)
    out = api.list_transitions(api.JiraConfig.resolve(), "ENT-1")
    review = out[0]
    assert (review["name"], review["to_status"]) == ("Start Review", "Code Review")
    by_name = {f["name"]: f for f in review["required_fields"]}
    assert set(by_name) == {"Fix versions", "Developer"}   # non-required dropped
    assert by_name["Fix versions"]["has_value"] is True
    assert by_name["Fix versions"]["allowed"] == ["Enterprise 6.1.0"]
    assert by_name["Developer"]["has_value"] is False
    assert out[1]["required_fields"] == []


def test_transition_matches_destination_status_and_posts(jira_env, monkeypatch):
    """'Pending RC' (the status) matches the 'Ready for RC' transition —
    operators think in status names, workflows name transitions freely."""
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_TRANSITIONS_JSON)
        posted["body"] = json.loads(request.content)
        return httpx.Response(204)

    _mock(monkeypatch, handler)
    out = api.transition_issue(api.JiraConfig.resolve(), "ENT-1", "pending rc")
    assert posted["body"] == {"transition": {"id": "41"}}
    assert out == {"transition": "Ready for RC", "to_status": "Pending RC",
                   "fields_set": []}


def test_transition_no_match_lists_valid_transitions(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_TRANSITIONS_JSON)

    _mock(monkeypatch, handler)
    with pytest.raises(api.JiraError) as exc:
        api.transition_issue(api.JiraConfig.resolve(), "ENT-1", "Done")
    assert "Start Review -> Code Review" in str(exc.value)
    assert "Ready for RC -> Pending RC" in str(exc.value)


def test_transition_coerces_fields_by_schema(jira_env, monkeypatch):
    """CLI strings become schema shapes: version arrays by name, users
    resolved display-name -> accountId via user search."""
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/user/search":
            assert request.url.params["query"] == "Jane Doe"
            return httpx.Response(200, json=[
                {"accountId": "acc-1", "displayName": "Jane Doe"}])
        if request.method == "GET":
            return httpx.Response(200, json=_TRANSITIONS_JSON)
        posted["body"] = json.loads(request.content)
        return httpx.Response(204)

    _mock(monkeypatch, handler)
    out = api.transition_issue(
        api.JiraConfig.resolve(), "ENT-1", "Start Review",
        fields={"Fix versions": "Enterprise 6.1.0", "Developer": "Jane Doe"})
    assert posted["body"]["fields"] == {
        "fixVersions": [{"name": "Enterprise 6.1.0"}],
        "customfield_9": {"accountId": "acc-1"}}
    assert out["fields_set"] == ["customfield_9", "fixVersions"]


def test_transition_ambiguous_user_is_an_error(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/user/search":
            return httpx.Response(200, json=[
                {"accountId": "a", "displayName": "Jane Doe"},
                {"accountId": "b", "displayName": "Jane Doerr"}])
        return httpx.Response(200, json=_TRANSITIONS_JSON)

    _mock(monkeypatch, handler)
    with pytest.raises(api.JiraError, match="resolves to 2 accounts"):
        api.transition_issue(api.JiraConfig.resolve(), "ENT-1", "Start Review",
                             fields={"Developer": "Jane"})


def test_transition_unknown_field_names_screen_fields(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_TRANSITIONS_JSON)

    _mock(monkeypatch, handler)
    with pytest.raises(api.JiraError, match="not on the 'Start Review'"):
        api.transition_issue(api.JiraConfig.resolve(), "ENT-1", "Start Review",
                             fields={"Sprint": "42"})


# ── issue-type change (Jira's "Move") ──


def _issue_type_handler(seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/issue/ENT-1" and request.method == "GET":
            assert request.url.params["fields"] == "issuetype,project"
            return httpx.Response(200, json={"fields": {
                "issuetype": {"id": "10002", "name": "Task", "subtask": False},
                "project": {"key": "ENTERPRISE"}}})
        if request.url.path == "/rest/api/3/project/ENTERPRISE":
            return httpx.Response(200, json={"issueTypes": [
                {"id": "10001", "name": "Bug", "subtask": False},
                {"id": "10002", "name": "Task", "subtask": False},
                {"id": "10003", "name": "Sub-task", "subtask": True}]})
        if request.method == "PUT":
            if seen is not None:
                seen["path"] = request.url.path
                seen["body"] = json.loads(request.content)
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url}")
    return handler


def test_list_issue_types_shapes_project_types(jira_env, monkeypatch):
    _mock(monkeypatch, _issue_type_handler())
    out = api.list_issue_types(api.JiraConfig.resolve(), "ENT-1")
    assert out["project"] == "ENTERPRISE"
    assert out["current"] == {"id": "10002", "name": "Task", "subtask": False}
    assert [t["name"] for t in out["issue_types"]] == ["Bug", "Task", "Sub-task"]


def test_change_issue_type_resolves_name_to_id(jira_env, monkeypatch):
    """The edit endpoint rejects name strings ('Could not find issuetype by
    id or name') — the change must carry the project-scoped numeric id."""
    seen = {}
    _mock(monkeypatch, _issue_type_handler(seen))
    out = api.change_issue_type(api.JiraConfig.resolve(), "ENT-1", "bug")
    assert seen["path"] == "/rest/api/3/issue/ENT-1"
    assert seen["body"] == {"fields": {"issuetype": {"id": "10001"}}}
    assert out == {"key": "ENT-1", "from": "Task", "to": "Bug"}


def test_change_issue_type_errors(jira_env, monkeypatch):
    _mock(monkeypatch, _issue_type_handler())
    cfg = api.JiraConfig.resolve()
    with pytest.raises(api.JiraError, match="Valid types: Bug, Task"):
        api.change_issue_type(cfg, "ENT-1", "Epic")
    with pytest.raises(api.JiraError, match="already a Task"):
        api.change_issue_type(cfg, "ENT-1", "Task")
    with pytest.raises(api.JiraError, match="hierarchy"):
        api.change_issue_type(cfg, "ENT-1", "Sub-task")


def test_jira_update_redirects_issuetype_to_change_type(jira_env, monkeypatch):
    """The live failure that motivated the op: jira-update on 'Issue Type'
    reached Jira with the wrong shape. Now it stops at the broker with a
    pointer to jira-change-type."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/editmeta")
        return httpx.Response(200, json={"fields": {
            "issuetype": {"name": "Issue Type",
                          "schema": {"type": "issuetype"}}}})

    _mock(monkeypatch, handler)
    with pytest.raises(api.JiraError, match="jira-change-type"):
        api.set_editable_field(api.JiraConfig.resolve(), "ENT-1",
                               "Issue Type", "Bug")


# ── named queries (pure resolution over workspace_overrides) ──


QUERY_OVERRIDES = {
    "query_defaults": {"project": "ENTERPRISE"},
    "named_queries": [
        {"name": "mine", "summary": "Open tickets assigned to me",
         "query": "project = {project} AND assignee = currentUser() "
                  "AND statusCategory != Done"},
        {"name": "release", "summary": "Tickets targeted at a release",
         "query": 'project = {project} AND fixVersion = "Enterprise {version}"'},
    ],
}


def test_list_queries_derives_caller_params_from_placeholders():
    """No separate params field to drift: caller params = placeholders in
    the query text minus query_defaults keys."""
    rows = queries.list_queries(QUERY_OVERRIDES)
    assert [(r["name"], r["params"]) for r in rows] == [
        ("mine", []), ("release", ["version"])]


def test_resolve_query_substitutes_defaults_and_params():
    jql = queries.resolve_query(QUERY_OVERRIDES, "release", {"version": "6.1.0"})
    assert jql == 'project = ENTERPRISE AND fixVersion = "Enterprise 6.1.0"'


def test_resolve_query_params_win_over_defaults():
    jql = queries.resolve_query(QUERY_OVERRIDES, "mine", {"project": "OTHER"})
    assert jql.startswith("project = OTHER AND ")


def test_resolve_query_errors_are_actionable():
    with pytest.raises(queries.QueryError, match="unknown named query"):
        queries.resolve_query(QUERY_OVERRIDES, "nope", {})
    with pytest.raises(queries.QueryError, match="requires: version"):
        queries.resolve_query(QUERY_OVERRIDES, "release", {})
    with pytest.raises(queries.QueryError, match="takes no param"):
        queries.resolve_query(QUERY_OVERRIDES, "mine", {"version": "1"})


def test_resolve_query_rejects_clause_smuggling():
    """A param value can't terminate the string literal it lands in —
    quotes and operators are outside the value allowlist."""
    with pytest.raises(queries.QueryError, match="invalid value"):
        queries.resolve_query(QUERY_OVERRIDES, "release",
                              {"version": '6.1.0" OR project = SECRET'})


def test_queries_with_no_overrides():
    assert queries.list_queries(None) == []
    assert queries.list_queries({}) == []
    with pytest.raises(queries.QueryError, match="none defined"):
        queries.resolve_query({}, "mine", {})


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


def test_fields_route_is_read_only_and_returns_editmeta(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/rest/api/3/issue/ENT-1/editmeta"
        return httpx.Response(200, json={"fields": {
            "customfield_10172": {
                "name": "Target Fix Versions",
                "schema": {"type": "option"},
                "allowedValues": [{"value": "Enterprise 6.1.0"}],
            },
        }})

    _mock(monkeypatch, handler)
    client = TestClient(_app())
    response = client.get("/api/jira/fields/ENT-1")
    assert response.status_code == 200
    assert response.json()["fields"] == [{
        "id": "customfield_10172",
        "name": "Target Fix Versions",
        "required": False,
        "type": "option",
        "items": None,
        "allowed": ["Enterprise 6.1.0"],
    }]


def test_search_route_runs_jql_read_only(jira_env, monkeypatch):
    """POST /api/jira/search is a read route like /api/jira/issue — no
    approval rendezvous, the search runs host-side immediately."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["jql"] == 'status = "Pending RC"'
        return httpx.Response(200, json={"issues": [
            {"key": "ENT-1", "fields": {"summary": "s"}}]})

    _mock(monkeypatch, handler)
    client = TestClient(_app())
    out = client.post("/api/jira/search",
                      json={"jql": 'status = "Pending RC"'}).json()
    assert out["items"][0]["key"] == "ENT-1"
    assert out["next_page_token"] is None


def test_search_route_requires_jql(jira_env):
    client = TestClient(_app())
    assert client.post("/api/jira/search", json={}).status_code == 400
    assert client.post("/api/jira/search",
                       content=b"not json").status_code == 400


def _stub_workspace(monkeypatch, overrides=QUERY_OVERRIDES):
    """Pin session→workspace resolution: any session maps to enterprise-ng
    with the given issue_tracker workspace_overrides."""
    monkeypatch.setattr(jira_routes, "_workspace_overrides",
                        lambda session: ("enterprise-ng", overrides))


def test_named_query_list_route(jira_env, monkeypatch):
    _stub_workspace(monkeypatch)
    client = TestClient(_app())
    out = client.get("/api/jira/query?session=auto-1").json()
    assert out["workspace"] == "enterprise-ng"
    assert [(q["name"], q["params"]) for q in out["queries"]] == [
        ("mine", []), ("release", ["version"])]


def test_named_query_run_route_resolves_and_searches(jira_env, monkeypatch):
    _stub_workspace(monkeypatch)
    ran = {}

    def handler(request: httpx.Request) -> httpx.Response:
        ran["jql"] = json.loads(request.content)["jql"]
        return httpx.Response(200, json={"issues": [
            {"key": "ENT-2", "fields": {"summary": "s"}}]})

    _mock(monkeypatch, handler)
    client = TestClient(_app())
    out = client.get(
        "/api/jira/query/release?session=auto-1&version=6.1.0").json()
    assert ran["jql"] == 'project = ENTERPRISE AND fixVersion = "Enterprise 6.1.0"'
    assert out["workspace"] == "enterprise-ng"
    assert out["query"] == "release"
    assert out["jql"] == ran["jql"]   # substituted JQL echoed for transparency
    assert out["items"][0]["key"] == "ENT-2"


def test_named_query_route_errors(jira_env, monkeypatch):
    _stub_workspace(monkeypatch)
    client = TestClient(_app())
    # Unknown query name / bad params → 400 with the QueryError message.
    r = client.get("/api/jira/query/nope?session=auto-1")
    assert r.status_code == 400 and "unknown named query" in r.json()["error"]
    r = client.get("/api/jira/query/release?session=auto-1")
    assert r.status_code == 400 and "requires: version" in r.json()["error"]
    # Session is mandatory — resolution is host-side, never caller-asserted.
    assert client.get("/api/jira/query").status_code == 400
    assert client.get("/api/jira/query/mine").status_code == 400


def test_named_query_unresolvable_session_is_404(jira_env, monkeypatch):
    def boom(session):
        raise LookupError(f"session {session!r} does not map to a workspace")

    monkeypatch.setattr(jira_routes, "_workspace_overrides", boom)
    client = TestClient(_app())
    r = client.get("/api/jira/query?session=ghost")
    assert r.status_code == 404 and "does not map" in r.json()["error"]


def test_transitions_read_route(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transitions"):
            return httpx.Response(200, json=_TRANSITIONS_JSON)
        return httpx.Response(200, json={"fields": {
            "fixVersions": [], "customfield_9": None}})

    _mock(monkeypatch, handler)
    client = TestClient(_app())
    out = client.get("/api/jira/transitions/ENT-1").json()
    assert [t["name"] for t in out["transitions"]] == [
        "Start Review", "Ready for RC"]
    assert out["transitions"][0]["required_fields"][0]["has_value"] is False


def test_jira_write_transition_flow(jira_env, monkeypatch):
    """op=transition through the approval rendezvous: executor re-resolves
    the transition by name and posts it with coerced fields."""
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_TRANSITIONS_JSON)
        posted["path"] = request.url.path
        posted["body"] = json.loads(request.content)
        return httpx.Response(204)

    _mock(monkeypatch, handler)

    async def scenario():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            r = await c.post("/api/approvals", json={
                "kind": "jira_write", "session": "auto-1",
                "request": {"op": "transition", "key": "ENTERPRISE-8348",
                            "transition": "Pending RC"}})
            rid = r.json()["id"]
            await c.post(f"/api/approvals/{rid}/decision", json={"approved": True})
            result = (await c.get(f"/api/approvals/{rid}?wait=10")).json()["result"]
            assert result["execution"] == {
                "ok": True, "transition": "Ready for RC",
                "to_status": "Pending RC", "fields_set": []}
            assert posted["path"] == "/rest/api/3/issue/ENTERPRISE-8348/transitions"
            assert posted["body"] == {"transition": {"id": "41"}}

    asyncio.run(scenario())


def test_issue_types_read_route(jira_env, monkeypatch):
    _mock(monkeypatch, _issue_type_handler())
    client = TestClient(_app())
    out = client.get("/api/jira/issue-types/ENT-1").json()
    assert out["current"]["name"] == "Task"
    assert [t["name"] for t in out["issue_types"]] == ["Bug", "Task", "Sub-task"]


def test_jira_write_change_type_flow(jira_env, monkeypatch):
    """op=change_type through the approval rendezvous: executor re-resolves
    the target type and PUTs the numeric id."""
    seen = {}
    _mock(monkeypatch, _issue_type_handler(seen))

    async def scenario():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            r = await c.post("/api/approvals", json={
                "kind": "jira_write", "session": "auto-1",
                "request": {"op": "change_type", "key": "ENT-1",
                            "issue_type": "Bug"}})
            rid = r.json()["id"]
            await c.post(f"/api/approvals/{rid}/decision", json={"approved": True})
            result = (await c.get(f"/api/approvals/{rid}?wait=10")).json()["result"]
            assert result["execution"] == {
                "ok": True, "key": "ENT-1", "from": "Task", "to": "Bug"}
            assert seen["body"] == {"fields": {"issuetype": {"id": "10001"}}}

    asyncio.run(scenario())


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


def test_jira_write_set_field_coerces_structured_value(jira_env, monkeypatch):
    """The approval executor applies schema coercion outside transitions."""
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/editmeta"):
            return httpx.Response(200, json={"fields": {
                "fixVersions": {
                    "name": "Fix versions",
                    "schema": {"type": "array", "items": "version"},
                },
            }})
        posted["body"] = json.loads(request.content)
        return httpx.Response(204)

    _mock(monkeypatch, handler)

    async def scenario():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            r = await c.post("/api/approvals", json={
                "kind": "jira_write",
                "session": "auto-1",
                "request": {
                    "op": "set_field",
                    "key": "ENTERPRISE-8853",
                    "field_name": "Fix versions",
                    "body_markdown": "Enterprise 6.2.0\n",
                },
            })
            rid = r.json()["id"]
            await c.post(
                f"/api/approvals/{rid}/decision",
                json={"approved": True},
            )
            result = (
                await c.get(f"/api/approvals/{rid}?wait=10")
            ).json()["result"]
            assert result["execution"] == {
                "ok": True,
                "field_id": "fixVersions",
            }

    asyncio.run(scenario())
    assert posted["body"] == {
        "fields": {"fixVersions": [{"name": "Enterprise 6.2.0"}]},
    }


def test_attachment_download_route(jira_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attachment/777"):
            return httpx.Response(200, json={"filename": "repro.log",
                                             "mimeType": "text/plain"})
        return httpx.Response(200, content=b"log line\n")

    _mock(monkeypatch, handler)
    client = TestClient(_app())
    resp = client.get("/api/jira/attachment/777")
    assert resp.status_code == 200
    assert resp.content == b"log line\n"
    assert 'filename="repro.log"' in resp.headers["content-disposition"]


def test_jira_write_attach_flow(jira_env, monkeypatch):
    import base64 as b64
    uploaded = {}

    def handler(request: httpx.Request) -> httpx.Response:
        uploaded["path"] = request.url.path
        uploaded["content"] = request.content
        return httpx.Response(200, json=[{"id": "9", "filename": "repro.log",
                                          "size": 9}])

    _mock(monkeypatch, handler)

    async def scenario():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            r = await c.post("/api/approvals", json={
                "kind": "jira_write", "session": "auto-1",
                "request": {"op": "attach", "key": "ENT-1",
                            "filename": "repro.log", "size": 9,
                            "mime_type": "text/plain",
                            "content_b64": b64.b64encode(b"log line\n").decode()}})
            rid = r.json()["id"]
            await c.post(f"/api/approvals/{rid}/decision", json={"approved": True})
            result = (await c.get(f"/api/approvals/{rid}?wait=10")).json()["result"]
            assert result["execution"] == {"ok": True, "id": "9",
                                           "filename": "repro.log", "size": 9}
            assert uploaded["path"] == "/rest/api/3/issue/ENT-1/attachments"
            assert b"log line" in uploaded["content"]

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
