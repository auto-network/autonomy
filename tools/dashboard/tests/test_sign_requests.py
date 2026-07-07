from __future__ import annotations

import base64

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.dao import sign_requests as sr
from tools.dashboard import sign_requests_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "DB_PATH", tmp_path / "sign_requests.db")
    return TestClient(Starlette(routes=sign_requests_routes.ROUTES))


PAYLOAD = b"tree abc\nauthor Dev <d@x> 1 +0000\ncommitter Dev <d@x> 1 +0000\n\nfix\n"


def _create(client):
    r = client.post("/api/sign-requests?session=auto-1&repo=org/repo", content=PAYLOAD)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_full_sign_flow(client):
    rid = _create(client)
    # pending: exact bytes come back, signature null
    d = client.get(f"/api/sign-requests/{rid}").json()
    assert base64.b64decode(d["payload_b64"]) == PAYLOAD   # byte-exact round trip
    assert d["signature"] is None
    assert d["session"] == "auto-1" and d["repo"] == "org/repo"
    # the session shows a pending request (backs commit_sign_pending)
    assert sr.pending_id_for_session("auto-1") == rid
    # operator signs
    ok = client.post(f"/api/sign-requests/{rid}/signature",
                     json={"armored_signature": "-----BEGIN PGP SIGNATURE-----\nx\n-----END PGP SIGNATURE-----"})
    assert ok.json() == {"ok": True}
    # now signed; no longer pending
    d = client.get(f"/api/sign-requests/{rid}").json()
    assert d["signature"].startswith("-----BEGIN PGP SIGNATURE-----")
    assert sr.pending_id_for_session("auto-1") is None


def test_decline_sets_empty_signature(client):
    rid = _create(client)
    client.post(f"/api/sign-requests/{rid}/signature", json={"armored_signature": ""})
    d = client.get(f"/api/sign-requests/{rid}").json()
    assert d["signature"] == ""            # '' == declined (shim surfaces to agent)
    assert sr.pending_id_for_session("auto-1") is None


def test_first_writer_wins(client):
    rid = _create(client)
    assert client.post(f"/api/sign-requests/{rid}/signature", json={"armored_signature": "SIG"}).json() == {"ok": True}
    # a second write to an already-resolved request does nothing
    assert client.post(f"/api/sign-requests/{rid}/signature", json={"armored_signature": "OTHER"}).json() == {"ok": False}
    assert client.get(f"/api/sign-requests/{rid}").json()["signature"] == "SIG"


def test_missing_fields_and_unknown(client):
    assert client.post("/api/sign-requests?session=&repo=x", content=b"y").status_code == 400
    assert client.post("/api/sign-requests?session=a&repo=b", content=b"").status_code == 400
    assert client.get("/api/sign-requests/nope").status_code == 404
    assert client.post("/api/sign-requests/nope/signature", json={"armored_signature": "x"}).status_code == 404
