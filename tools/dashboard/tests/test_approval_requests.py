from __future__ import annotations

import asyncio
import base64
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.dao import approval_requests as ar
from tools.dashboard import approvals_routes
from tools.dashboard.event_bus import event_bus


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


def test_byte_exact_round_trip_hostile_payload(client):
    """Byte-exactness over a payload with a trailing newline and non-ASCII
    bytes — the property GitHub's Verified check depends on."""
    payload = ("tree abc\nauthor Dév <d@x> 1 +0000\n"
               "committer Dév <d@x> 1 +0000\n\nfix \U0001f510\n").encode("utf-8")
    assert payload.endswith(b"\n") and any(b >= 0x80 for b in payload)
    r = client.post("/api/approvals", json={
        "kind": "commit_sign", "session": "auto-1",
        "request": {"repo": "repo",
                    "payload_b64": base64.b64encode(payload).decode("ascii")},
    })
    d = client.get(f"/api/approvals/{r.json()['id']}").json()
    assert base64.b64decode(d["request"]["payload_b64"]) == payload


# ── push side of the rendezvous: held GETs + SSE events (no polling) ──


def _async_client():
    app = Starlette(routes=approvals_routes.ROUTES)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver")


def test_wait_get_wakes_on_decision(tmp_path, monkeypatch):
    """GET ?wait=N is held open server-side and returns the moment the decision
    is written — the requester never busy-polls."""
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approval_requests.db")

    async def scenario():
        async with _async_client() as c:
            r = await c.post("/api/approvals", json={
                "kind": "commit_sign", "session": "auto-1",
                "request": {"repo": "repo",
                            "payload_b64": base64.b64encode(COMMIT_PAYLOAD).decode("ascii")},
            })
            rid = r.json()["id"]

            async def decide():
                await asyncio.sleep(0.15)
                return await c.post(f"/api/approvals/{rid}/decision",
                                    json={"approved": True, "signature": "SIG"})

            t0 = time.monotonic()
            held, decision = await asyncio.gather(
                c.get(f"/api/approvals/{rid}?wait=30"), decide())
            elapsed = time.monotonic() - t0
            assert decision.json() == {"ok": True}
            assert held.json()["result"]["signature"] == "SIG"
            assert elapsed < 5, "held GET must wake on the decision, not the wait window"
            # a held GET is the requester's path: no review enrichment attached
            assert "files" not in held.json() and "patch" not in held.json()

    asyncio.run(scenario())


def test_wait_get_times_out_pending_and_decided_returns_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approval_requests.db")

    async def scenario():
        async with _async_client() as c:
            r = await c.post("/api/approvals", json={
                "kind": "commit_sign", "session": "auto-1",
                "request": {"repo": "repo",
                            "payload_b64": base64.b64encode(COMMIT_PAYLOAD).decode("ascii")},
            })
            rid = r.json()["id"]
            # undecided: the held call elapses and reports still-pending
            d = (await c.get(f"/api/approvals/{rid}?wait=0.1")).json()
            assert d["result"] is None
            # decided: a subsequent wait call returns immediately
            await c.post(f"/api/approvals/{rid}/decision", json={"approved": False})
            t0 = time.monotonic()
            d = (await c.get(f"/api/approvals/{rid}?wait=30")).json()
            assert d["result"] == {"approved": False}
            assert time.monotonic() - t0 < 1

    asyncio.run(scenario())


# ── post-approval executors: verdict now, backend execution, outcome in result ──


def _jira_write(client_or_none=None):
    return {"kind": "jira_write", "session": "auto-2",
            "request": {"op": "comment", "key": "ENT-1", "body_markdown": "hi"}}


