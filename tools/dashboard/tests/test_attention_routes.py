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
from tools.dashboard.event_bus import EventBus
from tools.graph.schemas.central_attention import (
    APPROVAL_REQUEST_SET_ID, APPROVAL_RESOLUTION_SET_ID,
    ATTENTION_DELIVERY_SET_ID,
)

ROOT = "a" * 64
APPROVAL_ID = "approval-1234567890"
OTHER_ID = "approval-0987654321"


def test_requesting_session_link_is_resolved_from_verified_identity_not_grant_id(monkeypatch):
    from tools.dashboard.approval_service import canonical_session_requester_id
    from tools.dashboard.dao import dashboard_db
    from tools.dashboard import org_identity

    session = "auto-0911-214710"
    monkeypatch.setattr(dashboard_db, "get_session", lambda name: {
        "tmux_name": session, "project": "autonomy-codex",
    } if name == session else None)
    monkeypatch.setattr(org_identity, "resolve_session_org", lambda row: {"slug": "autonomy"})
    identity = canonical_session_requester_id(api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION, subject=session, org="autonomy",
    ))
    requester = {"kind": "session", "id": identity, "label": session + " · Review Settings"}
    assert attention_routes.session_requester_view(requester) == {
        "href": "/session/autonomy-codex/auto-0911-214710", "byline": "autonomy-codex",
    }
    assert identity not in attention_routes.session_requester_view(requester)["href"]
    assert attention_routes.session_requester_view({**requester, "id": "unmatched"}) == {}


def test_production_dashboard_access_review_projects_the_existing_application_result(monkeypatch):
    seen = []
    expected = {"approved": True, "execution": {"ok": True}}

    def project(_consumer, status):
        seen.append(status)
        return expected

    monkeypatch.setattr(
        attention_routes.dashboard_access_central.DashboardAccessResultConsumer,
        "project", project,
    )
    runtime = attention_routes.build_production_runtime()
    status = object()
    result = runtime.operator_result_projectors[
        attention_routes.dashboard_access_central.KIND
    ](status)
    assert seen == [status]
    assert result == expected


def _route_runtime(*, expiry=None, clock=None):
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
        request_expiry_policy=expiry or ApprovalExpiryPolicy(ExpiryMode.NEVER),
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
    ids = iter([APPROVAL_ID, OTHER_ID])
    approvals = ApprovalService(
        registry=registry, store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: ROOT,
        session_label_resolver=lambda _subject: "Requester session",
        id_factory=lambda: next(ids), clock=clock or (lambda: 100.0),
    )
    approvals.create_from_principal(
        "test_kind",
        api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.LOCAL_SESSION, "session-1",
        ),
        {},
    )
    return attention_routes.AttentionRouteRuntime(
        approvals=approvals,
        hub=attention_routes.PrivateAttentionHub(),
        inbox_texts={"test_kind": lambda status: ("Approval requested", "Review the request")},
    )


def _operator(monkeypatch):
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


def test_session_pending_central_matches_requester_decider_and_unresolved_state(monkeypatch):
    from tools.dashboard import org_identity
    runtime = _route_runtime()
    previous = attention_routes.configure_runtime(runtime)
    monkeypatch.setattr(org_identity, "resolve_session_org", lambda row: {"slug": "autonomy"})
    try:
        session = {"tmux_name": "session-1", "project": "workspace"}
        actor = HumanApprovalActor._verified(ROOT)
        assert attention_routes.pending_session_approval(session, actor) == {
            "id": APPROVAL_ID, "kind": "test_kind", "attention_id": APPROVAL_ID,
        }
        assert attention_routes.pending_session_approval({**session, "tmux_name": "other"}, actor) is None
        assert attention_routes.pending_session_approval(session, HumanApprovalActor._verified("b" * 64)) is None
        runtime.approvals.decide(APPROVAL_ID, actor, outcome="declined", decision={})
        assert attention_routes.pending_session_approval(session, actor) is None
    finally:
        attention_routes.configure_runtime(previous)


