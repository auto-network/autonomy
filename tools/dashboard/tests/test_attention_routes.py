from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from tools.dashboard import api_auth, attention_routes
from tools.dashboard.approval_kind_registry import (
    ApplicationScopePolicy, ApprovalAttentionClass,
    ApprovalAttentionClassCatalog, ApprovalExpiryPolicy,
    ApprovalKindRegistration, ApprovalKindRegistry, ApprovalKindRuntime,
    AuthorityRequirement, DeciderPolicy, ExpiryMode, RequesterPolicy,
)
from tools.dashboard.approval_service import (
    ApprovalService, HumanApprovalActor, InMemoryApprovalStore,
    resolve_human_approval_actor,
)
from tools.dashboard.attention_index_service import (
    AttentionIndexError, AttentionIndexService, AttentionQueryItem,
    InMemoryAttentionIndexStore,
)
from tools.dashboard.attention_presentation_service import (
    AttentionItemRecord as PresentationItemRecord,
    AttentionPresentationRecord as PresentationRecord,
    AttentionPresentationService,
)
from tools.dashboard.attention_registry import (
    AttentionApplicationRegistration, AttentionClassPolicy,
    AttentionClassRegistration, AttentionProjectionPlan,
    AttentionPublicationRuntime, AttentionRegistry, AttentionSourceEvidence,
)
from tools.dashboard.event_bus import EventBus, _BufferEntry
from tools.graph.schemas.central_attention import (
    APPROVAL_REQUEST_SET_ID, APPROVAL_RESOLUTION_SET_ID,
    ATTENTION_DELIVERY_SET_ID, ATTENTION_ITEM_SET_ID,
    ATTENTION_PRESENTATION_SET_ID,
)

ROOT = "a" * 64
APPROVAL_ID = "approval-1234567890"


class _PresentationStore:
    def __init__(self, index_store):
        self.index_store = index_store

    def get(self, attention_id):
        payload = self.index_store.presentations.get(attention_id)
        return None if payload is None else PresentationRecord(
            attention_id, dict(payload),
        )

    def upsert(self, attention_id, payload):
        self.index_store.presentations[attention_id] = dict(payload)
        return PresentationRecord(attention_id, dict(payload))


