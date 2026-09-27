"""The dashboard's side of its passkey gate (tools/dashboard/passkey_gate.py):
the record, the one-time enrollment token, the helper's projection and
container, and the two callbacks the helper makes."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import network_routes, passkey_gate
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.machine_vault import MACHINE_VAULT_AUDITED_SET_ID
from tools.network import passkey_gate as helper

HOST = "dashboard.alice-25dacd12af16373e566c.serve.auto.network"


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs / "personal.db").close()
    GraphDB.create_org_db("machine", type_="personal", path=orgs / "machine.db").close()
    # The machine vault rows the module writes (cookie key, helper secret,
    # the open token) are plain Settings here: nothing is sealed.
    store: dict[str, dict] = {}

    def read_set_key(set_id, key, *, org=None, peers=None):
        if set_id == MACHINE_VAULT_AUDITED_SET_ID:
            return {"payload": store[key]} if key in store else None
        return real_read(set_id, key, org=org, peers=peers)

    def write_by_key(set_id, revision, key, payload, *, org=None, **kw):
        if set_id == MACHINE_VAULT_AUDITED_SET_ID:
            store[key] = dict(payload)
            return "vault-row"
        return real_write(set_id, revision, key, payload, org=org, **kw)

    real_read, real_write = settings_ops.read_set_key, settings_ops.write_by_key
    monkeypatch.setattr(settings_ops, "read_set_key", read_set_key)
    monkeypatch.setattr(settings_ops, "write_by_key", write_by_key)
    runtime = tmp_path / "keycache" / "service-auth"
    runtime.mkdir(parents=True)
    monkeypatch.setattr(passkey_gate, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(passkey_gate, "assert_memory_backed", lambda path: None)
    monkeypatch.setattr(passkey_gate, "_own_runtime_cache", None)
    monkeypatch.setattr(passkey_gate, "_last_materialization", None)
    monkeypatch.setattr(passkey_gate, "own_runtime",
                        lambda: ("autonomy-node:local", {"type": "volume", "source": "autonomy_autonomy-code"}))
    yield SimpleNamespace(vault=store, runtime=runtime)
    GraphDB.close_all_pooled()


def _credential(**overrides):
    return {
        "token": overrides.pop("token", None), "credential_id": "Y3JlZC0x", "public_key": "cHVibGljLWtleQ",
        "sign_count": 0, "transports": ["internal"], "rp_id": HOST, **overrides,
    }


# ── record and token ───────────────────────────────────────────────────

def test_fresh_record_has_nothing_and_enrollment_closed(gate_env):
    assert passkey_gate.record()["credentials"] == []
    assert passkey_gate.enrolled_count() == 0
    assert passkey_gate.enrollment_state() == {"open": False, "expires_at": None}
    assert passkey_gate.open_token() is None
    assert passkey_gate.enrollment_url("https://" + HOST) is None


def test_open_enrollment_mints_a_token_whose_plaintext_lives_only_in_the_vault(gate_env):
    minted = passkey_gate.open_enrollment(opened_by="onboarding", now=1_000_000)
    assert minted["expires_at"] == 1_000_000 + passkey_gate.ENROLLMENT_TTL_S
    row = passkey_gate.record()
    assert row["enrollment"]["open"] is True
    assert row["enrollment"]["token_sha256"] == helper.token_sha256(minted["token"])
    assert minted["token"] not in json.dumps(row)
    assert json.loads(gate_env.vault[passkey_gate.TOKEN_VAULT_KEY]["value"])["token"] == minted["token"]
    assert passkey_gate.open_token(now=1_000_001)["token"] == minted["token"]
    assert passkey_gate.enrollment_url("https://" + HOST, now=1_000_001) == (
        f"https://{HOST}/oauth2/enroll?token={minted['token']}")
    # Expired: closed, no link.
    assert passkey_gate.enrollment_state(now=minted["expires_at"] + 1) == {"open": False, "expires_at": None}
    assert passkey_gate.open_token(now=minted["expires_at"] + 1) is None
    # Re-opening replaces the token; closing wipes it.
    again = passkey_gate.open_enrollment(opened_by="operator", now=1_000_100)
    assert again["token"] != minted["token"]
    assert passkey_gate.open_token(now=1_000_101)["token"] == again["token"]
    passkey_gate.close_enrollment()
    assert passkey_gate.enrollment_state(now=1_000_102)["open"] is False
    assert passkey_gate.open_token(now=1_000_102) is None


def test_register_credential_needs_the_open_token_and_closes_enrollment(gate_env):
    with pytest.raises(passkey_gate.GateRefusal) as refused:
        passkey_gate.register_credential(_credential(token="never-minted"))
    assert refused.value.code == "enrollment_closed" and refused.value.status == 403
    minted = passkey_gate.open_enrollment(opened_by="onboarding")
    saved = passkey_gate.register_credential(_credential(token=minted["token"]))
    assert [c["credential_id"] for c in saved["credentials"]] == ["Y3JlZC0x"]
    assert saved["credentials"][0]["rp_id"] == HOST
    assert saved.get("enrollment") is None
    assert passkey_gate.open_token() is None
    # The token is single-use: the same one is refused now.
    with pytest.raises(passkey_gate.GateRefusal) as again:
        passkey_gate.register_credential(_credential(token=minted["token"], credential_id="b3RoZXI"))
    assert again.value.code == "enrollment_closed"
    # A second enrollment under a new token cannot re-register the same id.
    second = passkey_gate.open_enrollment(opened_by="operator")
    with pytest.raises(passkey_gate.GateRefusal) as dup:
        passkey_gate.register_credential(_credential(token=second["token"]))
    assert dup.value.code == "credential_exists"
    passkey_gate.register_credential(_credential(token=second["token"], credential_id="b3RoZXI"))
    assert passkey_gate.enrolled_count() == 2
    passkey_gate.update_sign_count("Y3JlZC0x", 9)
    passkey_gate.update_sign_count("Y3JlZC0x", 3)  # never backwards
    assert passkey_gate.record()["credentials"][0]["sign_count"] == 9
    assert [c["credential_id"] for c in passkey_gate.revoke_credential("Y3JlZC0x")["credentials"]] == ["b3RoZXI"]
    with pytest.raises(passkey_gate.GateRefusal):
        passkey_gate.revoke_credential("Y3JlZC0x")


# ── the helper's projection and container ──────────────────────────────

def test_materialize_writes_the_projection_and_describes_the_helper_container(gate_env):
    minted = passkey_gate.open_enrollment(opened_by="onboarding")
    first = passkey_gate.materialize_helper(HOST, 4181, "172.16.0.9:8081")
    assert first.helper_id == "dashboard-passkey"
    directory = gate_env.runtime / "dashboard-passkey"
    assert first.runtime_dir == str(directory)
    projection = json.loads((directory / "gate.json").read_text())
    assert projection["rp_id"] == HOST and projection["origin"] == "https://" + HOST
    assert projection["dashboard_upstream"] == "172.16.0.9:8081"
    assert projection["credentials"] == []
    assert projection["enrollment"]["token_sha256"] == helper.token_sha256(minted["token"])
    assert minted["token"] not in (directory / "gate.json").read_text()
    assert set(projection["enrollment"]) == {"open", "token_sha256", "expires_at"}
    assert len(bytes.fromhex((directory / "cookie-secret").read_text())) == 32
    assert (directory / "helper-secret").read_text() == passkey_gate.helper_secret()
    assert (directory / "cookie-secret").stat().st_mode & 0o777 == 0o600
    service = first.service
    assert service["image"] == "autonomy-node:local"
    assert service["command"] == ["--runtime", "/run/gate", "--port", "4181"]
    assert service["volumes"][1] == {"type": "volume", "source": "autonomy_autonomy-code", "target": "/app", "read_only": True}
    # The record changed: the projection is rewritten and the revision moves.
    passkey_gate.register_credential(_credential(token=minted["token"]))
    projection = json.loads((directory / "gate.json").read_text())
    assert projection["enrollment"] is None
    assert projection["credentials"] == [{"credential_id": "Y3JlZC0x", "public_key": "cHVibGljLWtleQ",
                                          "sign_count": 0, "transports": ["internal"]}]
    second = passkey_gate.materialize_helper(HOST, 4181, "172.16.0.9:8081")
    assert second.revision != first.revision
    # A credential enrolled for another hostname is not offered on this one.
    other = passkey_gate.open_enrollment(opened_by="operator")
    passkey_gate.register_credential(_credential(token=other["token"], credential_id="b3RoZXI", rp_id="other.example"))
    assert [c["credential_id"] for c in passkey_gate.projection(HOST, "x:1")["credentials"]] == ["Y3JlZC0x"]


def test_helper_authorization_is_the_materialized_secret(gate_env):
    secret = passkey_gate.helper_secret()
    assert passkey_gate.helper_authorized({"authorization": f"Bearer {secret}"})
    assert not passkey_gate.helper_authorized({"authorization": "Bearer nope"})
    assert not passkey_gate.helper_authorized({"authorization": secret})
    assert not passkey_gate.helper_authorized({})


# ── the callbacks over the routes ──────────────────────────────────────

def _app():
    return Starlette(routes=[
        Route("/api/network/remote-access/gate/registered", network_routes.post_remote_access_gate_registered, methods=["POST"]),
        Route("/api/network/remote-access/gate/sign-count", network_routes.post_remote_access_gate_sign_count, methods=["POST"]),
    ])


def test_gate_callbacks_take_only_the_helper_secret(gate_env):
    minted = passkey_gate.open_enrollment(opened_by="onboarding")
    with TestClient(_app()) as client:
        refused = client.post("/api/network/remote-access/gate/registered", json=_credential(token=minted["token"]),
                              headers={"Authorization": "Bearer wrong"})
        assert refused.status_code == 403 and refused.json()["error"] == "helper_unauthorized"
        assert passkey_gate.enrolled_count() == 0
        headers = {"Authorization": f"Bearer {passkey_gate.helper_secret()}"}
        closed = client.post("/api/network/remote-access/gate/registered", json=_credential(token="stale"), headers=headers)
        assert closed.status_code == 403 and closed.json()["error"] == "enrollment_closed"
        ok = client.post("/api/network/remote-access/gate/registered", json=_credential(token=minted["token"]), headers=headers)
        assert ok.status_code == 200 and ok.json() == {"ok": True, "enrolled": 1}
        assert passkey_gate.enrollment_state()["open"] is False
        count = client.post("/api/network/remote-access/gate/sign-count",
                            json={"credential_id": "Y3JlZC0x", "sign_count": 4}, headers=headers)
        assert count.status_code == 200
        assert passkey_gate.record()["credentials"][0]["sign_count"] == 4
        bad = client.post("/api/network/remote-access/gate/sign-count", json={"sign_count": "x"}, headers=headers)
        assert bad.status_code == 400