@pytest.fixture
def route_client(monkeypatch):
    runtime = _route_runtime()
    previous = attention_routes.configure_runtime(runtime)
    _operator(monkeypatch)
    try:
        with TestClient(
            Starlette(routes=attention_routes.routes),
            base_url="https://dashboard.test",
        ) as client:
            yield client, runtime
    finally:
        attention_routes.configure_runtime(previous)


ORIGIN = {"Origin": "https://dashboard.test"}


def _decide(client, outcome="granted", decision=None, approval_id=APPROVAL_ID):
    return client.post(
        f"/api/attention/items/{approval_id}/approval-decision",
        headers=ORIGIN, json={"outcome": outcome, "decision": decision or {}},
    )


class TestAttentionOperatorAPI:
    def test_safe_list_and_detail(self, route_client):
        client, runtime = route_client
        response = client.get("/api/attention/items")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["counts"]["total_needs_attention"] == 1
        assert body["items"] == [{
            "attention_id": APPROVAL_ID,
            "application": {"scope": "test_app", "label": "test_app", "icon_ref": ""},
            "category": "approvals",
            "participant_role": "recipient",
            "attention_state": "needs_attention",
            "title": "Approval requested",
            "summary": "Review the request",
            "counterparty_ref": None,
            "occurred_at": 100.0,
            "open": {"mode": "registered_renderer", "renderer_id": "approval.test_kind.review"},
        }]
        for forbidden in ('"object_ref"', '"request":', '"staged"', '"result_ref"', ROOT):
            assert forbidden not in response.text
        detail = client.get(f"/api/attention/items/{APPROVAL_ID}")
        assert detail.status_code == 200
        review = detail.json()["review"]
        assert review["safe_review"] == {"summary": "Review machine"}
        assert review["requester"] == {"kind": "session", "label": "Requester session"}
        assert review["actions"] == ["granted", "declined"]
        assert review["resolution"] is None
        assert client.get("/api/attention/items/central-unknown-0001").status_code == 404

    def test_list_and_counts_read_the_approval_rows(self, route_client):
        client, runtime = route_client
        runtime.approvals.create_from_principal(
            "test_kind",
            api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, "session-2"),
            {},
        )
        body = client.get("/api/attention/items").json()
        # Newest first; equal times fall back to the approval id.
        assert [item["attention_id"] for item in body["items"]] == [OTHER_ID, APPROVAL_ID]
        assert body["counts"] == {
            "total_needs_attention": 2,
            "categories": {"apps": 0, "comms": 0, "approvals": 2},
            "applications": {"test_app": {"needs_attention": 2, "waiting": 0}},
        }
        assert _decide(client, "declined", approval_id=OTHER_ID).status_code == 200
        body = client.get("/api/attention/items").json()
        states = {item["attention_id"]: item["attention_state"] for item in body["items"]}
        assert states == {APPROVAL_ID: "needs_attention", OTHER_ID: "resolved"}
        assert body["counts"]["total_needs_attention"] == 1
        assert body["counts"]["applications"] == {"test_app": {"needs_attention": 1, "waiting": 0}}

    def test_operator_application_result_is_joined_and_bounded(self, route_client):
        client, runtime = route_client
        runtime.operator_result_projectors = {
            "test_kind": lambda status: {
                "approved": status.resolution is not None,
                "execution": {"ok": True},
                "url": "https://registry.example/l/" + "5" * 32,
            },
        }
        detail = client.get(f"/api/attention/items/{APPROVAL_ID}")
        assert detail.status_code == 200
        assert detail.headers["cache-control"] == "no-store"
        assert detail.json()["review"]["application_result"] == {
            "approved": False,
            "execution": {"ok": True},
            "url": "https://registry.example/l/" + "5" * 32,
        }

        runtime.operator_result_projectors = {
            "test_kind": lambda _status: {"oversized": "x" * (64 * 1024)},
        }
        refused = client.get(f"/api/attention/items/{APPROVAL_ID}")
        assert refused.status_code == 503
        assert refused.headers["cache-control"] == "no-store"
        assert refused.json() == {"error": "unavailable"}

    def test_human_methods_selectors_and_principal_matrix(
        self, route_client, monkeypatch,
    ):
        client, runtime = route_client

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
                f"/api/attention/items/{APPROVAL_ID}",
            ).json()["review"]["actions"] == ["granted", "declined"]
        actor_for("approval")
        assert _decide(client).status_code == 401
        actor_for("password", "b" * 64)
        assert _decide(client).status_code == 404
        actor_for("password")
        assert client.post(
            f"/api/attention/items/{APPROVAL_ID}/approval-decision",
            headers=ORIGIN,
            json={"outcome": "granted", "decision": {}, "org": "forged"},
        ).status_code == 422
        assert runtime.approvals.status(APPROVAL_ID).state == "open"
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

    def test_decided_disabled_and_storage_failure(self, route_client):
        client, runtime = route_client
        runtime.approvals.decide(
            APPROVAL_ID, HumanApprovalActor._verified(ROOT),
            outcome="granted", decision={},
        )
        decided = client.get(f"/api/attention/items/{APPROVAL_ID}")
        assert decided.status_code == 200
        assert decided.json()["review"]["resolution"]["outcome"] == "granted"
        assert decided.json()["review"]["actions"] == []
        assert decided.json()["item"]["attention_state"] == "resolved"

        runtime.inbox_texts = {}
        assert client.get(f"/api/attention/items/{APPROVAL_ID}").status_code == 404
        assert client.get("/api/attention/items").json()["items"] == []

        fresh = _route_runtime()
        old = attention_routes.configure_runtime(fresh)
        try:
            fresh.approvals.store.get_request = lambda _id: (_ for _ in ()).throw(
                RuntimeError("unavailable")
            )
            fresh.approvals.store.list_requests = lambda: (_ for _ in ()).throw(
                RuntimeError("unavailable")
            )
            assert client.get(f"/api/attention/items/{APPROVAL_ID}").status_code == 503
            assert client.get("/api/attention/items").status_code == 503
        finally:
            attention_routes.configure_runtime(old)

    def test_deadline_retry_concurrency_and_bounded_bodies(self, route_client):
        client, runtime = route_client
        runtime.approvals.store._requests[APPROVAL_ID].payload["expires_at"] = 100.0
        first = _decide(client)
        second = _decide(client)
        assert first.status_code == second.status_code == 409
        assert first.json() == second.json()
        assert first.json()["resolution"] == {"outcome": "expired", "resolved_at": 100.0}
        assert runtime.approvals.store.get_resolution(APPROVAL_ID) is None

        runtime = _route_runtime()
        previous = attention_routes.configure_runtime(runtime)
        entered, release = threading.Event(), threading.Event()
        original_append = runtime.approvals.store.append_resolution

        def blocked_append(approval_id, payload):
            entered.set()
            assert release.wait(timeout=3)
            return original_append(approval_id, payload)

        runtime.approvals.store.append_resolution = blocked_append

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                winning = pool.submit(_decide, client, "granted")
                assert entered.wait(timeout=3)
                losing = pool.submit(_decide, client, "declined")
                release.set()
                responses = (
                    winning.result(timeout=5), losing.result(timeout=5),
                )
            assert [row.status_code for row in responses] == [200, 200]
            assert responses[0].json() == responses[1].json()
            assert set(responses[0].json()) == {"resolution"}
        finally:
            attention_routes.configure_runtime(previous)

        headers = {**ORIGIN, "Content-Type": "application/json"}
        path = f"/api/attention/items/{APPROVAL_ID}/approval-decision"
        assert client.post(
            path, headers=headers,
            content='{"outcome":"granted","decision":{"value":NaN}}',
        ).status_code == 422
        assert client.post(
            path, headers=headers,
            content='{"outcome":"granted","decision":{"padding":"' + ("x" * 33_000) + '"}}',
        ).status_code == 422

    def test_query_validation_origin_and_compatibility(
        self, route_client, monkeypatch,
    ):
        client, _runtime = route_client
        for query in ("limit=1", "cursor=x", "unknown=x", "org=autonomy"):
            assert client.get(f"/api/attention/items?{query}").status_code == 400
        assert client.post(
            f"/api/attention/items/{APPROVAL_ID}/approval-decision",
            headers={"Origin": "https://foreign.test"},
            json={"outcome": "granted", "decision": {}},
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

    @pytest.mark.asyncio
    async def test_private_sse_frames_auth_and_legacy_route(self, monkeypatch):
        runtime = _route_runtime()
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
            runtime.hub.emit_refresh()
            frame = await asyncio.wait_for(anext(iterator), timeout=1)
            assert frame == b"event: attention:refresh\ndata: {}\n\n"
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


def test_an_undecided_approval_past_its_deadline_lists_as_resolved_without_a_row(monkeypatch):
    now = [100.0]
    runtime = _route_runtime(
        expiry=ApprovalExpiryPolicy(mode=ExpiryMode.FIXED, fixed_seconds=10),
        clock=lambda: now[0],
    )
    previous = attention_routes.configure_runtime(runtime)
    _operator(monkeypatch)
    try:
        with TestClient(
            Starlette(routes=attention_routes.routes), base_url="https://dashboard.test",
        ) as client:
            assert client.get("/api/attention/items").json()["counts"]["total_needs_attention"] == 1
            now[0] = 110.0
            body = client.get("/api/attention/items").json()
            assert body["counts"]["total_needs_attention"] == 0
            assert body["items"][0]["attention_state"] == "resolved"
            assert body["items"][0]["occurred_at"] == 110.0
            review = client.get(f"/api/attention/items/{APPROVAL_ID}").json()["review"]
            assert review["resolution"] == {"outcome": "expired", "resolved_at": 110.0}
            assert review["actions"] == []
            assert runtime.approvals.store.get_resolution(APPROVAL_ID) is None
    finally:
        attention_routes.configure_runtime(previous)


def test_production_runtime_has_inbox_text_for_every_live_kind():
    production = attention_routes.build_production_runtime()
    live = {kind for kind, row in production.approvals.registry.kinds.items() if row.runtime is not None}
    assert set(production.inbox_texts) == live


def test_decision_retry_after_resolution(route_client):
    client, runtime = route_client
    first = _decide(client, decision={"name": "SJC"})
    canonical = first.json()
    assert first.status_code == 200 and set(canonical) == {"resolution"}
    assert _decide(client, "declined").json() == canonical
    assert runtime.approvals.store.resolution_count(APPROVAL_ID) == 1


@pytest.mark.asyncio
async def test_hub_refreshes_pages_on_approval_row_changes_from_any_thread():
    hub = attention_routes.PrivateAttentionHub(subscriber_queue_size=4)
    await hub.start()
    queue = hub.subscribe()
    errors = []

    def emit():
        try:
            hub.emit_setting_change(
                operation="upsert",
                snapshot={"set_id": APPROVAL_REQUEST_SET_ID, "schema_revision": 1, "key": "k"},
                org=None,
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=emit)
    thread.start()
    thread.join()
    assert await asyncio.wait_for(queue.get(), timeout=1) == ("attention:refresh", {})
    hub.emit_setting_change(
        operation="upsert",
        snapshot={"set_id": APPROVAL_RESOLUTION_SET_ID, "schema_revision": 1, "key": "k"},
        org=None,
    )
    assert await asyncio.wait_for(queue.get(), timeout=1) == ("attention:refresh", {})
    for snapshot, org in (
        ({"set_id": APPROVAL_REQUEST_SET_ID, "schema_revision": 1, "key": "k"}, "other"),
        ({"set_id": ATTENTION_DELIVERY_SET_ID, "schema_revision": 1, "key": "k"}, None),
        ({"set_id": "dashboard.feature_flags", "schema_revision": 1, "key": "k"}, None),
    ):
        hub.emit_setting_change(operation="upsert", snapshot=snapshot, org=org)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.05)
    assert errors == []
    await hub.stop()
    assert queue.get_nowait() is attention_routes._SSE_CLOSE
    hub.emit_refresh()  # after stop: nothing, and no error


