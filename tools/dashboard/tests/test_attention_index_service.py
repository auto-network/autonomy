"""Contract tests for the Settings-native Central Attention index."""

from __future__ import annotations

from dataclasses import replace
import inspect
import threading

import pytest

from tools.dashboard import attention_index_service as index_service
from tools.dashboard.attention_index_service import (
    AttentionIndexError,
    AttentionIndexService,
    AttentionItemRecord,
    AttentionPresentationRecord,
    InMemoryAttentionIndexStore,
    canonical_attention_event_id,
)
from tools.dashboard.attention_registry import (
    MAX_SOURCE_VERSION,
    AttentionApplicationRegistration,
    AttentionClassPolicy,
    AttentionClassRegistration,
    AttentionProjectionPlan,
    AttentionPublicationRuntime,
    AttentionRegistry,
    AttentionSourceEvidence,
    build_production_attention_registry,
)
from tools.dashboard.approval_kind_registry import (
    ApprovalAttentionClass,
    ApprovalAttentionClassCatalog,
    ApprovalKindRegistry,
    ApprovalKindRuntime,
    AuthorityRequirement,
    PRODUCTION_APPROVAL_REGISTRY,
)
from tools.graph import settings_ops
from tools.graph.schemas.central_attention import (
    ATTENTION_APPLICATION_SET_ID,
    ATTENTION_ITEM_SET_ID,
    CENTRAL_ATTENTION_REVISION,
)


EXPECTED_APPLICATIONS = {
    "worktrees": "Worktrees",
    "jira": "Jira",
    "links": "Links",
    "sessions": "Sessions",
    "mission_control": "Mission Control",
    "vault": "Vault",
    "relay": "Relay",
    "fleet": "Fleet",
    "dropbox": "Dropbox",
    "backup": "Backup",
}

# Non-approval classes (auto-e4e66): kind -> (application, class).
EXPECTED_NON_APPROVAL_KIND_CLASS = {
    "backup.failed": ("backup", "backup_failed"),
    "backup.stale": ("backup", "backup_stale"),
    "backup.drill_failed": ("backup", "restore_drill_failed"),
    "backup.offsite_unreachable": ("backup", "offsite_unreachable"),
}

EXPECTED_KIND_CLASS = {
    "commit_sign": ("worktrees", "approval.commit_sign.requested"),
    "jira_write": ("jira", "approval.jira_write.requested"),
    "link_publish": ("links", "approval.link_publish.requested"),
    "link_revoke": ("links", "approval.link_revoke.requested"),
    "dashboard_access": ("sessions", "approval.dashboard_access.requested"),
    "visitor_token": ("mission_control", "approval.visitor_token.requested"),
    "secure_setting": ("vault", "approval.secure_setting.requested"),
    "mcp_peer_link": ("relay", "approval.mcp_peer_link.requested"),
    "mcp_crosstalk": ("relay", "approval.mcp_crosstalk.requested"),
    "fleet_machine_admission": (
        "fleet", "approval.fleet_machine_admission.requested",
    ),
    "external_service_access": (
        "dropbox", "approval.external_service_access.requested",
    ),
    "vault_open": ("vault", "approval.vault_open.requested"),
}


def _plan(source):
    return AttentionProjectionPlan(
        attention_id=source.get("attention_id", "attention:one"),
        object_ref=source.get("object_ref", "approval:one"),
        participant_role=source.get("participant_role", "recipient"),
        attention_state=source.get("attention_state", "needs_attention"),
        safe_title=source.get("safe_title", "Approval requested"),
        safe_summary=source.get("safe_summary", "Review the request."),
        counterparty_ref=source.get("counterparty_ref", "person:other"),
        occurred_at=source.get("occurred_at", 1000.0),
        source_version=source.get("source_version", 1),
    )


def _evidence(object_ref, source_version):
    return AttentionSourceEvidence(
        source_guard={
            "kind": "approval",
            "ref": object_ref,
            "version": source_version,
        },
        source_expires_at=2000.0,
    )


