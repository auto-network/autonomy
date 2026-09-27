"""Mailbox capability: mediated read-only mail, sending only after approval.

Mirrors test_jira_broker.py for the new contract ``mailbox@1``
(agents/capabilities/mailbox, tools/dashboard/mailbox_routes.py), plus the
check the Jira broker lacks: every route and the send executor refuse a
session whose workspace does not have the capability enabled.
"""

from __future__ import annotations

import asyncio
import email.message
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agents.capabilities.mailbox.backend import api
from tools.dashboard import mailbox_routes

ROOT = Path(__file__).resolve().parents[3]
PKG = ROOT / "agents" / "capabilities" / "mailbox"
PASSWORD = "s3cret-Pass-9Zq"


def _raw(uid: int, to: str, subject: str, body: str, html: str | None = None) -> bytes:
    m = email.message.EmailMessage()
    m["From"] = "Sign-in <no-reply@example.com>"
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = "Sun, 27 Sep 2026 05:00:00 +0000"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    return m.as_bytes()


class FakeIMAP:
    """Records every command; serves two messages."""
    instances: list = []
    messages = {
        1: _raw(1, "agent@auto.network", "Welcome", "hello"),
        2: _raw(2, "agent+claude@auto.network", "Your code is 482913",
                "Use 482913 to sign in.", '<p>or <a href="https://example.com/verify?t=abc">verify</a></p>'),
    }

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.calls = []
        self.readonly = None
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.calls.append(("login", user))
        if password != PASSWORD:
            raise api.imaplib.IMAP4.error(f"auth failed for {user} with {password}")
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        self.readonly = readonly
        self.calls.append(("select", folder, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            crit = [str(a) for a in args if a]
            hits = list(self.messages)
            for i, word in enumerate(crit):
                arg = crit[i + 1].strip('"') if i + 1 < len(crit) else ""
                if word == "UID":
                    lo = int(arg.split(":")[0])
                    # RFC 3501: "n:*" includes the highest UID even when it is below n.
                    hits = [u for u in hits if u >= lo or u == max(self.messages)]
                elif word in ("TO", "SUBJECT"):
                    hits = [u for u in hits if arg.encode() in self.messages[u]]
            return "OK", [" ".join(map(str, hits)).encode()]
        if command == "FETCH":
            out = []
            for u in [int(x) for x in args[0].split(",")]:
                raw = self.messages[u]
                meta = f'{u} (UID {u} RFC822.SIZE {len(raw)} INTERNALDATE "27-Sep-2026 05:00:00 +0000" BODY[] {{{len(raw)}}}'
                out += [(meta.encode(), raw), b")"]
            return "OK", out
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self):
        self.calls.append(("logout",))


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        assert password == PASSWORD

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def mail_env(monkeypatch, tmp_path):
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD + "\n")
    monkeypatch.setenv("MAILBOX_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("MAILBOX_USERNAME", "agent@auto.network")
    monkeypatch.setenv("MAILBOX_PASSWORD_FILE", str(pw))
    monkeypatch.setattr(api, "_IMAP", FakeIMAP)
    monkeypatch.setattr(api, "_SMTP", FakeSMTP)
    FakeIMAP.instances = []
    FakeSMTP.sent = []
    return api.MailboxConfig.resolve(None)


# ── package hygiene ─────────────────────────────────────────────────────────

def test_capability_ships_no_credential_surface():
    manifest = json.loads((PKG / "manifest.json").read_text())
    assert "required_env" not in manifest and "required_secret_files" not in manifest
    for tool in (PKG / "tools").iterdir():
        if not tool.is_file():
            continue
        text = tool.read_text()
        for forbidden in ("imaplib", "smtplib", "MAILBOX_PASSWORD", "agent-mailbox", "vault"):
            assert forbidden not in text, f"{tool.name} mentions {forbidden}"


def test_manifest_exposes_every_executable_tool():
    manifest = json.loads((PKG / "manifest.json").read_text())
    executables = sorted(p.name for p in (PKG / "tools").iterdir()
                         if p.is_file() and os.access(p, os.X_OK))
    assert sorted(manifest["tool_target"]["expose_commands"]) == executables


# ── configuration ───────────────────────────────────────────────────────────

def test_missing_config_names_what_is_missing(monkeypatch):
    for var in ("MAILBOX_IMAP_HOST", "MAILBOX_USERNAME", "MAILBOX_PASSWORD_FILE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(api.MailboxError, match="imap_host, username, password"):
        api.MailboxConfig.resolve(None)


def test_config_comes_from_the_install_setting(monkeypatch, tmp_path):
    for var in ("MAILBOX_IMAP_HOST", "MAILBOX_USERNAME"):
        monkeypatch.delenv(var, raising=False)
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD)
    monkeypatch.setenv("MAILBOX_PASSWORD_FILE", str(pw))
    from tools.graph import ops as graph_ops
    row = SimpleNamespace(payload={"contract": "mailbox", "broker_config": {
        "imap_host": "mail.example.org", "username": "bot@example.org", "smtp_port": "2525"}})
    monkeypatch.setattr(graph_ops, "read_set", lambda *a, **k: SimpleNamespace(members=[row]))
    cfg = api.MailboxConfig.resolve("acme")
    assert (cfg.imap_host, cfg.username, cfg.smtp_host, cfg.smtp_port, cfg.from_addr) == (
        "mail.example.org", "bot@example.org", "mail.example.org", 2525, "bot@example.org")
    assert cfg.password == PASSWORD


# ── read-only IMAP ──────────────────────────────────────────────────────────

def test_reads_open_the_folder_read_only_and_only_peek(mail_env):
    listed = api.list_messages(mail_env, limit=5)
    msg = api.read_message(mail_env, 2)
    assert [m["uid"] for m in listed["messages"]] == [2, 1]
    assert msg["codes"] == ["482913"]
    assert msg["links"] == ["https://example.com/verify?t=abc"]
    for conn in FakeIMAP.instances:
        assert conn.readonly is True
        for call in conn.calls:
            assert call[0] in {"login", "select", "uid", "logout"}
            if call[:2] == ("uid", "FETCH"):
                assert "BODY.PEEK[" in call[3] and "BODY[" not in call[3].replace("BODY.PEEK[", "")
            if call[0] == "uid":
                assert call[1] in {"SEARCH", "FETCH"}


def test_filters_reach_imap_search_and_reject_injection(mail_env):
    found = api.list_messages(mail_env, to="agent+claude@auto.network")
    assert [m["uid"] for m in found["messages"]] == [2]
    with pytest.raises(api.MailboxError):
        api.list_messages(mail_env, subject='x" OR ALL')


def test_errors_never_carry_the_password(mail_env, monkeypatch):
    bad = api.MailboxConfig(**{**mail_env.__dict__, "password": "wrong-" + PASSWORD})
    with pytest.raises(api.MailboxError) as e:
        api.probe(bad)
    assert PASSWORD not in str(e.value)


def test_wait_returns_found_or_not(mail_env):
    assert api.wait_for(mail_env, timeout=1, poll=0.01, to="agent+claude@auto.network")["found"] is True
    assert api.wait_for(mail_env, timeout=1, poll=0.01, subject="nothing", after_uid=5)["found"] is False


# ── sending ─────────────────────────────────────────────────────────────────

def test_send_refuses_header_injection_and_empty_fields():
    with pytest.raises(api.MailboxError):
        api.validate_send("a@example.com", "hi\r\nBcc: evil@example.com", "body")
    with pytest.raises(api.MailboxError):
        api.validate_send("", "subject", "body")
    with pytest.raises(api.MailboxError):
        api.validate_send("a@example.com", " ", "body")


def test_send_uses_the_configured_sender(mail_env):
    out = api.send_message(mail_env, to="Someone <a@example.com>", subject="Hello", body="Hi")
    assert out["to"] == "a@example.com"
    msg = FakeSMTP.sent[0]
    assert msg["From"] == "agent@auto.network" and msg["Subject"] == "Hello"


# ── routes and the executor ─────────────────────────────────────────────────

@pytest.fixture
def broker(monkeypatch, mail_env):
    """Token 'good' -> session s-enabled (workspace ws1, enabled); 'off' ->
    s-disabled (workspace ws2, enabled false); 'none' -> s-none (no row)."""
    from agents import workspace_settings
    from tools.dashboard.dao import auth_db, dashboard_db
    from tools.graph import ops as graph_ops
    import hashlib
    tokens = {hashlib.sha256(t.encode()).hexdigest(): (s, "acme") for t, s in
              (("good", "s-enabled"), ("off", "s-disabled"), ("none", "s-none"))}
    monkeypatch.setattr(auth_db, "resolve_token", lambda h: tokens.get(h))
    projects = {"s-enabled": "ws1", "s-disabled": "ws2", "s-none": "ws3"}
    monkeypatch.setattr(dashboard_db, "get_session", lambda s: {"project": projects.get(s, "")})
    monkeypatch.setattr(workspace_settings, "get_workspace",
                        lambda p: SimpleNamespace(id=p, graph_project="acme"))
    rows = [SimpleNamespace(key="ws1:mailbox", payload={"contract": "mailbox", "enabled": True}),
            SimpleNamespace(key="ws2:mailbox", payload={"contract": "mailbox", "enabled": False})]
    monkeypatch.setattr(graph_ops, "read_set", lambda *a, **k: SimpleNamespace(members=rows))
    monkeypatch.setattr(api.MailboxConfig, "resolve", classmethod(lambda cls, org: mail_env))
    return TestClient(Starlette(routes=mailbox_routes.ROUTES))


def _get(client, path, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(path, headers=headers)


def test_routes_require_a_bearer(broker):
    assert _get(broker, "/api/mailbox/messages").status_code == 401
    assert _get(broker, "/api/mailbox/messages", "unknown").status_code == 401


def test_routes_require_the_capability_enabled(broker):
    assert _get(broker, "/api/mailbox/messages", "off").status_code == 403
    assert _get(broker, "/api/mailbox/messages", "none").status_code == 403
    ok = _get(broker, "/api/mailbox/messages?session=s-enabled", "none")
    assert ok.status_code == 403, "a caller-supplied session is ignored"


def test_enabled_session_reads(broker):
    r = _get(broker, "/api/mailbox/messages?to=agent%2Bclaude%40auto.network", "good")
    assert r.status_code == 200 and [m["uid"] for m in r.json()["messages"]] == [2]
    m = _get(broker, "/api/mailbox/message/2", "good").json()
    assert m["codes"] == ["482913"]
    assert _get(broker, "/api/mailbox/message/0", "good").status_code == 400


def test_executor_sends_only_for_an_enabled_session(broker, monkeypatch):
    monkeypatch.setattr(mailbox_routes.approvals_routes, "_org_for_approval", lambda _id: "acme")
    req = {"to": "a@example.com", "subject": "Hi", "body": "Hello"}
    refused = asyncio.run(mailbox_routes._execute_email_send(
        {"id": "x", "session": "s-disabled", "request": req}, {}))
    assert refused["ok"] is False and "not enabled" in refused["error"]
    assert FakeSMTP.sent == []
    sent = asyncio.run(mailbox_routes._execute_email_send(
        {"id": "y", "session": "s-enabled", "request": req}, {}))
    assert sent["ok"] is True and len(FakeSMTP.sent) == 1


def test_email_send_is_a_registered_approval_kind_with_an_executor():
    from tools.dashboard import approvals_routes
    from tools.dashboard.approval_kind_registry import build_production_registry
    assert "email_send" in build_production_registry().kinds
    assert approvals_routes.EXECUTORS["email_send"] is mailbox_routes._execute_email_send
