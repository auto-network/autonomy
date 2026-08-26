"""Contract tests for the Settings-native central approval authority.

The public approval routes deliberately remain on the existing rendezvous in
this bead.  These tests exercise the new internal service directly.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tools.dashboard import api_auth
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
    RegisteredApprovalProducer,
    RequesterPolicy,
    build_production_registry,
)
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    HumanApprovalActor,
    InMemoryApprovalStore,
    SettingsApprovalStore,
    canonical_session_requester_id,
    normalize_unix_milliseconds_deadline,
    resolve_human_approval_actor,
    resolve_personal_root_public_key,
)
from tools.graph.schemas.central_attention import (
    APPROVAL_REQUEST_SET_ID,
    APPROVAL_RESOLUTION_SET_ID,
)


EXPECTED_KINDS = {
    "commit_sign",
    "jira_write",
    "link_publish",
    "link_revoke",
    "dashboard_access",
    "visitor_token",
    "secure_setting",
    "mcp_peer_link",
    "mcp_crosstalk",
    "fleet_machine_admission",
    "external_service_access",
    "vault_open",
}


def test_production_catalog_is_exact_closed_and_disabled():
    registry = build_production_registry()
    assert set(registry.kinds) == EXPECTED_KINDS
    assert "demo_ack" not in registry.kinds
    assert all(item.runtime is None for item in registry.kinds.values())
    assert registry.kinds["dashboard_access"].application_scope_policy.fixed == "sessions"
    assert registry.kinds["mcp_peer_link"].application_scope_policy.fixed == "relay"
    assert registry.kinds["vault_open"].request_expiry_policy == ApprovalExpiryPolicy(
        mode=ExpiryMode.BOUNDED, minimum_seconds=1, maximum_seconds=300,
        default_seconds=60,
    )
    external = registry.kinds["external_service_access"]
    assert external.application_scope_policy.by_producer == {
        "external_service.dropbox_enrollment": "dropbox"
    }


def test_production_catalog_field_map_is_exact():
    registry = build_production_registry()
    expected = {
        "commit_sign": ("worktrees", "session_principal", "organization_signing_key", "never"),
        "jira_write": ("jira", "session_principal", "operator_session", "never"),
        "link_publish": ("links", "session_principal", "organization_signing_key", "trusted_source_deadline"),
        "link_revoke": ("links", "session_principal", "organization_signing_key", "never"),
        "dashboard_access": ("sessions", "session_principal", "personal_root", "fixed"),
        "visitor_token": ("mission_control", "session_principal", "operator_session", "never"),
        "secure_setting": ("vault", "session_principal", "operator_session", "never"),
        "mcp_peer_link": ("relay", "registered_service", "operator_session", "never"),
        "mcp_crosstalk": ("relay", "registered_service", "operator_session", "never"),
        "fleet_machine_admission": ("fleet", "internal_producer", "personal_root", "trusted_source_deadline"),
        "external_service_access": ("dropbox", "internal_producer", "operator_session", "never"),
        "vault_open": ("vault", "session_principal", "vault_policy", "bounded"),
    }
    actual = {}
    for kind, item in registry.kinds.items():
        assert item.notification_class == f"approval.{kind}.requested"
        assert item.renderer_id == f"approval.{kind}.review"
        actual[kind] = (
            next(iter(item.application_scope_policy.applications)),
            item.requester_policy.value,
            item.authority_requirement.value,
            item.request_expiry_policy.mode.value,
        )
    assert actual == expected


def _runtime(*, planner=None, validator=None, consumer="test.consumer"):
    return ApprovalKindRuntime(
        request_planner=planner or (lambda context, body: body),
        decision_validator=validator or (lambda request, decision, grant: decision),
        resolution_consumer_id=consumer,
    )


def _registration(*, runtime=None, expiry=None):
    return ApprovalKindRegistration(
        kind="test_kind",
        application_scope_policy=ApplicationScopePolicy(fixed="test_app"),
        notification_class="approval.test_kind.requested",
        renderer_id="approval.test_kind.review",
        requester_policy=RequesterPolicy.SESSION_PRINCIPAL,
        decider_policy=DeciderPolicy.PERSONAL_OPERATOR,
        authority_requirement=AuthorityRequirement.OPERATOR_SESSION,
        request_expiry_policy=expiry or ApprovalExpiryPolicy(mode=ExpiryMode.NEVER),
        runtime=runtime,
    )


def _catalog():
    return ApprovalAttentionClassCatalog([
        ApprovalAttentionClass(
            application_scope="test_app",
            notification_class="approval.test_kind.requested",
            renderer_id="approval.test_kind.review",
        )
    ])


def test_registry_rejects_duplicate_partial_or_unknown_registration():
    item = _registration()
    with pytest.raises(ValueError, match="duplicate approval kind"):
        ApprovalKindRegistry([item, item], _catalog(), consumer_ids={"test.consumer"})
    with pytest.raises(ValueError, match="complete runtime"):
        ApprovalKindRuntime(
            request_planner=lambda context, body: body,
            decision_validator=None,
            resolution_consumer_id="test.consumer",
        )
    with pytest.raises(ValueError, match="consumer"):
        ApprovalKindRegistry(
            [_registration(runtime=_runtime(consumer="missing"))],
            _catalog(),
            consumer_ids={"test.consumer"},
        )
    with pytest.raises(ValueError, match="attention class"):
        ApprovalKindRegistry([item], ApprovalAttentionClassCatalog([]), consumer_ids=set())
    mismatched = ApprovalAttentionClassCatalog([
        ApprovalAttentionClass(
            "test_app", "approval.test_kind.requested", "approval.other.review",
        ),
    ])
    with pytest.raises(ValueError, match="renderer mismatch"):
        ApprovalKindRegistry([item], mismatched, consumer_ids=set())
    with pytest.raises(ValueError, match="unknown requester policy"):
        ApprovalKindRegistration(
            kind=item.kind,
            application_scope_policy=item.application_scope_policy,
            notification_class=item.notification_class,
            renderer_id=item.renderer_id,
            requester_policy="caller-selected",
            decider_policy=item.decider_policy,
            authority_requirement=item.authority_requirement,
            request_expiry_policy=item.request_expiry_policy,
        )
    with pytest.raises(ValueError, match="runtime must be complete"):
        ApprovalKindRegistration(
            kind=item.kind,
            application_scope_policy=item.application_scope_policy,
            notification_class=item.notification_class,
            renderer_id=item.renderer_id,
            requester_policy=item.requester_policy,
            decider_policy=item.decider_policy,
            authority_requirement=item.authority_requirement,
            request_expiry_policy=item.request_expiry_policy,
            runtime="partial",
        )
    with pytest.raises(ValueError, match="unknown approval expiry mode"):
        ApprovalExpiryPolicy(mode="sometimes")
    with pytest.raises(ValueError, match="fixed expiry"):
        ApprovalExpiryPolicy(mode=ExpiryMode.FIXED)
    with pytest.raises(ValueError, match="bounded expiry"):
        ApprovalExpiryPolicy(
            mode=ExpiryMode.BOUNDED, minimum_seconds=1, maximum_seconds=5,
        )
    assert set(build_production_registry().kinds) == set(build_production_registry().kinds)


def _plan(_context, body):
    return {
        "subject_ref": body.get("subject_ref", "subject-1"),
        "safe_review": body.get("safe_review", {"summary": "Review test"}),
        "request": body.get("request", {"operation": "test"}),
        "staged": body.get("staged"),
        "requested_expiry_seconds": body.get("requested_expiry_seconds"),
        "trusted_source_expires_at": body.get("trusted_source_expires_at"),
    }


def _validator(_request, decision, grant):
    if not isinstance(decision, dict) or set(decision) - {"reason"}:
        raise ValueError("bad decision")
    return decision


def _service(*, expiry=None, requester=RequesterPolicy.SESSION_PRINCIPAL,
             clock=None, store=None, wake=None, planner=_plan, validator=_validator,
             result_ref_builder=None):
    runtime = ApprovalKindRuntime(
        request_planner=planner,
        decision_validator=validator,
        resolution_consumer_id="test.consumer",
        result_ref_builder=result_ref_builder,
    )
    reg = _registration(runtime=runtime, expiry=expiry)
    if requester is not RequesterPolicy.SESSION_PRINCIPAL:
        reg = ApprovalKindRegistration(
            kind=reg.kind,
            application_scope_policy=reg.application_scope_policy,
            notification_class=reg.notification_class,
            renderer_id=reg.renderer_id,
            requester_policy=requester,
            decider_policy=reg.decider_policy,
            authority_requirement=reg.authority_requirement,
            request_expiry_policy=reg.request_expiry_policy,
            runtime=reg.runtime,
        )
    registry = ApprovalKindRegistry([reg], _catalog(), consumer_ids={"test.consumer"})
    return ApprovalService(
        registry=registry,
        store=store or InMemoryApprovalStore(),
        personal_root_resolver=lambda: "a" * 64,
        session_label_resolver=lambda subject: f"Session {subject}",
        clock=clock or (lambda: 1000.0),
        after_commit=wake,
    )


def _local(subject="auto-1"):
    return api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject=subject)


def _human():
    return HumanApprovalActor._verified(decider_ref="a" * 64)


def test_unknown_disabled_and_forged_outer_fields_fail_before_planning():
    production = ApprovalService(
        registry=build_production_registry(), store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: "a" * 64,
    )
    with pytest.raises(ApprovalServiceError, match="unknown_kind"):
        production.create_from_principal("missing", _local(), {})
    with pytest.raises(ApprovalServiceError, match="kind_disabled"):
        production.create_from_principal("commit_sign", _local(), {})
    service = _service()
    for selector in (
        "approval_id", "kind", "session", "requester_ref", "decider", "audience",
        "application_scope", "organization", "org", "source_identity",
        "producer_id", "source_approval_id",
    ):
        with pytest.raises(ApprovalServiceError, match="invalid_request"):
            service.create_from_principal("test_kind", _local(), {selector: "forged"})


@pytest.mark.parametrize("kind", [
    api_auth.ApiPrincipalKind.OPERATOR_COOKIE,
    api_auth.ApiPrincipalKind.MCP_SERVICE,
    api_auth.ApiPrincipalKind.EXTERNAL_SERVICE,
    api_auth.ApiPrincipalKind.COMPATIBILITY,
])
def test_session_requester_policy_rejects_other_principals(kind):
    with pytest.raises(ApprovalServiceError, match="unauthenticated"):
        _service().create_from_principal(
            "test_kind", api_auth.ApiPrincipal(kind, subject="caller"), {},
        )


def test_create_read_grant_and_reopen_from_shared_store():
    store = InMemoryApprovalStore()
    wakes = []
    service = _service(store=store, wake=lambda record_type, approval_id: wakes.append(
        (record_type, approval_id)
    ))
    created = service.create_from_principal("test_kind", _local(), {})
    assert created.approval_id.startswith("central-")
    assert "approval_id" not in created.payload
    assert created.payload["requester_ref"] == {
        "kind": "session",
        "id": canonical_session_requester_id(_local()),
        "label": "Session auto-1",
    }
    assert service.status(created.approval_id).state == "open"
    reopened = _service(store=store)
    assert reopened.get_request(created.approval_id) == created
    resolution = reopened.decide(
        created.approval_id, _human(), outcome="granted", decision={"reason": "ok"},
    )
    assert resolution.payload["outcome"] == "granted"
    assert reopened.status(created.approval_id).state == "resolved"
    assert wakes == [("request", created.approval_id)]
    created.payload["kind"] = "tampered-return-value"
    assert store.get_request(created.approval_id).payload["kind"] == "test_kind"


def test_stable_producer_retry_ignores_clock_and_detects_semantic_conflict():
    times = iter([1000.0, 2000.0, 3000.0])
    service = _service(requester=RequesterPolicy.INTERNAL_PRODUCER, clock=lambda: next(times))
    producer = RegisteredApprovalProducer(
        producer_id="test.producer", label="Test producer",
        allowed_kinds=frozenset({"test_kind"}), stable_source_ids=True,
    )
    first = service.create_from_producer(
        "test_kind", producer, {}, source_approval_id="source-1234567890",
    )
    again = service.create_from_producer(
        "test_kind", producer, {}, source_approval_id="source-1234567890",
    )
    assert again == first
    with pytest.raises(ApprovalServiceError, match="request_conflict"):
        service.create_from_producer(
            "test_kind", producer, {"request": {"operation": "changed"}},
            source_approval_id="source-1234567890",
        )
    with pytest.raises(ApprovalServiceError, match="request_conflict"):
        service.create_from_producer(
            "test_kind", producer, {"staged": {"opaque_ref": "changed"}},
            source_approval_id="source-1234567890",
        )
    assert next(times) == 2000.0


def test_stable_source_retry_rejects_changed_trusted_deadline_without_rewrite():
    def planner(_context, body):
        return {
            "subject_ref": "source-subject",
            "safe_review": {"summary": "Source request"},
            "request": {"operation": "source"},
            "trusted_source_expires_at": normalize_unix_milliseconds_deadline(
                body["native_ms"]
            ),
        }

    wakes = []
    store = InMemoryApprovalStore()
    service = _service(
        requester=RequesterPolicy.INTERNAL_PRODUCER,
        expiry=ApprovalExpiryPolicy(mode=ExpiryMode.TRUSTED_SOURCE_DEADLINE),
        planner=planner,
        store=store,
        wake=lambda record_type, approval_id: wakes.append((record_type, approval_id)),
    )
    producer = RegisteredApprovalProducer(
        producer_id="test.producer", label="Test producer",
        allowed_kinds=frozenset({"test_kind"}), stable_source_ids=True,
    )
    first = service.create_from_producer(
        "test_kind", producer, {"native_ms": 1_100_000},
        source_approval_id="deadline-source-change-001",
    )
    original = dict(first.payload)
    with pytest.raises(ApprovalServiceError, match="request_conflict"):
        service.create_from_producer(
            "test_kind", producer, {"native_ms": 1_200_000},
            source_approval_id="deadline-source-change-001",
        )
    assert store.get_request(first.approval_id).payload == original
    assert wakes == [("request", first.approval_id)]


def test_source_approval_id_requires_explicit_producer_authorization():
    wakes = []
    store = InMemoryApprovalStore()
    service = _service(
        requester=RequesterPolicy.INTERNAL_PRODUCER,
        store=store,
        wake=lambda record_type, approval_id: wakes.append((record_type, approval_id)),
    )
    unstable = RegisteredApprovalProducer(
        producer_id="test.unstable", label="Unstable producer",
        allowed_kinds=frozenset({"test_kind"}), stable_source_ids=False,
    )
    wrong_kind = RegisteredApprovalProducer(
        producer_id="test.wrong-kind", label="Wrong-kind producer",
        allowed_kinds=frozenset({"other_kind"}), stable_source_ids=True,
    )
    stable = RegisteredApprovalProducer(
        producer_id="test.stable", label="Stable producer",
        allowed_kinds=frozenset({"test_kind"}), stable_source_ids=True,
    )
    with pytest.raises(ApprovalServiceError, match="invalid_request"):
        service.create_from_producer(
            "test_kind", unstable, {}, source_approval_id="source-not-authorized-001",
        )
    with pytest.raises(ApprovalServiceError, match="unauthenticated"):
        service.create_from_producer(
            "test_kind", wrong_kind, {}, source_approval_id="source-wrong-kind-001",
        )
    with pytest.raises(ApprovalServiceError, match="invalid_request"):
        service.create_from_producer(
            "test_kind", stable, {}, source_approval_id="bad id",
        )
    assert store._requests == {}
    assert wakes == []


def test_expiry_modes_and_exact_deadline_precedence():
    bounded = ApprovalExpiryPolicy(
        mode=ExpiryMode.BOUNDED, minimum_seconds=1, maximum_seconds=300,
        default_seconds=60,
    )
    service = _service(expiry=bounded)
    created = service.create_from_principal("test_kind", _local(), {})
    assert created.payload["expires_at"] == 1060.0
    explicit = service.create_from_principal(
        "test_kind", _local(), {"requested_expiry_seconds": 20},
    )
    assert explicit.payload["expires_at"] == 1020.0
    for value in (True, 0, -1, 301):
        with pytest.raises(ApprovalServiceError, match="invalid_request"):
            service.create_from_principal(
                "test_kind", _local(), {"requested_expiry_seconds": value},
            )
    expired = service.reconcile_expiry(explicit.approval_id, now=1020.0)
    assert expired is not None and expired.payload["outcome"] == "expired"
    assert service.decide(
        explicit.approval_id, _human(), outcome="granted", decision={}, now=1020.0,
    ) == expired
    boundary = service.create_from_principal(
        "test_kind", _local(), {"requested_expiry_seconds": 20},
    )
    with pytest.raises(ApprovalServiceError, match="expired") as exc:
        service.decide(
            boundary.approval_id, _human(), outcome="granted", decision={}, now=1020.0,
        )
    assert exc.value.resolution is not None


def test_concurrent_opposite_answers_select_one_resolution():
    service = _service()
    created = service.create_from_principal("test_kind", _local(), {})
    barrier = threading.Barrier(2)
    answers = []

    def answer(outcome):
        barrier.wait()
        try:
            answers.append(service.decide(
                created.approval_id, _human(), outcome=outcome, decision={},
            ))
        except ApprovalServiceError as exc:
            answers.append(exc.resolution)

    threads = [threading.Thread(target=answer, args=(outcome,))
               for outcome in ("granted", "declined")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert answers[0] == answers[1]
    assert service.store.resolution_count(created.approval_id) == 1


def test_authorization_precedes_resolved_fast_path():
    service = _service()
    created = service.create_from_principal("test_kind", _local(), {})
    resolution = service.decide(
        created.approval_id, _human(), outcome="declined", decision={},
    )
    assert service.decide(
        created.approval_id, _human(), outcome="granted", decision={"bad": True},
    ) == resolution
    with pytest.raises(ApprovalServiceError, match="wrong_decider") as exc:
        service.decide(
            created.approval_id, HumanApprovalActor._verified(decider_ref="b" * 64),
            outcome="granted", decision={},
        )
    assert exc.value.resolution is None
    with pytest.raises(ApprovalServiceError, match="invalid_decision"):
        service.decide(
            created.approval_id, _human(), outcome="erase", decision={},
        )


def test_decision_uses_one_command_timestamp_at_deadline_boundary():
    times = iter([1000.0, 1019.999, 1020.0])
    service = _service(
        expiry=ApprovalExpiryPolicy(
            mode=ExpiryMode.BOUNDED, minimum_seconds=1, maximum_seconds=300,
            default_seconds=20,
        ),
        clock=lambda: next(times),
    )
    request = service.create_from_principal("test_kind", _local(), {})
    answer = service.decide(
        request.approval_id, _human(), outcome="granted", decision={},
    )
    assert answer.payload["resolved_at"] == 1019.999
    assert next(times) == 1020.0


def test_registered_service_and_internal_producer_requesters_are_server_derived():
    mcp = _service(requester=RequesterPolicy.REGISTERED_SERVICE)
    mcp._service_label = lambda subject: "Relay service"  # bounded resolver seam
    row = mcp.create_from_principal(
        "test_kind",
        api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.MCP_SERVICE, subject="relay-1"),
        {},
    )
    assert row.payload["requester_ref"] == {
        "kind": "registered_service", "id": "relay-1", "label": "Relay service",
    }
    internal = _service(requester=RequesterPolicy.INTERNAL_PRODUCER)
    producer = RegisteredApprovalProducer(
        producer_id="fleet.enrollment", label="Fleet enrollment",
        allowed_kinds=frozenset({"test_kind"}),
    )
    row = internal.create_from_producer("test_kind", producer, {})
    assert row.payload["requester_ref"] == {
        "kind": "internal_service", "id": "fleet.enrollment",
        "label": "Fleet enrollment",
    }


def test_org_session_requester_is_derived_from_authenticated_session_only():
    service = _service()
    principal = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION,
        subject="auto-org-1",
        org="example-org",
        persona_id="persona-secret",
    )
    row = service.create_from_principal("test_kind", principal, {})
    assert row.payload["requester_ref"] == {
        "kind": "session",
        "id": canonical_session_requester_id(principal),
        "label": "Session auto-org-1",
    }
    assert "org" not in row.payload
    assert "organization" not in row.payload
    assert "audience" not in row.payload
    assert "credential" not in row.payload


def test_session_requester_identity_is_scope_bound_and_authorized_before_status():
    service = _service()
    local = _local("same-subject")
    org_a = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION,
        subject="same-subject",
        org="org-a",
    )
    org_b = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION,
        subject="same-subject",
        org="org-b",
    )
    ids = {
        canonical_session_requester_id(local),
        canonical_session_requester_id(org_a),
        canonical_session_requester_id(org_b),
    }
    assert len(ids) == 3
    assert all(len(value) == 43 and "=" not in value for value in ids)

    row = service.create_from_principal("test_kind", org_a, {})
    assert service.status_for_principal(row.approval_id, org_a).state == "open"
    for wrong in (org_b, local):
        with pytest.raises(ApprovalServiceError, match="wrong_requester"):
            service.status_for_principal(row.approval_id, wrong)
        with pytest.raises(ApprovalServiceError, match="wrong_requester"):
            service.cancel_from_principal(row.approval_id, wrong)
    assert row.payload["requester_ref"]["id"] == canonical_session_requester_id(org_a)
    assert "same-subject" not in row.payload["requester_ref"]["id"]
    assert "org-a" not in row.payload["requester_ref"]["id"]


def test_staged_secret_shape_and_malformed_decision_fail_closed():
    service = _service()
    with pytest.raises(ApprovalServiceError, match="invalid_request"):
        service.create_from_principal(
            "test_kind", _local(), {"staged": {"token": "must-not-land"}},
        )
    created = service.create_from_principal("test_kind", _local(), {})
    with pytest.raises(ApprovalServiceError, match="invalid_decision"):
        service.decide(
            created.approval_id, _human(), outcome="granted",
            decision={"unexpected": "field"},
        )


def test_decline_uses_human_actor_but_skips_grant_ceremony():
    grants = []

    def validator(_request, decision, grant):
        grants.append(grant)
        if grant:
            raise ValueError("missing ceremony")
        if decision:
            raise ValueError("decline body must be empty")
        return {}

    declined = _service(validator=validator)
    request = declined.create_from_principal("test_kind", _local(), {})
    answer = declined.decide(
        request.approval_id, _human(), outcome="declined", decision={},
    )
    assert answer.payload["outcome"] == "declined"
    assert grants == [False]


def test_requester_cancel_and_wrong_requester_after_resolution():
    service = _service()
    request = service.create_from_principal("test_kind", _local(), {})
    answer = service.cancel_from_principal(request.approval_id, _local())
    assert answer.payload == {"outcome": "canceled", "resolved_at": 1000.0}
    assert service.cancel_from_principal(request.approval_id, _local()) == answer
    with pytest.raises(ApprovalServiceError, match="wrong_requester") as exc:
        service.cancel_from_principal(request.approval_id, _local("auto-2"))
    assert exc.value.resolution is None


def test_exact_registered_producer_can_cancel_but_another_cannot_read_resolution():
    service = _service(requester=RequesterPolicy.INTERNAL_PRODUCER)
    producer = RegisteredApprovalProducer(
        producer_id="test.producer", label="Test producer",
        allowed_kinds=frozenset({"test_kind"}),
    )
    other = RegisteredApprovalProducer(
        producer_id="test.other", label="Other producer",
        allowed_kinds=frozenset({"test_kind"}),
    )
    request = service.create_from_producer("test_kind", producer, {})
    answer = service.cancel_from_producer(request.approval_id, producer)
    assert answer.payload == {"outcome": "canceled", "resolved_at": 1000.0}
    assert service.cancel_from_producer(request.approval_id, producer) == answer
    with pytest.raises(ApprovalServiceError, match="wrong_requester") as exc:
        service.cancel_from_producer(request.approval_id, other)
    assert exc.value.resolution is None


def test_source_deadline_normalization_and_stale_source_no_write():
    assert normalize_unix_milliseconds_deadline(None) is None
    assert normalize_unix_milliseconds_deadline(0) is None
    assert normalize_unix_milliseconds_deadline(1_234_567) == 1234.567
    with pytest.raises(ValueError):
        normalize_unix_milliseconds_deadline(float("inf"))
    with pytest.raises(ValueError):
        normalize_unix_milliseconds_deadline(False)
    with pytest.raises(ValueError):
        normalize_unix_milliseconds_deadline(10 ** 1000)
    with pytest.raises(ValueError):
        normalize_unix_milliseconds_deadline(-1)

    def planner(_context, body):
        native = body.get("native_ms")
        return {
            "subject_ref": "subject-1",
            "safe_review": {"summary": "Source deadline"},
            "request": {"operation": "source"},
            "trusted_source_expires_at": normalize_unix_milliseconds_deadline(native),
        }

    source_policy = ApprovalExpiryPolicy(mode=ExpiryMode.TRUSTED_SOURCE_DEADLINE)
    service = _service(expiry=source_policy, planner=planner)
    without = service.create_from_principal("test_kind", _local(), {})
    assert "expires_at" not in without.payload
    with pytest.raises(ApprovalServiceError, match="source_expired"):
        service.create_from_principal("test_kind", _local(), {"native_ms": 999_000})
    assert len(service.store._requests) == 1


def test_never_and_fixed_expiry_reject_planner_selected_duration():
    never = _service(expiry=ApprovalExpiryPolicy(mode=ExpiryMode.NEVER))
    with pytest.raises(ApprovalServiceError, match="invalid_request"):
        never.create_from_principal(
            "test_kind", _local(), {"requested_expiry_seconds": 1},
        )
    fixed = _service(expiry=ApprovalExpiryPolicy(mode=ExpiryMode.FIXED, fixed_seconds=30))
    row = fixed.create_from_principal("test_kind", _local(), {})
    assert row.payload["expires_at"] == 1030.0
    with pytest.raises(ApprovalServiceError, match="invalid_request"):
        fixed.create_from_principal(
            "test_kind", _local(), {"requested_expiry_seconds": 20},
        )


def test_stable_bounded_retry_equates_omitted_and_explicit_default():
    policy = ApprovalExpiryPolicy(
        mode=ExpiryMode.BOUNDED, minimum_seconds=1, maximum_seconds=300,
        default_seconds=60,
    )
    service = _service(requester=RequesterPolicy.INTERNAL_PRODUCER, expiry=policy)
    producer = RegisteredApprovalProducer(
        producer_id="test.producer", label="Test producer",
        allowed_kinds=frozenset({"test_kind"}), stable_source_ids=True,
    )
    first = service.create_from_producer(
        "test_kind", producer, {}, source_approval_id="bounded-source-0001",
    )
    retry = service.create_from_producer(
        "test_kind", producer, {"requested_expiry_seconds": 60},
        source_approval_id="bounded-source-0001",
    )
    assert retry == first


def test_stable_source_retry_equates_missing_and_native_zero_deadline():
    def planner(_context, body):
        return {
            "subject_ref": "source-subject",
            "safe_review": {"summary": "Source request"},
            "request": {"operation": "source"},
            "trusted_source_expires_at": normalize_unix_milliseconds_deadline(
                body.get("native_ms")
            ),
        }

    service = _service(
        requester=RequesterPolicy.INTERNAL_PRODUCER,
        expiry=ApprovalExpiryPolicy(mode=ExpiryMode.TRUSTED_SOURCE_DEADLINE),
        planner=planner,
    )
    producer = RegisteredApprovalProducer(
        producer_id="test.producer", label="Test producer",
        allowed_kinds=frozenset({"test_kind"}), stable_source_ids=True,
    )
    first = service.create_from_producer(
        "test_kind", producer, {}, source_approval_id="deadline-source-001",
    )
    retry = service.create_from_producer(
        "test_kind", producer, {"native_ms": 0},
        source_approval_id="deadline-source-001",
    )
    assert retry == first


@pytest.mark.parametrize("method", ["bootstrap", "passkey", "password"])
def test_human_actor_resolver_accepts_only_direct_human_unlock_methods(monkeypatch, method):
    request = SimpleNamespace(state=SimpleNamespace(), cookies={})
    principal = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.OPERATOR_COOKIE, subject="sid-1",
    )
    monkeypatch.setattr(api_auth, "principal_from_request", lambda _request: principal)
    from tools.dashboard import unlock_routes
    monkeypatch.setattr(
        unlock_routes, "session_from_request",
        lambda _request: {"sid": "sid-1", "method": method},
    )
    actor = resolve_human_approval_actor(request, root_resolver=lambda: "a" * 64)
    assert actor.verified and actor.decider_ref == "a" * 64


@pytest.mark.parametrize("method", ["approval", "other", None])
def test_human_actor_resolver_rejects_delegated_or_unknown_cookie_methods(monkeypatch, method):
    request = SimpleNamespace(state=SimpleNamespace(), cookies={})
    principal = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.OPERATOR_COOKIE, subject="sid-1",
    )
    monkeypatch.setattr(api_auth, "principal_from_request", lambda _request: principal)
    from tools.dashboard import unlock_routes
    monkeypatch.setattr(
        unlock_routes, "session_from_request",
        lambda _request: {"sid": "sid-1", "method": method},
    )
    with pytest.raises(ApprovalServiceError, match="unauthenticated"):
        resolve_human_approval_actor(request, root_resolver=lambda: "a" * 64)


@pytest.mark.parametrize("principal_kind", [
    api_auth.ApiPrincipalKind.LOCAL_SESSION,
    api_auth.ApiPrincipalKind.ORG_SESSION,
    api_auth.ApiPrincipalKind.MCP_SERVICE,
    api_auth.ApiPrincipalKind.EXTERNAL_SERVICE,
    api_auth.ApiPrincipalKind.COMPATIBILITY,
])
def test_human_actor_resolver_rejects_every_nonhuman_principal(monkeypatch, principal_kind):
    request = SimpleNamespace(state=SimpleNamespace(), cookies={})
    monkeypatch.setattr(
        api_auth, "principal_from_request",
        lambda _request: api_auth.ApiPrincipal(principal_kind, subject="sid-1"),
    )
    with pytest.raises(ApprovalServiceError, match="unauthenticated"):
        resolve_human_approval_actor(request, root_resolver=lambda: "a" * 64)


def test_human_actor_resolver_rejects_cookie_sid_mismatch(monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace(), cookies={})
    monkeypatch.setattr(
        api_auth, "principal_from_request",
        lambda _request: api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.OPERATOR_COOKIE, subject="sid-1",
        ),
    )
    from tools.dashboard import unlock_routes
    monkeypatch.setattr(
        unlock_routes, "session_from_request",
        lambda _request: {"sid": "sid-other", "method": "passkey"},
    )
    with pytest.raises(ApprovalServiceError, match="unauthenticated"):
        resolve_human_approval_actor(request, root_resolver=lambda: "a" * 64)


def test_personal_root_explicit_armor_compatibility_and_mismatch(monkeypatch):
    class Rows:
        def __init__(self, payload):
            self.payload = payload

        def to_dict(self):
            return {"default": SimpleNamespace(payload=self.payload)}

    root = "a" * 64
    monkeypatch.setattr(
        "tools.dashboard.approval_service.settings_ops.read_set",
        lambda *_args, **_kwargs: Rows({"root_pub": root, "armored_private_key": "armor"}),
    )
    monkeypatch.setattr("tools.network.idkit.armor.armor_root_pub", lambda _armor: root)
    assert resolve_personal_root_public_key() == root
    monkeypatch.setattr("tools.network.idkit.armor.armor_root_pub", lambda _armor: "b" * 64)
    with pytest.raises(ApprovalServiceError, match="not_configured"):
        resolve_personal_root_public_key()
    monkeypatch.setattr(
        "tools.dashboard.approval_service.settings_ops.read_set",
        lambda *_args, **_kwargs: Rows({"armored_private_key": "armor"}),
    )
    assert resolve_personal_root_public_key() == "b" * 64
    monkeypatch.setattr(
        "tools.dashboard.approval_service.settings_ops.read_set",
        lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: {}),
    )
    with pytest.raises(ApprovalServiceError, match="not_configured"):
        resolve_personal_root_public_key()


def test_storage_failure_never_wakes_or_claims_success():
    class BrokenStore(InMemoryApprovalStore):
        def append_request(self, approval_id, payload):
            raise OSError("disk unavailable")

    wakes = []
    service = _service(store=BrokenStore(), wake=lambda *args: wakes.append(args))
    with pytest.raises(ApprovalServiceError, match="storage_unavailable"):
        service.create_from_principal("test_kind", _local(), {})
    assert wakes == []


def test_after_commit_wakes_only_after_each_durable_record():
    wakes = []
    service = _service(wake=lambda record_type, approval_id: wakes.append(
        (record_type, approval_id)
    ))
    request = service.create_from_principal("test_kind", _local(), {})
    assert wakes == [("request", request.approval_id)]
    service.decide(request.approval_id, _human(), outcome="declined", decision={})
    assert wakes == [
        ("request", request.approval_id),
        ("resolution", request.approval_id),
    ]


def test_result_ref_is_opaque_and_no_consumer_is_invoked():
    service = _service(
        result_ref_builder=lambda approval_id, _request, _decision: f"result:{approval_id}",
    )
    request = service.create_from_principal("test_kind", _local(), {})
    answer = service.decide(
        request.approval_id, _human(), outcome="granted", decision={},
    )
    assert answer.payload["result_ref"] == f"result:{request.approval_id}"
    assert "consumer" not in answer.payload


def test_unverified_actor_cannot_decide_and_after_commit_failure_cannot_undo_truth():
    service = _service(wake=lambda *_args: (_ for _ in ()).throw(RuntimeError("wake failed")))
    request = service.create_from_principal("test_kind", _local(), {})
    with pytest.raises(ApprovalServiceError, match="wrong_decider"):
        service.decide(
            request.approval_id, HumanApprovalActor(decider_ref="a" * 64),
            outcome="granted", decision={},
        )
    answer = service.decide(
        request.approval_id, _human(), outcome="granted", decision={},
    )
    assert service.get_resolution(request.approval_id) == answer


def test_settings_store_survives_service_reopen(tmp_path, monkeypatch):
    graph_db = tmp_path / "personal.db"
    monkeypatch.setenv("GRAPH_DB", str(graph_db))
    monkeypatch.delenv("GRAPH_API", raising=False)
    from tools.graph import db as graph_db_module
    original = graph_db_module._org_db_path
    monkeypatch.setattr(
        graph_db_module,
        "_org_db_path",
        lambda org, root=None: graph_db if org == "personal" else original(org, root),
    )
    first = _service(store=SettingsApprovalStore())
    request = first.create_from_principal("test_kind", _local(), {})
    first.close()
    second = _service(store=SettingsApprovalStore())
    assert second.get_request(request.approval_id) == request
    answer = second.decide(
        request.approval_id, _human(), outcome="granted", decision={},
    )
    second.close()
    third = _service(store=SettingsApprovalStore())
    assert third.get_resolution(request.approval_id) == answer
    assert set(third.store._read(APPROVAL_REQUEST_SET_ID, request.approval_id).payload) \
        >= {"kind", "requester_ref", "decider"}
    assert third.store._read(APPROVAL_RESOLUTION_SET_ID, request.approval_id) == answer

    expiring = _service(
        store=SettingsApprovalStore(),
        expiry=ApprovalExpiryPolicy(
            mode=ExpiryMode.BOUNDED, minimum_seconds=1, maximum_seconds=300,
            default_seconds=20,
        ),
    )
    due = expiring.create_from_principal("test_kind", _local(), {})
    expiring.close()
    materializer = _service(
        store=SettingsApprovalStore(),
        expiry=ApprovalExpiryPolicy(
            mode=ExpiryMode.BOUNDED, minimum_seconds=1, maximum_seconds=300,
            default_seconds=20,
        ),
    )
    expired = materializer.get_resolution(due.approval_id, now=1020.0)
    assert expired is not None and expired.payload["outcome"] == "expired"
    materializer.close()
    final = _service(
        store=SettingsApprovalStore(),
        expiry=ApprovalExpiryPolicy(
            mode=ExpiryMode.BOUNDED, minimum_seconds=1, maximum_seconds=300,
            default_seconds=20,
        ),
    )
    assert final.get_resolution(due.approval_id, now=9999.0) == expired