def _runtime(*, enabled=True, planner=_plan, evidence=_evidence):
    return AttentionPublicationRuntime(
        projection_planner=planner,
        source_evidence_builder=evidence,
        publication_enabled=enabled,
    )


def _approval_registry_with_runtime(*enabled_kinds):
    consumer_ids = set(PRODUCTION_APPROVAL_REGISTRY.consumer_ids)
    registrations = []
    for registration in PRODUCTION_APPROVAL_REGISTRY.kinds.values():
        if registration.kind in enabled_kinds:
            consumer_id = f"test.{registration.kind}.consumer"
            consumer_ids.add(consumer_id)
            registration = replace(registration, runtime=ApprovalKindRuntime(
                request_planner=lambda _context, _body: {},
                decision_validator=lambda _context, _request, _decision, _approved: {},
                resolution_consumer_id=consumer_id,
            ))
        registrations.append(registration)
    return ApprovalKindRegistry(
        registrations,
        PRODUCTION_APPROVAL_REGISTRY.attention_classes,
        consumer_ids=consumer_ids,
    )


def _approval_registry_from_rows(rows):
    classes = []
    consumers = set()
    for row in rows:
        for application in row.application_scope_policy.applications:
            classes.append(ApprovalAttentionClass(
                application, row.notification_class, row.renderer_id,
            ))
        if row.runtime is not None:
            consumers.add(row.runtime.resolution_consumer_id)
    return ApprovalKindRegistry(
        rows,
        ApprovalAttentionClassCatalog(classes),
        consumer_ids=consumers,
    )


def _registry(*, runtime=None):
    runtimes = {}
    if runtime is not None:
        runtimes[("fleet_machine_admission", "fleet")] = runtime
    approvals = (
        _approval_registry_with_runtime("fleet_machine_admission")
        if runtime is not None
        else PRODUCTION_APPROVAL_REGISTRY
    )
    return build_production_attention_registry(
        approval_registry=approvals,
        runtimes=runtimes,
    )


_DEFAULT_SECRET = object()


def _service(*, registry=None, store=None, callback=None, secret=_DEFAULT_SECRET):
    registry = registry or _registry(runtime=_runtime())
    kwargs = dict(
        registry=registry,
        store=store or InMemoryAttentionIndexStore(),
        after_commit=callback,
    )
    if secret is not _DEFAULT_SECRET:
        kwargs["cursor_secret"] = secret
    return AttentionIndexService(**kwargs)


def _publish(service, **changes):
    producer = service.registry.producer("fleet_machine_admission", "fleet")
    return service.publish(producer, changes)


def test_production_registry_has_exact_apps_kinds_and_uniform_policy():
    registry = build_production_attention_registry()
    assert {row.application_scope: row.label for row in registry.applications} == (
        EXPECTED_APPLICATIONS
    )
    assert set(registry.kind_bindings) == (
        set(EXPECTED_KIND_CLASS) | set(EXPECTED_NON_APPROVAL_KIND_CLASS)
    )
    assert "demo_ack" not in registry.kind_bindings
    for kind, (application, notification_class) in EXPECTED_KIND_CLASS.items():
        binding = registry.kind_bindings[kind]
        assert (binding.application_scope, binding.notification_class) == (
            application, notification_class,
        )
        assert binding.review_renderer_id == f"approval.{kind}.review"
        assert binding.surface_category == "approvals"
        assert binding.policy == AttentionClassPolicy.approval_phase_one()
        assert binding.runtime is None
    for kind, (application, notification_class) in (
            EXPECTED_NON_APPROVAL_KIND_CLASS.items()):
        binding = registry.kind_bindings[kind]
        assert (binding.application_scope, binding.notification_class) == (
            application, notification_class,
        )
        assert binding.surface_category == "apps"
        assert binding.policy == AttentionClassPolicy.backup_phase_one()
        assert binding.runtime is None
    assert all(not row.enabled for row in registry.application_records())