@pytest.mark.asyncio
async def test_hub_closes_a_slow_subscriber():
    hub = attention_routes.PrivateAttentionHub(subscriber_queue_size=1)
    await hub.start()
    queue = hub.subscribe()
    hub._fanout("attention:refresh", {})
    hub._fanout("attention:refresh", {})
    assert await asyncio.wait_for(queue.get(), timeout=1) is attention_routes._SSE_CLOSE
    assert queue not in hub._subscribers
    await hub.stop()


@pytest.mark.asyncio
async def test_synced_approval_rows_refresh_pages(monkeypatch):
    runtime = _route_runtime()
    offered = []
    runtime.approval_reconciler = type("Reconciler", (), {
        "offer_synced": lambda self, *, addresses: offered.append(addresses),
    })()
    previous = attention_routes.configure_runtime(runtime)
    try:
        await runtime.hub.start()
        queue = runtime.hub.subscribe()
        address = type("Address", (), {"set_id": APPROVAL_RESOLUTION_SET_ID})()
        attention_routes.emit_personal_sync_change(addresses=[address])
        assert await asyncio.wait_for(queue.get(), timeout=1) == ("attention:refresh", {})
        assert offered == [(address,)]
        attention_routes.emit_personal_sync_change(
            addresses=[type("Address", (), {"set_id": "dashboard.feature_flags"})()],
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)
    finally:
        await runtime.hub.stop()
        attention_routes.configure_runtime(previous)


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
        ATTENTION_DELIVERY_SET_ID,
    ):
        private = {
            "set_id": set_id, "schema_revision": 99, "key": "private-key",
            "publication_state": "raw", "deprecated": 0,
        }
        server._settings_emit_hook(
            operation="upsert", snapshot=private, org="wrong-org",
        )
    assert len(delivered) == 3 and bus.all_cached_topics() == []
    public = dict(private, set_id="dashboard.feature_flags", schema_revision=1)
    server._settings_emit_hook(operation="upsert", snapshot=public, org=None)
    assert bus.all_cached_topics() == ["setting.changed"]
    replay, complete = bus.replay(1, 1)
    assert complete is True
    assert replay[0]["data"]["set_id"] == "dashboard.feature_flags"


