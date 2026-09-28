"""mcp_crosstalk as a Central approval (auto-fkhq0.14, part 1).

Against the real ApprovalService (in-memory store), the real relay routes, a
real mcp_relay_db and auth.db in tmp_path, with the machine identity and the
clock stubbed:

- only the relay (after its service secret) opens this kind; the chat's raw
  openai/session never reaches Central;
- the operator reviews the whole message; one that cannot be reviewed whole
  is refused at creation with its reason (the serialized review is the gate);
- what is delivered is exactly what was reviewed; a second send while one is
  open changes nothing;
- a Grant applies once (grant live + one delivery) even under replay and
  concurrency, only on the accepting machine, only within 30 minutes; a
  decline denies; an interrupted or failed delivery ends delivery_failed.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import api_auth
from tools.dashboard import mcp_crosstalk_central as central
from tools.dashboard import mcp_relay_routes as routes
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    HumanApprovalActor,
    InMemoryApprovalStore,
)
from tools.dashboard.dao import auth_db
from tools.dashboard.dao import mcp_relay_db as db
from tools.graph.schemas.central_attention import ApprovalRequestV1
from tools.network.idkit.keys import KeyPair

ROOT = KeyPair.generate()
TOKEN = "test-service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HERE = "h" * 43
ELSEWHERE = "e" * 43
CHAT = "v1/chat-raw-openai-session-value"


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
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mcp_relay.db")
    auth_db.init_db(tmp_path / "auth.db")
    monkeypatch.setenv(routes.SERVICE_TOKEN_ENV, TOKEN)
    clock, machine = Clock(), Machine()
    registry = build_production_registry(runtimes={central.KIND: central.build_approval_runtime(
        destination_resolver=machine, machine_label=lambda: "Home",
        target_label=lambda target: "GIS pass" if target == "auto-x" else "")})
    approvals = ApprovalService(
        registry=registry, store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: ROOT.public_hex, clock=clock,
        registered_service_label_resolver=central.service_label,
    )
    desk = central.CrosstalkDesk(approvals=approvals, destination_resolver=machine, clock=clock)
    monkeypatch.setattr(routes, "_crosstalk_desk", lambda: desk)
    client = TestClient(Starlette(routes=routes.ROUTES))
    db.upsert_pending_session(CHAT)
    db.approve_session(CHAT, autonomy_org="autonomy", level="read", expires_at=None)
    try:
        yield type("Env", (), dict(approvals=approvals, desk=desk, clock=clock, machine=machine,
                                   client=client, handle=db.get_session(CHAT)["handle"]))
    finally:
        if auth_db._conn is not None and auth_db._conn is not saved:
            auth_db._conn.close()
        auth_db._conn = saved


def _relay(env, message="the actual payload", target="auto-x", **extra):
    return env.client.post("/api/mcp/crosstalk/relay", headers=AUTH, json={
        "openai_session": CHAT, "target_session": target, "message": message,
        "intent": "coordinate the GIS pass", "target_org": "personal", **extra})


def _status(env, target="auto-x"):
    return env.client.post("/api/mcp/crosstalk/status", headers=AUTH, json={
        "openai_session": CHAT, "target_session": target}).json()["status"]


def _decide(env, approval_id, outcome="granted", decision=None):
    if decision is None:
        decision = {"ttl_seconds": 3600} if outcome == "granted" else {}
    return env.approvals.decide(approval_id, HumanApprovalActor._verified(ROOT.public_hex),
                                outcome=outcome, decision=decision)


def _delivered(target="auto-x"):
    return [m["message"] for m in auth_db.get_messages(session=target)]


def _open_count(env):
    return len(env.approvals.store._requests)


# ── creation ─────────────────────────────────────────────────────────


def test_the_relay_opens_one_approval_carrying_the_whole_message_and_no_raw_session(env):
    body = _relay(env, message="line one\n\tindented\r\nline three").json()
    assert body["status"] == "pending" and body["from"] == env.handle
    payload = env.approvals.status(body["approval_id"]).request.payload
    ApprovalRequestV1.validate(payload)
    review = payload["safe_review"]
    assert review["message_lines"] == ["line one", "    indented", "line three"]
    assert review["handle"] == env.handle and review["target_label"] == "GIS pass"
    assert review["target_org"] == "personal" and review["requester_label"] == "ChatGPT relay"
    assert payload["requester_ref"]["kind"] == "registered_service"
    assert CHAT not in json.dumps(payload)


def test_only_the_relay_can_open_this_kind(env):
    session = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject="auto-x")
    with pytest.raises(ApprovalServiceError):
        env.approvals.create_from_principal(central.KIND, session, {
            "handle": "h", "target_session": "t", "target_org": "", "intent": "",
            "message": "m"})
    wrong = env.client.post("/api/mcp/crosstalk/relay", json={
        "openai_session": CHAT, "target_session": "auto-x", "message": "hi"})
    assert wrong.status_code == 401 and _open_count(env) == 0


@pytest.mark.parametrize("message,reason", [
    ("\n".join(['"' * 118] * 50), "too long to review whole"),   # < 6000 bytes, escapes past 8 KB
    ("x" * 6001, "over 6000 bytes"),
    ("\n".join(["x"] * 121), "over 120 lines"),
    ("bell \x07 here", "control characters"),
    ("   ", "empty"),
])
def test_a_message_the_operator_could_not_review_whole_is_refused_at_creation(
        env, message, reason):
    refused = _relay(env, message=message)
    assert refused.status_code == 400
    if reason != "empty":  # an empty message is refused by the route before review
        assert refused.json()["status"] == "refused" and reason in refused.json()["error"]
    assert _open_count(env) == 0


# ── delivery: exactly the reviewed text, once ─────────────────────────


def test_a_grant_delivers_exactly_the_reviewed_message_once(env):
    rid = _relay(env, message="A: the reviewed text").json()["approval_id"]
    # a second, different send while the first is open changes nothing
    again = _relay(env, message="B: a replacement").json()
    assert again["approval_id"] == rid and _open_count(env) == 1
    assert env.desk.operator_result(env.approvals.status(rid))["state"] == central.PENDING
    _decide(env, rid)
    assert env.desk.operator_result(env.approvals.status(rid))["state"] == central.AWAITING
    assert _status(env) == "approved"
    assert _delivered() == ["A: the reviewed text"]
    for _ in range(3):
        env.desk.apply(rid)
        _status(env)
    assert _delivered() == ["A: the reviewed text"]
    grant = db.get_crosstalk_grant(CHAT, "auto-x")
    assert grant["status"] == db.APPROVED and grant["expires_at"] == pytest.approx(env.clock.t + 3600)
    assert env.desk.operator_result(env.approvals.status(rid))["state"] == central.DELIVERED
    stored = auth_db.get_messages(session="auto-x")
    assert stored[0]["sender_session"] == env.handle


def test_concurrent_applications_deliver_once(env):
    rid = _relay(env).json()["approval_id"]
    _decide(env, rid)
    barrier = threading.Barrier(6)

    def apply():
        barrier.wait()
        env.desk.apply(rid)

    threads = [threading.Thread(target=apply) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _delivered() == ["the actual payload"]


def test_a_decline_denies_and_delivers_nothing(env):
    rid = _relay(env).json()["approval_id"]
    _decide(env, rid, outcome="declined")
    assert _status(env) == db.DENIED
    assert _delivered() == []
    assert env.desk.operator_result(env.approvals.status(rid))["state"] == "declined"
    assert _relay(env).json()["status"] == db.DENIED


def test_a_grant_applied_more_than_30_minutes_late_delivers_nothing(env):
    rid = _relay(env).json()["approval_id"]
    _decide(env, rid)
    env.clock.t += central.APPLY_WINDOW_SECONDS
    assert _status(env) != "approved"
    assert _delivered() == []
    assert env.desk.operator_result(env.approvals.status(rid))["state"] == central.EXPIRED
    # the chat may ask again: a fresh approval, not the stale one
    fresh = _relay(env).json()
    assert fresh["status"] == "pending" and fresh["approval_id"] != rid


def test_another_machine_applies_nothing(env):
    rid = _relay(env).json()["approval_id"]
    _decide(env, rid)
    env.machine.destination = ELSEWHERE
    assert _status(env) == "pending"
    assert _delivered() == []
    assert env.desk.operator_result(env.approvals.status(rid))["state"] == central.ELSEWHERE


def test_a_failed_delivery_ends_failed_and_is_not_retried(env, monkeypatch):
    rid = _relay(env).json()["approval_id"]
    _decide(env, rid)
    calls = []

    async def broken(handle, target, message):
        calls.append(message)
        raise OSError("tmux gone")

    monkeypatch.setattr(env.desk, "_deliver", broken)
    env.desk.apply(rid)
    env.desk.apply(rid)
    result = env.desk.operator_result(env.approvals.status(rid))
    assert result == {"state": central.FAILED, "machine_label": "Home",
                      "reason": "the target could not be reached"}
    assert len(calls) == 1


def test_an_interrupted_delivery_ends_failed_and_is_never_redelivered(env, monkeypatch):
    rid = _relay(env).json()["approval_id"]
    _decide(env, rid)
    # The process settled the grant and died before delivering.
    db.settle_crosstalk(rid, status=db.APPROVED, outcome=central.DELIVERING,
                        owner_pid=999_999, owner_start="gone")
    monkeypatch.setattr(env.desk, "_owner_alive", lambda row: False)
    env.desk.recover_interrupted()
    env.desk.apply(rid)
    assert _delivered() == []
    assert env.desk.operator_result(env.approvals.status(rid)) == {
        "state": central.FAILED, "machine_label": "Home", "reason": "interrupted"}


def test_a_live_delivery_is_not_marked_interrupted(env, monkeypatch):
    rid = _relay(env).json()["approval_id"]
    _decide(env, rid)
    db.settle_crosstalk(rid, status=db.APPROVED, outcome=central.DELIVERING,
                        owner_pid=1, owner_start="alive")
    monkeypatch.setattr(env.desk, "_owner_alive", lambda row: True)
    env.desk.recover_interrupted()
    assert db.get_outcome(rid)["state"] == central.DELIVERING


def test_concurrent_sends_open_one_approval(env):
    barrier = threading.Barrier(6)
    ids = []

    def send(n):
        barrier.wait()
        ids.append(_relay(env, message=f"send {n}").json()["approval_id"])

    threads = [threading.Thread(target=send, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 1 and _open_count(env) == 1


def test_a_grant_whose_grant_row_moved_on_reads_superseded(env):
    old = _relay(env).json()["approval_id"]
    newer = env.desk.open({"handle": env.handle, "target_session": "auto-x",
                           "target_org": "", "intent": "", "message": "newer"})
    db.upsert_pending_crosstalk(CHAT, "auto-x", approval_id=newer)
    _decide(env, old)
    env.desk.apply(old)
    assert _delivered() == []
    assert env.desk.operator_result(env.approvals.status(old))["state"] == central.SUPERSEDED


def test_delivery_goes_only_to_the_reviewed_target(env):
    rid = _relay(env).json()["approval_id"]
    conn = db._get_conn()
    conn.execute("UPDATE mcp_crosstalk_grants SET target_session='auto-other'"
                 " WHERE approval_id=?", (rid,))
    conn.commit()
    conn.close()
    _decide(env, rid)
    env.desk.apply(rid)
    assert _delivered("auto-other") == [] and _delivered() == []
    assert env.desk.operator_result(env.approvals.status(rid))["reason"] == "target changed"


# ── composition ──────────────────────────────────────────────────────


def test_production_composition_applies_crosstalk_and_labels_the_relay():
    from tools.dashboard import attention_routes, approvals_routes

    runtime = attention_routes.build_production_runtime()
    assert runtime.approval_http.claims_kind(central.KIND)
    assert isinstance(runtime.crosstalk_desk, central.CrosstalkDesk)
    assert central.KIND in runtime.operator_result_projectors
    for table in ("PREPARE_CREATE", "ENRICH", "EXECUTORS", "AUTHORIZE_DECISION"):
        assert central.KIND not in getattr(approvals_routes, table), table
