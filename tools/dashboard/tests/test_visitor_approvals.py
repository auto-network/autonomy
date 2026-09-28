"""visitor_token as a Central approval (auto-fkhq0.12).

Against the real ApprovalService (in-memory store) and ApprovalHttpBridge,
with a real mission_control_db in tmp_path and the avatar store and machine
identity stubbed at their seams:

- the operator is shown who is being let in (face, name, why) and a request
  that could never be minted is refused at creation; the decision is ``{}``;
- the token is minted at the authenticated requester's collect on the
  accepting machine, revealed on that read only, and never written to any
  Central row, operator result or log line; a wrong requester reads nothing;
- once per approval, even under concurrent reads, after a crash between the
  mint and the response, and after the guest is removed;
- decline, cancel and a Grant replicated to another machine mint nothing;
- the production composition claims the kind and the legacy hooks are gone.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest

from tools.dashboard import api_auth
from tools.dashboard import visitor_approvals as va
from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridge,
    ApprovalHttpBridgeError,
    ApprovalHttpRegistry,
    ApprovalWaitHub,
)
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    HumanApprovalActor,
    InMemoryApprovalStore,
)
from tools.dashboard.attention_registry import build_production_attention_registry
from tools.dashboard.dao import mission_control_db as db
from tools.graph.schemas.central_attention import ApprovalRequestV1
from tools.network.idkit.keys import KeyPair

ROOT = KeyPair.generate()
HERE = "h" * 43
ELSEWHERE = "e" * 43
PHOTO = "data:image/png;base64,iVBORw0KGgo="


class Machine:
    """Which Dashboard this test process is: the accepting one, or another."""

    def __init__(self):
        self.destination = HERE

    def __call__(self):
        return self.destination


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "mc.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    stored = []

    def store_avatar(avatar, name):
        if avatar.endswith("broken"):
            raise ValueError("that photo could not be stored")
        stored.append((avatar, name))
        return "att-" + str(len(stored))

    machine = Machine()
    registry = build_production_registry(runtimes={va.KIND: va.build_approval_runtime(
        destination_resolver=machine, machine_label=lambda: "Home", store_avatar=store_avatar)})
    hub = ApprovalWaitHub()
    approvals = ApprovalService(
        registry=registry,
        store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: ROOT.public_hex,
        session_label_resolver=lambda subject: f"{subject} · Guests",
        clock=lambda: 1000.0,
        after_commit=hub.notify,
    )
    desk = va.VisitorDesk(approvals=approvals, destination_resolver=machine)
    attention = build_production_attention_registry(
        approval_registry=registry,
        runtimes={(va.KIND, va.APPLICATION_SCOPE): va.build_attention_runtime(approvals)})
    bridge = ApprovalHttpBridge(
        approvals=approvals,
        registry=ApprovalHttpRegistry(approvals=registry, attention=attention,
                                      adapters={va.KIND: va.build_http_adapter(desk)}),
        wait_hub=hub,
    )
    yield type("Env", (), dict(approvals=approvals, desk=desk, bridge=bridge, machine=machine,
                               stored=stored, db_path=db_path))
    bridge.close()


def _agent(subject="auto-asker"):
    return api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject=subject)


def _create(env, request=None):
    return env.bridge.create(va.KIND, _agent(), request or {
        "display_name": "Shari Vietry", "avatar": PHOTO, "reason": "reviewing the deck"})


def _decide(env, approval_id, outcome="granted", decision=None):
    return env.approvals.decide(approval_id, HumanApprovalActor._verified(ROOT.public_hex),
                                outcome=outcome, decision=decision or {})


def _read(env, approval_id, principal=None):
    return env.bridge.envelope(approval_id, principal or _agent())


def _rows(env, table):
    conn = db._get_conn(env.db_path)
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    finally:
        conn.close()


def _central_text(env, approval_id):
    status = env.approvals.status(approval_id)
    return json.dumps([status.request.payload,
                       status.resolution.payload if status.resolution else None,
                       env.desk.operator_result(status)])


# ── what the operator is asked ──────────────────────────────────────


def test_the_operator_is_shown_the_face_the_name_and_why(env):
    approval_id = _create(env)
    payload = env.approvals.status(approval_id).request.payload
    ApprovalRequestV1.validate(payload)
    review = payload["safe_review"]
    assert review["display_name"] == "Shari Vietry"
    assert review["reason"] == "reviewing the deck"
    assert review["avatar_url"] == "/api/attachment/att-1"
    assert review["machine_label"] == "Home"
    assert review["requester_label"] == "auto-asker · Guests"
    assert env.stored == [(PHOTO, "Shari Vietry")]
    # The photo's bytes live in the attachment store, never in the approval.
    assert "base64" not in json.dumps(payload)
    assert payload["staged"]["result_destination_id"] == HERE


def test_a_person_with_no_photo_is_still_decidable(env):
    approval_id = _create(env, {"display_name": "Leon"})
    review = env.approvals.status(approval_id).request.payload["safe_review"]
    assert review["avatar_url"] is None and review["reason"] == ""
    assert env.stored == []


@pytest.mark.parametrize("request_body", [
    {"display_name": "  "},
    {"display_name": "x" * 121},
    {"display_name": "Leon", "avatar": "https://example.test/face.png"},
    {"display_name": "Leon", "avatar": "data:image/png;base64,broken"},
    {"display_name": "Leon", "admin": True},
    {"display_name": "Leon", "reason": 7},
])
def test_a_request_that_could_never_be_minted_never_reaches_the_operator(env, request_body):
    with pytest.raises(ApprovalHttpBridgeError):
        _create(env, request_body)
    assert env.approvals.store._requests == {}


def test_the_decision_carries_nothing(env):
    approval_id = _create(env)
    with pytest.raises(ApprovalServiceError) as exc:
        _decide(env, approval_id, decision={"token": "chosen-by-the-browser"})
    assert exc.value.code == "invalid_decision"
    assert _decide(env, approval_id).payload["decision"] == {}


# ── the token: minted at the requester's read, revealed once ────────


def test_the_requester_collects_the_token_once_and_central_never_holds_it(env, caplog):
    caplog.set_level(logging.DEBUG)
    approval_id = _create(env)
    assert _read(env, approval_id)["result"] is None
    assert env.desk.operator_result(env.approvals.status(approval_id))["state"] == va.PENDING
    _decide(env, approval_id)
    assert env.desk.operator_result(env.approvals.status(approval_id))["state"] == va.AWAITING
    assert _rows(env, "visitor_tokens") == []  # the Grant alone mints nothing

    first = _read(env, approval_id)["result"]
    execution = first["execution"]
    token = execution["token"]
    assert first["approved"] is True and execution["ok"] is True and len(token) == 64
    assert execution["display_name"] == "Shari Vietry"
    assert execution["avatar_attachment_id"] == "att-1"
    assert db.resolve_visitor(token)["participant_id"] == execution["participant_id"]

    second = _read(env, approval_id)["result"]["execution"]
    assert second == {"ok": True, "delivered": True,
                      "participant_id": execution["participant_id"],
                      "display_name": "Shari Vietry"}
    operator = env.desk.operator_result(env.approvals.status(approval_id))
    assert operator["state"] == va.MINTED
    assert operator["participant_id"] == execution["participant_id"]
    # Secret at rest: no Central row, operator result, mint journal or log line.
    assert token not in _central_text(env, approval_id)
    assert token not in json.dumps(_rows(env, "visitor_token_mints"))
    assert token not in caplog.text
    assert len(_rows(env, "visitor_tokens")) == 1


def test_a_wrong_requester_reads_nothing_and_mints_nothing(env):
    approval_id = _create(env)
    _decide(env, approval_id)
    with pytest.raises(ApprovalHttpBridgeError) as exc:
        _read(env, approval_id, _agent("auto-someone-else"))
    assert exc.value.code == "not_found"
    assert _rows(env, "visitor_tokens") == []
    assert "token" in _read(env, approval_id)["result"]["execution"]


def test_concurrent_first_reads_reveal_one_token_and_mint_one_guest(env):
    approval_id = _create(env)
    _decide(env, approval_id)
    results, barrier = [], threading.Barrier(8)

    def read():
        barrier.wait()
        results.append(_read(env, approval_id)["result"]["execution"])

    threads = [threading.Thread(target=read) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum("token" in r for r in results) == 1
    assert sum(r.get("delivered") is True for r in results) == 7
    assert len(_rows(env, "visitor_tokens")) == 1
    assert len({r["participant_id"] for r in results}) == 1


def test_a_crash_after_the_mint_never_reveals_and_the_orphan_can_be_removed(env):
    approval_id = _create(env)
    _decide(env, approval_id)
    # The process mints and dies before the response leaves: the token exists
    # only in the credential store, held by nobody.
    orphan = db.create_visitor_token("Shari Vietry", approval_id=approval_id)
    execution = _read(env, approval_id)["result"]["execution"]
    assert "token" not in execution and execution["delivered"] is True
    assert execution["participant_id"] == orphan["participant_id"]
    operator = env.desk.operator_result(env.approvals.status(approval_id))
    assert operator["state"] == va.MINTED
    assert operator["participant_id"] == orphan["participant_id"]

    # The operator removes it by its display-safe id; the bearer stops working
    # and no later read mints a replacement.
    assert db.delete_visitor(orphan["participant_id"])
    assert db.resolve_visitor(orphan["token"]) is None
    again = _read(env, approval_id)["result"]["execution"]
    assert again["removed"] is True and "token" not in again
    assert _rows(env, "visitor_tokens") == []
    assert env.desk.operator_result(env.approvals.status(approval_id))["removed"] is True


# ── nothing is minted without a Grant, here ─────────────────────────


def test_a_declined_request_mints_nothing(env):
    approval_id = _create(env)
    _decide(env, approval_id, outcome="declined")
    assert _read(env, approval_id)["result"] == {"approved": False, "outcome": "declined"}
    assert env.desk.collect(approval_id) is None
    assert _rows(env, "visitor_tokens") == [] and _rows(env, "visitor_token_mints") == []


def test_a_cancel_before_the_grant_wins_and_mints_nothing(env):
    approval_id = _create(env)
    env.approvals.cancel_from_principal(approval_id, _agent())
    assert _decide(env, approval_id).payload["outcome"] == "canceled"
    assert env.approvals.status(approval_id).resolution.payload["outcome"] == "canceled"
    assert _read(env, approval_id)["result"] == {"approved": False, "outcome": "canceled"}
    assert env.desk.collect(approval_id) is None
    assert _rows(env, "visitor_tokens") == []


def test_a_cancel_after_the_grant_is_answered_with_the_grant(env):
    """The first resolution is final: a cancel racing a Grant it lost returns
    the Grant, so the requester is told the guest exists and mints it once."""
    approval_id = _create(env)
    _decide(env, approval_id)
    resolution = env.approvals.cancel_from_principal(approval_id, _agent())
    assert resolution.payload["outcome"] == "granted"
    assert "token" in _read(env, approval_id)["result"]["execution"]
    assert len(_rows(env, "visitor_tokens")) == 1


def test_a_grant_replicated_to_another_machine_mints_nothing_there(env):
    approval_id = _create(env)
    _decide(env, approval_id)
    env.machine.destination = ELSEWHERE
    assert _read(env, approval_id)["result"] is None
    assert env.desk.operator_result(env.approvals.status(approval_id))["state"] == va.ELSEWHERE
    assert _rows(env, "visitor_tokens") == []


# ── composition ──────────────────────────────────────────────────────


def test_production_composition_claims_the_kind():
    from tools.dashboard import attention_routes

    runtime = attention_routes.build_production_runtime()
    assert runtime.approval_http.claims_kind(va.KIND)
    assert runtime.approval_http.migrated_kind(va.KIND)
    assert va.KIND in runtime.operator_result_projectors


def test_the_legacy_visitor_hooks_are_gone():
    from tools.dashboard import approvals_routes

    for table in ("PREPARE_CREATE", "ENRICH", "EXECUTORS", "AUTHORIZE_DECISION"):
        assert va.KIND not in getattr(approvals_routes, table), table
    for name in ("prepare_create", "enrich", "execute", "authorize_decision"):
        assert not hasattr(va, name), name
