from __future__ import annotations

import asyncio
import concurrent.futures
import json
from types import SimpleNamespace

import pytest

from tools.dashboard import api_auth, link_central
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalRecord,
    ApprovalStatus,
)
from tools.network.idkit import KeyPair, canonical_json
from tools.network.registry.signing import sign_link_operation_receipt


APPROVAL_ID = "central-link-runtime-0123456789abcdef"
ORG = "example-org"
ORG_UUID = "d55f8f0d-2de8-4fde-8678-68774cd68c19"
ROOT = "11" * 32
GENERATION = "22" * 32
SECRET = b"s" * 32


class MemoryStore:
    def __init__(self):
        self.intents = {}
        self.results = {}

    def get_intent(self, org, approval_id):
        value = self.intents.get((org, approval_id))
        return None if value is None else dict(value)

    def put_intent(self, org, approval_id, payload):
        candidate = dict(payload)
        current = self.intents.get((org, approval_id))
        if current is not None and current != candidate:
            raise link_central.LinkCentralError("request_conflict")
        self.intents[(org, approval_id)] = candidate

    def get_result(self, org, approval_id):
        value = self.results.get((org, approval_id))
        return None if value is None else dict(value)

    def put_result(self, org, approval_id, payload):
        candidate = dict(payload)
        current = self.results.get((org, approval_id))
        if current is not None and current != candidate:
            raise link_central.LinkCentralError("result_conflict")
        self.results[(org, approval_id)] = candidate


def _context(kind=api_auth.ApiPrincipalKind.ORG_SESSION.value, org=ORG):
    return link_central.ApprovalPlanningContext(
        approval_id=APPROVAL_ID,
        planning_time=1000.0,
        requester_ref={"kind": "session", "id": "requester"},
        application_scope="links",
        requester_principal_kind=kind,
        requester_org=org,
    )


def _binding(witness):
    return {
        "key": "registry.example",
        "revision": 2,
        "payload": {
            "org_uuid": ORG_UUID,
            "root_pub": ROOT,
            "binding_generation": GENERATION,
            "registry_url": "https://registry.example",
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2030-01-01T00:00:00Z",
            "last_renewed_at": "2029-12-01T00:00:00Z",
        },
    }


def _planner(monkeypatch, store, witness):
    monkeypatch.setattr(link_central, "_binding_context", lambda org: _binding(witness))
    monkeypatch.setattr(
        link_central.link_approvals,
        "_resolve_target",
        lambda *_args, **_kwargs: {"title": "Quarterly plan", "error": None},
    )
    monkeypatch.setattr(
        link_central.link_approvals,
        "_link_recipient",
        lambda *_args, **_kwargs: (None, None),
    )
    return link_central.build_request_planner(
        link_central.PUBLISH_KIND,
        store=store,
        witness_resolver=lambda _url: witness,
        secret_resolver=lambda: SECRET,
    )


def _planned(monkeypatch, store, witness):
    planner = _planner(monkeypatch, store, witness)
    plan = planner(_context(), {
        "target_uuid": "b88e521a-8d21-4e16-8f7c-f9a60110c745",
        "target_type": "design",
        "meta": {
            "ttl": 3600,
            "label": "Quarterly plan",
            "ice_policy": "relay_only",
        },
    })
    payload = {
        "application_scope": "links",
        "kind": link_central.PUBLISH_KIND,
        "requester_ref": {"kind": "session", "id": "requester"},
        "decider": {"kind": "person", "id": "aa" * 32},
        "subject_ref": plan.subject_ref,
        "safe_review": dict(plan.safe_review),
        "request": dict(plan.request),
        "staged": dict(plan.staged),
        "created_at": 1000.0,
        "source_version": 1,
    }
    return plan, payload


def _decision(store, payload, witness_key, *, decision_time=1010.0):
    intent = store.get_intent(ORG, APPROVAL_ID)
    binding = _binding(witness_key.public_hex)["payload"]
    expected = link_central._expected_receipt_payload(payload, intent, binding)
    operation = intent["operation"]
    receipt = {
        "v": 1,
        "org_uuid": ORG_UUID,
        "binding_root_pub": ROOT,
        "binding_generation": GENERATION,
        "operation_id": intent["operation_id"],
        "operation": operation,
        "receipt_request_digest": link_central._json_digest(expected),
        "registry_input_digest": intent["registry_input_digest"],
        "local_intent_digest": intent["local_intent_digest"],
        "operand_digest": intent.get("operand_digest"),
        "origin_proof_commitment": intent["origin_proof_commitment"],
        "source_expires_at_ms": intent["registry_input"].get(
            "source_expires_at_ms"
        ),
        "accepting_signer_pub": "33" * 32,
        "accepting_subject_kind": "operator",
        "accepting_subject_id": "44" * 32,
        "accepted_at": 1005,
    }
    decision = {
        "envelope": {"payload": expected},
        "receipt": receipt,
        "signature": sign_link_operation_receipt(witness_key, receipt),
    }
    if operation == "publish":
        decision["ttl"] = intent["registry_input"].get("meta", {}).get("ttl")
    return decision


def _planned_revoke(monkeypatch, store, witness, *, grant=None):
    monkeypatch.setattr(link_central, "_binding_context", lambda org: _binding(witness))
    monkeypatch.setattr(
        link_central.link_approvals,
        "_cached_grant",
        lambda *_args: grant,
    )
    planner = link_central.build_request_planner(
        link_central.REVOKE_KIND,
        store=store,
        witness_resolver=lambda _url: witness,
        secret_resolver=lambda: SECRET,
    )
    plan = planner(_context(), {"token": "66" * 16})
    payload = {
        "application_scope": "links",
        "kind": link_central.REVOKE_KIND,
        "requester_ref": {"kind": "session", "id": "requester"},
        "decider": {"kind": "person", "id": "aa" * 32},
        "subject_ref": plan.subject_ref,
        "safe_review": dict(plan.safe_review),
        "request": dict(plan.request),
        "staged": dict(plan.staged),
        "created_at": 1000.0,
        "source_version": 1,
    }
    return plan, payload


