"""Contract proof for the Settings requester HTTP compatibility bridge."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from tools.dashboard import api_auth, approvals_routes, attention_routes
from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridge,
    ApprovalHttpBridgeError,
    ApprovalHttpKindAdapter,
    ApprovalHttpRegistry,
    ApprovalWaitHub,
    CanonicalLegacyDecision,
    MAX_PROJECTED_JSON_BYTES,
    MAX_PROJECTED_JSON_DEPTH,
    central_stable_approval_id,
    has_central_approval_prefix,
    is_central_approval_id,
)
from tools.dashboard.approval_kind_registry import (
    ApplicationScopePolicy,
    ApprovalAttentionClass,
    ApprovalAttentionClassCatalog,
    ApprovalExpiryPolicy,
    ApprovalKindRegistration,
    ApprovalKindRegistry,
    ApprovalKindRuntime,
    AuthorityRequirement,
    DeciderPolicy,
    ExpiryMode,
    RequesterPolicy,
    PRODUCTION_APPROVAL_REGISTRY,
    RegisteredApprovalProducer,
)
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    HumanApprovalActor,
    InMemoryApprovalStore,
)
from tools.dashboard.attention_registry import (
    AttentionApplicationRegistration,
    AttentionClassPolicy,
    AttentionClassRegistration,
    AttentionPublicationRuntime,
    AttentionRegistry,
)


KIND = "test_kind"


def _principal(*, subject="auto-1", org=None):
    kind = (
        api_auth.ApiPrincipalKind.LOCAL_SESSION
        if org is None else api_auth.ApiPrincipalKind.ORG_SESSION
    )
    return api_auth.ApiPrincipal(kind, subject=subject, org=org)


def _adapter(results):
    def map_decision(body):
        if not isinstance(body, dict) or not isinstance(body.get("approved"), bool):
            raise ValueError("approved required")
        if set(body) - {"approved", "reason"}:
            raise ValueError("unexpected decision field")
        decision = {} if "reason" not in body else {"reason": body["reason"]}
        return CanonicalLegacyDecision(
            "granted" if body["approved"] else "declined",
            decision,
        )

    return ApprovalHttpKindAdapter(
        kind=KIND,
        request_projector=lambda payload: payload["request"],
        result_projector=lambda status: results.get(status.request.approval_id),
        legacy_decision_mapper=map_decision,
    )


def _build(
    *, approval_active=True, attention_active=True, expiry=None,
    http_active=True, clock=None, store=None,
    requester_policy=RequesterPolicy.SESSION_PRINCIPAL,
):
    runtime = ApprovalKindRuntime(
        request_planner=lambda _context, body: {
            "subject_ref": "subject-1",
            "safe_review": {"summary": "Review test"},
            "request": dict(body),
        },
        decision_validator=lambda _request, decision, _grant: dict(decision),
        resolution_consumer_id="test.consumer",
    ) if approval_active else None
    registration = ApprovalKindRegistration(
        kind=KIND,
        application_scope_policy=ApplicationScopePolicy(fixed="test_app"),
        notification_class="approval.test_kind.requested",
        renderer_id="approval.test_kind.review",
        requester_policy=requester_policy,
        decider_policy=DeciderPolicy.PERSONAL_OPERATOR,
        authority_requirement=AuthorityRequirement.OPERATOR_SESSION,
        request_expiry_policy=expiry or ApprovalExpiryPolicy(mode=ExpiryMode.NEVER),
        runtime=runtime,
    )
    catalog = ApprovalAttentionClassCatalog([
        ApprovalAttentionClass(
            "test_app", "approval.test_kind.requested", "approval.test_kind.review",
        ),
    ])
    approval_registry = ApprovalKindRegistry(
        [registration], catalog, consumer_ids={"test.consumer"},
    )
    publication = AttentionPublicationRuntime(
        projection_planner=lambda _source: None,
        source_evidence_builder=lambda _id, _version: None,
        publication_enabled=True,
    ) if attention_active else None
    attention_registration = AttentionClassRegistration(
        kind=KIND,
        application_scope="test_app",
        producer_id=None,
        notification_class="approval.test_kind.requested",
        surface_category="approvals",
        review_renderer_id="approval.test_kind.review",
        policy=AttentionClassPolicy.approval_phase_one(),
        approval_runtime_enabled=approval_active,
        runtime=publication,
    )
    attention_registry = AttentionRegistry([
        AttentionApplicationRegistration(
            application_scope="test_app",
            label="Test",
            icon_ref="attention.application.test",
            open_mode="registered_renderer",
            classes=(attention_registration,),
        ),
    ])
    results = {}
    http_registry = ApprovalHttpRegistry(
        approvals=approval_registry,
        attention=attention_registry,
        adapters={KIND: _adapter(results)} if http_active else {},
    )
    wait_hub = ApprovalWaitHub()
    service = ApprovalService(
        registry=approval_registry,
        store=store or InMemoryApprovalStore(),
        personal_root_resolver=lambda: "a" * 64,
        session_label_resolver=lambda subject: f"Session {subject}",
        clock=clock or (lambda: 1000.0),
        after_commit=wait_hub.notify,
    )
    bridge = ApprovalHttpBridge(
        approvals=service,
        registry=http_registry,
        wait_hub=wait_hub,
    )
    return bridge, service, results


def _human():
    return HumanApprovalActor._verified("a" * 64)


def _deep_object():
    value = {"leaf": True}
    for _ in range(MAX_PROJECTED_JSON_DEPTH + 2):
        value = {"child": value}
    return value


def _oversized_object():
    return {"value": "x" * (MAX_PROJECTED_JSON_BYTES + 1)}


def _bridge_with_adapter(bridge, service, adapter):
    return ApprovalHttpBridge(
        approvals=service,
        registry=ApprovalHttpRegistry(
            approvals=bridge.registry.approvals,
            attention=bridge.registry.attention,
            adapters={KIND: adapter},
        ),
    )


def _app_for_bridge(monkeypatch, bridge, *, principal=None):
    selected = principal or _principal(subject="same")

    async def stamp_principal(request, call_next):
        request.state.api_principal = selected
        return await call_next(request)

    app = Starlette(routes=approvals_routes.ROUTES)
    app.add_middleware(BaseHTTPMiddleware, dispatch=stamp_principal)
    monkeypatch.setattr(approvals_routes, "_approval_http_bridge", lambda: bridge)
    monkeypatch.setattr(attention_routes, "operator_mutation_guard", lambda _request: None)
    monkeypatch.setattr(
        approvals_routes, "resolve_human_approval_actor", lambda _request: _human(),
    )
    return app


PRODUCTION_KIND_INVENTORY = {
    "commit_sign": ("session_principal", "approved + signature", ".8", False),
    "jira_write": (
        "session_principal", "approved + terminal execution", ".9", False,
    ),
    "link_publish": (
        "session_principal", "approved + terminal execution.url", ".10", True,
    ),
    "link_revoke": (
        "session_principal", "approved + terminal execution", ".10", False,
    ),
    "dashboard_access": (
        "session_principal", "approved + grant.nonce", ".11", False,
    ),
    "visitor_token": (
        "session_principal", "sealed one-use delivery", ".12", False,
    ),
    "secure_setting": ("session_principal", "terminal execution", ".13", False),
    "mcp_peer_link": (
        "registered_service", "terminal application result", ".14", False,
    ),
    "mcp_crosstalk": (
        "registered_service", "terminal application result", ".14", False,
    ),
    "fleet_machine_admission": (
        "internal_producer", "application-local lifecycle correlation", ".25", False,
    ),
    "external_service_access": (
        "internal_producer", "held application result", ".26", False,
    ),
    "vault_open": (
        "session_principal", "terminal execution.receipt", ".27", False,
    ),
}


def test_production_composition_shares_one_service_and_activates_no_http_kind():
    runtime = attention_routes.approval_runtime()
    assert runtime.approval_http is not None
    assert runtime.approval_http.approvals is runtime.approvals
    assert runtime.approval_http.registry.approvals is runtime.approvals.registry
    assert runtime.approval_http.registry.attention is runtime.index.registry
    assert runtime.approval_http.registry.adapters == {}
    assert set(PRODUCTION_KIND_INVENTORY) == set(PRODUCTION_APPROVAL_REGISTRY.kinds)
    for kind, registration in PRODUCTION_APPROVAL_REGISTRY.kinds.items():
        assert not runtime.approval_http.claims_kind(kind)
        assert not runtime.approval_http.migrated_kind(kind)
        assert registration.runtime is None


def test_exact_twelve_kind_inventory_freezes_requester_result_and_owner():
    assert set(PRODUCTION_KIND_INVENTORY) == set(PRODUCTION_APPROVAL_REGISTRY.kinds)
    exceptions = []
    for kind, (requester, result_dependency, migration_owner, fleet_cookie_legacy) in (
        PRODUCTION_KIND_INVENTORY.items()
    ):
        registration = PRODUCTION_APPROVAL_REGISTRY.kinds[kind]
        assert registration.requester_policy.value == requester
        assert result_dependency
        assert migration_owner in {
            ".8", ".9", ".10", ".11", ".12", ".13", ".14", ".25", ".26", ".27",
        }
        if fleet_cookie_legacy:
            exceptions.append(kind)
    assert PRODUCTION_APPROVAL_REGISTRY.kinds[
        "link_publish"
    ].requester_policy is RequesterPolicy.SESSION_PRINCIPAL
    assert exceptions == ["link_publish"]


@pytest.mark.parametrize("approval_active,attention_active", [
    (False, True),
    (True, False),
])
def test_claimed_partial_runtime_fails_closed_instead_of_falling_back(
    approval_active, attention_active,
):
    bridge, _service, _results = _build(
        approval_active=approval_active,
        attention_active=attention_active,
    )
    assert bridge.claims_kind(KIND)
    assert not bridge.migrated_kind(KIND)
    with pytest.raises(ApprovalHttpBridgeError, match="kind_disabled"):
        bridge.create(KIND, _principal(), {"operation": "test"})


def test_runtime_activation_without_public_adapter_never_falls_back_to_legacy():
    bridge, _service, _results = _build(http_active=False)
    assert bridge.claims_kind(KIND)
    assert not bridge.migrated_kind(KIND)
    with pytest.raises(ApprovalHttpBridgeError, match="kind_disabled"):
        bridge.create(KIND, _principal(), {"operation": "test"})


def test_reserved_ids_and_stable_derivation_are_deterministic():
    bridge, _service, _results = _build()
    created = bridge.create(KIND, _principal(), {"operation": "test"})
    assert has_central_approval_prefix(created)
    assert is_central_approval_id(created)
    first = central_stable_approval_id("fleet", "source-request-1")
    assert first == central_stable_approval_id("fleet", "source-request-1")
    assert first != central_stable_approval_id("fleet", "source-request-2")
    assert is_central_approval_id(first)
    assert not has_central_approval_prefix("fleet-" + "0" * 64)
    assert not is_central_approval_id("central-" + "a" * 21)
    with pytest.raises(ApprovalHttpBridgeError, match="invalid_request"):
        central_stable_approval_id("n" * 65, "source-request-1")
    with pytest.raises(ValueError, match="reserved central approval id"):
        asyncio.run(approvals_routes.open_approval(
            kind="legacy_kind",
            session="legacy-session",
            request_payload={"operation": "test"},
            request_id="central-" + "a" * 32,
        ))


def test_requester_envelope_and_authorization_precede_disclosure():
    bridge, _service, _results = _build()
    owner = _principal(subject="same", org="org-a")
    approval_id = bridge.create(KIND, owner, {"operation": "test"})
    assert bridge.envelope(approval_id, owner) == {
        "id": approval_id,
        "kind": KIND,
        "session": "Session same",
        "request": {"operation": "test"},
        "result": None,
    }
    for wrong in (
        _principal(subject="same", org="org-b"),
        _principal(subject="same"),
        api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.OPERATOR_COOKIE, subject="sid"),
        api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.MCP_SERVICE, subject="relay"),
        api_auth.COMPATIBILITY_PRINCIPAL,
    ):
        with pytest.raises(ApprovalHttpBridgeError, match="not_found"):
            bridge.envelope(approval_id, wrong)


@pytest.mark.parametrize("requester_policy", [
    RequesterPolicy.REGISTERED_SERVICE,
    RequesterPolicy.INTERNAL_PRODUCER,
])
def test_non_session_requesters_are_refused_by_generic_http_but_trusted_path_works(
    monkeypatch, requester_policy,
):
    bridge, service, _results = _build(
        requester_policy=requester_policy,
        http_active=False,
    )
    assert bridge.claims_kind(KIND)
    assert not bridge.migrated_kind(KIND)
    with pytest.raises(ApprovalHttpBridgeError, match="kind_disabled"):
        bridge.create(KIND, _principal(), {"operation": "public"})

    if requester_policy is RequesterPolicy.REGISTERED_SERVICE:
        trusted = api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.MCP_SERVICE, subject="relay.service",
        )
        row = service.create_from_principal(KIND, trusted, {"operation": "trusted"})
        cancel_trusted = lambda: service.cancel_from_principal(row.approval_id, trusted)
        public_principal = trusted
    else:
        trusted = RegisteredApprovalProducer(
            producer_id="fleet.enrollment",
            label="Fleet enrollment",
            allowed_kinds=frozenset({KIND}),
            stable_source_ids=True,
        )
        row = service.create_from_producer(
            KIND,
            trusted,
            {"operation": "trusted"},
            source_approval_id=central_stable_approval_id(
                "fleet", "source-request-1",
            ),
        )
        cancel_trusted = lambda: service.cancel_from_producer(row.approval_id, trusted)
        public_principal = _principal()

    assert service.status(row.approval_id).state == "open"
    with pytest.raises(ApprovalHttpBridgeError, match="not_found"):
        bridge.envelope(row.approval_id, public_principal)
    with pytest.raises(ApprovalHttpBridgeError, match="not_found"):
        asyncio.run(bridge.held_envelope(row.approval_id, public_principal, 0))
    with pytest.raises(ApprovalHttpBridgeError, match="not_found"):
        bridge.cancel(row.approval_id, public_principal)

    app = _app_for_bridge(monkeypatch, bridge, principal=public_principal)
    with TestClient(app, base_url="https://localhost:8080") as client:
        create = client.post("/api/approvals", json={
            "kind": KIND, "request": {"operation": "public"},
        })
        assert create.status_code == 400
        assert create.json() == {"error": "kind_disabled"}
        assert create.headers["cache-control"] == "no-store"
        for path in (
            f"/api/approvals/{row.approval_id}",
            f"/api/approvals/{row.approval_id}?wait=0.01",
        ):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json() == {"error": "not found"}
            assert response.headers["cache-control"] == "no-store"
        cancel = client.post(f"/api/approvals/{row.approval_id}/cancel", json={})
        assert cancel.status_code == 404
        assert cancel.json() == {"error": "not found"}
        assert cancel.headers["cache-control"] == "no-store"
    assert cancel_trusted().payload["outcome"] == "canceled"


def test_held_wait_rechecks_direct_application_result_without_wake():
    bridge, service, results = _build()
    principal = _principal()
    approval_id = bridge.create(KIND, principal, {"operation": "test"})
    service.decide(
        approval_id, _human(), outcome="granted", decision={"reason": "ok"},
    )

    async def scenario():
        started = time.monotonic()
        held = asyncio.create_task(
            bridge.held_envelope(approval_id, principal, 5),
        )
        await asyncio.sleep(0.1)
        results[approval_id] = {
            "approved": True,
            "execution": {"ok": True, "value": "ready"},
        }
        answer = await held
        assert answer["result"] == results[approval_id]
        assert time.monotonic() - started < 1.6
        assert bridge.wait_hub.waiter_count == 0

    asyncio.run(scenario())


def test_held_wait_callback_before_register_and_callback_after_read_converge():
    bridge, service, _results = _build()
    principal = _principal()

    before_id = bridge.create(KIND, principal, {"operation": "before"})
    service.decide(before_id, _human(), outcome="declined", decision={})
    assert asyncio.run(bridge.held_envelope(before_id, principal, 5))["result"] == {
        "approved": False, "outcome": "declined",
    }

    after_id = bridge.create(KIND, principal, {"operation": "after"})
    original_wait = bridge.wait_hub.wait
    called = False

    async def lose_callback_race(approval_id, timeout):
        nonlocal called
        if not called:
            called = True
            service.decide(after_id, _human(), outcome="declined", decision={})
        return await original_wait(approval_id, min(timeout, 0.01))

    bridge.wait_hub.wait = lose_callback_race
    assert asyncio.run(bridge.held_envelope(after_id, principal, 5))["result"] == {
        "approved": False, "outcome": "declined",
    }
    assert called
    assert bridge.wait_hub.waiter_count == 0


def test_held_wait_recovers_direct_settings_arrival_timeout_and_reopen():
    store = InMemoryApprovalStore()
    bridge, service, _results = _build(store=store)
    principal = _principal()
    direct_id = bridge.create(KIND, principal, {"operation": "direct"})

    async def direct_arrival():
        held = asyncio.create_task(bridge.held_envelope(direct_id, principal, 5))
        await asyncio.sleep(0.05)
        store.append_resolution(direct_id, {
            "outcome": "declined",
            "decider_ref": "a" * 64,
            "resolved_at": 1000.0,
            "decision": {},
        })
        started = time.monotonic()
        answer = await held
        assert answer["result"] == {"approved": False, "outcome": "declined"}
        assert time.monotonic() - started < 1.2

    asyncio.run(direct_arrival())

    timeout_id = bridge.create(KIND, principal, {"operation": "timeout"})
    timed = asyncio.run(bridge.held_envelope(timeout_id, principal, 0.02))
    assert timed["result"] is None
    assert bridge.wait_hub.waiter_count == 0

    reopened, reopened_service, _ = _build(store=store)
    assert reopened_service is not service
    assert reopened.envelope(direct_id, principal)["result"] == {
        "approved": False, "outcome": "declined",
    }


def test_waiter_bookkeeping_is_bounded_and_stop_releases_all_waiters():
    bridge, _service, _results = _build()
    principal = _principal()
    approval_id = bridge.create(KIND, principal, {"operation": "many"})

    async def scenario():
        held = [
            asyncio.create_task(bridge.held_envelope(approval_id, principal, 5))
            for _ in range(16)
        ]
        for _ in range(50):
            if bridge.wait_hub.waiter_count == len(held):
                break
            await asyncio.sleep(0.01)
        assert bridge.wait_hub.waiter_count == len(held)
        bridge.close()
        answers = await asyncio.gather(*held, return_exceptions=True)
        assert all(
            isinstance(answer, ApprovalHttpBridgeError)
            and answer.code == "unavailable"
            for answer in answers
        )
        assert bridge.wait_hub.waiter_count == 0

    asyncio.run(scenario())


def test_bridge_close_releases_held_wait_without_claiming_a_result():
    bridge, _service, _results = _build()
    principal = _principal()
    approval_id = bridge.create(KIND, principal, {"operation": "test"})

    async def scenario():
        held = asyncio.create_task(bridge.held_envelope(approval_id, principal, 5))
        for _ in range(20):
            if bridge.wait_hub.waiter_count:
                break
            await asyncio.sleep(0.01)
        assert bridge.wait_hub.waiter_count == 1
        bridge.close()
        with pytest.raises(ApprovalHttpBridgeError, match="unavailable"):
            await asyncio.wait_for(held, 0.5)
        assert bridge.wait_hub.waiter_count == 0

    asyncio.run(scenario())


def test_cancel_is_exact_session_only_and_conflicts_with_other_truth():
    bridge, service, _results = _build()
    owner = _principal(subject="same", org="org-a")
    approval_id = bridge.create(KIND, owner, {"operation": "cancel"})
    canceled = bridge.cancel(approval_id, owner)
    assert canceled["result"] == {"approved": False, "outcome": "canceled"}
    assert bridge.cancel(approval_id, owner) == canceled
    with pytest.raises(ApprovalHttpBridgeError, match="not_found"):
        bridge.cancel(approval_id, _principal(subject="same", org="org-b"))

    granted_id = bridge.create(KIND, owner, {"operation": "grant"})
    service.decide(granted_id, _human(), outcome="granted", decision={})
    with pytest.raises(ApprovalHttpBridgeError, match="approval_conflict"):
        bridge.cancel(granted_id, owner)


def test_shared_terminal_envelopes_are_explicit_and_grant_waits_for_output():
    bridge, service, _results = _build()
    principal = _principal()

    declined_id = bridge.create(KIND, principal, {"operation": "decline"})
    service.decide(declined_id, _human(), outcome="declined", decision={})
    assert bridge.envelope(declined_id, principal)["result"] == {
        "approved": False, "outcome": "declined",
    }

    granted_id = bridge.create(KIND, principal, {"operation": "grant"})
    service.decide(granted_id, _human(), outcome="granted", decision={})
    assert bridge.envelope(granted_id, principal)["result"] is None

    expired, expired_service, _ = _build(
        expiry=ApprovalExpiryPolicy(mode=ExpiryMode.FIXED, fixed_seconds=1),
    )
    expired_id = expired.create(KIND, principal, {"operation": "expire"})
    expired_service.reconcile_expiry(expired_id, now=1001.0)
    assert expired.envelope(expired_id, principal)["result"] == {
        "approved": False, "outcome": "expired",
    }


def test_legacy_decision_exact_replay_and_terminal_conflicts():
    bridge, service, _results = _build()
    principal = _principal()
    approval_id = bridge.create(KIND, principal, {"operation": "answer"})
    body = {"approved": True, "reason": "ok"}
    bridge.decide_legacy(approval_id, _human(), body)
    bridge.decide_legacy(approval_id, _human(), body)
    for opposite in (
        {"approved": False},
        {"approved": True, "reason": "different"},
    ):
        with pytest.raises(ApprovalHttpBridgeError, match="approval_conflict"):
            bridge.decide_legacy(approval_id, _human(), opposite)

    canceled_id = bridge.create(KIND, principal, {"operation": "cancel"})
    service.cancel_from_principal(canceled_id, principal)
    with pytest.raises(ApprovalHttpBridgeError, match="approval_conflict"):
        bridge.decide_legacy(canceled_id, _human(), {"approved": True})

    expiring, _expiring_service, _ = _build(
        expiry=ApprovalExpiryPolicy(mode=ExpiryMode.FIXED, fixed_seconds=1),
    )
    expired_id = expiring.create(KIND, principal, {"operation": "expire"})
    expiring.approvals.reconcile_expiry(expired_id, now=1001.0)
    with pytest.raises(ApprovalHttpBridgeError, match="approval_conflict"):
        expiring.decide_legacy(expired_id, _human(), {"approved": True})

    malformed_id = bridge.create(KIND, principal, {"operation": "malformed"})
    with pytest.raises(ApprovalHttpBridgeError, match="invalid_decision"):
        bridge.decide_legacy(
            malformed_id, _human(), {"approved": True, "forged": "field"},
        )


def test_opposing_settings_decisions_return_one_answer_and_one_conflict():
    bridge, service, _results = _build()
    approval_id = bridge.create(KIND, _principal(), {"operation": "race"})
    bodies = ({"approved": True}, {"approved": False})

    def decide(body):
        try:
            bridge.decide_legacy(approval_id, _human(), body)
            return "accepted"
        except ApprovalHttpBridgeError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(decide, bodies))
    assert sorted(outcomes) == ["accepted", "approval_conflict"]
    canonical = service.status(approval_id).resolution
    assert canonical is not None
    assert canonical.payload["outcome"] in {"granted", "declined"}
    assert service.store.resolution_count(approval_id) == 1


def test_adapter_failures_are_bounded_without_disclosing_or_inventing_results():
    bridge, service, _results = _build()
    principal = _principal()
    approval_id = bridge.create(KIND, principal, {"operation": "test"})

    def broken_request(_payload):
        raise RuntimeError("request store unavailable")

    broken_request_bridge = ApprovalHttpBridge(
        approvals=service,
        registry=ApprovalHttpRegistry(
            approvals=bridge.registry.approvals,
            attention=bridge.registry.attention,
            adapters={KIND: ApprovalHttpKindAdapter(
                kind=KIND,
                request_projector=broken_request,
                result_projector=lambda _status: None,
                legacy_decision_mapper=_adapter({}).legacy_decision_mapper,
            )},
        ),
    )
    with pytest.raises(ApprovalHttpBridgeError, match="unavailable"):
        broken_request_bridge.envelope(approval_id, principal)

    service.decide(approval_id, _human(), outcome="granted", decision={})

    def broken_result(_status):
        raise RuntimeError("result store unavailable")

    broken_result_bridge = ApprovalHttpBridge(
        approvals=service,
        registry=ApprovalHttpRegistry(
            approvals=bridge.registry.approvals,
            attention=bridge.registry.attention,
            adapters={KIND: ApprovalHttpKindAdapter(
                kind=KIND,
                request_projector=lambda payload: payload["request"],
                result_projector=broken_result,
                legacy_decision_mapper=_adapter({}).legacy_decision_mapper,
            )},
        ),
    )
    with pytest.raises(ApprovalHttpBridgeError, match="unavailable"):
        broken_result_bridge.envelope(approval_id, principal)

    malformed_result_bridge = ApprovalHttpBridge(
        approvals=service,
        registry=ApprovalHttpRegistry(
            approvals=bridge.registry.approvals,
            attention=bridge.registry.attention,
            adapters={KIND: ApprovalHttpKindAdapter(
                kind=KIND,
                request_projector=lambda payload: payload["request"],
                result_projector=lambda _status: {
                    "approved": True, "value": float("nan"),
                },
                legacy_decision_mapper=_adapter({}).legacy_decision_mapper,
            )},
        ),
    )
    with pytest.raises(ApprovalHttpBridgeError, match="unavailable"):
        malformed_result_bridge.envelope(approval_id, principal)


@pytest.mark.parametrize("surface,shape_factory,status_code,error", [
    ("request", _deep_object, 503, "unavailable"),
    ("request", _oversized_object, 503, "unavailable"),
    ("result", _deep_object, 503, "unavailable"),
    ("result", _oversized_object, 503, "unavailable"),
    ("decision", _deep_object, 422, "invalid_decision"),
    ("decision", _oversized_object, 422, "invalid_decision"),
])
def test_bounded_adapter_output_fails_closed_at_route_boundary(
    monkeypatch, surface, shape_factory, status_code, error,
):
    bridge, service, _results = _build()
    principal = _principal()
    approval_id = bridge.create(KIND, principal, {"operation": surface})
    if surface == "result":
        service.decide(approval_id, _human(), outcome="granted", decision={})

    adapter = ApprovalHttpKindAdapter(
        kind=KIND,
        request_projector=(
            (lambda _payload: shape_factory())
            if surface == "request" else (lambda payload: payload["request"])
        ),
        result_projector=(
            (lambda _status: shape_factory())
            if surface == "result" else (lambda _status: None)
        ),
        legacy_decision_mapper=(
            (lambda _body: CanonicalLegacyDecision("granted", shape_factory()))
            if surface == "decision"
            else _adapter({}).legacy_decision_mapper
        ),
    )
    bounded = _bridge_with_adapter(bridge, service, adapter)
    app = _app_for_bridge(monkeypatch, bounded, principal=principal)
    with TestClient(app, base_url="https://localhost:8080") as client:
        response = (
            client.post(
                f"/api/approvals/{approval_id}/decision",
                json={"approved": True},
            )
            if surface == "decision"
            else client.get(f"/api/approvals/{approval_id}")
        )
    assert response.status_code == status_code
    assert response.json() == {"error": error}
    assert response.headers["cache-control"] == "no-store"
    if surface == "decision":
        assert service.status(approval_id).resolution is None


def test_central_routes_use_bridge_without_legacy_events_or_store(
    monkeypatch,
):
    bridge, _service, results = _build()

    async def stamp_principal(request, call_next):
        if request.headers.get("x-test-principal") == "compatibility":
            request.state.api_principal = api_auth.COMPATIBILITY_PRINCIPAL
        else:
            org = request.headers.get("x-test-org")
            request.state.api_principal = _principal(subject="same", org=org)
        return await call_next(request)

    app = Starlette(routes=approvals_routes.ROUTES)
    app.add_middleware(BaseHTTPMiddleware, dispatch=stamp_principal)
    monkeypatch.setattr(approvals_routes, "_approval_http_bridge", lambda: bridge)
    monkeypatch.setattr(
        approvals_routes,
        "_create_approval_legacy",
        lambda _request: (_ for _ in ()).throw(AssertionError("legacy create called")),
    )
    monkeypatch.setattr(
        approvals_routes.event_bus,
        "broadcast_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("global approval event emitted")
        ),
    )

    async def reject_legacy_push(*_args, **_kwargs):
        raise AssertionError("legacy Web Push adapter called")

    monkeypatch.setattr(
        approvals_routes.web_push, "register_approval_pending", reject_legacy_push,
    )
    monkeypatch.setattr(attention_routes, "operator_mutation_guard", lambda _request: None)
    monkeypatch.setattr(approvals_routes, "resolve_human_approval_actor", lambda _request: _human())

    with TestClient(app, base_url="https://localhost:8080") as client:
        unauthenticated = client.post(
            "/api/approvals",
            headers={"x-test-principal": "compatibility"},
            json={"kind": KIND, "request": {"operation": "test"}},
        )
        assert unauthenticated.status_code == 401
        assert unauthenticated.json() == {"error": "authentication required"}
        invalid = client.post("/api/approvals", json={
            "kind": KIND,
            "session": "forged",
            "request": {"operation": "test"},
        })
        assert invalid.status_code == 400
        created = client.post("/api/approvals", json={
            "kind": KIND,
            "request": {"operation": "test"},
        })
        assert created.status_code == 200
        assert created.headers["cache-control"] == "no-store"
        approval_id = created.json()["id"]

        wrong = client.get(
            f"/api/approvals/{approval_id}", headers={"x-test-org": "org-b"},
        )
        assert wrong.status_code == 404
        assert wrong.json() == {"error": "not found"}
        opened = client.get(f"/api/approvals/{approval_id}?wait=0")
        assert opened.status_code == 200
        assert opened.json()["result"] is None

        decided = client.post(
            f"/api/approvals/{approval_id}/decision",
            json={"approved": True, "reason": "ok"},
        )
        assert decided.json() == {"ok": True}
        results[approval_id] = {"approved": True, "execution": {"ok": True}}
        delivered = client.get(f"/api/approvals/{approval_id}?wait=2")
        assert delivered.json()["result"] == results[approval_id]
        conflict = client.post(
            f"/api/approvals/{approval_id}/decision",
            json={"approved": False},
        )
        assert conflict.status_code == 409
        assert conflict.json() == {
            "ok": False,
            "error": "approval has a different terminal outcome",
        }

        cancel_id = client.post("/api/approvals", json={
            "kind": KIND,
            "request": {"operation": "cancel"},
        }).json()["id"]
        canceled = client.post(f"/api/approvals/{cancel_id}/cancel", json={})
        assert canceled.status_code == 200
        assert canceled.json()["result"] == {
            "approved": False, "outcome": "canceled",
        }


def test_prefix_dispatch_never_cross_probes_legacy_and_settings_stores(monkeypatch):
    bridge, _service, _results = _build()
    legacy_calls = []

    async def legacy_get(request):
        legacy_calls.append(request.path_params["id"])
        return JSONResponse({"store": "legacy", "id": request.path_params["id"]})

    monkeypatch.setattr(approvals_routes, "_get_approval_legacy", legacy_get)
    app = _app_for_bridge(monkeypatch, bridge, principal=_principal())
    with TestClient(app, base_url="https://localhost:8080") as client:
        unknown_central = client.get("/api/approvals/central-" + "z" * 32)
        assert unknown_central.status_code == 404
        assert unknown_central.json() == {"error": "not found"}
        assert unknown_central.headers["cache-control"] == "no-store"
        assert legacy_calls == []

        legacy = client.get("/api/approvals/legacy-request-1")
        assert legacy.status_code == 200
        assert legacy.json() == {"store": "legacy", "id": "legacy-request-1"}
        assert legacy_calls == ["legacy-request-1"]

        malformed_reserved = client.get("/api/approvals/central-too-short")
        assert malformed_reserved.status_code == 404
        assert malformed_reserved.json() == {"error": "not found"}
        assert legacy_calls == ["legacy-request-1"]


def test_mixed_concurrent_legacy_and_settings_reads_never_cross_stores(monkeypatch):
    bridge, _service, _results = _build()
    principal = _principal()
    central_id = bridge.create(KIND, principal, {"operation": "mixed"})
    legacy_calls = []

    async def legacy_get(request):
        legacy_calls.append(request.path_params["id"])
        return JSONResponse({"store": "legacy", "id": request.path_params["id"]})

    monkeypatch.setattr(approvals_routes, "_get_approval_legacy", legacy_get)
    app = _app_for_bridge(monkeypatch, bridge, principal=principal)
    paths = [
        f"/api/approvals/{central_id}" if index % 2 == 0
        else f"/api/approvals/legacy-{index}"
        for index in range(12)
    ]
    with TestClient(app, base_url="https://localhost:8080") as client:
        with ThreadPoolExecutor(max_workers=6) as executor:
            responses = list(executor.map(client.get, paths))
    for path, response in zip(paths, responses, strict=True):
        if path.endswith(central_id):
            assert response.status_code == 200
            assert response.json()["id"] == central_id
            assert response.json()["result"] is None
        else:
            assert response.status_code == 200
            assert response.json()["store"] == "legacy"
    assert sorted(legacy_calls) == sorted(
        path.rsplit("/", 1)[-1] for path in paths if "legacy-" in path
    )


def test_service_failure_does_not_turn_into_cross_store_fallback():
    bridge, service, _results = _build()
    approval_id = bridge.create(KIND, _principal(), {"operation": "test"})

    def broken(_approval_id):
        raise RuntimeError("store unavailable")

    service.store.get_request = broken
    with pytest.raises(ApprovalHttpBridgeError, match="unavailable"):
        bridge.envelope(approval_id, _principal())
    assert has_central_approval_prefix(approval_id)