def test_application_payloads_are_exact_and_runtime_gates_are_per_class():
    registry = _registry(runtime=_runtime())
    records = {row.application_scope: row for row in registry.application_records()}
    fleet = records["fleet"]
    assert fleet.enabled is True
    assert fleet.payload == {
        "label": "Fleet",
        "icon_ref": "attention.application.fleet",
        "open_mode": "registered_renderer",
        "notification_classes": [{
            "notification_class": "approval.fleet_machine_admission.requested",
            "class_policy_revision": 1,
            "eligible_transition": "needs_attention",
            "push_policy": "fallback",
            "delivery_class": "normal",
            "budget_class": "operator_approval",
            "coalesce_scope": "object",
            "ttl_seconds": 21600,
            "urgency": "normal",
            "privacy_renderer_id": "web_push.generic.v1",
            "route_builder_id": "activity.approval.v1",
            "destination_id": "activity.approval",
        }],
        "enabled": True,
    }
    assert records["links"].enabled is False
    with pytest.raises(AttentionIndexError, match="class_disabled"):
        registry.producer("link_publish", "links")

    external = build_production_attention_registry(runtimes={
        ("external_service_access", "dropbox"): _runtime(),
    })
    assert next(
        row for row in external.application_records()
        if row.application_scope == "dropbox"
    ).enabled is False
    with pytest.raises(AttentionIndexError, match="class_disabled"):
        external.producer(
            "external_service_access",
            "dropbox",
            "external_service.dropbox_enrollment",
        )

    external = build_production_attention_registry(
        approval_registry=_approval_registry_with_runtime("external_service_access"),
        runtimes={("external_service_access", "dropbox"): _runtime()},
    )
    with pytest.raises(AttentionIndexError, match="not_found"):
        external.producer("external_service_access", "dropbox")
    assert external.producer(
        "external_service_access", "dropbox", "external_service.dropbox_enrollment",
    ).registration.producer_id == "external_service.dropbox_enrollment"


def test_registry_rejects_policy_revision_or_tuple_change_and_bad_shape():
    policy = AttentionClassPolicy.approval_phase_one()
    with pytest.raises(ValueError):
        replace(policy, class_policy_revision=2)
    with pytest.raises(ValueError):
        replace(policy, class_policy_revision=True)
    with pytest.raises(ValueError):
        replace(policy, push_policy="in_app_only")
    app = AttentionApplicationRegistration(
        application_scope="test",
        label="Test",
        icon_ref="attention.application.test",
        open_mode="registered_renderer",
        classes=(AttentionClassRegistration(
            kind="test_kind",
            application_scope="test",
            producer_id=None,
            notification_class="approval.test_kind.requested",
            surface_category="approvals",
            review_renderer_id="approval.test_kind.review",
            policy=policy,
            approval_runtime_enabled=True,
            runtime=_runtime(),
        ),),
    )
    with pytest.raises(ValueError, match="duplicate"):
        AttentionRegistry((app, app))
    with pytest.raises(ValueError):
        replace(app, open_mode="caller_selected")
    with pytest.raises(ValueError):
        _runtime(planner=None)
    with pytest.raises(ValueError, match="unknown attention runtime"):
        build_production_attention_registry(runtimes={
            ("unknown", "fleet"): _runtime(),
        })
    with pytest.raises(ValueError, match="application mismatch"):
        replace(app, classes=(replace(app.classes[0], application_scope="other"),))


