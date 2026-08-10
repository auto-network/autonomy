"""Agent-triggered secure-setting provisioning through the approval rendezvous.

The browser side is simulated with the Python idkit sealer — the JS/Python
wire-format parity is separately proven by
``tests/ceremony/test_sealing_parity.py``, so a Python ``seal`` here stands
in exactly for ``sealToEncapsulationKey`` in the operator's browser.
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import approvals_routes, secure_setting_keys
from tools.dashboard.dao import approval_requests as ar
from tools.graph import settings_ops
from tools.graph.schemas.secure_setting import SECURE_SETTING_SET_ID
from tools.network.idkit import seal, seal_open


ORG = "acme"
SECRET_VALUE = "hunter2-super-secret-password"
WORKSPACES = ["finance-ws", "ops-ws"]

REQUEST = {
    "target_key": "connector.eversource.login",
    "origin": "eversource.com",
    "schema": {
        "Username": {"key": "username", "secret": False},
        "Password": "password",
    },
    "title": "Eversource login",
    "description": "Credentials for the Eversource usage connector.",
    "org": ORG,
    "workspaces": WORKSPACES,
}


def _app() -> Starlette:
    return Starlette(routes=list(approvals_routes.ROUTES))


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    GraphDB(orgs_dir / f"{ORG}.db").close()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("APPROVAL_REQUESTS_DB", str(tmp_path / "approvals.db"))
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approvals.db")
    monkeypatch.setenv("REPL_LOGIN_KEY_FILE", str(tmp_path / "repl-login.key"))
    monkeypatch.setenv("DASHBOARD_AUTH", "off")

    # The allowlist is validated against known workspaces at request time;
    # this suite runs without workspace Settings, so stand in for the map.
    import agents.workspace_settings as workspace_settings

    def fake_get_workspace(workspace_id):
        if workspace_id not in WORKSPACES:
            raise KeyError(f"unknown workspace: {workspace_id!r}")
        return object()

    monkeypatch.setattr(workspace_settings, "get_workspace", fake_get_workspace)
    client = TestClient(_app(), base_url="https://localhost:8080")
    yield client, tmp_path
    GraphDB.close_all_pooled()


def _queue(client: TestClient, request: dict | None = None,
           session: str = "auto-0806-165334") -> tuple[str, dict]:
    response = client.post("/api/approvals", json={
        "kind": "secure_setting", "session": session,
        "request": request if request is not None else dict(REQUEST),
    })
    assert response.status_code == 200, response.text
    rid = response.json()["id"]
    rendered = client.get(f"/api/approvals/{rid}")
    assert rendered.status_code == 200, rendered.text
    return rid, rendered.json()["staged"]


def _seal_form(staged: dict, values: dict) -> dict[str, str]:
    """What the browser does: JSON-encode the form dict once, then seal it
    to the staged recipient key under each workspace's staged purpose."""
    plaintext = json.dumps(values).encode()
    return {ws: seal(plaintext, staged["recipient_pub"],
                     staged["purposes"][ws]).hex()
            for ws in staged["workspaces"]}


def _approve(client: TestClient, rid: str, body: dict) -> dict:
    response = client.post(f"/api/approvals/{rid}/decision",
                           json={"approved": True, **body})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    result = client.get(f"/api/approvals/{rid}?wait=10").json()["result"]
    return result["execution"]