def _planned_org_join(monkeypatch, store, witness):
    monkeypatch.setattr(link_central.link_approvals, "_org_join_request", lambda *_: {})
    planner = _planner(monkeypatch, store, witness)
    plan = planner(_context(), {
        "target_uuid": ORG_UUID,
        "target_type": "org:join",
        "invite_ref": "77" * 32,
        "expires_at": 2_000_000,
        "meta": {"label": "Join example org"},
    })
    payload = {
        "application_scope": "links",
        "kind": link_central.PUBLISH_KIND,
        "requester_ref": {"kind": "session", "id": "requester"},
        "decider": {"kind": "person", "id": "aa" * 32},
        "subject_ref": plan.subject_ref,
        "safe_review": dict(plan.safe_review),
        "request": dict(plan.request),
        "staged": dict(plan.staged),
        "created_at": 1000.0,
        "expires_at": 2000.0,
        "source_version": 1,
    }
    return plan, payload


def test_production_link_kinds_remain_disabled():
    registry = build_production_registry()
    assert registry.kinds[link_central.PUBLISH_KIND].runtime is None
    assert registry.kinds[link_central.REVOKE_KIND].runtime is None


@pytest.mark.parametrize("principal_kind", [
    api_auth.ApiPrincipalKind.LOCAL_SESSION.value,
    api_auth.ApiPrincipalKind.OPERATOR_COOKIE.value,
    api_auth.ApiPrincipalKind.MCP_SERVICE.value,
    api_auth.ApiPrincipalKind.EXTERNAL_SERVICE.value,
    None,
])
def test_link_planner_requires_server_derived_org_session(monkeypatch, principal_kind):
    witness = KeyPair.generate()
    planner = _planner(monkeypatch, MemoryStore(), witness.public_hex)
    body = {
        "target_uuid": "b88e521a-8d21-4e16-8f7c-f9a60110c745",
        "target_type": "design",
    }
    with pytest.raises(link_central.LinkCentralError, match="unauthenticated"):
        planner(_context(principal_kind), body)
    with pytest.raises(link_central.LinkCentralError, match="invalid request"):
        planner(_context(org=None), body)


@pytest.mark.parametrize("selector", [
    "org", "session", "application_scope", "audience", "destination",
    "registry_url", "invite_token", "fragment_secret", "password",
])
def test_link_planner_rejects_caller_routing_selectors(monkeypatch, selector):
    witness = KeyPair.generate()
    store = MemoryStore()
    planner = _planner(monkeypatch, store, witness.public_hex)
    body = {
        "target_uuid": "b88e521a-8d21-4e16-8f7c-f9a60110c745",
        "target_type": "design",
        selector: "caller-selected",
    }
    with pytest.raises(link_central.LinkCentralError, match="invalid request"):
        planner(_context(), body)
    assert store.intents == {}