def test_production_registry_rejects_added_missing_or_substituted_approval_catalog():
    rows = list(PRODUCTION_APPROVAL_REGISTRY.kinds.values())
    template = rows[0]
    extra = replace(
        template,
        kind="extra_kind",
        notification_class="approval.extra_kind.requested",
        renderer_id="approval.extra_kind.review",
    )
    with pytest.raises(ValueError, match="kinds do not match"):
        build_production_attention_registry(
            approval_registry=_approval_registry_from_rows([*rows, extra]),
        )
    with pytest.raises(ValueError, match="kinds do not match"):
        build_production_attention_registry(
            approval_registry=_approval_registry_from_rows(rows[:-1]),
        )
    changed = [
        replace(row, authority_requirement=AuthorityRequirement.PERSONAL_ROOT)
        if row.kind == "jira_write" else row
        for row in rows
    ]
    with pytest.raises(ValueError, match="metadata differs"):
        build_production_attention_registry(
            approval_registry=_approval_registry_from_rows(changed),
        )


def test_attention_runtime_cannot_activate_an_unmigrated_approval_kind():
    registry = build_production_attention_registry(runtimes={
        ("fleet_machine_admission", "fleet"): _runtime(),
    })
    binding = registry.kind_bindings["fleet_machine_admission"]
    assert binding.runtime is not None
    assert binding.approval_runtime_enabled is False
    assert binding.publication_enabled is False
    with pytest.raises(AttentionIndexError, match="class_disabled"):
        registry.producer("fleet_machine_admission", "fleet")


def test_registration_sync_writes_exact_personal_rows_and_is_idempotent():
    store = InMemoryAttentionIndexStore()
    service = _service(store=store)
    assert service.sync_registrations() == 10
    assert service.sync_registrations() == 0
    assert len(store.application_writes) == 10
    assert set(store.applications) == set(EXPECTED_APPLICATIONS)


def test_publication_converges_versions_and_freezes_identity():
    store = InMemoryAttentionIndexStore()
    changes = []
    service = _service(store=store, callback=changes.append)
    first = _publish(service)
    assert first.payload["source_version"] == 1
    assert len(store.item_writes) == len(changes) == 1

    again = _publish(service)
    assert again == first
    assert len(store.item_writes) == len(changes) == 1

    newer = _publish(
        service,
        source_version=2,
        attention_state="resolved",
        safe_title="Approval granted",
        safe_summary=None,
        occurred_at=1001.0,
    )
    assert newer.payload["attention_state"] == "resolved"
    assert len(store.item_writes) == len(changes) == 2

    with pytest.raises(AttentionIndexError, match="stale_source"):
        _publish(service, source_version=1)
    with pytest.raises(AttentionIndexError, match="source_conflict"):
        _publish(service, source_version=2, safe_title="Different")
    with pytest.raises(AttentionIndexError, match="identity_conflict"):
        _publish(service, source_version=3, object_ref="approval:other")
    assert len(store.item_writes) == len(changes) == 2


def test_publication_refuses_foreign_producer_and_bounds_store_failure():
    store = InMemoryAttentionIndexStore()
    callbacks = []
    service = _service(store=store, callback=callbacks.append)
    foreign = _service().registry.producer("fleet_machine_admission", "fleet")
    with pytest.raises(AttentionIndexError, match="invalid_request"):
        service.publish(foreign, {})
    assert store.item_writes == callbacks == []

    store.fail_items = True
    with pytest.raises(AttentionIndexError, match="unavailable"):
        _publish(service)
    assert store.item_writes == callbacks == []


def test_equal_version_retry_bounds_malformed_current_canonical_payload():
    store = InMemoryAttentionIndexStore()
    callbacks = []
    store.items["attention:one"] = {
        "application_scope": "fleet",
        "notification_class": "approval.fleet_machine_admission.requested",
        "object_ref": "approval:one",
        "participant_role": "recipient",
        "attention_state": "needs_attention",
        "safe_title": "isolated surrogate \ud800",
        "safe_summary": "Review the request.",
        "counterparty_ref": "person:other",
        "occurred_at": 1000.0,
        "source_version": 1,
    }
    service = _service(store=store, callback=callbacks.append)
    with pytest.raises(AttentionIndexError, match="unavailable"):
        _publish(service)
    assert store.item_writes == callbacks == []