def _route_runtime():
    approval_runtime = ApprovalKindRuntime(
        request_planner=lambda _context, body: {
            "subject_ref": "machine-1",
            "safe_review": body.get(
                "safe_review", {"summary": "Review machine"},
            ),
            "request": {"operation": "join"},
        },
        decision_validator=lambda _context, _request, decision, _grant: dict(decision),
        resolution_consumer_id="test.consumer",
    )
    registration = ApprovalKindRegistration(
        kind="test_kind",
        application_scope_policy=ApplicationScopePolicy(fixed="test_app"),
        notification_class="approval.test_kind.requested",
        renderer_id="approval.test_kind.review",
        requester_policy=RequesterPolicy.SESSION_PRINCIPAL,
        decider_policy=DeciderPolicy.PERSONAL_OPERATOR,
        authority_requirement=AuthorityRequirement.OPERATOR_SESSION,
        request_expiry_policy=ApprovalExpiryPolicy(ExpiryMode.NEVER),
        runtime=approval_runtime,
    )
    registry = ApprovalKindRegistry(
        [registration],
        ApprovalAttentionClassCatalog([ApprovalAttentionClass(
            "test_app", "approval.test_kind.requested",
            "approval.test_kind.review",
        )]),
        consumer_ids={"test.consumer"},
    )
    approvals = ApprovalService(
        registry=registry, store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: ROOT,
        session_label_resolver=lambda _subject: "Requester session",
        id_factory=lambda: APPROVAL_ID, clock=lambda: 100.0,
    )
    approvals.create_from_principal(
        "test_kind",
        api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.LOCAL_SESSION, "session-1",
        ),
        {},
    )

    def projection(source):
        return AttentionProjectionPlan(
            attention_id=source["attention_id"], object_ref=APPROVAL_ID,
            participant_role=source["role"], attention_state=source["state"],
            safe_title="Approval requested", safe_summary="Review the request",
            counterparty_ref=None,
            occurred_at=source.get("occurred_at", 100.0),
            source_version=source["version"],
        )

    attention_runtime = AttentionPublicationRuntime(
        projection_planner=projection,
        source_evidence_builder=lambda ref, version: AttentionSourceEvidence(
            {"kind": "approval", "ref": ref, "version": version},
        ),
    )
    attention_class = AttentionClassRegistration(
        kind="test_kind", application_scope="test_app", producer_id=None,
        notification_class="approval.test_kind.requested",
        surface_category="approvals",
        review_renderer_id="approval.test_kind.review",
        policy=AttentionClassPolicy.approval_phase_one(),
        approval_runtime_enabled=True, runtime=attention_runtime,
    )
    attention_registry = AttentionRegistry([AttentionApplicationRegistration(
        application_scope="test_app", label="Test app",
        icon_ref="attention.application.test", open_mode="registered_renderer",
        classes=(attention_class,),
    )])
    index_store = InMemoryAttentionIndexStore()
    index = AttentionIndexService(registry=attention_registry, store=index_store)
    index.sync_registrations()
    producer = attention_registry.producer("test_kind", "test_app")
    index.publish(producer, {
        "attention_id": "recipient-item", "role": "recipient",
        "state": "needs_attention", "version": 1,
    })
    index.publish(producer, {
        "attention_id": "sender-item", "role": "sender",
        "state": "waiting", "version": 1, "occurred_at": 99.0,
    })

    def resolve_item(attention_id):
        payload = index_store.items.get(attention_id)
        return None if payload is None else PresentationItemRecord(
            attention_id, dict(payload),
        )

    presentation = AttentionPresentationService(
        reference_resolver=resolve_item, store=_PresentationStore(index_store),
        clock=lambda: 110.0,
    )
    return attention_routes.AttentionRouteRuntime(
        index=index, presentation=presentation, approvals=approvals,
        hub=attention_routes.PrivateAttentionHub(
            item_resolver=index.get_query_item,
        ),
    ), producer


@pytest.fixture
def route_client(monkeypatch):
    runtime, producer = _route_runtime()
    previous = attention_routes.configure_runtime(runtime)
    monkeypatch.setattr(
        api_auth, "principal_from_request",
        lambda _request: api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.OPERATOR_COOKIE, "cookie-1",
        ),
    )
    monkeypatch.setattr(
        attention_routes.unlock_routes, "gate_enforced", lambda: True,
    )
    monkeypatch.setattr(
        attention_routes, "resolve_human_approval_actor",
        lambda _request: HumanApprovalActor._verified(ROOT),
    )
    try:
        with TestClient(
            Starlette(routes=attention_routes.routes),
            base_url="https://dashboard.test",
        ) as client:
            yield client, runtime, producer
    finally:
        attention_routes.configure_runtime(previous)


