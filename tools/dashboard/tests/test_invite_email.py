"""Invitation SMTP delivery and redemption-lifetime acceptance."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import invite_email, network_routes
from tools.dashboard.invite_email import (
    InviteEmailError,
    SmtpConfig,
    send_invite_email,
)
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair, derive_persona
from tools.network.ledger import HLC, LedgerStore, make_event, org_ledger_db_path
from tools.network.ledger.claims import mint_member_claim
from tools.network.ledger.found import found_org_ledger


ORG_ID = "019c0000-0000-7000-8000-000000000401"
ROOT_SEED = bytes(range(32))
PERSONAL_SEED = bytes(range(32, 64))
INVITEE_SEED = bytes(range(64, 96))
TOKEN = "ab" * 32
REPO_ROOT = Path(__file__).resolve().parents[3]


class CapturingSMTP:
    def __init__(self, *, send_error: Exception | None = None):
        self.send_error = send_error
        self.calls: list[object] = []
        self.messages = []

    def starttls(self):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message):
        self.calls.append("send_message")
        if self.send_error is not None:
            raise self.send_error
        self.messages.append(message)

    def quit(self):
        self.calls.append("quit")

    def close(self):
        self.calls.append("close")


def _cfg(**overrides) -> SmtpConfig:
    values = {
        "host": "smtp.example",
        "port": 587,
        "from_addr": "invites@example",
        "username": None,
        "password": None,
        "starttls": True,
    }
    values.update(overrides)
    return SmtpConfig(**values)


def _clear_smtp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AUTONOMY_SMTP_HOST",
        "AUTONOMY_SMTP_PORT",
        "AUTONOMY_SMTP_FROM",
        "AUTONOMY_SMTP_USERNAME",
        "AUTONOMY_SMTP_PASSWORD_FILE",
        "AUTONOMY_SMTP_STARTTLS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_send_produces_one_message_with_secret_link_and_reentry_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smtp = CapturingSMTP()
    monkeypatch.setattr(invite_email, "_smtp_client", lambda _config: smtp)
    link = "https://relay.example/l/grant#t=" + TOKEN
    expiry = 1_900_000_000_000
    receipt = send_invite_email(
        "invitee@example",
        link,
        expiry,
        org="acme",
        config_resolver=lambda _org: _cfg(),
    )

    assert len(smtp.messages) == 1
    message = smtp.messages[0]
    assert message["To"] == "invitee@example"
    assert message["From"] == "invites@example"
    assert message["Message-ID"] == receipt["message_id"]
    assert link in message.get_content()
    assert "Keep this link until you are admitted" in message.get_content()
    assert smtp.calls == ["starttls", "send_message", "quit"]
    assert receipt == {
        "recipient": "invitee@example",
        "message_id": message["Message-ID"],
    }


def test_smtp_auth_is_optional_but_both_or_neither_and_always_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_smtp_env(monkeypatch)
    monkeypatch.setattr(invite_email, "_installed_config", lambda _org: {})
    with pytest.raises(InviteEmailError, match="missing host, from-address"):
        SmtpConfig.resolve("acme")

    password_file = tmp_path / "smtp-password"
    password_file.write_text("top-secret\n", encoding="utf-8")
    monkeypatch.setenv("AUTONOMY_SMTP_HOST", "smtp.example")
    monkeypatch.setenv("AUTONOMY_SMTP_FROM", "invites@example")
    monkeypatch.setenv("AUTONOMY_SMTP_USERNAME", "mailer")
    with pytest.raises(InviteEmailError, match="missing password"):
        SmtpConfig.resolve("acme")
    monkeypatch.setenv("AUTONOMY_SMTP_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("AUTONOMY_SMTP_STARTTLS", "false")
    config = SmtpConfig.resolve("acme")
    assert config == _cfg(
        username="mailer",
        password="top-secret",
        starttls=False,
    )

    smtp = CapturingSMTP(
        send_error=invite_email.smtplib.SMTPException("top-secret")
    )
    monkeypatch.setattr(invite_email, "_smtp_client", lambda _config: smtp)
    with pytest.raises(InviteEmailError, match="^SMTP delivery failed$") as error:
        send_invite_email(
            "invitee@example",
            "https://relay.example/l/grant#t=secret",
            1_900_000_000_000,
            org="acme",
            config_resolver=lambda _org: config,
        )
    assert "top-secret" not in str(error.value)
    assert smtp.calls == [
        ("login", "mailer", "top-secret"),
        "send_message",
        "quit",
    ]

    monkeypatch.delenv("AUTONOMY_SMTP_USERNAME")
    with pytest.raises(InviteEmailError, match="missing username"):
        SmtpConfig.resolve("acme")


def test_smtp_resolve_uses_org_install_with_per_value_env_overlays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_smtp_env(monkeypatch)
    seen = []

    def installed(org):
        seen.append(org)
        return {
            "host": "installed.example",
            "port": 2525,
            "from_addr": "installed@example",
            "starttls": False,
        }

    monkeypatch.setattr(invite_email, "_installed_config", installed)
    monkeypatch.setenv("AUTONOMY_SMTP_HOST", "overlay.example")
    monkeypatch.setenv("AUTONOMY_SMTP_FROM", "overlay@example")
    config = SmtpConfig.resolve("acme")
    assert seen == ["acme"]
    assert config == _cfg(
        host="overlay.example",
        port=2525,
        from_addr="overlay@example",
        starttls=False,
    )


def test_post_invite_email_scopes_org_and_never_returns_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPH_ORG", "acme")
    calls = []
    link = "https://relay.example/l/grant#t=" + TOKEN

    def fake_send(to_addr, join_link, expiry, org):
        calls.append((to_addr, join_link, expiry, org))
        return {"recipient": to_addr, "message_id": "<safe@auto.network>"}

    monkeypatch.setattr(invite_email, "send_invite_email", fake_send)
    client = TestClient(Starlette(routes=network_routes.ROUTES))
    response = client.post(
        "/api/network/invite/email",
        headers={"X-Graph-Org": "acme"},
        json={
            "org": "acme",
            "to": "invitee@example",
            "join_link": link,
            "expiry": 1_900_000_000_000,
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "recipient": "invitee@example",
        "message_id": "<safe@auto.network>",
    }
    assert link not in response.text
    assert calls == [
        ("invitee@example", link, 1_900_000_000_000, "acme")
    ]

    assert client.post(
        "/api/network/invite/email",
        headers={"X-Graph-Org": "acme"},
        json={"org": "acme", "join_link": link, "expiry": 1},
    ).status_code == 400
    assert client.post(
        "/api/network/invite/email",
        headers={"X-Graph-Org": "acme"},
        json={
            "org": "other",
            "to": "invitee@example",
            "join_link": link,
            "expiry": 1,
        },
    ).status_code == 403

    def fail_send(*_args):
        raise InviteEmailError("SMTP delivery failed")

    monkeypatch.setattr(invite_email, "send_invite_email", fail_send)
    refused = client.post(
        "/api/network/invite/email",
        headers={"X-Graph-Org": "acme"},
        json={
            "org": "acme",
            "to": "invitee@example",
            "join_link": link,
            "expiry": 1,
        },
    )
    assert refused.status_code == 502
    assert refused.json() == {
        "ok": False,
        "error": "SMTP delivery failed",
    }
    assert link not in refused.text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_org_join_client_payload_pins_absolute_invite_expiry() -> None:
    invite_ref = "cd" * 32
    invite_expiry = 1_900_000_000_123
    module_url = (
        REPO_ROOT / "tools/dashboard/static/js/ceremony/invitation.js"
    ).as_uri()
    script = f"""
      import {{ buildOrgJoinGrantPayload }} from {json.dumps(module_url)};
      process.stdout.write(JSON.stringify(buildOrgJoinGrantPayload({{
        orgUuid: {json.dumps(ORG_ID)},
        inviteId: {json.dumps(invite_ref)},
        inviteExpiry: {invite_expiry},
      }})));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "org": ORG_ID,
        "target_uuid": ORG_ID,
        "target_type": "org:join",
        "invite_ref": invite_ref,
        "expires_at": invite_expiry,
    }