def test_full_provisioning_round_trip(env):
    client, tmp_path = env
    rid, staged = _queue(client)

    # The staged sealing context is complete and server-frozen.
    assert set(staged) == {"v", "nonce", "recipient_pub", "key_id", "purposes",
                           "workspaces", "fields", "target_key", "org"}
    assert len(staged["nonce"]) == 64
    assert len(staged["recipient_pub"]) == 64
    assert staged["workspaces"] == sorted(WORKSPACES)
    assert staged["purposes"] == {
        ws: (f"autonomy.secure-setting.v2|{ORG}|connector.eversource.login|"
             f"{staged['nonce']}|workspace={ws}")
        for ws in WORKSPACES
    }
    assert staged["fields"] == [
        {"label": "Username", "key": "username", "secret": False, "placeholder": ""},
        {"label": "Password", "key": "password", "secret": True, "placeholder": ""},
    ]

    values = {"username": "alex@example.com", "password": SECRET_VALUE}
    execution = _approve(client, rid, {
        "nonce": staged["nonce"],
        "sealed_payloads": _seal_form(staged, values),
    })
    assert execution["ok"] is True, execution
    assert execution["set_id"] == SECURE_SETTING_SET_ID
    assert execution["key"] == "connector.eversource.login"
    assert execution["org"] == ORG

    # The stored member holds ciphertexts + binding metadata: no plaintext
    # and — deliberately — no stored purpose string to trust.
    members = settings_ops.read_set(SECURE_SETTING_SET_ID, org=ORG).members
    assert len(members) == 1
    payload = members[0].payload
    assert payload["key_id"] == staged["key_id"]
    assert "purpose" not in payload
    assert payload["nonce"] == staged["nonce"]
    assert payload["workspaces"] == sorted(WORKSPACES)
    assert set(payload["ciphertexts_hex"]) == set(WORKSPACES)
    assert payload["origin"] == "eversource.com"
    assert payload["payload_keys"] == ["username", "password"]
    assert payload["approval_id"] == rid
    assert payload["requested_by_session"] == "auto-0806-165334"
    assert SECRET_VALUE not in json.dumps(payload)

    # Each record opens only under a RECONSTRUCTED label naming its own
    # workspace — and does not open under a different workspace's label.
    private_hex = (tmp_path / "repl-login.key").read_text().strip()
    for ws in WORKSPACES:
        purpose = (f"autonomy.secure-setting.v2|{ORG}|"
                   f"connector.eversource.login|{payload['nonce']}|workspace={ws}")
        opened = seal_open(bytes.fromhex(payload["ciphertexts_hex"][ws]),
                           private_hex, purpose)
        assert json.loads(opened) == values
    with pytest.raises(Exception):
        seal_open(
            bytes.fromhex(payload["ciphertexts_hex"][WORKSPACES[0]]),
            private_hex,
            f"autonomy.secure-setting.v2|{ORG}|connector.eversource.login|"
            f"{payload['nonce']}|workspace={WORKSPACES[1]}")

    # The plaintext appears nowhere in either durable store.
    for db_file in [tmp_path / "approvals.db", tmp_path / "orgs" / f"{ORG}.db"]:
        assert SECRET_VALUE.encode() not in db_file.read_bytes()


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("target_key"),
    lambda r: r.pop("org"),
    lambda r: r.update(extra="smuggled"),
    lambda r: r.update(target_key="Has Spaces"),
    lambda r: r.update(org="../evil"),
    lambda r: r.update(schema={}),
    lambda r: r.update(schema={"A": "dup", "B": "dup"}),
    lambda r: r.update(schema={"A": {"key": "k", "evil": True}}),
    lambda r: r.update(schema="username"),
    lambda r: r.update(title=""),
    lambda r: r.update(description="x" * 2001),
])
def test_create_rejects_malformed_requests(env, mutate):
    client, _tmp = env
    request = json.loads(json.dumps(REQUEST))
    mutate(request)
    response = client.post("/api/approvals", json={
        "kind": "secure_setting", "session": "auto-1", "request": request,
    })
    assert response.status_code == 400, response.text


def test_decision_rejects_bad_nonce_extra_fields_and_bad_records(env):
    client, _tmp = env
    values = {"username": "u", "password": "p"}

    rid, staged = _queue(client)
    sealed = _seal_form(staged, values)
    assert _approve(client, rid, {"nonce": "0" * 64, "sealed_payload": sealed}) == {
        "ok": False, "error": "the decision nonce does not match this request",
    }

    rid, staged = _queue(client)
    execution = _approve(client, rid, {
        "nonce": staged["nonce"], "sealed_payload": _seal_form(staged, values),
        "plaintext": "smuggled",
    })
    assert execution == {"ok": False, "error": (
        "secure_setting approval must carry only approved, nonce, "
        "and sealed_payload"
    )}

    for bad, error in [
        ("zz", "sealed_payload must be lowercase hex"),
        ("00" * 40, "sealed_payload is truncated"),
        ("02" + "00" * 60, "sealed_payload does not use the expected sealing suite"),
        ("01" + "00" * (64 * 1024), "sealed_payload exceeds the size limit"),
    ]:
        rid, staged = _queue(client)
        execution = _approve(client, rid, {"nonce": staged["nonce"],
                                           "sealed_payload": bad})
        assert execution == {"ok": False, "error": error}