class TestAttentionOperatorAPI:
    def test_safe_list_detail_sender_and_presentation(self, route_client):
        client, runtime, producer = route_client
        response = client.get("/api/attention/items")
        assert response.status_code == 200
        assert response.json()["counts"]["total_needs_attention"] == 1
        assert {row["attention_id"] for row in response.json()["items"]} == {
            "recipient-item", "sender-item",
        }
        for forbidden in (
            '"object_ref"', '"request":', '"staged"', '"result_ref"',
            ROOT, APPROVAL_ID,
        ):
            assert forbidden not in response.text
        detail = client.get("/api/attention/items/recipient-item")
        assert detail.status_code == 200
        assert detail.json()["review"]["safe_review"] == {
            "summary": "Review machine",
        }
        assert detail.json()["review"]["requester"] == {
            "kind": "session", "label": "Requester session",
        }
        assert detail.json()["review"]["actions"] == ["granted", "declined"]
        assert client.get(
            "/api/attention/items/sender-item",
        ).json()["review"]["actions"] == []
        original_decide = runtime.approvals.decide
        runtime.approvals.decide = lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("sender invoked decision service")
        )
        try:
            refused = client.post(
                "/api/attention/items/sender-item/approval-decision",
                headers={"Origin": "https://dashboard.test"},
                json={"outcome": "granted", "decision": {}},
            )
        finally:
            runtime.approvals.decide = original_decide
        assert refused.status_code == 409
        assert runtime.approvals.status(APPROVAL_ID).state == "open"
        seen = client.post(
            "/api/attention/items/recipient-item/seen",
            headers={"Origin": "https://dashboard.test"}, json={},
        )
        assert seen.status_code == 200
        assert seen.json()["presentation"]["seen_at"] == 110.0
        assert runtime.approvals.status(APPROVAL_ID).state == "open"
        runtime.index.publish(producer, {
            "attention_id": "opaque/item", "role": "recipient",
            "state": "needs_attention", "version": 1,
        })
        assert client.get(
            "/api/attention/items/opaque%2Fitem",
        ).status_code == 200
        assert client.post(
            "/api/attention/items/opaque%2Fitem/opened",
            headers={"Origin": "https://dashboard.test"}, json={},
        ).status_code == 200

    def test_query_cursor_filters_refresh_and_partial_read(self, route_client):
        client, runtime, producer = route_client
        runtime.index.publish(producer, {
            "attention_id": "recipient-item-2", "role": "recipient",
            "state": "needs_attention", "version": 1, "occurred_at": 98.0,
        })
        first = client.get("/api/attention/items?limit=1")
        cursor = first.json()["next_cursor"]
        assert first.status_code == 200 and isinstance(cursor, str)
        second = client.get(
            "/api/attention/items", params={"limit": "1", "cursor": cursor},
        )
        assert second.status_code == 200
        assert second.json()["items"][0]["attention_id"] == "sender-item"
        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        assert client.get(
            "/api/attention/items", params={"limit": "1", "cursor": tampered},
        ).status_code == 400
        assert client.get(
            "/api/attention/items",
            params={"limit": "1", "role": "recipient", "cursor": cursor},
        ).status_code == 400
        runtime.index.store.items["recipient-item-2"]["safe_summary"] = "Changed"
        assert client.get(
            "/api/attention/items", params={"limit": "1", "cursor": cursor},
        ).status_code == 409
        original_secret = runtime.index._cursor_secret
        runtime.index._cursor_secret = b"restarted-process-secret-32byte!"[:32]
        try:
            assert client.get(
                "/api/attention/items",
                params={"limit": "1", "cursor": cursor},
            ).status_code == 400
        finally:
            runtime.index._cursor_secret = original_secret
        runtime.index.store.fail_items = True
        assert client.get("/api/attention/items").status_code == 503

    def test_human_methods_selectors_and_principal_matrix(
        self, route_client, monkeypatch,
    ):
        client, runtime, _producer = route_client

        def actor_for(method, root=ROOT):
            monkeypatch.setattr(
                attention_routes.unlock_routes, "session_from_request",
                lambda _request: {"sid": "cookie-1", "method": method},
            )
            monkeypatch.setattr(
                attention_routes, "resolve_human_approval_actor",
                lambda request: resolve_human_approval_actor(
                    request, root_resolver=lambda: root,
                ),
            )

        for method in ("bootstrap", "passkey", "password"):
            actor_for(method)
            assert client.get(
                "/api/attention/items/recipient-item",
            ).json()["review"]["actions"] == ["granted", "declined"]
        actor_for("approval")
        assert client.post(
            "/api/attention/items/recipient-item/approval-decision",
            headers={"Origin": "https://dashboard.test"},
            json={"outcome": "granted", "decision": {}},
        ).status_code == 401
        actor_for("password", "b" * 64)
        assert client.post(
            "/api/attention/items/recipient-item/approval-decision",
            headers={"Origin": "https://dashboard.test"},
            json={"outcome": "granted", "decision": {}},
        ).status_code == 404
        actor_for("password")
        assert client.post(
            "/api/attention/items/recipient-item/approval-decision",
            headers={"Origin": "https://dashboard.test"},
            json={"outcome": "granted", "decision": {}, "org": "forged"},
        ).status_code == 422
        assert runtime.approvals.status(APPROVAL_ID).state == "open"
        assert client.get(f"/api/attention/items/{APPROVAL_ID}").status_code == 404
        for kind in (
            api_auth.ApiPrincipalKind.LOCAL_SESSION,
            api_auth.ApiPrincipalKind.ORG_SESSION,
            api_auth.ApiPrincipalKind.MCP_SERVICE,
            api_auth.ApiPrincipalKind.EXTERNAL_SERVICE,
        ):
            monkeypatch.setattr(
                api_auth, "principal_from_request",
                lambda _request, kind=kind: api_auth.ApiPrincipal(kind, "agent"),
            )
            assert client.get("/api/attention/items").status_code == 403

    def test_lifecycle_binding_mismatch_disabled_and_storage_failure(
        self, route_client,
    ):
        client, runtime, producer = route_client
        runtime.approvals.decide(
            APPROVAL_ID, HumanApprovalActor._verified(ROOT),
            outcome="granted", decision={},
        )
        for item_id in ("recipient-item", "sender-item"):
            stale = client.get(f"/api/attention/items/{item_id}")
            assert stale.status_code == 200
            assert stale.json()["review"]["resolution"]["outcome"] == "granted"
            assert stale.json()["review"]["actions"] == []
        for item_id, role in (
            ("recipient-item", "recipient"), ("sender-item", "sender"),
        ):
            runtime.index.publish(producer, {
                "attention_id": item_id, "role": role, "state": "resolved",
                "version": 2, "occurred_at": 111.0,
            })
            assert client.get(
                f"/api/attention/items/{item_id}",
            ).json()["review"]["actions"] == []
        payload = runtime.index.store.items["recipient-item"]
        for version, state in (
            (1, "resolved"), (2, "needs_attention"), (3, "resolved"),
        ):
            payload["source_version"], payload["attention_state"] = version, state
            assert client.get(
                "/api/attention/items/recipient-item",
            ).status_code == 409

        fresh, _ = _route_runtime()
        old = attention_routes.configure_runtime(fresh)
        try:
            fresh.index.store.items["recipient-item"]["object_ref"] = (
                "approval-missing-1234"
            )
            assert client.get(
                "/api/attention/items/recipient-item",
            ).status_code == 409
            fresh.index.store.items["recipient-item"]["object_ref"] = APPROVAL_ID
            registration = fresh.index.registry.require_class(
                "test_app", "approval.test_kind.requested",
            )
            saved = registration.runtime
            object.__setattr__(registration, "runtime", None)
            assert client.get(
                "/api/attention/items/recipient-item",
            ).status_code == 409
            object.__setattr__(registration, "runtime", saved)
            fresh.approvals.store.get_request = lambda _id: (_ for _ in ()).throw(
                RuntimeError("unavailable")
            )
            assert client.get(
                "/api/attention/items/recipient-item",
            ).status_code == 503
        finally:
            attention_routes.configure_runtime(old)

    def test_deadline_retry_concurrency_and_bounded_bodies(self, route_client):
        client, runtime, _producer = route_client
        runtime.approvals.store._requests[APPROVAL_ID].payload["expires_at"] = 100.0
        request = {
            "headers": {"Origin": "https://dashboard.test"},
            "json": {"outcome": "granted", "decision": {}},
        }
        first = client.post(
            "/api/attention/items/recipient-item/approval-decision", **request,
        )
        second = client.post(
            "/api/attention/items/recipient-item/approval-decision", **request,
        )
        assert first.status_code == second.status_code == 409
        assert first.json() == second.json()
        assert first.json()["resolution"]["outcome"] == "expired"

        runtime, _producer = _route_runtime()
        previous = attention_routes.configure_runtime(runtime)
        entered, release = threading.Event(), threading.Event()
        original_append = runtime.approvals.store.append_resolution

        def blocked_append(approval_id, payload):
            entered.set()
            assert release.wait(timeout=3)
            return original_append(approval_id, payload)

        runtime.approvals.store.append_resolution = blocked_append

        def decide(outcome):
            return client.post(
                "/api/attention/items/recipient-item/approval-decision",
                headers={"Origin": "https://dashboard.test"},
                json={"outcome": outcome, "decision": {}},
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                winning = pool.submit(decide, "granted")
                assert entered.wait(timeout=3)
                losing = pool.submit(decide, "declined")
                release.set()
                responses = (
                    winning.result(timeout=5), losing.result(timeout=5),
                )
            assert [row.status_code for row in responses] == [200, 200]
            assert responses[0].json() == responses[1].json()
            assert set(responses[0].json()) == {"resolution"}
        finally:
            attention_routes.configure_runtime(previous)

        client, runtime, _producer = route_client
        headers = {
            "Origin": "https://dashboard.test",
            "Content-Type": "application/json",
        }
        assert client.post(
            "/api/attention/items/recipient-item/snooze", headers=headers,
            content='{"duration_seconds":60,"duration_seconds":120}',
        ).status_code == 422
        assert client.post(
            "/api/attention/items/recipient-item/approval-decision",
            headers=headers,
            content='{"outcome":"granted","decision":{"value":NaN}}',
        ).status_code == 422
        assert client.post(
            "/api/attention/items/recipient-item/seen", headers=headers,
            content='{"padding":"' + ("x" * 33_000) + '"}',
        ).status_code == 422
        assert runtime.index.store.presentations == {}

    def test_query_validation_compatibility_and_production_registry(
        self, route_client, monkeypatch,
    ):
        client, _runtime, _producer = route_client
        for query in (
            "limit=+1", "limit=1&limit=2", "unknown=x", "org=autonomy",
        ):
            assert client.get(f"/api/attention/items?{query}").status_code == 400
        assert client.post(
            "/api/attention/items/recipient-item/seen",
            headers={"Origin": "https://foreign.test"}, json={},
        ).status_code == 403
        monkeypatch.setattr(
            api_auth, "principal_from_request",
            lambda _request: api_auth.COMPATIBILITY_PRINCIPAL,
        )
        assert client.get("/api/attention/items").status_code == 401
        monkeypatch.setattr(
            attention_routes.unlock_routes, "gate_enforced", lambda: False,
        )
        assert client.get("/api/attention/items").status_code == 200
        production = attention_routes.build_production_runtime()
        applications = tuple(production.index.registry.applications)
        assert len(applications) == 9
        assert sum(len(app.classes) for app in applications) == 12
        assert [app.application_scope for app in applications if app.enabled] == [
            "sessions"
        ]
        assert [
            cls.kind
            for app in applications
            for cls in app.classes
            if cls.publication_enabled
        ] == ["dashboard_access"]
        store = InMemoryAttentionIndexStore()
        index = AttentionIndexService(
            registry=production.index.registry, store=store,
        )
        assert index.sync_registrations() == 9
        assert index.sync_registrations() == 0
        assert len(store.applications) == 9
        assert store.items == {}

    @pytest.mark.asyncio
    async def test_private_sse_frames_auth_and_legacy_route(self, monkeypatch):
        runtime, _producer = _route_runtime()
        previous = attention_routes.configure_runtime(runtime)
        monkeypatch.setattr(
            api_auth, "principal_from_request",
            lambda _request: api_auth.ApiPrincipal(
                api_auth.ApiPrincipalKind.OPERATOR_COOKIE, "cookie-1",
            ),
        )
        request = Request({
            "type": "http", "http_version": "1.1", "method": "GET",
            "scheme": "https", "path": "/api/attention/events",
            "raw_path": b"/api/attention/events", "query_string": b"",
            "headers": [], "client": ("127.0.0.1", 1),
            "server": ("dashboard.test", 443),
        })
        try:
            await runtime.hub.start()
            response = await attention_routes.api_attention_events(request)
            iterator = response.body_iterator
            assert await anext(iterator) == b"event: attention:ready\ndata: {}\n\n"
            runtime.hub._fanout("attention:changed", {
                "event_id": "event-1", "attention_id": "recipient-item",
                "source_version": 1,
            })
            frame = await asyncio.wait_for(anext(iterator), timeout=1)
            assert frame.startswith(b"event: attention:changed\n")
            assert b"object_ref" not in frame and APPROVAL_ID.encode() not in frame
            await iterator.aclose()
            assert runtime.hub._subscribers == set()
            monkeypatch.setattr(
                api_auth, "principal_from_request",
                lambda _request: api_auth.ApiPrincipal(
                    api_auth.ApiPrincipalKind.LOCAL_SESSION, "agent",
                ),
            )
            assert (
                await attention_routes.api_attention_events(request)
            ).status_code == 403
        finally:
            await runtime.hub.stop()
            attention_routes.configure_runtime(previous)

        from tools.dashboard import server
        legacy = [r for r in server.routes if r.path == "/api/attention"]
        central = [r for r in server.routes if r.path == "/api/attention/items"]
        assert len(legacy) == len(central) == 1
        assert legacy[0].name == "api_attention"
        assert legacy[0].methods == {"GET", "HEAD"}


def test_decision_retry_after_resolved_projection(route_client):
    client, runtime, producer = route_client
    base = {
        "headers": {"Origin": "https://dashboard.test"},
        "json": {"outcome": "granted", "decision": {"name": "SJC"}},
    }
    first = client.post(
        "/api/attention/items/recipient-item/approval-decision", **base,
    )
    canonical = first.json()
    assert first.status_code == 200 and set(canonical) == {"resolution"}
    assert client.post(
        "/api/attention/items/recipient-item/approval-decision",
        headers=base["headers"],
        json={"outcome": "declined", "decision": {}},
    ).json() == canonical
    runtime.index.publish(producer, {
        "attention_id": "recipient-item", "role": "recipient",
        "state": "resolved", "version": 2, "occurred_at": 111.0,
    })
    resolved = client.post(
        "/api/attention/items/recipient-item/approval-decision",
        headers=base["headers"],
        json={"outcome": "declined", "decision": {}},
    )
    assert resolved.status_code == 200 and resolved.json() == canonical


def test_exact_item_uses_same_join_and_fails_closed():
    runtime, _producer = _route_runtime()
    queried = runtime.index.query().items[0]
    assert runtime.index.get_query_item(queried.attention_id) == queried
    runtime.index.store.items[queried.attention_id]["participant_role"] = "intruder"
    with pytest.raises(AttentionIndexError, match="unavailable"):
        runtime.index.get_query_item(queried.attention_id)


@pytest.mark.asyncio
async def test_private_hub_thread_coalescing_delete_and_shutdown():
    item = AttentionQueryItem(
        attention_id="item-1", payload={"source_version": 1},
        presentation=None, application_label="Test",
        icon_ref="attention.application.test", surface_category="approvals",
        review_renderer_id="approval.test.review",
    )
    hub = attention_routes.PrivateAttentionHub(
        item_resolver=lambda key: item if key == "item-1" else None,
        max_pending=1, subscriber_queue_size=4,
    )
    await hub.start()
    queue, errors = hub.subscribe(), []

    def emit_many():
        try:
            for key in ("item-1", "item-2", "item-3"):
                hub.emit_setting_change(
                    operation="upsert",
                    snapshot={
                        "set_id": ATTENTION_ITEM_SET_ID,
                        "schema_revision": 1, "key": key,
                    },
                    org=None,
                )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=emit_many)
    thread.start()
    thread.join()
    assert await asyncio.wait_for(queue.get(), timeout=1) == (
        "attention:refresh", {"attention_id": None},
    )
    assert errors == []
    hub.emit_setting_change(
        operation="delete",
        snapshot={
            "set_id": ATTENTION_ITEM_SET_ID,
            "schema_revision": 1, "key": "deleted-item",
        },
        org=None,
    )
    assert await asyncio.wait_for(queue.get(), timeout=1) == (
        "attention:refresh", {"attention_id": "deleted-item"},
    )
    await hub.stop()
    hub.emit_setting_change(
        operation="upsert",
        snapshot={
            "set_id": ATTENTION_ITEM_SET_ID,
            "schema_revision": 1, "key": "after-stop",
        },
        org=None,
    )