def test_link_planner_accepts_the_platform_org_slug_vocabulary(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    planner = _planner(monkeypatch, store, witness.public_hex)
    plan = planner(_context(org="example_org.2"), {
        "target_uuid": "b88e521a-8d21-4e16-8f7c-f9a60110c745",
        "target_type": "design",
    })
    assert plan.request["org_ref"] == "example_org.2"
    assert store.get_intent("example_org.2", APPROVAL_ID) is not None


@pytest.mark.parametrize(
    ("expires_at", "expected_state"),
    [
        ("2030-01-01T00:00:00Z", "binding_generation_recovery_required"),
        ("1970-01-01T00:00:01Z", "binding_reclaim_required"),
    ],
)
def test_v1_binding_plans_recovery_without_inventing_generation(
    monkeypatch, expires_at, expected_state,
):
    payload = dict(_binding("33" * 32)["payload"])
    payload.pop("binding_generation")
    payload["binding_expires_at"] = expires_at
    monkeypatch.setattr(
        link_central,
        "_binding_context",
        lambda _org: {"key": "registry.example", "revision": 1, "payload": payload},
    )
    planned = link_central._binding_plan(
        ORG,
        planning_time=1000.0,
        uuid_factory=lambda: None,
        witness_resolver=lambda _url: "33" * 32,
    )
    assert planned["state"] == expected_state
    assert "binding_generation" not in planned
    assert planned["registration_payload"] == {
        "org_uuid": ORG_UUID,
        "root_pub": ROOT,
        "recovery_policy": {"mode": "none"},
    }


def test_unbound_planning_freezes_server_generated_registration(monkeypatch):
    proposed = "55555555-5555-4555-8555-555555555555"
    monkeypatch.setattr(link_central, "_binding_context", lambda _org: None)
    monkeypatch.setattr(link_central, "_org_root_public_key", lambda _org: ROOT)
    monkeypatch.setattr(
        link_central.network_routes, "_registry_url", lambda: "https://registry.example"
    )
    planned = link_central._binding_plan(
        ORG,
        planning_time=1000.0,
        uuid_factory=lambda: proposed,
        witness_resolver=lambda _url: "33" * 32,
    )
    assert planned["state"] == "registration_required"
    assert planned["org_uuid"] == proposed
    assert "binding_generation" not in planned
    assert planned["registration_payload"]["org_uuid"] == proposed


def test_expired_org_join_stages_nothing(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    planner = _planner(monkeypatch, store, witness.public_hex)
    monkeypatch.setattr(link_central.link_approvals, "_org_join_request", lambda *_: {})
    with pytest.raises(link_central.ApprovalServiceError) as raised:
        planner(_context(), {
            "target_uuid": ORG_UUID,
            "target_type": "org:join",
            "invite_ref": "77" * 32,
            "expires_at": 999_999,
        })
    assert raised.value.code == "source_expired"
    assert store.intents == {}


def test_uncached_revoke_stages_bearer_only_in_org_intent(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    plan, payload = _planned_revoke(monkeypatch, store, witness.public_hex)
    intent = store.get_intent(ORG, APPROVAL_ID)
    assert plan.safe_review["target_title"] == "Unresolved link"
    assert intent["target"] == {"resolved": False}
    assert intent["revoke_token"] == "66" * 16
    assert "revoke_token" not in json.dumps(payload, sort_keys=True)


def test_publish_plan_keeps_local_fields_and_proof_out_of_personal_truth(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    plan, payload = _planned(monkeypatch, store, witness.public_hex)
    intent = store.get_intent(ORG, APPROVAL_ID)
    assert plan.request["org_ref"] == ORG
    assert plan.request["intent_ref"] == APPROVAL_ID
    assert "ice_policy" not in canonical_json(plan.request).decode()
    assert '"origin_proof":' not in json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    )
    assert intent["local_intent"]["meta"]["ice_policy"] == "relay_only"
    assert "ice_policy" not in intent["registry_input"]["meta"]
    assert intent["origin_destination_id"] == link_central.link_result_destination_id(
        secret=SECRET
    )
    assert intent["origin_proof_commitment"] == link_central.link_origin_proof_commitment(
        link_central.link_origin_proof(APPROVAL_ID, "publish", secret=SECRET)
    )


def test_planner_secret_failure_stages_no_intent(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    monkeypatch.setattr(link_central, "_binding_context", lambda _org: _binding(witness.public_hex))
    monkeypatch.setattr(
        link_central.link_approvals,
        "_resolve_target",
        lambda *_args, **_kwargs: {"title": "Quarterly plan", "error": None},
    )
    monkeypatch.setattr(
        link_central.link_approvals,
        "_link_recipient",
        lambda *_args, **_kwargs: (None, None),
    )
    planner = link_central.build_request_planner(
        link_central.PUBLISH_KIND,
        store=store,
        witness_resolver=lambda _url: witness.public_hex,
        secret_resolver=lambda: (_ for _ in ()).throw(RuntimeError("vault unavailable")),
    )
    with pytest.raises(link_central.LinkCentralError, match="origin proof unavailable"):
        planner(_context(), {
            "target_uuid": "b88e521a-8d21-4e16-8f7c-f9a60110c745",
            "target_type": "design",
        })
    assert store.intents == {}


def test_registered_runtime_preserves_bounded_link_failure_code(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    monkeypatch.setattr(link_central, "_binding_context", lambda _org: _binding(witness.public_hex))
    runtime = link_central.build_approval_runtime(
        link_central.PUBLISH_KIND,
        store=store,
        witness_resolver=lambda _url: witness.public_hex,
        secret_resolver=lambda: (_ for _ in ()).throw(RuntimeError("vault unavailable")),
    )
    with pytest.raises(link_central.ApprovalServiceError) as raised:
        runtime.request_planner(_context(), {
            "target_uuid": "b88e521a-8d21-4e16-8f7c-f9a60110c745",
            "target_type": "design",
        })
    assert raised.value.code == "origin_proof_unavailable"
    assert store.intents == {}


def test_link_decision_is_pure_and_receipt_bound(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    validated = link_central.validate_link_decision(
        link_central.ApprovalDecisionContext(APPROVAL_ID, 1010.0),
        payload,
        decision,
        True,
        store=store,
    )
    assert validated == decision
    changed = dict(decision)
    changed["ttl"] = 7200
    with pytest.raises(ValueError, match="lifetime"):
        link_central.validate_link_decision(
            link_central.ApprovalDecisionContext(APPROVAL_ID, 1010.0),
            payload,
            changed,
            True,
            store=store,
        )


def test_link_decision_refuses_registry_acceptance_at_source_deadline(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    payload["expires_at"] = 1005.0
    decision = _decision(store, payload, witness, decision_time=1004.0)
    decision["receipt"]["accepted_at"] = 1005
    decision["signature"] = sign_link_operation_receipt(
        witness,
        decision["receipt"],
    )
    with pytest.raises(ValueError, match="expired"):
        link_central.validate_link_decision(
            link_central.ApprovalDecisionContext(APPROVAL_ID, 1004.0),
            payload,
            decision,
            True,
            store=store,
        )


def test_receipt_forwarder_exact_join_and_registry_wire(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    status = ApprovalStatus(
        "open",
        ApprovalRecord(APPROVAL_ID, payload),
        None,
    )

    class Index:
        def get_query_item(self, attention_id):
            assert attention_id == link_central.link_attention_id(APPROVAL_ID)
            return SimpleNamespace(payload={
                "participant_role": "recipient",
                "attention_state": "needs_attention",
                "source_version": 1,
                "application_scope": "links",
                "object_ref": APPROVAL_ID,
            })

    class Approvals:
        def status(self, approval_id):
            assert approval_id == APPROVAL_ID
            return status

    calls = []
    forwarder = link_central.LinkReceiptForwarder(
        Approvals(),
        Index(),
        store,
        transport=lambda registry, body: calls.append((registry, body)) or {
            "receipt": decision["receipt"],
            "signature": decision["signature"],
        },
        clock=lambda: 1010.0,
        witness_resolver=lambda _url: witness.public_hex,
        secret_resolver=lambda: SECRET,
    )
    result = forwarder.forward(
        link_central.link_attention_id(APPROVAL_ID),
        {"envelope": decision["envelope"]},
    )
    assert result == {
        "receipt": decision["receipt"],
        "signature": decision["signature"],
    }
    assert calls[0][0] == "https://registry.example"
    assert set(calls[0][1]) == {"envelope", "registry_input"}
    assert "relay_only" not in json.dumps(calls[0][1], sort_keys=True)
    assert set(calls[0][1]["registry_input"]) == {
        "target_uuid", "target_type", "meta", "operation_id",
    }


def test_receipt_forwarder_refuses_personal_org_mismatch_before_network(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    payload = dict(payload)
    payload["request"] = dict(payload["request"], org_ref="different-org")
    status = ApprovalStatus("open", ApprovalRecord(APPROVAL_ID, payload), None)

    class Index:
        def get_query_item(self, _attention_id):
            return SimpleNamespace(payload={
                "participant_role": "recipient",
                "attention_state": "needs_attention",
                "source_version": 1,
                "application_scope": "links",
                "object_ref": APPROVAL_ID,
            })

    class Approvals:
        def status(self, _approval_id):
            return status

    calls = []
    forwarder = link_central.LinkReceiptForwarder(
        Approvals(), Index(), store,
        transport=lambda *_args: calls.append(True) or {},
        witness_resolver=lambda _url: witness.public_hex,
    )
    with pytest.raises(link_central.LinkCentralError, match="unavailable"):
        forwarder.forward(
            link_central.link_attention_id(APPROVAL_ID),
            {"envelope": {"payload": {}}},
        )
    assert calls == []


def test_receipt_forwarder_rejects_payload_drift_before_registry_io(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    status = ApprovalStatus("open", ApprovalRecord(APPROVAL_ID, payload), None)

    class Index:
        def get_query_item(self, _attention_id):
            return SimpleNamespace(payload={
                "participant_role": "recipient",
                "attention_state": "needs_attention",
                "source_version": 1,
                "application_scope": "links",
                "object_ref": APPROVAL_ID,
            })

    class Approvals:
        def status(self, _approval_id):
            return status

    witness_calls = []
    transport_calls = []
    forwarder = link_central.LinkReceiptForwarder(
        Approvals(), Index(), store,
        transport=lambda *_args: transport_calls.append(True) or {},
        witness_resolver=lambda _url: witness_calls.append(True) or witness.public_hex,
        secret_resolver=lambda: SECRET,
    )
    with pytest.raises(link_central.LinkCentralError, match="invalid decision"):
        forwarder.forward(
            link_central.link_attention_id(APPROVAL_ID),
            {"envelope": {"payload": {"operation": "publish"}}},
        )
    assert witness_calls == []
    assert transport_calls == []


def test_attention_runtime_advances_exact_approval_lifecycle():
    request = ApprovalRecord(APPROVAL_ID, {
        "kind": link_central.PUBLISH_KIND,
        "safe_review": {"target_title": "Quarterly plan"},
        "created_at": 1000.0,
        "expires_at": 2000.0,
    })
    pending = ApprovalStatus("open", request, None)
    resolved = ApprovalStatus(
        "resolved",
        request,
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": {},
            "resolved_at": 1010.0,
        }),
    )

    class Approvals:
        current = pending

        def status(self, approval_id):
            assert approval_id == APPROVAL_ID
            return self.current

    approvals = Approvals()
    runtime = link_central.build_attention_runtime(
        link_central.PUBLISH_KIND, approvals
    )
    first = runtime.projection_planner(pending)
    assert (first.attention_state, first.source_version) == ("needs_attention", 1)
    assert first.attention_id == link_central.link_attention_id(APPROVAL_ID)
    evidence = runtime.source_evidence_builder(APPROVAL_ID, 1)
    assert evidence.source_guard == {
        "kind": "approval", "ref": APPROVAL_ID, "version": 1,
    }
    assert evidence.source_expires_at == 2000.0

    approvals.current = resolved
    second = runtime.projection_planner(resolved)
    assert (second.attention_state, second.source_version) == ("resolved", 2)
    with pytest.raises(link_central.AttentionIndexError, match="stale source"):
        runtime.source_evidence_builder(APPROVAL_ID, 1)
    assert runtime.source_evidence_builder(APPROVAL_ID, 2).source_guard["version"] == 2


def test_origin_consumer_converges_on_one_public_result(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    resolution = ApprovalRecord(APPROVAL_ID, {
        "outcome": "granted",
        "decision": decision,
        "resolved_at": 1010.0,
    })
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        resolution,
    )
    writes = []
    monkeypatch.setattr(
        link_central.settings_ops,
        "upsert_by_key",
        lambda *args, **kwargs: writes.append((args, kwargs)) or "setting-1",
    )

    async def live(*_args, **_kwargs):
        return {
            "live": False,
            "via": "registry-http",
            "detail": "socket failed at private-registry.internal",
        }

    monkeypatch.setattr(link_central.link_approvals, "_probe_serving", live)
    calls = []
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        tunnel_transport=lambda org, operation, body: calls.append(
            (org, operation, body)
        ) or {
            "ok": True,
            "token": "55" * 16,
            "url": "https://registry.example/l/" + "55" * 16,
        },
        clock=lambda: 1020.0,
        witness_resolver=lambda _url: witness.public_hex,
    )
    assert consumer.materialize(status) is True
    assert consumer.materialize(status) is True
    assert len(calls) == 1
    assert len(writes) == 1
    projected = consumer.project(status)
    assert projected["approved"] is True
    assert projected["execution"] == {"ok": True}
    assert projected["token"] == "55" * 16
    assert projected["serving"] == {"live": True, "via": "tunnel-control"}
    assert calls[0][0:2] == (ORG, "create-link")
    assert set(calls[0][2]) == {
        "operation_id", "receipt", "signature", "origin_proof",
        "target_uuid", "target_type", "meta",
    }
    assert "relay_only" not in json.dumps(calls[0][2], sort_keys=True)
    operator = link_central.build_operator_result_projector(consumer)(status)
    assert "token" not in operator
    assert operator["url"] == projected["url"]
    assert '"origin_proof":' not in json.dumps(
        list(store.results.values()), separators=(",", ":"), sort_keys=True
    )


def test_org_join_publish_uses_http_and_never_tunnel_control(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned_org_join(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": decision,
            "resolved_at": 1010.0,
        }),
    )
    monkeypatch.setattr(
        link_central.settings_ops,
        "upsert_by_key",
        lambda *_args, **_kwargs: "setting-1",
    )

    async def live(*_args, **_kwargs):
        return {"live": False, "via": "registry-http"}

    monkeypatch.setattr(link_central.link_approvals, "_probe_serving", live)
    http_calls, tunnel_calls = [], []
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        transport=lambda registry, operation_id, body: http_calls.append(
            (registry, operation_id, body)
        ) or {
            "state": "succeeded",
            "token": "55" * 16,
            "url": "https://registry.example/l/" + "55" * 16,
            "completed_at": 1020.0,
        },
        tunnel_transport=lambda *_args: tunnel_calls.append(True) or {},
        witness_resolver=lambda _url: witness.public_hex,
    )
    assert consumer.materialize(status) is True
    assert len(http_calls) == 1
    assert tunnel_calls == []
    assert http_calls[0][0:2] == (
        "https://registry.example",
        link_central.link_operation_id(APPROVAL_ID, "publish"),
    )
    assert http_calls[0][2]["registry_input"]["target_type"] == "org:join"


def test_first_ordinary_publish_starts_tunnel_before_control(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": _decision(store, payload, witness),
            "resolved_at": 1010.0,
        }),
    )
    monkeypatch.setattr(
        link_central.settings_ops,
        "upsert_by_key",
        lambda *_args, **_kwargs: "setting-1",
    )
    order = []

    class Supervisor:
        def start(self, org):
            order.append(("start", org))
            return {"running": True}

    from tools.dashboard import link_serving_supervisor

    monkeypatch.setattr(link_serving_supervisor, "get_supervisor", lambda: Supervisor())

    def control(org, args):
        assert order == [("start", ORG)]
        order.append(("control", org, args))
        return {
            "ok": True,
            "token": "55" * 16,
            "url": "https://registry.example/l/" + "55" * 16,
        }

    monkeypatch.setattr(link_central.link_approvals, "_create_link_over_tunnel", control)
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        clock=lambda: 1020.0,
        witness_resolver=lambda _url: witness.public_hex,
    )
    assert consumer.materialize(status) is True
    assert [entry[0] for entry in order] == ["start", "control"]


def test_tunnel_reply_loss_replays_the_exact_stable_operation(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": _decision(store, payload, witness),
            "resolved_at": 1010.0,
        }),
    )
    monkeypatch.setattr(
        link_central.settings_ops,
        "upsert_by_key",
        lambda *_args, **_kwargs: "setting-1",
    )
    calls = []

    def tunnel(org, operation, args):
        calls.append((org, operation, json.loads(json.dumps(args))))
        if len(calls) == 1:
            raise ConnectionError("reply lost after registry commit")
        return {
            "ok": True,
            "token": "55" * 16,
            "url": "https://registry.example/l/" + "55" * 16,
        }

    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        tunnel_transport=tunnel,
        clock=lambda: 1020.0,
        witness_resolver=lambda _url: witness.public_hex,
    )
    with pytest.raises(link_central.LinkCentralError, match="registry unavailable"):
        consumer.materialize(status)
    assert store.results == {}
    assert consumer.materialize(status) is True
    assert calls[0] == calls[1]
    assert calls[0][2]["operation_id"] == link_central.link_operation_id(
        APPROVAL_ID,
        "publish",
    )