def test_nonce_is_single_use_per_request(env):
    client, _tmp = env
    rid, staged = _queue(client)
    sealed = _seal_form(staged, {"username": "u", "password": "p"})
    assert _approve(client, rid, {"nonce": staged["nonce"],
                                  "sealed_payload": sealed})["ok"] is True
    replay = client.post(f"/api/approvals/{rid}/decision", json={
        "approved": True, "nonce": staged["nonce"], "sealed_payload": sealed,
    })
    assert replay.json() == {
        "ok": False, "error": "This approval has already been completed.",
    }


def test_staged_context_is_write_once(env):
    client, _tmp = env
    rid, staged = _queue(client)
    forged = {**staged, "recipient_pub": "ab" * 32}
    assert ar.set_staged(rid, forged) is False


def test_key_file_is_0600_stable_and_reclamped(env):
    client, tmp_path = env
    _rid, staged = _queue(client)
    key_file = tmp_path / "repl-login.key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    pub_again, key_id_again = secure_setting_keys.recipient_public_key()
    assert pub_again == staged["recipient_pub"]
    assert key_id_again == staged["key_id"]

    os.chmod(key_file, 0o644)
    secure_setting_keys.recipient_public_key()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    key_file.write_text("not a key")
    response = client.post("/api/approvals", json={
        "kind": "secure_setting", "session": "auto-1", "request": dict(REQUEST),
    })
    assert response.status_code == 400
    assert "recipient key" in response.json()["error"]


def test_rotated_recipient_key_fails_closed(env):
    client, tmp_path = env
    rid, staged = _queue(client)
    sealed = _seal_form(staged, {"username": "u", "password": "p"})
    (tmp_path / "repl-login.key").unlink()  # forces a fresh key on next use
    execution = _approve(client, rid, {"nonce": staged["nonce"],
                                       "sealed_payload": sealed})
    assert execution == {"ok": False, "error": (
        "the host recipient key changed after this request was staged — "
        "decline and request provisioning again"
    )}
    assert settings_ops.read_set(SECURE_SETTING_SET_ID, org=ORG).members == []


def test_locked_dashboard_cannot_decide(env, monkeypatch):
    client, _tmp = env
    rid, staged = _queue(client)
    sealed = _seal_form(staged, {"username": "u", "password": "p"})
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    refused = client.post(f"/api/approvals/{rid}/decision", json={
        "approved": True, "nonce": staged["nonce"], "sealed_payload": sealed,
    })
    assert refused.status_code == 401
    assert client.get(f"/api/approvals/{rid}").json()["result"] is None


def test_reprovision_upserts_in_place(env):
    client, _tmp = env
    rid, staged = _queue(client)
    assert _approve(client, rid, {
        "nonce": staged["nonce"],
        "sealed_payload": _seal_form(staged, {"username": "u", "password": "old"}),
    })["ok"] is True
    first = settings_ops.read_set(SECURE_SETTING_SET_ID, org=ORG).members

    rid2, staged2 = _queue(client)
    assert _approve(client, rid2, {
        "nonce": staged2["nonce"],
        "sealed_payload": _seal_form(staged2, {"username": "u", "password": "new"}),
    })["ok"] is True
    second = settings_ops.read_set(SECURE_SETTING_SET_ID, org=ORG).members

    assert len(first) == 1 and len(second) == 1
    assert first[0].id == second[0].id  # one evolving row, not an append
    assert first[0].payload["ciphertext_hex"] != second[0].payload["ciphertext_hex"]


def test_decline_stores_nothing(env):
    client, _tmp = env
    rid, _staged = _queue(client)
    declined = client.post(f"/api/approvals/{rid}/decision",
                           json={"approved": False})
    assert declined.json() == {"ok": True}
    assert settings_ops.read_set(SECURE_SETTING_SET_ID, org=ORG).members == []