@pytest.mark.asyncio
async def test_private_hub_scope_filter_and_slow_subscriber_close():
    item = AttentionQueryItem(
        attention_id="item-1", payload={"source_version": 1},
        presentation=None, application_label="Test",
        icon_ref="attention.application.test", surface_category="approvals",
        review_renderer_id="approval.test.review",
    )
    hub = attention_routes.PrivateAttentionHub(
        item_resolver=lambda _key: item, subscriber_queue_size=1,
    )
    await hub.start()
    queue = hub.subscribe()
    for snapshot, org in (
        ({"set_id": ATTENTION_ITEM_SET_ID, "schema_revision": 1, "key": "item-1"}, "other"),
        ({"set_id": ATTENTION_ITEM_SET_ID, "schema_revision": 2, "key": "item-1"}, None),
        ({"set_id": ATTENTION_ITEM_SET_ID, "schema_revision": 1, "key": " bad"}, None),
    ):
        hub.emit_setting_change(operation="upsert", snapshot=snapshot, org=org)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.02)
    hub._fanout("attention:presentation", {"attention_id": "item-1"})
    hub._fanout("attention:changed", {
        "event_id": "event", "attention_id": "item-1", "source_version": 1,
    })
    assert await asyncio.wait_for(queue.get(), timeout=1) is attention_routes._SSE_CLOSE
    assert queue not in hub._subscribers
    await hub.stop()


