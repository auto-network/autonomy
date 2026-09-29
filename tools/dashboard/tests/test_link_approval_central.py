"""link_publish / link_revoke as Central approvals (auto-fkhq0.10a).

Against the real ApprovalService (in-memory store), with the planner,
verifier and executor stubbed at their seams (their own behaviour is covered
by test_link_publish_tunnel.py / test_link_approvals.py):

- a request is validated and frozen at creation; a refused plan raises no
  approval; the decision carries nothing;
- a Grant is operable only on the accepting machine, within 30 minutes, with
  an envelope signed no earlier than the Grant; it runs once, and a replay
  returns the recorded execution;
- the requester's result is null until the operation is recorded;
- a claim left by a stopped process resolves from the grant row it names,
  without resending (link_operations.read);
- the production composition claims both kinds and mounts the routes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tools.dashboard import api_auth
from tools.dashboard import link_approval_central as central
from tools.dashboard import link_operations as ops
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    HumanApprovalActor,
    InMemoryApprovalStore,
)
from tools.graph.schemas.central_attention import ApprovalRequestV1
from tools.network.idkit.keys import KeyPair

ROOT = KeyPair.generate()
HERE = "h" * 43
ELSEWHERE = "e" * 43
NOW = 50_000.0
TOKEN = "c0ffee00" * 4
REQUEST = {"org": "acme", "target_uuid": "11111111-1111-4111-8111-111111111111",
           "target_type": "present", "meta": {"label": "binder"}}
STAGED = {"method": "POST", "path": "/v1/links", "registry_url": "https://registry.test",
          "payload": {"org": "22222222-2222-4222-8222-222222222222",
                      "target_uuid": REQUEST["target_uuid"], "target_type": "present",
                      "meta": {"label": "binder"}},
          "binding": {"org_uuid": "22222222-2222-4222-8222-222222222222",
                      "root_pub": "ab" * 32, "registry_url": "https://registry.test"}}


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


class MemoryJournal:
    rows: dict = {}

    @classmethod
    def get(cls, key):
        row = cls.rows.get(key)
        return dict(row) if row is not None else None

    @classmethod
    def put(cls, key, payload):
        cls.rows[key] = dict(payload)


def _planner(op, request):
    if request.get("target_type") == "refuse":
        raise ops.LinkOperationError("invalid_request", "that target does not exist")
    review = {"op": op, "org": request.get("org"), "target_type": request.get("target_type"),
              "type_label": "Present deck", "target_title": "Deck", "label": "binder",
              "ttl": None, "fixed_expiry": False, "recipient": None,
              "acting_identity": {"slug": "acme"}, "actor_display_name": "Op"}
    return {"request": dict(request), "staged": dict(STAGED), "review": review}


@pytest.fixture
def env():
    MemoryJournal.rows = {}
    clock = Clock()
    ids = iter(f"central-link-{n:04d}" for n in range(1, 99))
    runtimes = {kind: central.build_approval_runtime(
        kind, destination_resolver=lambda: HERE, machine_label=lambda: "Home", planner=_planner)
        for kind in central.KINDS}
    approvals = ApprovalService(
        registry=build_production_registry(runtimes=runtimes),
        store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: ROOT.public_hex,
        session_label_resolver=lambda subject: f"{subject} · Publishing",
        clock=clock, id_factory=lambda: next(ids),
    )
    calls = []

    def verify(op, request, staged, body, *, not_before):
        envelope = body.get("envelope") if isinstance(body, dict) else None
        if not isinstance(envelope, dict):
            raise ops.LinkOperationError("invalid_request")
        if envelope.get("ts", 0) < int(not_before):
            raise ops.LinkOperationError("stale_envelope")
        if envelope.get("bad"):
            raise ops.LinkOperationError("authority_refused", "the persona holds no link:publish")
        return {"approved": True, **body}, "persona-pub"

    async def execute(key, entry, decision, persona, journal=MemoryJournal):
        state = journal.get(key)
        if state and state.get("state") in ("done", "failed"):
            return state["execution"]
        calls.append((key, entry["op"], decision.get("ttl")))
        execution = {"ok": True, "url": "https://relay.test/l/" + TOKEN + "#k", "token": TOKEN}
        journal.put(key, {**entry, "state": "done", "execution": execution,
                          "persona_pub": persona})
        return execution

    desk = central.LinkApprovalDesk(approvals=approvals,
                                    destination_resolver=lambda: HERE, journal=MemoryJournal,
                                    verify=verify, execute=execute,
                                    signing_view=lambda request, staged: {"registry_request": staged},
                                    clock=clock)
    return SimpleNamespace(approvals=approvals, desk=desk, clock=clock, calls=calls)


def _agent(org="acme"):
    return api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.ORG_SESSION, subject="auto-x", org=org)


def _host_terminal():
    return api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject="host-x")


def _central_body(request):
    return {("org_slug" if k == "org" else k): v for k, v in request.items()}


def _create(env, kind="link_publish", request=REQUEST):
    record = env.approvals.create_from_principal(kind, _agent(), _central_body(request))
    return record.approval_id


def _grant(env, approval_id):
    env.approvals.decide(approval_id, HumanApprovalActor._verified(ROOT.public_hex),
                         outcome="granted", decision={})


def _operate(env, approval_id, body):
    return asyncio.run(env.desk.operate(approval_id, body))


def _code(fn, *args):
    with pytest.raises(ops.LinkOperationError) as exc:
        fn(*args)
    return exc.value.code


def test_the_request_is_frozen_at_creation_and_carries_no_token_key(env):
    approval_id = _create(env)
    payload = env.approvals.status(approval_id).request.payload
    ApprovalRequestV1.validate(payload)
    assert payload["staged"]["payload"] == STAGED["payload"]
    assert payload["staged"]["result_destination_id"] == HERE
    assert payload["safe_review"]["machine_label"] == "Home"
    assert payload["safe_review"]["title"] == "Publish a share link"


def test_a_refused_plan_raises_no_approval(env):
    with pytest.raises(ApprovalServiceError):
        env.approvals.create_from_principal("link_publish", _agent(),
                                            _central_body({**REQUEST, "target_type": "refuse"}))


def test_org_is_named_as_org_slug_because_central_reserves_org(env):
    with pytest.raises(ApprovalServiceError):
        env.approvals.create_from_principal("link_publish", _agent(), dict(REQUEST))
    approval_id = _create(env)
    assert env.approvals.status(approval_id).request.payload["request"]["org"] == "acme"


def test_an_org_session_names_only_its_own_org(env):
    with pytest.raises(ApprovalServiceError):
        env.approvals.create_from_principal("link_publish", _agent("acme"),
                                            _central_body({**REQUEST, "org": "other"}))
    omitted = {k: v for k, v in REQUEST.items() if k != "org"}
    record = env.approvals.create_from_principal("link_publish", _agent("acme"), omitted)
    assert record.payload["request"]["org"] == "acme"


def test_a_host_terminal_names_any_org(env):
    record = env.approvals.create_from_principal(
        "link_publish", _host_terminal(), _central_body({**REQUEST, "org": "other"}))
    assert record.payload["request"]["org"] == "other"
    with pytest.raises(ApprovalServiceError):
        env.approvals.create_from_principal(
            "link_publish", _host_terminal(), {k: v for k, v in REQUEST.items() if k != "org"})


def test_a_decision_carrying_the_envelope_is_refused(env):
    approval_id = _create(env)
    with pytest.raises(ApprovalServiceError):
        env.approvals.decide(approval_id, HumanApprovalActor._verified(ROOT.public_hex),
                             outcome="granted", decision={"envelope": {"ts": 1}})


def test_a_granted_publish_runs_once_after_the_grant(env):
    approval_id = _create(env)
    status = env.approvals.status(approval_id)
    assert env.desk.state(status) == central.PENDING
    assert env.desk.bootstrap(approval_id) == {"registry_request": status.request.payload["staged"]}
    assert _code(_operate, env, approval_id, {"envelope": {"ts": int(NOW)}}) == "not_actionable"
    _grant(env, approval_id)
    status = env.approvals.status(approval_id)
    assert status.resolution.payload["decision"] == {}
    assert env.desk.state(status) == central.AWAITING
    assert env.desk.requester_result(status) is None

    # Signed before the Grant: refused.
    assert _code(_operate, env, approval_id, {"envelope": {"ts": int(NOW) - 1}}) == "stale_envelope"
    # The verifier's own words for a refused authority, nothing executed.
    with pytest.raises(ops.LinkOperationError) as refused:
        _operate(env, approval_id, {"envelope": {"ts": int(NOW), "bad": True}})
    assert refused.value.code == "authority_refused"
    assert "link:publish" in refused.value.detail
    assert env.calls == []

    done = _operate(env, approval_id, {"envelope": {"ts": int(NOW)}, "ttl": 3600})
    assert done["execution"]["ok"] is True
    assert env.calls == [(approval_id, "publish", 3600)]
    status = env.approvals.status(approval_id)
    assert env.desk.state(status) == central.DONE
    assert env.desk.requester_result(status) == {"approved": True, "execution": done["execution"]}
    assert env.desk.operator_result(status)["execution"] == done["execution"]
    # A replay returns the recorded execution and runs nothing again.
    assert _operate(env, approval_id, {"envelope": {"ts": int(NOW)}}) == done
    assert len(env.calls) == 1
    entry = MemoryJournal.get(approval_id)
    assert entry["initiator"] == f"approval:{approval_id}"
    assert entry["persona_pub"] == "persona-pub"


def test_the_window_closes_thirty_minutes_after_the_grant(env):
    approval_id = _create(env)
    _grant(env, approval_id)
    env.clock.t = NOW + central.OPERATION_WINDOW_SECONDS
    status = env.approvals.status(approval_id)
    assert env.desk.state(status) == central.EXPIRED
    assert env.desk.requester_result(status)["execution"]["ok"] is False
    assert _code(_operate, env, approval_id, {"envelope": {"ts": int(env.clock.t)}}) == "window_closed"
    assert env.calls == []


def test_another_machine_neither_bootstraps_nor_operates(env):
    approval_id = _create(env)
    _grant(env, approval_id)
    other = central.LinkApprovalDesk(approvals=env.approvals,
                                     destination_resolver=lambda: ELSEWHERE,
                                     journal=MemoryJournal, clock=env.clock)
    assert other.state(env.approvals.status(approval_id)) == central.ELSEWHERE
    assert _code(other.bootstrap, approval_id) == "elsewhere"
    assert _code(lambda: asyncio.run(other.operate(approval_id, {"envelope": {"ts": int(NOW)}}))) == "elsewhere"


def test_a_decline_operates_nothing(env):
    approval_id = _create(env)
    env.approvals.decide(approval_id, HumanApprovalActor._verified(ROOT.public_hex),
                         outcome="declined", decision={})
    assert _code(_operate, env, approval_id, {"envelope": {"ts": int(NOW)}}) == "not_actionable"
    assert env.desk.requester_result(env.approvals.status(approval_id)) is None
    assert env.calls == []


def test_a_revoke_is_its_own_kind(env):
    approval_id = _create(env, "link_revoke", {"org": "acme", "token": TOKEN})
    payload = env.approvals.status(approval_id).request.payload
    assert payload["safe_review"]["title"] == "Revoke a share link"
    _grant(env, approval_id)
    _operate(env, approval_id, {"envelope": {"ts": int(NOW)}})
    assert env.calls == [(approval_id, "revoke", None)]


# ── the journal: an interrupted claim resolves from its grant row ──────────


@pytest.mark.parametrize("grant, state", [
    ({"grant_id": "g1", "token": TOKEN, "url": "https://relay.test/l/" + TOKEN,
      "target_type": "present"}, "done"),
    ({"grant_id": "g1", "target_type": "present"}, "failed"),
    (None, "failed"),
])
def test_an_interrupted_publish_resolves_from_its_grant_row(monkeypatch, grant, state):
    MemoryJournal.rows = {"k1": {"state": "claimed", "op": "publish", "initiator": "operator",
                                 "prepared_at": NOW, "request": dict(REQUEST), "staged": dict(STAGED),
                                 "grant_id": "g1", "claimed_at": NOW}}
    dropped = []
    monkeypatch.setattr(ops, "_grant_row", lambda grant_id, org: grant)
    monkeypatch.setattr(ops.links, "_drop_cached_grant", lambda grant_id, org: dropped.append(grant_id))
    entry = ops.read("k1", journal=MemoryJournal)
    assert entry["state"] == state
    if state == "done":
        assert entry["execution"]["token"] == TOKEN
    else:
        assert entry["execution"]["ok"] is False
    assert dropped == (["g1"] if grant is not None and state == "failed" else [])
    assert MemoryJournal.rows["k1"]["state"] == state


def test_an_interrupted_revoke_resolves_from_the_cached_grant(monkeypatch):
    base = {"state": "claimed", "op": "revoke", "initiator": "operator", "prepared_at": NOW,
            "request": {"org": "acme", "token": TOKEN}, "staged": {}, "claimed_at": NOW}
    MemoryJournal.rows = {"k2": dict(base), "k3": dict(base)}
    monkeypatch.setattr(ops.links, "_cached_grant", lambda token, org: None)
    assert ops.read("k2", journal=MemoryJournal)["state"] == "done"
    monkeypatch.setattr(ops.links, "_cached_grant", lambda token, org: {"token": TOKEN})
    assert ops.read("k3", journal=MemoryJournal)["state"] == "failed"


def test_production_composition_claims_both_link_kinds_and_mounts_the_routes():
    from tools.dashboard import attention_routes, link_operation_routes

    runtime = attention_routes.build_production_runtime()
    for kind in central.KINDS:
        assert runtime.approval_http.claims_kind(kind)
        assert runtime.approval_http.migrated_kind(kind)
        assert kind in runtime.operator_result_projectors
    assert isinstance(runtime.link_operation_desk, central.LinkApprovalDesk)
    paths = {route.path for route in attention_routes.routes}
    assert "/api/attention/items/{attention_id:path}/link-operation-bootstrap" in paths
    assert "/api/attention/items/{attention_id:path}/link-operation" in paths
    assert {r.path for r in link_operation_routes.ROUTES} == {
        "/api/links/operations", "/api/links/operations/{operation_id}"}


def test_the_legacy_link_hooks_are_gone():
    from tools.dashboard import approvals_routes, link_approvals

    for table in ("PREPARE_CREATE", "ENRICH", "EXECUTORS"):
        assert not set(getattr(approvals_routes, table)) & set(central.KINDS), table
    for name in ("ENRICH", "EXECUTORS", "_enrich_link_publish", "_enrich_link_revoke",
                 "_staged_registry_request"):
        assert not hasattr(link_approvals, name), name


# ── a claim is settled only when its owner is gone (review of .10a) ────────


def _claimed_publish(**owner):
    return {"state": "claimed", "op": "publish", "initiator": "operator", "prepared_at": NOW,
            "request": dict(REQUEST), "staged": dict(STAGED), "grant_id": "g1",
            "claimed_at": __import__("time").time(), **owner}


def test_a_claim_held_by_a_live_other_worker_is_not_resolved(monkeypatch):
    """Hot-reload overlap: the old worker is mid-publish, its grant row written
    and the frame in flight. The new worker must not drop that row."""
    import os

    from tools.dashboard.connector_key_resolution import process_start

    MemoryJournal.rows = {"live": _claimed_publish(owner_pid=os.getpid(),
                                                   owner_start=process_start(os.getpid()))}
    dropped = []
    monkeypatch.setattr(ops, "_grant_row", lambda grant_id, org: {"grant_id": "g1"})
    monkeypatch.setattr(ops.links, "_drop_cached_grant", lambda g, org: dropped.append(g))
    assert ops.read("live", journal=MemoryJournal)["state"] == "claimed"
    assert dropped == []


def test_a_claim_whose_owner_died_is_resolved(monkeypatch):
    MemoryJournal.rows = {"dead": _claimed_publish(owner_pid=2**22 + 12345, owner_start="gone")}
    monkeypatch.setattr(ops, "_grant_row", lambda grant_id, org: None)
    assert ops.read("dead", journal=MemoryJournal)["state"] == "failed"


def test_a_claim_past_the_hard_bound_is_settled_even_with_a_live_owner(monkeypatch):
    import os
    import time as _time

    from tools.dashboard.connector_key_resolution import process_start

    old = _claimed_publish(owner_pid=os.getpid(), owner_start=process_start(os.getpid()))
    old["claimed_at"] = _time.time() - ops.CLAIM_HARD_BOUND_SECONDS - 1
    MemoryJournal.rows = {"old": old}
    monkeypatch.setattr(ops, "_grant_row", lambda grant_id, org: None)
    assert ops.read("old", journal=MemoryJournal)["state"] == "failed"


def test_prepare_prunes_operator_dialogs_never_signed(monkeypatch):
    from tools.dashboard import link_operation_routes as routes

    deleted = []
    seen = {}
    monkeypatch.setattr(ops, "plan", lambda op, request: {"request": dict(REQUEST),
                                                          "staged": dict(STAGED), "review": {}})
    monkeypatch.setattr(ops, "signing_view", lambda request, staged: {})
    monkeypatch.setattr(ops, "prepare_entry", lambda *a, **k: None)
    monkeypatch.setattr(ops.Journal, "stale_prepared",
                        staticmethod(lambda older_than: seen.setdefault("cut", older_than) and ["op-old"]))
    monkeypatch.setattr(ops.Journal, "delete", staticmethod(deleted.append))
    routes.prepare({"op": "publish", "request": dict(REQUEST)}, now=NOW)
    assert seen["cut"] == NOW - routes.PREPARED_WINDOW_SECONDS
    assert deleted == ["op-old"]