def test_executor_runs_after_verdict_and_delivers_outcome(tmp_path, monkeypatch):
    """Approve on an executor kind: the operator's POST is acknowledged
    immediately, the operation runs as a backend task, and the requester's held
    GET receives the execution outcome in the single result write."""
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approval_requests.db")
    calls = []

    async def fake_executor(row):
        calls.append(row["request"])
        await asyncio.sleep(0.05)
        return {"ok": True, "ticket": "ENT-1"}

    monkeypatch.setitem(approvals_routes.EXECUTORS, "jira_write", fake_executor)

    async def scenario():
        async with _async_client() as c:
            rid = (await c.post("/api/approvals", json=_jira_write())).json()["id"]
            held_task = asyncio.create_task(c.get(f"/api/approvals/{rid}?wait=30"))
            await asyncio.sleep(0.05)
            d = await c.post(f"/api/approvals/{rid}/decision", json={"approved": True})
            assert d.json() == {"ok": True}   # acknowledged before execution completes
            held = await held_task
            assert held.json()["result"] == {
                "approved": True, "execution": {"ok": True, "ticket": "ENT-1"}}
            assert calls == [{"op": "comment", "key": "ENT-1", "body_markdown": "hi"}]
            # after completion the result row is decided; further decisions refused
            again = await c.post(f"/api/approvals/{rid}/decision", json={"approved": False})
            assert again.json() == {"ok": False}

    asyncio.run(scenario())


def test_executor_never_runs_on_decline(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approval_requests.db")
    calls = []

    async def fake_executor(row):
        calls.append(row)
        return {"ok": True}

    monkeypatch.setitem(approvals_routes.EXECUTORS, "jira_write", fake_executor)
    client = TestClient(Starlette(routes=approvals_routes.ROUTES))
    rid = client.post("/api/approvals", json=_jira_write()).json()["id"]
    assert client.post(f"/api/approvals/{rid}/decision",
                       json={"approved": False}).json() == {"ok": True}
    assert client.get(f"/api/approvals/{rid}").json()["result"] == {"approved": False}
    assert calls == []


def test_executor_failure_reported_not_hung(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approval_requests.db")

    async def fake_executor(row):
        raise ValueError("boom")

    monkeypatch.setitem(approvals_routes.EXECUTORS, "jira_write", fake_executor)

    async def scenario():
        async with _async_client() as c:
            rid = (await c.post("/api/approvals", json=_jira_write())).json()["id"]
            await c.post(f"/api/approvals/{rid}/decision", json={"approved": True})
            d = (await c.get(f"/api/approvals/{rid}?wait=10")).json()
            assert d["result"]["approved"] is True
            assert d["result"]["execution"] == {"ok": False, "error": "boom"}

    asyncio.run(scenario())


def test_double_approve_executes_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approval_requests.db")
    calls = []

    async def slow_executor(row):
        calls.append(1)
        await asyncio.sleep(0.2)
        return {"ok": True}

    monkeypatch.setitem(approvals_routes.EXECUTORS, "jira_write", slow_executor)

    async def scenario():
        async with _async_client() as c:
            rid = (await c.post("/api/approvals", json=_jira_write())).json()["id"]
            first = await c.post(f"/api/approvals/{rid}/decision", json={"approved": True})
            second = await c.post(f"/api/approvals/{rid}/decision", json={"approved": True})
            assert first.json() == {"ok": True}
            assert second.json() == {"ok": False}   # already executing
            d = (await c.get(f"/api/approvals/{rid}?wait=10")).json()
            assert d["result"]["execution"] == {"ok": True}
            assert calls == [1]

    asyncio.run(scenario())


def test_sse_events_on_create_and_decision(tmp_path, monkeypatch):
    """The viewer trigger: creating a request broadcasts approval:pending on the
    event bus; deciding it broadcasts approval:decided. No poll anywhere."""
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approval_requests.db")

    async def scenario():
        queue = event_bus.subscribe()
        try:
            async with _async_client() as c:
                r = await c.post("/api/approvals", json={
                    "kind": "commit_sign", "session": "auto-9",
                    "request": {"repo": "repo",
                                "payload_b64": base64.b64encode(COMMIT_PAYLOAD).decode("ascii")},
                })
                rid = r.json()["id"]
                await c.post(f"/api/approvals/{rid}/decision", json={"approved": False})
            events = []
            while not queue.empty():
                topic, data, _seq = queue.get_nowait()
                if topic.startswith("approval:"):
                    events.append((topic, data))
            assert ("approval:pending",
                    {"id": rid, "kind": "commit_sign", "session": "auto-9"}) in events
            assert ("approval:decided",
                    {"id": rid, "kind": "commit_sign", "session": "auto-9"}) in events
        finally:
            event_bus.unsubscribe(queue)

    asyncio.run(scenario())
