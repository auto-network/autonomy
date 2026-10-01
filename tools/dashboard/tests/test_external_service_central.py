"""external_service_access as a Central approval (auto-fkhq0.26).

Against the real ApprovalService (in-memory store), a real auth.db in
tmp_path and the real dropbox enrollment routes, with the machine identity
and the clock stubbed:

- the device's ``id`` is its poll secret and never reaches Central (neither
  it nor its hash); the Central approval id is public and polls nothing;
- a public caller cannot open this kind or choose its audience/capabilities;
- the decision is ``{ttl_seconds}`` on a grant and ``{}`` on a decline;
- the bearer is minted at the device's poll on the accepting machine while
  the granted lifetime lasts, rotating atomically (one live bearer by exact
  name); a late Grant is collected at the next poll;
- decline, expiry, an ended lifetime and another machine mint nothing, and
  an enrollment nothing can be collected from is forgotten at the next enrollment;
- neither the uvicorn access log nor the request middleware writes the poll
  secret.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import socket
import threading
import time
import urllib.request

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient
import uvicorn

from tools.dashboard import api_auth, dropbox_routes
from tools.dashboard import external_service_approvals as ext
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    HumanApprovalActor,
    InMemoryApprovalStore,
)
from tools.dashboard.dao import auth_db
from tools.graph.schemas.central_attention import ApprovalRequestV1
from tools.network.idkit.keys import KeyPair

ROOT = KeyPair.generate()
HERE = "h" * 43
ELSEWHERE = "e" * 43


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class Machine:
    def __init__(self):
        self.destination = HERE

    def __call__(self):
        return self.destination


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved = auth_db._conn
    auth_db.init_db(tmp_path / "auth.db")
    dropbox_routes._enrollment_attempts.clear()
    clock, machine = Clock(), Machine()
    registry = build_production_registry(runtimes={ext.KIND: ext.build_approval_runtime(
        destination_resolver=machine, machine_label=lambda: "Home")})
    approvals = ApprovalService(
        registry=registry, store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: ROOT.public_hex, clock=clock,
    )
    desk = ext.EnrollmentDesk(approvals=approvals, destination_resolver=machine, clock=clock)
    monkeypatch.setattr(dropbox_routes, "_enrollment_desk", lambda: desk)
    try:
        yield type("Env", (), dict(approvals=approvals, desk=desk, clock=clock, machine=machine,
                                   tmp=tmp_path))
    finally:
        if auth_db._conn is not None and auth_db._conn is not saved:
            auth_db._conn.close()
        auth_db._conn = saved


def _app():
    return Starlette(routes=dropbox_routes.ROUTES)


def _enroll(client, **body):
    created = client.post("/api/dropbox/enrollments", json={
        "label": "iPhone Action Button", "requested_ttl_seconds": 31536000, **body})
    assert created.status_code == 202, created.text
    return created.json()["id"], created.json()["sourceApprovalId"]


def _decide(env, approval_id, outcome="granted", decision=None):
    if decision is None:
        decision = {"ttl_seconds": 86400} if outcome == "granted" else {}
    return env.approvals.decide(approval_id, HumanApprovalActor._verified(ROOT.public_hex),
                                outcome=outcome, decision=decision)


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _live(approval_id):
    conn = auth_db.get_conn()
    return [r["token_hash"] for r in conn.execute(
        "SELECT token_hash FROM session_tokens WHERE tmux_name=? AND revoked_at IS NULL",
        (f"dropbox-upload:{approval_id}",)).fetchall()]


def _named(approval_id):
    conn = auth_db.get_conn()
    return conn.execute("SELECT COUNT(*) FROM session_tokens WHERE tmux_name=?",
                        (f"dropbox-upload:{approval_id}",)).fetchone()[0]


def _central_text(env, approval_id):
    status = env.approvals.status(approval_id)
    return json.dumps([status.request.payload,
                       status.resolution.payload if status.resolution else None,
                       env.desk.operator_result(status)])


# ── enrollment ───────────────────────────────────────────────────────


def test_the_poll_secret_never_reaches_central_and_the_registry_owns_the_authority(env):
    with TestClient(_app()) as client:
        secret, approval_id = _enroll(
            client, resource_audience="sessions",
            capabilities=[{"method": "GET", "path": "/api/sessions"}])
    assert secret != approval_id
    payload = env.approvals.status(approval_id).request.payload
    ApprovalRequestV1.validate(payload)
    assert payload["request"]["resource_audience"] == "global_operator_dropbox"
    assert payload["request"]["capabilities"] == [{"method": "POST", "path": "/api/dropbox"}]
    assert payload["safe_review"]["requester_label"] == "iPhone Action Button"
    assert payload["application_scope"] == "dropbox"
    text = _central_text(env, approval_id)
    assert secret not in text and _hash(secret) not in text


def test_a_public_caller_cannot_open_this_kind(env):
    session = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject="auto-x")
    with pytest.raises(ApprovalServiceError):
        env.approvals.create_from_principal(ext.KIND, session, {
            "label": "x", "requested_ttl_seconds": None})


def test_the_public_approval_id_and_a_wrong_secret_poll_nothing(env):
    with TestClient(_app()) as client:
        secret, approval_id = _enroll(client)
        _decide(env, approval_id)
        for probe in (approval_id, secret[:-1] + ("A" if secret[-1] != "A" else "B"), "x" * 10):
            assert client.get(f"/api/dropbox/enrollments/{probe}").status_code == 404
    assert _named(approval_id) == 0


def test_the_decision_is_a_lifetime_on_grant_and_nothing_on_decline(env):
    with TestClient(_app()) as client:
        _secret, approval_id = _enroll(client)
    for bad in ({}, {"ttl_seconds": 0}, {"ttl_seconds": "3600"},
                {"ttl_seconds": 3600, "token": "chosen"}):
        with pytest.raises(ApprovalServiceError):
            _decide(env, approval_id, decision=bad)
    with pytest.raises(ApprovalServiceError):
        _decide(env, approval_id, outcome="declined", decision={"ttl_seconds": 1})
    assert _decide(env, approval_id, decision={"ttl_seconds": None}).payload["decision"] == {
        "ttl_seconds": None}


# ── collection ───────────────────────────────────────────────────────


def test_the_device_collects_a_rotating_upload_only_bearer(env):
    with TestClient(_app()) as client:
        secret, approval_id = _enroll(client)
        assert client.get(f"/api/dropbox/enrollments/{secret}").json()["status"] == "pending"
        assert env.desk.operator_result(env.approvals.status(approval_id))["state"] == ext.PENDING
        _decide(env, approval_id)
        assert env.desk.operator_result(env.approvals.status(approval_id))["state"] == ext.AWAITING
        assert _named(approval_id) == 0  # the Grant alone mints nothing

        first = client.get(f"/api/dropbox/enrollments/{secret}?wait=5").json()
        assert first["status"] == "approved"
        assert first["sourceApprovalId"] == approval_id
        assert first["capabilities"] == [{"method": "POST", "path": "/api/dropbox"}]
        assert first["expires_at"] == env.clock.t + 86400
        scope = auth_db.resolve_scoped_service_token(_hash(first["token"]), method="POST",
                                                     path="/api/dropbox")
        assert scope["name"] == f"dropbox-upload:{approval_id}"
        assert auth_db.resolve_scoped_service_token(_hash(first["token"]), method="GET",
                                                    path="/api/dropbox") is None
        assert auth_db.resolve_token(_hash(first["token"])) is None

        second = client.get(f"/api/dropbox/enrollments/{secret}").json()
    assert second["token"] != first["token"]
    assert _live(approval_id) == [_hash(second["token"])]
    operator = env.desk.operator_result(env.approvals.status(approval_id))
    assert operator["state"] == ext.DELIVERED
    for token in (first["token"], second["token"]):
        assert token not in _central_text(env, approval_id)


def test_concurrent_polls_leave_exactly_one_live_bearer(env):
    with TestClient(_app()) as client:
        _secret, approval_id = _enroll(client)
    _decide(env, approval_id)
    results, barrier = [], threading.Barrier(8)

    def poll():
        barrier.wait()
        results.append(env.desk.collect(approval_id))

    threads = [threading.Thread(target=poll) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r["status"] == "approved" for r in results)
    live = _live(approval_id)
    assert len(live) == 1 and live[0] in {_hash(r["token"]) for r in results}
    assert _named(approval_id) == 8


def test_a_later_poll_rotates_within_the_granted_lifetime(env):
    with TestClient(_app()) as client:
        secret, approval_id = _enroll(client)
        _decide(env, approval_id)
        client.get(f"/api/dropbox/enrollments/{secret}")
        env.clock.t += 3600
        later = client.get(f"/api/dropbox/enrollments/{secret}").json()
    assert later["status"] == "approved"
    assert _live(approval_id) == [_hash(later["token"])]

def test_a_late_grant_is_collected_at_the_next_poll(env):
    """A Grant long after the device's first polls mints nothing until the
    device polls again; then it is collected."""
    with TestClient(_app()) as client:
        secret, approval_id = _enroll(client)
        for _ in range(3):
            assert client.get(f"/api/dropbox/enrollments/{secret}").json()["status"] == "pending"
        _decide(env, approval_id)
        env.clock.t += 20 * 3600     # inside the one-day lifetime granted
        assert _named(approval_id) == 0
        assert env.desk.operator_result(env.approvals.status(approval_id))["state"] == ext.AWAITING
        assert client.get(f"/api/dropbox/enrollments/{secret}").json()["status"] == "approved"
    assert env.desk.operator_result(env.approvals.status(approval_id))["state"] == ext.DELIVERED

def test_a_short_lifetime_ends_collection(env):
    with TestClient(_app()) as client:
        secret, approval_id = _enroll(client)
        _decide(env, approval_id, decision={"ttl_seconds": 600})
        env.clock.t += 600
        assert client.get(f"/api/dropbox/enrollments/{secret}").json()["status"] == "expired"
    assert _named(approval_id) == 0


def test_a_decline_mints_nothing(env):
    with TestClient(_app()) as client:
        secret, approval_id = _enroll(client)
        _decide(env, approval_id, outcome="declined")
        assert client.get(f"/api/dropbox/enrollments/{secret}").json()["status"] == "declined"
    assert _named(approval_id) == 0

def test_only_an_enrollment_nothing_can_be_collected_from_is_pruned(env):
    with TestClient(_app()) as client:
        declined_secret, declined = _enroll(client)
        _decide(env, declined, outcome="declined")
        short_secret, short = _enroll(client)
        _decide(env, short, decision={"ttl_seconds": 600})
        _live_secret, live = _enroll(client)
        _decide(env, live)
        env.clock.t += 601
        env.desk.prune()
        assert auth_db.service_enrollment_approvals() == [live]
        assert client.get(f"/api/dropbox/enrollments/{declined_secret}").status_code == 404
        assert client.get(f"/api/dropbox/enrollments/{short_secret}").status_code == 404


def test_a_grant_on_another_machine_mints_nothing_here(env):
    with TestClient(_app()) as client:
        secret, approval_id = _enroll(client)
        _decide(env, approval_id)
        env.machine.destination = ELSEWHERE
        assert client.get(f"/api/dropbox/enrollments/{secret}").json()["status"] == "pending"
    assert env.desk.operator_result(env.approvals.status(approval_id))["state"] == ext.ELSEWHERE
    assert _named(approval_id) == 0


def test_pending_enrollments_are_capped(env):
    with TestClient(_app()) as client:
        for _ in range(dropbox_routes.MAX_PENDING_ENROLLMENTS):
            _enroll(client)
        dropbox_routes._enrollment_attempts.clear()
        refused = client.post("/api/dropbox/enrollments", json={"label": "one more"})
    assert refused.status_code == 429


# ── logs ─────────────────────────────────────────────────────────────


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_server(app, port):
    # uvicorn's default logging config, with its access log ON: the case a
    # launcher without --no-access-log would run.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="info", access_log=True))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append((record.name, record.getMessage()))


def test_no_log_line_carries_the_poll_secret(env, caplog):
    from tools.dashboard import server

    app = Starlette(routes=dropbox_routes.ROUTES,
                    middleware=[Middleware(server._RequestDurationMiddleware)])
    capture = _Capture()
    watched = [logging.getLogger("uvicorn.access"), server._http_logger,
               logging.getLogger("tools.dashboard")]
    caplog.set_level(logging.DEBUG)
    port = _free_port()
    with _live_server(app, port) as base:
        for logger in watched:
            logger.addHandler(capture)
        try:
            request = urllib.request.Request(
                base + "/api/dropbox/enrollments", method="POST",
                data=json.dumps({"label": "iPhone"}).encode(),
                headers={"Content-Type": "application/json"})
            created = json.loads(urllib.request.urlopen(request, timeout=10).read())
            secret, approval_id = created["id"], created["sourceApprovalId"]
            _decide(env, approval_id)
            with urllib.request.urlopen(f"{base}/api/dropbox/enrollments/{secret}?wait=1",
                                        timeout=10) as response:
                token = json.loads(response.read())["token"]
        finally:
            for logger in watched:
                logger.removeHandler(capture)
    text = "\n".join(line for _name, line in capture.lines) + "\n" + caplog.text
    for name in ("uvicorn.access", server._http_logger.name):
        assert any(logger == name and "/api/dropbox/enrollments/[redacted]" in line
                   for logger, line in capture.lines), (name, text)
    assert secret not in text and token not in text and _hash(secret) not in text