def test_origin_consumer_serializes_concurrent_local_materialization(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": decision,
            "resolved_at": 1010.0,
        }),
    )
    monkeypatch.setattr(
        link_central.settings_ops,
        "upsert_by_key",
        lambda *_args, **_kwargs: "setting-1",
    )

    async def live(*_args, **_kwargs):
        return {"live": True, "via": "registry-http"}

    monkeypatch.setattr(link_central.link_approvals, "_probe_serving", live)
    calls = []
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        tunnel_transport=lambda *_args: calls.append(True) or {
            "ok": True,
            "token": "55" * 16,
            "url": "https://registry.example/l/" + "55" * 16,
        },
        witness_resolver=lambda _url: witness.public_hex,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _unused: consumer.materialize(status), range(2)))
    assert results == [True, True]
    assert calls == [True]


def test_origin_consumer_wrong_destination_is_inert(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": decision,
            "resolved_at": 1010.0,
        }),
    )
    calls = []
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: b"other-origin" * 4,
        tunnel_transport=lambda *_args: calls.append(True) or {},
        witness_resolver=lambda _url: witness.public_hex,
    )
    assert consumer.materialize(status) is False
    assert calls == []
    assert store.results == {}


def test_copied_rows_on_another_machine_cannot_claim_or_disclose(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    open_status = ApprovalStatus(
        "open",
        ApprovalRecord(APPROVAL_ID, payload),
        None,
    )
    resolved = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": decision,
            "resolved_at": 1010.0,
        }),
    )
    store.put_result(ORG, APPROVAL_ID, {
        "operation": "publish",
        "state": "succeeded",
        "completed_at": 1020.0,
        "operation_id": link_central.link_operation_id(APPROVAL_ID, "publish"),
        "token": "55" * 16,
        "url": "https://registry.example/l/" + "55" * 16,
        "serving": {"live": True, "via": "tunnel-control"},
    })
    external = []

    class Index:
        def get_query_item(self, _attention_id):
            return SimpleNamespace(payload={
                "participant_role": "recipient",
                "attention_state": "needs_attention",
                "source_version": 1,
                "application_scope": "links",
                "object_ref": APPROVAL_ID,
            })

        def publish(self, *_args):
            return None

    class Approvals:
        def __init__(self, status):
            self.current = status

        def status(self, _approval_id):
            return self.current

    other_secret = lambda: b"another-machine-secret" * 2
    forwarder = link_central.LinkReceiptForwarder(
        Approvals(open_status),
        Index(),
        store,
        transport=lambda *_args: external.append("receipt") or {},
        witness_resolver=lambda _url: witness.public_hex,
        secret_resolver=other_secret,
    )
    with pytest.raises(link_central.LinkCentralError, match="not actionable"):
        forwarder.forward(
            link_central.link_attention_id(APPROVAL_ID),
            {"envelope": decision["envelope"]},
        )

    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=other_secret,
        transport=lambda *_args: external.append("http") or {},
        tunnel_transport=lambda *_args: external.append("tunnel") or {},
        witness_resolver=lambda _url: witness.public_hex,
    )
    assert consumer.materialize(resolved) is False
    assert consumer.project(resolved) is None
    assert link_central.build_operator_result_projector(consumer)(resolved) is None

    refreshes = []
    coordinator = link_central.LinkCoordinator(
        approvals=Approvals(resolved),
        index=Index(),
        producers={
            link_central.PUBLISH_KIND: object(),
            link_central.REVOKE_KIND: object(),
        },
        consumer=consumer,
        private_refresh=refreshes.append,
    )
    assert coordinator.reconcile_exact(APPROVAL_ID) == resolved
    assert external == []
    assert refreshes == []