@pytest.mark.parametrize("version", [0, MAX_SOURCE_VERSION])
def test_source_version_accepts_signed_64_bit_boundaries(version):
    service = _service()
    answer = _publish(service, source_version=version)
    assert answer.payload["source_version"] == version


@pytest.mark.parametrize("version", [True, -1, MAX_SOURCE_VERSION + 1, 10**1000])
def test_source_version_rejects_bool_negative_and_overflow(version):
    store = InMemoryAttentionIndexStore()
    changes = []
    with pytest.raises(AttentionIndexError, match="invalid_request"):
        _publish(_service(store=store, callback=changes.append), source_version=version)
    assert store.item_writes == changes == []


@pytest.mark.parametrize("bad_version", [True, -1, MAX_SOURCE_VERSION + 1, 2])
def test_source_evidence_version_must_be_bounded_and_exact(bad_version):
    runtime = _runtime(evidence=lambda ref, version: AttentionSourceEvidence(
        source_guard={"kind": "approval", "ref": ref, "version": bad_version},
        source_expires_at=None,
    ))
    store = InMemoryAttentionIndexStore()
    with pytest.raises(AttentionIndexError, match="invalid_request"):
        _publish(_service(registry=_registry(runtime=runtime), store=store))
    assert store.item_writes == []


@pytest.mark.parametrize(
    "changes",
    [
        {"attention_id": ""},
        {"attention_id": "a" * 129},
        {"attention_id": " format\u200b"},
        {"participant_role": "sender", "attention_state": "needs_attention"},
        {"participant_role": "recipient", "attention_state": "waiting"},
        {"occurred_at": float("nan")},
    ],
)
def test_invalid_projection_writes_and_emits_nothing(changes):
    store = InMemoryAttentionIndexStore()
    callbacks = []
    with pytest.raises(AttentionIndexError, match="invalid_request"):
        _publish(_service(store=store, callback=callbacks.append), **changes)
    assert store.item_writes == callbacks == []


def test_non_utf8_projection_is_bounded_before_write_or_callback():
    store = InMemoryAttentionIndexStore()
    callbacks = []
    with pytest.raises(AttentionIndexError, match="invalid_request"):
        _publish(
            _service(store=store, callback=callbacks.append),
            safe_title="isolated surrogate \ud800",
        )
    assert store.item_writes == callbacks == []