def test_session_requester_label_names_the_session_and_its_working_title(monkeypatch):
    """An approval must say WHICH session is asking. The label is the tmux
    name plus the working title the session set; a session with no title
    is still named; an unreadable dashboard DB never hides the name."""
    from tools.dashboard.dao import dashboard_db

    rows = {
        "auto-0910-155648": {"tmux_name": "auto-0910-155648",
                             "label": "auto-nh1po: serve routing"},
        "auto-0910-000001": {"tmux_name": "auto-0910-000001", "label": ""},
    }
    monkeypatch.setattr(dashboard_db, "get_session", lambda name: rows.get(name))
    assert attention_routes.session_requester_label("auto-0910-155648") == (
        "auto-0910-155648 · auto-nh1po: serve routing"
    )
    assert attention_routes.session_requester_label("auto-0910-000001") == "auto-0910-000001"
    assert attention_routes.session_requester_label("auto-unknown") == "auto-unknown"
    assert attention_routes.session_requester_label("") is None

    def boom(_name):
        raise RuntimeError("dashboard.db is locked")

    monkeypatch.setattr(dashboard_db, "get_session", boom)
    assert attention_routes.session_requester_label("auto-0910-155648") == "auto-0910-155648"


def test_production_runtime_resolves_requester_labels_through_the_session_registry(monkeypatch):
    from tools.dashboard.dao import dashboard_db

    monkeypatch.setattr(
        dashboard_db, "get_session",
        lambda name: {"tmux_name": name, "label": "Voice capsule"},
    )
    monkeypatch.setattr(
        attention_routes.unlock_routes, "gate_enforced", lambda: False,
    )
    production = attention_routes.build_production_runtime()
    label = production.approvals._session_label("auto-0910-155648")
    assert label == "auto-0910-155648 · Voice capsule"