def test_delivered_token_redemption_expiry_creates_no_event_or_pending_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orgs_dir = tmp_path / "orgs"
    slug = "email-redemption"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", slug)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    database = GraphDB.create_org_db(
        slug,
        root=orgs_dir,
        org_id=ORG_ID,
    )
    database.close()

    now = int(time.time() * 1000)
    founded_at = now - 120_000
    root = KeyPair.from_private_hex(ROOT_SEED.hex())
    path = org_ledger_db_path(slug)
    with LedgerStore(path) as store:
        founded = found_org_ledger(
            store,
            org_id=ORG_ID,
            org_root=root,
            personal_root_seed=PERSONAL_SEED,
            now=founded_at,
        )
        sponsor = derive_persona(PERSONAL_SEED, founded.genesis_id)
        invite = make_event(
            sponsor,
            {
                "type": "invite",
                "granted_role": "owner",
                "expiry": now - 1_000,
                "sponsor": sponsor.public_hex,
                "token_hash": hashlib.sha256(TOKEN.encode()).hexdigest(),
            },
            store.heads(),
            HLC(founded_at + 10_000),
        )
        store.append(invite)
        before_count = len(store)
        claim, persona = mint_member_claim(
            INVITEE_SEED,
            founded.genesis_id,
            invite_ref=invite.event_id,
            heads=store.heads(),
            # Honest event time remains before expiry: only the route's
            # online wall clock can reject this backdated redemption.
            hlc=HLC(founded_at + 20_000),
            token=TOKEN,
        )

    client = TestClient(Starlette(routes=network_routes.ROUTES))
    response = client.post(
        "/api/network/ledger/claim",
        headers={"X-Graph-Org": slug},
        json={"org": slug, "event": claim.to_json().decode("utf-8")},
    )
    assert response.status_code == 400
    assert response.json() == {
        "status": "rejected",
        "reason": "invite-expired",
    }
    with LedgerStore(path) as store:
        assert len(store) == before_count
        assert claim.event_id not in store
        assert store.get_pending_claim(
            store.claim_key(invite.event_id, persona.public_hex)
        ) is None
