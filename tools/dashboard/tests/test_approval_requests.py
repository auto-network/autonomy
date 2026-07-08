from __future__ import annotations

import base64

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.dao import approval_requests as ar
from tools.dashboard import approvals_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approval_requests.db")
    return TestClient(Starlette(routes=approvals_routes.ROUTES))


COMMIT_PAYLOAD = b"tree abc\nauthor Dev <d@x> 1 +0000\ncommitter Dev <d@x> 1 +0000\n\nfix\n"


def _create_commit_sign(client, session="auto-1"):
    """Create a commit_sign request exactly the way the shim does."""
    r = client.post("/api/approvals", json={
        "kind": "commit_sign", "session": session,
        "request": {"repo": "repo",
                    "payload_b64": base64.b64encode(COMMIT_PAYLOAD).decode("ascii")},
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_commit_sign_flow(client):
    """The signing flow on the generic primitive: byte-exact payload round trip,
    result NULL while pending, approve carries the armored signature."""
    rid = _create_commit_sign(client)
    d = client.get(f"/api/approvals/{rid}").json()
    assert d["kind"] == "commit_sign" and d["session"] == "auto-1"
    assert base64.b64decode(d["request"]["payload_b64"]) == COMMIT_PAYLOAD
    assert d["result"] is None                       # the shim's PENDING state
    assert ar.pending_for_session("auto-1") == {"id": rid, "kind": "commit_sign"}
    sig = "-----BEGIN PGP SIGNATURE-----\nx\n-----END PGP SIGNATURE-----"
    ok = client.post(f"/api/approvals/{rid}/decision",
                     json={"approved": True, "signature": sig})
    assert ok.json() == {"ok": True}
    d = client.get(f"/api/approvals/{rid}").json()
    # exactly what the shim polls for: approved + a non-empty signature
    assert d["result"]["approved"] is True
    assert d["result"]["signature"].startswith("-----BEGIN PGP SIGNATURE-----")
    assert ar.pending_for_session("auto-1") is None


def test_no_secret_kind_full_flow(client):
    """A Jira-style kind needs nothing beyond the generic primitive: the stored
    request is self-describing, approval is a bare {approved: true} with optional
    operator edits, no signature/passphrase anywhere."""
    r = client.post("/api/approvals", json={
        "kind": "jira_write", "session": "auto-2",
        "request": {"op": "create", "summary": "Fix the flux capacitor",
                    "description": "It fluxes when it should capacit."},
    })
    rid = r.json()["id"]
    assert ar.pending_for_session("auto-2") == {"id": rid, "kind": "jira_write"}
    d = client.get(f"/api/approvals/{rid}").json()
    assert d["request"]["summary"] == "Fix the flux capacitor"
    assert d["result"] is None
    # no enricher registered for this kind -> no extra fields, no error
    assert "files" not in d and "patch" not in d
    ok = client.post(f"/api/approvals/{rid}/decision",
                     json={"approved": True, "edits": {"summary": "Fix flux capacitor drift"}})
    assert ok.json() == {"ok": True}
    d = client.get(f"/api/approvals/{rid}").json()
    assert d["result"] == {"approved": True, "edits": {"summary": "Fix flux capacitor drift"}}
    assert ar.pending_for_session("auto-2") is None


def test_decline_is_kind_agnostic(client):
    """Decline is the same {approved: false} for every kind; the shim maps a
    non-approved result to DECLINED and fails the commit."""
    rid = _create_commit_sign(client)
    client.post(f"/api/approvals/{rid}/decision", json={"approved": False})
    d = client.get(f"/api/approvals/{rid}").json()
    assert d["result"] == {"approved": False}
    assert ar.pending_for_session("auto-1") is None


def test_first_writer_wins(client):
    rid = _create_commit_sign(client)
    assert client.post(f"/api/approvals/{rid}/decision",
                       json={"approved": True, "signature": "SIG"}).json() == {"ok": True}
    # a second decision on an already-decided request does nothing
    assert client.post(f"/api/approvals/{rid}/decision",
                       json={"approved": False}).json() == {"ok": False}
    assert client.get(f"/api/approvals/{rid}").json()["result"]["signature"] == "SIG"


def test_oldest_pending_first(client):
    """pending_for_session returns the OLDEST pending request so blocked
    requesters are served in order."""
    first = _create_commit_sign(client)
    second = _create_commit_sign(client)
    assert ar.pending_for_session("auto-1")["id"] == first
    client.post(f"/api/approvals/{first}/decision", json={"approved": False})
    assert ar.pending_for_session("auto-1")["id"] == second


def test_validation_and_unknown(client):
    # create: kind, session, and a non-empty request object are all required
    assert client.post("/api/approvals", json={"kind": "", "session": "s",
                                               "request": {"a": 1}}).status_code == 400
    assert client.post("/api/approvals", json={"kind": "k", "session": "",
                                               "request": {"a": 1}}).status_code == 400
    assert client.post("/api/approvals", json={"kind": "k", "session": "s",
                                               "request": {}}).status_code == 400
    assert client.post("/api/approvals", json={"kind": "k", "session": "s",
                                               "request": "not-an-object"}).status_code == 400
    # decision: approved must be a boolean
    rid = _create_commit_sign(client)
    assert client.post(f"/api/approvals/{rid}/decision",
                       json={"approved": "yes"}).status_code == 400
    # unknown ids
    assert client.get("/api/approvals/nope").status_code == 404
    assert client.post("/api/approvals/nope/decision",
                       json={"approved": True}).status_code == 404


def test_commit_sign_diff_from_parent_and_tree(tmp_path):
    """The live diff-tree of a not-yet-committed change (parent + tree), the way
    the commit_sign enricher renders a pending request for the overlay."""
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
    """When the worktree/tree isn't reachable, GET still returns 200 with an
    empty diff (never 500) — the payload is enough to review the message."""
    rid = _create_commit_sign(client)
    d = client.get(f"/api/approvals/{rid}").json()
    assert d["files"] == [] and d["patch"] == ""   # no worktree in this test -> graceful
    assert base64.b64decode(d["request"]["payload_b64"]) == COMMIT_PAYLOAD