def test_event_bus_scrub_gap_unrelated_and_restart(tmp_path):
    bus = EventBus()
    bus.broadcast_sync("nav", {"count": 1}, dedup=False)
    bus.broadcast_sync(
        "setting.changed", {"set_id": ATTENTION_ITEM_SET_ID, "key": "private"},
        dedup=False,
    )
    bus.broadcast_sync("dispatch", {"active": []}, dedup=False)
    bus.broadcast_sync(
        "setting.changed", {"set_id": "dashboard.feature_flags", "key": "public"},
        dedup=False,
    )
    malformed = _BufferEntry(5, "setting.changed", "{", 1.0, 1)
    bus._buffer.append(malformed)
    bus._buffer_bytes += malformed.size
    bus._seq = 5
    bus._last["setting.changed"] = "{"
    bus._last_seq["setting.changed"] = 5
    assert attention_routes.scrub_private_cached_events(bus) == 3
    assert bus._seq == 5 and "setting.changed" not in bus._last
    events, complete = bus.replay(1, 4)
    assert [row["seq"] for row in events] == [1, 3, 4]
    assert complete is False
    assert [
        row for row in events if row["topic"] == "setting.changed"
    ][0]["data"]["set_id"] == "dashboard.feature_flags"
    snapshot = tmp_path / "event-bus.json"
    bus.snapshot(snapshot)
    restored = EventBus()
    assert restored.restore(snapshot) is True
    assert attention_routes.scrub_private_cached_events(restored) == 0