def test_requester_result_projection_is_read_only_during_transient_execution(
    monkeypatch,
):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": _decision(store, payload, witness),
            "resolved_at": 1010.0,
        }),
    )
    external = []
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        transport=lambda *_args: external.append("http") or {},
        tunnel_transport=lambda *_args: external.append("tunnel") or {},
        witness_resolver=lambda _url: witness.public_hex,
    )
    adapter = link_central.build_http_adapter(
        link_central.PUBLISH_KIND,
        consumer,
    )
    assert [adapter.result_projector(status) for _ in range(5)] == [None] * 5
    assert external == []


@pytest.mark.parametrize("outcome", ["declined", "canceled", "expired"])
def test_origin_consumer_non_grants_never_reach_registry(monkeypatch, outcome):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": outcome,
            "resolved_at": 1010.0,
        }),
    )
    calls = []
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: (_ for _ in ()).throw(
            AssertionError("origin proof must not be resolved")
        ),
        tunnel_transport=lambda *_args: calls.append(True) or {},
        witness_resolver=lambda _url: witness.public_hex,
    )
    assert consumer.materialize(status) is False
    assert calls == []
    assert store.results == {}


def test_origin_consumer_refuses_witness_drift_before_execution(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": decision,
            "resolved_at": 1010.0,
        }),
    )
    calls = []
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        tunnel_transport=lambda *_args: calls.append(True) or {},
        witness_resolver=lambda _url: "99" * 32,
        clock=lambda: 1020.0,
    )
    assert consumer.materialize(status) is True
    assert calls == []
    assert store.results[(ORG, APPROVAL_ID)]["error_code"] == "binding_drift"