def test_two_services_share_process_lock_and_preserve_highest_version():
    store = InMemoryAttentionIndexStore()
    first = _service(store=store)
    second = _service(store=store)
    barrier = threading.Barrier(2)
    failures = []

    def run(service, version):
        try:
            barrier.wait()
            _publish(service, source_version=version, safe_title=f"Version {version}")
        except AttentionIndexError as exc:
            if exc.code != "stale_source":
                failures.append(exc)

    threads = [
        threading.Thread(target=run, args=(first, 1)),
        threading.Thread(target=run, args=(second, 2)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert store.items["attention:one"]["source_version"] == 2


def test_event_id_is_exact_domain_separated_and_restart_stable():
    assert canonical_attention_event_id("attention:one", 1) == (
        "A4rHkwbQOTdRdp1Q-q5_fsv6uMm_6qS-bM8Bx2a0sC8"
    )
    for attention_id in ("a:b|c", "é/承認"):
        first = canonical_attention_event_id(attention_id, MAX_SOURCE_VERSION)
        second = canonical_attention_event_id(attention_id, MAX_SOURCE_VERSION)
        assert first == second
        assert len(first) == 43
        assert "=" not in first


def test_after_commit_is_bounded_and_source_evidence_is_recoverable():
    changes = []
    service = _service(callback=changes.append)
    item = _publish(service)
    assert len(changes) == 1
    change = changes[0]
    assert change.event_id == canonical_attention_event_id("attention:one", 1)
    assert change.policy.class_policy_revision == 1
    assert change.source_evidence.source_guard == {
        "kind": "approval", "ref": "approval:one", "version": 1,
    }
    assert not callable(change.policy)
    assert service.source_evidence_for(item) == change.source_evidence


def test_disabled_publication_runtime_retains_historical_source_recovery():
    service = _service(registry=_registry(runtime=_runtime(enabled=False)))
    item = AttentionItemRecord("attention:one", {
        "application_scope": "fleet",
        "notification_class": "approval.fleet_machine_admission.requested",
        "object_ref": "approval:one",
        "participant_role": "recipient",
        "attention_state": "needs_attention",
        "safe_title": "Approval requested",
        "occurred_at": 1000.0,
        "source_version": 1,
    })
    with pytest.raises(AttentionIndexError, match="class_disabled"):
        service.registry.producer("fleet_machine_admission", "fleet")
    assert service.source_evidence_for(item).source_guard["version"] == 1


def test_historical_source_recovery_maps_malformed_row_or_evidence_to_unavailable():
    service = _service()
    malformed = AttentionItemRecord("attention:one", {
        "application_scope": "fleet",
        "notification_class": "approval.fleet_machine_admission.requested",
        "object_ref": "approval:one",
        "participant_role": "recipient",
        "attention_state": "needs_attention",
        "safe_title": "Approval requested",
        "occurred_at": 1000.0,
        "source_version": MAX_SOURCE_VERSION + 1,
    })
    with pytest.raises(AttentionIndexError, match="unavailable"):
        service.source_evidence_for(malformed)

    bad_evidence = _service(registry=_registry(runtime=_runtime(
        evidence=lambda ref, version: AttentionSourceEvidence(
            source_guard={"kind": "approval", "ref": ref, "version": version + 1},
        ),
    )))
    valid = replace(malformed, payload={**malformed.payload, "source_version": 1})
    with pytest.raises(AttentionIndexError, match="unavailable"):
        bad_evidence.source_evidence_for(valid)


def test_callback_failure_never_rolls_back_truth():
    store = InMemoryAttentionIndexStore()
    service = _service(
        store=store,
        callback=lambda _change: (_ for _ in ()).throw(RuntimeError("wake failed")),
    )
    assert _publish(service).attention_id == "attention:one"
    assert "attention:one" in store.items


def _seed_query_service():
    store = InMemoryAttentionIndexStore()
    registry = _registry(runtime=_runtime())
    # Query accepts historical registered rows even while their publication runtime is absent.
    rows = [
        ("fleet:need", "fleet", "approval.fleet_machine_admission.requested", "recipient", "needs_attention", 50.0),
        ("fleet:resolved", "fleet", "approval.fleet_machine_admission.requested", "recipient", "resolved", 40.0),
        ("links:need", "links", "approval.link_publish.requested", "recipient", "needs_attention", 30.0),
        ("relay:wait", "relay", "approval.mcp_crosstalk.requested", "sender", "waiting", 20.0),
    ]
    for attention_id, app, cls, role, state, occurred in rows:
        store.items[attention_id] = {
            "application_scope": app,
            "notification_class": cls,
            "object_ref": f"object:{attention_id}",
            "participant_role": role,
            "attention_state": state,
            "safe_title": attention_id,
            "occurred_at": occurred,
            "source_version": 1,
        }
    store.presentations["fleet:need"] = {"seen_at": 55.0}
    return _service(registry=registry, store=store), store


def test_query_count_cohorts_and_registered_zeroes_are_exact():
    service, _store = _seed_query_service()
    all_rows = service.query()
    assert [item.attention_id for item in all_rows.items] == [
        "fleet:need", "fleet:resolved", "links:need", "relay:wait",
    ]
    assert all_rows.counts.total_needs_attention == 2
    assert all_rows.counts.categories == {"apps": 0, "comms": 0, "approvals": 2}
    assert sum(all_rows.counts.categories.values()) == 2
    assert all_rows.counts.states == {
        "needs_attention": 2, "waiting": 1, "resolved": 1,
    }
    assert set(all_rows.counts.applications) == set(EXPECTED_APPLICATIONS)
    assert all_rows.counts.applications["jira"] == {
        "needs_attention": 0, "waiting": 0, "resolved": 0,
    }

    fleet = service.query(application_scope="fleet", surface_category="approvals")
    assert fleet.counts.total_needs_attention == 1
    assert fleet.counts.states == {
        "needs_attention": 1, "waiting": 0, "resolved": 1,
    }
    assert set(fleet.counts.applications) == {"fleet"}
    assert [item.attention_id for item in service.query(
        surface_category="approvals", attention_state="needs_attention",
    ).items] == ["fleet:need", "links:need"]

    sender = service.query(participant_role="sender")
    assert [item.attention_id for item in sender.items] == ["relay:wait"]
    assert sender.counts.total_needs_attention == 0
    assert sender.counts.states == {
        "needs_attention": 0, "waiting": 1, "resolved": 0,
    }


def test_query_presentation_joins_without_changing_semantic_snapshot():
    service, store = _seed_query_service()
    first = service.query(limit=1)
    assert first.items[0].presentation == {"seen_at": 55.0}
    cursor = first.next_cursor
    snapshot = first.snapshot_version
    store.presentations["fleet:need"] = {
        "seen_at": 55.0, "last_opened_at": 60.0,
    }
    assert service.query(limit=1, cursor=cursor).snapshot_version == snapshot
    assert service.query(limit=1).items[0].presentation["last_opened_at"] == 60.0


def test_cursor_pages_tamper_filter_restart_and_snapshot_classification():
    service, store = _seed_query_service()
    first = service.query(limit=1)
    second = service.query(limit=1, cursor=first.next_cursor)
    assert first.items[0].attention_id != second.items[0].attention_id
    with pytest.raises(AttentionIndexError, match="invalid_cursor"):
        service.query(limit=1, cursor=first.next_cursor + "x")
    with pytest.raises(AttentionIndexError, match="invalid_cursor"):
        service.query(limit=2, cursor=first.next_cursor)
    with pytest.raises(AttentionIndexError, match="invalid_cursor"):
        service.query(limit=1, cursor="x" * 4097)
    same_process = _service(registry=service.registry, store=store)
    assert same_process.query(
        limit=1, cursor=first.next_cursor,
    ).items[0].attention_id == second.items[0].attention_id
    with pytest.raises(AttentionIndexError, match="invalid_cursor"):
        _service(registry=service.registry, store=store, secret=b"n" * 32).query(
            limit=1, cursor=first.next_cursor,
        )
    store.items["new:item"] = {
        **store.items["fleet:need"],
        "object_ref": "object:new",
        "safe_title": "New",
        "occurred_at": 100.0,
        "source_version": 2,
    }
    with pytest.raises(AttentionIndexError, match="refresh_required"):
        service.query(limit=1, cursor=first.next_cursor)


def test_cursor_covers_101_items_without_skip_or_duplicate():
    service, store = _seed_query_service()
    template = dict(store.items["fleet:need"])
    store.items.clear()
    for number in range(101):
        store.items[f"item:{number:03}"] = {
            **template,
            "object_ref": f"object:{number}",
            "safe_title": f"Item {number}",
            "occurred_at": float(1000 - number),
        }
    first = service.query(limit=100)
    second = service.query(limit=100, cursor=first.next_cursor)
    ids = [item.attention_id for item in first.items + second.items]
    assert len(ids) == len(set(ids)) == 101
    assert second.next_cursor is None


@pytest.mark.parametrize("failure", ["items", "presentations"])
def test_query_store_failures_are_unavailable_without_partial_result(failure):
    service, store = _seed_query_service()
    setattr(store, f"fail_{failure}", True)
    with pytest.raises(AttentionIndexError, match="unavailable"):
        service.query()


def test_stale_presentation_is_ignored_but_malformed_known_row_fails():
    service, store = _seed_query_service()
    store.presentations["orphan"] = {"seen_at": 1.0}
    assert len(service.query().items) == 4
    store.presentations["fleet:need"] = {"bad": True}
    with pytest.raises(AttentionIndexError, match="unavailable"):
        service.query()


def test_incoherent_stored_item_or_malformed_presentation_key_is_unavailable():
    service, store = _seed_query_service()
    store.items["fleet:need"]["attention_state"] = "waiting"
    with pytest.raises(AttentionIndexError, match="unavailable"):
        service.query()
    store.items["fleet:need"]["attention_state"] = "needs_attention"
    store.presentations["bad\nkey"] = {"seen_at": 1.0}
    with pytest.raises(AttentionIndexError, match="unavailable"):
        service.query()


def test_unknown_class_and_non_json_store_rows_are_bounded_unavailable():
    service, store = _seed_query_service()
    store.items["fleet:need"]["notification_class"] = "approval.unknown.requested"
    with pytest.raises(AttentionIndexError, match="unavailable"):
        service.query()

    service, store = _seed_query_service()
    store.items["fleet:need"]["safe_title"] = {"not-json"}
    with pytest.raises(AttentionIndexError, match="unavailable"):
        service.query()


@pytest.mark.parametrize(
    "dropped_set",
    [ATTENTION_ITEM_SET_ID, index_service.ATTENTION_PRESENTATION_SET_ID],
)
def test_settings_store_drop_accounting_refuses_partial_query(
    monkeypatch, dropped_set,
):
    def read_set(set_id, *, org, peers):
        dropped = settings_ops.DropAccounting(
            schema_invalid=1 if set_id == dropped_set else 0,
        )
        return settings_ops.SetMembers(members=[], dropped=dropped)

    monkeypatch.setattr(index_service.settings_ops, "read_set", read_set)
    service = AttentionIndexService(
        registry=_registry(runtime=_runtime()),
        store=index_service.SettingsAttentionIndexStore(),
    )
    with pytest.raises(AttentionIndexError, match="unavailable"):
        service.query()


def test_settings_store_uses_only_personal_raw_rows(monkeypatch):
    calls = []

    def upsert_by_key(set_id, revision, key, payload, *, org, state):
        calls.append((set_id, revision, key, dict(payload), org, state))

    monkeypatch.setattr(index_service.settings_ops, "upsert_by_key", upsert_by_key)
    store = index_service.SettingsAttentionIndexStore()
    store.upsert_application("fleet", {
        "label": "Fleet", "icon_ref": "attention.application.fleet",
        "open_mode": "registered_renderer", "notification_classes": [],
        "enabled": False,
    })
    store.upsert_item("attention:one", {
        "application_scope": "fleet",
        "notification_class": "approval.fleet_machine_admission.requested",
        "object_ref": "approval:one", "participant_role": "recipient",
        "attention_state": "needs_attention", "safe_title": "Review",
        "occurred_at": 1.0, "source_version": 1,
    })
    assert calls[0][0:3] == (
        ATTENTION_APPLICATION_SET_ID, CENTRAL_ATTENTION_REVISION, "fleet",
    )
    assert calls[1][0:3] == (
        ATTENTION_ITEM_SET_ID, CENTRAL_ATTENTION_REVISION, "attention:one",
    )
    assert all(call[-2:] == (None, "raw") for call in calls)


def test_module_boundary_has_no_routes_sse_latch_push_or_legacy_mutation():
    source = inspect.getsource(index_service)
    for forbidden in (
        "Route(", "event_bus", "approval:changed", "AttentionDeliveryV1",
        "web_push", "approval_requests", "operator_dismissed",
    ):
        assert forbidden not in source
    signatures = " ".join(
        str(inspect.signature(method))
        for method in (AttentionIndexService.publish, AttentionIndexService.query)
    )
    for selector in ("recipient", "audience", "persona", "organization", "org"):
        assert selector not in signatures