def test_server_hook_diverts_all_private_sets(monkeypatch):
    from tools.dashboard import server

    bus, delivered = EventBus(), []
    monkeypatch.setattr(server, "event_bus", bus)
    monkeypatch.setattr(server, "_invalidate_setting_caches", lambda *a, **k: None)
    monkeypatch.setattr(
        attention_routes, "emit_setting_change",
        lambda **kwargs: delivered.append(kwargs),
    )
    for set_id in (
        APPROVAL_REQUEST_SET_ID, APPROVAL_RESOLUTION_SET_ID,
        ATTENTION_ITEM_SET_ID, ATTENTION_PRESENTATION_SET_ID,
        ATTENTION_DELIVERY_SET_ID,
    ):
        private = {
            "set_id": set_id, "schema_revision": 99, "key": "private-key",
            "publication_state": "raw", "deprecated": 0,
        }
        server._settings_emit_hook(
            operation="upsert", snapshot=private, org="wrong-org",
        )
    assert len(delivered) == 5 and bus.all_cached_topics() == []
    public = dict(private, set_id="dashboard.feature_flags", schema_revision=1)
    server._settings_emit_hook(operation="upsert", snapshot=public, org=None)
    assert bus.all_cached_topics() == ["setting.changed"]
    replay, complete = bus.replay(1, 1)
    assert complete is True
    assert replay[0]["data"]["set_id"] == "dashboard.feature_flags"
