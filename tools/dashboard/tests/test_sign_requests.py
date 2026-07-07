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


def test_sign_request_diff_from_parent_and_tree(tmp_path):
    """The live diff-tree of a not-yet-committed change (parent + tree), the way
    the GET enriches a pending request for the overlay."""
    import subprocess
    from agents import workspace_manager as wm
    wt = tmp_path / "auto-1" / "repo"
    wt.mkdir(parents=True)
    def g(*a):
        subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)
    def out(*a):
        return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True).stdout.strip()
    g("init", "-q"); g("config", "user.email", "d@x"); g("config", "user.name", "d")
    (wt / "f1").write_text("a\nb\n"); g("add", "."); g("commit", "-qm", "base")
    parent = out("rev-parse", "HEAD")
    (wt / "f1").write_text("a\nB\n"); (wt / "f2").write_text("new\n"); g("add", "-A")
    tree = out("write-tree")   # the tree that would go in the payload — no commit yet
    d = wm.sign_request_diff("auto-1", "repo", parent, tree, worktrees_dir=tmp_path)
    statuses = {(f["status"], f["path"]) for f in d["files"]}
    assert ("M", "f1") in statuses and ("A", "f2") in statuses
    assert "B" in d["patch"] and "f2" in d["patch"]


def test_get_degrades_without_worktree(client):
    """When the worktree/tree isn't reachable, GET still returns 200 with empty
    diff (never 500) — the payload is enough to review the message."""
    rid = _create(client)
    d = client.get(f"/api/sign-requests/{rid}").json()
    assert d["files"] == [] and d["patch"] == ""   # no worktree in this test -> graceful
    assert base64.b64decode(d["payload_b64"]) == PAYLOAD