def test_origin_consumer_records_stable_revoke_not_found(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned_revoke(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": decision,
            "resolved_at": 1010.0,
        }),
    )
    drops = []
    monkeypatch.setattr(
        link_central.link_approvals,
        "_drop_cached_grant",
        lambda *_args: drops.append(True) or True,
    )
    tunnel_calls = []
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        transport=lambda *_args: {
            "state": "not_found",
            "completed_at": 1020.0,
            "revoked_at": None,
        },
        tunnel_transport=lambda *_args: tunnel_calls.append(True) or {},
        witness_resolver=lambda _url: witness.public_hex,
    )
    assert consumer.materialize(status) is True
    assert drops == []
    assert tunnel_calls == []
    assert store.results[(ORG, APPROVAL_ID)]["error_code"] == "not_found"


def test_origin_consumer_converges_known_revoke_once(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    grant = {
        "target_uuid": "b88e521a-8d21-4e16-8f7c-f9a60110c745",
        "target_type": "design",
        "meta": {"label": "Quarterly plan"},
    }
    _plan, payload = _planned_revoke(
        monkeypatch,
        store,
        witness.public_hex,
        grant=grant,
    )
    decision = _decision(store, payload, witness)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": decision,
            "resolved_at": 1010.0,
        }),
    )
    drops = []
    monkeypatch.setattr(
        link_central.link_approvals,
        "_drop_cached_grant",
        lambda token, org: drops.append((token, org)),
    )
    monkeypatch.setattr(link_central.link_approvals, "_cached_grant", lambda *_args: None)
    calls = []
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        tunnel_transport=lambda org, operation, args: calls.append(
            (org, operation, args)
        ) or {
            "ok": True,
            "state": "succeeded",
            "revoked_at": 1019.0,
        },
        witness_resolver=lambda _url: witness.public_hex,
    )
    assert consumer.materialize(status) is True
    assert consumer.materialize(status) is True
    assert len(calls) == 1
    assert calls[0][0:2] == (ORG, "revoke-link")
    assert set(calls[0][2]) == {
        "operation_id", "receipt", "signature", "origin_proof", "token",
    }
    assert drops == [("66" * 16, ORG)]
    result = store.results[(ORG, APPROVAL_ID)]
    assert result["state"] == "succeeded"
    assert result["via"] == "tunnel-control"
    assert "registry_status" not in result


