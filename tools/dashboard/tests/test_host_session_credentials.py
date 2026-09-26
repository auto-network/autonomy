"""The host terminal's account: picked like a container session's, and
bootstrapped once from the operator's own sign-in under /host-home
(graph://89d3c8df-544 §3, driver S3; auto-87rmr)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents import session_launcher


def test_an_alias_steers_the_choice(monkeypatch):
    seen = {}

    def picker(*, prefer_alias=None):
        seen["alias"] = prefer_alias
        return {"type": "token", "token": "tok", "alias": prefer_alias}

    monkeypatch.setattr(
        session_launcher, "_resolve_credentials_via_substrate", picker)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    session_launcher._resolve_credentials(prefer_alias="auto-network")

    assert seen["alias"] == "auto-network"


def test_no_installed_account_resolves_to_none(monkeypatch):
    monkeypatch.setattr(
        session_launcher, "_resolve_credentials_via_substrate",
        lambda **kw: None,
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    assert session_launcher._resolve_credentials(prefer_alias=None) is None


def test_an_operator_override_still_wins(monkeypatch):
    """The env var is a legacy operator override and keeps that meaning."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "operator-override")

    creds = session_launcher._resolve_credentials(prefer_alias="ignored")

    assert creds == {"type": "token", "token": "operator-override"}


# ── api_session_create, type host ─────────────────────────────────────────


class _Request:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def host_create(monkeypatch):
    """Drive api_session_create's host branch with every side effect stubbed;
    returns the recorded calls."""
    from tools.dashboard import server
    from tools.graph import credential_import

    calls = {"resolve": [], "import": [], "pending": [], "jobs": []}
    state = {"rows": []}

    def fake_resolve(*, prefer_alias=None):
        calls["resolve"].append(prefer_alias)
        return state["rows"].pop(0) if state["rows"] else None

    def fake_import(home, **kwargs):
        calls["import"].append(home)
        return credential_import.ImportReport()

    async def fake_pending(tmux_name, **kwargs):
        calls["pending"].append((tmux_name, kwargs))

    monkeypatch.setattr(session_launcher, "_resolve_credentials", fake_resolve)
    monkeypatch.setattr(credential_import, "run_import", fake_import)
    monkeypatch.setattr(server.dashboard_db, "session_exists", lambda _name: False)
    monkeypatch.setattr(server.session_trace, "trace", lambda *a, **kw: None)
    monkeypatch.setattr(server.session_monitor, "register_pending", fake_pending)
    monkeypatch.setattr(server, "_resolve_host_session_model", lambda: "claude-opus-5-5")
    monkeypatch.setattr(server, "_render_host_orientation", lambda **kw: "welcome")
    monkeypatch.setattr(
        server, "_SESSION_LIFECYCLE_WORKER",
        SimpleNamespace(try_enqueue=lambda job: calls["jobs"].append(job) or True),
    )
    return server, calls, state


@pytest.mark.asyncio
async def test_host_create_bootstraps_from_the_operator_home_once(host_create):
    server, calls, state = host_create
    # No row before the import; one vault account after it.
    state["rows"] = [None, {"type": "vault", "harness_token": "acct-1", "alias": "me"}]

    resp = await server.api_session_create(_Request({"type": "host"}))

    assert resp.status_code == 202
    assert calls["import"] == ["/host-home"]
    tmux_name, pending = calls["pending"][0]
    assert tmux_name.startswith("host-")
    assert pending["session_type"] == "host"
    assert pending["project"] == "host"
    assert pending["harness_token"] == "acct-1"
    job = calls["jobs"][0]
    assert job.config["kind"] == "host"
    assert job.config["first_message"] == "welcome"
    assert job.config["register_project"] == "host"
    assert "host_cmd" not in job.config


@pytest.mark.asyncio
async def test_host_create_accepts_a_vault_account_without_import(host_create):
    server, calls, state = host_create
    state["rows"] = [{"type": "vault", "harness_token": "acct-2", "alias": "work"}]

    resp = await server.api_session_create(_Request({"type": "host", "alias": "work"}))

    assert resp.status_code == 202
    assert calls["import"] == []
    assert calls["resolve"] == ["work"]
    assert calls["jobs"][0].config["claude_alias"] == "work"


@pytest.mark.asyncio
async def test_host_create_with_no_credential_anywhere_names_the_file(host_create):
    server, calls, state = host_create
    state["rows"] = []

    resp = await server.api_session_create(_Request({"type": "host"}))

    assert resp.status_code == 503
    assert b"/host-home/.claude/.credentials.json" in resp.body
    assert calls["import"] == ["/host-home"]
    assert calls["jobs"] == []


def test_host_relaunch_config_carries_no_command_or_token(tmp_path):
    """Retry and restart rebuild a host terminal from its row: the worker
    launches it through the launcher, which mints the token after the
    restart's stop step revoked the old one (host-0916-103518)."""
    import asyncio

    from tools.dashboard import server

    transcript = tmp_path / "agent-runs" / "host-1-20260926" / "sessions" / "-workspace-repo" / "u.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    loop = asyncio.new_event_loop()
    try:
        config, error = server._build_session_relaunch_config(
            {"tmux_name": "host-1", "type": "host", "harness": "claude",
             "session_uuid": "u", "jsonl_path": str(transcript), "project": "host"},
            attempt=2, event_loop=loop,
        )
    finally:
        loop.close()
    assert error is None
    assert config["kind"] == "host"
    assert config["output_dir"] == str(transcript.parents[2])
    assert "host_cmd" not in config
    assert "CROSSTALK" not in repr(config)


def test_native_host_row_is_not_resumed_inside_the_node(tmp_path):
    import asyncio

    from tools.dashboard import server

    transcript = tmp_path / ".claude" / "projects" / "-opt-autonomy-code" / "u.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    loop = asyncio.new_event_loop()
    try:
        config, error = server._build_session_relaunch_config(
            {"tmux_name": "host-old", "type": "host", "harness": "claude",
             "session_uuid": "u", "jsonl_path": str(transcript)},
            attempt=2, event_loop=loop,
        )
    finally:
        loop.close()
    assert config is None
    assert "outside the node" in error
