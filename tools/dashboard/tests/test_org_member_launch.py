"""auto-1qj12: an organization member starts a session on another member's
runner. Home fills the launch's credentials from its own vault and sends it
over member-message/1; the runner launches the member's organization's
workspace with only those credentials and records the member as owner
(graph://7eb29bc8-31a v6 §9.8)."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from agents import session_launcher as sl
from tools.dashboard import remote_api
from tools.dashboard.dao import dashboard_db

PERSONA = "c1" * 32
OTHER = "d1" * 32
MACHINE = "b1" * 32
OP_ID = "0f" * 16
CALLER = remote_api.RemoteCaller("org", machine_pub=MACHINE, persona=PERSONA, org="alpha")
RUNNER = {"org": "alpha", "persona_pub": OTHER, "machine_pub": "e1" * 32}


def _proj(**kw):
    return SimpleNamespace(**{
        "id": "dev", "graph_project": "alpha", "harness": "claude", "model": None,
        "env": {"GH_TOKEN": "credential:github.token"}, "env_from_host": [],
        "capabilities": (), **kw})


@pytest.fixture
def server(tmp_path, monkeypatch):
    from tools.dashboard import server as srv

    if dashboard_db._conn is not None:
        dashboard_db._conn.close()
    dashboard_db._conn = None
    dashboard_db.init_db(tmp_path / "dashboard.db")
    monkeypatch.setattr(srv, "_org_launch_lock", None)
    yield srv
    dashboard_db._conn.close()
    dashboard_db._conn = None


def _body(**kw):
    signins = {sl.CLAUDE_BUNDLE_FILENAME: base64.b64encode(b"{}").decode()}
    return {"project": "dev", "operation_id": OP_ID, **kw,
            "credentials": kw.get("credentials", {
                "credentials": {"github.token": "ghp_X"}, "signins": signins})}


def _member_launch(srv, monkeypatch, body, workspaces=None):
    seen = {}

    async def create(request_body, request, provenance=None, *, workspace_org=None, carried=None):
        seen.update(body=request_body, provenance=provenance, org=workspace_org, carried=carried)
        name = "auto-member-1"
        dashboard_db.upsert_session(name, "container", request_body["project"])
        dashboard_db.set_launch_provenance(name, **provenance)
        return srv.JSONResponse({"tmux_name": name, "pending": True}, status_code=202)

    monkeypatch.setattr(srv, "_create_session_from_body", create)
    monkeypatch.setattr(srv.workspace_settings, "_workspaces_in_org",
                        lambda org: workspaces if workspaces is not None else
                        ({"dev": _proj()} if org == "alpha" else {}))
    response = asyncio.run(srv._create_org_member_session(body, CALLER))
    return response.status_code, json.loads(response.body), seen


def test_the_launch_route_is_open_to_members_only_on_an_offered_runner(server):
    rule = remote_api.rule_of(server.api_session_create)
    assert rule.kinds == ("fleet", "org") and rule.check_is_runner


def test_a_member_launch_runs_the_orgs_workspace_with_carried_credentials(server, monkeypatch):
    status, data, seen = _member_launch(server, monkeypatch, _body(model="claude-opus-5-5"))
    assert status == 202 and data["tmux_name"] == "auto-member-1"
    assert seen["body"] == {"type": "container", "project": "dev", "model": "claude-opus-5-5"}
    assert seen["org"] == "alpha"
    assert seen["carried"].credentials == {"github.token": "ghp_X"}
    assert seen["carried"].signins == {sl.CLAUDE_BUNDLE_FILENAME: b"{}"}
    row = dashboard_db.get_session("auto-member-1")
    assert (row["owner_persona"], row["home_machine"], row["launch_op_id"]) == (
        PERSONA, MACHINE, OP_ID)
    assert row["launched_by"] == f"persona:{PERSONA}"


def test_the_same_operation_returns_the_session_and_another_persona_cannot_reuse_it(
        server, monkeypatch):
    _member_launch(server, monkeypatch, _body())
    status, data, seen = _member_launch(server, monkeypatch, _body())
    assert (status, data["repeated"], seen) == (202, True, {})
    dashboard_db.set_launch_provenance("auto-member-1", launched_by="x", home_machine="y",
                                       launch_op_id=OP_ID, owner_persona=OTHER)
    status, data, _ = _member_launch(server, monkeypatch, _body())
    assert (status, data["refusal"]) == (409, "launch-op-spent")


@pytest.mark.parametrize("body, code", [
    (_body(credentials={"credentials": {}, "signins": {
        sl.CLAUDE_BUNDLE_FILENAME: base64.b64encode(b"{}").decode()}}), "credential-refused"),
    (_body(credentials={"credentials": {"github.token": "ghp_X"}}), "credential-refused"),
    (_body(type="host"), "missing-project"),
    (_body(machine="elsewhere"), "request-malformed"),
    (_body(operation_id="nope"), "bad-operation-id"),
    (_body(project="not-in-alpha"), "workspace-unavailable"),
    (_body(credentials={"signins": {"x": "%%%"}}), "request-malformed"),
])
def test_refusals_register_nothing(server, monkeypatch, body, code):
    status, data, seen = _member_launch(server, monkeypatch, body)
    assert data["refusal"] == code and status >= 400
    assert seen == {}


def test_a_workspace_reading_the_runner_is_refused(server, monkeypatch):
    proj = _proj(capabilities=(SimpleNamespace(
        implementation="cap", env_bindings={"T": "host:RUNNER"}, secret_file_bindings={}),))
    status, data, _ = _member_launch(server, monkeypatch, _body(), {"dev": proj})
    assert (status, data["refusal"]) == (403, "credential-refused")
    assert "runner's own environment" in data["error"]


# ── Home: filling the launch from its own vault ────────────────────────────


def test_home_fills_credentials_from_its_own_vault(server, monkeypatch):
    monkeypatch.setattr(sl, "_resolve_credential", lambda key: {"github.token": "ghp_HOME"}[key])
    monkeypatch.setattr(sl, "_resolve_credentials_via_substrate",
                        lambda **_k: {"type": "vault", "harness_token": "acct-1"})
    monkeypatch.setattr(sl, "_signin_payloads", lambda account, **_k:
                        {sl.CLAUDE_BUNDLE_FILENAME: b"bundle"} if account == "acct-1" else {})
    monkeypatch.setenv("FROM_HOME", "home-env")
    carried = server._member_launch_credentials(
        _proj(env_from_host=["FROM_HOME", "UNSET_HERE"]), "claude", None)
    assert carried == {"credentials": {"github.token": "ghp_HOME"},
                       "env": {"FROM_HOME": "home-env"},
                       "signins": {sl.CLAUDE_BUNDLE_FILENAME: base64.b64encode(b"bundle").decode()}}


def test_home_carries_a_setup_token_as_an_env_value(server, monkeypatch):
    monkeypatch.setattr(sl, "_resolve_credential", lambda key: "ghp_HOME")
    monkeypatch.setattr(sl, "_resolve_credentials_via_substrate",
                        lambda **_k: {"type": "token", "token": "tok", "harness_token": "a"})
    carried = server._member_launch_credentials(_proj(), "claude", None)
    assert carried["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"} and carried["signins"] == {}


def test_home_refuses_when_its_vault_lacks_a_named_credential(server, monkeypatch):
    monkeypatch.setattr(sl, "_resolve_credential", lambda key: None)
    assert "github.token" in server._member_launch_credentials(_proj(), "claude", None)


def test_home_sends_the_launch_to_the_runner_over_member_message(server, monkeypatch):
    from tools.dashboard import member_message_client as mmc

    sent = []
    monkeypatch.setattr(server.workspace_settings, "_workspaces_in_org",
                        lambda org: {"dev": _proj()})
    monkeypatch.setattr(server, "_member_launch_credentials",
                        lambda proj, harness, model: {"credentials": {"github.token": "v"},
                                                      "env": {}, "signins": {}})

    async def fake_request(target, op, payload, *, timeout=15.0):
        sent.append((target, op, payload))
        if len(sent) == 1:
            return {"ok": False, "refusal": "reply-lost"}
        return {"ok": True, "result": {"status": 202, "headers": {}, "body": base64.b64encode(
            json.dumps({"tmux_name": "auto-there", "pending": True}).encode()).decode()}}

    monkeypatch.setattr(mmc, "request", fake_request)
    response = asyncio.run(server._launch_on_org_runner(
        {"machine": "e1e1e1e1", "project": "dev"}, RUNNER))
    data = json.loads(response.body)
    assert response.status_code == 202 and data["tmux_name"] == "auto-there"
    assert len(sent) == 2 and sent[0][2] == sent[1][2]      # a lost reply retries the same op
    target, op, payload = sent[0]
    assert (target, op, payload["method"], payload["path"]) == (
        RUNNER, "api", "POST", "/api/session/create")
    launch = json.loads(base64.b64decode(payload["body"]))
    assert launch["credentials"]["credentials"] == {"github.token": "v"}
    assert launch["operation_id"] == data["operation_id"] and "machine" not in launch


# ── the worker and resume ──────────────────────────────────────────────────


def test_the_worker_launches_the_pinned_workspace_and_drops_the_secrets(server, monkeypatch, tmp_path):
    from tools.dashboard.session_lifecycle_worker import LifecycleJob, SessionLifecycleStateWriter

    dashboard_db.insert_session(tmux_name="auto-mw", session_type="container",
                                project="dev", harness="claude")
    proj = _proj(name="Dev", default_tags=[], startup=None, working_dir="/workspace/repo",
                 image="img", needs_nested_docker=False, session_runtime="standard",
                 network_host=False, capability_issues=(), env_from_host=["RUNNER_ONLY"])
    monkeypatch.setenv("RUNNER_ONLY", "runner-secret")
    monkeypatch.setattr(server.workspace_settings, "get_workspace",
                        lambda _p: (_ for _ in ()).throw(AssertionError("unpinned lookup")))
    monkeypatch.setattr(server.workspace_settings, "_workspaces_in_org",
                        lambda org: {"dev": proj} if org == "alpha" else {})
    monkeypatch.setattr(server.workspace_settings, "materialize_startup_script", lambda *_a: None)
    monkeypatch.setattr(server.workspace_settings, "artifact_mounts", lambda _p: {})
    monkeypatch.setattr(server, "render_workspace_primer", lambda *_a, **_k: "primer")
    monkeypatch.setattr(server, "prepare_session_mounts", lambda *_a, **_k: {})
    monkeypatch.setattr(server, "DATA_ROOT", tmp_path)
    launched = {}

    def fake_launch(**kw):
        launched.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "launch_session", fake_launch)
    carried = sl.CarriedCredentials({"github.token": "v"})
    job = LifecycleJob("start", "auto-mw", {"project_id": "dev", "workspace_org": "alpha",
                                            "carried": carried})
    server._run_project_session_start(job, SessionLifecycleStateWriter())
    assert launched["carried"] is carried
    assert "RUNNER_ONLY" not in (launched["extra_env"] or {})
    assert "carried" not in job.config


def test_resume_refuses_a_members_session(server, monkeypatch):
    from tools.dashboard.session_lifecycle_worker import LifecycleJob, SessionLifecycleStateWriter

    dashboard_db.insert_session(tmux_name="auto-mr", session_type="container",
                                project="dev", harness="claude")
    dashboard_db.set_launch_provenance("auto-mr", launched_by=f"persona:{PERSONA}",
                                       home_machine=MACHINE, launch_op_id=OP_ID,
                                       owner_persona=PERSONA)
    monkeypatch.setattr(server.workspace_settings, "get_workspace",
                        lambda _p: (_ for _ in ()).throw(AssertionError("resumed")))
    failed = []
    writer = SessionLifecycleStateWriter()
    monkeypatch.setattr(writer, "fail", lambda name, **kw: failed.append(kw.get("reason")))
    server._run_session_resume_start(
        LifecycleJob("resume", "auto-mr", {"kind": "project", "project_id": "dev",
                                           "resume_uuid": "u"}), writer)
    assert failed and "cannot be resumed" in failed[0]