def test_malformed_publish_success_writes_no_cache_or_result(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": decision,
            "resolved_at": 1010.0,
        }),
    )
    writes = []
    monkeypatch.setattr(
        link_central.settings_ops,
        "upsert_by_key",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    monkeypatch.setattr(
        link_central.link_approvals,
        "_probe_serving",
        lambda *_args: None,
    )
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        tunnel_transport=lambda *_args: {
            "ok": True,
            "token": "55" * 16,
            "url": "https://evil.example/not-the-token",
        },
        witness_resolver=lambda _url: witness.public_hex,
    )
    with pytest.raises(link_central.LinkCentralError, match="registry unavailable"):
        consumer.materialize(status)
    assert writes == []
    assert store.results == {}


def test_registry_failure_diagnostics_are_safely_projected(monkeypatch):
    witness = KeyPair.generate()
    store = MemoryStore()
    _plan, payload = _planned_revoke(monkeypatch, store, witness.public_hex)
    decision = _decision(store, payload, witness)
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": decision,
            "resolved_at": 1010.0,
        }),
    )
    consumer = link_central.LinkResultConsumer(
        store=store,
        secret_resolver=lambda: SECRET,
        transport=lambda *_args: {
            "state": "failed",
            "completed_at": 1020.0,
            "error_code": "backend_internal_secret",
            "error_message": "postgresql://password@private-host/database",
        },
        witness_resolver=lambda _url: witness.public_hex,
    )
    assert consumer.materialize(status) is True
    result = store.results[(ORG, APPROVAL_ID)]
    assert result["error_code"] == "registry_refused"
    assert result["error_message"] == (
        "The registry permanently refused this Link operation."
    )
    assert "private-host" not in json.dumps(result, sort_keys=True)


def test_receipt_forwarder_bounds_nested_input_before_lookup():
    nested = {}
    cursor = nested
    for _ in range(10):
        cursor["next"] = {}
        cursor = cursor["next"]
    forwarder = link_central.LinkReceiptForwarder(None, None, MemoryStore())
    with pytest.raises(link_central.LinkCentralError, match="invalid request"):
        forwarder.forward("ignored", {"envelope": nested})


def test_application_store_replays_float_result_without_rewrite(monkeypatch):
    result = {
        "operation": "publish",
        "state": "succeeded",
        "completed_at": 1020.25,
        "operation_id": "88" * 32,
        "token": "99" * 16,
        "url": "https://registry.example/l/" + "99" * 16,
        "serving": {"live": True, "via": "registry-http"},
    }
    monkeypatch.setattr(
        link_central,
        "_one_owned_member",
        lambda *_args, **_kwargs: SimpleNamespace(
            stored_revision=1, payload=dict(result)
        ),
    )
    writes = []
    monkeypatch.setattr(
        link_central.settings_ops,
        "add_setting",
        lambda *_args, **_kwargs: writes.append(True),
    )
    store = link_central.LinkApplicationStore()
    store.put_result(ORG, APPROVAL_ID, result)
    assert writes == []
    changed = dict(result, completed_at=1021.0)
    with pytest.raises(link_central.LinkCentralError, match="result conflict"):
        store.put_result(ORG, APPROVAL_ID, changed)


def test_coordinator_retries_startup_truth_and_emits_private_refresh(monkeypatch):
    payload = {
        "kind": link_central.PUBLISH_KIND,
        "created_at": 1000.0,
    }
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, payload),
        ApprovalRecord(APPROVAL_ID, {
            "outcome": "granted",
            "decision": {},
            "resolved_at": 1010.0,
        }),
    )

    class Approvals:
        def status(self, approval_id):
            assert approval_id == APPROVAL_ID
            return status

    published = []

    class Index:
        def publish(self, producer, source):
            published.append((producer, source))

    materialized = []

    class Consumer:
        def materialize(self, source):
            materialized.append(source)
            return True

        def project(self, source, *, operator=False):
            assert operator is True
            return {"approved": True, "execution": {"ok": True}}

    refreshes = []
    coordinator = link_central.LinkCoordinator(
        approvals=Approvals(),
        index=Index(),
        producers={
            link_central.PUBLISH_KIND: "publish-producer",
            link_central.REVOKE_KIND: "revoke-producer",
        },
        consumer=Consumer(),
        private_refresh=refreshes.append,
    )
    scans = 0

    def scan():
        nonlocal scans
        scans += 1
        if scans == 1:
            raise RuntimeError("partial Settings read")
        return ((APPROVAL_ID,), False)

    monkeypatch.setattr(coordinator, "_scan_ids", scan)

    async def exercise():
        await coordinator.start()
        for _ in range(200):
            if refreshes:
                break
            await asyncio.sleep(0.01)
        await coordinator.stop()
        coordinator.offer(APPROVAL_ID)

    asyncio.run(exercise())
    assert scans >= 2
    assert published == [("publish-producer", status)]
    assert materialized == [status]
    assert refreshes == [link_central.link_attention_id(APPROVAL_ID)]
    assert coordinator._pending == set()


