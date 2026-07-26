"""Node virtual-authenticator acceptance against the real dashboard routes."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import identity_routes, unlock_routes
from tools.dashboard.dao import identity_sessions
from tools.graph import settings_ops
from tools.graph.schemas.personal_identity import PASSKEY_SET_ID
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import encrypt_root_key

ORG = "nodeauthenticator"
HOST = "localhost:8080"
DRIVER = (
    Path(__file__).resolve().parents[1]
    / "static" / "js" / "ceremony" / "tests"
    / "authenticator-node-driver.mjs"
)
NODE_TEST = (
    Path(__file__).resolve().parents[1]
    / "static" / "js" / "ceremony" / "tests"
    / "authenticator-node.test.mjs"
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.fixture
def route_client(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.setenv("GRAPH_ORG", ORG)
    monkeypatch.setenv(
        "DASHBOARD_SESSION_SECRET_FILE",
        str(tmp_path / "session.secret"),
    )
    monkeypatch.setenv(
        "DASHBOARD_IDENTITY_SESSION_DB",
        str(tmp_path / "identity-sessions.db"),
    )
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    identity_routes._pending.clear()
    unlock_routes._assert_pending.clear()
    unlock_routes._pw_pending.clear()
    unlock_routes._secret_cache.update({"path": None, "value": None})
    unlock_routes._enforce_cache.update({"at": 0.0, "value": None})
    identity_sessions.reset_for_tests()
    app = Starlette(routes=[
        *identity_routes.ROUTES,
        *unlock_routes.ROUTES,
    ])
    with TestClient(app, base_url=f"https://{HOST}") as client:
        yield client
    identity_routes._pending.clear()
    unlock_routes._assert_pending.clear()
    unlock_routes._pw_pending.clear()
    unlock_routes._secret_cache.update({"path": None, "value": None})
    unlock_routes._enforce_cache.update({"at": 0.0, "value": None})
    identity_sessions.reset_for_tests()
    GraphDB.close_all_pooled()


class NodeAuthenticator:
    def __init__(self):
        self.process = subprocess.Popen(
            ["node", str(DRIVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def call(self, op: str, **payload):
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(json.dumps({"op": op, **payload}) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        assert line, self.process.stderr.read() if self.process.stderr else ""
        response = json.loads(line)
        assert response["ok"], response
        return response["result"]

    def close(self):
        if self.process.stdin is not None:
            self.process.stdin.close()
        self.process.wait(timeout=10)
        assert self.process.returncode == 0, (
            self.process.stderr.read() if self.process.stderr else ""
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_virtual_authenticator_emits_expected_structure_and_prf():
    result = subprocess.run(
        ["node", str(NODE_TEST)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "authenticator-node tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_virtual_authenticator_registers_unlocks_and_signals_clone(
    route_client,
):
    root = KeyPair.generate()
    armor = encrypt_root_key(
        root,
        "node virtual authenticator password",
        iterations=10_000,
    )
    personal = route_client.post(
        "/api/identity/personal",
        json={
            "display_name": "Node Operator",
            "armored_private_key": armor,
        },
    )
    assert personal.status_code == 200, personal.text

    authenticator = NodeAuthenticator()
    try:
        options = route_client.post(
            "/api/identity/passkey/register-options",
            json={},
        )
        assert options.status_code == 200, options.text
        minted = options.json()
        credential = authenticator.call(
            "create",
            options={
                "rpId": minted["rp_id"],
                "origin": minted["origin"],
                "challenge": minted["options"]["challenge"],
                "credentialId": _b64url(b"node-virtual-credential-0001"),
                "prf": True,
            },
        )
        registered = route_client.post(
            "/api/identity/passkey/register",
            json={
                "label": "Node virtual authenticator",
                "credential": credential,
            },
        )
        assert registered.status_code == 200, registered.text
        assert registered.json()["credential_id"] == credential["rawId"]

        rows = settings_ops.read_set(
            PASSKEY_SET_ID,
            org=settings_ops.CALLER_ORG,
        ).members
        assert len(rows) == 1
        assert rows[0].payload["credential_id"] == credential["rawId"]
        assert rows[0].payload["sign_count"] == 0

        route_client.cookies.clear()
        unlock_options = route_client.post(
            "/api/identity/unlock/passkey/options",
            json={},
        )
        assert unlock_options.status_code == 200, unlock_options.text
        unlock_minted = unlock_options.json()
        assertion = authenticator.call(
            "get",
            options={
                "rpId": unlock_minted["rp_id"],
                "origin": unlock_minted["origin"],
                "challenge": unlock_minted["options"]["challenge"],
                "credentialId": credential["rawId"],
            },
        )
        unlocked = route_client.post(
            "/api/identity/unlock/passkey",
            json={"credential": assertion},
        )
        assert unlocked.status_code == 200, unlocked.text
        assert unlocked.json() == {"ok": True, "method": "passkey"}
        assert unlock_routes.SESSION_COOKIE in route_client.cookies

        advanced = settings_ops.read_set(
            PASSKEY_SET_ID,
            org=settings_ops.CALLER_ORG,
        ).members[0]
        assert advanced.payload["sign_count"] == 1

        route_client.cookies.clear()
        stale_options = route_client.post(
            "/api/identity/unlock/passkey/options",
            json={},
        )
        assert stale_options.status_code == 200, stale_options.text
        stale_minted = stale_options.json()
        authenticator.call(
            "setSignCount",
            credentialId=credential["rawId"],
            signCount=0,
        )
        stale_assertion = authenticator.call(
            "get",
            options={
                "rpId": stale_minted["rp_id"],
                "origin": stale_minted["origin"],
                "challenge": stale_minted["options"]["challenge"],
                "credentialId": credential["rawId"],
            },
        )
        stale = route_client.post(
            "/api/identity/unlock/passkey",
            json={"credential": stale_assertion},
        )
        assert stale.status_code == 403, stale.text
        assert "sign count" in stale.json()["error"].lower()
    finally:
        authenticator.close()