def test_coordinator_alone_retries_transient_result_materialization(monkeypatch):
    status = ApprovalStatus(
        "resolved",
        ApprovalRecord(APPROVAL_ID, {"kind": link_central.PUBLISH_KIND}),
        ApprovalRecord(APPROVAL_ID, {"outcome": "granted"}),
    )

    class Approvals:
        def status(self, approval_id):
            assert approval_id == APPROVAL_ID
            return status

    class Index:
        def publish(self, _producer, source):
            assert source is status

    calls = []

    class Consumer:
        def materialize(self, source):
            assert source is status
            calls.append("materialize")
            if len(calls) == 1:
                raise link_central.LinkCentralError("registry_unavailable")
            return True

        def project(self, source, *, operator=False):
            assert source is status
            assert operator is True
            return {"approved": True, "execution": {"ok": True}}

    refreshes = []
    coordinator = link_central.LinkCoordinator(
        approvals=Approvals(),
        index=Index(),
        producers={
            link_central.PUBLISH_KIND: "publish-producer",
            link_central.REVOKE_KIND: "revoke-producer",
        },
        consumer=Consumer(),
        private_refresh=refreshes.append,
    )
    monkeypatch.setattr(
        coordinator,
        "_scan_ids",
        lambda: ((APPROVAL_ID,), False),
    )
    coordinator._retry_seconds = 0.01

    async def exercise():
        await coordinator.start()
        coordinator._retry_seconds = 0.01
        for _ in range(200):
            if refreshes:
                break
            await asyncio.sleep(0.01)
        await coordinator.stop()

    asyncio.run(exercise())
    assert calls == ["materialize", "materialize"]
    assert refreshes == [link_central.link_attention_id(APPROVAL_ID)]


def test_coordinator_poisoned_id_does_not_starve_later_scan_rows(monkeypatch):
    coordinator = link_central.LinkCoordinator(
        approvals=object(),
        index=object(),
        producers={
            link_central.PUBLISH_KIND: object(),
            link_central.REVOKE_KIND: object(),
        },
        consumer=object(),
    )
    poisoned_id = "aaaaaaaa-poisoned-link-approval"
    reconciled = []

    monkeypatch.setattr(
        coordinator,
        "_scan_ids",
        lambda: ((poisoned_id, APPROVAL_ID), False),
    )

    def reconcile(approval_id):
        reconciled.append(approval_id)
        if approval_id == poisoned_id:
            raise RuntimeError("poisoned approval row")

    monkeypatch.setattr(coordinator, "reconcile_exact", reconcile)
    coordinator._retry_seconds = 0.01

    async def exercise():
        await coordinator.start()
        coordinator._retry_seconds = 0.01
        for _ in range(200):
            if APPROVAL_ID in reconciled:
                break
            await asyncio.sleep(0.01)
        await coordinator.stop()

    asyncio.run(exercise())
    assert reconciled[:2] == [poisoned_id, APPROVAL_ID]


def test_coordinator_real_scan_skips_malformed_rows_and_progresses_valid_links(
    monkeypatch,
):
    witness = KeyPair.generate()
    _plan, publish_payload = _planned(
        monkeypatch,
        MemoryStore(),
        witness.public_hex,
    )
    _plan, revoke_payload = _planned_revoke(
        monkeypatch,
        MemoryStore(),
        witness.public_hex,
    )
    revoke_id = "central-link-revoke-0123456789abcdef"

    class Rows(list):
        dropped = {}

    rows = Rows([
        SimpleNamespace(key="central-bad-unrelated", payload="not-an-object"),
        SimpleNamespace(key=APPROVAL_ID, payload=publish_payload),
        SimpleNamespace(key=revoke_id, payload=revoke_payload),
    ])
    monkeypatch.setattr(
        link_central.settings_ops,
        "read_set",
        lambda *_args, **_kwargs: rows,
    )
    coordinator = link_central.LinkCoordinator(
        approvals=object(),
        index=object(),
        producers={
            link_central.PUBLISH_KIND: object(),
            link_central.REVOKE_KIND: object(),
        },
        consumer=object(),
    )
    assert coordinator._scan_ids() == (
        tuple(sorted((APPROVAL_ID, revoke_id))),
        True,
    )
    reconciled = []
    monkeypatch.setattr(coordinator, "reconcile_exact", reconciled.append)
    coordinator._retry_seconds = 0.01

    async def exercise():
        await coordinator.start()
        coordinator._retry_seconds = 0.01
        for _ in range(200):
            if {APPROVAL_ID, revoke_id}.issubset(reconciled):
                break
            await asyncio.sleep(0.01)
        await coordinator.stop()

    asyncio.run(exercise())
    assert {APPROVAL_ID, revoke_id}.issubset(reconciled)


def test_principal_planning_context_carries_kind_and_org():
    captured = {}

    def planner(context, _body):
        captured.update({
            "kind": context.requester_principal_kind,
            "org": context.requester_org,
        })
        return {
            "subject_ref": "test",
            "safe_review": {"title": "Test"},
            "request": {"operation": "test"},
        }

    runtime = link_central.ApprovalKindRuntime(
        request_planner=planner,
        decision_validator=lambda *_args: {},
        resolution_consumer_id="links.central-operation.v1",
    )
    registry = build_production_registry(runtimes={
        link_central.REVOKE_KIND: runtime,
    })
    from tools.dashboard.approval_service import ApprovalService, InMemoryApprovalStore

    service = ApprovalService(
        registry=registry,
        store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: "aa" * 32,
        clock=lambda: 1000.0,
    )
    service.create_from_principal(
        link_central.REVOKE_KIND,
        api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.ORG_SESSION,
            subject="session-1",
            org=ORG,
        ),
        {},
    )
    assert captured == {"kind": "org_session", "org": ORG}
